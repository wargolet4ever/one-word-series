"""A bed under the whole episode: score, ambience, room tone.

## Why this happens after the cut, not inside it

A score laid per shot restarts at every cut, which is the single most reliable
way to make an edit feel like a slideshow. Music is the one element that should
ignore the cuts entirely, so it goes on after the episode is assembled and runs
across the whole thing.

## Why it ducks itself

A fixed level is either loud enough to be heard under silence and too loud
under dialogue, or quiet enough under dialogue and inaudible everywhere else.
`sidechaincompress` solves that the way a dub stage does: the episode's own
audio drives the music's gain, so the bed steps back when someone speaks and
returns when they stop. Where the ffmpeg build has no sidechain filter, it
falls back to a fixed level and says so rather than failing.

## What this module does not do

It does not generate music. It mixes a file you supply. Generating a score is
a model call with its own cost and its own licensing questions, and neither
belongs behind a flag that looks like an audio mixer.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LEVEL_DB = -20.0
DEFAULT_FADE_S = 1.5
DUCK_RATIO = 6
DUCK_THRESHOLD = 0.05


class MusicError(RuntimeError):
    pass


def _ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def has_sidechain() -> bool:
    completed = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-filters"], capture_output=True, text=True, check=False
    )
    return " sidechaincompress " in completed.stdout


@dataclass
class MusicResult:
    path: Path
    ducked: bool
    level_db: float


def underlay(
    video: Path,
    music: Path,
    target: Path,
    *,
    level_db: float = DEFAULT_LEVEL_DB,
    fade_s: float = DEFAULT_FADE_S,
    duck: bool = True,
) -> MusicResult:
    """Mix `music` under `video`'s existing audio for the full running time."""

    video, music, target = Path(video), Path(music), Path(target)
    if not music.is_file():
        raise MusicError(f"music file not found: {music}")

    gain = 10 ** (level_db / 20)
    ducking = duck and has_sidechain()

    # -stream_loop repeats a short track; -shortest then trims the result to
    # the picture, so the bed always covers exactly the episode.
    command = [
        _ffmpeg(), "-hide_banner", "-loglevel", "error",
        "-i", str(video),
        "-stream_loop", "-1", "-i", str(music),
    ]

    bed = (
        f"[1:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
        f"volume={gain:.4f},afade=t=in:st=0:d={fade_s}[bed]"
    )
    if ducking:
        # The episode's own audio drives the bed's gain, so dialogue pushes the
        # music down and silence lets it back up.
        parts = [
            "[0:a]asplit=2[main][key]",
            bed,
            f"[bed][key]sidechaincompress=threshold={DUCK_THRESHOLD}:"
            f"ratio={DUCK_RATIO}:attack=20:release=400[duckedbed]",
            # normalize=0 is not optional: amix otherwise divides every input
            # by the number of inputs, so a two-input mix quietly loses 6 dB on
            # the dialogue as well as the bed. alimiter catches the peaks that
            # honest levels then make possible.
            "[main][duckedbed]amix=inputs=2:duration=first:dropout_transition=0:"
            "normalize=0,alimiter=limit=0.95[aout]",
        ]
    else:
        parts = [
            bed,
            "[0:a][bed]amix=inputs=2:duration=first:dropout_transition=0:"
            "normalize=0,alimiter=limit=0.95[aout]",
        ]

    command += [
        "-filter_complex", ";".join(parts),
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
        "-shortest", "-movflags", "+faststart", "-y", str(target),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0 or not target.is_file():
        detail = (completed.stderr or completed.stdout or "").strip()
        raise MusicError(f"mixing music under the episode failed: {detail[-400:]}")
    return MusicResult(path=target, ducked=ducking, level_db=level_db)
