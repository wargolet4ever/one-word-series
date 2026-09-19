"""Character portraits: the same face in shot 9 as in shot 1.

The bible locks a *description*, which is a type, not a person — so the danger
here is a tool that claims identity it never enforced. These tests are mostly
about that: a face is frozen once, is never adopted from a shot with two people
in it, is never reused across a change of look, and a refusal by the platform
is reported rather than swallowed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from unittest import mock  # noqa: E402

from oneword import cast, vendors  # noqa: E402
from tests.pricing import use_test_price  # noqa: E402
from oneword.cast import CastError, CastPortraits  # noqa: E402


def make_clip(target: Path, colour: str = "navy", seconds: float = 1.0) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [vendors.ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"color=c={colour}:s=320x180:d={seconds}:r=12",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(target)],
        capture_output=True, check=True,
    )
    return target


def make_image(target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [vendors.ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=teal:s=64x64:d=1", "-frames:v", "1",
         "-y", str(target)],
        capture_output=True, check=True,
    )
    return target


class SuppliedTests(unittest.TestCase):
    def test_a_spec_is_parsed_into_an_id_and_a_path(self):
        with TemporaryDirectory() as tmp:
            image = make_image(Path(tmp) / "face.jpg")
            self.assertEqual(cast.parse_supplied([f"C1={image}"]), {"C1": image})

    def test_a_spec_without_an_equals_sign_is_refused(self):
        with self.assertRaises(CastError):
            cast.parse_supplied(["face.jpg"])

    def test_a_missing_file_is_refused_before_the_run_starts(self):
        """Not halfway through, after four clips have been paid for."""

        with self.assertRaises(CastError):
            cast.parse_supplied(["C1=/nowhere/at/all.jpg"])


class FreezingTests(unittest.TestCase):
    def test_a_portrait_survives_a_new_index_object(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            CastPortraits(root).put(
                "C1", make_image(root / "face.jpg"), source="supplied", style="noir"
            )
            again = CastPortraits(root).get("C1")
            self.assertIsNotNone(again)
            self.assertEqual(again.source, "supplied")
            self.assertEqual(again.style, "noir")

    def test_a_second_put_does_not_replace_the_first(self):
        """Re-adopting a face each episode is how a cast quietly changes."""

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            portraits = CastPortraits(root)
            portraits.put("C1", make_image(root / "a.jpg"), source="adopted", style="noir")
            portraits.put("C1", make_image(root / "b.jpg"), source="supplied", style="noir")
            self.assertEqual(portraits.get("C1").source, "adopted")

    def test_force_is_how_a_replacement_happens(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            portraits = CastPortraits(root)
            portraits.put("C1", make_image(root / "a.jpg"), source="adopted", style="noir")
            portraits.put(
                "C1", make_image(root / "b.jpg"), source="supplied", style="noir", force=True
            )
            self.assertEqual(portraits.get("C1").source, "supplied")

    def test_clearing_removes_the_files_too(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            portraits = CastPortraits(root)
            portraits.put("C1", make_image(root / "a.jpg"), source="adopted", style="noir")
            stored = portraits.get("C1").path
            portraits.clear()
            self.assertIsNone(portraits.get("C1"))
            self.assertFalse(stored.exists())

    def test_a_deleted_file_is_not_reported_as_a_portrait(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            portraits = CastPortraits(root)
            portraits.put("C1", make_image(root / "a.jpg"), source="adopted", style="noir")
            portraits.get("C1").path.unlink()
            self.assertIsNone(portraits.get("C1"))

    def test_an_index_from_another_version_is_ignored_rather_than_misread(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cast").mkdir()
            (root / "cast" / "index.json").write_text(
                json.dumps({"cast_version": "something-else", "portraits": {"C1": {}}}),
                encoding="utf-8",
            )
            self.assertEqual(CastPortraits(root).data["portraits"], {})


class AdoptionTests(unittest.TestCase):
    def test_a_face_is_taken_from_a_clip(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = make_clip(root / "shot.mp4")
            portrait = CastPortraits(root).adopt_from_clip(
                "C1", clip, style="noir", from_shot="ep1-shot1"
            )
            self.assertIsNotNone(portrait)
            self.assertEqual(portrait.source, "adopted")
            self.assertTrue(portrait.path.stat().st_size)

    def test_adoption_leaves_an_existing_portrait_alone(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            portraits = CastPortraits(root)
            portraits.put("C1", make_image(root / "a.jpg"), source="supplied", style="noir")
            portraits.adopt_from_clip("C1", make_clip(root / "shot.mp4"), style="noir")
            self.assertEqual(portraits.get("C1").source, "supplied")

    def test_an_unreadable_clip_yields_no_portrait_rather_than_an_error(self):
        """A failed frame grab must not take down a shot that was paid for."""

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            broken = root / "broken.mp4"
            broken.write_bytes(b"not a video")
            self.assertIsNone(CastPortraits(root).adopt_from_clip("C1", broken))


class ForShotTests(unittest.TestCase):
    def test_only_the_people_in_this_shot_are_sent(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            portraits = CastPortraits(root)
            portraits.put("C1", make_image(root / "a.jpg"), source="supplied", style="noir")
            portraits.put("C2", make_image(root / "b.jpg"), source="supplied", style="noir")
            self.assertEqual(len(portraits.for_shot(["C2"])), 1)
            self.assertEqual(portraits.for_shot(["C2"])[0].name, portraits.get("C2").path.name)

    def test_a_character_without_a_portrait_is_simply_absent(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            portraits = CastPortraits(root)
            portraits.put("C1", make_image(root / "a.jpg"), source="supplied", style="noir")
            self.assertEqual(len(portraits.for_shot(["C1", "C9"])), 1)

    def test_no_more_than_the_platform_cap_is_sent(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            portraits = CastPortraits(root)
            for index in range(4):
                portraits.put(
                    f"C{index}", make_image(root / f"{index}.jpg"),
                    source="supplied", style="noir",
                )
            faces = portraits.for_shot([f"C{i}" for i in range(4)])
            self.assertEqual(len(faces), cast.MAX_REFERENCES_PER_SHOT)

    def test_styles_present_reports_what_the_portraits_were_shot_in(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            portraits = CastPortraits(root)
            portraits.put("C1", make_image(root / "a.jpg"), source="supplied", style="noir")
            portraits.put("C2", make_image(root / "b.jpg"), source="adopted", style="anime")
            self.assertEqual(portraits.styles_present(), {"noir", "anime"})


class VendorReferenceTests(unittest.TestCase):
    """The payload, and what happens when the platform says no."""

    def config(self):
        return vendors.ArkConfig(
            api_key="k", model="m", resolution="720p", ratio="16:9",
            duration=5, watermark=False, budget_cny=100.0,
        )

    def setUp(self):
        use_test_price(self, model="m", price=1.00)

    def vendor(self, opener):
        return vendors.SeedanceVendor(self.config(), opener=opener)

    def test_portraits_ride_along_as_reference_images(self):
        sent = {}

        def opener(request, timeout=None):
            sent["payload"] = json.loads(request.data.decode("utf-8"))
            return mock.MagicMock(
                __enter__=lambda s: s, __exit__=lambda *a: False,
                read=lambda: json.dumps({"id": "task-1"}).encode("utf-8"),
            )

        with TemporaryDirectory() as tmp:
            face = make_image(Path(tmp) / "face.jpg")
            self.vendor(opener).submit("a prompt", None, [face])

        roles = [item.get("role") for item in sent["payload"]["content"] if item["type"] == "image_url"]
        self.assertEqual(roles, ["reference_image"])

    def test_a_first_frame_and_portraits_are_both_sent(self):
        sent = {}

        def opener(request, timeout=None):
            sent["payload"] = json.loads(request.data.decode("utf-8"))
            return mock.MagicMock(
                __enter__=lambda s: s, __exit__=lambda *a: False,
                read=lambda: json.dumps({"id": "task-1"}).encode("utf-8"),
            )

        with TemporaryDirectory() as tmp:
            face = make_image(Path(tmp) / "face.jpg")
            frame = make_image(Path(tmp) / "frame.jpg")
            self.vendor(opener).submit("a prompt", frame, [face])

        roles = [item.get("role") for item in sent["payload"]["content"] if item["type"] == "image_url"]
        self.assertEqual(roles, ["first_frame", "reference_image"])

    def test_a_refused_portrait_drops_to_text_and_says_so(self):
        """Ark reads a good photorealistic portrait as a photo of a real person.

        Losing the shot over that would be worse than losing the face, but a
        report that stays silent about it would claim an identity nobody
        enforced.
        """

        calls = []

        def opener(request, timeout=None):
            payload = json.loads(request.data.decode("utf-8")) if request.data else {}
            calls.append(payload)
            images = [i for i in payload.get("content", []) if i["type"] == "image_url"]
            if images:
                raise urllib.error.HTTPError(
                    request.full_url, 400, "Bad Request", {},
                    __import__("io").BytesIO(json.dumps({
                        "error": {
                            "code": "InputImageSensitiveContentDetected.PrivacyInformation",
                            "message": "the input image may contain a real person",
                        }
                    }).encode("utf-8")),
                )
            return mock.MagicMock(
                __enter__=lambda s: s, __exit__=lambda *a: False,
                read=lambda: json.dumps({"id": "task-1"}).encode("utf-8"),
            )

        with TemporaryDirectory() as tmp:
            face = make_image(Path(tmp) / "face.jpg")
            vendor = self.vendor(opener)
            body = {"status": "succeeded", "content": {"video_url": "http://x/clip.mp4"}}
            with mock.patch.object(vendor, "poll", return_value=body), \
                 mock.patch.object(vendor, "download", side_effect=lambda url, target: target):
                clip = vendor.generate(
                    {"shot_id": "1", "reference_images": [str(face)]},
                    "a prompt", 1, Path(tmp) / "out.mp4",
                )

        self.assertIsNotNone(clip.references_dropped)
        self.assertIn("real person", clip.references_dropped)
        # Two submits: one with the portrait, one without.
        self.assertEqual(len(calls), 2)

    def test_an_error_that_is_not_about_the_image_is_not_retried(self):
        """A retried POST can create a second billable task."""

        calls = []

        def opener(request, timeout=None):
            calls.append(1)
            raise urllib.error.HTTPError(
                request.full_url, 429, "Too Many Requests", {},
                __import__("io").BytesIO(json.dumps({
                    "error": {"code": "RateLimitExceeded", "message": "slow down"}
                }).encode("utf-8")),
            )

        with TemporaryDirectory() as tmp:
            face = make_image(Path(tmp) / "face.jpg")
            vendor = self.vendor(opener)
            with self.assertRaises(vendors.ArkHTTPError):
                vendor.generate(
                    {"shot_id": "1", "reference_images": [str(face)]},
                    "a prompt", 1, Path(tmp) / "out.mp4",
                )
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
