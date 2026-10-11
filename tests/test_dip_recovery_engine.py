"""dip_recovery.engine — fills, management (exit/averaging/47-day escape),
and new-entry proposals, in that order.
"""

from __future__ import annotations

from src.dip_recovery import evaluate
from src.dip_recovery.core import (
    AVERAGE_PENDING,
    ENTRIES_DISABLED,
    ENTRY_PENDING,
    EXIT_LOCKED,
    EXIT_PENDING,
    HOLD_NO_NEW_BUYS,
    HOLDING,
    MAX_ACTIVE_POSITIONS,
    MAX_NEW_ENTRIES_PER_DAY,
    SYMBOL_LOCKED,
)
from tests.dip_recovery_factories import dip_recovery_context, fill, held_position, orders, skip_codes


# --- new entries -------------------------------------------------------------


def test_qualifying_watchlist_symbol_is_proposed_as_a_buy():
    result = evaluate(dip_recovery_context())
    assert orders(result, "buy") == ["DIP1"]
    assert result.position_updates["DIP1"]["state"] == ENTRY_PENDING
    assert result.evaluations[0]["passed"] is True


def test_watchlist_symbol_with_no_bars_is_rejected_and_logged():
    result = evaluate(dip_recovery_context(dip_recovery_bars={"DIP1": []}))
    assert orders(result, "buy") == []
    assert result.evaluations[0]["passed"] is False
    assert result.evaluations[0]["rejection_reasons"]


def test_entries_disabled_proposes_nothing_but_still_logs_nothing_pointless():
    result = evaluate(
        dip_recovery_context(dip_recovery_config={"dip_recovery_enabled": False})
    )
    assert orders(result) == []
    assert ENTRIES_DISABLED in skip_codes(result, "DIP1")
    # A disabled strategy generates no evaluation-log row at all for a
    # candidate it was never going to score — see `engine.py`'s docstring.
    assert result.evaluations == []


def test_whole_shares_only_refuses_a_slot_that_cannot_afford_one_share():
    # DIP1 asks 46.00; equity 5000 reserves 101.00, leaving a $149 balance
    # that splits 5 ways into a $29.80 slot -- below one share of DIP1.
    result = evaluate(
        dip_recovery_context(account={"buying_power": 250.00, "settled_cash": 250.00})
    )
    assert orders(result, "buy") == []
    assert "WHOLE_SHARE_ONLY" in skip_codes(result, "DIP1")


def test_whole_shares_only_off_still_proposes_a_fractional_buy():
    result = evaluate(
        dip_recovery_context(
            account={"buying_power": 250.00, "settled_cash": 250.00},
            config={"whole_shares_only": False},
        )
    )
    assert orders(result, "buy") == ["DIP1"]


def test_symbol_already_tracked_is_not_reproposed():
    result = evaluate(
        dip_recovery_context(dip_recovery_positions={"DIP1": held_position()})
    )
    assert "DIP1" not in orders(result, "buy")


def test_symbol_locked_to_the_other_side_is_skipped():
    result = evaluate(dip_recovery_context(symbol_lock={"DIP1": "core"}))
    assert SYMBOL_LOCKED in skip_codes(result, "DIP1")
    assert orders(result) == []


def test_max_active_positions_cap():
    positions = {
        f"HELD{i}": held_position(symbol=f"HELD{i}") for i in range(5)
    }
    result = evaluate(dip_recovery_context(dip_recovery_positions=positions))
    assert MAX_ACTIVE_POSITIONS in skip_codes(result, "DIP1")


def test_max_new_entries_per_day_cap():
    result = evaluate(
        dip_recovery_context(control={"dip_recovery_new_positions": {"date": "2026-08-31", "count": 3}})
    )
    assert MAX_NEW_ENTRIES_PER_DAY in skip_codes(result, "DIP1")


def test_approved_entry_advances_the_daily_counter():
    result = evaluate(dip_recovery_context())
    assert result.control_updates["dip_recovery_new_positions"] == {
        "date": "2026-08-31", "count": 1,
    }


# --- fills --------------------------------------------------------------


def test_buy_fill_moves_entry_pending_to_holding_and_sets_cost_basis():
    ctx = dip_recovery_context(
        dip_recovery_positions={
            "DIP1": {**held_position(), "state": ENTRY_PENDING, "total_quantity": 0, "total_fills": 0,
                     "weighted_avg_cost": None, "fills": []}
        },
        dip_recovery_watchlist=[],
        dip_recovery_fills=[fill(price=46.00, quantity=10.0)],
    )
    result = evaluate(ctx)
    row = result.position_updates["DIP1"]
    assert row["state"] == HOLDING
    assert row["weighted_avg_cost"] == 46.00
    assert row["total_quantity"] == 10.0
    assert result.symbol_lock_claims == ["DIP1"]


def test_average_down_fill_recomputes_weighted_average_cost():
    ctx = dip_recovery_context(
        dip_recovery_positions={"DIP1": held_position()},
        dip_recovery_watchlist=[],
        dip_recovery_fills=[fill(price=44.00, quantity=10.0, trading_day="2026-08-31")],
    )
    result = evaluate(ctx)
    row = result.position_updates["DIP1"]
    # (10 * 46.00 + 10 * 44.00) / 20 = 45.00
    assert row["weighted_avg_cost"] == 45.00
    assert row["total_quantity"] == 20.0
    assert row["total_fills"] == 2
    assert row["clock_trading_day"] == "2026-08-31"


def test_sell_fill_closes_the_position_and_releases_the_lock():
    ctx = dip_recovery_context(
        dip_recovery_positions={"DIP1": held_position()},
        dip_recovery_watchlist=[],
        dip_recovery_fills=[fill(side="sell", price=48.50, quantity=10.0)],
    )
    result = evaluate(ctx)
    assert result.closed == ["DIP1"]
    assert result.symbol_lock_releases == ["DIP1"]
    assert "DIP1" not in result.position_updates


# --- exits ----------------------------------------------------------------


def test_target_and_t1_clear_proposes_the_sell():
    # Weighted_avg_cost 46.00 -> target 46.00 * 1.0525 = 48.415; T+1 clears
    # two sessions after an earlier fill.
    ctx = dip_recovery_context(
        dip_recovery_positions={"DIP1": held_position()},
        dip_recovery_watchlist=[],
        quotes={"DIP1": {"bid": 48.50, "ask": 48.55, "last": 48.50}},
    )
    result = evaluate(ctx)
    assert orders(result, "sell") == ["DIP1"]
    assert result.position_updates["DIP1"]["state"] == EXIT_PENDING


def test_target_met_but_t1_not_clear_locks_without_placing():
    ctx = dip_recovery_context(
        dip_recovery_positions={
            "DIP1": held_position(clock_trading_day="2026-08-31", opened_trading_day="2026-08-31")
        },
        dip_recovery_watchlist=[],
        quotes={"DIP1": {"bid": 48.50, "ask": 48.55, "last": 48.50}},
    )
    result = evaluate(ctx)
    assert orders(result) == []
    assert result.position_updates["DIP1"]["state"] == EXIT_LOCKED


def test_below_target_holds_and_may_still_average_down():
    ctx = dip_recovery_context(
        dip_recovery_positions={"DIP1": held_position()},
        dip_recovery_watchlist=[],
        quotes={"DIP1": {"bid": 44.50, "ask": 44.55, "last": 44.50}},
        dip_recovery_config={"dip_recovery_enabled": True},
    )
    result = evaluate(ctx)
    assert orders(result, "sell") == []
    # -3.26% unrealized (below the -3.0% trigger), 3.26% below the 46.00
    # latest fill (above the 2.5% floor), and the fill itself is a week old
    # (comfortably above the 30-minute floor).
    assert orders(result, "buy") == ["DIP1"]
    assert result.position_updates["DIP1"]["state"] == AVERAGE_PENDING


# --- averaging down ---------------------------------------------------------


def test_too_many_fills_stops_averaging():
    ctx = dip_recovery_context(
        dip_recovery_positions={"DIP1": held_position(total_fills=4)},
        dip_recovery_watchlist=[],
        quotes={"DIP1": {"bid": 44.50, "ask": 44.55, "last": 44.50}},
    )
    result = evaluate(ctx)
    assert orders(result, "buy") == []
    assert result.position_updates["DIP1"]["state"] == HOLDING


def test_not_underwater_enough_stops_averaging():
    ctx = dip_recovery_context(
        dip_recovery_positions={"DIP1": held_position()},
        dip_recovery_watchlist=[],
        quotes={"DIP1": {"bid": 45.50, "ask": 45.55, "last": 45.50}},
    )
    result = evaluate(ctx)
    assert orders(result, "buy") == []


def test_too_soon_since_fill_stops_averaging():
    ctx = dip_recovery_context(
        dip_recovery_positions={"DIP1": held_position(latest_fill_at="2026-08-31T15:40:00Z")},
        dip_recovery_watchlist=[],
        quotes={"DIP1": {"bid": 44.50, "ask": 44.55, "last": 44.50}},
    )
    result = evaluate(ctx)
    assert orders(result, "buy") == []


def test_averaging_is_not_offered_while_entries_are_disabled():
    ctx = dip_recovery_context(
        dip_recovery_positions={"DIP1": held_position()},
        dip_recovery_watchlist=[],
        quotes={"DIP1": {"bid": 44.50, "ask": 44.55, "last": 44.50}},
        dip_recovery_config={"dip_recovery_enabled": False},
    )
    result = evaluate(ctx)
    assert orders(result, "buy") == []
    assert result.position_updates["DIP1"]["state"] == HOLDING


# --- the 47-trading-day escape ----------------------------------------------


def test_stalled_position_moves_to_hold_no_new_buys_and_flags_review():
    ctx = dip_recovery_context(
        dip_recovery_positions={"DIP1": held_position(clock_trading_day="2026-06-01")},
        dip_recovery_watchlist=[],
        quotes={"DIP1": {"bid": 44.50, "ask": 44.55, "last": 44.50}},
    )
    result = evaluate(ctx)
    assert result.position_updates["DIP1"]["state"] == HOLD_NO_NEW_BUYS
    assert result.manual_review[0].symbol == "DIP1"
    assert orders(result, "buy") == []


def test_hold_no_new_buys_still_exits_once_the_target_clears():
    ctx = dip_recovery_context(
        dip_recovery_positions={
            "DIP1": held_position(state=HOLD_NO_NEW_BUYS, clock_trading_day="2026-06-01")
        },
        dip_recovery_watchlist=[],
        quotes={"DIP1": {"bid": 48.50, "ask": 48.55, "last": 48.50}},
    )
    result = evaluate(ctx)
    assert orders(result, "sell") == ["DIP1"]
    assert result.position_updates["DIP1"]["state"] == EXIT_PENDING
