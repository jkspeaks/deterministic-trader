"""§12.2's six classes, one test each, plus the escalation that watches them.

Each starts from a context where the broker and the book agree and introduces
exactly one disagreement, so a failure names one class. The table is the whole
specification of this module's judgment: everything else it produces —
positions, ledger rows, control writes — is bookkeeping around these six.

The pair worth reading together is `PHANTOM_ORDER` and `UNTRACKED_FILL`. They
describe the *same* operator trade at two moments in its life and get opposite
responses, which is deliberate and explained where the classes are raised.
"""

from src import reconcile
from tests.factories import (
    broker_order,
    config,
    fill,
    found,
    placement,
    reconcile_context,
)


def run(**overrides):
    return reconcile.run(reconcile_context(**overrides))


# --- the agreeing baseline -------------------------------------------------


def test_a_cycle_where_nothing_diverges_finds_nothing():
    """The precondition for every test below. If this ever fails, the others
    are asserting against noise rather than against the change they made."""
    assert run().findings == []


# --- MISSING_ORDER ---------------------------------------------------------


def test_a_placement_the_broker_never_heard_of_is_missing():
    """§12.2: plan said buy, placement logged, no broker order. Alert and
    proceed — the money was not spent, so nothing needs undoing."""
    result = run(intent={"placements": [placement()]})

    assert found(result, reconcile.MISSING_ORDER) == ["F"]


def test_a_placement_that_never_got_an_order_id_is_missing():
    """The routine logs what the broker handed back. Nothing handed back is
    the same fact as nothing placed, and reads as the same class."""
    result = run(intent={"placements": [placement(order_id=None)]})

    assert found(result, reconcile.MISSING_ORDER) == ["F"]


def test_a_placement_the_broker_has_open_is_not_missing():
    result = run(
        intent={"placements": [placement()]},
        orders=[broker_order()],
    )

    assert reconcile.MISSING_ORDER not in found(result)


def test_a_placement_that_already_filled_is_not_missing():
    """An order can be gone from `get_equity_orders` because it completed.
    Reading only the open list would call every fill a missing order."""
    result = run(
        intent={"placements": [placement()]},
        fills=[fill()],
        positions=[{"symbol": "F", "quantity": 2.0}],
        tax_lots={
            "F": [
                {"quantity": 1.0, "cost_basis": 15.00, "acquired_date": "2026-07-30"},
                {"quantity": 1.0, "cost_basis": 15.00, "acquired_date": "2026-08-03"},
            ]
        },
    )

    assert reconcile.MISSING_ORDER not in found(result)


# --- PHANTOM_ORDER ---------------------------------------------------------


def test_a_broker_order_no_plan_proposed_is_a_phantom():
    """§13.1's failure, already survived once here: two deciders on one
    account. An order nothing in the runlog claims is the only evidence of it
    this module can see."""
    result = run(orders=[broker_order(order_id="zz99")])

    assert found(result, reconcile.PHANTOM_ORDER) == ["F"]


def test_a_phantom_blocks_entries_for_one_cycle():
    result = run(orders=[broker_order(order_id="zz99")])

    assert result.control_updates["entries_blocked"] is True
    assert "zz99" in result.control_updates["entries_blocked_reason"]


def test_a_phantom_block_lifts_on_the_next_clean_cycle():
    """"Block entries 1 cycle" is one cycle. A block that outlived its cause
    would be indistinguishable from the persistent escalation below."""
    assert run().control_updates["entries_blocked"] is False


def test_the_agents_own_order_is_not_a_phantom():
    result = run(
        intent={"placements": [placement()]},
        orders=[broker_order()],
    )

    assert reconcile.PHANTOM_ORDER not in found(result)


def test_a_phantom_is_never_cancelled():
    """The operator placing an order in the app is a supported workflow
    (§12.2). Blocking entries for a cycle costs one cycle; cancelling their
    order destroys something a human deliberately did."""
    result = run(orders=[broker_order(order_id="zz99")])

    assert result.cancels == []


# --- UNTRACKED_FILL --------------------------------------------------------


def test_a_fill_with_no_originating_plan_is_adopted():
    """§12.2's one class that is not an error. The operator bought GME in the
    app; Invariant 7 says it is managed on identical terms from now on."""
    result = run(
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

    assert found(result, reconcile.UNTRACKED_FILL) == ["GME"]


def test_an_untracked_fill_does_not_block_entries():
    """"Absorb silently" — alerting on the operator's own deliberate action is
    exactly the noise that makes a system tiresome (§12.2)."""
    result = run(
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

    assert result.control_updates["entries_blocked"] is False
    assert result.control_updates["divergence_streak"] == 0


def test_an_untracked_fill_does_not_also_read_as_a_position_mismatch():
    """The operator's shares are counted into the expected book before it is
    compared. Without that, every manual trade would raise two findings and
    the second would be an error — which is how "absorb silently" stops being
    silent."""
    result = run(
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

    assert reconcile.POSITION_DELTA_MISMATCH not in found(result)


# --- QUANTITY_DRIFT --------------------------------------------------------


def test_a_fill_quantity_past_epsilon_is_drift():
    result = run(
        intent={"placements": [placement(quantity=1.0)]},
        orders=[broker_order(state="filled")],
        fills=[fill(quantity=0.5)],
        positions=[{"symbol": "F", "quantity": 1.5}],
        tax_lots={
            "F": [
                {"quantity": 1.0, "cost_basis": 15.00, "acquired_date": "2026-07-30"},
                {"quantity": 0.5, "cost_basis": 7.50, "acquired_date": "2026-08-03"},
            ]
        },
    )

    assert found(result, reconcile.QUANTITY_DRIFT) == ["F"]


def test_fractional_rounding_inside_epsilon_is_not_drift():
    """§12.2 names the epsilon for exactly this: six-decimal quantities make a
    bare `!=` a permanent false positive on every fractional order."""
    result = run(
        intent={"placements": [placement(quantity=0.275534)]},
        orders=[broker_order(state="filled")],
        fills=[fill(quantity=0.2755341)],
        positions=[{"symbol": "F", "quantity": 1.2755341}],
        tax_lots={
            "F": [
                {"quantity": 1.0, "cost_basis": 15.00, "acquired_date": "2026-07-30"},
                {
                    "quantity": 0.2755341,
                    "cost_basis": 4.13,
                    "acquired_date": "2026-08-03",
                },
            ]
        },
    )

    assert reconcile.QUANTITY_DRIFT not in found(result)


def test_a_partial_fill_on_a_still_open_order_is_not_drift():
    """An order half filled at 10:40 is an order still working, not a broker
    that lost half the trade. Drift is only meaningful once the order is done."""
    result = run(
        intent={"placements": [placement(quantity=1.0)]},
        orders=[broker_order(state="partially_filled")],
        fills=[fill(quantity=0.5)],
        positions=[{"symbol": "F", "quantity": 1.5}],
        tax_lots={
            "F": [
                {"quantity": 1.0, "cost_basis": 15.00, "acquired_date": "2026-07-30"},
                {"quantity": 0.5, "cost_basis": 7.50, "acquired_date": "2026-08-03"},
            ]
        },
    )

    assert reconcile.QUANTITY_DRIFT not in found(result)


# --- POSITION_DELTA_MISMATCH -----------------------------------------------


def test_a_book_that_does_not_equal_prior_plus_fills_is_a_mismatch():
    """No fill explains the extra share, so something happened this module
    cannot account for. Alert and proceed: the broker wins, and the book it
    reports is the one carried forward."""
    result = run(positions=[{"symbol": "F", "quantity": 2.0}])

    assert found(result, reconcile.POSITION_DELTA_MISMATCH) == ["F"]


def test_a_position_that_vanished_with_no_sell_is_a_mismatch():
    result = run(positions=[])

    assert found(result, reconcile.POSITION_DELTA_MISMATCH) == ["F"]


def test_a_fill_that_explains_the_change_is_not_a_mismatch():
    result = run(
        intent={"placements": [placement()]},
        orders=[broker_order(state="filled")],
        fills=[fill()],
        positions=[{"symbol": "F", "quantity": 2.0}],
        tax_lots={
            "F": [
                {"quantity": 1.0, "cost_basis": 15.00, "acquired_date": "2026-07-30"},
                {"quantity": 1.0, "cost_basis": 15.00, "acquired_date": "2026-08-03"},
            ]
        },
    )

    assert reconcile.POSITION_DELTA_MISMATCH not in found(result)


def test_a_sell_fill_that_explains_the_exit_is_not_a_mismatch():
    result = run(
        intent={"placements": [placement(side="sell")]},
        orders=[broker_order(side="sell", state="filled")],
        fills=[fill(side="sell", price=14.00, cost_basis=15.00)],
        positions=[],
        tax_lots={},
    )

    assert reconcile.POSITION_DELTA_MISMATCH not in found(result)


# --- STALE_OPEN_ORDER ------------------------------------------------------


def test_an_order_open_across_two_reconciling_cycles_is_cancelled():
    """§12.2's "still open after 2 cycles", counted in §12.1's cycles — full,
    monitor and postclose, the only ones this module runs on."""
    result = run(
        intent={"placements": [placement()]},
        orders=[broker_order()],
        control={"open_order_cycles": {"a1b2": 1}},
    )

    assert found(result, reconcile.STALE_OPEN_ORDER) == ["F"]
    assert [c["order_id"] for c in result.cancels] == ["a1b2"]


def test_an_order_open_for_one_cycle_is_left_alone():
    result = run(
        intent={"placements": [placement()]},
        orders=[broker_order()],
    )

    assert reconcile.STALE_OPEN_ORDER not in found(result)
    assert result.cancels == []


def test_the_open_order_counter_advances_each_reconciling_cycle():
    result = run(
        intent={"placements": [placement()]},
        orders=[broker_order()],
    )

    assert result.control_updates["open_order_cycles"] == {"a1b2": 1}


def test_the_counter_forgets_an_order_that_is_no_longer_open():
    """Self-pruning, so the map holds what is working rather than everything
    the account ever placed."""
    result = run(
        intent={"placements": [placement()]},
        orders=[broker_order(state="filled")],
        fills=[fill()],
        control={"open_order_cycles": {"a1b2": 1}},
        positions=[{"symbol": "F", "quantity": 2.0}],
        tax_lots={
            "F": [
                {"quantity": 1.0, "cost_basis": 15.00, "acquired_date": "2026-07-30"},
                {"quantity": 1.0, "cost_basis": 15.00, "acquired_date": "2026-08-03"},
            ]
        },
    )

    assert result.control_updates["open_order_cycles"] == {}


def test_the_postclose_cycle_cancels_every_unfilled_order():
    """§13's post-close row: reconcile, cancel unfilled, write scorecard data.
    This is also why the stale counter never has to survive a weekend — no
    order of ours lives past 16:30."""
    result = run(
        cycle={"cycle_type": "postclose", "session": "extended_hours"},
        intent={"placements": [placement()]},
        orders=[broker_order()],
    )

    assert [c["order_id"] for c in result.cancels] == ["a1b2"]


def test_the_postclose_sweep_still_spares_a_phantom():
    """Same reason as above: an order this system did not place is not this
    system's to cancel, whatever the hour."""
    result = run(
        cycle={"cycle_type": "postclose", "session": "extended_hours"},
        orders=[broker_order(order_id="zz99")],
    )

    assert result.cancels == []


def test_the_postclose_sweep_spares_a_resting_protective_stop():
    """Without this a resting stop is cancelled the same day it is placed, and
    the position it protected has none. Marking the placement `protective_stop: true` is what tells
    reconcile apart a stop meant to rest from a stale entry order that
    never filled."""
    result = run(
        cycle={"cycle_type": "postclose", "session": "extended_hours"},
        intent={"placements": [placement(protective_stop=True)]},
        orders=[broker_order(side="sell", type="stop_market")],
    )

    assert result.cancels == []
    assert reconcile.STALE_OPEN_ORDER not in found(result)


def test_an_ordinary_stale_order_is_still_cancelled_at_postclose():
    """The exclusion is narrow: only a placement explicitly marked
    `protective_stop: true` is spared. Everything else keeps the old
    behaviour."""
    result = run(
        cycle={"cycle_type": "postclose", "session": "extended_hours"},
        intent={"placements": [placement()]},
        orders=[broker_order()],
    )

    assert [c["order_id"] for c in result.cancels] == ["a1b2"]


def test_a_resting_stop_open_across_two_cycles_is_also_spared():
    """The 2-cycle stale rule is the same code path as the postclose sweep
    (`seen >= STALE_ORDER_CYCLES`); the exclusion has to cover both, not just
    the `postclose` branch, or a resting stop still open at a second monitor
    cycle would be cancelled mid-session."""
    result = run(
        intent={"placements": [placement(protective_stop=True)]},
        orders=[broker_order(side="sell", type="stop_market")],
        control={"open_order_cycles": {"a1b2": 1}},
    )

    assert result.cancels == []
    assert reconcile.STALE_OPEN_ORDER not in found(result)


# --- ENTRY_RESUBMIT ------------------------------------------------------------
#
# A GFD entry limit at the ask sat unfilled on 3 of 4 buys and then died at the
# post-close with nothing re-tried. An entry that has worked longer than
# `entry_resubmit_after_min` while the day can still act on it is cancelled and
# its symbol handed back.


def _resubmit_run(**overrides):
    """A reconciling monitor cycle with one working entry limit, 40 min old."""
    base = dict(
        cycle={"entries_allowed": True},
        intent={"placements": [placement()]},
        orders=[broker_order(created_at="2026-08-03T13:00:00Z")],
    )
    base.update(overrides)
    return run(**base)


def test_an_entry_unfilled_past_the_window_is_cancelled_and_requeued():
    result = _resubmit_run()

    assert found(result, reconcile.ENTRY_RESUBMIT) == ["F"]
    assert [c["order_id"] for c in result.cancels] == ["a1b2"]
    assert result.control_updates["resubmit_candidates"] == {
        "trading_day": "2026-08-03",
        "triggers": [{"symbol": "F", "play_id": "DIP"}],
    }


def test_a_requeued_entry_is_retired_so_it_does_not_read_as_drift():
    """The cancelled order goes terminal next cycle with nothing filled against
    it; without retirement that reads as QUANTITY_DRIFT and, two cycles running,
    escalates to a persistent block."""
    result = _resubmit_run()

    assert "a1b2" in result.control_updates["retired_order_ids"]


def test_an_entry_still_inside_the_window_is_left_to_work():
    result = _resubmit_run(orders=[broker_order(created_at="2026-08-03T13:25:00Z")])

    assert reconcile.ENTRY_RESUBMIT not in found(result)
    assert result.cancels == []
    assert result.control_updates["resubmit_candidates"]["triggers"] == []


def test_the_resubmit_window_is_config():
    result = _resubmit_run(
        orders=[broker_order(created_at="2026-08-03T13:25:00Z")],
        config=config(entry_resubmit_after_min=10),
    )

    assert found(result, reconcile.ENTRY_RESUBMIT) == ["F"]


def test_nothing_is_requeued_once_the_entry_window_has_closed():
    """`entries_allowed` is false on the post-close sweep — re-queueing for a
    window that has shut buys nothing. The order is still cancelled, but by
    §13's blanket sweep, not as an ENTRY_RESUBMIT."""
    result = _resubmit_run(
        cycle={"cycle_type": "postclose", "session": "extended_hours",
               "entries_allowed": False},
    )

    assert reconcile.ENTRY_RESUBMIT not in found(result)
    assert result.control_updates["resubmit_candidates"]["triggers"] == []
    assert [c["order_id"] for c in result.cancels] == ["a1b2"]


def test_a_partly_filled_entry_is_working_and_not_resubmitted():
    result = _resubmit_run(
        orders=[broker_order(created_at="2026-08-03T13:00:00Z", quantity=2.0)],
        fills=[fill(quantity=1.0)],
        positions=[{"symbol": "F", "quantity": 2.0}],
    )

    assert reconcile.ENTRY_RESUBMIT not in found(result)
    assert result.cancels == []


def test_a_resting_protective_order_is_never_resubmitted():
    result = _resubmit_run(
        intent={"placements": [placement(side="sell", protective_stop=True)]},
        orders=[broker_order(side="sell", type="stop_market",
                             created_at="2026-08-03T13:00:00Z")],
    )

    assert reconcile.ENTRY_RESUBMIT not in found(result)
    assert result.cancels == []


def test_a_resubmit_and_a_stale_hit_on_one_order_cancel_it_once():
    """Both the 2-cycle stale rule and the resubmit window can bite the same
    order; the stale branch defers to the more specific resubmit so the routine
    is not handed the same cancel twice."""
    result = _resubmit_run(control={"open_order_cycles": {"a1b2": 1}})

    assert [c["order_id"] for c in result.cancels] == ["a1b2"]
    assert reconcile.STALE_OPEN_ORDER not in found(result)
    assert found(result, reconcile.ENTRY_RESUBMIT) == ["F"]


def test_an_ordinary_reconciling_cycle_writes_an_empty_resubmit_list():
    """Written every reconciling cycle so an empty list overwrites a prior
    cycle's — a re-queue must not outlive the cycle that raised it."""
    assert run().control_updates["resubmit_candidates"] == {
        "trading_day": "2026-08-03", "triggers": [],
    }


# --- RECONCILIATION_DIVERGENCE_PERSISTENT ----------------------------------


def test_one_cycle_of_divergence_does_not_escalate():
    result = run(positions=[{"symbol": "F", "quantity": 2.0}])

    assert reconcile.RECONCILIATION_DIVERGENCE_PERSISTENT not in found(result)
    assert result.control_updates["divergence_streak"] == 1


def test_divergence_two_cycles_running_blocks_entries_until_cleared():
    """§12.2. A repeat is a signal, not a second log line — and unlike the
    phantom block, this one does not lift on its own."""
    result = run(
        positions=[{"symbol": "F", "quantity": 2.0}],
        control={"divergence_streak": 1},
    )

    assert reconcile.RECONCILIATION_DIVERGENCE_PERSISTENT in found(result)
    assert result.control_updates["entries_blocked"] is True
    assert result.control_updates["entries_blocked_until_cleared"] is True


def test_a_persistent_block_survives_a_clean_cycle():
    """"Until cleared in edge_control" means by the operator. A clean cycle is
    evidence, not authorisation — the divergence may simply not have recurred
    yet, and self-clearing would hide a fault that alternates."""
    result = run(control={"entries_blocked_until_cleared": True})

    assert result.findings == []
    assert result.control_updates["entries_blocked"] is True
    assert result.control_updates["entries_blocked_until_cleared"] is True


def test_a_clean_cycle_resets_the_streak():
    result = run(control={"divergence_streak": 1})

    assert result.control_updates["divergence_streak"] == 0


def test_an_adopted_trade_and_a_slow_order_are_not_divergence():
    """Both are fully explained: the operator traded, and an order has not
    filled yet. Counting either toward the escalation would block entries for
    a supported workflow and for a working limit order."""
    result = run(
        intent={"placements": [placement()]},
        orders=[broker_order()],
        control={"open_order_cycles": {"a1b2": 1}, "divergence_streak": 1},
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

    assert reconcile.RECONCILIATION_DIVERGENCE_PERSISTENT not in found(result)
    assert result.control_updates["divergence_streak"] == 0


def test_a_terminal_placements_drift_does_not_rediverge_on_a_later_quiet_cycle():
    """An earlier production bug. `edge_prior()` hands back the same placement
    on every quiet cycle until a new one is sent — deliberately, so an order
    that might still be working is not lost between cycles. But nothing had
    ever advanced that pointer once an order went terminal, so a real
    QUANTITY_DRIFT raised once kept being raised, identically, off the same
    historical fact, on every cycle after it — turning one drift into an
    unbounded `divergence_streak` and a permanent
    `RECONCILIATION_DIVERGENCE_PERSISTENT` block with nothing new having
    happened. Retiring the order id once its fate is terminal is what stops
    the second cycle from re-finding the first cycle's fact."""
    same_orders_and_fills = dict(
        intent={"placements": [placement(quantity=1.0)]},
        orders=[broker_order(state="filled")],
        fills=[fill(quantity=0.5)],
        positions=[{"symbol": "F", "quantity": 1.5}],
        tax_lots={
            "F": [
                {"quantity": 1.0, "cost_basis": 15.00, "acquired_date": "2026-07-30"},
                {"quantity": 0.5, "cost_basis": 7.50, "acquired_date": "2026-08-03"},
            ]
        },
    )

    first = run(**same_orders_and_fills)
    assert found(first, reconcile.QUANTITY_DRIFT) == ["F"]
    assert first.control_updates["divergence_streak"] == 1
    assert first.control_updates["retired_order_ids"] == ["a1b2"]

    # Cycle two: edge_prior() returns the identical stale batch (nothing new
    # was placed), carrying forward exactly what cycle one wrote to control.
    second = reconcile.run(
        reconcile_context(control=first.control_updates, **same_orders_and_fills)
    )

    assert reconcile.QUANTITY_DRIFT not in found(second)
    assert reconcile.RECONCILIATION_DIVERGENCE_PERSISTENT not in found(second)
    assert second.control_updates["divergence_streak"] == 0


def test_a_sell_that_filled_since_the_last_cycle_is_not_drift_or_a_delta_mismatch():
    """The runner posts `orders` only; `normalize` derives the fills from their
    executions, and a later cycle (order retired) sees none."""
    from src import broker_shapes

    raw = reconcile_context(
        intent={"placements": [placement(symbol="INVH", side="sell", quantity=7.0)]},
        orders=[],
        positions=[],
        prior_positions=[{"symbol": "INVH", "quantity": 7.0, "play_id": "DIP"}],
    )
    raw["orders"] = [
        {
            "id": placement()["order_id"], "state": "filled", "side": "sell",
            "symbol": "INVH",
            "executions": [{"quantity": "7.0", "price": "26.55",
                            "timestamp": "2026-10-02T13:49:58Z"}],
        }
    ]
    raw.pop("fills", None)

    ctx = broker_shapes.normalize(raw, reconcile.calendar_day)
    result = reconcile.run(ctx)

    assert reconcile.QUANTITY_DRIFT not in found(result)
    assert reconcile.POSITION_DELTA_MISMATCH not in found(result)
