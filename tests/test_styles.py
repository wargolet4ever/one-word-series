"""Choosing a look, and what that must not be allowed to break.

The risk with styles is not that a preset produces the wrong pictures. It is
that a style becomes a free variable, and then the cross-episode comparison —
the thing this project exists for — starts reporting drift that is merely a
change of medium, or worse, stops noticing real drift because everything moved.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from calibrate_drift import make_room, make_shot  # noqa: E402

from oneword import drift, styles  # noqa: E402
from oneword.bible import build_bible  # noqa: E402
from oneword.pipeline import build_shots, compose_prompt  # noqa: E402
from oneword.registry import ReferenceRegistry  # noqa: E402
from test_drift import SeriesBuilder  # noqa: E402


class PresetTests(unittest.TestCase):
    def test_every_preset_is_complete(self):
        for name in styles.names():
            preset = styles.get(name)
            for key in ("label", "render", "lens", "lighting", "palette", "tone", "negative"):
                self.assertTrue(preset.get(key), f"{name} is missing {key}")

    def test_an_unknown_style_lists_the_real_ones(self):
        with self.assertRaises(styles.StyleError) as caught:
            styles.get("cyberpunk-neon")
        self.assertIn("noir", str(caught.exception))

    def test_a_custom_style_can_come_from_json(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "mine.json"
            path.write_text(json.dumps({
                "render": "pinhole photography",
                "lens": "very wide, soft everywhere",
                "lighting": "long exposure daylight",
                "palette": "sepia",
                "tone": "still",
            }), encoding="utf-8")
            style = styles.resolve(str(path))
        self.assertEqual(style["name"], "mine")
        self.assertEqual(style["render"], "pinhole photography")

    def test_an_incomplete_custom_style_is_refused(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text(json.dumps({"render": "pinhole"}), encoding="utf-8")
            with self.assertRaises(styles.StyleError):
                styles.resolve(str(path))


class BibleStyleTests(unittest.TestCase):
    def test_a_style_reaches_every_prompt(self):
        bible, _ = build_bible("rust", episodes=1, shots=3, allow_model=False, style="anime")
        prompt = compose_prompt(bible, build_shots(bible, 1)[0])
        self.assertIn("2D cel animation", prompt)
        self.assertEqual(bible.style_name, "anime")

    def test_the_style_is_identical_in_every_shot_and_episode(self):
        bible, _ = build_bible("rust", episodes=3, shots=4, allow_model=False, style="noir")
        heads = {
            compose_prompt(bible, shot).split("\n")[0]
            for number in bible.episode_numbers
            for shot in build_shots(bible, number)
        }
        self.assertEqual(len(heads), 1)

    def test_a_restyle_leaves_the_cast_and_locations_alone(self):
        """A change of medium does not change who anyone is."""

        bible, _ = build_bible("rust", episodes=1, shots=3, allow_model=False)
        before = {
            "cast": {k: v["locked_appearance"] for k, v in bible.data["characters"].items()},
            "places": {k: v["locked_description"] for k, v in bible.data["locations"].items()},
            "beats": bible.data["episodes"][0]["beats"],
        }
        styles.apply_to(bible.data, styles.get("storybook"))
        self.assertEqual(
            {k: v["locked_appearance"] for k, v in bible.data["characters"].items()},
            before["cast"],
        )
        self.assertEqual(
            {k: v["locked_description"] for k, v in bible.data["locations"].items()},
            before["places"],
        )
        self.assertEqual(bible.data["episodes"][0]["beats"], before["beats"])

    def test_a_bible_written_before_styles_still_works(self):
        bible, _ = build_bible("rust", episodes=1, shots=3, allow_model=False)
        bible.data.pop("style", None)
        self.assertEqual(bible.style_name, "model-written")
        self.assertIn("[STYLE]", compose_prompt(bible, build_shots(bible, 1)[0]))

    def test_the_style_negative_is_carried_into_prompts(self):
        bible, _ = build_bible("rust", episodes=1, shots=3, allow_model=False, style="noir")
        prompt = compose_prompt(bible, build_shots(bible, 1)[0])
        self.assertIn("[NEGATIVE]", prompt)
        self.assertIn("colour", prompt.split("[NEGATIVE]")[1])


class StyleAndDriftTests(unittest.TestCase):
    """The interaction that would otherwise produce a lying report."""

    def build(self, root: Path, style: str | None):
        builder = SeriesBuilder(root)
        if style:
            styles.apply_to(builder.bible.data, styles.get(style))
        builder.bible.save(root / "bible.json")
        room = make_room(41)
        for number in (1, 2):
            builder.episode(number, [
                {"image": make_shot(room, number), "location_id": "L1", "character_ids": ["C1"]},
            ])
        return builder

    def test_references_record_the_style_they_were_shot_in(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.build(root, "noir")
            drift.audit_series(root, use_model=False)
            registry = ReferenceRegistry(root)
        self.assertEqual(registry.styles_present(), {"noir"})

    def test_comparing_across_a_restyle_is_refused_not_reported(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            builder = self.build(root, "noir")
            drift.audit_series(root, use_model=False)

            # Same series, restyled. Every frame now differs by design.
            styles.apply_to(builder.bible.data, styles.get("anime"))
            builder.bible.save(root / "bible.json")

            with self.assertRaises(drift.DriftError) as caught:
                drift.audit_series(root, use_model=False)
        message = str(caught.exception)
        self.assertIn("noir", message)
        self.assertIn("anime", message)
        self.assertIn("--reset-references", message)

    def test_re_basing_makes_the_comparison_possible_again(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            builder = self.build(root, "noir")
            drift.audit_series(root, use_model=False)
            styles.apply_to(builder.bible.data, styles.get("anime"))
            builder.bible.save(root / "bible.json")

            report = drift.audit_series(root, use_model=False, reset_references=True)
            registry = ReferenceRegistry(root)
        self.assertEqual(report["style"], "anime")
        self.assertEqual(registry.styles_present(), {"anime"})

    def test_an_unchanged_style_compares_normally(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.build(root, "noir")
            drift.audit_series(root, use_model=False)
            report = drift.audit_series(root, use_model=False)
        self.assertEqual(report["style"], "noir")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class CombinedStyleTests(unittest.TestCase):
    """Combining two looks is a written choice, not an automatic merge."""

    def test_the_shipped_combination_resolves_and_is_complete(self):
        path = Path(__file__).resolve().parents[1] / "styles" / "16mm-noir.json"
        style = styles.resolve(str(path))
        for key in ("render", "lens", "lighting", "palette", "tone"):
            self.assertTrue(style[key], key)
        self.assertEqual(style["name"], "16mm-noir")

    def test_it_keeps_the_stock_from_one_and_the_palette_from_the_other(self):
        path = Path(__file__).resolve().parents[1] / "styles" / "16mm-noir.json"
        style = styles.resolve(str(path))
        self.assertIn("16mm", style["render"])          # from the film preset
        self.assertIn("black and white", style["palette"])  # from noir
        # And it does not carry noir's contradiction with 16mm's warm stock.
        self.assertNotIn("warm fading stock", style["palette"])

    def test_overriding_two_presets_field_by_field_would_not_have_worked(self):
        """Why the combination is written out rather than computed.

        A naive merge takes every field from whichever preset came last, so
        `16mm+noir` is just noir. The conflict is real — one look wants warm
        faded stock, the other wants no colour at all — and only a person can
        decide which parts survive.
        """

        merged = dict(styles.get("16mm"), **styles.get("noir"))
        self.assertEqual(merged["palette"], styles.get("noir")["palette"])
        self.assertNotIn("16mm", merged["render"])
