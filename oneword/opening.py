"""What the tool says and asks before it spends anything.

Two decisions belong at the very front of a run, before a single clip is paid
for, because both are expensive to discover afterwards.

## The look

A style is locked for the life of the series (see `oneword.styles`), so the
moment to pick one is the moment before the first shot — not after ten clips
arrive in a look nobody chose. If the run is interactive and no `--style` was
given, ask.

## Where the story came from

`local-template` was an honest label and a useless one. It reads like a small
technical detail, so a first-time user sails past it, pays for eight clips, and
only then discovers the film is a placeholder story that their word barely
touched: the cast, the locations and every line of dialogue are fixed strings
in this repo, and the seed word appears in a handful of action lines.

That is a fine way to see the machine work for free. It is a terrible surprise
to buy. So the notice says plainly what was ignored, and when a *paid* vendor is
about to shoot a template bible, the run stops and asks first.
"""

from __future__ import annotations

import sys
from typing import Any, Callable

from . import styles

TEMPLATE = "local-template"


def is_interactive(stream=None) -> bool:
    stream = stream or sys.stdout
    try:
        return bool(stream.isatty() and sys.stdin.isatty())
    except (AttributeError, ValueError):
        return False


# ---- the look ------------------------------------------------------


def style_menu() -> list[str]:
    names = styles.names()
    lines = ["", "Pick a look. It is locked for the whole series.", ""]
    for index, name in enumerate(names, start=1):
        preset = styles.PRESETS[name]
        lines.append(f"  {index}) {name:<12} {preset['label']}")
        lines.append(f"     {'':<12} {preset['render']}")
    lines.append("")
    lines.append("  0) let the writing model choose (whatever it imagines, unlocked)")
    lines.append("")
    return lines


def choose_style(
    supplied: str | None,
    *,
    stream=None,
    ask: bool = True,
    input_fn: Callable[[str], str] | None = None,
) -> str | None:
    """Return a style name (or None for 'model decides').

    `--style` given wins outright; there is nothing to ask. Otherwise, ask when
    we can, and when we cannot, say what is about to happen and how to choose.
    """

    stream = stream or sys.stdout
    if supplied:
        return supplied
    if not ask:
        stream.write(
            "· no --style given: the look is whatever the story model imagines, and "
            "is not locked.\n"
            f"  Pick one to lock it — {', '.join(styles.names())} "
            "(`oneword styles` describes them).\n"
        )
        stream.flush()
        return None

    input_fn = input_fn or input
    for line in style_menu():
        stream.write(line + "\n")
    stream.flush()
    names = styles.names()
    while True:
        try:
            answer = input_fn("  style [1]: ").strip()
        except EOFError:
            return None
        if answer == "":
            answer = "1"
        if answer == "0":
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(names):
            return names[int(answer) - 1]
        if answer.lower() in names:
            return answer.lower()
        stream.write(f"  not one of the options — 0-{len(names)}, or a name\n")
        stream.flush()


# ---- where the story came from -------------------------------------


def provenance_lines(provenance: dict[str, Any], word: str) -> list[str]:
    """What actually wrote this bible, in words that cost nothing to misread."""

    if provenance.get("source") != TEMPLATE:
        return []
    lines = []
    if provenance.get("error"):
        lines.append(
            f"· the writing model was configured but failed ({provenance['error']})"
        )
    lines += [
        "· no story was written for your word — this is the built-in placeholder.",
        f'  "{word}" appears only in a few action lines. The cast, the locations,',
        "  the beats and every line of dialogue are fixed strings shipped with this",
        "  tool, and they are the same for every word anyone types.",
        f'  Set LLM_API_KEY / LLM_MODEL for a series actually written from "{word}".',
    ]
    return lines


def paid_template_warning(word: str, vendor_name: str, unit_cost: float | None, clips: int) -> list[str]:
    """The one that has to stop somebody."""

    if unit_cost:
        money = f"about ¥{unit_cost * clips:.2f} ({clips} clips at ¥{unit_cost:.2f})"
    else:
        money = f"{clips} paid clips"

    return [
        "",
        "  ────────────────────────────────────────────────────────────────",
        f"  You are about to pay {vendor_name} for the placeholder story.",
        "",
        f'  Nothing here was written from "{word}". You would be buying',
        "  the demo — the same two characters, the same two rooms and the",
        "  same dialogue everyone else gets, rendered in your chosen look.",
        "",
        f"  Cost if you continue: {money}.",
        "",
        "  Free options:",
        "    · --vendor animatic          see the whole pipeline for ¥0",
        '    · set LLM_API_KEY/LLM_MODEL  then pay for a real story about "%s"' % word,
        "  ────────────────────────────────────────────────────────────────",
        "",
    ]


def confirm(
    question: str,
    *,
    stream=None,
    ask: bool = True,
    input_fn: Callable[[str], str] | None = None,
) -> bool:
    """Yes only when somebody typed yes.

    Non-interactive runs are not blocked — a scheduled job has nobody to answer
    — but they have already been told, above, exactly what they are buying.
    """

    stream = stream or sys.stdout
    if not ask:
        stream.write("  (not a terminal — continuing without asking)\n")
        stream.flush()
        return True
    input_fn = input_fn or input
    try:
        answer = input_fn(question).strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")
