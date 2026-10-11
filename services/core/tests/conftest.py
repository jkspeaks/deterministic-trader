"""A Supabase that records instead of writing.

The unit here is the order and the arguments of fourteen writes, and the only
thing that can assert on that is a double that keeps them. So the fake
implements `Supabase`'s two verbs and nothing else: no query engine, no schema,
no validation the real PostgREST would do. It proves `run.py` calls what the
prompt calls, with what the prompt passes — not that Postgres accepts it.
Applying `db/schema.sql` to a real database is what covers the other half.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class FakeSupabase:
    def __init__(self, tables: Mapping[str, list] | None = None, returns: Mapping[str, Any] | None = None):
        # `tables[name]` is what `select` hands back for that table, regardless
        # of the query string — the fake does not filter, because every caller
        # in `run.py` filters on this cycle's id and there is only one cycle.
        self.tables: dict[str, list] = {k: list(v) for k, v in (tables or {}).items()}
        # `returns[function]` is what that RPC answers with; anything unlisted
        # answers None, which is what most of these write-only functions do.
        self.returns: dict[str, Any] = dict(returns or {})
        self.calls: list[tuple[str, dict]] = []
        self.reads: list[tuple[str, str]] = []
        self.stamps: list[tuple[str, str, str, dict | None, str]] = []

    # -- Supabase's surface ------------------------------------------------

    def rpc(self, function: str, params: Mapping[str, Any] | None = None) -> Any:
        self.calls.append((function, dict(params or {})))
        if function == "edge_cycle_output_write":
            # Keep it readable back, the way the real table is: the finalize
            # phase reads the decide phase's `control_updates` out of it.
            fields = dict((params or {}).get("p_fields") or {})
            rows = self.tables.setdefault("edge_cycle_output", [])
            if rows:
                rows[0].update(fields)
            else:
                rows.append({"cycle_id": (params or {}).get("p_cycle_id"), **fields})
        return self.returns.get(function)

    def select(self, table: str, query: str) -> list[dict]:
        self.reads.append((table, query))
        if table == "edge_cycle_step" and table not in self.tables:
            # The triggers that write the step log live in Postgres, which this
            # double is not, so a test that never mentions the log gets a
            # *healthy* one (a row for every registered step) and the finalize
            # gate stays quiet. A test of the gate passes
            # `tables={"edge_cycle_step": [...]}` explicitly.
            from services.core.core import steps

            return [{"step": name, "event": "beat", "detail": None} for name in steps.STEP_NAMES]
        return list(self.tables.get(table, []))

    def stamp(self, cycle_id, step, event, detail=None, source="core") -> bool:
        """`Supabase.stamp`: recorded apart from `calls`, so the exact-call
        assertions every existing test makes are unaffected by step logging."""
        self.stamps.append((cycle_id, step, event, dict(detail) if detail else None, source))
        return True

    def stamped(self, step: str | None = None) -> list[tuple[str, str]]:
        return [(s, e) for _, s, e, _, _ in self.stamps if step is None or s == step]

    # -- assertions ---------------------------------------------------------

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def args(self, function: str) -> list[dict]:
        return [params for name, params in self.calls if name == function]

    def first(self, function: str) -> dict:
        found = self.args(function)
        assert found, f"{function} was never called; got {self.names()}"
        return found[0]


@pytest.fixture()
def supabase() -> FakeSupabase:
    return FakeSupabase()


def rows_from(payload: Mapping[str, Any], cycle_id: str) -> list[dict]:
    """Turn a `cycle.json` into the `edge_cycle_input` rows a runner would
    have posted for it — the inverse of `assemble.assemble`, used so a test
    can drive the hosted core with the exact payload the local `cli` tests
    drive `cli.decide` with.

    Symbol-keyed maps are exploded per symbol on purpose: that is the shape
    the runner actually produces (one row per batched quote), and a test that
    posted them whole would never exercise the per-symbol merge.
    """
    from services.core.core.assemble import DERIVED_KINDS
    from src.cycle_keys import CYCLE_JSON_KEY_SET

    per_symbol = {
        "quotes",
        "signals",
        "earnings",
        "fundamentals",
        "tradability",
        "mc_check",
        "day_context",
    }

    rows: list[dict] = []
    for key, value in payload.items():
        if key not in CYCLE_JSON_KEY_SET:
            continue
        if key in DERIVED_KINDS:
            # `triggers` is the DIP scanner's survivors as far as the runner is
            # concerned; the core merges them with `cli.candidates`' own.
            if key == "triggers":
                rows.append({"kind": "dip_survivors", "symbol": None, "payload": value})
            continue
        if key in per_symbol and isinstance(value, Mapping):
            for symbol, entry in value.items():
                rows.append({"kind": key, "symbol": symbol, "payload": entry})
            continue
        rows.append({"kind": key, "symbol": None, "payload": value})
    return rows
