"""What demotes a cycle to a no-op, and what rescues it (§12.1, §13).

§13: "The no-op path must be genuinely cheap. Read lock, pending events and
unreconciled state in a single Supabase query, then exit before touching
Robinhood." Everything asserted here is therefore decided from `edge_control`
alone — if any of it needed a Robinhood call, the saving it represents would
already have been spent.
"""

from src import cadence
from tests.factories import cadence_context

MIDDAY = "2026-08-03T16:40:00Z"  # 12:40 ET Monday
RAN = "2026-08-03T13:40:00Z"

# The state §15 calls "nothing pending": full already run, empty book, cap
# spent. Each test below relaxes exactly one field.
IDLE = {
    "last_full_cycle": RAN,
    "new_positions": {"date": "2026-08-03", "count": 3},
    "positions_held": 0,
    "unreconciled": False,
    "pending_events": False,
}


def cycle(now=MIDDAY, **control):
    return cadence.decide(cadence_context(now=now, control=dict(IDLE, **control)))


# --- the has-work predicate -----------------------------------------------


def test_an_idle_monitor_is_a_no_op():
    assert cycle().cycle_type == cadence.NOOP


def test_a_held_position_is_work():
    # Stops, exits and deadline checks all need a cycle to run in.
    assert cycle(positions_held=1).cycle_type == cadence.MONITOR


def test_an_unreconciled_prior_cycle_is_work():
    """§12.1: "An unreconciled prior cycle that placed orders is itself an
    event, promoting the next cycle out of no-op." """
    result = cycle(unreconciled=True)

    assert result.cycle_type == cadence.MONITOR
    assert result.reconcile_required is True


def test_a_pending_event_is_work():
    assert cycle(pending_events=True).cycle_type == cadence.MONITOR


def test_an_unspent_entry_budget_is_work():
    # An empty book with room to enter should go looking for a candidate.
    result = cycle(new_positions={"date": "2026-08-03", "count": 0})

    assert result.cycle_type == cadence.MONITOR
    assert result.entries_allowed is True


def test_a_no_op_reconciles_nothing_and_moves_no_clock():
    result = cycle()

    assert result.reconcile_required is False
    assert result.exits_allowed is False
    assert result.clock_updates == {}


# --- the overnight demotion (P1 x §13) ------------------------------------


def test_an_overnight_cycle_with_only_fractional_positions_is_a_no_op():
    """At $236 every position is fractional and P1 makes fractional orders
    regular-hours-only, so an overnight cycle cannot place any exit it might
    decide on. Running it would buy a full MCP session for a guaranteed
    nothing."""
    result = cycle(now="2026-08-03T08:30:00Z", positions_held=10)

    assert result.cycle_type == cadence.NOOP
    assert "whole-share" in result.reason


def test_one_whole_share_brings_the_overnight_cycles_to_life():
    """The demotion is a function of capital, not configuration — it lifts the
    moment the ladder produces a whole-share position, with no threshold to
    maintain (Invariant 5)."""
    result = cycle(
        now="2026-08-03T08:30:00Z", positions_held=3, has_whole_share_position=True
    )

    assert result.cycle_type == cadence.OVERNIGHT
    assert result.exits_allowed is True


def test_the_overnight_demotion_outranks_an_unreconciled_promotion():
    """§12.1 reconciles on full, monitor and post-close cycles — never on an
    overnight one. So an overnight cycle with unreconciled state still has
    nothing it can do, and the structural demotion wins."""
    result = cycle(now="2026-08-03T08:30:00Z", positions_held=10, unreconciled=True)

    assert result.cycle_type == cadence.NOOP


# --- cycles that run whatever the book looks like -------------------------


def test_the_post_close_cycle_runs_on_an_idle_day():
    """§8.4: `edge_benchmark` is one row per day and the SPY-relative series
    cannot be reconstructed later. A missing row is unrecoverable, so this is
    the one cycle exempt from the has-work test."""
    result = cycle(now="2026-08-03T20:30:00Z")

    assert result.cycle_type == cadence.POSTCLOSE
    assert result.reconcile_required is True


def test_the_premarket_cycle_no_ops_when_the_day_can_place_nothing():
    # Setting the day's regime is only worth an invocation if some later
    # cycle could act on it.
    assert cycle(now="2026-08-03T12:45:00Z").cycle_type == cadence.NOOP


def test_the_premarket_cycle_runs_when_the_day_still_has_entries():
    result = cycle(
        now="2026-08-03T12:45:00Z", new_positions={"date": "2026-08-03", "count": 0}
    )

    assert result.cycle_type == cadence.PREMARKET
    assert result.entries_allowed is False  # it sets the regime; it places nothing


# --- the self-healing full cycle ------------------------------------------


def test_a_missed_open_promotes_the_next_monitor_to_full():
    """§15: "first run of day -> full". The 09:40 slot has no privilege; a
    routine that failed at the open costs the day nothing but time."""
    result = cycle(last_full_cycle="2026-07-31T13:40:00Z", positions_held=3)

    assert result.cycle_type == cadence.FULL
    assert result.clock_updates == {"last_full_cycle": MIDDAY}


def test_the_first_cycle_ever_is_a_full_cycle():
    result = cycle(last_full_cycle=None, positions_held=3)

    assert result.cycle_type == cadence.FULL


def test_a_full_cycle_that_already_ran_is_not_repeated():
    result = cycle(positions_held=3)

    assert result.cycle_type == cadence.MONITOR
    assert result.clock_updates == {}


def test_a_missed_open_does_not_promote_the_post_close_cycle():
    """16:30 ET is outside regular hours, so a `full` there could place no
    entry. Promoting it would claim a full cycle ran and suppress tomorrow's
    self-heal for a cycle that did none of the work."""
    result = cycle(
        now="2026-08-03T20:30:00Z", last_full_cycle="2026-07-31T13:40:00Z"
    )

    assert result.cycle_type == cadence.POSTCLOSE
    assert result.clock_updates == {}


def test_an_overnight_cycle_stamps_only_its_own_clock():
    result = cycle(
        now="2026-08-03T08:30:00Z",
        last_full_cycle="2026-07-31T13:40:00Z",
        positions_held=3,
        has_whole_share_position=True,
    )

    assert result.clock_updates == {"last_overnight_cycle": "2026-08-03T08:30:00Z"}
