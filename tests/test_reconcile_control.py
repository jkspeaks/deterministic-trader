"""The `edge_control` row, which is how this module talks to every other one.

§13 requires cadence to reach the no-op decision from one Supabase read, before
any Robinhood call. That is only possible if something else has already written
down what the broker said — so `positions_held`, `has_whole_share_position`,
`unreconciled` and `pending_events` are all this module's to maintain, and a
cycle that skipped them would leave cadence blind rather than merely stale.

`high_water_mark` joins them for a different reason, settled: §10 says the mark
is maintained by the code that reads it, but `guard.py` reads it and emits no
writes at all. This module already holds `get_portfolio` equity at the start of
every reconciling cycle, so it is the only place the mark can be current before
guard denies against it.
"""

from src import cadence, reconcile
from tests.factories import cadence_context, fill, reconcile_context


def run(**overrides):
    return reconcile.run(reconcile_context(**overrides))


def updates(**overrides) -> dict:
    return run(**overrides).control_updates


# --- what cadence reads ----------------------------------------------------


def test_positions_held_is_the_reconciled_count():
    assert updates()["positions_held"] == 1


def test_an_empty_book_reports_zero_rather_than_omitting_the_field():
    """Cadence reads `positions_held` with a default of 0, so an omitted key
    and a real zero are indistinguishable there. They must not be here."""
    assert updates(positions=[], prior_positions=[], tax_lots={})["positions_held"] == 0


def test_a_whole_share_position_unlocks_the_overnight_cycles():
    """P1 x §13: while every position is fractional an overnight cycle cannot
    place anything, so cadence demotes 04:30 and 20:00 to no-ops. This flag is
    the only thing that promotes them back."""
    assert updates()["has_whole_share_position"] is True


def test_a_book_of_fractions_leaves_the_overnight_cycles_asleep():
    """The account as it actually stands today — ten fractional positions,
    §A.1. Every one of them is market-order-only and therefore regular-hours
    only, so an overnight cycle would be a paid-for no-op."""
    result = run(
        positions=[{"symbol": "F", "quantity": 0.275534}],
        tax_lots={
            "F": [
                {"quantity": 0.275534, "cost_basis": 4.13,
                 "acquired_date": "2026-07-30"}
            ]
        },
    )

    assert result.control_updates["has_whole_share_position"] is False


def test_exactly_one_share_is_a_whole_share():
    """The boundary, asserted directly. `>` rather than `>=` here would keep
    the overnight cycles asleep for the account's first real whole-share
    position, which is the exact event the flag exists to notice."""
    result = run(
        positions=[{"symbol": "F", "quantity": 1.0}],
        tax_lots={
            "F": [
                {"quantity": 1.0, "cost_basis": 15.00, "acquired_date": "2026-07-30"}
            ]
        },
    )

    assert result.control_updates["has_whole_share_position"] is True


# --- unreconciled ----------------------------------------------------------


def test_a_completed_pass_clears_unreconciled():
    """Set at placement by the routine, cleared here. §12.1: an unreconciled
    prior cycle that placed orders is itself an event, promoting the next
    cycle out of no-op."""
    assert updates()["unreconciled"] is False


def test_a_pass_that_found_divergence_still_clears_unreconciled():
    """Settled: two flags for two facts. "Has anyone looked" is not "did what
    they saw agree" — carrying both on one field would force every subsequent
    cycle out of no-op indefinitely, which is the expensive path §13 exists
    to avoid."""
    result = run(positions=[{"symbol": "F", "quantity": 2.0}])

    assert reconcile.POSITION_DELTA_MISMATCH in [f.code for f in result.findings]
    assert result.control_updates["unreconciled"] is False
    assert result.control_updates["divergence_streak"] == 1


# --- pending_events --------------------------------------------------------


def test_an_event_newer_than_the_last_full_cycle_is_pending():
    """Settled as: a row newer than `last_full_cycle` on a name currently held.
    Cadence's existing `positions_held > 0` disjunct already subsumes it, so
    this is belt and braces — it binds only if `has_work` ever tightens, and
    until then it costs one comparison."""
    result = run(events={"F": [{"observed_at": "2026-08-03T11:00:00Z"}]})

    assert result.control_updates["pending_events"] is True


def test_an_event_older_than_the_last_full_cycle_is_not_pending():
    """The last full cycle already reasoned about it."""
    result = run(events={"F": [{"observed_at": "2026-07-30T11:00:00Z"}]})

    assert result.control_updates["pending_events"] is False


def test_an_event_on_a_name_nobody_holds_is_not_pending():
    """Nothing to exit and nothing this flag could usefully wake the system
    for: an entry on an unheld name is already covered by cadence's own
    entries-remaining disjunct."""
    result = run(events={"GME": [{"observed_at": "2026-08-03T11:00:00Z"}]})

    assert result.control_updates["pending_events"] is False


def test_every_event_is_pending_when_no_full_cycle_has_ever_run():
    """Day one, and the safe direction: an unknown boundary wakes the system
    rather than sleeping through the first 8-K it ever sees."""
    result = run(
        control={"last_full_cycle": None},
        events={"F": [{"observed_at": "2026-07-01T11:00:00Z"}]},
    )

    assert result.control_updates["pending_events"] is True


# --- the high-water mark ---------------------------------------------------


def test_a_new_peak_raises_the_mark():
    result = run(account={"equity_total": 300.00})

    assert result.control_updates["high_water_mark"] == 300.00


def test_a_drawdown_never_lowers_the_mark():
    """The mark ratchets. A mark that tracked equity down would walk the §10
    floor down with it and the drawdown halt could never fire — a risk control
    that is present, tested, and unreachable."""
    result = run(account={"equity_total": 180.00})

    assert result.control_updates["high_water_mark"] == 236.42


def test_the_first_cycle_ever_sets_the_mark_from_equity():
    result = run(control={"high_water_mark": None}, account={"equity_total": 236.42})

    assert result.control_updates["high_water_mark"] == 236.42


def test_an_unreadable_equity_does_not_move_the_mark():
    """A missing/zero/non-numeric `equity_total` is ACCOUNT_UNREADABLE, not a new
    low. It must not ratchet the mark in either direction — the prior peak
    stands until a real read arrives."""
    for bad in ({"equity_total": None}, {"equity_total": 0}, {"equity_total": "abc"}):
        result = run(control={"high_water_mark": 236.42}, account=bad)

        assert result.control_updates["high_water_mark"] == 236.42, bad


def test_an_unreadable_equity_on_the_first_cycle_leaves_the_mark_at_zero():
    """No prior peak and no readable equity: there is nothing to ratchet, and
    0.0 keeps §10's floor at 0 until a real equity read arrives."""
    result = run(control={"high_water_mark": None}, account={"equity_total": None})

    assert result.control_updates["high_water_mark"] == 0.0


def test_the_mark_is_raised_in_the_same_cycle_guard_reads_it():
    """Why this module and not `report.py`: a mark written at 16:30 would
    leave guard denying against a stale floor for the whole session that set
    the peak."""
    result = run(cycle={"cycle_type": "monitor"}, account={"equity_total": 300.00})

    assert result.control_updates["high_water_mark"] == 300.00


# --- the handover to cadence -----------------------------------------------


def cadence_after(now: str, **overrides) -> dict:
    """A cadence firing at ``now``, reading what this module last wrote.

    Assignment into the context, not a factory keyword: `cadence_context`
    deep-merges, so passing these as `control=` would let a stale default fill
    any key this module stopped emitting and the assertion would still pass
    against a key that no longer exists.

    ``last_full_cycle`` is cadence's own clock, not this module's — it comes
    back on `Cycle.clock_updates`. It is set to this morning here so a 14:40
    firing reads as the monitor cycle it is rather than being promoted to the
    day's first full one.
    """
    ctx = cadence_context(now=now)
    ctx["control"] = {
        **ctx["control"],
        "last_full_cycle": "2026-08-03T13:40:00Z",
        **updates(**overrides),
    }
    return ctx


def test_what_this_module_writes_is_what_cadence_reads():
    """A flat book, nothing pending, and the day's three entries already taken:
    the one state in which §13's cheap path is reachable. Every field in that
    sentence except the entry count is this module's to report."""
    ctx = cadence_after(
        "2026-08-03T18:40:00Z", positions=[], prior_positions=[], tax_lots={}
    )
    ctx["control"]["new_positions"] = {"date": "2026-08-03", "count": 3}

    assert cadence.decide(ctx).cycle_type == cadence.NOOP


def test_a_reconciled_book_promotes_the_cycle_out_of_no_op():
    """The same 14:40 ET firing with the same spent entry cap, and one
    position on the books. Cadence has work because this module said so."""
    ctx = cadence_after("2026-08-03T18:40:00Z")
    ctx["control"]["new_positions"] = {"date": "2026-08-03", "count": 3}

    assert cadence.decide(ctx).cycle_type == cadence.MONITOR


def test_the_overnight_cycle_wakes_once_a_whole_share_exists():
    """P1 x §13, end to end: the flag this module writes at 09:40 is what lets
    the 20:00 cycle place an exit that night."""
    assert cadence.decide(cadence_after("2026-08-04T00:30:00Z")).cycle_type == (
        cadence.OVERNIGHT
    )


def test_a_book_of_fractions_leaves_the_overnight_cycle_a_no_op():
    """The other half of the same flag, which is what makes the test above
    about `has_whole_share_position` rather than about the hour."""
    ctx = cadence_after(
        "2026-08-04T00:30:00Z",
        positions=[{"symbol": "F", "quantity": 0.275534}],
        tax_lots={
            "F": [
                {"quantity": 0.275534, "cost_basis": 4.13,
                 "acquired_date": "2026-07-30"}
            ]
        },
    )

    assert cadence.decide(ctx).cycle_type == cadence.NOOP


# --- the handover to guard -------------------------------------------------


def test_the_block_this_module_sets_is_the_flag_guard_denies_on():
    """`guard.ENTRIES_BLOCKED` reads `control["entries_blocked"]` and its
    reason. §12.2's "block entries 1 cycle" is only a control if the two
    modules name the field identically."""
    control = updates(
        orders=[{"order_id": "zz99", "symbol": "F", "side": "buy",
                 "quantity": 1.0, "state": "queued"}]
    )

    assert control["entries_blocked"] is True
    assert control["entries_blocked_reason"]


def test_an_adopted_fill_leaves_the_reason_empty_rather_than_stale():
    """A reason that outlived its block is worse than none: the operator reads
    it and looks for a fault that has already cleared."""
    control = updates(
        fills=[fill(order_id="zz99", symbol="GME", quantity=2.0, price=30.00)],
        positions=[{"symbol": "F", "quantity": 1.0}, {"symbol": "GME", "quantity": 2.0}],
        tax_lots={
            "F": [
                {"quantity": 1.0, "cost_basis": 15.00, "acquired_date": "2026-07-30"}
            ],
            "GME": [
                {"quantity": 2.0, "cost_basis": 60.00, "acquired_date": "2026-08-03"}
            ],
        },
    )

    assert control["entries_blocked"] is False
    assert control["entries_blocked_reason"] is None


# --- operator_holds expiry -------------------------------------


def test_a_hold_past_hold_days_auto_expires():
    """`edge_control.operator_holds` used to say "No expires after N days"
    (docs/routine/cycle-prompt.md)."""
    result = run(
        control={"operator_holds": {"AMD": {"reason": "earnings run", "set_on": "2026-07-20"}}},
    )

    assert result.control_updates["operator_holds"] == {}
    assert any(
        f.code == reconcile.OPERATOR_HOLD_EXPIRED and f.symbol == "AMD"
        for f in result.findings
    )


def test_a_hold_within_hold_days_survives_and_is_not_rewritten():
    """No churn on a cycle nothing expired — matches this file's
    `unreconciled`/`retired_order_ids` economy of writes."""
    result = run(
        control={"operator_holds": {"AMD": {"reason": "earnings run", "set_on": "2026-07-28"}}},
    )

    assert "operator_holds" not in result.control_updates
    assert not any(f.code == reconcile.OPERATOR_HOLD_EXPIRED for f in result.findings)


def test_a_mixed_set_keeps_the_survivor_and_drops_the_expired_one():
    result = run(
        control={
            "operator_holds": {
                "AMD": {"reason": "earnings run", "set_on": "2026-07-20"},
                "MU": {"reason": "guidance call", "set_on": "2026-08-01"},
            }
        },
    )

    assert result.control_updates["operator_holds"] == {
        "MU": {"reason": "guidance call", "set_on": "2026-08-01"}
    }
