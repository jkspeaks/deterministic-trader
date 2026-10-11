"""The step log has to survive what destroyed the last thing like it: a later
change that `CREATE OR REPLACE`s a function, a `DROP TRIGGER`, a prompt edit, a
new phase nobody remembered to log. This test reads `db/*.sql` **in filename
order** (`db/schema.sql`, then anything added beside it) and asserts the
*final* state, so any of those going wrong turns CI red instead of silently
switching the log off. Nothing here needs a database.

If this file fails, the step log is about to disappear. Fix the change, not the
test.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from services.core.core import run, steps  # noqa: E402
from src import report  # noqa: E402

MIGRATIONS = sorted((REPO / "db").glob("*.sql"))
SCHEMA = REPO / "db" / "schema.sql"

# trigger name -> table. Eight, one per (table, operation-set) the log needs.
REQUIRED_TRIGGERS = {
    "edge_step_input_trg": "edge_cycle_input",
    "edge_step_output_ins": "edge_cycle_output",
    "edge_step_output_upd": "edge_cycle_output",
    "edge_step_day_context_trg": "edge_day_context",
    "edge_step_runlog_trg": "edge_runlog",
    "edge_step_lock_trg": "edge_control",
    "edge_step_stats_ins": "edge_session_stats",
    "edge_step_stats_upd": "edge_session_stats",
}
TRIGGER_FUNCTIONS = (
    "edge_step_on_input",
    "edge_step_on_output",
    "edge_step_on_day_context",
    "edge_step_on_runlog",
    "edge_step_on_lock",
    "edge_step_on_stats",
)

_STEP_LITERAL = re.compile(r"'((?:S\d+a?\.[A-Za-z_.]+)|(?:core\.[a-z]+)|ABORT)'")


def _sql() -> list[tuple[str, str]]:
    return [(p.name, p.read_text(encoding="utf-8")) for p in MIGRATIONS]


def _strip_comments(text: str) -> str:
    return re.sub(r"--[^\n]*", "", text)


def _final_definition(name: str) -> str:
    """The text of the last `create [or replace] function <name>(...) ... $$;`
    across all migrations, in filename order."""
    pattern = re.compile(
        rf"create\s+(?:or\s+replace\s+)?function\s+{re.escape(name)}\s*\(.*?\bas\s+\$\$.*?\$\$\s*;",
        re.IGNORECASE | re.DOTALL,
    )
    found = None
    for _, text in _sql():
        for match in pattern.finditer(_strip_comments(text)):
            found = match.group(0)
    assert found, f"no migration defines {name}()"
    return found


def _final_triggers() -> dict[str, str]:
    """name -> table for every trigger that still exists after the last
    migration (creates and drops replayed in order; `alter table ... disable
    trigger` counts as removal)."""
    state: dict[str, str] = {}
    create = re.compile(r"create\s+(?:or\s+replace\s+)?trigger\s+(\w+)\s+.*?\bon\s+(\w+)", re.I | re.S)
    drop = re.compile(r"drop\s+trigger\s+(?:if\s+exists\s+)?(\w+)", re.I)
    disable = re.compile(r"alter\s+table\s+\w+\s+disable\s+trigger\s+(\w+)", re.I)
    for _, raw in _sql():
        text = _strip_comments(raw)
        events = []
        for m in create.finditer(text):
            events.append((m.start(), "create", m.group(1), m.group(2)))
        for m in drop.finditer(text):
            events.append((m.start(), "drop", m.group(1), None))
        for m in disable.finditer(text):
            events.append((m.start(), "drop", m.group(1), None))
        for _, kind, name, table in sorted(events):
            if kind == "create":
                state[name] = table
            else:
                state.pop(name, None)
    return state


# -- the triggers ---------------------------------------------------------------


def test_every_required_trigger_exists_after_the_last_migration():
    final = _final_triggers()
    missing = {n: t for n, t in REQUIRED_TRIGGERS.items() if final.get(n) != t}
    assert not missing, (
        f"step-log trigger(s) gone or moved after replaying db/*.sql: {missing}. "
        "A migration dropped or disabled one — the step log is off."
    )


@pytest.mark.parametrize("name", TRIGGER_FUNCTIONS)
def test_trigger_functions_swallow_their_own_exceptions(name):
    """R5. A log bug must lose a row, never fail the write to a table a trade
    depends on."""
    definition = _final_definition(name)
    assert re.search(r"exception\s+when\s+others\s+then", definition, re.I), name
    assert "security definer" in definition.lower(), name


def test_the_stamper_and_beat_stay_exception_safe():
    """The stamper and its helper must still be exception-safe after the last
    migration that touches them."""
    for name in ("edge_step_stamp", "edge_step_beat"):
        assert re.search(r"exception\s+when\s+others", _final_definition(name), re.I), name


# -- wall-clock time ------------------------------------------------------------


def test_the_stamper_uses_wall_clock_time_not_the_transaction_start():
    """`now()` is the transaction's start: a 149-row batch shares one value,
    which is exactly why today's timestamps could not show progress."""
    stamp = _strip_comments(_final_definition("edge_step_stamp"))
    assert "clock_timestamp()" in stamp
    assert "now()" not in stamp
    beat = _strip_comments(_final_definition("edge_step_beat"))
    assert "clock_timestamp()" in beat and "now()" not in beat

    log_migration = _strip_comments(SCHEMA.read_text(encoding="utf-8"))
    assert re.search(r"at\s+timestamptz\s+not\s+null\s+default\s+clock_timestamp\(\)", log_migration)


# -- the registry ---------------------------------------------------------------


def test_the_registry_is_well_formed():
    names = [s.name for s in steps.REGISTRY]
    assert len(names) == len(set(names))
    orders = [s.ord for s in steps.REGISTRY]
    assert len(orders) == len(set(orders)) and orders == sorted(orders)
    assert all(s.source in steps.SOURCES for s in steps.REGISTRY)
    kinds = [k for s in steps.REGISTRY for k in s.kinds]
    assert len(kinds) == len(set(kinds)), "one input kind stamps two steps"


def test_the_migration_seed_is_generated_from_the_registry():
    """The *latest* file carrying a seed block is the table's current content.
    A registry change regenerates the whole block with `steps.seed_sql()`."""
    name, text = [(n, t) for n, t in _sql() if steps.SEED_BEGIN in t][-1]
    start, end = text.index(steps.SEED_BEGIN), text.index(steps.SEED_END) + len(steps.SEED_END)
    assert text[start:end] == steps.seed_sql(), (
        f"{name}'s edge_step_kind seed differs from steps.REGISTRY. Regenerate "
        "the block with steps.seed_sql()."
    )


def test_every_step_literal_in_the_sql_is_registered():
    """A trigger stamping a name the registry does not know is a step nothing
    reads, and the gate can never expect."""
    sql = ""
    for name in (*TRIGGER_FUNCTIONS, "edge_step_for_kind", "edge_step_watchdog"):
        sql += _strip_comments(_final_definition(name))
    used = set(_STEP_LITERAL.findall(sql))
    assert used, "found no step literals — the regex is stale"
    assert used <= set(steps.STEP_NAMES), sorted(used - set(steps.STEP_NAMES))


def test_the_input_fallback_is_the_batch_step():
    assert f"'{steps.S2_POST_BATCH}'" in _final_definition("edge_step_for_kind")


def test_every_expected_step_is_registered():
    for cycle_type in ("full", "monitor", "postclose", "overnight", "noop"):
        for reconcile in (True, False):
            for n in (0, 7):
                got = steps.expected_steps(cycle_type, reconcile_required=reconcile, resolved_symbols=n)
                assert set(got) <= set(steps.STEP_NAMES)


# -- the core --------------------------------------------------------------------


def test_every_core_phase_has_a_step():
    assert set(steps.PHASE_STEPS) == set(run.PHASES), (
        "a phase in run.PHASES has no entry in steps.PHASE_STEPS — it would run unlogged"
    )
    assert set(steps.PHASE_STEPS.values()) <= set(steps.STEP_NAMES)


def test_run_calls_every_phase_through_the_stamper():
    """`run.run` must bracket each phase (the full start/end/abort matrix is in
    services/core/tests/test_step_log.py)."""
    src = Path(run.__file__).read_text(encoding="utf-8")
    assert "steps.stamped(" in src and "steps.PHASE_STEPS[phase]" in src


# -- the alerts and the watchdog -------------------------------------------------


def test_the_new_titles_are_canonical_on_both_sides():
    enqueue = _final_definition("edge_alerts_enqueue")
    for title in (report.CYCLE_STALLED, report.CYCLE_OVER_BUDGET, report.STEP_LOG_GAP):
        assert title in report.CANONICAL_TITLES
        assert f"'{title}'" in enqueue, f"edge_alerts_enqueue no longer accepts {title!r}"


def test_every_canonical_title_survives_in_the_final_enqueue_definition():
    """A later `create or replace edge_alerts_enqueue` that forgets a title
    breaks every alert path that uses it. Generated `<Routine> cycle <status>` titles are matched by the regex."""
    enqueue = _final_definition("edge_alerts_enqueue")
    generated = re.compile(
        r"^(Full|Pre-market|Monitor|Post-close|Overnight) cycle "
        r"(succeeded|succeeded with warnings|partial|blocked|failed|skipped)$"
    )
    for title in report.CANONICAL_TITLES:
        if not generated.match(title):
            assert f"'{title}'" in enqueue, title


def test_the_watchdog_is_scheduled_and_never_unscheduled():
    scheduled = False
    for _, raw in _sql():
        text = _strip_comments(raw)
        if re.search(r"cron\.schedule\(\s*'edge-step-watchdog'", text):
            scheduled = True
        if re.search(r"cron\.unschedule\(\s*'edge-step-watchdog'", text):
            scheduled = False
    assert scheduled, "edge-step-watchdog is not scheduled after the last migration"


# -- the runner prompt -----------------------------------------------------------


def test_runner_prompt_names_no_unregistered_step():
    text = (REPO / "docs" / "routine" / "runner-prompt.md").read_text(encoding="utf-8")
    named = set(re.findall(r"\b(S\d+a?\.[a-z_.]+|core\.(?:reconcile|decide|place|report))\b", text))
    assert named <= set(steps.STEP_NAMES), sorted(named - set(steps.STEP_NAMES))


def test_runner_prompt_says_progress_is_recorded_for_it():
    text = (REPO / "docs" / "routine" / "runner-prompt.md").read_text(encoding="utf-8")
    assert "Progress is recorded for you" in text


# -- budget, lock renewal, honest stats --------------------

RUNNER_PROMPT = REPO / "docs" / "routine" / "runner-prompt.md"


def test_the_budget_defaults_are_one_set_of_numbers_in_three_places():
    """`config.json` is the operator's copy, `steps.py` the code's, and the SQL
    function's `case` the fallback when nothing is published."""
    from src import cli

    assert cli.load_config()["config"]["cycle_budget_min"] == dict(steps.CYCLE_BUDGET_MIN)

    sql = _strip_comments(_final_definition("edge_cycle_budget_min"))
    for cycle_type, minutes in steps.CYCLE_BUDGET_MIN.items():
        assert re.search(rf"when '{re.escape(cycle_type)}' then {minutes}\b", sql), cycle_type
    assert re.search(rf"else {steps.DEFAULT_BUDGET_MIN}\b", sql)
    assert "cycle_budget_min" in sql          # the published override is read first


def test_the_prompts_budgets_are_the_shipped_ones():
    assert dict(steps.CYCLE_BUDGET_MIN) == {
        "full": 40, "monitor": 20, "dip-monitor": 15, "postclose": 10, "overnight": 20,
    }


def test_the_watchdog_and_the_await_read_the_same_budget():
    watchdog = _strip_comments(_final_definition("edge_step_watchdog"))
    assert "edge_cycle_budget_min(r.cycle_type)" in watchdog
    assert not re.search(r"when 'full'\s+then 45", watchdog), "the hard-coded budgets are gone"
    assert "'Cycle over budget'" in watchdog and "'Cycle stalled'" in watchdog

    awaited = _strip_comments(_final_definition("edge_cycle_await"))
    assert awaited.count("|| edge_cycle_budget(p_cycle_id)") == 2   # the answer and the timeout
    budget = _strip_comments(_final_definition("edge_cycle_budget"))
    for key in ("elapsed_min", "budget_min", "budget_left_min"):
        assert f"'{key}'" in budget


def test_the_await_still_returns_every_key_the_runner_reads():
    awaited = _strip_comments(_final_definition("edge_cycle_await"))
    for key in ("cycle_id", "status", "symbols", "stale", "statics_stale",
                "fundamentals_needed", "plan", "alerts_summary", "detail"):
        assert f"'{key}'" in awaited, key


def test_edge_cycle_renew_still_returns_a_boolean():
    """`ops/supabase_client.py` calls it and reads a boolean; the budget lives
    in `edge_cycle_budget`, not here."""
    renew = _strip_comments(_final_definition("edge_cycle_renew"))
    assert re.search(r"returns\s+boolean", renew, re.I)
    assert "edge_cycle_budget" not in renew


def test_every_beat_renews_the_lock_without_looping():
    beat = _strip_comments(_final_definition("edge_step_beat"))

    assert "update edge_control" in beat and "key = 'cycle_in_progress'" in beat
    assert "value = p_cycle_id" in beat
    # The lock trigger calls back into this function on that update; both
    # guards have to be there or one input row recurses.
    assert "p_detail ? 'lock_renewed'" in beat
    assert "interval '30 seconds'" in beat
    assert "'lock_renewed', true" in _strip_comments(_final_definition("edge_step_on_lock"))
    # ...and it sits inside the function's own exception-safe block.
    assert beat.index("update edge_control") < beat.index("exception when others")


def test_the_runner_beat_stamps_renews_and_answers_with_the_budget():
    beat = _strip_comments(_final_definition("edge_cycle_beat"))

    assert "source = 'runner'" in beat                    # only a registered runner step
    assert "'start', null, 'runner'" in beat
    assert "update edge_control" in beat
    assert "edge_cycle_budget(p_cycle_id)" in beat
    assert "exception when others" in beat                # never raises


def test_session_finish_keeps_its_signature_and_stops_trusting_the_runner():
    text = SCHEMA.read_text(encoding="utf-8")
    finish = _strip_comments(_final_definition("edge_session_finish"))

    assert re.search(
        r"edge_session_finish\(p_cycle_id text, p_turns integer, p_tool_calls integer, "
        r"p_bytes_in bigint, p_notes text\)\s+returns void",
        _strip_comments(text),
    )
    for column in ("runner_turns", "runner_tool_calls", "runner_bytes_in"):
        assert re.search(rf"^\s+{column} (integer|bigint),?$", text, re.M), column
        assert column in finish
    # The runner's arguments land only in the runner_* columns...
    values = finish[finish.index("values ("):finish.index("on conflict")]
    assert re.sub(r"\s+", " ", values).strip().startswith(
        "values ( p_cycle_id, v_steps, v_events, v_bytes, p_turns, p_tool_calls, p_bytes_in"
    )
    # ...and the original three are counted from what the database saw.
    assert "from edge_cycle_step" in finish and "from edge_cycle_input" in finish
    assert "octet_length(payload::text)" in finish


def test_the_runner_prompt_beats_only_registered_runner_steps():
    text = RUNNER_PROMPT.read_text(encoding="utf-8")
    beaten = set(re.findall(r"edge_cycle_beat\('<cycle_id>', '([^'<]+)'\)", text))

    runner = {s.name for s in steps.REGISTRY if s.source == "runner"}
    assert beaten == runner, (beaten ^ runner)
