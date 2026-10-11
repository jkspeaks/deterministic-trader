"""Phase 0 — resolving the `candidates`-scoped fetch rows' symbol list.

The acceptance question: does `resolve` hand back the same symbol set
`_decide_phase` would derive on its own, before any of the runner's
`candidates`-scoped fetches (quotes/indicators/tradability/earnings/
fundamentals) exist — so the runner can name symbols on those calls instead
of aborting per `runner-prompt.md`'s Step 2 table.
"""

from __future__ import annotations

from services.core.core import run as core
from services.core.tests.conftest import FakeSupabase, rows_from
from src import cli
from tests.factories import cli_payload


def resolved(payload=None):
    payload = cli_payload() if payload is None else payload
    supabase = FakeSupabase(
        tables={"edge_cycle_input": rows_from(payload, payload["cycle_id"])}
    )
    return core.run(payload["cycle_id"], core.RESOLVE, supabase), supabase


def test_resolve_returns_the_same_symbols_decide_would_fetch_for():
    payload = cli_payload()

    result, _ = resolved(payload)

    # `cli_payload`'s baseline holds F and discovers PFE (see its docstring) —
    # `_symbols` is the union of held positions, discovered triggers and
    # macro targets that `_decide_phase` computes for `day-context-plan`.
    assert result["status"] == core.STATUS_RESOLVED
    assert "F" in result["symbols"]
    assert "PFE" in result["symbols"]
    assert result["symbols"] == sorted(result["symbols"])


def test_resolve_writes_nothing_to_the_runlog():
    """Unlike `decide`, `resolve` makes no decision and commits nothing —
    it is pure derivation, safe to run twice (`decide` does, deliberately;
    see `run._resolve_phase`'s docstring)."""
    payload = cli_payload()

    _, supabase = resolved(payload)

    assert supabase.args("edge_runlog_write") == []


def test_resolve_on_a_noop_cycle_returns_no_symbols():
    # A Saturday: no equity session at all, same NOOP `cadence.decide` gives
    # `_decide_phase` for the identical `now`.
    payload = cli_payload(now="2026-08-08T13:40:00Z")

    result, _ = resolved(payload)

    assert result["status"] == core.STATUS_NOOP
    assert result["symbols"] == []


def test_resolve_reports_every_symbol_stale_with_no_day_context_cached():
    """`cli_payload`'s baseline posts no `day_context` at all — the "never
    cached" half of `_day_context_fresh_symbols`'s fresh/stale split
    (`src/cli.py:485-496`). With nothing cached, every resolved symbol must
    read `stale`, matching what the runner would need to fetch live."""
    payload = cli_payload()

    result, _ = resolved(payload)

    assert result["symbols"] != []
    assert sorted(result["stale"]) == result["symbols"]
    assert sorted(result["statics_stale"]) == result["symbols"]


def test_resolve_excludes_a_symbol_whose_day_context_is_actually_fresh():
    """The write-back half of the loop: once a symbol's `day_context` entry is
    fresh, `resolve` must stop naming it as something Step 2a needs to
    fetch."""
    payload = cli_payload()
    loaded = cli.load_config()
    cycle = cli.cycle_of(cli.normalized(cli_payload(), loaded), loaded)
    # A `fetched_at` equal to `now` is as fresh as a cached entry gets.
    fresh_day_context = {
        "F": {
            "signals": {"rsi": 45.0, "atr": 1.2, "sma_20": 1, "sma_50": 1, "sma_200": 1},
            "earnings": {},
            "fetched_at": cycle.now,
        }
    }
    supabase = FakeSupabase(
        tables={"edge_cycle_input": rows_from(payload, payload["cycle_id"])},
        returns={"edge_day_context_recent": fresh_day_context},
    )

    result = core.run(payload["cycle_id"], core.RESOLVE, supabase)

    assert "F" in result["symbols"]
    assert "F" not in result["stale"]


def test_resolve_on_a_noop_cycle_returns_no_stale_either():
    payload = cli_payload(now="2026-08-08T13:40:00Z")

    result, _ = resolved(payload)

    assert result["stale"] == []
    assert result["statics_stale"] == []


def test_a_failed_resolve_stores_status_failed_like_any_other_phase():
    payload = cli_payload()
    payload.pop("control")
    supabase = FakeSupabase(
        tables={"edge_cycle_input": rows_from(payload, payload["cycle_id"])}
    )

    result = core.run(payload["cycle_id"], core.RESOLVE, supabase)

    assert result["status"] == core.STATUS_FAILED
    written = supabase.args("edge_cycle_output_write")
    assert written[-1]["p_fields"]["status"] == core.STATUS_FAILED
