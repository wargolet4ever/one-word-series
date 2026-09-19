"""What the tool says before it spends anything.

Two things are expensive to discover late: the look is locked for the whole
series, and the story may not have been written from your word at all. Both
belong in front of the first clip, and the second one has to be impossible to
sail past.
"""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from unittest import mock  # noqa: E402

from oneword import cli, opening, styles  # noqa: E402


class StyleChoiceTests(unittest.TestCase):
    def test_an_explicit_style_is_never_second_guessed(self):
        asked = []
        chosen = opening.choose_style(
            "noir", stream=io.StringIO(), ask=True,
            input_fn=lambda prompt: asked.append(prompt) or "1",
        )
        self.assertEqual(chosen, "noir")
        self.assertEqual(asked, [])

    def test_a_number_picks_that_preset(self):
        out = io.StringIO()
        chosen = opening.choose_style(None, stream=out, ask=True, input_fn=lambda _: "2")
        self.assertEqual(chosen, styles.names()[1])

    def test_a_name_works_too(self):
        chosen = opening.choose_style(
            None, stream=io.StringIO(), ask=True, input_fn=lambda _: "anime"
        )
        self.assertEqual(chosen, "anime")

    def test_zero_means_let_the_model_decide(self):
        chosen = opening.choose_style(
            None, stream=io.StringIO(), ask=True, input_fn=lambda _: "0"
        )
        self.assertIsNone(chosen)

    def test_nonsense_is_asked_again_rather_than_guessed_at(self):
        answers = iter(["purple", "99", "noir"])
        out = io.StringIO()
        chosen = opening.choose_style(
            None, stream=out, ask=True, input_fn=lambda _: next(answers)
        )
        self.assertEqual(chosen, "noir")
        self.assertIn("not one of the options", out.getvalue())

    def test_every_preset_is_on_the_menu(self):
        out = io.StringIO()
        opening.choose_style(None, stream=out, ask=True, input_fn=lambda _: "0")
        for name in styles.names():
            self.assertIn(name, out.getvalue())

    def test_a_non_interactive_run_says_what_it_is_about_to_do(self):
        """A cron job has nobody to ask, so it gets told instead."""

        out = io.StringIO()
        chosen = opening.choose_style(None, stream=out, ask=False)
        self.assertIsNone(chosen)
        self.assertIn("not locked", out.getvalue())


class ProvenanceTests(unittest.TestCase):
    def test_a_model_written_bible_gets_no_warning(self):
        self.assertEqual(
            opening.provenance_lines({"source": "model", "model": "x"}, "rust"), []
        )

    def test_the_template_notice_says_what_was_ignored(self):
        text = "\n".join(opening.provenance_lines({"source": "local-template"}, "rust"))
        self.assertIn("placeholder", text)
        self.assertIn("dialogue", text)
        self.assertIn("rust", text)

    def test_the_word_local_template_is_not_what_the_user_is_shown(self):
        """It reads like a technical detail; people sail straight past it."""

        text = "\n".join(opening.provenance_lines({"source": "local-template"}, "rust"))
        self.assertNotIn("local-template", text)

    def test_a_model_that_was_configured_and_failed_says_so(self):
        text = "\n".join(
            opening.provenance_lines(
                {"source": "local-template", "error": "401 unauthorized"}, "rust"
            )
        )
        self.assertIn("401 unauthorized", text)


class PaidTemplateWarningTests(unittest.TestCase):
    def test_it_names_the_money(self):
        text = "\n".join(opening.paid_template_warning("rust", "seedance-ark", 1.86, 10))
        self.assertIn("18.60", text)
        self.assertIn("10 clips", text)

    def test_it_still_warns_when_the_price_is_unknown(self):
        text = "\n".join(opening.paid_template_warning("rust", "seedance-ark", None, 10))
        self.assertIn("10 paid clips", text)

    def test_it_offers_the_free_way_out(self):
        text = "\n".join(opening.paid_template_warning("rust", "seedance-ark", 1.0, 3))
        self.assertIn("--vendor animatic", text)
        self.assertIn("LLM_API_KEY", text)


class ConfirmTests(unittest.TestCase):
    def test_only_yes_is_yes(self):
        for answer in ("y", "yes", "YES", " yes "):
            self.assertTrue(
                opening.confirm("?", stream=io.StringIO(), input_fn=lambda _: answer)
            )
        for answer in ("", "n", "no", "sure", "ok"):
            self.assertFalse(
                opening.confirm("?", stream=io.StringIO(), input_fn=lambda _: answer)
            )

    def test_a_closed_stdin_is_not_a_yes(self):
        def raises(_):
            raise EOFError

        self.assertFalse(opening.confirm("?", stream=io.StringIO(), input_fn=raises))

    def test_a_non_interactive_run_is_not_blocked_but_is_told(self):
        out = io.StringIO()
        self.assertTrue(opening.confirm("?", stream=out, ask=False))
        self.assertIn("without asking", out.getvalue())


class StubPaidVendor:
    name = "stub-paid"
    generative = True
    speaks = False
    accepts_first_frame = True
    accepts_reference_images = True
    unit_cost = 1.5


class CliGateTests(unittest.TestCase):
    """The gate has to close before any money moves, not after."""

    def run_cli(self, argv, answer):
        out = io.StringIO()
        with TemporaryDirectory() as tmp, \
             mock.patch.object(cli.opening, "is_interactive", return_value=True), \
             mock.patch.object(cli, "build_vendor", return_value=StubPaidVendor()), \
             mock.patch.object(cli, "run_episode") as shoot, \
             mock.patch("builtins.input", lambda _: answer), \
             contextlib.redirect_stdout(out):
            code = cli.main(argv + ["--out", tmp])
        return code, shoot, out.getvalue()

    def test_saying_no_stops_before_a_single_clip(self):
        code, shoot, text = self.run_cli(
            ["rust", "--vendor", "seedance", "--style", "noir", "--shots", "3"], "no"
        )
        self.assertEqual(code, cli.EXIT_STOPPED)
        shoot.assert_not_called()
        self.assertIn("nothing was charged", text)

    def test_the_warning_is_printed_before_the_question(self):
        _, _, text = self.run_cli(
            ["rust", "--vendor", "seedance", "--style", "noir", "--shots", "3"], "no"
        )
        self.assertIn("placeholder story", text)
        self.assertIn("¥", text)

    def test_the_free_vendor_is_never_gated(self):
        out = io.StringIO()
        with TemporaryDirectory() as tmp, contextlib.redirect_stdout(out):
            code = cli.main(["rust", "--shots", "3", "--style", "noir", "--yes", "--out", tmp])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertNotIn("about to pay", out.getvalue())


if __name__ == "__main__":
    unittest.main()
