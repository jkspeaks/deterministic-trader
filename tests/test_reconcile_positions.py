"""The book `plays.py` reads: entry basis, holding period, and the play_id.

Settled: both numbers come from the broker's tax lots plus the market
calendar, and nothing is counted. A per-cycle counter would have been one
fewer dependency, but it drifts permanently the first time a cycle is missed —
and it drifts in the unsafe direction, under-counting sessions so the time
stop fires late on every position at once.

This is the module's highest-stakes output. §6.5's time stop, ATR stop and
target all read these fields, and all three fail *silently* when they are
absent: the system reads as one that simply holds forever.
"""

from datetime import date

from src import plays, reconcile
from tests.factories import fill, found, reconcile_context, reconciled


def run(**overrides):
    return reconcile.run(reconcile_context(**overrides))


def run_without_lots(**overrides):
    """A cycle in which the broker handed over no tax lots at all.

    By assignment, because `reconcile_context` deep-merges: passing
    ``tax_lots={}`` merges an empty dict onto the baseline and leaves F's lot
    exactly where it was, so the test would pass against the very data it is
    trying to remove.
    """
    ctx = reconcile_context(**overrides)
    ctx["tax_lots"] = {}
    return reconcile.run(ctx)


def lots(*rows: tuple[float, float, str]) -> dict:
    return {
        "F": [
            {"quantity": q, "cost_basis": basis, "acquired_date": day}
            for q, basis, day in rows
        ]
    }


# --- entry basis -----------------------------------------------------------


def test_the_entry_price_is_the_tax_lot_basis():
    """§12.2 gives the adopted position its stop from `get_equity_tax_lots`.
    Invariant 7 then makes that the rule for every position, not a fallback for
    one kind — a book with two basis sources is a book where two positions in
    the same state get different stops."""
    assert reconciled(run(), "F").entry_price == 15.00


def test_several_lots_average_by_weight():
    """One position, one basis. Sizing scaled in twice and the stop belongs to
    the position, not to whichever lot the broker happens to list first."""
    result = run(
        positions=[{"symbol": "F", "quantity": 3.0}],
        prior_positions=[{"symbol": "F", "play_id": "DIP", "quantity": 3.0}],
        tax_lots=lots((1.0, 15.00, "2026-07-30"), (2.0, 36.00, "2026-07-31")),
    )

    assert reconciled(result, "F").entry_price == 17.00


def test_a_position_with_no_lot_data_says_so_rather_than_guessing():
    """The one case worth a finding that §12.2 does not name. A None basis
    disables the ATR stop and the target in `plays.py`, and it disables them
    quietly — so the absence is reported here where it is still visible."""
    result = run_without_lots()

    assert reconciled(result, "F").entry_price is None
    assert found(result, reconcile.POSITION_BASIS_UNKNOWN) == ["F"]


def test_a_missing_basis_is_not_divergence():
    """Nothing disagrees — the broker simply did not hand over a lot. It is
    reported without blocking entries, which is the §14 posture for a degraded
    input rather than a contradicted one."""
    result = run_without_lots(control={"divergence_streak": 1})

    assert result.control_updates["divergence_streak"] == 0


# --- holding period --------------------------------------------------------


def test_sessions_held_counts_open_sessions_since_acquisition():
    """The weekend is not a session, which is the whole reason this is the
    calendar's arithmetic and not a date subtraction."""
    assert reconciled(run(), "F").sessions_held == 2


def test_a_position_bought_today_has_held_no_sessions():
    """Zero, not one. §6.5's time stop is `sessions_held >= holding_days`, so
    counting the acquisition day would spend a session of every play's run
    before the position had survived a single close."""
    result = run(tax_lots=lots((1.0, 15.00, "2026-08-03")))

    assert reconciled(result, "F").sessions_held == 0


def test_a_market_holiday_does_not_count_as_a_session():
    """The same checked-in table `cadence.py` reads, through the same
    function. Two calendars would eventually disagree about how long a play
    has been running, and the disagreement would surface as a time stop that
    fires a day early in one place and a day late in the other."""
    result = run(
        calendar={"closed": ["2026-07-31"], "early_close": {}},
        tax_lots=lots((1.0, 15.00, "2026-07-30")),
    )

    assert reconciled(result, "F").sessions_held == 1


def test_a_half_day_is_still_a_whole_session():
    """A 13:00 close is a session that happened. `cadence.is_open` agrees, and
    it is the only opinion either module has."""
    result = run(calendar={"closed": [], "early_close": {"2026-07-31": "13:00"}})

    assert reconciled(result, "F").sessions_held == 2


def test_the_oldest_lot_sets_the_holding_period():
    """The position has been held since the first lot was bought. Averaging
    the acquisition dates, or taking the newest, would let scaling into a
    position reset its own time stop indefinitely."""
    result = run(
        positions=[{"symbol": "F", "quantity": 2.0}],
        prior_positions=[{"symbol": "F", "play_id": "DIP", "quantity": 2.0}],
        tax_lots=lots((1.0, 15.00, "2026-07-30"), (1.0, 15.00, "2026-08-03")),
    )

    assert reconciled(result, "F").sessions_held == 2


def test_a_position_with_no_lot_data_has_no_holding_period():
    assert reconciled(run_without_lots(), "F").sessions_held is None


# --- attribution -----------------------------------------------------------


def test_a_position_carries_its_play_for_its_whole_life():
    """§6: attribution survives even when the agent's reasoning does not. The
    play is remembered from the prior book, not re-derived from today's
    signals — a DIP that has since recovered is still a DIP position."""
    assert reconciled(run(), "F").play_id == "DIP"


def test_an_adopted_position_is_bound_to_the_adopted_play():
    """No trigger fires on GME here — no signals, no events, no earnings — so
    it takes `ADOPTED` and is flagged as such.

    §12.2's original answer was MANUAL, which resolves to no play at all and
    therefore to no time stop, no target and no signal exit. The operator
    reversed that premise: an inherited position is managed, and a position that
    can only be sold at a loss is not being managed."""
    result = run(
        fills=[fill(order_id="zz99", symbol="GME", quantity=2.0, price=30.00)],
        positions=[{"symbol": "F", "quantity": 1.0}, {"symbol": "GME", "quantity": 2.0}],
        tax_lots={
            "F": [
                {"quantity": 1.0, "cost_basis": 15.00, "acquired_date": "2026-07-30"}
            ],
            "GME": [
                {"quantity": 2.0, "cost_basis": 60.00, "acquired_date": "2026-08-03"}
            ],
        },
    )

    assert reconciled(result, "GME").play_id == plays.ADOPTED
    assert reconciled(result, "GME").adopted is True
    # The name the system did open is untouched by any of it.
    assert reconciled(result, "F").play_id == "DIP"
    assert reconciled(result, "F").adopted is False


def test_an_adopted_position_gets_a_stop_basis_like_any_other():
    """"Managed on identical terms from that cycle onward" is only true if the
    numbers the terms are made of are there."""
    result = run(
        fills=[fill(order_id="zz99", symbol="GME", quantity=2.0, price=30.00)],
        positions=[{"symbol": "F", "quantity": 1.0}, {"symbol": "GME", "quantity": 2.0}],
        tax_lots={
            "F": [
                {"quantity": 1.0, "cost_basis": 15.00, "acquired_date": "2026-07-30"}
            ],
            "GME": [
                {"quantity": 2.0, "cost_basis": 60.00, "acquired_date": "2026-08-03"}
            ],
        },
    )

    assert reconciled(result, "GME").entry_price == 30.00
    assert reconciled(result, "GME").sessions_held == 0


def test_a_new_position_takes_the_play_that_bought_it():
    """The placement carried a play_id into the runlog; the fill brings it
    back out. Without this hop a position the agent opened would be adopted as
    MANUAL on the very next cycle and lose its exits."""
    result = run(
        intent={
            "placements": [
                {
                    "order_id": "c3d4",
                    "symbol": "PFE",
                    "side": "buy",
                    "quantity": 2.0,
                    "play_id": "PEAD",
                }
            ]
        },
        orders=[{"order_id": "c3d4", "symbol": "PFE", "side": "buy",
                 "quantity": 2.0, "state": "filled"}],
        fills=[fill(order_id="c3d4", symbol="PFE", quantity=2.0, price=25.00)],
        positions=[{"symbol": "F", "quantity": 1.0}, {"symbol": "PFE", "quantity": 2.0}],
        tax_lots={
            "F": [
                {"quantity": 1.0, "cost_basis": 15.00, "acquired_date": "2026-07-30"}
            ],
            "PFE": [
                {"quantity": 2.0, "cost_basis": 50.00, "acquired_date": "2026-08-03"}
            ],
        },
    )

    assert reconciled(result, "PFE").play_id == "PEAD"


# --- the broker wins -------------------------------------------------------


def test_the_reconciled_book_is_the_brokers_book():
    """§12.2's closing rule. A mismatch is reported and the broker's quantity
    is what goes forward — repairing the difference would be this module
    deciding it knows better than the account statement."""
    result = run(positions=[{"symbol": "F", "quantity": 2.0}])

    assert reconcile.POSITION_DELTA_MISMATCH in found(result)
    assert reconciled(result, "F").quantity == 2.0


def test_a_position_the_broker_no_longer_reports_is_gone_from_the_book():
    assert reconciled(run(positions=[]), "F") is None


def test_the_book_is_ordered_by_symbol():
    """A golden file must not depend on the order `get_equity_positions`
    happened to return."""
    result = run_without_lots(
        positions=[
            {"symbol": "PFE", "quantity": 2.0},
            {"symbol": "F", "quantity": 1.0},
            {"symbol": "AAPL", "quantity": 1.0},
        ],
        prior_positions=[
            {"symbol": "F", "play_id": "DIP", "quantity": 1.0},
            {"symbol": "PFE", "play_id": "PEAD", "quantity": 2.0},
            {"symbol": "AAPL", "play_id": "CATALYST", "quantity": 1.0},
        ],
    )

    assert [p.symbol for p in result.positions] == ["AAPL", "F", "PFE"]


# --- symbol swap -------------------------------------------------------


def test_same_quantity_and_price_under_a_different_symbol_is_flagged():
    """A DIP entry intended for EQR filled under VMRK instead — same 2 shares,
    same $66.04, a different ticker. Read separately, the two ordinary
    `POSITION_DELTA_MISMATCH` rows (EQR missing, VMRK unexpected) hide the
    connection; a session had to notice it by eye and halted the whole cycle
    out of caution. This is that fingerprint reported on its own."""
    result = run(
        positions=[{"symbol": "VMRK", "quantity": 2.0, "average_buy_price": 66.04}],
        prior_positions=[
            {"symbol": "EQR", "play_id": "DIP", "quantity": 2.0, "entry_price": 66.04}
        ],
        tax_lots={
            "VMRK": [
                {"quantity": 2.0, "cost_basis": 132.08, "acquired_date": "2026-08-13"}
            ]
        },
    )

    assert found(result, reconcile.SYMBOL_SWAP_SUSPECTED) == ["VMRK"]
    assert reconcile.SYMBOL_SWAP_SUSPECTED in reconcile.DIVERGENCE_CLASSES
    # Still reported as two ordinary rows too — this adds the connection,
    # it does not replace the underlying divergence.
    assert found(result, reconcile.POSITION_DELTA_MISMATCH) == ["EQR", "VMRK"]


def test_a_different_price_is_not_flagged_as_a_swap():
    """Two names that happen to both diverge this cycle are not a swap unless
    the price matches too — otherwise every day with two unrelated divergences
    would falsely accuse them of being the same trade."""
    result = run(
        positions=[{"symbol": "VMRK", "quantity": 2.0, "average_buy_price": 40.00}],
        prior_positions=[
            {"symbol": "EQR", "play_id": "DIP", "quantity": 2.0, "entry_price": 66.04}
        ],
        tax_lots={
            "VMRK": [
                {"quantity": 2.0, "cost_basis": 80.00, "acquired_date": "2026-08-13"}
            ]
        },
    )

    assert found(result, reconcile.SYMBOL_SWAP_SUSPECTED) == []


# --- the calendar helper itself --------------------------------------------


def test_sessions_between_is_zero_for_a_lot_dated_in_the_future():
    """A clock skew between the broker and the routine must not produce a
    negative holding period, which would read as a very long one after any
    comparison against a positive limit."""
    assert (
        reconcile.sessions_between(
            date(2026, 8, 10), date(2026, 8, 3), {"closed": [], "early_close": {}}
        )
        == 0
    )
