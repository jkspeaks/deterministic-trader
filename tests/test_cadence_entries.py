"""The daily entry cap and the closing blackout (§10, §13).

§10 puts "max new positions/day" in this module (2 by design), and §13 adds
"entries only within the daily cap and never in the last 30 min". Guard's only
entry switch is the boolean `cycle.entries_allowed`, so cadence also emits the
count that lets `cli.py` truncate sizing's ranked list — a boolean alone
cannot express "one more entry is allowed".
"""

import pytest

from src import cadence
from tests.factories import cadence_context, config

# Mid-session on a normal Monday, with the morning's full cycle already run.
MIDDAY = "2026-08-03T16:40:00Z"  # 12:40 ET
RAN_THIS_MORNING = {"last_full_cycle": "2026-08-03T13:40:00Z", "positions_held": 3}


def cycle(now=MIDDAY, **control):
    return cadence.decide(
        cadence_context(now=now, control=dict(RAN_THIS_MORNING, **control))
    )


# --- the cap --------------------------------------------------------------


def test_an_untouched_day_has_the_whole_cap():
    result = cycle()

    assert result.entries_remaining == 3
    assert result.entries_allowed is True


def test_one_position_opened_leaves_two():
    result = cycle(new_positions={"date": "2026-08-03", "count": 1})

    assert result.entries_remaining == 2
    assert result.entries_allowed is True


def test_the_cap_closes_entries_for_the_rest_of_the_day():
    result = cycle(new_positions={"date": "2026-08-03", "count": 3})

    assert result.entries_remaining == 0
    assert result.entries_allowed is False


def test_the_count_never_goes_negative():
    """A manual trade the operator opened can push the count past the cap.

    Invariant 7 adopts it rather than arguing with it, so cadence must clamp
    rather than emit a negative budget for `cli.py` to slice a list with.
    """
    result = cycle(new_positions={"date": "2026-08-03", "count": 5})

    assert result.entries_remaining == 0


def test_yesterdays_count_does_not_bind_today():
    """The count carries its own date so it self-resets.

    A bare counter would need a separate daily reset job, and the failure mode
    of a stale one is an account that silently stops entering.
    """
    result = cycle(new_positions={"date": "2026-07-31", "count": 3})

    assert result.entries_remaining == 3
    assert result.entries_allowed is True


def test_a_cap_of_zero_disables_entries_entirely():
    result = cadence.decide(
        cadence_context(
            now=MIDDAY,
            control=RAN_THIS_MORNING,
            config=config(max_new_positions_per_day=0),
        )
    )

    assert result.entries_allowed is False


# --- the closing blackout -------------------------------------------------


@pytest.mark.parametrize(
    "now,allowed,label",
    [
        ("2026-08-03T19:29:00Z", True, "15:29 ET, one minute inside"),
        ("2026-08-03T19:30:00Z", False, "15:30 ET, the deadline itself"),
        ("2026-08-03T19:40:00Z", False, "15:40 ET, §13's last monitor slot"),
    ],
)
def test_entries_stop_thirty_minutes_before_the_close(now, allowed, label):
    assert cycle(now=now).entries_allowed is allowed, label


def test_the_last_monitor_of_the_day_still_manages_the_book():
    """15:40 ET places no entries, but it is not therefore idle.

    §13 lists monitors as entry-capable and also forbids entries in the last
    30 minutes; both hold, and the resolution is that the final monitor is an
    exits-and-stops cycle.
    """
    result = cycle(now="2026-08-03T19:40:00Z")

    assert result.cycle_type == cadence.MONITOR
    assert result.entries_allowed is False
    assert result.exits_allowed is True
    assert result.reconcile_required is True


# --- which cycles may enter at all ----------------------------------------


@pytest.mark.parametrize(
    "now,cycle_type",
    [
        ("2026-08-03T08:30:00Z", cadence.OVERNIGHT),   # 04:30 ET
        ("2026-08-03T12:45:00Z", cadence.PREMARKET),   # 08:45 ET
        ("2026-08-03T20:30:00Z", cadence.POSTCLOSE),   # 16:30 ET
    ],
)
def test_no_cycle_outside_regular_hours_places_an_entry(now, cycle_type):
    # §13: the premarket cycle sets the day's regime and places nothing; the
    # overnight cycles are exits only; postclose cancels and reports.
    result = cycle(now=now, has_whole_share_position=True)

    assert result.cycle_type == cycle_type
    assert result.entries_allowed is False
    assert result.entries_remaining == 3  # the budget survives; the slot cannot use it
