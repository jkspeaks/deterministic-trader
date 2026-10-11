"""§10.1's resting orders, through the same denial layer as everything else.

One word about the old resting stop: "Not
guard-evaluated". It was placed by the prompt, outside `plan.orders`, and the
prompt itself said so — "*it is also, at present, the one order `guard.py`
never sees*". These are the tests that make that sentence false.

The two rules worth stating outright:

* A protective order is judged for legality, tradability and pricing exactly
  like a plan order, because it is one.
* A protective order that already **rests** is not competition for an exit on
  the same symbol. `DUPLICATE_OPEN_ORDER` exists to stop a position doubling;
  a contingent stop under a position is not a second intent to sell it, and
  treating it as one silently made every exit on a protected name unplaceable.
"""

from src import guard
from src.plays import protection
from tests.factories import codes, context, order


def protective(kind=protection.STOP, price=14.10, **overrides) -> dict:
    """One order off `plays.protection.Protection.as_order`, by hand.

    Built here rather than through `protection.desired` because guard's
    subject is the order, not the book behind it — and a guard test that had
    to construct a position to reach a denial would fail for two reasons at
    once the day the book's shape changes.
    """
    stop = kind == protection.STOP
    return {
        "symbol": "F",
        "side": "sell",
        "type": "stop_market" if stop else "limit",
        "quantity": 1.0,
        "limit_price": None if stop else price,
        "stop_price": price if stop else None,
        "session": "regular_hours",
        "time_in_force": "gtc",
        "play_id": "DIP",
        "protective_stop": stop,
        "protective_target": not stop,
        **overrides,
    }


def resting(order_id="p1", kind=protection.STOP, price=14.10, **overrides) -> dict:
    return {**protective(kind, price), "order_id": order_id, "state": "confirmed",
            **overrides}


def held(**overrides) -> dict:
    return context(
        positions=[{"symbol": "F", "quantity": 1.0, "market_value": 14.68}],
        **overrides,
    )


# --- a protective order is an ordinary order -------------------------------


def test_both_protective_orders_are_approved_on_a_clean_book():
    result = guard.evaluate(
        [protective(protection.STOP), protective(protection.TARGET, 15.90)], held()
    )

    assert codes(result) == []
    assert len(result.approved) == 2


def test_a_stop_and_a_target_on_one_symbol_do_not_collide():
    """They are the two ends of one trade, not two intents to sell the same
    shares — so the duplicate rule must not read the first as claiming the
    second's slot."""
    result = guard.evaluate(
        [protective(protection.STOP), protective(protection.TARGET, 15.90)], held()
    )

    assert "DUPLICATE_OPEN_ORDER" not in codes(result)
    assert "DUPLICATE_PROTECTION" not in codes(result)


def test_an_untradable_symbol_is_refused_its_stop_like_anything_else():
    ctx = held(tradability={"F": {"tradeable": False}})

    assert "NOT_TRADABLE" in codes(guard.evaluate([protective()], ctx))


def test_a_stop_outside_regular_hours_is_refused():
    """`stop_market` is regular-hours-only and review does not enforce it, so
    an order tagged to another session queues instead of resting — which looks
    placed and protects nothing."""
    ctx = held(cycle={"session": "extended_hours"})
    result = guard.evaluate([protective(session="extended_hours")], ctx)

    assert "ORDER_TYPE_OUTSIDE_REGULAR_HOURS" in codes(result)


def test_a_fractional_stop_is_still_unverified_and_refused():
    result = guard.evaluate([protective(quantity=0.25)], held())

    assert "FRACTIONAL_STOP_UNVERIFIED" in codes(result)


# --- DUPLICATE_PROTECTION ---------------------------------------------------


def test_a_second_stop_at_the_same_price_is_refused():
    ctx = held(open_orders=[resting()])

    assert codes(guard.evaluate([protective()], ctx)) == ["DUPLICATE_PROTECTION"]


def test_a_stop_within_the_no_churn_band_is_refused_as_a_duplicate():
    """The same band `reconcile._protection_plan` keeps by, so the two cannot
    disagree about whether a resting order is the one that was wanted."""
    ctx = held(open_orders=[resting(price=14.11)])

    assert codes(guard.evaluate([protective(price=14.10)], ctx)) == [
        "DUPLICATE_PROTECTION"
    ]


def test_a_stop_at_a_genuinely_different_price_is_allowed_through():
    ctx = held(open_orders=[resting(price=12.00)])

    assert codes(guard.evaluate([protective(price=14.10)], ctx)) == []


def test_a_resting_stop_does_not_duplicate_a_target():
    ctx = held(open_orders=[resting(kind=protection.STOP, price=14.10)])
    result = guard.evaluate([protective(protection.TARGET, 15.90)], ctx)

    assert codes(result) == []


def test_the_order_being_replaced_is_not_its_own_duplicate():
    """A `protection_plan.replace` cancels and places in one plan, and the
    cancel goes first. Without this the new order would deny itself against
    the very order it exists to supersede."""
    ctx = held(open_orders=[resting(price=12.00)], cancelling_order_ids=["p1"])

    assert codes(guard.evaluate([protective(price=14.10)], ctx)) == []


def test_two_identical_protective_orders_in_one_plan_deny_the_second():
    """The broker's open-order list cannot see an order this same plan is
    about to place, so approvals are recorded as they are granted."""
    result = guard.evaluate([protective(), protective()], held())

    assert len(result.approved) == 1
    assert codes(result) == ["DUPLICATE_PROTECTION"]


# --- the exit that has to stay placeable ------------------------------------


def test_a_resting_stop_does_not_block_an_exit_on_the_same_symbol():
    """The one that mattered."""
    ctx = held(open_orders=[resting()])
    exit_order = order(side="sell", type="limit", limit_price=14.42, quantity=1.0)

    assert codes(guard.evaluate([exit_order], ctx)) == []


def test_an_ordinary_open_sell_still_blocks_a_second_one():
    """The rule the exemption is carved out of is unchanged: a working day
    limit sell on a symbol is a competing intent, and a blind second order is
    how a position doubles."""
    ctx = held(
        open_orders=[
            {"symbol": "F", "side": "sell", "type": "limit", "quantity": 1.0,
             "limit_price": 14.42, "state": "confirmed", "order_id": "d1"}
        ]
    )
    exit_order = order(side="sell", type="limit", limit_price=14.42, quantity=1.0)

    assert codes(guard.evaluate([exit_order], ctx)) == ["DUPLICATE_OPEN_ORDER"]


# --- the shares the broker has already reserved ----------------------
#
# Robinhood reserves 100% of a position's shares against the first resting sell
# it accepts and refuses the next one "Not enough shares to sell." Guard is
# where that stops being a rejection read out of a runlog the next morning.


def reserved(available=0.0, quantity=1.0, **overrides) -> dict:
    return context(
        positions=[
            {
                "symbol": "F",
                "quantity": quantity,
                "market_value": 14.68,
                "shares_available_for_sells": available,
                "shares_held_for_sells": quantity - available,
            }
        ],
        **overrides,
    )


def sell(quantity=1.0, **overrides) -> dict:
    return order(symbol="F", side="sell", type="limit", quantity=quantity,
                 limit_price=14.85, **overrides)


def test_a_sell_over_reserved_shares_is_refused_by_name():
    result = guard.evaluate([sell()], reserved())

    assert codes(result) == [guard.PROTECTION_CONTENDS]


def test_a_cancel_in_the_same_plan_releases_the_shares():
    """Step 8a performs cancels first, so the shares are back before the sell
    is sent. Without this the exit path would deny itself against the very
    reservation it is about to release."""
    ctx = reserved()
    ctx["cancelling_symbols"] = ["F"]

    assert codes(guard.evaluate([sell()], ctx)) == []


def test_a_cancel_on_another_symbol_releases_nothing():
    ctx = reserved()
    ctx["cancelling_symbols"] = ["PFE"]

    assert codes(guard.evaluate([sell()], ctx)) == [guard.PROTECTION_CONTENDS]


def test_a_position_with_enough_free_shares_is_untouched():
    assert codes(guard.evaluate([sell(quantity=1.0)], reserved(available=1.0))) == []


def test_a_partial_reservation_still_refuses_the_whole_sell():
    """There is no partial fill of a reservation to trade against: the broker
    refuses the order, it does not shrink it."""
    ctx = reserved(available=2.0, quantity=3.0)

    result = guard.evaluate([sell(quantity=3.0)], ctx)

    assert codes(result) == [guard.PROTECTION_CONTENDS]


def test_an_absent_reserved_count_denies_nothing():
    """§14 in the permissive direction here. A book row written before
    `shares_available_for_sells` was carried is not evidence of a reservation,
    and reading its absence as zero would deny every sell in the book."""
    assert codes(guard.evaluate([sell()], held())) == []


def test_a_buy_is_never_refused_for_reserved_shares():
    ctx = reserved()

    assert guard.PROTECTION_CONTENDS not in codes(
        guard.evaluate([order(symbol="F", side="buy", quantity=1.0)], ctx)
    )


def test_the_protective_order_itself_is_refused_over_a_reservation():
    """The denial is about the shares, not about who wants them — a second
    resting leg over a position that already has one is the case that produced
    all 16 rejections."""
    result = guard.evaluate([protective(protection.TARGET, 15.90)], reserved())

    assert guard.PROTECTION_CONTENDS in codes(result)
