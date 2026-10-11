"""`src.cli shape-placements` — Step 8's placement/rejection/alert bundle, shaped
by the core so the session never hand-assembles the `edge_runlog_write`
arguments.

The command validates and normalizes each `placements` row to
`{order_id, symbol, side, quantity, play_id, protective_stop,
protective_target, limit_price, session}`, nests each `rejections` row as
`{order: {...}, detail}` with the broker's sentence untouched, and passes
`alerts` straight through. Any structurally invalid row fails the whole call.
"""

import pytest

from src import cli
from tests.factories import cli_payload

CONFIG = cli.load_config()


def shape(**payload) -> dict:
    return cli.shape_placements(cli_payload(**payload), CONFIG)


def _placement(**overrides) -> dict:
    row = {
        "order_id": "abc-123",
        "symbol": "f",
        "side": "Buy",
        "quantity": 2,
    }
    row.update(overrides)
    return row


# --- placements: the happy path ----------------------------------------------


def test_a_valid_row_is_normalized():
    result = shape(placements=[_placement()])

    assert result["placements"] == [
        {
            "order_id": "abc-123",
            "symbol": "F",
            "side": "buy",
            "quantity": 2.0,
            "play_id": None,
            "protective_stop": False,
            "protective_target": False,
            "limit_price": None,
            "session": None,
        }
    ]


def test_the_flags_default_to_false_and_the_optionals_to_null():
    row = shape(placements=[_placement()])["placements"][0]

    assert row["protective_stop"] is False
    assert row["protective_target"] is False
    assert row["play_id"] is None
    assert row["session"] is None


def test_a_protective_stop_row_keeps_its_flag():
    result = shape(
        placements=[
            _placement(
                side="sell",
                protective_stop=True,
                limit_price=None,
                session="regular_hours",
                play_id="DIP",
            )
        ]
    )

    assert result["placements"][0]["protective_stop"] is True
    assert result["placements"][0]["session"] == "regular_hours"
    assert result["placements"][0]["play_id"] == "DIP"


def test_the_limit_price_is_coerced_to_a_float():
    result = shape(placements=[_placement(limit_price="14.92")])

    assert result["placements"][0]["limit_price"] == pytest.approx(14.92)


def test_an_empty_bundle_shapes_to_empty_lists():
    result = shape()

    assert result["placements"] == []
    assert result["rejections"] == []
    assert result["alerts"] == []
    assert "error" not in result


# --- placements: validation fails the whole call ---------------------------


def test_a_missing_order_id_fails_the_call():
    result = shape(placements=[_placement(order_id="")])

    assert result["error"]["code"] == cli.PLACEMENTS_INVALID
    assert "order_id" in result["error"]["detail"]
    assert "placements" not in result


def test_a_non_positive_quantity_fails():
    result = shape(placements=[_placement(quantity=0)])

    assert result["error"]["code"] == cli.PLACEMENTS_INVALID
    assert "quantity" in result["error"]["detail"]


def test_a_quantity_that_is_not_a_number_fails():
    result = shape(placements=[_placement(quantity="two")])

    assert result["error"]["code"] == cli.PLACEMENTS_INVALID


def test_a_side_outside_buy_sell_fails():
    result = shape(placements=[_placement(side="hold")])

    assert result["error"]["code"] == cli.PLACEMENTS_INVALID
    assert "side" in result["error"]["detail"]


def test_a_boolean_quantity_is_not_a_number():
    # bool is a subclass of int; `_is_number` rejects it so `quantity: true`
    # cannot slip through as 1.0.
    result = shape(placements=[_placement(quantity=True)])

    assert result["error"]["code"] == cli.PLACEMENTS_INVALID


def test_a_non_boolean_flag_fails():
    result = shape(placements=[_placement(protective_target="yes")])

    assert result["error"]["code"] == cli.PLACEMENTS_INVALID
    assert "protective_target" in result["error"]["detail"]


def test_every_bad_row_is_named_not_just_the_first():
    result = shape(
        placements=[
            _placement(),
            _placement(order_id=""),
            _placement(side="hold"),
        ]
    )

    detail = result["error"]["detail"]
    assert "2 row(s) failed" in detail
    assert "'index': 1" in detail
    assert "'index': 2" in detail


# --- rejections: nested, verbatim ----------------------------------------------


def test_a_rejection_is_nested_as_order_and_detail():
    result = shape(
        rejections=[
            {
                "symbol": "pfe",
                "side": "buy",
                "type": "Limit",
                "quantity": 3,
                "limit_price": 25.18,
                "session": "regular_hours",
                "detail": "Limit order quantity cannot include fractional shares",
            }
        ]
    )

    assert result["rejections"] == [
        {
            "order": {
                "symbol": "PFE",
                "side": "buy",
                "type": "limit",
                "quantity": 3.0,
                "limit_price": 25.18,
                "session": "regular_hours",
            },
            "detail": "Limit order quantity cannot include fractional shares",
        }
    ]


def test_the_broker_sentence_is_carried_through_untouched():
    weird = "  Order REJECTED: 'buying power' $0.48 short — do NOT retry.  "
    result = shape(
        rejections=[
            {"symbol": "F", "side": "buy", "type": "limit", "quantity": 2, "detail": weird}
        ]
    )

    assert result["rejections"][0]["detail"] == weird


def test_a_rejection_with_no_detail_fails():
    result = shape(
        rejections=[{"symbol": "F", "side": "buy", "type": "limit", "quantity": 2}]
    )

    assert result["error"]["code"] == cli.PLACEMENTS_INVALID
    assert "detail" in result["error"]["detail"]


def test_a_bad_placement_and_a_bad_rejection_are_both_reported():
    result = shape(
        placements=[_placement(quantity=-1)],
        rejections=[{"symbol": "F", "side": "buy", "type": "limit", "quantity": 2}],
    )

    detail = result["error"]["detail"]
    assert "'list': 'placements'" in detail
    assert "'list': 'rejections'" in detail


# --- alerts: canonical pass through, everything else is a runner note ---------


def test_canonical_alerts_pass_through_verbatim():
    alerts = [
        {"title": "Manual action required", "body": "AMD: no bid", "priority": "high"},
        {"title": "Order refused", "body": "MGM: not found", "priority": "default"},
    ]
    result = shape(alerts=alerts)

    assert result["alerts"] == alerts
    assert result["runner_notes"] == []


def test_a_code_detail_row_is_a_note_not_an_alert():
    row = {"code": "fetch_row_skipped", "detail": "step=regime skipped"}
    result = shape(alerts=[row])

    assert result["alerts"] == []
    assert result["runner_notes"] == [row]


def test_a_non_canonical_title_is_a_note_not_an_alert():
    row = {"title": "09:40 summary — no buy, no sell", "body": "...", "priority": "default"}
    result = shape(alerts=[row])

    assert result["alerts"] == []
    assert result["runner_notes"] == [row]


def test_a_canonical_title_with_no_body_is_a_note_not_an_alert():
    row = {"title": "Order refused", "body": "", "priority": "high"}
    result = shape(alerts=[row])

    assert result["alerts"] == []
    assert result["runner_notes"] == [row]
