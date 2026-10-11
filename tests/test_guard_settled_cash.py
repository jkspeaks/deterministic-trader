"""Invariant 1 — every buy is funded by settled cash (design v3 §2.3).

A Good Faith Violation requires the *purchase* to have used unsettled funds. If
no purchase ever does, no sell can create one, at any hour and at any frequency.
That is the whole reason the reactive exit loop is legal, so these are the
denials that carry the most regulatory weight in the file.

§2.4's conservative fallback bound until probe P3 returned. So `buying_power`
excludes unsettled proceeds, the live config runs `buying_power_is_settled`, and
the switch self-relaxed exactly as designed — one value in `config/config.json`,
no code change.

What P3 did **not** measure is a deposit, which the broker credits to
`buying_power` ahead of the ACH deliberately. `settled_cash` therefore stopped
being required and did not stop being binding; the tests below that want the
old both-required behaviour now name the conservative policy themselves.
"""

from src import guard
from tests.factories import codes, context, order

SOFI_MARKET = {
    "universe": {"candidates": ["F", "SOFI"]},
    "quotes": {
        "F": {"bid": 14.42, "ask": 14.87, "last": 14.68},
        "SOFI": {"bid": 16.20, "ask": 16.31, "last": 16.25},
    },
    "earnings": {
        "F": {
            "status": "known",
            "next_report_date": "2026-10-28",
            "sessions_until_report": 60,
        },
        "SOFI": {
            "status": "known",
            "next_report_date": "2026-10-27",
            "sessions_until_report": 59,
        },
    },
    "sectors": {"F": "Consumer Discretionary", "SOFI": "Financials"},
    "tradability": {
        "F": {
            "tradeable": True,
            "fractional_tradability": "tradable",
            "all_day_tradability": "tradable",
        },
        "SOFI": {
            "tradeable": True,
            "fractional_tradability": "tradable",
            "all_day_tradability": "tradable",
        },
    },
}


def test_buy_beyond_settled_cash_is_denied():
    result = guard.evaluate(
        [order()],
        context(account={"settled_cash": 10.00, "buying_power": 100.00}),
    )

    assert codes(result, "F") == [guard.INSUFFICIENT_SETTLED_CASH]


def test_buy_within_settled_cash_is_approved():
    result = guard.evaluate(
        [order()],
        context(account={"settled_cash": 20.00, "buying_power": 20.00}),
    )

    assert result.approved == [order()]


def test_unknown_settled_cash_denies_every_buy():
    # §14: "Settled cash unknown -> deny all buys, allow exits." Under the
    # conservative policy that is every case, because it had no verified
    # figure to fall back to; P3 gave the resolved policy one.
    result = guard.evaluate(
        [order()],
        context(
            account={"settled_cash": None},
            config={"settled_cash_policy": guard.POLICY_CONSERVATIVE},
        ),
    )

    assert codes(result, "F") == [guard.SETTLED_CASH_UNKNOWN]


def test_unknown_settled_cash_still_allows_exits():
    sell = order(side="sell", type="market", limit_price=None)
    result = guard.evaluate([sell], context(account={"settled_cash": None}))

    assert result.approved == [sell]


def test_a_sell_is_never_checked_against_settled_cash():
    sell = order(side="sell", type="market", limit_price=None)
    result = guard.evaluate(
        [sell],
        context(account={"settled_cash": 0.00, "buying_power": 0.00}),
    )

    assert result.approved == [sell]


def test_buying_power_above_settled_cash_does_not_raise_the_ceiling():
    # The conservative fallback is min(buying_power, settled). Reading
    # buying_power alone here is precisely the GFV that Invariant 1 removes.
    result = guard.evaluate(
        [order()],
        context(account={"settled_cash": 10.00, "buying_power": 250.00}),
    )

    assert codes(result, "F") == [guard.INSUFFICIENT_SETTLED_CASH]


def test_settled_cash_above_buying_power_does_not_raise_the_ceiling_either():
    result = guard.evaluate(
        [order()],
        context(account={"settled_cash": 250.00, "buying_power": 10.00}),
    )

    assert codes(result, "F") == [guard.INSUFFICIENT_SETTLED_CASH]


def test_a_buy_in_the_same_session_as_a_sell_is_denied_outright():
    # §2.4's fallback: with no verified settled/unsettled split, a sale earlier
    # in the session may have inflated buying_power with unsettled proceeds.
    # P3 disproved the premise on this account, so this is the conservative
    # policy's rule and the test names it. The retirement is pinned by
    # `test_p3_resolved_policy_reads_buying_power_directly` below.
    result = guard.evaluate(
        [order()],
        context(
            account={"sold_this_session": True, "settled_cash": 500.00},
            config={"settled_cash_policy": guard.POLICY_CONSERVATIVE},
        ),
    )

    assert codes(result, "F") == [guard.SAME_SESSION_SELL]


def test_a_sell_in_the_same_session_as_a_sell_is_untouched():
    sell = order(side="sell", type="market", limit_price=None)
    result = guard.evaluate([sell], context(account={"sold_this_session": True}))

    assert result.approved == [sell]


def test_p3_resolved_policy_reads_buying_power_directly():
    # The "excludes unsettled" branch of the P3 decision table: guard.py may
    # use buying_power directly and the same-session rule retires.
    ctx = context(
        account={
            "settled_cash": None,
            "buying_power": 100.00,
            "sold_this_session": True,
        },
        config={"settled_cash_policy": guard.POLICY_BUYING_POWER_IS_SETTLED},
    )

    result = guard.evaluate([order()], ctx)

    assert result.approved == [order()]


def test_p3_resolved_policy_still_denies_a_buy_beyond_buying_power():
    ctx = context(
        account={"settled_cash": None, "buying_power": 10.00},
        config={"settled_cash_policy": guard.POLICY_BUYING_POWER_IS_SETTLED},
    )

    result = guard.evaluate([order()], ctx)

    assert codes(result, "F") == [guard.INSUFFICIENT_SETTLED_CASH]


def test_the_default_policy_is_conservative():
    # If this flips by accident, every over-deployment becomes silent.
    assert guard.DEFAULT_SETTLED_CASH_POLICY == guard.POLICY_CONSERVATIVE


def test_a_limit_buy_is_costed_at_the_limit_price_not_the_quote():
    # Runbook §6 row 2: the broker computes the shortfall against the limit
    # notional. An F buy limit at 15.00 is short against 14.90 settled even
    # though the 14.87 ask would fit.
    result = guard.evaluate(
        [order(type="limit", limit_price=15.00, quantity=1.0)],
        context(account={"settled_cash": 14.90, "buying_power": 14.90}),
    )

    assert codes(result, "F") == [guard.INSUFFICIENT_SETTLED_CASH]


def test_a_market_buy_is_costed_at_the_ask_not_the_bid():
    # Costing a buy at the bid understates it by the spread, which is exactly
    # the error that leaves an order short at placement.
    result = guard.evaluate(
        [order(type="market", limit_price=None, quantity=1.0)],
        context(account={"settled_cash": 14.50, "buying_power": 14.50}),
    )

    assert codes(result, "F") == [guard.INSUFFICIENT_SETTLED_CASH]


def test_a_market_buy_for_an_unquoted_symbol_is_denied_not_costed_at_zero():
    # An unpriceable order must never fall through as free. Silently costing
    # it at zero is how a plan deploys more than the account holds.
    ctx = context()
    del ctx["quotes"]["F"]

    result = guard.evaluate([order(type="market", limit_price=None)], ctx)

    assert codes(result, "F") == [guard.ORDER_NOT_PRICEABLE]


def test_a_market_buy_whose_ask_is_null_is_denied():
    # The shape a stale or halted quote takes: the symbol is present, the
    # price is not.
    result = guard.evaluate(
        [order(type="market", limit_price=None)],
        context(quotes={"F": {"bid": None, "ask": None, "last": None}}),
    )

    assert codes(result, "F") == [guard.ORDER_NOT_PRICEABLE]


def test_a_limit_buy_with_no_limit_price_is_denied():
    result = guard.evaluate(
        [order(type="limit", limit_price=None)],
        context(),
    )

    assert codes(result, "F") == [guard.ORDER_NOT_PRICEABLE]


def test_settled_cash_is_consumed_across_a_plan_not_checked_per_order():
    # Two buys that each fit alone but not together. Checking them
    # independently would deploy 31.31 of 20.00 settled.
    ctx = context(**SOFI_MARKET)
    ctx["account"].update({"settled_cash": 20.00, "buying_power": 20.00})

    first = order(symbol="F", limit_price=15.00)
    second = order(symbol="SOFI", limit_price=16.31)
    result = guard.evaluate([first, second], ctx)

    assert result.approved == [first]
    assert codes(result, "SOFI") == [guard.INSUFFICIENT_SETTLED_CASH]


def test_a_denied_order_does_not_consume_settled_cash():
    # The 250.00 order is denied; the 15.00 order behind it must still fund.
    ctx = context(**SOFI_MARKET)
    ctx["account"].update({"settled_cash": 20.00, "buying_power": 20.00})

    unaffordable = order(symbol="SOFI", limit_price=16.31, quantity=15.0)
    affordable = order(symbol="F", limit_price=15.00)
    result = guard.evaluate([unaffordable, affordable], ctx)

    assert result.approved == [affordable]
