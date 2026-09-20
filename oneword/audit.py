"""Does this clip still obey the bible?

One rule governs everything here: a conclusion reached without looking at
pixels is never dressed up as one that looked.  The whole value of an
automatic PASS is that it means something.

    RULE TRIAGE ONLY     no frames examined
    FRAME VISUAL AUDIT   a multimodal model compared ordered frames against
                         the locked descriptions

A clip whose frames could not be read is HUMAN REVIEW, never PASS.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .contracts import BlockerFinding

from . import llm

RULE_TRIAGE = "RULE TRIAGE ONLY"
FRAME_AUDIT = "FRAME VISUAL AUDIT"
FRAME_POSITIONS = (0.1, 0.5, 0.9)

# A vision call carries two inlined frames and is answered by a slower model
# than a text prompt. Overridable because "slow" is a property of the account
# and the model, not of this code.
VISION_TIMEOUT_S = int(os.getenv("ONEWORD_VISION_TIMEOUT", "420"))

SYSTEM = (
    "You are a strict visual continuity auditor for a film series. "
    "You are given ordered frames from ONE shot and the written facts that must "
    "hold in every shot of the series. Report only violations you can see in the "
    "frames. Do not invent motion between frames. Output JSON only."
)


def _ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def _duration(path: Path) -> float:
    probe = shutil.which("ffprobe")
    if not probe:
        return 0.0
    completed = subprocess.run(
        [probe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=False,
    )
    try:
        return float(completed.stdout.strip())
    except ValueError:
        return 0.0


def extract_frames(clip: Path, workdir: Path, positions=FRAME_POSITIONS) -> list[Path]:
    """Ordered stills at fixed fractions of the clip.  Empty list on failure."""

    workdir.mkdir(parents=True, exist_ok=True)
    total = _duration(clip)
    frames: list[Path] = []
    for index, fraction in enumerate(positions, start=1):
        target = workdir / f"{clip.stem}-f{index}.jpg"
        timestamp = max(total * fraction, 0.0) if total else 0.0
        completed = subprocess.run(
            [_ffmpeg(), "-hide_banner", "-loglevel", "error",
             "-ss", f"{timestamp:.3f}", "-i", str(clip),
             "-frames:v", "1", "-q:v", "4", "-y", str(target)],
            capture_output=True, text=True, check=False,
        )
        if completed.returncode == 0 and target.is_file() and target.stat().st_size:
            frames.append(target)
    return frames


def _data_url(path: Path) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


class RuleTriageAuditor:
    """Looks at nothing.  Says so.  Useful when there is no multimodal key."""

    name = "rule-triage"
    evidence = RULE_TRIAGE

    def audit(self, clip, shot: dict[str, Any], bible) -> list[BlockerFinding]:
        return []


class FrameContinuityAuditor:
    """Pulls three ordered frames and checks them against the bible's rules."""

    name = "frame-continuity"

    def __init__(self, bible, *, workdir: Path, opener=None) -> None:
        self.bible = bible
        self.workdir = Path(workdir)
        self.last_evidence = RULE_TRIAGE

    def _facts(self, shot: dict[str, Any]) -> str:
        lines = list(self.bible.rule_lines())
        lines.append(
            f"LOCATION {shot['location_id']}: "
            f"{self.bible.location(shot['location_id']).get('locked_description', '')}"
        )
        for cid in shot.get("character_ids", []):
            character = self.bible.character(cid)
            lines.append(
                f"CHARACTER {character.get('name', cid)}: {character.get('locked_appearance', '')}"
            )
        return "\n".join(lines)

    def audit(self, clip, shot: dict[str, Any], bible=None) -> list[BlockerFinding]:
        self.last_evidence = RULE_TRIAGE
        frames = extract_frames(Path(clip.path), self.workdir)
        if not frames or not llm.configured():
            return []

        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    "Series facts that must hold in every frame:\n"
                    f"{self._facts(shot)}\n\n"
                    f"This shot: {shot.get('action', '')}\nCamera: {shot.get('camera', '')}\n\n"
                    'Return {"issues":[{"rule_id":"SER-01","evidence":"what you see",'
                    '"minimal_fix":"shortest prompt change that fixes it",'
                    '"severity":"regenerate|local_fix"}]}. Empty array if all facts hold.'
                ),
            }
        ]
        for index, frame in enumerate(frames, start=1):
            content.append({"type": "text", "text": f"FRAME {index}/{len(frames)}"})
            content.append({"type": "image_url", "image_url": {"url": _data_url(frame)}})

        try:
            parsed = _chat_vision(SYSTEM, content)
        except Exception:  # noqa: BLE001 — a failed audit downgrades, never upgrades
            return []

        self.last_evidence = FRAME_AUDIT
        findings: list[BlockerFinding] = []
        for issue in llm.items_under(parsed, "issues"):
            text = (issue.get("evidence") or "").strip()
            if not text:
                continue
            findings.append(
                BlockerFinding(
                    rule_id=str(issue.get("rule_id") or "SER-??"),
                    evidence=text[:400],
                    minimal_fix=(issue.get("minimal_fix") or "regenerate this shot").strip()[:300],
                    severity="local_fix" if issue.get("severity") == "local_fix" else "regenerate",
                )
            )
        return findings


def _chat_vision(system: str, content: list[dict[str, Any]]) -> dict[str, Any]:
    """One vision call, and on failure the platform's reason rather than a number.

    A vision model id that is wrong, unactivated, or text-only all answer 404
    or 400 with the actual reason in the body. Raising `HTTP Error 404: Not
    Found` sends you to check your key, your network and your spelling in that
    order, when the platform already said which one it was.
    """

    payload = {
        "model": os.environ["LLM_MODEL"],
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
    }
    try:
        # Two inlined frames is a far larger request than a text prompt, and a
        # vision model is slower to answer one. The text default timed these
        # out on real frames, and a timeout costs the appearance entirely.
        body = llm.post_chat(payload, timeout=VISION_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — re-raised with the reason attached
        raise llm.ModelUnavailable(
            f"vision model {os.environ.get('LLM_MODEL', '?')} failed: "
            f"{llm.error_detail(exc)}"
        ) from exc
    return llm._extract_json(body["choices"][0]["message"]["content"])


def continuity_agent_available() -> bool:
    """Is the Continuity-Agent verifier importable in this environment?

    It is an optional extra, never a requirement.  When it is installed you can
    hand it a canon and get the hand-maintained rule packs, the causal audit and
    the five-level evidence taxonomy — a far deeper check than the three frames
    this module looks at.  See README → "Deeper checks".

        pip install "one-word-series[continuity]"

    https://github.com/wargolet4ever/Continuity-Agent
    """

    try:
        import canon_loader  # noqa: F401
    except Exception:  # noqa: BLE001 — absence is the normal case
        return False
    return True


def build_auditor(kind: str, bible, workdir: Path):
    kind = (kind or "auto").lower()
    if kind in {"none", "triage"}:
        return RuleTriageAuditor()
    if kind in {"auto", "frames", "vision"}:
        return FrameContinuityAuditor(bible, workdir=workdir)
    raise ValueError(f"unknown auditor '{kind}'")
