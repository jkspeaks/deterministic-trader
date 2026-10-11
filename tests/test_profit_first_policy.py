"""1. No resting stop-loss order, ever. A resting stop fires on any intraday wick
   and has sold positions at a loss that would have recovered.
2. Every whole-share position rests exactly one order: a limit SELL priced
   ABOVE its buy price — the play's take-profit.
3. The stop is a per-cycle check (`plays.exits` ATR_STOP). Only when a
   position is actually through it does the cycle sell, with an ordinary
   limit order, after cancelling the resting target that holds its shares.

These tests pin the *shipped* configuration, not a fixture's, so a config
change that brings a resting stop back fails here before it reaches the
account.
"""

from __future__ import annotations

import json
from pathlib import Path

from src import cli
from src.plays import protection
from tests.factories import cli_payload, plays_context, position

SHIPPED = json.loads(
    (Path(__file__).resolve().parent.parent / "config" / "config.json").read_text(encoding="utf-8")
)

RESTING_TARGET = {
    "order_id": "t1", "symbol": "F", "side": "sell", "type": "limit", "price": 15.90,
    "quantity": 1.0, "state": "confirmed", "time_in_force": "gtc", "placed_agent": "agentic",
}

# F through its stop: a 0.01 ATR against a 10.00 print on a 15.00 entry.
STOPPED_OUT = {
    "signals": {"F": {"rsi": 45.0, "sma_20": 15.10, "sma_50": 15.20,
                      "sma_200": 13.50, "atr": 0.01}},
    "quotes": {"F": {"bid": 10.00, "ask": 10.02, "last": 10.00}},
}
NOTHING_RESTS = {"protection_plan": {"place": [], "replace": [], "keep": [], "notes": []}}


def test_the_shipped_config_rests_the_profit_target_and_never_the_stop():
    assert SHIPPED["protection_rest_kind"] == "target"


def test_every_resting_order_is_a_limit_sell_above_the_buy_price():
    ctx = plays_context(config=SHIPPED)
    ctx["positions"] = [position()]

    orders = protection.desired(ctx).orders

    assert orders, "a whole-share position with a play must rest its target"
    for order in orders:
        assert order.kind == protection.TARGET
        assert order.order_type == protection.LIMIT
        assert order.stop_price is None
        assert order.limit_price > float(position()["entry_price"])


def test_the_core_sleeve_rests_a_limit_sell_above_its_buy_and_has_no_stop_at_all():
    """The sleeve (`src/core_sleeve.py`) is one more position under the same rule,
    and goes one step further than the plays: it has no per-cycle stop check
    either. Switched on, bought, then down 40%: the cycle proposes no stop, places
    no sell, and wants one resting order, the target.
    """
    from src import core_sleeve

    base = cli.load_config()
    loaded = {**base, "config": {**base["config"], core_sleeve.ENABLED: True}}
    assert loaded["config"][core_sleeve.TARGET_PCT] > 0

    held = position(symbol="SPYM", play_id="CORE", entry_price=90.0, quantity=7.0,
                    market_value=378.0, high_since_entry=90.0, sessions_held=300,
                    adopted=False, adopted_on=None)
    payload = cli_payload(
        account={"equity_total": 1400.0, "cash": 500.0, "buying_power": 500.0, "settled_cash": 500.0},
        control={"high_water_mark": 1400.0},
        triggers=[{"symbol": "SPYM", "play_id": "CORE"}],
        positions=[{"symbol": "SPYM", "quantity": 7.0, "market_value": 378.0}],
        prior_positions=[{"symbol": "SPYM", "play_id": "CORE", "quantity": 7.0}],
        tax_lots={"SPYM": [{"quantity": 7.0, "tax_cost_basis": 630.0, "open_date": "2025-06-02"}]},
        quotes={"SPYM": {"bid": 53.98, "ask": 54.00, "last": 54.00}},
        signals={"SPYM": {"rsi": 20.0, "sma_20": 70.0, "sma_50": 80.0, "sma_200": 85.0, "atr": 1.0}},
        tradability={"SPYM": {"tradeable": True, "fractional_tradability": "tradable",
                              "all_day_tradability": "tradable"}},
        reconcile={"findings": [], "cancels": [], "positions": [held], **NOTHING_RESTS},
    )
    payload["reconcile"] = cli.reconcile(payload, loaded)
    result = cli.decide(payload, loaded)

    everything = result["plan"]["orders"] + result["plan"]["protection"]
    assert all(o.get("type") != "stop_market" and not o.get("stop_price") for o in everything)
    assert not [o for o in result["plan"]["orders"] if o["symbol"] == "SPYM" and o["side"] == "sell"]
    assert [e for e in result["exits"]["exits"] if e["symbol"] == "SPYM"] == []
    resting = [o for o in result["plan"]["protection"] if o["symbol"] == "SPYM"]
    assert [(o["side"], o["type"]) for o in resting] == [("sell", "limit")]
    assert resting[0]["limit_price"] > 90.0


def test_the_baseline_cycle_proposes_no_stop_order_of_any_kind():
    # `reconcile` recomputed as production does (both paths run it fresh each
    # reconciling cycle); `cli_payload()`'s canned block is a fixed stop from
    # the days a stop rested, and is fixture input rather than live behaviour.
    loaded = cli.load_config()
    payload = cli_payload()
    payload["reconcile"] = cli.reconcile(payload, loaded)
    result = cli.decide(payload, loaded)
    everything = result["plan"]["orders"] + result["plan"]["protection"]
    assert all(o.get("type") != "stop_market" and not o.get("stop_price") for o in everything)
    assert all(o.get("type") == "limit" for o in result["plan"]["protection"])


def test_a_position_through_its_stop_is_sold_with_a_limit_after_its_target_is_cancelled():
    result = cli.decide(
        cli_payload(reconcile=NOTHING_RESTS, open_orders=[RESTING_TARGET], **STOPPED_OUT),
        cli.load_config(),
    )

    assert [e["code"] for e in result["exits"]["exits"]] == ["ATR_STOP"]
    assert [c["order_id"] for c in result["plan"]["cancels"]] == ["t1"]
    sells = [o for o in result["plan"]["orders"] if o.get("side") == "sell"]
    assert [(o["symbol"], o["type"]) for o in sells] == [("F", "limit")]
    assert sells[0]["requires_cancel_of"] == ["t1"]
