"""guard.schedule._candidate_denials — Dip Recovery phase 4.5.

`_candidate_denials` used to read `ctx["universe"]["candidates"]` and
`ctx["positions"]` unconditionally, which are core's own sets — a Dip
Recovery order routed through `guard.evaluate` would misfire both ways: a
buy on a symbol Dip Recovery's own watchlist named would be refused for not
being in core's scan, and a sell of a Dip Recovery position would be refused
for not being in core's book. Fixed the same shape as `guard.lock`'s phase 1
fix and `guard.strategy`'s phase 3 fix: branch on `ctx["strategy"]`.
"""

from __future__ import annotations

from src import guard
from tests.factories import codes, context, order


def test_dip_recovery_buy_in_its_own_watchlist_is_not_denied():
    """Core's own `universe.candidates` is emptied here on purpose, to prove
    the check reads Dip Recovery's watchlist rather than core's list."""
    ctx = context(
        strategy="dip_recovery",
        dip_recovery_watchlist=["F"],
        universe={"candidates": []},
    )
    result = guard.evaluate([order()], ctx)
    assert guard.NOT_IN_CANDIDATE_SET not in codes(result, "F")


def test_dip_recovery_buy_outside_its_watchlist_is_denied():
    """F sits in core's own candidate set here, which must not leak in —
    Dip Recovery only ever reads its own watchlist."""
    ctx = context(strategy="dip_recovery", dip_recovery_watchlist=[])
    result = guard.evaluate([order()], ctx)
    assert guard.NOT_IN_CANDIDATE_SET in codes(result, "F")


def test_dip_recovery_sell_of_a_held_dip_position_is_not_denied():
    """F is in neither core's `positions` nor Dip Recovery's watchlist — only
    `dip_recovery_positions`, which is the set a Dip Recovery exit must be
    allowed against."""
    ctx = context(
        strategy="dip_recovery",
        dip_recovery_watchlist=[],
        dip_recovery_positions={"F": {"state": "HOLDING"}},
    )
    sell = order(side="sell", limit_price=None, stop_price=14.00, type="market")
    result = guard.evaluate([sell], ctx)
    assert guard.NOT_IN_CANDIDATE_SET not in codes(result, "F")


def test_core_is_unaffected_by_a_dip_recovery_watchlist():
    """The reverse leak: Dip Recovery naming F does not make F a core
    candidate — core keeps reading its own `universe.candidates` alone,
    exactly as before this phase."""
    ctx = context(universe={"candidates": []}, dip_recovery_watchlist=["F"])
    result = guard.evaluate([order()], ctx)
    assert guard.NOT_IN_CANDIDATE_SET in codes(result, "F")


def test_core_candidate_behavior_is_otherwise_unchanged():
    ctx = context(universe={"candidates": ["F"]})
    result = guard.evaluate([order()], ctx)
    assert result.approved == [order()]
