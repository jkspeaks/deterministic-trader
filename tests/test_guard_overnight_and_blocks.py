"""The overnight spread gate, stale universe, and entry blocks.

Design v3 §6.6, §14, §12.2, §13.

§6.6's evidence is Appendix A.3 — 20 real quotes taken at ~20:00 ET — and the
design names the exact symbols the 1.0% gate must admit and exclude. Those
quotes are used verbatim here, so the gate is tested against measurement rather
than against invented numbers.
"""

import pytest

from src import guard
from tests.factories import codes, context, order

# Appendix A.3, verbatim. §6.6: the 1.0% gate "admits MU, AAPL, NVDA, AMD, SMH,
# PFE, HOOD, INTC and TSLA, and correctly excludes MRK (14.8%), TXN (8.5%),
# ADI (6.2%) and CAT (5.5%)."
OVERNIGHT_QUOTES = {
    "MU": (812.39, 812.50),
    "AAPL": (307.30, 307.35),
    "NVDA": (198.90, 198.95),
    "PFE": (25.00, 25.04),
    "HOOD": (86.00, 86.25),
    "AMD": (470.00, 472.15),
    "SMH": (537.50, 539.96),
    "INTC": (89.20, 89.91),
    "TSLA": (306.80, 309.30),
    "BAC": (61.44, 62.07),
    "PLTR": (120.05, 122.77),
    "MRK": (125.00, 145.00),
    "TXN": (263.09, 286.33),
    "ADI": (345.25, 367.40),
    "CAT": (791.14, 835.66),
}

ADMITTED = ["MU", "AAPL", "NVDA", "PFE", "HOOD", "AMD", "SMH", "INTC", "TSLA"]
EXCLUDED = ["MRK", "TXN", "ADI", "CAT"]


def overnight_context(symbol: str, bid: float, ask: float, **position_extra):
    """An overnight cycle holding one whole share of ``symbol``."""
    ctx = context(
        cycle={
            "cycle_type": "overnight",
            "session": "all_day_hours",
            "entries_allowed": False,
        },
        positions=[
            {
                "symbol": symbol,
                "quantity": 1.0,
                "market_value": ask,
                "play_id": "DIP",
                **position_extra,
            }
        ],
    )
    ctx["universe"]["candidates"].append(symbol)
    ctx["quotes"][symbol] = {"bid": bid, "ask": ask, "last": (bid + ask) / 2}
    ctx["sectors"][symbol] = "Technology"
    ctx["earnings"][symbol] = {
        "status": "known",
        "next_report_date": "2026-12-01",
        "sessions_until_report": 80,
    }
    ctx["tradability"][symbol] = {
        "tradeable": True,
        "fractional_tradability": "tradable",
        "all_day_tradability": "tradable",
    }
    return ctx


def overnight_exit(symbol: str, bid: float, quantity: float = 1.0):
    # Market and stop orders are regular-hours-only, so an overnight exit is
    # necessarily a limit order.
    return order(
        symbol=symbol,
        side="sell",
        type="limit",
        limit_price=bid,
        quantity=quantity,
        session="all_day_hours",
    )


# --- the spread gate ------------------------------------------------------


@pytest.mark.parametrize("symbol", ADMITTED)
def test_the_one_percent_gate_admits_the_tight_names(symbol):
    bid, ask = OVERNIGHT_QUOTES[symbol]
    ctx = overnight_context(symbol, bid, ask)
    exit_order = overnight_exit(symbol, bid)

    result = guard.evaluate([exit_order], ctx)

    assert result.approved == [exit_order], codes(result)


@pytest.mark.parametrize("symbol", EXCLUDED)
def test_the_one_percent_gate_excludes_the_wide_names(symbol):
    bid, ask = OVERNIGHT_QUOTES[symbol]
    ctx = overnight_context(symbol, bid, ask)

    result = guard.evaluate([overnight_exit(symbol, bid)], ctx)

    assert guard.OVERNIGHT_SPREAD_GATE in codes(result, symbol)


def test_bac_sits_just_the_wrong_side_of_the_gate():
    # 1.020% — the nearest exclusion to the boundary, and the reason the gate
    # is a measured threshold rather than a rounded one.
    bid, ask = OVERNIGHT_QUOTES["BAC"]
    ctx = overnight_context("BAC", bid, ask)

    result = guard.evaluate([overnight_exit("BAC", bid)], ctx)

    assert guard.OVERNIGHT_SPREAD_GATE in codes(result, "BAC")


def test_the_catastrophe_override_pays_a_wide_spread_to_escape():
    # §6.6: "paying a 2% spread to escape a position 15% past its stop is
    # correct, and a flat gate would forbid it."
    ctx = overnight_context("NVDA", 98.00, 100.00, loss_pct_below_stop=0.15)

    result = guard.evaluate([overnight_exit("NVDA", 98.00)], ctx)

    assert guard.OVERNIGHT_SPREAD_GATE not in codes(result, "NVDA")


def test_the_catastrophe_override_does_not_fire_on_a_shallow_loss():
    # 5% below the stop against a 2.02% spread misses the 3x multiple.
    ctx = overnight_context("NVDA", 98.00, 100.00, loss_pct_below_stop=0.05)

    result = guard.evaluate([overnight_exit("NVDA", 98.00)], ctx)

    assert guard.OVERNIGHT_SPREAD_GATE in codes(result, "NVDA")


def test_a_position_with_no_recorded_loss_gets_no_override():
    ctx = overnight_context("MRK", *OVERNIGHT_QUOTES["MRK"])

    result = guard.evaluate([overnight_exit("MRK", 125.00)], ctx)

    assert guard.OVERNIGHT_SPREAD_GATE in codes(result, "MRK")


def test_the_spread_gate_does_not_apply_during_regular_hours():
    # §6.6 gates overnight exits only. MRK's 14.8% overnight spread is not a
    # reason to refuse selling it at 10:00.
    ctx = context(
        positions=[
            {"symbol": "MRK", "quantity": 1.0, "market_value": 130.22,
             "play_id": "DIP"}
        ],
    )
    ctx["universe"]["candidates"].append("MRK")
    ctx["quotes"]["MRK"] = {"bid": 125.00, "ask": 145.00, "last": 130.22}
    ctx["sectors"]["MRK"] = "Health Care"
    ctx["earnings"]["MRK"] = {
        "status": "known", "next_report_date": "2026-12-01",
        "sessions_until_report": 80,
    }
    ctx["tradability"]["MRK"] = {
        "tradeable": True, "fractional_tradability": "tradable",
        "all_day_tradability": "tradable",
    }
    sell = order(symbol="MRK", side="sell", type="market", limit_price=None)

    result = guard.evaluate([sell], ctx)

    assert result.approved == [sell]


def test_an_unquoted_symbol_cannot_clear_the_gate_on_an_assumed_spread():
    # §6.6: "Spread is measured live at the moment of the exit decision, never
    # cached." With no live quote there is nothing to measure.
    ctx = overnight_context("NVDA", 198.90, 198.95)
    ctx["quotes"]["NVDA"] = {"bid": None, "ask": None, "last": 198.92}

    result = guard.evaluate(
        [order(symbol="NVDA", side="sell", type="limit", limit_price=198.90,
               quantity=1.0, session="all_day_hours")],
        ctx,
    )

    assert guard.OVERNIGHT_SPREAD_GATE in codes(result, "NVDA")


def test_an_overnight_cycle_cannot_exit_a_fractional_position():
    # P1 x §13: until the first whole share exists, the 04:30 and 20:00
    # overnight cycles can do nothing but observe.
    ctx = overnight_context("INTC", 89.20, 89.91)
    ctx["positions"][0]["quantity"] = 0.275534

    result = guard.evaluate([overnight_exit("INTC", 89.20, quantity=0.275534)], ctx)

    assert guard.FRACTIONAL_OUTSIDE_REGULAR_HOURS in codes(result, "INTC")


# --- stale universe -------------------------------------------------------


def test_a_universe_snapshot_older_than_fourteen_days_blocks_entries():
    result = guard.evaluate([order()], context(universe={"snapshot_age_days": 15}))

    assert guard.UNIVERSE_STALE in codes(result, "F")


def test_a_fourteen_day_old_snapshot_is_still_fresh_enough():
    result = guard.evaluate([order()], context(universe={"snapshot_age_days": 14}))

    assert guard.UNIVERSE_STALE not in codes(result, "F")


def test_a_stale_universe_still_allows_exits_on_held_positions():
    # §14: "Proceed on positions only; no new entries."
    ctx = context(
        universe={"snapshot_age_days": 30},
        positions=[
            {"symbol": "F", "quantity": 1.0, "market_value": 14.68, "play_id": "DIP"}
        ],
    )
    sell = order(side="sell", type="market", limit_price=None)

    result = guard.evaluate([sell], ctx)

    assert result.approved == [sell]


# --- entry blocks ---------------------------------------------------------


def test_a_reconciliation_block_stops_entries():
    # §12.2: PHANTOM_ORDER blocks entries one cycle; persistent divergence
    # blocks until cleared in edge_control.
    result = guard.evaluate(
        [order()],
        context(
            control={
                "entries_blocked": True,
                "entries_blocked_reason": "RECONCILIATION_DIVERGENCE_PERSISTENT",
            }
        ),
    )

    assert guard.ENTRIES_BLOCKED in codes(result, "F")


def test_a_reconciliation_block_still_allows_exits():
    ctx = context(
        control={"entries_blocked": True, "entries_blocked_reason": "PHANTOM_ORDER"},
        positions=[
            {"symbol": "F", "quantity": 1.0, "market_value": 14.68, "play_id": "DIP"}
        ],
    )
    sell = order(side="sell", type="market", limit_price=None)

    result = guard.evaluate([sell], ctx)

    assert result.approved == [sell]


def test_the_block_reason_reaches_the_denial_message():
    # §14 keeps broker text verbatim for the same reason: a bucketed code
    # diagnoses nothing when the operator reads the runlog a week later.
    result = guard.evaluate(
        [order()],
        context(
            control={
                "entries_blocked": True,
                "entries_blocked_reason": "RECONCILIATION_DIVERGENCE_PERSISTENT",
            }
        ),
    )

    message = next(
        d.message
        for denied in result.denied
        for d in denied.denials
        if d.code == guard.ENTRIES_BLOCKED
    )
    assert "RECONCILIATION_DIVERGENCE_PERSISTENT" in message


def test_a_cycle_that_does_not_allow_entries_denies_a_buy():
    # §13: premarket, postclose and overnight cycles place nothing.
    result = guard.evaluate([order()], context(cycle={"entries_allowed": False}))

    assert guard.ENTRIES_NOT_ALLOWED_THIS_CYCLE in codes(result, "F")


def test_a_cycle_that_does_not_allow_entries_still_allows_exits():
    ctx = context(
        cycle={"entries_allowed": False},
        positions=[
            {"symbol": "F", "quantity": 1.0, "market_value": 14.68, "play_id": "DIP"}
        ],
    )
    sell = order(side="sell", type="market", limit_price=None)

    result = guard.evaluate([sell], ctx)

    assert result.approved == [sell]
