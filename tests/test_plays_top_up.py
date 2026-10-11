"""Topping up a name already held (operator ruling).

Idle cash goes into positions that are working, not into new slots and never by
selling something else. Without it the account cannot commit a cent: `open_slots`
is `max(0, n_target - held)`, which is 0 at 6 against the 10 names the system
woke up holding, so every entry path was closed regardless of what it saw.

The enforcement was already built and had never been reachable — both
`sizing._Budget.has_slot_for` and `guard._concentration_denials` exempt a symbol
already held from the slot limit. These tests are about the half that was
missing, which is a play proposing one at all.

The baseline `position()` is F, entered at 15.00 and now 14.88 against a 15.20
50-day: held, losing, below trend, and therefore not a top-up. Every test here
moves one thing off that.
"""

import pytest

from src import plays
from tests.factories import plays_context, position, proposal, proposals, skips

# Above the 15.20 50-day in `plays_context` by 5%, and above F's 15.00 entry.
WORKING = 15.96


def book(price: float = WORKING, **overrides) -> dict:
    """One held F, priced so it qualifies unless a test says otherwise.

    `candidates` is emptied so nothing DIP proposes can be confused for a
    top-up; the two are ranked together and the tests that care say so.
    """
    ctx = plays_context(candidates=[])
    ctx["positions"] = [position(**overrides)]
    ctx["quotes"]["F"]["last"] = price
    return ctx


# --- the two gates ----------------------------------------------------------


def test_a_held_name_above_its_fifty_day_and_in_profit_is_topped_up():
    result = plays.entries(book())

    assert proposals(result) == ["F"]
    assert proposal(result, "F").play_id == "DIP"


def test_a_name_below_its_fifty_day_is_not_topped_up():
    """Not working. Silent rather than skipped: this runs over every held name
    on every entry cycle and a note per name is the runlog nobody reads."""
    result = plays.entries(book(15.19))

    assert result.candidates == []
    assert skips(result) == []


def test_a_name_underwater_is_never_averaged_down():
    """The failure this whole idea invites, and the gate that refuses it.

    16.00 is above the 15.20 50-day, so the trend gate passes — and the entry
    was 16.50, so the position is losing. Adding here is averaging down, which
    is the opposite of the anti-churn rule that motivated top-ups: that rule
    says do not sell a loser to chase a winner, not buy more of the loser.
    """
    result = plays.entries(book(16.00, entry_price=16.50))

    assert result.candidates == []


def test_exactly_at_the_entry_price_is_not_a_profit():
    result = plays.entries(book(16.00, entry_price=16.00))

    assert result.candidates == []


@pytest.mark.parametrize("missing", ["entry_price", "last", "sma_50"])
def test_a_missing_input_fails_closed(missing):
    """Adding capital on an incomplete picture is the one direction that cannot
    be undone cheaply."""
    ctx = book()
    if missing == "entry_price":
        ctx["positions"] = [position(entry_price=None)]
    elif missing == "last":
        ctx["quotes"]["F"]["last"] = None
    else:
        ctx["signals"]["F"].pop("sma_50")

    assert plays.entries(ctx).candidates == []


# --- conviction -------------------------------------------------------------


def test_conviction_is_the_distance_above_the_fifty_day():
    """5% above a 15.20 mean, full scale at 10%, so a round 0.50 — and
    `regime_fit` 1.0 in the baseline leaves the arbitrated score the same."""
    result = plays.entries(book(15.20 * 1.05))

    assert proposal(result, "F").conviction_raw == pytest.approx(0.5)


def test_conviction_caps_at_full_scale():
    result = plays.entries(book(15.20 * 1.40))

    assert proposal(result, "F").conviction_raw == 1.0


def test_the_top_up_carries_no_crowding_penalty():
    """§6.0's penalty counts names held in the candidate's sector, and a held
    name is in its own — it would be charged for existing. §4.4's sector cap is
    the real constraint here, and `sizing` and `guard` both enforce it."""
    assert proposal(plays.entries(book()), "F").crowding_penalty == 0.0


# --- what may not be topped up ---------------------------------------------


def test_a_dark_play_is_not_topped_up():
    """Adding to a DIP the agent switched off today is a new commitment against
    its own stated judgment."""
    ctx = book()
    ctx["play_state"]["DIP"] = {"regime_fit": 0.0, "reason": "dips keep going"}

    result = plays.entries(ctx)

    assert result.candidates == []
    assert skips(result, "F") == [plays.REGIME_DARK]


def test_an_adopted_position_is_topped_up_despite_a_dark_regime_row():
    """`ADOPTED` is seeded at 0.0 by `edge_play_state_apply` and is not the
    agent's to enable, so gating on it would shut the inherited sleeve out of
    the very ruling that exists to deploy into it."""
    ctx = book(play_id=plays.ADOPTED)
    ctx["play_state"][plays.ADOPTED] = {"regime_fit": 0.0, "reason": "seeded"}

    assert proposals(plays.entries(ctx)) == ["F"]


def test_a_manual_position_is_not_topped_up():
    """MANUAL resolves to no play, so it has no exit terms — adding to it buys
    shares the system has no rule for selling."""
    result = plays.entries(book(play_id=plays.MANUAL))

    assert result.candidates == []
    assert skips(result, "F") == [plays.TOP_UP_UNMANAGED]


def test_a_name_reporting_soon_is_not_topped_up():
    """A top-up is an entry, and the system never holds through a print."""
    ctx = book()
    ctx["earnings"]["F"]["sessions_until_report"] = 2

    assert skips(plays.entries(ctx), "F") == [plays.EARNINGS_VETO]


def test_a_restatement_stops_a_top_up():
    ctx = book()
    ctx["events"]["F"] = [{"type": "8-K", "items": [plays.ITEM_NON_RELIANCE],
                           "filed_day": "2026-08-03"}]

    assert skips(plays.entries(ctx), "F") == [plays.HARD_VETO_RESTATEMENT]


# --- ranked against real entries -------------------------------------------


def test_a_fresh_signal_outranks_a_mild_top_up():
    """The reason top-ups join the ranked list rather than running as a second
    pass: one cycle's capital is allocated by one comparison. DIP fires on PFE
    at 0.70; F is 2% above its 50-day, which is 0.20."""
    ctx = plays_context(
        candidates=["PFE"],
        signals={
            "PFE": {"rsi": 27.0, "sma_20": 16.00, "sma_50": 15.20,
                    "sma_200": 13.50, "atr": 0.45},
        },
        quotes={"PFE": {"bid": 14.86, "ask": 14.88, "last": 14.88}},
        earnings={"PFE": {"status": "known", "sessions_until_report": 40}},
        sectors={"PFE": "Health Care", "F": "Consumer Discretionary"},
    )
    ctx["positions"] = [position()]
    ctx["quotes"]["F"]["last"] = 15.20 * 1.02

    result = plays.entries(ctx)

    assert proposals(result) == ["PFE", "F"]


def test_one_name_liked_twice_is_still_one_position():
    """DIP fires on F *and* F qualifies for a top-up. `_best_per_symbol` settles
    it, so sizing is never handed the same symbol twice."""
    ctx = plays_context()
    ctx["positions"] = [position()]
    # Oversold and above the 200-day fires DIP; above the 50-day and in profit
    # also qualifies as a top-up.
    ctx["quotes"]["F"]["last"] = 15.96

    result = plays.entries(ctx)

    assert proposals(result).count("F") == 1
