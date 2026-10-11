"""reconcile.py's output is plays.py's input, and nothing translates between.

`plays.py` was written first and blocked on two fields it had no source for:
``entry_price`` and ``sessions_held``. Three of §6.5's seven exits read them —
the ATR stop, the time stop and the target — and all three fail *silently* when
they are missing. A renamed key here would not raise; it would produce a system
that reads as one holding every position forever.

So these tests do not assert on names alone. They feed this module's real
output into `plays.exits` and assert the exit fires, because a key that guard
or plays reads through `.get()` has a default to fall back on and behaviour is
the only thing without one.

Every context below nests the reconciled book by **assignment**. Passing it as
`plays_context(positions=...)` would deep-merge it onto the factory's default,
and a field this module stopped emitting would be quietly filled in by the
fixture — which is exactly the failure a contract test exists to catch.
"""

import json

from src import guard, plays, reconcile
from tests.factories import (
    codes,
    context,
    exited,
    order,
    plays_context,
    position,
    reconcile_context,
)


def book(**overrides) -> list[dict]:
    """This module's reconciled positions, as `plays.py` would receive them."""
    return reconcile.run(reconcile_context(**overrides)).as_dict()["positions"]


def plays_ctx(positions: list[dict], **overrides) -> dict:
    ctx = plays_context(**overrides)
    ctx["positions"] = positions
    return ctx


def closed_at_a_loss(price: float = 14.00):
    """The baseline F position sold out entirely, against a 15.00 basis."""
    return reconcile.run(
        reconcile_context(
            positions=[],
            fills=[
                {"order_id": "zz99", "symbol": "F", "side": "sell", "quantity": 1.0,
                 "price": price, "cost_basis": 15.00}
            ],
            tax_lots={},
        )
    )


# --- the fields plays was blocked on ---------------------------------------


def test_the_reconciled_book_carries_every_key_plays_reads():
    """The names themselves, asserted directly. Everything below infers the
    handover from behaviour, and behaviour has defaults; the names do not."""
    held = book()[0]

    assert set(held) >= {
        "symbol",
        "play_id",
        "quantity",
        "entry_price",
        "sessions_held",
    }


def test_the_baseline_reconciles_to_the_fixture_plays_was_built_against():
    """`factories.position()` was hand-written for the plays build with an
    entry at 15.00 and two sessions held. This module now derives both from
    tax lots and the calendar, and it has to land on the same numbers or every
    exit test in the suite was pinning a shape nothing produces."""
    held = book()[0]

    assert held["entry_price"] == position()["entry_price"]
    assert held["sessions_held"] == position()["sessions_held"]
    assert held["play_id"] == position()["play_id"]


# --- the three exits that were silently dead -------------------------------


def test_the_atr_stop_fires_on_a_reconciled_entry_price():
    """§6.5 exit 4. Entry 15.00, ATR 0.45, so the 3xATR stop is at 13.65 and a
    13.45 print is through it."""
    ctx = plays_ctx(book(), quotes={"F": {"bid": 13.43, "ask": 13.47, "last": 13.45}})

    assert exited(plays.exits(ctx), "F").code == plays.ATR_STOP


def test_the_time_stop_fires_on_a_reconciled_holding_period():
    """§6.5 exit 5."""
    ctx = plays_ctx(
        book(
            tax_lots={
                "F": [
                    {"quantity": 1.0, "cost_basis": 15.00,
                     "acquired_date": "2026-07-20"}
                ]
            }
        )
    )

    assert exited(plays.exits(ctx), "F").code == plays.TIME_STOP


def test_the_target_fires_on_a_reconciled_entry_price():
    """§6.5 exit 6, on the play that declares one. PEAD targets +8%, so a
    15.00 entry takes profit at 16.20."""
    ctx = plays_ctx(
        book(prior_positions=[{"symbol": "F", "play_id": "PEAD", "quantity": 1.0}]),
        quotes={"F": {"bid": 16.48, "ask": 16.52, "last": 16.50}},
    )

    assert exited(plays.exits(ctx), "F").code == plays.TARGET


def test_a_position_inside_all_three_limits_is_held():
    """The baseline, so the three tests above are pinning the rule they name
    rather than an exit that fires unconditionally."""
    assert plays.exits(plays_ctx(book())).exits == []


# --- the adopted position --------------------------------------------------


def test_an_adopted_position_keeps_the_play_independent_exits():
    """§12.2's "managed on identical terms". The 3xATR stop is not play
    property, so it fires here whatever the name is bound to — and it only works
    because the basis came from tax lots.

    It fired under MANUAL too. What changed is everything *else* an adopted name
    now gets; this is the guarantee that predates it and has to survive the
    change."""
    adopted = book(
        prior_positions=[],
        fills=[
            {"order_id": "zz99", "symbol": "F", "side": "buy", "quantity": 1.0,
             "price": 15.00, "cost_basis": None}
        ],
    )
    ctx = plays_ctx(adopted, quotes={"F": {"bid": 13.43, "ask": 13.47, "last": 13.45}})

    assert adopted[0]["play_id"] == plays.ADOPTED
    assert exited(plays.exits(ctx), "F").code == plays.ATR_STOP


def test_an_adopted_position_past_its_leash_is_not_time_stopped_underwater():
    """`ADOPTED` gives an inherited name a clock where MANUAL gave it none — and
    this position is months past that clock. It still does not leave.

    The reason is the second half of the same ruling: a time stop on an adopted
    position would fire purely because time passed, which is "realise a loss to
    free capital". Underwater, the loss waits for a protective exit or a real
    signal."""
    adopted = book(
        prior_positions=[],
        fills=[
            {"order_id": "zz99", "symbol": "F", "side": "buy", "quantity": 1.0,
             "price": 15.00, "cost_basis": None}
        ],
        tax_lots={
            "F": [
                {"quantity": 1.0, "cost_basis": 15.00, "acquired_date": "2026-01-05"}
            ]
        },
    )

    assert adopted[0]["sessions_held"] > max(p.holding_days for p in plays.PLAYS.values())
    assert plays.exits(plays_ctx(adopted)).exits == []


# --- the counter plays reads -----------------------------------------------


def test_the_closed_trade_counter_is_the_one_plays_sizes_catalyst_on():
    """§6.3's 0.75x lifts at 20. `plays._notional_multiplier` reads
    `play_state[CATALYST]["closed_trades"]`, and this module is the only
    writer — so the key has to match or CATALYST is stuck at 0.75x forever."""
    result = reconcile.run(
        reconcile_context(
            prior_positions=[{"symbol": "F", "play_id": "CATALYST", "quantity": 1.0}],
            play_state={"CATALYST": {"closed_trades": 19}},
            positions=[],
            fills=[
                {"order_id": "zz99", "symbol": "F", "side": "sell", "quantity": 1.0,
                 "price": 16.00, "cost_basis": 15.00}
            ],
            tax_lots={},
        )
    )

    ctx = plays_context()
    ctx["play_state"]["CATALYST"] = {
        **ctx["play_state"]["CATALYST"],
        **result.play_state_updates["CATALYST"],
    }

    assert plays._notional_multiplier(plays.CATALYST, ctx) == 1.0


def test_one_closed_trade_short_still_sizes_at_075x():
    """The boundary from the other side, so the test above is pinning 20 and
    not merely "some number was written"."""
    result = reconcile.run(
        reconcile_context(
            prior_positions=[{"symbol": "F", "play_id": "CATALYST", "quantity": 1.0}],
            play_state={"CATALYST": {"closed_trades": 18}},
            positions=[],
            fills=[
                {"order_id": "zz99", "symbol": "F", "side": "sell", "quantity": 1.0,
                 "price": 16.00, "cost_basis": 15.00}
            ],
            tax_lots={},
        )
    )

    ctx = plays_context()
    ctx["play_state"]["CATALYST"] = {
        **ctx["play_state"]["CATALYST"],
        **result.play_state_updates["CATALYST"],
    }

    assert plays._notional_multiplier(plays.CATALYST, ctx) == 0.75


# --- the ledger guard reads ------------------------------------------------


def test_the_loss_row_is_shaped_the_way_guard_reads_it():
    """`guard._wash_sale_denials` walks `ctx["loss_closes"]` reading `symbol`
    and `close_date`. A third key is free; a missing one is a 31-day block
    that never applies."""
    result = reconcile.run(
        reconcile_context(
            positions=[],
            fills=[
                {"order_id": "zz99", "symbol": "F", "side": "sell", "quantity": 1.0,
                 "price": 14.00, "cost_basis": 15.00}
            ],
            tax_lots={},
        )
    )

    assert set(result.loss_closes[0]) >= {"symbol", "close_date"}


def test_a_reconciled_loss_close_blocks_the_repurchase():
    """End to end, because the shape assertion above has a default to fall
    back on and this does not: guard must actually refuse the buy."""
    ctx = context()
    ctx["loss_closes"] = closed_at_a_loss().loss_closes

    assert guard.WASH_SALE_COOLDOWN in codes(guard.evaluate([order()], ctx), "F")


def test_a_close_the_broker_reports_as_a_gain_leaves_the_name_buyable():
    """The other end of the same handover, and the bug it fixes: the live
    ledger held a 31-day ban on every name the account had closed, MU among
    them, sold at a profit. `reconcile` writes no row and `guard` therefore has
    nothing to deny on — the two halves have to agree for that to be true."""
    closed = reconcile.run(
        reconcile_context(
            positions=[],
            fills=[
                {"order_id": "zz99", "symbol": "F", "side": "sell",
                 "quantity": 1.0, "price": 16.00, "cost_basis": None}
            ],
            tax_lots={},
            realized=[
                {"order_id": "zz99", "symbol": "F", "date": "2026-08-03",
                 "realized_gain": 5.00}
            ],
        )
    )
    ctx = context()
    ctx["loss_closes"] = closed.loss_closes

    assert closed.loss_closes == []
    assert guard.WASH_SALE_COOLDOWN not in codes(guard.evaluate([order()], ctx), "F")


def test_a_reconciled_gain_close_leaves_the_repurchase_open():
    """§2.5: winners create no state. If they did, every profitable exit would
    fence the system out of its own best name for a month."""
    ctx = context()
    ctx["loss_closes"] = closed_at_a_loss(price=16.00).loss_closes

    assert guard.WASH_SALE_COOLDOWN not in codes(guard.evaluate([order()], ctx), "F")


# --- the shared definition of an open order --------------------------------


def test_guard_and_reconcile_agree_on_what_an_open_order_is():
    """One definition, imported, per §4.4's rule for the cap fractions.

    Guard's duplicate-order check and this module's stale sweep both partition
    `get_equity_orders` into working and finished. If they disagreed, guard
    would go on refusing the replacement sell for a symbol whose only open
    order reconcile had already cancelled — an account that silently stops
    being able to exit one name."""
    assert guard._TERMINAL_ORDER_STATES is reconcile.TERMINAL_ORDER_STATES


def test_a_cancelled_order_stops_blocking_a_second_sell():
    """The consequence, asserted through behaviour. `cancelled` is terminal to
    both modules, so the sell guard refused while the order was working is
    approved once it is not."""
    working = context()
    working["open_orders"] = [{"symbol": "F", "side": "sell", "state": "queued"}]
    done = context()
    done["open_orders"] = [{"symbol": "F", "side": "sell", "state": "cancelled"}]
    exit_order = order(side="sell", type="market", limit_price=None)

    assert guard.DUPLICATE_OPEN_ORDER in codes(guard.evaluate([exit_order], working))
    assert guard.DUPLICATE_OPEN_ORDER not in codes(guard.evaluate([exit_order], done))


# --- Invariant 2 -----------------------------------------------------------


def test_the_output_is_json():
    """"Pure function from JSON to JSON" is only true if the output survives a
    round trip — this is what `cli.py` writes to `runlog` before anything is
    placed."""
    result = reconcile.run(
        reconcile_context(
            intent={
                "placements": [
                    {"order_id": "a1b2", "symbol": "F", "side": "buy",
                     "quantity": 1.0, "play_id": "DIP"}
                ]
            },
            orders=[{"order_id": "zz99", "symbol": "GME", "side": "buy",
                     "quantity": 1.0, "state": "queued"}],
        )
    ).as_dict()

    assert json.loads(json.dumps(result)) == result
