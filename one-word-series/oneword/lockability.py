"""Not every fact you can write down is a fact a generator can hold.

## Where this came from

The first drift pass with a vision model on real Seedance footage produced
seven DRIFTED rows. Reading what the model actually saw, one pattern was
unmissable:

    locked fact                       flagged   what the model saw instead
    SER-03 clock reads 4:10                 6   10:52, 8:25, 8:25, 7:35, 7:35, 10:10
    scar through LEFT eyebrow               4   right cheek, forehead, forehead, right eyebrow
    SER-01 torn left cuff                   5   rolled, rolled, rolled, rolled, not torn
    SER-02 red collar pin                   4   absent, absent, absent, dark button
    SER-05 one warm practical light         3   window+ambient, cool fluorescent, window+lamp
    SER-04 green handrails                  0   — never flagged —

`SER-04` is the control. A large, coloured, architectural feature held in every
single shot. Everything that failed was small, lateral, readable, or a count —
and `SER-03` is worse than that: **no video model renders a clock at a time you
specify**, so that rule was guaranteed to fail in every shot of that room
forever. It shipped in this repo's own template.

That is the real cost. A report where six of seven flags are the bible's own
impossible demands is a report nobody reads twice, and the one real finding in
it — a jacket that became a shirt — is buried under noise the tool generated
itself.

## What this module does

It does not decide whether a shot drifted. It decides whether a *written fact*
was ever enforceable, so the noise can be separated from the signal before a
human reads the report — and, better, before the series is shot at all.

The categories are empirical, not theoretical. Each one earned its place in the
table above.
"""

from __future__ import annotations

import re
from typing import Any, NamedTuple


class Issue(NamedTuple):
    kind: str
    why: str
    instead: str


# Ordered most-certain first. The patterns are deliberately narrow: a false
# warning about a fact that would have held is its own kind of noise.
RULES: list[tuple[str, re.Pattern[str], str, str]] = [
    (
        "readable-value",
        re.compile(
            r"\b(clock|watch|timer|display|readout|meter|gauge)\b[^.]{0,40}"
            r"\b(reads?|shows?|says?|displays?|set to|stopped at)\b|"
            r"\b(reads?|shows?|displays?)\b[^.]{0,20}\b\d{1,2}[:：]\d{2}\b",
            re.I,
        ),
        "no generator renders a dial or a screen at a value you specify",
        "say the clock is stopped and unlit, not what time it shows",
    ),
    (
        "readable-text",
        re.compile(
            r"\b(sign|label|badge|plate|screen|poster|banner|tag)\b[^.]{0,40}"
            r"\b(reads?|says?|lettered|spells?|marked)\b|"
            r"\b(the (?:word|number|text))\b\s+[\"'“]",
            re.I,
        ),
        "specified lettering comes back misspelled or invented",
        "describe the sign's shape and colour, not its words",
    ),
    (
        "lateral",
        re.compile(
            # Up to two words may sit between the side and the feature —
            # "left jacket cuff", "right trouser pocket" — which is exactly
            # the phrasing a bible reaches for.
            r"\b(left|right)\b[- ]?(?:hand|side)?\s*(?:\w+\s+){0,2}"
            r"\b(eye|eyebrow|cheek|ear|temple|arm|hand|wrist|shoulder|cuff|sleeve|"
            r"pocket|leg|knee|foot|brow|jaw|lapel|collar|glove|boot|forearm)\b",
            re.I,
        ),
        "models do not hold which side of a body a detail sits on",
        "keep the feature, drop the side — 'a scar splitting one eyebrow'",
    ),
    (
        "tiny-accessory",
        re.compile(
            # Bare "ring" is excluded on purpose: a key ring on a belt is a
            # fist-sized object that holds fine, and flagging it would make the
            # warning itself the noise.
            r"\b(collar pin|lapel pin|tie pin|brooch|badge|earring|ear stud|"
            r"signet ring|wedding ring|cufflink|name ?tag|wristband)\b",
            re.I,
        ),
        "an object a few pixels across is below what the model reliably places",
        "move the detail up a size — a scarf, a bag, a jacket colour",
    ),
    (
        "count",
        re.compile(
            r"\b(only|exactly|no more than|never more than|at most)\s+"
            r"(one|two|three|a single|\d+)\b",
            re.I,
        ),
        "a generator does not count; it composes what looks right",
        "name what must be present, not how many things may be",
    ),
]


def classify(text: str) -> list[Issue]:
    """Every reason this written fact is unlikely to ever hold."""

    found: list[Issue] = []
    for kind, pattern, why, instead in RULES:
        if pattern.search(text or ""):
            found.append(Issue(kind, why, instead))
    return found


def enforceable(text: str) -> bool:
    return not classify(text)


def review(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Every locked string in a bible that a generator is unlikely to hold."""

    problems: list[dict[str, Any]] = []

    def look(where: str, text: str) -> None:
        issues = classify(text)
        if issues:
            problems.append({"where": where, "text": text, "issues": issues})

    for rule in data.get("continuity_rules") or []:
        look(str(rule.get("id", "rule")), str(rule.get("text", "")))
    for cid, character in (data.get("characters") or {}).items():
        look(
            f"{character.get('name', cid)} ({cid})",
            str(character.get("locked_appearance", "")),
        )
    for lid, location in (data.get("locations") or {}).items():
        look(
            f"{location.get('name', lid)} ({lid})",
            str(location.get("locked_description", "")),
        )
    return problems


def lines(problems: list[dict[str, Any]]) -> list[str]:
    """What to print. Grouped by kind, because the lesson is the kind."""

    if not problems:
        return []
    out = [
        f"· {len(problems)} locked fact(s) a generator is unlikely to ever hold, "
        "so they will read as drift in every episode:"
    ]
    for problem in problems:
        kinds = ", ".join(sorted({issue.kind for issue in problem["issues"]}))
        out.append(f"    {problem['where']} [{kinds}]")
        out.append(f"      {problem['text'][:110]}")
        out.append(f"      → {problem['issues'][0].instead}")
    out.append("  These are not caught by spending more; they are caught by rewording.")
    return out
