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
from .styles import StyleError, PRESETS, resolve as resolve_style
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
    parser.add_argument(
        "--audio", default="mix", choices=("mix", "keep", "replace"),
        help=(
            "what to do when a clip already has sound of its own: "
            "mix (duck it under the narration, default) | "
            "keep (the clip's audio wins, no narration) | "
            "replace (narration only, discard the clip's track)"
        ),
    )
    parser.add_argument(
        "--dialogue", default="auto", choices=("auto", "on", "off"),
        help=(
            "let the video model perform each line itself: "
            "auto (on when the vendor generates audio) | on | off"
        ),
    )
    parser.add_argument(
        "--chain", default="auto", choices=("auto", "off"),
        help=(
            "continue consecutive shots in one location from the previous "
            "shot's last frame: auto (on when the vendor accepts one) | off"
        ),
    )
    parser.add_argument(
        "--music", default=None,
        help="an audio file to lay under every episode, ducked under dialogue",
    )
    parser.add_argument(
        "--music-db", type=float, default=-20.0,
        help="how far under the picture the bed sits, in dB (default -20)",
    )
    parser.add_argument(
        "--style", default=None,
        help=(
            "a named look, or a path to your own style JSON: "
            + ", ".join(sorted(PRESETS))
            + ". Chosen once and locked like everything else; changing it on an "
            "existing bible restyles the series and requires --reset-references"
        ),
    )
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


def _progress_printer(stream=None):
    """One self-overwriting line per shot while a paid clip is generating.

    A clip takes minutes and the run printed nothing the whole time, which
    looks exactly like a hang — the first thing anyone does then is Ctrl-C a
    task they have already paid for. The line rewrites itself so a five-minute
    wait stays one line, and each finished shot leaves one line behind.
    """

    stream = stream or sys.stdout
    interactive = hasattr(stream, "isatty") and stream.isatty()

    def report(fields: dict) -> None:
        shot = fields.get("shot_id", "?")
        status = fields.get("status", "")
        elapsed = fields.get("elapsed", 0.0)
        line = f"  shot {shot} · {status} · {elapsed:.0f}s"
        if fields.get("spent_cny") is not None:
            line += f" · ¥{fields['spent_cny']:.2f} spent so far"
        if fields.get("attempt_failures"):
            line += f" · retry {fields['attempt_failures']}"

        if fields.get("done") and status == "saved":
            stream.write(("\r" + line.ljust(72) + "\n") if interactive else line + "\n")
        elif interactive:
            stream.write("\r" + line.ljust(72))
        elif status in ("submitting", "downloading"):
            # Not a terminal (a log file, CI): no rewriting, so only the
            # transitions are worth a line.
            stream.write(line + "\n")
        stream.flush()

    return report


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
    if argv and argv[0] == "styles":
        for name, preset in sorted(PRESETS.items()):
            print(f"{name:<12} {preset['label']}")
            print(f"{'':<12} {preset['render']}")
        return EXIT_OK

    args = _parser().parse_args(argv)
    if not 3 <= args.shots <= 8:
        print("--shots must be between 3 and 8", file=sys.stderr)
        return EXIT_ERROR

    try:
        if args.bible:
            bible = SeriesBible.load(args.bible)
            print(f"· bible reused: {bible.title} ({bible.path})")
            chosen = resolve_style(args.style)
            if chosen and chosen["name"] != bible.style_name:
                from .styles import apply_to

                was = bible.style_name
                apply_to(bible.data, chosen)
                bible.save(bible.path)
                print(f"· restyled {was} → {bible.style_name}; cast and locations untouched")
                if not args.reset_references:
                    print(
                        "  the reference stills are still in the old look — "
                        "add --reset-references or the drift pass will refuse to compare"
                    )
        else:
            bible, provenance = build_bible(
                args.word,
                episodes=args.episodes,
                shots=args.shots,
                language=args.language,
                allow_model=not args.no_model,
                style=args.style,
            )
            source = provenance["source"]
            if provenance.get("error"):
                print(f"· writing model unavailable ({provenance['error']}); using the local template")
            print(f"· bible written by {source}: {bible.title} · style {bible.style_name}")

        root = Path(args.out) / bible.slug
        root.mkdir(parents=True, exist_ok=True)
        bible_path = bible.path or bible.save(root / "bible.json")
        if not args.bible:
            bible_path = bible.save(root / "bible.json")

        vendor = build_vendor(args.vendor, on_progress=_progress_printer())
        voice_engine = build_voice(args.voice)
        print(f"· vendor {vendor.name} · voice {voice_engine.name} · audio {args.audio}")
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
                audio_mode=args.audio,
                dialogue=args.dialogue,
                chaining=args.chain,
                music=args.music,
                music_db=args.music_db,
            )
            paths = report.pop("_paths")
            scored = report.get("music")
            if scored and scored.get("error"):
                print(f"  note: no score — {scored['error']}")
            elif scored:
                print(
                    f"  score: {scored['track']} at {scored['level_db']:.0f} dB"
                    + (", ducked under dialogue" if scored["ducked_under_dialogue"]
                       else ", fixed level (no sidechain filter in this ffmpeg)")
                )
            for stale in report.get("stale_chains", []):
                print(
                    f"  note: shot {stale['shot_id']} continues shot "
                    f"{stale['continues']}, which was regenerated afterwards — "
                    "that cut may jump"
                )
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

    except (VendorError, VoiceError, PipelineError, StyleError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
