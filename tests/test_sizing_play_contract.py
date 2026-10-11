"""What a play may say about its own size (design v3 §6.3).

`CATALYST` is "sized at 0.75x normal notional until it has 20 closed trades",
and sizing derives notional from the ladder with no play awareness at all. The
multiplier is therefore the candidate's to carry: plays knows which play it is
and how many trades that play has closed, and sizing knows what a full rung is
worth. Neither can compute it alone.

It is deliberately the only sizing input a play controls. Everything else on
the ladder is a function of equity and broker flags (Invariant 5), and a play
that could widen its own position cap would be a play that could argue past
§4.4.
"""

import pytest

from src import sizing
from tests.factories import candidate, context, entry, reasons

# equity 236.42, three open slots, F at 14.87 -> a 31.7572 rung buys two whole
# shares. Three-quarters of that rung buys one.
FULL_RUNG_SHARES = 2.0
DERISKED_SHARES = 1.0


def test_a_play_may_shrink_its_own_notional():
    full = sizing.size([candidate()], context())
    derisked = sizing.size([candidate(notional_multiplier=0.75)], context())

    assert entry(full, "F").quantity == FULL_RUNG_SHARES
    assert entry(derisked, "F").quantity == DERISKED_SHARES


def test_a_candidate_that_says_nothing_sizes_at_the_full_rung():
    # The four plays that are not CATALYST never set the field, and the golden
    # ladder files were recorded before it existed.
    assert entry(sizing.size([candidate()], context()), "F").quantity == FULL_RUNG_SHARES


def test_an_explicit_null_multiplier_is_the_full_rung_too():
    # Same tolerance sizing already extends to a missing conviction: plays owns
    # the field, and sizing must not be the thing that breaks when a play emits
    # it empty.
    result = sizing.size([candidate(notional_multiplier=None)], context())

    assert entry(result, "F").quantity == FULL_RUNG_SHARES


def test_the_multiplier_is_reported_so_the_runlog_shows_why_the_position_is_small():
    """Without this, a 0.75x CATALYST entry is indistinguishable in the record
    from the ladder misbehaving."""
    result = sizing.size([candidate(notional_multiplier=0.75)], context())

    assert entry(result, "F").notional_multiplier == pytest.approx(0.75)


def test_a_multiplier_cannot_push_a_candidate_under_the_broker_floor_unnoticed():
    """Shrinking below $1.00 is a refusal, not a silently tiny order."""
    result = sizing.size([candidate(notional_multiplier=0.01)], context())

    assert entry(result, "F") is None
    assert reasons(result, "F") == [sizing.BELOW_MIN_NOTIONAL]


def test_the_multiplier_does_not_touch_the_concentration_caps():
    """§4.4's caps are equity-derived and guard enforces them independently. A
    play that shrank its notional must not thereby appear to have more room."""
    full = sizing.size([candidate()], context())
    derisked = sizing.size([candidate(notional_multiplier=0.75)], context())

    assert derisked.ladder.position_cap == full.ladder.position_cap
    assert derisked.ladder.sector_cap == full.ladder.sector_cap
    assert derisked.ladder.notional == full.ladder.notional
