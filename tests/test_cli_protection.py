"""`plan.protection`: reconcile's plan, through guard, into the routine's hands.

The join this file is about is the one `cli.py` always owns — three modules
that each hold half of the answer. `reconcile` knows what should be resting
and what already is; `guard` knows whether any of it may be placed; only
`decide` knows that an exit is also going out on the same symbol this cycle,
which is what makes the resting order under it dangerous rather than useful.
"""

from src import cli
from tests.factories import cli_payload


def decided(**overrides) -> dict:
    return cli.decide(cli_payload(**overrides), cli.load_config())


def protection(result) -> list[dict]:
    return result["plan"]["protection"]


def cancels(result) -> list[str]:
    return [c["order_id"] for c in result["plan"]["cancels"]]


REPLACE = {
    "protection_plan": {
        "place": [],
        "keep": [],
        "notes": [],
        "replace": [
            {
                "cancel_order_id": "old1",
                "new_order": {
                    "symbol": "F", "side": "sell", "type": "stop_market",
                    "quantity": 1.0, "limit_price": None, "stop_price": 14.10,
                    "session": "regular_hours", "time_in_force": "gtc",
                    "play_id": "DIP", "protective_stop": True,
                    "protective_target": False,
                },
            }
        ],
    }
}


def test_the_baseline_cycle_carries_the_one_resting_order():
    """One, not two. The broker reserves 100% of a position's shares against
    the first resting sell it accepts, so a stop and a target cannot both be
    there — `protection_rest_kind` picks which, and the default is the stop."""
    result = decided()

    assert [o["type"] for o in protection(result)] == ["stop_market"]
    assert all(o["time_in_force"] == "gtc" for o in protection(result))


def test_protection_is_kept_out_of_plan_orders():
    """Two lists because they are placed differently: these carry `gtc`, they
    are expected *not* to fill, and each one has to be written into
    `placements` with its own flag or the postclose sweep takes it back."""
    result = decided()

    assert [o["symbol"] for o in result["plan"]["orders"]] == ["PFE"]
    assert all(o["symbol"] == "F" for o in protection(result))


def test_a_replace_becomes_a_cancel_and_a_placement():
    result = decided(reconcile=REPLACE)

    assert cancels(result) == ["old1"]
    assert [o["stop_price"] for o in protection(result)] == [14.10]


def test_the_replacement_waits_for_the_order_it_supersedes():
    """The same dependency an exit carries, for the same reason: the order
    being superseded holds the position's shares until it actually reads
    `cancelled`, and a replacement refused into that window leaves the
    position with nothing resting at all."""
    result = decided(reconcile=REPLACE)

    assert [o["requires_cancel_of"] for o in protection(result)] == [["old1"]]


def test_the_replacement_is_not_denied_against_the_order_it_supersedes():
    """The cancel and the placement are in one plan; guard has to know."""
    result = decided(
        reconcile=REPLACE,
        open_orders=[
            {"order_id": "old1", "symbol": "F", "side": "sell",
             "type": "stop_market", "stop_price": 12.00, "quantity": 1.0,
             "state": "confirmed", "time_in_force": "gtc"}
        ],
    )

    assert len(protection(result)) == 1
    assert result["guard"]["denied"] == []


def test_a_symbol_being_sold_this_cycle_gets_no_protection():
    """A resting stop under a position an exit is about to close is a working
    sell order on shares that will not exist in an hour."""
    result = decided(
        signals={"F": {"rsi": 45.0, "sma_20": 15.10, "sma_50": 15.20,
                       "sma_200": 13.50, "atr": 0.01}},
        quotes={"F": {"bid": 10.00, "ask": 10.02, "last": 10.00}},
    )

    assert [e["code"] for e in result["exits"]["exits"]] == ["ATR_STOP"]
    assert protection(result) == []


# A protection plan with nothing in it: the resting stop under F is correct
# and kept, so the plan proposes no order of its own and the only thing that
# happens to it is the exit-side cancel below.
NOTHING_RESTS = {
    "protection_plan": {"place": [], "replace": [], "keep": [], "notes": []}
}

RESTING_STOP = {
    "order_id": "p1", "symbol": "F", "side": "sell", "type": "market",
    "trigger": "stop", "stop_price": 14.10, "quantity": 1.0,
    "state": "confirmed", "time_in_force": "gtc", "placed_agent": "agentic",
}

# The tape that puts F through its stop: a 0.01 ATR against a 10.00 print on a
# 15.00 entry is an `ATR_STOP` with no ambiguity in it.
STOPPED_OUT = {
    "signals": {"F": {"rsi": 45.0, "sma_20": 15.10, "sma_50": 15.20,
                      "sma_200": 13.50, "atr": 0.01}},
    "quotes": {"F": {"bid": 10.00, "ask": 10.02, "last": 10.00}},
}


def selling(result) -> list[dict]:
    return [o for o in result["plan"]["orders"] if o.get("side") == "sell"]


def test_a_symbol_being_sold_this_cycle_loses_the_protection_it_has():
    """The one case where the plan cancels a protective order without
    replacing it — otherwise it would sell the *next* position in that name."""
    result = decided(
        reconcile=NOTHING_RESTS,
        open_orders=[RESTING_STOP],
        **STOPPED_OUT,
    )

    assert cancels(result) == ["p1"]


def test_the_cancel_is_found_in_the_brokers_orders_not_in_a_keep_row():
    """This used to emit the cancel only for a `keep` row that carried an
    `order_id`, and every `keep` row carried `order_id: null` —
    the broker returns the id as `id` and nothing renamed it (see
    `src/broker_shapes.py`). No cancel went out, the resting stop kept its
    reservation on all six shares, and the `TIME_STOP` was refused *"Not enough
    shares to sell."* A null id upstream can no longer turn a cancel into a
    no-op, because the order is found by walking the broker's own list.
    """
    result = decided(
        reconcile={
            "protection_plan": {
                "place": [], "replace": [], "notes": [],
                "keep": [{"symbol": "F", "kind": "stop", "order_id": None}],
            }
        },
        open_orders=[RESTING_STOP],
        **STOPPED_OUT,
    )

    assert cancels(result) == ["p1"]


def test_the_sell_names_the_cancel_it_has_to_wait_for():
    """Step 8a performs cancels first, but "first" is not "acknowledged": a
    cancel the broker has accepted and not yet applied still holds the shares.
    The runner waits for `state == cancelled` on every id named here."""
    result = decided(
        reconcile=NOTHING_RESTS,
        open_orders=[RESTING_STOP],
        **STOPPED_OUT,
    )

    assert [o["requires_cancel_of"] for o in selling(result)] == [["p1"]]


def test_an_exit_with_nothing_resting_under_it_names_no_cancel():
    """The key is absent rather than empty: an order that has to wait and one
    that does not are different orders, and `[]` reads like a dependency that
    was computed and came back empty."""
    result = decided(
        reconcile=NOTHING_RESTS,
        open_orders=[],
        **STOPPED_OUT,
    )

    assert selling(result)
    assert all("requires_cancel_of" not in o for o in selling(result))


def test_an_operators_resting_sell_is_not_cancelled_for_our_exit():
    """§13's oldest rule holds at the top of that loop too. The exit is still
    placed — `guard` refuses it PROTECTION_CONTENDS by name rather than leaving
    the broker to refuse it in a rejection nobody reads."""
    result = decided(
        reconcile=NOTHING_RESTS,
        open_orders=[{**RESTING_STOP, "placed_agent": "user"}],
        **STOPPED_OUT,
    )

    assert cancels(result) == []


def test_a_cycle_without_a_reconcile_block_proposes_no_protection():
    """An overnight or a postclose does not reconcile, so there is no fresh
    book to compute a plan against — and `stop_market` is regular-hours-only
    on those cycles anyway."""
    result = decided(
        now="2026-08-04T00:30:00Z",
        control={"has_whole_share_position": True, "positions_held": 1},
        reconcile=None,
    )

    assert protection(result) == []


def test_the_resting_orders_do_not_spend_the_daily_entry_allowance():
    """§10's two-a-day cap counts buys. Every protective order is a sell, and
    a cap that counted them would run out on a quiet Tuesday."""
    assert decided()["control_updates"]["new_positions"]["count"] == 1


def test_a_twice_refused_stop_is_blocked_rather_than_sent_again():
    """§14's "no blind retry" is about the order's history, and it reaches
    every order in the plan. Without this a `stop_market` the broker has
    already refused twice today would be re-sent on every cycle."""
    result = decided(
        rejections=[
            {"order": {"symbol": "F", "side": "sell", "type": "stop_market",
                       "quantity": 1.0},
             "detail": "Stop price must be below the last trade price"}
        ] * 2
    )

    assert protection(result) == []
    assert [b["code"] for b in result["blocked"]] == ["PLACEMENT_BLOCKED"]
    assert result["blocked"][0]["order"]["protective_stop"] is True
