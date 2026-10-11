"""cli.py — the `dip-recovery-decide` command (Dip Recovery plan).

The command is deliberately thin: it composes a context and hands
`src.dip_recovery.evaluate`'s result back for the Dip Monitor routine to
write. These tests pin the plumbing — the envelope, the cycle derived from
the clock rather than taken on trust, the `cycle_id` stamp the evaluations
RPC needs, and that the four write-payloads (`position_updates`,
`symbol_lock_claims`/`releases`, `evaluations`, `control_updates`) survive
onto stdout. The decision logic itself is `tests/test_dip_recovery_*`.
"""

from __future__ import annotations

import io
import json

from src import cadence, cli
from tests.dip_recovery_factories import dip_payload, fill, held_position


def payload(**overrides) -> dict:
    """A Dip Monitor tick's `cycle.json`, from the same permissive baseline
    `tests/test_dip_recovery_engine.py` uses — plus the `now`/`cycle_id` the
    routine always sends and the command derives its cycle from.

    `dip_payload` rather than `dip_recovery_context`: the payload has to carry
    the broker's `get_earnings_results` envelope, because `cli._earnings` is
    what turns it into the shape guard reads. See that builder's docstring."""
    cycle_id = overrides.pop("cycle_id", "2026-08-31T15:47Z-dip")
    now = overrides.pop("now", "2026-08-31T15:47:00Z")
    return {**dip_payload(**overrides), "now": now, "cycle_id": cycle_id}


def decided(**overrides) -> dict:
    return cli.dip_recovery_decide(payload(**overrides), cli.load_config())


def run(payload_dict: dict) -> tuple[int, dict]:
    out = io.StringIO()
    code = cli.main(["dip-recovery-decide"], io.StringIO(json.dumps(payload_dict)), out)
    return code, json.loads(out.getvalue())


# --- the envelope --------------------------------------------------------------


def test_a_qualifying_watchlist_symbol_is_proposed_as_a_buy():
    result = decided()
    assert [o["symbol"] for o in result["orders"] if o["side"] == "buy"] == ["DIP1"]
    assert result["position_updates"]["DIP1"]["state"] == "ENTRY_PENDING"
    assert result["evaluations"][0]["passed"] is True


def test_the_envelope_carries_the_cycle_id_and_the_config_source():
    result = decided(cycle_id="2026-08-31T15:47Z-dip")
    assert result["cycle_id"] == "2026-08-31T15:47Z-dip"
    assert result["config_source"] == "checkout"
    assert "cycle" in result


def test_every_evaluation_row_is_stamped_with_this_cycle_id():
    result = decided(cycle_id="2026-08-31T15:47Z-dip")
    assert result["evaluations"]
    assert all(row["cycle_id"] == "2026-08-31T15:47Z-dip" for row in result["evaluations"])


def test_the_output_survives_a_json_round_trip():
    """This is what the routine writes to `edge_runlog` before it places
    anything; a value Python can no longer read back is an audit trail that
    cannot be replayed."""
    _, result = run(payload())
    assert json.loads(json.dumps(result)) == result


# --- the cycle is never taken on trust ---------------------------------------


def test_the_cycle_is_derived_from_the_clock_not_the_payload():
    """A forged post-close `now` classifies as POSTCLOSE regardless of any
    `cycle` dict the payload also carries — Invariant 6, the same property
    `test_cli_contract.py` pins for every other command."""
    _, result = run(
        payload(now="2026-08-31T20:30:00Z", cycle={"cycle_type": "full", "entries_allowed": True})
    )
    assert result["cycle"]["cycle_type"] == cadence.POSTCLOSE
    assert result["cycle"]["entries_allowed"] is False


def test_the_checkout_config_wins_over_a_payload_config():
    """`min_order_notional` lives in `config/config.json`; a payload that set
    it to a value nothing could clear must be ignored, so DIP1 is still
    proposed."""
    _, result = run(payload(config={"min_order_notional": 999_999.0}))
    assert [o["symbol"] for o in result["orders"] if o["side"] == "buy"] == ["DIP1"]


# --- the four write-payloads -------------------------------------------------


def test_a_sell_fill_reports_the_close_and_the_lock_release():
    result = decided(
        dip_recovery_positions={"DIP1": held_position()},
        dip_recovery_watchlist=[],
        dip_recovery_fills=[fill(side="sell", price=48.50, quantity=10.0)],
    )
    assert result["closed"] == ["DIP1"]
    assert result["symbol_lock_releases"] == ["DIP1"]
    assert "DIP1" not in result["position_updates"]


def test_a_buy_fill_reports_the_lock_claim():
    result = decided(
        dip_recovery_positions={
            "DIP1": {**held_position(), "state": "ENTRY_PENDING", "total_quantity": 0,
                     "total_fills": 0, "weighted_avg_cost": None, "fills": []}
        },
        dip_recovery_watchlist=[],
        dip_recovery_fills=[fill(price=46.00, quantity=10.0)],
    )
    assert result["symbol_lock_claims"] == ["DIP1"]
    assert result["position_updates"]["DIP1"]["state"] == "HOLDING"


def test_an_approved_entry_advances_the_dip_recovery_new_positions_counter():
    result = decided()
    assert result["control_updates"]["dip_recovery_new_positions"] == {
        "date": "2026-08-31", "count": 1,
    }


def test_a_symbol_locked_to_core_is_not_proposed():
    """Proves `strategy` is wired to `dip_recovery`: the lock map is read from
    this side's point of view, so DIP1 owned by core is skipped, not bought."""
    result = decided(symbol_lock={"DIP1": "core"})
    assert result["orders"] == []


def test_an_empty_tick_proposes_nothing_and_writes_nothing():
    _, result = run(payload(dip_recovery_watchlist=[], dip_recovery_positions={}, dip_recovery_fills=[]))
    assert result["orders"] == []
    assert result["position_updates"] == {}
    assert result["evaluations"] == []
