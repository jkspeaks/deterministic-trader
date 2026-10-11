"""The control arm in the shadow replay, and trades that can finally close (S0).

`test_shadow.py` covers the rule variants. This file covers the five rows a
*signal* earns — itself, a seeded random same-sector stand-in, a no-RSI-exit
twin of each, and SPY over the same dates — and the two properties that make
them evidence rather than decoration:

* **both arms are priced by one rule** — same fill, same ATR source, same
  walk — so a difference between them is a difference between the names;
* **a closed row never moves, and an open one is never stranded** — the
  ledger held 12 rows and no closed trade at an earlier audit because a
  trade could only be re-derived from cycle inputs that are reaped at 7 days.

The last section runs the arm over the recorded cycle.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping

import pytest

from services.core.core import shadow
from services.core.tests.conftest import rows_from
from services.core.tests.test_shadow import (
    DAY_ONE, DAY_TWO, FULL_ONE, FULL_TWO, MONITOR_ONE, NOW, ShadowSupabase, StubBars, bar,
)
from src import cli, control_arm
from tests.factories import cli_payload

LOADED = cli.load_config()
ROOT = Path(__file__).resolve().parents[3]

# The synthetic universe has one Health Care name (PFE, the DIP signal). Two
# more give the control arm somebody to draw.
PEERS = ("MRK", "JNJ")
SNAPSHOT = {
    "as_of": "2026-08-02",
    "holdings": [
        {"symbol": s, "sector": sector}
        for s, sector in (("AAPL", "Information Technology"), ("F", "Consumer Discretionary"),
                          ("PFE", "Health Care"), ("XOM", "Energy"),
                          ("MRK", "Health Care"), ("JNJ", "Health Care"))
    ],
}
SNAPSHOT["cross_check"] = list(SNAPSHOT["holdings"])


class Ledger(ShadowSupabase):
    """`ShadowSupabase` plus what `edge_shadow_open` answers."""

    def __init__(self, cycles, stored=None, broken=False):
        super().__init__(cycles)
        self.stored = stored or {"open": [], "closed": []}
        self.broken = broken

    def rpc(self, function: str, params: Mapping[str, Any] | None = None) -> Any:
        if function == "edge_shadow_open":
            self.calls.append((function, dict(params or {})))
            if self.broken:
                raise RuntimeError("Could not find the function public.edge_shadow_open")
            return self.stored
        return super().rpc(function, params)


def two_days(stored=None, broken=False, **overrides) -> Ledger:
    cycles = {}
    for cycle_id, now in ((FULL_ONE, FULL_ONE), (MONITOR_ONE, MONITOR_ONE), (FULL_TWO, FULL_TWO)):
        payload = cli_payload(now=now, cycle_id=cycle_id, snapshot=SNAPSHOT, **overrides)
        cycles[cycle_id] = rows_from(payload, cycle_id)
    return Ledger(cycles, stored, broken)


def weekdays(start: str, count: int, step: int = 1) -> list[str]:
    days, cursor = [], date.fromisoformat(start)
    while len(days) < count:
        cursor += timedelta(days=step)
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
    return sorted(days)


BEFORE = weekdays(DAY_ONE, 40, step=-1)          # forty sessions of history
AFTER = weekdays(DAY_ONE, 12)                    # DAY_TWO and eleven more


def flat(level: float = 100.0, drift: float = 0.0) -> list[dict]:
    """History, the signal day and twelve sessions after it, in a 0.2-wide
    range: no stop, no target, and (with ``drift`` > 0) an RSI of 100."""
    days = [*BEFORE, DAY_ONE, *AFTER]
    out = []
    for i, day in enumerate(days):
        close = level + drift * i
        out.append(bar(day, close, close + 0.1, close - 0.1, close))
    return out


def bars_for(control: str, **series) -> StubBars:
    base = {"PFE": flat(25.0), control: flat(100.0), "SPY": flat(700.0)}
    base.update(series)
    return StubBars(base)


def run(supabase, bars=None):
    return shadow.run_shadow(supabase, bars, days=2, now=NOW)


def control_rows(supabase, day: str = DAY_ONE) -> dict[str, dict]:
    rows = [r for r in supabase.written()
            if r["variant"] in shadow.CONTROL_VARIANTS and r["entry_day"] == day]
    assert len({r["variant"] for r in rows}) == len(rows)
    return {r["variant"]: r for r in rows}


def the_control() -> str:
    supabase = two_days()
    run(supabase, StubBars({}))
    return control_rows(supabase)[shadow.R_RANDOM]["symbol"]


# --- a signal earns five rows ------------------------------------------------


def test_every_signal_gets_the_five_rows_and_they_are_paired():
    control = the_control()
    supabase = two_days()
    run(supabase, bars_for(control))

    rows = control_rows(supabase)
    assert set(rows) == set(shadow.CONTROL_VARIANTS)
    assert control in PEERS
    for variant in (shadow.R_SIGNAL, shadow.R_SIGNAL + shadow.NO_RSI):
        assert (rows[variant]["symbol"], rows[variant]["pair_symbol"]) == ("PFE", "")
    for variant in (shadow.R_RANDOM, shadow.R_RANDOM + shadow.NO_RSI):
        assert (rows[variant]["symbol"], rows[variant]["pair_symbol"]) == (control, "PFE")
    assert (rows[shadow.B_HOLD_SPY]["symbol"], rows[shadow.B_HOLD_SPY]["pair_symbol"]) == ("SPY", "PFE")
    # The stand-in trades under the signal's play: same exits, different name.
    assert {r["play_id"] for r in rows.values()} == {"DIP"}


def test_the_summary_counts_the_arm():
    control = the_control()
    result = run(two_days(), bars_for(control))

    assert result["control"]["signals"] == 2          # PFE ranked on both days
    assert result["control"]["no_control"] == 0
    assert result["control"]["rows"] == 10


def test_the_draw_is_identical_on_every_replay():
    """The nightly job re-derives every row inside its window. A stand-in that
    changed between Monday's run and Tuesday's would resample the experiment."""
    first, second = two_days(), two_days()
    run(first, StubBars({}))
    run(second, StubBars({}))

    assert control_rows(first)[shadow.R_RANDOM]["symbol"] == control_rows(second)[shadow.R_RANDOM]["symbol"]
    # And it is `control_arm`'s draw, not something the replay re-rolled.
    assert control_rows(first)[shadow.R_RANDOM]["symbol"] == control_arm.pick(
        "PFE", DAY_ONE, sorted(PEERS))


def test_a_sector_etf_is_never_the_random_same_sector_name():
    """`decide` buckets XLV with Health Care. Without peers in the universe
    the arm reports NO_CONTROL — it does not fall back to the sector's fund."""
    cycles = {FULL_ONE: rows_from(cli_payload(now=FULL_ONE, cycle_id=FULL_ONE), FULL_ONE)}
    supabase = Ledger(cycles)

    result = run(supabase, StubBars({}))

    missing = control_rows(supabase)[shadow.R_RANDOM]
    assert (missing["reason"], missing["symbol"], missing["pair_symbol"]) == (shadow.NO_CONTROL, "", "PFE")
    assert result["control"]["no_control"] == 1
    assert shadow.R_RANDOM + shadow.NO_RSI not in control_rows(supabase)


def test_a_held_or_signalling_name_is_never_drawn():
    """F is held and PFE is the signal; put both in PFE's sector and the only
    name left to draw is the one that is neither."""
    snapshot = {**SNAPSHOT, "holdings": [
        {"symbol": "F", "sector": "Health Care"}, {"symbol": "PFE", "sector": "Health Care"},
        {"symbol": "MRK", "sector": "Health Care"}, {"symbol": "AAPL", "sector": "Information Technology"},
        {"symbol": "XOM", "sector": "Energy"},
    ]}
    snapshot["cross_check"] = list(snapshot["holdings"])
    cycles = {FULL_ONE: rows_from(cli_payload(now=FULL_ONE, cycle_id=FULL_ONE, snapshot=snapshot), FULL_ONE)}
    supabase = Ledger(cycles)

    run(supabase, StubBars({}))

    assert control_rows(supabase)[shadow.R_RANDOM]["symbol"] == "MRK"


# --- one pricing rule for both arms ------------------------------------------


def test_both_arms_fill_at_the_next_sessions_open():
    control = the_control()
    series = flat(100.0)
    next(b for b in series if b["datetime"] == DAY_TWO)["open"] = "101.5"
    supabase = two_days()
    run(supabase, bars_for(control, **{control: series}))

    rows = control_rows(supabase)
    assert rows[shadow.R_RANDOM]["entry"] == 101.5
    assert rows[shadow.R_SIGNAL]["entry"] == 25.0
    assert {r["fill_day"] for r in rows.values()} == {DAY_TWO}
    assert {r["entry_day"] for r in rows.values()} == {DAY_ONE}


def test_the_atr_is_measured_from_bars_before_the_signal_day_for_both_arms():
    control = the_control()
    supabase = two_days()
    run(supabase, bars_for(control))

    rows = control_rows(supabase)
    # A 0.2-wide range every day: Wilder's ATR is 0.2 for either name.
    assert rows[shadow.R_SIGNAL]["atr"] == pytest.approx(0.2)
    assert rows[shadow.R_RANDOM]["atr"] == pytest.approx(0.2)


def test_the_rsi_exit_cuts_the_stand_in_short_and_the_twin_shows_it():
    """A stand-in already past RSI 50 leaves at the first eligible close under
    DIP's exit; without that exit the same name runs to the time stop. This is
    the artifact an earlier parity check found, as two rows side by side."""
    control = the_control()
    supabase = two_days()
    run(supabase, bars_for(control, **{control: flat(100.0, drift=0.01)}))

    rows = control_rows(supabase)
    with_rsi, without = rows[shadow.R_RANDOM], rows[shadow.R_RANDOM + shadow.NO_RSI]

    assert with_rsi["reason"] == shadow.SIGNAL
    assert with_rsi["exit_day"] == AFTER[1]             # not the fill day: the day after
    assert without["reason"] == shadow.TIME_STOP
    assert without["exit_day"] == AFTER[8]              # DIP's eight sessions after the fill
    assert without["exit_day"] > with_rsi["exit_day"]


def test_only_the_resting_target_can_act_on_the_fill_day():
    """The fill day is session 0. A stop-sized drop that day is not an exit
    (the live same-day-fill guard); a touch of the +6% target is."""
    control = the_control()
    dropped = flat(100.0)
    next(b for b in dropped if b["datetime"] == DAY_TWO)["low"] = "90.0"
    hit = flat(100.0)
    next(b for b in hit if b["datetime"] == DAY_TWO)["high"] = "107.0"

    low = two_days()
    run(low, bars_for(control, **{control: dropped}))
    high = two_days()
    run(high, bars_for(control, **{control: hit}))

    assert control_rows(low)[shadow.R_RANDOM + shadow.NO_RSI]["exit_day"] != DAY_TWO
    target = control_rows(high)[shadow.R_RANDOM + shadow.NO_RSI]
    assert (target["reason"], target["exit_day"], target["exit"]) == (shadow.TARGET, DAY_TWO, 106.0)
    assert target["pnl_pct"] == pytest.approx(0.06)


def test_spy_is_held_over_the_dates_the_no_rsi_signal_trade_was():
    control = the_control()
    spy = flat(700.0)
    next(b for b in spy if b["datetime"] == DAY_TWO)["open"] = "690.0"
    next(b for b in spy if b["datetime"] == AFTER[8])["close"] = "724.5"
    supabase = two_days()
    run(supabase, bars_for(control, SPY=spy))

    rows = control_rows(supabase)
    twin, beta = rows[shadow.R_SIGNAL + shadow.NO_RSI], rows[shadow.B_HOLD_SPY]
    assert twin["exit_day"] == AFTER[8]
    assert (beta["fill_day"], beta["exit_day"]) == (DAY_TWO, twin["exit_day"])
    assert (beta["entry"], beta["exit"]) == (690.0, 724.5)
    assert beta["pnl_pct"] == pytest.approx(724.5 / 690.0 - 1, abs=1e-6)


# --- data that is not there --------------------------------------------------


def test_a_stand_in_with_no_bars_is_a_row_with_no_numbers():
    control = the_control()
    supabase = two_days()
    run(supabase, StubBars({"PFE": flat(25.0), "SPY": flat(700.0)}))

    rows = control_rows(supabase)
    missing = rows[shadow.R_RANDOM]
    assert (missing["symbol"], missing["reason"]) == (control, shadow.NO_BARS)
    assert missing["entry"] is None and missing["pnl_pct"] is None and missing["bars_source"] is None
    # The signal's own rows are unaffected, and no other name was drawn instead.
    assert rows[shadow.R_SIGNAL]["entry"] == 25.0


def test_bars_that_stop_at_the_signal_day_are_awaiting_the_open_not_missing():
    control = the_control()
    early = [b for b in flat(100.0) if b["datetime"] <= DAY_ONE]
    supabase = two_days()
    run(supabase, bars_for(control, **{control: early}))

    waiting = control_rows(supabase)[shadow.R_RANDOM]
    assert waiting["reason"] == shadow.AWAITING_OPEN
    assert waiting["entry"] is None and waiting["bars_source"] == "twelvedata"


def test_a_ledger_that_cannot_be_read_does_not_fail_the_replay():
    """On a database without `edge_shadow_open` the call raises."""
    control = the_control()
    supabase = two_days(broken=True)

    result = run(supabase, bars_for(control))

    assert result["status"] == shadow.STATUS_OK
    assert result["control"]["stored_state"].startswith("unavailable: RuntimeError")
    assert set(control_rows(supabase)) == set(shadow.CONTROL_VARIANTS)


# --- closed rows never move; open rows are never stranded --------------------


def key(variant, symbol, day, pair=""):
    return {"variant": variant, "symbol": symbol, "entry_day": day, "pair_symbol": pair}


def test_a_closed_row_is_neither_refetched_nor_rewritten():
    control = the_control()
    closed = [
        key(shadow.R_SIGNAL, "PFE", DAY_ONE), key(shadow.R_SIGNAL + shadow.NO_RSI, "PFE", DAY_ONE),
        key(shadow.R_RANDOM, control, DAY_ONE, "PFE"),
        key(shadow.R_RANDOM + shadow.NO_RSI, control, DAY_ONE, "PFE"),
        key(shadow.B_HOLD_SPY, "SPY", DAY_ONE, "PFE"),
    ]
    supabase = two_days(stored={"open": [], "closed": closed})

    result = run(supabase, bars_for(control))

    assert control_rows(supabase) == {}                    # day one: all five left alone
    assert set(control_rows(supabase, DAY_TWO)) == set(shadow.CONTROL_VARIANTS)
    assert result["control"]["signals_already_closed"] == 1


def test_a_closed_variant_trade_is_left_alone_too():
    stored = {"open": [], "closed": [key(shadow.BASELINE, "PFE", DAY_ONE)]}
    supabase = two_days(stored=stored)

    result = run(supabase, StubBars({}))

    baseline = [r for r in supabase.written() if r["variant"] == shadow.BASELINE]
    assert DAY_ONE not in {r["entry_day"] for r in baseline}
    assert result["variants"][shadow.BASELINE]["already_closed"] == 1


def stored_open(variant, symbol, day, pair="", **fields):
    return {**key(variant, symbol, day, pair), "play_id": "DIP", "entry_cycle_id": "old-cycle",
            "quantity": 1.0, "entry": None, "atr": None, "fill_day": None,
            "reason": shadow.OPEN, "updated_at": "2026-08-01T05:00:00Z", **fields}


def test_an_open_trade_whose_inputs_are_gone_is_closed_from_its_own_row():
    """No stored cycle at all — `edge_cycle_input` reaped everything — and the
    ledger still holds an open baseline trade with its entry and ATR."""
    stored = {"closed": [], "open": [
        stored_open(shadow.BASELINE, "PFE", DAY_ONE, entry=25.0, atr=0.2),
    ]}
    supabase = Ledger({}, stored)

    result = run(supabase, StubBars({"PFE": flat(25.0)}))

    (row,) = supabase.written()
    assert (row["variant"], row["symbol"], row["entry_day"]) == (shadow.BASELINE, "PFE", DAY_ONE)
    assert row["reason"] == shadow.TIME_STOP and row["exit_day"] == AFTER[7]
    assert row["entry"] == 25.0 and row["pnl_pct"] == pytest.approx(0.0)
    assert result["control"]["stored_unresolved"] == 1 and result["control"]["stored_resolved"] == 1


def test_an_orphaned_control_pair_is_finished_including_its_spy_row():
    stored = {"closed": [], "open": [
        stored_open(shadow.R_RANDOM + shadow.NO_RSI, "MRK", DAY_ONE, "PFE"),
        stored_open(shadow.B_HOLD_SPY, "SPY", DAY_ONE, "PFE", pair_exit_day=AFTER[8]),
    ]}
    supabase = Ledger({}, stored)

    run(supabase, StubBars({"MRK": flat(100.0), "SPY": flat(700.0)}))

    rows = {r["variant"]: r for r in supabase.written()}
    assert rows[shadow.R_RANDOM + shadow.NO_RSI]["reason"] == shadow.TIME_STOP
    assert rows[shadow.R_RANDOM + shadow.NO_RSI]["pair_symbol"] == "PFE"
    assert (rows[shadow.B_HOLD_SPY]["exit_day"], rows[shadow.B_HOLD_SPY]["reason"]) == (
        AFTER[8], shadow.TIME_STOP)


def test_an_orphan_with_no_bars_tonight_is_left_as_it_is():
    stored = {"closed": [], "open": [stored_open(shadow.BASELINE, "PFE", DAY_ONE, entry=25.0, atr=0.2)]}
    supabase = Ledger({}, stored)

    run(supabase, StubBars({}))

    assert supabase.written() == []


def test_the_bar_budget_defers_and_the_least_recently_walked_goes_first(monkeypatch):
    monkeypatch.setattr(shadow, "MAX_BAR_SYMBOLS", 2)
    stored = {"closed": [], "open": [
        stored_open(shadow.BASELINE, "AAA", "2026-07-20", entry=10.0, updated_at="2026-08-03T05:00:00Z"),
        stored_open(shadow.BASELINE, "BBB", "2026-07-21", entry=10.0, updated_at="2026-08-01T05:00:00Z"),
        stored_open(shadow.BASELINE, "CCC", "2026-07-22", entry=10.0, updated_at="2026-08-02T05:00:00Z"),
    ]}
    supabase = Ledger({}, stored)
    bars = StubBars({})

    result = run(supabase, bars)

    assert bars.asked == ["BBB", "CCC"]                 # oldest `updated_at` first
    assert result["control"]["bars_deferred"] == 1


# --- the recorded cycle -------------------------------------------

RECORDED = json.loads(
    (ROOT / "tests" / "fixtures" / "decide" / "full_cycles_2026-09-18_and_10-02.json")
    .read_text(encoding="utf-8")
)["2026-09-18"]
UNIVERSE = json.loads((ROOT / "data" / "universe.json").read_text(encoding="utf-8"))
SECTORS = {row["symbol"]: row["sector"] for row in UNIVERSE["cross_check"]}
RECORDED_DAY = "2026-09-18"


def recorded_pairs() -> list[shadow.Pair]:
    """The recorded decide row, with the two things the reduced fixture left
    out put back from their own source of record: the universe's sector map
    (`data/universe.json`) and the member list."""
    decided = {
        **RECORDED,
        "sectors": {**SECTORS, **RECORDED["sectors"]},
        "universe": {"candidates": RECORDED["pool"],
                     "members": [row["symbol"] for row in UNIVERSE["holdings"]]},
    }
    cycle = shadow.Cycle(RECORDED["cycle_id"], RECORDED_DAY, {"signals": {}})
    return shadow._pairs([(cycle, decided)], LOADED)


def test_the_recorded_cohort_gets_control_rows():
    """Each gets a stand-in from its own sector that the cycle had not touched,
    and with bars for both the five rows appear."""
    pairs = recorded_pairs()
    assert sorted(p.symbol for p in pairs) == ["INVH", "KIM", "RF", "RTX"]

    touched = set(RECORDED["pool"]) | {row["symbol"] for row in RECORDED["book"]}
    after = weekdays(RECORDED_DAY, 12)
    before = weekdays(RECORDED_DAY, 40, step=-1)

    def synthetic(level):
        # Bars are not part of a cycle's envelope; these are synthetic and
        # only there to show the rows price. The pairing is what is recorded.
        return [bar(day, level, level + 0.1, level - 0.1, level)
                for day in (*before, RECORDED_DAY, *after)]

    for pair in pairs:
        assert pair.control and pair.control not in touched
        assert SECTORS[pair.control] == SECTORS[pair.symbol]
        assert pair.play_id == "DIP" and pair.cycle_id == RECORDED["cycle_id"]

        bars = {pair.symbol: synthetic(50.0), pair.control: synthetic(80.0), "SPY": synthetic(700.0)}
        rows = shadow._pair_rows(pair, bars, LOADED)

        assert [r["variant"] for r in rows] == [
            shadow.R_SIGNAL, shadow.R_SIGNAL + shadow.NO_RSI,
            shadow.R_RANDOM, shadow.R_RANDOM + shadow.NO_RSI, shadow.B_HOLD_SPY,
        ]
        assert all(r["entry_day"] == RECORDED_DAY and r["fill_day"] == after[0] for r in rows)
        assert all(r["reason"] in shadow.CLOSED for r in rows)
        assert rows[2]["symbol"] == pair.control and rows[2]["pair_symbol"] == pair.symbol


def test_the_recorded_draw_is_stable():
    assert [(p.symbol, p.control) for p in recorded_pairs()] == [
        (p.symbol, p.control) for p in recorded_pairs()
    ]
