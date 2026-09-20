#!/usr/bin/env python3
"""The three seconds that decide whether a stranger reads the README.

A repo for a film tool with no moving picture in it asks people to take the
film on faith, and they don't — they scroll. This turns finished episodes into
a looping GIF that GitHub renders inline at the top of the page.

    python scripts/demo_gif.py out/rust/episode-01/episode-01.mp4
    python scripts/demo_gif.py out/rust/episode-*/episode-*.mp4 --seconds 10

Why a GIF and not the .mp4: GitHub does not play video embedded in a README
from the repo itself. A GIF plays, loops, and needs no click — which is the
whole point at three seconds.

The defaults target a README: 560px wide, 12fps, under ~4MB. It prints the
size it produced and what to change if that is too big, because a 12MB GIF at
the top of a README is worse than no GIF.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Under this, GitHub serves it briskly on a phone. Over it, the picture
# arrives after the reader has already left.
COMFORTABLE_MB = 4.0


def ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def run(args: list[str]) -> None:
    completed = subprocess.run(args, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise SystemExit(f"ffmpeg failed:\n{completed.stderr[-1500:]}")


def leading_black(clip: Path) -> float:
    """Where the picture actually starts.

    Episodes open on a fade-in, so a GIF cut from 0 shows a black rectangle —
    and a README's whole job at three seconds is to not be a black rectangle.
    ffmpeg already knows where the black is; this just asks it.
    """

    completed = subprocess.run(
        [ffmpeg(), "-hide_banner", "-i", str(clip),
         "-vf", "blackdetect=d=0.05:pix_th=0.10", "-f", "null", "-"],
        capture_output=True, text=True, check=False,
    )
    for line in completed.stderr.splitlines():
        if "black_start:0 " in line or "black_start:0\n" in line + "\n":
            for part in line.split():
                if part.startswith("black_end:"):
                    # A hair past the fade so the first frame carries an image.
                    return round(float(part.split(":", 1)[1]) + 0.05, 3)
    return 0.0


def concat(clips: list[Path], workdir: Path) -> Path:
    """Several episodes into one strip, without re-encoding twice."""

    if len(clips) == 1:
        return clips[0]
    listing = workdir / "concat.txt"
    listing.write_text(
        "".join(f"file '{clip.resolve().as_posix()}'\n" for clip in clips), encoding="utf-8"
    )
    joined = workdir / "joined.mp4"
    run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(listing), "-c", "copy", "-y", str(joined)])
    return joined


def make_gif(
    source: Path, target: Path, *, width: int, fps: int, seconds: float, start: float
) -> Path:
    """Two passes: a palette built from this footage, then applied to it.

    One pass with the default 216-colour web palette turns a graded film into
    posterised mud, which is a worse advertisement than no picture at all.
    """

    with tempfile.TemporaryDirectory() as tmp:
        palette = Path(tmp) / "palette.png"
        chain = f"fps={fps},scale={width}:-1:flags=lanczos"
        clip = ["-ss", str(start), "-t", str(seconds)]
        run([ffmpeg(), "-hide_banner", "-loglevel", "error", *clip, "-i", str(source),
             "-vf", f"{chain},palettegen=stats_mode=diff", "-y", str(palette)])
        run([ffmpeg(), "-hide_banner", "-loglevel", "error", *clip, "-i", str(source),
             "-i", str(palette),
             "-lavfi", f"{chain}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=3",
             "-loop", "0", "-y", str(target)])
    return target


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="demo_gif", description="finished episodes → a README GIF"
    )
    parser.add_argument("clips", nargs="+", help="one or more episode .mp4 files")
    parser.add_argument("--out", default="docs/demo.gif", help="where to write it")
    parser.add_argument("--width", type=int, default=560, help="pixels (default 560)")
    parser.add_argument("--fps", type=int, default=12, help="frames per second (default 12)")
    parser.add_argument("--seconds", type=float, default=8.0, help="how much to keep")
    parser.add_argument(
        "--start", type=float, default=None,
        help="seconds to skip first (default: just past the opening fade)",
    )
    args = parser.parse_args(argv)

    clips = [Path(c) for c in args.clips]
    missing = [c for c in clips if not c.is_file()]
    if missing:
        print(f"not on disk: {', '.join(str(m) for m in missing)}", file=sys.stderr)
        # The output directory is a flag, so a machine usually has several and
        # the one in an example command is rarely the one you used. Naming the
        # episodes that do exist turns this into a copy-paste instead of a hunt.
        found = sorted(
            path for pattern in ("*/*/episode-*/episode-*.mp4", "*/episode-*/episode-*.mp4")
            for path in Path().glob(pattern)
        )
        if found:
            print("\nEpisodes that are on disk here:", file=sys.stderr)
            for path in found[:10]:
                print(f"  {path}", file=sys.stderr)
            print(
                f"\n  python scripts/demo_gif.py {' '.join(str(p) for p in found[:2])}",
                file=sys.stderr,
            )
        else:
            print(
                "\nNo finished episodes anywhere below this directory. `.mp4` is "
                "gitignored,\nso a fresh clone or a copied folder has the reports "
                "and none of the footage.",
                file=sys.stderr,
            )
        return 2

    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        source = concat(clips, Path(tmp))
        start = args.start if args.start is not None else leading_black(source)
        make_gif(
            source, target,
            width=args.width, fps=args.fps, seconds=args.seconds, start=start,
        )

    size_mb = target.stat().st_size / 1_000_000
    print(f"· {target}  {size_mb:.1f} MB  ({args.width}px · {args.fps}fps · {args.seconds:g}s)")
    if args.start is None and start:
        print(f"  Started at {start:g}s, past the opening fade — the first frame is a picture.")

    if size_mb > COMFORTABLE_MB:
        print(f"  Over {COMFORTABLE_MB:g} MB — on a phone this arrives after the reader leaves.")
        print("  Cheapest fixes first: --seconds 6, then --width 480, then --fps 10.")
    else:
        print("  Good size for the top of a README.")

    print()
    print("  Commit it — .mp4 is gitignored but docs/ GIFs are not:")
    print(f"    git add -f {target}")
    print("  Then put it under the first heading:")
    print(f"    ![one word in, a series out]({target.as_posix()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
