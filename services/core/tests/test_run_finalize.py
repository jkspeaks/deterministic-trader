"""Phase 2 — the writes that can only happen after the session has placed.

Step 8c's placement runlog row, Step 9's report and benchmark, Step 10's one
merged `edge_control_apply`. The thing worth testing here is not that `report`
works (`tests/test_report_*.py` does that) but that this phase reads phase 1's
answer back out of `edge_runlog` instead of deciding a second time — a second
`decide` against a book the session has since traded into would be a different
answer to the same question, which §8's single-decider rule forbids.
"""

from __future__ import annotations

from services.core.core import run as core
from services.core.tests.conftest import FakeSupabase, rows_from
from src import cli
from tests.factories import cli_payload

CYCLE_ID = "2026-08-03T13:40:00Z-full"

PLACEMENT = {
    "order_id": "ord-1",
    "symbol": "PFE",
    "side": "buy",
    "quantity": 1,
    "play_id": "DIP",
}


def finalized(placements=(PLACEMENT,), rejections=(), alerts=(), payload=None, decide_json=None):
    payload = cli_payload() if payload is None else payload
    decide_json = (
        cli.decide(payload, cli.load_config()) if decide_json is None else decide_json
    )
    rows = rows_from(payload, CYCLE_ID) + [
        {"kind": "placements", "symbol": None, "payload": list(placements)},
        {"kind": "placement_rejections", "symbol": None, "payload": list(rejections)},
        {"kind": "placement_alerts", "symbol": None, "payload": list(alerts)},
    ]
    supabase = FakeSupabase(
        tables={
            "edge_cycle_input": rows,
            "edge_runlog": [{"command": "decide", "stdout": decide_json}],
            "edge_cycle_output": [
                {"cycle_id": CYCLE_ID, "status": "decided", "control_updates": {"unreconciled": True}}
            ],
        }
    )
    return core.run(CYCLE_ID, core.FINALIZE, supabase), supabase


def test_finalize_writes_the_place_row_then_the_report_row():
    result, supabase = finalized()

    assert result["status"] == core.STATUS_FINALIZED
    commands = [p["p_command"] for p in supabase.args("edge_runlog_write")]
    assert commands == ["place", "report"]


def test_the_place_row_carries_the_shaped_placements_not_the_raw_ones():
    _, supabase = finalized()

    row = supabase.args("edge_runlog_write")[0]
    placed = row["p_placements"][0]
    # `cli.shape_placements` defaults the two protection flags and coerces the
    # numbers; a raw row has neither.
    assert placed["symbol"] == "PFE"
    assert placed["protective_stop"] is False
    assert placed["protective_target"] is False


def test_a_structurally_invalid_placement_fails_the_cycle_rather_than_being_shaped_around():
    result, supabase = finalized(placements=({"symbol": "PFE"},))

    assert result["status"] == core.STATUS_FAILED
    assert "shape-placements" in result["detail"]
    # Nothing was written to the runlog under a half-shaped bundle.
    assert supabase.args("edge_runlog_write") == []


def test_control_is_applied_once_with_phase_1s_merge_underneath_reports():
    _, supabase = finalized()

    applied = supabase.args("edge_control_apply")
    assert len(applied) == 1, "Step 10 applies the merge exactly once"
    # Phase 1's accumulation (candidates, reconcile, decide) survived the
    # process boundary via `edge_cycle_output.control_updates`.
    assert applied[0]["p_updates"]["unreconciled"] is True


def test_decides_own_alerts_and_the_runners_placement_alerts_are_enqueued_together():
    runner_alert = {"title": "Order refused", "body": "MGM: instrument not found", "priority": "high"}
    _, supabase = finalized(alerts=(runner_alert,))

    enqueued = supabase.first("edge_alerts_enqueue")["p_alerts"]
    assert runner_alert in enqueued


def test_a_code_detail_runner_alert_never_reaches_the_enqueue_and_lands_in_the_report_row():
    note = {"code": "fetch_row_skipped", "detail": "step=regime skipped"}
    result, supabase = finalized(alerts=(note,))

    assert result["status"] == core.STATUS_FINALIZED
    for call in supabase.args("edge_alerts_enqueue"):
        assert all(a.get("title") and a.get("body") for a in call["p_alerts"])
    report_row = supabase.args("edge_runlog_write")[1]
    assert report_row["p_stdout"]["runner_notes"] == [note]


def test_a_non_postclose_cycle_writes_no_benchmark_row():
    _, supabase = finalized()

    assert supabase.args("edge_benchmark_upsert") == []
    assert supabase.args("edge_fill_quality_recent") == []
    # The shadow ledger is read on the same cycle and for the same reason:
    # both feed the scorecard, which only a postclose can write.
    assert supabase.args("edge_shadow_recent") == []
    assert supabase.args("edge_realized_trades_recent") == []


def test_a_postclose_reads_both_weekly_series_for_the_scorecard():
    # 20:30 UTC is 16:30 ET — the postclose slot.
    payload = cli_payload(now="2026-08-03T20:30:00Z")
    _, supabase = finalized(payload=payload, placements=())

    assert supabase.first("edge_fill_quality_recent")["p_days"] == 7
    assert supabase.first("edge_shadow_recent")["p_days"] == 7
    assert supabase.first("edge_realized_trades_recent")["p_days"] == 7


def test_finalize_never_calls_decide_again():
    _, supabase = finalized()

    # The tell would be a second `decide` runlog row. There is exactly one
    # write path to that command and it is phase 1's.
    assert "decide" not in [p["p_command"] for p in supabase.args("edge_runlog_write")]


# ---------------------------------------------------------------------------
# `edge_session_stats.status` — the business-outcome word
# ---------------------------------------------------------------------------
#
# The finding this closes: an earlier session narrated "Completed
# Successfully" with three of three sells rejected. These tests assert the
# derived word, not the narration, is what lands in the database.


def test_a_clean_cycle_with_no_rejections_or_blocks_is_success():
    _, supabase = finalized()

    call = supabase.first("edge_session_status_apply")
    assert call["p_cycle_id"] == CYCLE_ID
    assert call["p_status"] == "success"


def test_a_rejection_alongside_a_successful_placement_is_partial_success():
    rejection = {"symbol": "MGM", "side": "sell", "type": "limit", "quantity": 6, "detail": "Not enough shares to sell."}
    _, supabase = finalized(rejections=(rejection,))

    assert supabase.first("edge_session_status_apply")["p_status"] == "partial_success"


def test_every_order_rejected_and_nothing_placed_is_blocked():
    rejection = {"symbol": "MGM", "side": "sell", "type": "limit", "quantity": 6, "detail": "Not enough shares to sell."}
    _, supabase = finalized(placements=(), rejections=(rejection,))

    assert supabase.first("edge_session_status_apply")["p_status"] == "blocked"


def test_a_persistent_divergence_finding_is_blocked_even_with_no_rejections():
    payload = cli_payload()
    rows = rows_from(payload, CYCLE_ID) + [
        {"kind": "placements", "symbol": None, "payload": []},
        {"kind": "placement_rejections", "symbol": None, "payload": []},
        {"kind": "placement_alerts", "symbol": None, "payload": []},
    ]
    supabase = FakeSupabase(
        tables={
            "edge_cycle_input": rows,
            "edge_runlog": [
                {"command": "decide", "stdout": cli.decide(payload, cli.load_config())},
                {
                    "command": "reconcile",
                    "stdout": {
                        "findings": [
                            {
                                "code": "RECONCILIATION_DIVERGENCE_PERSISTENT",
                                "symbol": "MGM",
                                "detail": "still diverged after 2 cycles",
                                "order_id": None,
                            }
                        ]
                    },
                },
            ],
            "edge_cycle_output": [
                {"cycle_id": CYCLE_ID, "status": "decided", "control_updates": {}}
            ],
        }
    )

    core.run(CYCLE_ID, core.FINALIZE, supabase)

    assert supabase.first("edge_session_status_apply")["p_status"] == "blocked"


def test_manual_required_with_no_rejections_or_blocks_is_success_with_warnings():
    decide_json = cli.decide(cli_payload(), cli.load_config())
    decide_json = {**decide_json, "manual_required": [{"symbol": "PFE", "reason": "fractional"}]}
    _, supabase = finalized(decide_json=decide_json)

    assert supabase.first("edge_session_status_apply")["p_status"] == "success_with_warnings"


def test_a_stale_data_source_with_no_other_problem_is_success_with_warnings():
    # `_decide_phase` writes
    # `data_provenance` into the same decide runlog row `_finalize_phase`
    # reads back -- this is that row, hand-built the way it would arrive.
    decide_json = cli.decide(cli_payload(), cli.load_config())
    decide_json = {
        **decide_json,
        "data_provenance": {
            "macro": {"source": "supabase", "as_of": "2026-07-01", "age_trading_days": 5}
        },
    }
    _, supabase = finalized(decide_json=decide_json)

    assert supabase.first("edge_session_status_apply")["p_status"] == "success_with_warnings"


def test_data_only_two_trading_days_old_is_not_stale():
    decide_json = cli.decide(cli_payload(), cli.load_config())
    decide_json = {
        **decide_json,
        "data_provenance": {
            "macro": {"source": "checkout", "as_of": "2026-08-01", "age_trading_days": 2}
        },
    }
    _, supabase = finalized(decide_json=decide_json)

    assert supabase.first("edge_session_status_apply")["p_status"] == "success"


def test_the_weekly_snapshot_is_not_stale_five_sessions_in():
    # The Saturday snapshot read 5 sessions old on Thursday and put every cycle
    # Wednesday-Friday at "succeeded with warnings".
    decide_json = cli.decide(cli_payload(), cli.load_config())
    decide_json = {
        **decide_json,
        "data_provenance": {
            "snapshot": {"source": "supabase", "as_of": "2026-09-24", "age_trading_days": 5}
        },
    }
    _, supabase = finalized(decide_json=decide_json)

    assert supabase.first("edge_session_status_apply")["p_status"] == "success"


def test_a_snapshot_past_its_fourteen_days_is_stale_and_named_in_the_alert():
    decide_json = cli.decide(cli_payload(), cli.load_config())
    decide_json = {
        **decide_json,
        "data_provenance": {
            "snapshot": {"source": "supabase", "as_of": "2026-09-10", "age_trading_days": 11}
        },
    }
    _, supabase = finalized(decide_json=decide_json)

    assert supabase.first("edge_session_status_apply")["p_status"] == "success_with_warnings"
    body = supabase.first("edge_alerts_enqueue")["p_alerts"][0]["body"]
    assert "Stale data source(s): snapshot." in body


def test_a_sell_guard_denied_is_blocked_and_named_in_the_alert():
    decide_json = cli.decide(cli_payload(), cli.load_config())
    decide_json = {
        **decide_json,
        "guard": {
            "denied": [
                {
                    "order": {"side": "sell", "symbol": "INVH", "quantity": 7.0},
                    "denials": [
                        {"code": "PROTECTION_CONTENDS", "message": "0 of 7 shares available"}
                    ],
                },
                {
                    "order": {"side": "buy", "symbol": "PFE", "quantity": 1.0},
                    "denials": [{"code": "ENTRY_X", "message": "not a sell"}],
                },
            ]
        },
    }
    _, supabase = finalized(placements=(), decide_json=decide_json)

    assert supabase.first("edge_session_status_apply")["p_status"] == "blocked"
    body = supabase.first("edge_alerts_enqueue")["p_alerts"][0]["body"]
    assert "INVH sell x7.0 denied by guard (PROTECTION_CONTENDS)" in body
    assert "PFE" not in body


# ---------------------------------------------------------------------------
# `finalize` must not re-fetch what `report()` never reads
# ---------------------------------------------------------------------------
#
# `cli.report()`'s ctx dict reads `play_state`/`prior_play_state`
# (`edge_play_state_json`) and nothing else out of `_SUPABASE_READS`/
# `_ENTRY_SUPABASE_READS`/`_LOSS_CLOSES_READ`, and none of `CHECKOUT_DATA`
# (`data_provenance` comes back off the `decide` runlog row, never
# recomputed). A regression here is silent — the extra RPCs and
# `edge_data_files` reads answer with whatever the fake hands back and get
# quietly discarded, so nothing about the plan/report output would change.
# The only way to catch it is asserting on what was *called*.

# Every RPC `_SUPABASE_READS`/`_ENTRY_SUPABASE_READS`/`_LOSS_CLOSES_READ`
# name, minus the one `finalize` is allowed: `edge_play_state_json`.
_UNNEEDED_FINALIZE_RPCS = (
    "edge_prior",
    "edge_symbol_lock_json",
    "edge_dip_recovery_positions_json",
    "edge_day_context_recent",
    "edge_earnings_reactions_recent",
    "edge_loss_closes_recent",
)


def test_finalize_calls_none_of_the_rpcs_report_never_reads():
    _, supabase = finalized()

    called = set(supabase.names())
    wasted = called & set(_UNNEEDED_FINALIZE_RPCS)
    assert not wasted, f"finalize called RPCs report() never reads: {wasted}"


def test_finalize_reads_no_checkout_data_files():
    _, supabase = finalized()

    checkout_reads = [table for table, _ in supabase.reads if table == "edge_data_files"]
    assert checkout_reads == [], "finalize re-fetched CHECKOUT_DATA report() never reads"


def test_finalize_still_reads_play_state_for_report():
    # The one RPC that must survive the cut — report does read it. Built
    # without a posted `play_state`/`prior_play_state` row (the runner never
    # posts either), unlike `finalized()`'s default `cli_payload()`, which
    # carries both for the *local* `cli.decide` contract and would let a
    # "presence wins" skip hide a broken RPC call.
    payload = cli_payload()
    payload.pop("play_state", None)
    payload.pop("prior_play_state", None)
    decide_json = cli.decide(cli_payload(), cli.load_config())
    rows = [
        row
        for row in rows_from(payload, CYCLE_ID)
        if row["kind"] not in ("play_state", "prior_play_state")
    ] + [
        {"kind": "placements", "symbol": None, "payload": [PLACEMENT]},
        {"kind": "placement_rejections", "symbol": None, "payload": []},
        {"kind": "placement_alerts", "symbol": None, "payload": []},
    ]
    play_state_answer = {"DIP": {"regime_fit": 1.0}}
    supabase = FakeSupabase(
        tables={
            "edge_cycle_input": rows,
            "edge_runlog": [{"command": "decide", "stdout": decide_json}],
            "edge_cycle_output": [
                {"cycle_id": CYCLE_ID, "status": "decided", "control_updates": {}}
            ],
        },
        returns={"edge_play_state_json": play_state_answer},
    )

    core.run(CYCLE_ID, core.FINALIZE, supabase)

    play_state_calls = supabase.args("edge_play_state_json")
    assert len(play_state_calls) == 1, "play_state/prior_play_state share one RPC call"


def test_a_rejection_outranks_a_stale_data_source():
    rejection = {"symbol": "MGM", "side": "sell", "type": "limit", "quantity": 6, "detail": "Not enough shares to sell."}
    decide_json = cli.decide(cli_payload(), cli.load_config())
    decide_json = {
        **decide_json,
        "data_provenance": {
            "macro": {"source": "supabase", "as_of": "2026-07-01", "age_trading_days": 5}
        },
    }
    _, supabase = finalized(rejections=(rejection,), decide_json=decide_json)

    assert supabase.first("edge_session_status_apply")["p_status"] == "partial_success"


# ---------------------------------------------------------------------------
# The alert can no longer disagree with `edge_session_stats`
# ---------------------------------------------------------------------------
#
# Before this prompt, `_session_status` (this file) and `report._cycle_status`
# derived the outcome word independently, from different precedence rules
# over the same findings. A bare `PHANTOM_ORDER` (no high-priority `blocked`
# row) was the concrete, reproducible case: the database wrote `blocked`
# while the pushed alert read "succeeded with warnings". These tests assert
# both are read off the *same* value now (`run._session_status`, passed into
# `cli.report()` as `ctx["session_status"]`), not just aligned precedences
# that could drift apart again.


def test_a_bare_phantom_order_no_longer_disagrees_with_the_alert():
    payload = cli_payload()
    rows = rows_from(payload, CYCLE_ID) + [
        {"kind": "placements", "symbol": None, "payload": []},
        {"kind": "placement_rejections", "symbol": None, "payload": []},
        {"kind": "placement_alerts", "symbol": None, "payload": []},
    ]
    supabase = FakeSupabase(
        tables={
            "edge_cycle_input": rows,
            "edge_runlog": [
                {"command": "decide", "stdout": cli.decide(payload, cli.load_config())},
                {
                    "command": "reconcile",
                    "stdout": {
                        "findings": [
                            {
                                "code": "PHANTOM_ORDER",
                                "symbol": "MRK",
                                "detail": "no plan proposed this order",
                                "order_id": "zz99",
                            }
                        ]
                    },
                },
            ],
            "edge_cycle_output": [
                {"cycle_id": CYCLE_ID, "status": "decided", "control_updates": {}}
            ],
        }
    )

    core.run(CYCLE_ID, core.FINALIZE, supabase)

    db_status = supabase.first("edge_session_status_apply")["p_status"]
    alert_title = supabase.first("edge_alerts_enqueue")["p_alerts"][0]["title"]

    assert db_status == "blocked"
    # The bug this closes: this used to read "... succeeded with warnings".
    assert alert_title == "Full cycle blocked"


def test_every_order_rejected_is_blocked_in_both_places():
    rejection = {
        "symbol": "MGM", "side": "sell", "type": "limit",
        "quantity": 6, "detail": "Not enough shares to sell.",
    }
    _, supabase = finalized(placements=(), rejections=(rejection,))

    db_status = supabase.first("edge_session_status_apply")["p_status"]
    alert_title = supabase.first("edge_alerts_enqueue")["p_alerts"][0]["title"]

    assert db_status == "blocked"
    assert alert_title == "Full cycle blocked"


def test_a_normal_no_candidates_cycle_still_reads_success_in_both_places():
    _, supabase = finalized(placements=())

    db_status = supabase.first("edge_session_status_apply")["p_status"]
    alert_title = supabase.first("edge_alerts_enqueue")["p_alerts"][0]["title"]

    assert db_status == "success"
    assert alert_title == "Full cycle succeeded"


def test_a_noop_cycle_reports_skipped_not_a_false_success():
    # A Saturday: no equity session, same NOOP `cadence.decide` gives
    # `_decide_phase`/`_resolve_phase` for the identical `now`.
    payload = cli_payload(now="2026-08-08T13:40:00Z")
    _, supabase = finalized(payload=payload, placements=())

    assert supabase.first("edge_session_status_apply")["p_status"] == "skipped"
    alert_title = supabase.first("edge_alerts_enqueue")["p_alerts"][0]["title"]
    assert "skipped" in alert_title
