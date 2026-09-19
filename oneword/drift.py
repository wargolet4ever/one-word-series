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

from . import llm, metrics
from .audit import FRAME_POSITIONS, _chat_vision, _data_url, extract_frames
from .bible import SeriesBible
from .registry import ReferenceRegistry

DRIFT_REPORT_VERSION = "series-drift-1"

# Measured, not chosen: see docs/drift-calibration.md.  The two distributions
# overlap, so these are set by which error costs more — a missed drift ships a
# broken series, a needless review costs one model call.  Roughly 80% of pairs
# land in the review band, which is the screen being honest about its reach.
SAME_PLACE_MAX = 0.03
DIFFERENT_PLACE_MIN = 0.09

SCREENABLE_KINDS = ("location",)

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


def verdict_for(composite: float) -> str:
    if composite <= SAME_PLACE_MAX:
        return CONSISTENT
    if composite >= DIFFERENT_PLACE_MIN:
        return DRIFTED
    return REVIEW


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
) -> list[dict[str, Any]] | None:
    """None means no model looked — never an empty list, which means it did."""

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
    except Exception:  # noqa: BLE001 — a failed call downgrades, never upgrades
        return None

    differences = []
    for item in parsed.get("differences") or []:
        evidence = (item.get("evidence") or "").strip()
        if not evidence:
            continue
        differences.append(
            {
                "fact": (item.get("fact") or "").strip()[:300],
                "evidence": evidence[:300],
                "severity": "local_fix" if item.get("severity") == "local_fix" else "regenerate",
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
        raise DriftError(f"no episode reports under {root}")

    registry = ReferenceRegistry(root)
    if reset_references:
        registry.clear()

    workdir = root / "references" / ".frames"
    findings: list[dict[str, Any]] = []
    model_used = False

    for report in reports:
        data = report["data"]
        episode = int(data["episode"])
        episode_dir = report["path"].parent
        generative = bool(data.get("vendor_is_generative"))

        for shot in data["shots"]:
            clip = episode_dir / "clips" / shot["file"]
            if not clip.is_file():
                continue
            frames = extract_frames(clip, workdir, FRAME_POSITIONS)
            if not frames:
                continue
            prints = [metrics.fingerprint(frame) for frame in frames]

            for kind, subject_id, display in _subjects_in_shot(shot, bible):
                entry = registry.get(kind, subject_id)

                if entry is None:
                    registry.adopt(
                        kind, subject_id, frames[0],
                        episode=episode, shot_id=shot["shot_id"], name=display,
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
                else:
                    best = None
                    verdict = NOT_CHECKED
                    evidence = NOT_CHECKED

                differences: list[dict[str, Any]] = []
                # Escalate to a model only where it can change the answer, and
                # only where a model actually produced the pixels.
                worth_looking = verdict in (REVIEW, DRIFTED) or kind not in SCREENABLE_KINDS
                if use_model and generative and worth_looking:
                    reference_frame = registry.frame_path(kind, subject_id)
                    if reference_frame:
                        result = visual_compare(
                            reference_frame, frames[len(frames) // 2],
                            _facts_for(bible, kind, subject_id),
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
                        "reference_episode": entry["episode"],
                        "reference_shot": entry["shot_id"],
                    }
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
        "episodes": [int(r["data"]["episode"]) for r in reports],
        "thresholds": {
            "same_place_max": SAME_PLACE_MAX,
            "different_place_min": DIFFERENT_PLACE_MIN,
            "calibration": "docs/drift-calibration.md",
        },
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
        diffs = "<br>".join(
            f"<b>{esc(d['fact'])}</b> — {esc(d['evidence'])}" for d in finding["differences"]
        ) or "—"
        rows.append(
            "<tr>"
            f"<td>{esc(finding['episode'])}</td><td>{esc(finding['shot_id'])}</td>"
            f"<td>{esc(finding['name'])}<br><small>{esc(finding['kind'])}</small></td>"
            f"<td style='color:{colour.get(finding['verdict'], '#333')};font-weight:600'>"
            f"{esc(finding['verdict'])}</td>"
            f"<td>{esc(detail)}<br><small>{esc(parts)}</small></td>"
            f"<td><small>{esc(finding['evidence_source'])}</small></td>"
            f"<td>{diffs}</td>"
            "</tr>"
        )

    references = "".join(
        f"<li><b>{esc(r['name'])}</b> ({esc(r['kind'])}) — locked from episode "
        f"{esc(r['from_episode'])}, shot {esc(r['from_shot'])}"
        + (f" · re-based {esc(r['rebased'])}×" if r["rebased"] else "")
        + "</li>"
        for r in report["references"]
    )

    caveats = []
    if not report["model_looked"]:
        caveats.append(
            "No multimodal model examined these frames. Locations carry a pixel screen only, "
            "and characters were not checked at all."
        )
    if report["summary"]["storyboard_episodes"]:
        caveats.append(
            "Episodes "
            + ", ".join(str(e) for e in report["summary"]["storyboard_episodes"])
            + " were shot with the offline storyboard vendor. Those pixels are stand-ins, "
            "so agreement between them says nothing about whether a real video model drifts."
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
<table><thead><tr><th>Ep</th><th>Shot</th><th>Subject</th><th>Verdict</th><th>Distance</th><th>Evidence</th><th>Differences</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table>
<p><small>Thresholds: consistent ≤ {esc(report['thresholds']['same_place_max'])},
drifted ≥ {esc(report['thresholds']['different_place_min'])} —
see <code>{esc(report['thresholds']['calibration'])}</code>.</small></p>
</html>
"""
