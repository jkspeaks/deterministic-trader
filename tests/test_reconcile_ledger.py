"""The three tallies this module owes other modules, kept by two different rules.

Settled. §9's wash-sale row is written on *any* sell fill that realises a loss,
partial or full, because the IRS rule attaches to the shares sold rather than to
the position reaching zero. §6.3's `closed_trades` counts only a position going
flat, because a trade is a round trip and a position scaled out in four tranches
is not four pieces of evidence about CATALYST. §1's `realized_pnl` follows the
ledger's rule and not the counter's: money that has been realised is realised
whether or not the position went with it.

`reconcile` emits one `realized_trades` row per broker-reported close and
`edge_realized_trades_append` credits the play once per trade;
`play_state_updates` carries the counter only.

Asymmetric on purpose. Making them agree would break one: counting wash sales
on full closes only under-blocks a repurchase §9 wants blocked; counting closed
trades per fill promotes CATALYST to full size on trades that never finished;
and booking P&L only on full closes would leave a scaled-out winner reading as
a play that made nothing.
"""

from src import reconcile
from tests.factories import config, fill, realized, reconcile_context


def run(**overrides):
    return reconcile.run(reconcile_context(**overrides))


def sold(**overrides) -> dict:
    """A full exit of the baseline F position, at whatever price is passed."""
    return {
        "intent": {
            "placements": [
                {
                    "order_id": "a1b2",
                    "symbol": "F",
                    "side": "sell",
                    "quantity": 1.0,
                    "play_id": "DIP",
                }
            ]
        },
        "orders": [{"order_id": "a1b2", "symbol": "F", "side": "sell",
                    "quantity": 1.0, "state": "filled"}],
        "fills": [fill(side="sell", **overrides)],
        "positions": [],
        "tax_lots": {},
    }


# --- §9, the loss ledger ---------------------------------------------------


def test_a_close_at_a_loss_writes_a_row():
    """Proceeds 14.00 against a 15.00 basis. The row is what `guard.py` reads
    to deny a repurchase for 31 days."""
    result = run(**sold(price=14.00, cost_basis=15.00))

    assert result.loss_closes == [
        {"symbol": "F", "close_date": "2026-08-03", "loss_amount": 1.00,
         "basis_unknown": False}
    ]


def test_a_close_at_a_gain_writes_nothing():
    """Per the explicit requirement: only names closed at a loss are ever
    monitored. Winners and flat closes create no state (§2.5)."""
    assert run(**sold(price=16.00, cost_basis=15.00)).loss_closes == []


def test_a_flat_close_writes_nothing():
    assert run(**sold(price=15.00, cost_basis=15.00)).loss_closes == []


def test_sub_cent_noise_is_not_a_loss():
    """The same half-cent slack `sizing.py` compares money with. A row written
    on a rounding artefact blocks a real repurchase for a month."""
    assert run(**sold(price=14.999, cost_basis=15.00)).loss_closes == []


def test_the_loss_is_computed_from_basis_and_proceeds_when_the_broker_is_silent():
    """The fallback, unchanged. It is now second in line rather than the only
    rule (see the broker section below), and it is the one that still works on
    a *partial* sale — the lot the shares came out of is still open."""
    result = run(**sold(quantity=1.0, price=12.50, cost_basis=15.00))

    assert result.loss_closes[0]["loss_amount"] == 2.50


def test_a_partial_sale_at_a_loss_still_writes_a_row():
    """Those shares were sold at a loss whatever the rest of
    the position does, and §9's own fallback says over-blocking a repurchase
    costs one trade while under-blocking costs the bookkeeping the operator
    explicitly asked to avoid."""
    result = run(
        intent={
            "placements": [
                {"order_id": "a1b2", "symbol": "F", "side": "sell",
                 "quantity": 0.4, "play_id": "DIP"}
            ]
        },
        orders=[{"order_id": "a1b2", "symbol": "F", "side": "sell",
                 "quantity": 0.4, "state": "filled"}],
        fills=[fill(side="sell", quantity=0.4, price=14.00, cost_basis=6.00)],
        positions=[{"symbol": "F", "quantity": 0.6}],
        tax_lots={
            "F": [
                {"quantity": 0.6, "cost_basis": 9.00, "acquired_date": "2026-07-30"}
            ]
        },
    )

    assert [r["symbol"] for r in result.loss_closes] == ["F"]


def test_a_close_with_no_lot_data_is_treated_as_a_loss():
    """§9's stated fallback, and the reason it is the right direction: an
    unknown result blocks one repurchase, while assuming a gain risks the
    disallowed loss the whole ledger exists to prevent.

    `basis_unknown` is what separates this row from the measured loss above."""
    result = run(**sold(price=14.00, cost_basis=None))

    assert result.loss_closes == [
        {"symbol": "F", "close_date": "2026-08-03", "loss_amount": None,
         "basis_unknown": True}
    ]


def test_an_operators_loss_close_is_ledgered_like_any_other():
    """The IRS does not care which of them pressed sell. Invariant 7 again."""
    result = run(
        fills=[fill(order_id="zz99", side="sell", price=14.00, cost_basis=15.00)],
        positions=[],
        tax_lots={},
    )

    assert [r["symbol"] for r in result.loss_closes] == ["F"]


def test_a_buy_fill_never_writes_a_ledger_row():
    result = run(
        fills=[fill(price=15.00, cost_basis=15.00)],
        positions=[{"symbol": "F", "quantity": 2.0}],
        tax_lots={
            "F": [
                {"quantity": 2.0, "cost_basis": 30.00, "acquired_date": "2026-07-30"}
            ]
        },
    )

    assert result.loss_closes == []


def test_the_close_date_is_the_session_not_the_wall_clock():
    """`guard.py` measures the 31 days against the trading day, so the row is
    stamped with the session it belongs to. A 20:00 fill belongs to the next
    session, and `cadence.trading_day` has already made that shift once."""
    result = run(
        cycle={"trading_day": "2026-08-04"},
        **sold(price=14.00, cost_basis=15.00),
    )

    assert result.loss_closes[0]["close_date"] == "2026-08-04"


# --- the broker's own realized figure ---------------------------------------
#
# The basis on a sell fill came from `get_equity_tax_lots`, which returns
# *open* lots — so a position sold to zero had nothing left to price against
# and every full close read as "unknown". The live ledger held 15 consecutive
# null rows and `edge_play_state.realized_pnl` was 0.00 across 13 closed
# trades. `get_pnl_trade_history` is the read that survives the close, so it
# goes first and the lot arithmetic stays as the fallback.
#
# The broker's response *field names* are unconfirmed (spec §2, every row
# TODO [V]); these tests are written against the internal names `cli._realized`
# produces, which is the one place a confirmed name lands.


def test_a_broker_gain_writes_no_ledger_row():
    """The fix, in one assertion. This exact fill — a full close with no open
    lot left — is what produced 15 null rows and a 31-day repurchase ban on
    names closed in profit."""
    result = run(
        realized=[realized(price=16.00, proceeds=16.00, realized_gain=5.00)],
        **sold(price=16.00, cost_basis=None),
    )

    assert result.loss_closes == []
    assert [(t["play_id"], t["realized_gain"]) for t in result.realized_trades] == [
        ("DIP", 5.00)
    ]


def test_a_broker_loss_writes_a_measured_row():
    result = run(
        realized=[realized(realized_gain=-3.00)],
        **sold(price=14.00, cost_basis=None),
    )

    assert result.loss_closes == [
        {"symbol": "F", "close_date": "2026-08-03", "loss_amount": 3.00,
         "basis_unknown": False}
    ]
    assert [t["realized_gain"] for t in result.realized_trades] == [-3.00]


def test_the_broker_wins_over_the_tax_lot_when_both_are_present():
    """One fill cannot have two answers, and the broker owns this one: the lot
    figure is a subtraction this system performs, the broker's is the number on
    the 1099-B."""
    result = run(
        realized=[realized(realized_gain=-3.00)],
        **sold(price=14.00, cost_basis=15.00),
    )

    assert result.loss_closes[0]["loss_amount"] == 3.00


def test_a_fill_with_no_broker_row_falls_back_to_the_lot():
    """Not an error. A partial sale still has its lot open, and every cycle
    before this change carries no `realized` key at all."""
    result = run(
        realized=[realized(order_id="zz99", symbol="PFE", date="2026-08-03")],
        **sold(price=14.00, cost_basis=15.00),
    )

    assert result.loss_closes[0]["loss_amount"] == 1.00


def test_a_fill_with_neither_is_still_ledgered_as_unknown():
    assert run(
        realized=[],
        **sold(price=14.00, cost_basis=None),
    ).loss_closes == [
        {"symbol": "F", "close_date": "2026-08-03", "loss_amount": None,
         "basis_unknown": True}
    ]


def test_a_broker_break_even_is_a_read_not_a_gap():
    """`realized_gain: 0.0` is an answer. `None` is a failed read. Collapsing
    them is exactly the `float(x or 0.0)` that turned a missing `equity_total`
    into a whole-book flatten."""
    result = run(
        realized=[realized(realized_gain=0.0)],
        **sold(price=15.00, cost_basis=None),
    )

    assert result.loss_closes == []
    assert [t["realized_gain"] for t in result.realized_trades] == [0.0]


def test_proceeds_minus_basis_answers_when_the_gain_field_does_not():
    """A row that carries both sides of the subtraction is still the broker's
    basis, and the broker's basis is the thing tax lots cannot supply on a
    full close."""
    result = run(
        realized=[realized(realized_gain=None, proceeds=14.00, cost_basis=15.00)],
        **sold(price=14.00, cost_basis=None),
    )

    assert result.loss_closes[0]["loss_amount"] == 1.00


def test_a_row_is_matched_by_order_id_before_symbol_and_date():
    """Two sells of one name on one day. Only the id can tell them apart, and
    a wrong attribution here lands in the scorecard permanently."""
    result = run(
        intent={"placements": []},
        orders=[],
        positions=[],
        tax_lots={},
        fills=[
            fill(order_id="zz01", side="sell", quantity=1.0, price=14.00),
            fill(order_id="zz02", side="sell", quantity=1.0, price=20.00),
        ],
        realized=[
            realized(order_id="zz01", realized_gain=-1.00),
            realized(order_id="zz02", realized_gain=5.00),
        ],
    )

    assert result.loss_closes == [
        {"symbol": "F", "close_date": "2026-08-03", "loss_amount": 1.00,
         "basis_unknown": False}
    ]


def test_two_rows_on_one_symbol_and_date_match_neither():
    """No id to disambiguate and two candidates: the fill takes the fallback
    rather than an arbitrary one of them. Ambiguity is not an answer."""
    result = run(
        realized=[
            realized(order_id=None, realized_gain=-1.00),
            realized(order_id=None, realized_gain=5.00),
        ],
        **sold(price=14.00, cost_basis=None),
    )

    assert result.loss_closes[0]["basis_unknown"] is True


def test_a_single_row_matches_on_symbol_and_date_alone():
    """The degraded path the spec allows for, in case a live response carries
    no order id at all."""
    result = run(
        realized=[realized(order_id=None, realized_gain=5.00)],
        **sold(price=16.00, cost_basis=None),
    )

    assert result.loss_closes == []
    assert [t["realized_gain"] for t in result.realized_trades] == [5.00]


# --- the `realized` backfill pass -------------------
#
# `realized` is one dedicated broker call (`get_pnl_trade_history`) that
# reports the same closing trade regardless of what `fills` carries, so
# `_loss_closes` now walks it as a second, independent pass rather than only as
# a lookup the `fills` pass can fail to reach.


def test_a_loss_missing_from_fills_is_still_ledgered_from_realized():
    """The exact shape of the confirmed defect: the session's hand-built
    `fills` omits the sell entirely (not merely its cost basis), and the
    ledger must not depend on `fills` having remembered it."""
    result = run(
        intent={"placements": []},
        orders=[],
        fills=[],
        positions=[],
        tax_lots={},
        realized=[realized(order_id="zz77", symbol="INVH", realized_gain=-6.76)],
    )

    assert result.loss_closes == [
        {"symbol": "INVH", "close_date": "2026-08-03", "loss_amount": 6.76,
         "basis_unknown": False}
    ]


def test_a_gain_missing_from_fills_still_writes_nothing():
    result = run(
        intent={"placements": []},
        orders=[],
        fills=[],
        positions=[],
        tax_lots={},
        realized=[realized(order_id="zz77", symbol="INVH", realized_gain=4.00)],
    )

    assert result.loss_closes == []


def test_the_same_close_seen_in_both_fills_and_realized_writes_one_row():
    """The normal case — a fill the session *did* remember to paste, whose
    broker-reported figure the `fills` pass already matches by order id. The
    `realized` pass must not turn one close into two ledger rows."""
    result = run(
        realized=[realized(realized_gain=-3.00)],
        **sold(price=14.00, cost_basis=None),
    )

    assert result.loss_closes == [
        {"symbol": "F", "close_date": "2026-08-03", "loss_amount": 3.00,
         "basis_unknown": False}
    ]


def test_a_realized_row_with_no_gain_field_is_left_to_the_fills_pass():
    """A `realized` row with neither `realized_gain` nor `proceeds`/
    `cost_basis` cannot price a close on its own, and — unlike a `fills` row —
    it never falls back to `basis_unknown`: that fallback belongs to a fill
    this system can actually see. If the same close is also in `fills`, that
    pass still owns it."""
    result = run(
        realized=[
            {"order_id": "zz88", "symbol": "PFE", "quantity": 1.0,
             "price": 14.00, "date": "2026-08-03"}
        ],
        **sold(price=14.00, cost_basis=15.00),
    )

    assert result.loss_closes == [
        {"symbol": "F", "close_date": "2026-08-03", "loss_amount": 1.00,
         "basis_unknown": False}
    ]


def test_mgm_and_invh_both_ledger_from_the_audits_own_figures():
    """Both must reach the ledger from one reconcile call."""
    result = run(
        cycle={"trading_day": "2026-09-16"},
        intent={"placements": []},
        orders=[],
        positions=[],
        tax_lots={},
        fills=[
            fill(order_id="mgm-1", symbol="MGM", side="sell",
                 quantity=6.0, price=39.19, cost_basis=None),
        ],
        realized=[
            realized(order_id="mgm-1", symbol="MGM", realized_gain=-10.12,
                     date="2026-09-15", traded_at="2026-09-15T19:55:33Z"),
            realized(order_id="invh-1", symbol="INVH", realized_gain=-6.76,
                     date="2026-09-16", traded_at="2026-09-16T19:21:43Z"),
        ],
    )

    # Each close is dated by its own trade, not by the cycle that read it —
    # MGM's broker timestamp is the 15th, and the `fills` pass takes the
    # same date from the row it matched, so the two passes still collapse
    # to one row.
    assert {(r["symbol"], r["close_date"], r["loss_amount"], r["basis_unknown"])
            for r in result.loss_closes} == {
        ("MGM", "2026-09-15", 10.12, False),
        ("INVH", "2026-09-16", 6.76, False),
    }


def test_a_close_re_offered_days_later_keeps_its_own_date():
    """A loss read again by a cycle five days later because `realized`'s
    window is a week. It wrote the same loss again under the later date — a duplicate that
    missed the ledger's key and pushed the wash-sale window out five days."""
    result = run(
        cycle={"trading_day": "2026-09-21"},
        intent={"placements": []},
        orders=[],
        fills=[],
        positions=[],
        tax_lots={},
        realized=[
            realized(symbol="INVH", realized_gain=-6.76, date="2026-09-16",
                     traded_at="2026-09-16T19:21:43Z"),
        ],
    )

    assert result.loss_closes == [
        {"symbol": "INVH", "close_date": "2026-09-16", "loss_amount": 6.76,
         "basis_unknown": False}
    ]


# --- the wash-sale predicate both readers share ------------------------------


def test_a_measured_loss_always_blocks():
    row = {"symbol": "F", "close_date": "2026-08-03", "loss_amount": 1.00,
           "basis_unknown": False}

    assert reconcile.wash_sale_blocks(row, config())
    assert reconcile.wash_sale_blocks(
        row, config(wash_sale_block_unknown_basis=False)
    )


def test_an_unknown_basis_blocks_by_default_and_not_when_switched_off():
    row = {"symbol": "F", "close_date": "2026-08-03", "loss_amount": None,
           "basis_unknown": True}

    assert reconcile.wash_sale_blocks(row, config())
    assert not reconcile.wash_sale_blocks(
        row, config(wash_sale_block_unknown_basis=False)
    )


def test_a_historical_row_with_no_flag_reads_as_unknown():
    """Rows written by an older schema read back `basis_unknown: null`. They keep
    behaving exactly as they do today rather than silently unblocking."""
    row = {"symbol": "F", "close_date": "2026-08-03", "loss_amount": None}

    assert reconcile.wash_sale_blocks(row, config())
    assert not reconcile.wash_sale_blocks(
        row, config(wash_sale_block_unknown_basis=False)
    )


# --- §6.3, the closed-trade counter ----------------------------------------


def test_a_position_going_flat_counts_one_closed_trade():
    result = run(**sold(price=16.00, cost_basis=15.00))

    assert result.play_state_updates == {"DIP": {"closed_trades": 5}}


def test_a_partial_exit_counts_nothing():
    """A trade is a round trip. Counting tranches would let one position
    scaled out in four pieces read as four closed trades, and §6.3's 0.75x
    would lift on a quarter of the evidence it asks for.

    The money it realised is credited all the same — see the P&L section
    below. This asserts only that the *counter* stayed where it was, which
    means no play-state write at all."""
    result = run(
        intent={
            "placements": [
                {"order_id": "a1b2", "symbol": "F", "side": "sell",
                 "quantity": 0.4, "play_id": "DIP"}
            ]
        },
        orders=[{"order_id": "a1b2", "symbol": "F", "side": "sell",
                 "quantity": 0.4, "state": "filled"}],
        fills=[fill(side="sell", quantity=0.4, price=16.00, cost_basis=6.00)],
        positions=[{"symbol": "F", "quantity": 0.6}],
        tax_lots={
            "F": [
                {"quantity": 0.6, "cost_basis": 9.00, "acquired_date": "2026-07-30"}
            ]
        },
    )

    assert "DIP" not in result.play_state_updates


def test_a_manual_close_counts_toward_no_play():
    """An adopted position proves nothing about CATALYST's edge, or about any
    other play's. It has no play to credit, and inventing one would be the
    system taking attribution for a trade it did not choose."""
    result = run(
        prior_positions=[{"symbol": "F", "play_id": "MANUAL", "quantity": 1.0}],
        **sold(price=16.00, cost_basis=15.00),
    )

    assert result.play_state_updates == {}


def test_a_catalyst_close_is_what_lifts_the_075x():
    """§6.3 sizes CATALYST at 0.75x until it has 20 closed trades, and
    `plays._notional_multiplier` reads exactly this counter. An absent count
    reads as zero there, so this module writing nothing is the smaller size."""
    result = run(
        prior_positions=[{"symbol": "F", "play_id": "CATALYST", "quantity": 1.0}],
        play_state={"CATALYST": {"closed_trades": 19}},
        **sold(price=16.00, cost_basis=15.00),
    )

    assert result.play_state_updates == {"CATALYST": {"closed_trades": 20}}


def test_a_play_with_no_prior_count_starts_at_one():
    result = run(
        prior_positions=[{"symbol": "F", "play_id": "PEAD", "quantity": 1.0}],
        play_state={},
        **sold(price=16.00, cost_basis=15.00),
    )

    assert result.play_state_updates == {"PEAD": {"closed_trades": 1}}


def test_two_positions_of_one_play_closing_together_count_twice():
    result = run(
        positions=[],
        prior_positions=[
            {"symbol": "F", "play_id": "DIP", "quantity": 1.0},
            {"symbol": "PFE", "play_id": "DIP", "quantity": 2.0},
        ],
        fills=[
            fill(order_id="zz01", side="sell", price=16.00, cost_basis=15.00),
            fill(order_id="zz02", symbol="PFE", side="sell", quantity=2.0,
                 price=26.00, cost_basis=50.00),
        ],
        tax_lots={},
    )

    assert result.play_state_updates == {"DIP": {"closed_trades": 6}}


def test_a_cycle_that_closes_nothing_writes_no_play_state():
    """An update per cycle would rewrite `edge_play_state` on every one of the
    day's eleven invocations to say the same number."""
    assert run().play_state_updates == {}


# --- §1, the money each play actually made ---------------------------------
#
# Settled: `closed_trades` answers §6.3's "is CATALYST proven yet";
# `realized_pnl` answers §1's "which play had a bad month". It follows the
# *ledger's* rule — realised money is realised whether or not the position
# went flat.
#
# It was credited here from `ctx["fills"]`, which the runner never sends, so
# it read 0.0 for every play in production. It now comes from the broker's
# own per-trade read: one `realized_trades` row per close, credited once by
# `edge_realized_trades_append`, which keys each on `(symbol,
# traded_at, quantity)` because `realized` re-offers the same trade on every
# cycle for a week.


def trades(result):
    return [
        (t["symbol"], t["play_id"], t["realized_gain"]) for t in result.realized_trades
    ]


def test_a_closed_trade_credits_its_play_with_the_money():
    result = run(realized=[realized(realized_gain=1.00)], **sold(price=16.00))

    assert trades(result) == [("F", "DIP", 1.00)]


def test_a_loss_is_credited_as_a_negative():
    result = run(realized=[realized(realized_gain=-1.25)], **sold(price=13.75))

    assert trades(result) == [("F", "DIP", -1.25)]


def test_play_state_leaves_the_stored_money_alone():
    """Accumulation is the RPC's now. `edge_play_state_apply` coalesces an
    absent key to the stored figure, so emitting none is what keeps it."""
    result = run(
        play_state={"DIP": {"closed_trades": 4, "realized_pnl": 10.00}},
        realized=[realized(realized_gain=1.00)],
        **sold(price=16.00),
    )

    assert "realized_pnl" not in result.play_state_updates["DIP"]


def test_a_partial_exit_credits_the_money_even_though_it_counts_no_trade():
    """The asymmetry this file is about. 0.4 shares realised forty cents that
    exist regardless of what the remaining 0.6 does."""
    result = run(
        intent={
            "placements": [
                {"order_id": "a1b2", "symbol": "F", "side": "sell",
                 "quantity": 0.4, "play_id": "DIP"}
            ]
        },
        orders=[{"order_id": "a1b2", "symbol": "F", "side": "sell",
                 "quantity": 0.4, "state": "filled"}],
        fills=[fill(side="sell", quantity=0.4, price=16.00, cost_basis=6.00)],
        realized=[realized(quantity=0.4, price=16.00, realized_gain=0.40)],
        positions=[{"symbol": "F", "quantity": 0.6}],
        tax_lots={
            "F": [
                {"quantity": 0.6, "cost_basis": 9.00, "acquired_date": "2026-07-30"}
            ]
        },
    )

    assert trades(result) == [("F", "DIP", 0.40)]
    assert result.play_state_updates == {}


def test_an_unknown_gain_credits_nothing_rather_than_guessing():
    """No broker figure, no row: a number assumed here would be wrong in the
    scorecard forever. The loss ledger still carries the close as unknown."""
    result = run(realized=[], **sold(price=14.00, cost_basis=None))

    assert result.realized_trades == []
    assert result.loss_closes[0]["basis_unknown"] is True


def test_a_row_with_no_instant_is_not_credited():
    """Without `traded_at` there is no key, and a row with no key would be
    credited again by every cycle the broker's week-wide window re-offers it."""
    result = run(realized=[realized(traded_at="2026-08-03")], **sold(price=14.00))

    assert result.realized_trades == []


def test_the_operators_own_trade_is_stored_but_names_manual():
    """Same rule as the counter: the RPC credits no play for a MANUAL row. The
    trade is still recorded — annotate, never drop."""
    result = run(
        prior_positions=[{"symbol": "F", "play_id": "MANUAL", "quantity": 1.0}],
        realized=[realized(realized_gain=1.00)],
        **sold(price=16.00),
    )

    assert trades(result) == [("F", "MANUAL", 1.00)]
    assert result.play_state_updates == {}


def test_a_buy_credits_nothing():
    """Nothing is realised on the way in."""
    result = run(realized=[realized(side="buy", realized_gain=0.0)])

    assert result.realized_trades == []


def test_a_symbol_no_book_held_is_stored_with_no_play():
    result = run(
        prior_positions=[],
        realized=[realized(symbol="SLB", realized_gain=1.48)],
    )

    assert trades(result) == [("SLB", None, 1.48)]


# --- the near-flat annotation ----------------------------------


def near_flat(**row):
    result = run(realized=[realized(**row)])
    return result.realized_trades[0]["near_flat"]


def test_mgms_eight_cent_exit_is_near_flat():
    """4 x 38.92 = 155.68 proceeds, 155.60 basis: $0.08 and 0.051%."""
    assert near_flat(symbol="MGM", quantity=4.0, price=38.92, realized_gain=0.08)


def test_a_three_dollar_winner_is_not_near_flat():
    """VMRK, 2 x 67.74, +$3.40."""
    assert not near_flat(symbol="VMRK", quantity=2.0, price=67.74, realized_gain=3.40)


def test_small_dollars_on_a_small_basis_is_not_near_flat():
    """$0.50 on ~$20 is 2.5%: small in dollars only because the position was.
    Either test alone would flag it; the rule needs both."""
    assert not near_flat(quantity=1.0, price=20.50, realized_gain=0.50)


def test_the_thresholds_come_from_config():
    result = run(
        config=config(near_flat_max_dollars=5.00, near_flat_max_pct=0.10),
        realized=[realized(quantity=2.0, price=67.74, realized_gain=3.40)],
    )

    assert result.realized_trades[0]["near_flat"] is True


def test_an_exit_off_a_disputed_entry_is_marked():
    result = run(
        prior_positions=[{"symbol": "F", "play_id": "DIP", "quantity": 1.0,
                          "entry_price_disputed": True}],
        realized=[realized(realized_gain=0.08)],
        **sold(price=15.08),
    )

    assert result.realized_trades[0]["entry_disputed"] is True
