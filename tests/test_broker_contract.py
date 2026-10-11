"""Recorded broker responses, fed through `normalize -> reconcile -> decide`.

**The test that would have caught it five times** — the one missing test in a
suite of 1947: every fixture in
`tests/` is written in the vocabulary the code already reads, so a fixture can
never catch a rename. This module is the other kind — it starts from
`tests/fixtures/broker/`, which holds tool responses as the broker returns
them, and asserts on what comes out the far end.

2. `reconcile._ours` therefore matched nothing, so all three of this account's
   own resting GTC stops (`placed_agent: agentic`) were classified
   `PHANTOM_ORDER` and the SPG one also `MISSING_ORDER` — while visible in the
   same list.
3. `divergence_streak` reached 5 and entries were blocked until an operator
   cleared `edge_control`.
4. `protection_plan.keep` rows carried `order_id: null`, so
   `cli._protection_orders` could not emit the cancel an exit needs, and three
   exits in a row were rejected "Not enough shares to sell."

Each of those is one assertion below, and the last test is the one that keeps
the fix honest: an order of *unknown* provenance resting on the same book is
still a phantom, because §13's oldest safety rule is that a phantom order is
never cancelled — it may be the operator's.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import broker_shapes, cli, reconcile

BROKER = Path(__file__).parent / "fixtures" / "broker"


def raw(name: str):
    """One recorded response, exactly as `tests/fixtures/broker/README.md`
    describes it. Never reshaped on the way in — that is the thing under
    test."""
    return json.loads((BROKER / f"{name}.json").read_text(encoding="utf-8"))


# The book behind the recorded orders: DIP opened all three, two sessions ago
# for INVH/MGM and one for SPG. Carried on `prior_positions` because that is
# where `reconcile._attribution` reads a play from once `edge_prior()`'s
# placements have rolled off — which,, they had.
BOOK = [
    {"symbol": "INVH", "play_id": "DIP", "quantity": 8.0, "entry_price": 33.00},
    {"symbol": "MGM", "play_id": "DIP", "quantity": 6.0, "entry_price": 40.88},
    {"symbol": "SPG", "play_id": "DIP", "quantity": 2.0, "entry_price": 175.00},
]

# One ATR per name, chosen so each recorded stop is `protection.stop_price`'s
# own answer for that basis: the fixed 3xATR floor for MGM and SPG, and the
# 3.5xATR trailing floor for INVH, which is quoted above its entry. The
# fixture's prices and these are one internally-consistent set, and
# `tests/fixtures/broker/README.md` says which half is recorded and which is
# reconstructed. No assertion below reads a price.
#
# Each RSI is under `dip.DIP_RSI_EXIT` (50.0) on purpose: this is a book that
# is *resting*, which is the state the whole file is about.
SIGNALS = {
    "INVH": {"atr": 0.285, "rsi": 41.0, "sma_200": 30.00},
    "MGM": {"atr": 0.465, "rsi": 48.0, "sma_200": 38.00},
    "SPG": {"atr": 2.00, "rsi": 44.0, "sma_200": 160.00},
}

QUOTES = {
    "INVH": {"bid": 33.50, "ask": 33.54, "last": 33.52},
    "MGM": {"bid": 40.81, "ask": 40.85, "last": 40.83},
    "SPG": {"bid": 176.18, "ask": 176.24, "last": 176.20},
}

TAX_LOTS = {
    "INVH": [{"quantity": 8.0, "tax_cost_basis": 264.00, "open_date": "2026-09-09"}],
    "MGM": [{"quantity": 6.0, "tax_cost_basis": 245.28, "open_date": "2026-09-09"}],
    "SPG": [{"quantity": 2.0, "tax_cost_basis": 350.00, "open_date": "2026-09-10"}],
}


def payload(**overrides):
    """`orders`, `positions`, `account` and `accounts` are the raw bodies. Every
    other key is in Appendix D's vocabulary, because every other key really is
    assembled by the routine rather than pasted from a tool.

    The INVH and MGM stops had rolled off it entirely, which is exactly the
    state `protection.is_ours_to_replace` exists for.
    """
    base = {
        "cycle_id": "2026-09-14T18:47:00Z-monitor",
        "now": "2026-09-14T18:47:00Z",
        "control": {
            "readable": True,
            "trading_enabled": True,
            # The real one for this account (see the loss-close backfill). No
            # assertion looked at `exits`, so the baseline reconciled clean while
            # `decide` was flattening the book.
            "high_water_mark": 799.54,
            "last_full_cycle": "2026-09-14",
            "last_overnight_cycle": None,
            "new_positions": {"date": None, "count": 0},
            "positions_held": 3,
            "has_whole_share_position": True,
            "unreconciled": False,
            "pending_events": False,
            "entries_blocked": False,
            "entries_blocked_reason": None,
            "entries_blocked_until_cleared": False,
            "divergence_streak": 0,
            "open_order_cycles": {},
            "retired_order_ids": [],
            "last_scorecard_date": None,
            "last_drawdown_alert": None,
            "last_data_fresh_at": {"trading_day": None, "fetched_at": None},
        },
        # The two raw account responses, posted rather than hand-merged.
        "account": raw("get_portfolio"),
        "accounts": raw("get_accounts")["results"],
        "account_number": "<your-cash-account-number>",
        "orders": raw("get_equity_orders"),
        # Appendix D's "working subset of `orders`". The routine derives it,
        # but every order in this response is `confirmed`, so the subset is the
        # whole of it — and `decide` reads this key where `reconcile` reads the
        # one above.
        "open_orders": raw("get_equity_orders"),
        "positions": raw("get_equity_positions"),
        "prior_positions": BOOK,
        "intent": {
            "placements": [
                {
                    "order_id": "1b70ca01",
                    "symbol": "SPG",
                    "side": "sell",
                    "quantity": 2.0,
                    "play_id": "DIP",
                    "protective_stop": True,
                }
            ]
        },
        "fills": [],
        "tax_lots": TAX_LOTS,
        "quotes": QUOTES,
        "signals": SIGNALS,
        "earnings": {},
        # `get_equity_tradability`'s boolean, post an earlier schema change
        # that replaced the `tradability` string with `tradeable` and blocked
        # every order for a day. Needed here because this book now has an exit
        # to place, and guard refuses an order on a symbol it cannot confirm.
        "tradability": {
            symbol: {"tradeable": True, "all_day_tradability": "tradable"}
            for symbol in ("INVH", "MGM", "SPG")
        },
        "snapshot": {"as_of": "2026-09-13", "holdings": [], "cross_check": []},
        "triggers": [],
    }
    base.update(overrides)
    return base


@pytest.fixture(scope="module")
def loaded():
    """The five checked-in files, with `protection_rest_kind` pinned to
    `"stop"`. This module is a regression test for an earlier resting- stop
    outage (see the module docstring) — it needs a resting stop to exist to
    prove reconcile finds it, so it pins the leg under test rather than
    inheriting whatever the live config currently defaults to."""
    loaded = cli.load_config()
    loaded["config"] = {**loaded["config"], "protection_rest_kind": "stop"}
    return loaded


@pytest.fixture(scope="module")
def reconciled(loaded):
    return cli.reconcile(payload(), loaded)


# --- 1. the rename ----------------------------------------------------------


def test_the_order_id_arrives_under_id_and_is_renamed_here():
    """The defect itself, at its own level. `get_equity_orders` says `id`;
    everything from `reconcile._ours` to `cli._protection_orders` says
    `order_id`."""
    orders = broker_shapes.orders(raw("get_equity_orders"))

    assert [o["order_id"] for o in orders][:3] == ["49eacea8", "81b7df72", "1b70ca01"]
    # Pass-through, not a projection: the read-back vocabulary
    # (`type: "market"` + `trigger: "stop"`) is what `protection.resting_kind`
    # matches on, and a normaliser that dropped it would break the thing it
    # was added to fix.
    assert orders[0]["trigger"] == "stop"
    assert orders[0]["placed_agent"] == "agentic"


def test_the_reserved_share_count_survives_the_boundary():
    """The broker reserves every share against the resting stop, which is why 16
    exits were refused; nothing carried the fact across until now."""
    book = broker_shapes.positions(raw("get_equity_positions"))

    assert all(p["shares_available_for_sells"] == "0.00000000" for p in book)


# --- 2/3. classification ----------------------------------------------------


def test_our_own_resting_stops_are_not_phantoms(reconciled):
    """The headline. Three agentic GTC stops, none of them in this cycle's
    placements, and not one of them a divergence."""
    codes = {f["code"] for f in reconciled["findings"]}

    assert reconcile.PHANTOM_ORDER not in codes


def test_a_resting_order_still_in_placements_is_not_missing(reconciled):
    """SPG was reported `MISSING_ORDER` while its order
    sat three rows above it in the same response, because the broker row's id
    read null and the placement's did not."""
    missing = [f for f in reconciled["findings"] if f["code"] == reconcile.MISSING_ORDER]

    assert missing == []


def test_the_recorded_cycle_reconciles_clean(reconciled):
    """The acceptance condition: nothing about this book is a disagreement."""
    assert reconciled["findings"] == []


def test_the_divergence_streak_is_not_advanced(reconciled):
    """What blocked entries for five cycles. A clean reconcile resets the
    streak to 0 and never raises it; `entries_blocked` stays false."""
    updates = reconciled["control_updates"]

    assert updates.get("divergence_streak", 0) == 0
    assert not updates.get("entries_blocked")


# --- 4. the plan ------------------------------------------------------------


def test_keep_rows_carry_the_order_ids_an_exit_needs_to_cancel(reconciled):
    """`cli._protection_orders` cancels a `keep` stop for an exiting symbol
    only when the row has an id. Nulled, it emitted no cancel, the stop kept
    its shares reserved, and the exit was rejected."""
    keep = reconciled["protection_plan"]["keep"]

    assert {row["symbol"] for row in keep} == {"INVH", "MGM", "SPG"}
    assert all(row["order_id"] is not None for row in keep)
    assert all(row["ours"] for row in keep)


def test_nothing_is_placed_or_replaced_because_it_all_already_rests(reconciled):
    plan = reconciled["protection_plan"]

    assert plan["place"] == []
    assert plan["replace"] == []


def test_the_resting_book_proposes_nothing_and_is_refused_nothing(reconciled, loaded):
    """The three-position steady state, which is what every cycle between exits
    should look like: each position carries the one stop it should, so there
    is no order to place and therefore nothing for the broker to refuse."""
    decided = cli.decide(payload(reconcile=reconciled), loaded)

    assert decided["plan"] == {"cancels": [], "orders": [], "protection": []}
    assert decided["guard"]["denied"] == []
    assert decided["blocked"] == []


def test_decide_proposes_no_protection_over_orders_that_already_rest(reconciled, loaded):
    """End of the chain. The protection the plan would place is empty, because
    `reconcile` saw that every desired order is already resting — which is the
    state the broker's own `shares_available_for_sells: 0` describes."""
    decided = cli.decide(payload(reconcile=reconciled), loaded)

    assert decided["plan"]["protection"] == []


# --- the fix that must not overreach ----------------------------------------


def test_a_resting_sell_of_unknown_provenance_is_still_a_phantom(loaded):
    """§13's oldest safety rule. `placed_agent: "user"` is the operator's own
    order: reported, never cancelled, and still a divergence that blocks
    entries for one cycle. Provenance is the whole of what the fix reads, so
    this is the assertion that says it reads it."""
    orders = raw("get_equity_orders")
    theirs = dict(orders["results"][1], id="6aa9ffff", placed_agent="user")
    orders = {"results": [*orders["results"], theirs]}

    result = cli.reconcile(payload(orders=orders), loaded)
    phantoms = [f for f in result["findings"] if f["code"] == reconcile.PHANTOM_ORDER]

    assert [f["order_id"] for f in phantoms] == ["6aa9ffff"]


def test_an_agentic_order_that_is_not_protection_is_still_a_phantom(loaded):
    """The other half of "both conditions, never one". An agentic *day* limit
    buy that no plan proposed is a real divergence — two deciders on one
    account — and provenance alone must not excuse it."""
    orders = raw("get_equity_orders")
    stray = {
        "id": "6aa9aaaa",
        "symbol": "MGM",
        "side": "buy",
        "type": "limit",
        "trigger": "immediate",
        "state": "confirmed",
        "quantity": "1.00000000",
        "price": "40.00000000",
        "time_in_force": "gfd",
        "placed_agent": "agentic",
        "executions": [],
    }
    orders = {"results": [*orders["results"], stray]}

    result = cli.reconcile(payload(orders=orders), loaded)
    phantoms = [f for f in result["findings"] if f["code"] == reconcile.PHANTOM_ORDER]

    assert [f["order_id"] for f in phantoms] == ["6aa9aaaa"]


# --- the account, which the same pass now shapes ----------------------------


def test_the_raw_portfolio_response_is_shaped_rather_than_hand_merged(loaded):
    """A Haiku session read `equity_value` (669.53) where the rule says
    `total_value` (1400.00) and raised an urgent Drawdown halt on a number the
    account never had. Both fields are in the fixture; only one of them is the
    one `shape_account` copies, and no session is asked to know which."""
    shaped = cli.normalized(payload(), loaded)

    assert shaped["account"]["equity_total"] == "1400.00"
    assert shaped["account"]["settled_cash"] == 569.61
    # The raw response is kept beside the shaped one rather than consumed.
    assert shaped["portfolio"]["equity_value"] == "669.53"


def test_an_account_a_session_already_merged_is_left_alone(loaded):
    """Detected, not assumed."""
    merged = {"equity_total": 1400.00, "cash": 569.61, "buying_power": 569.61}

    assert cli.normalized(payload(account=merged), loaded)["account"] == merged


# --- the exit the resting stop was holding the shares against ----------


@pytest.fixture(scope="module")
def stopped_out(loaded):
    """An earlier cycle that mattered: MGM on session 8 of DIP's 8.

    One value moves off the baseline — MGM's lot opens far enough back for
    `TIME_STOP` to fire — because that is the one fact the recorded runlog
    carries that the fixtures cannot: `edge_runlog` row 467 is
    ``orders: [MGM sell 6 @ 39.67 TIME_STOP], cancels: [], protection: [INVH
    target, SPG target]``, and all three were rejected *"Not enough shares to
    sell."*
    """
    lots = {**TAX_LOTS, "MGM": [{"quantity": 6.0, "tax_cost_basis": 245.28,
                                 "open_date": "2026-08-28"}]}
    base = payload(tax_lots=lots)
    reconciled = cli.reconcile(base, loaded)
    return cli.decide(payload(tax_lots=lots, reconcile=reconciled), loaded)


def test_the_time_stop_fires_on_the_session_it_was_rejected(stopped_out):
    assert [(e["symbol"], e["code"]) for e in stopped_out["exits"]["exits"]] == [
        ("MGM", "TIME_STOP")
    ]


def test_the_resting_stop_does_not_cover_a_time_stop(stopped_out):
    """A resting stop covers `ATR_STOP` and nothing else — it sells at a
    different price for a different reason. Reading it as cover here would
    leave the position unsold at the end of its limit, which is the quieter
    version of the same failure."""
    assert stopped_out["exits"]["covered_by_resting"] == []


def test_the_plan_cancels_the_stop_and_sells_the_position(stopped_out):
    """The acceptance condition, replayed against the recorded inputs: one
    cancel, one sell, nothing resting proposed over a symbol that is leaving."""
    plan = stopped_out["plan"]

    assert [c["order_id"] for c in plan["cancels"]] == ["81b7df72"]
    assert [(o["symbol"], o["side"], o["quantity"]) for o in plan["orders"]] == [
        ("MGM", "sell", 6.0)
    ]
    assert plan["protection"] == []


def test_the_sell_waits_for_the_cancel_it_depends_on(stopped_out):
    """Cancels are performed first, but a cancel that has not been
    acknowledged still holds the shares."""
    assert [o["requires_cancel_of"] for o in stopped_out["plan"]["orders"]] == [
        ["81b7df72"]
    ]


def test_no_order_in_the_plan_is_denied(stopped_out):
    """The 16 rejections were never denials — guard approved every one and the
    broker refused it. `PROTECTION_CONTENDS` is what makes that visible here,
    and it must not fire on the plan that carries its own cancel."""
    assert stopped_out["guard"]["denied"] == []


def test_the_other_two_positions_keep_the_stops_they_have(stopped_out):
    """INVH and SPG are not leaving, their stops are correct, and nothing is
    cancelled or re-placed on them."""
    assert [c["symbol"] for c in stopped_out["plan"]["cancels"]] == ["MGM"]


def _as_runner_posts_quotes(flat):
    """`assemble`'s result for per-symbol `get_equity_quotes` rows."""
    return {
        symbol: {
            "close": {"symbol": symbol, "price": str(q["last"])},
            "quote": {
                "state": "active",
                "symbol": symbol,
                "bid_price": f"{q['bid']:.6f}",
                "ask_price": f"{q['ask']:.6f}",
                "last_trade_price": f"{q['last']:.6f}",
                "adjusted_previous_close": f"{q['last']:.6f}",
            },
        }
        for symbol, q in flat.items()
    }


def test_a_time_stop_is_priced_from_per_symbol_quote_rows(stopped_out, loaded):
    """Cycle d4917ae9: the runner posts one quotes row per symbol, `exits.py` read
    no bid, and four TIME_STOPs became "manual limit order required". The same
    book, with quotes in the shape the runner posts, must plan the same sell as
    with the flat shape."""
    lots = {**TAX_LOTS, "MGM": [{"quantity": 6.0, "tax_cost_basis": 245.28,
                                 "open_date": "2026-08-28"}]}
    quotes = _as_runner_posts_quotes(QUOTES)
    reconciled = cli.reconcile(payload(tax_lots=lots, quotes=quotes), loaded)
    out = cli.decide(payload(tax_lots=lots, quotes=quotes, reconcile=reconciled), loaded)

    assert out["exits"].get("manual_required", []) == []
    assert out["plan"]["orders"] == stopped_out["plan"]["orders"]
    assert out["plan"]["orders"][0]["limit_price"] == 40.81


def test_a_denied_exit_keeps_the_resting_target_it_would_have_cancelled(loaded):
    """Replay of an earlier overnight against the 19:08Z inputs: guard denied
    INVH/KIM/RF `EXTENDED_SESSION_NOT_TRADABLE` but the plan still cancelled
    all four resting targets, leaving three positions with no target and no
    exit. A sell that is not placed must not cancel anything."""
    lots = {**TAX_LOTS, "MGM": [{"quantity": 6.0, "tax_cost_basis": 245.28,
                                 "open_date": "2026-08-28"}]}
    tradability = {
        **payload()["tradability"],
        "MGM": {"tradeable": True, "all_day_tradability": "untradable"},
    }
    overnight = dict(now="2026-09-15T02:47:30Z", tax_lots=lots, tradability=tradability)
    reconciled = cli.reconcile(payload(**overnight), loaded)
    out = cli.decide(payload(**overnight, reconcile=reconciled), loaded)

    assert [d["order"]["symbol"] for d in out["guard"]["denied"]] == ["MGM"]
    assert out["guard"]["denied"][0]["denials"][0]["code"] == "EXTENDED_SESSION_NOT_TRADABLE"
    assert out["plan"]["orders"] == []
    assert out["plan"]["cancels"] == []


def _as_runner_posts(**overrides):
    """The payload the runner assembles: per-symbol quotes, and `orders` with
    **no `open_orders`** — `runner-prompt.md` never posts that key."""
    base = payload(quotes=_as_runner_posts_quotes(QUOTES), **overrides)
    base.pop("open_orders")
    return base


def test_a_time_stop_cancels_its_resting_target_when_only_orders_is_posted(
    stopped_out, loaded
):
    """Cycle 1fa9fd8a: INVH/KIM/RF/RTX TIME_STOPs were priced and then denied
    `PROTECTION_CONTENDS` ("0 of 7 shares available"), `plan.cancels` empty,
    because the runner posts `orders` and `decide` read `open_orders`. Same
    book, runner shape: the cancel and the dependent sell must match the
    live-session plan, and nothing is denied."""
    lots = {**TAX_LOTS, "MGM": [{"quantity": 6.0, "tax_cost_basis": 245.28,
                                 "open_date": "2026-08-28"}]}
    reconciled = cli.reconcile(_as_runner_posts(tax_lots=lots), loaded)
    out = cli.decide(_as_runner_posts(tax_lots=lots, reconcile=reconciled), loaded)

    assert out["guard"]["denied"] == []
    assert out["plan"]["cancels"] == stopped_out["plan"]["cancels"]
    assert out["plan"]["orders"] == stopped_out["plan"]["orders"]

