"""CLI: one word → a series.

    oneword rust --episodes 2                     # free, offline, runs now
    oneword rust --episodes 2 --vendor seedance   # real clips, costs money
    oneword drift out/rust                        # cross-episode drift only

`python -m oneword ...` does the same thing.

Exit codes
    0   every episode delivered, no drift found
    20  an episode still had blockers after its repair rounds
    21  pipeline error
    30  cross-episode drift found
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .audit import build_auditor
from .bible import SeriesBible, build_bible
from .drift import DriftError, audit_series
from .pipeline import PipelineError, run_episode
from .vendors import VendorError, build_vendor
from .voice import VoiceError, build_voice

EXIT_OK = 0
EXIT_BLOCKERS = 20
EXIT_ERROR = 21
EXIT_DRIFT = 30


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
    parser.add_argument(
        "--no-drift", action="store_true",
        help="skip the cross-episode drift pass at the end of a multi-episode run",
    )
    parser.add_argument(
        "--reset-references", action="store_true",
        help="forget the locked reference stills and establish them again from this run",
    )
    return parser


def _drift_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oneword drift",
        description="compare every appearance against its locked reference still",
    )
    parser.add_argument("series_dir", help="a series directory (the one holding bible.json)")
    parser.add_argument(
        "--reset-references", action="store_true",
        help="re-base every reference from the earliest episode present",
    )
    parser.add_argument("--no-model", action="store_true", help="pixel screen only, no model calls")
    return parser


def _print_drift(report: dict) -> None:
    summary = report["summary"]
    print(
        f"· drift: {summary['status']} — {summary['checked']} appearances, "
        f"{summary['drifted']} drifted, {summary['review']} to review, "
        f"{summary['not_checked']} not checked"
    )
    if not report["model_looked"]:
        print("  (no multimodal model looked; locations screened on pixels, characters unchecked)")
    for finding in report["findings"]:
        if finding["verdict"] in ("DRIFTED", "REVIEW"):
            distance = finding["distance"]
            detail = f" d={distance['composite']:.3f}" if distance else ""
            print(
                f"  {finding['verdict']:<10} ep{finding['episode']} shot {finding['shot_id']} "
                f"· {finding['name']}{detail} (reference: ep{finding['reference_episode']})"
            )


def drift_main(argv: list[str]) -> int:
    args = _drift_parser().parse_args(argv)
    try:
        report = audit_series(
            args.series_dir,
            reset_references=args.reset_references,
            use_model=not args.no_model,
        )
    except DriftError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    _print_drift(report)
    print(f"· report: {Path(args.series_dir) / 'series-drift-report.html'}")
    return EXIT_DRIFT if report["summary"]["drifted"] else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "drift":
        return drift_main(argv[1:])

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

        # The whole point of a series is that episode 9 still matches episode 1,
        # so this runs by default rather than waiting to be asked.
        if not args.no_drift and len(index) > 1:
            try:
                report = audit_series(
                    root,
                    reset_references=args.reset_references,
                    use_model=not args.no_model,
                )
                _print_drift(report)
                if report["summary"]["drifted"] and exit_code == EXIT_OK:
                    exit_code = EXIT_DRIFT
            except DriftError as exc:
                print(f"· drift pass skipped: {exc}")

        return exit_code

    except (VendorError, VoiceError, PipelineError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
