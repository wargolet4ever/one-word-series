"""Give a test a price, and take away any the machine already had.

`estimated_cost_cny` reads `ONEWORD_PRICE` **before** it reads the table, which
is the right precedence for a run — it is the one-off escape hatch — and a trap
for a test suite. Patching `vendors.PRICE_CNY` alone leaves that env var in
charge, so anyone who has ever exported a real price for a real run gets five
failures that have nothing to do with their checkout.

That is the bug this file exists to make impossible: the test environment is
now stated, not inherited.
"""

from __future__ import annotations

import os
from unittest import mock

from oneword import vendors

TEST_MODEL = "doubao-seedance-1-0-lite-t2v-250428"
PRICE_ENV = ("ONEWORD_PRICE", "ONEWORD_PRICES")


def use_test_price(
    case,
    model: str = TEST_MODEL,
    resolution: str = "720p",
    duration: int = 5,
    price: float = 0.75,
) -> None:
    """One known price, and no ambient ones."""

    environment = mock.patch.dict(os.environ, {}, clear=False)
    environment.start()
    case.addCleanup(environment.stop)
    for name in PRICE_ENV:
        os.environ.pop(name, None)

    table = mock.patch.dict(vendors.PRICE_CNY, {(model, resolution, duration): price})
    table.start()
    case.addCleanup(table.stop)
