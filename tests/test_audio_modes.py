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

from oneword import assemble, vendors, voice  # noqa: E402
from oneword.bible import build_bible  # noqa: E402
from oneword.contracts import GeneratedClip  # noqa: E402
from oneword.pipeline import run_episode  # noqa: E402


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
    """dB. Silence comes back as -91.

    A missing file is an error, not silence. Returning -91 for one made a test
    that measured a deleted file — an assert left outside its TemporaryDirectory
    — look exactly like a test that had caught a silent film. It cost two wrong
    diagnoses before the helper was the suspect.
    """

    if not Path(path).is_file():
        raise AssertionError(f"nothing to measure: {path} does not exist")
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


class SilentStubVendor:
    """Returns a clip with no audio track at all, like Seedance by default."""

    name = "silent-stub"
    generative = True
    speaks = False

    def generate(self, shot, prompt, attempt, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [vendors.ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=c=black:s=320x180:d=0.4:r=12",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", "-y", str(target)],
            capture_output=True, check=True,
        )
        return GeneratedClip(shot_id=str(shot["shot_id"]), attempt=attempt,
                             provider=self.name, path=target, prompt=prompt)


class CountingVoice:
    """A voice engine that always succeeds, so the count means what it says."""

    name = "counting"

    def synthesize(self, line, voice, cid, target: Path) -> Path:
        subprocess.run(
            [vendors.ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "sine=frequency=300:duration=0.6",
             "-y", str(target)],
            capture_output=True, check=True,
        )
        return target


class SilentClipTests(unittest.TestCase):
    """`--audio keep` must not mean "no sound at all".

    Seedance returns silent clips unless SEEDANCE_AUDIO=1, which is not the
    default. Combined with `keep`, that produced a film with no voice anywhere
    — the model was never asked to speak and the narrator was switched off —
    and the run reported success. `normalise` has always documented the rule:
    a clip with no audio behaves the same under every mode.
    """

    def run_mode(self, mode, tmp):
        bible, _ = build_bible("rust", episodes=1, shots=3, allow_model=False)
        return run_episode(
            bible, 1, Path(tmp),
            vendor=SilentStubVendor(), auditor=None,
            voice_engine=CountingVoice(),
            audio_mode=mode,
        )

    def test_a_silent_clip_is_narrated_under_keep(self):
        with TemporaryDirectory() as tmp:
            report = self.run_mode("keep", tmp)
        self.assertGreater(
            report["narrated_shots"], 0,
            "keep silenced a film whose clips had no voice of their own",
        )

    def test_every_mode_narrates_a_silent_clip_identically(self):
        counts = {}
        for mode in ("keep", "mix", "replace"):
            with TemporaryDirectory() as tmp:
                counts[mode] = self.run_mode(mode, tmp)["narrated_shots"]
        self.assertEqual(len(set(counts.values())), 1, counts)


class CountingSilentVendor(SilentStubVendor):
    """Silent clips, and every generate() is a purchase, so the count is the bill."""

    accepts_first_frame = False
    unit_cost = 1.41

    def __init__(self) -> None:
        self.generated: list[str] = []

    def generate(self, shot, prompt, attempt, target):
        self.generated.append(str(shot["shot_id"]))
        return super().generate(shot, prompt, attempt, target)


class RescueASilentFilmTests(unittest.TestCase):
    """Giving an already-paid-for film a voice must cost nothing.

    This is the real situation the `--audio keep` bug left behind: eight clips
    bought, downloaded and silent, and a finished episode with no sound in it.
    The fix is only worth anything if the second run reuses every clip — a rerun
    that re-buys what is already on disk is not a fix, it is a second bill.
    """

    def episode(self, root: Path, vendor, voice_engine):
        bible, _ = build_bible("rust", episodes=1, shots=3, allow_model=False)
        return run_episode(
            bible, 1, root,
            vendor=vendor, auditor=None,
            voice_engine=voice_engine, audio_mode="keep",
        )

    def test_the_second_run_adds_a_voice_and_buys_nothing(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)

            # Run one: the clips are bought, and nothing narrates them.
            first_vendor = CountingSilentVendor()
            first = self.episode(root, first_vendor, voice.SilentVoice())
            self.assertEqual(len(first_vendor.generated), 3)
            self.assertEqual(first["narrated_shots"], 0)
            # The container always carries an audio stream; what matters is
            # whether anything is audible in it. Measure, do not trust the probe.
            self.assertLess(
                mean_volume(root / first["outputs"]["video"]), -80.0,
                "the first run should reproduce the silent film, or this proves nothing",
            )

            # Run two: same clips, now with a voice engine present.
            second_vendor = CountingSilentVendor()
            second = self.episode(root, second_vendor, CountingVoice())

            self.assertEqual(
                second_vendor.generated, [],
                "the rerun bought clips it already had on disk",
            )
            self.assertEqual(len(second["summary"]["reused_shot_ids"]), 3)
            self.assertGreater(second["narrated_shots"], 0)
            self.assertGreater(
                mean_volume(root / second["outputs"]["video"]), -80.0,
                "the film is still silent after the run that was supposed to fix it",
            )

    def test_the_saving_is_reported_so_the_rerun_is_visibly_free(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.episode(root, CountingSilentVendor(), voice.SilentVoice())
            second = self.episode(root, CountingSilentVendor(), CountingVoice())
        self.assertAlmostEqual(second["summary"]["reused_saving_cny"], 3 * 1.41, places=2)


class NormaliseSilentClipTests(unittest.TestCase):
    """The invariant asserted where it lives, not only through a whole episode.

    `narrated_shots` counted the TTS calls, so it went up the moment the
    pipeline stopped skipping them — and stayed green while `normalise` threw
    the resulting files away under `keep`. Counting work done is not the same
    as measuring what came out. These listen to the segment.
    """

    def segment(self, tmp: Path, mode: str, *, tone: int | None):
        clip = make_clip(tmp / "clip.mp4", seconds=2.0, tone=tone)
        speech = make_speech(tmp / "speech.wav", seconds=1.0)
        out = tmp / f"seg-{mode}.mp4"
        assemble.normalise(clip, speech, out, duration=2.0, mode=mode)
        return out

    def test_keep_narrates_a_clip_that_has_no_voice(self):
        with TemporaryDirectory() as tmp:
            out = self.segment(Path(tmp), "keep", tone=None)
            self.assertGreater(
                mean_volume(out), -80.0,
                "keep dropped the narration from a clip that had nothing of its own",
            )

    def test_keep_still_refuses_to_dub_over_a_clip_that_speaks(self):
        # The other half of the rule, and the more important one: a model's own
        # performance must never get a flat line read laid over the top of it.
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            clip = make_clip(tmp / "clip.mp4", seconds=2.0, tone=440)
            speech = make_speech(tmp / "speech.wav", seconds=1.0)
            plain = tmp / "plain.mp4"
            dubbed = tmp / "dubbed.mp4"
            assemble.normalise(clip, None, plain, duration=2.0, mode="keep")
            assemble.normalise(clip, speech, dubbed, duration=2.0, mode="keep")
            self.assertAlmostEqual(mean_volume(plain), mean_volume(dubbed), delta=0.5)

    def test_a_silent_clip_sounds_the_same_under_every_mode(self):
        volumes = {}
        for mode in ("keep", "mix", "replace"):
            with TemporaryDirectory() as tmp:
                volumes[mode] = mean_volume(self.segment(Path(tmp), mode, tone=None))
        self.assertLess(max(volumes.values()) - min(volumes.values()), 0.5, volumes)
