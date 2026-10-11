"""`services/core/core/premarket.py` — the tick-based state machine.

Acceptance (not 8 requests): a run driven tick by tick — one Twelve Data
request per invocation, exactly as a real pg_cron-paced deployment would
call it — eventually produces the same `edge_regime` payload and per-symbol
SMA values the single-shot design did, and a plan too large for the
300-credit daily cap fails on its very first tick having sent no request at
all.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from services.core.core import premarket
from services.core.core.premarket import run_premarket
from services.core.core.twelvedata import TwelveData


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


# These tests predate an earlier retirement and shelving of the event plays
# and use them as they then ran. See `all_plays_live` in conftest.py.
pytestmark = pytest.mark.usefixtures("all_plays_live")


class RoutedSupabase:
    """Records every `rpc`; answers `select` by table + a query substring.

    `edge_premarket_state` is special-cased to actually persist: the whole
    point of these tests is that state survives between separate
    `run_premarket` calls, the way it survives between separate Vercel
    invocations in production.
    """

    def __init__(self, rows: dict[tuple[str, str], list] | None = None, returns=None, raises=()):
        self.rows = rows or {}
        self.returns = dict(returns or {})
        self.raises = set(raises)
        self.calls: list[tuple[str, dict]] = []
        self._state: dict | None = None

    def rpc(self, function: str, params=None):
        params = dict(params or {})
        self.calls.append((function, params))
        if function in self.raises:
            raise RuntimeError(f"{function} unavailable")
        if function == "edge_premarket_state_write":
            self._state = dict(params["p_state"])
        return self.returns.get(function)

    def select(self, table: str, query: str) -> list[dict]:
        if table == "edge_premarket_state":
            return [self._state] if self._state is not None else []
        for (t, needle), value in self.rows.items():
            if t == table and needle in query:
                return list(value)
        return []

    # assertions
    def args(self, function: str) -> list[dict]:
        return [p for n, p in self.calls if n == function]

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]


class FakeFetcher:
    """Synthesises Twelve Data responses. `time_series` gets a rising series
    with a little wobble (so realized vol and RSI are non-degenerate);
    `sma` gets a value a few percent under the last close. Records every URL
    it is asked for, across however many separate `TwelveData` instances a
    test constructs — the caller passes one shared instance to mimic every
    tick hitting the same vendor."""

    def __init__(self, bars: int = 250, bad=()):
        self.bars = bars
        self.bad = set(bad)
        self.urls: list[str] = []

    def _dates(self) -> list[str]:
        out: list[str] = []
        day = date(2026, 9, 10)
        while len(out) < self.bars:
            if day.weekday() < 5:
                out.append(day.isoformat())
            day -= timedelta(days=1)
        return list(reversed(out))

    def get(self, url: str) -> tuple[int, bytes]:
        self.urls.append(url)
        symbols = url.split("symbol=")[1].split("&")[0].split(",")
        is_ts = "/time_series?" in url
        dates = self._dates()
        block = {}
        for sym in symbols:
            base = 100.0 + (hash(sym) % 50)
            if is_ts:
                values = []
                for i, day in enumerate(dates):
                    px = base * (1.0 + 0.002 * i) + (0.5 if i % 3 == 0 else -0.25)
                    values.append({"datetime": day, "close": f"{px:.4f}",
                                   "open": f"{px:.4f}", "high": f"{px + 1:.4f}",
                                   "low": f"{px - 1:.4f}", "volume": "1000000"})
                block[sym] = ({"status": "error", "code": 404, "message": "symbol not found"}
                              if sym in self.bad else {"values": values, "status": "ok"})
            else:
                last = base * (1.0 + 0.002 * (self.bars - 1))
                block[sym] = {"values": [{"datetime": "2026-09-10",
                                          "sma": f"{last * 0.96:.4f}"}],
                              "status": "ok"}
        payload = block[symbols[0]] if len(symbols) == 1 else block
        return 200, json.dumps(payload).encode()


# The pre-market job reads `snapshot`/`events`/`macro` the same way the runner
# does (`run.checkout_document`, `edge_data_files` first), so a test must hand
# it a small universe or it falls through to the real bundled 503-name file.
# Eight members, weights descending by design so the screen's heaviest-first
# order is visible.
SNAPSHOT_ROWS = [
    {"symbol": s, "sector": sector, "weight": w}
    for s, sector, w in [
        ("NVDA", "Information Technology", 7.0), ("MSFT", "Information Technology", 6.5),
        ("JPM", "Financials", 2.0), ("MU", "Information Technology", 0.5),
        ("TXN", "Information Technology", 0.4), ("AMD", "Information Technology", 0.9),
        ("XOM", "Energy", 1.1), ("PLD", "Real Estate", 0.3),
    ]
]


def _data_files(events=None):
    snapshot = {"as_of": "2026-09-20", "holdings": SNAPSHOT_ROWS, "cross_check": SNAPSHOT_ROWS}
    return {
        ("edge_data_files", "name=eq.snapshot"): [{"name": "snapshot", "as_of": "2026-09-20", "payload": snapshot}],
        ("edge_data_files", "name=eq.events"): [{"name": "events", "as_of": "2026-09-26",
                                                 "payload": {"events": events or {}}}],
        ("edge_data_files", "name=eq.macro"): [{"name": "macro", "as_of": "2026-09-26",
                                                "payload": {"releases": []}}],
    }


def _supabase(held=("MU", "TXN"), survivors=("AMD",), events=None, control=None, raises=()):
    return RoutedSupabase(
        returns={"edge_control_json": control} if control is not None else None,
        raises=raises,
        rows={
            **_data_files(events),
            ("edge_runlog", "book=not.is.null"): [
                {"book": {"positions": [{"symbol": s} for s in held]}}
            ],
            ("edge_control", "dip_recovery_watchlist"): [
                {"payload": {"symbols": list(survivors)}}
            ],
            ("edge_runlog", "command=eq.decide"): [],
            ("edge_earnings_reactions", "select=symbol,report_date"): [],
        }
    )


def day_context_writes(supabase) -> dict:
    """`{symbol: symbol_data}` across every batched day-context write."""
    out: dict = {}
    for call in supabase.args("edge_day_context_write_symbols"):
        out.update(call["p_symbols"])
    return out


def run_to_completion(supabase, fetcher, *, max_ticks=30, credit_cap=300, now=None) -> list[dict]:
    """Drives `run_premarket` tick by tick, exactly one Twelve Data request
    per call (a fresh `TwelveData` each time, matching a fresh Vercel
    process), until the day's status is `ok` or `failed`. Returns every
    tick's result so a test can inspect the sequence, not just the end."""
    results = []
    for _ in range(max_ticks):
        td = TwelveData("KEY", fetcher, sleep=lambda _s: None, credit_cap=credit_cap)
        result = run_premarket(supabase, td, now=now)
        results.append(result)
        if result["status"] in ("ok", "failed"):
            return results
    raise AssertionError(f"did not finish within {max_ticks} ticks: {results[-1]}")


# ---------------------------------------------------------------------------
# One tick, one request
# ---------------------------------------------------------------------------


def test_a_single_tick_sends_at_most_one_twelve_data_request():
    supabase = _supabase()
    fetcher = FakeFetcher()
    td = TwelveData("KEY", fetcher, sleep=lambda _s: None)

    result = run_premarket(supabase, td)

    assert result["status"] == "in_progress"
    assert len(fetcher.urls) == 1


def test_the_first_tick_builds_the_plan_and_freezes_the_universe():
    supabase = _supabase(held=("MU",), survivors=("AMD",))
    td = TwelveData("KEY", FakeFetcher(), sleep=lambda _s: None)

    run_premarket(supabase, td)

    (write,) = supabase.args("edge_premarket_state_write")
    state = write["p_state"]
    assert state["next_index"] == 1
    universe = state["accum"]["universe"]
    assert {"SPY", "MU", "AMD"} <= set(universe["regime_symbols"])
    assert len(universe["regime_symbols"]) == 15  # SPY + RSP + 11 sectors + MU + AMD


# ---------------------------------------------------------------------------
# Driven to completion
# ---------------------------------------------------------------------------


def test_driven_to_completion_writes_the_edge_regime_payload():
    supabase = _supabase()
    fetcher = FakeFetcher()

    results = run_to_completion(supabase, fetcher)

    assert results[-1]["status"] == "ok"
    # every request was a single batch of at most 8 symbols
    for url in fetcher.urls:
        batch = url.split("symbol=")[1].split("&")[0].split(",")
        assert len(batch) <= 8

    (regime_write,) = supabase.args("edge_regime_write")
    inputs = regime_write["p_payload"]["regime_inputs"]
    assert set(inputs["spy"]) >= {"last", "sma50", "sma200", "closes"}
    assert len(inputs["spy"]["closes"]) >= 21
    assert len(inputs["sectors"]) == 11
    assert results[-1]["regime"] in {"risk_on", "neutral", "risk_off"}


def test_driven_to_completion_writes_per_symbol_sma_and_bars_meta():
    supabase = _supabase(held=("MU", "TXN"), survivors=("AMD",))
    fetcher = FakeFetcher()

    run_to_completion(supabase, fetcher)

    writes = day_context_writes(supabase)
    assert {"SPY", "XLK", "MU", "TXN", "AMD"} <= set(writes)
    # One batched merge per <=100 symbols, never one call per symbol.
    assert supabase.args("edge_day_context_write_symbol") == []

    mu = writes["MU"]
    # Daily RSI(14)/ATR(14) ride with the SMAs — the runner fetches no
    # indicators, so this is the hosted path's only source for either.
    assert set(mu["signals"]) == {"sma_20", "sma_50", "sma_200", "rsi", "atr"}
    assert all(isinstance(v, float) for v in mu["signals"].values())
    assert mu["bars_meta"]["source"] == "twelvedata"
    assert mu["bars_meta"]["last_bar"]
    assert mu["fetched_at"]


def test_every_sma_is_computed_locally_from_250_bars():
    """A 250-bar `time_series` costs the same one credit as a 60-bar one, so the
    200-day SMA no longer needs its own `/sma` request."""
    supabase = _supabase(held=(), survivors=())
    fetcher = FakeFetcher()

    run_to_completion(supabase, fetcher)

    assert [u for u in fetcher.urls if "/sma?" in u] == []
    assert all("outputsize=250" in u for u in fetcher.urls)

    spy = day_context_writes(supabase)["SPY"]
    assert {"sma_20", "sma_50", "sma_200", "rsi", "atr"} == set(spy["signals"])
    assert all(isinstance(v, float) for v in spy["signals"].values())


def test_a_realistic_universe_finishes_in_a_handful_of_ticks():
    supabase = _supabase(
        held=("MU", "TXN", "ADI", "MRK", "MS"),
        survivors=("AMD", "INTC", "SMH"),
    )
    fetcher = FakeFetcher()

    results = run_to_completion(supabase, fetcher, max_ticks=10)

    assert results[-1]["status"] == "ok"
    # 21 core symbols (SPY+RSP+11+5+3) in 3 steps, then the DIP screen: the five
    # snapshot members not already core (NVDA, MSFT, JPM, XOM, PLD) in 1 step.
    # No /sma steps any more.
    assert len(results) == 4
    assert results[-1]["credits_used"] == 26
    assert results[-1]["screened"] == 5


def test_after_completion_every_further_tick_is_a_free_no_op():
    supabase = _supabase(held=(), survivors=())
    fetcher = FakeFetcher()
    run_to_completion(supabase, fetcher)
    urls_before = len(fetcher.urls)
    writes_before = len(supabase.args("edge_regime_write"))

    td = TwelveData("KEY", fetcher, sleep=lambda _s: None)
    result = run_premarket(supabase, td)

    assert result["status"] == "ok"
    assert len(fetcher.urls) == urls_before  # no new Twelve Data call
    assert len(supabase.args("edge_regime_write")) == writes_before  # no rewrite


# ---------------------------------------------------------------------------
# The credit cap — checked against the whole plan, before any request
# ---------------------------------------------------------------------------


def test_a_core_set_too_large_for_the_cap_fails_on_the_first_tick():
    supabase = _supabase(held=tuple(f"SYM{i}" for i in range(600)), survivors=())
    fetcher = FakeFetcher()
    td = TwelveData("KEY", fetcher, sleep=lambda _s: None)

    result = run_premarket(supabase, td)

    assert result["status"] == "failed"
    assert "cap" in result["detail"].lower() or "credit" in result["detail"].lower()
    assert fetcher.urls == []  # not one request was sent
    assert supabase.args("edge_regime_write") == []


def test_a_failed_day_stays_failed_and_never_retries():
    supabase = _supabase(held=tuple(f"SYM{i}" for i in range(600)), survivors=())
    fetcher = FakeFetcher()

    for _ in range(3):
        td = TwelveData("KEY", fetcher, sleep=lambda _s: None)
        result = run_premarket(supabase, td)
        assert result["status"] == "failed"

    assert fetcher.urls == []


# ---------------------------------------------------------------------------
# PEAD reactions
# ---------------------------------------------------------------------------


WEDNESDAY = datetime(2026, 9, 9, 6, 0, tzinfo=ZoneInfo("America/New_York"))


def test_an_in_window_earnings_name_gets_a_reaction_row_written():
    supabase = RoutedSupabase(
        {
            **_data_files(),
            ("edge_runlog", "book=not.is.null"): [
                {"book": {"positions": [{"symbol": "MU"}]}}
            ],
            ("edge_control", "dip_recovery_watchlist"): [],
            ("edge_runlog", "command=eq.decide"): [
                {"stdout": {"earnings": {
                    "NKE": {"status": "known", "last_report_date": "2026-09-04",
                            "sessions_since_report": 2, "timing": "pm"},
                    "MU": {"status": "known", "last_report_date": "2026-01-01",
                           "sessions_since_report": 40, "timing": "am"},
                }}}
            ],
            ("edge_earnings_reactions", "select=symbol,report_date"): [],
        }
    )
    fetcher = FakeFetcher()

    result = run_to_completion(supabase, fetcher, now=WEDNESDAY)[-1]

    (write,) = supabase.args("edge_earnings_reactions_write")
    rows = write["p_rows"]
    assert [r["symbol"] for r in rows] == ["NKE"]  # MU is out of the 1-3 window
    assert rows[0]["report_date"] == "2026-09-04"
    assert "reaction_pct" in rows[0]
    assert result["reactions_written"] == 1


def test_a_name_already_in_edge_earnings_reactions_is_not_recomputed():
    supabase = RoutedSupabase(
        {
            **_data_files(),
            ("edge_runlog", "book=not.is.null"): [{"book": {"positions": []}}],
            ("edge_control", "dip_recovery_watchlist"): [],
            ("edge_runlog", "command=eq.decide"): [
                {"stdout": {"earnings": {
                    "NKE": {"status": "known", "last_report_date": "2026-09-04",
                            "sessions_since_report": 2, "timing": "pm"},
                }}}
            ],
            ("edge_earnings_reactions", "select=symbol,report_date"): [
                {"symbol": "NKE", "report_date": "2026-09-04"}
            ],
        }
    )

    result = run_to_completion(supabase, FakeFetcher(), now=WEDNESDAY)[-1]

    assert supabase.args("edge_earnings_reactions_write") == []
    assert result["reactions_written"] == 0


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_sma_is_the_mean_of_the_last_period_closes_or_none_when_too_short():
    closes = [float(i) for i in range(1, 61)]  # 1..60
    assert premarket._sma(closes, 20) == pytest.approx(sum(range(41, 61)) / 20)
    assert premarket._sma(closes, 50) == pytest.approx(sum(range(11, 61)) / 50)
    assert premarket._sma(closes, 200) is None  # only 60 bars


def test_rsi_is_none_for_a_short_series_and_bounded_otherwise():
    assert premarket._rsi([1.0, 2.0, 3.0], 14) is None
    rising = [100.0 + i for i in range(30)]
    assert premarket._rsi(rising, 14) == pytest.approx(100.0)
    falling = [100.0 - i for i in range(30)]
    assert 0.0 <= premarket._rsi(falling, 14) <= 5.0
    val = premarket._rsi([100.0, 101.0, 100.5, 101.5, 100.8, 102.0, 101.2, 103.0,
                          102.5, 104.0, 103.2, 105.0, 104.1, 106.0, 105.3], 14)
    assert 0.0 <= val <= 100.0 and not math.isnan(val)


def test_chunk_de_duplicates_and_splits_at_the_batch_size():
    assert premarket._chunk(["A", "B", "A", "C"], 2) == [["A", "B"], ["C"]]
    assert premarket._chunk([f"S{i}" for i in range(9)], 8) == (
        [[f"S{i}" for i in range(8)], ["S8"]]
    )


# ---------------------------------------------------------------------------
# Errors that used to be silent now alert
# ---------------------------------------------------------------------------
#
# Three distinct silent-failure surfaces closed together: `_finalize`'s
# accumulated per-symbol Twelve Data errors (job still finishes), the
# credit-cap refusal in `_build_plan` (job never starts), and an unhandled
# exception in `_tick` (job dies mid-way). All three used to persist their
# evidence (`accum.errors`, a `detail` string, or nothing at all) without
# ever calling `edge_alerts_enqueue`.


class _FailFirstCallFetcher(FakeFetcher):
    """Like `FakeFetcher`, but the very first request comes back HTTP 500 —
    one bad batch among otherwise-clean ones, the shape `TwelveData._request`
    turns into a `.errors` row rather than raising."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._failed_once = False

    def get(self, url: str) -> tuple[int, bytes]:
        if not self._failed_once:
            self._failed_once = True
            self.urls.append(url)
            return 500, b"internal error"
        return super().get(url)


def test_accumulated_twelve_data_errors_alert_once_the_job_finishes():
    supabase = _supabase(held=("MU", "TXN"), survivors=("AMD",))
    fetcher = _FailFirstCallFetcher()

    results = run_to_completion(supabase, fetcher)

    assert results[-1]["status"] == "ok"
    assert results[-1]["errors"], "the induced HTTP 500 should show up as an error"
    (alert_call,) = supabase.args("edge_alerts_enqueue")
    alert = alert_call["p_alerts"][0]
    assert alert["title"] == "Pre-market data job degraded"
    assert alert["priority"] == "default"


def test_a_clean_run_never_calls_edge_alerts_enqueue():
    supabase = _supabase(held=("MU", "TXN"), survivors=("AMD",))
    fetcher = FakeFetcher()

    results = run_to_completion(supabase, fetcher)

    assert results[-1]["status"] == "ok"
    assert results[-1]["errors"] == []
    assert supabase.args("edge_alerts_enqueue") == []


def test_the_credit_cap_refusal_alerts_high_priority():
    supabase = _supabase(held=tuple(f"SYM{i}" for i in range(600)), survivors=())
    fetcher = FakeFetcher()
    td = TwelveData("KEY", fetcher, sleep=lambda _s: None)

    result = run_premarket(supabase, td)

    assert result["status"] == "failed"
    (alert_call,) = supabase.args("edge_alerts_enqueue")
    alert = alert_call["p_alerts"][0]
    assert alert["title"] == "Pre-market data job degraded"
    assert alert["priority"] == "high"
    assert "credit" in alert["body"].lower()


class _ExplodingSupabase(RoutedSupabase):
    """Raises on `edge_regime_write` — deep enough into `_finalize` that the
    plan already ran, so this exercises `run_premarket`'s own top-level
    exception handler rather than `_build_plan`'s credit-cap path."""

    def rpc(self, function: str, params=None):
        if function == "edge_regime_write":
            raise RuntimeError("simulated write failure")
        return super().rpc(function, params)


def test_an_unhandled_exception_alerts_high_priority_too():
    supabase = _ExplodingSupabase(_supabase(held=(), survivors=()).rows)
    fetcher = FakeFetcher()

    results = run_to_completion(supabase, fetcher)

    assert results[-1]["status"] == "failed"
    assert "simulated write failure" in results[-1]["detail"]
    (alert_call,) = supabase.args("edge_alerts_enqueue")
    alert = alert_call["p_alerts"][0]
    assert alert["title"] == "Pre-market data job degraded"
    assert alert["priority"] == "high"


# ---------------------------------------------------------------------------
# Daily ATR(14) / RSI(14) for the hosted path
# ---------------------------------------------------------------------------


def _bar(high, low, close):
    return {"high": high, "low": low, "close": close}


def test_atr_is_wilders_true_range_average():
    # Constant 2-point range, no gaps: every true range is 2, so ATR is 2.
    flat = [_bar(101.0, 99.0, 100.0)] * 20
    assert premarket._atr(flat) == pytest.approx(2.0)

    # A gap is part of the true range: prior close 100, next bar 110-108.
    gapped = [_bar(101.0, 99.0, 100.0)] * 15 + [_bar(110.0, 108.0, 109.0)]
    first = sum([2.0] * 14) / 14
    assert premarket._atr(gapped) == pytest.approx((first * 13 + 10.0) / 14)


def test_atr_is_none_for_a_short_or_close_only_series():
    assert premarket._atr([_bar(101.0, 99.0, 100.0)] * 14) is None
    assert premarket._atr([{"close": 100.0}] * 40) is None


def test_the_step_keeps_high_and_low_for_the_atr():
    accum = {"bars": {}, "sma200": {}, "errors": []}
    td = TwelveData("KEY", FakeFetcher(), sleep=lambda _s: None, credit_cap=300)
    premarket._execute_step(td, {"endpoint": premarket.STEP_TIME_SERIES, "symbols": ["MU"]}, accum)

    bar = accum["bars"]["MU"][-1]
    assert bar["high"] == pytest.approx(bar["close"] + 1.0, abs=1e-3)
    assert bar["low"] == pytest.approx(bar["close"] - 1.0, abs=1e-3)


def test_held_names_get_a_usable_daily_atr_and_rsi():
    supabase = _supabase(held=("MU",), survivors=())
    run_to_completion(supabase, FakeFetcher())

    mu = day_context_writes(supabase)["MU"]
    assert 0.0 < mu["signals"]["atr"] < 10.0
    assert 0.0 <= mu["signals"]["rsi"] <= 100.0
    assert mu["bars_meta"]["indicators"] == "daily_close"


# ---------------------------------------------------------------------------
# Candidate prefetch
# ---------------------------------------------------------------------------


def _plan(supabase):
    run_premarket(supabase, TwelveData("KEY", FakeFetcher(), sleep=lambda _s: None))
    return supabase.args("edge_premarket_state_write")[0]["p_state"]


PEAD_8K = [{"type": "8-K", "items": ["2.02"], "sessions_ago": 1}]


def test_todays_discovery_candidates_are_core_not_screen():
    state = _plan(_supabase(held=(), survivors=(), events={"PLD": PEAD_8K}))
    universe = state["accum"]["universe"]
    assert universe["discovered"] == ["PLD"]
    core = [sym for step in state["plan"] if not step.get("screen") for sym in step["symbols"]]
    assert "PLD" in core


def test_the_screen_is_the_rotation_filtered_universe_heaviest_first():
    everything = _plan(_supabase(held=(), survivors=()))
    screen = [sym for step in everything["plan"] if step.get("screen") for sym in step["symbols"]]
    assert screen == ["NVDA", "MSFT", "JPM", "XOM", "AMD", "MU", "TXN", "PLD"]

    energy_only = _plan(_supabase(held=(), survivors=(), control={"active_sectors": ["Energy"]}))
    screen = [sym for step in energy_only["plan"] if step.get("screen") for sym in step["symbols"]]
    assert screen == ["XOM"]


def test_a_tight_cap_truncates_the_screen_instead_of_failing(monkeypatch):
    from services.core.core import premarket as pm

    monkeypatch.setattr(pm, "DAILY_CREDIT_CAP", 15)  # SPY + RSP + 11 sleeve + 2 room
    state = _plan(_supabase(held=(), survivors=()))

    assert state["status"] == "in_progress"
    screen = [sym for step in state["plan"] if step.get("screen") for sym in step["symbols"]]
    assert screen == ["NVDA", "MSFT"]
    assert state["accum"]["universe"]["screen_dropped"] == 6


def test_a_screen_symbol_the_vendor_cannot_serve_is_counted_not_alerted():
    supabase = _supabase(held=(), survivors=())
    results = run_to_completion(supabase, FakeFetcher(bad=("PLD",)))

    assert results[-1]["status"] == "ok"
    assert results[-1]["screen_errors"] == 1
    assert supabase.args("edge_alerts_enqueue") == []


def test_a_core_symbol_the_vendor_cannot_serve_still_alerts():
    supabase = _supabase(held=("MU",), survivors=())
    run_to_completion(supabase, FakeFetcher(bad=("MU",)))
    assert supabase.args("edge_alerts_enqueue")


def test_a_failed_discovery_falls_back_to_the_regime_set():
    state = _plan(_supabase(held=("MU",), survivors=(), raises=("edge_control_json",)))
    universe = state["accum"]["universe"]
    assert universe["discovered"] == []
    assert universe["screened"] == 0
    assert "edge_control_json" in universe["prefetch_error"]
    assert "MU" in universe["regime_symbols"]


def test_day_context_writes_are_batched_at_most_100_per_call(monkeypatch):
    from services.core.core import premarket as pm

    monkeypatch.setattr(pm, "DAY_CONTEXT_BATCH", 5)
    supabase = _supabase(held=(), survivors=())
    run_to_completion(supabase, FakeFetcher())

    calls = supabase.args("edge_day_context_write_symbols")
    assert len(calls) > 1
    assert all(len(c["p_symbols"]) <= 5 for c in calls)


def test_only_regime_and_pead_symbols_keep_their_bars():
    supabase = _supabase(held=("MU",), survivors=())
    run_to_completion(supabase, FakeFetcher())
    state = supabase.args("edge_premarket_state_write")[-1]["p_state"]
    kept = state["accum"]["bars"]

    assert "MU" in kept and "SPY" in kept
    assert "NVDA" not in kept  # screen-only: signals kept, bars dropped
    assert "NVDA" in state["accum"]["signals"]
    assert all(len(pairs) <= 60 for pairs in kept.values())


# ---------------------------------------------------------------------------
# The inputs breadth and sector relative strength read
# ---------------------------------------------------------------------------


def test_the_regime_payload_carries_what_breadth_and_relative_strength_need():
    """The posture's new inputs are derived here, from bars already fetched:
    each sector's 50-day and its 1M/3M return, and SPY's. Nothing else on the
    runner path can supply them — the runner fetches no indicators."""
    supabase = _supabase()
    fetcher = FakeFetcher()

    run_to_completion(supabase, fetcher)

    (regime_write,) = supabase.args("edge_regime_write")
    payload = regime_write["p_payload"]
    inputs = payload["regime_inputs"]
    assert {"ret_1m", "ret_3m"} <= set(inputs["spy"])
    for block in inputs["sectors"].values():
        assert {"last", "sma200", "sma50", "ret_1m", "ret_3m"} <= set(block)
        assert all(isinstance(block[k], float) for k in ("sma50", "ret_1m", "ret_3m"))

    regime = payload["regime"]
    assert regime["breadth"]["n_available"] == 11
    assert regime["breadth"]["breadth_200"] is not None
    assert regime["breadth"]["breadth_50"] is not None
    assert len(regime["sector_rs"]) == 11
    assert regime["posture"] in {
        "TRADE_NORMALLY", "REDUCED_SIZE", "HIGH_CONVICTION_ONLY",
        "NO_NEW_POSITIONS", "HOLD_CASH",
    }
    # No request was added for any of it.
    assert all("/time_series?" in url for url in fetcher.urls)


def test_a_plan_frozen_before_the_deploy_still_finishes_without_the_returns():
    """`accum["returns"]` does not exist in a state row an older deploy
    wrote. The 1-month return is re-derived from the kept closes, the 3-month
    one cannot be (60 kept, 64 needed) and is left out, not invented."""
    closes = {"SPY": [100.0 + i for i in range(60)], "XLK": [50.0 + i for i in range(60)]}

    inputs = premarket._regime_inputs(closes, {"SPY": 90.0, "XLK": 45.0}, ["XLK"])

    assert inputs["sectors"]["XLK"]["ret_1m"] == pytest.approx(109.0 / 88.0 - 1.0)
    assert "ret_3m" not in inputs["sectors"]["XLK"]
    assert inputs["sectors"]["XLK"]["sma50"] == pytest.approx(sum(range(60, 110)) / 50)
    assert "ret_3m" not in inputs["spy"]


# ---------------------------------------------------------------------------
# RSP-vs-SPY and SPY's 52-week drawdown, from the same bars
# ---------------------------------------------------------------------------


def test_rsp_rides_the_regime_batch_and_costs_one_credit():
    state = _plan(_supabase(held=(), survivors=()))

    assert "RSP" in state["accum"]["universe"]["regime_symbols"]
    assert "RSP" in state["accum"]["universe"]["keep_bars"]
    # SPY + RSP + the eleven sector ETFs, nothing else in the core steps.
    core = [s for step in state["plan"] if not step.get("screen") for s in step["symbols"]]
    assert len(core) == 13


def test_the_regime_payload_carries_rsp_vs_spy_and_the_drawdown():
    supabase = _supabase()
    fetcher = FakeFetcher()

    run_to_completion(supabase, fetcher)

    (regime_write,) = supabase.args("edge_regime_write")
    inputs = regime_write["p_payload"]["regime_inputs"]
    assert inputs["rsp_vs_spy_1m"] == pytest.approx(
        inputs["rsp"]["ret_1m"] - inputs["spy"]["ret_1m"]
    )
    # A drawdown is zero or negative, never positive.
    assert -1.0 < inputs["spy"]["drawdown_52w"] <= 0.0
    notes = regime_write["p_payload"]["regime"]["notes"]
    assert "rsp_vs_spy_unavailable" not in notes
    assert "drawdown_52w_unavailable" not in notes
    # Still one endpoint, still at most eight symbols a request.
    assert all("/time_series?" in url for url in fetcher.urls)


def test_the_drawdown_is_taken_from_the_whole_fetched_year_not_the_kept_bars():
    """The high is 190 sessions back: inside the 250 fetched, outside the 60
    kept. Measured off the kept bars it would read as no drawdown at all."""
    closes = [100.0] * 60 + [150.0] + [120.0] * 189

    assert premarket._returns_from(closes)["drawdown_52w"] == pytest.approx(-0.20)
    assert max(closes[-premarket.KEPT_BARS:]) == 120.0


def test_without_rsp_bars_the_comparison_is_left_out_not_invented():
    """A plan frozen before this deploy never fetched RSP."""
    closes = {"SPY": [100.0 + i for i in range(60)], "XLK": [50.0 + i for i in range(60)]}

    inputs = premarket._regime_inputs(closes, {"SPY": 90.0, "XLK": 45.0}, ["XLK"])

    assert "rsp_vs_spy_1m" not in inputs and "rsp" not in inputs
    assert "drawdown_52w" not in inputs["spy"]


# ---------------------------------------------------------------------------
# The PEAD targets, from recorded decide rows
# ---------------------------------------------------------------------------

_RECORDED = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "tests" / "fixtures" / "plays" / "pead_decide_earnings_2026-10-01.json"
    ).read_text(encoding="utf-8")
)


def _decide_rows(*names):
    """`edge_runlog` decide rows, newest first, as the job selects them."""
    return RoutedSupabase(
        {
            ("edge_runlog", "command=eq.decide"): [
                {"stdout": {"earnings": _RECORDED[name]}} for name in names
            ],
            ("edge_earnings_reactions", "select=symbol,report_date"): [],
        }
    )


def _targets(supabase, trading_day):
    from src import cli

    return {
        t["symbol"]: t for t in premarket._pead_targets(supabase, cli.load_config(), trading_day)
    }


def test_the_latest_decide_alone_would_have_found_no_pead_target():
    """What the job saw at 05:26 ET: the 22:53 overnight row, which carries
    the held book's earnings and nothing in the window."""
    assert _targets(_decide_rows("overnight_640"), "2026-10-02") == {}


def test_names_an_earlier_cycle_looked_at_are_targets_despite_the_overnight_row():
    """The recorded morning. MU, FDS and JBL were PEAD candidates at 15:13 ET
    the day before and skipped "reaction None" on every cycle of 10-01 and
    10-02, because the newest decide row was not the one that named them."""
    targets = _targets(_decide_rows("overnight_640", "monitor_633"), "2026-10-02")

    assert sorted(targets) == ["FDS", "JBL", "MU"]
    assert targets["MU"] == {"symbol": "MU", "report_date": "2026-09-30", "timing": "pm"}
    assert targets["FDS"]["timing"] == "am"


def test_with_pead_retired_the_same_morning_has_no_reaction_targets(monkeypatch):
    """As shipped. The module's other tests lift the retirement; this one
    puts it back."""
    from src import cli
    from src.plays import core

    monkeypatch.setattr(core, "RETIRED", {core.PEAD: core.RETIRED_REASON})
    rows = _decide_rows("overnight_640", "monitor_633")

    assert premarket._pead_targets(rows, cli.load_config(), "2026-10-02") == []


def test_the_window_is_counted_against_today_not_read_off_the_stored_row():
    """The stored rows say one session since. Five sessions later the same
    rows are outside the window; on the report day itself they are not in it
    yet."""
    rows = _decide_rows("overnight_640", "monitor_633")

    assert sorted(_targets(rows, "2026-10-05")) == ["FDS", "JBL", "MU"]   # 3 sessions
    assert _targets(rows, "2026-10-06") == {}                             # 4 sessions
    assert _targets(rows, "2026-09-30") == {}                             # 0 sessions


def test_a_name_whose_reaction_is_already_stored_is_not_a_target_again():
    supabase = _decide_rows("overnight_640", "monitor_633")
    supabase.rows[("edge_earnings_reactions", "select=symbol,report_date")] = [
        {"symbol": "MU", "report_date": "2026-09-30"}
    ]

    assert sorted(_targets(supabase, "2026-10-02")) == ["FDS", "JBL"]


def test_the_newest_report_wins_when_a_symbol_appears_in_two_rows():
    older = {"MU": {**_RECORDED["monitor_633"]["MU"], "last_report_date": "2026-06-25"}}
    supabase = RoutedSupabase(
        {
            ("edge_runlog", "command=eq.decide"): [
                {"stdout": {"earnings": older}},
                {"stdout": {"earnings": _RECORDED["monitor_633"]}},
            ],
            ("edge_earnings_reactions", "select=symbol,report_date"): [],
        }
    )

    assert _targets(supabase, "2026-10-02")["MU"]["report_date"] == "2026-09-30"


def test_the_job_asks_for_more_than_the_latest_decide_row():
    supabase = _decide_rows("overnight_640")
    seen = []
    real = supabase.select
    supabase.select = lambda table, query: (seen.append((table, query)), real(table, query))[1]

    _targets(supabase, "2026-10-02")

    (query,) = [q for t, q in seen if t == "edge_runlog"]
    assert f"limit={premarket.PEAD_DECIDE_LOOKBACK}" in query
    assert premarket.PEAD_DECIDE_LOOKBACK > 4
