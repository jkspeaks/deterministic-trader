"""Price resolution and execution-path selection (design v3 §4.3).

The path is not a preference — it decides the order type, which sessions are
reachable, and whether a resting stop can protect the position between cycles
(§10.1). Two rules govern the price:

* Sizing sizes against the price the order will actually transact at, so
  guard's costing and sizing's costing agree by construction. Guard costs a
  limit at ``limit_price``; the runbook's F case was $0.48 short precisely
  because the two used different numbers.
* A **staged** order — one that will sit unfilled across a session boundary —
  must carry a limit. That is the whole protection staging needs: a market
  order queued for the open takes whatever the open hands it.
"""

import pytest

from src import sizing
from tests.factories import candidate, config, context, entry, reasons

# equity 236.42 -> n=3, reserve 4.7284, deployable 95.2716, notional 31.7572
BASE_NOTIONAL = 31.7572


def admit(ctx, symbol: str, bid: float, ask: float, sector: str = "Technology"):
    ctx["quotes"][symbol] = {"bid": bid, "ask": ask, "last": (bid + ask) / 2}
    ctx["sectors"][symbol] = sector
    return ctx


# --- the formula (§4.3) ---------------------------------------------------


def test_a_price_inside_the_notional_takes_the_whole_path():
    path, quantity = sizing.select_path(price=14.87, notional=31.7572)

    assert path == sizing.WHOLE
    assert quantity == 2


def test_a_price_above_the_notional_takes_the_fractional_path():
    path, quantity = sizing.select_path(price=198.90, notional=31.7572)

    assert path == sizing.FRACTIONAL
    assert quantity == pytest.approx(0.159664, abs=5e-7)


def test_a_price_exactly_equal_to_the_notional_buys_one_whole_share():
    # §4.3 also asks for whole_shares >= 1, which this shows is implied:
    # price <= notional can never floor to zero shares.
    path, quantity = sizing.select_path(price=25.03, notional=25.03)

    assert path == sizing.WHOLE
    assert quantity == 1


def test_a_fractional_quantity_is_floored_not_rounded():
    """Rounding a fractional quantity up overspends the notional.

    At $198.90 the exact quantity is 0.1596641..., and the sixth decimal is
    where Robinhood's precision stops. Rounding to 0.159665 would cost more
    than the cycle budgeted.
    """
    _, quantity = sizing.select_path(price=198.90, notional=31.7572)

    assert quantity == 0.159664


# --- what the path implies ------------------------------------------------


def test_the_whole_path_is_a_limit_order_with_a_resting_stop():
    result = sizing.size([candidate()], context())

    sized = entry(result, "F")
    assert sized.path == sizing.WHOLE
    assert sized.order_type == "limit"
    assert sized.resting_stop_eligible is True
    assert sized.sessions_eligible == ("regular_hours", "extended_hours")


def test_the_fractional_path_is_a_regular_hours_market_order_with_no_stop():
    """§10.1: a fractional position has no protection between cycles.

    The path itself is the reason, which is why §4.1 argues that fewer, larger
    positions is the safe configuration rather than the risky one. Exercised
    with whole_shares_only off since this is the mechanics of the fractional
    path itself, still reachable when an operator opts back in.
    """
    ctx = admit(context(config=config(whole_shares_only=False)), "NVDA", 198.85, 198.90)

    result = sizing.size([candidate(symbol="NVDA")], ctx)

    sized = entry(result, "NVDA")
    assert sized.path == sizing.FRACTIONAL
    assert sized.order_type == "market"
    assert sized.resting_stop_eligible is False
    assert sized.sessions_eligible == ("regular_hours",)


# --- which price sizing uses ----------------------------------------------


def test_an_immediate_order_with_no_limit_and_no_atr_is_sized_at_the_ask():
    # `context()` carries no `signals`, so `_marketable_limit` has no ATR to
    # mark up with and falls back to the plain ask — an un-marked limit that
    # might not fill still beats no order at all.
    result = sizing.size([candidate()], context())

    sized = entry(result, "F")
    assert sized.price == pytest.approx(14.87)
    assert sized.price_source == "ask"


def test_a_whole_entry_with_no_limit_marks_the_ask_up_by_the_atr_offset():
    """A bare ask sat unfilled on 3 of 4 buys. With ATR(14) on hand the WHOLE path's
    limit is ask + `entry_limit_offset_atr` x ATR, rounded to the cent — 14.87 +
    0.10 x 0.50 = 14.92 — so the limit is marketable and sizing budgets against
    the price it will actually cross at."""
    result = sizing.size([candidate()], context(signals={"F": {"atr": 0.50}}))

    sized = entry(result, "F")
    assert sized.limit_price == pytest.approx(14.92)
    assert sized.price == pytest.approx(14.92)
    assert sized.quantity * sized.limit_price == pytest.approx(sized.notional)


def test_the_atr_offset_is_config():
    result = sizing.size(
        [candidate()],
        context(signals={"F": {"atr": 0.50}},
                config=config(entry_limit_offset_atr=0.40)),
    )

    # 14.87 + 0.40 x 0.50 = 15.07
    assert entry(result, "F").limit_price == pytest.approx(15.07)


def test_a_supplied_limit_is_never_marked_up():
    """The markup is only for the no-limit case. A play that named its price
    means it, and guard costs that number unchanged."""
    result = sizing.size(
        [candidate(limit_price=15.00)], context(signals={"F": {"atr": 5.0}})
    )

    assert entry(result, "F").limit_price == pytest.approx(15.00)


def test_a_supplied_limit_is_the_price_sizing_uses():
    # The play intends to pay 15.00; sizing must budget 15.00, not the 14.68
    # ask, because guard costs the order at the limit.
    result = sizing.size([candidate(limit_price=15.00)], context())

    sized = entry(result, "F")
    assert sized.price == pytest.approx(15.00)
    assert sized.price_source == "limit"
    assert sized.quantity == 2
    assert sized.notional == pytest.approx(30.00)


def test_the_emitted_limit_price_is_the_price_sized_against():
    # Guard and sizing must reach the same notional from the same number.
    result = sizing.size([candidate(limit_price=15.00)], context())

    sized = entry(result, "F")
    assert sized.limit_price == pytest.approx(15.00)
    assert sized.quantity * sized.limit_price == pytest.approx(sized.notional)


def test_a_fractional_market_order_carries_no_limit_price():
    ctx = admit(context(config=config(whole_shares_only=False)), "NVDA", 198.85, 198.90)

    result = sizing.size([candidate(symbol="NVDA")], ctx)

    assert entry(result, "NVDA").limit_price is None


def test_a_candidate_with_neither_a_limit_nor_a_quote_cannot_be_sized():
    ctx = context()
    del ctx["quotes"]["F"]

    result = sizing.size([candidate()], ctx)

    assert reasons(result, "F") == [sizing.NO_PRICE]


def test_a_limit_above_the_notional_still_needs_a_quote_to_cost_the_fill():
    """A $500 limit on a $31.76 slot sizes fractional, and a fractional order
    fills at market — so the limit that chose the path cannot also price it.
    Without an ask there is nothing left to cost the order against."""
    ctx = context(config=config(whole_shares_only=False))
    del ctx["quotes"]["F"]

    result = sizing.size([candidate(limit_price=500.00)], ctx)

    assert reasons(result, "F") == [sizing.NO_PRICE]


# --- staged orders --------------------------------------------------------


def test_a_staged_order_without_a_limit_is_refused():
    """The MRK rule: an order queued for a session that is not open yet must
    name its price, or a gap between placement and the open is taken at
    whatever the market offers."""
    ctx = context(cycle={"session": "extended_hours"})

    result = sizing.size([candidate(session="regular_hours")], ctx)

    assert reasons(result, "F") == [sizing.STAGED_ORDER_NEEDS_LIMIT]


def test_a_staged_order_with_a_limit_is_sized_at_that_limit():
    ctx = context(cycle={"session": "extended_hours"})

    result = sizing.size([candidate(session="regular_hours", limit_price=15.00)], ctx)

    sized = entry(result, "F")
    assert sized.price == pytest.approx(15.00)
    assert sized.session == "regular_hours"


def test_an_order_into_the_open_session_is_not_staged():
    # Same session as the cycle: it is placed now, so the ask is a live price
    # and a limit is optional (§4.3).
    result = sizing.size([candidate(session="regular_hours")], context())

    assert entry(result, "F") is not None


def test_a_staged_fractional_order_is_refused():
    """A fractional order is market-only (§4.3), and a market order cannot
    carry the limit that staging requires. The two rules meet here, and at
    $236 that means overnight and pre-market cycles stage nothing — which is
    what the probe findings already concluded for exits."""
    ctx = admit(
        context(cycle={"session": "extended_hours"}, config=config(whole_shares_only=False)),
        "NVDA", 198.85, 198.90,
    )

    result = sizing.size(
        [candidate(symbol="NVDA", session="regular_hours", limit_price=198.90)], ctx
    )

    assert reasons(result, "NVDA") == [sizing.STAGED_FRACTIONAL_MARKET]


def test_a_fractional_candidate_cannot_target_an_extended_session():
    # P1 closed this at the tool surface: fractional and dollar-based orders
    # are regular-hours-only regardless of the per-symbol flag.
    ctx = admit(
        context(cycle={"session": "extended_hours"}, config=config(whole_shares_only=False)),
        "NVDA", 198.85, 198.90,
    )

    result = sizing.size([candidate(symbol="NVDA", session="extended_hours")], ctx)

    assert reasons(result, "NVDA") == [sizing.PATH_SESSION_INELIGIBLE]


# --- whole_shares_only -----------------------


def test_whole_shares_only_refuses_a_slot_that_cannot_afford_one_share():
    ctx = admit(context(), "NVDA", 198.85, 198.90)

    result = sizing.size([candidate(symbol="NVDA")], ctx)

    assert entry(result, "NVDA") is None
    assert reasons(result, "NVDA") == [sizing.WHOLE_SHARE_ONLY]


def test_whole_shares_only_off_still_sizes_fractional():
    ctx = admit(context(config=config(whole_shares_only=False)), "NVDA", 198.85, 198.90)

    result = sizing.size([candidate(symbol="NVDA")], ctx)

    sized = entry(result, "NVDA")
    assert sized.path == sizing.FRACTIONAL


def test_the_target_session_defaults_to_the_cycle_session():
    ctx = context(cycle={"session": "extended_hours"})

    result = sizing.size([candidate(limit_price=15.00)], ctx)

    assert entry(result, "F").session == "extended_hours"


# --- the ladder is reported alongside the entries -------------------------


def test_the_result_carries_the_rung_it_sized_at():
    result = sizing.size([candidate()], context())

    assert result.ladder.n_target == 3
    assert result.ladder.open_slots == 3
    assert result.ladder.deployable == pytest.approx(95.2716)
    assert result.ladder.notional == pytest.approx(BASE_NOTIONAL)


def test_an_unknown_settled_balance_sizes_nothing():
    # §14: Invariant 1 fails closed. No entry may be sized against a balance
    # that could not be established. Under the conservative policy a missing
    # `settled_cash` is that case; P3 gave the resolved policy `buying_power`
    # to fall back to, so the unknown balance has to be the whole account now.
    ctx = context(
        account={"settled_cash": None, "buying_power": None},
        config=config(settled_cash_policy=sizing.POLICY_CONSERVATIVE),
    )

    result = sizing.size([candidate()], ctx)

    assert result.entries == []
    assert reasons(result, "F") == [sizing.SETTLED_CASH_UNKNOWN]
    assert result.ladder.deployable is None


def test_config_drives_the_rung_without_a_code_change():
    # Invariant 5: capability is computed, never configured — the only knob
    # is the minimum position size, and it moves the whole ladder.
    ctx = context(config=config(min_position_size=40.0))

    result = sizing.size([candidate()], ctx)

    assert result.ladder.n_target == 6
