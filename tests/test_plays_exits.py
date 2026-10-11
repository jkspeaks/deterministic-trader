"""§6.5 exits, in priority order.

"Exits are evaluated on every cycle, including overnight cycles. Entries are
not." All seven live here because guard denies and never emits — guard's own
docstring turns on that division, making the drawdown breach an order denial
rather than a halt precisely so the flatten it demands can still be placed.

Exits 2 and 3 therefore appear twice in the system and are not duplicated: this
module emits the sell, guard refuses the buy. They are the two halves of one
rule and they read the same numbers, which is why the drawdown floor lives in
`sizing.py` where both can import it.
"""

import pytest

from src import plays
from tests.factories import config, exited, plays_context, position


def holding(**overrides) -> dict:
    """A cycle holding one clean F position."""
    ctx = plays_context()
    ctx["positions"] = [position(**overrides)]
    return ctx


def test_a_healthy_position_is_held():
    assert plays.exits(holding()).exits == []


# --- 1. hard event exit ---------------------------------------------------


def test_a_restatement_on_a_held_name_forces_the_exit():
    ctx = holding()
    ctx["events"]["F"] = [{"type": "8-K", "items": ["4.02"], "sessions_ago": 0}]

    assert exited(plays.exits(ctx), "F").code == plays.HARD_EVENT_EXIT


def test_an_officer_departure_no_longer_forces_the_exit():
    """5.02 was dropped from `FORCE_EXIT_ITEMS`. CI reads the 8-K item *code* out
    of EDGAR's Atom summary, not the officer's role, so a routine director
    election read the same as a CEO/CFO departure. A held name's 5.02 is a
    note now, not a sale.
    """
    ctx = holding()
    ctx["events"]["F"] = [{"type": "8-K", "items": ["5.02"], "sessions_ago": 0}]
    result = plays.exits(ctx)

    assert result.exits == []
    assert any("item 5.02" in note for note in result.notes)


def test_a_routine_filing_forces_nothing():
    ctx = holding()
    ctx["events"]["F"] = [{"type": "8-K", "items": ["2.02", "9.01"], "sessions_ago": 0}]

    assert plays.exits(ctx).exits == []


# --- 2. drawdown ----------------------------------------------------------


def test_a_drawdown_breach_flattens_the_book():
    ctx = holding()
    ctx["control"]["high_water_mark"] = 400.00  # floor 320.00, equity 236.42

    assert exited(plays.exits(ctx), "F").code == plays.DRAWDOWN_FLATTEN


def test_an_unreadable_equity_does_not_flatten_the_book():
    """An absent/zero/non-numeric `equity_total` became a large negative flow-adjusted figure
    and DRAWDOWN_FLATTEN took the whole book. §10 is now *not evaluated* on that read —
    guard halts the cycle with ACCOUNT_UNREADABLE — and a note records that the flatten was
    skipped for a data reason, not because the book was healthy."""
    for bad in ({}, {"equity_total": None}, {"equity_total": 0}, {"equity_total": "abc"}):
        ctx = holding()
        ctx["control"]["high_water_mark"] = 400.00  # a real breach at any normal equity
        ctx["account"] = bad

        result = plays.exits(ctx)

        assert [e.code for e in result.exits] == [], bad
        assert any("DRAWDOWN_FLATTEN" in note for note in result.notes), bad


def test_a_readable_equity_writes_no_such_note():
    ctx = holding()
    ctx["control"]["high_water_mark"] = 400.00

    assert plays.exits(ctx).notes == []


def test_the_flatten_uses_the_same_floor_guard_denies_against():
    """§10's number, defined once in `sizing.py`. Two arithmetics would let
    plays flatten at a level guard still considered safe."""
    ctx = holding()
    ctx["control"]["high_water_mark"] = 295.525  # floor is exactly 236.42

    assert plays.exits(ctx).exits == []


def test_a_cent_under_the_floor_does_flatten():
    ctx = holding()
    ctx["control"]["high_water_mark"] = 295.525
    ctx["account"]["equity_total"] = 236.41

    assert exited(plays.exits(ctx), "F").code == plays.DRAWDOWN_FLATTEN


def test_an_operator_hold_overrides_the_drawdown_flatten():
    """The one override the operator has, per name — set by hand in
    `edge_control.operator_holds`, nothing in this codebase writes it."""
    ctx = holding()
    ctx["control"]["high_water_mark"] = 400.00  # would otherwise flatten
    ctx["control"]["operator_holds"] = {"F": {"reason": "holding through"}}

    assert plays.exits(ctx).exits == []


def test_an_implausible_equity_does_not_flatten_the_book():
    """An equity read that fails the plausibility cross-check must not flatten
    the book either, for the same reason an unreadable one does not —
    `guard.ACCOUNT_IMPLAUSIBLE` denies the buy side; this is the exit side of
    the same suppression."""
    ctx = holding(market_value=999.00)
    ctx["control"]["high_water_mark"] = 400.00  # would otherwise flatten
    ctx["account"]["equity_total"] = 236.42  # below the one position's value

    result = plays.exits(ctx)

    assert [e.code for e in result.exits] == []
    assert any("DRAWDOWN_FLATTEN" in note for note in result.notes)


def test_an_operator_hold_on_a_different_symbol_does_not_protect_this_one():
    ctx = holding()
    ctx["control"]["high_water_mark"] = 400.00
    ctx["control"]["operator_holds"] = {"NOT_F": {"reason": "unrelated"}}

    assert exited(plays.exits(ctx), "F").code == plays.DRAWDOWN_FLATTEN


def test_an_operator_hold_does_not_override_a_hard_event_exit():
    """Scoped to the drawdown rule alone — a forced 8-K exit and the earnings
    deadline below are both "the system never holds through X" commitments,
    not a risk call the operator's own read can outweigh."""
    ctx = holding()
    ctx["control"]["high_water_mark"] = 400.00
    ctx["control"]["operator_holds"] = {"F": {"reason": "holding through"}}
    ctx["events"]["F"] = [{"type": "8-K", "items": ["4.02"], "sessions_ago": 0}]

    assert exited(plays.exits(ctx), "F").code == plays.HARD_EVENT_EXIT


def test_an_operator_hold_does_not_override_the_earnings_deadline():
    ctx = holding()
    ctx["control"]["high_water_mark"] = 400.00
    ctx["control"]["operator_holds"] = {"F": {"reason": "holding through"}}
    ctx["earnings"]["F"]["sessions_until_report"] = 1

    assert exited(plays.exits(ctx), "F").code == plays.EARNINGS_DEADLINE


# --- 3. earnings deadline -------------------------------------------------


def test_a_confirmed_report_two_sessions_out_flattens():
    """"The system never holds through an earnings print." At these position
    sizes no sizing rule protects against a 25% overnight gap."""
    ctx = holding()
    ctx["earnings"]["F"]["sessions_until_report"] = 2

    assert exited(plays.exits(ctx), "F").code == plays.EARNINGS_DEADLINE


def test_three_sessions_out_is_not_yet_the_deadline():
    ctx = holding()
    ctx["earnings"]["F"]["sessions_until_report"] = 3

    assert plays.exits(ctx).exits == []


def test_an_unknown_earnings_date_does_not_flatten_a_held_position():
    """The asymmetry with guard is deliberate. Guard denies a *buy* it cannot
    promise to exit in time; forcing a sale on missing data would liquidate the
    book every time the earnings feed hiccuped."""
    ctx = holding()
    ctx["earnings"]["F"] = {"status": "unknown"}

    assert plays.exits(ctx).exits == []


# --- 3.5 same-day-fill guard -----------------------------------------------


def test_a_same_day_fill_holds_off_an_atr_stop():
    """A position that also picked up a fill today has not had a trajectory yet
    for a stop to be evidence about."""
    ctx = holding(last_fill_on="2026-08-03")  # plays_context's trading_day
    ctx["quotes"]["F"]["last"] = 14.09  # would otherwise be ATR_STOP

    assert plays.exits(ctx).exits == []


def test_a_same_day_fill_holds_off_a_time_stop_too():
    ctx = holding(last_fill_on="2026-08-03", sessions_held=8)

    assert plays.exits(ctx).exits == []


def test_a_fill_from_an_earlier_session_does_not_gate_anything():
    ctx = holding(last_fill_on="2026-08-01")
    ctx["quotes"]["F"]["last"] = 13.64

    assert exited(plays.exits(ctx), "F").code == plays.ATR_STOP


def test_the_same_day_guard_does_not_override_a_hard_event_exit():
    """Scoped exactly as the operator-hold override is (item 2's own test
    above): a forced 8-K exit and the earnings deadline are "never wait"
    commitments, not a trajectory judgment this guard has any business
    overriding."""
    ctx = holding(last_fill_on="2026-08-03")
    ctx["events"]["F"] = [{"type": "8-K", "items": ["4.02"], "sessions_ago": 0}]

    assert exited(plays.exits(ctx), "F").code == plays.HARD_EVENT_EXIT


def test_the_same_day_guard_does_not_override_the_drawdown_flatten():
    ctx = holding(last_fill_on="2026-08-03")
    ctx["control"]["high_water_mark"] = 400.00

    assert exited(plays.exits(ctx), "F").code == plays.DRAWDOWN_FLATTEN


def test_the_same_day_guard_does_not_override_the_earnings_deadline():
    ctx = holding(last_fill_on="2026-08-03")
    ctx["earnings"]["F"]["sessions_until_report"] = 2

    assert exited(plays.exits(ctx), "F").code == plays.EARNINGS_DEADLINE


# --- 4. the ATR stop ------------------------------------------------------


def test_three_atr_below_entry_stops_out():
    ctx = holding()
    ctx["quotes"]["F"]["last"] = 13.64  # entry 15.00, ATR 0.45, stop 13.65

    assert exited(plays.exits(ctx), "F").code == plays.ATR_STOP


def test_exactly_at_the_stop_stops_out():
    ctx = holding()
    ctx["quotes"]["F"]["last"] = 13.65

    assert exited(plays.exits(ctx), "F").code == plays.ATR_STOP


def test_the_stop_multiple_is_config():
    """§10 fixes it at `config.atr_stop_multiple` (3xATR shipped), and a
    position that is comfortably inside that stop is outside a tighter one.
    Tightening to 0.2 puts the stop at 14.91, just above the 14.88 last."""
    ctx = holding()
    ctx["config"] = config(atr_stop_multiple=0.2)

    assert exited(plays.exits(ctx), "F").code == plays.ATR_STOP


def test_a_position_with_no_atr_reading_keeps_its_other_exits():
    """§10.1 is blunt that a fractional position has no continuous protection.
    A missing ATR must not therefore suppress the time stop as well — the
    alternative to one exit is the others, not none."""
    ctx = holding(sessions_held=8)
    del ctx["signals"]["F"]["atr"]

    assert exited(plays.exits(ctx), "F").code == plays.TIME_STOP


# --- 5, 6, 7: the play's own terms ----------------------------------------


def test_the_time_stop_fires_at_the_plays_declared_horizon():
    assert exited(plays.exits(holding(sessions_held=8)), "F").code == plays.TIME_STOP


def test_a_session_short_of_the_horizon_holds():
    assert plays.exits(holding(sessions_held=7)).exits == []


def test_each_play_carries_its_own_horizon():
    """CATALYST is seven sessions and MACRO fifteen, so the same age is an exit
    for one and a hold for the other."""
    catalyst = holding(play_id="CATALYST", sessions_held=7)
    macro = holding(play_id="MACRO", sessions_held=7)

    assert exited(plays.exits(catalyst), "F").code == plays.TIME_STOP
    assert plays.exits(macro).exits == []


def test_the_target_takes_the_profit():
    ctx = holding(play_id="PEAD", entry_price=13.00)  # +8% is 14.04
    ctx["earnings"]["F"]["sessions_until_report"] = 60

    assert exited(plays.exits(ctx), "F").code == plays.TARGET


def test_dip_now_takes_a_target_instead_of_riding_the_round_trip():
    """This test asserted the opposite until then: DIP declared no target, so a
    position up fourteenfold was held until RSI happened to reach 50 — and a
    dip that recovered without getting there gave the whole move back. 6% is
    DIP's own number now, and Invariant 3 still forbids inventing one at the
    exit: it comes from `PLAYS`, the same place every other target does.
    """
    ctx = holding(entry_price=1.00)

    assert exited(plays.exits(ctx), "F").code == plays.TARGET


def test_a_recovered_dip_exits_on_its_signal():
    ctx = holding()
    ctx["signals"]["F"]["rsi"] = 50.0

    assert exited(plays.exits(ctx), "F").code == plays.SIGNAL_EXIT


def test_a_dip_still_oversold_holds():
    ctx = holding()
    ctx["signals"]["F"]["rsi"] = 49.9

    assert plays.exits(ctx).exits == []


def test_a_flipped_regime_exits_a_macro_position():
    """§6.4's third exit. The agent declaring MACRO dark *is* the regime flip —
    §6 makes regime the one judgment that is the agent's."""
    ctx = holding(play_id="MACRO")
    ctx["play_state"]["MACRO"] = {"regime_fit": 0.0, "reason": "curve re-steepened"}

    assert exited(plays.exits(ctx), "F").code == plays.SIGNAL_EXIT


def test_an_unwritten_regime_row_does_not_flip_a_position_out():
    """The asymmetry the operator settled: regime fails *closed* for entries
    and open for exits. A premarket cycle that failed to run must not liquidate
    every macro position in the book."""
    ctx = holding(play_id="MACRO")
    del ctx["play_state"]["MACRO"]

    assert plays.exits(ctx).exits == []


# --- adopted positions ----------------------------------------------------


def test_an_adopted_trade_takes_the_play_independent_exits():
    """§12.2: "managed on identical terms from that cycle onward: same stops,
    same limits, same exits"."""
    ctx = holding(play_id=plays.MANUAL)
    ctx["quotes"]["F"]["last"] = 13.64

    assert exited(plays.exits(ctx), "F").code == plays.ATR_STOP


def test_an_adopted_trade_has_no_time_stop_because_it_has_no_play():
    """The honest reading of §12.2. A time stop and a target are play property,
    and inventing one for a position the operator opened would be the system
    deciding how long the operator meant to hold it."""
    assert plays.exits(holding(play_id=plays.MANUAL, sessions_held=90)).exits == []


def test_an_adopted_trade_is_still_flattened_before_a_print():
    ctx = holding(play_id=plays.MANUAL)
    ctx["earnings"]["F"]["sessions_until_report"] = 1

    assert exited(plays.exits(ctx), "F").code == plays.EARNINGS_DEADLINE


# --- priority -------------------------------------------------------------


def test_the_highest_priority_reason_is_the_one_recorded():
    """Everything fires at once. §6.5 is a priority list, so the position exits
    once and the record names the reason that mattered most."""
    ctx = holding(sessions_held=20)
    ctx["events"]["F"] = [{"type": "8-K", "items": ["4.02"], "sessions_ago": 0}]
    ctx["control"]["high_water_mark"] = 400.00
    ctx["earnings"]["F"]["sessions_until_report"] = 1
    ctx["quotes"]["F"]["last"] = 10.00

    leaving = plays.exits(ctx).exits

    assert [e.code for e in leaving] == [plays.HARD_EVENT_EXIT]
    assert leaving[0].priority == 1


def test_drawdown_outranks_the_earnings_deadline():
    ctx = holding()
    ctx["control"]["high_water_mark"] = 400.00
    ctx["earnings"]["F"]["sessions_until_report"] = 1

    assert exited(plays.exits(ctx), "F").priority == 2


def test_the_earnings_deadline_outranks_the_stop():
    ctx = holding()
    ctx["earnings"]["F"]["sessions_until_report"] = 1
    ctx["quotes"]["F"]["last"] = 10.00

    assert exited(plays.exits(ctx), "F").code == plays.EARNINGS_DEADLINE


def test_the_stop_outranks_the_time_stop():
    ctx = holding(sessions_held=20)
    ctx["quotes"]["F"]["last"] = 10.00

    assert exited(plays.exits(ctx), "F").code == plays.ATR_STOP


def test_exits_come_out_in_priority_order():
    ctx = plays_context()
    ctx["positions"] = [
        position(symbol="F", sessions_held=20),          # 5, time stop
        position(symbol="PFE", play_id="PEAD", sessions_held=1),  # 3, earnings
    ]
    ctx["signals"]["PFE"] = {"rsi": 55.0, "atr": 1.0}
    ctx["quotes"]["PFE"] = {"bid": 24.97, "ask": 25.03, "last": 25.00}
    ctx["earnings"]["PFE"] = {"status": "known", "sessions_until_report": 1}

    assert [e.symbol for e in plays.exits(ctx).exits] == ["PFE", "F"]


# --- the order that comes out ---------------------------------------------


def test_an_exit_sells_the_whole_position():
    ctx = holding(quantity=3.0, sessions_held=8)

    assert exited(plays.exits(ctx), "F").quantity == 3.0


def test_a_regular_hours_exit_is_a_limit_at_the_bid():
    """Every exit is a limit order, in every session — there is no market path
    any more, regular hours included."""
    leaving = exited(plays.exits(holding(sessions_held=8)), "F")

    assert (leaving.order_type, leaving.session) == ("limit", "regular_hours")
    assert leaving.limit_price == pytest.approx(14.85)


def test_an_overnight_exit_is_a_limit_at_the_bid():
    """A sale transacts at the bid, in any session — regular hours included;
    this is the case that motivated it first, since guard has always
    refused a market order outside regular hours."""
    ctx = holding(sessions_held=8)
    ctx["cycle"] = {"cycle_type": "overnight", "session": "all_day_hours",
                    "now": "2026-08-04T00:30:00Z", "entries_allowed": False}

    leaving = exited(plays.exits(ctx), "F")

    assert (leaving.order_type, leaving.session) == ("limit", "all_day_hours")
    assert leaving.limit_price == pytest.approx(14.85)


def test_a_fractional_exit_in_regular_hours_is_a_market_sell():
    """A fractional lot can't carry a limit, so it used to land in
    `manual_required` — turning every stop on the fractional book into an
    alert. The broker allows a fractional *market* sell in regular hours, so
    that is what it places now."""
    ctx = holding(quantity=0.4, sessions_held=8)

    result = plays.exits(ctx)
    leaving = exited(result, "F")

    assert result.manual_required == []
    assert (leaving.order_type, leaving.limit_price) == ("market", None)
    assert leaving.session == "regular_hours"
    assert leaving.quantity == 0.4


def test_a_fractional_exit_outside_regular_hours_still_needs_a_hand():
    """A fractional market order only queues to the next open outside regular
    hours, so a hand-placed limit at the open stays the honest fallback."""
    ctx = holding(quantity=0.4, sessions_held=8)
    ctx["cycle"] = {"cycle_type": "overnight", "session": "all_day_hours",
                    "now": "2026-08-04T00:30:00Z", "entries_allowed": False}

    result = plays.exits(ctx)

    assert result.exits == []
    assert [m.symbol for m in result.manual_required] == ["F"]


def test_fractional_exit_market_ok_false_routes_to_manual():
    ctx = holding(quantity=0.4, sessions_held=8)
    ctx["config"] = config(fractional_exit_market_ok=False)

    result = plays.exits(ctx)

    assert result.exits == []
    assert [m.code for m in result.manual_required] == [plays.TIME_STOP]


def test_a_fractional_exit_with_no_bid_still_needs_a_hand():
    ctx = holding(quantity=0.4, sessions_held=8)
    ctx["quotes"]["F"].pop("bid")

    result = plays.exits(ctx)

    assert result.exits == []
    assert [m.symbol for m in result.manual_required] == ["F"]


def test_a_whole_share_exit_is_never_a_market_order():
    """The market path is the fractional book's alone — a whole position is a
    limit at the bid in every session, with no market fallback."""
    leaving = exited(plays.exits(holding(quantity=2.0, sessions_held=8)), "F")

    assert leaving.order_type == "limit"


def test_exits_run_on_a_cycle_that_places_no_entries():
    """§6.5's opening line. The overnight cycle exists to catch a post-close
    earnings reaction, and a module that gated exits on `entries_allowed` would
    make it pointless."""
    ctx = holding(sessions_held=8)
    ctx["cycle"]["entries_allowed"] = False

    assert exited(plays.exits(ctx), "F") is not None


def test_the_reason_names_the_numbers_that_produced_it():
    ctx = holding()
    ctx["quotes"]["F"]["last"] = 13.50

    assert "13.65" in exited(plays.exits(ctx), "F").reason


# --- 8. what a resting broker order already covers -------------------------
#
# §6.5 keeps being evaluated on every cycle after an earlier resting orders,
# deliberately — a resting order can be cancelled by the operator, rejected at
# placement, or never have reached the broker at all, and a system that stopped
# checking would learn about any of those the next time it read a position that
# should not still be there. What changes is only whether the exit is *placed*.


def resting(kind="stop", price=14.10, quantity=1.0, **overrides) -> dict:
    stop = kind == "stop"
    return {
        "order_id": "p1",
        "symbol": "F",
        "side": "sell",
        "type": "stop_market" if stop else "limit",
        "quantity": quantity,
        "stop_price": price if stop else None,
        "limit_price": None if stop else price,
        "time_in_force": "gtc",
        "state": "confirmed",
        **overrides,
    }


def through_its_stop(**overrides) -> dict:
    """F at 10.00 against a 14.10 stop — §6.5 exit 4, unambiguously."""
    ctx = holding(**overrides)
    ctx["quotes"]["F"] = {"bid": 9.98, "ask": 10.02, "last": 10.00}
    return ctx


def at_its_target(**overrides) -> dict:
    """F at 16.00 against DIP's 15.90 — §6.5 exit 6."""
    ctx = holding(**overrides)
    ctx["quotes"]["F"] = {"bid": 15.98, "ask": 16.02, "last": 16.00}
    return ctx


def test_an_atr_stop_behind_a_resting_stop_is_not_placed_again():
    ctx = through_its_stop()
    ctx["open_orders"] = [resting()]

    result = plays.exits(ctx)

    assert result.exits == []
    assert [c.code for c in result.covered_by_resting] == [plays.ATR_STOP]
    assert result.covered_by_resting[0].order_id == "p1"


def test_a_target_behind_a_resting_limit_is_not_placed_again():
    ctx = at_its_target()
    ctx["open_orders"] = [resting(kind="target", price=15.90)]

    result = plays.exits(ctx)

    assert result.exits == []
    assert [c.code for c in result.covered_by_resting] == [plays.TARGET]


def test_an_unprotected_position_still_places_its_exit():
    """The belt, with no braces. Nothing rests, so the sale is this cycle's."""
    result = plays.exits(through_its_stop())

    assert exited(result, "F").code == plays.ATR_STOP
    assert result.covered_by_resting == []


def test_a_resting_stop_below_the_computed_one_is_not_cover():
    """It has not triggered, and `reconcile._protection_plan` is about to
    replace it — until then the position genuinely needs selling."""
    ctx = through_its_stop()
    ctx["open_orders"] = [resting(price=8.00)]

    assert exited(plays.exits(ctx), "F").code == plays.ATR_STOP


def test_a_resting_order_for_part_of_the_position_is_not_cover():
    """One share of three covers a third of the position and none of the exit."""
    ctx = through_its_stop(quantity=3.0)
    ctx["open_orders"] = [resting(quantity=1.0)]

    assert exited(plays.exits(ctx), "F").code == plays.ATR_STOP


def test_a_time_stop_is_never_covered_by_anything_resting():
    """No order type at this broker has a clock in it."""
    ctx = holding(sessions_held=8)
    ctx["open_orders"] = [resting(), resting(kind="target", price=15.90)]

    assert exited(plays.exits(ctx), "F").code == plays.TIME_STOP


def test_an_earnings_deadline_is_never_covered_either():
    """Exits 1, 2 and 3 sell *now*, at whatever the bid is. A resting order
    priced for a different reason is not cover for any of them."""
    ctx = through_its_stop()
    ctx["open_orders"] = [resting()]
    ctx["earnings"]["F"]["sessions_until_report"] = 1

    assert exited(plays.exits(ctx), "F").code == plays.EARNINGS_DEADLINE


def test_a_covered_exit_is_reported_covered_not_placed():
    """There is no placement to make when the sale is already resting at the
    broker — the rule still fires and is recorded as `covered_by_resting`."""
    ctx = through_its_stop()
    ctx["open_orders"] = [resting()]

    result = plays.exits(ctx)

    assert result.exits == []
    assert [c.code for c in result.covered_by_resting] == [plays.ATR_STOP]
