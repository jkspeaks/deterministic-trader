"""A whole trading day, slot by slot, golden-file tested (§13, §15).

§13 schedules ~11 invocations per trading day. This walks all eleven in order,
threading each cycle's `clock_updates` into the next one's control state the
way the routine does, so the golden file is a simulated day rather than
eleven unrelated snapshots.

Two days are recorded, and the difference between them *is* the P1 finding:

* `day_fractional` — the account as it stands (Appendix A.1): ten fractional
  positions, no whole share. Both overnight cycles demote to no-ops, because
  no exit they could decide on is placeable.
* `day_whole` — the same day once the ladder has produced a whole-share
  position. All eleven slots do work.

Nothing changes between them but one flag in `edge_control`, which is what
Invariant 5 means by capability being computed rather than configured.
"""

import json
from pathlib import Path

import pytest

from src import cadence
from tests.factories import cadence_context

GOLDEN = Path(__file__).parent / "golden" / "cadence"

# 04:30, 08:45, 09:40, six hourly monitors from 10:40, 16:30, and 20:00 ET.
SLOTS = [
    "2026-08-03T08:30:00Z",
    "2026-08-03T12:45:00Z",
    "2026-08-03T13:40:00Z",
    "2026-08-03T14:40:00Z",
    "2026-08-03T15:40:00Z",
    "2026-08-03T16:40:00Z",
    "2026-08-03T17:40:00Z",
    "2026-08-03T18:40:00Z",
    "2026-08-03T19:40:00Z",
    "2026-08-03T20:30:00Z",
    "2026-08-04T00:00:00Z",
]

CALENDAR = json.loads(
    (Path(__file__).resolve().parents[1] / "config" / "market_holidays.json").read_text(
        encoding="utf-8"
    )
)


def walk(**control) -> list[dict]:
    """Run every slot in order, carrying the two clocks forward."""
    state = {
        "last_full_cycle": "2026-07-31T13:40:00Z",  # Friday's full cycle
        "last_overnight_cycle": "2026-07-31T20:00:00Z",
        "new_positions": {"date": None, "count": 0},
        "unreconciled": False,
        "pending_events": False,
        **control,
    }

    day = []
    for now in SLOTS:
        cycle = cadence.decide(
            cadence_context(now=now, control=dict(state), calendar=CALENDAR)
        )
        state.update(cycle.clock_updates)
        day.append(cycle.as_dict())
    return day


FRACTIONAL = {"positions_held": 10, "has_whole_share_position": False}
WHOLE = {"positions_held": 3, "has_whole_share_position": True}


def types(day) -> list[str]:
    return [c["cycle_type"] for c in day]


# --- the shape of a day ---------------------------------------------------


def test_the_account_as_it_stands_works_nine_of_eleven_slots():
    """Both overnight cycles are paid-for no-ops until a whole share exists."""
    day = types(walk(**FRACTIONAL))

    assert day == [
        cadence.NOOP,       # 04:30 — no placeable overnight exit (P1)
        cadence.PREMARKET,  # 08:45
        cadence.FULL,       # 09:40
        cadence.MONITOR,    # 10:40
        cadence.MONITOR,    # 11:40
        cadence.MONITOR,    # 12:40
        cadence.MONITOR,    # 13:40
        cadence.MONITOR,    # 14:40
        cadence.MONITOR,    # 15:40
        cadence.POSTCLOSE,  # 16:30
        cadence.NOOP,       # 20:00 — same
    ]


def test_a_whole_share_position_lights_up_both_overnight_cycles():
    day = types(walk(**WHOLE))

    assert day[0] == cadence.OVERNIGHT
    assert day[-1] == cadence.OVERNIGHT
    assert day[1:-1] == types(walk(**FRACTIONAL))[1:-1]


def test_exactly_one_full_cycle_runs_in_a_day():
    """The 09:40 slot claims it and the five later monitors do not re-run it —
    the property that keeps `last_full_cycle` from being advanced twice."""
    assert types(walk(**WHOLE)).count(cadence.FULL) == 1


def test_only_the_full_cycle_advances_the_full_clock():
    day = walk(**WHOLE)
    stamped = [c["now"] for c in day if "last_full_cycle" in c["clock_updates"]]

    assert stamped == ["2026-08-03T13:40:00Z"]


def test_both_overnight_cycles_stamp_the_overnight_clock():
    day = walk(**WHOLE)
    stamped = [c["now"] for c in day if "last_overnight_cycle" in c["clock_updates"]]

    assert stamped == ["2026-08-03T08:30:00Z", "2026-08-04T00:00:00Z"]


def test_entries_open_at_the_full_cycle_and_shut_before_the_close():
    allowed = [c["entries_allowed"] for c in walk(**WHOLE)]

    #      04:30  08:45  09:40  10:40 11:40 12:40 13:40 14:40  15:40  16:30  20:00
    assert allowed == [
        False, False, True, True, True, True, True, True, False, False, False
    ]


def test_every_cycle_that_reconciles_is_a_full_monitor_or_post_close():
    # §12.1 names exactly those three, and never a no-op.
    for cycle in walk(**FRACTIONAL):
        assert cycle["reconcile_required"] == (
            cycle["cycle_type"] in (cadence.FULL, cadence.MONITOR, cadence.POSTCLOSE)
        )


# --- the golden files -----------------------------------------------------


@pytest.mark.parametrize(
    "name,control", [("day_fractional", FRACTIONAL), ("day_whole", WHOLE)]
)
def test_the_day_matches_its_golden_file(name, control):
    expected = json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))

    assert walk(**control) == expected
