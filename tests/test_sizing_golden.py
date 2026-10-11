"""The capital ladder, golden-file tested at five rungs (design v3 §15).

"sizing.py is golden-file tested across the capital ladder: $236, $500,
$1,000, $5,000, $10,000 x a fixed candidate set, asserting position count,
notional, and WHOLE/FRACTIONAL path selection at each."

One candidate set, priced from Appendix A.4's measured quotes, run against five
equities. Because the set spans $14.68 to $746.81, the WHOLE frontier sweeps
visibly across it as the account is funded: two whole names at $236, all nine at
$10,000. That sweep is the ladder, and it happens with no code change and no
threshold to maintain (Invariant 5).

The explicit assertions below are the specification. The golden files catch
everything else that moved.
"""

import json
from pathlib import Path

import pytest

from src import sizing
from tests.factories import config

GOLDEN = Path(__file__).parent / "golden" / "sizing"

# Appendix A.4, plus A.3's bid where one was measured. Fixed forever: a golden
# file that re-derives its own inputs tests nothing.
QUOTES = {
    "F": (14.62, 14.68),
    "SOFI": (16.25, 16.31),
    "T": (23.21, 23.27),
    "PFE": (24.97, 25.03),
    "BAC": (61.91, 61.97),
    "INTC": (90.15, 90.21),
    "NVDA": (198.84, 198.90),
    "AMD": (476.18, 476.24),
    "SPY": (746.75, 746.81),
}

SECTORS = {
    "F": "Consumer Discretionary",
    "SOFI": "Financials",
    "T": "Communication Services",
    "PFE": "Health Care",
    "BAC": "Financials",
    "INTC": "Technology",
    "NVDA": "Technology",
    "AMD": "Technology",
    "SPY": "Index",
}

# Three Technology names on purpose, so the sector cap has something to bite.
CONVICTION = {
    "NVDA": 0.92,
    "F": 0.88,
    "PFE": 0.85,
    "AMD": 0.80,
    "BAC": 0.76,
    "INTC": 0.72,
    "T": 0.68,
    "SOFI": 0.64,
    "SPY": 0.60,
}

CANDIDATES = [
    {
        "symbol": symbol,
        "conviction": CONVICTION[symbol],
        "play_id": "DIP",
        "limit_price": None,
        "session": None,
    }
    for symbol in QUOTES
]


def rung(equity: float) -> dict:
    """A clean slate at ``equity``: all cash, nothing held.

    §A.1 forecasts near-full liquidation on day one, so an all-cash account is
    the state the ladder actually starts from rather than a convenience.
    """
    return {
        "cycle": {
            "cycle_type": "full",
            "session": "regular_hours",
            "now": "2026-08-03T13:45:00Z",
            "entries_allowed": True,
        },
        "account": {
            "equity_total": equity,
            "cash": equity,
            "buying_power": equity,
            "settled_cash": equity,
            "sold_this_session": False,
        },
        "positions": [],
        "quotes": {s: {"bid": b, "ask": a, "last": a} for s, (b, a) in QUOTES.items()},
        "sectors": dict(SECTORS),
        # §15's ladder spec predates an earlier whole_shares_only ruling and
        # is deliberately about the WHOLE/FRACTIONAL frontier itself, so this
        # suite opts back into fractional sizing rather than testing the new
        # gate (see test_sizing_path.py for that).
        "config": config(whole_shares_only=False),
    }


def paths(result) -> dict[str, str]:
    return {e.symbol: e.path for e in result.entries}


# --- §15's three named assertions, at each rung ---------------------------


def test_the_account_as_it_stands_today_supports_three_positions():
    # §4.1: "At today's $236 that is 3 positions." Six candidates are refused
    # for want of a slot, which is the ladder working, not a failure.
    result = sizing.size(CANDIDATES, rung(236.00))

    assert result.ladder.n_target == 3
    assert result.ladder.notional == pytest.approx(77.0933, abs=1e-4)
    assert len(result.entries) == 3
    assert paths(result) == {"F": "WHOLE", "NVDA": "FRACTIONAL", "PFE": "WHOLE"}


def test_five_hundred_dollars_still_floors_at_three_but_buys_nvda_whole():
    # At the $200 rung $500 is 2.5 positions, so the book floors at 3 — but
    # each slot is now ~$163, enough to hold a whole share of NVDA, which the
    # $80 rung could only ever size fractional.
    result = sizing.size(CANDIDATES, rung(500.00))

    assert result.ladder.n_target == 3
    assert len(result.entries) == 3
    assert paths(result) == {"F": "WHOLE", "NVDA": "WHOLE", "PFE": "WHOLE"}


def test_a_thousand_dollars_fills_five_slots():
    # $1,000 at the $200 rung is 5 positions of ~$196 each. The top five by
    # conviction are sized (NVDA now whole, where the $80 rung's ~$75 slot
    # forced it fractional); the rest are left unsized for want of a slot,
    # which is the ladder working, not a failure.
    result = sizing.size(CANDIDATES, rung(1_000.00))

    assert result.ladder.n_target == 5
    assert len(result.entries) == 5
    assert {e.symbol for e in result.entries} == {"F", "NVDA", "PFE", "BAC", "AMD"}
    assert paths(result)["NVDA"] == "WHOLE"
    assert len(result.unsized) == 4


def test_five_thousand_dollars_leaves_only_the_dearest_names_fractional():
    # 25 slots: `MIN_POSITION_WEIGHT`'s ceiling, reached at $2,000 and flat
    # above it. Under the old constant this rung read 12 and NVDA was whole.
    #
    # AMD and SPY stay fractional — their even split is nowhere near a half
    # share here, so rounding either up would spend well past this rung's
    # fair slice and is deliberately not attempted (see
    # `test_a_wider_book_costs_whole_shares_at_the_top_of_the_ladder` for the
    # equity level where they do cross that line).
    result = sizing.size(CANDIDATES, rung(5_000.00))

    assert result.ladder.n_target == 25
    assert {s for s, p in paths(result).items() if p == "FRACTIONAL"} == {
        "AMD", "SPY"
    }


def test_a_wider_book_costs_whole_shares_at_the_top_of_the_ladder():
    """The price of the 25-name ceiling, pinned so it is not discovered later.

    §4.1's argument is that concentration buys capability: a whole share carries
    a limit order, extended-hours reach and a resting stop, and a fractional one
    carries none of the three. Under `MAX_POSITIONS = 12` a $10,000 account
    sized every one of these names whole. At 25 slots the rung is $392 and the
    two dearest no longer fit.

    It binds only above $2,000; nothing at today's equity moves.
    """
    result = sizing.size(CANDIDATES, rung(10_000.00))
    fractional = {s for s, p in paths(result).items() if p == "FRACTIONAL"}

    assert result.ladder.n_target == 25
    assert fractional == set()
    assert all(e.resting_stop_eligible for e in result.entries
               if e.symbol not in fractional)


def test_the_whole_share_frontier_only_ever_widens():
    # Monotonicity is the property that makes the ladder a ladder: funding the
    # account can never take a name back off the whole-share path.
    frontiers = [
        {s for s, p in paths(sizing.size(CANDIDATES, rung(e))).items() if p == "WHOLE"}
        for e in (1_000.00, 5_000.00, 10_000.00)
    ]

    assert frontiers[0] < frontiers[1] < frontiers[2]


# --- the golden files -----------------------------------------------------


def rounded(value):
    """Six decimals: past Robinhood's share precision, short of float noise."""
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {k: rounded(v) for k, v in value.items()}
    if isinstance(value, list):
        return [rounded(v) for v in value]
    return value


@pytest.mark.parametrize("equity", [236, 500, 1_000, 5_000, 10_000])
def test_the_rung_matches_its_golden_file(equity):
    result = sizing.size(CANDIDATES, rung(float(equity)))

    expected = json.loads((GOLDEN / f"equity_{equity}.json").read_text(encoding="utf-8"))
    assert rounded(result.as_dict()) == rounded(expected)
