"""The §10 drawdown floor, defined once (design v3 §10).

`guard.py` denies buys below the floor and `plays.py` emits the flatten that
takes the book down — §6.5's exit 2. They are the two halves of one rule, and
two arithmetics would let plays flatten at a floor guard still considers safe.

It lives here for the same reason §4.4's cap fractions do: it is risk
arithmetic, and the modules that enforce it import rather than restate it.
"""

import pytest

from src import guard, sizing
from tests.factories import codes, config, context, order

HIGH_WATER = 295.525
# 20% below, and the number that first surfaced the float-noise problem:
# 0.8 * 295.525 is 236.42000000000002 in binary floating point.
FLOOR = 236.42


def test_the_floor_is_twenty_percent_below_the_high_water_mark():
    assert sizing.drawdown_floor(HIGH_WATER, config()) == pytest.approx(FLOOR)


def test_equity_at_the_floor_is_not_a_breach():
    """The account sitting exactly on its own high-water floor must not halt.

    0.8 * 295.525 lands a fraction of a cent above 236.42, so an exact `<`
    would breach the system on a number it computed itself.
    """
    assert not sizing.drawdown_breached({"equity_total": FLOOR}, HIGH_WATER, config())


def test_a_cent_below_the_floor_is_a_breach():
    assert sizing.drawdown_breached({"equity_total": FLOOR - 0.01}, HIGH_WATER, config())


def test_half_a_cent_below_the_floor_is_noise_and_not_a_breach():
    assert not sizing.drawdown_breached({"equity_total": FLOOR - 0.004}, HIGH_WATER, config())


def test_the_halt_percentage_is_config_not_a_constant():
    assert sizing.drawdown_floor(1_000.0, config(drawdown_halt_pct=0.10)) == pytest.approx(900.0)


def test_guard_denies_against_this_floor_and_not_its_own():
    """The regression this shared definition exists to prevent.

    Guard's denial and plays' flatten must move together. With a 50% halt
    percentage the floor drops to 147.76 and this equity is comfortably above
    it, so a guard still holding a private 20% would deny a buy that §10 now
    permits.
    """
    ctx = context(
        control={"high_water_mark": HIGH_WATER},
        account={"equity_total": FLOOR - 0.01},
        config=config(drawdown_halt_pct=0.50),
    )

    result = guard.evaluate([order()], ctx)

    assert not sizing.drawdown_breached({"equity_total": FLOOR - 0.01}, HIGH_WATER, ctx["config"])
    assert guard.DRAWDOWN_HALT not in codes(result)
