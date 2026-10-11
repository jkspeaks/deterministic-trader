"""§6.0 play arbitration — the single score four plays compete on.

    score = play_conviction x regime_fit + whole_share_bonus - crowding_penalty

Three of the four terms are settled here. ``whole_share_bonus`` is sizing's,
because it depends on what a rung of the capital ladder buys and nothing in
this module can know that. The division is deliberate and tested at the seam
below: what plays calls `conviction` is what sizing ranks on.
"""

import pytest

from src import plays, sizing
from tests.factories import (
    config,
    context,
    entry,
    plays_context,
    proposal,
    proposals,
    regime,
    skips,
)


# These tests predate an earlier retirement and shelving of the event plays
# and use them as they then ran. See `all_plays_live` in conftest.py.
pytestmark = pytest.mark.usefixtures("all_plays_live")


def both_plays_ctx() -> dict:
    """A cycle where PEAD and DIP both want F, at 0.90 and 0.70."""
    ctx = plays_context()
    ctx["signals"]["F"]["sma_50"] = 14.00
    ctx["earnings"]["F"] = {
        "status": "known",
        "sessions_until_report": 60,
        "sessions_since_report": 2,
        "eps_surprise_pct": 9.0,
        "reaction_pct": 0.03,
    }
    return ctx


# --- the regime multiplier, and the fail-OPEN default ---------------------


def test_the_regime_multiplies_conviction():
    """§6.0's first term, now `src/regime.py`'s answer rather than a number
    somebody wrote into `edge_play_state` last Tuesday."""
    ctx = plays_context()
    ctx["regime"] = {"play_multipliers": {"DIP": 0.5}, "reason": "half weight"}

    cand = proposal(plays.entries(ctx), "F")

    assert cand.conviction_raw == pytest.approx(0.70)
    assert cand.conviction == pytest.approx(0.35)


def test_a_risk_off_tape_darks_every_play():
    ctx = plays_context()
    ctx["regime"] = regime(spy={"last": 580.0, "rv20": 0.42})

    result = plays.entries(ctx)

    assert result.candidates == []
    assert skips(result, "F") == [plays.REGIME_DARK]
    assert "realized vol" in result.skipped[0].message


def test_a_tradeable_tape_leaves_conviction_alone():
    ctx = plays_context()
    ctx["regime"] = regime()

    assert proposal(plays.entries(ctx), "F").conviction == pytest.approx(0.70)


def test_a_play_the_agent_disabled_proposes_nothing():
    ctx = plays_context()
    ctx["play_state"]["DIP"] = {"regime_fit": 0.0, "reason": "trend tape, dips keep going"}

    result = plays.entries(ctx)

    assert result.candidates == []
    assert skips(result, "F") == [plays.REGIME_DARK]


def test_the_agents_reason_survives_into_the_record():
    """§6 requires the agent to record why a play is off. A skip that did not
    carry the reason would make the runlog say a play was disabled and never
    say by whom or on what grounds."""
    ctx = plays_context()
    ctx["play_state"]["DIP"] = {"regime_fit": 0.0, "reason": "trend tape, dips keep going"}

    assert "trend tape" in plays.entries(ctx).skipped[0].message


def test_an_unwritten_regime_row_no_longer_stops_anything():
    """An earlier inversion, and the defect it closes.

    This used to be `REGIME_UNSET`: one missing `edge_play_state` row stopped
    every entry, on the reasoning that something which cannot hear "stop" must
    not act. What it actually bought was the opposite — the premarket producer
    died and the rows it left behind kept reading as opinions, so the system
    traded a frozen 0.4 for a week and the fail-closed path never fired once.
    The regime is derived from prices now; a row nobody wrote is simply a
    switch nobody touched.
    """
    ctx = plays_context()
    del ctx["play_state"]["DIP"]

    assert proposal(plays.entries(ctx), "F").conviction == pytest.approx(0.70)


def test_an_empty_play_state_table_stops_nothing_either():
    """Day one, before anything has ever written a row.

    Emptied by assignment: the factory deep-merges, so passing ``play_state={}``
    as a keyword leaves all four rows exactly where they were and the test
    silently asserts nothing.
    """
    ctx = plays_context()
    ctx["play_state"] = {}

    assert proposal(plays.entries(ctx), "F").conviction == pytest.approx(0.70)


def test_a_non_zero_regime_fit_is_no_longer_read_at_all():
    """Half of the old manual switch is gone deliberately. A written 0.4 is
    exactly the artefact that went stale in production, and the column's only
    surviving meaning is the explicit 0.0 below."""
    ctx = plays_context()
    ctx["play_state"]["DIP"]["regime_fit"] = 0.4

    assert proposal(plays.entries(ctx), "F").conviction == pytest.approx(0.70)


def test_the_operator_switch_still_darks_a_play():
    """The half that survives: an explicit 0.0 someone typed. It cannot go
    stale in a direction that keeps trading, which is why it kept its job."""
    ctx = plays_context()
    ctx["play_state"]["DIP"] = {"regime_fit": 0.0, "reason": "off"}

    assert skips(plays.entries(ctx), "F") == [plays.REGIME_DARK]


def test_the_operator_switch_is_named_ahead_of_the_tape():
    """Two ways to be dark, one skip. "I turned DIP off on Tuesday" is the
    more specific answer for a reader chasing why a name was not bought."""
    ctx = plays_context()
    ctx["regime"] = regime(spy={"last": 580.0, "rv20": 0.42})
    ctx["play_state"]["DIP"] = {"regime_fit": 0.0, "reason": "operator says so"}

    assert "operator says so" in plays.entries(ctx).skipped[0].message


def test_a_dark_regime_on_one_play_leaves_the_others_alone():
    ctx = both_plays_ctx()
    ctx["play_state"]["PEAD"]["regime_fit"] = 0.0

    result = plays.entries(ctx)

    assert proposals(result) == ["F"]
    assert proposal(result, "F").play_id == plays.DIP


# --- crowding -------------------------------------------------------------


def crowded_ctx(*held: str) -> dict:
    """F, plus ``held`` positions that share its Consumer Discretionary bucket."""
    ctx = plays_context()
    ctx["positions"] = [{"symbol": s, "quantity": 1.0} for s in held]
    for symbol in held:
        ctx["sectors"][symbol] = "Consumer Discretionary"
    return ctx


def test_crowding_no_longer_comes_off_the_score():
    """An earlier change. It was `conviction × regime − crowding`, and at the
    frozen regime of 0.4 a flat 0.10 penalty took a third of what was left of
    a good signal — a hard cap wearing a soft gradient's name §"""
    assert proposal(plays.entries(crowded_ctx("GM")), "F").conviction == pytest.approx(0.70)


def test_the_penalty_is_still_measured_and_still_stacks():
    """At n=3, two names in a sector already brush §4.4's 50% sector cap. The
    number is unchanged and still recorded; only what it is *used for* moved."""
    cand = proposal(plays.entries(crowded_ctx("GM", "TSLA")), "F")

    assert cand.crowding_penalty == pytest.approx(0.20)
    assert cand.conviction == pytest.approx(0.70)


def test_a_position_in_another_sector_costs_nothing():
    ctx = plays_context()
    ctx["positions"] = [{"symbol": "XOM", "quantity": 1.0}]
    ctx["sectors"]["XOM"] = "Energy"

    assert proposal(plays.entries(ctx), "F").crowding_penalty == 0.0


def test_no_amount_of_crowding_can_erase_a_signal():
    """Seven names deep in one sector used to refuse the trade outright
    (`CROWDED_OUT`). §4.4's sector cap is the real constraint and it is
    enforced twice already, in `sizing._Budget` and in
    `guard._concentration_denials`; this layer ranks, it does not deny."""
    result = plays.entries(crowded_ctx(*(f"CD{i}" for i in range(7))))

    assert proposal(result, "F").conviction == pytest.approx(0.70)
    assert skips(result, "F") == []


def test_crowding_orders_two_otherwise_equal_candidates():
    """What "apply it after ranking" buys: between two names the plays scored
    the same, the one whose sector is not already carrying the bet goes
    first — and it can no longer move a name past a *better* one."""
    ctx = crowded_ctx("GM")
    ctx["candidates"] = ["F", "PFE"]
    ctx["sectors"]["PFE"] = "Health Care"
    ctx["signals"]["PFE"] = dict(ctx["signals"]["F"])
    ctx["quotes"]["PFE"] = dict(ctx["quotes"]["F"])
    ctx["earnings"]["PFE"] = dict(ctx["earnings"]["F"])

    result = plays.entries(ctx)

    assert [c.conviction for c in result.candidates] == [0.70, 0.70]
    assert proposals(result) == ["PFE", "F"]


def test_the_penalty_is_reported_alongside_the_score():
    cand = proposal(plays.entries(crowded_ctx("GM")), "F")

    assert cand.crowding_penalty == pytest.approx(0.10)
    assert cand.regime_fit == 1.0


def test_the_penalty_is_config_because_it_is_a_cross_play_term():
    """Unlike a play's own thresholds, §6.0's terms apply to all four and are
    the operator's to tune."""
    ctx = crowded_ctx("GM")
    ctx["config"] = config(crowding_penalty=0.30)

    assert proposal(plays.entries(ctx), "F").crowding_penalty == pytest.approx(0.30)


def test_a_sectorless_candidate_is_not_penalised_here():
    """Sizing refuses it outright and guard fails it closed, so there is
    nothing a ranking penalty could improve."""
    ctx = crowded_ctx("GM")
    del ctx["sectors"]["F"]

    assert proposal(plays.entries(ctx), "F").crowding_penalty == 0.0


# --- one name, one position -----------------------------------------------


def test_the_stronger_play_takes_a_name_both_want():
    result = plays.entries(both_plays_ctx())

    assert proposals(result) == ["F"]
    assert proposal(result, "F").play_id == plays.PEAD


def test_the_play_that_lost_the_name_is_recorded():
    """Attribution: DIP wanted this and did not get it. A silent drop would
    make the play look like it never fired."""
    result = plays.entries(both_plays_ctx())

    assert skips(result, "F") == [plays.OUTRANKED_ON_SYMBOL]
    assert result.skipped[0].play_id == plays.DIP


def test_the_regime_can_change_which_play_wins_a_name():
    """The arbitration is on the §6.0 score, not on raw conviction — so a term
    that applies equally to both cannot reorder them, and one that does not,
    can. Halving PEAD's weight is what §6.0 means by the regime choosing."""
    ctx = both_plays_ctx()
    ctx["regime"] = {"play_multipliers": {"PEAD": 0.5}, "reason": "PEAD is out of favour"}

    assert proposal(plays.entries(ctx), "F").play_id == plays.DIP


# --- ordering -------------------------------------------------------------


def test_conviction_is_a_clean_number_so_exact_ties_stay_reachable():
    """§6.0 breaks *exact* ties toward the shorter holding period, and binary
    floating point turns most of those ties into 0.6999999999999995 against
    0.7. A tiebreak that can never fire is indistinguishable from one nobody
    wrote, so conviction is rounded past any precision it could carry meaning
    at — which also keeps the golden files legible.
    """
    assert proposal(plays.entries(plays_context()), "F").conviction_raw == 0.70


def test_the_arbitrated_score_is_clean_too():
    """The noise compounds: raw conviction, then the regime multiply."""
    ctx = plays_context()
    ctx["regime"] = {"play_multipliers": {"DIP": 0.3}, "reason": "third weight"}

    assert proposal(plays.entries(ctx), "F").conviction == 0.21


def test_a_stacked_penalty_is_clean():
    # 3 x 0.10 is 0.30000000000000004 in binary floating point.
    assert proposal(plays.entries(crowded_ctx("GM", "TSLA", "RIVN")), "F").crowding_penalty == 0.30


def test_two_plays_reaching_the_same_score_really_tie():
    """The end the rounding exists for. DIP at a 7% depth and PEAD at a 7%
    surprise are both 0.70, and the shorter horizon has to win."""
    ctx = both_plays_ctx()
    ctx["earnings"]["F"]["eps_surprise_pct"] = 7.0

    result = plays.entries(ctx)

    assert proposal(result, "F").play_id == plays.DIP  # 8 sessions beats 10


def test_candidates_come_out_ranked():
    ctx = both_plays_ctx()
    ctx["candidates"] = ["F"]
    ctx["macro"]["releases"] = [
        {"series": "CPI", "surprise_pct": 0.10, "sessions_ago": 0}
    ]

    result = plays.entries(ctx)

    # F at 0.90 (PEAD), then the two macro tilts at 0.40 breaking on symbol.
    assert proposals(result) == ["F", "XLE", "XLF"]


def test_ties_break_toward_the_shorter_holding_period():
    """§6.0, and the same rule `sizing._rank` applies. CATALYST's seven
    sessions beat MACRO's fifteen at an equal score."""
    ctx = plays_context(candidates=["F"])
    ctx["signals"]["F"].update({"rsi": 55.0})
    ctx["quotes"]["F"]["change_pct"] = 0.05
    ctx["fundamentals"]["F"] = {"volume": 90_000_000, "average_volume": 50_000_000}
    ctx["events"]["F"] = [{"type": "8-K", "items": ["9.01"], "sessions_ago": 1}]
    ctx["macro"]["releases"] = [
        {"series": "CPI", "surprise_pct": 0.125, "sessions_ago": 0}
    ]

    result = plays.entries(ctx)

    # All three score 0.50; F is CATALYST at 7 days, XLE and XLF MACRO at 15.
    assert [c.conviction for c in result.candidates] == [0.5, 0.5, 0.5]
    assert proposals(result) == ["F", "XLE", "XLF"]


# --- the seam with sizing -------------------------------------------------


def test_a_plays_candidate_is_a_sizing_candidate_without_translation():
    """The contract §6.0 is split across. Plays emits three of the four terms
    under the name `conviction`; sizing adds the fourth and ranks on the sum.

    The sizing context is built separately and the candidates assigned into the
    call, never merged into a factory — a deep-merged fixture fills a renamed
    key from its own defaults and the test stops noticing.
    """
    proposed = plays.entries(plays_context()).candidates

    result = sizing.size([c.as_dict() for c in proposed], context())

    sized = entry(result, "F")
    assert sized.play_id == plays.DIP
    assert sized.conviction == pytest.approx(0.70)
    assert sized.rank_bonus == pytest.approx(0.05)
    assert sized.rank_score == pytest.approx(0.75)
    assert sized.notional_multiplier == 1.0


def test_the_catalyst_multiplier_survives_the_seam():
    """§6.3's 0.75x is decided in plays and applied in sizing; it is the one
    number that has to cross intact."""
    ctx = plays_context()
    ctx["signals"]["F"].update({"rsi": 55.0})
    ctx["quotes"]["F"]["change_pct"] = 0.05
    ctx["fundamentals"]["F"] = {"volume": 90_000_000, "average_volume": 50_000_000}
    ctx["events"]["F"] = [{"type": "8-K", "items": ["9.01"], "sessions_ago": 1}]

    proposed = plays.entries(ctx).candidates
    result = sizing.size([c.as_dict() for c in proposed], context())

    assert entry(result, "F").notional_multiplier == pytest.approx(0.75)
    assert entry(result, "F").quantity == 1.0


def test_plays_and_sizing_rank_in_the_same_order():
    """Two rankers that disagreed would make the runlog's stated order and the
    order capital is actually committed in two different things."""
    ctx = both_plays_ctx()
    ctx["macro"]["releases"] = [
        {"series": "CPI", "surprise_pct": 0.10, "sessions_ago": 0}
    ]
    # Ranking agreement is under test here, not whole_shares_only, and XLE/XLF
    # cannot afford a full share at this rung.
    sizing_ctx = context(config=config(whole_shares_only=False))
    for symbol in ("XLE", "XLF"):
        sizing_ctx["quotes"][symbol] = {"bid": 90.00, "ask": 90.05, "last": 90.02}
        sizing_ctx["sectors"][symbol] = "Energy"
    sizing_ctx["universe"]["candidates"] = ["F", "XLE", "XLF"]

    proposed = plays.entries(ctx).candidates
    result = sizing.size([c.as_dict() for c in proposed], sizing_ctx)

    assert [c.symbol for c in proposed] == [e.symbol for e in result.entries]
