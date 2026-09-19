"""What each location and character is *supposed* to look like.

The registry is the piece that makes continuity cross-episode rather than
merely within-episode.  The first time a location or character appears and
passes, its frame is adopted as that subject's reference still and written to
`references/` beside the bible.  Every later appearance, in every later
episode, in every later run, is compared against that same still.

## Why the reference is never re-based automatically

The obvious design is to compare each episode against the previous one.  It is
also the design that guarantees slow failure.  Episode 2 drifts three percent
from episode 1 and passes; episode 2 becomes the reference; episode 3 drifts
three percent from episode 2 and passes; by episode 9 nothing resembles episode
1 and every single check passed on the way there.

So the reference is adopted once and then frozen.  Re-basing exists, but it is
an explicit act — `adopt(..., force=True)`, surfaced as `--reset-references` —
and it is recorded in the registry with the episode that caused it, because a
re-base is a decision about the series, not a housekeeping detail.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import metrics

REGISTRY_VERSION = "reference-registry-1"
KINDS = ("location", "character")


class RegistryError(RuntimeError):
    pass


class ReferenceRegistry:
    """Persisted under `<series root>/references/`."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.dir = self.root / "references"
        self.index_path = self.dir / "index.json"
        self.data: dict[str, Any] = self._load()

    # ---- persistence -------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if self.index_path.is_file():
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            if data.get("registry_version") == REGISTRY_VERSION:
                return data
        return {
            "registry_version": REGISTRY_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "entries": {},
        }

    def save(self) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return self.index_path

    # ---- lookup ------------------------------------------------------

    @staticmethod
    def key(kind: str, subject_id: str) -> str:
        if kind not in KINDS:
            raise RegistryError(f"kind must be one of {KINDS}, got {kind!r}")
        return f"{kind}:{subject_id}"

    def get(self, kind: str, subject_id: str) -> dict[str, Any] | None:
        return self.data["entries"].get(self.key(kind, subject_id))

    def has(self, kind: str, subject_id: str) -> bool:
        return self.get(kind, subject_id) is not None

    def frame_path(self, kind: str, subject_id: str) -> Path | None:
        entry = self.get(kind, subject_id)
        if not entry:
            return None
        path = self.dir / entry["frame"]
        return path if path.is_file() else None

    def subjects(self) -> list[tuple[str, str]]:
        return [tuple(key.split(":", 1)) for key in sorted(self.data["entries"])]

    # ---- adoption ----------------------------------------------------

    def adopt(
        self,
        kind: str,
        subject_id: str,
        frame: str | Path,
        *,
        episode: int,
        shot_id: str,
        name: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        """Make this frame the reference.  Refuses to overwrite unless forced."""

        key = self.key(kind, subject_id)
        existing = self.data["entries"].get(key)
        if existing and not force:
            raise RegistryError(
                f"{key} already has a reference from episode {existing['episode']}; "
                "pass force=True (CLI: --reset-references) to re-base it deliberately"
            )

        source = Path(frame)
        if not source.is_file():
            raise RegistryError(f"reference frame does not exist: {source}")

        self.dir.mkdir(parents=True, exist_ok=True)
        stored = f"{kind}-{subject_id}{source.suffix or '.jpg'}"
        shutil.copyfile(source, self.dir / stored)

        entry = {
            "kind": kind,
            "id": subject_id,
            "name": name or subject_id,
            "frame": stored,
            "episode": int(episode),
            "shot_id": str(shot_id),
            "fingerprint": metrics.fingerprint(self.dir / stored),
            "adopted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if existing:
            history = list(existing.get("rebased_from", []))
            history.append(
                {
                    "episode": existing["episode"],
                    "shot_id": existing["shot_id"],
                    "adopted_at": existing["adopted_at"],
                }
            )
            entry["rebased_from"] = history

        self.data["entries"][key] = entry
        self.save()
        return entry

    def clear(self) -> None:
        """Forget every reference.  The next run establishes them again."""

        self.data["entries"] = {}
        if self.dir.is_dir():
            for path in self.dir.glob("*.jpg"):
                path.unlink(missing_ok=True)
            for path in self.dir.glob("*.png"):
                path.unlink(missing_ok=True)
        self.save()
