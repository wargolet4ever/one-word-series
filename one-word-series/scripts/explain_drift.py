#!/usr/bin/env python3
"""Why did *this* appearance drift?

`oneword drift` answers "how far apart are these two frames". It does not
answer "what was different about how this shot was made", and that is usually
the question worth asking, because the answer is already on disk: every episode
report records whether a shot continued the previous one, whether the platform
refused its first frame, and whether the cast portraits were sent or dropped.

This joins the two. It buys nothing and generates nothing — it reads files a
finished run already wrote:

    python scripts/explain_drift.py out/rust

It was built to test one hypothesis, which it then half refuted — the useful
outcome. The idea was that a chained shot should sit almost on top of the
series reference. The first real footage says a chained shot sits on top of
**the shot it continued**, and inherits whatever offset that shot already had:
one chained shot measured 0.010 from its reference and another 0.075, because
the second faithfully continued a shot that had already drifted.

So the two numbers worth reading together are distance-to-reference and
distance-to-predecessor. Tight to the predecessor but far from the reference
means the whole run of shots moved as a unit, which wants re-establishing the
episode rather than regenerating one shot. The `vs shot N` column carries the
second number where a run recorded it.
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oneword.drift import (  # noqa: E402
    CHAINED, CHAIN_REFUSED, CHAIN_UNKNOWN as UNKNOWN, UNCHAINED, chain_state_for,
)


def _episode_reports(root: Path) -> dict[int, dict[str, Any]]:
    reports = {}
    for path in sorted(root.glob("episode-*/episode-report.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        reports[int(data["episode"])] = data
    return reports


def shot_state(report: dict[str, Any], shot_id: str) -> dict[str, Any]:
    """How the final take of this shot was made, plus what else was dropped.

    The chain half is `drift.chain_state_for` — the same function the drift pass
    itself uses to pick a band, so this script cannot drift away from it.
    """

    state = dict(chain_state_for(report, shot_id))
    events = [
        event for event in report.get("generation_events", [])
        if str(event["shot_id"]) == str(shot_id)
    ]
    last = events[-1] if events else {}
    state["portraits"] = str(last.get("references_dropped") or "")
    state["attempt"] = last.get("attempt")
    return state


def _describe(values: list[float]) -> str:
    if not values:
        return "—"
    if len(values) == 1:
        return f"n=1  {values[0]:.3f}"
    return (
        f"n={len(values)}  min {min(values):.3f} · "
        f"p50 {statistics.median(values):.3f} · max {max(values):.3f}"
    )


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print("usage: python scripts/explain_drift.py <series dir>", file=sys.stderr)
        return 2

    root = Path(argv[0])
    drift_path = root / "series-drift-report.json"
    if not drift_path.is_file():
        print(f"no series-drift-report.json in {root} — run `oneword drift {root}` first",
              file=sys.stderr)
        return 2

    drift = json.loads(drift_path.read_text(encoding="utf-8"))
    reports = _episode_reports(root)

    print(f"{drift['series_title']} · episodes {', '.join(str(e) for e in drift['episodes'])}")
    print(f"thresholds: consistent ≤ {drift['thresholds']['same_place_max']}, "
          f"drifted ≥ {drift['thresholds']['different_place_min']}")
    print()

    header = f"{'ep':>2}  {'shot':>4}  {'subject':<16} {'verdict':<12} {'comp':>6} {'col':>6} {'str':>6}  how it was shot"
    print(header)
    print("-" * len(header))

    buckets: dict[str, list[float]] = {}
    rows = 0
    portrait_notes: list[str] = []
    carried: list[dict[str, Any]] = []

    for finding in drift["findings"]:
        distance = finding.get("distance")
        if not distance:
            continue
        report = reports.get(int(finding["episode"]))
        state = shot_state(report, finding["shot_id"]) if report else {
            "state": UNKNOWN, "detail": "", "portraits": ""
        }
        how = state["state"]
        if state["detail"] and how != CHAIN_REFUSED:
            how = f"{how} ({state['detail']})"
        elif how == CHAIN_REFUSED:
            how = f"{how}: {state['detail'][:48]}"

        neighbour = finding.get("neighbour_distance")
        versus = (
            f"  vs shot {finding.get('continues_shot', '?')}: {neighbour['composite']:.3f}"
            if neighbour else ""
        )
        print(
            f"{finding['episode']:>2}  {finding['shot_id']:>4}  {finding['name'][:16]:<16} "
            f"{finding['verdict']:<12} {distance['composite']:>6.3f} "
            f"{distance['colour']:>6.3f} {distance['structure']:>6.3f}  {how}{versus}"
        )
        buckets.setdefault(state["state"], []).append(distance["composite"])
        # Held the cut, lost the series: the run moved as a unit.
        if neighbour and neighbour["composite"] <= 0.03 < distance["composite"]:
            carried.append({
                "episode": finding["episode"], "shot_id": finding["shot_id"],
                "reference": distance["composite"], "neighbour": neighbour["composite"],
                "continues": finding.get("continues_shot", "?"),
            })
        rows += 1
        if state.get("portraits"):
            portrait_notes.append(
                f"  ep{finding['episode']} shot {finding['shot_id']}: "
                f"portraits dropped — {state['portraits'][:70]}"
            )

    if not rows:
        print("(no screened appearances — every finding was a reference or unchecked)")
        return 0

    print()
    print("By how the shot was made")
    print("-" * len(header))
    for state in (CHAINED, UNCHAINED, CHAIN_REFUSED, UNKNOWN):
        if state in buckets:
            print(f"  {state:<16} {_describe(buckets[state])}")

    # Two claims, each stated only where the data carries it.
    chained = buckets.get(CHAINED, [])
    refused = buckets.get(CHAIN_REFUSED, [])
    print()
    if refused and chained and min(refused) > max(chained):
        print(f"Every shot whose first frame was refused ({min(refused):.3f} at best) is")
        print(f"further from its reference than every chained one ({max(chained):.3f} at")
        print("worst). On this footage a moderation refusal has a measurable price.")
    elif not chained:
        print("No shot in this series was successfully chained, so there is nothing to")
        print("compare against. Either the vendor takes no first frame, --chain was off,")
        print("or every attempt was refused — the rows above say which.")

    if carried:
        print()
        print("These shots sit on top of the shot they continued but away from the series")
        print("reference, so the cut held and the whole run moved together:")
        for row in carried:
            print(f"  ep{row['episode']} shot {row['shot_id']}: "
                  f"{row['reference']:.3f} from the reference, "
                  f"{row['neighbour']:.3f} from shot {row['continues']}")
        print("Regenerating one of them reshoots a shot that matches its neighbour exactly.")
        print("What needs re-establishing against the reference is the episode.")
    elif not any(f.get("neighbour_distance") for f in drift["findings"]):
        print()
        print("This report predates the predecessor comparison, so only distance-to-reference")
        print("is available. Rerun `oneword drift` to get both numbers.")

    if portrait_notes:
        print()
        print("Shots whose cast portraits were refused")
        for note in portrait_notes:
            print(note)

    unchecked = drift["summary"]["not_checked"]
    if unchecked:
        print()
        print(f"{unchecked} appearance(s) were not checked at all — every one of them a")
        print("character. Set LLM_API_KEY / LLM_MODEL and rerun `oneword drift` to have a")
        print("model compare them; a whole-frame fingerprint cannot honestly judge a face.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
