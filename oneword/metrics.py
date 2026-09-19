"""A cheap, local, honest frame fingerprint.

This module answers exactly one question: **do these two frames plausibly show
the same place?**  It cannot tell you the handrail changed from green to grey.
It can tell you the second frame is not the same room at all, for free, without
a model and without a network call — which is enough to decide whether paying
for a multimodal opinion is worth it.

Two signals, because either alone is easy to fool:

`grid`   an 8x8 map of mean RGB.  Spatial, not a global histogram: a red wall
         on the left and a red wall on the right are different here, whereas a
         plain colour histogram calls them identical.
`edges`  a 4x4 map of mean edge magnitude.  A rough structural proxy — a room
         whose architecture was rebuilt moves here even when the palette is
         held.  It survives a relight that the colour grid would over-react to.

Both are deliberately coarse.  A shot is allowed to have a different subject,
a different camera angle and different lighting from its reference and still be
the same location; the fingerprint is built to tolerate that and to notice only
wholesale change.  Thresholds are not invented — see `docs/drift-calibration.md`
for the measurements they come from.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

GRID = 8
EDGE_GRID = 4
COLOUR_WEIGHT = 0.65
STRUCTURE_WEIGHT = 0.35
FINGERPRINT_VERSION = 2


def _cell_means(image, cells: int) -> list[float]:
    """Mean value per cell, row-major.  Uses Pillow's own box resample.

    Reads the raw buffer rather than `getdata()`: same numbers, no deprecation,
    and RGB arrives already flattened to R,G,B,R,G,B…
    """

    small = image.resize((cells, cells))
    return [float(value) for value in small.tobytes()]


def fingerprint(path: str | Path) -> dict[str, Any]:
    """Compact, JSON-serialisable description of one frame."""

    from PIL import Image, ImageFilter

    with Image.open(path) as handle:
        rgb = handle.convert("RGB")
        grid = _cell_means(rgb, GRID)
        grey = rgb.convert("L")
        edges = _cell_means(grey.filter(ImageFilter.FIND_EDGES), EDGE_GRID)
        luma = grey.resize((1, 1)).tobytes()[0]

    return {
        "version": FINGERPRINT_VERSION,
        "grid": [round(value, 2) for value in grid],
        "edges": [round(value, 2) for value in edges],
        "luma": round(float(luma), 2),
    }


def _mean_abs_diff(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        raise ValueError("fingerprints are not comparable")
    return sum(abs(a - b) for a, b in zip(left, right)) / len(left)


def _exposure_normalised(grid: list[float]) -> list[float]:
    """Subtract the frame's own mean, so a relight is not read as a new room.

    Two setups in one location routinely differ by a stop or two, and without
    this the brightness difference swamps the layout difference the grid is
    actually there to measure.  Measured effect on the calibration harness:
    same-room p95 drops from 0.081 to 0.061 while the closest different-room
    pair barely moves, which is exactly the direction that matters.
    """

    mean = sum(grid) / len(grid)
    return [value - mean for value in grid]


def distance(left: dict[str, Any], right: dict[str, Any]) -> dict[str, float]:
    """0.0 identical … 1.0 nothing alike.  Returns the parts, not just a number.

    Handing back the components matters: a run that is far apart on colour but
    close on structure is a relight, and a run that is close on colour but far
    on structure is a rebuilt set.  Those want different fixes, and a single
    scalar would hide the difference.
    """

    if left.get("version") != right.get("version"):
        raise ValueError("fingerprint versions differ; recompute both sides")

    colour = _mean_abs_diff(
        _exposure_normalised(left["grid"]), _exposure_normalised(right["grid"])
    ) / 255.0
    structure = _mean_abs_diff(left["edges"], right["edges"]) / 255.0
    return {
        "colour": round(colour, 4),
        "structure": round(structure, 4),
        "composite": round(COLOUR_WEIGHT * colour + STRUCTURE_WEIGHT * structure, 4),
    }


def best_distance(candidates: list[dict[str, Any]], reference: dict[str, Any]) -> dict[str, float]:
    """The closest of several frames from one shot.

    A shot is sampled at three points and a subject can walk across the frame
    between them, so the fairest comparison against a reference still is the
    best of the three, not their average.  Being harsh on a wide gesture is how
    you end up with a drift detector nobody leaves switched on.
    """

    if not candidates:
        raise ValueError("no candidate fingerprints")
    scored = [distance(candidate, reference) for candidate in candidates]
    return min(scored, key=lambda item: item["composite"])


def load(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(data: dict[str, Any], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target
