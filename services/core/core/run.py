"""The cycle, run server-side. Two phases, fifteen writes, no judgment.

`docs/routine/cycle-prompt.md` Steps 5-10 as a function. Every rule it appears
to apply belongs to a module underneath it — the same sentence `src/cli.py`'s
docstring opens with, and for the same reason: this is plumbing between
`cli.candidates`, `cli.reconcile`, `cli.decide` and `cli.report`, and it
decides nothing any of them would not have decided when a live session ran the
identical commands against the identical `cycle.json`.

**Why two phases.** The natural shape is one call that runs
`reconcile -> decide -> report`. It cannot be one call, and the reason is
`report.py`'s own inputs: it reads `failure` (the verbatim broker text of a
placement that was refused) and its alerts are about what actually reached the
broker. A report written before the session placed would be a report about a
cycle that had not happened yet. So:

* **decide** (`POST /api/cycle`, phase `decide`) — assemble, candidates,
  reconcile, decide, and the nine writes Step 7 makes. Returns the plan.
* **finalize** (phase `finalize`, after the session has placed) — the Step 8c
  placement runlog row, `report`, and the Step 9/10 writes.

The split is the prompt's own: Step 7 writes the decide runlog row *before*
placing (Invariant 3), and Steps 9-10 run after. Nothing moved; the boundary
just became a process boundary.

**What is derived here and not accepted from the runner.** `triggers` and
`macro_targets` come from `cli.candidates`, `reconcile` from `cli.reconcile`,
`fetch_meta` from `cli.day_context_plan`. `assemble.DERIVED_KINDS` refuses a
runner that posts one, so the two sides cannot quietly disagree about who owns
a key.

**Failure is fail-closed, everywhere.** Any exception aborts the phase, writes
`status = 'failed'` with the exception's own sentence, and returns no plan. The
runner places nothing without a plan, which is the whole safety property this
service has to hold: §14's "when you cannot tell, stop", enforced by there
being nothing to place rather than by a prompt remembering to stop.
"""

from __future__ import annotations

import json
import traceback
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from src import broker_shapes, cadence, cli, plays
from src import deployment as deployments
from src import snapshot as snapshots
from src.plays import catalyst
from src.reconcile import DIVERGENCE_CLASSES, RECONCILIATION_DIVERGENCE_PERSISTENT

from . import steps
from .assemble import assemble
from .supabase import Supabase

RESOLVE = "resolve"
DECIDE = "decide"
FINALIZE = "finalize"
PHASES = (RESOLVE, DECIDE, FINALIZE)

CORE = "core"

# The three CI-published files. `cli.load_config` already reads `config/` out
# of the deployment for the same reason; these are the other half of that
# channel and until now the *routine* read them, because the routine had a git
# checkout. **The runner does not.** It has no clone, so it cannot post
# `snapshot`/`events`/`macro` at all — the fetch rows that name them carry
# `source: "checkout"` and there is no checkout on the hands' side any more.
# The core fills them itself, from `edge_data_files` first and the bundled
# `data/` file second (`_payload`) — `.vercelignore` still keeps `data/` in
# the bundle as the fallback, but `vercel.json`'s `ignoreCommand` means a
# data-only commit no longer triggers a build at all, so the bundle is a
# snapshot of whenever the last *code* change shipped, not of whenever CI last
# ran.
#
# A row the runner *did* post still wins: `merge-fetch`-style session recovery
# is the one case where a value arrives from outside, and overriding it here
# would silently discard it.
# `(filename, inner key)`. **`events` is unwrapped and the other two are
# not**, and that asymmetry is the file's, not a preference: every consumer in
# `src/` reads `events` as `{symbol: [rows]}` (`plays/core.py:550`,
# `reconcile.py:1474`), which is `data/events.json`'s *`events` key*, while
# `macro` and `snapshot` are read at their top level (`macro["releases"]`,
# `snapshot["holdings"]`) and so are the whole file. Appendix D says
# "`data/events.json` (checkout)" for all three, which is true of where they
# come from and silent about this. Handing `candidates.assemble` the whole
# events file makes it iterate `("as_of", "2026-09-10")` as a symbol and its
# date string as a row list -- found exactly that way here.
DATA_DIR = Path(__file__).resolve().parents[3] / "data"
CHECKOUT_DATA = {
    "snapshot": ("universe.json", None),
    "events": ("events.json", "events"),
    "macro": ("macro.json", None),
}

def checkout_document(supabase: Supabase, key: str) -> tuple[Any, dict | None]:
    """One `CHECKOUT_DATA` key: `edge_data_files` first, the bundled `data/`
    file second, `(None, None)` when neither exists.

    Shared by `_payload` and the pre-market job, which needs the same
    `snapshot`/`events`/`macro` to discover today's candidates before the open —
    one reader, so the two can never disagree about which file is today's.
    """
    filename, inner = CHECKOUT_DATA[key]
    # Matched by `name` in Python, not trusted off `rows[0]`: the
    # `FakeSupabase` test double intentionally does not filter by query
    # string (it has exactly one cycle to serve), and a table keyed on
    # `name` is the first caller here that queries the same table for
    # three different rows in one request. This is correct against real
    # PostgREST too — `select=name,as_of,payload` already filters — so
    # nothing changes there; it only stops a test double from handing key
    # `snapshot` the row that was written for `macro`.
    rows = supabase.select("edge_data_files", f"name=eq.{key}&select=name,as_of,payload")
    row = next((r for r in rows if isinstance(r, Mapping) and r.get("name") == key), None)
    if row is not None:
        document = row.get("payload") or {}
        value = document if inner is None else (document.get(inner) or {})
        return value, {"source": "supabase", "as_of": row.get("as_of")}

    path = DATA_DIR / filename
    if not path.exists():
        # §14: "`data/events.json` or `data/macro.json` stale or missing ->
        # Proceed. CATALYST and MACRO no-op; two plays remain. Degradation,
        # not failure." An absent `universe.json` is caught downstream by
        # `universe.py`, which blocks entries itself.
        return None, None
    document = json.loads(path.read_text(encoding="utf-8"))
    value = document if inner is None else (document.get(inner) or {})
    return value, {"source": "checkout", "as_of": document.get("as_of")}


STATUS_RESOLVED = "resolved"
STATUS_DECIDED = "decided"
STATUS_FINALIZED = "finalized"
STATUS_FAILED = "failed"
STATUS_NOOP = "noop"

# `edge_session_stats.status` — the business-outcome word, distinct from the
# `edge_cycle_output.status` four above (which say
# only how far this *phase* got, never whether the cycle's actions succeeded).
# Ordered worst-to-best; `_session_status` returns the first one that applies.
SESSION_BLOCKED = "blocked"
SESSION_PARTIAL_SUCCESS = "partial_success"
SESSION_SUCCESS_WITH_WARNINGS = "success_with_warnings"
SESSION_SUCCESS = "success"
SESSION_SKIPPED = "skipped"


class CycleError(RuntimeError):
    """This cycle cannot be run. Always terminal, never retried here."""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(cycle_id: str, phase: str, supabase: Supabase, config_dir=None) -> dict:
    if phase not in PHASES:
        raise CycleError(f"phase must be one of {PHASES}, got {phase!r}")
    if not cycle_id:
        raise CycleError("cycle_id is required")

    try:
        # Every phase is bracketed in the step log — `start`, then `end` with
        # the duration and the counts, or `abort` with the exception (re-raised
        # here, so it reaches the boundary below unchanged).
        # `steps.PHASE_STEPS` must name every entry of `PHASES`
        # (`tests/test_step_log_contract.py`), so a phase added without a step
        # fails CI instead of running unlogged.
        with steps.stamped(supabase, cycle_id, steps.PHASE_STEPS[phase]) as span:
            if phase == RESOLVE:
                result = _resolve_phase(cycle_id, supabase, config_dir)
            elif phase == DECIDE:
                result = _decide_phase(cycle_id, supabase, config_dir)
            else:
                result = _finalize_phase(cycle_id, supabase, config_dir)
            span.detail.update(steps.result_detail(phase, result))
    except Exception as exc:  # noqa: BLE001 — the boundary; see the docstring
        detail = f"{type(exc).__name__}: {exc}"
        _store(
            supabase,
            cycle_id,
            status=STATUS_FAILED,
            detail=detail[:2000],
            trace=traceback.format_exc()[-2000:],
        )
        return {"cycle_id": cycle_id, "status": STATUS_FAILED, "detail": detail}

    _store(supabase, cycle_id, **result)
    return {"cycle_id": cycle_id, **result}


# ---------------------------------------------------------------------------
# Phase 0 — resolve the candidate symbol set
# ---------------------------------------------------------------------------


def _resolve_phase(cycle_id: str, supabase: Supabase, config_dir) -> dict:
    """Answers the one question a `candidates`-scoped fetch row cannot answer
    for itself: which symbols.

    `edge_should_run`'s `fetch` list carries `scope: "candidates"` rows
    (quotes, indicators, tradability, earnings, fundamentals) with no
    `symbols` array, because the candidate set does not exist until
    `cli.candidates` has run — and `cli.candidates` needs `day_context`,
    `universe`/`events`/`macro` and the account view, all of which this phase
    already has through `_payload`, exactly as `_decide_phase` does.

    This does not decide anything Step 5 didn't already decide when a live
    session ran it — `cli.candidates` is idempotent and `_decide_phase` calls
    it again once the runner has posted the resolved symbols' live data, the
    same "second pass is free" rule `_payload`'s docstring states for
    `cli.normalized`. Running it twice here is deliberate: the alternative is
    carrying resolve's `discovered`/`triggers` across two HTTP calls through a
    field nothing else needs once decide is running, for a call that costs no
    network I/O (the day-context read that backs it happened once already, in
    `_payload`, and `candidate_discovery.assemble` reads from what's already
    in `payload`).

    A `candidates`-scoped call still needs a *tool*, an *interval*, a
    *start_time* — none of that is symbol-dependent, and the runner already
    has it verbatim off `should.fetch`'s own rows from Step 0. This phase
    hands back only what those rows are missing.
    """
    loaded = cli.load_config(config_dir)
    # `dip_survivors` rides in `payload`; nothing in `extras` is read at
    # resolve time.
    payload, _extras = _payload(cycle_id, supabase, loaded, RESOLVE)

    plan_fetch = cli.plan_fetch(payload, loaded)
    cycle = plan_fetch["cycle"]
    cycle_type = cycle["cycle_type"]
    trading_day = cycle["trading_day"]

    if cycle_type == cadence.NOOP:
        # Same reasoning as `_decide_phase`'s own NOOP branch: `edge_should_run`
        # should have kept the runner out, but the clock can move between that
        # call and this one. Nothing to resolve, nothing written.
        return {
            "status": STATUS_NOOP,
            "symbols": [],
            "stale": [],
            "statics_stale": [],
            "fundamentals_needed": [],
        }

    if "regime_inputs" not in payload:
        regime_row = supabase.select(
            "edge_regime", f"trading_day=eq.{trading_day}&select=payload"
        )
        regime_payload = regime_row[0].get("payload") if regime_row else None
        inputs = (regime_payload or {}).get("regime_inputs")
        if inputs:
            payload["regime_inputs"] = inputs

    discovered = cli.candidates(payload, loaded)
    payload["triggers"] = list(discovered.get("triggers") or [])
    payload["macro_targets"] = list(discovered.get("macro_targets") or [])

    # The runner fetches live data for every symbol this phase returns, one
    # call at a time, so the size of this list *is* the cycle's cost.
    # `_decide_phase` applies the same trim, deterministically, so a name
    # trimmed here is never evaluated there without the data it never got.
    scope = _apply_scope_ceiling(payload, loaded)
    if scope.trimmed or scope.mandatory_over:
        _report_scope(supabase, cycle_id, scope)

    # Same call `_decide_phase` makes for `fetch_meta`, same reason: `symbols`
    # is never written onto `payload` itself, only merged into the dict handed
    # to `day_context_plan` — the ordering (after `cli.candidates`, which is
    # what makes `_symbols(payload)` non-empty) matters as much as the call.
    symbols = _symbols(payload)
    plan = cli.day_context_plan({**payload, "symbols": symbols}, loaded)

    return {
        "status": STATUS_RESOLVED,
        "symbols": symbols,
        "stale": list(plan.get("stale") or []),
        "statics_stale": list(plan.get("statics_stale") or []),
        # The only names the runner fetches `get_equity_fundamentals` for.
        # Carried to the runner by `edge_cycle_await`; a runner on the
        # older prompt ignores the key and keeps fetching every symbol, which is
        # wasteful but still correct.
        "fundamentals_needed": _fundamentals_needed(payload, loaded, trading_day, symbols),
    }


# ---------------------------------------------------------------------------
# Phase 1 — through `decide`
# ---------------------------------------------------------------------------


def _decide_phase(cycle_id: str, supabase: Supabase, config_dir) -> dict:
    loaded = cli.load_config(config_dir)
    payload, extras = _payload(cycle_id, supabase, loaded, DECIDE)

    # Step 2, minus the minting: the id was minted by the runner and claimed
    # against the lock with it, so a second one generated here would leave the
    # lock and the runlog naming different cycles (`cli._envelope`).
    plan_fetch = cli.plan_fetch(payload, loaded)
    cycle = plan_fetch["cycle"]
    cycle_type = cycle["cycle_type"]
    trading_day = cycle["trading_day"]

    if cycle_type == cadence.NOOP:
        # Step 3. `edge_should_run` should have kept the runner out of here
        # entirely; reaching it means the clock moved between that call and
        # this one, and the answer is the same one the prompt gives — write
        # the row, place nothing.
        supabase.rpc(
            "edge_runlog_write",
            {
                "p_cycle_id": cycle_id,
                "p_cycle_type": cycle_type,
                "p_trading_day": trading_day,
                "p_command": "plan-fetch",
                "p_stdout": plan_fetch,
            },
        )
        return {"status": STATUS_NOOP, "plan": [], "protection": [], "cancels": []}

    # The pre-market data job (`services/core/core/premarket.py`) writes a
    # `regime_inputs` block to `edge_regime` once a day; pull it in when the
    # runner did not post one itself. Presence, not truthiness — a runner that
    # posted `regime_inputs` (session recovery) keeps it. `regime.evaluate`
    # reads an absent block as "no opinion", so a missing `edge_regime` row is
    # a safe degradation, never a dark regime.
    if "regime_inputs" not in payload:
        regime_row = supabase.select(
            "edge_regime", f"trading_day=eq.{trading_day}&select=payload"
        )
        regime_payload = regime_row[0].get("payload") if regime_row else None
        inputs = (regime_payload or {}).get("regime_inputs")
        if inputs:
            payload["regime_inputs"] = inputs

    # -- Step 5: the candidate set ----------------------------------------
    discovered = cli.candidates(payload, loaded)
    payload["triggers"] = list(discovered.get("triggers") or [])
    payload["macro_targets"] = list(discovered.get("macro_targets") or [])
    # The same trim `resolve` made (no alert here — it already raised one), so
    # decide never weighs a candidate the runner was not asked to fetch. What
    # was trimmed joins Step 5's drop list below.
    scope = _apply_scope_ceiling(payload, loaded)
    control_updates = dict(discovered.get("control_updates") or {})
    # Reaching this point means a real cycle is running — a NOOP cycle returned
    # above without ever setting this. `edge_control.needs_session` promised
    # "something changed, a session should look"; only a session that actually
    # looked may say that stopped being true, so this clears it rather than
    # `watch.py` clearing it early and losing a flag a session never got to see.
    control_updates["needs_session"] = None

    # Step 7's one-line precondition: `fetch_meta` is `day-context-plan`'s
    # output verbatim, and `decide` only counts both lists for the runlog.
    payload["fetch_meta"] = cli.day_context_plan(
        {**payload, "symbols": _symbols(payload)}, loaded
    )

    # On a cycle that ran Step 2a (entries allowed — the only cycle type whose
    # fetch list carries `candidates`-scoped rows), the core now writes the
    # day's cache from what the runner posted, and checks the runner fetched
    # fundamentals only where `resolve` asked. Both are fail-soft: a cache
    # write or a scope warning must never cost the cycle its plan. The write
    # itself waits until after the decide runlog row (below), so Step 7's
    # writes keep their order and nothing precedes Invariant 3's row.
    runner_fetch: dict[str, Any] = {}
    needed: list[str] = []
    if cycle["entries_allowed"]:
        needed = _fundamentals_needed(payload, loaded, trading_day, _symbols(payload))
        runner_fetch = _fundamentals_scope(supabase, cycle_id, extras.get("posted") or {}, needed)

    reconcile_json: dict | None = None
    if cycle["reconcile_required"]:
        _require_broker_view(payload)
        reconcile_json = cli.reconcile(payload, loaded)
        _write_reconcile(supabase, cycle_id, cycle_type, trading_day, reconcile_json)
        payload["reconcile"] = reconcile_json
        control_updates.update(reconcile_json.get("control_updates") or {})

    if cycle_type == cadence.POSTCLOSE:
        # `reconcile -> cancel -> report`, no decide. The cancels are
        # `reconcile.cancels`, already excluding anything flagged
        # `protective_stop`/`protective_target` and anything the Dip Recovery
        # side owns.
        cancels = list((reconcile_json or {}).get("cancels") or [])
        return {
            "status": STATUS_DECIDED,
            "plan": [],
            "protection": [],
            "cancels": cancels,
            "control_updates": control_updates,
        }

    # P3: Step 5's drop list, handed to `decide` so its deployment funnel can
    # count what discovery surfaced before the sector filter, the caps and the
    # rotation took names out. `decide` only counts it.
    payload["candidates_dropped"] = list(discovered.get("candidates_dropped") or []) + list(
        scope.trimmed
    )

    decide_json = cli.decide(payload, loaded)
    # Not one of `cli.decide`'s own
    # keys — added here, after it returns, so the derivation in `src/` stays
    # untouched by a plumbing concern. `_finalize_phase` reads it back off
    # this same runlog row via `_runlog`, which is how `_session_status`
    # sees it without a second write.
    decide_json = {**decide_json, "data_provenance": _data_provenance(extras, trading_day, loaded)}
    # Step 5's drop list — the 20-name cap, the regime's sector filter and
    # the operator's sector rotation — was discarded on this path until now,
    # so a runner runlog could not answer "why did this play stop firing".
    # Added here for the same reason `data_provenance` is: a plumbing
    # concern kept out of `cli.decide`'s own derivation. The live session
    # merges the same list into its runlog by hand (Step 5).
    decide_json["candidates_dropped"] = list(discovered.get("candidates_dropped") or []) + list(
        scope.trimmed
    )
    if runner_fetch:
        decide_json["runner_fetch"] = runner_fetch
    # S0: the decision snapshot — every candidate this cycle judged, with the
    # features it was judged on (`src/snapshot.py`). Carried on the runlog row
    # as well as written to its own table below: `finalize` has no
    # `day_context` (see `_payload`), so it could not rebuild these rows, and
    # a row that survives in the runlog can be restored if the table write
    # fails. `[]` on a cycle that cannot buy.
    decide_json["decision_snapshot"] = snapshots.rows(payload, decide_json, loaded["etfs"])

    # Invariant 3: the runlog row lands before anything can be placed. The
    # runner cannot place until this function returns, so the ordering is
    # structural here rather than a rule a session has to follow.
    supabase.rpc(
        "edge_runlog_write",
        {
            "p_cycle_id": cycle_id,
            "p_cycle_type": cycle_type,
            "p_trading_day": trading_day,
            "p_command": "decide",
            "p_stdout": decide_json,
        },
    )
    play_updates = decide_json.get("play_state_updates") or {}
    if play_updates:
        # `{}` on a NOOP cycle, and the prompt skips the call only there.
        supabase.rpc("edge_play_state_apply", {"p_updates": play_updates})

    # P3: the deployment decision, written now so a cycle that dies before
    # `finalize` still leaves its row. `finalize` writes it again, completed
    # with what the broker took, and is where a failure becomes visible.
    _write_deployment(supabase, decide_json.get("deployment"))

    # S0: the snapshot's own table. After the runlog row, for the same reason
    # as the deployment record, and equally unable to cost the cycle its plan.
    # Quiet here; `finalize` writes the rows again and names a failure.
    _write_snapshot(supabase, decide_json["decision_snapshot"])

    if cycle["entries_allowed"]:
        _write_day_context(
            supabase,
            cycle_id,
            trading_day,
            payload,
            extras.get("posted") or {},
            stale=payload["fetch_meta"].get("stale") or [],
            needed=needed,
        )

    control_updates.update(decide_json.get("control_updates") or {})
    plan = decide_json.get("plan") or {}

    return {
        "status": STATUS_DECIDED,
        "plan": list(plan.get("orders") or []),
        "protection": list(plan.get("protection") or []),
        "cancels": list(plan.get("cancels") or []),
        "control_updates": control_updates,
    }


def _write_deployment(supabase: Supabase, row: Mapping[str, Any] | None) -> str | None:
    """Upsert one `edge_deployment_decision` row; the error text if it failed.

    **Never raises.** The record describes a plan that already exists, so a
    failure here — the migration not applied, a bad row, the network — must
    not cost the cycle that plan. The caller decides how loudly to say so:
    the decide phase stays quiet (finalize will try again), finalize puts the
    error in the status alert.
    """
    if not row:
        return None
    try:
        supabase.rpc("edge_deployment_decision_write", {"p_row": dict(row)})
    except Exception as exc:  # noqa: BLE001 - telemetry never fails a cycle
        return f"{type(exc).__name__}: {exc}"
    return None


def _write_snapshot(supabase: Supabase, rows: Sequence[Mapping[str, Any]] | None) -> str | None:
    """Upsert one cycle's `edge_decision_snapshot` rows; the error text if it
    failed. **Never raises**, for `_write_deployment`'s reason: the rows
    describe a decision that already exists."""
    if not rows:
        return None
    try:
        supabase.rpc("edge_decision_snapshot_write", {"p_rows": [dict(r) for r in rows]})
    except Exception as exc:  # noqa: BLE001 - telemetry never fails a cycle
        return f"{type(exc).__name__}: {exc}"
    return None


def _write_reconcile(
    supabase: Supabase,
    cycle_id: str,
    cycle_type: str,
    trading_day: str,
    result: Mapping[str, Any],
) -> None:
    """Step 7's six writes plus the symbol lock, in the prompt's order.

    A reconcile that *threw* never reaches here — the exception propagates to
    `run`, `unreconciled` stays set and the next cycle is awake, which is
    exactly what the prompt says a thrown reconcile leaves behind.
    """
    supabase.rpc(
        "edge_runlog_write",
        {
            "p_cycle_id": cycle_id,
            "p_cycle_type": cycle_type,
            "p_trading_day": trading_day,
            "p_command": "reconcile",
            "p_stdout": result,
            "p_placements": [],
            "p_rejections": [],
            "p_book": {
                "trading_day": trading_day,
                "positions": result.get("positions") or [],
            },
        },
    )
    supabase.rpc(
        "edge_control_apply", {"p_updates": result.get("control_updates") or {}}
    )
    supabase.rpc(
        "edge_loss_closes_append",
        {"p_rows": result.get("loss_closes") or [], "p_cycle_id": cycle_id},
    )
    # The only writer of `edge_play_state.realized_pnl` now — it credits a
    # play once per closing trade, however many cycles re-offer it.
    supabase.rpc(
        "edge_realized_trades_append",
        {"p_rows": result.get("realized_trades") or [], "p_cycle_id": cycle_id},
    )
    supabase.rpc(
        "edge_play_state_apply", {"p_updates": result.get("play_state_updates") or {}}
    )
    supabase.rpc(
        "edge_fill_quality_write",
        {
            "p_rows": result.get("fills_quality") or [],
            "p_cycle_id": cycle_id,
            "p_trading_day": trading_day,
        },
    )
    # `protection_plan.desired` (`reconcile._protection_plan`) is
    # `src/plays/protection.desired`'s answer for the whole book, every
    # reconciling cycle — a full replace, not an append, because it describes
    # what should be resting *now*, not a history of what was computed.
    # `services/core/core/watch.py` reads it back so the 30-minute
    # state-change gate never has to recompute ATR/signals itself.
    supabase.rpc(
        "edge_protection_apply",
        {"p_orders": (result.get("protection_plan") or {}).get("desired") or []},
    )

    for symbol in result.get("symbol_lock_claims") or []:
        claimed = supabase.rpc(
            "edge_symbol_lock_claim", {"p_symbol": symbol, "p_owner": CORE}
        )
        if claimed is False:
            # The Dip Recovery side owns it. Alert and continue — never retry,
            # never re-place on that symbol.
            supabase.rpc(
                "edge_alerts_enqueue",
                {
                    "p_alerts": [
                        {
                            "title": f"Symbol lock refused: {symbol}",
                            "body": (
                                f"{symbol} is owned by another strategy; core "
                                "reconciled it but will not claim or re-place "
                                "on it."
                            ),
                            "priority": "high",
                            "cycle_id": cycle_id,
                        }
                    ]
                },
            )
    for symbol in result.get("symbol_lock_releases") or []:
        supabase.rpc("edge_symbol_lock_release", {"p_symbol": symbol, "p_owner": CORE})


# ---------------------------------------------------------------------------
# Phase 2 — after the session has placed
# ---------------------------------------------------------------------------


def _finalize_phase(cycle_id: str, supabase: Supabase, config_dir) -> dict:
    loaded = cli.load_config(config_dir)
    payload, extras = _payload(cycle_id, supabase, loaded, FINALIZE)

    plan_fetch = cli.plan_fetch(payload, loaded)
    cycle = plan_fetch["cycle"]
    cycle_type = cycle["cycle_type"]
    trading_day = cycle["trading_day"]

    prior = _prior_output(supabase, cycle_id)
    control_updates = dict(prior.get("control_updates") or {})

    # Read phase 1's output back out of the runlog rather than recomputing it.
    # Re-running `decide` here would be a second decision against a book that
    # has since changed (the session just placed into it), and §8's
    # single-decider rule does not stop applying because the two runs are three
    # minutes apart.
    runlog = _runlog(supabase, cycle_id)
    decide_json = runlog.get("decide") or {}
    reconcile_json = runlog.get("reconcile") or {}
    if reconcile_json:
        payload["reconcile"] = reconcile_json
    for key in ("entries", "exits", "blocked", "manual_required", "held_for_news"):
        if decide_json.get(key) is not None:
            payload[key] = decide_json[key]

    # -- Step 8c: the placement runlog row --------------------------------
    shaped = cli.shape_placements(
        {
            "now": payload.get("now"),
            "control": payload.get("control"),
            "cycle_id": cycle_id,
            "placements": extras.get("placements") or [],
            "rejections": extras.get("placement_rejections") or [],
            "alerts": extras.get("placement_alerts") or [],
        },
        loaded,
    )
    if shaped.get("error"):
        raise CycleError(
            f"shape-placements refused the runner's rows: {shaped['error']}"
        )

    supabase.rpc(
        "edge_runlog_write",
        {
            "p_cycle_id": cycle_id,
            "p_cycle_type": cycle_type,
            "p_trading_day": trading_day,
            "p_command": "place",
            "p_stdout": None,
            "p_placements": shaped.get("placements") or [],
            "p_rejections": shaped.get("rejections") or [],
        },
    )

    # -- Step 9: report ----------------------------------------------------
    if cycle_type == cadence.POSTCLOSE:
        payload["fill_quality_week"] = supabase.rpc(
            "edge_fill_quality_recent", {"p_days": 7}
        )
        # The shadow ledger's closed trades for the
        # week; `report._shadow_lines` turns them into one line per rule
        # variant on the scorecard. Read here rather than computed anywhere in
        # this phase — the replay is `services/core/core/shadow.py`'s job and
        # runs on its own schedule.
        payload["shadow_week"] = supabase.rpc("edge_shadow_recent", {"p_days": 7})
        # The week's closes, for the scorecard's data-quality line.
        payload["realized_trades_week"] = supabase.rpc(
            "edge_realized_trades_recent", {"p_days": 7}
        )

    # Computed once,
    # here, rather than after `cli.report()` returns: `_session_status` and
    # `report._cycle_status` used to derive the outcome word independently,
    # from different precedence rules over the same findings, and could (and
    # did — a bare `PHANTOM_ORDER` with no high-priority blocked row) disagree:
    # `edge_session_stats.status = 'blocked'` while the pushed alert read
    # "succeeded with warnings". `report.run` now takes this word as
    # `ctx["session_status"]` and uses it verbatim for the alert title/
    # priority instead of re-deriving one — the same value lands in both
    # places because it is the same computation, not two aligned ones.
# `report._cycle_status` stays as the fallback for a caller that does not
# supply this — the word is now derived server-side in `run.py` end to end.
    session_status = _session_status(cycle_type, reconcile_json, decide_json, shaped)
    # Named in the status alert's body. Four denied exits and a stale snapshot
    # produced "succeeded with warnings" over a body reading "Nothing to
    # report." — the title was right and nothing said why.
    payload["denied_exits"] = _denied_exits(decide_json)
    payload["placed_orders"] = shaped.get("placements") or []
    payload["stale_data_sources"] = _stale_data_sources(decide_json)

    # P3: the deployment record, completed with what was actually placed, and
    # the one line of it the status alert carries. A failed write is named in
    # that alert and downgrades a clean cycle to a warning; it never fails the
    # cycle — the plan is already placed.
    payload["dead_plays"] = decide_json.get("dead_plays") or []
    deployment_error: str | None = None
    if decide_json.get("deployment"):
        payload["deployment"] = deployments.with_placements(
            decide_json["deployment"],
            shaped.get("placements") or [],
            shaped.get("rejections") or [],
        )
        deployment_error = _write_deployment(supabase, payload["deployment"])
        if deployment_error:
            payload["deployment_write_error"] = deployment_error

    # The cycle's own outcome is untouched — a clean cycle is downgraded to
    # `success_with_warnings` (never `failed`: the plan is already placed) and
    # one `Step log gap` alert names what is missing. An unreadable log is
    # "unknown", never a gap acted on (`log_gaps` returns None).
    gap_alerts: list[dict] = []
    if cycle_type != cadence.NOOP:
        missing = steps.log_gaps(
            supabase,
            cycle_id,
            cycle_type,
            reconcile_required=bool(cycle.get("reconcile_required")),
        )
        if missing:
            gap_alerts.append(steps.gap_alert(cycle_id, missing))
            if session_status == SESSION_SUCCESS:
                session_status = SESSION_SUCCESS_WITH_WARNINGS
        # Fundamentals fetched outside `resolve`'s list and the batch rule's
        # batch-rule offences, counted into the status alert's body. Warn-only
        # — the status word is not changed by these.
        warnings = steps.fetch_warnings(supabase, cycle_id)
        if warnings:
            payload["runner_fetch"] = warnings
    if deployment_error and session_status == SESSION_SUCCESS:
        session_status = SESSION_SUCCESS_WITH_WARNINGS

    # S0: the decision snapshot again, read back off the decide runlog row,
    # each approved buy now marked with whether the broker took it. A failed
    # write is one line in the status alert and a warning, never a failed
    # cycle.
    snapshot_error = _write_snapshot(
        supabase,
        snapshots.with_placements(
            decide_json.get("decision_snapshot") or [], shaped.get("placements") or []
        ),
    )
    if snapshot_error:
        payload["snapshot_write_error"] = snapshot_error
        if session_status == SESSION_SUCCESS:
            session_status = SESSION_SUCCESS_WITH_WARNINGS
    payload["session_status"] = session_status

    report_json = cli.report(payload, loaded)

    if cycle_type == cadence.POSTCLOSE and report_json.get("benchmark") is not None:
        supabase.rpc("edge_benchmark_upsert", {"p_row": report_json["benchmark"]})

    alerts = (
        list(report_json.get("alerts") or [])
        + list(shaped.get("alerts") or [])
        + gap_alerts
    )
    if alerts:
        supabase.rpc("edge_alerts_enqueue", {"p_alerts": alerts})

    # Runner alert rows that were not enqueueable (see `cli.shape_placements`)
    # stay queryable in the permanent runlog instead of being lost.
    if shaped.get("runner_notes"):
        report_json = {**report_json, "runner_notes": shaped["runner_notes"]}

    supabase.rpc(
        "edge_runlog_write",
        {
            "p_cycle_id": cycle_id,
            "p_cycle_type": cycle_type,
            "p_trading_day": trading_day,
            "p_command": "report",
            "p_stdout": report_json,
        },
    )

    # -- Step 10: merge control, in the prompt's order --------------------
    # candidates, reconcile, decide (all three carried in `control_updates`
    # from phase 1, merged in that order there) then report, applied once.
    control_updates.update(report_json.get("control_updates") or {})
    supabase.rpc("edge_control_apply", {"p_updates": control_updates})

    supabase.rpc(
        "edge_session_status_apply",
        {"p_cycle_id": cycle_id, "p_status": session_status},
    )

    return {
        "status": STATUS_FINALIZED,
        "alerts_summary": {
            "count": len(alerts),
            "titles": [alert.get("title") for alert in alerts],
            "placed": len(shaped.get("placements") or []),
            "rejected": len(shaped.get("rejections") or []),
        },
    }


def _session_status(
    cycle_type: str,
    reconcile_json: Mapping[str, Any],
    decide_json: Mapping[str, Any],
    shaped: Mapping[str, Any],
) -> str:
    """The business-outcome word, worst-to-best.

    A false "Completed Successfully" (three of three sells rejected) is the
    finding this exists to close: the session's own narration is not
    evidence, this is data every finalize phase already holds. One §2.4
    driver has no signal on the runner path and is deliberately not simulated
    here rather than guessed at — a chunked runlog write cannot happen (the
    core writes its own JSON, once, never through a session's SQL). A
    Twelve-Data-vs-Robinhood fallback is likewise not tracked;
    `_stale_data_sources` covers the other named driver, a stale `data/`
    source, via `decide_json.data_provenance`.
    """
    if cycle_type == cadence.NOOP:
        return SESSION_SKIPPED

    findings = {f.get("code") for f in (reconcile_json.get("findings") or [])}
    if findings & (DIVERGENCE_CLASSES | {RECONCILIATION_DIVERGENCE_PERSISTENT}):
        return SESSION_BLOCKED

    if decide_json.get("blocked"):
        return SESSION_BLOCKED

    # A sell guard refused never reaches the broker, so it is in no rejection
    # row: the plan said "exit" and nothing was placed.
    rejections = shaped.get("rejections") or []
    if rejections or _denied_exits(decide_json):
        return SESSION_PARTIAL_SUCCESS if shaped.get("placements") else SESSION_BLOCKED

    if decide_json.get("manual_required") or decide_json.get("held_for_news"):
        return SESSION_SUCCESS_WITH_WARNINGS

    if _stale_data_sources(decide_json):
        return SESSION_SUCCESS_WITH_WARNINGS

    return SESSION_SUCCESS


# Older than this many
# trading sessions and a `data/` source counts as stale enough to warn on —
# two sessions is a full trading day's slack past the half-hourly cadence
# both fetchers run on, so a single missed CI run never trips it, but a
# genuinely stuck pipeline (the kind `edge_data_files` exists to survive a
# blocked Vercel build for) does.
STALE_DATA_TRADING_DAYS = 2
# `snapshot` is the weekly constituent file (`universe.yml`, Saturday) and
# §5.1 tolerates 14 days; judged by the half-hourly fetchers' two-session limit
# it read stale on Wednesday through Friday of every week, so the cycle status
# said "warnings" for no reason and the warning stopped meaning anything.
# 14 calendar days is 10 sessions.
STALE_DATA_TRADING_DAYS_BY_KEY = {"snapshot": 10}


def _stale_data_sources(decide_json: Mapping[str, Any]) -> list[str]:
    provenance = decide_json.get("data_provenance") or {}
    return sorted(
        key
        for key, row in provenance.items()
        if isinstance(row, Mapping)
        and (row.get("age_trading_days") or 0)
        > STALE_DATA_TRADING_DAYS_BY_KEY.get(key, STALE_DATA_TRADING_DAYS)
    )


def _denied_exits(decide_json: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Sells `guard` refused, as `{"symbol", "quantity", "code", "message"}`."""
    denied = []
    for row in (decide_json.get("guard") or {}).get("denied") or []:
        order = row.get("order") or {}
        if order.get("side") != "sell":
            continue
        first = (row.get("denials") or [{}])[0]
        denied.append(
            {
                "symbol": order.get("symbol"),
                "quantity": order.get("quantity"),
                "code": first.get("code"),
                "message": first.get("message"),
            }
        )
    return denied


def _data_provenance(
    extras: Mapping[str, Any], trading_day: str, loaded: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    """`{key: {"source", "as_of", "age_trading_days"}}` for each of
    `CHECKOUT_DATA`'s three keys, `_payload`'s raw `{"source", "as_of"}` plus
    the age `_session_status` reads. `age_trading_days` is sessions strictly
    between `as_of` and this cycle's trading day (`cadence.sessions_between`,
    same-day is 0). A row with no `as_of` — the runner posted the key itself,
    which `_payload` never records provenance for — has no computable age and
    is left out rather than guessed at.
    """
    day = date.fromisoformat(trading_day)
    calendar = loaded["calendar"]
    provenance: dict[str, dict[str, Any]] = {}
    for key, entry in (extras.get("data_provenance") or {}).items():
        row = dict(entry)
        as_of = row.get("as_of")
        if as_of:
            try:
                row["age_trading_days"] = cadence.sessions_between(
                    date.fromisoformat(str(as_of)[:10]), day, calendar
                )
            except ValueError:
                pass
        provenance[key] = row
    return provenance


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


# The account-scope keys every reconciling cycle's fetch list names. Absence
# of any one of them means the fetch did not happen, which is not the same
# fact as an empty account — and `reconcile.py` cannot tell them apart,
# because a book it cannot see looks exactly like a book that was sold.
#
# **This is the one check the old prompt got for free and this design does
# not.** A live session that could not fetch stopped at §14's "a tool call
# fails twice -> Cycle failed" and never reached `reconcile`. The core is
# handed rows and has no idea what was attempted. `guard.halts`'
# ACCOUNT_UNREADABLE is not the backstop either: guard runs inside `decide`,
# and a `postclose` cycle has no decide at all — which is precisely the cycle
# type adopted first.
#
# Presence, not truthiness: `[]` is a real answer from a flat account.
BROKER_VIEW_KEYS = ("positions", "orders", "account")


def _require_broker_view(payload: Mapping[str, Any]) -> None:
    missing = [key for key in BROKER_VIEW_KEYS if key not in payload]
    if missing:
        raise CycleError(
            "refusing to reconcile without the broker view: no "
            f"{', '.join(missing)} row(s) posted. An unfetched book is "
            "indistinguishable from a liquidated one."
        )


def _payload(
    cycle_id: str, supabase: Supabase, loaded: Mapping[str, Any], phase: str = DECIDE
) -> tuple[dict, dict]:
    """`phase` narrows what gets fetched beyond the `edge_cycle_input` rows
    themselves. `finalize` is the one caller that does not need the full set:
    `_finalize_phase` builds `cli.report()`'s ctx, and `report()` reads
    `account`/`control`/`positions`/`quotes`/`cycle` straight off the
    runner-posted rows and `loaded` (no RPC involved), plus exactly one RPC
    answer — `play_state`/`prior_play_state`, both `edge_play_state_json`.
    It reads none of `intent`/`prior_book`/`rejections`/`symbol_lock`/
    `dip_recovery_positions`/`day_context`/`earnings_reactions`/`loss_closes`
    (confirmed by reading `cli.report`'s ctx dict end to end) and none of
    `CHECKOUT_DATA` (`snapshot`/`events`/`macro`) — `decide_json.data_provenance`,
    which is the only consumer of that, was already computed once in
    `_decide_phase` and is read back from the `decide` runlog row, never
    recomputed here. Fetching all of it anyway on every `finalize` call was
    six wasted RPCs and three wasted `edge_data_files` selects, every cycle.
    """
    rows = supabase.select(
        "edge_cycle_input",
        f"cycle_id=eq.{cycle_id}&select=kind,symbol,payload&order=id.asc",
    )
    payload, extras = assemble(rows)
    # What the runner itself posted, before `_fill_from_day_context` mixes
    # cached values in. `_write_day_context` writes back only these — a value
    # read out of the cache must never be written back as if fetched now.
    extras["posted"] = _posted(payload)

    posted = payload.get("cycle_id")
    if posted is not None and posted != cycle_id:
        raise CycleError(
            f"edge_cycle_input rows claim cycle_id {posted!r}, "
            f"called for {cycle_id!r}"
        )
    payload["cycle_id"] = cycle_id

    # `edge_data_files` first, the
    # bundled `data/` file second: `vercel.json`'s `ignoreCommand` now skips a
    # production build for a data-only commit, so the
    # bundle can no longer be trusted to be fresh even when a build *does*
    # land — `edge_data_files` is written on every CI run regardless of
    # whether Vercel deploys. `data_provenance` records which source answered
    # each key and how old it was, for `_decide_phase` to turn into an age and
    # write into the decide runlog.
    data_provenance: dict[str, dict[str, Any]] = {}
    # `finalize` reads `data_provenance` back from the `decide` runlog row
    # (`_finalize_phase`'s `decide_json`), never from a fresh computation —
    # see this function's docstring. Skip the fetch, not just the write.
    if phase != FINALIZE:
        for key in CHECKOUT_DATA:
            # Presence, not truthiness: a runner that posted `{}` said "no events",
            # which is a real answer and must not be overwritten by either source.
            if key in payload:
                continue

            document, provenance = checkout_document(supabase, key)
            if document is None:
                continue
            payload[key] = document
            data_provenance[key] = provenance

    extras["data_provenance"] = data_provenance

    if not payload.get("now"):
        raise CycleError("no `now` row: the runner must post the wall clock it read")
    if not payload.get("control"):
        # §14 row 1. Something that cannot read its own control state must not
        # act, and an empty control here is indistinguishable from unreadable.
        raise CycleError("no `control` row: edge_control_json() was never posted")

    # The runner posts `get_accounts` under `balances`, which is an
    # `EXTRA_KINDS` row rather than an Appendix D key, so it arrives in
    # `extras` and `cli.shape_account` cannot see it. Named here — where the
    # two halves are both in hand — and nowhere else; Appendix D still has one
    # `account` key and this does not add a second.
    #
    # **`extras["balances"]` is `{"data": {"accounts": [...]}}`, not the list
    # itself.** It is the row's raw posted payload — `get_accounts`'s own
    # response shape, echoed verbatim per the runner prompt's "post the
    # response as it came" rule — and `list()` of a dict iterates its *keys*.
    # Every runner postclose cycle since 9323a98 crashed here:
    # `list({"accounts": [...]})` produced `["accounts"]`, one string, which
    # `_settled_cash_value` then called `.get("account_number")` on and died
    # with exactly the `AttributeError: 'str' object has no attribute 'get'`
    # four consecutive postclose sessions reported — never caught because
    # nothing had read `edge_cycle_output.status` yet. An earlier fix stopped
    # there and missed that `get_accounts` also wraps its object in the tool's
    # own `data` envelope (`broker_shapes._unwrap_account` strips the same
    # envelope off `account`) — so every cycle since kept silently reading
    # `payload["accounts"]` back as `[]` instead of crashing, until an earlier
    # postclose runlog was read by hand.
    balances = extras.get("balances") or {}
    if isinstance(balances.get("data"), Mapping):
        balances = balances["data"]
    if extras.get("balances") is not None and "accounts" not in payload:
        payload["accounts"] = list(balances.get("accounts") or [])

    # After the two guards, because normalising derives the cycle (to shape the
    # account) and `cadence.decide` needs both of them.
    #
    # One call, every key. This used to be `_shape_account` here plus four
    # renamers inside `cli`, which is how the hosted path and the live path
    # ended up with different vocabularies for `orders` — `src/broker_shapes.py`
    # carries the whole story. The live path reaches the same function through
    # `cli.reconcile`/`decide`/`report`, and every normaliser is idempotent
    # precisely so this second pass is free.
    payload = cli.normalized(payload, loaded)
    _supabase_reads(payload, supabase, loaded, phase)
    _fill_from_day_context(payload, loaded)
    return payload, extras


def _fill_from_day_context(payload: dict, loaded: Mapping[str, Any]) -> None:
    """The runner's half of Step 6's "copy `fresh` symbols from `day_context`".

    A live session copies each fresh symbol's cached `signals`/`earnings` into
    `cycle.json` by hand. The runner posts neither for a symbol it did not
    fetch — it fetches no indicators at all, and skips `earnings` for any
    symbol `resolve` did not call stale (runner-prompt.md: "the core reads its
    cached value itself").

    * **`signals`** — every cached symbol whose entry is inside
      `DAY_CONTEXT_MAX_AGE`, merged *under* anything posted (a posted value
      wins, same rule as every other server-side fill here). The pre-market
      job's daily RSI(14)/ATR(14)/SMAs are what lands, as of the prior close.
    * **`earnings`** — only for a symbol that is fully `fresh`
      (`cli._symbol_fresh`: RSI *and* earnings cached this window) and has no
      posted row. A pre-market-only entry has no earnings, so it is never
      filled from here and the runner still fetches it.

    Held positions need nothing special: they are in `day_context` because the
    pre-market job writes every held name.
    """
    cache = payload.get("day_context") or {}
    if not isinstance(cache, Mapping) or not cache or not payload.get("now"):
        return
    cycle = cli._cycle(payload, loaded)

    signals = dict(payload.get("signals") or {})
    earnings = dict(payload.get("earnings") or {})
    for symbol, entry in cache.items():
        if not isinstance(entry, Mapping) or not cli._age_fresh(cycle, entry.get("fetched_at")):
            continue
        cached = entry.get("signals")
        if isinstance(cached, Mapping) and cached:
            signals[symbol] = {**cached, **(signals.get(symbol) or {})}
        if symbol not in earnings and cli._symbol_fresh(cycle, entry):
            earnings[symbol] = entry["earnings"]

    if signals:
        payload["signals"] = signals
    if earnings:
        payload["earnings"] = earnings


# Every `cycle.json` key that comes out of Supabase, and the RPC that answers
# it. `(key, function, params, unwrap)` — `unwrap` picks a field out of a
# composite answer, `None` takes the whole thing.
#
# **Why these moved server-side.** They were fetch-plan rows with `source:
# "supabase"`, which meant the runner read them and posted them back to the
# core — a round trip through a session for data that never leaves the
# database the core is already connected to. That is not merely wasteful:
# `edge_should_run` publishes the fetch list, and the published list is what a
# session executes, so a wrong RPC name in it is a cycle that dies (and did,
# twice). With the reads here there is no supabase row left to get wrong, and
# `schedule.py` refuses to publish one at all.
#
# `run.py` already did exactly this for `edge_regime` and for the three
# checked-in `data/` files, for the same reason and with the same rule: a key
# the runner *did* post wins, because session recovery is the one case where a
# value legitimately arrives from outside.
_SUPABASE_READS: tuple[tuple[str, str, str | None], ...] = (
    # `edge_prior`'s whole result is `intent` — the function returns
    # `placements` and the core reads `intent.placements`. Its other two
    # fields are separate Appendix D keys and are split out below.
    ("intent", "edge_prior", None),
    ("prior_book", "edge_prior", "prior_book"),
    ("rejections", "edge_prior", "rejections"),
    ("play_state", "edge_play_state_json", None),
    ("prior_play_state", "edge_play_state_json", None),
    ("symbol_lock", "edge_symbol_lock_json", None),
    ("dip_recovery_positions", "edge_dip_recovery_positions_json", None),
    ("day_context", "edge_day_context_recent", None),
)

# The two that stay behind `entries_allowed`, exactly as `cli._fetch_plan`
# gates them. They are cheap here — two more RPCs in a function already making
# a dozen — but "cheap" is not a reason to change what the core decides from.
# An exit-only cycle that suddenly had `earnings_reactions` in hand would let
# `_pead` see a window the live path never showed it, which is a strategy
# change wearing a plumbing change's clothes.
_ENTRY_SUPABASE_READS: tuple[tuple[str, str, str | None], ...] = (
    ("earnings_reactions", "edge_earnings_reactions_recent", None),
)

# §9's ledger, on every cycle that buys *or* reconciles — `cli._fetch_plan`'s
# own rule. `reconcile._anchored_entry` reads it to tell a wash-sale-adjusted
# tax lot from a genuinely disputed one; a post-close cycle without it would
# re-dispute a position the full cycle had just cleared.
_LOSS_CLOSES_READ: tuple[str, str, str | None] = (
    "loss_closes", "edge_loss_closes_recent", None,
)

# The RPCs that need the trading day. The rest take no arguments.
_DAY_SCOPED = {"edge_prior": "p_trading_day", "edge_day_context_recent": "p_trading_day"}

# `finalize`'s whole RPC need, per `_payload`'s docstring: `play_state`/
# `prior_play_state`, one function, both keys `cli.report()` actually reads.
_FINALIZE_SUPABASE_READS: tuple[tuple[str, str, str | None], ...] = (
    ("play_state", "edge_play_state_json", None),
    ("prior_play_state", "edge_play_state_json", None),
)


def _supabase_reads(
    payload: dict, supabase: Supabase, loaded: Mapping[str, Any], phase: str = DECIDE
) -> None:
    """Fill the `cycle.json` keys that come from this database, in place.

    Called after `cli.normalized` so the cycle can be derived (it needs a
    shaped `control` and `now`), and before anything reads a key.

    **Presence, not truthiness.** A runner that posted `{}` for `symbol_lock`
    said "nothing is locked", which is a real answer; overwriting it here
    would silently discard a session-recovery payload. Same rule the
    `regime_inputs` and `CHECKOUT_DATA` blocks already follow.

    Each RPC is called once even when two keys read from it — `play_state` and
    `prior_play_state` are the same snapshot under two names (the live prompt
    posts it twice for the same reason), and `edge_prior` answers three keys.
    """
    cycle = cli.cycle_of(payload, loaded)
    if phase == FINALIZE:
        reads = list(_FINALIZE_SUPABASE_READS)
    else:
        reads = list(_SUPABASE_READS)
        if cycle.entries_allowed:
            reads.extend(_ENTRY_SUPABASE_READS)
        if cycle.entries_allowed or cycle.reconcile_required:
            reads.append(_LOSS_CLOSES_READ)

    answers: dict[str, Any] = {}
    for key, function, unwrap in reads:
        if key in payload:
            continue
        if function not in answers:
            param = _DAY_SCOPED.get(function)
            answers[function] = supabase.rpc(
                function, {param: cycle.trading_day} if param else {}
            )
        answer = answers[function]
        payload[key] = answer if unwrap is None else (answer or {}).get(unwrap)


def _runlog(supabase: Supabase, cycle_id: str) -> dict:
    """This cycle's `decide` and `reconcile` stdout, latest row per command."""
    rows = supabase.select(
        "edge_runlog",
        f"cycle_id=eq.{cycle_id}&select=command,stdout&order=id.asc",
    )
    latest: dict[str, Any] = {}
    for row in rows:
        command = row.get("command")
        if command in ("decide", "reconcile") and row.get("stdout") is not None:
            latest[command] = row["stdout"]
    return latest


def _prior_output(supabase: Supabase, cycle_id: str) -> dict:
    rows = supabase.select(
        "edge_cycle_output", f"cycle_id=eq.{cycle_id}&select=control_updates,status"
    )
    return rows[0] if rows else {}


def _store(supabase: Supabase, cycle_id: str, **fields: Any) -> None:
    supabase.rpc("edge_cycle_output_write", {"p_cycle_id": cycle_id, "p_fields": fields})


# ---------------------------------------------------------------------------
# Small shapers
# ---------------------------------------------------------------------------


class Scope:
    """What `_apply_scope_ceiling` did: for the alert, the runlog and the step log."""

    def __init__(self, before: int, after: int, ceiling: int, trimmed: list[dict], dip: int, events: int):
        self.before, self.after, self.ceiling = before, after, ceiling
        self.trimmed = trimmed
        self.trimmed_dip, self.trimmed_events = dip, events
        # The mandatory set (held + sector ETFs) alone is past the ceiling: nothing
        # left to trim, and the runner will be asked for all of it.
        self.mandatory_over = after > ceiling


def _trigger_symbol(row: Any) -> str | None:
    return row.get("symbol") if isinstance(row, Mapping) else None


def _apply_scope_ceiling(payload: dict, loaded: Mapping[str, Any]) -> Scope:
    """Keep `_symbols(payload)` within `config.resolve_max_symbols` by trimming
    **discovery names only**, and never the same way twice.

    Never trimmed (Invariant 7 and the regime's needs): a held position, and a
    sector ETF in `macro_targets`. A trigger on a held name keeps its row too —
    dropping it would not shrink the list, and the position is fetched anyway.

    Trim order, deterministic: names whose only trigger is the DIP scan first,
    from the tail of the scan order (the scanner's list is the unbounded one,
    and it is the broad, catalyst-less one); then event-driven names from the
    tail of the recency ranking (oldest filing first). All of a name's trigger
    rows go together, so a symbol is either fetched or absent, never half-kept.

    If the mandatory set alone is over the ceiling nothing is trimmed and
    `Scope.mandatory_over` says so.
    """
    ceiling = cli.resolve_max_symbols(loaded)
    triggers = [t for t in (payload.get("triggers") or []) if _trigger_symbol(t)]
    before = len(_symbols(payload))
    if before <= ceiling:
        return Scope(before, before, ceiling, [], 0, 0)

    held = {p["symbol"] for p in payload.get("positions") or [] if isinstance(p, Mapping) and p.get("symbol")}
    macro = {
        (row.get("symbol") if isinstance(row, Mapping) else row)
        for row in payload.get("macro_targets") or []
    }
    # The core sleeve's ETF is fetched every cycle the sleeve is on; trimming
    # its row would stall the sleeve on a busy day.
    core = {row["symbol"] for row in triggers if row.get("play_id") == "CORE"}
    protected = held | macro | core

    plays_by_symbol: dict[str, set[str]] = {}
    for row in triggers:
        plays_by_symbol.setdefault(row["symbol"], set()).add(row.get("play_id"))

    tail_first = list(dict.fromkeys(row["symbol"] for row in reversed(triggers)))
    droppable = [s for s in tail_first if s not in protected]
    dip_only = [s for s in droppable if plays_by_symbol[s] <= {"DIP"}]
    events = [s for s in droppable if s not in set(dip_only)]

    excess = before - ceiling
    to_drop: list[str] = []
    for symbol in dip_only + events:
        if len(to_drop) >= excess:
            break
        to_drop.append(symbol)

    dropped_set = set(to_drop)
    payload["triggers"] = [t for t in payload.get("triggers") or [] if _trigger_symbol(t) not in dropped_set]

    trimmed = [
        {
            "symbol": symbol,
            "reason": f"outside the {ceiling}-symbol resolve ceiling"
            + (", DIP scan only" if plays_by_symbol[symbol] <= {"DIP"} else ", event trigger"),
        }
        for symbol in to_drop
    ]
    return Scope(
        before,
        len(_symbols(payload)),
        ceiling,
        trimmed,
        sum(1 for s in to_drop if plays_by_symbol[s] <= {"DIP"}),
        sum(1 for s in to_drop if not plays_by_symbol[s] <= {"DIP"}),
    )


def _report_scope(supabase: Supabase, cycle_id: str, scope: Scope) -> None:
    """A `warn` in the step log and one alert. **Fail-soft on purpose**: the
    alert title is canonical only where the schema knows it, and a scope
    notification must never be what fails a `resolve` — the runner would place
    nothing over a cosmetic error."""
    steps.stamp(
        supabase,
        cycle_id,
        steps.S2A_RESOLVE,
        steps.WARN,
        {
            "scope": "trimmed" if scope.trimmed else "mandatory_over_ceiling",
            "before": scope.before,
            "after": scope.after,
            "ceiling": scope.ceiling,
            "trimmed": len(scope.trimmed),
        },
    )
    if scope.trimmed:
        body = (
            f"Cycle {cycle_id[:8]}: the resolve step would have asked the runner for "
            f"{scope.before} symbols; the ceiling is {scope.ceiling}, so {len(scope.trimmed)} "
            f"discovery name(s) were trimmed ({scope.trimmed_dip} DIP-scan only, "
            f"{scope.trimmed_events} event-driven) and {scope.after} remain. Held positions and "
            "sector ETFs are never trimmed. Raise resolve_max_symbols or lower dip_survivor_cap "
            "in config/config.json to change this."
        )
    else:
        body = (
            f"Cycle {cycle_id[:8]}: held positions and sector ETFs alone come to {scope.after} "
            f"symbols, past the {scope.ceiling}-symbol ceiling. Nothing was trimmed."
        )
    try:
        supabase.rpc(
            "edge_alerts_enqueue",
            {
                "p_alerts": [
                    {
                        "title": "Resolve scope trimmed",
                        "body": body,
                        "priority": "default",
                        "cycle_id": cycle_id,
                    }
                ]
            },
        )
    except Exception:  # noqa: BLE001 — see the docstring
        steps.STAMP_FAILURES += 1


# ---------------------------------------------------------------------------
# Fundamentals only where read; the core writes `day_context`
# ---------------------------------------------------------------------------

# The daily-stable half of `get_equity_fundamentals` that `day_context` caches,
# and **exactly** these keys (plus its own `fetched_at`). The same three names
# `cli.day_context_write_symbol` (the live session's shaper) has always
# written.
DAY_CONTEXT_STATIC_KEYS = ("average_volume", "sector", "market_cap")


def _fundamentals_needed(
    payload: Mapping[str, Any], loaded: Mapping[str, Any], trading_day: str, symbols: list[str]
) -> list[str]:
    """The resolved symbols a consumer will read live fundamentals for.

    Consumers (`src/` has no other reader of `ctx["fundamentals"]` on the hosted
    `decide` path):

    * `plays/catalyst.py` — `volume` / `average_volume`, and only past the play's
      own event check. That check is `catalyst.triggered`, called here, so the
      list is the play's precondition itself rather than a second opinion about
      which names are "CATALYST". Every candidate is offered to every play
      (`plays.entries`), held names included — a held name with a fresh 8-K can
      re-trigger CATALYST — so held names are **not** excluded.
    * `plays/dip.py` `_adv_usd` — `average_volume`, only under the pullback
      reshape (`play_dip_pullback_enabled`, off by default), and then for any
      candidate. With the flag on this returns every symbol: unnarrowed, as
      before this prompt.
    * `dip_recovery/` reads `volume`/`average_volume` too, but in the dip-monitor
      and universe-pick prompts; the hosted `decide` never reaches it.

    A name missing from this list gets no `fundamentals` row, and both readers
    fail closed on that: CATALYST's multiple is 0.0 (no entry), DIP's classic
    §6.2 reads nothing.
    """
    if plays.pullback_enabled({"config": loaded.get("config") or {}}):
        return list(symbols)
    ctx = {
        "events": payload.get("events") or {},
        "cycle": {"trading_day": trading_day},
        "calendar": loaded.get("calendar") or {},
    }
    return [symbol for symbol in symbols if catalyst.triggered(symbol, ctx)]


def _posted(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """`{"earnings": {symbol: raw}, "fundamentals": {symbol: row}}` as the runner
    posted them, read straight off `assemble`'s output."""
    earnings = payload.get("earnings")
    return {
        "earnings": dict(earnings) if isinstance(earnings, Mapping) else {},
        "fundamentals": broker_shapes.symbol_map(payload.get("fundamentals")),
    }


def _day_context_writes(
    posted: Mapping[str, Mapping[str, Any]],
    *,
    stale: list[str],
    needed: list[str],
    now: str,
) -> dict[str, dict[str, Any]]:
    """* never `signals` (the pre-market job's SMAs/RSI/ATR would be erased);
    * `earnings` and the top-level `fetched_at` only for a symbol in `stale`
      whose earnings were actually posted this cycle — a stamp on anything else
      would falsely mark it fresh;
    * `fundamentals_static` only for a symbol in `needed` whose fundamentals row
      was posted, with exactly `DAY_CONTEXT_STATIC_KEYS` and its own
      `fetched_at`, and never an all-null block (it would read back as fresh).
    """
    writes: dict[str, dict[str, Any]] = {}
    earnings = posted.get("earnings") or {}
    for symbol in stale:
        if earnings.get(symbol) is not None:
            writes.setdefault(symbol, {}).update({"earnings": earnings[symbol], "fetched_at": now})

    fundamentals = posted.get("fundamentals") or {}
    for symbol in needed:
        row = fundamentals.get(symbol)
        if not isinstance(row, Mapping):
            continue
        statics = {key: row.get(key) for key in DAY_CONTEXT_STATIC_KEYS}
        if all(value is None for value in statics.values()):
            continue
        writes.setdefault(symbol, {})["fundamentals_static"] = {**statics, "fetched_at": now}

    # Registry order is irrelevant to the merge; sorted so the write sequence
    # (and every test of it) is deterministic.
    return {symbol: writes[symbol] for symbol in sorted(writes)}


def _write_day_context(
    supabase: Supabase,
    cycle_id: str,
    trading_day: str,
    payload: Mapping[str, Any],
    posted: Mapping[str, Mapping[str, Any]],
    *,
    stale: list[str],
    needed: list[str],
) -> None:
    """Stamped as `S2a.day_context` by the core even when there is nothing to
    write, so a warm cache is not mistaken for a missing step by the finalize
    gate. Fail-soft per symbol: a cache write is never worth a cycle's plan.
    """
    writes = _day_context_writes(posted, stale=stale, needed=needed, now=payload.get("now"))
    failed: list[str] = []
    with steps.stamped(supabase, cycle_id, steps.S2A_DAY_CONTEXT, {"symbols": len(writes)}) as span:
        for symbol, data in writes.items():
            try:
                supabase.rpc(
                    "edge_day_context_write_symbol",
                    {"p_trading_day": trading_day, "p_symbol": symbol, "p_symbol_data": data},
                )
            except Exception:  # noqa: BLE001 — see the docstring
                failed.append(symbol)
        span.detail.update(
            {
                "earnings": sum(1 for d in writes.values() if "earnings" in d),
                "fundamentals_static": sum(1 for d in writes.values() if "fundamentals_static" in d),
                "failed": len(failed),
            }
        )
    if failed:
        steps.stamp(
            supabase, cycle_id, steps.S2A_DAY_CONTEXT, steps.WARN, {"write_failed": failed[:20]}
        )


def _fundamentals_scope(
    supabase: Supabase,
    cycle_id: str,
    posted: Mapping[str, Mapping[str, Any]],
    needed: list[str],
) -> dict[str, Any]:
    """It is still used — the plan is identical either way, which is the whole
    premise of the narrowing. Empty when the runner posted no fundamentals at
    all."""
    fundamentals = posted.get("fundamentals") or {}
    if not fundamentals:
        return {}
    outside = sorted(set(fundamentals) - set(needed))
    if outside:
        steps.stamp(
            supabase,
            cycle_id,
            steps.S2A_POST_FUNDAMENTALS,
            steps.WARN,
            {"outside_need": True, "count": len(outside), "needed": len(needed), "symbols": outside[:20]},
        )
    return {
        "fundamentals_needed": len(needed),
        "fundamentals_posted": len(fundamentals),
        "fundamentals_outside_need": outside,
    }


def _symbols(payload: Mapping[str, Any]) -> list[str]:
    """The names `day-context-plan` splits fresh/stale: everything held, plus
    everything today's triggers and macro targets name. Held positions are
    never dropped (Step 5's first line), so this is a union, not a cap."""
    names: set[str] = set()
    for position in payload.get("positions") or []:
        if isinstance(position, Mapping) and position.get("symbol"):
            names.add(position["symbol"])
    for row in payload.get("triggers") or []:
        if isinstance(row, Mapping) and row.get("symbol"):
            names.add(row["symbol"])
    for row in payload.get("macro_targets") or []:
        if isinstance(row, Mapping) and row.get("symbol"):
            names.add(row["symbol"])
        elif isinstance(row, str):
            names.add(row)
    return sorted(names)
