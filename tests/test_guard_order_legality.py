"""Order legality — the layer nothing upstream will catch.

Runbook §7 restates an early probe along the boundary the findings doc had not
located: `review_equity_order` validates **account state** (buying power, share
ownership) and does **not** validate **order legality** (session rules,
fractional + stop, the session clock). Guard is therefore the only place these
rules exist, and a clean review says nothing about any of them.

Every test here cites the probe row it descends from.
"""

from src import guard
from tests.factories import codes, context, order


def _sessions(cycle_session: str, order_session: str, **overrides):
    ctx = context(cycle={"session": cycle_session}, **overrides)
    ctx["account"].update({"settled_cash": 500.00, "buying_power": 500.00})
    return ctx, order(session=order_session)


# --- P1: fractional is regular-hours only ---------------------------------


def test_a_fractional_order_outside_regular_hours_is_denied():
    # P1, rows 1-3: rejected client-side with "fractional and dollar-based
    # orders are only allowed in regular_hours", regardless of the per-symbol
    # extended_hours_fractional_tradability flag.
    ctx, _ = _sessions("extended_hours", "extended_hours")

    result = guard.evaluate(
        [order(session="extended_hours", quantity=0.05, type="limit")], ctx
    )

    assert guard.FRACTIONAL_OUTSIDE_REGULAR_HOURS in codes(result, "F")


def test_a_fractional_order_in_all_day_hours_is_denied_too():
    # P1 row 3: all_day_hours is refused on the same string.
    ctx, _ = _sessions("all_day_hours", "all_day_hours")

    result = guard.evaluate(
        [order(session="all_day_hours", quantity=0.05, type="limit")], ctx
    )

    assert guard.FRACTIONAL_OUTSIDE_REGULAR_HOURS in codes(result, "F")


def test_a_fractional_order_in_regular_hours_is_fine():
    # A fractional market sell in regular hours passes with order_checks {}.
    result = guard.evaluate(
        [order(side="sell", type="market", limit_price=None, quantity=0.05)],
        context(),
    )

    assert guard.FRACTIONAL_OUTSIDE_REGULAR_HOURS not in codes(result, "F")


def test_a_whole_share_limit_in_extended_hours_is_allowed():
    # P1 row 5 — the control that made the probe conclusive: the same extended
    # session that refuses 0.05 shares accepts 1 whole share.
    ctx, proposed = _sessions("extended_hours", "extended_hours")

    result = guard.evaluate([proposed], ctx)

    assert result.approved == [proposed]


def test_the_per_symbol_fractional_flag_is_never_consulted():
    # The findings doc records extended_hours_fractional_tradability as "moot
    # and should not be read by any module". A symbol flagged true must still
    # be denied fractionally outside regular hours.
    ctx, _ = _sessions("extended_hours", "extended_hours")
    ctx["tradability"]["F"]["extended_hours_fractional_tradability"] = True

    result = guard.evaluate(
        [order(session="extended_hours", quantity=0.05, type="limit")], ctx
    )

    assert guard.FRACTIONAL_OUTSIDE_REGULAR_HOURS in codes(result, "F")


# --- market and stop orders are regular-hours only ------------------------


def test_a_stop_order_tagged_to_extended_hours_is_denied():
    # Findings doc an early probe: documented as "rejected if tagged to
    # another session", yet an F stop_market + extended_hours returned a
    # normal priced review. Guard is the only enforcement point.
    ctx, _ = _sessions("extended_hours", "extended_hours")

    result = guard.evaluate(
        [
            order(
                session="extended_hours",
                type="stop_market",
                limit_price=None,
                stop_price=13.50,
            )
        ],
        ctx,
    )

    assert guard.ORDER_TYPE_OUTSIDE_REGULAR_HOURS in codes(result, "F")


def test_a_market_order_tagged_to_extended_hours_is_denied():
    ctx, _ = _sessions("extended_hours", "extended_hours")

    result = guard.evaluate(
        [order(session="extended_hours", type="market", limit_price=None)], ctx
    )

    assert guard.ORDER_TYPE_OUTSIDE_REGULAR_HOURS in codes(result, "F")


def test_a_stop_order_in_regular_hours_is_allowed():
    proposed = order(type="stop_market", limit_price=None, stop_price=13.50)
    ctx = context()
    ctx["account"].update({"settled_cash": 500.00, "buying_power": 500.00})

    result = guard.evaluate([proposed], ctx)

    assert result.approved == [proposed]


# --- fractional + stop stays conservative ---------------------------------


def test_a_fractional_stop_is_denied_even_in_regular_hours():
    # Runbook §6 row 4: ADI sell stop_market 0.02 returned order_checks {} —
    # a clean review on exactly the combination §10.1 claims Robinhood
    # refuses. An early probe calls that "unverified, not refuted", and the
    # conservative behaviour holds until a live placement settles it.
    result = guard.evaluate(
        [
            order(
                side="sell",
                type="stop_market",
                limit_price=None,
                stop_price=13.50,
                quantity=0.05,
            )
        ],
        context(),
    )

    assert guard.FRACTIONAL_STOP_UNVERIFIED in codes(result, "F")


def test_a_whole_share_stop_is_not_caught_by_the_fractional_stop_rule():
    # §10.1's WHOLE row: the first whole share is the first continuous
    # protection the account has ever had, and this rule must not block it.
    proposed = order(
        side="sell", type="stop_market", limit_price=None, stop_price=13.50,
        quantity=1.0,
    )

    result = guard.evaluate([proposed], context())

    assert result.approved == [proposed]


# --- the session clock ----------------------------------------------------


def test_nothing_places_while_every_session_is_closed():
    # Findings doc an early probe, row 4: every review above succeeded at
    # 02:30 ET Saturday. P5 confirms 24/5 — there is no weekend equity
    # session.
    ctx, proposed = _sessions("closed", "regular_hours")

    result = guard.evaluate([proposed], ctx)

    assert result.halted
    assert result.approved == []
    assert codes(result) == [guard.SESSION_CLOSED]


def test_the_closed_session_halt_stops_exits_too():
    ctx, _ = _sessions("closed", "regular_hours")
    sell = order(side="sell", type="market", limit_price=None)

    result = guard.evaluate([sell], ctx)

    assert result.approved == []


# --- tradability flags ----------------------------------------------------


def test_the_bare_tradable_string_is_what_admits_an_extended_session():
    """The secondary finding, and §15 asks for this test by name.

    The guide documents the comparison as
    ``all_day_tradability == 'all_day_tradability_tradable'`` while the API
    returns the bare ``'tradable'``. A literal implementation matches nothing
    and denies every symbol for every extended session — a bug that looks
    exactly like correct conservative behaviour and is therefore never
    investigated.
    """
    ctx, proposed = _sessions("extended_hours", "extended_hours")
    assert ctx["tradability"]["F"]["all_day_tradability"] == "tradable"

    result = guard.evaluate([proposed], ctx)

    assert result.approved == [proposed]


def test_the_documented_but_never_returned_value_is_not_treated_as_tradable():
    # The other direction: if the API ever did return the long form, guard
    # must not silently accept a value it has never seen in the wild.
    ctx, proposed = _sessions("extended_hours", "extended_hours")
    ctx["tradability"]["F"]["all_day_tradability"] = "all_day_tradability_tradable"

    result = guard.evaluate([proposed], ctx)

    assert guard.EXTENDED_SESSION_NOT_TRADABLE in codes(result, "F")


def test_a_symbol_not_tradable_in_extended_hours_is_denied_there():
    ctx, proposed = _sessions("extended_hours", "extended_hours")
    ctx["tradability"]["F"]["all_day_tradability"] = "not_tradable"

    result = guard.evaluate([proposed], ctx)

    assert guard.EXTENDED_SESSION_NOT_TRADABLE in codes(result, "F")


def test_extended_tradability_is_irrelevant_during_regular_hours():
    ctx = context()
    ctx["account"].update({"settled_cash": 500.00, "buying_power": 500.00})
    ctx["tradability"]["F"]["all_day_tradability"] = "not_tradable"

    result = guard.evaluate([order()], ctx)

    assert result.approved == [order()]


def test_a_symbol_that_is_not_tradable_at_all_is_denied():
    ctx = context()
    ctx["tradability"]["F"]["tradeable"] = False

    result = guard.evaluate([order()], ctx)

    assert guard.NOT_TRADABLE in codes(result, "F")


def test_a_symbol_with_the_old_string_field_is_denied():
    # get_equity_tradability dropped the "tradability" string
    # field entirely in favour of a boolean "tradeable" one. A response that
    # still carried the old field (and nothing else recognisable) must fail
    # closed, not be silently treated as tradable.
    ctx = context()
    del ctx["tradability"]["F"]["tradeable"]
    ctx["tradability"]["F"]["tradability"] = "tradable"

    result = guard.evaluate([order()], ctx)

    assert guard.NOT_TRADABLE in codes(result, "F")


def test_a_symbol_with_no_tradability_record_fails_closed():
    ctx = context()
    del ctx["tradability"]["F"]

    result = guard.evaluate([order()], ctx)

    assert guard.NOT_TRADABLE in codes(result, "F")


# --- whole_shares_only -----------------------


def test_a_fractional_buy_is_denied_when_whole_shares_only_is_on():
    # Defense-in-depth: sizing already refuses these (WHOLE_SHARE_ONLY in
    # sizing.py), so this covers a hand-placed or replayed order that
    # bypassed sizing.
    result = guard.evaluate(
        [order(side="buy", type="market", limit_price=None, quantity=0.05)], context()
    )

    assert guard.WHOLE_SHARE_ONLY in codes(result, "F")


def test_a_fractional_sell_stays_legal_under_whole_shares_only():
    # Exits stay fractional-safe: the gate is buy-side only.
    proposed = order(side="sell", type="market", limit_price=None, quantity=0.05)

    result = guard.evaluate([proposed], context())

    assert guard.WHOLE_SHARE_ONLY not in codes(result, "F")


def test_whole_shares_only_off_admits_a_fractional_buy():
    ctx = context()
    ctx["account"].update({"settled_cash": 500.00, "buying_power": 500.00})
    ctx["config"] = {**ctx["config"], "whole_shares_only": False}
    proposed = order(side="buy", type="market", limit_price=None, quantity=0.05)

    result = guard.evaluate([proposed], ctx)

    assert guard.WHOLE_SHARE_ONLY not in codes(result, "F")


def test_a_fractional_order_needs_fractional_tradability():
    ctx = context()
    ctx["tradability"]["F"]["fractional_tradability"] = "not_tradable"

    result = guard.evaluate(
        [order(side="sell", type="market", limit_price=None, quantity=0.05)], ctx
    )

    assert guard.FRACTIONAL_NOT_TRADABLE in codes(result, "F")
