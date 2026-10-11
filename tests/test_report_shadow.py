"""The shadow block on the weekly scorecard.

The scorecard is the only notification this system sends when nothing is
wrong, which makes it the one place shadow evidence will actually be read.
"""

import pytest

from src import report
from tests.factories import report_context

FRIDAY = {"trading_day": "2026-08-07", "now": "2026-08-07T20:30:00Z"}


def trade(variant, pnl, exit_day="2026-08-06", reason="TARGET"):
    return {
        "variant": variant,
        "symbol": "PFE",
        "entry_day": "2026-08-04",
        "exit_day": exit_day,
        "pnl_pct": pnl,
        "reason": reason,
    }


def scorecard_body(rows):
    result = report.run(report_context(cycle=FRIDAY, shadow_week=rows))
    alerts = [a for a in result.alerts if a.title == report.WEEKLY_SCORECARD]
    assert len(alerts) == 1
    return alerts[0].body


# ---------------------------------------------------------------------------
# Absent, not empty
# ---------------------------------------------------------------------------


def test_no_shadow_rows_means_no_shadow_block_at_all():
    body = scorecard_body([])

    assert "shadow" not in body


def test_rows_that_never_closed_do_not_manufacture_a_variant_line():
    # An entry that never filled, and one still open. Both are real findings
    # and both are readable in `edge_shadow_trades`; neither is a return.
    body = scorecard_body(
        [
            trade("baseline", None, exit_day=None, reason="NO_FILL_QUOTE"),
            trade("A_dip_band", None, exit_day=None, reason="OPEN"),
        ]
    )

    assert "shadow" not in body


def test_a_context_with_no_shadow_key_at_all_is_the_same_as_none():
    result = report.run(report_context(cycle=FRIDAY))
    alert = next(a for a in result.alerts if a.title == report.WEEKLY_SCORECARD)

    assert "shadow" not in alert.body


# ---------------------------------------------------------------------------
# One line per variant, baseline first, bounded
# ---------------------------------------------------------------------------


def shadow_lines(body):
    """Everything after the shadow heading, minus the trailing
    blank-line-plus-doc-link footer every alert body now ends with."""
    heading = report._SHADOW_HEADING.strip()
    assert heading in body
    rest = body.split(heading + "\n", 1)[1]
    doc_link = "\n\n" + report._doc_link(report.WEEKLY_SCORECARD)
    assert rest.endswith(doc_link)
    return rest[: -len(doc_link)].splitlines()


def test_one_line_per_variant_with_baseline_first():
    body = scorecard_body(
        [
            trade("D_no_trade", 0.02),
            trade("baseline", 0.04),
            trade("A_dip_band", -0.01),
        ]
    )

    lines = shadow_lines(body)
    assert len(lines) == 3
    assert lines[0].strip().startswith("baseline")
    assert [line.split()[0] for line in lines[1:]] == ["A_dip_band", "D_no_trade"]


def test_each_line_carries_count_win_rate_mean_median_and_drawdown():
    body = scorecard_body(
        [trade("baseline", 0.10), trade("baseline", -0.02), trade("baseline", 0.06)]
    )

    line = shadow_lines(body)[0]
    assert "3t" in line          # count
    assert "67%w" in line        # win rate: 2 of 3
    assert "avg +4.67%" in line  # mean of +10, -2, +6
    assert "med +6.00%" in line  # median
    assert "dd -2.00%" in line   # the one losing step


def test_the_block_never_exceeds_its_line_budget():
    rows = [trade(f"variant_{index}", 0.01) for index in range(9)]

    lines = shadow_lines(scorecard_body(rows))

    assert len(lines) == report.SHADOW_MAX_LINES
    assert lines[-1].strip().startswith("+5 more variants")


def test_the_shadow_block_sits_under_the_account_lines_not_over_them():
    body = scorecard_body([trade("baseline", 0.04)])

    assert body.index("agent") < body.index("shadow")


# ---------------------------------------------------------------------------
# The drawdown walk
# ---------------------------------------------------------------------------


def test_a_series_that_never_gave_anything_back_has_no_drawdown():
    assert report._max_drawdown([0.01, 0.02, 0.03]) == 0.0


def test_the_drawdown_is_the_deepest_peak_to_trough_not_the_worst_trade():
    # +10, -3, -4 then +8: the peak is +10 and the trough is +3, so the
    # drawdown is -7 — deeper than any single trade.
    assert report._max_drawdown([0.10, -0.03, -0.04, 0.08]) == pytest.approx(-0.07)


def test_cli_report_forwards_shadow_week_from_the_payload():
    """The key has to survive the one boundary between `cycle.json` and here.

    `fill_quality_week` was the last key added on this path; it is in Appendix
    D, `cycle_keys.py` and `cli.report`'s context because all three are needed
    for a value to reach `report.run` at all.
    """
    from src import cli
    from src.cycle_keys import CYCLE_JSON_KEY_SET
    from tests.factories import cli_payload

    assert "shadow_week" in CYCLE_JSON_KEY_SET

    payload = cli_payload(
        now="2026-08-07T20:30:00Z",
        control={"last_scorecard_date": None},
        shadow_week=[trade("baseline", 0.04)],
    )
    result = cli.report(payload, cli.load_config())

    scorecards = [
        alert for alert in result["alerts"]
        if alert["title"] == report.WEEKLY_SCORECARD
    ]
    assert len(scorecards) == 1
    assert "shadow" in scorecards[0]["body"]
    assert "baseline 1t" in scorecards[0]["body"]


def test_an_unparseable_pnl_is_dropped_rather_than_read_as_zero():
    body = scorecard_body(
        [trade("baseline", "nonsense"), trade("baseline", 0.04)]
    )

    assert "1t" in shadow_lines(body)[0]
