"""guard.strategy — Dip Recovery plan, phase 3: the two per-strategy enable
flags read from `dip_recovery_config`.

The baseline context carries no `dip_recovery_config` key at all, which is
deliberate: every fixture here predates this phase, and the first assertion
in each group is that an absent config changes nothing for core — while
correctly fail-closing a `strategy="dip_recovery"` request, since no routine
has been built yet to ask for anything else.
"""

from __future__ import annotations

from src import guard
from tests.factories import codes, context, order


def test_no_config_table_core_is_not_denied():
    ctx = context()
    result = guard.evaluate([order()], ctx)
    assert guard.STRATEGY_DISABLED not in codes(result, "F")


def test_no_config_table_dip_recovery_is_denied():
    """Fail-closed default: a strategy with no routine yet must not be one
    payload key away from placing an order."""
    ctx = context(strategy="dip_recovery")
    result = guard.evaluate([order()], ctx)
    assert guard.STRATEGY_DISABLED in codes(result, "F")


def test_core_denied_when_its_flag_is_off():
    ctx = context(dip_recovery_config={"core_strategies_enabled": False})
    result = guard.evaluate([order()], ctx)
    assert guard.STRATEGY_DISABLED in codes(result, "F")


def test_dip_recovery_allowed_when_its_flag_is_on():
    ctx = context(
        strategy="dip_recovery",
        dip_recovery_config={"dip_recovery_enabled": True},
    )
    result = guard.evaluate([order()], ctx)
    assert guard.STRATEGY_DISABLED not in codes(result, "F")


def test_the_other_sides_flag_does_not_affect_this_side():
    ctx = context(dip_recovery_config={"dip_recovery_enabled": True})
    result = guard.evaluate([order()], ctx)
    assert guard.STRATEGY_DISABLED not in codes(result, "F")


def test_denial_is_entry_only_a_paused_side_still_exits():
    """A strategy turned off is paused, not liquidated — it must go on
    managing and exiting whatever it already holds."""
    ctx = context(
        dip_recovery_config={"core_strategies_enabled": False},
        positions=[{"symbol": "F", "market_value": 14.88}],
    )
    sell = order(side="sell", limit_price=None, stop_price=14.00, type="market")
    result = guard.evaluate([sell], ctx)
    assert guard.STRATEGY_DISABLED not in codes(result, "F")


def test_strategy_defaults_to_core_when_absent():
    """Every call site that predates Dip Recovery never sends `strategy` at
    all, and must behave exactly as if it said `"core"`."""
    ctx = context(dip_recovery_config={"core_strategies_enabled": False})
    result = guard.evaluate([order()], ctx)
    assert guard.STRATEGY_DISABLED in codes(result, "F")
