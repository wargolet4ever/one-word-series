"""The types every other module agrees on.

These three are deliberately tiny and dependency-free.  They are the boundary
a vendor, an auditor and the pipeline meet at, so keeping them in one small
file is what lets you swap in a new video model by writing one class.

They are wire-compatible with the same names in the Continuity-Agent verifier
(https://github.com/wargolet4ever/Continuity-Agent).  A `BlockerFinding` made
there can be handed to the pipeline here and vice versa — see
`oneword.audit.continuity_agent_available()` for the optional deep-audit path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

SEVERITIES = ("regenerate", "local_fix")


class OneWordError(RuntimeError):
    """Base for every error this package raises on purpose."""

    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class BlockerFinding:
    """One thing wrong with one clip.

    `severity` decides what happens next, and only two values mean anything:
    `regenerate` costs money and reshoots the shot, `local_fix` is a note for
    the edit.  Anything else is rejected rather than quietly treated as minor.
    """

    rule_id: str
    evidence: str
    minimal_fix: str
    severity: str = "regenerate"

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"severity must be one of {SEVERITIES}, got {self.severity!r}")


@dataclass
class GeneratedClip:
    """One take of one shot, on disk."""

    shot_id: str
    attempt: int
    provider: str
    path: Path
    prompt: str
    # Set when a first frame was refused and the shot was made from text
    # instead, so the report can say the cut may jump rather than implying a
    # continuity that was never achieved.
    chain_dropped: str | None = None


@runtime_checkable
class FilmVendor(Protocol):
    """Anything that can turn a prompt into a clip file.

    `generative` is not decoration.  The pipeline refuses to attach a visual
    evidence label to output from a vendor that declares False, because no
    model looked at those pixels and the report must not imply one did.
    """

    name: str
    generative: bool

    def generate(
        self,
        shot: dict[str, Any],
        prompt: str,
        attempt: int,
        target: Path,
    ) -> GeneratedClip: ...


@runtime_checkable
class ClipAuditor(Protocol):
    """Anything that can say what is wrong with a clip.

    `last_evidence` must describe how the most recent verdict was reached, so
    a PASS reached without looking is never confused with one that looked.
    """

    name: str
    last_evidence: str

    def audit(self, clip: GeneratedClip, shot: dict[str, Any], bible: Any) -> list[BlockerFinding]: ...
