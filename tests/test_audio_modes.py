"""What happens to a clip that already speaks for itself.

Video models return clips with dialogue in them now. The first paid run of
this tool threw that away and dubbed a flat TTS read over the top, which is
the failure these tests exist to stop coming back.

Loudness is measured with ffmpeg's `volumedetect`, so each mode is checked by
what the finished file actually sounds like, not by which flags were passed.
"""

from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oneword import assemble  # noqa: E402


def ffmpeg() -> str:
    return assemble._ffmpeg()


def make_clip(target: Path, *, seconds: float = 2.0, tone: int | None = 440) -> Path:
    """A clip with a steady tone as its 'own' audio, or no audio at all."""

    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg(), "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=navy:s=320x180:d={seconds}:r=12",
    ]
    if tone is not None:
        command += ["-f", "lavfi", "-i", f"sine=frequency={tone}:duration={seconds}"]
    command += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if tone is not None:
        command += ["-c:a", "aac", "-shortest"]
    command += ["-y", str(target)]
    subprocess.run(command, capture_output=True, check=True)
    return target


def make_speech(target: Path, *, seconds: float = 1.0) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg(), "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"sine=frequency=880:duration={seconds}",
         "-y", str(target)],
        capture_output=True, check=True,
    )
    return target


def mean_volume(path: Path) -> float:
    """dB. Silence comes back as -91."""

    completed = subprocess.run(
        [ffmpeg(), "-hide_banner", "-i", str(path), "-af", "volumedetect",
         "-f", "null", "-"],
        capture_output=True, text=True, check=False,
    )
    match = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB", completed.stderr)
    return float(match.group(1)) if match else -91.0


class ClipAudioDetectionTests(unittest.TestCase):
    def test_a_clip_with_sound_is_recognised(self):
        with TemporaryDirectory() as tmp:
            self.assertTrue(assemble.has_audio(make_clip(Path(tmp) / "a.mp4")))

    def test_a_silent_clip_is_recognised(self):
        with TemporaryDirectory() as tmp:
            self.assertFalse(assemble.has_audio(make_clip(Path(tmp) / "b.mp4", tone=None)))


class AudioModeTests(unittest.TestCase):
    def build(self, mode: str, *, clip_tone: int | None = 440, with_speech: bool = True):
        tmp = TemporaryDirectory()
        root = Path(tmp.name)
        clip = make_clip(root / "clip.mp4", tone=clip_tone)
        speech = make_speech(root / "speech.wav") if with_speech else None
        out = root / "out.mp4"
        assemble.normalise(clip, speech, out, duration=2.0, mode=mode)
        return tmp, out

    def test_keep_uses_the_clips_own_audio_and_drops_the_narration(self):
        tmp, out = self.build("keep")
        with tmp:
            self.assertGreater(mean_volume(out), -50.0)  # there is sound
            self.assertTrue(assemble.has_audio(out))

    def test_replace_discards_the_clips_own_audio(self):
        """The old behaviour, now opt-in: after the 1s read the shot is silent."""

        tmp, out = self.build("replace")
        with tmp:
            tail = out.parent / "tail.wav"
            subprocess.run(
                [ffmpeg(), "-hide_banner", "-loglevel", "error", "-ss", "1.6",
                 "-i", str(out), "-y", str(tail)],
                capture_output=True, check=True,
            )
            # Nothing but the narration was kept, so the tail is near silence.
            self.assertLess(mean_volume(tail), -50.0)

    def test_mix_keeps_both(self):
        tmp, out = self.build("mix")
        with tmp:
            tail = out.parent / "tail.wav"
            subprocess.run(
                [ffmpeg(), "-hide_banner", "-loglevel", "error", "-ss", "1.6",
                 "-i", str(out), "-y", str(tail)],
                capture_output=True, check=True,
            )
            # The clip's own tone is still running after the read has finished.
            self.assertGreater(mean_volume(tail), -50.0)

    def test_mix_ducks_the_clip_under_the_narration(self):
        tmp_mix, mixed = self.build("mix")
        tmp_keep, kept = self.build("keep")
        with tmp_mix, tmp_keep:
            def head(path):
                target = path.parent / "head.wav"
                subprocess.run(
                    [ffmpeg(), "-hide_banner", "-loglevel", "error", "-t", "1.2",
                     "-i", str(path), "-y", str(target)],
                    capture_output=True, check=True,
                )
                return target
            # Under the narration the original sits lower than it does alone.
            self.assertLess(mean_volume(head(mixed)) - mean_volume(head(kept)), 6.0)

    def test_a_silent_clip_still_gets_an_audio_stream(self):
        """Without one the concat demuxer produces a broken file."""

        for mode in ("keep", "mix", "replace"):
            tmp, out = self.build(mode, clip_tone=None, with_speech=False)
            with tmp:
                self.assertTrue(assemble.has_audio(out), mode)

    def test_an_unknown_mode_is_refused(self):
        with TemporaryDirectory() as tmp:
            clip = make_clip(Path(tmp) / "c.mp4")
            with self.assertRaises(assemble.AssembleError):
                assemble.normalise(clip, None, Path(tmp) / "o.mp4", duration=2.0, mode="loudest")


if __name__ == "__main__":
    unittest.main(verbosity=2)
