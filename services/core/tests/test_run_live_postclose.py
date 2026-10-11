"""A post-close cycle, end to end, on the broker's own envelopes.

Every earlier fixture was a `cycle.json` in *this core's* vocabulary, exploded into
rows — which is the one shape the runner never posts.

These rows are an earlier runner cycle's own `edge_cycle_input` payloads,
trimmed to two positions and the quotes they need, plus the per-symbol
`get_equity_tax_lots` rows the fixed prompt now posts. Every envelope —
`data`, `data.results`, `data.positions`, `data.orders`, `data.trades` — is
exactly as the tools return it. If a renamer stops accepting one of them, this
test is where it shows, not the 18:47 cycle.
"""

from __future__ import annotations

import copy

from services.core.core import run as core
from services.core.tests.conftest import FakeSupabase

CYCLE_ID = "c8daa91f-live-shapes"

ACCOUNT = {"data": {"cash": "180.0000", "currency": "USD", "total_value": "1250.0000",
                    "buying_power": {"buying_power": "180.0000", "display_currency": "USD",
                                     "unleveraged_buying_power": "180.0000"},
                    "equity_value": "1070.0000", "pending_deposits": "0"}}
BALANCES = {"data": {"accounts": [
    {"type": "margin", "state": "active", "account_number": "<your-margin-account-number>",
     "agentic_allowed": False, "unsettled_funds": "644.3500"},
    {"type": "cash", "state": "active", "nickname": "Agentic", "account_number": "<your-cash-account-number>",
     "agentic_allowed": True, "unsettled_funds": "0.0000"},
]}}
POSITIONS = {"data": {"positions": [
    {"type": "long", "symbol": "INVH", "quantity": "7.000000", "average_buy_price": "27.150000",
     "shares_held_for_sells": "0.000000", "shares_available_for_sells": "7.000000"},
    {"type": "long", "symbol": "KIM", "quantity": "1.000000", "average_buy_price": "22.560000",
     "shares_held_for_sells": "1.000000", "shares_available_for_sells": "0.000000"},
]}}
ORDERS = {"data": {"orders": [
    {"id": "4237066a-be3c-48e4-a135-225c857668f7", "symbol": "KIM", "side": "sell",
     "type": "market", "trigger": "stop", "state": "confirmed", "quantity": "1.000000",
     "cumulative_quantity": "0.000000", "price": None, "stop_price": "21.000000",
     "time_in_force": "gtc", "market_hours": "regular_hours", "placed_agent": "agentic",
     "created_at": "2026-09-18T16:00:00Z", "executions": []},
]}}
REALIZED = {"data": {"span": "week", "account_number": "<your-cash-account-number>", "next_cursor": "", "trades": [
    {"side": "sell", "price": "38.92", "symbol": "MGM", "quantity": "4",
     "timestamp": "2026-09-17T16:16:55Z", "realized_gain": "0.08"},
    {"side": "sell", "price": "27.22", "symbol": "INVH", "quantity": "8",
     "timestamp": "2026-09-16T19:21:43Z", "realized_gain": "-6.76"},
]}}


def _quote(symbol, bid, ask, last):
    return {"close": {"date": "2026-09-17", "price": last, "symbol": symbol},
            "quote": {"state": "active", "symbol": symbol, "bid_price": bid, "ask_price": ask,
                      "last_trade_price": last, "adjusted_previous_close": last}}


QUOTES = {"data": {"results": [
    _quote("INVH", "27.300000", "27.340000", "27.320000"),
    _quote("KIM", "22.600000", "22.640000", "22.620000"),
    _quote("SPY", "762.830000", "762.990000", "761.640000"),
]}}


def _lots(symbol, quantity, basis, opened):
    return {"data": {"symbol": symbol, "tax_lots": [
        {"open_lot_id": f"{symbol}-lot", "open_tran_type": "buy", "quantity": quantity,
         "quantity_available": quantity, "is_selectable": True,
         "cost_per_share": f"{float(basis) / float(quantity):.6f}", "tax_cost_basis": basis,
         "open_date": opened, "term": "st"}]}}


CONTROL = {"readable": True, "trading_enabled": True, "unreconciled": False,
           "positions_held": 2, "entries_blocked": False, "high_water_mark": 750.00,
           "last_full_cycle": "2026-09-18T13:41:18Z", "divergence_streak": 0,
           "open_order_cycles": {}, "retired_order_ids": [], "operator_holds": {},
           "has_whole_share_position": True, "last_scorecard_date": "2026-09-04"}

ROWS = [
    {"kind": "account", "symbol": None, "payload": ACCOUNT},
    {"kind": "balances", "symbol": None, "payload": BALANCES},
    {"kind": "positions", "symbol": None, "payload": POSITIONS},
    {"kind": "orders", "symbol": None, "payload": ORDERS},
    {"kind": "tax_lots", "symbol": "INVH", "payload": _lots("INVH", "7.000000", "195.950000", "2026-09-18")},
    {"kind": "tax_lots", "symbol": "KIM", "payload": _lots("KIM", "1.000000", "22.560000", "2026-09-18")},
    {"kind": "realized", "symbol": None, "payload": REALIZED},
    {"kind": "quotes", "symbol": None, "payload": QUOTES},
    {"kind": "now", "symbol": None, "payload": "2026-09-18T22:50:00Z"},
    {"kind": "control", "symbol": None, "payload": CONTROL},
]

PLACED_AFTER = [
    {"kind": "placements", "symbol": None, "payload": []},
    {"kind": "placement_rejections", "symbol": None, "payload": []},
    {"kind": "placement_alerts", "symbol": None, "payload": []},
]

PRIOR_BOOK = {"trading_day": "2026-09-18", "positions": [
    {"symbol": "INVH", "play_id": "DIP", "quantity": 7.0, "entry_price": 27.15,
     "entry_anchored_on": "2026-09-18", "entry_price_source": "carried",
     "entry_price_disputed": True, "high_since_entry": 27.215},
    {"symbol": "KIM", "play_id": "DIP", "quantity": 1.0, "entry_price": 22.56,
     "entry_anchored_on": "2026-09-18", "entry_price_source": "carried",
     "entry_price_disputed": False, "high_since_entry": 22.56},
]}

RETURNS = {
    "edge_prior": {"placements": [], "prior_book": PRIOR_BOOK, "rejections": []},
    "edge_play_state_json": {"DIP": {"regime_fit": 0.4, "closed_trades": 3,
                                     "realized_pnl": -14.39, "near_flat_trades": 1}},
    "edge_symbol_lock_json": {"INVH": "core", "KIM": "core"},
    "edge_dip_recovery_positions_json": {},
    "edge_day_context_recent": {},
    "edge_loss_closes_recent": [
        {"symbol": "INVH", "close_date": "2026-09-16", "loss_amount": 6.76, "basis_unknown": False},
    ],
    "edge_benchmark_latest": {"equity": 1200.000, "trading_day": "2026-09-17"},
    "edge_fill_quality_recent": [],
    "edge_shadow_recent": [],
    "edge_realized_trades_recent": [],
}


def _cycle():
    supabase = FakeSupabase(
        tables={"edge_cycle_input": copy.deepcopy(ROWS), "edge_runlog": []},
        returns=copy.deepcopy(RETURNS),
    )
    decided = core.run(CYCLE_ID, core.DECIDE, supabase)

    # What the real `edge_runlog` would now hold, for finalize to read back.
    supabase.tables["edge_runlog"] = [
        {"command": p["p_command"], "stdout": p["p_stdout"]}
        for p in supabase.args("edge_runlog_write")
    ]
    supabase.tables["edge_cycle_input"] += copy.deepcopy(PLACED_AFTER)
    finalized = core.run(CYCLE_ID, core.FINALIZE, supabase)
    return decided, finalized, supabase


def test_a_postclose_on_the_brokers_own_envelopes_decides_and_finalizes():
    decided, finalized, _ = _cycle()

    assert decided["status"] == core.STATUS_DECIDED, decided.get("detail")
    assert finalized["status"] == core.STATUS_FINALIZED, finalized.get("detail")
    assert decided["plan"] == [] and decided["protection"] == []


def test_the_reconcile_it_writes_saw_both_positions_and_their_lots():
    _, _, supabase = _cycle()

    reconciled = next(
        p["p_stdout"] for p in supabase.args("edge_runlog_write") if p["p_command"] == "reconcile"
    )
    book = {p["symbol"]: p for p in reconciled["positions"]}

    assert set(book) == {"INVH", "KIM"}
    # INVH's lot carries the wash-sale adjustment and the ledger row is in
    # hand, so it is no longer disputed.
    assert book["INVH"]["entry_price_disputed"] is False
    assert book["INVH"]["entry_price"] == 27.15
    assert book["KIM"]["open_lot_count"] == 1


def test_the_live_quote_envelope_reaches_the_benchmark():
    """`data.results[].quote` — unwrapped. Before, `data` was read as a symbol
    and SPY had no price."""
    _, _, supabase = _cycle()

    report_row = next(
        p["p_stdout"] for p in supabase.args("edge_runlog_write") if p["p_command"] == "report"
    )
    assert report_row["benchmark"]["spy_close"] is not None


def test_the_corrected_deposit_record_does_not_flatten_this_book():
    """This cycle's control row carries the mark as it really was, 750.00 —
    measured while three August deposits were unrecorded — and the checkout
    now carries those deposits. Equity 1250.00 less the 1100.00 deposited is
    150.00, far under the 380.00 the old floor put beneath that mark. The
    anchor in `config/inception.json` is what stops §10 from selling INVH and
    KIM on a bookkeeping correction."""
    decided, _, supabase = _cycle()
    rows = {p["p_command"]: p["p_stdout"] for p in supabase.args("edge_runlog_write")}

    # A flatten would be two sells in the plan.
    assert decided["status"] == core.STATUS_DECIDED and decided["plan"] == []
    updates = rows["reconcile"]["control_updates"]
    assert updates["high_water_mark"] == 250.00
    assert updates["high_water_mark_equity"] == 950.00
    assert updates["high_water_mark_as_of"] == "2026-08-17"
    assert all("Drawdown" not in a["title"] for a in rows["report"]["alerts"])


def test_the_scorecard_base_now_includes_every_deposit():
    _, _, supabase = _cycle()

    report_row = next(
        p["p_stdout"] for p in supabase.args("edge_runlog_write") if p["p_command"] == "report"
    )
    assert report_row["benchmark"]["net_deposits"] == 1100.00
