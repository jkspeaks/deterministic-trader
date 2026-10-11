"""cli.py — the `dip-recovery-pool` and `dip-recovery-pick` commands.

The Universe Pick routine's two steps, with the per-symbol Robinhood fetch
between them. These tests pin the plumbing the routine depends on: the
envelope, the cycle derived from the clock, the membership fence resolved
through `universe.resolve` rather than a second hand-rolled member set, the
derived-earnings contract, and the one `edge_control_apply` payload the whole
routine exists to produce. The ranking itself is
`tests/test_dip_recovery_watchlist.py`.
"""

from __future__ import annotations

import io
import json

from src import cli
from src.dip_recovery import core
from tests.dip_recovery_factories import (
    dip1_earnings_raw,
    dip_recovery_config,
    held_position,
    ranked_symbol,
)

SNAPSHOT = {
    "as_of": "2026-08-30",
    "holdings": [
        {"symbol": "DIP1", "sector": "Information Technology"},
        {"symbol": "DIP2", "sector": "Health Care"},
        {"symbol": "CORE1", "sector": "Energy"},
    ],
    "cross_check": [
        {"symbol": "DIP1", "sector": "Information Technology"},
        {"symbol": "DIP2", "sector": "Health Care"},
        {"symbol": "CORE1", "sector": "Energy"},
    ],
}


def pool_payload(**overrides) -> dict:
    """Step 1's stdin: the scan's tickers plus the four free reads.

    07:50 ET, which is 11:50 UTC — after core's 07:40 full cycle, so the day's
    `data/universe.json` and `edge_loss_closes` are the ones core just used.
    """
    base = {
        "now": "2026-08-31T11:50:00Z",
        "cycle_id": "2026-08-31T07:50Z-pick",
        "control": {"readable": True, "trading_enabled": True},
        "dip_recovery_config": dip_recovery_config(),
        "snapshot": SNAPSHOT,
        "scan": ["DIP1", "DIP2"],
        "symbol_lock": {},
        "dip_recovery_positions": {},
        "positions": [],
        "loss_closes": [],
    }
    return {**base, **overrides}


def pick_payload(**overrides) -> dict:
    """Step 2's stdin: step 1's `fetch` list plus what the routine fetched for
    it. `earnings` carries `get_earnings_results`' own envelope, never the
    derived shape — `cli._earnings` is what derives `sessions_until_report`,
    and a payload that arrived pre-derived asserts a contract nothing in
    `src/` implements (which caught the phase-7 build too)."""
    rows = overrides.pop("rows", {"DIP1": ranked_symbol(), "DIP2": ranked_symbol(atr=2.00)})
    base = {
        "now": "2026-08-31T11:50:00Z",
        "cycle_id": "2026-08-31T07:50Z-pick",
        "control": {"readable": True, "trading_enabled": True},
        "dip_recovery_config": dip_recovery_config(),
        "fetch": list(rows),
        "quotes": {s: {"last": r["last"]} for s, r in rows.items()},
        "signals": {s: {"atr": r["atr"]} for s, r in rows.items()},
        "fundamentals": {s: {"average_volume": r["average_volume"]} for s, r in rows.items()},
        "earnings": {s: dip1_earnings_raw() for s in rows},
    }
    return {**base, **overrides}


def run(command: str, payload_dict: dict) -> tuple[int, dict]:
    out = io.StringIO()
    code = cli.main([command], io.StringIO(json.dumps(payload_dict)), out)
    return code, json.loads(out.getvalue())


# --- the pool step -------------------------------------------------------------


def test_the_pool_returns_the_scan_hits_that_survive_the_free_filters():
    result = cli.dip_recovery_pool(pool_payload(), cli.load_config())

    assert result["fetch"] == ["DIP1", "DIP2"]
    assert result["skipped"] == []
    assert result["cycle_id"] == "2026-08-31T07:50Z-pick"
    assert result["config_source"] == "checkout"


def test_membership_comes_from_the_snapshot_not_from_the_scan():
    """`universe.resolve` and not a second member set, for the reason
    `sector_map` was factored out in phase 7: two derivations of "who is in the
    universe" would let this watchlist admit a name guard's own candidate check
    would then refuse."""
    result = cli.dip_recovery_pool(
        pool_payload(scan=["DIP1", "NOTAMEMBER"]), cli.load_config()
    )

    assert result["fetch"] == ["DIP1"]
    assert result["skipped"][0]["code"] == core.NOT_IN_UNIVERSE


def test_a_snapshot_older_than_the_limit_picks_nothing():
    stale = {**SNAPSHOT, "as_of": "2026-06-01"}
    result = cli.dip_recovery_pool(pool_payload(snapshot=stale), cli.load_config())

    assert result["fetch"] == []
    assert {row["code"] for row in result["skipped"]} == {core.NOT_IN_UNIVERSE}


def test_the_pool_reads_this_sides_own_position_rows():
    result = cli.dip_recovery_pool(
        pool_payload(dip_recovery_positions={"DIP1": held_position()}), cli.load_config()
    )

    assert result["fetch"] == ["DIP2"]
    assert result["skipped"][0]["code"] == core.ALREADY_TRACKED


# --- the pick step -------------------------------------------------------------


def test_the_pick_produces_the_one_control_write_the_routine_makes():
    result = cli.dip_recovery_pick(pick_payload(), cli.load_config())

    assert result["control_updates"] == {
        "dip_recovery_watchlist": {
            "trading_day": "2026-08-31",
            "symbols": ["DIP2", "DIP1"],
        }
    }


def test_earnings_are_derived_from_the_brokers_envelope():
    """A print inside `earnings_flatten_days` has to be excluded, and the only
    way to know it is there is `_earnings_view` reducing the quarter rows. A
    command that passed the raw envelope through would find no
    `sessions_until_report` and drop every symbol `EVENT_RISK` instead."""
    soon = {"DIP1": dip1_earnings_raw("2026-09-01"), "DIP2": dip1_earnings_raw()}
    result = cli.dip_recovery_pick(pick_payload(earnings=soon), cli.load_config())

    assert result["control_updates"]["dip_recovery_watchlist"]["symbols"] == ["DIP2"]
    assert [row["code"] for row in result["skipped"]] == [core.EVENT_RISK]


def test_the_cycle_is_derived_from_the_clock_not_taken_on_stdin():
    """Invariant 6. The pick runs premarket by design — a payload that could
    assert its own cycle could assert a different one."""
    result = cli.dip_recovery_pick(
        pick_payload(cycle={"cycle_type": "full", "trading_day": "1999-01-01"}),
        cli.load_config(),
    )

    assert result["cycle"]["trading_day"] == "2026-08-31"
    assert result["control_updates"]["dip_recovery_watchlist"]["trading_day"] == "2026-08-31"


def test_both_commands_are_registered_and_round_trip_through_json():
    """This is what the routine writes to `edge_runlog`; a value Python can no
    longer read back is an audit trail that cannot be replayed."""
    for command, body in (
        ("dip-recovery-pool", pool_payload()),
        ("dip-recovery-pick", pick_payload()),
    ):
        code, result = run(command, body)
        assert code == 0
        assert json.loads(json.dumps(result)) == result


def test_a_pick_with_nothing_fetched_still_writes_todays_empty_watchlist():
    """The Dip Monitor reads a watchlist whose `trading_day` is not today as
    empty anyway. The explicit write is what separates "this morning ran and
    found nothing" from "this morning may never have run"."""
    result = cli.dip_recovery_pick(pick_payload(rows={}), cli.load_config())

    assert result["control_updates"] == {
        "dip_recovery_watchlist": {"trading_day": "2026-08-31", "symbols": []}
    }
