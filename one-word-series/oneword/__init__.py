"""One word in, a whole series out — with the continuity written down.

A one-prompt pipeline keeps a series' facts in the model's context, so by
episode three the jacket has changed colour.  This keeps them in a file.

    bible.json ──► the same locked bytes injected into every prompt
                   of every shot of every episode

Public surface:

    from oneword import build_bible, run_episode, build_vendor, build_voice

    bible, _ = build_bible("rust", episodes=3, shots=5)
    bible.save("out/rust/bible.json")
    run_episode(bible, 1, "out/rust/episode-01",
                vendor=build_vendor("animatic"),
                voice_engine=build_voice("espeak"))

Everything creative comes from a model once, at bible time.  Everything that
has to stay identical is assembled locally, deterministically, forever after.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .assemble import build_episode
from .audit import FrameContinuityAuditor, RuleTriageAuditor, build_auditor
from .bible import SeriesBible, build_bible
from .contracts import BlockerFinding, ClipAuditor, FilmVendor, GeneratedClip, OneWordError
from .drift import DriftError, audit_series
from .registry import ReferenceRegistry
from .pipeline import build_shots, compose_prompt, run_episode, validate_episode_report
from .vendors import AnimaticVendor, SeedanceVendor, build_vendor
from .voice import build_voice, write_srt

__all__ = [
    "__version__",
    "AnimaticVendor",
    "BlockerFinding",
    "DriftError",
    "ReferenceRegistry",
    "ClipAuditor",
    "FilmVendor",
    "FrameContinuityAuditor",
    "GeneratedClip",
    "OneWordError",
    "RuleTriageAuditor",
    "SeedanceVendor",
    "SeriesBible",
    "audit_series",
    "build_auditor",
    "build_bible",
    "build_episode",
    "build_shots",
    "build_vendor",
    "build_voice",
    "compose_prompt",
    "run_episode",
    "validate_episode_report",
    "write_srt",
]
