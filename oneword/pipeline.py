"""The closed loop: one word in, finished episodes out.

    word ─► bible ─► episode beats ─► shots ─► prompts
                                          │
                                          ├─► vendor.generate ──┐
                                          │                     │
                                          │   ┌── audit ◄───────┘
                                          │   │
                                          │   ├─ blockers? regenerate ONLY those
                                          │   │  (at most two rounds, never the
                                          │   │   whole episode)
                                          │   ▼
                                          └─► speech + subtitles + assembly
                                                      │
                                                      ▼
                                              episode-NN.mp4

The bible is written once and reused, so episode 9's prompts contain the same
locked character and location strings as episode 1's.  That is the part a
one-prompt pipeline cannot do: it has nowhere to put the facts.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import BlockerFinding, GeneratedClip

from . import assemble, cast as cast_mod, chain, ledger, music as music_mod
from .audit import RULE_TRIAGE, RuleTriageAuditor
from .bible import IDENTITY_CRITICAL_BEATS, BEATS, SeriesBible
from .voice import SilentVoice, audio_duration, write_srt

MAX_REPAIR_ROUNDS = 2
REPORT_VERSION = "series-episode-1"


class PipelineError(RuntimeError):
    pass


# ──────────────────────────────────────────────────────────────────────
# Shots and prompts — deterministic, no model involved
# ──────────────────────────────────────────────────────────────────────


def build_shots(bible: SeriesBible, episode_no: int, *, clip_seconds: int = 5) -> list[dict[str, Any]]:
    episode = bible.episode(episode_no)
    shots = []
    for index, beat in enumerate(episode["beats"], start=1):
        beat_name = BEATS[min(index - 1, len(BEATS) - 1)]
        location_id = beat["location_id"]
        shots.append(
            {
                "shot_id": str(index),
                "episode": int(episode_no),
                "beat": beat_name,
                "location_id": location_id,
                "location_name": bible.location(location_id).get("name", location_id),
                "character_ids": list(beat.get("character_ids") or []),
                "action": beat.get("action", ""),
                "camera": beat.get("camera", ""),
                "line": beat.get("line", ""),
                "duration_sec": clip_seconds,
                "identity_critical": beat_name in IDENTITY_CRITICAL_BEATS,
                "model_tier": "primary" if beat_name in IDENTITY_CRITICAL_BEATS else "economy",
            }
        )
    return shots


# A model asked to include dialogue sometimes writes the words on screen
# instead of speaking them. These are appended to whatever the bible already
# forbids, only on shots that carry a spoken line.
DIALOGUE_NEGATIVE = (
    "subtitles, captions, burned-in text, text overlay, speech bubble, "
    "karaoke text, on-screen writing of the dialogue"
)


def _is_cjk(text: str) -> bool:
    return any("一" <= character <= "鿿" for character in text)


def dialogue_block(bible: SeriesBible, shot: dict[str, Any]) -> str:
    """Ask the model to perform the line rather than illustrate it.

    A line performed by the video model is lip-synced, in character and in the
    same acoustic space as the shot. No external TTS can match that, because it
    is reading over a performance instead of being one.

    The phrasing is deliberate: it names the speaker, says the words are spoken
    aloud, and says what must not appear. "Include this dialogue" on its own is
    the phrasing that gets you the sentence printed across the frame.
    """

    line = (shot.get("line") or "").strip()
    if not line:
        return ""
    speaker_id = (shot.get("character_ids") or [None])[0]
    try:
        speaker = bible.character(speaker_id).get("name", "the character")
    except (KeyError, TypeError):
        speaker = "the character"
    language = "Mandarin Chinese" if _is_cjk(line) else "English"
    return (
        f"[DIALOGUE] {speaker} speaks this line aloud, in {language}, "
        f"lip-synced and audible, as the only speech in the shot:\n"
        f"“{line}”\n"
        "The words are heard, never shown. No text appears anywhere in frame."
    )


def compose_prompt(
    bible: SeriesBible,
    shot: dict[str, Any],
    *,
    spoken: bool = False,
) -> str:
    """Locked facts first, then the action.  Byte-stable for the same shot.

    `spoken` is set when the vendor generates audio, and adds the line as
    dialogue for the model to perform. It is off for a vendor that returns
    silent clips, where a line in the prompt buys nothing and risks the model
    drawing the words instead.
    """

    parts = [
        bible.locked_block(shot["location_id"], shot["character_ids"]),
        f"[ACTION] {shot['action']}",
        f"[CAMERA] {shot['camera']}",
    ]
    dialogue = dialogue_block(bible, shot) if spoken else ""
    if dialogue:
        parts.append(dialogue)
    rules = bible.rule_lines()
    if rules:
        parts.append("[CONTINUITY — MUST HOLD]\n" + "\n".join(f"MUST: {line}" for line in rules))
    negative = bible.negative_prompt()
    if dialogue:
        negative = f"{negative}, {DIALOGUE_NEGATIVE}" if negative else DIALOGUE_NEGATIVE
    if negative:
        parts.append(f"[NEGATIVE] {negative}")
    return "\n\n".join(part for part in parts if part.strip())


def repair_prompt(base: str, findings: list[BlockerFinding]) -> str:
    lines = [base, "", "[REPAIR — CHANGE ONLY WHAT IS LISTED, KEEP EVERYTHING ELSE IDENTICAL]"]
    lines.extend(f"{finding.rule_id}: {finding.minimal_fix}" for finding in findings)
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────


def _speech_for(shot: dict[str, Any], bible: SeriesBible, engine, workdir: Path) -> Path | None:
    line = (shot.get("line") or "").strip()
    if not line:
        return None
    cid = (shot.get("character_ids") or ["C1"])[0]
    try:
        character = bible.character(cid)
    except KeyError:
        character = {}
    target = workdir / f"shot-{shot['shot_id']}.wav"
    try:
        return engine.synthesize(line, character.get("voice", {}), cid, target)
    except Exception:  # noqa: BLE001 — a missing voice must not lose the picture
        return None


def run_episode(
    bible: SeriesBible,
    episode_no: int,
    output_dir: str | Path,
    *,
    vendor,
    auditor=None,
    voice_engine=None,
    clip_seconds: int = 5,
    max_repair_rounds: int = MAX_REPAIR_ROUNDS,
    audio_mode: str = "mix",
    dialogue: str = "auto",
    chaining: str = "auto",
    music: str | Path | None = None,
    music_db: float = music_mod.DEFAULT_LEVEL_DB,
    resume: bool = True,
    portraits: cast_mod.CastPortraits | None = None,
    use_portraits: bool = True,
) -> dict[str, Any]:
    started = time.time()
    root = Path(output_dir)
    clips_dir = root / "clips"
    work_dir = root / "work"
    for directory in (clips_dir, work_dir):
        directory.mkdir(parents=True, exist_ok=True)

    auditor = auditor or RuleTriageAuditor()
    voice_engine = voice_engine or SilentVoice()

    # A storyboard card has no pixels worth auditing, and letting a vision model
    # grade one would produce a visual verdict about something no model shot.
    if not getattr(vendor, "generative", False):
        auditor = RuleTriageAuditor()

    # "auto" means: let the model perform the lines exactly when it can make
    # sound at all. A silent vendor gets no dialogue in its prompts.
    if dialogue == "auto":
        spoken = bool(getattr(vendor, "speaks", False))
    else:
        spoken = dialogue == "on"

    shots = build_shots(bible, episode_no, clip_seconds=clip_seconds)
    prompts = {
        shot["shot_id"]: compose_prompt(bible, shot, spoken=spoken) for shot in shots
    }

    generation_events: list[dict[str, Any]] = []
    audit_events: list[dict[str, Any]] = []
    audit_failures: list[dict[str, Any]] = []
    current: dict[str, GeneratedClip] = {}
    attempts: dict[str, int] = {shot["shot_id"]: 0 for shot in shots}

    pending = [shot["shot_id"] for shot in shots]
    by_id = {shot["shot_id"]: shot for shot in shots}

    # Consecutive shots in one location are continued from the previous shot's
    # last frame, so the cut between them is physical rather than two guesses
    # at the same room. Off for a vendor that cannot take a first frame.
    # Words describe a type, not a person, so a text-only shot casts a new face
    # every time. A portrait sent with every appearance is what makes shot 9
    # the same person as shot 1.
    casting = (
        portraits
        if portraits is not None and use_portraits
        and getattr(vendor, "accepts_reference_images", False)
        else None
    )
    style_name = bible.style_name

    chain_links = (
        chain.plan(shots)
        if chaining == "auto" and getattr(vendor, "accepts_first_frame", False)
        else {}
    )
    # A shot whose last audit still said REGENERATE after the repair cap is a
    # shot the tool did not fix.  The report says so rather than rounding up.
    unresolved: set[str] = set()
    reused: list[str] = []
    saved_cny = 0.0

    for round_index in range(max_repair_rounds + 1):
        if not pending:
            break
        for shot_id in pending:
            shot = by_id[shot_id]
            source_id = chain_links.get(shot_id)

            # Already bought? A run that died at shot 8 left seven paid clips
            # on disk; buying them again is the most expensive bug this tool
            # could have. Only an exact prompt and vendor match counts.
            existing = (
                ledger.find_reusable(clips_dir, shot_id, prompts[shot_id], vendor.name)
                if resume and round_index == 0
                else None
            )
            if existing is not None:
                attempts[shot_id] = existing.attempt
                clip = GeneratedClip(
                    shot_id=shot_id, attempt=existing.attempt, provider=vendor.name,
                    path=existing.path, prompt=prompts[shot_id],
                    chain_dropped=existing.chain_dropped,
                )
                current[shot_id] = clip
                reused.append(shot_id)
                saved_cny += existing.cost_cny
                generation_events.append(
                    {
                        "round": round_index, "shot_id": shot_id,
                        "attempt": existing.attempt, "provider": vendor.name,
                        "file": existing.path.name, "model_tier": shot["model_tier"],
                        "continues_shot": existing.continues_shot,
                        "chain_dropped": existing.chain_dropped,
                        "reused": True,
                    }
                )
                continue

            shot.pop("first_frame", None)
            if source_id and source_id in current:
                frame = chain.last_frame(
                    current[source_id].path, work_dir / f"chain-from-{source_id}.jpg"
                )
                if frame:
                    shot["first_frame"] = str(frame)
            shot.pop("reference_images", None)
            if casting is not None:
                faces = casting.for_shot(shot.get("character_ids") or [])
                if faces:
                    shot["reference_images"] = [str(path) for path in faces]

            attempts[shot_id] += 1
            attempt = attempts[shot_id]
            prompt = prompts[shot_id]
            target = clips_dir / f"shot-{int(shot_id):02d}-take-{attempt:02d}.mp4"
            clip = vendor.generate(shot, prompt, attempt, target)
            current[shot_id] = clip
            # A shot with one person in it can define that person's face. A shot
            # with two cannot — there would be no way to say which face was
            # whose — so those are never adopted from.
            solo = list(shot.get("character_ids") or [])
            if casting is not None and len(solo) == 1:
                casting.adopt_from_clip(
                    solo[0], clip.path, style=style_name,
                    from_shot=f"ep{episode_no}-shot{shot_id}", workdir=work_dir,
                )

            ledger.record(
                target,
                shot_id=shot_id, attempt=attempt, prompt=prompt, provider=vendor.name,
                cost_cny=getattr(vendor, "unit_cost", None),
                chain_dropped=clip.chain_dropped,
                continues_shot=source_id if shot.get("first_frame") and not clip.chain_dropped else None,
            )
            generation_events.append(
                {
                    "round": round_index,
                    "shot_id": shot_id,
                    "attempt": attempt,
                    "provider": vendor.name,
                    "file": target.name,
                    "model_tier": shot["model_tier"],
                    "continues_shot": (
                        source_id if shot.get("first_frame") and not clip.chain_dropped else None
                    ),
                    "chain_dropped": clip.chain_dropped,
                    "references_dropped": clip.references_dropped,
                }
            )

        blockers: list[str] = []
        for shot_id in pending:
            # The audit is an improvement, like chaining and the portraits, and
            # it runs AFTER every clip in this episode has been paid for. An
            # exception here threw away the assembly of work already bought —
            # which is the most expensive possible place to crash, and the one
            # place this package had left unguarded. A broken audit costs the
            # audit, never the episode.
            try:
                findings = auditor.audit(current[shot_id], by_id[shot_id], bible)
            except Exception as exc:  # noqa: BLE001 — degrades, never destroys
                audit_failures.append(
                    {"shot_id": shot_id, "reason": f"{type(exc).__name__}: {exc}"[:300]}
                )
                audit_events.append(
                    {
                        "round": round_index,
                        "shot_id": shot_id,
                        # Not PASS. Nobody looked, and an unearned pass is the
                        # one thing this report may never print.
                        "decision": "NOT AUDITED",
                        "evidence_source": RULE_TRIAGE,
                        "findings": [],
                        "error": f"{type(exc).__name__}: {exc}"[:300],
                    }
                )
                continue
            hard = [finding for finding in findings if finding.severity == "regenerate"]
            decision = "REGENERATE" if hard else ("LOCAL FIX" if findings else "PASS")
            audit_events.append(
                {
                    "round": round_index,
                    "shot_id": shot_id,
                    "decision": decision,
                    "evidence_source": getattr(auditor, "last_evidence", RULE_TRIAGE),
                    "findings": [
                        {
                            "rule_id": finding.rule_id,
                            "evidence": finding.evidence,
                            "minimal_fix": finding.minimal_fix,
                            "severity": finding.severity,
                        }
                        for finding in findings
                    ],
                }
            )
            if hard:
                unresolved.add(shot_id)
                if round_index < max_repair_rounds:
                    prompts[shot_id] = repair_prompt(prompts[shot_id], hard)
                    blockers.append(shot_id)
            else:
                unresolved.discard(shot_id)
        pending = blockers

    # ---- speech, subtitles, assembly --------------------------------

    clip_specs = []
    narrated = 0
    for shot in shots:
        clip = current[shot["shot_id"]]
        # A clip that already speaks does not need a narrator reading the same
        # line over the top of it. Asking first is cheaper than a TTS call and
        # much better than the double-dialogue it avoids.
        #
        # Ask the CLIP, not the mode. `--audio keep` means "the clip's own
        # voice wins" — and where there is no voice there is nothing to keep,
        # so a silent clip still gets narrated. Treating the mode as the answer
        # made `keep` produce a completely silent film whenever the vendor
        # returned silent clips, which is what SEEDANCE_AUDIO=0 does by
        # default. `assemble.normalise` has always documented the rule this
        # restores: a clip with no audio behaves the same under every mode.
        speaks_for_itself = assemble.has_audio(clip.path)
        speech = None if speaks_for_itself else _speech_for(shot, bible, voice_engine, work_dir)
        narrated += 1 if speech else 0
        duration = float(shot["duration_sec"])
        if speech:
            # Never let a line get cut off: stretch the shot if the read is longer.
            duration = max(duration, audio_duration(speech) + 0.9)
        clip_specs.append(
            {
                "shot_id": f"shot-{int(shot['shot_id']):02d}",
                "path": clip.path,
                "duration": round(duration, 2),
                "line": shot.get("line", ""),
                "speech": speech,
            }
        )

    built = assemble.build_episode(
        clip_specs, root, stem=f"episode-{int(episode_no):02d}", mode=audio_mode
    )

    # The score goes on after the cut, never per shot: a bed that restarts at
    # every cut is what makes an edit sound like a slideshow.
    scored: dict[str, Any] | None = None
    if music:
        try:
            result = music_mod.underlay(
                built["video"], Path(music),
                work_dir / f"scored-{int(episode_no):02d}.mp4",
                level_db=music_db,
            )
            result.path.replace(built["video"])
            scored = {
                "track": Path(music).name,
                "level_db": result.level_db,
                "ducked_under_dialogue": result.ducked,
            }
        except music_mod.MusicError as exc:
            # A missing or unreadable track loses the score, never the episode.
            scored = {"track": Path(music).name, "error": str(exc)[:200]}

    repaired = sorted(
        {event["shot_id"] for event in generation_events if event["round"] > 0}, key=int
    )
    report = {
        "report_version": REPORT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seed_word": bible.word,
        "series_title": bible.title,
        "episode": int(episode_no),
        "episode_title": bible.episode(episode_no).get("title", ""),
        "logline": bible.episode(episode_no).get("logline", ""),
        "vendor": vendor.name,
        "vendor_is_generative": bool(getattr(vendor, "generative", False)),
        "voice_engine": getattr(voice_engine, "name", "silent"),
        "audio_mode": audio_mode,
        "dialogue_performed_by_model": spoken,
        "music": scored,
        "chain_links": chain_links,
        "stale_chains": chain.stale_links(chain_links, generation_events)
        + [
            {
                "shot_id": shot_id,
                "continues": source,
                "reason": (
                    f"shot {shot_id} was reused from an earlier run but shot {source} "
                    "was generated again, so it continues a take that is no longer "
                    "in the episode"
                ),
            }
            for shot_id, source in chain_links.items()
            if shot_id in reused and source not in reused
        ],
        "dropped_chains": [
            {"shot_id": event["shot_id"], "reason": event["chain_dropped"]}
            for event in generation_events
            if event.get("chain_dropped")
        ],
        "dropped_portraits": [
            {"shot_id": event["shot_id"], "reason": event["references_dropped"]}
            for event in generation_events
            if event.get("references_dropped")
        ],
        "cast_portraits": (
            {
                cid: {
                    "file": entry["file"], "source": entry["source"],
                    "from_shot": entry.get("from_shot", ""),
                }
                for cid, entry in casting.data["portraits"].items()
            }
            if casting is not None else {}
        ),
        # Shots the audit could not reach. Named, because "no blockers"
        # and "nobody looked" must never read the same.
        "audit_failures": audit_failures,
        "narrated_shots": narrated,
        "auditor": getattr(auditor, "name", "rule-triage"),
        "evidence_source": getattr(auditor, "last_evidence", RULE_TRIAGE),
        "bible_provenance": bible.data.get("provenance", {}),
        "policy": {
            "shot_count": len(shots),
            "single_vendor": True,
            "regenerate_blockers_only": True,
            "max_repair_rounds": max_repair_rounds,
        },
        "shots": [
            {
                "shot_id": shot["shot_id"],
                "beat": shot["beat"],
                "location": shot["location_name"],
                "characters": [bible.character(c).get("name", c) for c in shot["character_ids"]],
                "model_tier": shot["model_tier"],
                "final_take": attempts[shot["shot_id"]],
                "file": current[shot["shot_id"]].path.name,
                "line": shot.get("line", ""),
                "prompt": prompts[shot["shot_id"]],
            }
            for shot in shots
        ],
        "generation_events": generation_events,
        "audit_events": audit_events,
        "summary": {
            "status": "DELIVERED" if not unresolved else "BLOCKERS REMAIN",
            "unresolved_shot_ids": sorted(unresolved, key=int),
            "initial_generation_count": len(shots),
            "total_generation_count": len(generation_events),
            "repaired_shot_ids": repaired,
            # What this run did not have to buy again, and what that was worth.
            "reused_shot_ids": sorted(reused, key=int),
            "reused_saving_cny": round(saved_cny, 2),
            "portraits_used": bool(casting),
            "whole_film_rerun": False,
            "runtime_sec": built["runtime_sec"],
            "subtitles_burned": built["subtitles_burned"],
            "wall_clock_sec": round(time.time() - started, 1),
        },
        "outputs": {
            "video": built["video"].name,
            "srt": built["srt"].name,
        },
    }
    validate_episode_report(report)
    (root / "episode-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (root / "episode-report.html").write_text(render_html(report), encoding="utf-8")
    report["_paths"] = {
        "video": built["video"],
        "srt": built["srt"],
        "json": root / "episode-report.json",
        "html": root / "episode-report.html",
    }
    return report


def validate_episode_report(report: dict[str, Any]) -> dict[str, Any]:
    """The invariants that make the repair claim worth anything."""

    policy = report["policy"]
    events = report["generation_events"]
    audits = report["audit_events"]

    if policy["single_vendor"] is not True:
        raise PipelineError("an episode must use exactly one vendor")
    if {event["provider"] for event in events} != {report["vendor"]}:
        raise PipelineError("episode report contains mixed vendors")
    if not 0 <= policy["max_repair_rounds"] <= MAX_REPAIR_ROUNDS:
        raise PipelineError("repair rounds exceed the two-round cap")

    initial = [event for event in events if event["round"] == 0]
    if len(initial) != policy["shot_count"]:
        raise PipelineError("round 0 must generate every shot exactly once")

    permitted = {
        (event["round"] + 1, event["shot_id"])
        for event in audits
        if event["decision"] == "REGENERATE" and event["round"] < policy["max_repair_rounds"]
    }
    actual = {(event["round"], event["shot_id"]) for event in events if event["round"] > 0}
    if len(actual) != len(events) - len(initial):
        raise PipelineError("duplicate repair generations in the report")
    if actual - permitted:
        raise PipelineError("a shot was regenerated without being a blocker")
    if report["summary"]["whole_film_rerun"] is not False:
        raise PipelineError("whole-episode rerun is forbidden")
    if report["summary"]["total_generation_count"] != len(events):
        raise PipelineError("generation count mismatch")
    if not report["vendor_is_generative"] and report["evidence_source"] != RULE_TRIAGE:
        # Belt and braces: a storyboard render must never carry a visual verdict.
        raise PipelineError("a non-generative vendor cannot carry a visual evidence label")
    return report


def render_html(report: dict[str, Any]) -> str:
    import html as html_mod

    esc = lambda value: html_mod.escape(str(value))
    rows = "".join(
        "<tr>"
        f"<td>{esc(shot['shot_id'])}</td><td>{esc(shot['beat'])}</td>"
        f"<td>{esc(shot['location'])}</td><td>{esc(', '.join(shot['characters']))}</td>"
        f"<td>{esc(shot['model_tier'])}</td><td>{esc(shot['final_take'])}</td>"
        f"<td>{esc(shot['line'])}</td>"
        "</tr>"
        for shot in report["shots"]
    )
    repaired = ", ".join(report["summary"]["repaired_shot_ids"]) or "none"
    honesty = (
        "A generative video model produced these clips."
        if report["vendor_is_generative"]
        else "These clips are locally rendered storyboard cards, not model output. "
        "The loop is real; the pixels are a stand-in."
    )
    return f"""<!doctype html>
<html lang="en"><meta charset="utf-8">
<title>{esc(report['series_title'])} · Episode {esc(report['episode'])}</title>
<style>
 body{{font:16px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;max-width:900px;margin:48px auto;padding:0 20px;color:#1b1f24}}
 h1{{font-size:26px;margin:0 0 4px}} .sub{{color:#5c6672;margin:0 0 28px}}
 table{{border-collapse:collapse;width:100%;margin:20px 0;font-size:14px}}
 th,td{{border:1px solid #d8dde3;padding:8px 10px;text-align:left;vertical-align:top}}
 th{{background:#f4f6f8;font-weight:600}}
 .note{{background:#f4f6f8;border-left:3px solid #8b97a4;padding:12px 16px;margin:20px 0}}
 ul{{padding-left:20px}} code{{font-size:13px;background:#f4f6f8;padding:1px 5px;border-radius:3px}}
</style>
<h1>{esc(report['series_title'])} — Episode {esc(report['episode'])}: {esc(report['episode_title'])}</h1>
<p class="sub">Seed word「{esc(report['seed_word'])}」 · {esc(report['logline'])}</p>
<div class="note"><b>Evidence:</b> {esc(report['evidence_source'])}. {esc(honesty)}</div>
<ul>
 <li>Vendor: <code>{esc(report['vendor'])}</code> · Voice: <code>{esc(report['voice_engine'])}</code> · Auditor: <code>{esc(report['auditor'])}</code></li>
 <li>Shots: {esc(report['policy']['shot_count'])} · Generations: {esc(report['summary']['total_generation_count'])} · Repaired: {esc(repaired)}</li>
 <li>Repair rounds allowed: {esc(report['policy']['max_repair_rounds'])} · Whole-episode rerun: no</li>
 <li>Runtime: {esc(report['summary']['runtime_sec'])}s · Wall clock: {esc(report['summary']['wall_clock_sec'])}s</li>
 <li>Output: <code>{esc(report['outputs']['video'])}</code> + <code>{esc(report['outputs']['srt'])}</code></li>
</ul>
<table><thead><tr><th>Shot</th><th>Beat</th><th>Location</th><th>Cast</th><th>Tier</th><th>Take</th><th>Line</th></tr></thead>
<tbody>{rows}</tbody></table>
</html>
"""
