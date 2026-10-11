"""§6.1 `PEAD` — post-earnings drift.

"The best-evidenced anomaly on this horizon, and entirely free to serve."
Trigger is a report in the last 1–3 sessions; entry needs a positive surprise,
a confirming report-day reaction and price above the 50-day.

The fixture raises F's RSI out of `DIP` range on purpose. A name that just beat
and rallied 3% is not oversold, and letting both plays fire here would make
every assertion below a statement about §6.0 arbitration instead of about PEAD.
"""

import pytest

from src import plays
from tests.factories import plays_context, proposal, proposals, skips


# These tests predate an earlier retirement and shelving of the event plays
# and use them as they then ran. See `all_plays_live` in conftest.py.
pytestmark = pytest.mark.usefixtures("all_plays_live")


def pead_ctx(**earnings) -> dict:
    """F reported two sessions ago, beat by 7%, and rose 3% on the day."""
    ctx = plays_context()
    ctx["signals"]["F"]["sma_50"] = 14.00
    ctx["signals"]["F"]["rsi"] = 55.0
    ctx["earnings"]["F"] = {
        "status": "known",
        "next_report_date": "2026-10-28",
        "sessions_until_report": 60,
        "sessions_since_report": 2,
        "eps_surprise_pct": 7.0,
        "reaction_pct": 0.03,
        **earnings,
    }
    return ctx


def test_a_beat_that_the_tape_confirmed_fires():
    result = plays.entries(pead_ctx())

    assert proposals(result, plays.PEAD) == ["F"]


def test_a_report_today_has_not_drifted_yet_and_does_not_trigger():
    """§6.1 counts from one session, not zero. The reaction is the entry
    condition, so there is nothing to confirm on the day it prints."""
    result = plays.entries(pead_ctx(sessions_since_report=0))

    assert proposals(result, plays.PEAD) == []
    assert skips(result) == []


def test_a_report_four_sessions_back_is_outside_the_window():
    result = plays.entries(pead_ctx(sessions_since_report=4))

    assert proposals(result, plays.PEAD) == []
    assert skips(result) == []


@pytest.mark.parametrize("sessions", [1, 2, 3])
def test_the_whole_one_to_three_session_window_triggers(sessions):
    result = plays.entries(pead_ctx(sessions_since_report=sessions))

    assert proposals(result, plays.PEAD) == ["F"]


def test_a_miss_is_refused_however_well_the_tape_took_it():
    """A stock can rally on a miss. §6.1 requires the surprise *and* the
    reaction, and drift is the anomaly around the surprise."""
    result = plays.entries(pead_ctx(eps_surprise_pct=-2.0))

    assert proposals(result, plays.PEAD) == []
    assert skips(result, "F") == [plays.ENTRY_CONDITIONS_UNMET]


def test_a_beat_the_tape_ignored_is_refused():
    result = plays.entries(pead_ctx(reaction_pct=0.01))

    assert skips(result, "F") == [plays.ENTRY_CONDITIONS_UNMET]


def test_a_two_percent_reaction_is_exactly_enough():
    result = plays.entries(pead_ctx(reaction_pct=0.02))

    assert proposals(result, plays.PEAD) == ["F"]


def test_a_beat_below_the_fifty_day_is_refused():
    ctx = pead_ctx()
    ctx["signals"]["F"]["sma_50"] = 15.50

    result = plays.entries(ctx)

    assert skips(result, "F") == [plays.ENTRY_CONDITIONS_UNMET]


def test_a_flat_surprise_is_not_a_positive_one():
    """§6.1 says surprise > 0. In-line is the null result, not a weak beat."""
    result = plays.entries(pead_ctx(eps_surprise_pct=0.0))

    assert skips(result, "F") == [plays.ENTRY_CONDITIONS_UNMET]


def test_conviction_scales_with_the_size_of_the_beat():
    # reaction pinned at the gate floor on the small side so the confirming
    # move (folded in below) cannot outscore the surprise this test isolates.
    small = plays.entries(pead_ctx(eps_surprise_pct=2.0, reaction_pct=0.02))
    large = plays.entries(pead_ctx(eps_surprise_pct=8.0))

    assert proposal(small, "F").conviction_raw == pytest.approx(0.20)
    assert proposal(large, "F").conviction_raw == pytest.approx(0.80)


def test_conviction_also_scales_with_the_size_of_the_confirming_reaction():
    """§6.1's surprise was the only input; two names with an identical, modest
    beat could still tie even though one's tape reaction was three times the
    other's. Mirrors how `CATALYST` already sizes on its confirming move."""
    weak_tape = plays.entries(pead_ctx(eps_surprise_pct=2.0, reaction_pct=0.02))
    strong_tape = plays.entries(pead_ctx(eps_surprise_pct=2.0, reaction_pct=0.08))

    assert proposal(strong_tape, "F").conviction_raw > proposal(weak_tape, "F").conviction_raw
    assert proposal(strong_tape, "F").conviction_raw == pytest.approx(0.80)


def test_a_strong_reaction_never_marks_conviction_down():
    """`max`, not an average: a beat that already clears the gate on surprise
    alone is never scored lower for a reaction that was merely adequate."""
    result = plays.entries(pead_ctx(eps_surprise_pct=8.0, reaction_pct=0.02))

    assert proposal(result, "F").conviction_raw == pytest.approx(0.80)


def test_conviction_is_capped_at_one():
    """§6.1 says so in as many words. A 60% surprise is usually a broken
    consensus figure rather than six times the edge of a 10% one."""
    result = plays.entries(pead_ctx(eps_surprise_pct=60.0))

    assert proposal(result, "F").conviction_raw == 1.0


def test_a_report_with_no_surprise_figure_cannot_be_sized_and_is_refused():
    result = plays.entries(pead_ctx(eps_surprise_pct=None))

    assert skips(result, "F") == [plays.SKIP_NO_SURPRISE]


def test_the_declared_holding_period_is_ten_sessions():
    assert proposal(plays.entries(pead_ctx()), "F").expected_holding_days == 10


def test_the_target_is_eight_percent():
    # §6.1's exit row, declared once so §6.5 and the runlog read the same number.
    assert plays.PLAYS[plays.PEAD].target_pct == pytest.approx(0.08)


# --- P2: a missing reaction row is not a failed test -----------


def test_a_missing_reaction_row_has_its_own_code_before_any_threshold():
    """67 of 79 PEAD skips read "reaction None" inside "does not clear §6.1"."""
    result = plays.entries(pead_ctx(reaction_pct=None))

    assert proposals(result, plays.PEAD) == []
    assert skips(result, "F") == [plays.SKIP_NO_REACTION_ROW]
    assert "no reaction row" in result.skipped[0].message


def test_the_missing_row_is_named_even_when_another_clause_would_also_fail():
    """Before any threshold: a miss with no row is still a missing row."""
    ctx = pead_ctx(reaction_pct=None, eps_surprise_pct=-2.0)
    ctx["signals"]["F"]["sma_50"] = 15.50

    assert skips(plays.entries(ctx), "F") == [plays.SKIP_NO_REACTION_ROW]


def test_a_zero_reaction_is_a_reaction_and_not_a_missing_one():
    result = plays.entries(pead_ctx(reaction_pct=0.0))

    assert skips(result, "F") == [plays.ENTRY_CONDITIONS_UNMET]
    assert "reaction +0.0%" in result.skipped[0].message


def test_a_missing_surprise_is_a_different_code_from_a_missing_reaction():
    assert skips(plays.entries(pead_ctx(eps_surprise_pct=None)), "F") == [
        plays.SKIP_NO_SURPRISE
    ]
    assert plays.SKIP_NO_SURPRISE != plays.SKIP_NO_REACTION_ROW


def test_a_zero_surprise_is_a_surprise_and_not_a_missing_one():
    assert skips(plays.entries(pead_ctx(eps_surprise_pct=0.0)), "F") == [
        plays.ENTRY_CONDITIONS_UNMET
    ]


@pytest.mark.parametrize(
    "timing, since, expected",
    [
        ("am", 1, "SKIP_NO_REACTION_ROW"),   # reported before the open: bar closed
        ("pm", 1, "SKIP_REACTION_PENDING"),  # reacts today: no closed bar yet
        (None, 1, "SKIP_REACTION_PENDING"),  # unknown takes the pm rule
        ("pm", 2, "SKIP_NO_REACTION_ROW"),
        ("pm", 3, "SKIP_NO_REACTION_ROW"),
    ],
)
def test_a_row_that_cannot_exist_yet_is_pending_not_missing(timing, since, expected):
    """`pead_reaction.py` measures a `pm` report on the next session's bar,
    which is not complete on the first session after the report."""
    result = plays.entries(
        pead_ctx(reaction_pct=None, timing=timing, sessions_since_report=since)
    )

    assert skips(result, "F") == [expected]


def test_a_missing_price_or_fifty_day_is_missing_data():
    ctx = pead_ctx()
    del ctx["signals"]["F"]["sma_50"]

    assert skips(plays.entries(ctx), "F") == [plays.SKIP_NO_TREND_DATA]


def test_a_genuine_failure_names_the_clause_that_failed():
    result = plays.entries(pead_ctx(reaction_pct=0.01))

    assert "reaction +1.0% is under the +2% confirmation" in result.skipped[0].message
    assert "None" not in result.skipped[0].message


def test_nothing_is_inferred_in_place_of_a_missing_reaction():
    """A +9% day on the quote is not a reaction row."""
    ctx = pead_ctx(reaction_pct=None)
    ctx["quotes"]["F"]["change_pct"] = 0.09

    assert proposals(plays.entries(ctx), plays.PEAD) == []
