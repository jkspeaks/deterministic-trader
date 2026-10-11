"""The JSON boundary and the remaining fail-closed edges.

Invariant 2 makes every module a pure function from JSON to JSON, so the
serialised shape is part of guard's contract with `cli.py` and with §15's
golden files — not an implementation detail.
"""

import json

from src import guard
from tests.factories import codes, context, order


# --- serialisation --------------------------------------------------------


def test_a_halted_result_serialises_to_json():
    result = guard.evaluate([order()], context(control={"readable": False}))

    payload = result.as_dict()

    assert payload["halted"] is True
    assert payload["approved"] == []
    assert payload["denied"] == []
    assert [h["code"] for h in payload["halts"]] == [guard.CONTROL_UNREADABLE]
    assert payload["halts"][0]["message"]
    json.dumps(payload)


def test_a_denied_order_serialises_with_its_order_and_codes():
    result = guard.evaluate([order()], context(universe={"candidates": []}))

    payload = result.as_dict()

    assert payload["halted"] is False
    assert payload["denied"][0]["order"]["symbol"] == "F"
    assert [d["code"] for d in payload["denied"][0]["denials"]] == [
        guard.NOT_IN_CANDIDATE_SET
    ]
    json.dumps(payload)


def test_an_approved_order_survives_serialisation_unchanged():
    proposed = order()

    payload = guard.evaluate([proposed], context()).as_dict()

    assert json.loads(json.dumps(payload))["approved"] == [proposed]


# --- orders with no quantity ----------------------------------------------


def test_a_dollar_denominated_order_is_denied():
    """`dollar_amount` is a real tool shape and the design never emits it.

    Runbook §6 row 1 shows a `dollar_amount` sell passing review, and runbook
    T1 chooses `quantity` instead so a flatten leaves no fractional remainder.
    Guard cannot check a dollar-based order against the concentration caps
    without first pricing it, so it is denied rather than waved through.
    """
    proposed = order(type="market", limit_price=None)
    del proposed["quantity"]
    proposed["dollar_amount"] = 5.00

    result = guard.evaluate([proposed], context())

    assert codes(result, "F") == [guard.ORDER_NOT_PRICEABLE]


def test_a_limit_order_with_no_quantity_is_denied():
    proposed = order()
    del proposed["quantity"]

    result = guard.evaluate([proposed], context())

    assert codes(result, "F") == [guard.ORDER_NOT_PRICEABLE]


# --- overnight edges ------------------------------------------------------


def test_an_overnight_buy_is_stopped_by_the_cycle_not_by_the_spread_gate():
    # The spread gate is a one-way test: overnight cycles only sell, so a buy
    # must be refused for being an entry rather than for its spread.
    ctx = context(
        cycle={
            "cycle_type": "overnight",
            "session": "all_day_hours",
            "entries_allowed": False,
        }
    )

    result = guard.evaluate([order(session="all_day_hours")], ctx)

    assert guard.ENTRIES_NOT_ALLOWED_THIS_CYCLE in codes(result, "F")
    assert guard.OVERNIGHT_SPREAD_GATE not in codes(result, "F")


def test_an_overnight_exit_of_an_unheld_name_gets_no_catastrophe_override():
    # No position means no recorded loss below stop, so a wide spread has
    # nothing to justify crossing it.
    ctx = context(
        cycle={
            "cycle_type": "overnight",
            "session": "all_day_hours",
            "entries_allowed": False,
        }
    )
    ctx["universe"]["candidates"].append("MRK")
    ctx["quotes"]["MRK"] = {"bid": 125.00, "ask": 145.00, "last": 130.22}
    ctx["sectors"]["MRK"] = "Health Care"
    ctx["earnings"]["MRK"] = {
        "status": "known",
        "next_report_date": "2026-12-01",
        "sessions_until_report": 80,
    }
    ctx["tradability"]["MRK"] = {
        "tradeable": True,
        "fractional_tradability": "tradable",
        "all_day_tradability": "tradable",
    }

    result = guard.evaluate(
        [
            order(
                symbol="MRK",
                side="sell",
                type="limit",
                limit_price=125.00,
                quantity=1.0,
                session="all_day_hours",
            )
        ],
        ctx,
    )

    assert guard.OVERNIGHT_SPREAD_GATE in codes(result, "MRK")
