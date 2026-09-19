"""Not paying twice for a clip that is already on disk.

This exists because of a real bill: a paid run died at shot 8 with seven
finished clips already downloaded, and the only way to continue was to buy all
seven again. The tests below are mostly about the other half of the problem —
making sure a clip is *never* reused when the next run would have asked for
something different, because a wrong reuse is far more expensive than a
re-purchase.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from oneword import ledger, styles, voice  # noqa: E402
from oneword.bible import build_bible  # noqa: E402
from oneword.pipeline import run_episode  # noqa: E402
from test_oneword import ScriptedAuditor, StubVendor  # noqa: E402


class CountingVendor(StubVendor):
    """Every generate() is a purchase, so the count is the bill."""

    accepts_first_frame = False
    unit_cost = 1.86

    def __init__(self):
        super().__init__()
        self.generated: list[str] = []

    def generate(self, shot, prompt, attempt, target):
        self.generated.append(str(shot["shot_id"]))
        return super().generate(shot, prompt, attempt, target)


def run(root: Path, bible, vendor, **kwargs):
    return run_episode(
        bible, 1, root,
        vendor=vendor,
        auditor=ScriptedAuditor(kwargs.pop("failing", {})),
        voice_engine=voice.SilentVoice(),
        **kwargs,
    )


def make_bible(**kwargs):
    bible, _ = build_bible("rust", episodes=1, shots=3, allow_model=False, **kwargs)
    return bible


class LedgerTests(unittest.TestCase):
    def test_a_clip_records_what_it_was_made_from(self):
        with TemporaryDirectory() as tmp:
            clip = Path(tmp) / "shot-01-take-01.mp4"
            clip.write_bytes(b"x")
            ledger.record(clip, shot_id="1", attempt=1, prompt="a stairwell",
                          provider="seedance-ark", cost_cny=1.86)
            entry = json.loads(ledger.sidecar_for(clip).read_text(encoding="utf-8"))
        self.assertEqual(entry["shot_id"], "1")
        self.assertEqual(entry["cost_cny"], 1.86)
        self.assertEqual(entry["prompt_fingerprint"], ledger.fingerprint("a stairwell"))

    def test_the_same_prompt_is_found(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "shot-01-take-01.mp4"
            clip.write_bytes(b"x")
            ledger.record(clip, shot_id="1", attempt=1, prompt="p", provider="v")
            found = ledger.find_reusable(root, "1", "p", "v")
        self.assertIsNotNone(found)
        self.assertEqual(found.attempt, 1)

    def test_a_changed_prompt_is_not_reused(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "shot-01-take-01.mp4"
            clip.write_bytes(b"x")
            ledger.record(clip, shot_id="1", attempt=1, prompt="p", provider="v")
            self.assertIsNone(ledger.find_reusable(root, "1", "DIFFERENT", "v"))

    def test_a_different_vendor_is_not_reused(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "shot-01-take-01.mp4"
            clip.write_bytes(b"x")
            ledger.record(clip, shot_id="1", attempt=1, prompt="p", provider="v")
            self.assertIsNone(ledger.find_reusable(root, "1", "p", "other"))

    def test_a_vanished_file_is_not_reused(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "shot-01-take-01.mp4"
            clip.write_bytes(b"x")
            ledger.record(clip, shot_id="1", attempt=1, prompt="p", provider="v")
            clip.unlink()
            self.assertIsNone(ledger.find_reusable(root, "1", "p", "v"))

    def test_an_empty_file_is_not_reused(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "shot-01-take-01.mp4"
            clip.write_bytes(b"")
            ledger.record(clip, shot_id="1", attempt=1, prompt="p", provider="v")
            self.assertIsNone(ledger.find_reusable(root, "1", "p", "v"))

    def test_the_newest_take_wins(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for attempt in (1, 2):
                clip = root / f"shot-01-take-0{attempt}.mp4"
                clip.write_bytes(b"x")
                ledger.record(clip, shot_id="1", attempt=attempt, prompt="p", provider="v")
            self.assertEqual(ledger.find_reusable(root, "1", "p", "v").attempt, 2)


class ResumeTests(unittest.TestCase):
    def test_a_second_run_buys_nothing(self):
        with TemporaryDirectory() as tmp:
            root, bible = Path(tmp), make_bible()
            first = CountingVendor()
            run(root, bible, first)
            second = CountingVendor()
            report = run(root, bible, second)
        self.assertEqual(len(first.generated), 3)
        self.assertEqual(second.generated, [])
        self.assertEqual(report["summary"]["reused_shot_ids"], ["1", "2", "3"])

    def test_the_saving_is_reported_in_money(self):
        with TemporaryDirectory() as tmp:
            root, bible = Path(tmp), make_bible()
            run(root, bible, CountingVendor())
            report = run(root, bible, CountingVendor())
        self.assertAlmostEqual(report["summary"]["reused_saving_cny"], 3 * 1.86, places=2)

    def test_an_interrupted_run_only_buys_what_is_missing(self):
        """The case this exists for: seven clips saved, the eighth lost."""

        with TemporaryDirectory() as tmp:
            root, bible = Path(tmp), make_bible()
            run(root, bible, CountingVendor())
            # Lose the last shot, as a crash mid-download would.
            for path in (root / "clips").glob("shot-03-*"):
                path.unlink()
            second = CountingVendor()
            report = run(root, bible, second)
        self.assertEqual(second.generated, ["3"])
        self.assertEqual(report["summary"]["reused_shot_ids"], ["1", "2"])

    def test_fresh_ignores_everything_on_disk(self):
        with TemporaryDirectory() as tmp:
            root, bible = Path(tmp), make_bible()
            run(root, bible, CountingVendor())
            second = CountingVendor()
            report = run(root, bible, second, resume=False)
        self.assertEqual(len(second.generated), 3)
        self.assertEqual(report["summary"]["reused_shot_ids"], [])

    def test_a_restyle_is_never_resumed_from_the_old_look(self):
        """The reuse that would quietly ship two different-looking episodes."""

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            bible = make_bible(style="noir")
            run(root, bible, CountingVendor())

            restyled = make_bible(style="anime")
            second = CountingVendor()
            report = run(root, restyled, second)
        self.assertEqual(len(second.generated), 3)
        self.assertEqual(report["summary"]["reused_shot_ids"], [])

    def test_an_edited_bible_is_not_resumed(self):
        with TemporaryDirectory() as tmp:
            root, bible = Path(tmp), make_bible()
            run(root, bible, CountingVendor())

            edited = make_bible()
            edited.data["locations"]["L1"]["locked_description"] = "a different room entirely"
            second = CountingVendor()
            run(root, edited, second)
        # Only the shots in L1 are re-bought; the rest are still valid.
        self.assertTrue(second.generated)
        self.assertLess(len(second.generated), 3)

    def test_the_episode_still_assembles_from_reused_clips(self):
        with TemporaryDirectory() as tmp:
            root, bible = Path(tmp), make_bible()
            run(root, bible, CountingVendor())
            report = run(root, bible, CountingVendor())
            self.assertTrue((root / report["outputs"]["video"]).is_file())

    def test_the_report_still_counts_every_shot_once(self):
        """Resume must not break the invariant that round 0 covers the episode."""

        with TemporaryDirectory() as tmp:
            root, bible = Path(tmp), make_bible()
            run(root, bible, CountingVendor())
            report = run(root, bible, CountingVendor())
        initial = [e for e in report["generation_events"] if e["round"] == 0]
        self.assertEqual(len(initial), report["policy"]["shot_count"])
        self.assertTrue(all(e.get("reused") for e in initial))


if __name__ == "__main__":
    unittest.main(verbosity=2)
