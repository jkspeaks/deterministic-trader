"""The hosted core against the same payload `cli.decide` is tested with.

The acceptance question for this whole service is one sentence: does a cycle
run server-side produce the plan the cycle would have produced when a live
session ran `uv run python -m src.cli decide < cycle.json`? Everything else
here is about the fourteen Supabase writes — that they happen, in the prompt's
order, with the prompt's arguments, and that a failure produces no plan at all
rather than a partial one.
"""

from __future__ import annotations

import pytest

from services.core.core import run as core
from services.core.core.run import CycleError
from services.core.tests.conftest import FakeSupabase, rows_from
from src import cli
from tests.factories import cli_payload

CYCLE_ID = "2026-08-03T13:40:00Z-full"

# The `cycle.json` reads `run._payload` makes for itself. They go through
# `rpc` like every write does, so any test asserting on the *write*
# sequence has to filter them out.
READ_RPCS = {
    function
    for _, function, _ in core._SUPABASE_READS + core._ENTRY_SUPABASE_READS + (core._LOSS_CLOSES_READ,)
}


def driven(payload=None, returns=None, phase=core.DECIDE, extra_rows=(), runlog=()):
    payload = cli_payload() if payload is None else payload
    supabase = FakeSupabase(
        tables={
            "edge_cycle_input": rows_from(payload, CYCLE_ID) + list(extra_rows),
            "edge_runlog": list(runlog),
        },
        returns=returns,
    )
    return core.run(CYCLE_ID, phase, supabase), supabase


def local_decide(payload, loaded):
    """What a live session's `decide` step actually consumes, reproduced
    without the hosted core.

    `_decide_phase` never trusts `payload.reconcile` as posted — `reconcile`
    is one of `assemble.DERIVED_KINDS`, so the runner cannot post it at all,
    and on a reconciling cycle `_decide_phase` always calls `cli.reconcile`
    fresh against `loaded` and overwrites `payload["reconcile"]` with its
    output before calling `cli.decide`. `cli_payload()`'s own canned
    `reconcile.protection_plan` is fixed test input for `decide`-level tests
    that do not care where it came from (`tests/test_cli_protection.py`,
    `test_cli_golden.py`) — it is not a claim about what a fresh
    `cli.reconcile` call would produce today, so comparing against it
    directly here would be comparing the hosted core against test data
    instead of against the local pipeline it is meant to match.
    """
    payload = {**payload, "reconcile": cli.reconcile(payload, loaded)}
    return cli.decide(payload, loaded)


# ---------------------------------------------------------------------------
# The acceptance criterion
# ---------------------------------------------------------------------------


def test_the_hosted_plan_is_the_plan_cli_decide_returns_for_the_same_payload():
    payload = cli_payload()
    local = local_decide(payload, cli.load_config())["plan"]

    result, _ = driven(payload)

    assert result["status"] == core.STATUS_DECIDED
    assert result["plan"] == local["orders"]
    assert result["protection"] == local["protection"]
    assert result["cancels"] == local["cancels"]


def test_the_decide_runlog_row_carries_decide_json_whole():
    payload = cli_payload()
    local = local_decide(payload, cli.load_config())

    _, supabase = driven(payload)

    written = [
        params
        for params in supabase.args("edge_runlog_write")
        if params["p_command"] == "decide"
    ]
    assert len(written) == 1
    # Not a summary, not a length -- the whole document, the same rule Step 7
    # states for the hand-pasted version.
    assert written[0]["p_stdout"]["plan"] == local["plan"]
    assert written[0]["p_stdout"]["guard"] == local["guard"]


# ---------------------------------------------------------------------------
# The writes, in the prompt's order
# ---------------------------------------------------------------------------


def test_step_7_writes_reconcile_before_decide_and_in_the_prompts_order():
    _, supabase = driven()

    # They are reads and carry no ordering promise; the writes below are the
    # ones Step 7 fixes the order of.
    names = [name for name in supabase.names() if name not in READ_RPCS]
    reconcile_writes = [
        "edge_runlog_write",
        "edge_control_apply",
        "edge_loss_closes_append",
        "edge_realized_trades_append",
        "edge_play_state_apply",
        "edge_fill_quality_write",
    ]
    assert names[: len(reconcile_writes)] == reconcile_writes

    # Invariant 3: the decide runlog row lands before the caller can place.
    commands = [p["p_command"] for p in supabase.args("edge_runlog_write")]
    assert commands == ["reconcile", "decide"]


def test_the_reconcile_runlog_row_carries_the_book_the_prompt_sends():
    _, supabase = driven()

    row = supabase.args("edge_runlog_write")[0]
    assert set(row["p_book"]) == {"trading_day", "positions"}
    assert row["p_book"]["trading_day"] == row["p_trading_day"]


def test_reconciles_desired_protection_is_persisted_to_edge_protection():
    """`edge_protection_apply` gets `protection_plan.desired` verbatim, the
    same `src/plays/protection.desired` output the plan's own `protection`
    key is built from — so `watch.py` never sees a different answer than
    the cycle that computed it.
    """
    result, supabase = driven()

    reconcile_row = supabase.args("edge_runlog_write")[0]
    desired = reconcile_row["p_stdout"]["protection_plan"]["desired"]

    call = supabase.first("edge_protection_apply")
    assert call["p_orders"] == desired


def test_a_real_cycle_clears_needs_session():
    """A cycle that reaches decide has looked, honestly, at whatever `watch.py`
    flagged — clear it here, not in `watch.py` itself, so only a session that
    actually ran can say the state change was addressed.
    """
    result, _ = driven()

    assert result["control_updates"]["needs_session"] is None


def test_a_refused_symbol_lock_alerts_and_does_not_stop_the_cycle():
    result, supabase = driven(returns={"edge_symbol_lock_claim": False})

    assert result["status"] == core.STATUS_DECIDED
    alerts = supabase.args("edge_alerts_enqueue")
    assert alerts, "a lock owned by the other strategy must surface"
    assert "Symbol lock refused" in alerts[0]["p_alerts"][0]["title"]


def test_a_granted_symbol_lock_raises_no_alert():
    _, supabase = driven(returns={"edge_symbol_lock_claim": True})

    assert supabase.args("edge_alerts_enqueue") == []


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------


def test_a_cycle_with_no_control_row_plans_nothing_and_records_why():
    payload = cli_payload()
    rows = [r for r in rows_from(payload, CYCLE_ID) if r["kind"] != "control"]
    supabase = FakeSupabase(tables={"edge_cycle_input": rows})

    result = core.run(CYCLE_ID, core.DECIDE, supabase)

    assert result["status"] == core.STATUS_FAILED
    assert "control" in result["detail"]
    assert "plan" not in result
    # The failure is durable: the runner reads `status`, not this return value.
    stored = supabase.first("edge_cycle_output_write")["p_fields"]
    assert stored["status"] == core.STATUS_FAILED


def test_rows_claiming_a_different_cycle_id_are_refused():
    payload = cli_payload()
    rows = rows_from(payload, CYCLE_ID) + [
        {"kind": "cycle_id", "symbol": None, "payload": "someone-elses-cycle"}
    ]
    supabase = FakeSupabase(tables={"edge_cycle_input": rows})

    result = core.run(CYCLE_ID, core.DECIDE, supabase)

    assert result["status"] == core.STATUS_FAILED
    assert "someone-elses-cycle" in result["detail"]


def test_a_write_that_fails_takes_the_whole_cycle_with_it():
    class Exploding(FakeSupabase):
        def rpc(self, function, params=None):
            if function == "edge_control_apply":
                raise RuntimeError("PostgREST said 503")
            return super().rpc(function, params)

    payload = cli_payload()
    supabase = Exploding(tables={"edge_cycle_input": rows_from(payload, CYCLE_ID)})

    result = core.run(CYCLE_ID, core.DECIDE, supabase)

    assert result["status"] == core.STATUS_FAILED
    assert "503" in result["detail"]
    # Nothing to place -- which is the point. A half-written cycle that still
    # handed back orders is the failure mode this shape exists to remove.
    assert "plan" not in result


def test_an_unknown_phase_is_refused_before_anything_is_read():
    supabase = FakeSupabase()

    with pytest.raises(CycleError):
        core.run(CYCLE_ID, "improvise", supabase)

    assert supabase.calls == []


# ---------------------------------------------------------------------------
# The three CI-published files, which the runner cannot supply
# ---------------------------------------------------------------------------
#
# `snapshot`/`events`/`macro` carry `source: "checkout"` on their fetch rows,
# and the thin runner has no checkout -- no clone, no `data/`. They ship with
# the deployment instead and the core fills them, the same channel
# `cli.load_config` already uses for `config/`.


def _without(payload, *keys):
    return {k: v for k, v in payload.items() if k not in keys}


def test_the_core_fills_the_checkout_files_the_runner_cannot_post():
    payload = _without(cli_payload(), "snapshot", "events", "macro")
    supabase = FakeSupabase(tables={"edge_cycle_input": rows_from(payload, CYCLE_ID)})

    result = core.run(CYCLE_ID, core.DECIDE, supabase)

    # It got far enough to decide, which it could not have with no universe
    # snapshot: `universe.resolve` would have had nothing to fence against.
    assert result["status"] == core.STATUS_DECIDED


def test_events_is_unwrapped_but_macro_and_snapshot_are_not():
    # The asymmetry is `data/events.json`'s: every consumer in `src/` reads
    # `events` as `{symbol: [rows]}`, which is that file's `events` key, while
    # `macro`/`snapshot` are read at their top level. Handing the whole events
    # file through makes `candidates.assemble` iterate `as_of` as a symbol.
    import json
    from pathlib import Path

    from services.core.core.run import CHECKOUT_DATA, DATA_DIR, _payload

    payload, _ = _payload(
        CYCLE_ID,
        FakeSupabase(
            tables={
                "edge_cycle_input": rows_from(
                    _without(cli_payload(), "snapshot", "events", "macro"), CYCLE_ID
                )
            }
        ),
        cli.load_config(),
    )

    raw_events = json.loads((DATA_DIR / "events.json").read_text(encoding="utf-8"))
    assert payload["events"] == (raw_events.get("events") or {})
    assert "as_of" not in payload["events"], "the wrapper leaked through"

    raw_macro = json.loads((DATA_DIR / "macro.json").read_text(encoding="utf-8"))
    assert payload["macro"] == raw_macro
    assert "releases" in payload["macro"]

    assert set(CHECKOUT_DATA) == {"snapshot", "events", "macro"}
    assert all((DATA_DIR / name).exists() for name, _ in CHECKOUT_DATA.values()), (
        "a data file the deployment ships is missing -- check .vercelignore "
        "still keeps data/ in the bundle"
    )


# ---------------------------------------------------------------------------
# `edge_data_files` — the Supabase mirror, read before the bundled file
# ---------------------------------------------------------------------------
#
# `vercel.json`'s `ignoreCommand`
# means a data-only commit no longer triggers a build, so the bundle can be
# arbitrarily older than the last thing CI actually published; the mirror is
# what stays current regardless of whether a build ran.


def test_edge_data_files_wins_over_the_bundled_file_when_present():
    from services.core.core.run import _payload

    payload = _without(cli_payload(), "snapshot", "events", "macro")
    supabase = FakeSupabase(
        tables={
            "edge_cycle_input": rows_from(payload, CYCLE_ID),
            "edge_data_files": [
                {
                    "name": "macro",
                    "as_of": "2026-08-03",
                    "payload": {"as_of": "2026-08-03", "releases": [{"series": "CPI"}]},
                }
            ],
        }
    )

    result, extras = _payload(CYCLE_ID, supabase, cli.load_config())

    # The whole-file shape (macro is not unwrapped), and it is the Supabase
    # row's content, not whatever data/macro.json on disk happens to hold.
    assert result["macro"] == {"as_of": "2026-08-03", "releases": [{"series": "CPI"}]}
    assert extras["data_provenance"]["macro"] == {"source": "supabase", "as_of": "2026-08-03"}
    # snapshot/events had no edge_data_files row, so they still fell back to
    # the bundled file -- the mirror is additive, not all-or-nothing.
    assert extras["data_provenance"]["snapshot"]["source"] == "checkout"
    assert extras["data_provenance"]["events"]["source"] == "checkout"


def test_a_runner_posted_key_gets_no_provenance_row_at_all():
    from services.core.core.run import _payload

    # `cli_payload()` posts snapshot/events/macro itself (the default fixture
    # carries all three) -- none of them should appear in data_provenance,
    # since `_payload` never touched Supabase or the bundle for them.
    supabase = FakeSupabase(tables={"edge_cycle_input": rows_from(cli_payload(), CYCLE_ID)})

    _, extras = _payload(CYCLE_ID, supabase, cli.load_config())

    assert extras["data_provenance"] == {}


def test_decide_runlog_carries_data_provenance_with_a_computed_age():
    payload = _without(cli_payload(), "macro")
    supabase = FakeSupabase(
        tables={
            "edge_cycle_input": rows_from(payload, CYCLE_ID),
            "edge_data_files": [
                {
                    "name": "macro",
                    "as_of": "2026-07-20",
                    "payload": {"as_of": "2026-07-20", "releases": []},
                }
            ],
        }
    )

    core.run(CYCLE_ID, core.DECIDE, supabase)

    decide_row = next(
        p for p in supabase.args("edge_runlog_write") if p["p_command"] == "decide"
    )
    provenance = decide_row["p_stdout"]["data_provenance"]["macro"]
    assert provenance["source"] == "supabase"
    assert provenance["as_of"] == "2026-07-20"
    assert provenance["age_trading_days"] > 2


# ---------------------------------------------------------------------------
# The account, merged from the runner's two raw rows
# ---------------------------------------------------------------------------
#
# An early runner cycle died here: `assemble` refused the `balances` row
# as not an Appendix D key. The row is legitimate — `cli._fetch_plan` names the
# step and `get_accounts` answers it — and what was missing was somewhere for
# it to go. See `tests/test_cli_shape_account.py` for the merge itself; these
# are about the two sides meeting.

RAW_ACCOUNTS = [
    {"type": "margin", "account_number": "<your-margin-account-number>", "unsettled_funds": "40.0000"},
    {"type": "cash", "account_number": "<your-cash-account-number>", "unsettled_funds": "0.0000"},
]
# `get_portfolio`'s and `get_accounts`' actual response shapes — both wrap
# their object in the tool's own `{"data": {...}}` envelope off the wire, and
# the runner posts that verbatim ("post the response as it came"). An
# unwrapped dict/list here (what these fixtures carried before) is not a
# shape either tool has ever returned:
#
# - a flat `RAW_PORTFOLIO` (no `data`) is what let a runner-posted `account`
#   sail past `account_is_raw` unrecognised and die in `finalize` on
#   `KeyError: 'equity_total'` — an earlier postclose cycle;
#
# Both fixtures now carry the envelope so a regression here fails a test
# instead of a live cycle.
RAW_PORTFOLIO_WIRE = {
    "data": {
        "cash": "765.62",
        "total_value": "1400.00",
        "buying_power": {"buying_power": "765.6200"},
        "pending_deposits": "0",
    }
}
RAW_PORTFOLIO = RAW_PORTFOLIO_WIRE["data"]
RAW_BALANCES = {"data": {"accounts": RAW_ACCOUNTS}}


def _runner_rows(payload=None):
    """The rows a runner posts: `get_portfolio` under `account`,
    `get_accounts` under `balances`, both verbatim (including each tool's
    own `data` envelope)."""
    payload = {**(payload or cli_payload()), "account": RAW_PORTFOLIO_WIRE}
    return rows_from(payload, CYCLE_ID) + [
        {"kind": "balances", "symbol": None, "payload": RAW_BALANCES}
    ]


def test_a_balances_row_no_longer_fails_the_cycle():
    from services.core.core.run import _payload

    payload, extras = _payload(
        CYCLE_ID,
        FakeSupabase(tables={"edge_cycle_input": _runner_rows()}),
        cli.load_config(),
    )

    assert extras["balances"] == RAW_BALANCES
    # ...and `balances` never becomes a `cycle.json` key.
    assert "balances" not in payload


def test_the_runners_raw_portfolio_becomes_appendix_ds_account():
    from services.core.core.run import _payload

    payload, _ = _payload(
        CYCLE_ID,
        FakeSupabase(tables={"edge_cycle_input": _runner_rows()}),
        cli.load_config(),
    )

    assert payload["account"]["equity_total"] == "1400.00"
    assert payload["account"]["buying_power"] == "765.6200"
    # Measured against the cash account, not the margin one.
    assert payload["account"]["settled_cash"] == 765.62
    assert payload["account_problems"] == []
    # The raw responses stay beside it, so a runlog reader can see what the
    # settled figure was computed from.
    assert payload["portfolio"] == RAW_PORTFOLIO
    assert payload["accounts"] == RAW_ACCOUNTS


def test_an_account_a_live_session_already_merged_is_left_alone():
    # `equity_total` present is the hand-merged shape (or `merge-fetch`
    # recovery). Reshaping it would be a second answer to a settled question.
    from services.core.core.run import _payload

    original = cli_payload()["account"]
    payload, _ = _payload(
        CYCLE_ID,
        FakeSupabase(tables={"edge_cycle_input": rows_from(cli_payload(), CYCLE_ID)}),
        cli.load_config(),
    )

    assert payload["account"] == original
    assert "account_problems" not in payload


def test_the_hosted_plan_is_unchanged_when_the_runner_posts_the_raw_pair():
    """The acceptance question, on the shape the runner actually posts.

    `cli_payload`'s own `account` and `RAW_PORTFOLIO` describe different
    balances, so this does not assert the same plan — it asserts the cycle
    reaches a plan at all, which the raw shape could not do before: an
    `account` with no `equity_total` is `guard.halts`' ACCOUNT_UNREADABLE and
    every order is denied.
    """
    supabase = FakeSupabase(tables={"edge_cycle_input": _runner_rows()})

    result = core.run(CYCLE_ID, core.DECIDE, supabase)

    assert result["status"] == core.STATUS_DECIDED
    assert [order["symbol"] for order in result["plan"]] == ["PFE"]


def test_the_raw_shape_would_have_denied_every_order_before_this():
    # The regression itself: what the runner's `account` looked like to guard.
    from src import guard

    ctx = {
        **cli_payload(),
        "config": cli.load_config()["config"],
        "account": RAW_PORTFOLIO,
        "control": {},
        "positions": [],
    }
    verdict = guard.evaluate(
        [{"symbol": "PFE", "side": "buy", "type": "limit", "quantity": 1,
          "limit_price": 25.0, "session": "regular_hours"}],
        ctx,
    )

    assert verdict.approved == []


def test_a_posted_empty_events_map_is_a_real_answer_and_is_not_overwritten():
    # `{}` means "no events today", not "nothing was sent". Truthiness would
    # replace it with the file and re-trigger names the runner knew were stale.
    payload = {**cli_payload(), "events": {}}
    supabase = FakeSupabase(tables={"edge_cycle_input": rows_from(payload, CYCLE_ID)})

    from services.core.core.run import _payload

    assembled, _ = _payload(CYCLE_ID, supabase, cli.load_config())

    assert assembled["events"] == {}


# ---------------------------------------------------------------------------
# The real transport
# ---------------------------------------------------------------------------


def test_the_production_transport_speaks_to_a_real_socket():
    """`UrllibTransport` against a local server, once.

    Every other test here injects a fake, which proves the write *sequence* and
    nothing about the client. This one covers the gap that shipped a broken
    deployment: the transport was `httpx`, `httpx` was never installed, and no
    test noticed because no test ever constructed the production transport.
    """
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen = {}

    class Echo(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["method"] = self.command
            seen["path"] = self.path
            seen["auth"] = self.headers.get("Authorization")
            seen["body"] = self.rfile.read(int(self.headers["Content-Length"]))
            payload = _json.dumps([{"ok": True}]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            return

    from services.core.core.supabase import Supabase, SupabaseError, UrllibTransport

    srv = HTTPServer(("127.0.0.1", 0), Echo)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        client = Supabase(f"http://127.0.0.1:{srv.server_port}", "svc-key", UrllibTransport())

        assert client.rpc("edge_control_apply", {"p_updates": {}}) == [{"ok": True}]
        assert seen["path"] == "/rest/v1/rpc/edge_control_apply"
        assert seen["auth"] == "Bearer svc-key"
        assert _json.loads(seen["body"]) == {"p_updates": {}}

        # A non-2xx comes back as a status, not as a raised HTTPError -- what a
        # status means is `Supabase._call`'s decision, not the transport's.
        with pytest.raises(SupabaseError) as exc:
            client.select("edge_cycle_input", "cycle_id=eq.nope")
        assert "404" in str(exc.value)
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------
# The broker-view precondition
# ---------------------------------------------------------------------------


def test_a_reconciling_cycle_refuses_to_run_without_the_broker_view():
    """The failure this exists to prevent: `reconcile` reads an absent
    `positions` as an empty book, an empty book against a real `prior_book` as
    every position closed, and emits loss closes, lock releases and control
    updates for a liquidation that never happened. The old prompt never got
    here (a failed fetch stopped the session at §14); the core is handed rows
    and cannot know what was attempted.
    """
    for key in ("positions", "orders", "account"):
        payload = {k: v for k, v in cli_payload().items() if k != key}
        supabase = FakeSupabase(tables={"edge_cycle_input": rows_from(payload, CYCLE_ID)})

        result = core.run(CYCLE_ID, core.DECIDE, supabase)

        assert result["status"] == core.STATUS_FAILED, f"{key} absent must fail"
        assert key in result["detail"]
        # Nothing was written -- above all no loss closes for a book that is
        # still there.
        assert supabase.args("edge_loss_closes_append") == []
        assert supabase.args("edge_symbol_lock_release") == []
        assert supabase.args("edge_runlog_write") == []


def test_an_empty_but_present_broker_view_is_a_real_answer_and_proceeds():
    # A genuinely flat account posts `[]`, which must not be confused with a
    # fetch that never ran.
    payload = {**cli_payload(), "positions": [], "orders": []}
    supabase = FakeSupabase(tables={"edge_cycle_input": rows_from(payload, CYCLE_ID)})

    result = core.run(CYCLE_ID, core.DECIDE, supabase)

    assert result["status"] == core.STATUS_DECIDED


# ---------------------------------------------------------------------------
# regime_inputs from the pre-market job's edge_regime row
# ---------------------------------------------------------------------------


def test_the_decide_phase_pulls_regime_inputs_from_edge_regime_when_absent():
    # The pre-market data job (services/core/core/premarket.py) writes a
    # `regime_inputs` block once a day. A runner that did not post one gets it
    # from `edge_regime`.
    payload = _without(cli_payload(), "regime_inputs")
    inputs = {"spy": {"last": 610.0, "sma50": 600.0, "sma200": 590.0},
              "sectors": {}}
    supabase = FakeSupabase(
        tables={
            "edge_cycle_input": rows_from(payload, CYCLE_ID),
            "edge_regime": [{"payload": {"regime_inputs": inputs}}],
        }
    )

    core.run(CYCLE_ID, core.DECIDE, supabase)

    assert any(table == "edge_regime" for table, _ in supabase.reads)
    decide_row = next(
        p["p_stdout"] for p in supabase.args("edge_runlog_write")
        if p["p_command"] == "decide"
    )
    # `regime.evaluate` ran against the pulled block: a stacked SPY reads
    # risk_on.
    assert decide_row["regime"]["market_state"] == "risk_on"


def test_a_runner_posted_regime_inputs_block_wins_over_edge_regime():
    posted = {"spy": {"last": 1.0, "sma50": 2.0, "sma200": 3.0}, "sectors": {}}
    stored = {"spy": {"last": 610.0, "sma50": 600.0, "sma200": 590.0}, "sectors": {}}
    payload = {**cli_payload(), "regime_inputs": posted}
    supabase = FakeSupabase(
        tables={
            "edge_cycle_input": rows_from(payload, CYCLE_ID),
            "edge_regime": [{"payload": {"regime_inputs": stored}}],
        }
    )

    core.run(CYCLE_ID, core.DECIDE, supabase)

    decide_row = next(
        p["p_stdout"] for p in supabase.args("edge_runlog_write")
        if p["p_command"] == "decide"
    )
    # posted SPY is below its 200-day -> not risk_on; the stored block would
    # have been risk_on.
    assert decide_row["regime"]["market_state"] != "risk_on"


# ---------------------------------------------------------------------------
# The sector rotation on the runner path
# ---------------------------------------------------------------------------


def test_the_runner_rotates_dip_survivors_and_its_runlog_says_why():
    """End to end through `_decide_phase`: the runner posts survivors
    unfiltered (`conftest.rows_from` turns `triggers` into a `dip_survivors`
    row), the core drops the inactive-sector one, and the reason reaches the
    decide runlog row — which the runner path used to discard entirely."""
    payload = cli_payload()
    payload["triggers"] = [{"symbol": "PFE", "play_id": "DIP"}, {"symbol": "XOM", "play_id": "DIP"}]
    payload["control"] = {**payload["control"], "active_sectors": ["Energy"]}

    result, supabase = driven(payload)

    assert result["status"] == core.STATUS_DECIDED
    assert all(order["symbol"] != "PFE" for order in result["plan"])
    decide_row = next(
        p["p_stdout"] for p in supabase.args("edge_runlog_write") if p["p_command"] == "decide"
    )
    assert {"symbol": "PFE", "reason": "Health Care is outside the active sector rotation"} in (
        decide_row["candidates_dropped"]
    )
    assert decide_row["rotation"] == {
        "active_sectors": ["Energy"], "active_sub_industries": [], "unmatched": [],
        "filtering": True, "sub_industry_data": False,
    }


def test_with_no_rotation_the_runner_plan_is_unchanged():
    payload = cli_payload()
    baseline, _ = driven(payload)
    payload["control"] = {**payload["control"], "active_sectors": []}
    rotated, _ = driven(payload)
    assert rotated["plan"] == baseline["plan"]
    assert rotated["protection"] == baseline["protection"]


# ---------------------------------------------------------------------------
# The runner reads RSI/ATR out of the pre-market cache
# ---------------------------------------------------------------------------

_PREMARKET_STAMP = "2026-08-03T10:26:00Z"  # 06:26 ET, inside the 12h window


def _runner_payload():
    """What a runner actually posts: no `signals` at all, ever."""
    payload = cli_payload()
    payload.pop("signals", None)
    return payload


def _cache(atr, *, earnings=None):
    entry = {
        "signals": {"rsi": 45.0, "atr": atr, "sma_20": 15.10, "sma_50": 15.20, "sma_200": 13.50},
        "bars_meta": {"source": "twelvedata", "indicators": "daily_close"},
        "fetched_at": _PREMARKET_STAMP,
    }
    if earnings is not None:
        entry["earnings"] = earnings
    return {"F": entry}


def _decide_row(supabase):
    return next(
        p["p_stdout"] for p in supabase.args("edge_runlog_write") if p["p_command"] == "decide"
    )


def test_without_the_cache_the_runner_has_no_atr_and_no_stop_can_fire():
    """The defect: every hosted decide ran with `signals == {}`."""
    _, supabase = driven(_runner_payload())
    assert [e["code"] for e in _decide_row(supabase)["exits"]["exits"]] == []


def test_the_cached_daily_atr_arms_the_per_cycle_stop_on_the_runner_path():
    """F: 15.00 entry, 14.88 last. A cached ATR of 0.01 puts its stop at
    ~14.97, so the per-cycle check fires and sells with a limit order —
    no resting stop is involved at any point."""
    result, supabase = driven(
        _runner_payload(), returns={"edge_day_context_recent": _cache(0.01)}
    )

    assert [e["code"] for e in _decide_row(supabase)["exits"]["exits"]] == ["ATR_STOP"]
    sells = [o for o in result["plan"] if o["side"] == "sell"]
    assert [(o["symbol"], o["type"]) for o in sells] == [("F", "limit")]


def test_a_wide_cached_atr_leaves_the_position_alone():
    _, supabase = driven(_runner_payload(), returns={"edge_day_context_recent": _cache(2.0)})
    assert _decide_row(supabase)["exits"]["exits"] == []


def test_a_posted_signal_wins_over_the_cache():
    payload = cli_payload()  # posts F's own signals, atr 0.30 -> stop 14.10
    _, supabase = driven(payload, returns={"edge_day_context_recent": _cache(0.01)})
    assert _decide_row(supabase)["exits"]["exits"] == []


def test_cached_earnings_fill_only_a_fully_fresh_symbol():
    payload = _runner_payload()
    payload.pop("earnings", None)
    cached_earnings = {"marker": "from-cache"}

    loaded = cli.load_config()
    pre_only = {"day_context": _cache(2.0), "now": payload["now"], "control": payload["control"]}
    core._fill_from_day_context(pre_only, loaded)
    assert "earnings" not in pre_only  # pre-market wrote no earnings -> runner must fetch

    fresh = {"day_context": _cache(2.0, earnings=cached_earnings),
             "now": payload["now"], "control": payload["control"]}
    core._fill_from_day_context(fresh, loaded)
    assert fresh["earnings"]["F"] == cached_earnings
    assert fresh["signals"]["F"]["atr"] == 2.0


def test_a_stale_cache_entry_fills_nothing():
    stale = _cache(0.01)
    stale["F"]["fetched_at"] = "2026-08-02T10:26:00Z"  # yesterday
    payload = {"day_context": stale, "now": "2026-08-03T13:40:00Z",
               "control": cli_payload()["control"]}
    core._fill_from_day_context(payload, cli.load_config())
    assert "signals" not in payload


# ---------------------------------------------------------------------------
# The posture on the runner path, from recorded edge_regime rows
# ---------------------------------------------------------------------------


def _recorded_regime(day):
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "regime"
    return json.loads((path / f"edge_regime_{day}.json").read_text(encoding="utf-8"))


def _decide_row_with_regime(regime_payload, payload=None):
    payload = _without(cli_payload() if payload is None else payload, "regime_inputs")
    supabase = FakeSupabase(
        tables={
            "edge_cycle_input": rows_from(payload, CYCLE_ID),
            "edge_regime": [{"payload": regime_payload}],
        }
    )
    result = core.run(CYCLE_ID, core.DECIDE, supabase)
    assert result["status"] == core.STATUS_DECIDED
    return next(
        p["p_stdout"] for p in supabase.args("edge_runlog_write")
        if p["p_command"] == "decide"
    )


def test_a_recorded_edge_regime_row_yields_a_posture_and_breadth_on_the_runner_path():
    """An earlier row exactly as the pre-market job wrote it, pulled by the hosted
    core the way a runner cycle pulls it. SPY reads `risk_on`; 3 of 11 sectors
    are above trend."""
    row = _decide_row_with_regime(_recorded_regime("2026-10-02"))

    assert row["regime"]["market_state"] == "risk_on"
    assert row["regime"]["posture"] == "NO_NEW_POSITIONS"
    assert row["regime"]["breadth"]["breadth_200"] == pytest.approx(3 / 11)
    assert row["regime"]["breadth"]["n_available"] == 11
    # The flag ships off: the baseline entry is still planned, and the new
    # rule is recorded beside it.
    assert row["regime_v2_enabled"] is False
    assert row["shadow_posture"] == "NO_NEW_POSITIONS"
    assert [o["symbol"] for o in row["plan"]["orders"] if o["side"] == "buy"] == ["PFE"]
    assert [r["symbol"] for r in row["shadow_dip_would_block"]] == ["PFE"]


def test_a_recorded_row_predating_p1_has_no_fifty_day_breadth_and_no_crash():
    """Those rows carry no sector `sma50` and no returns. Unavailable, noted,
    not gated on."""
    row = _decide_row_with_regime(_recorded_regime("2026-09-18"))

    assert row["regime"]["posture"] == "REDUCED_SIZE"
    assert row["regime"]["breadth"]["breadth_50"] is None
    assert row["regime"]["sector_rs"] == {}
    assert {"breadth_50_unavailable", "rs_unavailable"} <= set(row["regime"]["notes"])


def test_a_row_missing_sector_etfs_is_breadth_unavailable_and_the_gate_is_not_applied(
    monkeypatch,
):
    """Seven of the eleven ETFs arrived. No breadth figure, so no breadth rule
    fires — even with the flag ON the entry is taken exactly as before."""
    recorded = _recorded_regime("2026-10-02")
    kept = sorted(recorded["regime_inputs"]["sectors"])[:7]
    recorded["regime_inputs"]["sectors"] = {
        etf: recorded["regime_inputs"]["sectors"][etf] for etf in kept
    }

    real = cli.load_config

    def flag_on(config_dir=None):
        loaded = real(config_dir)
        return {**loaded, "config": {**loaded["config"], "regime_v2_enabled": True}}

    monkeypatch.setattr(cli, "load_config", flag_on)
    row = _decide_row_with_regime(recorded)

    assert row["regime_v2_enabled"] is True
    assert row["regime"]["breadth"]["breadth_unavailable"] is True
    assert row["regime"]["breadth"]["breadth_200"] is None
    assert "breadth_unavailable" in row["regime"]["notes"]
    assert row["regime"]["posture"] == "TRADE_NORMALLY"
    assert [o["symbol"] for o in row["plan"]["orders"] if o["side"] == "buy"] == ["PFE"]
    assert "SKIP_REGIME" not in {s["code"] for s in row["entries"]["skipped"]}
