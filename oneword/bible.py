"""One word → a series bible that every later episode is shot against.

The bible is written ONCE.  It holds the facts that must not drift:

* `world`          — premise, tone, era, palette
* `characters`     — each with a `locked_appearance` string and a voice
* `locations`      — each with a `locked_description` string
* `visual_grammar` — aspect ratio, lens, lighting, negative prompt
* `continuity_rules` — falsifiable-from-one-frame rules, same shape as canon
* `episodes`       — the plan: logline + beats per episode

Every shot prompt in every episode is assembled locally from these strings.
The model never re-describes a character; it only decides what happens.
That is the whole reason a *series* stays consistent while a one-prompt
pipeline drifts by episode 3.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import llm

BIBLE_VERSION = "series-bible-1"

BEATS = ("establish", "escalate", "turn", "cost", "resolve")
BEAT_SECONDS = {"establish": 5, "escalate": 5, "turn": 5, "cost": 5, "resolve": 5}
IDENTITY_CRITICAL_BEATS = {"turn", "cost"}

SYSTEM = (
    "You are a series showrunner and a production designer. "
    "You answer with one JSON object and nothing else. "
    "Descriptions must be concrete and visual: things a camera can photograph. "
    "Never use abstract adjectives alone."
)

PROMPT_TEMPLATE = """Seed word: 「{word}」

Invent an original short-form film SERIES built from that single word.
{language_note}

Hard requirements:
- Exactly {cast} named characters, no more. Each gets a `locked_appearance`:
  one sentence, 20-40 words, listing only permanent visible traits
  (build, age range, hair, face, clothing, one signature prop). No mood,
  no action, no lighting — those change shot to shot, these never do.
- Exactly {places} locations. Each gets a `locked_description`:
  one sentence naming permanent architecture, materials and fixed objects.
- {episodes} episodes. Each episode is a self-contained {shots}-shot story
  that also advances one series-long question. Give each a `beats` array of
  exactly {shots} items, in this order: {beat_order}.
  Each beat item: {{"action": "...", "location_id": "L1", "character_ids": ["C1"],
  "camera": "...", "line": "..."}}
  `line` is ONE spoken or narrated sentence, under 18 words, or "" for silence.
  `camera` is a shot size plus movement, e.g. "medium close-up, slow push in".
- `continuity_rules`: 4-6 rules that a single still frame could falsify,
  e.g. "the left sleeve of C1 is always torn at the cuff". Give each an id
  SER-01.. and a severity of "regenerate" or "local_fix".

Return exactly this JSON shape:
{{"title": "...", "logline": "...",
 "world": {{"premise": "...", "tone": "...", "era": "...", "palette": "..."}},
 "visual_grammar": {{"lens": "...", "lighting": "...", "negative": "..."}},
 "characters": {{"C1": {{"name": "...", "role": "...", "locked_appearance": "...",
                       "voice": {{"gender": "male|female|neutral", "age": "...", "quality": "..."}}}}}},
 "locations": {{"L1": {{"name": "...", "locked_description": "..."}}}},
 "continuity_rules": [{{"id": "SER-01", "text": "...", "severity": "regenerate"}}],
 "episodes": [{{"no": 1, "title": "...", "logline": "...", "beats": [...]}}]}}
"""


def slugify(word: str) -> str:
    norm = unicodedata.normalize("NFKD", (word or "").strip())
    ascii_only = re.sub(r"[^a-zA-Z0-9]+", "-", norm).strip("-").lower()
    if ascii_only:
        return ascii_only[:40]
    # CJK and other non-latin seeds keep a stable, filesystem-safe hash name.
    return "seed-" + format(abs(hash(word)) % (16**8), "08x")


# ──────────────────────────────────────────────────────────────────────
# Deterministic fallback — runs with no API key at all
# ──────────────────────────────────────────────────────────────────────

_FALLBACK_ACTIONS = (
    "{a} stands still while the {w} spreads across the far wall",
    "{a} forces the door open and finds the {w} already inside",
    "{b} says the thing {a} has been refusing to hear",
    "{a} destroys the one object that was keeping the {w} out",
    "{a} walks out and leaves the {w} behind, changed",
)
_FALLBACK_LINES = (
    "It started at the edges. Nobody looked at the edges.",
    "You said this room was sealed.",
    "I knew. I have known since the first night.",
    "Then it was never the building. It was us.",
    "Leave it. Whatever it becomes now, it becomes without me.",
)
_FALLBACK_CAMERA = (
    "wide establishing shot, locked off",
    "medium shot, slow handheld drift",
    "medium close-up, slow push in",
    "close-up, static",
    "wide shot, slow pull back",
)


def _fallback(word: str, *, episodes: int, shots: int) -> dict[str, Any]:
    w = (word or "seed").strip()
    beats = list(BEATS[:shots]) + ["resolve"] * max(0, shots - len(BEATS))
    return {
        "title": f"《{w}》",
        "logline": f"A two-hander about what {w} does to the people who refuse to name it.",
        "world": {
            "premise": f"A sealed residential block where {w} has begun to appear on surfaces.",
            "tone": "restrained, cold, documentary",
            "era": "near future",
            "palette": "desaturated teal and rust, single warm practical light",
        },
        "visual_grammar": {
            "lens": "35mm anamorphic, shallow depth of field",
            "lighting": "hard key from one practical source, deep shadow",
            "negative": "text, watermark, extra fingers, warped architecture, style change",
        },
        "characters": {
            "C1": {
                "name": "Wen",
                "role": "building caretaker",
                "locked_appearance": (
                    "Late thirties, wiry build, black hair cropped short and greying at the temple, "
                    "deep vertical scar through the left eyebrow, olive workwear jacket with a torn left cuff, "
                    "always carrying a brass key ring on the belt."
                ),
                "voice": {"gender": "male", "age": "adult", "quality": "low, unhurried"},
            },
            "C2": {
                "name": "Lin",
                "role": "the tenant who stayed",
                "locked_appearance": (
                    "Early twenties, slight build, long dark hair tied at the nape, round wire glasses, "
                    "oversized grey knit sweater with a stretched right sleeve, "
                    "a red enamel pin on the collar."
                ),
                "voice": {"gender": "female", "age": "young adult", "quality": "quiet, precise"},
            },
        },
        "locations": {
            "L1": {
                "name": "Stairwell C",
                "locked_description": (
                    "A concrete stairwell with painted green handrails, one flickering tube light, "
                    "numbered landing plates, and a steel fire door at the bottom."
                ),
            },
            "L2": {
                "name": "Unit 704",
                "locked_description": (
                    "A one-room flat with a west-facing window, a folding table under it, "
                    "a wall of taped paper notes, and an unplugged wall clock stopped at 4:10."
                ),
            },
        },
        "continuity_rules": [
            {"id": "SER-01", "text": "C1's left jacket cuff is torn in every shot.", "severity": "regenerate"},
            {"id": "SER-02", "text": "C2 wears round wire glasses and the red collar pin in every shot.", "severity": "regenerate"},
            {"id": "SER-03", "text": "The wall clock in Unit 704 always reads 4:10.", "severity": "local_fix"},
            {"id": "SER-04", "text": "Stairwell handrails are green; they never change colour.", "severity": "regenerate"},
            {"id": "SER-05", "text": "Only one warm practical light source is visible per shot.", "severity": "local_fix"},
        ],
        "episodes": [
            {
                "no": index + 1,
                "title": f"Episode {index + 1}",
                "logline": f"{w}, one floor lower than yesterday.",
                "beats": [
                    {
                        "action": _FALLBACK_ACTIONS[i % len(_FALLBACK_ACTIONS)].format(
                            a="Wen", b="Lin", w=w
                        ),
                        "location_id": "L1" if i % 2 == 0 else "L2",
                        "character_ids": ["C1"] if i % 3 != 2 else ["C1", "C2"],
                        "camera": _FALLBACK_CAMERA[i % len(_FALLBACK_CAMERA)],
                        "line": _FALLBACK_LINES[i % len(_FALLBACK_LINES)],
                    }
                    for i in range(shots)
                ],
            }
            for index in range(episodes)
        ],
        "_beats_order": beats,
    }


# ──────────────────────────────────────────────────────────────────────
# Validation — the model is allowed to be creative, not incoherent
# ──────────────────────────────────────────────────────────────────────


class BibleError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise BibleError(message)


def validate(data: dict[str, Any], *, episodes: int, shots: int) -> dict[str, Any]:
    _require(isinstance(data.get("characters"), dict) and data["characters"], "bible has no characters")
    _require(isinstance(data.get("locations"), dict) and data["locations"], "bible has no locations")
    _require(isinstance(data.get("episodes"), list) and data["episodes"], "bible has no episodes")

    for cid, character in data["characters"].items():
        _require(
            len((character.get("locked_appearance") or "").split()) >= 8,
            f"character {cid} has no usable locked_appearance",
        )
        character.setdefault("voice", {"gender": "neutral", "age": "adult", "quality": "plain"})
    for lid, location in data["locations"].items():
        _require(
            len((location.get("locked_description") or "").split()) >= 6,
            f"location {lid} has no usable locked_description",
        )

    known_c = set(data["characters"])
    known_l = set(data["locations"])
    data["episodes"] = data["episodes"][:episodes]
    for episode in data["episodes"]:
        beats = episode.get("beats") or []
        _require(len(beats) >= shots, f"episode {episode.get('no')} has fewer than {shots} beats")
        episode["beats"] = beats[:shots]
        for position, beat in enumerate(episode["beats"], start=1):
            # An unknown id is exactly the cross-episode drift this layer exists
            # to stop, so it is repaired against the bible instead of passed on.
            if beat.get("location_id") not in known_l:
                beat["location_id"] = sorted(known_l)[(position - 1) % len(known_l)]
            ids = [c for c in (beat.get("character_ids") or []) if c in known_c]
            beat["character_ids"] = ids or [sorted(known_c)[0]]
            beat.setdefault("camera", _FALLBACK_CAMERA[(position - 1) % len(_FALLBACK_CAMERA)])
            beat["line"] = (beat.get("line") or "").strip()
            beat.setdefault("action", "")
    rules = data.get("continuity_rules") or []
    data["continuity_rules"] = [
        {
            "id": rule.get("id") or f"SER-{i + 1:02d}",
            "text": rule.get("text", "").strip(),
            "severity": rule.get("severity") if rule.get("severity") in ("regenerate", "local_fix") else "regenerate",
        }
        for i, rule in enumerate(rules)
        if (rule.get("text") or "").strip()
    ]
    return data


# ──────────────────────────────────────────────────────────────────────


@dataclass
class SeriesBible:
    data: dict[str, Any]
    path: Path | None = None

    # ---- accessors -------------------------------------------------

    @property
    def word(self) -> str:
        return self.data["seed_word"]

    @property
    def slug(self) -> str:
        return self.data["slug"]

    @property
    def title(self) -> str:
        return self.data.get("title", self.word)

    def episode(self, number: int) -> dict[str, Any]:
        for episode in self.data["episodes"]:
            if int(episode.get("no", 0)) == int(number):
                return episode
        raise BibleError(f"episode {number} is not in the bible")

    @property
    def episode_numbers(self) -> list[int]:
        return [int(e.get("no", i + 1)) for i, e in enumerate(self.data["episodes"])]

    def character(self, cid: str) -> dict[str, Any]:
        return self.data["characters"][cid]

    def location(self, lid: str) -> dict[str, Any]:
        return self.data["locations"][lid]

    # ---- the locked block injected into every prompt ----------------

    def locked_block(self, location_id: str, character_ids: list[str]) -> str:
        """Deterministic.  Same ids in, byte-identical string out, forever."""

        grammar = self.data.get("visual_grammar", {})
        world = self.data.get("world", {})
        lines = [
            f"[STYLE] {grammar.get('lens', '')}; {grammar.get('lighting', '')}; "
            f"palette {world.get('palette', '')}; {world.get('tone', '')}.",
            f"[LOCATION · {self.location(location_id).get('name', location_id)}] "
            f"{self.location(location_id).get('locked_description', '')}",
        ]
        for cid in character_ids:
            character = self.character(cid)
            lines.append(
                f"[CHARACTER · {character.get('name', cid)}] {character.get('locked_appearance', '')}"
            )
        return "\n".join(line for line in lines if line.strip())

    def negative_prompt(self) -> str:
        return self.data.get("visual_grammar", {}).get("negative", "")

    def rule_lines(self) -> list[str]:
        return [f"{rule['id']}: {rule['text']}" for rule in self.data.get("continuity_rules", [])]

    # ---- persistence -----------------------------------------------

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        self.path = target
        return target

    @classmethod
    def load(cls, path: str | Path) -> "SeriesBible":
        target = Path(path)
        return cls(json.loads(target.read_text(encoding="utf-8")), target)


def build_bible(
    word: str,
    *,
    episodes: int = 3,
    shots: int = 5,
    cast: int = 2,
    places: int = 2,
    language: str = "en",
    allow_model: bool = True,
) -> tuple[SeriesBible, dict[str, Any]]:
    """Return (bible, provenance).  Never raises just because there is no key."""

    word = (word or "").strip()
    if not word:
        raise BibleError("give me one word to start from")

    provenance = {"source": "local-template", "model": None, "error": None}
    data: dict[str, Any] | None = None

    if allow_model and llm.configured():
        note = (
            "Write every visible string in Simplified Chinese, except the JSON keys "
            "and the id values, which stay ASCII."
            if language.startswith("zh")
            else "Write every visible string in English."
        )
        prompt = PROMPT_TEMPLATE.format(
            word=word,
            episodes=episodes,
            shots=shots,
            cast=cast,
            places=places,
            language_note=note,
            beat_order=", ".join(BEATS[:shots]),
        )
        try:
            data = validate(llm.chat_json(SYSTEM, prompt), episodes=episodes, shots=shots)
            provenance = {"source": "model", "model": llm.model_name(), "error": None}
        except (llm.ModelUnavailable, BibleError, ValueError, KeyError) as exc:
            provenance = {"source": "local-template", "model": None, "error": str(exc)[:200]}
            data = None

    if data is None:
        data = validate(_fallback(word, episodes=episodes, shots=shots), episodes=episodes, shots=shots)

    data["bible_version"] = BIBLE_VERSION
    data["seed_word"] = word
    data["slug"] = slugify(word)
    data["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data["shots_per_episode"] = shots
    data["provenance"] = provenance
    return SeriesBible(data), provenance
