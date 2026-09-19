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

## Real footage

The harness is a stand-in. Once there are finished episodes on disk, measure
those instead — the same comparison, against the thing the thresholds are
actually applied to:

    python scripts/calibrate_drift.py --series out/rust [out/other …]

Real mode splits same-place pairs by **how the later shot was made**, because a
shot continued from the previous shot's last frame and a shot generated from
text are not one population. It reproduces production exactly: the earlier
shot's first frame is the reference, the later shot's three sampled frames are
the candidates, and the score is the best of the three — `metrics.best_distance`,
the same call `drift.audit_series` makes.
"""

from __future__ import annotations

import json
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


def bands_from(same: list[float], different: list[float]) -> tuple[float, float]:
    """The same cost-asymmetry rule the synthetic run uses.

    The pass bar sits under the closest genuinely-different pair; the flag bar
    sits over the furthest genuinely-same pair. Everything between is handed to
    the multimodal audit instead of being guessed at.
    """

    return min(different) * 0.95, max(same) * 1.02


# ──────────────────────────────────────────────────────────────────────
# Real footage
# ──────────────────────────────────────────────────────────────────────


def _shots_in(series: Path) -> list[dict]:
    """Every shot of every episode, fingerprinted, with how it was made."""

    from oneword import drift
    from oneword.audit import FRAME_POSITIONS, extract_frames

    workdir = series / "references" / ".calibration"
    found: list[dict] = []
    for report_path in sorted(series.glob("episode-*/episode-report.json")):
        data = json.loads(report_path.read_text(encoding="utf-8"))
        episode = int(data["episode"])
        for shot in data["shots"]:
            clip = report_path.parent / "clips" / shot["file"]
            if not clip.is_file():
                continue
            frames = extract_frames(clip, workdir, FRAME_POSITIONS)
            if not frames:
                continue
            state = drift.chain_state_for(data, shot["shot_id"])
            found.append(
                {
                    "series": series.name,
                    "episode": episode,
                    "shot_id": shot["shot_id"],
                    "location": shot.get("location", ""),
                    "chain_state": state["state"],
                    "prints": [metrics.fingerprint(frame) for frame in frames],
                }
            )
    return found


def measure_series(dirs: list[Path]) -> int:
    from oneword import drift

    shots: list[dict] = []
    for series in dirs:
        if not (series / "bible.json").is_file():
            print(f"{series} has no bible.json — is that a series directory?", file=sys.stderr)
            return 2
        found = _shots_in(series)
        print(f"{series}: {len(found)} shots with clips on disk")
        shots += found

    if len(shots) < 2:
        print("not enough shots to compare", file=sys.stderr)
        return 2

    buckets: dict[str, list[float]] = {}
    for i, earlier in enumerate(shots):
        for later in shots[i + 1:]:
            if earlier["series"] != later["series"]:
                continue
            # Production compares a later appearance's three frames against the
            # reference's first frame, and keeps the best. Same call here.
            score = metrics.best_distance(later["prints"], earlier["prints"][0])["composite"]
            if earlier["location"] != later["location"]:
                buckets.setdefault("different place", []).append(score)
            elif later["chain_state"] == drift.CHAINED:
                buckets.setdefault("same place, chained", []).append(score)
            else:
                buckets.setdefault("same place, unchained", []).append(score)

    print()
    for label in ("same place, chained", "same place, unchained", "different place"):
        if buckets.get(label):
            describe(label, buckets[label])
        else:
            print(f"{label:<24} n=0     — nothing in this series to measure")

    chained = buckets.get("same place, chained") or []
    unchained = buckets.get("same place, unchained") or []
    different = buckets.get("different place") or []

    print()
    if chained and unchained:
        print(
            f"Chained shots sit at p50 {statistics.median(chained):.4f}; unchained ones at "
            f"p50 {statistics.median(unchained):.4f}"
        )
        if max(chained) < min(unchained):
            print("and the two do not overlap at all. One threshold for both would be one")
            print("ruler held against two populations.")
        else:
            print("but the two overlap, so the split buys less than it looks like it does.")

    if not different:
        print()
        print("No different-place pairs: this series has one location, so there is no")
        print("control group and no honest flag bar can be derived. Measure a series")
        print("with at least two locations.")
        return 0

    print()
    print("Bands this footage implies (paste into oneword/drift.py BANDS):")
    for label, values in (("chained", chained), ("unchained", unchained)):
        if not values:
            print(f'    "{label}": nothing measured — leave measured_on: None')
            continue
        consistent_max, drifted_min = bands_from(values, different)
        if consistent_max >= drifted_min:
            # The bars crossing means the two distributions did NOT overlap:
            # every same-place pair is closer than every different-place pair,
            # so there is a clean gap and no need for a review band at all.
            # One cut in the middle of that gap decides everything.
            cut = (max(values) + min(different)) / 2
            print(f'    "{label}": {{"consistent_max": {cut:.4f}, '
                  f'"drifted_min": {cut:.4f}, '
                  f'"measured_on": "{len(values)} pairs, cleanly separated"}},')
            print(f"       ↑ the populations do not overlap here (same place tops out at "
                  f"{max(values):.4f}, different place starts at {min(different):.4f}),")
            print("         so one cut decides every pair and the review band is empty.")
            print("         That is a small sample talking — treat it as provisional.")
            continue
        print(f'    "{label}": {{"consistent_max": {consistent_max:.3f}, '
              f'"drifted_min": {drifted_min:.3f}, '
              f'"measured_on": "{len(values)} pairs from real footage"}},')

    print()
    print("These are measurements of the footage you have, not a law. Rerun them when")
    print("you have more episodes, and say in the docs what they were measured on.")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == "--series":
        return measure_series([Path(value) for value in argv[1:]] or [Path("out")])

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
