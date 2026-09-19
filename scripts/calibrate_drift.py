"""Measure where the drift thresholds belong, instead of guessing them.

The question the thresholds have to answer is not "are these frames different"
— every two frames are — but "is this difference bigger than the difference a
legitimate second shot of the same place already produces?"

So this builds a synthetic harness with a known answer:

* a ROOM is a procedural image with its own palette and its own architecture;
* a SHOT of that room varies the things a real second shot varies — camera
  crop, brightness, colour temperature, grain, and a subject standing somewhere
  in frame — while keeping the room itself;
* WITHIN pairs are two shots of the same room, which must read as consistent;
* ACROSS pairs are shots of two different rooms, which must read as drifted.

The gap between those two distributions is the only place a threshold can
honestly live. Run:

    python scripts/calibrate_drift.py

It prints the distributions and the thresholds they imply. If they overlap, it
says so rather than picking a number that hides the overlap.
"""

from __future__ import annotations

import random
import statistics
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw, ImageEnhance  # noqa: E402

from oneword import metrics  # noqa: E402

WIDTH, HEIGHT = 640, 360
ROOMS = 6
SHOTS_PER_ROOM = 8


def make_room(seed: int) -> Image.Image:
    """A room: its own palette, its own architecture, its own fixtures."""

    rng = random.Random(seed)
    base = (rng.randint(20, 90), rng.randint(20, 90), rng.randint(25, 100))
    floor = tuple(max(0, channel - rng.randint(10, 30)) for channel in base)
    image = Image.new("RGB", (WIDTH, HEIGHT), base)
    draw = ImageDraw.Draw(image)

    horizon = rng.randint(int(HEIGHT * 0.5), int(HEIGHT * 0.75))
    draw.rectangle([0, horizon, WIDTH, HEIGHT], fill=floor)

    # Architecture: a few permanent verticals and one opening.
    for _ in range(rng.randint(3, 6)):
        x = rng.randint(0, WIDTH - 40)
        w = rng.randint(14, 60)
        shade = tuple(min(255, channel + rng.randint(15, 60)) for channel in base)
        draw.rectangle([x, 0, x + w, horizon], fill=shade)

    opening = rng.randint(0, WIDTH - 120)
    draw.rectangle(
        [opening, horizon - rng.randint(90, 150), opening + rng.randint(60, 110), horizon],
        fill=tuple(min(255, channel + rng.randint(60, 130)) for channel in base),
    )

    # Fixtures: small, fixed, in fixed places.
    for _ in range(rng.randint(2, 5)):
        x = rng.randint(0, WIDTH - 30)
        y = rng.randint(0, HEIGHT - 30)
        size = rng.randint(8, 26)
        draw.ellipse(
            [x, y, x + size, y + size],
            fill=tuple(rng.randint(60, 200) for _ in range(3)),
        )
    return image


def make_shot(room: Image.Image, seed: int) -> Image.Image:
    """The same room, shot again: new angle, new light, someone in frame."""

    rng = random.Random(seed)

    # Camera: crop 70-100% of the frame from a shifted origin, then re-frame.
    scale = rng.uniform(0.70, 1.0)
    cw, ch = int(WIDTH * scale), int(HEIGHT * scale)
    left = rng.randint(0, WIDTH - cw)
    top = rng.randint(0, HEIGHT - ch)
    shot = room.crop((left, top, left + cw, top + ch)).resize((WIDTH, HEIGHT))

    # Light: exposure and colour temperature move between setups.
    shot = ImageEnhance.Brightness(shot).enhance(rng.uniform(0.78, 1.28))
    shot = ImageEnhance.Color(shot).enhance(rng.uniform(0.80, 1.25))

    # A subject occupies part of the frame, somewhere different each time.
    draw = ImageDraw.Draw(shot)
    sx = rng.randint(20, WIDTH - 120)
    sh = rng.randint(120, 240)
    draw.rectangle(
        [sx, HEIGHT - sh, sx + rng.randint(50, 110), HEIGHT],
        fill=tuple(rng.randint(30, 120) for _ in range(3)),
    )

    # Grain.
    pixels = shot.load()
    for _ in range(4000):
        x, y = rng.randrange(WIDTH), rng.randrange(HEIGHT)
        r, g, b = pixels[x, y]
        jitter = rng.randint(-18, 18)
        pixels[x, y] = (
            max(0, min(255, r + jitter)),
            max(0, min(255, g + jitter)),
            max(0, min(255, b + jitter)),
        )
    return shot


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def describe(label: str, values: list[float]) -> None:
    print(
        f"{label:<18} n={len(values):<5} "
        f"min={min(values):.4f}  p50={statistics.median(values):.4f}  "
        f"p95={percentile(values, 0.95):.4f}  max={max(values):.4f}"
    )


def main() -> int:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        prints: dict[int, list[dict]] = {}
        for room_id in range(ROOMS):
            room = make_room(seed=1000 + room_id)
            prints[room_id] = []
            for shot_id in range(SHOTS_PER_ROOM):
                path = root / f"r{room_id}-s{shot_id}.jpg"
                make_shot(room, seed=room_id * 100 + shot_id).save(path, quality=85)
                prints[room_id].append(metrics.fingerprint(path))

        within: list[float] = []
        across: list[float] = []
        for room_id, shots in prints.items():
            for i in range(len(shots)):
                for j in range(i + 1, len(shots)):
                    within.append(metrics.distance(shots[i], shots[j])["composite"])
            for other, other_shots in prints.items():
                if other <= room_id:
                    continue
                for a in shots:
                    for b in other_shots:
                        across.append(metrics.distance(a, b)["composite"])

    print(f"\nrooms={ROOMS}  shots per room={SHOTS_PER_ROOM}\n")
    describe("same room", within)
    describe("different room", across)

    print()
    if percentile(within, 0.95) >= percentile(across, 0.05):
        print(
            "The distributions OVERLAP: some second shots of one room differ more than\n"
            "some pairs of genuinely different rooms. No single cut separates them, so\n"
            "the bands below are set by cost, not by a split point."
        )

    # The two errors are not equally expensive. Calling drift "consistent" ships
    # a broken series; calling a fine shot "review" costs one model call. So the
    # pass bar sits under the closest different-room pair ever observed, the flag
    # bar sits above the furthest same-room pair, and everything between — which
    # is most of it — is handed to the multimodal audit rather than guessed at.
    same_max = min(across) * 0.95
    different_min = max(within) * 1.02
    review_share = sum(1 for v in within + across if same_max < v < different_min)
    total = len(within) + len(across)

    print(f"\nSAME_PLACE_MAX      = {same_max:.2f}   "
          f"(under the closest different-room pair, {min(across):.4f})")
    print(f"DIFFERENT_PLACE_MIN = {different_min:.2f}   "
          f"(over the furthest same-room pair, {max(within):.4f})")
    print(f"\nreview band covers {review_share / total * 100:.0f}% of all pairs — "
          "that is the screen admitting what it cannot decide alone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
