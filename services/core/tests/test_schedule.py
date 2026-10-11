"""The schedule producer, against the calendar it will actually meet.

`edge_cycle_schedule` was hand-typed once and the result was three trading
days with no post-close cycle and no alert. The
producer's whole job is that the table is never typed again, so the questions
here are the ones a hand could get wrong: does a holiday disappear, does a
half-day stop scheduling monitors into a shut auction, does an ET wall clock
survive a DST boundary, and does anything a runner cannot execute ever reach
the published plan.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from services.core.core import schedule as producer
from services.core.core.schedule import ScheduleError
from services.core.tests.conftest import FakeSupabase
from src import broker_tools, cadence, cli


@pytest.fixture(scope="module")
def loaded():
    return cli.load_config()


def days_of(rows):
    return sorted({row["trading_day"] for row in rows})


def slots_on(rows, day: str):
    return [row for row in rows if row["trading_day"] == day]


def times_on(rows, day: str):
    return [row["slot_et"][:5] for row in slots_on(rows, day)]


# ---------------------------------------------------------------------------
# The calendar
# ---------------------------------------------------------------------------


def test_a_market_holiday_gets_no_slots_at_all(loaded):
    # 2026-11-26 is Thanksgiving, in config/market_holidays.json.
    rows = producer.build_schedule(date(2026, 11, 25), loaded, horizon=3)

    assert "2026-11-26" not in days_of(rows)
    assert days_of(rows) == ["2026-11-25", "2026-11-27", "2026-11-30"]


def test_a_half_day_stops_scheduling_monitors_at_the_close(loaded):
    # 2026-11-27 closes at 13:00. The last monitor must be before that, not
    # 15:30 -- three sessions into a shut auction.
    rows = producer.build_schedule(date(2026, 11, 27), loaded, horizon=1)
    monitors = [r for r in slots_on(rows, "2026-11-27") if r["cycle_type"] == "monitor"]

    assert [r["slot_et"][:5] for r in monitors] == [
        "10:00", "10:30", "11:00", "11:30", "12:00", "12:30",
    ]


def test_a_half_days_postclose_moves_with_the_close(loaded):
    """§8.4's benchmark row cannot be backfilled, so the half-day is the one
    day the post-close slot must not be left five hours after everything shut."""
    rows = producer.build_schedule(date(2026, 11, 27), loaded, horizon=1)
    half = [r for r in slots_on(rows, "2026-11-27") if r["cycle_type"] == "postclose"]
    full = [
        r
        for r in slots_on(
            producer.build_schedule(date(2026, 11, 30), loaded, horizon=1), "2026-11-30"
        )
        if r["cycle_type"] == "postclose"
    ]

    assert [r["slot_et"][:5] for r in half] == ["15:47"]   # 13:00 + 2h47
    assert [r["slot_et"][:5] for r in full] == ["18:47"]   # 16:00 + 2h47


def test_the_evening_overnight_slot_fires_the_previous_calendar_day(loaded):
    """`cadence.trading_day` rolls at 20:00, so Monday's 22:47 cycle is Sunday
    evening. `trading_day + slot_et` would put it twenty-four hours late, which
    is the whole reason `slot_at` is its own column."""
    rows = producer.build_schedule(date(2026, 11, 30), loaded, horizon=1)
    evening = next(r for r in rows if r["slot_et"].startswith("22:47"))

    assert evening["trading_day"] == "2026-11-30"          # Monday
    assert evening["slot_at"] == "2026-11-29T22:47:00"     # Sunday evening
    assert evening["cycle_type"] == "overnight"


def test_no_friday_evening_slot_is_generated(loaded):
    """Friday 22:47 belongs to Saturday, which is not a trading day. The live
    `47 2 * * 2-6` cron fires it anyway and pays for a guaranteed no-op."""
    rows = producer.build_schedule(date(2026, 11, 30), loaded, horizon=5)
    evenings = {r["slot_at"] for r in rows if r["slot_et"].startswith("22:47")}

    for stamp in evenings:
        # Saturday is 5. No generated evening slot may fall on a Friday.
        assert datetime.fromisoformat(stamp).weekday() != 4, stamp


# ---------------------------------------------------------------------------
# DST
# ---------------------------------------------------------------------------


def test_the_wall_clock_is_identical_either_side_of_a_dst_boundary(loaded):
    """2026-11-01 is the EDT->EST change. The point of storing an ET wall clock
    is that nothing in this table moves across it -- the twice-a-year `+1h UTC`
    edit the cron runbook manages does not apply here at all."""
    before = producer.build_schedule(date(2026, 10, 30), loaded, horizon=1)
    after = producer.build_schedule(date(2026, 11, 2), loaded, horizon=1)

    assert times_on(before, "2026-10-30") == times_on(after, "2026-11-02")
    assert "09:40" in times_on(after, "2026-11-02")


def test_the_indicator_start_time_tracks_the_real_utc_offset(loaded):
    """`cli._indicator_start` re-labels its input `Z` without converting it, so
    the `now` a generated plan is derived against has to already be UTC. If it
    were not, every indicator window either side of the boundary would be
    wrong by the offset."""
    edt = producer._utc_stamp(datetime(2026, 10, 30, 9, 40))
    est = producer._utc_stamp(datetime(2026, 11, 2, 9, 40))

    assert edt == "2026-10-30T13:40:00Z"   # UTC-4
    assert est == "2026-11-02T14:40:00Z"   # UTC-5


# ---------------------------------------------------------------------------
# What gets published
# ---------------------------------------------------------------------------


def test_every_published_row_is_a_broker_or_vendor_row(loaded):
    """`supabase` rows are `run._payload`'s now and `checkout` rows name
    files the runner has no clone to read."""
    rows = producer.build_schedule(date(2026, 9, 16), loaded, horizon=3)

    sources = {step["source"] for row in rows for step in row["fetch_plan"]}
    assert sources <= producer.PUBLISHED_SOURCES
    assert "supabase" not in sources
    assert "checkout" not in sources


def test_every_published_tool_is_a_real_mcp_tool_name(loaded):
    """The fence. A session is told to "run every row with the tool it names",
    so a name that is not a tool is a dead cycle."""
    rows = producer.build_schedule(date(2026, 9, 16), loaded, horizon=3)

    for row in rows:
        for step in row["fetch_plan"]:
            assert broker_tools.is_fetch_tool(step["source"], step["tool"]), step


def test_a_tool_that_is_not_in_the_allowlist_is_refused_not_published():
    with pytest.raises(ScheduleError, match="contract break"):
        producer._published_rows(
            [{"step": "runlog", "source": "robinhood", "tool": "edge_runlog"}]
        )


def test_the_unknowable_call_estimate_is_dropped(loaded):
    """`cli._calls` derives `calls` from `positions_held`, which nobody has at
    04:30 for a slot fourteen hours away."""
    rows = producer.build_schedule(date(2026, 9, 16), loaded, horizon=1)

    assert all("calls" not in step for row in rows for step in row["fetch_plan"])


def test_the_full_slot_carries_the_entry_scoped_steps(loaded):
    """A plan generated wide. Guessing narrow would hand the core a cycle it is
    allowed to enter on and no candidate data to enter from."""
    rows = producer.build_schedule(date(2026, 9, 16), loaded, horizon=1)
    full = next(r for r in rows if r["cycle_type"] == "full")
    steps = {step["step"] for step in full["fetch_plan"]}

    assert {"fundamentals_live", "orders", "tax_lots", "realized"} <= steps


def test_no_slot_publishes_a_step_the_core_cannot_assemble(loaded):
    """`indicators` and `regime` rows reached the runner,
    which could not run them (no `type`, one symbol per call) and alerted."""
    rows = producer.build_schedule(date(2026, 9, 16), loaded, horizon=3)

    published = {step["step"] for row in rows for step in row["fetch_plan"]}
    assert not published & {"indicators", "regime"}


def test_the_postclose_slot_fetches_the_frozen_book_and_nothing_candidate_scoped(loaded):
    rows = producer.build_schedule(date(2026, 9, 16), loaded, horizon=1)
    postclose = next(r for r in rows if r["cycle_type"] == "postclose")
    steps = {step["step"] for step in postclose["fetch_plan"]}
    scopes = {step["scope"] for step in postclose["fetch_plan"]}

    assert "quotes" in steps
    assert "candidates" not in scopes


def test_no_premarket_slot_is_scheduled(loaded):
    """The 07:40 pre-market cycle's work moved into the 06:30 ET server job
    (services/core/core/premarket.py). A slot here would wake a session to
    redo it."""
    rows = producer.build_schedule(date(2026, 9, 16), loaded, horizon=5)

    assert {r["cycle_type"] for r in rows} == {
        cadence.OVERNIGHT, cadence.FULL, cadence.MONITOR, cadence.POSTCLOSE
    }


def test_a_full_day_is_sixteen_slots(loaded):
    rows = producer.build_schedule(date(2026, 9, 16), loaded, horizon=1)

    # 2 overnight + 1 full + 12 monitors + 1 post-close.
    assert len(rows) == 16


def test_the_generated_type_is_cadences_and_a_disagreement_raises(loaded):
    """`slots_for` decides when; `cadence.decide` decides what. If the two ever
    disagree the producer must stop, not pick one."""
    with pytest.raises(ScheduleError, match="drifted"):
        producer._row(
            datetime(2026, 9, 16, 9, 40), datetime(2026, 9, 16, 9, 40).time(),
            cadence.POSTCLOSE, date(2026, 9, 16), loaded,
        )


def test_a_monitor_cadence_of_sixty_halves_the_band(loaded):
    config = {**loaded["config"], "monitor_cadence_minutes": 60}
    times = producer._monitor_times(date(2026, 9, 16), loaded["calendar"], config)

    assert [t.strftime("%H:%M") for t in times] == [
        "10:00", "11:00", "12:00", "13:00", "14:00", "15:00",
    ]


def test_a_nonsense_cadence_raises_rather_than_looping(loaded):
    config = {**loaded["config"], "monitor_cadence_minutes": 0}

    with pytest.raises(ScheduleError, match="monitor_cadence_minutes"):
        producer._monitor_times(date(2026, 9, 16), loaded["calendar"], config)


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------


def test_run_schedule_writes_the_slots_then_publishes_the_cap(loaded):
    """The cap write is second on purpose: a failed schedule write must not leave
    a published cap describing a schedule that was not published."""
    supabase = FakeSupabase(returns={"edge_cycle_schedule_apply": 160})

    result = producer.run_schedule(supabase, now=datetime(2026, 9, 16, 4, 30, tzinfo=cadence.ET))

    assert supabase.names() == ["edge_cycle_schedule_apply", "edge_control_apply"]
    assert result["status"] == "ok"
    assert result["slots"] == len(result["trading_days"]) * 16
    assert len(result["trading_days"]) == producer.HORIZON_TRADING_DAYS

    published = supabase.first("edge_control_apply")["p_updates"]
    assert published["max_new_positions_per_day"] == loaded["config"]["max_new_positions_per_day"]
    # The wall-clock budgets ride the same daily write, because
    # `edge_cycle_budget_min()` runs in Postgres and cannot read the checkout.
    assert published["cycle_budget_min"] == loaded["config"]["cycle_budget_min"]
    assert set(published) == {"max_new_positions_per_day", "cycle_budget_min"}


def test_run_schedule_publishes_ten_trading_days_starting_today(loaded):
    supabase = FakeSupabase()

    result = producer.run_schedule(supabase, now=datetime(2026, 9, 16, 4, 30, tzinfo=cadence.ET))

    assert result["trading_days"][0] == "2026-09-16"
    assert result["through"] == result["trading_days"][-1]


def test_nothing_is_written_when_generation_raises(monkeypatch, loaded):
    """Fail-closed: the previous generation's nine remaining days are worth
    more than a half-written table, which `edge_should_run` reads as a quiet
    market."""
    supabase = FakeSupabase()
    monkeypatch.setattr(
        producer, "build_schedule",
        lambda *a, **k: (_ for _ in ()).throw(ScheduleError("boom")),
    )

    with pytest.raises(ScheduleError):
        producer.run_schedule(supabase, now=datetime(2026, 9, 16, 4, 30, tzinfo=cadence.ET))

    assert supabase.names() == []
