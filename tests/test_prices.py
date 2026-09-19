"""Where the price of a clip comes from.

The budget cap is only worth having if the number behind it is real. That
makes pricing per-account data, not source code — and the first time a price
was hard-coded, an upgrade silently overwrote the user's corrected value and
the run stopped dead. These tests pin the arrangement that replaced it.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oneword import vendors  # noqa: E402

MODEL = "doubao-seedance-2-0-mini-260615"


def config(**kwargs):
    base = dict(api_key="k", model=MODEL, resolution="480p", duration=5, budget_cny=20.0)
    base.update(kwargs)
    return vendors.ArkConfig(**base)


class PriceSourceTests(unittest.TestCase):
    def setUp(self):
        for name in ("ONEWORD_PRICE", "ONEWORD_PRICES"):
            patch = mock.patch.dict(os.environ, {}, clear=False)
            patch.start()
            self.addCleanup(patch.stop)
            os.environ.pop(name, None)

    def test_nothing_ships_with_a_price(self):
        """A number in source is a number that is wrong for somebody."""

        self.assertEqual(vendors.PRICE_CNY, {})

    def test_a_price_file_is_read(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "prices.json"
            path.write_text(json.dumps({MODEL: {"480p": {"5": 1.86}}}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"ONEWORD_PRICES": str(path)}):
                self.assertEqual(vendors.estimated_cost_cny(config()), 1.86)

    def test_the_env_override_wins_for_one_run(self):
        with mock.patch.dict(os.environ, {"ONEWORD_PRICE": "2.31"}):
            self.assertEqual(vendors.estimated_cost_cny(config()), 2.31)

    def test_a_non_numeric_override_is_refused(self):
        with mock.patch.dict(os.environ, {"ONEWORD_PRICE": "cheap"}):
            with self.assertRaises(vendors.VendorError):
                vendors.estimated_cost_cny(config())

    def test_an_unpriced_combination_says_exactly_what_to_write(self):
        with TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"ONEWORD_PRICES": str(Path(tmp) / "none.json")}):
                with self.assertRaises(vendors.VendorError) as caught:
                    vendors.estimated_cost_cny(config())
        message = str(caught.exception)
        self.assertIn(MODEL, message)
        self.assertIn("480p", message)
        self.assertIn("ONEWORD_PRICE", message)
        self.assertIn('{"480p": {"5"', message)

    def test_a_different_resolution_is_a_different_price(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "prices.json"
            path.write_text(
                json.dumps({MODEL: {"480p": {"5": 1.86}, "720p": {"5": 4.00}}}),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"ONEWORD_PRICES": str(path)}):
                self.assertEqual(vendors.estimated_cost_cny(config(resolution="480p")), 1.86)
                self.assertEqual(vendors.estimated_cost_cny(config(resolution="720p")), 4.00)

    def test_a_broken_price_file_is_named_not_ignored(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "prices.json"
            path.write_text("{not json", encoding="utf-8")
            with mock.patch.dict(os.environ, {"ONEWORD_PRICES": str(path)}):
                with self.assertRaises(vendors.VendorError) as caught:
                    vendors.estimated_cost_cny(config())
        self.assertIn("prices.json", str(caught.exception))

    def test_the_budget_cap_uses_the_price_it_was_given(self):
        with mock.patch.dict(os.environ, {"ONEWORD_PRICE": "9.00"}):
            vendor = vendors.SeedanceVendor(
                config(budget_cny=10.0), opener=lambda request, timeout=None: None
            )
            self.assertEqual(vendor.unit_cost, 9.00)


if __name__ == "__main__":
    unittest.main(verbosity=2)
