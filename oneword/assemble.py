"""Turn approved clips + speech into one finished episode file.

Every clip is normalised to the same container spec first — same size, same
frame rate, same audio layout — because the concat demuxer copies streams and
silently produces a broken file when they disagree.  A vendor that returns
1080p/30 for one shot and 720p/24 for the next is the normal case, not the
exception, so normalisation is not optional.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import voice as voice_mod

WIDTH = 1280
HEIGHT = 720
FPS = 24
SAMPLE_RATE = 44100


class AssembleError(RuntimeError):
    pass


def _ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def _run(command: list[str], what: str) -> None:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise AssembleError(f"{what} failed: {detail[-400:]}")


def has_subtitle_filter() -> bool:
    completed = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-filters"], capture_output=True, text=True, check=False
    )
    return " subtitles " in completed.stdout


@dataclass
class Segment:
    shot_id: str
    path: Path
    duration: float
    line: str


def has_audio(clip: Path) -> bool:
    """Does this clip carry an audio stream of its own?

    Video models now return clips with dialogue and room tone already in them.
    Overwriting that with an external narrator is almost always the wrong call,
    so nothing downstream may assume a clip is silent — it has to ask.
    """

    probe = shutil.which("ffprobe")
    if probe:
        completed = subprocess.run(
            [probe, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(clip)],
            capture_output=True, text=True, check=False,
        )
        return bool(completed.stdout.strip())
    # No ffprobe: ffmpeg still names the streams it found on stderr.
    completed = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-i", str(clip), "-f", "null", "-"],
        capture_output=True, text=True, check=False,
    )
    return "Audio:" in completed.stderr


def normalise(
    clip: Path,
    speech: Path | None,
    target: Path,
    *,
    duration: float,
    music: Path | None = None,
    mode: str = "mix",
    duck_db: float = -9.0,
) -> Segment:
    """One shot → one self-contained segment with a real audio track.

    `mode` decides what happens when the clip already has sound of its own:

    `keep`     the clip's audio is the audio. Narration is dropped.
    `mix`      both, with the clip ducked under the narration by `duck_db`.
    `replace`  narration only — the clip's own track is discarded.

    A clip with no audio behaves the same under every mode. The default is
    `mix` rather than `replace` because discarding a performance the model
    produced, in favour of a flat line read over the top of it, is a loss
    disguised as a feature.
    """

    if mode not in ("keep", "mix", "replace"):
        raise AssembleError(f"audio mode must be keep, mix or replace — got {mode!r}")

    ffmpeg = _ffmpeg()
    video_filter = (
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fps={FPS},format=yuv420p"
    )
    fmt = f"aformat=sample_fmts=fltp:sample_rates={SAMPLE_RATE}:channel_layouts=stereo"
    clip_has_audio = has_audio(clip)
    use_speech = bool(speech and Path(speech).is_file()) and mode != "keep"

    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(clip)]
    parts = [f"[0:v]{video_filter}[vout]"]

    if clip_has_audio and mode != "replace":
        parts.append(f"[0:a]{fmt},apad[orig]")
    if use_speech:
        command += ["-i", str(speech)]
        # Delay the read 0.35s so a line never starts on the cut, then pad the
        # tail so the stream is exactly as long as the shot.
        parts.append(f"[1:a]{fmt},adelay=350|350,apad[speech]")

    if use_speech and clip_has_audio and mode == "mix":
        # Duck the clip under the narration rather than muting it: the room
        # stays alive, the words stay legible.
        gain = 10 ** (duck_db / 20)
        parts.append(f"[orig]volume={gain:.3f}[ducked]")
        parts.append("[ducked][speech]amix=inputs=2:duration=longest:dropout_transition=0[mixed]")
        source = "[mixed]"
    elif use_speech:
        source = "[speech]"
    elif clip_has_audio:
        source = "[orig]"
    else:
        command += [
            "-f", "lavfi", "-t", f"{duration:.3f}",
            "-i", f"anullsrc=channel_layout=stereo:sample_rate={SAMPLE_RATE}",
        ]
        # Only reachable with no speech and no clip audio, so the silence is
        # input 1 — a shot still needs a stream or the concat demuxer breaks.
        parts.append(f"[1:a]{fmt},apad[orig]")
        source = "[orig]"

    parts.append(f"{source}atrim=0:{duration:.3f},asetpts=N/SR/TB[aout]")
    command += [
        "-filter_complex", ";".join(parts),
        "-map", "[vout]", "-map", "[aout]",
    ]

    command += [
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", str(SAMPLE_RATE), "-ac", "2",
        "-shortest", "-y", str(target),
    ]
    target.parent.mkdir(parents=True, exist_ok=True)
    _run(command, f"normalising shot {target.stem}")
    return Segment(shot_id=target.stem, path=target, duration=duration, line="")


def concat(segments: list[Segment], target: Path) -> Path:
    if not segments:
        raise AssembleError("nothing to concatenate")
    root = target.parent
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / "concat.txt"
    manifest.write_text(
        "".join(f"file '{segment.path.name}'\n" for segment in segments), encoding="utf-8"
    )
    if manifest.parent != target.parent:
        raise AssembleError("concat manifest must sit beside its output file")
    completed = subprocess.run(
        [
            _ffmpeg(), "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", manifest.name,
            "-c", "copy", "-movflags", "+faststart", "-y", target.name,
        ],
        cwd=root, capture_output=True, text=True, check=False,
    )
    if completed.returncode != 0 or not target.is_file():
        detail = (completed.stderr or completed.stdout or "").strip()
        raise AssembleError(f"concatenating the episode failed: {detail[-400:]}")
    return target


def subtitle_entries(segments: list[Segment]) -> list[dict[str, Any]]:
    entries = []
    cursor = 0.0
    for segment in segments:
        if segment.line.strip():
            entries.append(
                {
                    "start": cursor + 0.35,
                    "end": cursor + segment.duration - 0.15,
                    "text": segment.line.strip(),
                }
            )
        cursor += segment.duration
    return entries


def burn_subtitles(video: Path, srt: Path, target: Path) -> Path | None:
    """Best effort.  A build without libass returns None instead of failing."""

    if not has_subtitle_filter():
        return None
    style = "FontSize=20,PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,BorderStyle=3,Outline=1,MarginV=36"
    completed = subprocess.run(
        [
            _ffmpeg(), "-hide_banner", "-loglevel", "error",
            "-i", video.name,
            "-vf", f"subtitles={srt.name}:force_style='{style}'",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "copy", "-y", target.name,
        ],
        cwd=video.parent, capture_output=True, text=True, check=False,
    )
    if completed.returncode != 0 or not target.is_file():
        return None
    return target


def build_episode(
    clips: list[dict[str, Any]],
    root: Path,
    *,
    stem: str = "episode",
    mode: str = "mix",
) -> dict[str, Any]:
    """clips: [{shot_id, path, duration, line, speech}] in cut order."""

    workdir = root / "segments"
    workdir.mkdir(parents=True, exist_ok=True)
    segments: list[Segment] = []
    for item in clips:
        target = workdir / f"{item['shot_id']}.mp4"
        segment = normalise(
            Path(item["path"]),
            Path(item["speech"]) if item.get("speech") else None,
            target,
            duration=float(item["duration"]),
            mode=mode,
        )
        segment.line = item.get("line", "")
        segment.shot_id = str(item["shot_id"])
        segments.append(segment)

    silent_named = root / f"{stem}.mp4"
    # concat demuxer needs the parts beside the output, so assemble in workdir
    # and move the finished file up.
    staged = workdir / f"{stem}.mp4"
    concat(segments, staged)

    entries = subtitle_entries(segments)
    srt = voice_mod.write_srt(entries, workdir / f"{stem}.srt")

    burned = burn_subtitles(staged, srt, workdir / f"{stem}.subbed.mp4") if entries else None
    final_source = burned or staged
    shutil.move(str(final_source), str(silent_named))
    final_srt = root / f"{stem}.srt"
    shutil.copy(str(srt), str(final_srt))

    return {
        "video": silent_named,
        "srt": final_srt,
        "subtitles_burned": bool(burned),
        "runtime_sec": round(sum(segment.duration for segment in segments), 2),
        "segments": [
            {"shot_id": segment.shot_id, "duration": segment.duration, "line": segment.line}
            for segment in segments
        ],
    }
