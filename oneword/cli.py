"""CLI: one word → a series.

    oneword rust --episodes 2                     # free, offline, runs now
    oneword rust --episodes 2 --vendor seedance   # real clips, costs money

`python -m oneword ...` does the same thing.

Exit codes
    0   every episode delivered
    20  an episode still had blockers after its repair rounds
    21  pipeline error
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .audit import build_auditor
from .bible import SeriesBible, build_bible
from .pipeline import PipelineError, run_episode
from .vendors import VendorError, build_vendor
from .voice import VoiceError, build_voice

EXIT_OK = 0
EXIT_BLOCKERS = 20
EXIT_ERROR = 21


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oneword", description="one word → a film series")
    parser.add_argument("word", help="the seed word")
    parser.add_argument("--episodes", type=int, default=1, help="how many episodes to shoot")
    parser.add_argument("--shots", type=int, default=5, help="shots per episode (3-8)")
    parser.add_argument("--seconds", type=int, default=5, help="seconds per shot")
    parser.add_argument("--out", default="out", help="output directory")
    parser.add_argument("--vendor", default="animatic", help="animatic | seedance")
    parser.add_argument(
        "--voice",
        default="auto",
        help="auto (best available, falls back to silent) | espeak | volc | silent",
    )
    parser.add_argument("--auditor", default="auto", help="auto | triage")
    parser.add_argument("--language", default="en", help="en | zh")
    parser.add_argument("--bible", default=None, help="reuse an existing bible.json")
    parser.add_argument("--no-model", action="store_true", help="skip the writing model entirely")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 3 <= args.shots <= 8:
        print("--shots must be between 3 and 8", file=sys.stderr)
        return EXIT_ERROR

    try:
        if args.bible:
            bible = SeriesBible.load(args.bible)
            print(f"· bible reused: {bible.title} ({bible.path})")
        else:
            bible, provenance = build_bible(
                args.word,
                episodes=args.episodes,
                shots=args.shots,
                language=args.language,
                allow_model=not args.no_model,
            )
            source = provenance["source"]
            if provenance.get("error"):
                print(f"· writing model unavailable ({provenance['error']}); using the local template")
            print(f"· bible written by {source}: {bible.title}")

        root = Path(args.out) / bible.slug
        root.mkdir(parents=True, exist_ok=True)
        bible_path = bible.path or bible.save(root / "bible.json")
        if not args.bible:
            bible_path = bible.save(root / "bible.json")

        vendor = build_vendor(args.vendor)
        voice_engine = build_voice(args.voice)
        print(f"· vendor {vendor.name} · voice {voice_engine.name}")
        if voice_engine.name == "silent" and args.voice == "auto":
            print(
                "  (no TTS engine on this machine — shipping picture and subtitles "
                "without speech. Install espeak-ng, or set VOLC_TTS_APPID/VOLC_TTS_TOKEN.)"
            )

        exit_code = EXIT_OK
        index: list[dict] = []
        for number in bible.episode_numbers[: args.episodes]:
            episode_root = root / f"episode-{number:02d}"
            auditor = build_auditor(args.auditor, bible, episode_root / "frames")
            print(f"· shooting episode {number} …")
            report = run_episode(
                bible,
                number,
                episode_root,
                vendor=vendor,
                auditor=auditor,
                voice_engine=voice_engine,
                clip_seconds=args.seconds,
            )
            paths = report.pop("_paths")
            status = report["summary"]["status"]
            if status != "DELIVERED":
                exit_code = EXIT_BLOCKERS
            print(
                f"  {status} · {report['summary']['runtime_sec']}s · "
                f"{report['summary']['total_generation_count']} generations · {paths['video']}"
            )
            index.append(
                {
                    "episode": number,
                    "title": report["episode_title"],
                    "status": status,
                    "video": str(paths["video"]),
                    "report": str(paths["html"]),
                }
            )

        (root / "series-index.json").write_text(
            json.dumps(
                {
                    "seed_word": bible.word,
                    "series_title": bible.title,
                    "bible": str(bible_path),
                    "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "episodes": index,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"· series index: {root / 'series-index.json'}")
        return exit_code

    except (VendorError, VoiceError, PipelineError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
