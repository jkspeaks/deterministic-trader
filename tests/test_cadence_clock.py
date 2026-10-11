"""The ET clock and the four session bands (§13, P5).

Every §13 boundary is an ET wall clock while the routine hands cadence a UTC
stamp, so the conversion is load-bearing for every other decision in the
module. These tests pin it, including both DST transitions — the failure mode
there is an hour of wrong classification that reappears twice a year and looks
like a scheduler glitch.
"""

import pytest

from src import cadence
from tests.factories import cadence_context


def cycle(now: str, **overrides):
    return cadence.decide(cadence_context(now=now, **overrides))


# --- daylight saving ------------------------------------------------------


def test_the_open_is_found_in_summer_when_et_is_utc_minus_four():
    # 2026-06-15 is a Monday in EDT: 09:40 ET is 13:40 UTC.
    assert cycle("2026-06-15T13:40:00Z").session == cadence.REGULAR_HOURS


def test_the_open_is_found_in_winter_when_et_is_utc_minus_five():
    # 2026-12-14 is a Monday in EST: the same 09:40 ET is 14:40 UTC.
    assert cycle("2026-12-14T14:40:00Z").session == cadence.REGULAR_HOURS


def test_the_summer_utc_stamp_is_pre_market_in_winter():
    """The same UTC instant is a different session on either side of DST.

    This is the assertion that would fail if the offset were hardcoded: 13:40
    UTC is the open in June and 08:40 ET — pre-market — in December.
    """
    assert cycle("2026-12-14T13:40:00Z").session == cadence.EXTENDED_HOURS


# --- the four bands from `trading://market-hours` -------------------------


@pytest.mark.parametrize(
    "now,expected",
    [
        ("2026-08-03T08:30:00Z", cadence.ALL_DAY_HOURS),   # 04:30 ET
        ("2026-08-03T10:59:00Z", cadence.ALL_DAY_HOURS),   # 06:59 ET
        ("2026-08-03T11:00:00Z", cadence.EXTENDED_HOURS),  # 07:00 ET, pre-market
        ("2026-08-03T12:45:00Z", cadence.EXTENDED_HOURS),  # 08:45 ET
        ("2026-08-03T13:29:00Z", cadence.EXTENDED_HOURS),  # 09:29 ET
        ("2026-08-03T13:30:00Z", cadence.REGULAR_HOURS),   # 09:30 ET, the open
        ("2026-08-03T19:59:00Z", cadence.REGULAR_HOURS),   # 15:59 ET
        ("2026-08-03T20:00:00Z", cadence.EXTENDED_HOURS),  # 16:00 ET, the close
        ("2026-08-03T20:30:00Z", cadence.EXTENDED_HOURS),  # 16:30 ET
        ("2026-08-03T23:59:00Z", cadence.EXTENDED_HOURS),  # 19:59 ET
        ("2026-08-04T00:00:00Z", cadence.ALL_DAY_HOURS),   # 20:00 ET
    ],
)
def test_each_session_boundary_lands_on_the_documented_side(now, expected):
    assert cycle(now).session == expected


# --- the trading day, and the 20:00 shift ---------------------------------


def test_twenty_hundred_belongs_to_the_next_trading_day():
    """The overnight session opens at 20:00 and runs to 07:00 the next morning.

    Monday 20:00 ET is therefore already Tuesday's trading day, which is the
    single shift that makes the weekend and holiday checks fall out of one
    test rather than three.
    """
    assert cycle("2026-08-04T00:00:00Z").trading_day == "2026-08-04"


def test_one_minute_before_twenty_hundred_is_still_today():
    assert cycle("2026-08-03T23:59:00Z").trading_day == "2026-08-03"


def test_the_small_hours_belong_to_the_day_they_land_in():
    # 04:30 ET Tuesday is Tuesday's trading day, not Monday's.
    assert cycle("2026-08-04T08:30:00Z").trading_day == "2026-08-04"
