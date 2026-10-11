"""The weekend gap, market holidays, and half-days (§13, P5).

P5 measured the weekend gap: the equity band runs Sunday 20:00 ET to Friday
20:00 ET and "Saturday 04:30 ET and Sunday 04:30 ET are dead slots [that] must
not fire". Holidays are not in design v3 at all — a cycle firing on
Thanksgiving would otherwise compute a `full` and try to trade — so the
checked-in calendar is the gap this file closes.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from src import cadence
from tests.factories import cadence_context

CALENDAR = json.loads(
    (Path(__file__).resolve().parents[1] / "config" / "market_holidays.json").read_text(
        encoding="utf-8"
    )
)

# The book is non-empty so nothing here can no-op for lack of work; the only
# thing under test is whether the market is open.
HELD = {"positions_held": 3, "has_whole_share_position": True}


def cycle(now: str, calendar=None, **overrides):
    return cadence.decide(
        cadence_context(
            now=now,
            calendar=calendar if calendar is not None else CALENDAR,
            control=dict(HELD, **overrides.pop("control", {})),
            **overrides,
        )
    )


# --- the weekend gap ------------------------------------------------------


@pytest.mark.parametrize(
    "now,label",
    [
        ("2026-08-01T08:30:00Z", "Saturday 04:30 ET"),
        ("2026-08-01T13:40:00Z", "Saturday 09:40 ET"),
        ("2026-08-02T08:30:00Z", "Sunday 04:30 ET"),
        ("2026-08-02T23:59:00Z", "Sunday 19:59 ET, one minute before the open"),
    ],
)
def test_the_weekend_dead_slots_do_not_fire(now, label):
    result = cycle(now)

    assert result.cycle_type == cadence.NOOP, label
    assert result.session == cadence.CLOSED
    assert result.reconcile_required is False


def test_the_week_opens_at_sunday_twenty_hundred():
    # P5: the band runs Sunday 20:00 ET onward, and that first cycle belongs
    # to Monday's trading day.
    result = cycle("2026-08-03T00:00:00Z")

    assert result.cycle_type == cadence.OVERNIGHT
    assert result.trading_day == "2026-08-03"


def test_friday_twenty_hundred_is_already_the_weekend():
    """The Friday close is the last cycle of the week, not the first of the gap.

    A naive "is today a weekday" check passes here and is wrong: 20:00 Friday
    belongs to Saturday's trading day, and P5 measured quotes frozen from
    exactly this instant.
    """
    result = cycle("2026-08-08T00:00:00Z")

    assert result.cycle_type == cadence.NOOP
    assert result.session == cadence.CLOSED


def test_one_minute_before_the_friday_close_still_trades():
    # 19:59 ET is the tail of Friday's after-hours band, so the live cycle
    # there is `postclose`. One minute later the week is shut.
    result = cycle("2026-08-07T23:59:00Z")

    assert result.session == cadence.EXTENDED_HOURS
    assert result.cycle_type == cadence.POSTCLOSE


# --- full closures --------------------------------------------------------


def test_thanksgiving_does_not_trade():
    # 2026-11-26, 09:40 ET in EST.
    result = cycle("2026-11-26T14:40:00Z")

    assert result.cycle_type == cadence.NOOP
    assert result.session == cadence.CLOSED


def test_the_overnight_session_into_a_holiday_does_not_run():
    """Wednesday 20:00 ET opens Thanksgiving's overnight session, so it is shut.

    This is the case a calendar keyed to the wall-clock date gets wrong: the
    stamp says Wednesday, a trading day, while the session it opens belongs to
    a holiday.
    """
    result = cycle("2026-11-26T01:00:00Z")  # Wed 2026-11-25, 20:00 ET

    assert result.trading_day == "2026-11-26"
    assert result.cycle_type == cadence.NOOP


def test_the_evening_after_a_holiday_reopens():
    # Thursday 20:00 ET opens Friday, which trades (a half-day).
    result = cycle("2026-11-27T01:00:00Z")

    assert result.trading_day == "2026-11-27"
    assert result.cycle_type == cadence.OVERNIGHT


# --- half-days ------------------------------------------------------------


def test_a_half_day_closes_the_regular_session_at_thirteen_hundred():
    # 13:40 ET on 2026-11-27 is after the 13:00 close.
    result = cycle("2026-11-27T18:40:00Z")

    assert result.session == cadence.EXTENDED_HOURS
    assert result.cycle_type == cadence.POSTCLOSE


def test_a_half_day_still_trades_before_its_close():
    # The morning's full cycle already ran, so this is a plain monitor slot.
    result = cycle(
        "2026-11-27T17:00:00Z",  # 12:00 ET
        control={"last_full_cycle": "2026-11-27T14:40:00Z"},
    )

    assert result.session == cadence.REGULAR_HOURS
    assert result.cycle_type == cadence.MONITOR


def test_the_entry_blackout_moves_with_an_early_close():
    """§13 stops entries in the last 30 minutes, measured from the real close.

    On a 13:00 day that is 12:30, not 15:30. A fixed 15:30 deadline would let
    the system open a position 20 minutes before a half-day close with no
    monitor cycle left to manage it.
    """
    assert cycle("2026-11-27T17:29:00Z").entries_allowed is True   # 12:29 ET
    assert cycle("2026-11-27T17:30:00Z").entries_allowed is False  # 12:30 ET


# --- the file itself ------------------------------------------------------


def test_the_calendar_covers_the_current_year_and_the_next():
    """A calendar that has quietly run out is indistinguishable from a year
    with no holidays, and would trade straight through Thanksgiving."""
    years = {int(d[:4]) for d in CALENDAR["closed"]}
    this_year = date.today().year

    assert this_year in years
    assert this_year + 1 in years


def test_every_calendar_date_parses_and_no_closure_is_a_weekend():
    """A holiday listed on a Saturday is a transcription error: the weekend is
    already closed, so the entry does nothing and the real observed weekday
    closure is missing."""
    for iso in CALENDAR["closed"]:
        assert date.fromisoformat(iso).weekday() < cadence.SATURDAY, iso

    for iso, at in CALENDAR["early_close"].items():
        assert date.fromisoformat(iso).weekday() < cadence.SATURDAY, iso
        assert cadence.close_time(date.fromisoformat(iso), CALENDAR) < (
            cadence.REGULAR_CLOSE
        ), at


def test_no_date_is_both_closed_and_an_early_close():
    assert not set(CALENDAR["closed"]) & set(CALENDAR["early_close"])
