"""The join that turns drift numbers into an explanation.

`oneword drift` says how far apart two frames are. The episode reports say how
each shot was made. Neither alone answers "why did this one drift", and the
answer was sitting in two files that nobody joined.

These run the script against JSON fixtures — no clips, no ffmpeg, no spending.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "explain_drift.py"


def finding(episode, shot_id, name, verdict, composite, colour=None, structure=0.007):
    return {
        "episode": episode, "shot_id": str(shot_id), "kind": "location",
        "id": "L1", "name": name, "verdict": verdict,
        "evidence_source": "REFERENCE PIXEL SCREEN",
        "distance": {
            "colour": colour if colour is not None else composite,
            "structure": structure,
            "composite": composite,
        },
        "differences": [], "tripped_by": "colour",
    }


def write_series(root: Path, findings, episodes):
    root.mkdir(parents=True, exist_ok=True)
    (root / "series-drift-report.json").write_text(
        json.dumps(
            {
                "series_title": "《rust》", "seed_word": "rust",
                "episodes": sorted(episodes),
                "thresholds": {"same_place_max": 0.03, "different_place_min": 0.09},
                "findings": findings,
                "summary": {"checked": len(findings), "drifted": 0, "review": 0,
                            "not_checked": 8},
            }
        ),
        encoding="utf-8",
    )
    for number, data in episodes.items():
        directory = root / f"episode-{number:02d}"
        directory.mkdir(exist_ok=True)
        (directory / "episode-report.json").write_text(json.dumps(data), encoding="utf-8")


def run(root: Path):
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), str(root)],
        capture_output=True, text=True, check=False,
    )
    return completed.returncode, completed.stdout + completed.stderr


class ExplainTests(unittest.TestCase):
    def test_it_groups_the_rows_by_how_each_shot_was_made(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "rust"
            write_series(
                root,
                [finding(1, 2, "Stairwell C", "CONSISTENT", 0.010),
                 finding(2, 3, "Unit 704", "DRIFTED", 0.112, colour=0.168)],
                {
                    1: {"episode": 1, "chain_links": {"2": "1"}, "generation_events": [
                        {"shot_id": "2", "attempt": 1, "continues_shot": "1",
                         "chain_dropped": None, "references_dropped": None}]},
                    2: {"episode": 2, "chain_links": {"4": "3"}, "generation_events": [
                        {"shot_id": "3", "attempt": 1, "continues_shot": None,
                         "chain_dropped": None, "references_dropped": None}]},
                },
            )
            code, out = run(root)

        self.assertEqual(code, 0)
        self.assertIn("chained", out)
        self.assertIn("not chained", out)

    def test_a_run_that_held_the_cut_but_lost_the_series_is_named(self):
        """The distinction the pairwise matrix turned up.

        Tight to the shot it continued, far from the reference: the cut is
        fine and the whole episode moved. Regenerating one shot would reshoot
        something that already matches its neighbour exactly.
        """

        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "rust"
            drifted = finding(2, 2, "Stairwell C", "REVIEW", 0.075, colour=0.109)
            drifted["neighbour_distance"] = {
                "colour": 0.010, "structure": 0.008, "composite": 0.010,
            }
            drifted["continues_shot"] = "1"
            write_series(
                root, [drifted],
                {2: {"episode": 2, "chain_links": {"2": "1"}, "generation_events": [
                    {"shot_id": "2", "attempt": 1, "continues_shot": "1",
                     "chain_dropped": None, "references_dropped": None}]}},
            )
            code, out = run(root)

        self.assertEqual(code, 0)
        self.assertIn("vs shot 1: 0.010", out)
        self.assertIn("the whole run moved together", out)
        self.assertIn("re-establishing", out)

    def test_a_refused_first_frame_is_named_as_the_reason(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "rust"
            write_series(
                root,
                [finding(1, 4, "Unit 704", "DRIFTED", 0.112, colour=0.168)],
                {1: {"episode": 1, "chain_links": {"4": "3"}, "generation_events": [
                    {"shot_id": "4", "attempt": 1, "continues_shot": None,
                     "chain_dropped": "InputImageSensitiveContentDetected.PrivacyInformation",
                     "references_dropped": None}]}},
            )
            code, out = run(root)

        self.assertEqual(code, 0)
        self.assertIn("chain refused", out)
        self.assertIn("Sensitive", out)

    def test_refused_portraits_get_their_own_section(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "rust"
            write_series(
                root,
                [finding(1, 2, "Stairwell C", "REVIEW", 0.072)],
                {1: {"episode": 1, "chain_links": {}, "generation_events": [
                    {"shot_id": "2", "attempt": 1, "continues_shot": None,
                     "chain_dropped": None,
                     "references_dropped": "the input image may contain a real person"}]}},
            )
            code, out = run(root)

        self.assertEqual(code, 0)
        self.assertIn("portraits were refused", out)
        self.assertIn("real person", out)

    def test_the_refusal_claim_is_withheld_when_the_data_contradicts_it(self):
        """A neat conclusion the data does not support is worse than none.

        Here a refused shot is *closer* to its reference than a chained one, so
        the "a refusal has a price" line must not be printed.
        """

        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "rust"
            write_series(
                root,
                [finding(1, 2, "Stairwell C", "REVIEW", 0.080),
                 finding(2, 3, "Unit 704", "CONSISTENT", 0.020)],
                {
                    1: {"episode": 1, "chain_links": {"2": "1"}, "generation_events": [
                        {"shot_id": "2", "attempt": 1, "continues_shot": "1",
                         "chain_dropped": None, "references_dropped": None}]},
                    2: {"episode": 2, "chain_links": {"3": "2"}, "generation_events": [
                        {"shot_id": "3", "attempt": 1, "continues_shot": None,
                         "chain_dropped": "refused", "references_dropped": None}]},
                },
            )
            code, out = run(root)

        self.assertEqual(code, 0)
        self.assertNotIn("measurable price", out)

    def test_unchecked_characters_are_surfaced_with_what_to_do(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "rust"
            write_series(
                root, [finding(1, 2, "Stairwell C", "CONSISTENT", 0.010)],
                {1: {"episode": 1, "chain_links": {}, "generation_events": []}},
            )
            _, out = run(root)
        self.assertIn("LLM_API_KEY", out)

    def test_a_directory_without_a_drift_report_says_what_to_run(self):
        with TemporaryDirectory() as tmp:
            code, out = run(Path(tmp))
        self.assertEqual(code, 2)
        self.assertIn("oneword drift", out)


if __name__ == "__main__":
    unittest.main()
