"""S0: the decision snapshot on the hosted path.

`src/snapshot.py` builds the rows and `tests/test_snapshot.py` covers what is
in them, against a recorded cycle. What is tested here is the plumbing: the
rows are written on every entry-capable cycle, after the decide runlog row,
and a write that fails is **visible and harmless** — named in the cycle's
status alert, the plan returned exactly as it would have been. Same contract,
and deliberately the same shape of test, as `test_run_deployment.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

from services.core.core import run as core
from services.core.tests.conftest import FakeSupabase, rows_from
from src import cli
from src import snapshot as snapshots
from tests.factories import cli_payload

CYCLE_ID = "2026-08-03T13:40:00Z-full"
WRITE = "edge_decision_snapshot_write"
SCHEMA = Path(__file__).resolve().parents[3] / "db" / "schema.sql"

PLACEMENT = {"order_id": "ord-1", "symbol": "PFE", "side": "buy", "quantity": 1, "play_id": "DIP"}


class BrokenSnapshotTable(FakeSupabase):
    """The schema was never applied: the one RPC raises, as PostgREST does for a
    function that does not exist."""

    def rpc(self, function, params=None):
        if function == WRITE:
            self.calls.append((function, dict(params or {})))
            raise RuntimeError("Could not find the function public.edge_decision_snapshot_write")
        return super().rpc(function, params)


def decided(payload=None, supabase_class=FakeSupabase):
    payload = cli_payload() if payload is None else payload
    supabase = supabase_class(tables={"edge_cycle_input": rows_from(payload, CYCLE_ID)})
    return core.run(CYCLE_ID, core.DECIDE, supabase), supabase


def decide_row(supabase) -> dict:
    (row,) = [p for p in supabase.args("edge_runlog_write") if p["p_command"] == "decide"]
    return row["p_stdout"]


def finalized(placements=(PLACEMENT,), supabase_class=FakeSupabase):
    """Decide for real, then finalize against the runlog row decide wrote —
    the snapshot rows finalize re-writes are the ones on that row."""
    _, first = decided()
    payload = cli_payload()
    rows = rows_from(payload, CYCLE_ID) + [
        {"kind": "placements", "symbol": None, "payload": list(placements)},
        {"kind": "placement_rejections", "symbol": None, "payload": []},
        {"kind": "placement_alerts", "symbol": None, "payload": []},
    ]
    supabase = supabase_class(
        tables={
            "edge_cycle_input": rows,
            "edge_runlog": [{"command": "decide", "stdout": decide_row(first)}],
            "edge_cycle_output": [
                {"cycle_id": CYCLE_ID, "status": "decided", "control_updates": {}}
            ],
        }
    )
    return core.run(CYCLE_ID, core.FINALIZE, supabase), supabase


def status_alert(supabase):
    alerts = [a for call in supabase.args("edge_alerts_enqueue") for a in call["p_alerts"]]
    (alert,) = [a for a in alerts if " cycle " in a["title"]]
    return alert


def pfe(rows):
    (row,) = [r for r in rows if r["symbol"] == "PFE" and r["play_id"] == "DIP"]
    return row


# --- decide ------------------------------------------------------------------


def test_decide_writes_the_snapshot_after_the_runlog_row():
    result, supabase = decided()

    assert result["status"] == core.STATUS_DECIDED
    decide_at = max(
        i for i, (name, params) in enumerate(supabase.calls)
        if name == "edge_runlog_write" and params["p_command"] == "decide"
    )
    assert supabase.names().index(WRITE) > decide_at        # Invariant 3 is untouched

    (write,) = supabase.args(WRITE)
    row = pfe(write["p_rows"])
    assert row["cycle_id"] == CYCLE_ID and row["trading_day"] == "2026-08-03"
    assert (row["verdict"], row["placed"]) == ("APPROVED", None)
    assert row["rsi"] == 27.0 and row["sector"] == "Health Care"


def test_the_rows_written_are_the_rows_on_the_runlog_row():
    """One derivation. `finalize` has no `day_context` and cannot rebuild the
    rows, so it reads them back from here; and a table write that fails still
    leaves the evidence in the permanent runlog."""
    _, supabase = decided()

    assert decide_row(supabase)["decision_snapshot"] == supabase.first(WRITE)["p_rows"]


def test_the_snapshot_reads_what_decide_read():
    """The runner posts no indicators; `run._fill_from_day_context` supplies
    them. The RSI on the row must be the one the play judged on — proved here
    by changing it and watching both the verdict and the row move together."""
    payload = cli_payload()
    payload["signals"]["PFE"]["rsi"] = 55.0

    result, supabase = decided(payload)

    assert [o for o in result["plan"] if o["side"] == "buy"] == []
    (row,) = [r for r in supabase.first(WRITE)["p_rows"] if r["symbol"] == "PFE"]
    assert row["rsi"] == 55.0
    # Not oversold, so DIP never triggered: no skip, and still a row.
    assert (row["verdict"], row["play_id"]) == ("UNTRIGGERED", "NONE")


def test_a_rejected_candidate_is_written_too():
    payload = cli_payload()
    payload["signals"]["PFE"]["rsi"] = 55.0

    _, supabase = decided(payload)

    rows = supabase.first(WRITE)["p_rows"]
    assert rows and snapshots.APPROVED not in {r["verdict"] for r in rows}
    assert "PFE" in {r["symbol"] for r in rows}


def test_a_cycle_that_cannot_buy_writes_no_snapshot():
    payload = cli_payload(
        now="2026-08-04T00:10:00Z", control={"last_full_cycle": "2026-08-03T13:40:00Z"}
    )

    result, supabase = decided(payload)

    assert result["status"] == core.STATUS_DECIDED
    assert supabase.args(WRITE) == []
    assert decide_row(supabase)["decision_snapshot"] == []


def test_a_failed_write_does_not_cost_the_cycle_its_plan():
    healthy, _ = decided()
    result, supabase = decided(supabase_class=BrokenSnapshotTable)

    assert result["status"] == core.STATUS_DECIDED
    assert result["plan"] == healthy["plan"]
    assert result["protection"] == healthy["protection"]
    assert len(supabase.args(WRITE)) == 1               # tried once, not in a loop
    # Everything after the snapshot write still happened.
    assert supabase.names()[-1] == "edge_cycle_output_write"
    assert supabase.names().index(WRITE) < len(supabase.names()) - 1


def test_the_snapshot_changes_nothing_decide_decided():
    """It is computed from `decide`'s output, after it. The plan a cycle
    returns is byte-for-byte what `cli.decide` alone produces."""
    result, _ = decided()
    alone = cli.decide(cli_payload(), cli.load_config())["plan"]

    assert result["plan"] == alone["orders"]
    assert result["cancels"] == alone["cancels"]


# --- finalize ----------------------------------------------------------------


def test_finalize_marks_the_approved_buy_with_what_the_broker_took():
    result, supabase = finalized()

    assert result["status"] == core.STATUS_FINALIZED
    assert pfe(supabase.first(WRITE)["p_rows"])["placed"] is True


def test_an_approved_buy_the_broker_did_not_take_is_placed_false():
    _, supabase = finalized(placements=())

    assert pfe(supabase.first(WRITE)["p_rows"])["placed"] is False


def test_finalize_keeps_the_features_decide_recorded():
    _, supabase = finalized()

    row = pfe(supabase.first(WRITE)["p_rows"])
    assert row["rsi"] == 27.0 and row["verdict"] == "APPROVED"


def test_a_failed_write_is_named_in_the_alert_and_does_not_abort_the_cycle():
    result, supabase = finalized(supabase_class=BrokenSnapshotTable)

    # The cycle finished: report written, control applied, status recorded.
    assert result["status"] == core.STATUS_FINALIZED
    commands = [p["p_command"] for p in supabase.args("edge_runlog_write")]
    assert commands == ["place", "report"]
    assert len(supabase.args("edge_control_apply")) == 1

    alert = status_alert(supabase)
    assert "Decision snapshot not written: RuntimeError" in alert["body"]
    assert alert["title"] == "Full cycle succeeded with warnings"
    assert supabase.first("edge_session_status_apply")["p_status"] == "success_with_warnings"


def test_a_healthy_write_says_nothing_and_leaves_the_status_alone():
    _, supabase = finalized()

    alert = status_alert(supabase)
    assert alert["title"] == "Full cycle succeeded"
    assert "Decision snapshot" not in alert["body"]


def test_the_alert_title_is_still_a_canonical_one():
    """`edge_alerts_enqueue` raises on a title it does not know, so the line
    rides inside the status alert rather than under a title of its own."""
    from src import report

    _, supabase = finalized(supabase_class=BrokenSnapshotTable)

    for call in supabase.args("edge_alerts_enqueue"):
        for alert in call["p_alerts"]:
            assert alert["title"] in report.CANONICAL_TITLES


# --- the schema --------------------------------------------------------------


def test_the_schema_defines_what_the_core_calls():
    sql = SCHEMA.read_text(encoding="utf-8")

    for signature in (
        r"edge_decision_snapshot_write\(p_rows jsonb\)",
        r"edge_decision_snapshot_pending\(p_limit integer default 4000\)",
        r"edge_decision_forward_write\(p_rows jsonb\)",
        r"edge_decision_snapshot_read\(p_from date, p_to date\)",
        r"edge_shadow_trades_write\(p_rows jsonb\)",
        r"edge_shadow_open\(p_days integer default 90\)",
        r"edge_shadow_read\(p_from date, p_to date\)",
        r"edge_evidence_dispatch\(\)",
    ):
        assert re.search(rf"create or replace function {signature}", sql), signature

    assert "create table if not exists edge_decision_snapshot" in sql
    assert "alter table edge_decision_snapshot enable row level security" in sql
    assert "primary key (cycle_id, symbol, play_id)" in sql
    assert "on conflict (cycle_id, symbol, play_id) do update" in sql
    # One transaction.
    assert len(re.findall(r"^begin;$", sql, re.M)) == 1
    assert len(re.findall(r"^commit;$", sql, re.M)) == 1


def test_no_evidence_function_is_callable_by_the_public_roles():
    sql = SCHEMA.read_text(encoding="utf-8")
    created = (
        "edge_decision_snapshot_write", "edge_decision_snapshot_pending",
        "edge_decision_forward_write", "edge_decision_snapshot_read",
        "edge_shadow_trades_write", "edge_shadow_trades_reap", "edge_shadow_open",
        "edge_shadow_read", "edge_evidence_dispatch",
    )

    for name in created:
        assert re.search(
            rf"revoke execute on function {name}\([^)]*\)\s+from anon, authenticated, public", sql
        ), name


def test_the_snapshot_upsert_cannot_erase_a_backfilled_return():
    """Finalize re-writes every row. If its `on conflict` touched the
    back-filled columns, the second cycle to see a name would wipe the first's
    forward returns."""
    sql = SCHEMA.read_text(encoding="utf-8")
    start = sql.index("create or replace function edge_decision_snapshot_write")
    body = sql[start : sql.index("$$;", start)]
    updates = body[body.index("do update") :]

    for column in ("price_features", "fwd", "fwd_status", "complete", "backfilled_at"):
        assert not re.search(rf"\b{column}\s*=", updates), column


def test_the_control_arm_survives_the_shadow_reap_and_keys_on_the_pair():
    sql = SCHEMA.read_text(encoding="utf-8")

    assert "primary key (variant, symbol, entry_day, pair_symbol)" in sql
    assert "on conflict (variant, symbol, entry_day, pair_symbol) do update" in sql
    start = sql.index("create or replace function edge_shadow_trades_reap")
    reap = sql[start : sql.index("$$;", start)]
    from services.core.core import shadow

    for variant in shadow.CONTROL_VARIANTS:
        assert f"'{variant}'" in reap, variant
