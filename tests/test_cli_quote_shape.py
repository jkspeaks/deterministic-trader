"""`quotes` arrives in two shapes and only one of them was ever handled.

A live session read `get_equity_quotes` and hand-built Appendix D's ``{symbol:
{bid, ask, last, adjusted_previous_close}}``; the runner pastes the response as
it came, ``{"results": [{"close": {...}, "quote": {...}}, ...]}`` under the
broker's own names. Third instance of the same boundary defect, after
`balances` and `intent`.

See `tests/test_broker_shapes.py` for the rest of the keys.

Both shapes are pinned here, and so is the omission rule: a field the broker
did not send does not become a `None` that something downstream reads as a
real number.
"""

from __future__ import annotations

from src.broker_shapes import quotes as _quote_map
from src.cli import _quotes

# One `results` row, exactly as an earlier rows stored it.
RAW = {
    "results": [
        {
            "close": {"date": "2026-09-09", "price": "365.07", "symbol": "ADI"},
            "quote": {
                "state": "active",
                "symbol": "ADI",
                "ask_price": "362.450000",
                "bid_price": "361.010000",
                "previous_close": "365.070000",
                "last_trade_price": "361.240000",
                "adjusted_previous_close": "365.070000",
                "last_non_reg_trade_price": "361.250000",
            },
        }
    ]
}


def test_the_brokers_envelope_is_renamed_to_appendix_d():
    assert _quote_map(RAW) == {
        "ADI": {
            "last": "361.240000",
            "bid": "361.010000",
            "ask": "362.450000",
            "adjusted_previous_close": "365.070000",
        }
    }


def test_the_live_tools_data_envelope_is_unwrapped_too():
    """What `get_equity_quotes` actually returns today: the `results` list sits
    one level down, under `data`."""
    assert _quote_map({"data": RAW}) == _quote_map(RAW)
    assert "data" not in _quote_map({"data": RAW})


def test_last_is_the_regular_session_print():
    # The response carries both. This system places `regular_hours` only, so
    # `last_trade_price` is the print from the sessions it can trade in --
    # `last_non_reg_trade_price` is extended hours and is deliberately dropped.
    assert _quote_map(RAW)["ADI"]["last"] == "361.240000"


def test_a_live_sessions_own_shape_still_passes_through():
    shaped = {"ADI": {"last": "361.24", "bid": "361.01", "ask": "362.45"}}

    assert _quote_map(shaped) == shaped


def test_a_field_the_broker_omitted_is_omitted_not_nulled():
    thin = {"results": [{"quote": {"symbol": "ADI", "ask_price": "362.45"}}]}

    assert _quote_map(thin) == {"ADI": {"ask": "362.45"}}


def test_a_row_with_no_symbol_is_dropped_rather_than_keyed_by_nothing():
    junk = {"results": [{"quote": {"ask_price": "1.00"}}, "not a row", {}]}

    assert _quote_map(junk) == {}


def test_absent_and_unreadable_are_empty_not_a_crash():
    assert _quote_map(None) == {}
    assert _quote_map([]) == {}
    assert _quote_map({"results": "nope"}) == {}


def test_change_pct_is_computed_off_the_renamed_fields():
    # The whole point of the rename: `_change_pct` reads `last` and
    # `adjusted_previous_close`, which the broker calls something else. Before
    # this, a runner-posted cycle reached `_catalyst` with neither.
    quotes = _quotes({"quotes": RAW})
    expected = (361.24 - 365.07) / 365.07

    assert quotes["ADI"]["change_pct"] == expected
