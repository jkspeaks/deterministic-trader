"""cli.py — Dip Recovery plan phase 3: the read-path wiring
`guard.strategy` needs.

`guard._strategy_enabled_denials` would be inert (the phase 1 defect this
project already paid for once) without something in `ctx` assembly reading
`payload["dip_recovery_config"]` — this proves that plumbing end to end, from
the routine's payload through `cli.decide` to `guard.evaluate`'s verdict.
"""

from __future__ import annotations

from src import cli, guard
from tests.factories import cli_payload, planned


def decided(**overrides) -> dict:
    return cli.decide(cli_payload(**overrides), cli.load_config())


def test_core_denied_when_the_payloads_config_turns_it_off():
    result = decided(dip_recovery_config={"core_strategies_enabled": False})
    codes = [
        d["code"]
        for denied in result["guard"]["denied"]
        for d in denied["denials"]
        if denied["order"]["symbol"] == "PFE"
    ]
    assert guard.STRATEGY_DISABLED in codes
    assert "PFE" not in planned(result, "buy")


def test_an_empty_config_leaves_core_unaffected():
    result = decided(dip_recovery_config={})
    assert "PFE" in planned(result, "buy")


def test_the_default_payload_with_no_config_key_at_all_is_unaffected():
    """Every routine invocation before this phase existed never sends
    `dip_recovery_config` at all — `cli_payload()`'s baseline doesn't
    either."""
    result = decided()
    assert "PFE" in planned(result, "buy")
