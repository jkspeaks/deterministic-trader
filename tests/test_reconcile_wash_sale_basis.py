"""A wash-sale-adjusted tax lot is not a disputed entry.

The trade this file exists for: INVH sold 8 shares at a $6.76 loss and bought 7
back at 27.15 on 09-18. The broker rolled the disallowed loss into the new lot
— `get_equity_tax_lots` read `tax_cost_basis` 195.95 (27.99 a share), $5.90
over the fill and 7/8 of the loss — and `_anchored_entry` read that gap as two
feeds disagreeing. Every figure below is the broker's own, pulled (account
<your-cash-account-number>).
"""

from src import reconcile
from tests.factories import config, reconcile_context


INVH_FILL = 27.15
INVH_LOT_BASIS = 195.95      # the lot's tax_cost_basis after the adjustment
INVH_LOSS = 6.76
INVH_ATR = 0.30


def invh_cycle(*, loss_closes, lot_basis=INVH_LOT_BASIS, acquired="2026-09-18", **over):
    return reconcile_context(
        cycle={
            "cycle_type": "full",
            "session": "regular_hours",
            "now": "2026-09-22T14:30:00Z",
            "trading_day": "2026-09-22",
            "reconcile_required": True,
        },
        positions=[{"symbol": "INVH", "quantity": 7.0, "average_buy_price": INVH_FILL}],
        tax_lots={
            "INVH": [
                {"quantity": 7.0, "cost_basis": lot_basis, "acquired_date": acquired}
            ]
        },
        prior_positions=[
            {"symbol": "INVH", "play_id": "DIP", "quantity": 7.0,
             "entry_price": INVH_FILL, "entry_anchored_on": acquired,
             "entry_price_source": "carried"},
        ],
        intent={"placements": []},
        orders=[],
        fills=[],
        loss_closes=loss_closes,
        **over,
    )


def loss(symbol="INVH", close_date="2026-09-16", amount=INVH_LOSS, basis_unknown=False):
    return {"symbol": symbol, "close_date": close_date,
            "loss_amount": amount, "basis_unknown": basis_unknown}


def held(ctx):
    return next(p for p in reconcile.run(ctx).positions if p.symbol == "INVH")


def test_the_invh_lot_is_the_fill_plus_the_disallowed_loss_not_a_dispute():
    position = held(invh_cycle(loss_closes=[loss()]))

    assert position.entry_price_disputed is False
    assert position.entry_price == INVH_FILL
    assert position.entry_price_source == reconcile.ENTRY_FROM_WASH_SALE_ADJUSTED


def test_without_the_ledger_row_the_same_gap_is_still_a_dispute():
    """The hold is kept for everything the ledger does not explain."""
    position = held(invh_cycle(loss_closes=[]))

    assert position.entry_price_disputed is True


def test_an_excess_beyond_the_recorded_losses_is_still_a_dispute():
    # $0.06 more than the loss accounts for — past the $0.05 rounding allowance.
    over = 7 * INVH_FILL + INVH_LOSS + 0.06
    position = held(invh_cycle(loss_closes=[loss()], lot_basis=over))

    assert position.entry_price_disputed is True


def test_a_lot_basis_below_the_fill_is_still_a_dispute():
    """A disallowed loss only ever raises basis; a lower lot is a bad read."""
    under = 7 * INVH_FILL * 0.97
    position = held(invh_cycle(loss_closes=[loss()], lot_basis=under))

    assert position.entry_price_disputed is True


def test_a_loss_outside_the_30_day_window_does_not_count():
    position = held(invh_cycle(loss_closes=[loss(close_date="2026-08-18")]))

    assert position.entry_price_disputed is True


def test_an_unpriced_loss_and_another_symbols_loss_do_not_count():
    position = held(
        invh_cycle(
            loss_closes=[
                loss(amount=None, basis_unknown=True),
                loss(symbol="MGM", amount=10.12, close_date="2026-09-15"),
            ]
        )
    )

    assert position.entry_price_disputed is True


def test_the_mgm_basis_audit_called_wrong_was_the_same_adjustment():
    """The ledger explains it."""
    ctx = reconcile_context(
        cycle={
            "cycle_type": "full",
            "session": "regular_hours",
            "now": "2026-09-17T14:30:00Z",
            "trading_day": "2026-09-17",
            "reconcile_required": True,
        },
        positions=[{"symbol": "MGM", "quantity": 4.0, "average_buy_price": 38.8999}],
        tax_lots={
            "MGM": [{"quantity": 4.0, "cost_basis": 4 * 40.635, "acquired_date": "2026-09-16"}]
        },
        prior_positions=[
            {"symbol": "MGM", "play_id": "DIP", "quantity": 4.0, "entry_price": 38.8999,
             "entry_anchored_on": "2026-09-16", "entry_price_source": "broker_average"},
        ],
        intent={"placements": []},
        orders=[],
        fills=[],
        loss_closes=[loss(symbol="MGM", close_date="2026-09-15", amount=10.12)],
    )

    mgm = next(p for p in reconcile.run(ctx).positions if p.symbol == "MGM")

    assert mgm.entry_price_disputed is False
    assert mgm.entry_price == 38.8999


def test_invh_gets_its_resting_stop_back():
    """The acceptance criterion: once not disputed, the existing protection
    plan places the stop the position has been missing."""
    ctx = invh_cycle(
        loss_closes=[loss()],
        signals={"INVH": {"atr": INVH_ATR}},
        quotes={"INVH": {"bid": 27.40, "ask": 27.44, "last": 27.42}},
        # Pinned rather than left to the shipped default — this is
        # specifically the acceptance test for the resting stop coming back
        # once the position is no longer disputed.
        config=config(protection_rest_kind="stop"),
    )

    plan = reconcile.run(ctx).protection_plan

    placed = [o for o in plan["place"] if o["symbol"] == "INVH"]
    assert len(placed) == 1
    assert placed[0]["protective_stop"] is True
    assert placed[0]["stop_price"] < INVH_FILL
