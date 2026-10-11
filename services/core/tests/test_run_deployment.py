"""P3: the deployment record on the hosted path.

`src/deployment.py` builds the row and `tests/test_deployment.py` covers what
is in it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from services.core.core import run as core
from services.core.tests.conftest import FakeSupabase, rows_from
from src import cli
from tests.factories import cli_payload, regime_inputs

CYCLE_ID = "2026-08-03T13:40:00Z-full"
WRITE = "edge_deployment_decision_write"
SCHEMA = Path(__file__).resolve().parents[3] / "db" / "schema.sql"

PLACEMENT = {"order_id": "ord-1", "symbol": "PFE", "side": "buy", "quantity": 1, "play_id": "DIP"}


# These tests predate an earlier retirement and shelving of the event plays
# and use them as they then ran. See `all_plays_live` in conftest.py.
pytestmark = pytest.mark.usefixtures("all_plays_live")


class BrokenDeploymentTable(FakeSupabase):
    """The migration was never applied: the one RPC raises, as PostgREST does
    for a function that does not exist."""

    def rpc(self, function, params=None):
        if function == WRITE:
            self.calls.append((function, dict(params or {})))
            raise RuntimeError("Could not find the function public.edge_deployment_decision_write")
        return super().rpc(function, params)


def decided(payload=None, supabase_class=FakeSupabase):
    payload = cli_payload() if payload is None else payload
    supabase = supabase_class(tables={"edge_cycle_input": rows_from(payload, CYCLE_ID)})
    return core.run(CYCLE_ID, core.DECIDE, supabase), supabase


def finalized(placements=(PLACEMENT,), rejections=(), payload=None, supabase_class=FakeSupabase):
    payload = cli_payload() if payload is None else payload
    decide_json = cli.decide(payload, cli.load_config())
    rows = rows_from(payload, CYCLE_ID) + [
        {"kind": "placements", "symbol": None, "payload": list(placements)},
        {"kind": "placement_rejections", "symbol": None, "payload": list(rejections)},
        {"kind": "placement_alerts", "symbol": None, "payload": []},
    ]
    supabase = supabase_class(
        tables={
            "edge_cycle_input": rows,
            "edge_runlog": [{"command": "decide", "stdout": decide_json}],
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


# --- decide ------------------------------------------------------------------


def test_decide_writes_the_row_after_the_runlog_row():
    result, supabase = decided()

    assert result["status"] == core.STATUS_DECIDED
    names = supabase.names()
    decide_at = max(
        i for i, (name, params) in enumerate(supabase.calls)
        if name == "edge_runlog_write" and params["p_command"] == "decide"
    )
    assert names.index(WRITE) > decide_at            # Invariant 3 is untouched

    (write,) = supabase.args(WRITE)
    row = write["p_row"]
    assert row["cycle_id"] == CYCLE_ID and row["trading_day"] == "2026-08-03"
    assert (row["decision"], row["reason"]) == ("DEPLOY", "DEPLOYED")
    assert row["funnel"]["approved"] == 1 and row["placed"] is None


def test_a_cycle_with_no_proposals_still_writes_a_row():
    payload = cli_payload()
    payload["signals"]["PFE"]["rsi"] = 55.0

    result, supabase = decided(payload)

    assert [o for o in result["plan"] if o["side"] == "buy"] == []
    row = supabase.first(WRITE)["p_row"]
    assert (row["decision"], row["reason"], row["intended"]) == ("NONE", "NO_CANDIDATES", True)


def test_the_funnel_counts_what_discovery_dropped():
    """Step 5's drop list reaches `decide`, so `discovered` is larger than the
    pool when the regime took a sector's names out before the fetch."""
    payload = cli_payload(
        regime_inputs=regime_inputs(sectors={"XLK": {"rsi14": 74.0}}),
    )
    payload["events"] = {"AAPL": [{"type": "8-K", "items": ["2.02"], "sessions_ago": 1}]}
    payload["triggers"] = []

    _, supabase = decided(payload)

    row = supabase.first(WRITE)["p_row"]
    assert row["funnel"]["discovered"] > row["funnel"]["pool"]


def test_a_cycle_that_cannot_buy_writes_no_row():
    payload = cli_payload(
        now="2026-08-04T00:10:00Z", control={"last_full_cycle": "2026-08-03T13:40:00Z"}
    )

    result, supabase = decided(payload)

    assert result["status"] == core.STATUS_DECIDED
    assert supabase.args(WRITE) == []


def test_a_failed_write_does_not_cost_the_cycle_its_plan():
    healthy, _ = decided()
    result, supabase = decided(supabase_class=BrokenDeploymentTable)

    assert result["status"] == core.STATUS_DECIDED
    assert result["plan"] == healthy["plan"]
    assert result["protection"] == healthy["protection"]
    assert len(supabase.args(WRITE)) == 1            # tried once, not retried in a loop


# --- finalize ----------------------------------------------------------------


def test_finalize_completes_the_row_with_what_was_placed():
    result, supabase = finalized()

    assert result["status"] == core.STATUS_FINALIZED
    row = supabase.first(WRITE)["p_row"]
    assert row["placed"] == 1 and row["funnel"]["placed"] == 1
    assert (row["decision"], row["reason"]) == ("DEPLOY", "DEPLOYED")


def test_an_approved_buy_the_broker_refused_is_recorded_as_an_exec_fail():
    rejection = {"symbol": "PFE", "side": "buy", "type": "limit", "quantity": 1,
                 "detail": "Not enough buying power."}

    _, supabase = finalized(placements=(), rejections=(rejection,))

    row = supabase.first(WRITE)["p_row"]
    assert (row["decision"], row["reason"], row["intended"]) == ("NONE", "EXEC_FAIL", False)


def test_the_status_alert_carries_the_deployment_line():
    _, supabase = finalized()

    body = status_alert(supabase)["body"]
    assert body.count("Deployment:") == 1
    assert "DEPLOY (DEPLOYED)" in body and "funnel 20 > 2 > 1 > 1 > 1 > 1 > 1" in body


def test_a_failed_write_is_visible_and_does_not_abort_the_cycle():
    result, supabase = finalized(supabase_class=BrokenDeploymentTable)

    # The cycle finished: report written, control applied, status recorded.
    assert result["status"] == core.STATUS_FINALIZED
    commands = [p["p_command"] for p in supabase.args("edge_runlog_write")]
    assert commands == ["place", "report"]
    assert len(supabase.args("edge_control_apply")) == 1

    alert = status_alert(supabase)
    assert "Deployment record not written: RuntimeError" in alert["body"]
    assert alert["title"] == "Full cycle succeeded with warnings"
    assert supabase.first("edge_session_status_apply")["p_status"] == "success_with_warnings"


def test_a_healthy_write_leaves_the_status_alone():
    _, supabase = finalized()

    assert status_alert(supabase)["title"] == "Full cycle succeeded"


def test_the_alert_title_is_still_a_canonical_one():
    """`edge_alerts_enqueue` raises on a title it does not know, so the new
    lines ride inside the status alert rather than under a title of their own."""
    from src import report

    _, supabase = finalized(supabase_class=BrokenDeploymentTable)

    for call in supabase.args("edge_alerts_enqueue"):
        for alert in call["p_alerts"]:
            assert alert["title"] in report.CANONICAL_TITLES


# --- the schema --------------------------------------------------------------


def test_the_schema_defines_what_the_core_calls():
    sql = SCHEMA.read_text(encoding="utf-8")

    assert re.search(rf"create or replace function {WRITE}\(p_row jsonb\)", sql)
    assert "create table if not exists edge_deployment_decision" in sql
    assert "alter table edge_deployment_decision enable row level security" in sql
    assert f"revoke execute on function {WRITE}(jsonb) from anon, authenticated, public" in sql
    assert "on conflict (cycle_id) do update" in sql
    # One transaction.
    assert len(re.findall(r"^begin;$", sql, re.M)) == 1
    assert len(re.findall(r"^commit;$", sql, re.M)) == 1


def test_every_column_the_write_reads_is_a_key_the_row_carries():
    sql = SCHEMA.read_text(encoding="utf-8")
    start = sql.index(f"create or replace function {WRITE}(")
    sql = sql[start : sql.index("$$;", start)]
    row = cli.decide(cli_payload(), cli.load_config())["deployment"]

    read = set(re.findall(r"p_row->>?'([a-z_0-9]+)'", sql))
    assert read and read <= set(row), sorted(read - set(row))


@pytest.mark.parametrize("decision", ["DEPLOY", "PARTIAL", "NONE"])
def test_the_check_constraint_knows_every_decision(decision):
    from src import deployment

    assert decision in deployment.DECISIONS
    assert f"'{decision}'" in SCHEMA.read_text(encoding="utf-8")
