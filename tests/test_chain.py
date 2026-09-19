"""First-frame chaining: continuing a shot from where the last one ended.

The value is a cut that does not jump; the danger is chaining two shots that
are not actually continuous, which pastes the wrong room into the frame and
the model dutifully keeps it. These tests are mostly about the danger.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oneword import chain, vendors  # noqa: E402


def shot(shot_id, location_id):
    return {"shot_id": str(shot_id), "location_id": location_id}


def make_clip(target: Path, colour: str = "navy", seconds: float = 1.0) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [vendors.ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"color=c={colour}:s=320x180:d={seconds}:r=12",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(target)],
        capture_output=True, check=True,
    )
    return target


class EligibilityTests(unittest.TestCase):
    def test_consecutive_shots_in_one_location_chain(self):
        self.assertTrue(chain.eligible(shot(1, "L1"), shot(2, "L1")))

    def test_a_cut_to_another_location_does_not_chain(self):
        """Otherwise the wrong room is pasted into the first frame."""

        self.assertFalse(chain.eligible(shot(1, "L1"), shot(2, "L2")))

    def test_non_adjacent_shots_do_not_chain(self):
        self.assertFalse(chain.eligible(shot(1, "L1"), shot(4, "L1")))

    def test_the_first_shot_has_nothing_to_continue(self):
        self.assertFalse(chain.eligible(None, shot(1, "L1")))

    def test_the_plan_links_only_the_runs(self):
        shots = [shot(1, "L1"), shot(2, "L1"), shot(3, "L2"), shot(4, "L2"), shot(5, "L1")]
        self.assertEqual(chain.plan(shots), {"2": "1", "4": "3"})

    def test_a_single_shot_episode_plans_nothing(self):
        self.assertEqual(chain.plan([shot(1, "L1")]), {})


class LastFrameTests(unittest.TestCase):
    def test_the_last_frame_of_a_clip_is_readable(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            frame = chain.last_frame(make_clip(root / "c.mp4"), root / "f.jpg")
            self.assertIsNotNone(frame)
            self.assertGreater(frame.stat().st_size, 200)

    def test_an_unreadable_clip_returns_none_rather_than_raising(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            broken = root / "broken.mp4"
            broken.write_bytes(b"not a video")
            self.assertIsNone(chain.last_frame(broken, root / "f.jpg"))

    def test_the_frame_encodes_as_a_data_url(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            frame = chain.last_frame(make_clip(root / "c.mp4"), root / "f.jpg")
            url = chain.data_url(frame)
        self.assertTrue(url.startswith("data:image/jpeg;base64,"))


class StaleChainTests(unittest.TestCase):
    """The case that would otherwise fail silently."""

    def events(self, *pairs):
        return [{"shot_id": str(s), "round": r} for s, r in pairs]

    def test_a_repaired_source_makes_its_dependent_stale(self):
        stale = chain.stale_links(
            {"2": "1"}, self.events((1, 0), (2, 0), (1, 1))
        )
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["shot_id"], "2")
        self.assertIn("regenerated", stale[0]["reason"])

    def test_a_dependent_repaired_after_its_source_is_not_stale(self):
        """Shot 2 was re-shot last, so it continues the current shot 1."""

        self.assertEqual(
            chain.stale_links({"2": "1"}, self.events((1, 0), (2, 0), (1, 1), (2, 2))),
            [],
        )

    def test_a_clean_run_has_no_stale_chains(self):
        self.assertEqual(chain.stale_links({"2": "1"}, self.events((1, 0), (2, 0))), [])


class ArkPayloadTests(unittest.TestCase):
    def config(self):
        return vendors.ArkConfig(
            api_key="k", model="doubao-seedance-1-0-lite-t2v-250428",
            resolution="720p", duration=5, budget_cny=10.0,
        )

    def submit(self, first_frame):
        seen = {}

        class R(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def opener(request, timeout=None):
            seen["body"] = json.loads(request.data.decode("utf-8"))
            return R(json.dumps({"id": "t"}).encode("utf-8"))

        vendors.SeedanceVendor(self.config(), opener=opener).submit("a stairwell", first_frame)
        return seen["body"]

    def test_without_a_first_frame_the_payload_is_text_only(self):
        body = self.submit(None)
        self.assertEqual([item["type"] for item in body["content"]], ["text"])

    def test_a_first_frame_is_sent_inline_with_the_right_role(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            frame = chain.last_frame(make_clip(root / "c.mp4"), root / "f.jpg")
            body = self.submit(frame)
        image = [item for item in body["content"] if item["type"] == "image_url"]
        self.assertEqual(len(image), 1)
        self.assertEqual(image[0]["role"], "first_frame")
        self.assertTrue(image[0]["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_the_offline_vendor_declares_it_takes_no_first_frame(self):
        self.assertFalse(vendors.AnimaticVendor().accepts_first_frame)

    def test_the_paid_vendor_declares_it_does(self):
        self.assertTrue(vendors.SeedanceVendor.accepts_first_frame)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class PipelineChainingTests(unittest.TestCase):
    """The wiring: does a chaining vendor actually receive the frames?"""

    def run_episode(self, *, accepts: bool, chaining: str = "auto", failing=None):
        from oneword import voice
        from oneword.bible import build_bible
        from oneword.pipeline import run_episode

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_oneword import ScriptedAuditor, StubVendor

        class ChainVendor(StubVendor):
            accepts_first_frame = accepts

            def __init__(self):
                super().__init__()
                self.first_frames = {}

            def generate(self, shot, prompt, attempt, target):
                self.first_frames.setdefault(str(shot["shot_id"]), []).append(
                    shot.get("first_frame")
                )
                return super().generate(shot, prompt, attempt, target)

        bible, _ = build_bible("rust", episodes=1, shots=4, allow_model=False)
        # The fallback bible alternates L1/L2/L1/L2, so force a real run of one
        # location: shots 1 and 2 share it, shot 3 cuts away.
        for index, location in enumerate(["L1", "L1", "L2", "L2"]):
            bible.data["episodes"][0]["beats"][index]["location_id"] = location

        vendor = ChainVendor()
        tmp = TemporaryDirectory()
        report = run_episode(
            bible, 1, Path(tmp.name),
            vendor=vendor,
            auditor=ScriptedAuditor(failing or {}),
            voice_engine=voice.SilentVoice(),
            chaining=chaining,
        )
        return tmp, vendor, report

    def test_a_chained_shot_receives_the_previous_shots_frame(self):
        tmp, vendor, report = self.run_episode(accepts=True)
        with tmp:
            self.assertEqual(report["chain_links"], {"2": "1", "4": "3"})
            self.assertIsNotNone(vendor.first_frames["2"][0])
            self.assertIsNotNone(vendor.first_frames["4"][0])

    def test_the_first_shot_and_a_cut_receive_nothing(self):
        tmp, vendor, report = self.run_episode(accepts=True)
        with tmp:
            self.assertIsNone(vendor.first_frames["1"][0])
            self.assertIsNone(vendor.first_frames["3"][0])

    def test_a_vendor_that_cannot_take_a_frame_is_never_given_one(self):
        tmp, vendor, report = self.run_episode(accepts=False)
        with tmp:
            self.assertEqual(report["chain_links"], {})
            self.assertTrue(all(f is None for frames in vendor.first_frames.values() for f in frames))

    def test_chaining_off_disables_it_for_a_capable_vendor(self):
        tmp, vendor, report = self.run_episode(accepts=True, chaining="off")
        with tmp:
            self.assertEqual(report["chain_links"], {})

    def test_the_report_names_what_each_shot_continues(self):
        tmp, vendor, report = self.run_episode(accepts=True)
        with tmp:
            continues = {
                event["shot_id"]: event["continues_shot"]
                for event in report["generation_events"]
            }
        self.assertEqual(continues["2"], "1")
        self.assertIsNone(continues["1"])

    def test_repairing_a_source_shot_is_reported_as_a_stale_chain(self):
        """Shot 1 is re-shot after shot 2 was chained from it."""

        tmp, vendor, report = self.run_episode(accepts=True, failing={"1": 1})
        with tmp:
            stale = report["stale_chains"]
        self.assertEqual([item["shot_id"] for item in stale], ["2"])
