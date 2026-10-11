"""The core sleeve through the hosted runner, on the shipped config.

`tests/test_core_sleeve.py` pins the arithmetic and the rules. This pins the
path a live cycle takes: `resolve` must ask the session to fetch the ETF, and
`decide` must order it from what the session then posts. The numbers are the
account's (equity 1,393.55) and the broker's answers for SPYM that evening: a
two-sided quote, and an empty earnings list, which is what
`get_earnings_results` returns for an ETF.
"""

from __future__ import annotations

from services.core.core import run as core
from services.core.tests.conftest import FakeSupabase, rows_from
from src import cli, core_sleeve
from tests.factories import cli_payload

ETF = "SPYM"
TRADABLE = {"tradeable": True, "fractional_tradability": "tradable", "all_day_tradability": "tradable"}
ACCOUNT = {"equity_total": 1393.55, "cash": 1190.0, "buying_power": 1190.0, "settled_cash": 1190.0}
CONTROL = {"high_water_mark": 1393.55}


def fetched():
    base = cli_payload()
    return cli_payload(
        account=ACCOUNT,
        control=CONTROL,
        quotes={**base["quotes"], ETF: {"bid": 91.23, "ask": 91.25, "last": 91.22}},
        tradability={**base["tradability"], ETF: TRADABLE},
        earnings={**base["earnings"], ETF: []},
    )


def decide_row(payload):
    supabase = FakeSupabase(tables={"edge_cycle_input": rows_from(payload, payload["cycle_id"])})
    resolved = core.run(payload["cycle_id"], core.RESOLVE, supabase)
    decided = core.run(payload["cycle_id"], core.DECIDE, supabase)
    row = next(a for a in supabase.args("edge_runlog_write") if a["p_command"] == "decide")
    return resolved, decided, row["p_stdout"]


def test_the_shipped_config_is_the_one_under_test():
    config = cli.load_config()["config"]
    assert config[core_sleeve.ENABLED] is True and config[core_sleeve.SYMBOL] == ETF


def test_resolve_asks_the_session_to_fetch_the_etf():
    payload = cli_payload(account=ACCOUNT, control=CONTROL)
    supabase = FakeSupabase(tables={"edge_cycle_input": rows_from(payload, payload["cycle_id"])})

    resolved = core.run(payload["cycle_id"], core.RESOLVE, supabase)

    assert resolved["status"] == core.STATUS_RESOLVED
    assert ETF in resolved["symbols"]


def test_decide_buys_the_sleeve_after_the_plays_and_guard_approves_it():
    _, decided, row = decide_row(fetched())

    assert decided["status"] == core.STATUS_DECIDED
    orders = [(o["symbol"], o["side"], o["play_id"]) for o in row["plan"]["orders"]]
    assert orders == [("PFE", "buy", "DIP"), (ETF, "buy", "CORE")]

    [buy] = [o for o in row["plan"]["orders"] if o["symbol"] == ETF]
    assert buy["quantity"] == 7 and buy["limit_price"] == 91.25
    assert buy["type"] == "limit" and buy.get("stop_price") is None

    record = row["core_sleeve"]
    assert record["placed"] is True
    assert record["cap_value"] == 696.77 and record["kept_back"] == 226.95
    assert record["notional"] <= record["cap_value"]
    assert [d for d in row["guard"]["denied"] if d["order"]["symbol"] == ETF] == []


def test_no_order_in_the_plan_is_a_stop():
    _, _, row = decide_row(fetched())

    for order in [*row["plan"]["orders"], *row["plan"]["protection"]]:
        assert order["type"] == "limit" and order.get("stop_price") is None
    for resting in row["plan"]["protection"]:
        assert resting["side"] == "sell" and resting["protective_target"] is True
