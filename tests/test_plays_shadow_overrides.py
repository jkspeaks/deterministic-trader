"""The two config overrides the shadow ledger's B and C variants turn on.

Both exist for one reason: §4.3 B and C describe rules that **already
shipped**, so the only comparison left that means anything is the retired rule
against today's. Neither override is set in `config/config.json`, and the first
test of each group is the one that says so — a knob that quietly came on would
move the live account onto a rule that was removed, which is the opposite of
what a shadow ledger is for.
"""

from src import plays
# `from ... import` rather than `import src.plays.entries as ...`: the
# package re-exports `entries` as the *function*, so the dotted form
# resolves to that and not to the module.
from src.plays.entries import CROWDING_TIEBREAK, _crowding_mode
from src.plays.core import ITEM_NON_RELIANCE, ITEM_OFFICER_DEPARTURE
from tests.factories import config, plays_context, position

# ---------------------------------------------------------------------------
# crowding_mode — variant B
# ---------------------------------------------------------------------------


def crowded(**overrides):
    """F proposed by DIP with one position already in its own sector.

    `plays_context`'s baseline F is Consumer Discretionary and scores 0.70;
    holding another name in that sector charges one `crowding_penalty` (0.10).
    """
    return plays_context(
        positions=[position(symbol="GM", play_id="DIP")],
        sectors={"F": "Consumer Discretionary", "GM": "Consumer Discretionary"},
        **overrides,
    )


def test_the_shipped_config_leaves_crowding_as_a_tiebreak():
    assert "crowding_mode" not in config()
    assert _crowding_mode({"config": config()}) == (
        CROWDING_TIEBREAK
    )


def test_the_shipped_reading_charges_the_penalty_without_touching_the_score():
    result = plays.entries(crowded())

    candidate = next(c for c in result.candidates if c.symbol == "F")
    assert candidate.crowding_penalty == 0.10
    # The score is conviction x regime, and the penalty is not one of its terms.
    assert candidate.conviction == candidate.conviction_raw


def test_the_retired_reading_subtracts_it_from_the_score():
    result = plays.entries(crowded(config=config(crowding_mode="score")))

    candidate = next(c for c in result.candidates if c.symbol == "F")
    assert candidate.conviction_raw == 0.70
    assert candidate.conviction == 0.60


def test_the_retired_reading_can_erase_a_candidate_and_says_which_penalty_did_it():
    # An earlier defect, reproduced: at the frozen `regime_fit` of 0.4 a 0.70
    # signal scored 0.28 and a flat penalty took 36% of it. `plays_context` runs
    # a healthy 1.0 regime, so the penalty is raised to 0.70 instead — same
    # arithmetic, one number moved, and the candidate is gone.
    context = plays_context(
        config=config(crowding_mode="score", crowding_penalty=0.70),
        positions=[position(symbol="GM", play_id="DIP")],
        sectors={"F": "Consumer Discretionary", "GM": "Consumer Discretionary"},
    )

    result = plays.entries(context)

    assert [c.symbol for c in result.candidates] == []
    erased = [s for s in result.skipped if s.symbol == "F"]
    assert erased
    assert "crowding_mode=score" in erased[0].message


def test_an_unrecognised_mode_reads_as_the_shipped_one():
    # A typo must not reinstate a rule that was removed for erasing candidates.
    assert _crowding_mode({"config": config(crowding_mode="Score")}) == (
        CROWDING_TIEBREAK
    )


# ---------------------------------------------------------------------------
# force_exit_items — variant C
# ---------------------------------------------------------------------------


def filed(item: str, **overrides):
    """F held, with one 8-K carrying `item` filed against it today."""
    return plays_context(
        positions=[position(symbol="F", play_id="DIP")],
        events={"F": [{"date": "2026-08-03", "items": [item]}]},
        **overrides,
    )


def exit_codes(context):
    return [e.code for e in plays.exits(context).exits]


def test_the_shipped_config_forces_on_non_reliance_alone():
    assert "force_exit_items" not in config()
    assert plays.force_exit_items({"config": config()}) == frozenset(
        {ITEM_NON_RELIANCE}
    )


def test_a_non_reliance_filing_still_forces_an_exit():
    assert plays.HARD_EVENT_EXIT in exit_codes(filed(ITEM_NON_RELIANCE))


def test_an_officer_departure_is_a_note_and_not_an_exit_by_default():
    result = plays.exits(filed(ITEM_OFFICER_DEPARTURE))

    assert plays.HARD_EVENT_EXIT not in [e.code for e in result.exits]
    assert any(ITEM_OFFICER_DEPARTURE in note for note in result.notes)


def test_the_override_puts_5_02_back_and_drops_the_note_with_it():
    context = filed(
        ITEM_OFFICER_DEPARTURE,
        config=config(force_exit_items=[ITEM_NON_RELIANCE, ITEM_OFFICER_DEPARTURE]),
    )

    result = plays.exits(context)

    assert plays.HARD_EVENT_EXIT in [e.code for e in result.exits]
    # The operator note exists precisely because nothing acts on the filing.
    # With the forced exit back, restating it would be two answers to one fact.
    assert not any(ITEM_OFFICER_DEPARTURE in note for note in result.notes)


def test_a_malformed_override_reads_as_the_shipped_set():
    # An override that emptied this would switch off the one exit §6.5
    # describes as not waiting for the price.
    for broken in ([], "4.02", None, [4.02]):
        assert plays.force_exit_items({"config": config(force_exit_items=broken)}) == (
            frozenset({ITEM_NON_RELIANCE})
        )
