"""Four cycles recorded whole (§15).

§15 asks for one of these by name: "an `UNTRACKED_FILL` case asserting silent
adoption with `play_id: MANUAL` and a stop derived from tax-lot basis". The
adoption is still silent and the stop still comes from the tax lot; the play id
is `ADOPTED`, because the operator's ruling made an inherited position one the
system manages rather than one it merely records. The other three are the
states that actually recur.

* `quiet_cycle` — the broker and the book agree. The shape of ten of the
  eleven invocations on a normal day.
* `untracked_fill` — §15's case. The operator bought GME in the app.
* `agent_round_trip` — a plan placed, filled, and a losing position closed:
  the ledger row, the closed-trade counter and the book all move at once.
* `divergence_escalates` — a phantom order, a position that does not add up,
  and a prior cycle that already diverged. Everything that can block entries,
  blocking at once.
"""

import json
from pathlib import Path

import pytest

from src import plays, reconcile
from tests.factories import config, reconcile_context

GOLDEN = Path(__file__).parent / "golden" / "reconcile"

# Recorded while `protection_rest_kind: "stop"` was the shipped default
# ; pinned here explicitly now that the
# default is `"none"` (protection.py's NONE), so these four golden files keep
# exercising the §10.1 resting-stop mechanics they were written to record
# rather than silently starting to test "nothing rests" instead. See
# `tests/test_reconcile_protection.py::test_none_places_nothing_for_an_unprotected_position`
# for what "none" itself looks like.
_STOP_RESTS = config(protection_rest_kind="stop")

SCENARIOS = {
    "quiet_cycle": reconcile_context(config=_STOP_RESTS),
    "untracked_fill": reconcile_context(
        config=_STOP_RESTS,
        fills=[
            {"order_id": "zz99", "symbol": "GME", "side": "buy", "quantity": 2.0,
             "price": 30.00, "cost_basis": None}
        ],
        positions=[
            {"symbol": "F", "quantity": 1.0},
            {"symbol": "GME", "quantity": 2.0},
        ],
        tax_lots={
            "F": [
                {"quantity": 1.0, "cost_basis": 15.00, "acquired_date": "2026-07-30"}
            ],
            "GME": [
                {"quantity": 2.0, "cost_basis": 61.40, "acquired_date": "2026-08-03"}
            ],
        },
    ),
    "agent_round_trip": reconcile_context(
        config=_STOP_RESTS,
        intent={
            "placements": [
                {"order_id": "a1b2", "symbol": "F", "side": "sell",
                 "quantity": 1.0, "play_id": "DIP"},
                {"order_id": "c3d4", "symbol": "PFE", "side": "buy",
                 "quantity": 2.0, "play_id": "PEAD"},
            ]
        },
        orders=[
            {"order_id": "a1b2", "symbol": "F", "side": "sell",
             "quantity": 1.0, "state": "filled"},
            {"order_id": "c3d4", "symbol": "PFE", "side": "buy",
             "quantity": 2.0, "state": "filled"},
        ],
        fills=[
            {"order_id": "a1b2", "symbol": "F", "side": "sell", "quantity": 1.0,
             "price": 13.75, "cost_basis": 15.00},
            {"order_id": "c3d4", "symbol": "PFE", "side": "buy", "quantity": 2.0,
             "price": 25.03, "cost_basis": None},
        ],
        positions=[{"symbol": "PFE", "quantity": 2.0}],
        tax_lots={
            "PFE": [
                {"quantity": 2.0, "cost_basis": 50.06, "acquired_date": "2026-08-03"}
            ]
        },
        account={"equity_total": 249.10},
    ),
    "divergence_escalates": reconcile_context(
        config=_STOP_RESTS,
        intent={
            "placements": [
                {"order_id": "a1b2", "symbol": "F", "side": "buy",
                 "quantity": 1.0, "play_id": "DIP"}
            ]
        },
        orders=[
            {"order_id": "a1b2", "symbol": "F", "side": "buy",
             "quantity": 1.0, "state": "queued"},
            {"order_id": "zz99", "symbol": "MRK", "side": "buy",
             "quantity": 1.0, "state": "queued"},
        ],
        positions=[{"symbol": "F", "quantity": 1.6}],
        control={"divergence_streak": 1, "open_order_cycles": {"a1b2": 1}},
        events={"F": [{"observed_at": "2026-08-03T12:00:00Z"}]},
        account={"equity_total": 180.00},
    ),
    # §10.1's resting orders, all three outcomes at once. This is the only
    # reconcile fixture carrying `signals`/`quotes`, which is what makes it the
    # only one that can compute a stop at all — the other four record the
    # missing-ATR note instead, honestly and uselessly.
    #
    # F has run to 21.00 from a 15.00 entry, so its stop trails at 21.00 − 3.5
    # × 0.45 = 19.43 and the resting one at 14.10 is stale: **replace**. GME's
    # resting stop is already at the 38.50 its own high asks for (same as the
    # fixture's other back-solved ATRs see `tests/test_broker_contract.py`'s
    # SIGNALS comment): **keep**. PFE has nothing resting at all: **place**.
    #
    # It is now a **cancel with nothing to replace it**: only one leg rests
    # (`protection_rest_kind`), because the broker reserves the whole position
    # against the first resting sell and the target is what would refuse the
    # stop.
    "protected_book": reconcile_context(
        config=_STOP_RESTS,
        positions=[{"symbol": "F", "quantity": 1.0},
                   {"symbol": "GME", "quantity": 1.0},
                   {"symbol": "PFE", "quantity": 2.0}],
        prior_positions=[
            {"symbol": "F", "play_id": "DIP", "quantity": 1.0,
             "high_since_entry": 21.00},
            {"symbol": "GME", "play_id": "PEAD", "quantity": 1.0},
            {"symbol": "PFE", "play_id": "PEAD", "quantity": 2.0},
        ],
        tax_lots={
            "F": [{"quantity": 1.0, "cost_basis": 15.00,
                   "acquired_date": "2026-07-30"}],
            "GME": [{"quantity": 1.0, "cost_basis": 40.00,
                     "acquired_date": "2026-08-03"}],
            "PFE": [{"quantity": 2.0, "cost_basis": 50.06,
                     "acquired_date": "2026-08-03"}],
        },
        intent={
            "placements": [
                {"order_id": "s1", "symbol": "F", "side": "sell",
                 "quantity": 1.0, "play_id": "DIP", "protective_stop": True},
                {"order_id": "t1", "symbol": "F", "side": "sell",
                 "quantity": 1.0, "play_id": "DIP", "protective_target": True},
            ]
        },
        orders=[
            {"order_id": "s1", "symbol": "F", "side": "sell", "type": "stop_market",
             "quantity": 1.0, "stop_price": 14.10, "time_in_force": "gtc",
             "state": "confirmed", "placed_agent": "agentic"},
            {"order_id": "t1", "symbol": "F", "side": "sell", "type": "limit",
             "quantity": 1.0, "limit_price": 15.90, "time_in_force": "gtc",
             "state": "confirmed", "placed_agent": "agentic"},
            {"order_id": "s2", "symbol": "GME", "side": "sell", "type": "stop_market",
             "quantity": 1.0, "stop_price": 38.50, "time_in_force": "gtc",
             "state": "confirmed", "placed_agent": "agentic"},
        ],
        signals={"F": {"atr": 0.45}, "GME": {"atr": 0.713}, "PFE": {"atr": 0.50}},
        quotes={"F": {"bid": 20.98, "ask": 21.02, "last": 21.00},
                "GME": {"bid": 40.98, "ask": 41.02, "last": 41.00},
                "PFE": {"bid": 24.97, "ask": 25.03, "last": 25.00}},
        play_state={"DIP": {"closed_trades": 4}, "PEAD": {"closed_trades": 2}},
    ),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_the_cycle_matches_its_golden_file(name):
    expected = json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))

    assert reconcile.run(SCENARIOS[name]).as_dict() == expected


# --- the properties the golden files are there to protect ------------------


def test_the_quiet_cycle_writes_control_and_nothing_else():
    """Ten of the day's eleven invocations look like this. If a quiet cycle
    ever produced a finding, a cancel or a ledger row, the runlog would fill
    with rows saying nothing happened and stop being read."""
    result = reconcile.run(SCENARIOS["quiet_cycle"])

    assert result.findings == []
    assert result.cancels == []
    assert result.loss_closes == []
    assert result.play_state_updates == {}


def test_the_operators_trade_is_adopted_silently_with_a_stop(  # §15, by name
):
    """§15's named case. Adopted onto the ADOPTED play, with an entry basis
    taken from the tax lot — 61.40 over two shares is 30.70, not the 30.00 the
    fill printed, because the basis is what the broker will report at the close
    and the fill price is not.

    The operator's ruling made every position deterministic-trader's to manage, and
    MANUAL resolves to no play at all — so the silence was total: no time stop, no target,
    and a name that could only ever be sold at a loss."""
    result = reconcile.run(SCENARIOS["untracked_fill"])
    gme = next(p for p in result.positions if p.symbol == "GME")

    assert [f.code for f in result.findings] == [reconcile.UNTRACKED_FILL]
    assert gme.play_id == plays.ADOPTED
    assert gme.adopted is True
    assert gme.entry_price == 30.70
    assert result.control_updates["entries_blocked"] is False


def test_a_round_trip_moves_the_ledger_and_the_counter_together():
    """One cycle in which a losing DIP closed and a PEAD opened. The wash-sale
    row, the closed-trade credit and the play's realised result are the three
    things no other module can reconstruct afterwards.

    The ledger's 1.25 and the play's −1.25 are the same $1.25, signed for their
    two readers: §9 wants the size of a loss and §1 wants the direction of a
    result. They come from one computation for that reason."""
    result = reconcile.run(SCENARIOS["agent_round_trip"])

    assert result.loss_closes == [
        {"symbol": "F", "close_date": "2026-08-03", "loss_amount": 1.25,
         "basis_unknown": False}
    ]
    # The play's −1.25 is credited by `edge_realized_trades_append` from the
    # broker's per-trade row; this fixture carries none, so only the counter
    # moves here.
    assert result.play_state_updates == {"DIP": {"closed_trades": 5}}
    assert [p.symbol for p in result.positions] == ["PFE"]
    assert result.positions[0].play_id == "PEAD"


def test_the_escalating_cycle_blocks_entries_two_different_ways():
    """A phantom blocks for one cycle; a second consecutive divergence blocks
    until an operator clears it. Both are true here, and they are recorded
    separately because only one of them lifts by itself."""
    result = reconcile.run(SCENARIOS["divergence_escalates"])

    assert reconcile.PHANTOM_ORDER in [f.code for f in result.findings]
    assert reconcile.RECONCILIATION_DIVERGENCE_PERSISTENT in [
        f.code for f in result.findings
    ]
    assert result.control_updates["entries_blocked"] is True
    assert result.control_updates["entries_blocked_until_cleared"] is True


def test_the_escalating_cycle_still_cancels_only_its_own_order():
    """Two orders open, one of them a phantom. The stale sweep takes ours and
    leaves theirs, on a cycle where everything else is going wrong."""
    result = reconcile.run(SCENARIOS["divergence_escalates"])

    assert [c["order_id"] for c in result.cancels] == ["a1b2"]


def test_a_drawdown_does_not_lower_the_mark_even_on_a_bad_cycle():
    """Equity at 180 against a 236.42 peak. §10's floor is 189.14, so this is
    a real breach — and the mark that defines it must not move down with the
    account or the halt could never fire."""
    result = reconcile.run(SCENARIOS["divergence_escalates"])

    assert result.control_updates["high_water_mark"] == 236.42


def test_the_protected_book_records_all_three_protection_outcomes():
    """The file is only worth keeping if it exercises the whole diff. A
    fixture that quietly stopped producing a `replace` would still match its
    own golden and prove nothing."""
    plan = reconcile.run(SCENARIOS["protected_book"]).protection_plan

    assert [o["symbol"] for o in plan["place"]] == ["PFE"]
    assert [k["symbol"] for k in plan["keep"]] == ["GME"]
    assert [
        (r["cancel_order_id"], r["new_order"] is None) for r in plan["replace"]
    ] == [("s1", False), ("t1", True)]


def test_a_resting_leg_that_no_longer_rests_is_cancelled_outright():
    """The broker reserves 100% of a position's shares against the first resting
    sell it accepts, so F's GTC target at 15.90 is not a redundant order
    sitting beside the stop — it is the reason the stop cannot be placed. Ours,
    so it is cancelled; the replacement is already in the `s1` row."""
    plan = reconcile.run(SCENARIOS["protected_book"]).protection_plan

    assert {"cancel_order_id": "t1", "new_order": None} in plan["replace"]
