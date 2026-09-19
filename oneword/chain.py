"""Continuing a shot from where the last one ended.

Every shot so far has been generated from text alone, so two consecutive shots
in one room are two independent guesses at that room. The cut between them
jumps: the light moves, the furniture shifts, the wall is a slightly different
grey. The bible stops the room from becoming a *different* room; it cannot make
one shot continue the other.

Image-to-video can. Hand the model the last frame of the previous shot as the
first frame of the next, and the two are physically continuous.

## When that is legitimate, and when it is a lie

Chaining is only honest between shots that really are continuous:

* **same location** — chaining across a cut to another room would paste the
  wrong room into the first frame and the model would obediently keep it;
* **adjacent in the cut** — shot 4 continues shot 3, never shot 1;
* **not across a repair** — if shot 3 is regenerated after shot 4 was chained
  from it, shot 4 now continues a take that no longer exists.

That last case is the interesting one. The obvious response — regenerate the
dependents too — spends money the user never approved, and this project's rule
is that only a blocker spends again. So the chain is marked stale in the report
instead, and left for a person to decide about. A stale chain that says so is
worth more than a silent re-shoot.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
from pathlib import Path
from typing import Any

CHAIN_MODES = ("auto", "off")


class ChainError(RuntimeError):
    pass


def _ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def last_frame(clip: Path, target: Path) -> Path | None:
    """The final frame of a clip, as a jpg.  None if it cannot be read.

    Seeks from the end rather than to a timestamp: clip durations come back
    slightly different from what was asked for, and a seek past the end yields
    nothing at all.
    """

    target.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-loglevel", "error",
         "-sseof", "-0.5", "-i", str(clip),
         "-update", "1", "-frames:v", "1", "-q:v", "2", "-y", str(target)],
        capture_output=True, text=True, check=False,
    )
    if completed.returncode == 0 and target.is_file() and target.stat().st_size:
        return target
    return None


def data_url(frame: Path) -> str:
    suffix = frame.suffix.lower().lstrip(".") or "jpeg"
    media = "jpeg" if suffix in ("jpg", "jpeg") else suffix
    return f"data:image/{media};base64," + base64.b64encode(frame.read_bytes()).decode("ascii")


def eligible(previous: dict[str, Any] | None, shot: dict[str, Any]) -> bool:
    """Is this shot a continuation of the one before it?"""

    if previous is None:
        return False
    if previous.get("location_id") != shot.get("location_id"):
        return False
    try:
        return int(shot["shot_id"]) == int(previous["shot_id"]) + 1
    except (KeyError, TypeError, ValueError):
        return False


def plan(shots: list[dict[str, Any]]) -> dict[str, str]:
    """{shot_id: the shot_id it continues}, for every chainable pair."""

    links: dict[str, str] = {}
    for index, shot in enumerate(shots):
        previous = shots[index - 1] if index else None
        if eligible(previous, shot):
            links[str(shot["shot_id"])] = str(previous["shot_id"])
    return links


def stale_links(
    links: dict[str, str],
    generation_events: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Chains whose source shot was regenerated after they were built.

    Nothing is re-shot in response. The report carries the fact so a person can
    decide whether the jump matters, which is the same bargain the rest of the
    tool makes: say what happened, do not spend on their behalf.
    """

    round_of: dict[str, int] = {}
    for event in generation_events:
        shot_id = str(event["shot_id"])
        round_of[shot_id] = max(round_of.get(shot_id, 0), int(event.get("round", 0)))

    stale = []
    for shot_id, source_id in links.items():
        if round_of.get(source_id, 0) > round_of.get(shot_id, 0):
            stale.append(
                {
                    "shot_id": shot_id,
                    "continues": source_id,
                    "reason": (
                        f"shot {source_id} was regenerated after shot {shot_id} "
                        "was chained from it, so the first frame is from a take "
                        "that is no longer in the episode"
                    ),
                }
            )
    return sorted(stale, key=lambda item: int(item["shot_id"]))
