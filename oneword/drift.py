"""Is the stairwell in episode 9 still the stairwell from episode 1?

Everything else in this package works inside one episode.  This module is the
only part that looks across them, and it is the part a one-prompt pipeline has
no way to build: it needs a written-down reference to compare against, and a
pipeline that keeps its facts in a context window has nothing to point at.

## What gets checked, and what honestly cannot be

`location`   The frame is mostly the location, so the local fingerprint in
             `metrics.py` says something real about it for free.  Screened
             always; escalated to a multimodal comparison when one is available.

`character`  A person occupies a fraction of the frame, sharing it with a set
             that legitimately changes shot to shot.  A whole-frame fingerprint
             therefore says almost nothing about whether the jacket cuff is
             still torn, so this module does **not** screen characters locally.
             With no multimodal model configured, character drift is reported as
             `NOT CHECKED` — not as a pass.  A green tick nobody earned is worse
             than an honest blank.

The thresholds below are measurements, not taste.  `docs/drift-calibration.md`
records the runs they came from and how to redo them.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import llm, lockability, metrics
from .audit import FRAME_POSITIONS, _chat_vision, _data_url, extract_frames
from .bible import SeriesBible
from .cast import CastPortraits
from .registry import ReferenceRegistry

DRIFT_REPORT_VERSION = "series-drift-1"

# Measured, not chosen: see docs/drift-calibration.md.  The two distributions
# overlap, so these are set by which error costs more — a missed drift ships a
# broken series, a needless review costs one model call.  Roughly 80% of pairs
# land in the review band, which is the screen being honest about its reach.
SAME_PLACE_MAX = 0.03
DIFFERENT_PLACE_MIN = 0.09

# There is one band, and the reason there is one band was measured rather than
# assumed.  An earlier version split it by whether a shot had been chained, on
# the theory that a shot continued from the previous shot's last frame sits far
# closer to the series reference.  The first real footage says otherwise: a
# chained shot resembles **the shot it continued**, not the reference, and it
# inherits whatever offset that predecessor already carried.  One chained shot
# in that data sits 0.010 from its reference and another sits 0.075, because
# the second one faithfully continued a shot that had already drifted.
#
# Chaining is therefore not a property of the comparison the band is applied
# to, and splitting on it was measuring the wrong pair.  See
# docs/drift-calibration.md for the matrix that settled it.
BANDS: dict[str, dict[str, Any]] = {
    "reference": {
        "consistent_max": SAME_PLACE_MAX,
        "drifted_min": DIFFERENT_PLACE_MIN,
        "measured_on": "synthetic harness, 6 rooms x 8 shots (docs/drift-calibration.md)",
    },
}

SCREENABLE_KINDS = ("location",)

# How the final take of a shot was actually made.
CHAINED = "chained"
CHAIN_REFUSED = "chain refused"
UNCHAINED = "not chained"
CHAIN_UNKNOWN = "unknown"

PIXEL_SCREEN = "REFERENCE PIXEL SCREEN"
VISUAL_AUDIT = "REFERENCE VISUAL AUDIT"
NO_REFERENCE = "NO REFERENCE YET"
NOT_CHECKED = "NOT CHECKED"

CONSISTENT = "CONSISTENT"
REVIEW = "REVIEW"
DRIFTED = "DRIFTED"
REFERENCE_SET = "REFERENCE SET"

SYSTEM = (
    "You compare two frames from the same film series. The first is the "
    "established reference for a subject; the second is a later appearance. "
    "Report only differences you can actually see, against the written facts "
    "given. Camera angle, framing, lighting mood and the action are ALLOWED to "
    "differ — you are looking for changes to things that are supposed to be "
    "fixed. Output JSON only."
)


class DriftError(RuntimeError):
    pass


def verdict_for(composite: float, population: str = "reference") -> str:
    band = BANDS.get(population, BANDS["reference"])
    if composite <= band["consistent_max"]:
        return CONSISTENT
    if composite >= band["drifted_min"]:
        return DRIFTED
    return REVIEW


def chain_state_for(episode_report: dict[str, Any], shot_id: str) -> dict[str, str]:
    """How the final take of this shot was made, from the episode's own record.

    The *final* take: a repaired shot is the one in the film and the one the
    drift pass measured, so an earlier attempt's chain does not describe it.
    """

    events = [
        event for event in episode_report.get("generation_events", [])
        if str(event["shot_id"]) == str(shot_id)
    ]
    links = episode_report.get("chain_links") or {}
    if not events:
        return {"state": CHAIN_UNKNOWN, "detail": ""}

    last = events[-1]
    if last.get("chain_dropped"):
        return {"state": CHAIN_REFUSED, "detail": str(last["chain_dropped"])[:300]}
    if last.get("continues_shot"):
        return {
            "state": CHAINED,
            "detail": f"from shot {last['continues_shot']}",
            "continues": str(last["continues_shot"]),
        }
    if not links:
        return {"state": UNCHAINED, "detail": "chaining off for this run"}
    if str(shot_id) not in {str(key) for key in links}:
        return {"state": UNCHAINED, "detail": "cuts to another location"}
    return {"state": UNCHAINED, "detail": "first frame not available"}


def portraits_dropped_for(episode_report: dict[str, Any], shot_id: str) -> str:
    """Why this shot went out without the cast portraits, if it did.

    Without this, a character who drifted in exactly the shot whose portrait the
    platform refused looks like a failure of the mechanism rather than a shot
    the mechanism never got to touch.
    """

    events = [
        event for event in episode_report.get("generation_events", [])
        if str(event["shot_id"]) == str(shot_id)
    ]
    if not events:
        return ""
    return str(events[-1].get("references_dropped") or "")[:300]


# ──────────────────────────────────────────────────────────────────────
# Multimodal comparison
# ──────────────────────────────────────────────────────────────────────


def _facts_for(bible: SeriesBible, kind: str, subject_id: str) -> str:
    if kind == "location":
        entry = bible.location(subject_id)
        head = f"LOCATION {entry.get('name', subject_id)}"
        body = entry.get("locked_description", "")
    else:
        entry = bible.character(subject_id)
        head = f"CHARACTER {entry.get('name', subject_id)}"
        body = entry.get("locked_appearance", "")
    rules = "\n".join(bible.rule_lines())
    return f"{head}: {body}\n\nSERIES RULES:\n{rules}"


def visual_compare(
    reference_frame: Path,
    candidate_frame: Path,
    facts: str,
    errors: list[str] | None = None,
) -> list[dict[str, Any]] | None:
    """None means no model looked — never an empty list, which means it did.

    When a model *was* configured and the call failed, the reason is appended
    to `errors` rather than vanishing. Silently downgrading to NOT CHECKED is
    how you set a text-only model id, wait, and get a report that looks exactly
    like the one you would get with no key at all.
    """

    if not llm.configured():
        return None

    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                f"{facts}\n\n"
                "FRAME A is the established reference. FRAME B is a later appearance.\n"
                'Return {"differences":[{"fact":"the written fact that no longer holds",'
                '"evidence":"what you see in B instead","severity":"regenerate|local_fix"}]}. '
                "Empty array if every written fact still holds in B."
            ),
        },
        {"type": "text", "text": "FRAME A (reference)"},
        {"type": "image_url", "image_url": {"url": _data_url(reference_frame)}},
        {"type": "text", "text": "FRAME B (later appearance)"},
        {"type": "image_url", "image_url": {"url": _data_url(candidate_frame)}},
    ]
    try:
        parsed = _chat_vision(SYSTEM, content)
    except Exception as exc:  # noqa: BLE001 — a failed call downgrades, never upgrades
        if errors is not None:
            reason = f"{type(exc).__name__}: {exc}".strip()[:300]
            if reason not in errors:
                errors.append(reason)
        return None

    differences = []
    for item in llm.items_under(parsed, "differences"):
        evidence = (item.get("evidence") or "").strip()
        if not evidence:
            continue
        fact = (item.get("fact") or "").strip()[:300]
        # A fact no generator can hold produces this finding in every episode
        # forever. It is still true that the fact does not hold — it is just
        # not news, and six of them bury the one that is.
        unenforceable = [issue.kind for issue in lockability.classify(fact)]
        differences.append(
            {
                "fact": fact,
                "evidence": evidence[:300],
                "severity": "local_fix" if item.get("severity") == "local_fix" else "regenerate",
                "unenforceable": unenforceable,
            }
        )
    return differences


# ──────────────────────────────────────────────────────────────────────
# Walking a finished series
# ──────────────────────────────────────────────────────────────────────


def _episode_reports(root: Path) -> list[dict[str, Any]]:
    reports = []
    for path in sorted(root.glob("episode-*/episode-report.json")):
        reports.append({"path": path, "data": json.loads(path.read_text(encoding="utf-8"))})
    return sorted(reports, key=lambda item: int(item["data"]["episode"]))


def _subjects_in_shot(shot: dict[str, Any], bible: SeriesBible) -> list[tuple[str, str, str]]:
    """[(kind, id, display name)] — resolved back to bible ids by name."""

    found: list[tuple[str, str, str]] = []
    for lid, location in bible.data["locations"].items():
        if location.get("name") == shot.get("location"):
            found.append(("location", lid, location.get("name", lid)))
            break
    names = set(shot.get("characters") or [])
    for cid, character in bible.data["characters"].items():
        if character.get("name") in names:
            found.append(("character", cid, character.get("name", cid)))
    return found


def audit_series(
    root: str | Path,
    *,
    reset_references: bool = False,
    use_model: bool = True,
) -> dict[str, Any]:
    """Compare every appearance against its reference and write the report."""

    root = Path(root)
    bible_path = root / "bible.json"
    if not bible_path.is_file():
        raise DriftError(f"no bible.json in {root} — is that a series directory?")
    bible = SeriesBible.load(bible_path)

    reports = _episode_reports(root)
    if not reports:
        # The output directory is a flag, so a machine usually has several.
        # Naming the ones that do hold a series saves a guess.
        elsewhere = sorted(
            str(candidate.parent)
            for candidate in root.parent.parent.glob("*/*/bible.json")
            if candidate.parent != root
        )[:5] if root.parent.parent.is_dir() else []
        message = f"no episode reports under {root}"
        if elsewhere:
            message += "\n  Series directories that do have them:\n    " + "\n    ".join(elsewhere)
        raise DriftError(message)

    registry = ReferenceRegistry(root)
    if reset_references:
        registry.clear()

    # A style change legitimately alters every frame, so comparing across one
    # produces a report full of drift that is not drift. Refusing is the only
    # honest answer: the references have to be re-based, and that is a decision
    # rather than something to do quietly on the user's behalf.
    style_now = bible.style_name
    shot_under = registry.styles_present() - {"unknown"}
    foreign = shot_under - {style_now}
    if foreign:
        raise DriftError(
            f"the references were shot in {', '.join(sorted(foreign))} but this bible "
            f"is now {style_now}. Every frame differs by design, so a comparison would "
            "report drift that is not drift.\n  Re-base them deliberately: "
            "oneword drift <dir> --reset-references"
        )

    workdir = root / "references" / ".frames"
    findings: list[dict[str, Any]] = []
    model_used = False
    model_errors: list[str] = []
    missing_clips: list[str] = []
    # (episode, shot_id) → fingerprints, so a chained shot can also be measured
    # against the shot it actually continued.
    by_shot: dict[tuple[int, str], list[dict[str, Any]]] = {}

    # A character's locked portrait is a better reference than a frame from the
    # shot they happened to appear in first: it is the picture that was actually
    # sent to every later shot, so comparing against it asks the exact question
    # that matters — did sending it work?
    portraits = CastPortraits(root)

    for report in reports:
        data = report["data"]
        episode = int(data["episode"])
        episode_dir = report["path"].parent
        generative = bool(data.get("vendor_is_generative"))

        for shot in data["shots"]:
            clip = episode_dir / "clips" / shot["file"]
            if not clip.is_file():
                # Silently skipping produces a report that says "0 appearances"
                # and looks like a series with nothing wrong with it.
                missing_clips.append(f"ep{episode} shot {shot['shot_id']} ({shot['file']})")
                continue
            frames = extract_frames(clip, workdir, FRAME_POSITIONS)
            if not frames:
                continue
            prints = [metrics.fingerprint(frame) for frame in frames]
            chain = chain_state_for(data, shot["shot_id"])
            portrait_refused = portraits_dropped_for(data, shot["shot_id"])
            by_shot[(episode, str(shot["shot_id"]))] = prints

            # Two different questions, and conflating them is how a whole
            # episode's drift gets filed as four unrelated broken shots.
            #   against the reference  — has the series held?
            #   against the predecessor — did this cut hold?
            # A shot tight to the shot it continued but far from the reference
            # means the episode moved as a unit and carried this shot with it.
            neighbour = None
            source = chain.get("continues")
            if source and (episode, source) in by_shot:
                neighbour = metrics.best_distance(
                    prints, by_shot[(episode, source)][0]
                )

            for kind, subject_id, display in _subjects_in_shot(shot, bible):
                entry = registry.get(kind, subject_id)

                if entry is None:
                    registry.adopt(
                        kind, subject_id, frames[0],
                        episode=episode, shot_id=shot["shot_id"], name=display,
                        style=style_now,
                    )
                    findings.append(
                        {
                            "episode": episode, "shot_id": shot["shot_id"],
                            "kind": kind, "id": subject_id, "name": display,
                            "verdict": REFERENCE_SET, "evidence_source": NO_REFERENCE,
                            "distance": None, "differences": [],
                            "note": f"first appearance — adopted as the reference for {display}",
                        }
                    )
                    continue

                if kind in SCREENABLE_KINDS:
                    best = metrics.best_distance(prints, entry["fingerprint"])
                    verdict = verdict_for(best["composite"])
                    evidence = PIXEL_SCREEN
                    channel = metrics.tripped_by(best)
                else:
                    best = None
                    verdict = NOT_CHECKED
                    evidence = NOT_CHECKED
                    channel = None

                differences: list[dict[str, Any]] = []
                reference_kind = "first appearance"
                portrait = portraits.get(subject_id) if kind == "character" else None
                if portrait is not None:
                    reference_kind = f"locked portrait ({portrait.source})"

                # Escalate to a model only where it can change the answer, and
                # only where a model actually produced the pixels.
                worth_looking = verdict in (REVIEW, DRIFTED) or kind not in SCREENABLE_KINDS
                if use_model and generative and worth_looking:
                    reference_frame = (
                        portrait.path if portrait is not None
                        else registry.frame_path(kind, subject_id)
                    )
                    if reference_frame:
                        result = visual_compare(
                            reference_frame, frames[len(frames) // 2],
                            _facts_for(bible, kind, subject_id),
                            model_errors,
                        )
                        if result is not None:
                            model_used = True
                            differences = result
                            evidence = VISUAL_AUDIT
                            hard = any(d["severity"] == "regenerate" for d in differences)
                            verdict = DRIFTED if hard else (REVIEW if differences else CONSISTENT)

                findings.append(
                    {
                        "episode": episode, "shot_id": shot["shot_id"],
                        "kind": kind, "id": subject_id, "name": display,
                        "verdict": verdict, "evidence_source": evidence,
                        "distance": best, "differences": differences,
                        "tripped_by": channel,
                        "chain_state": chain["state"],
                        "chain_detail": chain["detail"],
                        "band": "reference",
                        # Only meaningful for a location: a character's face is
                        # not what a whole-frame fingerprint measures.
                        "neighbour_distance": neighbour if kind in SCREENABLE_KINDS else None,
                        "continues_shot": chain.get("continues", ""),
                        "reference_kind": reference_kind,
                        "portrait_refused": portrait_refused if kind == "character" else "",
                        "reference_episode": entry["episode"],
                        "reference_shot": entry["shot_id"],
                    }
                )

    # Every clip gone is not a clean series, it is a series nobody could look
    # at. `.mp4` is in .gitignore, so a cloned or copied series directory has
    # the reports and none of the footage — exactly the shape that would
    # otherwise produce a confident, empty, meaningless report.
    if missing_clips and not findings:
        raise DriftError(
            f"none of the {len(missing_clips)} clips under {root} are on disk, so there "
            "was nothing to compare.\n"
            "  The reports survive a copy or a git clone; the .mp4 files do not "
            "(.gitignore excludes them).\n"
            "  Point this at the directory the run actually wrote, or shoot it again."
        )

    drifted = [f for f in findings if f["verdict"] == DRIFTED]
    review = [f for f in findings if f["verdict"] == REVIEW]
    unchecked = [f for f in findings if f["verdict"] == NOT_CHECKED]

    non_generative = [r["data"] for r in reports if not r["data"].get("vendor_is_generative")]

    result = {
        "report_version": DRIFT_REPORT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "series_title": bible.title,
        "seed_word": bible.word,
        "style": style_now,
        "episodes": [int(r["data"]["episode"]) for r in reports],
        "thresholds": {
            "same_place_max": SAME_PLACE_MAX,
            "different_place_min": DIFFERENT_PLACE_MIN,
            "calibration": "docs/drift-calibration.md",
        },
        "bands": BANDS,
        "references": [
            {
                "kind": kind, "id": subject_id,
                "name": registry.get(kind, subject_id)["name"],
                "from_episode": registry.get(kind, subject_id)["episode"],
                "from_shot": registry.get(kind, subject_id)["shot_id"],
                "rebased": len(registry.get(kind, subject_id).get("rebased_from", [])),
            }
            for kind, subject_id in registry.subjects()
        ],
        "model_looked": model_used,
        # A configured model that never answered is a different situation from
        # no model at all, and the report has to be able to tell you which.
        "model_errors": model_errors,
        "missing_clips": missing_clips,
        "findings": findings,
        "summary": {
            "checked": len(findings),
            "drifted": len(drifted),
            "review": len(review),
            "not_checked": len(unchecked),
            # "CONSISTENT" is reserved for a series where every subject was
            # actually examined and held. A run with characters nobody looked at
            # is PARTIAL, because a clean-looking summary is exactly what would
            # let unchecked drift through.
            "status": (
                "DRIFT FOUND" if drifted
                else "REVIEW" if review
                else "PARTIAL" if unchecked
                else "CONSISTENT"
            ),
            "storyboard_episodes": [int(d["episode"]) for d in non_generative],
            # Named here rather than buried in the bands table: a verdict reached
            # with a borrowed threshold is a weaker claim than one reached with a
            # measured one, and the summary is where that belongs.
            "unmeasured_bands": sorted(
                {
                    f["band"] for f in findings
                    if f.get("band") and BANDS.get(f["band"], {}).get("measured_on") is None
                }
            ),
        },
    }
    validate_drift_report(result)

    (root / "series-drift-report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (root / "series-drift-report.html").write_text(render_html(result), encoding="utf-8")
    return result


def validate_drift_report(report: dict[str, Any]) -> dict[str, Any]:
    """The honesty invariants, enforced rather than intended."""

    for finding in report["findings"]:
        if finding["verdict"] == CONSISTENT and finding["evidence_source"] == NOT_CHECKED:
            raise DriftError("a subject that was never checked cannot be reported CONSISTENT")
        if finding["differences"] and finding["evidence_source"] != VISUAL_AUDIT:
            raise DriftError("only a visual audit may report specific differences")
        if finding["kind"] not in SCREENABLE_KINDS and finding["distance"] is not None:
            raise DriftError(f"{finding['kind']} must not carry a whole-frame screen distance")
        if finding["distance"] is not None and not finding.get("tripped_by"):
            # A number with no account of which channel produced it invites the
            # reader to assume both did.
            raise DriftError("a screened finding must say which channel decided it")
    if report["summary"]["status"] == "CONSISTENT" and report["summary"]["not_checked"]:
        # Otherwise a series whose characters were never examined reads as clean.
        raise DriftError("cannot summarise as CONSISTENT while subjects remain unchecked")
    return report


def render_html(report: dict[str, Any]) -> str:
    import html as html_mod

    esc = lambda value: html_mod.escape(str(value))
    colour = {
        CONSISTENT: "#087a42", REVIEW: "#a16207",
        DRIFTED: "#b42318", REFERENCE_SET: "#3a6ea5", NOT_CHECKED: "#6b7280",
    }

    rows = []
    for finding in report["findings"]:
        distance = finding["distance"]
        detail = f"{distance['composite']:.3f}" if distance else "—"
        parts = (
            f"colour {distance['colour']:.3f} · structure {distance['structure']:.3f}"
            if distance else ""
        )
        if distance and finding.get("tripped_by"):
            parts += f"<br>decided by {esc(finding['tripped_by'])}"
        neighbour = finding.get("neighbour_distance")
        if neighbour:
            parts += (
                f"<br>vs shot {esc(finding['continues_shot'])}: "
                f"<b>{neighbour['composite']:.3f}</b>"
            )
        made = esc(finding.get("chain_state") or "—")
        if finding.get("chain_detail"):
            made += f"<br><small>{esc(finding['chain_detail'][:60])}</small>"
        if finding.get("portrait_refused"):
            made += "<br><small><b>portrait refused</b></small>"
        diffs = "<br>".join(
            (
                f"<b>{esc(d['fact'])}</b> — {esc(d['evidence'])}"
                + (
                    f"<br><small>⚠ unenforceable ({esc(', '.join(d['unenforceable']))}) — "
                    "this fact will fail in every episode until it is reworded</small>"
                    if d.get("unenforceable") else ""
                )
            )
            for d in finding["differences"]
        ) or "—"
        rows.append(
            "<tr>"
            f"<td>{esc(finding['episode'])}</td><td>{esc(finding['shot_id'])}</td>"
            f"<td>{esc(finding['name'])}<br><small>{esc(finding['kind'])}</small></td>"
            f"<td style='color:{colour.get(finding['verdict'], '#333')};font-weight:600'>"
            f"{esc(finding['verdict'])}</td>"
            f"<td>{esc(detail)}<br><small>{parts}</small></td>"
            f"<td><small>{made}</small></td>"
            f"<td><small>{esc(finding['evidence_source'])}</small></td>"
            f"<td>{diffs}</td>"
            "</tr>"
        )

    portrait_for = {
        f["id"]: f["reference_kind"] for f in report["findings"]
        if f.get("reference_kind", "").startswith("locked portrait")
    }
    references = "".join(
        f"<li><b>{esc(r['name'])}</b> ({esc(r['kind'])}) — locked from episode "
        f"{esc(r['from_episode'])}, shot {esc(r['from_shot'])}"
        + (f" · re-based {esc(r['rebased'])}×" if r["rebased"] else "")
        + (f" · compared against their {esc(portrait_for[r['id']])}"
           if r["id"] in portrait_for else "")
        + "</li>"
        for r in report["references"]
    )

    caveats = []
    if report.get("model_errors"):
        caveats.append(
            "A model WAS configured and every call to it failed: "
            + "; ".join(report["model_errors"][:3])
            + ". Nothing below was examined by a model — check that LLM_MODEL names a "
            "vision-capable model, because a text-only one fails exactly like this."
        )
    elif not report["model_looked"]:
        caveats.append(
            "No multimodal model examined these frames. Locations carry a pixel screen only, "
            "and characters were not checked at all — which is also the only thing that can "
            "answer whether the cast portraits held. Set LLM_API_KEY and LLM_MODEL to a "
            "vision-capable model and run this again."
        )
    if report["summary"]["storyboard_episodes"]:
        caveats.append(
            "Episodes "
            + ", ".join(str(e) for e in report["summary"]["storyboard_episodes"])
            + " were shot with the offline storyboard vendor. Those pixels are stand-ins, "
            "so agreement between them says nothing about whether a real video model drifts."
        )

    if report.get("missing_clips"):
        caveats.append(
            f"{len(report['missing_clips'])} shot(s) were skipped because their clip is "
            "not on disk: " + ", ".join(report["missing_clips"][:4])
            + ". Those appearances are absent from this report, not consistent."
        )

    noise = [
        d for f in report["findings"] for d in f["differences"] if d.get("unenforceable")
    ]
    total_diffs = sum(len(f["differences"]) for f in report["findings"])
    if noise:
        kinds = sorted({kind for d in noise for kind in d["unenforceable"]})
        caveats.append(
            f"{len(noise)} of {total_diffs} differences below cite a locked fact no generator "
            f"can hold ({', '.join(kinds)}). They are true and they are not news — the same "
            "fact will fail in every episode until the bible is reworded. Spending more does "
            "not fix these."
        )

    screened = [f for f in report["findings"] if f.get("tripped_by")]
    if screened and all(f["tripped_by"] == "colour" for f in screened):
        caveats.append(
            "Every verdict here was decided by colour; structure never moved. That is the "
            "fingerprint working as designed — the rooms really are the same rooms — but it "
            "means the composite is a one-channel measurement on this series, not the blend "
            "its weights imply."
        )
    if report["summary"].get("unmeasured_bands"):
        caveats.append(
            "Shots judged against an unmeasured band: "
            + ", ".join(report["summary"]["unmeasured_bands"])
            + ". Those thresholds were measured on a different population and are being "
            "borrowed. Measure them: python scripts/calibrate_drift.py --series <this dir>"
        )

    # A whole episode sliding as a unit is one problem; four independently bad
    # shots is another. Saying which costs nothing and changes what you do.
    carried = [
        f for f in report["findings"]
        if f.get("neighbour_distance")
        and f["distance"]
        and f["neighbour_distance"]["composite"] <= SAME_PLACE_MAX
        and f["distance"]["composite"] > SAME_PLACE_MAX
    ]
    if carried:
        shots = ", ".join(f"ep{f['episode']} shot {f['shot_id']}" for f in carried[:6])
        caveats.append(
            f"{len(carried)} shot(s) sit on top of the shot they continued but away from the "
            f"series reference ({shots}). The cut held; what moved is the whole run of shots. "
            "Regenerating one of them would reshoot a shot that matches its neighbour "
            "perfectly — the episode needs re-establishing against the reference instead."
        )
    summary = report["summary"]

    return f"""<!doctype html>
<html lang="en"><meta charset="utf-8">
<title>{esc(report['series_title'])} · cross-episode drift</title>
<style>
 body{{font:16px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;max-width:1040px;margin:48px auto;padding:0 20px;color:#1b1f24}}
 h1{{font-size:26px;margin:0 0 4px}} .sub{{color:#5c6672;margin:0 0 24px}}
 table{{border-collapse:collapse;width:100%;margin:20px 0;font-size:14px}}
 th,td{{border:1px solid #d8dde3;padding:8px 10px;text-align:left;vertical-align:top}}
 th{{background:#f4f6f8;font-weight:600}} small{{color:#6b7280}}
 .note{{background:#fff8e6;border-left:3px solid #d0a215;padding:12px 16px;margin:16px 0}}
 .tiles{{display:flex;gap:12px;flex-wrap:wrap;margin:20px 0}}
 .tile{{border:1px solid #d8dde3;border-radius:8px;padding:12px 18px;min-width:120px}}
 .tile b{{display:block;font-size:24px}}
 @media (max-width:640px){{body{{margin:24px auto}}table{{font-size:13px}}}}
</style>
<h1>{esc(report['series_title'])} — cross-episode drift</h1>
<p class="sub">Seed word「{esc(report['seed_word'])}」 · episodes {esc(', '.join(str(e) for e in report['episodes']))} · status <b>{esc(summary['status'])}</b></p>
<div class="tiles">
 <div class="tile"><b>{esc(summary['checked'])}</b>appearances</div>
 <div class="tile"><b>{esc(summary['drifted'])}</b>drifted</div>
 <div class="tile"><b>{esc(summary['review'])}</b>review</div>
 <div class="tile"><b>{esc(summary['not_checked'])}</b>not checked</div>
</div>
{"".join(f'<div class="note">{esc(c)}</div>' for c in caveats)}
<h2>References</h2>
<p>Every appearance below is compared against these, not against the previous episode.</p>
<ul>{references}</ul>
<h2>Every appearance</h2>
<table><thead><tr><th>Ep</th><th>Shot</th><th>Subject</th><th>Verdict</th><th>Distance</th><th>How it was shot</th><th>Evidence</th><th>Differences</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table>
<p><small>Thresholds: consistent ≤ {esc(report['thresholds']['same_place_max'])},
drifted ≥ {esc(report['thresholds']['different_place_min'])} —
see <code>{esc(report['thresholds']['calibration'])}</code>.</small></p>
</html>
"""
