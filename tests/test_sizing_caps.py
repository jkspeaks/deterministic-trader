"""The budget walk: slots, caps and cash consumed in rank order (§4.2, §4.4).

Guard denies an order that breaches a cap. Sizing's job is to not propose one:
the notional is shrunk to whatever the position cap, the sector cap and the
remaining cash all allow, and a candidate that cannot reach the $1 broker floor
is returned unsized rather than as an order guard will refuse.

The walk is sequential for the same reason guard's is. Two buys sized
independently against the same sector headroom breach it together.
"""

import pytest

from src import sizing
from tests.factories import candidate, config, context, entry, reasons

CD = "Consumer Discretionary"


def book(equity: float, cash: float, holdings: dict[str, tuple[float, str]]):
    """A context holding ``holdings`` as {symbol: (market_value, sector)}."""
    ctx = context(
        account={
            "equity_total": equity,
            "cash": cash,
            "buying_power": cash,
            "settled_cash": cash,
        },
        control={"high_water_mark": equity},
        positions=[
            {"symbol": s, "quantity": 1.0, "market_value": v}
            for s, (v, _) in holdings.items()
        ],
        # This suite is about the §4.2/§4.4 budget walk, not an earlier
        # whole_shares_only gate, so it opts back into fractional sizing.
        config=config(whole_shares_only=False),
    )
    for symbol, (_, sector) in holdings.items():
        ctx["sectors"][symbol] = sector
        ctx["quotes"][symbol] = {"bid": 99.90, "ask": 100.00, "last": 99.95}
    return ctx


def admit(ctx, symbol: str, ask: float, sector: str | None = CD):
    ctx["quotes"][symbol] = {"bid": ask - 0.05, "ask": ask, "last": ask}
    if sector is not None:
        ctx["sectors"][symbol] = sector
    return ctx


# --- the position cap -----------------------------------------------------


def test_the_notional_is_clamped_to_the_position_cap():
    # One open slot and $195 deployable would put the whole balance into one
    # name. The cap at n=3 is 40% of equity — $94.57.
    ctx = book(236.42, 200.00, {"TSLA": (60.00, "Technology"), "PFE": (60.00, "Health Care")})

    result = sizing.size([candidate()], ctx)

    sized = entry(result, "F")
    assert result.ladder.notional > result.ladder.position_cap  # the clamp bit
    assert sized.quantity == 6
    assert sized.notional == pytest.approx(89.22)


def test_the_clamp_counts_what_is_already_held_in_that_name():
    # Holding $85 of F leaves $9.57 of headroom, which is less than one
    # share — so the top-up drops to the fractional path.
    ctx = book(236.42, 200.00, {"F": (85.00, CD)})

    result = sizing.size([candidate()], ctx)

    sized = entry(result, "F")
    assert sized.path == sizing.FRACTIONAL
    assert sized.notional == pytest.approx(9.568, abs=0.001)


def test_a_name_already_at_its_cap_is_refused():
    ctx = book(236.42, 200.00, {"F": (95.00, CD)})

    result = sizing.size([candidate()], ctx)

    assert reasons(result, "F") == [sizing.NO_CAP_HEADROOM]


# --- the sector cap -------------------------------------------------------


def test_the_notional_is_clamped_to_the_sector_cap():
    # TSLA already holds $100 of Consumer Discretionary against a $118.21 cap,
    # so F gets the $18.21 that is left — one share, not four.
    ctx = book(236.42, 200.00, {"TSLA": (100.00, CD)})

    result = sizing.size([candidate()], ctx)

    assert entry(result, "F").quantity == 1


def test_two_candidates_in_one_sector_cannot_breach_it_together():
    """Each fits the sector cap alone; sized independently they would not.

    This is the case that makes the walk sequential rather than a map.
    """
    ctx = admit(book(236.42, 200.00, {}), "TSLA", 300.00)

    result = sizing.size(
        [candidate(conviction=0.90), candidate(symbol="TSLA", conviction=0.80)], ctx
    )

    total = sum(e.notional for e in result.entries)
    assert len(result.entries) == 2
    assert total <= result.ladder.sector_cap + 0.005


def test_a_sector_that_cannot_be_established_is_refused():
    # Guard fails closed on an unknown sector; sizing must not hand it an
    # order that is certain to be denied.
    ctx = admit(book(236.42, 200.00, {}), "ZZZZ", 20.00, sector=None)

    result = sizing.size([candidate(symbol="ZZZZ")], ctx)

    assert reasons(result, "ZZZZ") == [sizing.SECTOR_UNKNOWN]


# --- slots ----------------------------------------------------------------


def test_a_new_name_with_no_free_slot_is_refused():
    ctx = book(
        236.42,
        100.00,
        {"TSLA": (50.00, "Technology"), "PFE": (50.00, "Health Care"), "XLE": (50.00, "Energy")},
    )

    result = sizing.size([candidate()], ctx)

    assert reasons(result, "F") == [sizing.NO_OPEN_SLOTS]


def test_a_top_up_of_a_held_name_needs_no_free_slot():
    ctx = book(
        236.42,
        100.00,
        {"TSLA": (50.00, "Technology"), "PFE": (50.00, "Health Care"), "XLE": (50.00, "Energy")},
    )

    result = sizing.size([candidate(symbol="PFE")], ctx)

    assert entry(result, "PFE") is not None


def test_slots_are_consumed_within_a_single_plan():
    ctx = admit(
        book(236.42, 100.00, {"TSLA": (50.00, "Technology"), "PFE": (50.00, "Health Care")}),
        "BAC",
        61.97,
        sector="Financials",
    )

    result = sizing.size(
        [candidate(conviction=0.90), candidate(symbol="BAC", conviction=0.80)], ctx
    )

    assert [e.symbol for e in result.entries] == ["F"]
    assert reasons(result, "BAC") == [sizing.NO_OPEN_SLOTS]


# --- cash -----------------------------------------------------------------


def test_cash_is_consumed_in_rank_order_until_it_runs_out():
    """Three top-ups against a balance that funds two.

    Top-ups consume no slot, so nothing else stops the third — only the
    money, which is the point.
    """
    ctx = book(
        236.42,
        100.00,
        {"TSLA": (1.00, "Technology"), "PFE": (1.00, "Health Care"), "XLE": (1.00, "Energy")},
    )

    result = sizing.size(
        [
            candidate(symbol="TSLA", conviction=0.90),
            candidate(symbol="PFE", conviction=0.80),
            candidate(symbol="XLE", conviction=0.70),
        ],
        ctx,
    )

    assert [e.symbol for e in result.entries] == ["TSLA", "PFE"]
    assert reasons(result, "XLE") == [sizing.DEPLOYABLE_EXHAUSTED]


def test_the_sum_of_a_plan_never_exceeds_the_deployable_balance():
    ctx = book(
        236.42,
        100.00,
        {"TSLA": (1.00, "Technology"), "PFE": (1.00, "Health Care"), "XLE": (1.00, "Energy")},
    )

    result = sizing.size(
        [
            candidate(symbol="TSLA", conviction=0.90),
            candidate(symbol="PFE", conviction=0.80),
            candidate(symbol="XLE", conviction=0.70),
        ],
        ctx,
    )

    assert sum(e.notional for e in result.entries) <= result.ladder.deployable + 0.005


# --- the broker's $1 floor ------------------------------------------------


def test_a_slot_too_thin_to_reach_the_broker_floor_is_refused():
    # $2 deployable across three slots is 67c a position. The account is not
    # broken; it is simply too thin to open anything this cycle.
    ctx = book(236.42, 6.7284, {})

    result = sizing.size([candidate()], ctx)

    assert reasons(result, "F") == [sizing.BELOW_MIN_NOTIONAL]


def test_a_fractional_quantity_that_floors_below_the_floor_is_refused():
    """$1.00 of NVDA floors to 0.005027 shares, which is 99.987c.

    The budget clears the broker's floor and the order that comes out of it
    does not — so the check has to be on the order, not the budget.
    """
    ctx = admit(book(236.42, 7.7284, {}), "NVDA", 198.90, sector="Technology")

    result = sizing.size([candidate(symbol="NVDA")], ctx)

    assert reasons(result, "NVDA") == [sizing.BELOW_MIN_NOTIONAL]
