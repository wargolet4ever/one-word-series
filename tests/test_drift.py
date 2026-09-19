"""Cross-episode drift: the checks that make the series claim mean something.

These tests build a two-episode series on disk out of synthetic rooms with a
known answer, so "episode 2 is somewhere else" is a fact the test knows and the
detector has to discover — not a mock that simply returns what we want.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from calibrate_drift import make_room, make_shot  # noqa: E402

from oneword import drift, metrics, vendors  # noqa: E402
from oneword.bible import build_bible  # noqa: E402
from oneword.registry import ReferenceRegistry, RegistryError  # noqa: E402


def clip_from_image(image, target: Path, seconds: float = 0.6) -> Path:
    """A real mp4 whose every frame is this picture."""

    target.parent.mkdir(parents=True, exist_ok=True)
    png = target.with_suffix(".png")
    image.save(png)
    subprocess.run(
        [vendors.ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
         "-loop", "1", "-i", str(png), "-t", f"{seconds}", "-r", "12",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(target)],
        capture_output=True, check=True,
    )
    png.unlink(missing_ok=True)
    return target


class SeriesBuilder:
    """Writes the on-disk shape `drift.audit_series` reads."""

    def __init__(self, root: Path) -> None:
        self.root = root
        bible, _ = build_bible("rust", episodes=3, shots=2, allow_model=False)
        self.bible = bible
        bible.save(root / "bible.json")
        self.location_names = {
            lid: entry["name"] for lid, entry in bible.data["locations"].items()
        }
        self.character_names = {
            cid: entry["name"] for cid, entry in bible.data["characters"].items()
        }

    def episode(self, number: int, shots: list[dict], *, generative: bool = True) -> None:
        directory = self.root / f"episode-{number:02d}"
        (directory / "clips").mkdir(parents=True, exist_ok=True)
        entries = []
        for index, shot in enumerate(shots, start=1):
            filename = f"shot-{index:02d}-take-01.mp4"
            clip_from_image(shot["image"], directory / "clips" / filename)
            entries.append(
                {
                    "shot_id": str(index),
                    "beat": "establish",
                    "location": self.location_names[shot["location_id"]],
                    "characters": [self.character_names[c] for c in shot.get("character_ids", [])],
                    "model_tier": "economy",
                    "final_take": 1,
                    "file": filename,
                    "line": "",
                    "prompt": "",
                }
            )
        report = {
            "report_version": "series-episode-1",
            "episode": number,
            "vendor": "test-vendor",
            "vendor_is_generative": generative,
            "shots": entries,
        }
        (directory / "episode-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )


class RegistryTests(unittest.TestCase):
    def test_first_appearance_becomes_the_reference(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            frame = root / "f.png"
            make_room(1).save(frame)
            registry = ReferenceRegistry(root)
            self.assertFalse(registry.has("location", "L1"))
            entry = registry.adopt("location", "L1", frame, episode=1, shot_id="1")
            self.assertEqual(entry["episode"], 1)
            self.assertTrue(registry.has("location", "L1"))
            self.assertTrue(registry.frame_path("location", "L1").is_file())

    def test_a_reference_is_never_silently_rebased(self):
        """The boiling-frog failure: each episode passing against the last one."""

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, second = root / "a.png", root / "b.png"
            make_room(1).save(first)
            make_room(2).save(second)
            registry = ReferenceRegistry(root)
            registry.adopt("location", "L1", first, episode=1, shot_id="1")
            with self.assertRaises(RegistryError):
                registry.adopt("location", "L1", second, episode=2, shot_id="1")

    def test_a_forced_rebase_is_recorded(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, second = root / "a.png", root / "b.png"
            make_room(1).save(first)
            make_room(2).save(second)
            registry = ReferenceRegistry(root)
            registry.adopt("location", "L1", first, episode=1, shot_id="1")
            entry = registry.adopt(
                "location", "L1", second, episode=2, shot_id="1", force=True
            )
            self.assertEqual(entry["episode"], 2)
            self.assertEqual(len(entry["rebased_from"]), 1)
            self.assertEqual(entry["rebased_from"][0]["episode"], 1)

    def test_the_registry_survives_a_restart(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            frame = root / "f.png"
            make_room(1).save(frame)
            ReferenceRegistry(root).adopt("location", "L1", frame, episode=1, shot_id="1")
            self.assertTrue(ReferenceRegistry(root).has("location", "L1"))


class MetricsTests(unittest.TestCase):
    def test_a_frame_matches_itself_exactly(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.png"
            make_room(7).save(path)
            print_a = metrics.fingerprint(path)
            self.assertEqual(metrics.distance(print_a, print_a)["composite"], 0.0)

    def test_mismatched_fingerprint_versions_are_refused(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.png"
            make_room(7).save(path)
            current = metrics.fingerprint(path)
            stale = dict(current, version=current["version"] - 1)
            with self.assertRaises(ValueError):
                metrics.distance(current, stale)

    def test_a_relight_moves_less_than_a_different_room(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            room = make_room(11)
            other = make_room(12)
            paths = {}
            for name, image in (
                ("base", make_shot(room, 1)),
                ("relit", make_shot(room, 2)),
                ("elsewhere", make_shot(other, 1)),
            ):
                paths[name] = root / f"{name}.jpg"
                image.save(paths[name], quality=85)
            prints = {name: metrics.fingerprint(path) for name, path in paths.items()}
            same = metrics.distance(prints["base"], prints["relit"])["composite"]
            different = metrics.distance(prints["base"], prints["elsewhere"])["composite"]
            self.assertLess(same, different)


class DriftTests(unittest.TestCase):
    def build(self, tmp: Path, *, swap_location_in_episode_2: bool):
        builder = SeriesBuilder(tmp)
        stairwell = make_room(21)
        flat = make_room(22)
        somewhere_else = make_room(99)

        builder.episode(1, [
            {"image": make_shot(stairwell, 1), "location_id": "L1", "character_ids": ["C1"]},
            {"image": make_shot(flat, 1), "location_id": "L2", "character_ids": ["C2"]},
        ])
        episode_two_stairwell = somewhere_else if swap_location_in_episode_2 else stairwell
        builder.episode(2, [
            {"image": make_shot(episode_two_stairwell, 5), "location_id": "L1", "character_ids": ["C1"]},
            {"image": make_shot(flat, 5), "location_id": "L2", "character_ids": ["C2"]},
        ])
        return builder

    def test_a_different_room_in_episode_two_is_caught(self):
        with TemporaryDirectory() as tmp:
            self.build(Path(tmp), swap_location_in_episode_2=True)
            report = drift.audit_series(Path(tmp), use_model=False)

        stairwell = [
            f for f in report["findings"]
            if f["id"] == "L1" and f["episode"] == 2
        ]
        self.assertEqual(len(stairwell), 1)
        self.assertIn(stairwell[0]["verdict"], (drift.DRIFTED, drift.REVIEW))
        self.assertEqual(stairwell[0]["reference_episode"], 1)

    def test_the_same_room_again_is_not_flagged_as_drift(self):
        with TemporaryDirectory() as tmp:
            self.build(Path(tmp), swap_location_in_episode_2=False)
            report = drift.audit_series(Path(tmp), use_model=False)
        self.assertEqual(report["summary"]["drifted"], 0)

    def test_the_swapped_room_scores_further_than_the_kept_one(self):
        """The comparison is doing the work, not the verdict bands."""

        distances = {}
        for swapped in (False, True):
            with TemporaryDirectory() as tmp:
                self.build(Path(tmp), swap_location_in_episode_2=swapped)
                report = drift.audit_series(Path(tmp), use_model=False)
            distances[swapped] = next(
                f["distance"]["composite"] for f in report["findings"]
                if f["id"] == "L1" and f["episode"] == 2
            )
        self.assertGreater(distances[True], distances[False])

    def test_characters_are_reported_unchecked_never_passed(self):
        with TemporaryDirectory() as tmp:
            self.build(Path(tmp), swap_location_in_episode_2=False)
            report = drift.audit_series(Path(tmp), use_model=False)
        characters = [
            f for f in report["findings"]
            if f["kind"] == "character" and f["verdict"] != drift.REFERENCE_SET
        ]
        self.assertTrue(characters)
        for finding in characters:
            self.assertEqual(finding["verdict"], drift.NOT_CHECKED)
            self.assertIsNone(finding["distance"])

    def test_a_series_is_never_summarised_clean_while_anything_is_unchecked(self):
        with TemporaryDirectory() as tmp:
            self.build(Path(tmp), swap_location_in_episode_2=False)
            report = drift.audit_series(Path(tmp), use_model=False)
        self.assertGreater(report["summary"]["not_checked"], 0)
        self.assertNotEqual(report["summary"]["status"], "CONSISTENT")
        self.assertIn(report["summary"]["status"], ("PARTIAL", "REVIEW", "DRIFT FOUND"))

    def test_references_come_from_episode_one_and_stay_there(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.build(root, swap_location_in_episode_2=True)
            drift.audit_series(root, use_model=False)
            drift.audit_series(root, use_model=False)  # a second pass must not re-base
            registry = ReferenceRegistry(root)
        for kind, subject_id in registry.subjects():
            self.assertEqual(registry.get(kind, subject_id)["episode"], 1)

    def test_the_report_and_its_html_are_written(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.build(root, swap_location_in_episode_2=False)
            drift.audit_series(root, use_model=False)
            self.assertTrue((root / "series-drift-report.json").is_file())
            html = (root / "series-drift-report.html").read_text(encoding="utf-8")
        self.assertIn("cross-episode drift", html)
        self.assertIn("characters were not checked", html)

    def test_a_storyboard_run_is_flagged_as_proving_nothing(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            builder = SeriesBuilder(root)
            room = make_room(31)
            for number in (1, 2):
                builder.episode(
                    number,
                    [{"image": make_shot(room, number), "location_id": "L1", "character_ids": ["C1"]}],
                    generative=False,
                )
            report = drift.audit_series(root, use_model=False)
            html = (root / "series-drift-report.html").read_text(encoding="utf-8")
        self.assertEqual(report["summary"]["storyboard_episodes"], [1, 2])
        self.assertIn("stand-ins", html)


class DriftValidatorTests(unittest.TestCase):
    def base(self) -> dict:
        return {
            "summary": {"status": "REVIEW", "not_checked": 0},
            "findings": [],
        }

    def test_consistent_without_anyone_looking_is_rejected(self):
        report = self.base()
        report["findings"] = [
            {"verdict": drift.CONSISTENT, "evidence_source": drift.NOT_CHECKED,
             "differences": [], "kind": "location", "distance": None}
        ]
        with self.assertRaises(drift.DriftError):
            drift.validate_drift_report(report)

    def test_differences_without_a_visual_audit_are_rejected(self):
        report = self.base()
        report["findings"] = [
            {"verdict": drift.DRIFTED, "evidence_source": drift.PIXEL_SCREEN,
             "differences": [{"fact": "x", "evidence": "y", "severity": "regenerate"}],
             "kind": "location", "distance": None}
        ]
        with self.assertRaises(drift.DriftError):
            drift.validate_drift_report(report)

    def test_a_character_may_not_carry_a_whole_frame_distance(self):
        report = self.base()
        report["findings"] = [
            {"verdict": drift.CONSISTENT, "evidence_source": drift.VISUAL_AUDIT,
             "differences": [], "kind": "character",
             "distance": {"colour": 0.0, "structure": 0.0, "composite": 0.0}}
        ]
        with self.assertRaises(drift.DriftError):
            drift.validate_drift_report(report)

    def test_consistent_summary_with_unchecked_subjects_is_rejected(self):
        report = self.base()
        report["summary"] = {"status": "CONSISTENT", "not_checked": 2}
        with self.assertRaises(drift.DriftError):
            drift.validate_drift_report(report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
