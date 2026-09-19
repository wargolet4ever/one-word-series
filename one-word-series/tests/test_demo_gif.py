"""The README picture.

A repo for a film tool with no moving picture in it asks people to take the
film on faith. The one thing that must not happen is the GIF opening on the
fade-in, because then the picture that was supposed to stop someone scrolling
is a black rectangle.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from oneword.vendors import ffmpeg_exe  # noqa: E402

spec = importlib.util.spec_from_file_location("demo_gif", ROOT / "scripts" / "demo_gif.py")
demo_gif = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo_gif)


def clip_with_fade(target: Path, *, fade: float = 0.5, seconds: float = 3.0) -> Path:
    """Colour bars that fade up from black, like a finished episode."""

    target.parent.mkdir(parents=True, exist_ok=True)
    # `fade` with d=0 is not "no fade" — it still blacks the first frames — so
    # the no-fade case has to omit the filter entirely.
    filters = ["-vf", f"fade=t=in:st=0:d={fade}"] if fade else []
    subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc=size=320x180:duration={seconds}:rate=12",
         *filters,
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(target)],
        capture_output=True, check=True,
    )
    return target


class LeadingBlackTests(unittest.TestCase):
    def test_a_fade_in_is_skipped(self):
        with TemporaryDirectory() as tmp:
            clip = clip_with_fade(Path(tmp) / "ep.mp4", fade=0.5)
            start = demo_gif.leading_black(clip)
        self.assertGreater(start, 0.1, "the opening fade was not detected")
        self.assertLess(start, 1.0, "skipped far more than the fade")

    def test_footage_that_opens_on_a_picture_is_not_trimmed(self):
        with TemporaryDirectory() as tmp:
            clip = clip_with_fade(Path(tmp) / "ep.mp4", fade=0.0)
            self.assertEqual(demo_gif.leading_black(clip), 0.0)


class GifTests(unittest.TestCase):
    def test_the_first_frame_is_not_black(self):
        """The whole point. A README GIF that opens black is no GIF."""

        from PIL import Image

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = clip_with_fade(root / "ep.mp4", fade=0.5)
            target = demo_gif.make_gif(
                clip, root / "demo.gif",
                width=160, fps=8, seconds=1.0, start=demo_gif.leading_black(clip),
            )
            with Image.open(target) as image:
                first = image.convert("RGB").resize((1, 1)).getpixel((0, 0))

        self.assertGreater(max(first), 24, f"first frame is almost black: {first}")

    def test_several_episodes_join_into_one_strip(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clips = [clip_with_fade(root / f"ep{n}.mp4", seconds=1.0) for n in (1, 2)]
            joined = demo_gif.concat(clips, root)
            # Inside the temp dir: asserting after it is cleaned up tests nothing.
            self.assertTrue(joined.is_file())
            self.assertGreater(joined.stat().st_size, 0)

    def test_a_single_clip_is_not_needlessly_remuxed(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = clip_with_fade(root / "ep.mp4", seconds=1.0)
            self.assertEqual(demo_gif.concat([clip], root), clip)


if __name__ == "__main__":
    unittest.main()
