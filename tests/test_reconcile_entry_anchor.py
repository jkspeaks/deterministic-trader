"""The entry price §6.5's stop is measured down from.

The trade this file exists for: the book carried MGM at an ``entry_price`` of
40.635 on a position whose only open lot was the four shares bought the
previous day at 38.8999. The 2xATR stop measured down from 40.635 landed at
38.90 — two cents under the position's own fill — and the position stopped out
that afternoon for +$0.08, which read as an unremarkable small win to
everything downstream.

The mechanism was **not** a blend of a just-closed lot with a fresh one, which
is what the reconciliation looked like from the outside. There was exactly one
lot (`open_lot_count: 1`) and its cost was simply
wrong, and nothing could catch that because `entry_price`, `last_fill_price`
and `high_since_entry` were all derived from that same one lot and therefore
all agreed with each other. The broker's `average_buy_price` — the average cost
of the shares *currently held*, on the same call that says the position exists
— did not agree, and is the second witness this fix is built on.

* a round trip does not inherit the closed position's basis;
* a genuine multi-fill entry still blends (which
  is the behaviour that must survive the fix);
* two feeds that disagree produce a hold and an alert, never a stop;
* and the 09-16/09-17 sequence, replayed, produces the ~37.17 stop the review
  says a correctly-anchored 2xATR would have rested at.
"""

from src import plays, reconcile
# Not `from src.plays import exits` — `plays/__init__` re-exports the *function*
# of that name over the submodule, which is the shape every other test in this
# suite reads it through too.
from src.plays.exits import ENTRY_PRICE_DISPUTED
from tests.factories import config, reconcile_context


# MGM's real figures, off the broker's own order history (account
# <your-cash-account-number>). Named rather than inlined because four tests below turn on the
# exact digits and a typo in any of them would quietly make the fix look like it worked.
LOT1_FIRST = 41.3700
LOT1_SECOND = 40.7787
LOT1_BLEND = 40.8772   # (41.3700 + 5 x 40.7787) / 6
LOT2_FILL = 38.8999
LOT2_BAD_BASIS = 40.635  # what the 09-17 tax-lot read actually carried

# The ATR the 09-17 cycle used, recovered from the runlog's own two numbers:
# the reason string says the stop was 38.90 and the entry 40.63, and
# 40.635 − 38.905 = 1.73 = 2 x 0.865.
MGM_ATR = 0.865


def book(ctx):
    return {p.symbol: p for p in reconcile.run(ctx).positions}


def mgm_cycle(*, trading_day, positions, tax_lots, prior_positions, **over):
    return reconcile_context(
        cycle={
            "cycle_type": "full",
            "session": "regular_hours",
            "now": f"{trading_day}T14:30:00Z",
            "trading_day": trading_day,
            "reconcile_required": True,
        },
        positions=positions,
        tax_lots=tax_lots,
        prior_positions=prior_positions,
        intent={"placements": []},
        orders=[],
        fills=[],
        **over,
    )


# --- 1. the round trip ------------------------------------------------------


def test_a_reopened_position_does_not_inherit_the_closed_lots_basis():
    """MGM 09-15 out, 09-16 back in. The new position's entry is its own fill.

    The prior book still carries the six shares at 40.8772 — it is last cycle's
    book and last cycle the position was open. What makes this a new position
    is not the quantity (which could also just be a partial sell) but the
    oldest open lot having moved from 09-01 to 09-16: those six shares are
    gone, and an anchor taken against them describes nothing that is still
    held.
    """
    held = book(
        mgm_cycle(
            trading_day="2026-09-16",
            positions=[
                {"symbol": "MGM", "quantity": 4.0, "average_buy_price": LOT2_FILL}
            ],
            tax_lots={
                "MGM": [
                    {"quantity": 4.0, "cost_basis": 4 * LOT2_FILL,
                     "acquired_date": "2026-09-16"}
                ]
            },
            prior_positions=[
                {"symbol": "MGM", "play_id": "DIP", "quantity": 6.0,
                 "entry_price": LOT1_BLEND, "entry_anchored_on": "2026-09-01",
                 "entry_price_source": "broker_average"},
            ],
        )
    )["MGM"]

    assert held.entry_price == LOT2_FILL
    assert held.entry_anchored_on == "2026-09-16"
    assert held.entry_price_source == reconcile.ENTRY_FROM_BROKER_AVERAGE
    assert held.entry_price_disputed is False


def test_a_same_day_close_and_reopen_is_still_a_round_trip():
    """The quantity fell *and* the position is new — the hard case.

    Six shares out and four back in on one trading day looks exactly like a
    partial sell from the quantity alone, and a partial sell must keep its
    anchor. The oldest open lot is what tells the two apart, and it is the only
    thing that does.
    """
    held = book(
        mgm_cycle(
            trading_day="2026-09-16",
            positions=[
                {"symbol": "MGM", "quantity": 4.0, "average_buy_price": LOT2_FILL}
            ],
            tax_lots={
                "MGM": [
                    {"quantity": 4.0, "cost_basis": 4 * LOT2_FILL,
                     "acquired_date": "2026-09-16"}
                ]
            },
            prior_positions=[
                {"symbol": "MGM", "play_id": "DIP", "quantity": 6.0,
                 "entry_price": LOT1_BLEND, "entry_anchored_on": "2026-09-01"},
            ],
        )
    )["MGM"]

    assert held.entry_price == LOT2_FILL


def test_a_partial_sell_keeps_the_anchor_it_already_had():
    """The mirror image: fewer shares, same lot, same position, same entry.

    `average_buy_price` would have given the same answer here — the broker does
    not move it on a sell — but the anchor is carried rather than retaken, and
    this is the test that says so.
    """
    held = book(
        mgm_cycle(
            trading_day="2026-09-09",
            positions=[
                {"symbol": "MGM", "quantity": 3.0, "average_buy_price": LOT1_BLEND}
            ],
            tax_lots={
                "MGM": [
                    {"quantity": 3.0, "cost_basis": 3 * LOT1_BLEND,
                     "acquired_date": "2026-09-01"}
                ]
            },
            prior_positions=[
                {"symbol": "MGM", "play_id": "DIP", "quantity": 6.0,
                 "entry_price": LOT1_BLEND, "entry_anchored_on": "2026-09-01"},
            ],
        )
    )["MGM"]

    assert held.entry_price == LOT1_BLEND
    assert held.entry_price_source == reconcile.ENTRY_FROM_CARRY


# --- 2. the legitimate blend ------------------------------------------------


def test_a_second_fill_into_a_still_open_position_still_blends():
    """MGM 09-01 (1 share) then 09-08 (5 more). One position, one weighted entry.

    This is the behaviour the fix must not break, and the reason the anchor is
    retaken on an *add* rather than frozen at the first fill: these six shares
    were never closed, the second buy is the play scaling into the same thesis,
    and 40.8772 is the honest entry for the position as it now stands.
    """
    held = book(
        mgm_cycle(
            trading_day="2026-09-08",
            positions=[
                {"symbol": "MGM", "quantity": 6.0, "average_buy_price": LOT1_BLEND}
            ],
            tax_lots={
                "MGM": [
                    {"quantity": 1.0, "cost_basis": LOT1_FIRST,
                     "acquired_date": "2026-09-01"},
                    {"quantity": 5.0, "cost_basis": 5 * LOT1_SECOND,
                     "acquired_date": "2026-09-08"},
                ]
            },
            prior_positions=[
                {"symbol": "MGM", "play_id": "DIP", "quantity": 1.0,
                 "entry_price": LOT1_FIRST, "entry_anchored_on": "2026-09-01"},
            ],
        )
    )["MGM"]

    assert held.entry_price == LOT1_BLEND
    assert held.entry_price_source == reconcile.ENTRY_FROM_BROKER_AVERAGE
    assert held.entry_price_disputed is False
    # The clock does not restart on an average-in, and never has.
    assert held.entry_anchored_on == "2026-09-01"


def test_a_book_with_no_broker_average_still_reads_the_tax_lots():
    """The tax-lot basis is still the answer — it is one witness rather than none,
    which is not a dispute."""
    held = book(reconcile_context())["F"]

    assert held.entry_price == 15.00
    assert held.entry_price_source == reconcile.ENTRY_FROM_TAX_LOTS
    assert held.entry_price_disputed is False


# --- 3. the two feeds disagreeing -------------------------------------------


def disputed_cycle():
    """09-17: the tax-lot feed has gone bad on a position nothing else changed.

    Four shares, still the 09-16 lot, quantity unchanged — but the lot now
    reports a 162.54 basis (40.635 a share) against the 38.8999 the broker
    still reports as the position's average cost.
    """
    return mgm_cycle(
        trading_day="2026-09-17",
        positions=[
            {"symbol": "MGM", "quantity": 4.0, "average_buy_price": LOT2_FILL}
        ],
        tax_lots={
            "MGM": [
                {"quantity": 4.0, "cost_basis": 4 * LOT2_BAD_BASIS,
                 "acquired_date": "2026-09-16"}
            ]
        },
        prior_positions=[
            {"symbol": "MGM", "play_id": "DIP", "quantity": 4.0,
             "entry_price": LOT2_FILL, "entry_anchored_on": "2026-09-16",
             "entry_price_source": "broker_average"},
        ],
        signals={"MGM": {"atr": MGM_ATR}},
        quotes={"MGM": {"bid": 38.68, "ask": 38.70, "last": 38.69}},
    )


def test_a_lagging_tax_lot_feed_is_caught_rather_than_trusted():
    result = reconcile.run(disputed_cycle())
    held = next(p for p in result.positions if p.symbol == "MGM")

    assert held.entry_price_disputed is True
    # The anchor it holds is still the right one — the dispute is about
    # trusting it enough to act, not about which number is better.
    assert held.entry_price == LOT2_FILL
    assert held.entry_price_source == reconcile.ENTRY_FROM_CARRY

    finding = next(
        f for f in result.findings if f.code == reconcile.POSITION_ENTRY_DISPUTED
    )
    assert finding.symbol == "MGM"
    assert "40.6350" in finding.detail


def test_a_disputed_position_gets_no_exit_and_an_alert_instead():
    """38.69 is through the 38.90 stop the bad basis implies — this is the
    exact moment the real MGM sell was proposed. Nothing is proposed now."""
    ctx = disputed_cycle()
    result = reconcile.run(ctx)
    leaving = plays.exits(
        {**ctx, "positions": [p.as_dict() for p in result.positions]}
    )

    assert [e.symbol for e in leaving.exits] == []
    assert [(m.symbol, m.code) for m in leaving.manual_required] == [
        ("MGM", ENTRY_PRICE_DISPUTED)
    ]


def test_a_disputed_position_neither_gains_nor_loses_a_resting_stop():
    """The half of the hold that is easy to get backwards.

    A stop is already resting at 37.22 (the real one). The position is dropped
    from the protection diff — which, done naively, is indistinguishable from
    "nothing wants this order any more" and would cancel it, stripping the
    protection off the one position whose data is least trustworthy. Nothing is
    placed, nothing is replaced, nothing is cancelled.
    """
    ctx = disputed_cycle()
    ctx["orders"] = [
        {"order_id": "s-mgm", "symbol": "MGM", "side": "sell",
         "type": "stop_market", "quantity": 4.0, "stop_price": 37.22,
         "time_in_force": "gtc", "state": "confirmed", "placed_agent": "agentic"}
    ]

    plan = reconcile.run(ctx).protection_plan

    assert plan["place"] == []
    assert plan["replace"] == []
    assert plan["keep"] == []
    assert any("entry price disputed" in note for note in plan["notes"])


def test_a_divergence_inside_the_tolerance_is_not_a_dispute():
    """A check that fired on this would hold every position the account holds."""
    held = book(
        mgm_cycle(
            trading_day="2026-09-18",
            positions=[
                {"symbol": "TROW", "quantity": 2.0, "average_buy_price": 103.20}
            ],
            tax_lots={
                "TROW": [
                    {"quantity": 1.0, "cost_basis": 104.14,
                     "acquired_date": "2026-09-16"},
                    {"quantity": 1.0, "cost_basis": 102.25,
                     "acquired_date": "2026-09-16"},
                ]
            },
            prior_positions=[],
        )
    )["TROW"]

    assert held.entry_price_disputed is False
    assert held.entry_price == 103.20


def test_the_tolerance_comes_from_config():
    ctx = disputed_cycle()
    ctx["config"] = config(entry_price_divergence_pct=0.10)

    held = next(p for p in reconcile.run(ctx).positions if p.symbol == "MGM")

    assert held.entry_price_disputed is False


# --- 4. the replay ----------------------------------------------------------


def test_the_mgm_round_trip_replayed_rests_its_stop_at_36_30():
    """The acceptance criterion, run end to end over both real cycles.

    09-16 opens the position and anchors it at its own 38.8999 fill; 09-17
    carries that anchor through a tax-lot feed that has gone to 40.635. The
    3xATR stop the fix produces is 38.8999 − 3 x 0.865 = 36.30, which is the
    number a correctly-anchored stop rests at under the current tuning — and
    38.69, the price that triggered the real sale, is nowhere near it.

    The 09-17 position is also disputed, so nothing places that stop this
    cycle; the one already resting at 37.22 from 09-16 stays where it is. Both
    facts are the same fix and both are asserted here, because a stop computed
    correctly and then placed off disputed data would pass half this test.
    """
    day_one = mgm_cycle(
        trading_day="2026-09-16",
        positions=[
            {"symbol": "MGM", "quantity": 4.0, "average_buy_price": LOT2_FILL}
        ],
        tax_lots={
            "MGM": [
                {"quantity": 4.0, "cost_basis": 4 * LOT2_FILL,
                 "acquired_date": "2026-09-16"}
            ]
        },
        prior_positions=[
            {"symbol": "MGM", "play_id": "DIP", "quantity": 6.0,
             "entry_price": LOT1_BLEND, "entry_anchored_on": "2026-09-01"},
        ],
        signals={"MGM": {"atr": MGM_ATR}},
        quotes={"MGM": {"bid": 38.88, "ask": 38.92, "last": 38.90}},
    )
    opened = next(p for p in reconcile.run(day_one).positions if p.symbol == "MGM")

    assert opened.entry_price == LOT2_FILL
    assert plays.protection.stop_price(
        {**opened.as_dict(), "play_id": "DIP"}, day_one
    ) == 36.30

    # 09-17. The anchor is carried into a cycle whose lots have gone wrong.
    day_two = disputed_cycle()
    day_two["prior_positions"] = [opened.as_dict()]
    carried = next(p for p in reconcile.run(day_two).positions if p.symbol == "MGM")

    assert carried.entry_price == LOT2_FILL
    assert carried.entry_price_source == reconcile.ENTRY_FROM_CARRY

    # What the stop *is*, on the anchor the fix keeps — and what the old code
    # produced from the same lots, which is the stop that actually fired.
    undisputed = {**carried.as_dict(), "entry_price_disputed": False}
    assert plays.protection.stop_price(undisputed, day_two) == 36.30

    old_stop = LOT2_BAD_BASIS - 2 * MGM_ATR
    assert abs(old_stop - LOT2_FILL) < 0.02
    assert LOT2_FILL - 36.30 > 1.70

    # And with the flag as reconcile actually set it, no stop is computed at all.
    assert plays.protection.stop_price(carried.as_dict(), day_two) is None


def test_the_protection_plan_rests_the_corrected_stop_at_the_broker():
    """The same 36.30, as an order rather than as a number.

    `protection.stop_price` and `exits`' item 4 read one entry price by design
    ("the stop this sells through, the stop resting at the broker and the stop
    the size was measured against are one number"), so anchoring it once fixes
    all three — but the resting order is the one that has to be right while
    nobody is watching, and this is the test that walks the whole path to it.
    """
    ctx = mgm_cycle(
        trading_day="2026-09-16",
        positions=[
            {"symbol": "MGM", "quantity": 4.0, "average_buy_price": LOT2_FILL}
        ],
        tax_lots={
            "MGM": [
                {"quantity": 4.0, "cost_basis": 4 * LOT2_FILL,
                 "acquired_date": "2026-09-16"}
            ]
        },
        prior_positions=[
            {"symbol": "MGM", "play_id": "DIP", "quantity": 6.0,
             "entry_price": LOT1_BLEND, "entry_anchored_on": "2026-09-01"},
        ],
        signals={"MGM": {"atr": MGM_ATR}},
        quotes={"MGM": {"bid": 38.88, "ask": 38.92, "last": 38.90}},
        # Pinned rather than left to the shipped default — this test is
        # specifically about the resting order carrying the anchored stop
        # price.
        config=config(protection_rest_kind="stop"),
    )

    plan = reconcile.run(ctx).protection_plan

    placed = [o for o in plan["place"] if o["symbol"] == "MGM"]
    assert len(placed) == 1
    assert placed[0]["stop_price"] == 36.30
    assert placed[0]["protective_stop"] is True


def test_an_atr_stop_reason_names_the_source_of_its_entry_price():
    """What an earlier runlog row could not tell anyone: where 40.63 came from.
    Establishing that took a source trace of four modules; it is now one word
    in the line itself."""
    ctx = reconcile_context(
        signals={"F": {"atr": 0.45}},
        quotes={"F": {"bid": 13.00, "ask": 13.02, "last": 13.01}},
    )
    result = reconcile.run(ctx)
    leaving = plays.exits(
        {**ctx, "positions": [p.as_dict() for p in result.positions]}
    )

    exit_row = next(e for e in leaving.exits if e.code == plays.ATR_STOP)
    assert "3xATR from a 15.00 entry, tax_lots" in exit_row.reason
