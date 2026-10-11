"""`services/core/core/twelvedata.py` — the batching, the pacing, the budget.

The one hard rule this file pins is the credit cap: a run that would spend past
`credit_cap` raises rather than truncating, because the 800/day budget is
shared with the next run. Everything else — batch size, the 8-second spacing,
a vendor error becoming an `.errors` row instead of an exception — is checked
against a fake fetcher so no test touches `api.twelvedata.com`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.core.core.twelvedata import (
    CreditCapExceeded,
    TwelveData,
    TwelveDataError,
)

FIXTURES = Path(__file__).parent / "fixtures"
TS_FIXTURE = json.loads((FIXTURES / "td_time_series.json").read_text())
SMA_FIXTURE = json.loads((FIXTURES / "td_sma.json").read_text())


class FakeFetcher:
    """Maps a URL to a canned `(status, body)` by which endpoint it names.
    Records every URL it was asked for so a test can assert on pacing/batching.
    """

    def __init__(self, responses: dict[str, tuple[int, bytes]] | None = None):
        self.responses = responses or {}
        self.urls: list[str] = []

    def get(self, url: str) -> tuple[int, bytes]:
        self.urls.append(url)
        for needle, response in self.responses.items():
            if needle in url:
                return response
        return 200, b"{}"


def _ok(payload) -> tuple[int, bytes]:
    return 200, json.dumps(payload).encode()


def _td(fetcher, **kw):
    return TwelveData("KEY", fetcher, sleep=lambda _s: None, **kw)


# ---------------------------------------------------------------------------
# Parsing the two response shapes
# ---------------------------------------------------------------------------


def test_time_series_reads_a_batch_map_from_the_hand_written_fixture():
    td = _td(FakeFetcher({"time_series": _ok(TS_FIXTURE)}))

    bars = td.time_series(["SPY", "XLK"])

    assert set(bars) == {"SPY", "XLK"}
    assert [b["datetime"] for b in bars["SPY"]][-1] == "2026-09-10"
    assert bars["SPY"][-1]["close"] == "605.10"
    assert td.errors == []


def test_sma_reads_a_flat_single_symbol_block_and_parses_the_float():
    td = _td(FakeFetcher({"sma": _ok(SMA_FIXTURE)}))

    values = td.sma(["SPY"], 200)

    assert values == {"SPY": pytest.approx(571.284350)}


def test_a_multi_symbol_sma_batch_is_a_symbol_keyed_map():
    payload = {
        "SPY": {"values": [{"datetime": "2026-09-10", "sma": "570.0"}], "status": "ok"},
        "XLK": {"values": [{"datetime": "2026-09-10", "sma": "250.5"}], "status": "ok"},
    }
    td = _td(FakeFetcher({"sma": _ok(payload)}))

    assert td.sma(["SPY", "XLK"], 50) == {"SPY": 570.0, "XLK": 250.5}


def test_price_reads_a_flat_single_symbol_block():
    td = _td(FakeFetcher({"price": _ok({"price": "605.10"})}))

    assert td.price(["SPY"]) == {"SPY": pytest.approx(605.10)}


def test_price_reads_a_multi_symbol_batch_and_costs_one_credit_each():
    payload = {"SPY": {"price": "605.10"}, "XLK": {"price": "250.55"}}
    td = _td(FakeFetcher({"price": _ok(payload)}))

    values = td.price(["SPY", "XLK"])

    assert values == {"SPY": pytest.approx(605.10), "XLK": pytest.approx(250.55)}
    assert td.budget.used == 2


def test_price_records_an_unparseable_value_as_an_error_row():
    td = _td(FakeFetcher({"price": _ok({"price": "not-a-number"})}))

    assert td.price(["SPY"]) == {}
    assert any("SPY" in e for e in td.errors)


# ---------------------------------------------------------------------------
# Batching and pacing
# ---------------------------------------------------------------------------


def test_more_than_eight_symbols_is_split_into_calls_of_at_most_eight():
    fetcher = FakeFetcher({"time_series": _ok({})})
    td = _td(fetcher)

    td.time_series([f"S{i}" for i in range(20)])

    assert len(fetcher.urls) == 3  # 8 + 8 + 4
    first_batch = fetcher.urls[0].split("symbol=")[1].split("&")[0]
    assert len(first_batch.split(",")) == 8


def test_the_first_call_does_not_sleep_and_the_rest_do():
    slept: list[float] = []
    td = TwelveData("KEY", FakeFetcher({"time_series": _ok({})}), sleep=slept.append)

    td.time_series([f"S{i}" for i in range(17)])  # 3 calls

    assert slept == [8.0, 8.0]  # between call 1-2 and 2-3, not before call 1


def test_symbols_are_de_duplicated_before_batching():
    fetcher = FakeFetcher({"time_series": _ok({})})
    td = _td(fetcher)

    td.time_series(["SPY", "SPY", "XLK", "SPY"])

    assert fetcher.urls[0].split("symbol=")[1].split("&")[0] == "SPY,XLK"


# ---------------------------------------------------------------------------
# The credit budget
# ---------------------------------------------------------------------------


def test_each_symbol_in_a_call_costs_one_credit():
    td = _td(FakeFetcher({"time_series": _ok({})}))

    td.time_series(["A", "B", "C"])

    assert td.budget.used == 3


def test_a_call_that_would_exceed_the_cap_raises_before_it_is_sent():
    fetcher = FakeFetcher({"time_series": _ok({})})
    td = _td(fetcher, credit_cap=10)

    td.time_series([f"S{i}" for i in range(8)])  # 8 <= 10, fine
    with pytest.raises(CreditCapExceeded):
        td.time_series([f"T{i}" for i in range(8)])  # would be 16

    assert td.budget.used == 8  # the refused call spent nothing
    assert len(fetcher.urls) == 1  # and was never sent


def test_the_cap_spans_time_series_and_sma_together():
    td = _td(FakeFetcher(), credit_cap=5)

    td.time_series(["A", "B", "C"])  # 3
    with pytest.raises(CreditCapExceeded):
        td.sma(["D", "E", "F"], 200)  # 3 more -> 6 > 5


# ---------------------------------------------------------------------------
# Errors are rows, not exceptions (except the cap)
# ---------------------------------------------------------------------------


def test_a_per_symbol_error_block_is_recorded_and_that_symbol_is_absent():
    payload = {
        "SPY": {"values": [{"datetime": "2026-09-10", "close": "600"}], "status": "ok"},
        "ZZZZ": {"status": "error", "code": 404, "message": "symbol not found"},
    }
    td = _td(FakeFetcher({"time_series": _ok(payload)}))

    bars = td.time_series(["SPY", "ZZZZ"])

    assert "SPY" in bars and "ZZZZ" not in bars
    assert any("ZZZZ" in e for e in td.errors)


def test_a_non_2xx_is_an_error_row_and_yields_nothing_for_the_batch():
    td = _td(FakeFetcher({"time_series": (429, b'{"message":"rate limit"}')}))

    bars = td.time_series(["SPY", "XLK"])

    assert bars == {}
    assert any("429" in e for e in td.errors)


def test_a_fetcher_that_throws_is_caught_as_an_error_row():
    class Boom:
        def get(self, url):
            raise ConnectionError("socket died")

    td = TwelveData("KEY", Boom(), sleep=lambda _s: None)
    assert td.time_series(["SPY"]) == {}
    assert any("socket died" in e for e in td.errors)


# ---------------------------------------------------------------------------
# from_env
# ---------------------------------------------------------------------------


def test_from_env_fails_closed_when_the_key_is_absent(monkeypatch):
    monkeypatch.delenv("TWELVE_DATA_API_KEY", raising=False)
    with pytest.raises(TwelveDataError):
        TwelveData.from_env(FakeFetcher())


def test_from_env_uses_the_key_as_the_apikey_query_param(monkeypatch):
    monkeypatch.setenv("TWELVE_DATA_API_KEY", "secret-123")
    fetcher = FakeFetcher({"time_series": _ok({})})
    td = TwelveData.from_env(fetcher, sleep=lambda _s: None)

    td.time_series(["SPY"])

    assert "apikey=secret-123" in fetcher.urls[0]
