"""What has already been paid for.

A paid run that dies at shot 8 leaves seven finished clips on disk and no way
to use them: the next run starts at shot 1 and buys all seven again. That
happened, it cost real money, and the fix is not clever — it is writing down
what was bought, at the moment it was bought.

So every successful generation drops a small JSON file beside its clip. The
next run reads those and skips what it already has.

## What makes a clip reusable, and what must not

Reuse is only safe when the next run would have asked for **exactly the same
thing**, so the ledger records a fingerprint of the prompt and the vendor that
produced it. A clip is reused only when both match and the file is still
readable.

That one rule covers the cases that matter without special-casing any of them:
edit the bible, change the style, switch model, change resolution — the prompt
or the vendor differs, the fingerprint misses, and the shot is made again. A
resume that silently kept a clip from a different look would be far more
expensive than re-buying one.

The ledger is a record of spending, not a cache to be trusted blindly: it says
what was made and what it cost, and `--fresh` ignores it entirely.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LEDGER_VERSION = "clip-ledger-1"


def fingerprint(prompt: str) -> str:
    """Short, stable hash of the exact prompt a clip was made from."""

    return hashlib.sha256((prompt or "").encode("utf-8")).hexdigest()[:16]


def sidecar_for(clip: Path) -> Path:
    return Path(clip).with_suffix(".json")


def record(
    clip: Path,
    *,
    shot_id: str,
    attempt: int,
    prompt: str,
    provider: str,
    cost_cny: float | None = None,
    chain_dropped: str | None = None,
    continues_shot: str | None = None,
) -> Path:
    """Write down what this clip is, immediately after it lands."""

    entry = {
        "ledger_version": LEDGER_VERSION,
        "shot_id": str(shot_id),
        "attempt": int(attempt),
        "prompt_fingerprint": fingerprint(prompt),
        "provider": provider,
        "cost_cny": cost_cny,
        "chain_dropped": chain_dropped,
        "continues_shot": continues_shot,
        "file": Path(clip).name,
    }
    target = sidecar_for(clip)
    target.write_text(json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


@dataclass(frozen=True)
class Reusable:
    path: Path
    attempt: int
    entry: dict[str, Any]

    @property
    def cost_cny(self) -> float:
        value = self.entry.get("cost_cny")
        return float(value) if value else 0.0

    @property
    def chain_dropped(self) -> str | None:
        return self.entry.get("chain_dropped")

    @property
    def continues_shot(self) -> str | None:
        return self.entry.get("continues_shot")


def find_reusable(
    clips_dir: Path,
    shot_id: str,
    prompt: str,
    provider: str,
) -> Reusable | None:
    """The newest take of this shot that was made from exactly this prompt."""

    clips_dir = Path(clips_dir)
    if not clips_dir.is_dir():
        return None

    wanted = fingerprint(prompt)
    best: Reusable | None = None
    for sidecar in clips_dir.glob("*.json"):
        try:
            entry = json.loads(sidecar.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if entry.get("ledger_version") != LEDGER_VERSION:
            continue
        if str(entry.get("shot_id")) != str(shot_id):
            continue
        if entry.get("prompt_fingerprint") != wanted:
            continue
        if entry.get("provider") != provider:
            continue

        clip = clips_dir / str(entry.get("file") or "")
        if not clip.is_file() or clip.stat().st_size == 0:
            continue
        attempt = int(entry.get("attempt", 1))
        if best is None or attempt > best.attempt:
            best = Reusable(path=clip, attempt=attempt, entry=entry)
    return best
