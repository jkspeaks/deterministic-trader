"""guard.lock — Dip Recovery plan, phase 1: `strategy_symbol_lock`.

Robinhood has no broker-level way to mark a symbol's shares as belonging to
one strategy or the other, so this is the guard's own substitute: a symbol
locked to one side denies every order — buy or sell — the other side tries to
place on it. The baseline context carries no `symbol_lock` or `strategy` key
at all, which is deliberate: every one of these fixtures predates Dip
Recovery, and the first assertion in each test group is that an absent lock
changes nothing for the code that has always run without one.
"""

from __future__ import annotations

from src import guard
from tests.factories import codes, context, order


def test_no_lock_table_is_not_a_denial():
    ctx = context()
    result = guard.evaluate([order()], ctx)
    assert guard.SYMBOL_LOCKED_OTHER_SIDE not in codes(result, "F")


def test_unclaimed_symbol_is_not_a_denial_for_either_side():
    ctx = context(symbol_lock={}, strategy="dip_recovery")
    result = guard.evaluate([order()], ctx)
    assert guard.SYMBOL_LOCKED_OTHER_SIDE not in codes(result, "F")


def test_core_may_still_trade_a_symbol_locked_to_core():
    ctx = context(symbol_lock={"F": "core"})
    result = guard.evaluate([order()], ctx)
    assert guard.SYMBOL_LOCKED_OTHER_SIDE not in codes(result, "F")


def test_dip_recovery_denied_a_symbol_locked_to_core():
    ctx = context(symbol_lock={"F": "core"}, strategy="dip_recovery")
    result = guard.evaluate([order()], ctx)
    assert guard.SYMBOL_LOCKED_OTHER_SIDE in codes(result, "F")


def test_core_denied_a_symbol_locked_to_dip_recovery():
    """The asking side defaults to `"core"` when `ctx["strategy"]` is absent —
    every call site that predates Dip Recovery keeps behaving exactly as it
    always has."""
    ctx = context(symbol_lock={"F": "dip_recovery"})
    result = guard.evaluate([order()], ctx)
    assert guard.SYMBOL_LOCKED_OTHER_SIDE in codes(result, "F")


def test_lock_denies_a_sell_on_the_other_sides_symbol_too():
    """The sharpest gap the plan review found: core's generic exits evaluate
    every held position by symbol with no notion of who opened it, so the
    lock has to gate exits exactly as it gates entries or a Dip Recovery
    position is invisible to the lock the moment core tries to close it."""
    ctx = context(
        symbol_lock={"F": "dip_recovery"},
        positions=[{"symbol": "F", "market_value": 14.88}],
    )
    sell = order(side="sell", limit_price=None, stop_price=14.00, type="market")
    result = guard.evaluate([sell], ctx)
    assert guard.SYMBOL_LOCKED_OTHER_SIDE in codes(result, "F")


def test_dip_recovery_may_sell_its_own_locked_symbol():
    ctx = context(
        symbol_lock={"F": "dip_recovery"},
        strategy="dip_recovery",
        positions=[{"symbol": "F", "market_value": 14.88}],
    )
    sell = order(side="sell", limit_price=None, stop_price=14.00, type="market")
    result = guard.evaluate([sell], ctx)
    assert guard.SYMBOL_LOCKED_OTHER_SIDE not in codes(result, "F")
