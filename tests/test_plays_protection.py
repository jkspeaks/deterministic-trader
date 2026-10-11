"""§10.1's resting orders: what should be behind every whole-share position.

`plays/protection.py` computes; it never places and never compares against the
broker. The comparison lives in `tests/test_reconcile_protection.py` and the
denials in `tests/test_guard_protection.py`, which is the same three-way split
`plays`/`reconcile`/`guard` already has for everything else.
"""

import pytest

from src import plays
from src.plays import protection
from tests.factories import config, plays_context, position


def book(*positions, last: float | None = None, **overrides) -> dict:
    """`plays_context` with a book in it, and F's last trade where a test needs
    the position to be genuinely above its trailing stop rather than through it.

    The baseline quote is 14.88, which is below every trailing stop a high
    above the entry produces — so a test about the ratchet has to move the
    market too, or it is testing the "stop above the market" branch instead.

    ``protection_rest_kind: "stop"`` is pinned here rather than left to
    `config()`'s shipped default — this file is specifically about what rests,
    so it keeps testing that regardless of which way live config points.
    `test_none_rests_nothing_at_all` below is what covers the shipped default
    itself.
    """
    overrides.setdefault("config", config(protection_rest_kind=protection.STOP))
    ctx = plays_context(**overrides)
    ctx["positions"] = list(positions)
    if last is not None:
        ctx["quotes"]["F"] = {"bid": last - 0.01, "ask": last + 0.01, "last": last}
    return ctx


def only(orders, kind: str):
    matching = [o for o in orders if o.kind == kind]
    assert len(matching) <= 1, f"more than one {kind} for one position"
    return matching[0] if matching else None


# --- the initial state ------------------------------------------------------


def test_a_fresh_position_rests_one_order_and_it_is_the_stop():
    """One end of the trade, not both — `protection.REST_KINDS`.
    """
    result = protection.desired(book(position()))

    assert [o.kind for o in result.orders] == [protection.STOP]
    assert result.notes == []


def test_the_other_leg_rests_when_the_operator_asks_for_it():
    """`protection_rest_kind` is a choice between the two, not a switch that
    turns one off — `"target"` rests the target and no stop."""
    ctx = book(position(), config=config(protection_rest_kind=protection.TARGET))
    result = protection.desired(ctx)

    assert [o.kind for o in result.orders] == [protection.TARGET]
    assert result.notes == []


def test_an_unreadable_rest_kind_falls_back_to_the_stop():
    """`cli._validate_config` raises on a typo before a cycle runs. If one
    reaches here anyway, the stop is the leg whose absence costs money."""
    ctx = book(position(), config=config(protection_rest_kind="both"))

    assert [o.kind for o in protection.desired(ctx).orders] == [protection.STOP]


def test_none_rests_nothing_at_all():
    """The shipped default. Neither leg rests, for any position — §6.5's
    `ATR_STOP`/`TARGET` in `plays/exits.py` are the whole of the protection
    now, evaluated fresh every cycle instead of resting at the broker between
    them."""
    ctx = book(position(), config=config(protection_rest_kind=protection.NONE))

    result = protection.desired(ctx)
    assert result.orders == []
    assert result.notes == []


def test_the_fixed_stop_wins_while_the_position_has_never_been_up():
    """3xATR below a 15.00 entry is 13.65; 3.5xATR below a high that is still
    the entry is 13.425 (13.43 at a cent). `max` keeps the tighter one, which
    is the whole reason the trailing floor cannot loosen an existing stop."""
    stop = only(protection.desired(book(position())).orders, protection.STOP)

    assert stop.stop_price == 13.65
    assert stop.order_type == "stop_market"
    assert stop.limit_price is None


def test_the_target_is_the_plays_own_percentage():
    ctx = book(position(), config=config(protection_rest_kind=protection.TARGET))
    target = only(protection.desired(ctx).orders, protection.TARGET)

    assert target.limit_price == round(15.00 * (1.0 + plays.DIP_TARGET_PCT), 2)
    assert target.order_type == "limit"
    assert target.stop_price is None


def test_a_position_with_no_play_at_all_gets_no_target():
    held = position(play_id=plays.MANUAL, adopted=True)
    assert plays.PLAYS.get(plays.MANUAL) is None

    # No play at all: §12.2 refuses to invent exit terms for a position the
    # system never chose, so there is a stop and no target.
    result = protection.desired(book(held))

    assert [o.kind for o in result.orders] == [protection.STOP]


def test_every_position_gets_a_stop_even_without_a_play():
    """§10.1's stop is global — exit 4 in `exits._reason_to_leave` runs before
    the `play is None` return, and this mirrors it."""
    stop = only(
        protection.desired(book(position(play_id=plays.MANUAL))).orders,
        protection.STOP,
    )

    assert stop.stop_price == 13.65


# --- the trailing ratchet ---------------------------------------------------


def test_a_position_that_has_run_trails_its_stop_from_the_high():
    """The gap named: "fixed from entry; no trailing"."""
    held = position(entry_price=15.00, high_since_entry=20.00)

    stop = only(protection.desired(book(held, last=19.50)).orders, protection.STOP)

    assert stop.stop_price == round(20.00 - 3.5 * 0.45, 2)
    assert stop.stop_price > 15.00  # above the entry: the gain is locked in


def test_the_high_water_price_only_ever_ratchets_the_stop_up():
    lower = only(
        protection.desired(book(position(high_since_entry=18.00), last=19.50)).orders,
        protection.STOP,
    )
    higher = only(
        protection.desired(book(position(high_since_entry=20.00), last=19.50)).orders,
        protection.STOP,
    )

    assert higher.stop_price > lower.stop_price


def test_a_missing_high_falls_closed_to_the_entry_price():
    """A book row written before the field existed must not tighten a stop on
    evidence the system does not have."""
    without = position()
    del without["high_since_entry"]

    assert protection.high_since_entry(without) == 15.00
    assert only(protection.desired(book(without)).orders, protection.STOP).stop_price == 13.65


def test_a_high_below_the_entry_is_read_as_the_entry():
    assert protection.high_since_entry(position(high_since_entry=12.00)) == 15.00


def test_the_acceptance_case_from_the_prompt():
    """5 MGM at 40.78, ATR 0.85, high 43.00.

    The trailing floor is 43.00 − 3.5 × 0.85 = 40.025 against a fixed stop of
    40.78 − 3 × 0.85 = 38.23, so the trailing one wins — and 40.025 rounds to
    40.02 at a cent, which is the direction that tightens rather than loosens.
    """
    ctx = book(
        position(
            symbol="MGM",
            play_id=plays.DIP,
            quantity=5.0,
            entry_price=40.78,
            high_since_entry=43.00,
        )
    )
    ctx["signals"]["MGM"] = {"atr": 0.85}
    ctx["quotes"]["MGM"] = {"bid": 41.95, "ask": 42.05, "last": 42.00}

    orders = protection.desired(ctx).orders

    assert only(orders, protection.STOP).stop_price == 40.02
    assert [o.quantity for o in orders] == [5.0]

    # The target is still computed, and still what §6.5's TARGET exit prices
    # against every cycle — it simply does not rest at the broker.
    resting_target = config(protection_rest_kind="target")
    wanted = protection.desired({**ctx, "config": resting_target})
    assert only(wanted.orders, protection.TARGET).limit_price == round(40.78 * 1.06, 2)


# --- what is refused --------------------------------------------------------


def test_a_fractional_position_gets_nothing_and_says_nothing():
    """§10.1 gives a `FRACTIONAL` position no resting order at all. Silent —
    the fractional book is a standing condition, not this cycle's news."""
    result = protection.desired(book(position(quantity=0.25)))

    assert result.orders == []
    assert result.notes == []


def test_a_missing_atr_is_a_note_rather_than_a_silence():
    ctx = book(position())
    del ctx["signals"]["F"]["atr"]

    result = protection.desired(ctx)

    assert result.orders == []
    assert "no resting stop" in result.notes[0]


def test_a_stop_above_the_market_is_left_to_the_exit_path():
    """A sell stop at or above the market triggers immediately, and a triggered
    `stop_market` is a market order — the shape an earlier limit-only ruling
    removed. §6.5's exit prices the same sale as a limit."""
    ctx = book(position(high_since_entry=20.00))  # trailing stop 18.43
    ctx["quotes"]["F"]["last"] = 18.00

    result = protection.desired(ctx)

    assert result.orders == []
    assert "market sell" in result.notes[0]


def test_a_symbol_the_other_strategy_owns_is_not_this_sides_to_protect():
    ctx = book(position())
    ctx["symbol_lock"] = {"F": "dip_recovery"}

    assert protection.desired(ctx).orders == []


# --- the order shape --------------------------------------------------------


def test_every_resting_order_is_a_gtc_regular_hours_sell():
    """`gtc` is what makes it survive to the next cycle; `regular_hours` is
    what stops a stop order queueing instead of resting."""
    for order in (o.as_order() for o in protection.desired(book(position())).orders):
        assert order["side"] == "sell"
        assert order["time_in_force"] == "gtc"
        assert order["session"] == "regular_hours"


def test_the_two_flags_are_exclusive_and_one_is_always_set():
    stop = [o.as_order() for o in protection.desired(book(position())).orders]
    target = [
        o.as_order()
        for o in protection.desired(
            book(position(), config=config(protection_rest_kind=protection.TARGET))
        ).orders
    ]

    def flags(orders):
        return [(o["protective_stop"], o["protective_target"]) for o in orders]

    assert flags(stop) == [(True, False)]
    assert flags(target) == [(False, True)]


@pytest.mark.parametrize(
    "order,kind",
    [
        ({"side": "sell", "type": "stop_market", "stop_price": 1.0}, protection.STOP),
        ({"side": "sell", "type": "stop_limit", "stop_price": 1.0}, protection.STOP),
        ({"side": "sell", "type": "limit", "time_in_force": "gtc"}, protection.TARGET),
        ({"side": "sell", "type": "limit", "time_in_force": "gfd"}, None),
        ({"side": "sell", "type": "limit"}, None),
        ({"side": "buy", "type": "stop_market"}, None),
    ],
)
def test_a_broker_order_is_recognised_as_protection_by_its_shape(order, kind):
    """The broker returns none of our flags, so anything reading its list has
    to recognise the shape. A day limit sell is an ordinary exit, not cover."""
    assert protection.resting_kind(order) == kind


# --- the no-churn band ------------------------------------------------------


def test_a_cent_of_drift_is_inside_the_band():
    band = float(config()["protection_replace_min_pct"])

    assert protection.within_band(14.10, 14.11, band)


def test_a_real_move_is_outside_the_band():
    band = float(config()["protection_replace_min_pct"])

    assert not protection.within_band(14.10, 13.50, band)


# --- the broker's read-back vocabulary, not the placement one ---------------
#
# `place_equity_order` takes `type: "stop_market"`; `get_equity_orders` hands
# the same order back as `type: "market"` with `trigger: "stop"`. Both fixtures
# below are live: the MGM stop is verbatim from this account (6 shares held, 5
# committed to a resting stop at 39.15), and the limit is the byte-identical F
# order from an earlier P4 probe.
#
# The first version of `resting_kind` matched only the placement word, so it
# recognised nothing the broker actually returns — which would have read the
# MGM stop as an ordinary competing sell (denying any exit on MGM
# `DUPLICATE_OPEN_ORDER`) and placed a *second* stop over a position that
# already had five of its six shares committed.

LIVE_MGM_STOP = {
    "id": "ade7ea63-3310-4c9f-9aea-1363b95c50a6",
    "symbol": "MGM", "side": "sell", "type": "market", "state": "confirmed",
    "quantity": "5.000000", "price": None, "stop_price": "39.150000",
    "time_in_force": "gtc", "market_hours": "regular_hours", "trigger": "stop",
    "placed_agent": "agentic",
}

LIVE_LIMIT_READBACK = {
    "symbol": "F", "side": "sell", "type": "limit", "state": "confirmed",
    "quantity": "1.000000", "price": "2.000000", "stop_price": None,
    "time_in_force": "gtc", "trigger": "immediate",
}


def test_a_live_stop_readback_is_recognised_as_a_stop():
    assert protection.resting_kind(LIVE_MGM_STOP) == protection.STOP


def test_a_live_stop_readback_prices_off_stop_price():
    assert protection.resting_price(LIVE_MGM_STOP) == 39.15


def test_a_live_limit_readback_prices_off_price_not_limit_price():
    """A limit's price is `limit_price` going out and `price` coming back.
    Classifying it correctly and then failing to price it reads downstream as
    "nothing equivalent rests", which places a duplicate."""
    assert protection.resting_kind(LIVE_LIMIT_READBACK) == protection.TARGET
    assert protection.resting_price(LIVE_LIMIT_READBACK) == 2.00


def test_a_plain_market_sell_is_still_not_protection():
    """The recognition widened; it must not have widened to everything. A
    market sell with no stop price and no stop trigger is an ordinary exit."""
    plain = {"symbol": "F", "side": "sell", "type": "market",
             "quantity": "1.000000", "stop_price": None, "trigger": "immediate"}

    assert protection.resting_kind(plain) is None
