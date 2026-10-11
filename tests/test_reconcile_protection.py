"""§12 applied to the one class of order it could never see.

`_order_findings` diffs *intent* against broker truth, and a resting protective
order stops being intent the moment the placements batch that carried it rolls
off `edge_prior()`. So a stop cancelled a fortnight ago and a stop working
perfectly look identical to it. `_protection_plan` diffs what *should* be true
against truth instead, which is the only diff that can notice an absence.
"""

import pytest

from src import reconcile
from src.plays import protection
from tests.factories import config, reconcile_context

# What the routine sends for a position it can compute a stop for. The
# baseline `reconcile_context` carries neither, so every existing golden
# records the missing-ATR note rather than an order — accurate for that
# fixture and useless as a record of the normal case, which is this one.
MARKET = {
    "signals": {"F": {"atr": 0.45}},
    "quotes": {"F": {"bid": 14.85, "ask": 14.87, "last": 14.88}},
}


# This whole file exercises the resting-stop machinery `protection.py` still
# has — `protection_rest_kind: "stop"` is no longer the shipped default (see
# protection.py's NONE), but the mechanics it tests (which order gets
# kept/replaced/cancelled, whose order is whose) are unchanged and still
# load-bearing whenever the operator sets it back. Pinned here rather than
# left to `config()`'s shipped default so this file keeps testing them
# regardless of which way the live config points.
def planned(**overrides) -> dict:
    overrides.setdefault("config", config(protection_rest_kind="stop"))
    return reconcile.run(reconcile_context(**{**MARKET, **overrides})).protection_plan


def resting(order_id="p1", type="stop_market", price=13.65, quantity=1.0, **extra):
    """One working protective order of *ours*, as `get_equity_orders` returns it.

    `placed_agent: "agentic"` is not decoration — it is what makes the order
    this side's to cancel (`protection.is_ours_to_replace`). The live MGM stop
    on this account carries it; a fixture without it describes an order of
    unknown provenance, which the plan deliberately leaves alone.
    """
    order = {
        "order_id": order_id,
        "symbol": "F",
        "side": "sell",
        "type": type,
        "quantity": quantity,
        "state": "confirmed",
        "time_in_force": "gtc",
        "placed_agent": "agentic",
        **extra,
    }
    order["stop_price" if type == "stop_market" else "limit_price"] = price
    return order


# --- the high-water ratchet -------------------------------------------------


def test_a_new_position_starts_its_high_at_the_entry_price():
    book = reconcile.run(reconcile_context(**MARKET)).positions

    assert book[0].high_since_entry == 15.00


def test_the_high_ratchets_up_with_the_tape():
    book = reconcile.run(
        reconcile_context(**{**MARKET, "quotes": {"F": {"last": 21.00}}})
    ).positions

    assert book[0].high_since_entry == 21.00


def test_the_high_never_falls_back():
    """The ratchet is what makes the trailing stop trail rather than follow."""
    book = reconcile.run(
        reconcile_context(
            **MARKET,
            prior_positions=[
                {"symbol": "F", "play_id": "DIP", "quantity": 1.0,
                 "high_since_entry": 21.00}
            ],
        )
    ).positions

    assert book[0].high_since_entry == 21.00


def test_the_intraday_high_beats_the_last_trade():
    """A cycle running at 15:40 that read only `last` has thrown away most of
    the session, and a stop is about where the position has *been*."""
    book = reconcile.run(
        reconcile_context(**{**MARKET, "quotes": {"F": {"last": 16.00, "high": 17.50}}})
    ).positions

    assert book[0].high_since_entry == 17.50


def test_a_closed_position_takes_its_high_with_it():
    """No control key to prune: the book is rebuilt from the broker's own
    positions every cycle, so a symbol sold and re-bought later starts fresh
    rather than inheriting a peak from a thesis that no longer exists."""
    book = reconcile.run(
        reconcile_context(
            **MARKET,
            positions=[],
            prior_positions=[
                {"symbol": "F", "play_id": "DIP", "quantity": 1.0,
                 "high_since_entry": 21.00}
            ],
        )
    ).positions

    assert book == []


# --- place, keep, replace ---------------------------------------------------


def test_an_unprotected_position_places_the_one_order_that_rests():
    """One, not two. The broker reserves the whole position against the first
    resting sell it accepts, so a stop and a target cannot both be there —
    `protection_rest_kind` picks the stop."""
    plan = planned()

    assert [o["type"] for o in plan["place"]] == ["stop_market"]
    assert plan["replace"] == []
    assert plan["keep"] == []


def test_a_correct_resting_stop_is_kept_rather_than_replaced():
    plan = planned(orders=[resting()])

    assert [o["symbol"] for o in plan["keep"]] == ["F"]
    assert plan["keep"][0]["order_id"] == "p1"
    assert plan["place"] == []
    assert plan["replace"] == []


def test_a_stop_a_cent_out_is_inside_the_band_and_left_alone():
    """Without the band a trailing stop that ratchets a cent a day would be
    cancelled and replaced every cycle — three windows a day in which the
    position has no stop at all, to move a price by less than a spread."""
    plan = planned(orders=[resting(price=13.66)])

    assert [o["kind"] for o in plan["keep"]] == [protection.STOP]
    assert plan["replace"] == []


def test_a_stop_at_the_wrong_price_is_cancelled_and_replaced():
    plan = planned(orders=[resting(price=12.00)])

    assert len(plan["replace"]) == 1
    assert plan["replace"][0]["cancel_order_id"] == "p1"
    assert plan["replace"][0]["new_order"]["stop_price"] == 13.65


def test_a_stop_that_covers_only_part_of_the_position_is_replaced():
    """A resting order for 1 of 3 shares covers a third of the position."""
    plan = planned(
        positions=[{"symbol": "F", "quantity": 3.0}],
        tax_lots={"F": [{"quantity": 3.0, "cost_basis": 45.00,
                         "acquired_date": "2026-07-30"}]},
        orders=[resting(quantity=1.0)],
    )

    assert plan["replace"][0]["cancel_order_id"] == "p1"
    assert plan["replace"][0]["new_order"]["quantity"] == 3.0


def test_a_terminal_order_is_not_protection_that_rests():
    plan = planned(orders=[resting(state="cancelled")])

    assert plan["keep"] == []
    assert [o["type"] for o in plan["place"]] == ["stop_market"]


def test_a_second_stop_on_one_symbol_is_cancelled_with_nothing_to_replace_it():
    """Two live stops on one position sell it twice. The first carries the
    replacement; the extra is cancelled outright."""
    plan = planned(orders=[resting("p1", price=12.00), resting("p2", price=11.00)])

    assert [(r["cancel_order_id"], r["new_order"] is None) for r in plan["replace"]] == [
        ("p1", False),
        ("p2", True),
    ]


def test_an_ordinary_day_limit_sell_is_not_read_as_a_target():
    """Every order this system places from `plan.orders` is a day order, so a
    day limit sell on a held name is the operator's own exit, not cover."""
    plan = planned(orders=[resting(type="limit", price=15.90, time_in_force="gfd")])

    assert plan["keep"] == []
    assert [o["type"] for o in plan["place"]] == ["stop_market"]


# --- protection_rest_kind: "none" (the shipped default) --------


def test_none_places_nothing_for_an_unprotected_position():
    """The shipped config. `protection.desired` proposes no order at all, for
    any position, so there is nothing to place and nothing to keep — §6.5's
    `ATR_STOP`/`TARGET` in `plays/exits.py` are what protect the position now,
    evaluated fresh every cycle, not a resting broker order."""
    plan = reconcile.run(
        reconcile_context(**{**MARKET, "config": config(protection_rest_kind="none")})
    ).protection_plan

    assert plan["place"] == []
    assert plan["keep"] == []
    assert plan["replace"] == []


def test_none_cancels_a_stop_left_resting_from_before_the_switch():
    """A stop placed while `protection_rest_kind` was `"stop"` does not linger
    once the config flips — nothing wants it any more, so it is cancelled with
    nothing to replace it, the same as any other leftover leg."""
    plan = reconcile.run(
        reconcile_context(
            **{**MARKET, "config": config(protection_rest_kind="none")},
            orders=[resting()],
        )
    ).protection_plan

    assert plan["place"] == []
    assert plan["keep"] == []
    assert [(r["cancel_order_id"], r["new_order"]) for r in plan["replace"]] == [
        ("p1", None)
    ]


# --- the postclose sweep ----------------------------------------------------


def cancelled(placements) -> list[str]:
    result = reconcile.run(
        reconcile_context(
            **MARKET,
            cycle={
                "cycle_type": "postclose",
                "session": "regular_hours",
                "now": "2026-08-03T20:10:00Z",
                "trading_day": "2026-08-03",
                "reconcile_required": True,
            },
            intent={"placements": placements},
            orders=[
                {**resting(p["order_id"]), "type": p.get("type", "stop_market")}
                for p in placements
            ],
        )
    )
    return [c["order_id"] for c in result.cancels]


def test_the_postclose_sweep_still_cancels_an_ordinary_order():
    assert cancelled(
        [{"order_id": "x1", "symbol": "F", "side": "sell", "quantity": 1.0}]
    ) == ["x1"]


@pytest.mark.parametrize("flag", ["protective_stop", "protective_target"])
def test_the_postclose_sweep_spares_both_kinds_of_protection(flag):
    """The target joined it with `plan.protection`: losing that one is the
    quieter failure, because nothing looks wrong until the move is given
    back."""
    assert cancelled(
        [{"order_id": "x1", "symbol": "F", "side": "sell", "quantity": 1.0, flag: True}]
    ) == []


# --- whose order is it? -----------------------------------------------------
#
# §13's oldest safety rule: "a phantom order is never cancelled, at any hour —
# it may be the operator's." The first version of `_protection_plan` diffed
# against *every* resting order, so an operator's manual take-profit at 25.00
# came back as `replace(-> 15.90)` — cancelling a sale they placed on purpose
# and replacing it with one ~40% lower — in the same cycle reconciliation
# flagged that order `PHANTOM_ORDER`. Caught by simulation, before any cycle
# ran on it.


def foreign(order_id="operator-1", price=25.00, **overrides):
    """An operator's own GTC take-profit, placed in the app."""
    return {"order_id": order_id, "symbol": "F", "side": "sell", "type": "limit",
            "state": "confirmed", "quantity": "1.000000", "price": str(price),
            "stop_price": None, "time_in_force": "gtc", "trigger": "immediate",
            "placed_agent": "user", **overrides}


def test_an_operators_own_resting_sell_is_never_cancelled():
    plan = planned(orders=[foreign()])

    assert plan["replace"] == []
    assert [k["order_id"] for k in plan["keep"]] == ["operator-1"]


def test_an_operators_order_is_named_as_not_ours():
    """`keep` is the honest description — checked, left alone, not ours — and
    the flag is what stops a later reader assuming everything kept is the
    system's own work."""
    assert planned(orders=[foreign()])["keep"][0]["ours"] is False


def test_an_operators_target_covers_the_stop_it_does_not_resemble():
    """Their order rests on the shares, and the shares are what matters.

    They cannot: the broker reserves 100% of the position against the first
    resting sell, so an operator's GTC take-profit is exactly what would refuse
    our stop. Nothing is placed over it, and nothing of theirs is cancelled
    (§13).
    """
    plan = planned(orders=[foreign()])

    assert plan["place"] == []
    assert plan["replace"] == []
    assert [k["order_id"] for k in plan["keep"]] == ["operator-1"]


def test_our_own_stale_order_is_still_replaced():
    """The exemption must not disable the trailing ratchet on our own orders."""
    plan = planned(orders=[foreign(order_id="a1", placed_agent="agentic")])

    assert plan["replace"][0]["cancel_order_id"] == "a1"
    assert plan["keep"] == []


def test_an_order_in_this_cycles_own_placements_is_ours():
    """`placed_agent` is the durable signal, but a placement this cycle still
    counts — the two qualify independently."""
    plan = planned(
        orders=[foreign(order_id="a1", placed_agent=None)],
        intent={"placements": [{"order_id": "a1", "symbol": "F", "side": "sell",
                                "quantity": 1.0, "protective_target": True}]},
    )

    assert plan["replace"][0]["cancel_order_id"] == "a1"


def test_unknown_provenance_fails_toward_leaving_it_alone():
    """A stale order left alone costs one order `STALE_OPEN_ORDER` still
    reports; cancelling one costs the operator a trade they meant to place."""
    plan = planned(orders=[foreign(order_id="mystery", placed_agent=None)])

    assert plan["replace"] == []
    assert [k["order_id"] for k in plan["keep"]] == ["mystery"]
