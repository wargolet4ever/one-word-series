"""Which locked facts a generator can actually hold.

The categories here are not theory. Each one was flagged by a vision model
against real Seedance footage, and one — a large coloured architectural
feature — was never flagged at all. That asymmetry is the whole module.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oneword import lockability  # noqa: E402
from oneword.bible import build_bible  # noqa: E402


class MeasuredFailuresTests(unittest.TestCase):
    """Every string here failed in the first real vision pass."""

    def test_a_clock_at_a_stated_time_never_holds(self):
        issues = lockability.classify("The wall clock in Unit 704 always reads 4:10.")
        self.assertEqual([i.kind for i in issues], ["readable-value"])

    def test_which_side_of_a_body_never_holds(self):
        for text in (
            "deep vertical scar through the left eyebrow",
            "C1's left jacket cuff is torn in every shot.",
            "a notebook in his right trouser pocket",
        ):
            with self.subTest(text=text):
                self.assertIn("lateral", [i.kind for i in lockability.classify(text)])

    def test_an_object_a_few_pixels_across_never_holds(self):
        issues = lockability.classify("C2 wears the red collar pin in every shot.")
        self.assertEqual([i.kind for i in issues], ["tiny-accessory"])

    def test_a_count_never_holds(self):
        issues = lockability.classify("Only one warm practical light source is visible per shot.")
        self.assertEqual([i.kind for i in issues], ["count"])

    def test_specified_lettering_never_holds(self):
        self.assertIn(
            "readable-text", [i.kind for i in lockability.classify("The door sign reads STAIRWELL C.")]
        )


class MeasuredSuccessTests(unittest.TestCase):
    """The control. Large, coloured, material facts held in every single shot."""

    def test_the_one_rule_that_was_never_flagged_is_enforceable(self):
        self.assertTrue(
            lockability.enforceable("Stairwell handrails are green; they never change colour.")
        )

    def test_architecture_and_materials_are_enforceable(self):
        self.assertTrue(lockability.enforceable(
            "A concrete stairwell with painted green handrails, one flickering tube light, "
            "and a steel fire door at the bottom."
        ))

    def test_a_fist_sized_prop_is_not_a_tiny_accessory(self):
        """Flagging a key ring would make the warning itself the noise."""

        self.assertTrue(
            lockability.enforceable("always carrying a brass key ring on the belt")
        )

    def test_a_garment_is_enforceable(self):
        self.assertTrue(lockability.enforceable("an olive workwear jacket with a torn cuff"))


class ShippedTemplateTests(unittest.TestCase):
    def test_the_built_in_bible_holds_its_own_standard(self):
        """It did not. SER-03 asked every clip for a clock reading 4:10.

        That rule shipped in this repo and could never be satisfied, so it
        produced a drift finding in every shot of that room, for every user,
        forever — burying the one real finding under six invented ones.
        """

        bible, _ = build_bible("rust", episodes=2, shots=4, allow_model=False)
        problems = lockability.review(bible.data)
        self.assertEqual(
            problems, [], f"template contains unenforceable facts: {problems}"
        )

    def test_the_writing_prompt_warns_the_model_off_them(self):
        from oneword.bible import PROMPT_TEMPLATE

        for phrase in ("clock reads", "which SIDE", "collar pin", "only one light"):
            self.assertIn(phrase, PROMPT_TEMPLATE)


class ReviewTests(unittest.TestCase):
    def test_review_reaches_rules_characters_and_locations(self):
        data = {
            "continuity_rules": [{"id": "SER-09", "text": "The meter reads 220 in every shot."}],
            "characters": {"C1": {"name": "Ana", "locked_appearance": "a scar on her left cheek"}},
            "locations": {"L1": {"name": "Dock", "locked_description": "a steel gantry, rusted"}},
        }
        found = {problem["where"] for problem in lockability.review(data)}
        self.assertEqual(found, {"SER-09", "Ana (C1)"})

    def test_a_clean_bible_reviews_empty(self):
        data = {
            "continuity_rules": [{"id": "SER-01", "text": "The gantry is always rusted."}],
            "characters": {"C1": {"name": "Ana", "locked_appearance": "a heavy red coat"}},
            "locations": {},
        }
        self.assertEqual(lockability.review(data), [])

    def test_the_printed_lines_say_what_to_do_instead(self):
        data = {"continuity_rules": [{"id": "SER-09", "text": "The clock reads 4:10."}]}
        text = "\n".join(lockability.lines(lockability.review(data)))
        self.assertIn("SER-09", text)
        self.assertIn("readable-value", text)
        self.assertIn("not what time it shows", text)
        self.assertIn("rewording", text)

    def test_nothing_is_printed_when_nothing_is_wrong(self):
        self.assertEqual(lockability.lines([]), [])


if __name__ == "__main__":
    unittest.main()
