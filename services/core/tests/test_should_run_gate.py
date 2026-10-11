"""A pure-Python mirror of `edge_should_run`'s monitor-slot gate and its entries-remaining arithmetic, tested without a database.

There is no DB in CI (the suite runs against a fake Supabase, never Postgres
— `services/core/tests/conftest.py`'s own docstring says so), so this file is
the SQL function's contract: a straight line-for-line port of the two blocks,
kept in sync by hand. Applying `db/schema.sql` to a real database is the live
half of that promise.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping

# The schema's literal, kept as the fallback when `edge_control` has never seen
# the schedule job. **The fallback is the OLD number, deliberately**: a
# missing key cannot then silently raise a cap.
DEFAULT_CAP = 2


def entries_remaining(control: Mapping[str, Any], trading_day: date) -> int:
    """Mirrors `edge_should_run`'s `cap`/`remaining` block.

    The cap was once the literal `2` while `config/config.json` has
    said `3`, so a monitor slot could be skipped on a day a
    third entry was still allowed. The 04:30 schedule job now publishes
    `max_new_positions_per_day` into `edge_control` — Postgres cannot read the
    git checkout — and this reads it from there.

    The count carries the day it was taken on and so self-resets, which is
    `cadence._entries_remaining`'s rule and the reason no separate daily reset
    exists to go stale.
    """
    cap = control.get("max_new_positions_per_day")
    cap = DEFAULT_CAP if cap is None else int(cap)

    taken = control.get("new_positions") or {}
    if taken.get("date") != trading_day.isoformat():
        return cap
    return max(0, cap - int(taken.get("count") or 0))


def monitor_gate(
    control: Mapping[str, Any],
    trading_day: date,
    remaining: int,
) -> dict | None:
    """Mirrors `db/schema.sql`'s two `if` blocks for a
    `monitor` slot. Returns a `{"run": ..., "reason": ...}` verdict if this
    gate decides the outcome, or `None` to mean "fall through" — exactly what
    the SQL does by not returning.
    """
    needs = control.get("needs_session")
    has_needs = needs is not None

    if has_needs:
        reason = (needs or {}).get("reason") or "needs_session set"
        return {"run": True, "reason": f"state change: {reason}"}

    last_full = control.get("last_full_cycle")
    full_today = (
        last_full is not None
        and datetime.fromisoformat(last_full).date() == trading_day
    )

    fresh_candidates = bool(
        ((control.get("dip_recovery_watchlist") or {}).get("symbols") or [])
    )

    if full_today and (remaining == 0 or not fresh_candidates):
        return {
            "run": False,
            "reason": (
                "quiet: no state change, full cycle already ran today, "
                "and entries/candidates are exhausted"
            ),
        }

    return None


DAY = date(2026, 9, 11)


def test_needs_session_null_and_no_candidates_is_a_noop_for_an_11_00_slot():
    control = {
        "needs_session": None,
        "last_full_cycle": "2026-09-11T13:40:00+00:00",
        "dip_recovery_watchlist": {"symbols": []},
    }

    verdict = monitor_gate(control, DAY, remaining=2)

    assert verdict == {
        "run": False,
        "reason": (
            "quiet: no state change, full cycle already ran today, "
            "and entries/candidates are exhausted"
        ),
    }


def test_needs_session_set_runs_true_with_reason_regardless_of_candidates():
    control = {
        "needs_session": {"reason": "near_stop: MGM", "symbols": ["MGM"]},
        "last_full_cycle": "2026-09-11T13:40:00+00:00",
        "dip_recovery_watchlist": {"symbols": []},
    }

    verdict = monitor_gate(control, DAY, remaining=0)

    assert verdict == {"run": True, "reason": "state change: near_stop: MGM"}


def test_needs_session_null_but_entries_remaining_and_candidates_exist_falls_through():
    control = {
        "needs_session": None,
        "last_full_cycle": "2026-09-11T13:40:00+00:00",
        "dip_recovery_watchlist": {"symbols": ["F"]},
    }

    # Candidates exist AND entries remain -> neither half of the OR is true,
    # so the quiet gate does not fire; the generic noop check downstream
    # decides instead (out of this file's scope).
    assert monitor_gate(control, DAY, remaining=1) is None


def test_full_cycle_has_not_run_yet_today_never_gates_the_slot():
    control = {
        "needs_session": None,
        "last_full_cycle": "2026-09-10T13:40:00+00:00",  # yesterday
        "dip_recovery_watchlist": {"symbols": []},
    }

    assert monitor_gate(control, DAY, remaining=0) is None


def test_entries_remaining_zero_alone_is_enough_even_with_candidates():
    control = {
        "needs_session": None,
        "last_full_cycle": "2026-09-11T13:40:00+00:00",
        "dip_recovery_watchlist": {"symbols": ["F"]},
    }

    verdict = monitor_gate(control, DAY, remaining=0)

    assert verdict is not None and verdict["run"] is False


# ---------------------------------------------------------------------------
# The published cap
# ---------------------------------------------------------------------------


def test_the_published_cap_is_what_the_arithmetic_uses():
    control = {
        "max_new_positions_per_day": 3,
        "new_positions": {"date": DAY.isoformat(), "count": 2},
    }

    # Under a hard-coded 2 this read 0 and the slot was gated out on a
    # day a third entry was still allowed.
    assert entries_remaining(control, DAY) == 1


def test_an_absent_cap_falls_back_to_db_023s_two_not_to_the_config_value():
    control = {"new_positions": {"date": DAY.isoformat(), "count": 2}}

    assert entries_remaining(control, DAY) == 0


def test_yesterdays_count_does_not_carry_into_today():
    control = {
        "max_new_positions_per_day": 3,
        "new_positions": {"date": "2026-09-10", "count": 3},
    }

    assert entries_remaining(control, DAY) == 3


def test_the_count_never_goes_negative_when_a_cap_is_lowered_mid_day():
    control = {
        "max_new_positions_per_day": 2,
        "new_positions": {"date": DAY.isoformat(), "count": 3},
    }

    assert entries_remaining(control, DAY) == 0


def test_the_quiet_gate_and_the_cap_compose_the_way_the_sql_orders_them():
    """`remaining` is computed before the monitor gate consults it, so a
    raised cap is what stops the quiet gate firing on a day with entries left
    and fresh candidates."""
    control = {
        "needs_session": None,
        "last_full_cycle": "2026-09-11T13:40:00+00:00",
        "max_new_positions_per_day": 3,
        "new_positions": {"date": DAY.isoformat(), "count": 2},
        "dip_recovery_watchlist": {"symbols": ["F"]},
    }

    assert monitor_gate(control, DAY, entries_remaining(control, DAY)) is None
