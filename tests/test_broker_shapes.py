"""`src/broker_shapes.py` — the three rules, one test each, per key.

`tests/test_broker_contract.py` is the end-to-end half of this: recorded
responses through `normalize -> reconcile -> decide`. This half is the unit
half, and it exists because the contract test can only pin the shapes the
account happened to return on one day, while the module promises to accept
every shape either caller can produce.

The three rules, restated so a reader can check the assertions against them:

* **Both shapes are accepted** — the tool's own envelope and a live session's
  hand-built value, with no caller having to know which one it got.
* **Unknown fields pass through** — a response that grows a column needs no
  edit here.
* **Nothing is defaulted** — a field the broker did not send is absent, never
  `None` and never `0.0`.

And the fourth, which is a property of the wiring rather than of any one key:
**idempotent**, because `run._payload` normalises and then hands the result to
`cli.reconcile`, which normalises again.
"""

from __future__ import annotations

import pytest

from src import broker_shapes
from src.reconcile import calendar_day

RAW_ORDER = {
    "id": "81b7df72",
    "symbol": "MGM",
    "side": "sell",
    "type": "market",
    "trigger": "stop",
    "state": "confirmed",
    "quantity": "6.00000000",
    "stop_price": "39.48000000",
    "time_in_force": "gtc",
    "placed_agent": "agentic",
}


# --- orders -----------------------------------------------------------------


@pytest.mark.parametrize(
    "envelope",
    [
        [RAW_ORDER],
        {"results": [RAW_ORDER]},
        {"orders": [RAW_ORDER]},
        {"data": {"orders": [RAW_ORDER]}},
        {"data": {"results": [RAW_ORDER]}},
        # `assemble` builds this out of an `edge_cycle_input` row that carried
        # a symbol, which is how a per-symbol fetch of orders would arrive.
        {"MGM": [RAW_ORDER]},
        # One order, pasted without its list. The `state`/`side` marks are what
        # tell this apart from the per-symbol map above.
        RAW_ORDER,
    ],
)
def test_every_shape_orders_arrive_in_yields_the_same_rows(envelope):
    assert broker_shapes.orders(envelope) == [{**RAW_ORDER, "order_id": "81b7df72"}]


def test_the_id_is_renamed_and_the_original_is_kept():
    """Kept rather than popped: `id` is the broker's own name for it and a
    runlog reader comparing a row against the app should see the same field
    the app shows. Nothing downstream reads it."""
    order = broker_shapes.orders([RAW_ORDER])[0]

    assert order["order_id"] == "81b7df72"
    assert order["id"] == "81b7df72"


def test_an_order_that_already_carries_order_id_is_not_clobbered():
    """The internal name is first in its own candidate tuple, which is what
    makes `run._payload` normalising and then `cli.reconcile` normalising
    again a no-op rather than a second answer."""
    session_built = {"order_id": "s1", "symbol": "F", "side": "sell", "state": "confirmed"}

    assert broker_shapes.orders([session_built]) == [session_built]
    assert broker_shapes.orders(broker_shapes.orders([RAW_ORDER])) == broker_shapes.orders(
        [RAW_ORDER]
    )


def test_the_read_back_vocabulary_is_passed_through_not_flattened():
    """A `stop_market` comes back as `type: "market"` with `trigger: "stop"`,
    and `protection.resting_kind` matches on exactly that. Collapsing it to the
    placement word here would lose which of the two vocabularies an order came
    from — and recognising nothing the broker returns was the first version of
    `resting_kind`'s own bug."""
    order = broker_shapes.orders([RAW_ORDER])[0]

    assert (order["type"], order["trigger"], order["stop_price"]) == (
        "market",
        "stop",
        "39.48000000",
    )


def test_an_absent_or_unreadable_orders_key_is_empty_rather_than_a_crash():
    assert broker_shapes.orders(None) == []
    assert broker_shapes.orders("nope") == []
    assert broker_shapes.orders({"next_cursor": "z"}) == []


# --- positions --------------------------------------------------------------


RAW_POSITION = {
    "symbol": "MGM",
    "quantity": "6.00000000",
    "average_buy_price": "40.8800",
    "shares_available_for_sells": "0.00000000",
    "shares_held_for_sells": "6.00000000",
}


def test_positions_are_unwrapped_and_nothing_is_renamed():
    """This core and `get_equity_positions` already agree on every name. What
    this key needed was the envelope — and for the two reserved-share fields
    to stop being dropped."""
    assert broker_shapes.positions({"results": [RAW_POSITION]}) == [RAW_POSITION]
    assert broker_shapes.positions([RAW_POSITION]) == [RAW_POSITION]


# --- tax lots ---------------------------------------------------------------


def test_a_lot_is_renamed_once_and_not_twice():
    raw = {"F": [{"quantity": 1.0, "tax_cost_basis": 15.00, "open_date": "2026-07-30"}]}
    once = broker_shapes.tax_lots(raw)

    assert once["F"][0]["cost_basis"] == 15.00
    assert once["F"][0]["acquired_date"] == "2026-07-30"
    assert broker_shapes.tax_lots(once) == once


# --- realized ---------------------------------------------------------------


def test_realized_descends_the_live_envelope_and_stays_stable():
    """`get_pnl_trade_history` nests `data.trades`. A single-level unwrap found
    `data`, saw a Mapping rather than a list, and returned nothing — so this
    key was inert in production from the day it shipped."""
    live = {
        "data": {
            "account_number": "<your-cash-account-number>",
            "span": "week",
            "trades": [
                {
                    "timestamp": "2026-09-04T16:33:05Z",
                    "symbol": "MU",
                    "side": "sell",
                    "quantity": "1.00000000",
                    "price": "112.30000000",
                    "realized_gain": "-8.53",
                }
            ],
        },
        "guide": "…",
    }
    rows = broker_shapes.realized(live, calendar_day)

    assert [r["symbol"] for r in rows] == ["MU"]
    assert rows[0]["realized_gain"] == -8.53
    assert rows[0]["date"] == "2026-09-04"
    # No identifier of any kind comes back, so the `(symbol, date)` fallback is
    # the only match there has ever been. Recorded, not a bug.
    assert rows[0]["order_id"] is None
    assert broker_shapes.realized(rows, calendar_day) == rows


def test_a_missing_realized_number_is_none_and_never_zero():
    """A zero realized gain is a real break-even; an absent one is a failed
    read, and `reconcile` treats them differently on purpose."""
    rows = broker_shapes.realized([{"symbol": "F"}], calendar_day)

    assert rows[0]["realized_gain"] is None
    assert broker_shapes.realized([{"symbol": "F", "realized_gain": 0}], calendar_day)[0][
        "realized_gain"
    ] == 0.0


# --- symbol-keyed maps ------------------------------------------------------


def test_a_batched_response_is_rekeyed_by_symbol():
    """`fundamentals`, `tradability` and `signals` are all read as
    `ctx[key][symbol]`, and all three are filled from tools that answer a batch
    at once. A runner posting one response verbatim would otherwise hand the
    reader a `results` list keyed by nothing."""
    batched = {"results": [{"symbol": "MGM", "tradeable": True}]}

    assert broker_shapes.symbol_map(batched) == {"MGM": {"symbol": "MGM", "tradeable": True}}


def test_a_session_built_map_passes_through_untouched():
    built = {"MGM": {"tradeable": True, "all_day_tradability": "tradable"}}

    assert broker_shapes.symbol_map(built) == built
    assert broker_shapes.symbol_map(broker_shapes.symbol_map(built)) == built


# --- the one pass -----------------------------------------------------------


def test_normalize_touches_only_the_keys_that_came_from_the_broker():
    """Everything else — `control`, `events`, the routine's own `triggers` —
    is copied through, because this module has no opinion about anything the
    broker did not send."""
    payload = {
        "control": {"divergence_streak": 2},
        "triggers": [{"symbol": "PFE", "play_id": "DIP"}],
        "events": {"as_of": "2026-09-12"},
        "orders": {"results": [RAW_ORDER]},
    }
    out = broker_shapes.normalize(payload, calendar_day)

    assert out["control"] is payload["control"]
    assert out["triggers"] is payload["triggers"]
    assert out["events"] is payload["events"]
    assert out["orders"][0]["order_id"] == "81b7df72"


def test_normalize_does_not_mutate_what_it_was_given():
    payload = {"orders": {"results": [RAW_ORDER]}}
    broker_shapes.normalize(payload, calendar_day)

    assert payload["orders"] == {"results": [RAW_ORDER]}


def test_normalize_is_idempotent():
    payload = {
        "orders": {"results": [RAW_ORDER]},
        "positions": {"results": [RAW_POSITION]},
        "quotes": {"results": [{"quote": {"symbol": "MGM", "bid_price": "40.81"}}]},
        "tax_lots": {"F": [{"quantity": 1.0, "tax_cost_basis": 15.0}]},
        "realized": {"data": {"trades": [{"symbol": "MU", "realized_gain": "-8.53"}]}},
        "tradability": {"results": [{"symbol": "MGM", "tradeable": True}]},
    }
    once = broker_shapes.normalize(payload, calendar_day)

    assert broker_shapes.normalize(once, calendar_day) == once


def test_normalize_derives_open_orders_as_the_working_subset_of_orders():
    """The runner posts `orders` only; `decide` reads `open_orders`."""
    done = {**RAW_ORDER, "id": "6aa2done", "state": "filled"}
    payload = {"orders": {"results": [RAW_ORDER, done]}}

    out = broker_shapes.normalize(payload, calendar_day)

    assert [o["order_id"] for o in out["open_orders"]] == ["81b7df72"]
    assert [o["order_id"] for o in out["orders"]] == ["81b7df72", "6aa2done"]
    assert broker_shapes.normalize(out, calendar_day) == out


def test_normalize_leaves_a_posted_open_orders_alone():
    posted = [{"id": "x1", "state": "confirmed", "side": "sell"}]
    out = broker_shapes.normalize(
        {"orders": {"results": [RAW_ORDER]}, "open_orders": posted}, calendar_day
    )

    assert [o["order_id"] for o in out["open_orders"]] == ["x1"]


def test_normalize_adds_no_open_orders_when_there_are_no_orders():
    assert "open_orders" not in broker_shapes.normalize({"control": {}}, calendar_day)


def test_the_terminal_states_match_reconciles():
    from src.reconcile import TERMINAL_ORDER_STATES

    assert broker_shapes._TERMINAL_ORDER_STATES == TERMINAL_ORDER_STATES


def test_normalize_derives_prior_positions_from_prior_book():
    """`prior_positions` is derived here so no future session's cycle.json can
    omit it."""
    payload = {
        "prior_book": {
            "positions": [{"symbol": "D", "quantity": 3, "adopted": True}],
            "trading_day": "2026-09-16",
        }
    }
    out = broker_shapes.normalize(payload, calendar_day)

    assert out["prior_positions"] == [{"symbol": "D", "quantity": 3, "adopted": True}]


def test_normalize_does_not_override_an_explicit_prior_positions():
    payload = {
        "prior_book": {"positions": [{"symbol": "D", "quantity": 3}]},
        "prior_positions": [{"symbol": "D", "quantity": 1}],
    }
    out = broker_shapes.normalize(payload, calendar_day)

    assert out["prior_positions"] == [{"symbol": "D", "quantity": 1}]


def test_normalize_leaves_prior_positions_absent_when_prior_book_is_null():
    """`edge_prior()` returns `'null'::jsonb` for `prior_book` on a day with no
    prior reconcile — the very first cycle ever. Nothing to derive; downstream
    reads `ctx.get("prior_positions") or []`, which already handles absence."""
    out = broker_shapes.normalize({"prior_book": None}, calendar_day)

    assert "prior_positions" not in out


def test_a_key_the_payload_does_not_carry_is_not_invented():
    """An absent key stays absent rather than becoming an empty list. Every
    command already reads these with `or {}` / `or []`, and a key this module
    added would say the routine fetched something it did not."""
    assert broker_shapes.normalize({"now": "2026-09-14T18:47:00Z"}, calendar_day) == {
        "now": "2026-09-14T18:47:00Z"
    }


def test_a_raw_portfolio_is_relocated_so_shape_account_can_find_it():
    payload = {"account": {"total_value": "1400.00"}, "balances": [{"type": "cash"}]}
    out = broker_shapes.normalize(payload, calendar_day)

    assert broker_shapes.account_is_raw(out)
    assert out["portfolio"] == {"total_value": "1400.00"}
    assert out["accounts"] == [{"type": "cash"}]


def test_an_already_merged_account_is_not_relocated():
    payload = {"account": {"equity_total": 1400.00, "total_value": "1400.00"}}
    out = broker_shapes.normalize(payload, calendar_day)

    assert not broker_shapes.account_is_raw(out)
    assert "portfolio" not in out


def test_a_data_wrapped_portfolio_is_unwrapped_before_relocating():
    """`get_portfolio` answers `{"data": {"total_value": ..., ...}}` and the
    runner posts it verbatim (docs/routine/runner-prompt.md). Before this,
    `account_is_raw` looked for `total_value` at the top level of `account`
    and never found it under `data`, so `_relocate_account` never ran,
    `equity_total` was never derived, and `report.py` died with `KeyError:
    'equity_total'` in `finalize` — an earlier postclose runner cycle."""
    payload = {
        "account": {"data": {"total_value": "1400.00", "cash": "180.00"}},
        "balances": [{"type": "cash"}],
    }
    out = broker_shapes.normalize(payload, calendar_day)

    assert broker_shapes.account_is_raw(out)
    assert out["portfolio"] == {"total_value": "1400.00", "cash": "180.00"}
    assert out["accounts"] == [{"type": "cash"}]


# --- the orders fetch is two responses (cli._fetch_plan) -------------------


def test_a_list_of_order_responses_flattens_and_keeps_a_shared_order_once():
    """A live session posts the open and the recent response as a two-element
    list under `orders`. Each element is a whole envelope; an order both
    windows returned is one order."""
    open_row = {**RAW_ORDER, "id": "a", "state": "confirmed"}
    recent_row = {**RAW_ORDER, "id": "b", "state": "filled"}

    shaped = broker_shapes.orders(
        [
            {"data": {"orders": [open_row]}},
            {"data": {"orders": [recent_row, open_row]}},
        ]
    )

    assert [row["order_id"] for row in shaped] == ["a", "b"]


def test_an_empty_response_in_the_list_is_still_a_response():
    row = {**RAW_ORDER, "id": "a"}

    shaped = broker_shapes.orders([{"data": {"orders": [row]}}, {"data": {"orders": []}}])

    assert [r["order_id"] for r in shaped] == ["a"]
    assert broker_shapes.orders([{"data": {"orders": []}}, {"data": {"orders": []}}]) == []


def test_a_list_of_bare_rows_is_unchanged_by_the_response_list_rule():
    rows = [{**RAW_ORDER, "id": "a"}, {**RAW_ORDER, "id": "b"}]

    assert [r["order_id"] for r in broker_shapes.orders(rows)] == ["a", "b"]


# The row the runner posted for INVH in cycle d4917ae9, trimmed to the fields
# that matter. `assemble` keys these by symbol.
PER_SYMBOL_QUOTE = {
    "close": {"date": "2026-09-30", "price": "26.33", "symbol": "INVH"},
    "quote": {
        "state": "active",
        "symbol": "INVH",
        "ask_price": "26.180000",
        "bid_price": "26.170000",
        "last_trade_price": "26.185000",
        "adjusted_previous_close": "26.330000",
    },
}


def test_quotes_renames_per_symbol_rows_so_exits_can_see_a_bid():
    out = broker_shapes.quotes({"INVH": PER_SYMBOL_QUOTE})

    assert out["INVH"] == {
        "last": "26.185000",
        "bid": "26.170000",
        "ask": "26.180000",
        "adjusted_previous_close": "26.330000",
    }


def test_quotes_per_symbol_rows_match_the_results_list_shape():
    assert broker_shapes.quotes({"INVH": PER_SYMBOL_QUOTE}) == broker_shapes.quotes(
        {"data": {"results": [PER_SYMBOL_QUOTE]}}
    )


def test_quotes_leaves_an_already_flat_map_alone():
    flat = {"INVH": {"bid": "26.17", "ask": "26.18", "last": "26.185"}}

    assert broker_shapes.quotes(flat) == flat
    assert broker_shapes.quotes(broker_shapes.quotes({"INVH": PER_SYMBOL_QUOTE})) == (
        broker_shapes.quotes({"INVH": PER_SYMBOL_QUOTE})
    )


def test_normalize_gives_exits_a_bid_from_per_symbol_quote_rows():
    out = broker_shapes.normalize({"quotes": {"INVH": PER_SYMBOL_QUOTE}}, calendar_day)

    assert out["quotes"]["INVH"]["bid"] == "26.170000"



def _sell_with_execution(order_id="o1"):
    return {
        "id": order_id, "state": "filled", "side": "sell", "symbol": "INVH",
        "executions": [{"quantity": "7.000000", "price": "26.55",
                        "timestamp": "2026-10-02T13:49:58.017Z"}],
    }


def test_normalize_derives_fills_from_executions_of_our_orders():
    """The runner posts `orders` only."""
    payload = {
        "orders": {"results": [_sell_with_execution(), _sell_with_execution("other")]},
        "intent": {"placements": [{"order_id": "o1", "symbol": "INVH"}]},
    }

    out = broker_shapes.normalize(payload, calendar_day)

    assert out["fills"] == [{
        "order_id": "o1", "symbol": "INVH", "side": "sell", "quantity": "7.000000",
        "price": "26.55", "timestamp": "2026-10-02T13:49:58.017Z", "derived": True,
    }]
    assert broker_shapes.normalize(out, calendar_day) == out


def test_normalize_skips_fills_for_retired_orders():
    payload = {
        "orders": {"results": [_sell_with_execution()]},
        "intent": {"placements": [{"order_id": "o1"}]},
        "control": {"retired_order_ids": ["o1"]},
    }

    assert "fills" not in broker_shapes.normalize(payload, calendar_day)


def test_normalize_leaves_posted_fills_alone_and_adds_none_without_placements():
    posted = [{"order_id": "x", "symbol": "F", "side": "buy", "quantity": 1}]
    out = broker_shapes.normalize(
        {"orders": {"results": [_sell_with_execution()]},
         "intent": {"placements": [{"order_id": "o1"}]}, "fills": posted},
        calendar_day,
    )
    assert out["fills"] == posted
    assert "fills" not in broker_shapes.normalize(
        {"orders": {"results": [_sell_with_execution()]}}, calendar_day
    )
