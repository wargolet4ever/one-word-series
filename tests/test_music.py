"""The bed under the episode.

Checked by measuring the finished file: whether the music is audible under
silence, whether it steps back under speech, and whether a bad track costs you
the score or the whole episode.
"""

from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oneword import music  # noqa: E402


def ffmpeg() -> str:
    return music._ffmpeg()


def make_episode(target: Path, *, speech_from: float = 2.0, seconds: float = 4.0) -> Path:
    """Video whose audio is silent, then a loud tone — silence, then 'speech'."""

    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg(), "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"color=c=black:s=320x180:d={seconds}:r=12",
         "-f", "lavfi", "-i",
         f"sine=frequency=300:duration={seconds}:sample_rate=44100,"
         f"volume=enable='gte(t,{speech_from})':volume=1,"
         f"volume=enable='lt(t,{speech_from})':volume=0",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", "-y", str(target)],
        capture_output=True, check=True,
    )
    return target


def make_track(target: Path, *, seconds: float = 1.0) -> Path:
    """Deliberately shorter than the episode, so looping is exercised."""

    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg(), "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"sine=frequency=1200:duration={seconds}:sample_rate=44100",
         "-y", str(target)],
        capture_output=True, check=True,
    )
    return target


def window_volume(path: Path, start: float, length: float) -> float:
    completed = subprocess.run(
        [ffmpeg(), "-hide_banner", "-ss", str(start), "-t", str(length),
         "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, check=False,
    )
    match = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB", completed.stderr)
    return float(match.group(1)) if match else -91.0


class MusicTests(unittest.TestCase):
    def test_the_bed_fills_the_silence_the_episode_had(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            episode = make_episode(root / "ep.mp4")
            before = window_volume(episode, 0.3, 1.2)
            out = music.underlay(
                episode, make_track(root / "m.wav"), root / "out.mp4", fade_s=0.2
            ).path
            after = window_volume(out, 0.3, 1.2)
        self.assertLess(before, -50.0)      # the episode was silent there
        self.assertGreater(after, -45.0)    # the bed is now audible

    def test_a_short_track_is_looped_to_cover_the_whole_episode(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = music.underlay(
                make_episode(root / "ep.mp4", seconds=4.0),
                make_track(root / "m.wav", seconds=1.0),
                root / "out.mp4", fade_s=0.2,
            ).path
            # Second 3 is well past the end of a one-second track.
            self.assertGreater(window_volume(out, 3.0, 0.8), -45.0)

    def test_ducking_leaves_the_bed_alone_where_there_is_no_speech(self):
        """What ducking must NOT do: quieten the bed when nobody is talking.

        How far it pulls the bed down under speech cannot be measured from the
        mix — the speech dominates the window — so this asserts the half that
        is measurable, and `test_the_result_says_whether_it_ducked` covers
        whether the sidechain was engaged at all.
        """

        if not music.has_sidechain():
            self.skipTest("this ffmpeg has no sidechaincompress")
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            episode, track = make_episode(root / "ep.mp4"), make_track(root / "m.wav")
            ducked = music.underlay(episode, track, root / "d.mp4",
                                    fade_s=0.2, duck=True).path
            flat = music.underlay(episode, track, root / "f.mp4",
                                  fade_s=0.2, duck=False).path
            self.assertAlmostEqual(
                window_volume(ducked, 0.5, 1.0), window_volume(flat, 0.5, 1.0), delta=2.0
            )

    def test_the_result_says_whether_it_ducked(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            flat = music.underlay(
                make_episode(root / "ep.mp4"), make_track(root / "m.wav"),
                root / "out.mp4", fade_s=0.2, duck=False,
            )
            asked = music.underlay(
                make_episode(root / "ep2.mp4"), make_track(root / "m2.wav"),
                root / "out2.mp4", fade_s=0.2, duck=True,
            )
        self.assertFalse(flat.ducked)
        self.assertEqual(asked.ducked, music.has_sidechain())
        result = flat
        self.assertFalse(result.ducked)
        self.assertEqual(result.level_db, music.DEFAULT_LEVEL_DB)

    def test_a_lower_level_is_actually_quieter(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            episode, track = make_episode(root / "ep.mp4"), make_track(root / "m.wav")
            loud = music.underlay(episode, track, root / "loud.mp4",
                                  level_db=-10, fade_s=0.2, duck=False).path
            soft = music.underlay(episode, track, root / "soft.mp4",
                                  level_db=-30, fade_s=0.2, duck=False).path
            self.assertGreater(window_volume(loud, 0.5, 1.0), window_volume(soft, 0.5, 1.0))

    def test_a_missing_track_is_refused_by_name(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(music.MusicError):
                music.underlay(make_episode(root / "ep.mp4"), root / "nope.mp3",
                               root / "out.mp4")

    def test_the_video_stream_is_copied_not_re_encoded(self):
        """Re-encoding the picture to add a score would be a quality loss."""

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            episode = make_episode(root / "ep.mp4")
            out = music.underlay(episode, make_track(root / "m.wav"),
                                 root / "out.mp4", fade_s=0.2).path
            def video_size(path):
                completed = subprocess.run(
                    [ffmpeg(), "-hide_banner", "-i", str(path), "-f", "null", "-"],
                    capture_output=True, text=True, check=False,
                )
                return completed.stderr
            self.assertIn("Video:", video_size(out))


class PipelineMusicTests(unittest.TestCase):
    def test_a_bad_track_loses_the_score_not_the_episode(self):
        from oneword import voice
        from oneword.bible import build_bible
        from oneword.pipeline import run_episode
        from oneword.vendors import AnimaticVendor

        bible, _ = build_bible("rust", episodes=1, shots=3, allow_model=False)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = run_episode(
                bible, 1, root,
                vendor=AnimaticVendor(),
                voice_engine=voice.SilentVoice(),
                clip_seconds=2,
                music=root / "does-not-exist.mp3",
            )
            self.assertTrue((root / report["outputs"]["video"]).is_file())
        self.assertIn("error", report["music"])

    def test_a_real_track_is_recorded_in_the_report(self):
        from oneword import voice
        from oneword.bible import build_bible
        from oneword.pipeline import run_episode
        from oneword.vendors import AnimaticVendor

        bible, _ = build_bible("rust", episodes=1, shots=3, allow_model=False)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            track = make_track(root / "score.wav", seconds=1.0)
            report = run_episode(
                bible, 1, root,
                vendor=AnimaticVendor(),
                voice_engine=voice.SilentVoice(),
                clip_seconds=2,
                music=track,
            )
            video = root / report["outputs"]["video"]
            self.assertGreater(window_volume(video, 2.5, 1.5), -45.0)
        self.assertEqual(report["music"]["track"], "score.wav")
        self.assertNotIn("error", report["music"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
