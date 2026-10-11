"""The cycle's step log: one registry, one stamper, one gate.

An earlier full cycle ran for 3 h 03 m and nothing said which step it was in —
the only timestamps were `edge_cycle_input.created_at`, and a batch's rows all
carry the *transaction's* `now()`, so even those could not show progress inside
a phase. There was a 69-minute hole after `resolve` that no table can explain.

**Three layers, none of which depends on the LLM remembering anything** (this
is the whole point):

1. **Triggers on the tables the runner and the core write to** stamp a step every time a row lands. They live
   on the *tables*, never inside an RPC body: `edge_cycle_await` has been
   redefined three times, and a stamp hidden in a
   function body is deleted by the next `CREATE OR REPLACE`.
2. **`stamped()` here** wraps every core phase (`run.PHASES`) and writes
   `start`, then `end` with `duration_ms`, or `abort` with the exception text —
   and re-raises, so it can never change what a phase does.
3. **`log_gaps()`** is the finalize gate: an expected step with no row becomes a
   `Step log gap` alert and a `success_with_warnings` status on the very next
   cycle, and `edge_step_watchdog()` (pg_cron, every minute) raises
   `Cycle stalled` when a running cycle stops producing rows.

**This module is the single home of the step vocabulary.** The migration's seed
block is generated from `REGISTRY` by `seed_sql()` and a test compares the two,
so a new step is one row here, not five edits.

**Telemetry never fails a cycle.** `stamp()` swallows every exception, counts
it in `STAMP_FAILURES`, and returns False. The gate then reports the missing
rows — a failed stamp write is surfaced, never propagated into a trading
decision.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Sequence

log = logging.getLogger(__name__)

# Events. `beat` is what a trigger writes when a row lands; `start`/`end` bracket
# a core phase; `warn` is a step that finished but something was off; `abort`
# is a step that raised.
START, END, BEAT, WARN, ABORT_EVENT = "start", "end", "beat", "warn", "abort"
EVENTS = (START, END, BEAT, WARN, ABORT_EVENT)

SOURCES = ("trigger", "core", "runner", "watchdog")

# The step names. Never spell one out anywhere else — import it.
S1_SESSION_START = "S1.session_start"
S1_CLAIM = "S1.claim"
# The runner's own beats, one before each phase that touches no
# table (Robinhood fetches, order placement). Everything else in the log is
# written by a trigger or by the core; these six are the only steps the runner
# writes, each with one `select edge_cycle_beat(...)`.
S2_FETCH = "S2.fetch"
S2_POST_BATCH = "S2.post_batch"
S2A_RESOLVE = "S2a.resolve"
S2A_FETCH_QUOTES = "S2a.fetch.quotes"
S2A_FETCH_TRADABILITY = "S2a.fetch.tradability"
S2A_FETCH_EARNINGS = "S2a.fetch.earnings"
S2A_FETCH_FUNDAMENTALS = "S2a.fetch.fundamentals"
S4_PLACE = "S4.place"
S2A_POST_QUOTES = "S2a.post.quotes"
S2A_POST_TRADABILITY = "S2a.post.tradability"
S2A_POST_FUNDAMENTALS = "S2a.post.fundamentals"
S2A_POST_EARNINGS = "S2a.post.earnings"
S2A_DAY_CONTEXT = "S2a.day_context"
S3_DECIDE = "S3.decide"
CORE_RECONCILE = "core.reconcile"
CORE_DECIDE = "core.decide"
S5_POST_RESULTS = "S5.post_results"
S6_FINALIZE = "S6.finalize"
CORE_PLACE = "core.place"
CORE_REPORT = "core.report"
S6_RELEASE = "S6.release"
S6_SESSION_FINISH = "S6.session_finish"
ABORT = "ABORT"


@dataclass(frozen=True)
class Step:
    name: str
    ord: int
    phase: str
    # The longest legitimate quiet period once this is the latest step. The
    # watchdog alerts past it. Deliberately generous where the *runner* is
    # working between two table writes (Robinhood fetches touch no table):
    # after `S1.claim` it fetches the account, orders and the DIP scan; after
    # `S2a.resolve` and `S3.decide` it fetches candidates / places orders.
    ceiling_s: int
    # `edge_cycle_input.kind`s that stamp this step. Empty for steps that are
    # not input-driven. `S2.post_batch` is the fall-through for any other kind.
    kinds: tuple[str, ...] = ()
    source: str = "trigger"
    description: str = ""


REGISTRY: tuple[Step, ...] = (
    Step(S1_SESSION_START, 10, "session", 300, source="trigger",
         description="edge_session_start opened the session-stats row"),
    Step(S1_CLAIM, 20, "session", 1200, source="trigger",
         description="the cycle lock was claimed; the runner is fetching account/orders/scan"),
    Step(S2_FETCH, 25, "fetch", 900, source="runner",
         description="runner beat: Step 2's account, orders and scan fetches are starting"),
    Step(S2_POST_BATCH, 30, "fetch", 900, source="trigger",
         description="Step 2's batch landed in edge_cycle_input"),
    Step(S2A_RESOLVE, 40, "resolve", 900, source="core",
         description="the core resolved the candidate symbols; the runner fetches them next"),
    Step(S2A_FETCH_QUOTES, 41, "resolve", 600, source="runner",
         description="runner beat: fetching candidate quotes"),
    Step(S2A_FETCH_TRADABILITY, 42, "resolve", 600, source="runner",
         description="runner beat: fetching candidate tradability"),
    Step(S2A_FETCH_EARNINGS, 43, "resolve", 1200, source="runner",
         description="runner beat: fetching earnings, one call per stale symbol"),
    Step(S2A_FETCH_FUNDAMENTALS, 44, "resolve", 600, source="runner",
         description="runner beat: fetching fundamentals for the names resolve asked for"),
    Step(S2A_POST_QUOTES, 50, "resolve", 900, kinds=("quotes",),
         description="candidate quotes landed"),
    Step(S2A_POST_TRADABILITY, 51, "resolve", 900, kinds=("tradability",),
         description="candidate tradability landed"),
    Step(S2A_POST_FUNDAMENTALS, 52, "resolve", 900, kinds=("fundamentals",),
         description="candidate fundamentals landed"),
    Step(S2A_POST_EARNINGS, 53, "resolve", 900, kinds=("earnings",),
         description="candidate earnings landed"),
    Step(S2A_DAY_CONTEXT, 60, "resolve", 600, source="trigger",
         description="edge_day_context was written for the running cycle"),
    Step(S3_DECIDE, 70, "decide", 900, source="core",
         description="the core's decide phase; the runner places the plan next"),
    Step(CORE_RECONCILE, 71, "decide", 300, source="trigger",
         description="the reconcile runlog row was written"),
    Step(CORE_DECIDE, 72, "decide", 300, source="trigger",
         description="the decide runlog row was written"),
    Step(S4_PLACE, 75, "place", 900, source="runner",
         description="runner beat: placing the plan's cancels, orders and protection"),
    Step(S5_POST_RESULTS, 80, "place", 600, kinds=("placements", "placement_rejections", "placement_alerts"),
         description="the runner posted what it placed"),
    Step(S6_FINALIZE, 90, "finalize", 300, source="core",
         description="the core's finalize phase"),
    Step(CORE_PLACE, 91, "finalize", 300, source="trigger",
         description="the placement runlog row was written"),
    Step(CORE_REPORT, 92, "finalize", 300, source="trigger",
         description="the report runlog row was written"),
    Step(S6_RELEASE, 95, "session", 300, source="trigger",
         description="the cycle lock was released"),
    Step(S6_SESSION_FINISH, 96, "session", 0, source="trigger",
         description="edge_session_finish closed the session-stats row"),
    Step(ABORT, 99, "session", 0, source="trigger",
         description="the cycle failed or was aborted"),
)

STEP_NAMES = tuple(step.name for step in REGISTRY)
_BY_NAME = {step.name: step for step in REGISTRY}

# `run.PHASES` -> the step the core stamps around it. `tests/test_step_log_contract.py`
# asserts this covers every phase in `run.PHASES`, so adding a phase without a
# step fails CI instead of running unlogged.
PHASE_STEPS: Mapping[str, str] = {
    "resolve": S2A_RESOLVE,
    "decide": S3_DECIDE,
    "finalize": S6_FINALIZE,
}

# Wall-clock budgets, in minutes per cycle type. One number does two jobs:
# past it `edge_cycle_await` / `edge_cycle_beat` report a negative
# `budget_left_min` and the runner stops fetching earnings and fundamentals,
# and `edge_step_watchdog()` raises `Cycle over budget`.
#
# `config/config.json`'s `cycle_budget_min` is the operator's copy; the daily
# schedule job publishes it to `edge_control` (Postgres cannot read the
# checkout), and `edge_cycle_budget_min()` in `db/schema.sql` falls back to these same
# numbers when no row is published. `tests/test_step_log_contract.py` pins all
# three to each other.
CYCLE_BUDGET_MIN: Mapping[str, int] = {
    "full": 40,
    "monitor": 20,
    "dip-monitor": 15,
    "postclose": 10,
    "overnight": 20,
}
DEFAULT_BUDGET_MIN = 30
CYCLE_BUDGET_KEY = "cycle_budget_min"

# The runner beats whose absence the finalize gate reports. Only the ones every
# such cycle makes: `S2a.fetch.earnings` / `.fundamentals` are skipped when
# `resolve` asks for none, and `S4.place` when the plan is empty.
RUNNER_BEATS_ALWAYS = (S2_FETCH,)
RUNNER_BEATS_RESOLVED = (S2A_FETCH_QUOTES, S2A_FETCH_TRADABILITY)


def step(name: str) -> Step:
    return _BY_NAME[name]


# ---------------------------------------------------------------------------
# The stamper
# ---------------------------------------------------------------------------

# Every stamp write that raised, this process. Read by `log_gaps`' caller and by
# tests; never reset in production code (a warm Vercel instance keeps it, which
# is the point — a rising count is the signal).
STAMP_FAILURES = 0


def stamp(
    supabase: Any,
    cycle_id: str,
    step_name: str,
    event: str,
    detail: Mapping[str, Any] | None = None,
    source: str = "core",
) -> bool:
    """Write one row of the log. **Never raises** (R5): a broken log must not
    stop a trade, delay a plan or change a phase's return value.

    A `supabase` without a `stamp` method (an older double) is a no-op, not an
    error — the gate reports the resulting gap.
    """
    global STAMP_FAILURES
    writer = getattr(supabase, "stamp", None)
    if writer is None:
        return False
    try:
        return bool(writer(cycle_id, step_name, event, dict(detail) if detail else None, source))
    except Exception as exc:  # noqa: BLE001 — telemetry is never load-bearing
        STAMP_FAILURES += 1
        log.warning("step stamp failed (%s %s %s): %s", cycle_id, step_name, event, exc)
        return False


class Span:
    """What `stamped()` yields: fill `detail` and it is written on `end`."""

    def __init__(self) -> None:
        self.detail: dict[str, Any] = {}


@contextmanager
def stamped(
    supabase: Any, cycle_id: str, step_name: str, detail: Mapping[str, Any] | None = None
) -> Iterator[Span]:
    """`start`, then `end` (with `duration_ms`) — or `abort` with the exception
    text, and **re-raise**. The wrapper adds observation, never behaviour."""
    span = Span()
    span.detail.update(detail or {})
    stamp(supabase, cycle_id, step_name, START, span.detail)
    began = time.monotonic()
    try:
        yield span
    except BaseException as exc:
        stamp(
            supabase,
            cycle_id,
            step_name,
            ABORT_EVENT,
            {**span.detail, "error": f"{type(exc).__name__}: {exc}"[:500],
             "duration_ms": int((time.monotonic() - began) * 1000)},
        )
        raise
    stamp(
        supabase,
        cycle_id,
        step_name,
        END,
        {**span.detail, "duration_ms": int((time.monotonic() - began) * 1000)},
    )


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def expected_steps(
    cycle_type: str,
    *,
    reconcile_required: bool = True,
    resolved_symbols: int = 0,
) -> list[str]:
    """Steps that must have left a row **by the time finalize runs**.

    Excluded on purpose: `S5.post_results` (the runner skips Step 5 on an empty
    plan, and a quiet cycle is normal), and everything that happens after
    finalize starts (`core.report`, `S6.release`, `S6.session_finish`).
    `noop` cycles run nothing and expect nothing.
    """
    if cycle_type == "noop":
        return []
    names = [S1_SESSION_START, S1_CLAIM, *RUNNER_BEATS_ALWAYS, S2_POST_BATCH, S3_DECIDE]
    if reconcile_required:
        names.append(CORE_RECONCILE)
    if cycle_type != "postclose":
        names.append(CORE_DECIDE)
    if resolved_symbols > 0:
        names += [
            S2A_RESOLVE, *RUNNER_BEATS_RESOLVED,
            S2A_POST_QUOTES, S2A_POST_TRADABILITY, S2A_DAY_CONTEXT,
        ]
    return names


def log_gaps(
    supabase: Any,
    cycle_id: str,
    cycle_type: str,
    *,
    reconcile_required: bool = True,
) -> list[str] | None:
    """Expected steps with no row, in registry order. `None` when the log could
    not be read at all — the caller treats that as "unknown", never as a gap it
    can act on (R5)."""
    try:
        rows = supabase.select("edge_cycle_step", f"cycle_id=eq.{cycle_id}&select=step,event,detail")
    except Exception as exc:  # noqa: BLE001
        log.warning("step log unreadable for %s: %s", cycle_id, exc)
        return None

    seen = {row.get("step") for row in rows if isinstance(row, Mapping)}
    resolved = 0
    for row in rows:
        if isinstance(row, Mapping) and row.get("step") == S2A_RESOLVE:
            try:
                resolved = max(resolved, int((row.get("detail") or {}).get("symbols") or 0))
            except (TypeError, ValueError):
                continue
    wanted = expected_steps(
        cycle_type, reconcile_required=reconcile_required, resolved_symbols=resolved
    )
    return [name for name in wanted if name not in seen]


def fetch_warnings(supabase: Any, cycle_id: str) -> dict[str, int]:
    """Only the non-zero ones are returned; `{}` when the log is clean or unreadable
    (R5)."""
    try:
        rows = supabase.select(
            "edge_cycle_step", f"cycle_id=eq.{cycle_id}&event=eq.{WARN}&select=step,event,detail"
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("step log unreadable for %s: %s", cycle_id, exc)
        return {}

    counts = {"outside_need": 0, "per_row_writes": 0, "oversize_batches": 0}
    for row in rows:
        if not isinstance(row, Mapping) or row.get("event") != WARN:
            continue
        detail = row.get("detail") or {}
        if not isinstance(detail, Mapping):
            continue
        if detail.get("outside_need"):
            try:
                counts["outside_need"] += int(detail.get("count") or 0)
            except (TypeError, ValueError):
                counts["outside_need"] += 1
        if detail.get("per_row_write"):
            counts["per_row_writes"] += 1
        if detail.get("oversize_batch"):
            counts["oversize_batches"] += 1
    return {key: value for key, value in counts.items() if value}


def gap_alert(cycle_id: str, missing: Sequence[str]) -> dict:
    return {
        "title": "Step log gap",
        "body": (
            f"Cycle {cycle_id[:8]} finished, but the step log has no row for: "
            f"{', '.join(missing)}. The cycle itself is unaffected. A missing step "
            "means a trigger, a stamp write or a runner step did not leave its "
            "mark — check edge_cycle_timeline for this cycle."
        ),
        "priority": "default",
        "cycle_id": cycle_id,
    }


def result_detail(phase: str, result: Mapping[str, Any]) -> dict[str, Any]:
    """The counts a phase's `end` row carries. `symbols`/`stale`/`statics_stale`
    on `resolve` are the numbers that would have exposed 149-vs-21 the moment it
    happened."""
    if phase == "resolve":
        return {
            "status": result.get("status"),
            "symbols": len(result.get("symbols") or []),
            "stale": len(result.get("stale") or []),
            "statics_stale": len(result.get("statics_stale") or []),
            "fundamentals_needed": len(result.get("fundamentals_needed") or []),
        }
    if phase == "decide":
        return {
            "status": result.get("status"),
            "orders": len(result.get("plan") or []),
            "protection": len(result.get("protection") or []),
            "cancels": len(result.get("cancels") or []),
        }
    summary = result.get("alerts_summary") or {}
    return {"status": result.get("status"), "alerts": summary.get("count")}


# ---------------------------------------------------------------------------
# The migration seed
# ---------------------------------------------------------------------------

SEED_BEGIN = "-- BEGIN GENERATED step registry (services/core/core/steps.py: seed_sql)"
SEED_END = "-- END GENERATED step registry"


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def seed_sql() -> str:
    """The `edge_step_kind` seed, generated so the registry and the table cannot
    be hand-edited apart. `db/schema.sql` embeds this text between
    `SEED_BEGIN` and `SEED_END`; `tests/test_step_log_contract.py` regenerates it
    and fails on any difference."""
    lines = [SEED_BEGIN]
    lines.append(
        "insert into edge_step_kind (step, ord, phase, ceiling_s, kinds, source, description) values"
    )
    rows = []
    for s in REGISTRY:
        kinds = "array[" + ", ".join(_lit(k) for k in s.kinds) + "]::text[]" if s.kinds else "'{}'::text[]"
        rows.append(
            f"  ({_lit(s.name)}, {s.ord}, {_lit(s.phase)}, {s.ceiling_s}, {kinds}, "
            f"{_lit(s.source)}, {_lit(s.description)})"
        )
    lines.append(",\n".join(rows))
    lines.append(
        "on conflict (step) do update set ord = excluded.ord, phase = excluded.phase,\n"
        "  ceiling_s = excluded.ceiling_s, kinds = excluded.kinds, source = excluded.source,\n"
        "  description = excluded.description;"
    )
    lines.append(SEED_END)
    return "\n".join(lines)
