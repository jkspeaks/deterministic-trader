"""The realized-P&L join — `get_pnl_trade_history`'s names becoming reconcile's.

A live read that day settled it, and two of the guesses were wrong; the confirmed
shape and what it cost is at the bottom of this file.

The tests in this half were written to survive that edit and did. They assert
the two properties that hold whatever the names turn out to be:

* an unknown extra field is ignored, so a response that grows a column needs
  no code change;
* a missing, null or unparseable numeric field becomes `None`, **never 0.0** —
  a zero realized gain is a real break-even and an absent one is a failed read,
  and `float(x or 0.0)` collapsing those two is precisely what turned a missing
  `equity_total` into a proposed whole-book flatten.
"""

import pytest

from src import broker_shapes, cli, reconcile


def _realized(payload):
    """`broker_shapes.realized`, called the way `cli.reconcile` calls it.

    The wrapper keeps these tests written against the payload key rather
    than the raw value, which is what they are about: an absent `realized`
    is an empty list, not a crash.
    """
    return broker_shapes.realized(payload.get("realized"), reconcile.calendar_day)


def test_broker_field_names_become_the_shape_reconcile_reads():
    rows = _realized(
        {
            "realized": [
                {
                    "order_id": "a1b2",
                    "symbol": "F",
                    "quantity": 1.0,
                    "price": 14.00,
                    "proceeds": 14.00,
                    "cost_basis": 15.00,
                    "realized_gain": -1.00,
                    "date": "2026-08-03",
                }
            ]
        }
    )

    assert rows == [
        {
            "order_id": "a1b2",
            "symbol": "F",
            "quantity": 1.0,
            "price": 14.00,
            "proceeds": 14.00,
            "cost_basis": 15.00,
            "realized_gain": -1.00,
            "date": "2026-08-03",
            # Kept whole for `edge_realized_trades`' key. A row that only
            # carries a day has only a day to give.
            "traded_at": "2026-08-03",
            "side": None,
        }
    ]


@pytest.mark.parametrize(
    "broker_key,internal,value",
    [
        ("execution_id", "order_id", "a1b2"),
        ("id", "order_id", "a1b2"),
        ("basis", "cost_basis", 15.00),
        ("realized_pnl", "realized_gain", -1.25),
        ("realized_profit_loss", "realized_gain", -1.25),
        ("trade_date", "date", "2026-08-03"),
        ("executed_at", "date", "2026-08-03"),
        ("settlement_date", "date", "2026-08-03"),
    ],
)
def test_each_alternative_broker_name_is_accepted(broker_key, internal, value):
    """`REALIZED_FIELDS` lists several candidates per field because the live
    name is a guess. Confirming one is deleting the others, not rewriting the
    parser."""
    rows = _realized({"realized": [{broker_key: value}]})

    assert rows[0][internal] == value


def test_an_unknown_extra_field_is_ignored():
    """A broker response that grows a column must not need a code change, and
    must not leak an unread key into `reconcile`'s matching."""
    rows = _realized(
        {"realized": [{"symbol": "F", "asset_class": "equity", "currency": "USD"}]}
    )

    assert set(rows[0]) == set(broker_shapes.REALIZED_FIELDS)


def test_a_missing_numeric_field_is_none_and_never_zero():
    rows = _realized({"realized": [{"symbol": "F"}]})

    assert rows[0]["realized_gain"] is None
    assert rows[0]["proceeds"] is None
    assert rows[0]["cost_basis"] is None
    assert rows[0]["quantity"] is None


def test_an_unparseable_number_is_none_rather_than_a_crash():
    rows = _realized({"realized": [{"symbol": "F", "realized_gain": "n/a"}]})

    assert rows[0]["realized_gain"] is None


def test_a_numeric_string_is_read_as_the_number():
    """Every other Robinhood tool in this system sends money as a string."""
    rows = _realized({"realized": [{"symbol": "F", "realized_gain": "-1.25"}]})

    assert rows[0]["realized_gain"] == -1.25


def test_a_real_zero_survives_as_a_zero():
    """The distinction the whole helper exists for: the broker said
    break-even, and that is not the same statement as saying nothing."""
    rows = _realized({"realized": [{"symbol": "F", "realized_gain": 0}]})

    assert rows[0]["realized_gain"] == 0.0


def test_a_wrapped_response_is_unwrapped_here_not_by_hand():
    """The tool pages (`next_cursor`), so its rows may well arrive inside an
    envelope. Unwrapping is `cli.py`'s job for the same reason renaming is."""
    for envelope in broker_shapes._LIST_ENVELOPES:
        rows = _realized({"realized": {envelope: [{"symbol": "F"}], "next_cursor": "z"}})

        assert [row["symbol"] for row in rows] == ["F"], envelope


def test_an_object_with_no_recognizable_rows_is_empty_not_a_crash():
    assert _realized({"realized": {"next_cursor": "z"}}) == []


def test_no_realized_key_is_an_empty_list():
    """Each fill then falls back to the tax lot, which is the prior behaviour
    exactly."""
    assert _realized({}) == []
    assert _realized({"realized": None}) == []


def test_a_non_row_entry_is_skipped():
    assert _realized({"realized": ["F", None, {"symbol": "F"}]}) == [
        {**{key: None for key in broker_shapes.REALIZED_FIELDS}, "symbol": "F"}
    ]


# --- the live response, as the broker actually answers it -------------------
#
# Confirmed against account ••0000. Everything above this line was written
# against a *guessed* schema — `get_pnl_trade_history`'s arguments are
# documented and its response was not — and two of those guesses were wrong in
# a way that made the whole path inert rather than noisy: the rows are nested a
# level deeper than the unwrapper looked, and the field that dates a trade is
# `timestamp`, which was not among the four names the renamer tried.
#
# Verbatim, trimmed to two trades and with the `guide` prose shortened. Not
# composed: this is the fixture-contract rule this repo keeps relearning — a
# fixture that describes a world production does not produce makes a green
# suite prove nothing.

LIVE_RESPONSE = {
    "data": {
        "account_number": "<your-cash-account-number>",
        "span": "all",
        "trades": [
            {"timestamp": "2026-09-04T16:33:05Z", "symbol": "MS", "side": "sell",
             "quantity": "0.716109", "price": "216.726783213170062099484855",
             "realized_gain": "-0.93"},
            {"timestamp": "2026-08-19T14:45:39Z", "symbol": "MRK", "side": "sell",
             "quantity": "1.162979", "price": "149.5383837541348554015162785",
             "realized_gain": "20.11"},
        ],
        "next_cursor": "",
    },
    "guide": "trades are returned most-recent-first. ...",
}


def test_the_live_response_shape_yields_rows_at_all():
    """The regression. A single-level unwrap found `data`, saw a Mapping
    rather than a list, and returned nothing — so `realized` shipped inert and
    every fill fell back to the tax-lot arithmetic it exists to replace."""
    assert len(_realized({"realized": LIVE_RESPONSE})) == 2


def test_the_live_timestamp_becomes_the_calendar_day_the_fill_is_matched_on():
    rows = _realized({"realized": LIVE_RESPONSE})

    assert [r["date"] for r in rows] == ["2026-09-04", "2026-08-19"]


def test_the_live_response_carries_no_order_id_and_that_is_expected():
    """`_Realized`'s preferred match is permanently unavailable: the tool
    returns no identifier of any kind. The `(symbol, date)` fallback was
    written for exactly this, and this test is what stops someone "fixing"
    the missing key by inventing one."""
    assert all(r["order_id"] is None for r in _realized({"realized": LIVE_RESPONSE}))


def test_losses_come_back_signed_not_as_magnitudes():
    """The assumption the entire ledger inverts on if it is wrong. Confirmed
    live: MS lost 0.93 and reports -0.93, MRK made 20.11 and reports 20.11."""
    rows = _realized({"realized": LIVE_RESPONSE})

    assert [r["realized_gain"] for r in rows] == [-0.93, 20.11]


def test_a_fill_is_matched_to_its_live_realized_row_end_to_end():
    """The join the two fixes exist to make work: broker row dated by an
    instant, fill dated by a day, matched on symbol and calendar day."""
    realized = reconcile._Realized(
        {
            "cycle": {"trading_day": "2026-09-04"},
            "realized": _realized({"realized": LIVE_RESPONSE}),
        }
    )

    assert realized.gain({"symbol": "MS"}) == -0.93
    assert realized.gain({"symbol": "MRK", "trade_date": "2026-08-19"}) == 20.11
    assert realized.gain({"symbol": "AMD"}) is None
