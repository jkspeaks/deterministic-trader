"""Reads `db/*.sql` in filename order (the same way
`tests/test_step_log_contract.py` does) and reads `docs/routine/runner-prompt.md`,
so a later migration that retypes `edge_cycle_await` without
`fundamentals_needed`, or a prompt edit that brings back the hand-composed
`day_context` write-back, turns CI red.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from services.core.core import run  # noqa: E402

MIGRATIONS = sorted((REPO / "db").glob("*.sql"))
PROMPT = (REPO / "docs" / "routine" / "runner-prompt.md").read_text(encoding="utf-8")


def _final_definition(name: str) -> str:
    """The last `create [or replace] function <name>(...)` body, any dollar tag."""
    pattern = re.compile(
        rf"create\s+(?:or\s+replace\s+)?function\s+{re.escape(name)}\s*\(.*?\bas\s+\$(\w*)\$.*?\$\1\$\s*;",
        re.IGNORECASE | re.DOTALL,
    )
    found = None
    for path in MIGRATIONS:
        text = re.sub(r"--[^\n]*", "", path.read_text(encoding="utf-8"))
        for match in pattern.finditer(text):
            found = match.group(0)
    assert found, f"no migration defines {name}()"
    return found


def _prompt() -> str:
    return PROMPT


def _step_2a() -> str:
    start = PROMPT.index("## Step 2a")
    return PROMPT[start: PROMPT.index("\n## ", start + 1)]


# -- the database ---------------------------------------------------------------


def test_resolve_carries_fundamentals_needed_to_the_runner():
    assert "fundamentals_needed" in _final_definition("edge_cycle_output_write")
    assert "'fundamentals_needed'" in _final_definition("edge_cycle_await")


def test_the_await_still_carries_everything_it_did():
    await_ = _final_definition("edge_cycle_await")
    for key in ("'symbols'", "'stale'", "'statics_stale'", "'plan'", "'alerts_summary'", "'detail'"):
        assert key in await_, key


def test_the_single_row_write_is_checked_for_market_data_kinds():
    body = _final_definition("edge_cycle_input_write")
    assert "per_row_write" in body and "edge_batch_rule_mode" in body
    for kind in ("quotes", "tradability", "fundamentals", "earnings"):
        assert f"'{kind}'" in body


def test_the_batch_write_keeps_its_order_and_checks_its_size():
    body = _final_definition("edge_cycle_input_write_batch")
    assert "jsonb_to_recordset" in body, "the order-preserving insert is gone"
    assert "oversize_batch" in body and "edge_batch_limit" in body


def test_the_batch_rule_defaults_to_warn():
    mode = _final_definition("edge_batch_rule_mode")
    assert "'warn'" in mode and "'reject'" in mode


# -- the runner prompt ----------------------------------------------------------


def test_the_runner_no_longer_writes_day_context():
    assert "select edge_day_context_write_symbol" not in PROMPT
    assert "Do not write `day_context`" in _step_2a()


def test_fundamentals_are_fetched_only_for_what_resolve_names():
    section = _step_2a()
    assert "resolved.fundamentals_needed" in section
    assert "fetch it for every name in `resolved.symbols`" not in section


def test_the_trimmed_fundamentals_row_is_what_the_core_reads():
    """The five named fields are `plays/catalyst.py`'s two plus the three
    `day_context` caches — nothing the core reads may be trimmed away."""
    match = re.search(r'\{"symbol": "\.\.\.", ([^}]*)\}', _step_2a())
    assert match, "the trimmed fundamentals row is not spelled out"
    fields = set(re.findall(r'"(\w+)"', match.group(0))) - {"symbol"}
    assert fields == {"volume", *run.DAY_CONTEXT_STATIC_KEYS}


def test_scripts_and_scratch_files_are_banned_for_every_step_not_only_2a():
    """The rule moved from Step 2a to the top of the prompt, where
    it binds every step, and Step 2a points back at it."""
    head = _prompt().split("## Step 0", 1)[0]
    assert "No `python3`, no Bash, no scratch files" in head
    assert "You hold data; you do not process it" in _step_2a()


# -- budget, beats, hygiene -----------------------------------------

import re  # noqa: E402

ABSOLUTES = """The four absolutes that bind you:

1. **Place only what `plan.orders`, `plan.protection` and `plan.cancels`
   name** — not one order more, at no price other than the one given.
2. **The exact broker text, verbatim.** Never paraphrase a rejection.
3. **One cycle at a time.** If the lock refuses you, you stop.
4. **When you cannot tell, stop.** Every ambiguity resolves toward placing
   nothing.
"""


def _text() -> str:
    return _prompt().replace("\r\n", "\n")


def _fenced_sql() -> list[str]:
    return re.findall(r"```sql\n(.*?)```", _text(), re.S)


def test_the_four_absolutes_are_untouched():
    assert ABSOLUTES in _text()


def test_a_full_cycle_makes_at_most_twenty_five_database_statements():
    """12d's acceptance: a 40-symbol full cycle's runner call count is about 25,
    down from about 300. What the prompt can promise is its own database
    statements — one `execute_sql` each — on the longest path a cycle can
    take: every Step 2a kind fetched, a plan placed, a clean finalize.
    Robinhood calls are bounded separately, by the batch sizes in Step 2a's
    table and by `resolve_max_symbols`."""
    text = _text()
    path = [
        "select edge_should_run(now())",                         # Step 0
        "select edge_session_start(",                            # Step 1 (one block, with the claim)
        "edge_cycle_beat('<cycle_id>', 'S2.fetch')",             # Step 2
        "select edge_control_json() as control,",                # control + now, one statement
        "select edge_cycle_input_write_batch(",                  # Step 2's batch
        "select edge_cycle_dispatch('<cycle_id>', 'resolve')",
        "select edge_cycle_await('<cycle_id>', 60, 'resolve')",
        "edge_cycle_beat('<cycle_id>', 'S2a.fetch.quotes')",
        "edge_cycle_beat('<cycle_id>', 'S2a.fetch.tradability')",
        "edge_cycle_beat('<cycle_id>', 'S2a.fetch.earnings')",
        "edge_cycle_beat('<cycle_id>', 'S2a.fetch.fundamentals')",
        "select edge_cycle_dispatch('<cycle_id>', 'decide')",
        "select edge_cycle_await('<cycle_id>', 60, 'decide')",
        "edge_cycle_beat('<cycle_id>', 'S4.place')",
        "select edge_cycle_input_write('<cycle_id>', 'placements'",   # Step 5, one block
        "select edge_cycle_dispatch('<cycle_id>', 'finalize')",
        "select edge_cycle_await('<cycle_id>', 60, 'finalize')",
        "select edge_cycle_release('<cycle_id>');",
        "select edge_session_finish(",
    ]
    for statement in path:
        assert statement in text, statement

    kinds_batched = 4          # Step 2a: one batch statement per kind, at most
    total = len(path) + kinds_batched
    assert total == 23 and total <= 25

    # Nothing else in the prompt is a per-symbol or per-row database call.
    assert "one `execute_sql` statement" in text
    assert "never per symbol" in text


def test_the_runner_never_renews_the_lock_itself():
    text = _text()

    assert "edge_cycle_renew" not in text
    assert "You do not renew the lock" in text


def test_the_clock_for_now_comes_from_the_database_not_a_shell():
    text = _text()

    assert "date -u" not in text
    (statement,) = [b for b in _fenced_sql() if "edge_control_json()" in b]
    assert "clock_timestamp() at time zone 'UTC'" in statement
    assert 'YYYY-MM-DD"T"HH24:MI:SS"Z"' in statement


def test_the_only_shell_commands_left_are_the_uuid_and_the_cycle_end_line():
    text = _text()

    assert text.count("python -c \"import uuid") == 1
    assert "the single permitted Python call" in text
    assert 'TZ=America/New_York date "+Cycle end' in text
    assert "```bash" not in text and "```sh" not in text


def test_the_budget_rule_cuts_only_earnings_and_fundamentals():
    text = _text()
    rule = " ".join(text[text.index("**The budget rule.**"):text.index("## Step 0")].split())

    assert "`budget_left_min` below zero" in rule
    assert "Stop fetching `earnings` and `fundamentals`" in rule
    assert "Never skip `quotes` or `tradability`" in rule
    assert "Never abort for time alone" in rule
    assert "You raise no alert about it" in rule and "Cycle over budget" in rule
    assert "A `null` `budget_left_min`" in rule


def test_step_2a_fetches_quotes_and_tradability_before_the_kinds_the_budget_can_cut():
    section = _step_2a()
    order = [section.index(f"| `{kind}` |") for kind in
             ("quotes", "tradability", "earnings", "fundamentals_live")]

    assert order == sorted(order)
    assert "come first and are never cut" in section


def test_a_robinhood_failure_is_retried_once_and_never_slept_on():
    text = _text()

    assert "is retried once, immediately, with the identical arguments" in text
    assert "you call no Twelve Data tool" in text
    assert "retry once, then the\nRobinhood tool for the same field" not in text
    assert "| A Robinhood call fails once | Retry it once, identical arguments, no wait |" in text
    assert "| A tool call fails twice | Abort" in text


def test_the_verify_habits_are_named_and_forbidden():
    head = _text().split("## Step 0", 1)[0]

    assert 'No "verify N/N"' in head
    assert "Never abbreviate, summarise, re-key, reorder or \"fix\" a payload" in head
    # Step 4's check that a protective order actually rested is not that habit
    # and stays: it is a broker read the plan depends on.
    assert "verify each one rested" in _text()


def test_the_session_counts_are_the_databases():
    text = _text()

    assert "The database counts" in text and "do not estimate one" in text
