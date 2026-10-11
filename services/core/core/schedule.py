"""The schedule producer — `edge_cycle_schedule`, generated instead of typed.

`edge_should_run` answers "should this firing do
anything" by looking up a slot in `edge_cycle_schedule`. Until now nothing filled that
table. Twenty-one rows were inserted by hand, all `postclose`, and two of them carried
a `fetch_plan` naming `edge_runlog` and `edge_play_state` as *tools*. They are not
tools. The runner prompt says "run every row of `should.fetch` with the `tool` it
names", so an earlier post-close session called two functions that do not exist,
failed twice, and ended — without an alert, twice over. The hand-seeded rows also ran
out, which was a deadline nothing was tracking.

This module is the producer. It runs once a day at 04:30 ET, asks
`src/cadence.py` and `cli.plan_fetch` what each of the next ten trading days
looks like, and upserts the answer. Three properties are the point:

**One decider.** The slots and the fetch list are `cadence`'s and `cli`'s, not
this file's. `slots_for` decides *when* a slot is (which is a schedule, and
has to live somewhere) and then hands the moment to `cadence.decide` through
`cli.plan_fetch` to find out *what* it is. A slot whose generated
`cycle_type` disagreed with what `cadence` would say at the moment it fires
would be the drift the schedule table exists to prevent, so
the generated type is read back off the cycle rather than asserted.

**Broker and vendor rows only.** A published `fetch_plan` carries the
`robinhood` (and, when one appears, `twelvedata`) rows and nothing else. The
`checkout` rows name files the runner has no clone to read — `run.py` already
fills them from the deployment's own `data/` — and as of this change the
`supabase` rows are read by `run._payload` too. What is left is exactly the
set of rows a runner can actually execute, which is what makes "run every row
with the tool it names" a safe instruction. Every remaining row's tool is
checked against `src/broker_tools.py` before it is stored; a row that fails is
refused, loudly, rather than published.

**Wall clock, not cron.** Slots are stored as an ET wall clock
(`slot_at`/`slot_et`), so DST moves nothing: 09:40 ET is 09:40 ET on both
sides of a daylight-saving change, and the twice-a-year `+1h UTC` cron edit
does not apply to this table at all. The only thing that has to survive DST is
the 04:30 producer job itself, and it is scheduled the way the pre-market
pre-market job is: fire on both candidate UTC hours, no-op on the one whose ET
clock is wrong.

**What this module does not schedule.** There is no `premarket` runner slot. The 07:40
pre-market cycle's job — set the day's regime, refresh day context — moved server-side
to `services/core/core/premarket.py` and runs from its own pg_cron entry at 06:30 ET.
Publishing a `premarket` slot here would wake a session to do work a server job has
already done, so the slot list is `overnight`, `full`, `monitor`, `postclose` and
stops there.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Mapping

from src import broker_tools, cadence, cli

from . import steps
from .supabase import Supabase

# ---------------------------------------------------------------------------
# The slot table — §13's cadence, as times rather than as cron lines
# ---------------------------------------------------------------------------

# The one intraday full cycle. Ten minutes after the open, which is the
# existing `cycle full 09:40` trigger's time and is late enough that the
# opening auction has printed.
FULL_SLOT = time(9, 40)

# The monitor band.
MONITOR_FIRST = time(10, 0)
MONITOR_LAST = time(15, 30)

# Post-close, measured **from the close rather than fixed at 18:47**. The
# live trigger is 18:47 ET against a 16:00 close, which is this offset; on a
# 13:00 half-day the same offset puts the post-close at 15:47, still inside
# the extended band, instead of leaving a half-day's benchmark row to a slot
# five hours after everything shut. §8.4's series cannot be backfilled, so the
# half-day is exactly the day not to get wrong.
POSTCLOSE_AFTER_CLOSE = timedelta(hours=2, minutes=47)

# The two overnight slots. `OVERNIGHT_EVENING` is 22:47 ET on the *previous
# calendar day*: `cadence.trading_day` rolls at 20:00, so Sunday 22:47 is
# already Monday's trading day and Friday 22:47 is Saturday's — which is not a
# trading day at all. Generating from the trading day backwards therefore
# produces the Sunday-evening cycle the live `47 2 * * 2-6` cron (21:47 CT /
# 22:47 ET) misses and skips the Friday-evening one it wastes.
#
# 22:47, not 20:00: the "Deterministic Trader: Overnight ..." routine fires at
# 22:47 ET (see docs/routines.md). The exact minute still has to fall inside
# `cadence`'s overnight band (>= `EXTENDED_CLOSE`, i.e. >= 20:00 ET) so
# `cadence.decide` keeps agreeing this is `OVERNIGHT` — 22:47 comfortably
# does.
OVERNIGHT_MORNING = time(4, 30)
OVERNIGHT_EVENING = time(22, 47)

# How far ahead to publish. Ten trading days is two weeks of slack: the daily
# job has to miss nine consecutive runs before the table runs dry, and the
# schedule-completeness check fires long before that.
HORIZON_TRADING_DAYS = 10

# The sources a published row may carry. Everything else is read by the core.
PUBLISHED_SOURCES = frozenset(broker_tools.FETCH_SOURCES)

# `cli._fetch_plan` steps the runner cannot execute or post: indicators and the
# regime are computed server-side (`src/indicators.py`, `edge_regime`), and
# `assemble` refuses these kinds. Kept for the live cycle-prompt's plan only.
RUNNER_UNASSEMBLABLE_STEPS = frozenset({"indicators", "regime"})


class ScheduleError(RuntimeError):
    """The schedule could not be generated, so nothing is published.

    Fail-closed in the same direction `run.py` fails: a partial schedule is a
    day with holes in it, and a hole in this table reads to `edge_should_run`
    as "no scheduled slot", which is indistinguishable from a quiet market.
    Better to keep yesterday's ten days and alarm.
    """


# ---------------------------------------------------------------------------
# When the slots are
# ---------------------------------------------------------------------------


def trading_days(start: date, count: int, calendar: Mapping[str, Any]) -> list[date]:
    """The next ``count`` open sessions from ``start`` inclusive.

    `cadence.is_open` is the only thing consulted, so the weekend rule and the
    holiday table are the same ones every other module reads. A second holiday
    list here is the drift the schema refuses to write in PL/pgSQL, and it would
    be no better in Python.
    """
    days: list[date] = []
    day = start
    # A generous bound rather than `while True`: ten trading days can never be
    # more than a month of calendar days even across a holiday-heavy stretch,
    # and a calendar file that somehow closed every day should raise rather
    # than spin.
    for _ in range(count * 4 + 30):
        if len(days) == count:
            return days
        if cadence.is_open(day, calendar):
            days.append(day)
        day += timedelta(days=1)
    if len(days) == count:
        return days
    raise ScheduleError(
        f"only {len(days)} open sessions in the {count * 4 + 30} days from "
        f"{start}: config/market_holidays.json is probably short"
    )


def _monitor_times(day: date, calendar: Mapping[str, Any], config: Mapping[str, Any]) -> list[time]:
    """The monitor slots for ``day``, every `monitor_cadence_minutes`.

    Bounded by the day's own close, so a 13:00 half-day stops at 12:30 rather
    than scheduling three monitor sessions into a shut auction. The cadence is
    config so the band can be thinned without a code review — it is a cost
    knob, not a rule about what a monitor may do.
    """
    step = int(config["monitor_cadence_minutes"])
    if step <= 0:
        raise ScheduleError(f"monitor_cadence_minutes must be positive, got {step}")

    close = cadence.close_time(day, calendar)
    first = datetime.combine(day, MONITOR_FIRST)
    last = datetime.combine(day, MONITOR_LAST)

    out: list[time] = []
    moment = first
    while moment <= last:
        if moment.time() < close:
            out.append(moment.time())
        moment += timedelta(minutes=step)
    return out


def slots_for(
    day: date, calendar: Mapping[str, Any], config: Mapping[str, Any]
) -> list[tuple[datetime, time, str]]:
    """Every slot belonging to trading day ``day``.

    Returns `(slot_at, slot_et, expected_type)` where `slot_at` is the ET wall
    clock the slot actually fires at and `slot_et` is its time-of-day. The two
    differ for exactly one slot — the 22:47 overnight, whose wall clock is the
    previous calendar evening — and that is the whole reason `slot_at` exists
    as a column rather than being recomputed as `trading_day + slot_et`.

    `expected_type` is what this function believes the slot is. It is checked
    against `cadence.decide`'s answer in `_row`, never trusted over it.
    """
    close = cadence.close_time(day, calendar)

    rows: list[tuple[datetime, time, str]] = [
        # Previous evening, belonging to this trading day.
        (
            datetime.combine(day - timedelta(days=1), OVERNIGHT_EVENING),
            OVERNIGHT_EVENING,
            cadence.OVERNIGHT,
        ),
        (datetime.combine(day, OVERNIGHT_MORNING), OVERNIGHT_MORNING, cadence.OVERNIGHT),
        (datetime.combine(day, FULL_SLOT), FULL_SLOT, cadence.FULL),
    ]

    for slot in _monitor_times(day, calendar, config):
        rows.append((datetime.combine(day, slot), slot, cadence.MONITOR))

    postclose = (datetime.combine(day, close) + POSTCLOSE_AFTER_CLOSE).time()
    rows.append((datetime.combine(day, postclose), postclose, cadence.POSTCLOSE))

    return rows


# ---------------------------------------------------------------------------
# What each slot fetches
# ---------------------------------------------------------------------------

# The `edge_control` a generated plan is derived against.
#
# **It is the maximal legitimate shape of the slot, and that direction is
# chosen.** A plan generated at 04:30 cannot know how many entries will be
# left at 14:00 or whether a whole-share position will exist by 20:00. Guessing
# *narrow* would publish an entries-only plan for a cycle that turns out to be
# allowed to enter, and the core would then decide against a candidate set it
# was never given the data for — a quieter, worse failure than the cost of
# guessing wide. Guessing *wide* over-fetches on an exhausted day, and
# `edge_should_run` already answers `run: false` for most of those days before
# a single call is made.
#
# `positions_held` is deliberately absent: it only feeds `cli._calls`, whose
# output this module drops (see `_published_rows`).
_BASE_CONTROL: dict[str, Any] = {
    # P1 x §13: without this, `cadence.decide` classifies every overnight slot
    # as a NOOP and the generated plan is empty for the two slots that exist
    # precisely to place an exit outside regular hours.
    "has_whole_share_position": True,
}


def _control_for(expected_type: str, day: date) -> dict:
    """The synthetic control that makes `cadence.decide` classify this slot.

    Only one key varies. `cadence` promotes a `monitor` slot to `full` when no
    full cycle has run today, so a monitor's control has to carry a
    `last_full_cycle` stamped with this trading day and the full cycle's must
    not. Everything else about the classification is the clock's.
    """
    control = dict(_BASE_CONTROL)
    if expected_type == cadence.MONITOR:
        control["last_full_cycle"] = _utc_stamp(datetime.combine(day, FULL_SLOT))
    return control


def _utc_stamp(et_wall_clock: datetime) -> str:
    """An ET wall clock as the `Z`-suffixed UTC string every caller posts.

    `cli._indicator_start` reads `now` with `datetime.fromisoformat` and then
    re-labels the result `Z` without converting it, so a `now` carrying a
    `-04:00` offset would publish an indicator `start_time` four hours wrong.
    The runner posts `date -u`; so does this.
    """
    aware = et_wall_clock.replace(tzinfo=cadence.ET)
    return aware.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _published_rows(fetch: Iterable[Mapping[str, Any]]) -> list[dict]:
    """The rows a runner can actually execute, validated.

    Three filters and one drop:

    * **step** — `RUNNER_UNASSEMBLABLE_STEPS` are never published. Neither is an
      Appendix D key or an `assemble.EXTRA_KINDS` member, so a runner that
      fetched and posted one would fail assembly; and neither can be executed as
      written (`get_equity_technical_indicators` takes one symbol and a required
      `type`, which the row does not carry).
    * **source** — `supabase` and `checkout` rows are the core's to read.
    * **tool** — checked against `src/broker_tools.py`.
    * **`calls`** — dropped. It is `cli._calls`' cost estimate and it is
      derived from `positions_held`, a number nobody has at 04:30 for a slot
      fourteen hours away. A published estimate that is always wrong is worse
      than an absent one, and the runner prompt already lists `calls` among
      the keys that are not call parameters.
    """
    rows: list[dict] = []
    for step in fetch:
        if step.get("step") in RUNNER_UNASSEMBLABLE_STEPS:
            continue
        source = step.get("source")
        if source not in PUBLISHED_SOURCES:
            continue
        tool = step.get("tool")
        if not broker_tools.is_fetch_tool(str(source), str(tool)):
            raise ScheduleError(
                f"fetch step {step.get('step')!r} names tool {tool!r} on source "
                f"{source!r}, which src/broker_tools.py does not list. This is "
                "a contract break; refusing to publish it."
            )
        rows.append({k: v for k, v in step.items() if k != "calls"})
    return rows


def _row(
    slot_at: datetime,
    slot_et: time,
    expected_type: str,
    day: date,
    loaded: Mapping[str, Any],
) -> dict:
    """One `edge_cycle_schedule` row, with its plan.

    The `cycle_type` stored is `cli.plan_fetch`'s, which is `cadence.decide`'s.
    A disagreement with `expected_type` means this module's slot table and
    `cadence`'s bands have drifted apart — the exact failure the schema declined
    to risk by re-implementing the bands in PL/pgSQL — so it raises instead of
    quietly publishing whichever one happened to be asked last.
    """
    payload = {"now": _utc_stamp(slot_at), "control": _control_for(expected_type, day)}
    plan = cli.plan_fetch(payload, loaded)
    cycle = plan["cycle"]

    if cycle["cycle_type"] != expected_type:
        raise ScheduleError(
            f"{slot_at.isoformat()} ET was generated as a {expected_type!r} slot "
            f"but cadence.decide calls it {cycle['cycle_type']!r}. The slot "
            "table in services/core/core/schedule.py and src/cadence.py have "
            "drifted."
        )
    if cycle["trading_day"] != day.isoformat():
        raise ScheduleError(
            f"{slot_at.isoformat()} ET belongs to trading day "
            f"{cycle['trading_day']}, not {day.isoformat()}"
        )

    return {
        "trading_day": day.isoformat(),
        "slot_et": slot_et.strftime("%H:%M:%S"),
        "slot_at": slot_at.strftime("%Y-%m-%dT%H:%M:%S"),
        "cycle_type": cycle["cycle_type"],
        "fetch_plan": _published_rows(plan["fetch"]),
    }


def build_schedule(
    start: date, loaded: Mapping[str, Any], horizon: int = HORIZON_TRADING_DAYS
) -> list[dict]:
    """Every slot for the next ``horizon`` trading days from ``start``.

    Pure: clock in, rows out, no Supabase. `run_schedule` is the half that
    talks to a database, and this is the half the tests can drive across a DST
    boundary and a holiday without one.
    """
    calendar = loaded["calendar"]
    config = loaded["config"]

    rows: list[dict] = []
    for day in trading_days(start, horizon, calendar):
        for slot_at, slot_et, expected in slots_for(day, calendar, config):
            rows.append(_row(slot_at, slot_et, expected, day, loaded))
    return rows


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------


def run_schedule(
    supabase: Supabase, config_dir=None, now: datetime | None = None
) -> dict:
    """Generate and publish. Called by `POST /api/schedule`, once a day.

    Two writes, in this order:

    1. `edge_cycle_schedule_apply` — the ten days of slots, upserted.
    2. `edge_control_apply` — `max_new_positions_per_day`, which
       `edge_should_run` once hard-coded as `2` while
       `config/config.json` has said `3`. The cap is published rather than
       read from `config/` because Postgres cannot read the git checkout; the
       consequence of it going stale is a monitor slot skipped on a day a
       third entry was still allowed.

    The cap write is second on purpose: a failed schedule write must not leave
    a published cap describing a schedule that was not published.
    """
    loaded = cli.load_config(config_dir)
    moment = now or datetime.now(cadence.ET)
    if moment.tzinfo is not None:
        moment = moment.astimezone(cadence.ET)

    rows = build_schedule(cadence.trading_day(moment), loaded)
    if not rows:
        raise ScheduleError("generated an empty schedule; refusing to publish")

    written = supabase.rpc("edge_cycle_schedule_apply", {"p_rows": rows})

    cap = int(loaded["config"]["max_new_positions_per_day"])
    updates: dict[str, Any] = {"max_new_positions_per_day": cap}
    # The wall-clock budgets, published for the same reason the cap is —
    # `edge_cycle_budget_min()` runs in Postgres and cannot read the checkout.
    # Absent from config, nothing is published and the SQL falls back to its
    # own defaults (`steps.CYCLE_BUDGET_MIN`).
    budgets = loaded["config"].get(steps.CYCLE_BUDGET_KEY)
    if budgets:
        updates[steps.CYCLE_BUDGET_KEY] = {k: int(v) for k, v in budgets.items()}
    supabase.rpc("edge_control_apply", {"p_updates": updates})

    days = sorted({row["trading_day"] for row in rows})
    return {
        "status": "ok",
        "slots": len(rows),
        "written": written,
        "trading_days": days,
        "through": days[-1] if days else None,
        "max_new_positions_per_day": cap,
    }
