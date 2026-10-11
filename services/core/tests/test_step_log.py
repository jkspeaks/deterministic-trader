"""The step log's Python half.

The triggers are Postgres's and are proven by
`tests/test_step_log_contract.py`. What this
file pins is everything the core itself owns:

* every phase is bracketed `start` -> `end` (with counts), or `abort` -- and the
  exception still reaches `run()`'s boundary unchanged;
* a stamp that fails changes nothing about a phase's result (R5);
* the finalize gate turns a *missing* step into an alert and a
  `success_with_warnings`, and an unreadable log into nothing at all.
"""

from __future__ import annotations

import pytest

from services.core.core import run, steps
from services.core.tests.conftest import FakeSupabase, rows_from
from src import cli
from tests.factories import cli_payload

CYCLE = "f98bb2d5-34b8-49b1-8967-b2f2c321805c"


# -- stamped(): start / end / abort -------------------------------------------


def test_stamped_writes_start_then_end_with_duration():
    fake = FakeSupabase()
    with steps.stamped(fake, CYCLE, steps.S3_DECIDE) as span:
        span.detail["orders"] = 2
    assert fake.stamped() == [(steps.S3_DECIDE, "start"), (steps.S3_DECIDE, "end")]
    end = fake.stamps[-1]
    assert end[3]["orders"] == 2
    assert end[3]["duration_ms"] >= 0
    assert end[4] == "core"


def test_stamped_writes_abort_and_reraises():
    fake = FakeSupabase()
    with pytest.raises(ValueError, match="boom"):
        with steps.stamped(fake, CYCLE, steps.S2A_RESOLVE):
            raise ValueError("boom")
    assert fake.stamped() == [(steps.S2A_RESOLVE, "start"), (steps.S2A_RESOLVE, "abort")]
    assert "ValueError: boom" in fake.stamps[-1][3]["error"]


class _ExplodingStamp(FakeSupabase):
    def stamp(self, *args, **kwargs):
        raise RuntimeError("log is down")


def test_a_failing_stamp_never_changes_the_phase(monkeypatch):
    """R5. Telemetry is not load-bearing: the body runs, its exception (if any)
    is the body's own, and the failure is counted rather than raised."""
    before = steps.STAMP_FAILURES
    fake = _ExplodingStamp()
    with steps.stamped(fake, CYCLE, steps.S3_DECIDE):
        result = 42
    assert result == 42
    assert steps.STAMP_FAILURES > before

    with pytest.raises(KeyError):
        with steps.stamped(fake, CYCLE, steps.S3_DECIDE):
            raise KeyError("the body's own")


def test_a_supabase_without_stamp_is_a_noop():
    class Bare:
        pass

    assert steps.stamp(Bare(), CYCLE, steps.S3_DECIDE, "start") is False


# -- run(): every phase is stamped ---------------------------------------------


@pytest.mark.parametrize("phase", run.PHASES)
def test_run_brackets_every_phase(phase, monkeypatch):
    fake = FakeSupabase()
    results = {
        run.RESOLVE: {"status": "resolved", "symbols": ["A", "B", "C"], "stale": ["A"], "statics_stale": []},
        run.DECIDE: {"status": "decided", "plan": [{}], "protection": [], "cancels": []},
        run.FINALIZE: {"status": "finalized", "alerts_summary": {"count": 3}},
    }
    monkeypatch.setattr(run, "_resolve_phase", lambda *a, **k: results[run.RESOLVE])
    monkeypatch.setattr(run, "_decide_phase", lambda *a, **k: results[run.DECIDE])
    monkeypatch.setattr(run, "_finalize_phase", lambda *a, **k: results[run.FINALIZE])

    out = run.run(CYCLE, phase, fake)

    step = steps.PHASE_STEPS[phase]
    assert out["status"] == results[phase]["status"]
    assert fake.stamped(step) == [(step, "start"), (step, "end")]
    end = fake.stamps[-1][3]
    if phase == run.RESOLVE:
        # The numbers that would have exposed 149-vs-21 the moment it happened.
        assert (end["symbols"], end["stale"], end["statics_stale"]) == (3, 1, 0)
    if phase == run.DECIDE:
        assert end["orders"] == 1


@pytest.mark.parametrize("phase", run.PHASES)
def test_run_stamps_abort_and_still_stores_the_failure(phase, monkeypatch):
    """The boundary's contract is unchanged: the exception is stored as
    `status = 'failed'` and no plan is returned. The wrapper only observes."""
    fake = FakeSupabase()

    def boom(*args, **kwargs):
        raise RuntimeError("no plan")

    monkeypatch.setattr(run, "_resolve_phase", boom)
    monkeypatch.setattr(run, "_decide_phase", boom)
    monkeypatch.setattr(run, "_finalize_phase", boom)

    out = run.run(CYCLE, phase, fake)

    step = steps.PHASE_STEPS[phase]
    assert out["status"] == "failed" and "no plan" in out["detail"]
    assert fake.stamped(step) == [(step, "start"), (step, "abort")]
    assert fake.first("edge_cycle_output_write")["p_fields"]["status"] == "failed"


def test_run_still_refuses_an_unknown_phase_without_stamping():
    fake = FakeSupabase()
    with pytest.raises(run.CycleError):
        run.run(CYCLE, "sideways", fake)
    assert fake.stamps == []


# -- the finalize gate ----------------------------------------------------------


def _rows(*names, resolved=0):
    rows = [{"step": name, "event": "beat", "detail": None} for name in names]
    if resolved:
        rows.append({"step": steps.S2A_RESOLVE, "event": "end", "detail": {"symbols": resolved}})
    return rows


def test_a_complete_log_has_no_gaps():
    log = _rows(*steps.expected_steps("full", reconcile_required=True, resolved_symbols=5), resolved=5)
    fake = FakeSupabase(tables={"edge_cycle_step": log})
    assert steps.log_gaps(fake, CYCLE, "full", reconcile_required=True) == []


def test_missing_steps_are_named_in_registry_order():
    fake = FakeSupabase(tables={"edge_cycle_step": _rows(steps.S1_SESSION_START, steps.S1_CLAIM)})
    assert steps.log_gaps(fake, CYCLE, "full", reconcile_required=True) == [
        steps.S2_FETCH, steps.S2_POST_BATCH, steps.S3_DECIDE, steps.CORE_RECONCILE,
        steps.CORE_DECIDE,
    ]


def test_candidate_steps_are_expected_only_once_resolve_reported_symbols():
    base = _rows(steps.S1_SESSION_START, steps.S1_CLAIM, steps.S2_FETCH, steps.S2_POST_BATCH,
                 steps.S3_DECIDE, steps.CORE_RECONCILE, steps.CORE_DECIDE)
    quiet = FakeSupabase(tables={"edge_cycle_step": base})
    assert steps.log_gaps(quiet, CYCLE, "full") == []

    resolved = FakeSupabase(tables={"edge_cycle_step": base + _rows(resolved=40)})
    assert steps.log_gaps(resolved, CYCLE, "full") == [
        steps.S2A_FETCH_QUOTES, steps.S2A_FETCH_TRADABILITY,
        steps.S2A_POST_QUOTES, steps.S2A_POST_TRADABILITY, steps.S2A_DAY_CONTEXT,
    ]


# -- the runner's own beats -----------------------------------------


def test_a_runner_that_skipped_its_beats_is_a_gap_and_a_warning_never_a_failure():
    """The beats are optional for the runner and visible to the operator: a
    cycle without them finalizes normally and says which were missing."""
    log = [
        row for row in _rows(
            *steps.expected_steps("full", reconcile_required=True, resolved_symbols=5),
            resolved=5,
        )
        if row["step"] not in steps.RUNNER_BEATS_ALWAYS + steps.RUNNER_BEATS_RESOLVED
    ]
    fake = FakeSupabase(tables={"edge_cycle_step": log})

    missing = steps.log_gaps(fake, CYCLE, "full", reconcile_required=True)

    assert missing == [steps.S2_FETCH, steps.S2A_FETCH_QUOTES, steps.S2A_FETCH_TRADABILITY]
    assert steps.gap_alert(CYCLE, missing)["title"] == "Step log gap"


def test_the_beats_a_cycle_may_legitimately_skip_are_never_expected():
    """No stale names means no earnings fetch; an empty plan means no placing."""
    wanted = steps.expected_steps("full", resolved_symbols=40)

    for optional in (steps.S2A_FETCH_EARNINGS, steps.S2A_FETCH_FUNDAMENTALS, steps.S4_PLACE):
        assert optional not in wanted
        assert steps.step(optional).source == "runner"


def test_every_runner_step_is_written_by_the_runner_and_nothing_else():
    runner = {s.name for s in steps.REGISTRY if s.source == "runner"}

    assert runner == {
        steps.S2_FETCH, steps.S2A_FETCH_QUOTES, steps.S2A_FETCH_TRADABILITY,
        steps.S2A_FETCH_EARNINGS, steps.S2A_FETCH_FUNDAMENTALS, steps.S4_PLACE,
    }
    # No input kind maps to one: a trigger must never stamp a runner step.
    assert all(not steps.step(name).kinds for name in runner)


def test_a_postclose_cycle_does_not_expect_a_decide_row():
    assert steps.CORE_DECIDE not in steps.expected_steps("postclose")
    assert steps.expected_steps("noop") == []


def test_an_unreadable_log_is_unknown_not_a_gap():
    class Broken(FakeSupabase):
        def select(self, table, query):
            raise RuntimeError("PostgREST down")

    assert steps.log_gaps(Broken(), CYCLE, "full") is None


def test_gap_alert_uses_a_canonical_title():
    from src import report

    alert = steps.gap_alert(CYCLE, [steps.S2_POST_BATCH])
    assert alert["title"] == report.STEP_LOG_GAP and alert["title"] in report.CANONICAL_TITLES
    assert alert["body"] and steps.S2_POST_BATCH in alert["body"]
    assert alert["cycle_id"] == CYCLE


# -- the gate, end to end through finalize --------------------------------------


# The cycle id the shared `cli_payload()` fixture claims; `_payload` refuses any other.
FIN_CYCLE = "2026-08-03T13:40:00Z-full"


def _finalize(step_log):
    payload = cli_payload()
    decide_json = cli.decide(payload, cli.load_config())
    tables = {
        "edge_cycle_input": rows_from(payload, FIN_CYCLE) + [
            {"kind": "placements", "symbol": None, "payload": []},
            {"kind": "placement_rejections", "symbol": None, "payload": []},
            {"kind": "placement_alerts", "symbol": None, "payload": []},
        ],
        "edge_runlog": [{"command": "decide", "stdout": decide_json}],
        "edge_cycle_output": [{"cycle_id": FIN_CYCLE, "status": "decided", "control_updates": {}}],
    }
    if step_log is not None:
        tables["edge_cycle_step"] = step_log
    fake = FakeSupabase(tables=tables)
    return run.run(FIN_CYCLE, run.FINALIZE, fake), fake


def _titles(fake):
    return [a["title"] for call in fake.args("edge_alerts_enqueue") for a in call["p_alerts"]]


def test_finalize_raises_a_step_log_gap_and_downgrades_a_clean_cycle():
    result, fake = _finalize(_rows(steps.S1_SESSION_START))  # nearly everything missing

    assert result["status"] == run.STATUS_FINALIZED  # the cycle itself is untouched
    assert "Step log gap" in _titles(fake)
    gap = next(a for c in fake.args("edge_alerts_enqueue") for a in c["p_alerts"] if a["title"] == "Step log gap")
    assert steps.S1_CLAIM in gap["body"]
    applied = fake.first("edge_session_status_apply")
    assert applied["p_status"] == run.SESSION_SUCCESS_WITH_WARNINGS


def test_finalize_is_quiet_when_the_log_is_complete():
    log = _rows(*steps.expected_steps("full", reconcile_required=True), resolved=0)
    result, fake = _finalize(log)

    assert result["status"] == run.STATUS_FINALIZED
    assert "Step log gap" not in _titles(fake)
    assert fake.first("edge_session_status_apply")["p_status"] == run.SESSION_SUCCESS


def test_finalize_ignores_an_unreadable_log():
    """R5: reading the log failed, so nothing is known -- not a gap, not an alert,
    and the finalize still completes."""

    class Broken(FakeSupabase):
        def select(self, table, query):
            if table == "edge_cycle_step":
                raise RuntimeError("PostgREST down")
            return super().select(table, query)

    payload = cli_payload()
    fake = Broken(tables={
        "edge_cycle_input": rows_from(payload, FIN_CYCLE) + [
            {"kind": "placements", "symbol": None, "payload": []},
            {"kind": "placement_rejections", "symbol": None, "payload": []},
            {"kind": "placement_alerts", "symbol": None, "payload": []},
        ],
        "edge_runlog": [{"command": "decide", "stdout": cli.decide(payload, cli.load_config())}],
        "edge_cycle_output": [{"cycle_id": FIN_CYCLE, "status": "decided", "control_updates": {}}],
    })
    result = run.run(FIN_CYCLE, run.FINALIZE, fake)
    assert result["status"] == run.STATUS_FINALIZED
    assert "Step log gap" not in _titles(fake)
