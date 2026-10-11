"""Reference-integrity contract for the routine's governed documents.

The routine prompts are read fresh from `origin/main` by a live Claude
session on every scheduled cycle (no deploy step) and followed as a runbook
against a funded account. They have **no other test or CI coverage of their
own**, and prose drifting from what the code actually does goes undetected
until a live cycle hits it.

This test does not, and cannot, validate prose semantics, call signatures,
argument order, data shape, execution order, authorization, or broker
behavior. It proves two narrower things only: (1) every `src.cli`
subcommand and `edge_*` SQL function the governed docs *name* actually
exists in the current code/db surface, and (2) a short list of
non-negotiable control statements and reference-table blocks survive
verbatim (whitespace-normalized).
"""

import re
from pathlib import Path

import pytest

from src import cli

REPO_ROOT = Path(__file__).resolve().parent.parent

GOVERNED_ROUTINE_DOCS = [
    REPO_ROOT / "docs" / "routine" / "cycle-prompt.md",
    # The thin runner: the runbook the scheduled core routines follow against
    # a funded account. The same reference-integrity check applies.
    REPO_ROOT / "docs" / "routine" / "runner-prompt.md",
    # The on-demand Manual Check: unscheduled, but a live session follows it
    # against the funded account every time the operator starts it.
    REPO_ROOT / "docs" / "routine" / "manual-check-prompt.md",
]

# A fenced block, ``` ... ```, possibly language-tagged, DOTALL so it spans lines.
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)

# Stage A: a live invocation, inside a fenced block only. A prose sentence
# that merely *mentions* a command inline must never be able to fail this
# test on its own -- only an actual invocation counts.
_CLI_INVOCATION_RE = re.compile(r"python -m src\.cli\s+(\S+)")

# Stage B: an inline `src.cli <hyphen-command>` code span, anywhere in the
# text. The character class is deliberately hyphen/lowercase/digit only, so
# it excludes `src.cli settled_cash` (an underscore, prose referring to the
# settled-cash *concept*, not the `settled-cash` subcommand) and
# `src.cli._tax_lots` (a Python attribute, not a subcommand) -- both appear
# in the prompt on purpose and must never be mistaken for commands.
_CLI_BACKTICK_RE = re.compile(r"`src\.cli ([a-z0-9-]+)`")

# A SQL function *call* -- the name immediately followed by '(' -- so a bare
# mention like "`edge_control` unreadable" in Step 12's table (never
# followed by a paren) is correctly never flagged.
_SQL_CALL_RE = re.compile(r"\b(edge_[a-zA-Z_]+)(?=\()")

# The routine API's own declaration form, scanned across every db/*.sql
# file. If this project's declaration convention ever changes (quoted
# names, schema-qualified names, a different DDL verb), this regex should
# fail to match anything and this test should start failing loudly rather
# than silently under-scanning.
_SQL_DECLARATION_RE = re.compile(
    r"create or replace function\s+(edge_[a-zA-Z_]+)\s*\(", re.IGNORECASE
)


def _fenced_blocks(text: str) -> list[str]:
    return _FENCE_RE.findall(text)


def _named_cli_commands(text: str) -> set[str]:
    invoked = set()
    for block in _fenced_blocks(text):
        invoked.update(_CLI_INVOCATION_RE.findall(block))
    mentioned = set(_CLI_BACKTICK_RE.findall(text))
    return invoked | mentioned


def _named_sql_functions(text: str) -> set[str]:
    return set(_SQL_CALL_RE.findall(text))


def _real_sql_functions() -> set[str]:
    names = set()
    for sql_file in sorted((REPO_ROOT / "db").glob("*.sql")):
        names.update(_SQL_DECLARATION_RE.findall(sql_file.read_text(encoding="utf-8")))
    return names


def _normalize(text: str) -> str:
    """Collapse all whitespace runs to single spaces. Lets a required block
    survive incidental reflow/reformatting elsewhere in the file while still
    catching a changed value, a dropped row, or reworded text -- a raw
    byte-for-byte `in` check would be too brittle to keep passing through
    ordinary editing of the surrounding prose."""
    return " ".join(text.split())


# --- fixtures ----------------------------------------------------------


@pytest.fixture(params=GOVERNED_ROUTINE_DOCS, ids=lambda p: p.name)
def governed_doc_text(request) -> str:
    """Skip, don't fail, a governed doc that doesn't exist."""
    path: Path = request.param
    if not path.exists():
        pytest.skip(f"{path} does not exist yet")
    return path.read_text(encoding="utf-8")


# --- reference integrity -------------------------------------------------


def test_every_named_cli_command_actually_exists(governed_doc_text):
    named = _named_cli_commands(governed_doc_text)
    unknown = named - set(cli.COMMANDS)
    assert not unknown, (
        f"governed doc names src.cli command(s) with no cli.COMMANDS "
        f"entry: {sorted(unknown)} (real commands: {sorted(cli.COMMANDS)})"
    )


def test_every_named_sql_function_actually_exists(governed_doc_text):
    named = _named_sql_functions(governed_doc_text)
    real = _real_sql_functions()
    unknown = named - real
    assert not unknown, (
        f"governed doc calls edge_* function(s) with no db/*.sql "
        f"definition: {sorted(unknown)}"
    )


def test_the_sql_declaration_regex_actually_finds_functions():
    """A regex that silently matched nothing would make the test above
    vacuously pass -- assert the real routine API is non-empty so a future
    change to the declaration convention (quoting, schema-qualification,
    a different DDL verb) fails loudly here instead of under-scanning."""
    assert len(_real_sql_functions()) >= len(cli.COMMANDS)


def test_cli_backtick_extraction_excludes_known_non_commands():
    """Two real strings that already appear in this project's docs on
    purpose, pinned so the extraction regex's exclusions don't regress
    silently if someone "simplifies" the pattern later."""
    text = (
        "see `src.cli settled_cash`'s docstring for the null-safety rule, "
        "and do not pre-rename `src.cli._tax_lots`'s output by hand."
    )
    assert _named_cli_commands(text) == set()


def test_cli_backtick_extraction_still_catches_a_real_mention():
    text = "use `src.cli merge-fetch` -- never retype, reformat or reconstruct the values"
    assert _named_cli_commands(text) == {"merge-fetch"}


def test_cli_invocation_extraction_requires_a_fence():
    """The failure mode this guards against: a plain sentence that
    *describes* running a command (not a fenced invocation) must not be able
    to fail the contract test on its own -- only fenced, literal invocations
    count as Stage A."""
    text = "That day we ran python -m src.cli decide by hand from the shell, unfenced."
    assert _named_cli_commands(text) == set()


def test_sql_call_extraction_ignores_a_bare_table_or_key_mention():
    text = "| `edge_control` unreadable | Stop. Place nothing. |"
    assert _named_sql_functions(text) == set()


# --- negative control: the contract must actually be able to fail --------


def test_the_contract_fails_on_an_injected_fake_cli_command():
    forged = "```bash\nuv run python -m src.cli not-a-real-command < cycle.json\n```"
    named = _named_cli_commands(forged)
    assert named == {"not-a-real-command"}
    assert named - set(cli.COMMANDS) == {"not-a-real-command"}


def test_the_contract_fails_on_an_injected_fake_sql_function():
    forged = "select edge_not_a_real_function('<trading_day>');"
    named = _named_sql_functions(forged)
    assert named == {"edge_not_a_real_function"}
    assert named - _real_sql_functions() == {"edge_not_a_real_function"}


# --- REQUIRED_PROMPT_LITERALS: non-negotiable control statements ---------
#
# One entry per invariant this project has already lost once and rebuilt --
# grouped so a reviewer can see *why* each string is pinned, not just that
# it is. Whole-block entries protect the highest-scrutiny reference zones
# byte-content-for-byte-content (whitespace-normalized); short entries pin
# the exact imperative sentence an incident was fixed by. Only checked
# against docs/routine/cycle-prompt.md.

REQUIRED_PROMPT_LITERALS: dict[str, str] = {
    # Absolute 2 -- the rule an earlier skipped_count incident violated.
    "absolute-2-no-arithmetic": (
        "Never invent, round, average or recompute a number."
    ),
    # Step 7 -- the direct fix for that same incident.
    "step7-no-shortening-lists": (
        "Do not shorten a list to its length, here or in `report.json` or "
        "`reconcile.json`, for any key"
    ),
    "step6-tradability-literal-string": (
        "`all_day_tradability`/`fractional_tradability` are unaffected — still the bare "
        'string `"tradable"`, not `all_day_tradability_tradable`; the MCP guide is wrong '
        "about that value and a literal reading of the guide denies every symbol in "
        "every extended session."
    ),
    # Step 6 -- the direct fix for an earlier tradeable-boolean incident.
    "step6-tradeable-boolean": (
        "The headline field is `tradeable`, a boolean — not a `\"tradability\"` string."
    ),
    # Step 6 -- merge-fetch: the user's own worked example of a rule that
    # must survive verbatim regardless of how the surrounding prose is cut.
    "step6-never-retype": (
        "never retype, reformat or reconstruct the values from memory, even when you are "
        "confident you remember them correctly"
    ),
    # Step 8a -- cancel/sell/buy ordering; getting this wrong doubles a
    # position or leaves proceeds stranded.
    "step8a-order-of-operations": (
        "**Cancels, then sells, then buys.** `guard` already ordered the sells ahead of "
        "the buys inside `plan.orders`; preserve the order it gave you."
    ),
    # Step 8a -- the no-blind-retry rule.
    "step8a-no-retry": (
        "**do not retry**, at all, this cycle (§14: blind retry is how a position\ndoubles);"
    ),
    # Step 7 -- the large-payload procedure's core imperatives (items 1-3
    # verbatim, and item 4's rule).
    "step7-payload-grep-tag-check": (
        "`grep -c '\\$' decide.json` (or whichever file). If it's `0`, any tag is\n"
        "   safe; `$decidejson$` is a fine default. If it's nonzero, `grep -o\n"
        "   '\\$[a-zA-Z0-9_]*\\$' decide.json | sort -u` and pick a tag not in that list."
    ),
    "step7-payload-one-call-not-retyped": (
        "the whole file, pasted once, not\n   retyped from memory and not split into pieces."
    ),
    "step7-payload-verify-by-hash": (
        "Verify by hash, not by eye:"
    ),
    "step7-payload-jsonb-set-patch": (
        "**If it mismatches, patch the specific broken path with `jsonb_set`\n"
        "against the one wrong key**"
    ),
    "step7-payload-never-scratch-table": (
        "never by deleting and re-pasting the whole\n"
        "   blob a second time, and never by staging the payload through a scratch\n"
        "   table in fixed-size pieces and reassembling it with `string_agg`"
    ),
    "step7-payload-do-not-reach-for-it": "Do not\nreach for it.",
    # Step 8b -- the resting-order field block, verbatim, untouched. Nothing is
    # computed here any more. `reconcile` works out what should rest behind the
    # whole book, `decide` puts what guard approved into `plan.protection`, and
    # this step places it as given.
    "step8b-order-field-block": (
        "```\nplace_equity_order\n  symbol        <row.symbol>\n  side          sell\n"
        "  type          <row.type>            stop_market for a stop, limit for a target\n"
        "  quantity      <row.quantity>\n"
        "  stop_price    <row.stop_price>      present on a stop, null on a target\n"
        "  limit_price   <row.limit_price>     present on a target, null on a stop\n"
        "  market_hours  <row.session>         always regular_hours\n"
        "  time_in_force <row.time_in_force>   always gtc\n```"
    ),
    "step8b-verify-each-rested": (
        "**Verify each one rested.** Call `get_equity_orders --symbol <symbol>` and\n"
        "confirm the state is `queued` or `confirmed`. A 200 from placement is not\n"
        "verification."
    ),
    # The rule the whole of 8b turns on. Without the flag, 8c's postclose sweep
    # cancels the protection the same cycle placed it.
    "step8b-mark-the-flag": (
        "**Mark each row in `placements` (§8c) with the flag its plan row carries**"
    ),
    # Appendix B -- the SQL call table, verbatim, untouched.
    "appendix-b-table": (
        "| # | Call | When |\n"
        "|---|---|---|\n"
        "| 1 | `select edge_control_json()` | Step 1, and again after the claim |\n"
        "| 2 | `select edge_cycle_claim(id, type)` | Step 4 |\n"
        "| 3 | `select edge_cycle_renew(id)` | after Steps 6, 7, 8 |\n"
        "| 4 | `select edge_cycle_release(id)` | Step 10, and on any abort |\n"
        "| 5 | `select edge_prior('<trading_day>')` | Step 6 — placements, `prior_book`, "
        "today's rejections, in one call |\n"
        "| 6 | `select edge_play_state_json()` | Step 6 |\n"
        "| 7 | `select edge_loss_closes_recent()` | Step 6, reconciling and entry "
        "cycles — rows carry `basis_unknown`; `reconcile` reads it "
        "too |\n"
        "| 8 | `select edge_runlog_write(...)` | Steps 3, 7, 8c, 9 |\n"
        "| 9 | `select edge_control_apply(...)`, `edge_play_state_apply(...)`, "
        "`edge_loss_closes_append(...)`, `edge_realized_trades_append(...)`, "
        "`edge_benchmark_upsert(...)`, "
        "`edge_alerts_enqueue(...)` | Steps 7, 9, 10 |\n"
        "| 10 | `select edge_day_context_recent(trading_day)` | Step 5 — read once, "
        "before `day-context-plan` |\n"
        "| 11 | `select edge_day_context_write_symbol(trading_day, symbol, symbol_data)` "
        "| Step 6 — once per `stale` symbol, immediately after that symbol's own fetch |"
    ),
    # The DIP scanner runs *before* `src.cli candidates` and its survivors
    # go in unfiltered as `dip_survivors`: `candidates` merges them and
    # applies the operator's sector rotation. A session merging by hand
    # after `candidates` returned — the old Step 5 — would bypass the
    # rotation entirely on this path.
    "step-5-dip-scanner-before-candidates": (
        "**On an entries-allowed cycle, run the DIP scanner (Appendix C) next — also\n"
        "before `src.cli candidates`.**"
    ),
    "step-5-no-session-side-sector-filtering": (
        "The rotation is applied inside `candidates` — never pre-filter a\n"
        "candidate, a DIP survivor or anything else by sector yourself."
    ),
    # Appendix C -- the DIP scanner filter block, verbatim. The price floor is
    # a TODO in the prose, not a filter line, until its
    # `get_scanner_filter_specs` name is confirmed.
    "appendix-c-scanner-block": (
        '```\ncreate_scan  name "edge-dip-oversold"\n'
        "  FILTER_TYPE_RSI            <= 30    length 14   interval 1d\n"
        "  FILTER_TYPE_EMA            (close above)  length 200  interval 1d\n"
        "  FILTER_TYPE_AVERAGE_VOLUME >= 1500000       interval 1d\n"
        "  FILTER_TYPE_MARKET_CAP     >= 2000000000\n```"
    ),
    # Appendix D -- the cycle.json key map, verbatim. Pinned as two halves
    # (it's long) rather than one block, whitespace-normalized, requiring the
    # whole table intact. Every broker-fed row says "paste the response as it
    # came" and names `src/broker_shapes.py`: `get_equity_orders` returns the
    # order id as `id`, and a session that renames it by hand makes this
    # system's own resting orders read as phantoms. The pin is the table's
    # rows staying together and in order, not the wording of any one.
    "appendix-d-table-top": (
        "| Key | Filled from |\n"
        "|---|---|\n"
        "| `cycle_id`, `now` | you, Step 2 |\n"
        "| `control` | call 1 |\n"
        "| `account` | `get_portfolio` under `account` and `get_accounts` under "
        "`accounts`, **both pasted as they came** — `src.cli shape-account` does "
        "the merge, on every path. Never hand-merge; never retype a field |\n"
        "| `positions` | `get_equity_positions` — paste the response as it came; "
        "`src/broker_shapes.py` unwraps it (it carries `market_value`, "
        "`shares_available_for_sells` and `average_buy_price`, all three of "
        "which the book needs — `average_buy_price` is the second witness the "
        "entry-price anchor is checked against, so a response edited down to "
        "\"just the useful fields\" silently removes the check that stops a "
        "2xATR stop resting at the position's own entry price) |\n"
        "| `orders` | `get_equity_orders` — **`plan-fetch` lists it twice** "
        "(`state=confirmed`: what is resting, of any age; `created_at_gte`: what "
        "happened recently), because the unfiltered call is the whole account "
        "history and overflowed the inline limit. Run both with "
        "their filters and every call parameter unchanged (plus `account_number` "
        "from `config/inception.json`), then put the **two responses, each whole "
        "and unedited, as a two-element list under `orders`, in the order "
        "listed**. Do not concatenate their rows yourself and do not dedupe — "
        "`src/broker_shapes.py` flattens the list, keeps an order both returned "
        "once, and renames the order id from `id` to `order_id`. **Renaming it "
        "by hand blocks entries** |\n"
        "| `fills` | executions off `get_equity_orders`, each with `cost_basis` from tax "
        "lots |\n"
        "| `tax_lots` | `get_equity_tax_lots`, per symbol — paste each response as "
        "it came; `src/broker_shapes.py` renames `tax_cost_basis`/`open_date` to "
        "`cost_basis`/`acquired_date` |"
    ),
    "appendix-d-table-bottom": (
        # The pin is the three rows staying together at the bottom of the
        # table, not the wording of any one of them.
        "| `reconcile` | `reconcile.json`, on the cycles that run it — including "
        "its `protection_plan`, which is where `decide`'s `plan.protection` and "
        "its protective cancels come from |\n"
        '| `failure` | `null`, or `{"step": "...", "detail": "<verbatim broker text>"}` |\n'
        "| `sectors` | `get_equity_fundamentals`, **only** for adopted non-members — "
        "the issuer holdings file wins every disagreement |"
    ),
}


@pytest.mark.parametrize("literal_id", sorted(REQUIRED_PROMPT_LITERALS))
def test_required_literal_survives_in_cycle_prompt(literal_id):
    doc = (REPO_ROOT / "docs" / "routine" / "cycle-prompt.md").read_text(encoding="utf-8")
    required = REQUIRED_PROMPT_LITERALS[literal_id]
    assert _normalize(required) in _normalize(doc), (
        f"required literal {literal_id!r} no longer found (even whitespace-"
        f"normalized) in docs/routine/cycle-prompt.md -- this pins a rule "
        f"that was fixed after a real production incident; see the "
        f"REQUIRED_PROMPT_LITERALS comment above for which one."
    )


# ---------------------------------------------------------------------------
# Appendix D against `src/cycle_keys.py`
# ---------------------------------------------------------------------------
#
# Appendix D is the specification of `cycle.json`, and as of the hosted core
# (`services/core`) it has two independent implementations: the live session
# that assembles the document by hand, and `services/core/core/assemble.py`
# that assembles it from `edge_cycle_input` rows. `src/cycle_keys.py` is the
# list both import. This test is the half that faces the prose; the half that
# faces the assembler is
# `services/core/tests/test_cycle_json_contract.py`.
#
# A key added to the table and not to the constant fails here. A key added to
# the constant and not to the table fails here too, which is the direction
# that matters: it is how the hosted core would grow an input the prompt never
# tells anyone to send.

_APPENDIX_D_HEADING = "## Appendix D"
# The first cell of a table row, and every `code span` inside it. Appendix D
# lists two, three and four keys on one row (`triggers`, `macro_targets`;
# `intent`, `prior_book`, `rejections`), so a regex that took only the first
# span would under-read the table and pass while missing keys.
_TABLE_ROW_RE = re.compile(r"^\|([^|]*)\|", re.MULTILINE)
_CODE_SPAN_RE = re.compile(r"`([a-z_][a-z0-9_]*)`")


def _appendix_d_keys() -> set[str]:
    doc = (REPO_ROOT / "docs" / "routine" / "cycle-prompt.md").read_text(encoding="utf-8")
    start = doc.index(_APPENDIX_D_HEADING)
    section = doc[start:]

    keys: set[str] = set()
    for cell in _TABLE_ROW_RE.findall(section):
        if set(cell.strip()) <= {"-", ":", " "}:  # the header underline row
            continue
        keys.update(_CODE_SPAN_RE.findall(cell))
    return keys


def test_appendix_d_names_exactly_the_shared_cycle_key_list():
    from src.cycle_keys import CYCLE_JSON_KEY_SET

    documented = _appendix_d_keys()

    assert documented, (
        "Appendix D's table parsed to nothing -- the table markup changed and "
        "this test is now silently checking an empty set. Fix the parser "
        "before touching the constant."
    )
    assert documented == set(CYCLE_JSON_KEY_SET), (
        "docs/routine/cycle-prompt.md Appendix D and src/cycle_keys.py disagree.\n"
        f"  in the table, not in the constant: {sorted(documented - set(CYCLE_JSON_KEY_SET))}\n"
        f"  in the constant, not in the table: {sorted(set(CYCLE_JSON_KEY_SET) - documented)}"
    )
