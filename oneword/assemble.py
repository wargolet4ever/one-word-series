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


def normalise(
    clip: Path,
    speech: Path | None,
    target: Path,
    *,
    duration: float,
    music: Path | None = None,
) -> Segment:
    """One shot → one self-contained segment with a real audio track."""

    ffmpeg = _ffmpeg()
    video_filter = (
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fps={FPS},format=yuv420p"
    )
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(clip)]

    if speech and speech.is_file():
        command += ["-i", str(speech)]
        # Delay speech 0.35s so a line never starts on the cut, then pad the
        # tail with silence so the audio stream is exactly as long as the shot.
        audio_filter = (
            "[1:a]aformat=sample_fmts=fltp:sample_rates=%d:channel_layouts=stereo,"
            "adelay=350|350,apad[speech];"
            "[speech]atrim=0:%.3f,asetpts=N/SR/TB[aout]" % (SAMPLE_RATE, duration)
        )
        command += [
            "-filter_complex", f"[0:v]{video_filter}[vout];{audio_filter}",
            "-map", "[vout]", "-map", "[aout]",
        ]
    else:
        command += [
            "-f", "lavfi", "-t", f"{duration:.3f}",
            "-i", f"anullsrc=channel_layout=stereo:sample_rate={SAMPLE_RATE}",
            "-filter_complex", f"[0:v]{video_filter}[vout]",
            "-map", "[vout]", "-map", "1:a",
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
