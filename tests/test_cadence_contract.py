"""The §15 cycle-type contract, golden-file tested.

Design v3 §15 names four assertions for this module and no others:

    "cadence.py cycle-type contract is golden-file tested: first run of day ->
    full; overnight event after the full cycle -> overnight with
    entries_allowed: false; nothing pending and full already run -> no-op; an
    overnight cycle must not advance last_full_cycle."

They are here, one test each, ahead of everything else the module does. The
rest of the suite elaborates; these four are the specification.
"""

from src import cadence
from tests.factories import cadence_context


def test_the_first_run_of_the_day_is_a_full_cycle():
    # 09:40 ET Monday, nothing run yet. §13 slot 3.
    cycle = cadence.decide(cadence_context(now="2026-08-03T13:40:00Z"))

    assert cycle.cycle_type == cadence.FULL
    assert cycle.entries_allowed is True
    assert cycle.reconcile_required is True


def test_an_overnight_cycle_after_the_full_cycle_allows_no_entries():
    # 20:00 ET, the day's full cycle already ran. §13 slot 14.
    cycle = cadence.decide(
        cadence_context(
            now="2026-08-04T00:00:00Z",
            control={
                "last_full_cycle": "2026-08-03T13:40:00Z",
                "has_whole_share_position": True,
                "positions_held": 3,
            },
        )
    )

    assert cycle.cycle_type == cadence.OVERNIGHT
    assert cycle.entries_allowed is False


def test_nothing_pending_after_the_full_cycle_is_a_no_op():
    # 12:40 ET, full already run, empty book, nothing outstanding. The no-op
    # path must be reachable without touching Robinhood, so every field this
    # decision reads comes from edge_control.
    cycle = cadence.decide(
        cadence_context(
            now="2026-08-03T16:40:00Z",
            control={
                "last_full_cycle": "2026-08-03T13:40:00Z",
                "new_positions": {"date": "2026-08-03", "count": 3},
            },
        )
    )

    assert cycle.cycle_type == cadence.NOOP
    assert cycle.reconcile_required is False


def test_an_overnight_cycle_does_not_advance_the_full_cycle_clock():
    """Two clocks, never one (§13).

    A shared stamp would let the 04:30 overnight cycle mark the day as run and
    suppress the 09:40 full cycle entirely.
    """
    cycle = cadence.decide(
        cadence_context(
            now="2026-08-04T08:30:00Z",  # 04:30 ET Tuesday
            control={
                "last_full_cycle": "2026-08-03T13:40:00Z",
                "has_whole_share_position": True,
                "positions_held": 3,
            },
        )
    )

    assert cycle.cycle_type == cadence.OVERNIGHT
    assert "last_full_cycle" not in cycle.clock_updates
    assert cycle.clock_updates["last_overnight_cycle"] == "2026-08-04T08:30:00Z"
