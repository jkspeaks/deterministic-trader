"""The Manual Check: the real book, judged by the Dip engine's rules.

`tests/fixtures/broker/manual_check/` holds three broker responses recorded from the
live account (••0000): `get_equity_positions` (SPG, SPYM, BNY, USB),
`get_equity_tax_lots USB` and `get_equity_orders state=confirmed` (the four resting
take-profit sells). Everything the market says (quotes, tradability, sectors,
earnings) is built here in the live envelope, because it moves; the book's shape
does not.

* USB   avg 56.51, trades 60.00, target 59.48, bought 10-08 -> SELL (T+1 clear)
* SPYM  avg 91.64, trades 97.00, target 96.46, bought today  -> at target, T+1 holds
* BNY   avg 142.31, trades 134.00 (-5.8%)                    -> AVERAGE DOWN
* SPG   avg 206.10, trades 210.00, below its 216.92 target   -> nothing to do
"""

from __future__ import annotations

import copy
import io
import json
import shutil
from pathlib import Path

import pytest

from src import cli, manual_assemble, manual_check
from tests.dip_recovery_factories import dip1_earnings_raw, dip_recovery_config

RECORDED = Path(__file__).parent / "fixtures" / "broker" / "manual_check"

SEED = {"cycle_id": "9acbd870-600a-4914-8124-38a512052fa1", "now": "2026-10-13T14:30:00Z"}

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
            {"account_number": "<your-margin-account-number>", "type": "margin", "unsettled_funds": "0.0000"},
            {"account_number": "<your-cash-account-number>", "type": "cash", "unsettled_funds": "0.0000"},
        ]
    }
}

PRICES = {"USB": 60.00, "SPYM": 97.00, "BNY": 134.00, "SPG": 210.00}
SECTORS = {"USB": "Financials", "SPYM": "Financials", "BNY": "Financials", "SPG": "Real Estate"}
OTHER_LOTS = {
    "SPYM": [("2026-10-13", "91.640000", "7.000000")],
    "BNY": [("2026-10-08", "142.310000", "1.000000")],
    "SPG": [("2026-09-28", "206.100000", "1.000000")],
}


def write(directory: Path, name: str, value) -> None:
    (directory / name).write_text(json.dumps(value), encoding="utf-8")


def quote_response(prices: dict[str, float], spread: float = 0.04) -> dict:
    return {"data": {"results": [
        {"quote": {
            "symbol": symbol, "last_trade_price": f"{price:.6f}",
            "bid_price": f"{price - spread / 2:.6f}", "ask_price": f"{price + spread / 2:.6f}",
            "adjusted_previous_close": f"{price:.6f}", "state": "active",
        }, "close": {"symbol": symbol, "price": f"{price:.2f}"}}
        for symbol, price in prices.items()]}}


def tradability_response(symbols) -> dict:
    return {"data": {"results": [
        {"symbol": s, "state": "active", "tradeable": True,
         "fractional_tradability": "tradable", "all_day_tradability": "tradable"}
        for s in symbols]}}


def fundamentals_response(symbols) -> dict:
    return {"data": {"results": [
        {"symbol": s, "sector": SECTORS[s], "volume": "1000000", "average_volume": "1000000"}
        for s in symbols]}}


def lots_response(symbol: str, lots) -> dict:
    return {"data": {"symbol": symbol, "tax_lots": [
        {"open_lot_id": f"{symbol}-{i}", "open_tran_type": "buy", "quantity": qty,
         "quantity_available": qty, "cost_per_share": price, "tax_cost_basis": price,
         "open_date": day, "term": "st"}
        for i, (day, price, qty) in enumerate(lots)]}}


def earnings(symbol: str) -> dict:
    raw = dip1_earnings_raw()
    for row in raw["data"]["results"]:
        row["symbol"] = symbol
    return raw


def state(**overrides) -> dict:
    base = {
        "control": {
            "readable": True, "trading_enabled": True, "high_water_mark": 1000.0,
            "cycle_in_progress": SEED["cycle_id"],
        },
        "dip_recovery_config": dip_recovery_config(),
        "loss_closes": [],
        "regime_inputs": None,
        "day_context": {},
    }
    return {**base, **overrides}


@pytest.fixture
def inputs(tmp_path):
    for path in RECORDED.glob("*.json"):
        shutil.copy(path, tmp_path / path.name)
    for symbol, lots in OTHER_LOTS.items():
        write(tmp_path, f"taxlots.{symbol}.json", lots_response(symbol, lots))
    write(tmp_path, "seed.json", SEED)
    write(tmp_path, "state.json", [{"state": state()}])
    write(tmp_path, "portfolio.json", PORTFOLIO)
    write(tmp_path, "accounts.json", ACCOUNTS)
    write(tmp_path, "quotes.json", quote_response(PRICES))
    write(tmp_path, "tradability.json", tradability_response(PRICES))
    write(tmp_path, "fundamentals.json", fundamentals_response(PRICES))
    for symbol in PRICES:
        write(tmp_path, f"earnings.{symbol}.json", earnings(symbol))
    write(tmp_path, "orders.today.json", {"data": {"orders": []}})
    write(tmp_path, "fills.json", {"data": {"orders": []}})
    return tmp_path


def assembled(directory: Path) -> tuple[dict, dict]:
    return manual_assemble.assemble(
        manual_assemble.read_inputs(directory), cli.load_config(), {"holdings": []}
    )


def decided(directory: Path) -> dict:
    cycle, _notes = assembled(directory)
    return cli.manual_check_decide(cycle, cli.load_config())


def held(result: dict) -> dict[str, dict]:
    return {row["symbol"]: row for row in result["holdings"]}


# --- the whole book, end to end -------------------------------------------------


def test_every_holding_is_judged_on_the_recorded_book(inputs):
    result = decided(inputs)

    assert result["command"] == "manual-check-decide"
    assert result["session"] == "regular_hours"
    decisions = {s: row["decision"] for s, row in held(result).items()}
    assert decisions == {
        "USB": manual_check.SELL,
        "SPYM": manual_check.TARGET_T1_LOCKED,
        "BNY": manual_check.AVERAGE_DOWN,
        "SPG": manual_check.HOLD,
    }, result["blocked"]


def test_the_sale_cancels_the_resting_target_first_and_rests_as_gtc(inputs):
    result = decided(inputs)

    [sell] = [o for o in result["orders"] if o["side"] == "sell"]
    assert sell["symbol"] == "USB" and sell["quantity"] == 2.0
    assert sell["limit_price"] == 60.0 and sell["type"] == "limit"
    assert sell["time_in_force"] == "gtc" and sell["market_hours"] == "regular_hours"
    assert sell["cancel_order_ids"] == ["87dfde1d-75a3-4d1b-9415-9cf29945ebb6"]
    assert result["cancels"] == [
        {"symbol": "USB", "order_id": "87dfde1d-75a3-4d1b-9415-9cf29945ebb6",
         "reason": "frees USB's shares for the take-profit sale"}
    ]
    assert result["orders"][0]["side"] == "sell", "sells are placed before buys"


def test_the_average_down_is_one_fifth_of_the_cash_at_most_and_a_day_order(inputs):
    result = decided(inputs)

    [buy] = [o for o in result["orders"] if o["side"] == "buy"]
    assert buy["symbol"] == "BNY" and buy["cancel_order_ids"] == []
    assert buy["time_in_force"] == "gfd"
    cycle, _ = assembled(inputs)
    deployable = cycle["account"]["settled_cash"]
    assert buy["quantity"] * buy["limit_price"] <= deployable / 5 + 1e-6


def test_nothing_is_persisted_for_the_scheduled_routines_to_trip_over(inputs):
    result = decided(inputs)

    for key in ("position_updates", "symbol_lock_claims", "symbol_lock_releases",
                "control_updates", "evaluations"):
        assert key not in result, f"{key} would move state a manual run must not touch"


# --- T+1 --------------------------------------------------------------------------


def test_t1_blocks_a_sale_on_the_day_of_the_buy_and_says_why(inputs):
    result = decided(inputs)

    spym = held(result)["SPYM"]
    assert spym["t1_clear"] is False and spym["newest_buy_day"] == "2026-10-13"
    assert "T+1" in spym["detail"]
    assert not [o for o in result["orders"] if o["symbol"] == "SPYM"]


def test_t1_clears_on_the_next_session_but_not_across_a_closed_one(inputs):
    # USB was bought Thursday 10-08. Friday is the next session: clear.
    cycle, _ = assembled(inputs)
    friday = copy.deepcopy(cycle)
    friday["now"] = "2026-10-09T14:30:00Z"
    assert held(cli.manual_check_decide(friday, cli.load_config()))["USB"]["t1_clear"] is True

    # Bought on the Friday itself, looked at on that Friday: not clear.
    same_day = copy.deepcopy(cycle)
    same_day["now"] = "2026-10-08T14:30:00Z"
    assert held(cli.manual_check_decide(same_day, cli.load_config()))["USB"]["t1_clear"] is False


# --- profit first -----------------------------------------------------------------


def test_a_position_underwater_is_never_sold(inputs):
    write(inputs, "quotes.json", quote_response({**PRICES, "USB": 50.00}))
    result = decided(inputs)

    assert not [o for o in result["orders"] if o["side"] == "sell"]
    assert held(result)["USB"]["decision"] != manual_check.SELL
    assert held(result)["USB"]["unrealized_pct"] < 0


def test_no_sell_leaves_this_module_at_or_below_cost():
    from src.dip_recovery.core import Order

    row = {"weighted_avg_cost": 56.51}
    assert manual_check._never_below_cost(Order("USB", "sell", 2, 56.52, ""), row)
    assert not manual_check._never_below_cost(Order("USB", "sell", 2, 56.51, ""), row)
    assert not manual_check._never_below_cost(Order("USB", "sell", 2, 50.0, ""), row)
    assert not manual_check._never_below_cost(Order("USB", "sell", 2, 60.0, ""), {})


def test_no_stop_order_is_ever_emitted(inputs):
    result = decided(inputs)

    assert {o["type"] for o in result["orders"]} == {"limit"}
    assert all(o.get("stop_price") is None for o in result["orders"])


# --- the resting take-profit --------------------------------------------------------


def test_an_operators_resting_sell_is_never_cancelled_and_the_sale_waits(inputs):
    orders = json.loads((inputs / "orders.confirmed.json").read_text(encoding="utf-8"))
    for order in orders["data"]["orders"]:
        if order["symbol"] == "USB":
            order["placed_agent"] = "user"
    write(inputs, "orders.confirmed.json", orders)

    result = decided(inputs)

    assert result["cancels"] == []
    assert not [o for o in result["orders"] if o["side"] == "sell"]
    usb = held(result)["USB"]
    assert usb["decision"] == manual_check.BLOCKED
    assert "PROTECTION_CONTENDS" in usb["detail"]


# --- fail closed --------------------------------------------------------------------


def test_a_holding_with_no_tax_lots_has_no_t1_clock_and_is_left_alone(inputs):
    (inputs / "taxlots.BNY.json").unlink()
    cycle, notes = assembled(inputs)

    assert notes["excluded_no_tax_lots"] == ["BNY"]
    assert "BNY" not in cycle["manual_rows"]
    assert "BNY" not in held(cli.manual_check_decide(cycle, cli.load_config()))


def test_a_missing_quote_leaves_that_symbol_unpriced_not_guessed(inputs):
    write(inputs, "quotes.json", quote_response({k: v for k, v in PRICES.items() if k != "USB"}))
    result = decided(inputs)

    assert held(result)["USB"]["decision"] == manual_check.NO_PRICE
    assert not [o for o in result["orders"] if o["symbol"] == "USB"]


def test_a_missing_earnings_date_blocks_the_buy_but_not_a_sale(inputs):
    (inputs / "earnings.BNY.json").unlink()
    result = decided(inputs)

    assert held(result)["USB"]["decision"] == manual_check.SELL
    assert held(result)["BNY"]["decision"] == manual_check.BLOCKED
    assert "EARNINGS_DATE_UNAVAILABLE" in held(result)["BNY"]["detail"]


# --- the clock ----------------------------------------------------------------------


def test_a_saturday_is_read_as_an_all_day_session_and_orders_are_placed(inputs):
    write(inputs, "seed.json", {**SEED, "now": "2026-10-10T15:48:00Z"})
    result = decided(inputs)

    assert result["weekend_run"] is True
    assert result["session"] == "all_day_hours"
    assert result["cycle"]["session"] == "closed", "cadence itself is untouched"
    assert {o["symbol"]: o["side"] for o in result["orders"]} == {"USB": "sell", "BNY": "buy"}
    assert {o["market_hours"] for o in result["orders"]} == {"all_day_hours"}
    assert "SESSION_CLOSED" not in json.dumps(result["blocked"])


def test_the_weekend_runs_from_friday_evening_to_sunday_before_the_open(inputs):
    for now in ("2026-10-10T01:00:00Z",   # Friday 21:00 ET
                "2026-10-11T23:59:00Z"):  # Sunday 19:59 ET
        write(inputs, "seed.json", {**SEED, "now": now})
        result = decided(inputs)
        assert result["weekend_run"] is True, now
        assert [o["symbol"] for o in result["orders"] if o["side"] == "sell"] == ["USB"], now


def test_the_weekend_keeps_the_overnight_spread_gate_and_t1(inputs):
    write(inputs, "seed.json", {**SEED, "now": "2026-10-10T15:48:00Z"})
    write(inputs, "quotes.json", quote_response(PRICES, spread=3.0))  # ~5% on USB
    result = decided(inputs)

    usb = held(result)["USB"]
    assert usb["decision"] == manual_check.BLOCKED and "OVERNIGHT_SPREAD_GATE" in usb["detail"]
    assert result["cancels"] == []

    # Bought Friday, looked at on the Saturday: the next session has not opened.
    write(inputs, "quotes.json", quote_response(PRICES))
    write(inputs, "taxlots.USB.json", lots_response("USB", [("2026-10-09", "56.510000", "2.000000")]))
    friday_buy = decided(inputs)
    assert held(friday_buy)["USB"]["decision"] == manual_check.TARGET_T1_LOCKED
    assert not [o for o in friday_buy["orders"] if o["symbol"] == "USB"]


def test_a_market_holiday_stays_closed_and_places_nothing(inputs):
    # Thanksgiving 2026-11-26 is a weekday, not a weekend: nothing observed
    # about it, so SESSION_CLOSED still applies.
    write(inputs, "seed.json", {**SEED, "now": "2026-11-26T15:00:00Z"})
    result = decided(inputs)

    assert result["weekend_run"] is False and result["session"] == "closed"
    assert result["orders"] == [] and result["cancels"] == []
    assert {d["code"] for b in result["blocked"] for d in b["denials"]} == {"SESSION_CLOSED"}
    assert held(result)["USB"]["decision"] == manual_check.BLOCKED


def test_sunday_night_is_an_open_session_and_gets_the_overnight_spread_gate(inputs):
    # Sunday 21:30 ET belongs to Monday's trading day and is all-day hours.
    write(inputs, "seed.json", {**SEED, "now": "2026-10-12T01:30:00Z"})
    write(inputs, "quotes.json", quote_response(PRICES, spread=3.0))  # ~5% on USB
    result = decided(inputs)

    assert result["session"] == "all_day_hours" and result["weekend_run"] is False
    assert result["cycle"]["trading_day"] == "2026-10-12"
    usb = held(result)["USB"]
    assert usb["decision"] == manual_check.BLOCKED and "OVERNIGHT_SPREAD_GATE" in usb["detail"]
    assert result["cancels"] == [], "a refused sale must not cancel the take-profit"


def test_a_tight_overnight_spread_sells_with_the_overnight_session(inputs):
    write(inputs, "seed.json", {**SEED, "now": "2026-10-12T01:30:00Z"})
    result = decided(inputs)

    [sell] = [o for o in result["orders"] if o["side"] == "sell"]
    assert sell["market_hours"] == "all_day_hours" and sell["symbol"] == "USB"


# --- the other gates stay on --------------------------------------------------------


def test_the_kill_switch_stops_everything(inputs):
    control = state()["control"] | {"trading_enabled": False}
    write(inputs, "state.json", [{"state": state(control=control)}])
    result = decided(inputs)

    assert result["orders"] == []
    assert {d["code"] for b in result["blocked"] for d in b["denials"]} == {"TRADING_DISABLED"}


def test_a_recent_loss_close_blocks_the_average_down(inputs):
    losses = [{"symbol": "BNY", "close_date": "2026-10-09", "loss_amount": 5.0}]
    write(inputs, "state.json", [{"state": state(loss_closes=losses)}])
    result = decided(inputs)

    assert held(result)["BNY"]["decision"] == manual_check.BLOCKED
    assert "WASH_SALE_COOLDOWN" in held(result)["BNY"]["detail"]


# --- the command ------------------------------------------------------------------


def test_the_assembler_writes_the_document_to_stdout_and_notes_to_stderr(inputs):
    out, err = io.StringIO(), io.StringIO()
    code = manual_assemble.main([str(inputs)], out, err)

    assert code == 0
    cycle = json.loads(out.getvalue())
    assert sorted(cycle["manual_rows"]) == ["BNY", "SPG", "SPYM", "USB"]
    notes = json.loads(err.getvalue())
    assert sorted(notes["symbols"]) == ["BNY", "SPG", "SPYM", "USB"]
    assert notes["missing"] == {} and notes["excluded_no_tax_lots"] == []


def test_the_assembler_refuses_an_unknown_file_kind(inputs):
    write(inputs, "watchlist.json", {})
    out, err = io.StringIO(), io.StringIO()

    assert manual_assemble.main([str(inputs)], out, err) == 1
    assert json.loads(out.getvalue())["error"]["code"] == "UNKNOWN_KIND"


def test_the_command_is_registered_and_runs_from_stdin(inputs):
    cycle, _ = assembled(inputs)
    out = io.StringIO()

    assert "manual-check-decide" in cli.COMMANDS
    assert cli.main(["manual-check-decide"], io.StringIO(json.dumps(cycle)), out) == 0
    assert json.loads(out.getvalue())["command"] == "manual-check-decide"
