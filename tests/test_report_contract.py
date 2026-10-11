"""What `cli.py` and Supabase depend on, and where report gets its numbers.

Two contracts meet here. Downwards, every row this module emits is written
into a table whose columns are fixed by a migration `cli.py` cannot alter at
runtime, so a renamed key is a write that fails at 16:30 on a Friday. Upwards,
the scorecard's play lines are `reconcile.py`'s numbers — this module never
recomputes them, and the tests below feed one module's real output into the
other rather than asserting on names, because a key read through `.get()` has
a default and behaviour is the only thing without one.
"""

import json
import re
from pathlib import Path

from src import cadence, guard, reconcile, report, sizing
from tests.factories import reconcile_context, report_context

FRIDAY = {"trading_day": "2026-08-07", "now": "2026-08-07T20:30:00Z"}


# --- the shape Supabase is expecting ---------------------------------------


def test_the_whole_result_survives_a_json_round_trip():
    """Invariant 2: JSON in, JSON out. The routine writes this over Supabase
    MCP, so anything that will not serialise fails after the cycle has already
    traded."""
    result = report.run(report_context(cycle=FRIDAY))

    assert json.loads(json.dumps(result.as_dict())) == result.as_dict()


def test_the_benchmark_row_is_exactly_the_edge_benchmark_columns():
    """§8.4's table, as settled: five columns, not four. A sixth key here is
    an insert that fails; a fifth that went missing is a series with a hole in
    it that §8.4 says cannot be backfilled."""
    row = report.run(report_context()).benchmark.as_dict()

    assert set(row) == {
        "trading_day",
        "equity",
        "net_deposits",
        "spy_close",
        "frozen_value",
    }


def test_the_benchmark_row_is_keyed_on_the_trading_day():
    """It is an upsert, not an insert. §13's post-close is a band, so a
    13:00 half-day fires four of them and four inserts would be four rows for
    one Friday."""
    early = {"early_close": {"2026-08-06": "13:00"}}
    rows = [
        report.run(
            report_context(calendar=early, cycle={"now": stamp})
        ).benchmark.as_dict()
        for stamp in ("2026-08-06T17:40:00Z", "2026-08-06T20:30:00Z")
    ]

    assert rows[0] == rows[1]


def test_an_alert_is_exactly_the_edge_alerts_columns():
    """The migration's own column list, minus the ones the row does not carry:
    `id` and `created_at` are the table's, `delivered_at` is the relay's, and
    `cycle_id` is stamped by `cli.py`, which knows the invocation."""
    ctx = report_context(failure={"step": "reconcile", "detail": "timeout"})

    assert set(report.run(ctx).alerts[0].as_dict()) == {"title", "body", "priority"}


def test_the_control_update_names_the_field_cli_writes():
    result = report.run(report_context(cycle=FRIDAY))

    assert set(result.control_updates) == {"last_scorecard_date"}


def test_a_cycle_with_nothing_to_say_writes_nothing_but_its_status_push():
    """`control_updates` is written to `edge_control` verbatim. An empty dict on
    a quiet cycle is what stops eleven no-op writes a day. The cycle still
    gets its one `min`-priority status push — silence at the `edge_alerts`
    layer would make a dead relay indistinguishable from a quiet market."""
    result = report.run(report_context())

    assert result.control_updates == {}
    assert len(result.alerts) == 1
    assert result.alerts[0].priority == report.PRIORITY_MIN
    assert result.status == report.STATUS_SUCCESS


# --- what this module is not ------------------------------------------------


def test_report_emits_no_orders_and_no_denials():
    """§11 splits decision from measurement. Everything here runs *after* the
    cycle has traded, and a measurement module that could place an order would
    be a second decider on one account — the failure §13.1's lock exists for,
    and one that has already happened here once."""
    result = report.run(report_context(cycle=FRIDAY)).as_dict()

    assert set(result) == {
        "benchmark",
        "scorecard",
        "alerts",
        "control_updates",
        "notes",
        "status",
    }


def test_report_does_not_maintain_the_high_water_mark():
    """Settled, call 24: `reconcile.py` owns it, because it holds `get_portfolio`
    equity at the start of every reconciling cycle and guard denies against the
    mark in the same cycle. A mark written here — at the close, once a day —
    would leave guard measuring a stale floor all session."""
    ctx = report_context(cycle=FRIDAY, account={"equity_total": 500.00})

    assert "high_water_mark" not in report.run(ctx).control_updates


def test_report_and_guard_measure_the_drawdown_with_one_function():
    """Not merely the same threshold — the same function object. Two readings
    of §10's 20% that agreed today would drift, and the day they did the
    operator would get an alert about a halt that had not happened, or a halt
    with no alert."""
    assert report.drawdown_breached is sizing.drawdown_breached
    assert guard.drawdown_breached is sizing.drawdown_breached


def test_report_reads_the_same_calendar_cadence_does():
    """One calendar, or the week ends on two different days. `reconcile.py`
    imports the same function for the same reason."""
    assert report.is_open is cadence.is_open
    assert reconcile.is_open is cadence.is_open


# --- upwards: reconcile's numbers, unmodified -------------------------------


def closed_a_losing_dip() -> dict:
    """One real cycle of `reconcile.run`: a DIP position sold at a loss."""
    return reconcile_context(
        intent={
            "placements": [
                {"order_id": "a1b2", "symbol": "F", "side": "sell",
                 "quantity": 1.0, "play_id": "DIP"}
            ]
        },
        orders=[{"order_id": "a1b2", "symbol": "F", "side": "sell",
                 "quantity": 1.0, "state": "filled"}],
        fills=[{"order_id": "a1b2", "symbol": "F", "side": "sell",
                "quantity": 1.0, "price": 13.75, "cost_basis": 15.00}],
        positions=[],
        tax_lots={},
    )


def test_the_scorecard_reads_reconciles_play_state_without_translation():
    """`closed_trades` is still `reconcile.py`'s, nested by assignment rather
    than passed as a factory keyword, so a key it stopped emitting cannot be
    filled in by the fixture."""
    updates = reconcile.run(closed_a_losing_dip()).play_state_updates

    ctx = report_context(cycle=FRIDAY)
    ctx["play_state"] = updates

    line = next(p for p in report.run(ctx).scorecard.plays if p.play_id == "DIP")

    assert line.closed_trades == 5


def test_the_scorecard_reads_the_money_edge_play_state_json_returns():
    """This is that read's shape, field for field."""
    ctx = report_context(cycle=FRIDAY)
    ctx["play_state"] = {
        "DIP": {"regime_fit": 1.0, "reason": None, "closed_trades": 4,
                "realized_pnl": -14.39, "near_flat_trades": 1},
    }

    line = next(p for p in report.run(ctx).scorecard.plays if p.play_id == "DIP")

    assert (line.realized_pnl, line.near_flat_trades) == (-14.39, 1)


def test_a_play_reconcile_has_never_written_reads_as_flat_not_as_missing():
    """`edge_play_state` starts empty and fills in as plays trade. A scorecard
    that skipped the untraded ones would make a play that stopped firing look
    like a play that was switched off — the one distinction §1 asks it to
    preserve."""
    ctx = report_context(cycle=FRIDAY)
    ctx["play_state"] = {"MACRO": {"regime_fit": 1.0}}

    line = next(p for p in report.run(ctx).scorecard.plays if p.play_id == "MACRO")

    assert line.closed_trades == 0
    assert line.realized_pnl == 0.0


def test_the_divergence_alert_reads_reconciles_findings_verbatim():
    """`reconcile.as_dict()` is what `cli.py` has in hand, so it is what this
    module is handed. A finding whose `code` or `detail` were renamed would
    produce an alert with the word `None` in it rather than a failure."""
    diverged = reconcile.run(
        reconcile_context(
            orders=[{"order_id": "zz99", "symbol": "MRK", "side": "buy",
                     "quantity": 1.0, "state": "queued"}]
        )
    )

    ctx = report_context()
    ctx["reconcile"] = diverged.as_dict()

    body = report.run(ctx).alerts[0].body

    assert reconcile.PHANTOM_ORDER in body
    assert diverged.findings[0].detail in body


# --- every alert's doc link must resolve ------------------------


def test_every_canonical_title_has_a_matching_operations_heading():
    """`_doc_slug(title)` is what `_footer_with_link`/`_doc_link` put in every
    alert body (`docs/OPERATIONS.md#<slug>`). This proves the other half:
    that `docs/OPERATIONS.md` actually has a `## <title>` heading whose own
    GitHub-rendered anchor is that exact slug — so a future doc edit that
    drops or retitles a heading fails here, not silently in production."""
    text = Path("docs/OPERATIONS.md").read_text(encoding="utf-8")
    headings = {m.group(1) for m in re.finditer(r"^## (.+)$", text, re.MULTILINE)}

    titles = set(report.CANONICAL_TITLES) | {
        report.ORDER_REFUSED,
        report.MANUAL_ACTION_REQUIRED,
    }
    for title in sorted(titles):
        assert title in headings, f"docs/OPERATIONS.md is missing '## {title}'"

    slugs = {report._doc_slug(h) for h in headings}
    assert len(slugs) == len(headings), "two headings collided onto one anchor"
