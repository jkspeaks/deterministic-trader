"""The §5.3 daily candidate set.

    candidates = current_positions
               ∪ names with a live play trigger today
               ∪ macro rotation targets if a play is live

Three sources with three different levels of trust. A position is admitted
unconditionally — Invariant 7 means an adopted manual buy has to be
manageable whatever it is. A trigger is admitted only if the issuer file
already listed it, because a play that could name its own symbols would be a
way around Invariant 3. A rotation target is admitted only from the eleven.
"""

from src import universe
from tests.factories import notes, universe_context


def held(*symbols: str) -> list[dict]:
    return [{"symbol": s, "quantity": 1.0} for s in symbols]


# --- the three sources ----------------------------------------------------


def test_a_triggered_name_becomes_a_candidate():
    result = universe.resolve(universe_context())

    assert result.candidates == ["F"]


def test_a_held_name_is_a_candidate_with_no_trigger_at_all():
    """Exits are evaluated every cycle (§6.5), so a position that stopped
    triggering must not fall out of the set that guard will admit orders for."""
    ctx = universe_context(positions=held("PFE"), triggers=[])

    result = universe.resolve(ctx)

    assert result.candidates == ["PFE"]


def test_a_macro_target_becomes_a_candidate_when_the_play_is_live():
    ctx = universe_context(macro_targets=["XLE"])

    result = universe.resolve(ctx)

    assert "XLE" in result.candidates


def test_the_candidate_set_is_the_union_of_all_three():
    ctx = universe_context(
        positions=held("PFE"),
        triggers=[{"symbol": "AAPL", "play_id": "PEAD"}],
        macro_targets=["XLE"],
    )

    result = universe.resolve(ctx)

    assert result.candidates == ["AAPL", "PFE", "XLE"]


def test_a_name_both_held_and_triggered_appears_once():
    ctx = universe_context(positions=held("F"))

    result = universe.resolve(ctx)

    assert result.candidates == ["F"]


def test_candidates_are_sorted():
    ctx = universe_context(
        triggers=[{"symbol": s, "play_id": "DIP"} for s in ("XOM", "AAPL", "F")]
    )

    result = universe.resolve(ctx)

    assert result.candidates == ["AAPL", "F", "XOM"]


def test_a_cycle_with_nothing_held_and_nothing_triggered_has_no_candidates():
    ctx = universe_context(triggers=[])

    assert universe.resolve(ctx).candidates == []


# --- the membership fence -------------------------------------------------


def test_a_trigger_on_a_name_outside_the_universe_is_dropped():
    """Invariant 3 in its load-bearing form. A play may select, never invent."""
    ctx = universe_context(triggers=[{"symbol": "GME", "play_id": "DIP"}])

    result = universe.resolve(ctx)

    assert result.candidates == []


def test_a_dropped_trigger_names_the_play_that_proposed_it():
    ctx = universe_context(triggers=[{"symbol": "GME", "play_id": "DIP"}])

    result = universe.resolve(ctx)

    assert notes(result, universe.TRIGGER_OUTSIDE_UNIVERSE) == ["GME"]
    assert "DIP" in result.notes[0].detail


def test_a_trigger_on_a_sleeve_etf_is_admitted():
    """The sleeve is universe membership, so a DIP on SPY is a legal trigger
    even though rotation can never reach it."""
    ctx = universe_context(triggers=[{"symbol": "SPY", "play_id": "DIP"}])

    assert universe.resolve(ctx).candidates == ["SPY"]


def test_a_held_name_outside_the_universe_is_still_a_candidate():
    """The operator bought something off-index in the app. Invariant 7 says it
    is managed on identical terms from the next cycle, which it cannot be if
    universe.py refuses to name it."""
    ctx = universe_context(positions=held("GME"), triggers=[])

    result = universe.resolve(ctx)

    assert result.candidates == ["GME"]


def test_an_adopted_non_member_is_logged():
    ctx = universe_context(positions=held("GME"), triggers=[])

    result = universe.resolve(ctx)

    assert notes(result, universe.ADOPTED_NON_MEMBER) == ["GME"]


def test_a_held_member_is_not_logged_as_adopted():
    ctx = universe_context(positions=held("F"))

    assert notes(universe.resolve(ctx), universe.ADOPTED_NON_MEMBER) == []


# --- rotation is bounded by the sleeve ------------------------------------


def test_a_rotation_target_outside_the_eleven_is_dropped():
    """§6.4 expresses macro through sector ETFs specifically — they carry the
    tighter overnight spreads §6.6 gates on. A single name is not a tilt."""
    ctx = universe_context(triggers=[], macro_targets=["AAPL"])

    result = universe.resolve(ctx)

    assert result.candidates == []
    assert notes(result, universe.MACRO_TARGET_NOT_IN_ROTATION) == ["AAPL"]


def test_a_broad_etf_is_not_a_rotation_target():
    ctx = universe_context(triggers=[], macro_targets=["SPY"])

    result = universe.resolve(ctx)

    assert result.candidates == []
    assert notes(result, universe.MACRO_TARGET_NOT_IN_ROTATION) == ["SPY"]


def test_rotation_cannot_exceed_the_eleven_sector_etfs():
    """§5.3's '≤ 11' is a consequence of the sleeve, not a separate cap that
    could drift out of step with it."""
    rotation = universe.resolve(universe_context()).rotation
    ctx = universe_context(triggers=[], macro_targets=rotation)

    result = universe.resolve(ctx)

    assert len(result.candidates) == 11


# --- the economics §5.3 is built on ---------------------------------------


def test_the_sleeve_is_membership_and_not_a_standing_candidate():
    """Fifteen ETFs are members every day. If they were candidates every day
    they would cost fifteen Robinhood calls a cycle for nothing."""
    result = universe.resolve(universe_context())

    assert "XLE" in result.members
    assert "XLE" not in result.candidates


def test_the_candidate_set_is_a_small_fraction_of_the_universe():
    ctx = universe_context(
        positions=held("PFE"), macro_targets=["XLE", "XLF", "XLK"]
    )

    result = universe.resolve(ctx)

    assert len(result.candidates) < len(result.members)
