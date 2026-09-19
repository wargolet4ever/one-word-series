"""The same face, shot after shot.

## What the bible can and cannot do

The bible locks a character's *described* facts and puts them, byte for byte,
into every prompt: late thirties, wiry, a scar through the left eyebrow, an
olive jacket with a torn cuff. That description stops the cuff mending itself
between episodes.

It does not give you the same actor. A description names a **type**, not a
person, and a model generating from text alone casts a new face that fits the
description every single time. No amount of prompt writing fixes that, because
words are not a face.

Identity needs a picture. A portrait of the character, sent with every shot
they appear in, is what makes shot 9 the same person as shot 1.

## Where the portrait comes from

Supplied, or adopted. You can hand one in, and otherwise the first shot that
features that character **alone** becomes their portrait — the same bargain the
reference registry makes for locations: the first appearance defines the truth,
and it is then frozen. Frozen matters for the same reason: re-adopting a face
each episode is how a series slowly becomes a different cast while every
individual step looks fine.

A shot with two people in it is never adopted from; there would be no way to
say which face was whose.

## What can go wrong, and does

Platforms moderate input images. Ark refuses a frame it reads as a photograph
of a real person, which is exactly what a good photorealistic portrait looks
like. So a portrait is an attempt, never a guarantee, and the caller degrades
to text rather than losing the shot. Non-photographic styles — animation,
watercolour, stop motion — are far less likely to be refused.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CAST_VERSION = "cast-portraits-1"
MAX_REFERENCES_PER_SHOT = 2


class CastError(RuntimeError):
    pass


def _ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def middle_frame(clip: Path, target: Path) -> Path | None:
    """A frame from the middle of a clip — past the cut, before the end."""

    target.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-loglevel", "error",
         "-sseof", "-2", "-i", str(clip),
         "-update", "1", "-frames:v", "1", "-q:v", "2", "-y", str(target)],
        capture_output=True, text=True, check=False,
    )
    if completed.returncode == 0 and target.is_file() and target.stat().st_size:
        return target
    return None


def parse_supplied(specs: list[str] | None) -> dict[str, Path]:
    """`--cast-image C1=face.jpg` → {"C1": Path("face.jpg")}."""

    supplied: dict[str, Path] = {}
    for spec in specs or []:
        if "=" not in spec:
            raise CastError(f"--cast-image wants ID=path, got {spec!r}")
        key, _, value = spec.partition("=")
        path = Path(value).expanduser()
        if not path.is_file():
            raise CastError(f"cast image for {key.strip()} does not exist: {path}")
        supplied[key.strip()] = path
    return supplied


@dataclass
class Portrait:
    character_id: str
    path: Path
    source: str
    style: str


class CastPortraits:
    """Persisted under `<series root>/cast/`."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.dir = self.root / "cast"
        self.index_path = self.dir / "index.json"
        self.data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if self.index_path.is_file():
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            if data.get("cast_version") == CAST_VERSION:
                return data
        return {"cast_version": CAST_VERSION, "portraits": {}}

    def save(self) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return self.index_path

    # ---- lookup ------------------------------------------------------

    def get(self, character_id: str) -> Portrait | None:
        entry = self.data["portraits"].get(character_id)
        if not entry:
            return None
        path = self.dir / entry["file"]
        if not path.is_file():
            return None
        return Portrait(character_id, path, entry.get("source", "unknown"), entry.get("style", ""))

    def styles_present(self) -> set[str]:
        return {str(e.get("style") or "unknown") for e in self.data["portraits"].values()}

    def for_shot(self, character_ids: list[str]) -> list[Path]:
        """Portraits for the people in this shot, in the shot's own order."""

        found = [self.get(cid) for cid in character_ids]
        return [p.path for p in found if p][:MAX_REFERENCES_PER_SHOT]

    # ---- adoption ----------------------------------------------------

    def put(
        self,
        character_id: str,
        image: Path,
        *,
        source: str,
        style: str = "",
        from_shot: str = "",
        force: bool = False,
    ) -> Portrait:
        if self.get(character_id) and not force:
            # Frozen for the same reason location references are: re-adopting a
            # face every episode is how a cast changes while each step passes.
            return self.get(character_id)

        image = Path(image)
        if not image.is_file():
            raise CastError(f"portrait image does not exist: {image}")
        self.dir.mkdir(parents=True, exist_ok=True)
        stored = f"{character_id}{image.suffix or '.jpg'}"
        shutil.copyfile(image, self.dir / stored)
        self.data["portraits"][character_id] = {
            "file": stored,
            "source": source,
            "style": style or "unknown",
            "from_shot": from_shot,
            "adopted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.save()
        return self.get(character_id)

    def adopt_from_clip(
        self,
        character_id: str,
        clip: Path,
        *,
        style: str = "",
        from_shot: str = "",
        workdir: Path | None = None,
    ) -> Portrait | None:
        """Take this character's face from a shot they are alone in."""

        if self.get(character_id):
            return self.get(character_id)
        workdir = Path(workdir or self.dir / ".frames")
        frame = middle_frame(Path(clip), workdir / f"{character_id}-candidate.jpg")
        if frame is None:
            return None
        return self.put(
            character_id, frame, source="adopted", style=style, from_shot=from_shot
        )

    def clear(self) -> None:
        self.data["portraits"] = {}
        if self.dir.is_dir():
            for pattern in ("*.jpg", "*.jpeg", "*.png"):
                for path in self.dir.glob(pattern):
                    path.unlink(missing_ok=True)
        self.save()
