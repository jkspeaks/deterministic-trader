"""Concentration caps and the slot ceiling (design v3 §4.4, §10, §15).

The caps scale with ``n`` on purpose. §4.4: "A flat 15% cap is unsatisfiable at
three positions and would deadlock the system at low capital — a failure mode
worth naming, because a guard that can never pass is indistinguishable from a
broken system." §15 therefore requires the satisfiability test at n = 3, 6, 12
that closes this file.
"""

import pytest

from src import guard
from tests.factories import codes, context, order


def _tradable():
    return {
        "tradeable": True,
        "fractional_tradability": "tradable",
        "all_day_tradability": "tradable",
    }


def _clear_earnings():
    return {"status": "known", "next_report_date": "2026-12-01", "sessions_until_report": 80}


def book(equity: float, n_target: int, holdings: dict[str, tuple[float, str]]):
    """A context holding ``holdings`` as {symbol: (market_value, sector)}."""
    ctx = context(
        sizing={"n_target": n_target},
        account={
            "equity_total": equity,
            "settled_cash": 100_000.0,
            "buying_power": 100_000.0,
        },
        control={"high_water_mark": equity},
        positions=[
            {"symbol": s, "quantity": 1.0, "market_value": v, "play_id": "DIP"}
            for s, (v, _) in holdings.items()
        ],
    )
    for symbol, (_, sector) in holdings.items():
        ctx["sectors"][symbol] = sector
        ctx["quotes"][symbol] = {"bid": 99.0, "ask": 100.0, "last": 99.5}
        ctx["earnings"][symbol] = _clear_earnings()
        ctx["tradability"][symbol] = _tradable()
        ctx["universe"]["candidates"].append(symbol)
    return ctx


def admit(ctx, symbol: str, sector: str, notional: float):
    """Register ``symbol`` as buyable and return an order for ``notional``."""
    ctx["sectors"][symbol] = sector
    ctx["quotes"][symbol] = {"bid": 99.0, "ask": 100.0, "last": 99.5}
    ctx["earnings"][symbol] = _clear_earnings()
    ctx["tradability"][symbol] = _tradable()
    if symbol not in ctx["universe"]["candidates"]:
        ctx["universe"]["candidates"].append(symbol)
    return order(symbol=symbol, type="limit", limit_price=notional, quantity=1.0)


# --- the formulas ---------------------------------------------------------


@pytest.mark.parametrize(
    "n,expected",
    [(1, 1.20), (3, 0.40), (4, 0.30), (6, 0.20), (8, 0.15), (12, 0.15)],
)
def test_position_cap_is_max_of_fifteen_percent_and_twelve_tenths_over_n(n, expected):
    # §4.4: 40% at n=3, scaling to 15% at n>=8.
    assert guard.position_cap_fraction(n) == pytest.approx(expected)


@pytest.mark.parametrize(
    "n,expected",
    [(1, 1.50), (3, 0.50), (4, 0.375), (5, 0.30), (8, 0.30), (12, 0.30)],
)
def test_sector_cap_is_max_of_thirty_percent_and_fifteen_tenths_over_n(n, expected):
    # §4.4: 50% at n=3, scaling to 30% at n>=5.
    assert guard.sector_cap_fraction(n) == pytest.approx(expected)


@pytest.mark.parametrize("cap", ["position_cap_fraction", "sector_cap_fraction"])
def test_a_degenerate_slot_count_does_not_divide_by_zero(cap):
    # sizing.py clamps n to [3, 12], but guard must not be the thing that
    # crashes if that contract is ever broken upstream.
    assert getattr(guard, cap)(0) == getattr(guard, cap)(1)


# --- position cap ---------------------------------------------------------


def test_a_buy_above_the_scaled_position_cap_is_denied():
    # n=3 -> cap 40% of 236.42 = 94.57. A 105.00 buy breaches it.
    ctx = book(236.42, 3, {})
    proposed = admit(ctx, "F", "Consumer Discretionary", 105.00)

    result = guard.evaluate([proposed], ctx)

    assert guard.POSITION_CAP_EXCEEDED in codes(result, "F")


def test_a_buy_just_under_the_scaled_position_cap_is_approved():
    ctx = book(236.42, 3, {})
    proposed = admit(ctx, "F", "Consumer Discretionary", 90.00)

    result = guard.evaluate([proposed], ctx)

    assert result.approved == [proposed]


def test_the_position_cap_counts_what_is_already_held():
    # Holding 90.00 of F, a further 10.00 lands at 100.00 against a 94.57 cap.
    # Sizing the top-up alone would slip past the cap one purchase at a time.
    ctx = book(236.42, 3, {"F": (90.00, "Consumer Discretionary")})
    proposed = admit(ctx, "F", "Consumer Discretionary", 10.00)

    result = guard.evaluate([proposed], ctx)

    assert guard.POSITION_CAP_EXCEEDED in codes(result, "F")


def test_a_larger_n_tightens_the_position_cap():
    # The same 90.00 buy that passes at n=3 fails at n=12 (cap 15% = 35.46).
    ctx = book(236.42, 12, {})
    proposed = admit(ctx, "F", "Consumer Discretionary", 90.00)

    result = guard.evaluate([proposed], ctx)

    assert guard.POSITION_CAP_EXCEEDED in codes(result, "F")


def test_the_position_cap_never_blocks_a_sell():
    ctx = book(236.42, 3, {"F": (150.00, "Consumer Discretionary")})
    sell = order(side="sell", type="market", limit_price=None, quantity=1.0)

    result = guard.evaluate([sell], ctx)

    assert result.approved == [sell]


# --- sector cap -----------------------------------------------------------


def test_a_buy_above_the_scaled_sector_cap_is_denied():
    # n=3 -> sector cap 50% of 236.42 = 118.21. TSLA already holds 60.00 of
    # Consumer Discretionary; a further 60.00 of F reaches 120.00.
    ctx = book(236.42, 3, {"TSLA": (60.00, "Consumer Discretionary")})
    proposed = admit(ctx, "F", "Consumer Discretionary", 60.00)

    result = guard.evaluate([proposed], ctx)

    assert guard.SECTOR_CAP_EXCEEDED in codes(result, "F")


def test_the_same_buy_in_a_different_sector_is_approved():
    ctx = book(236.42, 3, {"TSLA": (60.00, "Consumer Discretionary")})
    proposed = admit(ctx, "PFE", "Health Care", 60.00)

    result = guard.evaluate([proposed], ctx)

    assert result.approved == [proposed]


def test_an_unknown_sector_fails_closed_on_a_buy():
    # The sector cap cannot be evaluated without a sector, and an
    # unverifiable cap must deny rather than wave the order through.
    ctx = book(236.42, 3, {})
    proposed = admit(ctx, "F", "Consumer Discretionary", 50.00)
    del ctx["sectors"]["F"]

    result = guard.evaluate([proposed], ctx)

    assert guard.SECTOR_UNKNOWN in codes(result, "F")


def test_an_unknown_sector_still_allows_the_exit():
    ctx = book(236.42, 3, {"F": (50.00, "Consumer Discretionary")})
    del ctx["sectors"]["F"]
    sell = order(side="sell", type="market", limit_price=None, quantity=1.0)

    result = guard.evaluate([sell], ctx)

    assert result.approved == [sell]


# --- slot ceiling ---------------------------------------------------------


def test_the_n_plus_first_position_is_denied():
    ctx = book(
        236.42,
        3,
        {
            "TSLA": (60.00, "Consumer Discretionary"),
            "PFE": (60.00, "Health Care"),
            "XLE": (60.00, "Energy"),
        },
    )
    proposed = admit(ctx, "F", "Financials", 20.00)

    result = guard.evaluate([proposed], ctx)

    assert guard.SLOT_LIMIT_REACHED in codes(result, "F")


def test_filling_the_last_open_slot_is_approved():
    ctx = book(
        236.42,
        3,
        {"TSLA": (60.00, "Consumer Discretionary"), "PFE": (60.00, "Health Care")},
    )
    proposed = admit(ctx, "XLE", "Energy", 20.00)

    result = guard.evaluate([proposed], ctx)

    assert result.approved == [proposed]


def test_adding_to_a_held_name_does_not_need_a_free_slot():
    # A top-up consumes no slot: the book still holds three names.
    ctx = book(
        236.42,
        3,
        {
            "TSLA": (60.00, "Consumer Discretionary"),
            "PFE": (60.00, "Health Care"),
            "XLE": (60.00, "Energy"),
        },
    )
    proposed = admit(ctx, "PFE", "Health Care", 20.00)

    result = guard.evaluate([proposed], ctx)

    assert guard.SLOT_LIMIT_REACHED not in codes(result, "PFE")


def test_slots_are_consumed_within_a_single_plan():
    # Two new names proposed into one free slot: the second must be denied.
    ctx = book(
        236.42,
        3,
        {"TSLA": (60.00, "Consumer Discretionary"), "PFE": (60.00, "Health Care")},
    )
    first = admit(ctx, "XLE", "Energy", 20.00)
    second = admit(ctx, "F", "Financials", 20.00)

    result = guard.evaluate([first, second], ctx)

    assert result.approved == [first]
    assert guard.SLOT_LIMIT_REACHED in codes(result, "F")


# --- satisfiability (§15) -------------------------------------------------


@pytest.mark.parametrize("n", [3, 6, 12])
def test_an_equal_weight_book_of_n_positions_is_admissible(n):
    """The caps must admit the book the ladder is trying to build.

    A cap that can never pass is indistinguishable from a broken system, so
    this asserts the positive case: n equal positions in n distinct sectors
    clear both the position cap and the sector cap at every rung.
    """
    equity = 1200.0
    slice_value = equity / n
    holdings = {f"SYM{i}": (slice_value, f"Sector{i}") for i in range(n - 1)}

    ctx = book(equity, n, holdings)
    proposed = admit(ctx, f"SYM{n - 1}", f"Sector{n - 1}", slice_value)

    result = guard.evaluate([proposed], ctx)

    assert result.approved == [proposed], codes(result)


# --- §17's SURGE full-send bypass --------------------------------


def test_a_full_deployment_order_skips_every_concentration_cap():
    """A full-send order breaches the slot ceiling, the position cap and the
    sector cap at once, and is approved anyway — `sizing` has already committed
    the whole balance to it and §4.4 is the one guard category SURGE waives."""
    ctx = book(
        236.42,
        3,
        {
            "A": (60.0, "Technology"),
            "B": (60.0, "Technology"),
            "C": (60.0, "Financials"),
        },
    )
    proposed = admit(ctx, "UP", "Technology", 5_000.00)
    proposed["full_deployment"] = True

    result = guard.evaluate([proposed], ctx)

    assert result.approved == [proposed], codes(result)
    assert guard.SLOT_LIMIT_REACHED not in codes(result, "UP")
    assert guard.POSITION_CAP_EXCEEDED not in codes(result, "UP")
    assert guard.SECTOR_CAP_EXCEEDED not in codes(result, "UP")


def test_the_bypass_does_not_leak_to_an_ordinary_order_in_the_same_plan():
    ctx = book(236.42, 3, {})
    surge = admit(ctx, "UP", "Technology", 5_000.00)
    surge["full_deployment"] = True
    ordinary = admit(ctx, "F", "Consumer Discretionary", 105.00)  # over the 94.57 cap

    result = guard.evaluate([surge, ordinary], ctx)

    assert surge in result.approved
    assert guard.POSITION_CAP_EXCEEDED in codes(result, "F")
