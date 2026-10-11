"""Per-symbol eligibility gates: earnings, wash sale, broker floor, duplicates.

Design v3 §6.5 (exit priority 3), §9 (wash-sale ledger), §4.4 (min order
notional), §14 (earnings date unavailable, blind retry).

The shape shared by all four: they block *entries* and never block exits. An
eligibility rule that blocked its own flatten would hold the position through
exactly the event the rule exists to avoid.
"""

from src import guard
from tests import factories
from tests.factories import codes, context, order

# This is the live case the runbook is built around.
AMD_REPORTS_TOMORROW = {
    "universe": {"candidates": ["AMD", "F"]},
    "quotes": {"AMD": {"bid": 470.00, "ask": 472.15, "last": 476.24}},
    "earnings": {
        "AMD": {
            "status": "known",
            "next_report_date": "2026-08-04",
            "sessions_until_report": 1,
        }
    },
    "sectors": {"AMD": "Technology"},
    "tradability": {
        "AMD": {
            "tradeable": True,
            "fractional_tradability": "tradable",
            "all_day_tradability": "tradable",
        }
    },
    "positions": [
        {
            "symbol": "AMD",
            "quantity": 0.043408,
            "market_value": 20.67,
            "play_id": "MANUAL",
        }
    ],
}


def _amd_context(**overrides):
    ctx = context(**AMD_REPORTS_TOMORROW)
    ctx["account"].update({"settled_cash": 500.00, "buying_power": 500.00})
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(ctx.get(key), dict):
            ctx[key].update(value)
        else:
            ctx[key] = value
    return ctx


# --- earnings -------------------------------------------------------------


def test_a_buy_inside_the_earnings_window_is_denied():
    result = guard.evaluate([order(symbol="AMD", limit_price=480.00)], _amd_context())

    assert guard.EARNINGS_WINDOW in codes(result, "AMD")


def test_the_earnings_flatten_itself_is_never_blocked():
    # §6.5: "The system never holds through an earnings print."
    sell = order(symbol="AMD", side="sell", type="market", limit_price=None,
                 quantity=0.043408)

    result = guard.evaluate([sell], _amd_context())

    assert result.approved == [sell]


def test_a_buy_outside_the_earnings_window_is_approved():
    ctx = _amd_context()
    ctx["earnings"]["AMD"]["sessions_until_report"] = 3

    result = guard.evaluate([order(symbol="AMD", limit_price=480.00)], ctx)

    assert guard.EARNINGS_WINDOW not in codes(result, "AMD")


def test_the_earnings_window_boundary_is_inclusive_at_t_minus_2():
    # §6.5 flattens *at* T-2, so T-2 is already inside the window for entries.
    ctx = _amd_context()
    ctx["earnings"]["AMD"]["sessions_until_report"] = 2

    result = guard.evaluate([order(symbol="AMD", limit_price=480.00)], ctx)

    assert guard.EARNINGS_WINDOW in codes(result, "AMD")


def test_an_unavailable_earnings_date_blocks_the_entry():
    # §14: "T-2 is a promise that cannot be kept blind."
    ctx = _amd_context()
    ctx["earnings"]["AMD"] = {"status": "unavailable"}

    result = guard.evaluate([order(symbol="AMD", limit_price=480.00)], ctx)

    assert guard.EARNINGS_DATE_UNAVAILABLE in codes(result, "AMD")


def test_an_unavailable_earnings_date_still_allows_the_exit():
    ctx = _amd_context()
    ctx["earnings"]["AMD"] = {"status": "unavailable"}
    sell = order(symbol="AMD", side="sell", type="market", limit_price=None,
                 quantity=0.043408)

    result = guard.evaluate([sell], ctx)

    assert result.approved == [sell]


def test_a_symbol_missing_from_the_earnings_map_fails_closed():
    ctx = _amd_context()
    del ctx["earnings"]["AMD"]

    result = guard.evaluate([order(symbol="AMD", limit_price=480.00)], ctx)

    assert guard.EARNINGS_DATE_UNAVAILABLE in codes(result, "AMD")


# --- wash sale ------------------------------------------------------------


def test_a_repurchase_inside_the_31_day_window_is_denied():
    result = guard.evaluate(
        [order()],
        context(loss_closes=[{"symbol": "F", "close_date": "2026-07-20"}]),
    )

    assert guard.WASH_SALE_COOLDOWN in codes(result, "F")


def test_the_wash_sale_window_still_blocks_on_day_31():
    # §9 deletes rows once *past* 31 days, so day 31 is still a live block.
    result = guard.evaluate(
        [order()],
        context(loss_closes=[{"symbol": "F", "close_date": "2026-07-03"}]),
    )

    assert guard.WASH_SALE_COOLDOWN in codes(result, "F")


def test_a_repurchase_on_day_32_is_clear():
    result = guard.evaluate(
        [order()],
        context(loss_closes=[{"symbol": "F", "close_date": "2026-07-02"}]),
    )

    assert guard.WASH_SALE_COOLDOWN not in codes(result, "F")


def test_a_loss_close_in_another_symbol_does_not_block_this_one():
    result = guard.evaluate(
        [order()],
        context(loss_closes=[{"symbol": "MU", "close_date": "2026-07-20"}]),
    )

    assert result.approved == [order()]


def test_a_measured_loss_blocks_whatever_the_unknown_basis_switch_says():
    """The switch is about rows the system could not *price*, never about a
    loss it measured."""
    result = guard.evaluate(
        [order()],
        context(
            config=factories.config(wash_sale_block_unknown_basis=False),
            loss_closes=[
                {"symbol": "F", "close_date": "2026-07-20", "loss_amount": 1.25,
                 "basis_unknown": False}
            ],
        ),
    )

    assert guard.WASH_SALE_COOLDOWN in codes(result, "F")


def test_an_unpriced_close_blocks_by_default():
    """§9's fail-closed fallback, and the live account's entire ledger: 15
    rows, every one of them a close the tax lots could not price, every one
    of them banning a repurchase for 31 days."""
    result = guard.evaluate(
        [order()],
        context(
            loss_closes=[
                {"symbol": "F", "close_date": "2026-07-20", "loss_amount": None,
                 "basis_unknown": True}
            ]
        ),
    )

    assert guard.WASH_SALE_COOLDOWN in codes(result, "F")


def test_an_unpriced_close_stops_blocking_when_the_switch_is_off():
    """`wash_sale_block_unknown_basis: false` — the setting to reach for once
    the broker's realized read is confirmed live and the null rows are known to
    be historical rather than current."""
    result = guard.evaluate(
        [order()],
        context(
            config=factories.config(wash_sale_block_unknown_basis=False),
            loss_closes=[
                {"symbol": "F", "close_date": "2026-07-20", "loss_amount": None,
                 "basis_unknown": True}
            ],
        ),
    )

    assert guard.WASH_SALE_COOLDOWN not in codes(result, "F")


def test_the_wash_sale_ledger_never_blocks_a_sell():
    sell = order(side="sell", type="market", limit_price=None)

    result = guard.evaluate(
        [sell],
        context(loss_closes=[{"symbol": "F", "close_date": "2026-07-20"}]),
    )

    assert result.approved == [sell]


# --- broker floor ---------------------------------------------------------


def test_an_order_below_the_one_dollar_floor_is_denied():
    result = guard.evaluate(
        [order(type="limit", limit_price=15.00, quantity=0.05)],
        context(),
    )

    assert guard.BELOW_MIN_NOTIONAL in codes(result, "F")


def test_the_one_dollar_floor_applies_to_sells_too():
    # It is a broker floor, not a risk rule; the broker rejects either side.
    sell = order(side="sell", type="market", limit_price=None, quantity=0.01)

    result = guard.evaluate([sell], context())

    assert guard.BELOW_MIN_NOTIONAL in codes(result, "F")


def test_an_order_exactly_at_the_floor_is_allowed():
    result = guard.evaluate(
        [order(type="limit", limit_price=1.00, quantity=1.0)],
        context(),
    )

    assert guard.BELOW_MIN_NOTIONAL not in codes(result, "F")


# --- duplicate orders -----------------------------------------------------


def test_a_second_buy_while_one_is_open_is_denied():
    # §14: "Blind retry is how a position doubles."
    result = guard.evaluate(
        [order()],
        context(open_orders=[{"symbol": "F", "side": "buy", "state": "queued"}]),
    )

    assert guard.DUPLICATE_OPEN_ORDER in codes(result, "F")


def test_an_open_sell_does_not_block_a_buy_on_the_same_symbol():
    result = guard.evaluate(
        [order()],
        context(open_orders=[{"symbol": "F", "side": "sell", "state": "queued"}]),
    )

    assert guard.DUPLICATE_OPEN_ORDER not in codes(result, "F")


def test_a_resting_stop_blocks_a_second_sell_on_the_same_symbol():
    # The resting stop from §10.1 is an open sell. The plan must cancel it
    # before emitting a discretionary exit, which is what §12.2's cancel
    # machinery exists for; placing both risks overselling the position.
    sell = order(side="sell", type="market", limit_price=None)

    result = guard.evaluate(
        [sell],
        context(open_orders=[{"symbol": "F", "side": "sell", "state": "confirmed"}]),
    )

    assert guard.DUPLICATE_OPEN_ORDER in codes(result, "F")


def test_a_filled_order_is_not_an_open_order():
    result = guard.evaluate(
        [order()],
        context(open_orders=[{"symbol": "F", "side": "buy", "state": "filled"}]),
    )

    assert guard.DUPLICATE_OPEN_ORDER not in codes(result, "F")


def test_two_buys_on_one_symbol_in_a_single_plan_collide():
    # The open-order list is the broker's view and cannot see an order this
    # same plan is about to place.
    first = order()
    second = order(limit_price=15.10)

    result = guard.evaluate([first, second], context())

    assert result.approved == [first]
    assert guard.DUPLICATE_OPEN_ORDER in codes(result, "F")
