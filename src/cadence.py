"""cadence.py — cycle type, what is due, daily caps, the two clocks.

Design v3 §13 and §11. Routines fire on a schedule; this module decides what
kind of cycle each firing is (Invariant 6). Every "what runs now" decision is
therefore golden-file testable and cannot drift in prompt text — the previous
system kept its equity stop in cloud-prompt text and lost it silently when the
routines were disabled.

Cadence is given **no broker state**. §13 requires the no-op path to be
reachable from one Supabase read and exit before touching Robinhood, so
everything this module reads comes from ``edge_control``, a checked-in
calendar, or the clock. A cadence that needed ``get_portfolio`` to decide
whether to skip would turn 11 cheap invocations into 11 full MCP sessions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo

# Every §13 boundary is an ET wall clock, and the DST rules behind it have been
# changed by act of Congress before. The tz database is a dependency precisely
# so that change is a package upgrade rather than a silent off-by-an-hour for
# three weeks each spring.
ET = ZoneInfo("America/New_York")

FULL = "full"
PREMARKET = "premarket"
MONITOR = "monitor"
POSTCLOSE = "postclose"
OVERNIGHT = "overnight"
NOOP = "noop"

REGULAR_HOURS = "regular_hours"
EXTENDED_HOURS = "extended_hours"
ALL_DAY_HOURS = "all_day_hours"
CLOSED = "closed"

# P5, from the authoritative MCP resource `trading://market-hours`. The four
# equity sessions tile without gaps into a continuous band running Sunday
# 20:00 ET to Friday 20:00 ET; there is no weekend equity session.
PREMARKET_OPEN = time(7, 0)
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EXTENDED_CLOSE = time(20, 0)

SATURDAY = 5


@dataclass(frozen=True)
class Cycle:
    """What this firing is, and what it is permitted to do.

    The first four fields are exactly ``guard.py``'s ``ctx["cycle"]`` surface,
    so this drops in without translation.
    """

    cycle_type: str
    session: str
    now: str
    entries_allowed: bool
    entries_remaining: int
    exits_allowed: bool
    reconcile_required: bool
    reason: str
    trading_day: str
    clock_updates: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "cycle_type": self.cycle_type,
            "session": self.session,
            "now": self.now,
            "entries_allowed": self.entries_allowed,
            "entries_remaining": self.entries_remaining,
            "exits_allowed": self.exits_allowed,
            "reconcile_required": self.reconcile_required,
            "reason": self.reason,
            "trading_day": self.trading_day,
            "clock_updates": dict(self.clock_updates),
        }


def _et(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(ET)


def trading_day(moment: datetime) -> date:
    """The session-day ``moment`` belongs to.

    The overnight session opens at 20:00 ET and runs to 07:00 the next
    morning, so 20:00 Monday is already Tuesday's trading day. Getting this
    one shift right is what makes weekends and holidays fall out of the same
    check: Friday 20:00 belongs to Saturday and is therefore shut, while
    Sunday 20:00 belongs to Monday and is open.
    """
    if moment.time() >= EXTENDED_CLOSE:
        return moment.date() + timedelta(days=1)
    return moment.date()


def is_open(day: date, calendar: Mapping[str, Any]) -> bool:
    """Whether ``day`` is a trading day at all (P5 plus the holiday table)."""
    if day.weekday() >= SATURDAY:
        return False
    return day.isoformat() not in set(calendar.get("closed") or [])


def sessions_between(start: date, trading_day: date, calendar: Mapping[str, Any]) -> int:
    """Open sessions strictly after ``start``, through ``trading_day``.

    Lives here because it is a question about the calendar and nothing else,
    and because three modules now ask it: `reconcile.py` for how long a
    position has been held, `plays.py` for how long ago an event landed, and
    `cli.py` for ageing a carried book forward. Two implementations would
    eventually disagree about the length of a holiday week, and the
    disagreement surfaces as a time stop that fires a day early in one module
    and a day late in another.

    **Strictly after**, so something dated today is zero sessions old. §6.5's
    time stop is ``sessions_held >= holding_days``, and counting the day itself
    would spend one session of every play's allowance before the position had
    survived a single close. `ci/common.sessions_between` counts the same way,
    which is what makes a recomputed `sessions_ago` comparable to a published
    one.
    """
    day = start + timedelta(days=1)
    counted = 0
    while day <= trading_day:
        if is_open(day, calendar):
            counted += 1
        day += timedelta(days=1)
    return counted


def close_time(day: date, calendar: Mapping[str, Any]) -> time:
    """When the regular session ends — 13:00 on the published half-days."""
    early = (calendar.get("early_close") or {}).get(day.isoformat())
    if early is None:
        return REGULAR_CLOSE
    return time.fromisoformat(early)


# The regular session, in minutes: 09:30 to 16:00 ET.
SESSION_MINUTES = 390.0


def session_minutes(day: date, calendar: Mapping[str, Any]) -> float:
    """How long ``day``'s regular session is — 390, or 210 on a half-day."""
    close = close_time(day, calendar)
    return float(
        (close.hour - REGULAR_OPEN.hour) * 60 + (close.minute - REGULAR_OPEN.minute)
    )


def minutes_since_open(now: Any, day: Any) -> float | None:
    """Minutes from 09:30 ET on ``day`` to ``now`` (an ISO8601 stamp).

    Negative before the open and on an overnight cycle, whose trading day is
    tomorrow; unbounded after the close — the caller clamps. ``None`` when
    either argument is missing, unparseable, or a clock with no zone.
    """
    if not now or not day:
        return None
    try:
        moment = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
        session_day = date.fromisoformat(str(day))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        return None
    opened = datetime.combine(session_day, REGULAR_OPEN, tzinfo=ET)
    return (moment.astimezone(ET) - opened).total_seconds() / 60.0


def time_normalised_volume(
    volume: Any, average: Any, minutes: float | None, session: float
) -> tuple[float | None, float | None]:
    """``(raw, time-normalised)`` relative volume.

    ``raw`` is today's volume so far over the full-day average, None when
    either is missing, unparseable or not positive. The normalised figure
    scales it to a whole session — ``raw * session / minutes``, minutes
    clamped to the session so a reading after the close is the raw one — and
    is None when raw is, when there is no clock, or before the open.

    One function for the two readers that had the same defect: CATALYST's
    1.5x test (`plays/catalyst.py`) and the Dip Recovery sleeve's 1.2x gate
    (`dip_recovery/signal.py`) both compared volume *so far* with a *full
    day's* average.
    """
    try:
        v = None if volume is None else float(volume)
        a = None if average is None else float(average)
    except (TypeError, ValueError):
        return None, None
    if not v or not a or v < 0 or a < 0 or v != v or a != a:
        return None, None
    raw = v / a
    if minutes is None or minutes <= 0 or session <= 0:
        return raw, None
    return raw, raw * session / min(minutes, session)


def session_of(moment: datetime, day: date, calendar: Mapping[str, Any]) -> str:
    """Which Robinhood order session ``moment`` falls in.

    On an early-close day only the *regular* session moves; the extended band
    is left running to 20:00. Over-claiming extended hours costs nothing —
    no cycle after the close places an entry, and guard checks
    ``all_day_tradability`` per symbol anyway — whereas over-claiming regular
    hours would let a market order be aimed at a shut auction.
    """
    if not is_open(day, calendar):
        return CLOSED

    now = moment.time()
    if now >= EXTENDED_CLOSE or now < PREMARKET_OPEN:
        return ALL_DAY_HOURS
    if now < REGULAR_OPEN:
        return EXTENDED_HOURS
    if now < close_time(day, calendar):
        return REGULAR_HOURS
    return EXTENDED_HOURS


def _slot_type(moment: datetime, day: date, calendar: Mapping[str, Any]) -> str:
    """The cycle §13 schedules for this part of the day.

    Bands, not instants: a routine fires when the scheduler gets to it, and a
    cadence keyed to 09:40:00 exactly would classify a 09:41 firing as nothing
    at all.
    """
    now = moment.time()
    if now >= EXTENDED_CLOSE or now < PREMARKET_OPEN:
        return OVERNIGHT
    if now < REGULAR_OPEN:
        return PREMARKET
    if now < close_time(day, calendar):
        return MONITOR
    return POSTCLOSE


def _entries_remaining(control: Mapping[str, Any], day: date, config) -> int:
    """§10's two-new-positions-a-day cap, against today's count only.

    The count carries the day it was taken on so it self-resets. A bare
    counter would need a separate daily reset, and the failure mode of a stale
    one is an account that silently stops entering — the same class of defect
    §4.4 names for an unsatisfiable cap.
    """
    cap = int(config["max_new_positions_per_day"])
    taken = control.get("new_positions") or {}
    if taken.get("date") != day.isoformat():
        return cap
    return max(0, cap - int(taken.get("count") or 0))


def _entry_deadline(day: date, calendar: Mapping[str, Any], config) -> time:
    """When entries stop: §13's "never in the last 30 min", before the close.

    Derived from the close rather than fixed, so a 13:00 half-day moves the
    deadline to 12:30 with it.
    """
    close = datetime.combine(day, close_time(day, calendar))
    blackout = timedelta(minutes=int(config["entry_blackout_minutes"]))
    return (close - blackout).time()


def decide(ctx: Mapping[str, Any]) -> Cycle:
    """Classify this firing. Pure function of the clock and ``edge_control``."""
    control = ctx.get("control") or {}
    calendar = ctx.get("calendar") or {}
    config = ctx["config"]

    moment = _et(ctx["now"])
    day = trading_day(moment)
    session = session_of(moment, day, calendar)
    remaining = _entries_remaining(control, day, config)

    def cycle(cycle_type: str, reason: str) -> Cycle:
        entries = (
            cycle_type in (FULL, MONITOR)
            and moment.time() < _entry_deadline(day, calendar, config)
            and remaining > 0
        )
        updates: dict = {}
        if cycle_type == FULL:
            updates["last_full_cycle"] = ctx["now"]
        elif cycle_type == OVERNIGHT:
            updates["last_overnight_cycle"] = ctx["now"]

        return Cycle(
            cycle_type=cycle_type,
            session=session,
            now=ctx["now"],
            entries_allowed=entries,
            entries_remaining=remaining,
            exits_allowed=cycle_type in (FULL, MONITOR, OVERNIGHT),
            reconcile_required=cycle_type in (FULL, MONITOR, POSTCLOSE),
            reason=reason,
            trading_day=day.isoformat(),
            clock_updates=updates,
        )

    stamp = moment.strftime("%H:%M ET %a")

    if session == CLOSED:
        return cycle(NOOP, f"{stamp}: no equity session on {day.isoformat()}")

    slot = _slot_type(moment, day, calendar)

    if slot == MONITOR:
        last = control.get("last_full_cycle")
        if last is None or trading_day(_et(last)) != day:
            slot = FULL

    if slot == OVERNIGHT and not control.get("has_whole_share_position", False):
        # P1 x §13: fractional orders place only in regular hours, so while
        # every position is fractional an overnight cycle cannot exit
        # anything. It is not wrong to exist — it goes live the moment a
        # whole-share position does — but until then it is a paid-for no-op.
        return cycle(
            NOOP,
            f"{stamp}: no whole-share position, so no overnight exit is "
            "placeable (P1)",
        )

    if slot == POSTCLOSE:
        # Exempt from the has-work test below: this is the only cycle that
        # writes the day's edge_benchmark row, and §8.4 notes the SPY-relative
        # series cannot be reconstructed after the fact.
        #
        # On a half-day the 13:40 through 16:30 slots all land here, so
        # `report.py` must upsert its benchmark row rather than insert it. A
        # cadence that suppressed the repeats instead would be choosing which
        # slot is the real close, which is the broker's call and not ours.
        return cycle(POSTCLOSE, f"{stamp}: after the close")

    has_work = (
        int(control.get("positions_held") or 0) > 0
        or bool(control.get("unreconciled", False))
        or bool(control.get("pending_events", False))
        or remaining > 0
    )
    if not has_work:
        return cycle(
            NOOP,
            f"{stamp}: nothing held, nothing pending, nothing reconcilable, "
            f"and {remaining} entries left today",
        )

    return cycle(slot, f"{stamp}: {slot} cycle for {day.isoformat()}")
