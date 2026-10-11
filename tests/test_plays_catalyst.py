"""§6.3 `CATALYST` — event-driven, and the highest-variance of the four.

"Direction comes from price, not from text." An 8-K item code says something
material happened, never whether it was good, so the entry requires a
confirming price reaction on confirming volume and the system never classifies
sentiment.

The fixture raises F's RSI out of `DIP` range so these assertions are about
CATALYST rather than about §6.0 arbitration.
"""

import json
from pathlib import Path

import pytest

from src import cli, plays
from src.plays import catalyst
from tests.factories import config, plays_context, proposal, proposals, skips


# These tests predate an earlier retirement and shelving of the event plays
# and use them as they then ran. See `all_plays_live` in conftest.py.
pytestmark = pytest.mark.usefixtures("all_plays_live")


def catalyst_ctx(
    *, items=("9.01",), sessions_ago=1, events=None,
    change_pct=0.05, volume=90_000_000, avg_volume=50_000_000, **signals,
) -> dict:
    """F filed an 8-K yesterday and rose 5% on 1.8x average volume."""
    ctx = plays_context()
    ctx["signals"]["F"].update({"rsi": 55.0, **signals})
    ctx["quotes"]["F"]["change_pct"] = change_pct
    ctx["fundamentals"]["F"] = {"volume": volume, "average_volume": avg_volume}
    ctx["events"]["F"] = (
        events
        if events is not None
        else [{"type": "8-K", "items": list(items), "sessions_ago": sessions_ago}]
    )
    return ctx


def test_an_eight_k_the_tape_confirmed_fires():
    result = plays.entries(catalyst_ctx())

    assert proposals(result, plays.CATALYST) == ["F"]


def test_an_eight_k_three_sessions_back_is_outside_the_window():
    result = plays.entries(catalyst_ctx(sessions_ago=3))

    assert proposals(result, plays.CATALYST) == []
    assert skips(result) == []


@pytest.mark.parametrize("sessions_ago", [0, 1, 2])
def test_the_whole_two_session_window_triggers(sessions_ago):
    result = plays.entries(catalyst_ctx(sessions_ago=sessions_ago))

    assert proposals(result, plays.CATALYST) == ["F"]


# --- the hard veto --------------------------------------------------------


def test_a_restatement_is_vetoed_however_well_the_tape_took_it():
    """§6.3's hard veto. Item 4.02 is non-reliance — the company has said its
    own published financials cannot be trusted, and no price reaction makes
    that a trade."""
    result = plays.entries(catalyst_ctx(items=("4.02",)))

    assert proposals(result, plays.CATALYST) == []
    assert skips(result, "F") == [plays.HARD_VETO_RESTATEMENT]


def test_the_veto_survives_being_filed_alongside_routine_items():
    """EDGAR's `items` field is a comma-joined list and 4.02 rarely arrives
    alone. A check that only read the first code would miss most of them."""
    result = plays.entries(catalyst_ctx(items=("2.02", "4.02", "9.01")))

    assert skips(result, "F") == [plays.HARD_VETO_RESTATEMENT]


def test_a_restatement_blocks_every_play_on_that_name_not_only_catalyst():
    """§6.3 files the veto under CATALYST and §6.5 makes the *exit* on 4.02
    global. Entering on any play would propose a position exit 1 flattens on
    the very next cycle, so the veto is read at the name.
    """
    ctx = plays_context()  # F is a clean DIP trigger here
    ctx["events"]["F"] = [{"type": "8-K", "items": ["4.02"], "sessions_ago": 1}]

    result = plays.entries(ctx)

    assert result.candidates == []
    # The filing triggers CATALYST and the oversold reading triggers DIP. Both
    # record the veto: which plays wanted the name is the attribution a reader
    # chasing the symbol later actually needs.
    assert sorted((s.play_id, s.code) for s in result.skipped) == [
        (plays.CATALYST, plays.HARD_VETO_RESTATEMENT),
        (plays.DIP, plays.HARD_VETO_RESTATEMENT),
    ]


def test_an_officer_departure_does_not_veto_an_entry():
    """5.02 force-*exits* a held name under §6.5 but is not in §6.3's veto row.
    A CEO change is material and directional-neutral, which is precisely what
    the price-confirmation rule is for."""
    result = plays.entries(catalyst_ctx(items=("5.02",)))

    assert proposals(result, plays.CATALYST) == ["F"]


# --- price and volume confirmation ----------------------------------------


def test_a_reaction_under_three_percent_is_not_confirmation():
    result = plays.entries(catalyst_ctx(change_pct=0.02))

    assert skips(result, "F") == [plays.SKIP_BELOW_MIN_MOVE]


def test_a_fall_is_never_confirmation_however_large():
    """The trap this play is built to avoid: an item code plus a big move is
    not a signal until the *direction* is known, and only price knows it."""
    result = plays.entries(catalyst_ctx(change_pct=-0.09))

    assert skips(result, "F") == [plays.SKIP_BELOW_MIN_MOVE]


def test_a_rise_on_ordinary_volume_is_not_confirmation():
    result = plays.entries(catalyst_ctx(volume=60_000_000))

    assert skips(result, "F") == [plays.SKIP_BELOW_RVOL]


def test_the_boundaries_confirm():
    result = plays.entries(catalyst_ctx(change_pct=0.03, volume=75_000_000))

    assert proposals(result, plays.CATALYST) == ["F"]


def test_conviction_scales_with_the_size_of_the_reaction():
    small = plays.entries(catalyst_ctx(change_pct=0.04))
    large = plays.entries(catalyst_ctx(change_pct=0.09))

    assert proposal(small, "F").conviction_raw == pytest.approx(0.40)
    assert proposal(large, "F").conviction_raw == pytest.approx(0.90)


# --- the news cluster -----------------------------------------------------


def test_an_analyst_or_news_cluster_triggers_without_a_filing():
    """§6.3's second trigger. CI counts headlines and makes no judgment about
    them (§7.3); the threshold is the play's."""
    result = plays.entries(
        catalyst_ctx(events=[{"type": "cluster", "headline_count": 3, "sessions_ago": 0}])
    )

    assert proposals(result, plays.CATALYST) == ["F"]


def test_a_single_headline_is_not_a_cluster():
    result = plays.entries(
        catalyst_ctx(events=[{"type": "cluster", "headline_count": 2, "sessions_ago": 0}])
    )

    assert proposals(result, plays.CATALYST) == []
    assert skips(result) == []


def test_a_name_with_no_events_at_all_does_not_trigger():
    result = plays.entries(catalyst_ctx(events=[]))

    assert proposals(result, plays.CATALYST) == []


# --- §6.3's 0.75x while the play is unproven ------------------------------


def test_an_unproven_catalyst_sizes_at_three_quarters():
    result = plays.entries(catalyst_ctx())

    assert proposal(result, "F").notional_multiplier == pytest.approx(0.75)


def test_the_multiplier_lifts_at_twenty_closed_trades():
    ctx = catalyst_ctx()
    ctx["play_state"]["CATALYST"]["closed_trades"] = 20

    assert proposal(plays.entries(ctx), "F").notional_multiplier == 1.0


def test_nineteen_closed_trades_is_still_unproven():
    ctx = catalyst_ctx()
    ctx["play_state"]["CATALYST"]["closed_trades"] = 19

    assert proposal(plays.entries(ctx), "F").notional_multiplier == pytest.approx(0.75)


def test_an_unwritten_trade_count_reads_as_unproven():
    """`reconcile.py` owes this counter. Until it writes one, the smaller size
    is the safe reading — an absent count is not twenty trades."""
    ctx = catalyst_ctx()
    del ctx["play_state"]["CATALYST"]["closed_trades"]

    assert proposal(plays.entries(ctx), "F").notional_multiplier == pytest.approx(0.75)


def test_no_other_play_carries_a_multiplier():
    """§6.3 is the only play §6 de-risks. If DIP ever acquires one it should be
    because someone wrote it down, not because the default leaked."""
    assert proposal(plays.entries(plays_context()), "F").notional_multiplier == 1.0


def test_the_declared_holding_period_is_seven_sessions():
    # The shortest of the four, and therefore §6.0's tiebreak winner.
    assert proposal(plays.entries(catalyst_ctx()), "F").expected_holding_days == 7


def test_the_target_is_ten_percent():
    assert plays.PLAYS[plays.CATALYST].target_pct == pytest.approx(0.10)


# --- the stale-file hazard -----------


def test_a_frozen_file_does_not_keep_a_stale_eight_k_alive():
    """The hazard this closes. CI re-derives `sessions_ago` on every run, so it
    is right until CI stops — and then the file freezes and a week-old 8-K goes
    on reading as one session old, firing CATALYST forever. §14 says a dead
    EDGAR pipeline must make CATALYST *no-op*, which is the opposite.

    `filed_day` is absolute and cannot go stale, so it is counted from here.
    """
    stale = catalyst_ctx(
        events=[{"type": "8-K", "items": ["9.01"], "sessions_ago": 1,
                 "filed_day": "2026-07-24"}]
    )

    assert proposals(plays.entries(stale), plays.CATALYST) == []


def test_the_absolute_date_wins_over_the_published_count():
    """The other direction, and the one that proves it is counted rather than
    sanity-checked: a row claiming to be nine sessions old still fires when it
    was filed yesterday."""
    fresh = catalyst_ctx(
        events=[{"type": "8-K", "items": ["9.01"], "sessions_ago": 9,
                 "filed_day": "2026-07-31"}]
    )

    assert proposals(plays.entries(fresh), plays.CATALYST) == ["F"]


def test_the_published_count_is_the_fallback_when_there_is_no_filed_day():
    """A row that predates the absolute field is not thereby unusable. This is
    the only case where a frozen count can still be believed, and `plays.py`
    has nothing better to offer for it."""
    assert proposals(plays.entries(catalyst_ctx(sessions_ago=1)), plays.CATALYST) == ["F"]


def test_the_weekend_is_not_counted_against_the_window():
    """Friday's 8-K is one session old on Monday, not three days old. The count
    is `cadence.sessions_between`, so the calendar is the market's."""
    friday = catalyst_ctx(
        events=[{"type": "8-K", "items": ["9.01"], "sessions_ago": 99,
                 "filed_day": "2026-07-31"}]
    )
    friday["cycle"]["trading_day"] = "2026-08-03"

    assert proposals(plays.entries(friday), plays.CATALYST) == ["F"]


def test_a_holiday_does_not_age_an_event():
    """The same argument as the weekend, through the checked-in table rather
    than through the weekday rule."""
    ctx = catalyst_ctx(
        events=[{"type": "8-K", "items": ["9.01"], "sessions_ago": 99,
                 "filed_day": "2026-07-30"}]
    )
    ctx["calendar"] = {"closed": ["2026-07-31"], "early_close": {}}

    assert proposals(plays.entries(ctx), plays.CATALYST) == ["F"]


# --- P2: reason codes and time-normalised relative volume ------
#
# Two separate things. The reason codes apply whichever way the switch is and
# change no decision. The time-normalised volume test is behind
# `catalyst_rvol_time_normalised`, off by default.

AVERAGE = 1_000_000


def at_minute(minutes, *, tnorm=True, volume=750_000, average=AVERAGE,
              change_pct=0.05, min_minutes=None, **cycle) -> dict:
    """F with an 8-K, ``minutes`` into Monday an earlier regular session.

    750k against a 1M average is 0.75x of a full day; at minute 195 (half the
    session) that is exactly 1.5x time-normalised.
    """
    ctx = catalyst_ctx(change_pct=change_pct, volume=volume, avg_volume=average)
    overrides = {"catalyst_rvol_time_normalised": tnorm}
    if min_minutes is not None:
        overrides["catalyst_min_minutes"] = min_minutes
    ctx["config"] = config(**overrides)
    if minutes is not None:
        total = 13 * 60 + 30 + minutes  # 09:30 ET is 13:30Z in August
        ctx["cycle"]["now"] = f"2026-08-03T{int(total // 60):02d}:{int(total % 60):02d}:00Z"
    ctx["cycle"].update(cycle)
    return ctx


def verdict(ctx):
    """`(code or None, the Skipped/Proposal)` for F's CATALYST verdict."""
    outcome = catalyst._catalyst("F", ctx)
    return getattr(outcome, "code", None), outcome


def test_the_minutes_since_open_helper_reads_the_cycle_clock():
    assert plays.minutes_since_open(at_minute(14)) == pytest.approx(14.0)
    assert plays.minutes_since_open(at_minute(390)) == pytest.approx(390.0)
    # Before the open, and with no clock at all.
    assert plays.minutes_since_open(at_minute(-45)) == pytest.approx(-45.0)
    assert plays.minutes_since_open({"cycle": {"trading_day": "2026-08-03"}}) is None
    assert plays.minutes_since_open({}) is None


def test_an_overnight_cycle_is_before_tomorrows_open():
    """After 20:00 ET the trading day is the next one."""
    ctx = {"cycle": {"now": "2026-08-04T02:47:00Z", "trading_day": "2026-08-04"}}

    assert plays.minutes_since_open(ctx) < 0


def test_a_half_day_session_is_two_hundred_and_ten_minutes():
    ctx = at_minute(100)
    ctx["calendar"] = {"closed": [], "early_close": {"2026-08-03": "13:00"}}

    assert plays.session_minutes(ctx) == 210.0
    assert plays.session_minutes(at_minute(100)) == 390.0


@pytest.mark.parametrize(
    "volume, average, minutes, change, expected",
    [
        (750_000, AVERAGE, 195, 0.05, None),                             # exactly 1.5x
        (745_000, AVERAGE, 195, 0.05, "SKIP_BELOW_RVOL_TNORM"),          # 1.49x
        (750_000, AVERAGE, 0, 0.05, "SKIP_TOO_EARLY_FOR_RVOL"),
        (750_000, AVERAGE, 14, 0.05, "SKIP_TOO_EARLY_FOR_RVOL"),
        (750_000, AVERAGE, 29, 0.05, "SKIP_TOO_EARLY_FOR_RVOL"),
        (750_000, AVERAGE, -45, 0.05, "SKIP_TOO_EARLY_FOR_RVOL"),        # premarket
        (60_000, AVERAGE, 30, 0.05, "SKIP_BELOW_RVOL_TNORM"),            # 0.78x at the floor
        (120_000, AVERAGE, 30, 0.05, None),                              # 1.56x at the floor
        (1_500_000, AVERAGE, 390, 0.05, None),                           # the close: raw
        (1_490_000, AVERAGE, 390, 0.05, "SKIP_BELOW_RVOL_TNORM"),
        (1_500_000, AVERAGE, 500, 0.05, None),                           # past it: clamped
        (1_490_000, AVERAGE, 500, 0.05, "SKIP_BELOW_RVOL_TNORM"),
        (750_000, 0, 195, 0.05, "SKIP_NO_VOLUME"),
        (750_000, None, 195, 0.05, "SKIP_NO_VOLUME"),
        (None, AVERAGE, 195, 0.05, "SKIP_NO_VOLUME"),
        (0, AVERAGE, 195, 0.05, "SKIP_NO_VOLUME"),
        (750_000, AVERAGE, 195, None, "SKIP_NO_CHANGE"),
        (750_000, AVERAGE, 195, 0.029, "SKIP_BELOW_MIN_MOVE"),
        (750_000, AVERAGE, 195, 0.03, None),                             # exactly 3%
    ],
)
def test_the_time_normalised_table(volume, average, minutes, change, expected):
    code, outcome = verdict(
        at_minute(minutes, volume=volume, average=average, change_pct=change)
    )

    assert code == expected
    if expected is None:
        assert isinstance(outcome, plays._Proposal)


def test_missing_data_outranks_a_failed_test():
    """No change *and* no volume: reported as the data that is missing, never
    as a move or a volume that failed."""
    code, _ = verdict(at_minute(195, change_pct=None, volume=None))
    assert code == "SKIP_NO_CHANGE"

    code, _ = verdict(at_minute(195, change_pct=0.01, volume=None))
    assert code == "SKIP_NO_VOLUME"


def test_no_message_says_zero_times_average_volume_any_more():
    """126 of 157 CATALYST rejections read "no change on 0.0x average volume"."""
    for kwargs in ({"change_pct": None}, {"volume": None}, {"average": 0}):
        for tnorm in (True, False):
            _, outcome = verdict(at_minute(195, tnorm=tnorm, **kwargs))
            assert "0.0x" not in outcome.message and "no change on" not in outcome.message


def test_the_minimum_minutes_is_config():
    assert verdict(at_minute(14, min_minutes=10, volume=120_000))[0] is None
    assert verdict(at_minute(40, min_minutes=60))[0] == "SKIP_TOO_EARLY_FOR_RVOL"


def test_a_half_day_normalises_against_its_own_length():
    """Minute 105 of a 210-minute session is half the day."""
    ctx = at_minute(105)
    ctx["calendar"] = {"closed": [], "early_close": {"2026-08-03": "13:00"}}

    code, outcome = verdict(ctx)

    assert code is None and "1.5x time-normalised" in outcome.detail


def test_without_a_clock_the_raw_rule_decides_rather_than_a_block():
    """`would_open` and `mc_prefilter` carry no cycle clock."""
    ctx = at_minute(None, volume=1_600_000)
    del ctx["cycle"]["now"]
    assert verdict(ctx)[0] is None

    ctx = at_minute(None, volume=750_000)
    del ctx["cycle"]["now"]
    assert verdict(ctx)[0] == "SKIP_BELOW_RVOL"


# --- flag off: today's decision, exactly ------------------------------------


@pytest.mark.parametrize("minutes", [0, 14, 29, 30, 195, 390, 500])
@pytest.mark.parametrize(
    "volume, change, enters",
    [
        (1_500_000, 0.05, True),
        (1_490_000, 0.05, False),
        (1_500_000, 0.029, False),
        (None, 0.05, False),
        (1_500_000, None, False),
    ],
)
def test_with_the_flag_off_the_raw_multiple_decides_at_every_minute(
    minutes, volume, change, enters
):
    """The regression pin: 1.5x of a full day and a 3% move, whatever the
    clock says — the rule as it was before P2."""
    code, outcome = verdict(
        at_minute(minutes, tnorm=False, volume=volume, change_pct=change)
    )

    assert isinstance(outcome, plays._Proposal) is enters
    assert code not in ("SKIP_TOO_EARLY_FOR_RVOL", "SKIP_BELOW_RVOL_TNORM")


def test_the_flag_ships_off_and_an_absent_key_reads_as_off():
    assert config()["catalyst_rvol_time_normalised"] is False
    assert config()["catalyst_min_minutes"] == 30
    assert catalyst.time_normalised({}) is False
    assert catalyst.time_normalised({"config": {}}) is False


def test_a_confirmed_event_is_unchanged_with_the_flag_off():
    result = plays.entries(catalyst_ctx())

    assert proposal(result, "F").detail == (
        "event confirmed by a +5.0% move on 1.8x average volume"
    )


# --- both figures are recorded in both states --------------------------------


@pytest.mark.parametrize("tnorm", [True, False])
def test_every_refusal_records_both_volume_readings(tnorm):
    _, outcome = verdict(at_minute(195, tnorm=tnorm, volume=745_000))

    factors = outcome.as_dict()["factors"]
    assert factors["rvol_raw"] == pytest.approx(0.745)
    assert factors["rvol_tnorm"] == pytest.approx(1.49)
    assert factors["minutes_elapsed"] == pytest.approx(195.0)
    assert factors["rule"] == ("tnorm" if tnorm else "raw")
    assert factors["raw_would_pass"] is False and factors["tnorm_would_pass"] is False


def test_the_flag_off_refusal_says_what_the_other_rule_would_have_done():
    """0.9x of a full day at minute 195 is 1.8x time-normalised: refused today,
    entered under the switch. The record carries both."""
    _, outcome = verdict(at_minute(195, tnorm=False, volume=900_000))

    assert outcome.code == "SKIP_BELOW_RVOL"
    assert outcome.factors["tnorm_would_pass"] is True
    assert "1.8x time-normalised at minute 195" in outcome.message


def test_a_skip_from_a_play_that_records_nothing_serialises_as_before():
    skip = plays.Skipped("F", plays.DIP, plays.NO_CONVICTION, "no depth")

    assert set(skip.as_dict()) == {"symbol", "play_id", "code", "message"}


# --- a recorded case: HPE, 14 minutes into the session -----------------------

RECORDED = Path(__file__).parent / "fixtures" / "plays"


def hpe_ctx(tnorm: bool) -> dict:
    """HPE as the runner posted it, through the core's own shapers."""
    recorded = json.loads(
        (RECORDED / "hpe_2026-10-02_catalyst.json").read_text(encoding="utf-8")
    )
    payload = {"quotes": recorded["quotes"], "fundamentals": recorded["fundamentals"]}
    return {
        "cycle": recorded["cycle"],
        "calendar": cli.load_config()["calendar"],
        "config": config(catalyst_rvol_time_normalised=tnorm),
        "quotes": cli._quotes(payload),
        "fundamentals": payload["fundamentals"],
        "events": recorded["events"],
    }


def test_the_recorded_hpe_case_reads_the_inputs_production_read():
    ctx = hpe_ctx(False)

    assert ctx["quotes"]["HPE"]["change_pct"] == pytest.approx(0.06999, abs=1e-4)
    reading = catalyst.relative_volume("HPE", ctx)
    assert reading["rvol_raw"] == pytest.approx(0.1556, abs=1e-4)
    assert reading["minutes_elapsed"] == pytest.approx(11.67, abs=0.01)


def test_hpe_with_the_flag_off_is_refused_as_it_was_and_now_says_why():
    outcome = catalyst._catalyst("HPE", hpe_ctx(False))

    assert outcome.code == "SKIP_BELOW_RVOL"
    assert "+7.0% on 0.2x average volume" in outcome.message
    assert outcome.factors["rvol_tnorm"] == pytest.approx(5.2, abs=0.1)


def test_hpe_with_the_flag_on_is_too_early_not_a_volume_failure():
    """Twelve minutes into the session. The new verdict is not an entry — it
    is an honest "too early to read", where the old one said the tape had not
    confirmed a +7% move."""
    outcome = catalyst._catalyst("HPE", hpe_ctx(True))

    assert outcome.code == "SKIP_TOO_EARLY_FOR_RVOL"
    assert "minute 12" in outcome.message


def test_the_same_hpe_reading_at_a_monitor_cycle_would_enter():
    """The identical tape, read at 12:47 ET with the day's volume scaled to
    that hour — where a monitor cycle would see it."""
    ctx = hpe_ctx(True)
    ctx["cycle"] = {**ctx["cycle"], "now": "2026-10-02T16:47:00Z"}
    ctx["fundamentals"] = {"HPE": {"volume": "16000000", "average_volume": "19234019.667964"}}

    outcome = catalyst._catalyst("HPE", ctx)

    assert isinstance(outcome, plays._Proposal)
    assert "time-normalised volume at minute 197" in outcome.detail
