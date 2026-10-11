"""cli.py — Dip Recovery plan phase 2: the read-path wiring guard.lock needs.

`guard._symbol_lock_denials` was inert until something in `ctx` assembly
actually read `payload["symbol_lock"]`/`payload["strategy"]` — phase 1 wired
the denial, this proves the plumbing that feeds it exists end to end, from
the routine's payload through `cli.decide` to `guard.evaluate`'s verdict.
"""

from __future__ import annotations

from src import cli, guard
from tests.factories import cli_payload, planned


def decided(**overrides) -> dict:
    return cli.decide(cli_payload(**overrides), cli.load_config())


def test_a_candidate_locked_to_the_other_side_is_denied_and_dropped():
    result = decided(symbol_lock={"PFE": "dip_recovery"})
    codes = [
        d["code"]
        for denied in result["guard"]["denied"]
        for d in denied["denials"]
        if denied["order"]["symbol"] == "PFE"
    ]
    assert guard.SYMBOL_LOCKED_OTHER_SIDE in codes
    assert "PFE" not in planned(result, "buy")


def test_an_unlocked_candidate_is_unaffected_by_an_empty_lock_map():
    result = decided(symbol_lock={})
    assert "PFE" in planned(result, "buy")


def test_the_default_payload_with_no_lock_key_at_all_is_unaffected():
    """Every routine invocation before Dip Recovery existed never sends
    `symbol_lock` at all — `cli_payload()`'s baseline doesn't either."""
    result = decided()
    assert "PFE" in planned(result, "buy")
