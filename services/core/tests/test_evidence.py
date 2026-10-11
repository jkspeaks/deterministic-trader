"""`core/evidence.py` — the nightly back-fill of the decision snapshot (S0).

What matters about this job is what it refuses to do: spend a Twelve Data
credit on a row that has nothing to gain, write a number it did not observe,
recompute "as of the decision" from a later history, or raise. The arithmetic
of a return or a feature is `src/forward.py`'s and `tests/test_forward.py`'s;
this file is about which rows are visited, what is fetched for them and what
is written back.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Mapping

import pytest

from services.core.core import evidence
from src import cli, forward

CALENDAR = cli.load_config()["calendar"]

DAY = "2026-08-03"                    # a Monday
PENDING = "edge_decision_snapshot_pending"
WRITE = "edge_decision_forward_write"


def sessions(count: int, start: str = DAY) -> list[str]:
    return forward.sessions_after(start, count, CALENDAR)


def after(day: str, days: int = 1) -> str:
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


class Supabase:
    def __init__(self, pending: list[dict], fail: str | None = None):
        self.pending = pending
        self.fail = fail
        self.calls: list[tuple[str, dict]] = []

    def rpc(self, function: str, params: Mapping[str, Any] | None = None) -> Any:
        self.calls.append((function, dict(params or {})))
        if function == self.fail:
            raise RuntimeError("PostgREST said no")
        if function == PENDING:
            return self.pending
        if function == WRITE:
            return len((params or {}).get("p_rows") or [])
        return None

    def written(self) -> list[dict]:
        return [row for name, params in self.calls if name == WRITE for row in params["p_rows"]]


class Budget:
    used = 0


class Bars:
    """A `TwelveData` with one method, a credit counter and no network."""

    def __init__(self, series: Mapping[str, list]):
        self.series = {key: list(value) for key, value in series.items()}
        self.asked: list[str] = []
        self.outputsize = None
        self.budget = Budget()
        self.errors: list[str] = []

    def time_series(self, symbols, outputsize=None):
        self.asked = list(symbols)
        self.outputsize = outputsize
        self.budget.used += len(self.asked)
        return {s: list(self.series[s]) for s in symbols if s in self.series}


def td(day: str, close: float, open_: float | None = None) -> dict:
    """One bar the way Twelve Data sends it: strings, under `datetime`."""
    open_ = close if open_ is None else open_
    return {"datetime": day, "open": str(open_), "high": str(max(open_, close) + 0.5),
            "low": str(min(open_, close) - 0.5), "close": str(close)}


def history(count: int = 280, end_before: str = DAY, first: float = 50.0) -> list[dict]:
    days: list[str] = []
    cursor = date.fromisoformat(end_before)
    while len(days) < count:
        cursor -= timedelta(days=1)
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
    days.reverse()
    return [td(day, first * 1.001 ** i) for i, day in enumerate(days)]


def through(count: int, base: float = 100.0) -> list[dict]:
    """A year of history, the decision day at ``base`` and ``count`` sessions
    after it, each one point higher."""
    return history() + [td(day, base + i) for i, day in enumerate([DAY, *sessions(count)])]


def row(symbol="MU", play_id="DIP", cycle_id="c-1", day=DAY, etf="XLK",
        has_features=False, status=None) -> dict:
    return {"cycle_id": cycle_id, "symbol": symbol, "play_id": play_id, "trading_day": day,
            "sector_etf": etf, "has_features": has_features, "fwd_status": status or {}}


def run(supabase, bars, today, **kw):
    return evidence.run_backfill(supabase, bars, now=f"{today}T06:05:00Z", **kw)


# --- the first visit ---------------------------------------------------------


def test_the_first_night_writes_features_and_the_one_session_return():
    today = after(sessions(1)[0])
    supabase = Supabase([row()])
    bars = Bars({"MU": through(1), "SPY": through(1, 700.0), "XLK": through(1, 200.0)})

    result = run(supabase, bars, today)

    assert result["status"] == "ok" and result["written"] == 1
    (update,) = supabase.written()
    assert (update["cycle_id"], update["symbol"], update["play_id"]) == ("c-1", "MU", "DIP")
    assert update["fwd"]["cc_1"] == pytest.approx(0.01)
    assert update["fwd_status"]["1"] == "ok" and update["fwd_status"]["8"] == "pending"
    assert update["complete"] is False and update["bars_source"] == "twelvedata"
    # 12-1 momentum, residual momentum and RS vs the sector ETF: all need the
    # year of bars and the two reference series this pass also fetched.
    features = update["price_features"]
    assert features["mom_12_1"] is not None and features["dist_52w_high"] is not None
    assert features["rs_3m_vs_sector"] is not None and features["resid_mom_12_1"] is not None
    assert features["last_bar"] < DAY                      # strictly before the decision


def test_the_reference_series_are_fetched_once_for_the_whole_pass():
    today = after(sessions(1)[0])
    supabase = Supabase([row("MU"), row("ON"), row("HPE")])
    bars = Bars({s: through(1) for s in ("MU", "ON", "HPE", "SPY", "XLK")})

    result = run(supabase, bars, today)

    assert sorted(bars.asked) == ["HPE", "MU", "ON", "SPY", "XLK"]
    assert bars.outputsize == evidence.BAR_COUNT
    assert result["credits"] == 5 and result["written"] == 3


def test_one_fetch_fills_every_pending_row_for_the_symbol():
    """Two cycles and two plays judged MU on the same day: three rows, one credit."""
    today = after(sessions(1)[0])
    supabase = Supabase([
        row(cycle_id="c-1", play_id="DIP"),
        row(cycle_id="c-1", play_id="CATALYST"),
        row(cycle_id="c-2", play_id="DIP"),
    ])
    bars = Bars({"MU": through(1), "SPY": through(1), "XLK": through(1)})

    result = run(supabase, bars, today)

    assert bars.asked.count("MU") == 1
    assert result["written"] == 3
    assert {(u["cycle_id"], u["play_id"]) for u in supabase.written()} == {
        ("c-1", "DIP"), ("c-1", "CATALYST"), ("c-2", "DIP")}


# --- spending a credit only where it buys something --------------------------


def test_a_row_with_features_and_no_new_horizon_is_not_fetched():
    """After the first visit the next thing worth fetching for is the
    8-session return. Sessions 2 to 7 cost nothing."""
    visited = row(has_features=True, status={"1": "ok", "3": "pending", "5": "pending",
                                             "8": "pending", "21": "pending"})
    for elapsed in (2, 3, 5, 7):
        supabase = Supabase([visited])
        bars = Bars({"MU": through(elapsed)})

        result = run(supabase, bars, after(sessions(elapsed)[-1]))

        assert bars.asked == [], elapsed
        assert result["due"] == 0 and result["credits"] == 0
        assert supabase.written() == []


def test_the_eight_session_visit_fills_three_five_and_eight_together():
    visited = row(has_features=True, status={"1": "ok", "3": "pending", "5": "pending",
                                             "8": "pending", "21": "pending"})
    supabase = Supabase([visited])
    bars = Bars({"MU": through(8)})

    run(supabase, bars, after(sessions(8)[-1]))

    assert bars.asked == ["MU"]                 # no SPY, no ETF: features are done
    (update,) = supabase.written()
    assert [update["fwd_status"][h] for h in ("3", "5", "8", "21")] == ["ok", "ok", "ok", "pending"]
    assert update["fwd"]["cc_8"] == pytest.approx(0.08)


def test_features_are_written_once_and_never_recomputed():
    """`price_features` is "as of the decision". A later pass has a longer,
    possibly re-adjusted history and must not send a second opinion."""
    visited = row(has_features=True, status={"1": "ok"})
    supabase = Supabase([visited])

    run(supabase, Bars({"MU": through(8)}), after(sessions(8)[-1]))

    assert supabase.written()[0]["price_features"] is None


def test_the_last_visit_completes_the_row():
    visited = row(has_features=True, status={"1": "ok", "3": "ok", "5": "ok", "8": "ok", "21": "pending"})
    supabase = Supabase([visited])

    run(supabase, Bars({"MU": through(22)}), after(sessions(21)[-1]))

    (update,) = supabase.written()
    assert update["complete"] is True
    assert update["fwd"]["cc_21"] == pytest.approx(0.21)
    assert update["fwd"]["no_21"] == pytest.approx(121 / 101 - 1, abs=1e-6)


def test_a_decision_made_today_has_nothing_to_fetch_for_but_its_features():
    """The night after the decision the bars before it exist; a return does not."""
    supabase = Supabase([row()])
    bars = Bars({"MU": history() + [td(DAY, 100.0)], "SPY": history(), "XLK": history()})

    run(supabase, bars, after(DAY))

    (update,) = supabase.written()
    assert update["price_features"]["mom_12_1"] is not None
    assert set(update["fwd_status"].values()) == {"pending"}
    assert all(value is None for value in update["fwd"].values())


# --- data that is not there --------------------------------------------------


def test_a_symbol_with_no_bars_gets_no_numbers_and_says_so():
    today = after(sessions(1)[0])
    supabase = Supabase([row("ZZZZ", etf=None)])
    bars = Bars({"SPY": through(1)})

    result = run(supabase, bars, today)

    assert result["symbols_without_bars"] == ["ZZZZ"]
    (update,) = supabase.written()
    assert update["price_features"] is None and update["bars_source"] is None
    assert update["fwd_status"]["1"] == "missing_bar"
    assert all(value is None for value in update["fwd"].values())
    assert update["complete"] is False                      # still inside the grace


def test_a_halted_session_is_recorded_as_missing_not_bridged():
    today = after(sessions(8)[-1])
    series = [b for b in through(8) if b["datetime"] != sessions(5)[-1]]
    supabase = Supabase([row(has_features=True, status={"1": "ok"})])

    run(supabase, Bars({"MU": series}), today)

    (update,) = supabase.written()
    assert update["fwd_status"]["5"] == "missing_bar" and update["fwd"]["cc_5"] is None
    assert update["fwd_status"]["8"] == "ok" and update["fwd"]["cc_8"] == pytest.approx(0.08)


def test_an_unadjusted_split_is_flagged_and_no_loss_is_written():
    today = after(sessions(8)[-1])
    days = [DAY, *sessions(8)]
    split = history() + [
        td(day, (100.0 if i < 4 else 50.0) + 0.1 * i, open_=50.3 if i == 4 else None)
        for i, day in enumerate(days)
    ]
    supabase = Supabase([row(has_features=True, status={"1": "ok"})])

    run(supabase, Bars({"MU": split}), today)

    (update,) = supabase.written()
    assert update["fwd_status"]["3"] == "ok"
    assert update["fwd_status"]["5"] == update["fwd_status"]["8"] == "split_suspect"
    assert update["fwd"]["cc_5"] is None and update["fwd"]["cc_8"] is None


def test_a_row_past_every_grace_is_closed_without_a_fetch():
    """A delisted name: every horizon matured long ago and no bar ever came.
    It is closed as it stands instead of being fetched for ever."""
    stale = row("DEAD", has_features=False,
                status={h: "missing_bar" for h in ("1", "3", "5", "8", "21")})
    today = after(sessions(21)[-1], forward.MISSING_GRACE_DAYS + 5)
    supabase = Supabase([stale])
    bars = Bars({})

    result = run(supabase, bars, today)

    assert bars.asked == [] and result["closed_unfilled"] == 1
    (update,) = supabase.written()
    assert update["complete"] is True
    # Nulls, so `edge_decision_forward_write`'s coalesce keeps what is stored.
    assert update["fwd"] is None and update["fwd_status"] is None


# --- the budget --------------------------------------------------------------


def test_a_pass_stays_inside_its_credit_budget_and_takes_the_oldest_first():
    today = after(sessions(1)[0])
    older = "2026-07-31"
    pending = [row(f"S{i:02d}", etf="XLK") for i in range(10)] + [row("OLD", day=older, etf="XLE")]
    supabase = Supabase(pending)
    bars = Bars({})

    result = run(supabase, bars, today, max_symbols=6)

    assert len(bars.asked) <= 6
    # The oldest decision first, with the SPY and sector series it needs.
    assert bars.asked[0] == "OLD" and {"SPY", "XLE"} <= set(bars.asked)
    assert result["symbols_due"] == 11
    assert len(result["symbols_fetched"]) < 11             # the rest wait for the next pass


def test_a_name_is_not_fetched_without_the_reference_series_its_features_need():
    """Room for one more symbol, and the next name needs a new sector ETF as
    well: it waits, rather than being written with a null relative strength
    it would never be revisited for."""
    today = after(sessions(1)[0])
    supabase = Supabase([row("AAA", etf="XLK"), row("BBB", etf="XLE")])
    bars = Bars({})

    run(supabase, bars, today, max_symbols=4)

    assert sorted(bars.asked) == ["AAA", "SPY", "XLK"]


def test_nothing_due_makes_no_request_at_all():
    visited = row(has_features=True, status={"1": "ok"})
    supabase = Supabase([visited])
    bars = Bars({})

    result = run(supabase, bars, after(sessions(3)[-1]))

    assert bars.asked == [] and result["credits"] == 0
    assert WRITE not in [name for name, _ in supabase.calls]


# --- never on a cycle's path, never raising ----------------------------------


def test_no_market_data_key_is_a_degradation_not_a_failure():
    supabase = Supabase([row()])

    result = evidence.run_backfill(supabase, None, now=f"{after(sessions(1)[0])}T06:05:00Z")

    assert result["status"] == "ok"
    assert result["due"] == 1 and result["written"] == 0 and result["symbols_fetched"] == []


@pytest.mark.parametrize("failing", [PENDING, WRITE])
def test_a_supabase_failure_is_reported_not_raised(failing):
    supabase = Supabase([row()], fail=failing)
    bars = Bars({"MU": through(1), "SPY": through(1), "XLK": through(1)})

    result = run(supabase, bars, after(sessions(1)[0]))

    assert result["status"] == "failed" and "PostgREST said no" in result["detail"]


def test_a_dry_run_computes_everything_and_writes_nothing():
    supabase = Supabase([row()])
    bars = Bars({"MU": through(1), "SPY": through(1), "XLK": through(1)})

    result = run(supabase, bars, after(sessions(1)[0]), write=False)

    assert result["updated"] == 1 and result["written"] == 0
    assert WRITE not in [name for name, _ in supabase.calls]


def test_the_job_touches_nothing_a_cycle_reads():
    """Two RPCs, both on `edge_decision_snapshot`. No control key, no runlog,
    no alert: this job cannot change what a cycle decides."""
    supabase = Supabase([row()])
    bars = Bars({"MU": through(1), "SPY": through(1), "XLK": through(1)})

    run(supabase, bars, after(sessions(1)[0]))

    assert {name for name, _ in supabase.calls} == {PENDING, WRITE}
