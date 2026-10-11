"""`reconcile.fills_quality` — §1.3/§4.2 execution telemetry.

One row per terminal order the core placed: intended vs filled quantity,
limit vs average fill price, minutes to fill. Purely observational — it must
add no finding, no cancel and no control update, and an order that never
filled has to be visible.
"""

from src import reconcile
from tests.factories import reconcile_context


def quality(**overrides):
    return reconcile.run(reconcile_context(**overrides)).fills_quality


def one(symbol, **overrides):
    rows = quality(**overrides)
    return next(r for r in rows if r["symbol"] == symbol)


def test_a_filled_order_records_intent_fill_and_price():
    rows = quality(
        intent={"placements": [
            {"order_id": "a1b2", "symbol": "F", "side": "buy", "quantity": 2.0,
             "play_id": "DIP", "limit_price": 15.00}
        ]},
        orders=[{"order_id": "a1b2", "symbol": "F", "side": "buy",
                 "quantity": 2.0, "state": "filled"}],
        fills=[{"order_id": "a1b2", "symbol": "F", "side": "buy", "quantity": 2.0,
                "price": 14.90, "cost_basis": None}],
    )

    assert rows == [{
        "symbol": "F", "side": "buy",
        "intended_qty": 2.0, "filled_qty": 2.0,
        "limit_price": 15.00, "avg_fill_price": 14.90,
        "minutes_to_fill": None,
    }]


def test_an_unfilled_terminal_order_is_recorded_with_a_zero_fill():
    """The case that has no other footprint anywhere in the system."""
    row = one(
        "F",
        intent={"placements": [
            {"order_id": "a1b2", "symbol": "F", "side": "buy", "quantity": 3.0,
             "play_id": "DIP", "limit_price": 15.00}
        ]},
        orders=[{"order_id": "a1b2", "symbol": "F", "side": "buy",
                 "quantity": 3.0, "state": "cancelled"}],
        fills=[],
    )

    assert row["filled_qty"] == 0.0
    assert row["avg_fill_price"] is None
    assert row["limit_price"] == 15.00


def test_a_working_order_is_not_recorded_yet():
    """A partial fill on an order still working is an order still working —
    the same reading `_order_findings` takes for QUANTITY_DRIFT."""
    assert quality(
        intent={"placements": [
            {"order_id": "a1b2", "symbol": "F", "side": "buy", "quantity": 3.0,
             "play_id": "DIP"}
        ]},
        orders=[{"order_id": "a1b2", "symbol": "F", "side": "buy",
                 "quantity": 3.0, "state": "queued"}],
        fills=[{"order_id": "a1b2", "symbol": "F", "side": "buy", "quantity": 1.0,
                "price": 14.90, "cost_basis": None}],
    ) == []


def test_an_order_that_is_not_ours_is_not_recorded():
    """A phantom / other-strategy order the broker shows but no plan placed."""
    assert quality(
        intent={"placements": []},
        orders=[{"order_id": "zz99", "symbol": "MRK", "side": "buy",
                 "quantity": 1.0, "state": "filled"}],
        fills=[{"order_id": "zz99", "symbol": "MRK", "side": "buy", "quantity": 1.0,
                "price": 128.0, "cost_basis": None}],
    ) == []


def test_a_partial_fill_shows_the_shortfall_and_a_weighted_average():
    row = one(
        "PFE",
        intent={"placements": [
            {"order_id": "c3d4", "symbol": "PFE", "side": "buy", "quantity": 4.0,
             "play_id": "PEAD", "limit_price": 25.00}
        ]},
        orders=[{"order_id": "c3d4", "symbol": "PFE", "side": "buy",
                 "quantity": 4.0, "state": "filled"}],
        fills=[
            {"order_id": "c3d4", "symbol": "PFE", "side": "buy", "quantity": 1.0,
             "price": 25.00, "cost_basis": None},
            {"order_id": "c3d4", "symbol": "PFE", "side": "buy", "quantity": 2.0,
             "price": 25.15, "cost_basis": None},
        ],
    )

    assert row["intended_qty"] == 4.0
    assert row["filled_qty"] == 3.0
    assert row["avg_fill_price"] == round((25.00 + 2 * 25.15) / 3, 6)


def test_the_limit_price_falls_back_to_the_broker_order_row():
    """The placement record carries no limit price; `get_equity_orders` does."""
    row = one(
        "F",
        intent={"placements": [
            {"order_id": "a1b2", "symbol": "F", "side": "buy", "quantity": 1.0,
             "play_id": "DIP"}
        ]},
        orders=[{"order_id": "a1b2", "symbol": "F", "side": "buy", "quantity": 1.0,
                 "state": "filled", "limit_price": 14.87}],
        fills=[{"order_id": "a1b2", "symbol": "F", "side": "buy", "quantity": 1.0,
                "price": 14.80, "cost_basis": None}],
    )

    assert row["limit_price"] == 14.87


def test_minutes_to_fill_comes_from_the_order_timestamps_when_present():
    row = one(
        "F",
        intent={"placements": [
            {"order_id": "a1b2", "symbol": "F", "side": "buy", "quantity": 1.0,
             "play_id": "DIP"}
        ]},
        orders=[{
            "order_id": "a1b2", "symbol": "F", "side": "buy", "quantity": 1.0,
            "state": "filled",
            "created_at": "2026-08-03T13:40:00Z",
            "last_transaction_at": "2026-08-03T14:07:30Z",
        }],
        fills=[{"order_id": "a1b2", "symbol": "F", "side": "buy", "quantity": 1.0,
                "price": 14.80, "cost_basis": None}],
    )

    assert row["minutes_to_fill"] == 27.5


def test_rows_are_sorted_by_symbol_then_side():
    rows = quality(
        intent={"placements": [
            {"order_id": "o1", "symbol": "PFE", "side": "buy", "quantity": 1.0,
             "play_id": "PEAD"},
            {"order_id": "o2", "symbol": "F", "side": "sell", "quantity": 1.0,
             "play_id": "DIP"},
            {"order_id": "o3", "symbol": "F", "side": "buy", "quantity": 1.0,
             "play_id": "DIP"},
        ]},
        orders=[
            {"order_id": "o1", "symbol": "PFE", "side": "buy", "quantity": 1.0,
             "state": "filled"},
            {"order_id": "o2", "symbol": "F", "side": "sell", "quantity": 1.0,
             "state": "filled"},
            {"order_id": "o3", "symbol": "F", "side": "buy", "quantity": 1.0,
             "state": "filled"},
        ],
        fills=[
            {"order_id": "o1", "symbol": "PFE", "side": "buy", "quantity": 1.0,
             "price": 25.0, "cost_basis": None},
            {"order_id": "o2", "symbol": "F", "side": "sell", "quantity": 1.0,
             "price": 15.0, "cost_basis": 14.0},
            {"order_id": "o3", "symbol": "F", "side": "buy", "quantity": 1.0,
             "price": 14.0, "cost_basis": None},
        ],
    )

    assert [(r["symbol"], r["side"]) for r in rows] == [
        ("F", "buy"), ("F", "sell"), ("PFE", "buy")
    ]


def test_it_is_observational_only():
    """A fill-quality row must never become a divergence, a cancel or a flag.
    The `agent_round_trip` scenario reconciles cleanly (0 findings in its
    golden); adding the fills_quality read to it must not change that."""
    result = reconcile.run(reconcile_context(
        intent={"placements": [
            {"order_id": "a1b2", "symbol": "F", "side": "sell",
             "quantity": 1.0, "play_id": "DIP"},
        ]},
        orders=[{"order_id": "a1b2", "symbol": "F", "side": "sell",
                 "quantity": 1.0, "state": "filled"}],
        fills=[{"order_id": "a1b2", "symbol": "F", "side": "sell", "quantity": 1.0,
                "price": 13.75, "cost_basis": 15.00}],
        positions=[],
        prior_positions=[{"symbol": "F", "play_id": "DIP", "quantity": 1.0}],
        tax_lots={},
    ))

    assert result.fills_quality and result.fills_quality[0]["symbol"] == "F"
    assert result.cancels == []
    assert "fills_quality" not in result.control_updates
