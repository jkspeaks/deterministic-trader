"""§6.4 `MACRO` — regime rotation through the sector sleeve.

"Stated honestly: this is the least evidenced of the four." The
surprise→sector mapping is a heuristic in `config/macro_map.json`, and §6.4
puts it there so the play can be revised or retired without touching code. It
is also the first play §6.4 says to kill if attribution is unkind — which is
why the tests below assert against the shipped file rather than a fixture.

MACRO is the one play that does not scan symbols. A release tilts sectors, so
its candidates come out of the map, not out of §5.3's pool.
"""

import pytest

from src import plays
from tests.factories import MACRO_MAP, plays_context, proposal, proposals, skips


# These tests predate an earlier retirement and shelving of the event plays
# and use them as they then ran. See `all_plays_live` in conftest.py.
pytestmark = pytest.mark.usefixtures("all_plays_live")


def macro_ctx(*releases) -> dict:
    """A cycle whose only signal is the macro tape."""
    ctx = plays_context(candidates=[])
    ctx["macro"]["releases"] = [
        {"series": "CPI", "surprise_pct": 0.20, "sessions_ago": 0, **r}
        for r in (releases or [{}])
    ]
    return ctx


def test_a_hot_inflation_print_tilts_toward_energy_and_financials():
    result = plays.entries(macro_ctx())

    assert proposals(result, plays.MACRO) == ["XLE", "XLF"]


def test_a_cool_inflation_print_tilts_the_other_way():
    result = plays.entries(macro_ctx({"surprise_pct": -0.20}))

    assert proposals(result, plays.MACRO) == ["XLK", "XLY"]


def test_the_tilt_comes_from_the_shipped_file_and_not_from_code():
    """Retiring MACRO is emptying `releases` in the JSON. If the map were
    inlined here, §6.4's escape hatch would not exist."""
    ctx = macro_ctx()
    ctx["macro_map"] = {"releases": {}}

    assert plays.entries(ctx).candidates == []


def test_every_target_is_one_of_the_eleven_sector_spdrs():
    """§6.4 expresses macro through the sleeve and nothing else. `universe.py`
    refuses a target outside the rotation set independently, so a map that
    named SPY would silently produce nothing rather than fail loudly."""
    rotation = set(plays_context()["macro_map"]["releases"])
    targets = {
        symbol
        for series in MACRO_MAP["releases"].values()
        for key in ("above", "below")
        for symbol in series[key]
    }
    sleeve = set(plays_context()["sectors"]) | {
        "XLE", "XLF", "XLK", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC",
    }

    assert rotation  # the map is not empty in the shipped file
    assert targets <= sleeve


def test_a_release_nobody_mapped_produces_nothing():
    result = plays.entries(macro_ctx({"series": "GDP"}))

    assert result.candidates == []
    assert skips(result) == []


def test_a_release_three_sessions_back_is_stale():
    result = plays.entries(macro_ctx({"sessions_ago": 3}))

    assert result.candidates == []


@pytest.mark.parametrize("sessions_ago", [0, 1, 2])
def test_the_release_stays_tradeable_for_two_sessions(sessions_ago):
    result = plays.entries(macro_ctx({"sessions_ago": sessions_ago}))

    assert proposals(result, plays.MACRO) == ["XLE", "XLF"]


def test_an_in_line_print_has_no_direction_and_no_tilt():
    """A release that lands on consensus is a non-event. It is recorded rather
    than silent because the release itself did happen — §6.4's trigger fired
    and had nothing to say."""
    result = plays.entries(macro_ctx({"surprise_pct": 0.0}))

    assert result.candidates == []
    assert skips(result) == [plays.NO_CONVICTION]


def test_conviction_scales_with_the_surprise_against_consensus():
    small = plays.entries(macro_ctx({"surprise_pct": 0.05}))
    large = plays.entries(macro_ctx({"surprise_pct": 0.20}))

    assert proposal(small, "XLE").conviction_raw == pytest.approx(0.20)
    assert proposal(large, "XLE").conviction_raw == pytest.approx(0.80)


def test_conviction_is_capped_at_one():
    result = plays.entries(macro_ctx({"surprise_pct": 2.0}))

    assert proposal(result, "XLE").conviction_raw == 1.0


def test_a_downside_surprise_is_sized_on_its_magnitude_not_its_sign():
    """The tilt direction is the map's job; conviction is about how far from
    consensus the print landed, and −20% is as informative as +20%."""
    hot = plays.entries(macro_ctx({"surprise_pct": 0.20}))
    cool = plays.entries(macro_ctx({"surprise_pct": -0.20}))

    assert proposal(cool, "XLK").conviction_raw == proposal(hot, "XLE").conviction_raw


def test_two_releases_agreeing_on_a_sector_keep_the_stronger_reading():
    """CPI and PCE both tilt to XLE when hot. That is one position, and it
    should be sized on the better of the two signals rather than logged as one
    play outranking itself."""
    result = plays.entries(
        macro_ctx(
            {"series": "CPI", "surprise_pct": 0.05},
            {"series": "PCE", "surprise_pct": 0.20},
        )
    )

    assert proposal(result, "XLE").conviction_raw == pytest.approx(0.80)
    assert skips(result) == []


def test_a_threshold_move_in_a_tracked_series_enters_the_same_way():
    """§6.4's second trigger form. CI emits the same row shape for a scheduled
    release and for a series that crossed a threshold, so plays needs no second
    code path — which is the point of keeping interpretation out of CI (§7.3).
    """
    result = plays.entries(macro_ctx({"series": "NFP", "surprise_pct": 0.30}))

    assert proposals(result, plays.MACRO) == ["XLF", "XLI"]


def test_the_declared_holding_period_is_fifteen_sessions():
    # The longest of the four, and therefore §6.0's tiebreak loser.
    assert proposal(plays.entries(macro_ctx()), "XLE").expected_holding_days == 15


def test_macro_takes_five_percent_the_tightest_target_of_the_core_plays():
    # SECTOR is excluded from the comparison rather than beating it. It targets
    # 4% (§4.3 C) and is the same *kind* of trade — a sector ETF — on a tighter
    # clock, ten sessions against fifteen, which is why its target is tighter
    # too. It is also switched off, so "the tightest of what the account
    # actually runs" is still MACRO's.
    assert plays.PLAYS[plays.MACRO].target_pct == plays.MACRO_TARGET_PCT
    assert plays.MACRO_TARGET_PCT == min(
        play.target_pct
        for play_id, play in plays.PLAYS.items()
        if play_id != plays.SECTOR
    )
    assert plays.SECTOR_TARGET_PCT < plays.MACRO_TARGET_PCT
    assert plays.PLAYS[plays.SECTOR].holding_days < plays.PLAYS[plays.MACRO].holding_days


def test_a_macro_candidate_needs_no_earnings_or_volume_data():
    """MACRO still costs no Robinhood call of its own kind: it needs no
    earnings or fundamentals fetch. It does now read the trend signal every
    member of the candidate pool already carries (below) — see the 200-day
    tests for why."""
    ctx = macro_ctx()
    ctx["earnings"] = {}
    ctx["fundamentals"] = {}

    assert proposals(plays.entries(ctx), plays.MACRO) == ["XLE", "XLF"]


# --- the 200-day confirmation ----------------------------------


def test_a_target_below_its_two_hundred_day_is_refused():
    """PEAD requires price > the 50-day, CATALYST a confirmed move, DIP price
    > the 200-day. MACRO alone traded on the surprise sign with no check that
    the tape agrees — this closes it the same way DIP's own rule does."""
    ctx = macro_ctx()
    ctx["signals"]["XLE"]["sma_200"] = ctx["quotes"]["XLE"]["last"] + 5.0

    result = plays.entries(ctx)

    assert proposals(result, plays.MACRO) == ["XLF"]
    assert skips(result, "XLE") == [plays.ENTRY_CONDITIONS_UNMET]


def test_a_target_with_no_trend_data_cannot_confirm_and_is_refused():
    """Missing data cannot confirm a trend any more than it can for DIP or
    PEAD; production never sees this since a macro target is always in the
    candidate pool its signals get fetched for."""
    ctx = macro_ctx()
    del ctx["signals"]["XLE"]

    result = plays.entries(ctx)

    assert proposals(result, plays.MACRO) == ["XLF"]
    assert skips(result, "XLE") == [plays.ENTRY_CONDITIONS_UNMET]


def test_exactly_on_the_two_hundred_day_is_not_holding_it():
    """Same boundary DIP draws: a moving average is not a floor, and a target
    sitting exactly on its 200-day has not demonstrated it holds above one."""
    ctx = macro_ctx()
    ctx["signals"]["XLE"]["sma_200"] = ctx["quotes"]["XLE"]["last"]

    result = plays.entries(ctx)

    assert proposals(result, plays.MACRO) == ["XLF"]


# --- the stale-file hazard -----------


def test_a_frozen_macro_file_does_not_keep_an_old_release_live():
    """`data/macro.json` freezes the same way `data/events.json` does if CI
    stops, and a June CPI print reading as today's would tilt the book on
    two-month-old news. `first_seen` is absolute and is counted from here."""
    stale = macro_ctx({"sessions_ago": 0, "first_seen": "2026-06-01"})

    assert proposals(plays.entries(stale), plays.MACRO) == []


def test_the_absolute_date_wins_over_the_published_count_for_a_release():
    """Counted, not sanity-checked: a release claiming to be forty-five
    sessions old still tilts when it landed this morning."""
    fresh = macro_ctx({"sessions_ago": 45, "first_seen": "2026-08-03"})

    assert proposals(plays.entries(fresh), plays.MACRO) == ["XLE", "XLF"]


def test_a_release_without_first_seen_falls_back_to_the_published_count():
    """Same fallback as `CATALYST`, for the same reason."""
    assert proposals(plays.entries(macro_ctx()), plays.MACRO) == ["XLE", "XLF"]
