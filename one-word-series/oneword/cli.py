"""CLI: one word → a series.

    oneword rust --episodes 2                     # free, offline, runs now
    oneword rust --episodes 2 --vendor seedance   # real clips, costs money
    oneword drift out/rust                        # cross-episode drift only

`python -m oneword ...` does the same thing.

Exit codes
    0   every episode delivered, no drift found
    10  you were warned what you were about to buy and said no
    20  an episode still had blockers after its repair rounds
    21  pipeline error
    30  cross-episode drift found
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import lockability, opening
from .audit import build_auditor
from .bible import SeriesBible, build_bible
from .cast import CastError, CastPortraits, parse_supplied
from .drift import DriftError, audit_series
from .pipeline import PipelineError, run_episode
from .styles import StyleError, PRESETS, resolve as resolve_style
from .vendors import VendorError, build_vendor
from .voice import VoiceError, build_voice

EXIT_OK = 0
EXIT_STOPPED = 10
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
            "existing bible restyles the series and requires --reset-references. "
            "Left out, an interactive run asks before it shoots anything"
        ),
    )
    parser.add_argument(
        "--cast-image", action="append", default=None, metavar="ID=PATH",
        help=(
            "a portrait for one character, e.g. --cast-image C1=face.jpg. "
            "Repeatable. Sent with every shot that character appears in, because "
            "a description names a type and only a picture fixes a face. Without "
            "one, the first shot a character is alone in becomes their portrait"
        ),
    )
    parser.add_argument(
        "--no-cast-images", action="store_true",
        help="never send character portraits — every shot casts its own face",
    )
    parser.add_argument(
        "--reset-cast", action="store_true",
        help="forget the locked character portraits and adopt them again from this run",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="ask nothing: take the defaults and accept any spending warning",
    )
    parser.add_argument("--language", default="en", help="en | zh")
    parser.add_argument("--bible", default=None, help="reuse an existing bible.json")
    parser.add_argument(
        "--fresh", action="store_true",
        help=(
            "ignore clips already on disk and generate every shot again — "
            "by default a run reuses anything it already paid for, when the "
            "prompt and vendor match exactly"
        ),
    )
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


def _writing_printer(stream=None):
    """The bible is one big JSON object and a slow model is slow to write it.

    Before this, the first thing a run printed was the finished bible — so
    between pressing enter and that line there was a silent gap that, with the
    retries, can run to the better part of ten minutes. Silence of that length
    is indistinguishable from a hang, and killing a run that was working is the
    mistake the video vendor's progress line already exists to prevent.
    """

    stream = stream or sys.stdout
    interactive = hasattr(stream, "isatty") and stream.isatty()

    def report(fields: dict) -> None:
        status = fields.get("status", "")
        elapsed = fields.get("elapsed", 0.0)
        if status == "waiting":
            line = (
                f"· writing the series with {fields.get('model', '?')} — "
                f"this takes a minute or two"
            )
            if fields.get("attempt", 1) > 1:
                line += f" (attempt {fields['attempt']} of {fields.get('of', '?')})"
            stream.write(line + "\n")
        elif status == "retrying":
            stream.write(f"  retrying after {elapsed:.0f}s — {fields.get('reason', '')}\n")
        elif status == "failed":
            stream.write(f"  gave up after {elapsed:.0f}s\n")
        elif status == "answered" and interactive:
            stream.write(f"  model answered in {elapsed:.0f}s\n")
        stream.flush()

    return report


def _load_cast(root: Path, bible, args, supplied: dict[str, Path], vendor):
    """The frozen portraits for this series, or None if they are switched off.

    A portrait is what makes shot 9 the same person as shot 1, so it is frozen
    the moment it exists — and it is frozen *in a look*. A portrait shot in
    `noir` is not this character in `anime`; using it after a restyle would
    fight the style rather than hold the face, so those are set aside and
    re-adopted rather than silently reused.
    """

    if args.no_cast_images:
        print("· character portraits off — every shot casts its own face")
        return None
    if not getattr(vendor, "accepts_reference_images", False):
        if supplied:
            print(
                f"· {vendor.name} does not take reference images — the portraits you "
                "supplied are not being used"
            )
        return None

    portraits = CastPortraits(root)
    if args.reset_cast:
        portraits.clear()
        print("· forgot the locked character portraits; this run adopts them again")

    for cid, path in supplied.items():
        try:
            name = bible.character(cid).get("name", cid)
        except KeyError:
            known = ", ".join(sorted(bible.data.get("characters", {})))
            raise CastError(f"no character {cid!r} in this bible. Known ids: {known}")
        portraits.put(
            cid, path, source="supplied", style=bible.style_name, force=True
        )
        print(f"· portrait supplied for {name} ({cid}): {path.name}")

    stale = {
        style for style in portraits.styles_present()
        if style not in ("", "unknown", bible.style_name)
    }
    if stale and not args.reset_cast:
        print(
            f"· the locked portraits were shot in {', '.join(sorted(stale))}, not "
            f"{bible.style_name} — setting them aside for this run."
        )
        print("  Add --reset-cast to adopt new ones in this look, or --style to go back.")
        return None

    if not portraits.data["portraits"]:
        print(
            "· no character portraits yet — the first shot each character is alone "
            "in becomes theirs, and every later shot is sent that face."
        )
    return portraits


def _print_drift(report: dict) -> None:
    summary = report["summary"]
    print(
        f"· drift: {summary['status']} — {summary['checked']} appearances, "
        f"{summary['drifted']} drifted, {summary['review']} to review, "
        f"{summary['not_checked']} not checked"
    )
    # "Nobody was asked" and "everybody was asked and nobody answered" are
    # different problems with different fixes, and printing one line for both
    # is how you set a key, see no change, and conclude the key is fine.
    errors = report.get("model_errors") or []
    if errors and report["model_looked"]:
        # Some calls answered and some did not. Saying "every call failed" here
        # is a false claim sitting directly above verdicts a model did reach —
        # the exact kind of contradiction this tool exists to refuse.
        print(f"  {len(errors)} call(s) to the model failed; the rest answered:")
        for reason in errors[:3]:
            print(f"    {reason}")
        print(
            f"  The {summary['not_checked']} appearance(s) below marked NOT CHECKED are the "
            "ones whose call failed.\n"
            "  Everything else carries a verdict a model actually reached."
        )
    elif errors:
        print(f"  a model WAS configured and every call to it failed ({len(errors)} distinct):")
        for reason in errors[:3]:
            print(f"    {reason}")
        print(
            "  Nothing below was examined by a model. Check that LLM_MODEL names a "
            "vision-capable model —\n"
            "  a text-only one fails exactly like this."
        )
    elif not report["model_looked"]:
        print("  (no multimodal model looked; locations screened on pixels, characters unchecked)")
        if summary["not_checked"]:
            print(
                f"  {summary['not_checked']} character appearance(s) unchecked — a whole-frame "
                "fingerprint cannot honestly judge a face."
            )
            # Naming which half of the pair is missing turns "set LLM_API_KEY /
            # LLM_MODEL" from advice into a diagnosis.
            absent = [n for n in ("LLM_API_KEY", "LLM_MODEL") if not os.getenv(n)]
            storyboard = summary.get("storyboard_episodes") or []
            if absent:
                print(
                    f"  {' and '.join(absent)} not set in this shell, which is why nothing "
                    "was asked.\n"
                    "  In PowerShell these last only for the window you set them in."
                )
            elif storyboard:
                # Not a misconfiguration: grading storyboard cards with a vision
                # model would buy a visual verdict about pixels no model drew.
                print(
                    f"  A model is configured, but episode(s) "
                    f"{', '.join(str(e) for e in storyboard)} were shot with the offline "
                    "storyboard vendor,\n"
                    "  so no model was asked about them on purpose."
                )
            else:
                print(
                    "  LLM_API_KEY and LLM_MODEL are both set and the footage is real, so "
                    "this is a bug — please report it."
                )
    if report.get("missing_clips"):
        print(
            f"  {len(report['missing_clips'])} shot(s) skipped — their clip is not on disk: "
            + ", ".join(report["missing_clips"][:3])
        )
    for band in summary.get("unmeasured_bands", []):
        print(
            f"  note: {band} shots were judged with a borrowed threshold — measure it with "
            "`python scripts/calibrate_drift.py --series <dir>`"
        )
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

    # `oneword styles` lists the presets, but only when `styles` comes first.
    # Put any flag before it — `oneword --yes styles` — and argparse takes it
    # as the seed word and shoots a film about the word "styles". Both are
    # legitimate readings, so this says which one it picked rather than
    # guessing silently or refusing a word somebody might actually want.
    if args.word in ("styles", "drift"):
        print(
            f"· shooting a series from the word \"{args.word}\". If you wanted the "
            f"{args.word} command, it has to come first: oneword {args.word} …"
        )

    if not 3 <= args.shots <= 8:
        print("--shots must be between 3 and 8", file=sys.stderr)
        return EXIT_ERROR

    ask = opening.is_interactive() and not args.yes

    try:
        supplied_faces = parse_supplied(args.cast_image)
        # Before anything is written or bought: the look is locked for the life
        # of the series, so it is chosen here or not at all.
        if not args.bible:
            args.style = opening.choose_style(args.style, ask=ask)

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
                on_progress=_writing_printer(),
            )
            print(f"· bible: {bible.title} · style {bible.style_name}")
            for line in opening.provenance_lines(provenance, bible.word):
                print(line)

        # Before anything is shot: a locked fact no generator can hold is a
        # guaranteed drift finding in every episode, and enough of them bury
        # the real ones. Rewording costs nothing; discovering it after eight
        # paid clips costs eight paid clips.
        for line in lockability.lines(lockability.review(bible.data)):
            print(line)

        root = Path(args.out) / bible.slug
        root.mkdir(parents=True, exist_ok=True)
        bible_path = bible.path or bible.save(root / "bible.json")
        if not args.bible:
            bible_path = bible.save(root / "bible.json")

        vendor = build_vendor(args.vendor, on_progress=_progress_printer())
        voice_engine = build_voice(args.voice)
        print(f"· vendor {vendor.name} · voice {voice_engine.name} · audio {args.audio}")
        # Which model, at what size, for how much — before the first submit.
        # "seedance-ark" alone does not distinguish 1.0 lite at 720p from 2.0
        # mini at 480p, and those are different pictures at different prices.
        config = getattr(vendor, "config", None)
        if config is not None:
            print(
                f"  {config.model} · {config.resolution} · {int(config.duration)}s"
                f" · ¥{vendor.unit_cost:.2f} per clip"
                f" · budget ¥{config.budget_cny:.2f}"
            )

        # ONEWORD_PRICE outranks the whole price table, for every model, every
        # resolution and every duration. That is right for a one-off run and
        # wrong the moment it survives in a shell: a number typed for a 720p/5s
        # job silently prices a 1080p/10s one, and the budget cap is then
        # computed from a figure nobody rechecked.
        override = os.getenv("ONEWORD_PRICE")
        if override and getattr(vendor, "generative", False):
            print(
                f"  ONEWORD_PRICE={override} is set, so every clip is being costed at "
                f"¥{override} regardless of model, resolution or duration."
            )
            print("  Unset it to use the price table: Remove-Item Env:ONEWORD_PRICE")
        if voice_engine.name == "silent" and args.voice == "auto":
            print(
                "  (no TTS engine on this machine — shipping picture and subtitles "
                "without speech. Install espeak-ng, or set VOLC_TTS_APPID/VOLC_TTS_TOKEN.)"
            )

        # The last moment before money. A placeholder story rendered by a paid
        # vendor is the one combination nobody would have chosen on purpose.
        template = bible.data.get("provenance", {}).get("source") == opening.TEMPLATE
        if template and getattr(vendor, "generative", False):
            for line in opening.paid_template_warning(
                bible.word,
                vendor.name,
                getattr(vendor, "unit_cost", None),
                args.episodes * args.shots,
            ):
                print(line)
            if not opening.confirm("  type yes to shoot the placeholder anyway: ", ask=ask):
                print("· stopped. Nothing was generated and nothing was charged.")
                return EXIT_STOPPED

        portraits = _load_cast(root, bible, args, supplied_faces, vendor)

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
                resume=not args.fresh,
                portraits=portraits,
                use_portraits=not args.no_cast_images,
            )
            paths = report.pop("_paths")
            if report["summary"].get("reused_shot_ids"):
                saved = report["summary"]["reused_saving_cny"]
                print(
                    f"  reused {len(report['summary']['reused_shot_ids'])} shot(s) "
                    f"already on disk" + (f", saving ¥{saved:.2f}" if saved else "")
                )
            scored = report.get("music")
            if scored and scored.get("error"):
                print(f"  note: no score — {scored['error']}")
            elif scored:
                print(
                    f"  score: {scored['track']} at {scored['level_db']:.0f} dB"
                    + (", ducked under dialogue" if scored["ducked_under_dialogue"]
                       else ", fixed level (no sidechain filter in this ffmpeg)")
                )
            for dropped in report.get("dropped_chains", []):
                print(
                    f"  note: shot {dropped['shot_id']} was shot from text — "
                    "the platform refused its first frame, so that cut may jump"
                )
            for failed in report.get("audit_failures", []):
                print(
                    f"  note: shot {failed['shot_id']} was NOT audited — {failed['reason']}\n"
                    "        the clip is in the film; nobody checked it"
                )
            for dropped in report.get("dropped_portraits", []):
                # Two different situations wear the same field. One is a
                # refusal, which may cost the face; the other is this tool
                # choosing the chain over the portraits, which does not.
                if str(dropped.get("reason", "")).startswith("superseded"):
                    print(
                        f"  shot {dropped['shot_id']} used the previous shot's frame "
                        "instead of the portraits — the frame already carries the face"
                    )
                else:
                    print(
                        f"  note: shot {dropped['shot_id']} was shot without the cast "
                        "portraits — the platform refused them, so that face may differ"
                    )
            adopted = [
                cid for cid, entry in (report.get("cast_portraits") or {}).items()
                if entry.get("from_shot", "").startswith(f"ep{number}-")
            ]
            if adopted:
                names = ", ".join(bible.character(cid).get("name", cid) for cid in adopted)
                print(f"  locked a face for {names} — every later shot is sent it")
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

    except (VendorError, VoiceError, PipelineError, StyleError, CastError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
