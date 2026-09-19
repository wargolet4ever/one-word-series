"""Named looks, chosen once and then locked like everything else.

## Why style is not a free parameter

The argument this whole tool makes is that the look does not drift. If the
look can be changed casually — per episode, per shot, per run — that argument
is gone, and the drift detector is worse than useless: it compares a frame
against a reference shot under a different look and reports a series that has
fallen apart, when all that happened is somebody tried a preset.

So a style is picked once, written into the bible, and injected byte-identically
into every prompt, exactly like a character's torn cuff. Changing it is allowed
and is a deliberate act: it rewrites the bible's style block and invalidates
every reference still, because in a new look, every frame legitimately differs
from the old one. `drift.audit_series` refuses to compare across a style change
and says to re-base rather than producing a report full of false drift.

## What belongs in a style and what does not

A style is **how it is photographed**: medium, lens, lighting, palette, grade,
and what the look must never contain.

A character's `locked_appearance` is **what is true about them**: the torn left
cuff, the scar through the eyebrow, the round wire glasses. Those facts survive
a change of medium — the cuff is still torn when the series is redrawn as
animation — so they live in the bible's cast, not here, and a restyle leaves
them untouched.

Getting that line wrong is how you end up with a "style" that quietly changes
who the characters are.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

STYLE_VERSION = "style-1"

PRESETS: dict[str, dict[str, str]] = {
    "documentary": {
        "label": "Observational documentary",
        "render": "photographic, shot on location",
        "lens": "35mm anamorphic, shallow depth of field, slight handheld weight",
        "lighting": "hard key from one practical source, deep unlit shadow, no fill",
        "palette": "desaturated teal and rust, one warm practical in frame",
        "tone": "restrained, cold, unstaged",
        "negative": "cinematic lens flare, colour grading gloss, staged posing, CGI sheen",
    },
    "noir": {
        "label": "Black and white noir",
        "render": "photographic, black and white",
        "lens": "40mm spherical, deep focus, locked-off or slow dolly",
        "lighting": "single hard source, venetian-blind shadows, crushed blacks, hot highlights",
        "palette": "black and white, no colour anywhere in frame",
        "tone": "fatalistic, still, heavy",
        "negative": "colour, pastel, soft even lighting, modern digital clarity",
    },
    "anime": {
        "label": "Hand-drawn animation",
        "render": "2D cel animation, visible line art, flat colour fills",
        "lens": "wide compositions, limited animation, held frames with slow pans",
        "lighting": "painted light, hard cel shadows in two tones, bloom on practicals",
        "palette": "saturated sky blues and warm ochres, high-contrast key art",
        "tone": "composed, deliberate, quietly emotional",
        "negative": "photorealism, 3D render, live-action faces, motion blur",
    },
    "storybook": {
        "label": "Watercolour storybook",
        "render": "watercolour and ink on paper, visible paper grain and brush edges",
        "lens": "flat frontal staging, shallow stage-like depth",
        "lighting": "soft diffuse daylight, no harsh shadow, colour bleeding at edges",
        "palette": "muted washes, warm paper white, ink-black linework",
        "tone": "gentle, unhurried, slightly melancholy",
        "negative": "photorealism, sharp digital edges, lens flare, 3D shading",
    },
    "16mm": {
        "label": "16mm film",
        "render": "photographic, 16mm film stock with visible grain and gate weave",
        "lens": "25mm, moderate depth of field, occasional focus breathing",
        "lighting": "available light, blown windows, halation around highlights",
        "palette": "warm fading stock, green-leaning shadows, milky blacks",
        "tone": "nostalgic, immediate, imperfect",
        "negative": "clean digital sharpness, noise reduction, HDR, stabilised smoothness",
    },
    "clinical": {
        "label": "Clean and clinical",
        "render": "photographic, high-key studio",
        "lens": "50mm, everything in focus, static tripod framing",
        "lighting": "large soft sources, even fill, no visible shadow direction",
        "palette": "white, pale grey, one accent colour used sparingly",
        "tone": "precise, neutral, airless",
        "negative": "grain, lens flare, handheld shake, moody shadow",
    },
    "analog": {
        "label": "Analog video",
        "render": "VHS-era analog video, interlacing artefacts, tape noise",
        "lens": "zoom lens, soft corners, auto-exposure hunting",
        "lighting": "on-camera light, blown faces, dark falloff behind",
        "palette": "smeared reds, chroma bleed, low contrast",
        "tone": "uneasy, surveillance-like, degraded",
        "negative": "4K clarity, modern colour science, filmic grain, stabilisation",
    },
    "stopmotion": {
        "label": "Stop motion",
        "render": "stop-motion puppets, felt and painted surfaces, visible fingerprints",
        "lens": "macro, very shallow focus, tiny set depth",
        "lighting": "miniature practicals, hard small sources, visible set edges",
        "palette": "handmade materials, warm wood and wool, dusty pastels",
        "tone": "tactile, careful, slightly uncanny",
        "negative": "smooth CGI, live-action skin, motion blur, digital perfection",
    },
}

SHARED_NEGATIVE = "text, watermark, extra fingers, warped architecture, style change"


class StyleError(ValueError):
    pass


def names() -> list[str]:
    return sorted(PRESETS)


def get(name: str) -> dict[str, str]:
    key = (name or "").strip().lower()
    if key not in PRESETS:
        raise StyleError(
            f"unknown style {name!r}. Available: {', '.join(names())}. "
            "A custom one can be supplied as a JSON file — see README."
        )
    return dict(PRESETS[key], name=key)


def load_custom(path: str | Path) -> dict[str, str]:
    """A style of your own, as JSON with the same keys as a preset."""

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    required = ("render", "lens", "lighting", "palette", "tone")
    missing = [key for key in required if not str(data.get(key, "")).strip()]
    if missing:
        raise StyleError(f"custom style is missing: {', '.join(missing)}")
    data.setdefault("negative", "")
    data.setdefault("label", Path(path).stem)
    data["name"] = data.get("name") or Path(path).stem
    return {key: str(value) for key, value in data.items()}


def resolve(style: str | None) -> dict[str, str] | None:
    """A preset name, or a path to a JSON file, or nothing."""

    if not style:
        return None
    candidate = Path(style)
    if candidate.suffix.lower() == ".json" and candidate.is_file():
        return load_custom(candidate)
    return get(style)


def apply_to(data: dict[str, Any], style: dict[str, str]) -> dict[str, Any]:
    """Write the style into a bible, leaving the cast and locations alone.

    Deliberately narrow: `world.premise`, every `locked_appearance` and every
    `locked_description` are untouched. A restyle changes how the series is
    photographed, never who is in it or what the rooms contain.
    """

    negative = ", ".join(
        part for part in (SHARED_NEGATIVE, style.get("negative", "").strip()) if part
    )
    data["visual_grammar"] = {
        "render": style["render"],
        "lens": style["lens"],
        "lighting": style["lighting"],
        "negative": negative,
    }
    world = data.setdefault("world", {})
    world["palette"] = style["palette"]
    world["tone"] = style["tone"]
    data["style"] = {
        "style_version": STYLE_VERSION,
        "name": style.get("name", "custom"),
        "label": style.get("label", style.get("name", "custom")),
    }
    return data


def current_name(data: dict[str, Any]) -> str:
    """What a bible is shot in.  Bibles written before styles say so."""

    style = data.get("style") or {}
    return str(style.get("name") or "model-written")
