"""Invariant 3 — the agent may only place what the plan emits (design v3 §3).

"The agent invented a trade" has to be a code-level impossibility rather than a
prompt instruction, and this is where that is made true.

The one asymmetry: §5.3 defines the candidate set as
``current_positions ∪ triggered names ∪ macro targets``, so a held name is a
candidate by construction. Guard therefore admits an *exit* of a held name even
if the candidate list handed to it omits the symbol — otherwise an upstream bug
in universe.py would trap a position with no way to sell it.
"""

from src import guard
from tests.factories import codes, context, order

HELD_INTC = {
    "positions": [
        {
            "symbol": "INTC",
            "quantity": 1.0,
            "market_value": 90.21,
            "play_id": "MANUAL",
        }
    ],
    "quotes": {"INTC": {"bid": 90.10, "ask": 90.30, "last": 90.21}},
    "earnings": {
        "INTC": {
            "status": "known",
            "next_report_date": "2026-10-24",
            "sessions_until_report": 55,
        }
    },
    "sectors": {"INTC": "Technology"},
    "tradability": {
        "INTC": {
            "tradeable": True,
            "fractional_tradability": "tradable",
            "all_day_tradability": "tradable",
        }
    },
}


def test_a_buy_outside_the_candidate_set_is_denied():
    result = guard.evaluate(
        [order(symbol="NVDA", limit_price=200.00)],
        context(universe={"candidates": ["F"]}),
    )

    assert guard.NOT_IN_CANDIDATE_SET in codes(result, "NVDA")


def test_a_buy_inside_the_candidate_set_is_approved():
    result = guard.evaluate([order()], context(universe={"candidates": ["F"]}))

    assert result.approved == [order()]


def test_an_exit_of_a_held_name_survives_a_candidate_list_that_omits_it():
    sell = order(symbol="INTC", side="sell", type="market", limit_price=None)
    ctx = context(universe={"candidates": ["F"]}, **HELD_INTC)

    result = guard.evaluate([sell], ctx)

    assert result.approved == [sell]


def test_a_sell_of_an_unheld_name_outside_the_candidate_set_is_denied():
    sell = order(symbol="NVDA", side="sell", type="market", limit_price=None)
    ctx = context(universe={"candidates": ["F"]})
    ctx["quotes"]["NVDA"] = {"bid": 198.90, "ask": 198.95, "last": 198.92}

    result = guard.evaluate([sell], ctx)

    assert guard.NOT_IN_CANDIDATE_SET in codes(result, "NVDA")


def test_holding_a_name_does_not_license_buying_more_of_it():
    # Invariant 3 still binds for entries: being held is not a trigger.
    buy = order(symbol="INTC", limit_price=91.00)
    ctx = context(universe={"candidates": ["F"]}, **HELD_INTC)
    ctx["account"].update({"settled_cash": 500.00, "buying_power": 500.00})

    result = guard.evaluate([buy], ctx)

    assert guard.NOT_IN_CANDIDATE_SET in codes(result, "INTC")


def test_an_empty_candidate_set_denies_every_entry():
    result = guard.evaluate([order()], context(universe={"candidates": []}))

    assert guard.NOT_IN_CANDIDATE_SET in codes(result, "F")
