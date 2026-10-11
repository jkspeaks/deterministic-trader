"""dip_assemble.py — the Dip Monitor's tool responses into `cycle.json`.

Every broker file under `tests/fixtures/broker/dip_monitor/` is a response as
the tool returned it. These tests start
from those files and assert on what `cli.dip_recovery_decide` then reads,
because a fixture written in the shape the code already reads cannot catch a
rename (see that directory's README).
"""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import pytest

from src import cli, dip_assemble
from tests.dip_recovery_factories import dip1_earnings_raw, dip_recovery_config

RECORDED = Path(__file__).parent / "fixtures" / "broker" / "dip_monitor"

SEED = {"cycle_id": "2553f1c0-28e0-4725-8b6d-3bdd5e48517e", "now": "2026-10-05T16:48:10Z"}

# `get_portfolio` and `get_accounts` in the live envelope, figures from that
# tick.
PORTFOLIO = {
    "data": {
        "total_value": "1393.02",
        "equity_value": "201.4",
        "cash": "1191.62",
        "pending_deposits": "0",
        "buying_power": {"buying_power": "1191.6200", "unleveraged_buying_power": "1191.6200"},
    }
}
ACCOUNTS = {
    "data": {
        "accounts": [
            {"account_number": "<your-margin-account-number>", "type": "margin", "unsettled_funds": "2632.2900"},
            {"account_number": "<your-cash-account-number>", "type": "cash", "unsettled_funds": "0.0000"},
        ]
    }
}


def earnings(symbol: str) -> dict:
    raw = dip1_earnings_raw()
    for row in raw["data"]["results"]:
        row["symbol"] = symbol
    return raw


def state(**overrides) -> dict:
    base = {
        "control": {
            "readable": True,
            "trading_enabled": True,
            "cycle_in_progress": SEED["cycle_id"],
            "dip_recovery_watchlist": {"trading_day": "2026-10-05", "symbols": ["BMY", "JNJ"]},
            "dip_recovery_new_positions": {"date": "2026-10-05", "count": 0},
        },
        "dip_recovery_config": dip_recovery_config(),
        "symbol_lock": {"SPG": "core"},
        "dip_recovery_positions": {},
        "loss_closes": [{"symbol": "MS", "close_date": "2026-09-04", "loss_amount": 0.93}],
        "regime_inputs": None,
        "day_context": {"BMY": {"earnings": earnings("BMY")}},
        "last_decide_now": "2026-10-05T15:47:59Z",
    }
    return {**base, **overrides}


def write(directory: Path, name: str, value) -> None:
    (directory / name).write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def inputs(tmp_path):
    """A tick's directory: the recorded responses plus the four the session
    builds from Supabase and the account tools."""
    for path in RECORDED.glob("*.json"):
        shutil.copy(path, tmp_path / path.name)

    write(tmp_path, "seed.json", SEED)
    write(tmp_path, "state.json", [{"state": state()}])
    write(tmp_path, "portfolio.json", PORTFOLIO)
    write(tmp_path, "accounts.json", ACCOUNTS)
    write(tmp_path, "earnings.JNJ.json", earnings("JNJ"))
    return tmp_path


def assembled(directory: Path) -> tuple[dict, dict]:
    return dip_assemble.assemble(
        dip_assemble.read_inputs(directory), cli.load_config(), {"holdings": []}
    )


def run(directory: Path) -> tuple[int, dict, str]:
    out, err = io.StringIO(), io.StringIO()
    code = dip_assemble.main([str(directory)], out, err)
    return code, json.loads(out.getvalue()), err.getvalue()


# --- the tick that aborted, end to end -----------------------------------------


def test_the_recorded_tick_reaches_a_decision_on_the_dip_itself(inputs):
    """What an earlier ticks decided by hand: BMY over the RSI ceiling, JNJ
    short of the volume floor. Neither is a missing-input refusal, which is
    what every one of guard's fail-closed categories would say had a key not
    arrived in the shape it reads."""
    cycle, _notes = assembled(inputs)
    result = cli.dip_recovery_decide(cycle, cli.load_config())

    reasons = {row["symbol"]: row["message"] for row in result["skipped"]}
    assert reasons["BMY"] == "BMY's RSI (37.9) is above the 35 ceiling"
    assert "relative volume" not in reasons["BMY"]
    assert set(reasons) == {"BMY", "JNJ"}
    assert "missing" not in reasons["JNJ"] and "no cached RSI" not in reasons["JNJ"]
    assert result["blocked"] == [] and result["orders"] == []
    assert result["cycle_id"] == SEED["cycle_id"]


def test_the_command_writes_the_document_to_stdout_and_notes_to_stderr(inputs):
    code, cycle, notes = run(inputs)

    assert code == 0
    assert cycle["now"] == SEED["now"]
    assert "holdings" in cycle["snapshot"], "data/universe.json from the checkout"
    assert json.loads(notes)["symbols"] == ["BMY", "JNJ"]
    assert json.loads(notes)["missing"] == {}


# --- each key, in the shape its reader reads -----------------------------------


def test_the_account_is_shaped_from_the_two_raw_responses(inputs):
    cycle, notes = assembled(inputs)

    assert cycle["account"]["equity_total"] == "1393.02"
    assert cycle["account"]["buying_power"] == "1191.6200"
    assert cycle["account"]["settled_cash"] == pytest.approx(1191.62)
    assert notes["account_problems"] == []


def test_signals_are_named_by_indicator_and_period(inputs):
    cycle, _ = assembled(inputs)

    assert cycle["signals"]["BMY"] == {
        "rsi": 37.853067073367846, "sma_20": 63.1385, "sma_200": 59.396199999999936,
    }
    assert set(cycle["signals"]["JNJ"]) == {"rsi", "sma_20", "sma_200"}


def test_signals_are_read_from_the_brokers_results_list(inputs):
    # The live shape: every answer, for one symbol or several, arrives as
    # `data.results`, one row per symbol.
    def call(kind, params, values):
        return {"data": {"results": [
            {"symbol": symbol, "interval": "day", "bounds": "regular", "indicators": [
                {"type": kind, "params": params,
                 "series": [{"begins_at": "2026-10-06T00:00:00Z", "value": value}]}]}
            for symbol, value in values.items()]}, "guide": "Convert begins_at (UTC)."}

    write(inputs, "indicators.json", [
        call("rsi", {"period": 14}, {"BMY": 39.17274633981381, "JNJ": 30.12158545788718}),
        call("sma", {"period": 20}, {"BMY": 90.50050000000002, "JNJ": 122.27949999999996}),
        call("sma", {"period": 200}, {"BMY": 73.09269999999995, "JNJ": 107.01650000000019}),
    ])
    cycle, notes = assembled(inputs)

    assert cycle["signals"]["BMY"] == {
        "rsi": 39.17274633981381, "sma_20": 90.50050000000002, "sma_200": 73.09269999999995,
    }
    assert cycle["signals"]["JNJ"]["sma_200"] == 107.01650000000019
    assert "signals" not in notes["missing"]


def test_a_null_indicator_reading_is_left_out_not_zeroed(inputs):
    write(inputs, "indicators.json", [
        {"data": {"symbol": "BMY", "indicators": [
            {"type": "rsi", "params": {"period": 14},
             "series": [{"begins_at": "2026-10-02T00:00:00Z", "value": None}]}]}},
    ])
    cycle, notes = assembled(inputs)

    assert "BMY" not in cycle["signals"]
    assert notes["missing"]["signals"] == ["BMY", "JNJ"]


def test_bars_are_high_low_close_oldest_first_with_the_brokers_own_figures(inputs):
    cycle, _ = assembled(inputs)

    bars = cycle["dip_recovery_bars"]["BMY"]
    assert bars[0] == {"high": "63.890000", "low": "61.950000", "close": "62.860000"}
    assert bars[-1] == {"high": "61.240000", "low": "60.240000", "close": "61.150000"}
    assert len(bars) == 4


def test_a_gap_filled_bar_is_dropped(inputs):
    raw = json.loads((RECORDED / "historicals.json").read_text(encoding="utf-8"))
    raw["data"]["results"][0]["bars"][1]["interpolated"] = True
    write(inputs, "historicals.json", raw)
    cycle, _ = assembled(inputs)

    assert len(cycle["dip_recovery_bars"]["BMY"]) == 3
    assert len(cycle["dip_recovery_bars"]["JNJ"]) == 4


def test_quotes_fundamentals_and_tradability_are_keyed_by_symbol(inputs):
    cycle, _ = assembled(inputs)

    assert cycle["quotes"]["BMY"]["bid"] == "58.600000"
    assert cycle["quotes"]["BMY"]["last"] == "58.810000"
    assert cycle["fundamentals"]["JNJ"]["average_volume"] == "6560608.680190"
    assert cycle["tradability"]["BMY"]["tradeable"] is True
    assert cycle["sectors"] == {"BMY": "Health Technology", "JNJ": "Health Technology"}


def test_earnings_come_from_the_days_cache_and_a_fetched_miss_wins(inputs):
    cycle, _ = assembled(inputs)
    assert set(cycle["earnings"]) == {"BMY", "JNJ"}

    fresh = earnings("BMY")
    fresh["data"]["results"][1]["report"]["date"] = "2026-10-22"
    write(inputs, "earnings.BMY.json", fresh)
    cycle, _ = assembled(inputs)
    assert cycle["earnings"]["BMY"]["data"]["results"][1]["report"]["date"] == "2026-10-22"


def test_open_orders_are_the_working_ones_under_the_cores_order_id(inputs):
    cycle, _ = assembled(inputs)

    assert [(o["symbol"], o["order_id"]) for o in cycle["open_orders"]] == [
        ("SPG", "ad447800-9b7f-4abc-8536-ec9f84d4f845")
    ]


def test_mc_check_is_never_written(inputs):
    cycle, _ = assembled(inputs)
    assert "mc_check" not in cycle


def test_a_null_regime_is_an_absent_key_and_a_real_one_is_copied(inputs):
    cycle, _ = assembled(inputs)
    assert "regime_inputs" not in cycle

    write(inputs, "state.json", state(regime_inputs={"spy": {"last": 769.64}}))
    cycle, _ = assembled(inputs)
    assert cycle["regime_inputs"] == {"spy": {"last": 769.64}}


# --- the watchlist --------------------------------------------------------------


def test_yesterdays_watchlist_is_treated_as_empty(inputs):
    stale = state()
    stale["control"]["dip_recovery_watchlist"]["trading_day"] = "2026-10-02"
    write(inputs, "state.json", stale)
    cycle, notes = assembled(inputs)

    assert cycle["dip_recovery_watchlist"] == []
    assert notes["symbols"] == []


# --- fills ----------------------------------------------------------------------


def held(symbol: str, **overrides) -> dict:
    return {symbol: {"symbol": symbol, "state": "HOLDING", "fills": [], **overrides}}


def test_cores_fills_are_not_this_sides(inputs):
    """RTX and INVH were core's sells. Neither is locked to this side and
    neither has a position row."""
    cycle, _ = assembled(inputs)
    assert cycle["dip_recovery_fills"] == []


def test_a_fill_on_this_sides_symbol_after_the_last_decision_is_shaped(inputs):
    write(inputs, "state.json", state(
        symbol_lock={"INVH": "dip_recovery"},
        dip_recovery_positions=held("INVH"),
        last_decide_now="2026-10-02T13:00:00Z",
    ))
    cycle, _ = assembled(inputs)

    assert cycle["dip_recovery_fills"] == [{
        "symbol": "INVH", "side": "sell", "quantity": "7.000000", "price": "26.490000",
        "trading_day": "2026-10-02", "filled_at": "2026-10-02T13:49:38.06Z",
    }]


def test_a_fill_the_last_decision_already_saw_is_not_repeated(inputs):
    write(inputs, "state.json", state(
        symbol_lock={"INVH": "dip_recovery"},
        dip_recovery_positions=held("INVH"),
        last_decide_now="2026-10-02T15:47:00Z",
    ))
    cycle, _ = assembled(inputs)
    assert cycle["dip_recovery_fills"] == []


def test_a_pending_entry_is_this_sides_before_it_has_a_lock(inputs):
    raw = json.loads((RECORDED / "fills.json").read_text(encoding="utf-8"))
    raw["data"]["orders"][1]["side"] = "buy"
    write(inputs, "fills.json", raw)
    write(inputs, "state.json", state(
        dip_recovery_positions={"INVH": {"symbol": "INVH", "state": "ENTRY_PENDING", "fills": []}},
        last_decide_now=None,
    ))
    cycle, _ = assembled(inputs)
    assert [f["side"] for f in cycle["dip_recovery_fills"]] == ["buy"]


def test_a_buy_already_in_the_position_rows_fills_is_not_applied_twice(inputs):
    raw = json.loads((RECORDED / "fills.json").read_text(encoding="utf-8"))
    raw["data"]["orders"][1]["side"] = "buy"
    write(inputs, "fills.json", raw)
    write(inputs, "state.json", state(
        symbol_lock={"INVH": "dip_recovery"},
        dip_recovery_positions=held("INVH", fills=[{"filled_at": "2026-10-02T13:49:38.06Z"}]),
        last_decide_now="2026-10-02T13:00:00Z",
    ))
    cycle, _ = assembled(inputs)
    assert cycle["dip_recovery_fills"] == []


def test_an_evening_fill_is_dated_by_the_eastern_day():
    assert dip_assemble._moment("2026-10-06T01:30:00.5Z").astimezone(
        dip_assemble.cadence.ET
    ).date().isoformat() == "2026-10-05"
    assert dip_assemble._moment("2026-10-05T19:59:59.781155436Z") is not None
    assert dip_assemble._moment("not a time") is None


# --- refusals -------------------------------------------------------------------


def test_a_missing_required_file_is_refused_by_name(inputs):
    (inputs / "portfolio.json").unlink()
    code, out, _ = run(inputs)

    assert code == dip_assemble.EXIT_BAD_INPUT
    assert out["error"]["code"] == dip_assemble.INPUTS_INCOMPLETE
    assert "portfolio.json" in out["error"]["detail"]


def test_a_file_of_an_unknown_kind_is_refused_not_ignored(inputs):
    write(inputs, "mc_check.json", {})
    code, out, _ = run(inputs)

    assert code == dip_assemble.EXIT_BAD_INPUT
    assert out["error"]["code"] == dip_assemble.UNKNOWN_KIND


def test_a_truncated_file_is_refused_with_the_parsers_own_sentence(inputs):
    (inputs / "quotes.json").write_text('{"data": {"results": [', encoding="utf-8")
    code, out, _ = run(inputs)

    assert code == dip_assemble.EXIT_BAD_INPUT
    assert out["error"]["code"] == cli.BAD_INPUT
    assert out["error"]["detail"].startswith("quotes.json:")


def test_an_optional_fetch_that_never_happened_is_named_in_the_notes(inputs):
    (inputs / "tradability.json").unlink()
    cycle, notes = assembled(inputs)

    assert cycle["tradability"] == {}
    assert notes["missing"]["tradability"] == ["BMY", "JNJ"]


# --- the prompt and the command agree -------------------------------------------

PROMPT = Path(__file__).resolve().parents[1] / "docs" / "routine" / "dip-monitor-prompt.md"


def prompt_text() -> str:
    return PROMPT.read_text(encoding="utf-8").replace("\r\n", "\n")


def test_every_file_the_prompt_names_is_a_kind_the_command_reads():
    """A file under a name the command does not know is refused, so a renamed
    file in the prompt would abort every tick. This is the pin."""
    import re

    named = set(re.findall(r"inputs/([a-z_]+)\.", prompt_text())) - {"<name>"}
    assert named == set(dip_assemble.KINDS)


def test_the_prompt_runs_the_checked_in_command_and_forbids_a_script():
    text = prompt_text()

    assert "uv run python -m src.dip_assemble inputs > cycle.json" in text
    assert "Never write a script" in text
    assert "select edge_cycle_release('<cycle_id>', '{\"stage\":" in text


def test_the_prompt_never_reads_the_whole_day_context():
    """The 106 KB read that overflowed the inline limit and cost the tick ten
    minutes before it reached a broker call."""
    text = prompt_text()

    assert "select edge_day_context_recent(" not in text
    assert "jsonb_each(edge_day_context_recent('<trading_day>'))" in text
