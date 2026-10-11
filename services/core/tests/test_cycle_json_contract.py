"""The one contract that keeps the prompt and the function from drifting.

Three documents describe the same object and none of them can be the source of
truth on its own:

* `docs/routine/cycle-prompt.md` Appendix D — what a live session assembles;
* `src/cycle_keys.CYCLE_JSON_KEYS` — the list both consumers import;
* `services/core/core/assemble.py` — what the hosted core assembles.

So Appendix D is checked against the constant (in
`tests/test_cycle_prompt_contract.py`, beside the rest of the prompt's
reference-integrity checks) and the assembler is checked against it here. The
failure this prevents is the one that has already cost this project two live
incidents: prose and code agreeing right up until the day they don't, with
nothing between them that fails.
"""

from __future__ import annotations

from services.core.core.assemble import DERIVED_KINDS, EXTRA_KINDS, assemble
from services.core.tests.conftest import rows_from
from src.cycle_keys import CYCLE_JSON_KEYS, CYCLE_JSON_KEY_SET
from tests.factories import cli_payload

CYCLE_ID = "2026-08-03T13:40:00Z-full"


def test_the_assembler_emits_nothing_outside_appendix_d():
    payload = cli_payload()

    cycle, _ = assemble(rows_from(payload, CYCLE_ID))

    outside = set(cycle) - CYCLE_JSON_KEY_SET
    assert outside == set(), f"assembled keys not in Appendix D: {sorted(outside)}"


def test_every_appendix_d_key_can_actually_be_assembled():
    # One row per documented key, all at once. A key the assembler silently
    # drops -- because it is not in its own list, or because the merge routed
    # it into `extras` -- shows up here rather than as an absent value inside
    # a live `decide`.
    assemblable = [k for k in CYCLE_JSON_KEYS if k not in DERIVED_KINDS]
    rows = [{"kind": key, "symbol": None, "payload": {"_": key}} for key in assemblable]

    cycle, extras = assemble(rows)

    assert set(cycle) == set(assemblable)
    assert extras == {}


def test_the_three_key_families_do_not_overlap():
    assert EXTRA_KINDS & CYCLE_JSON_KEY_SET == frozenset()
    # `balances` in particular: it is a real fetch step (`cli._fetch_plan`
    # names it, `get_accounts` answers it) that must NOT become a cycle.json
    # key, because Appendix D has one `account` key that both account-scope
    # tools feed and `cli.shape_account` is what merges them.
    assert "balances" in EXTRA_KINDS
    assert "balances" not in CYCLE_JSON_KEY_SET
    assert DERIVED_KINDS <= CYCLE_JSON_KEY_SET, (
        "a derived key is still an Appendix D key -- the core fills it, the "
        "runner may not"
    )


def test_the_constant_has_no_duplicates():
    assert len(CYCLE_JSON_KEYS) == len(CYCLE_JSON_KEY_SET)


def test_the_five_checkout_files_are_never_cycle_json_keys():
    # Appendix D's closing line: "Not on this list and never on it: config,
    # calendar, etfs, macro_map, inception. cli.py reads all five from the
    # checkout itself." A cycle that supplied them would be handing the core
    # its own rules.
    for name in ("config", "calendar", "etfs", "macro_map", "inception"):
        assert name not in CYCLE_JSON_KEY_SET
