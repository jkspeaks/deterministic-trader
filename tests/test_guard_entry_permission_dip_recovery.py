"""_entry_permission_denials — Dip Recovery phase 4.5 (part b).

`_entry_permission_denials` applied core's weekly-universe staleness gate to
every buy unconditionally. A Dip Recovery buy has no such snapshot — its
freshness gate is the Dip Monitor routine's `dip_recovery_watchlist.trading_day`
check — so `UNIVERSE_STALE` fired on every Dip Recovery cycle. Fixed the same
shape as `_candidate_denials` (phase 4.5) and `guard.lock` / `guard.strategy`:
branch on `ctx["strategy"]`. The cadence gate and the operator's
`entries_blocked` kill still apply to both sides.
"""

from __future__ import annotations

from src import guard
from tests.factories import codes, context, order

_STALE = {"snapshot_age_days": 99, "candidates": ["F"]}


def test_core_buy_is_still_denied_against_a_stale_universe_snapshot():
    result = guard.evaluate([order()], context(universe=_STALE))
    assert guard.UNIVERSE_STALE in codes(result, "F")


def test_dip_recovery_buy_is_not_denied_for_core_snapshot_staleness():
    ctx = context(strategy="dip_recovery", dip_recovery_watchlist=["F"], universe=_STALE)
    result = guard.evaluate([order()], ctx)
    assert guard.UNIVERSE_STALE not in codes(result, "F")


def test_entries_blocked_still_stops_a_dip_recovery_buy():
    """The operator kill is not carved out — it gates both sides."""
    ctx = context(
        strategy="dip_recovery",
        dip_recovery_watchlist=["F"],
        control={"entries_blocked": True, "entries_blocked_reason": "manual hold"},
    )
    result = guard.evaluate([order()], ctx)
    assert guard.ENTRIES_BLOCKED in codes(result, "F")


def test_cores_cadence_table_does_not_gate_a_dip_recovery_buy():
    """Reversed in phase 7, deliberately. `cycle.entries_allowed` comes from
    `cadence`'s cycle-type table — core's seven-slot schedule — and Dip Monitor
    runs on its own ~90-minute interval whose entire purpose is to enter.
    Judged against core's table every Dip Monitor tick is a `monitor` cycle, so
    phase 4.5b's version of this rule denied every Dip Recovery entry that has
    ever been proposed: the same mistake as checking its buys against core's
    universe, one function later.

    What still bounds this side is session-shaped, not cadence-shaped, and the
    two tests below pin it."""
    ctx = context(
        strategy="dip_recovery",
        dip_recovery_watchlist=["F"],
        cycle={"cycle_type": "monitor", "entries_allowed": False},
    )
    result = guard.evaluate([order()], ctx)
    assert guard.ENTRIES_NOT_ALLOWED_THIS_CYCLE not in codes(result, "F")


def test_core_is_still_gated_by_its_own_cadence():
    """The carve-out is one-directional — nothing about core changes."""
    ctx = context(cycle={"cycle_type": "monitor", "entries_allowed": False})
    result = guard.evaluate([order()], ctx)
    assert guard.ENTRIES_NOT_ALLOWED_THIS_CYCLE in codes(result, "F")


def test_a_closed_session_still_stops_a_dip_recovery_buy():
    """The replacement bound, and the reason dropping the cadence gate is safe:
    a closed market is a halt, which stops the whole plan rather than one
    order."""
    ctx = context(
        strategy="dip_recovery",
        dip_recovery_watchlist=["F"],
        cycle={"cycle_type": "postclose", "session": "closed", "entries_allowed": False},
    )
    result = guard.evaluate([order()], ctx)
    assert result.halted
    assert [d.code for d in result.halts] == [guard.SESSION_CLOSED]
