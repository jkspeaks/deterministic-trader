"""Account-level halts — denials that apply to the whole plan, not one order.

Design v3 §14 (error handling) and §10 (risk controls). The distinction that
matters here: the kill switch stops *everything*, while a drawdown breach is
itself exit priority 2 in §6.5 — it must flatten the book, so it stops buys and
lets sells through.
"""

from src import guard
from tests.factories import codes, context, order


def test_unreadable_control_state_places_nothing():
    result = guard.evaluate([order()], context(control={"readable": False}))

    assert result.halted
    assert result.approved == []
    assert codes(result) == [guard.CONTROL_UNREADABLE]


def test_unreadable_control_state_stops_exits_too():
    result = guard.evaluate(
        [order(side="sell", type="market", limit_price=None)],
        context(control={"readable": False}),
    )

    assert result.approved == []


def test_kill_switch_off_places_nothing():
    result = guard.evaluate([order()], context(control={"trading_enabled": False}))

    assert result.halted
    assert result.approved == []
    assert codes(result) == [guard.TRADING_DISABLED]


def test_kill_switch_is_absolute_and_stops_exits():
    result = guard.evaluate(
        [order(side="sell", type="market", limit_price=None)],
        context(control={"trading_enabled": False}),
    )

    assert result.approved == []


def test_missing_equity_is_a_halt_not_a_breach():
    """An absent `equity_total` became a large negative flow-adjusted figure and
    §10 read it as a 20% breach, proposing a whole-book flatten. A balance the
    system cannot read is ACCOUNT_UNREADABLE — the same fail-closed posture as
    CONTROL_UNREADABLE — never a drawdown."""
    ctx = context()
    del ctx["account"]["equity_total"]

    result = guard.evaluate([order()], ctx)

    assert result.halted
    assert result.approved == []
    assert codes(result) == [guard.ACCOUNT_UNREADABLE]


def test_null_zero_and_non_numeric_equity_all_halt():
    """An account under agentic control always holds *some* equity, so `null`,
    `0` and an unparseable string are all failed reads, not balances."""
    for bad in (None, 0, 0.0, -1.0, "abc"):
        result = guard.evaluate([order()], context(account={"equity_total": bad}))

        assert result.halted, bad
        assert result.approved == [], bad
        assert codes(result) == [guard.ACCOUNT_UNREADABLE], bad


def test_an_unreadable_equity_stops_exits_too():
    """A system that cannot read its own balance places nothing, sells
    included — the same absolute posture the kill switch and CONTROL_UNREADABLE
    already take."""
    result = guard.evaluate(
        [order(side="sell", type="market", limit_price=None)],
        context(account={"equity_total": None}),
    )

    assert result.approved == []


def test_a_readable_zero_dollar_change_is_still_fine():
    """The guard is on `equity_total`, not on P&L: a real balance that happens
    to sit exactly on its high-water mark is not a bad read."""
    result = guard.evaluate([order()], context(account={"equity_total": 236.42}))

    assert not result.halted
    assert guard.ACCOUNT_UNREADABLE not in codes(result)


def test_drawdown_breach_denies_a_buy():
    # 20% below a 300.00 high-water mark is 240.00; equity is 236.42.
    result = guard.evaluate(
        [order()],
        context(control={"high_water_mark": 300.00}),
    )

    assert codes(result, "F") == [guard.DRAWDOWN_HALT]
    assert result.approved == []


def test_drawdown_breach_still_allows_the_flatten_that_it_triggers():
    # §6.5 lists the drawdown halt as exit priority 2. A halt that blocked its
    # own flatten would trap the book in the position it is halting over.
    sell = order(symbol="INTC", side="sell", type="market", limit_price=None)
    result = guard.evaluate(
        [sell],
        context(
            control={"high_water_mark": 300.00},
            universe={"candidates": ["INTC"]},
            positions=[
                {
                    "symbol": "INTC",
                    "quantity": 1.0,
                    "market_value": 90.21,
                    "play_id": "MANUAL",
                }
            ],
            quotes={"INTC": {"bid": 90.10, "ask": 90.30, "last": 90.21}},
            earnings={"INTC": {"status": "known", "next_report_date": "2026-10-24"}},
            sectors={"INTC": "Technology"},
            tradability={
                "INTC": {
                    "tradeable": True,
                    "fractional_tradability": "tradable",
                    "all_day_tradability": "tradable",
                }
            },
        ),
    )

    assert result.approved == [sell]
    assert not result.halted


def test_equity_exactly_at_the_drawdown_threshold_is_not_a_breach():
    # 80% of 295.525 is exactly 236.42. The halt is "below", not "at or below";
    # an off-by-one here would halt the system on its own high-water mark.
    result = guard.evaluate(
        [order()],
        context(control={"high_water_mark": 295.525}),
    )

    assert guard.DRAWDOWN_HALT not in codes(result)


def test_a_clean_plan_is_approved_unchanged():
    proposed = order()
    result = guard.evaluate([proposed], context())

    assert result.approved == [proposed]
    assert result.denied == []
    assert not result.halted


# --- ACCOUNT_IMPLAUSIBLE -----------------------------------------------


def test_implausible_equity_denies_a_buy():
    """Equity_total read 669.53 while the account held more than that in open
    positions. `ACCOUNT_IMPLAUSIBLE` is a buy-only denial, unlike
    `ACCOUNT_UNREADABLE` — it does not halt the whole plan."""
    result = guard.evaluate(
        [order()],
        context(
            account={"equity_total": 236.42},
            positions=[{"symbol": "OTHER", "market_value": 300.00}],
        ),
    )

    assert codes(result, "F") == [guard.ACCOUNT_IMPLAUSIBLE]
    assert not result.halted


def test_implausible_equity_still_allows_an_exit():
    """Unlike a halt, this must not trap the book — an exit is the operator's
    way to find out the read is bad in the first place."""
    sell = order(symbol="OTHER", side="sell", type="market", limit_price=None)
    result = guard.evaluate(
        [sell],
        context(
            account={"equity_total": 236.42},
            positions=[
                {
                    "symbol": "OTHER",
                    "quantity": 1.0,
                    "market_value": 300.00,
                    "play_id": "MANUAL",
                }
            ],
            universe={"candidates": []},
            quotes={"OTHER": {"bid": 299.00, "ask": 301.00, "last": 300.00}},
            earnings={"OTHER": {"status": "known", "next_report_date": "2026-10-24"}},
            sectors={"OTHER": "Technology"},
            tradability={
                "OTHER": {
                    "tradeable": True,
                    "fractional_tradability": "tradable",
                    "all_day_tradability": "tradable",
                }
            },
        ),
    )

    assert result.approved == [sell]
    assert not result.halted


def test_implausible_equity_outranks_the_drawdown_halt():
    """The same reading that would (wrongly) look like a drawdown breach must
    report as ACCOUNT_IMPLAUSIBLE, not DRAWDOWN_HALT — the false-urgent alert
    once raised was exactly this ambiguity resolved the wrong way."""
    result = guard.evaluate(
        [order()],
        context(
            control={"high_water_mark": 1000.00},
            account={"equity_total": 100.00},
            positions=[{"symbol": "OTHER", "market_value": 300.00}],
        ),
    )

    assert codes(result, "F") == [guard.ACCOUNT_IMPLAUSIBLE]


def test_a_plausible_equity_is_not_denied_on_this_code():
    result = guard.evaluate([order()], context())

    assert guard.ACCOUNT_IMPLAUSIBLE not in codes(result)
