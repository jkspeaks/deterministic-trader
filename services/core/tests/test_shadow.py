"""Three things are worth asserting here and everything else follows from them:

1. **The replay runs the shipped core.** A variant is a config override and
   nothing else, so `baseline` must produce exactly the buys `cli.decide`
   produces for the same payload, and a variant's override must be visible in
   its own trades without leaking into any other variant's.
2. **A day with no stored input is reported, not skipped.** `edge_cycle_input`
   is reaped at seven days; a ten-day window that silently returned eight days
   of trades would be indistinguishable from eight quiet days, which is this
   project's dominant defect class wearing a new hat.
3. **Nothing is priced that was not observed.** No next-cycle quote is
   `NO_FILL_QUOTE`, no bars is `NO_BARS`, and neither carries a number.
"""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from services.core.core import shadow
from services.core.tests.conftest import rows_from
from src import cli
from tests.factories import cli_payload

# Both are open days in `config/market_holidays.json`, which is what
# `_trading_days` walks.
DAY_ONE = "2026-08-03"
DAY_TWO = "2026-08-04"

# 09:40 and 11:40 ET, the two slots Appendix A actually runs on a Monday.
FULL_ONE = "2026-08-03T13:40:00Z"
MONITOR_ONE = "2026-08-03T15:40:00Z"
FULL_TWO = "2026-08-04T13:40:00Z"

NOW = "2026-08-04T21:00:00Z"


class ShadowSupabase:
    """A Supabase that can tell two cycles apart.

    `conftest.FakeSupabase` deliberately does not filter — every caller in
    `run.py` reads one cycle and there is only one. The shadow replay reads
    several, in order, and a fake that returned every cycle's rows for every
    cycle id would assemble one enormous cycle and prove nothing.
    """

    def __init__(self, cycles: Mapping[str, list], listed=None):
        self.cycles = {key: list(value) for key, value in cycles.items()}
        self.listed = (
            [{"cycle_id": cycle_id} for cycle_id in self.cycles]
            if listed is None
            else list(listed)
        )
        self.calls: list[tuple[str, dict]] = []

    def rpc(self, function: str, params: Mapping[str, Any] | None = None) -> Any:
        self.calls.append((function, dict(params or {})))
        if function == "edge_shadow_cycles":
            return self.listed
        if function == "edge_shadow_trades_write":
            return len((params or {}).get("p_rows") or [])
        return None

    def select(self, table: str, query: str) -> list[dict]:
        assert table == "edge_cycle_input"
        cycle_id = query.split("cycle_id=eq.", 1)[1].split("&", 1)[0]
        return list(self.cycles.get(cycle_id, []))

    def written(self) -> list[dict]:
        for name, params in self.calls:
            if name == "edge_shadow_trades_write":
                return list(params["p_rows"])
        return []


class StubBars:
    """A `TwelveData` with one method and no network."""

    def __init__(self, series: Mapping[str, list]):
        self.series = {key: list(value) for key, value in series.items()}
        self.asked: list[str] = []

    def time_series(self, symbols, outputsize=None):
        self.asked = list(symbols)
        return {s: list(self.series[s]) for s in symbols if s in self.series}


def bar(day: str, open_: float, high: float, low: float, close: float) -> dict:
    return {
        "datetime": day,
        "open": str(open_),
        "high": str(high),
        "low": str(low),
        "close": str(close),
    }


def two_days(**payload_overrides) -> ShadowSupabase:
    """Two trading days: a full cycle and a monitor on the first, a full on the
    second. The monitor is what the first day's entry fills against."""
    cycles = {}
    for cycle_id, now in (
        (FULL_ONE, FULL_ONE),
        (MONITOR_ONE, MONITOR_ONE),
        (FULL_TWO, FULL_TWO),
    ):
        payload = cli_payload(now=now, cycle_id=cycle_id, **payload_overrides)
        cycles[cycle_id] = rows_from(payload, cycle_id)
    return ShadowSupabase(cycles)


def variant_rows(supabase) -> list[dict]:
    """The rows of `shadow.VARIANTS` — everything but the control arm, which
    `test_shadow_control.py` covers."""
    names = {variant.name for variant in shadow.VARIANTS}
    return [row for row in supabase.written() if row["variant"] in names]


def run(supabase, bars=None, days=2, write=True):
    return shadow.run_shadow(supabase, bars, days=days, now=NOW, write=write)


# ---------------------------------------------------------------------------
# 1. The replay runs the shipped core
# ---------------------------------------------------------------------------


def test_baseline_enters_exactly_what_cli_decide_would_have_bought():
    payload = cli_payload(now=FULL_ONE, cycle_id=FULL_ONE)
    expected = {
        order["symbol"]
        for order in cli.decide(payload, cli.load_config())["plan"]["orders"]
        if order["side"] == "buy"
    }
    assert expected, "the fixture must produce a buy or this test proves nothing"

    supabase = two_days()
    run(supabase)

    baseline = [
        row for row in supabase.written() if row["variant"] == shadow.BASELINE
    ]
    assert {row["symbol"] for row in baseline} == expected
    assert all(row["play_id"] == "DIP" for row in baseline)


def test_every_variant_is_replayed_and_none_leaks_into_another():
    supabase = two_days()
    result = run(supabase)

    assert set(result["variants"]) == {variant.name for variant in shadow.VARIANTS}
    rows = supabase.written()
    assert rows, "the replay produced no trades at all"
    # Every row belongs to exactly one registered variant, and the primary key
    # the migration declares is unique across the batch.
    keys = [(row["variant"], row["symbol"], row["entry_day"]) for row in rows]
    assert len(keys) == len(set(keys))


def test_a_variant_override_reaches_the_plays_and_not_the_shipped_config():
    shipped = cli.load_config()
    variant = next(v for v in shadow.VARIANTS if v.name == "A_dip_band")

    overridden = shadow._variant_config(shipped, variant)

    assert overridden["config"]["dip_trend_band_pct"] == 0.0
    # The shipped mapping is untouched — every variant shares it.
    assert shipped["config"]["dip_trend_band_pct"] == 0.03
    # And only `config` is replaced; the calendar is the same object.
    assert overridden["calendar"] is shipped["calendar"]


def test_the_prompt_52_variants_flip_switches_the_shipped_file_ships_false():
    """So the assertion is `is False` rather than "not there", and it is the
    assertion that catches somebody turning one on in the command centre by
    hand.
    """
    shipped = cli.load_config()

    for key in (
        "play_dip_pullback_enabled",
        "play_sector_enabled",
        "risk_sizing_enabled",
    ):
        assert shipped["config"][key] is False, key

    by_name = {v.name: v for v in shadow.VARIANTS}
    assert by_name["E_dip_pullback"].overrides == {
        "play_dip_pullback_enabled": True,
        "risk_sizing_enabled": True,
    }
    assert by_name["F_sector_play"].overrides == {"play_sector_enabled": True}

    # And the overrides actually land, without touching the shipped mapping.
    on = shadow._variant_config(shipped, by_name["F_sector_play"])
    assert on["config"]["play_sector_enabled"] is True
    assert shipped["config"]["play_sector_enabled"] is False


def test_every_variant_name_is_unique_and_fits_the_ledgers_column():
    """`edge_shadow_trades`'s primary key is (variant, symbol, entry_day), so a
    duplicated name would silently merge two rule sets into one column."""
    names = [v.name for v in shadow.VARIANTS]

    assert len(names) == len(set(names))
    assert all(name and len(name) <= 32 for name in names)


def test_a_variant_that_suppresses_leaves_baselines_trade_with_no_row_of_its_own():
    # §4.3 F's count-the-suppressed-trades instruction, made answerable: a
    # 2%-wide spread on PFE is far over F's 0.3% ceiling, so `D_no_trade`
    # never proposes it while every other variant does.
    cycles = {}
    for cycle_id, now in ((FULL_ONE, FULL_ONE), (MONITOR_ONE, MONITOR_ONE)):
        payload = cli_payload(
            now=now,
            cycle_id=cycle_id,
            quotes={"PFE": {"bid": 24.88, "ask": 25.38, "last": 25.11}},
        )
        cycles[cycle_id] = rows_from(payload, cycle_id)
    supabase = ShadowSupabase(cycles)

    run(supabase, StubBars({}))

    entered = {
        (row["variant"], row["symbol"]) for row in supabase.written()
    }
    assert (shadow.BASELINE, "PFE") in entered
    assert ("D_no_trade", "PFE") not in entered


def test_the_baseline_variant_overrides_nothing():
    baseline = next(v for v in shadow.VARIANTS if v.name == shadow.BASELINE)
    assert baseline.overrides == {}


def test_a_variant_whose_decide_refuses_every_cycle_says_so_rather_than_zero():
    # Zero entries because the rules declined, and zero entries because the
    # replay crashed, are the same number. One of them has to be named.
    supabase = two_days()
    result = run(supabase)
    assert all(row["cycles_refused"] == [] for row in result["variants"].values())

    original = shadow._decide

    def exploding(cycle, loaded):
        if loaded["config"].get("dip_trend_band_pct") == 0.0:
            raise RuntimeError("the A variant broke")
        return original(cycle, loaded)

    shadow._decide = exploding
    try:
        result = run(two_days())
    finally:
        shadow._decide = original

    assert result["status"] == shadow.STATUS_OK
    assert result["variants"]["A_dip_band"]["entries"] == 0
    assert len(result["variants"]["A_dip_band"]["cycles_refused"]) == 3
    assert "the A variant broke" in result["variants"]["A_dip_band"]["cycles_refused"][0]
    # Every other variant is unaffected.
    assert result["variants"][shadow.BASELINE]["entries"] > 0


# ---------------------------------------------------------------------------
# 2. A day with no stored input is reported, not skipped
# ---------------------------------------------------------------------------


def test_a_requested_day_with_no_rows_says_so_instead_of_reading_as_quiet():
    # Ten days requested, two days stored.
    result = run(two_days(), days=10)

    days = {row["trading_day"]: row for row in result["days"]}
    assert len(days) == 10
    assert days[DAY_ONE]["status"] == shadow.DAY_REPLAYED
    assert days[DAY_TWO]["status"] == shadow.DAY_REPLAYED

    missing = [row for row in result["days"] if row["cycles"] == 0]
    assert missing, "a ten-day window over two stored days must report gaps"
    assert all(row["status"] == shadow.DAY_NO_INPUTS for row in missing)
    assert all("reaped" in row["status"] for row in missing)


def test_a_cycle_whose_rows_will_not_assemble_is_recorded_not_fatal():
    supabase = two_days()
    supabase.cycles["broken"] = [{"kind": "control", "symbol": None, "payload": {}}]
    supabase.listed.append({"cycle_id": "broken"})

    result = run(supabase)

    assert result["status"] == shadow.STATUS_OK
    assert [row["cycle_id"] for row in result["unreadable"]] == ["broken"]
    # The other three cycles still ran.
    assert result["cycles"] == 3


def test_a_day_holding_only_unassemblable_rows_is_not_reported_as_having_none():
    # "Nothing happened" and "nothing could be read" are opposite findings.
    supabase = ShadowSupabase(
        {"broken": [{"kind": "balances", "symbol": None, "payload": {}}]},
        listed=[{"cycle_id": "broken", "first_seen": f"{DAY_TWO}T22:47:00Z"}],
    )

    result = run(supabase)

    days = {row["trading_day"]: row for row in result["days"]}
    assert days[DAY_TWO]["status"] == shadow.DAY_UNREADABLE
    assert days[DAY_TWO]["unreadable"] == 1
    assert days[DAY_TWO]["cycles"] == 0
    # The day that genuinely held nothing still says so.
    assert days[DAY_ONE]["status"] == shadow.DAY_NO_INPUTS
    assert days[DAY_ONE]["unreadable"] == 0


def test_an_unreadable_cycle_outside_the_window_does_not_mislabel_a_day():
    supabase = ShadowSupabase(
        {"broken": [{"kind": "balances", "symbol": None, "payload": {}}]},
        listed=[{"cycle_id": "broken", "first_seen": "2025-01-02T14:00:00Z"}],
    )

    result = run(supabase)

    assert all(row["status"] == shadow.DAY_NO_INPUTS for row in result["days"])
    assert len(result["unreadable"]) == 1


def test_no_stored_cycles_at_all_is_a_clean_empty_run():
    result = run(ShadowSupabase({}))

    assert result["status"] == shadow.STATUS_OK
    assert result["trades"] == 0
    assert result["cycles"] == 0
    assert all(row["status"] == shadow.DAY_NO_INPUTS for row in result["days"])


# ---------------------------------------------------------------------------
# 3. Nothing is priced that was not observed
# ---------------------------------------------------------------------------


def test_an_entry_fills_at_the_next_cycles_ask_not_its_own():
    # The monitor cycle quotes PFE a dollar higher than the full cycle that
    # proposed it. An order placed at 09:40 could not have filled at 09:40's
    # ask, so 26.13 is the answer and 25.13 is the wrong one.
    cycles = {}
    for cycle_id, now, ask in (
        (FULL_ONE, FULL_ONE, 25.13),
        (MONITOR_ONE, MONITOR_ONE, 26.13),
    ):
        payload = cli_payload(
            now=now,
            cycle_id=cycle_id,
            quotes={"PFE": {"bid": ask - 0.03, "ask": ask, "last": ask - 0.01}},
        )
        cycles[cycle_id] = rows_from(payload, cycle_id)
    supabase = ShadowSupabase(cycles)

    run(supabase, StubBars({}))

    entries = [
        row
        for row in supabase.written()
        if row["symbol"] == "PFE" and row["entry"] is not None
    ]
    assert entries
    assert all(row["entry"] == 26.13 for row in entries)


def test_a_final_cycle_with_nothing_after_it_cannot_fill():
    payload = cli_payload(now=FULL_ONE, cycle_id=FULL_ONE)
    supabase = ShadowSupabase({FULL_ONE: rows_from(payload, FULL_ONE)})

    run(supabase, StubBars({}))

    rows = variant_rows(supabase)
    assert rows
    assert all(row["reason"] == shadow.NO_FILL_QUOTE for row in rows)
    assert all(row["entry"] is None and row["pnl_pct"] is None for row in rows)


def test_a_filled_entry_with_no_bars_is_no_bars_and_not_a_zero():
    supabase = two_days()
    run(supabase, StubBars({}))

    filled = [row for row in supabase.written() if row["entry"] is not None]
    assert filled
    assert all(row["reason"] == shadow.NO_BARS for row in filled)
    assert all(row["exit"] is None and row["pnl_pct"] is None for row in filled)


def test_bars_are_never_fetched_for_an_unfilled_entry():
    payload = cli_payload(now=FULL_ONE, cycle_id=FULL_ONE)
    supabase = ShadowSupabase({FULL_ONE: rows_from(payload, FULL_ONE)})
    bars = StubBars({"PFE": [bar(DAY_TWO, 25.0, 26.0, 24.5, 25.5)]})

    run(supabase, bars)

    # S0: the control arm prices every *signal* off daily bars whether or not
    # a variant's entry filled, so PFE is now asked for on its account. What
    # still holds is that no variant row was priced from those bars.
    assert all(row["bars_source"] is None for row in variant_rows(supabase))
    assert all(row["exit"] is None for row in variant_rows(supabase))


def test_no_market_data_client_is_a_degradation_not_a_failure():
    supabase = two_days()
    result = run(supabase, None)

    assert result["status"] == shadow.STATUS_OK
    assert all(
        # NO_CONTROL: the synthetic universe has one Health Care name, so the
        # control arm has nobody to draw — a reason, and still not a number.
        row["reason"] in (shadow.NO_BARS, shadow.NO_FILL_QUOTE, shadow.NO_CONTROL)
        for row in supabase.written()
    )


# ---------------------------------------------------------------------------
# The exit walk — Phase 2.2's resting stop and target over daily bars
# ---------------------------------------------------------------------------
#
# PFE fills at 25.13 with ATR 0.50 and the DIP play, so:
#   fixed stop   25.13 - 3.0 x 0.50 = 23.63
#   trailing     high  - 3.5 x 0.50
#   target       round(25.13 x 1.06, 2) = 26.64


def walked(series, **intent_kw):
    intent = shadow.Intent(
        **{
            "variant": shadow.BASELINE,
            "symbol": "PFE",
            "play_id": "DIP",
            "quantity": 1.0,
            "entry_day": DAY_ONE,
            "cycle_id": FULL_ONE,
            "cycle_index": 0,
            "entry": 25.13,
            "atr": 0.50,
            **intent_kw,
        }
    )
    bars = [b for b in (shadow._bar(row) for row in series) if b is not None]
    return shadow._walk(intent, bars, cli.load_config())


def test_the_target_exits_at_the_target_price_not_the_bars_high():
    day, price, reason = walked([bar(DAY_TWO, 25.5, 27.0, 25.4, 26.9)])

    assert (day, reason) == (DAY_TWO, shadow.TARGET)
    assert price == pytest.approx(26.64)


def test_the_stop_exits_at_the_stop_price_when_the_bar_trades_through_it():
    day, price, reason = walked([bar(DAY_TWO, 25.0, 25.2, 23.5, 23.7)])

    assert (day, reason) == (DAY_TWO, shadow.STOP)
    assert price == pytest.approx(23.63)


def test_a_gap_through_the_stop_fills_at_the_open_not_the_stop():
    # A `stop_market` triggered below its price is a market order into
    # whatever is there — the open, here, and not the 24.13 nobody traded at.
    day, price, reason = walked([bar(DAY_TWO, 22.0, 22.5, 21.8, 22.1)])

    assert (day, reason) == (DAY_TWO, shadow.STOP)
    assert price == pytest.approx(22.0)


def test_the_stop_is_taken_before_the_target_when_one_bar_touches_both():
    day, price, reason = walked([bar(DAY_TWO, 25.5, 27.0, 23.5, 26.0)])

    assert reason == shadow.STOP
    assert price == pytest.approx(23.63)


def test_the_trailing_stop_ratchets_with_the_high_but_never_on_its_own_bar():
    # Day one runs to 26.5: the trailing floor becomes 26.5 - 1.75 = 24.75,
    # above the 23.63 fixed stop. Its own low of 25.0 is below that floor and
    # must NOT stop it out — the high is added after the day's stop is tested.
    # Day two's 24.5 low is then through the ratcheted 24.75.
    day, price, reason = walked(
        [
            bar(DAY_TWO, 25.5, 26.5, 25.0, 26.4),
            bar("2026-08-05", 26.4, 26.5, 24.5, 25.2),
        ]
    )

    assert (day, reason) == ("2026-08-05", shadow.STOP)
    assert price == pytest.approx(24.75)


def test_the_time_stop_closes_at_the_play_holding_period():
    # DIP holds 8 sessions. Eight flat bars, none near the stop or the target.
    series = [
        bar(f"2026-08-{day:02d}", 25.2, 25.3, 25.1, 25.2) for day in range(4, 12)
    ]
    day, price, reason = walked(series)

    assert reason == shadow.TIME_STOP
    assert day == series[7]["datetime"]
    assert price == pytest.approx(25.2)


def test_a_trade_still_running_at_the_end_of_the_bars_is_open_not_closed():
    day, price, reason = walked([bar(DAY_TWO, 25.2, 25.3, 25.1, 25.2)])

    assert (day, price, reason) == (None, None, shadow.OPEN)


def test_a_missing_atr_leaves_no_stop_rather_than_guessing_one():
    day, price, reason = walked([bar(DAY_TWO, 25.0, 25.2, 20.0, 24.2)], atr=None)

    # 20.00 is far through where a 2xATR stop would have sat; with no ATR
    # there is no stop to be through, and the target still governs.
    assert reason == shadow.OPEN
    assert (day, price) == (None, None)


def test_a_bar_missing_its_low_is_dropped_rather_than_read_as_unhit():
    incomplete = {"datetime": DAY_TWO, "open": "25", "high": "26", "close": "25.5"}
    assert shadow._bar(incomplete) is None


# ---------------------------------------------------------------------------
# The write
# ---------------------------------------------------------------------------


def test_a_dry_run_writes_nothing():
    supabase = two_days()
    result = run(supabase, StubBars({}), write=False)

    assert result["trades"] > 0
    assert result["written"] == 0
    assert "edge_shadow_trades_write" not in [name for name, _ in supabase.calls]


def test_every_written_row_carries_the_migrations_columns():
    supabase = two_days()
    run(supabase, StubBars({}))

    for row in supabase.written():
        assert set(row) == {
            "variant", "symbol", "entry_day", "play_id", "entry_cycle_id",
            "quantity", "entry", "exit_day", "exit", "pnl_pct", "reason",
            "bars_source",
            # The control arm's pairing, and what a later night needs
            # to walk an open trade again.
            "pair_symbol", "atr", "fill_day",
        }
        assert row["reason"]


def test_a_failure_anywhere_returns_failed_and_writes_nothing():
    class Exploding(ShadowSupabase):
        def rpc(self, function, params=None):
            if function == "edge_shadow_cycles":
                raise RuntimeError("PostgREST said no")
            return super().rpc(function, params)

    result = run(Exploding({}))

    assert result["status"] == shadow.STATUS_FAILED
    assert "PostgREST said no" in result["detail"]


def test_a_fill_is_found_in_the_brokers_own_quote_envelope():
    """A runner-posted cycle stores `get_equity_quotes` whole.

    `quotes[symbol]` is absent in that shape, so reading the payload directly
    priced every entry `NO_FILL_QUOTE` -- a ledger of unfilled trades
    indistinguishable from a week with no quotes.
    """
    intent = shadow.Intent(
        variant=shadow.BASELINE,
        symbol="ADI",
        play_id="DIP",
        quantity=1.0,
        entry_day=DAY_ONE,
        cycle_id=FULL_ONE,
        cycle_index=0,
        atr=1.0,
    )
    raw = {
        "results": [
            {"quote": {"symbol": "ADI", "ask_price": "362.45", "bid_price": "361.01"}}
        ]
    }
    cycles = [
        shadow.Cycle(FULL_ONE, DAY_ONE, {}),
        shadow.Cycle(MONITOR_ONE, DAY_ONE, {"quotes": raw}),
    ]

    shadow._fill(intent, cycles)

    assert intent.entry == 362.45
    assert intent.reason == shadow.OPEN
