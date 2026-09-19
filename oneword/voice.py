"""Speech for each shot's one line, plus the subtitle track.

Two engines behind one contract:

`EspeakVoice`  — local espeak-ng.  Robotic on purpose; it exists so the whole
                 loop can run with no account, no key and no spend.
`VolcTTSVoice` — Volcengine 豆包语音 HTTP v1.  Same account family as Seedance.

A character's voice is chosen from the bible, not per shot, so the same
character sounds the same in episode 1 and episode 9 — the audio half of the
same continuity promise the prompts make for the picture.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

VOLC_TTS_URL = "https://openspeech.bytedance.com/api/v1/tts"

# A small stable roster.  A character is assigned one of these by hashing its
# id, so the mapping never shifts between runs or episodes.
VOLC_VOICES = {
    "male": ["BV002_streaming", "BV701_streaming", "BV004_streaming"],
    "female": ["BV001_streaming", "BV700_streaming", "BV005_streaming"],
    "neutral": ["BV001_streaming", "BV002_streaming"],
}
ESPEAK_VARIANTS = {
    "male": ["+m3", "+m5", "+m1"],
    "female": ["+f3", "+f4", "+f2"],
    "neutral": ["+m2", "+f2"],
}


class VoiceError(RuntimeError):
    pass


def _stable_index(key: str, size: int) -> int:
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return digest[0] % max(size, 1)


def _is_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def ffmpeg_exe() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def audio_duration(path: Path) -> float:
    """Length in seconds, via ffprobe when present, else ffmpeg's own report."""

    probe = shutil.which("ffprobe")
    if probe:
        completed = subprocess.run(
            [probe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, check=False,
        )
        try:
            return float(completed.stdout.strip())
        except ValueError:
            pass
    completed = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-i", str(path), "-f", "null", "-"],
        capture_output=True, text=True, check=False,
    )
    for token in reversed(completed.stderr.split("time=")):
        stamp = token.split(" ")[0].strip()
        parts = stamp.split(":")
        if len(parts) == 3:
            try:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            except ValueError:
                continue
    return 0.0


class VoiceEngine(Protocol):
    name: str

    def synthesize(self, text: str, voice: dict[str, Any], key: str, target: Path) -> Path | None: ...


@dataclass
class SilentVoice:
    name: str = "silent"

    def synthesize(self, text: str, voice: dict[str, Any], key: str, target: Path) -> Path | None:
        return None


class EspeakVoice:
    name = "espeak-ng"

    def __init__(self, *, speed: int = 150) -> None:
        self.exe = shutil.which("espeak-ng") or shutil.which("espeak")
        if not self.exe:
            raise VoiceError(
                "espeak-ng not found. Linux: apt install espeak-ng · macOS: brew install espeak-ng "
                "· Windows: install espeak-ng and put it on PATH, or use --voice silent"
            )
        self.speed = speed

    def synthesize(self, text: str, voice: dict[str, Any], key: str, target: Path) -> Path | None:
        text = (text or "").strip()
        if not text:
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        gender = (voice or {}).get("gender", "neutral")
        variants = ESPEAK_VARIANTS.get(gender, ESPEAK_VARIANTS["neutral"])
        variant = variants[_stable_index(key, len(variants))]
        language = "cmn" if _is_cjk(text) else "en-us"
        command = [
            self.exe, "-v", f"{language}{variant}", "-s", str(self.speed),
            "-w", str(target), text,
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
            raise VoiceError(f"espeak-ng failed: {(completed.stderr or '')[:200]}")
        return target


class VolcTTSVoice:
    """豆包语音 HTTP v1, non-streaming.

    Note the header really is `Bearer;<token>` with a semicolon — that is the
    platform's format, not a typo.
    """

    name = "volc-tts"

    def __init__(
        self,
        *,
        appid: str | None = None,
        token: str | None = None,
        cluster: str | None = None,
        opener=None,
    ) -> None:
        self.appid = appid or os.getenv("VOLC_TTS_APPID", "")
        self.token = token or os.getenv("VOLC_TTS_TOKEN", "")
        self.cluster = cluster or os.getenv("VOLC_TTS_CLUSTER", "volcano_tts")
        if not (self.appid and self.token):
            raise VoiceError("VOLC_TTS_APPID and VOLC_TTS_TOKEN are required for volc-tts")
        self._opener = opener or urllib.request.urlopen

    def voice_type(self, voice: dict[str, Any], key: str) -> str:
        override = (voice or {}).get("voice_type")
        if override:
            return str(override)
        roster = VOLC_VOICES.get((voice or {}).get("gender", "neutral"), VOLC_VOICES["neutral"])
        return roster[_stable_index(key, len(roster))]

    def synthesize(self, text: str, voice: dict[str, Any], key: str, target: Path) -> Path | None:
        text = (text or "").strip()
        if not text:
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "app": {"appid": self.appid, "token": self.token, "cluster": self.cluster},
            "user": {"uid": "continuity-agent"},
            "audio": {
                "voice_type": self.voice_type(voice, key),
                "encoding": "mp3",
                "speed_ratio": float((voice or {}).get("speed_ratio", 1.0)),
            },
            "request": {
                "reqid": str(uuid.uuid4()),
                "text": text,
                "text_type": "plain",
                "operation": "query",
            },
        }
        request = urllib.request.Request(
            VOLC_TTS_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer;{self.token}",
            },
            method="POST",
        )
        with self._opener(request, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8"))
        if not body.get("data"):
            raise VoiceError(f"volc-tts returned no audio: {body.get('code')} {body.get('message', '')}")
        mp3 = target.with_suffix(".mp3")
        mp3.write_bytes(base64.b64decode(body["data"]))
        completed = subprocess.run(
            [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(mp3),
             "-ar", "44100", "-ac", "2", "-y", str(target)],
            capture_output=True, text=True, check=False,
        )
        mp3.unlink(missing_ok=True)
        if completed.returncode != 0 or not target.is_file():
            raise VoiceError("failed to transcode volc-tts mp3 to wav")
        return target


def build_voice(kind: str = "auto") -> VoiceEngine:
    """Build a voice engine.

    `auto` is the default and never raises: it takes the best engine this
    machine actually has and falls back to silence.  A missing TTS binary is a
    reason to ship the picture without speech, not a reason to lose the film.

    An explicitly named engine still fails loudly — if you asked for volc-tts
    you want to know it is misconfigured, not get silence and a shrug.
    """

    kind = (kind or "auto").lower()
    if kind in {"none", "silent", "off"}:
        return SilentVoice()
    if kind in {"espeak", "espeak-ng", "offline"}:
        return EspeakVoice()
    if kind in {"volc", "volc-tts", "doubao"}:
        return VolcTTSVoice()
    if kind == "auto":
        for candidate in (VolcTTSVoice, EspeakVoice):
            try:
                return candidate()
            except VoiceError:
                continue
        return SilentVoice()
    raise VoiceError(f"unknown voice engine '{kind}'; use auto, espeak, volc or silent")


# ──────────────────────────────────────────────────────────────────────
# Subtitles
# ──────────────────────────────────────────────────────────────────────


def _stamp(seconds: float) -> str:
    seconds = max(seconds, 0.0)
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def write_srt(entries: list[dict[str, Any]], target: Path) -> Path:
    """entries: [{start, end, text}] in seconds."""

    blocks = []
    index = 0
    for entry in entries:
        text = (entry.get("text") or "").strip()
        if not text:
            continue
        index += 1
        blocks.append(
            f"{index}\n{_stamp(entry['start'])} --> {_stamp(entry['end'])}\n{text}\n"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(blocks), encoding="utf-8")
    return target
