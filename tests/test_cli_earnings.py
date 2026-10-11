"""The earnings join — the broker's quarters becoming the fields the core reads.

`guard._earnings_denials` and `plays._earnings_veto` gate every entry on
``status``, ``sessions_until_report`` and friends. None of those is a field any
broker returns: they are derived from `get_earnings_results`' quarters and the
checked-in calendar. These tests exist so the fixture can never assert that
contract again.

The rule they encode: the fixture supplies what the broker supplies, and the
derivation is what is under test.
"""

from datetime import date

import pytest

from src import cli, guard, plays
from tests.factories import cli_payload, codes, context, order

CALENDAR = cli.load_config()["calendar"]


def quarter(
    report_date: str, estimate: str | None, actual: str | None, verified: bool = True
) -> dict:
    return {
        "eps": {"estimate": estimate, "actual": actual},
        "report": {"date": report_date, "timing": "pm", "verified": verified},
    }


def view(*quarters, on: str = "2026-08-06", symbol: str = "F") -> dict:
    """One symbol's broker payload, derived against ``on``."""
    return cli._earnings_view(
        symbol, {"data": {"results": list(quarters)}}, date.fromisoformat(on), CALENDAR
    )


def decided(**overrides) -> dict:
    return cli.decide(cli_payload(**overrides), cli.load_config())


# --- the derivation --------------------------------------------------------


def test_quarters_become_the_shape_the_core_reads():
    """A recorded AMD response: one
    delivered quarter two sessions back and one tentative quarter in November."""
    derived = view(
        quarter("2026-08-04", "1.550000", "1.660000"),
        quarter("2026-11-03", "1.760000", None, verified=False),
    )

    assert derived["status"] == "known"
    assert derived["next_report_date"] == "2026-11-03"
    assert derived["last_report_date"] == "2026-08-04"
    assert derived["sessions_since_report"] == 2
    assert round(derived["eps_surprise_pct"], 2) == 7.10


def test_sessions_are_counted_over_the_calendar_not_the_diary():
    """Labor Day 2026 is 09-07. Six calendar days from 09-03 to 09-09 is three
    sessions, and a T-2 flatten measured in days would fire on the wrong one."""
    derived = view(quarter("2026-09-09", "1.00", None), on="2026-09-03")

    assert derived["sessions_until_report"] == 3


def test_an_unverified_forward_date_is_still_known():
    """The whole defect in one assertion. Companies confirm a date weeks ahead,
    so requiring `verified` would leave most names unknown most of the time and
    veto every entry — which is the behaviour being repaired, not a fix."""
    derived = view(quarter("2026-11-03", "1.76", None, verified=False))

    assert derived["status"] == "known"
    assert derived["verified"] is False
    assert derived["sessions_until_report"] > 0


def test_a_verified_date_says_so():
    assert view(quarter("2026-11-03", "1.76", None, verified=True))["verified"] is True


def test_the_surprise_comes_from_the_most_recent_delivered_quarter():
    """Ordering is the broker's, but it is not to be trusted: a response that
    arrived out of order would otherwise scale conviction off a year-old beat."""
    derived = view(
        quarter("2026-08-04", "1.550000", "1.660000"),
        quarter("2025-08-05", "0.400000", "0.480000"),
    )

    assert derived["last_report_date"] == "2026-08-04"
    assert round(derived["eps_surprise_pct"], 2) == 7.10


def test_a_miss_is_negative_and_pead_never_sees_it_as_a_beat():
    derived = view(quarter("2026-08-04", "1.000000", "0.900000"))

    assert round(derived["eps_surprise_pct"], 2) == -10.00


def test_a_zero_estimate_has_no_percentage():
    """Dividing would emit an infinity, and an infinite beat is precisely the
    number that would size a position."""
    assert view(quarter("2026-08-04", "0.000000", "0.120000"))["eps_surprise_pct"] is None


def test_no_upcoming_quarter_reads_unknown():
    """Which vetoes. §14: a date that cannot be read is not a date to guess."""
    derived = view(quarter("2026-08-04", "1.550000", "1.660000"))

    assert derived["status"] == "unknown"
    assert derived["sessions_until_report"] is None


def test_an_overdue_print_reads_as_imminent_not_as_negative():
    """A date that has passed with no actual means the company has not reported
    when it said it would. Zero sessions away vetoes; a negative number would
    also veto but would read as nonsense in the runlog."""
    derived = view(quarter("2026-08-04", "1.76", None), on="2026-08-06")

    assert derived["sessions_until_report"] == 0


def test_reaction_pct_is_absent_without_a_matching_reaction_row():
    """No fetch step derives this on its own — it arrives pre-computed from
    `edge_earnings_reactions` (§6.1's separate reaction routine) or not at
    all. Absent that row, PEAD proposes nothing, which is the known,
    documented gap this closes rather than works around."""
    derived = cli._earnings_view(
        "F",
        {"data": {"results": [quarter("2026-08-04", "1.55", "1.66")]}},
        date(2026, 8, 6),
        CALENDAR,
    )

    assert derived["reaction_pct"] is None


def test_reaction_pct_is_read_from_the_matching_reaction_row():
    """Keyed on `(symbol, last_report_date)` — the same pair the reaction
    routine writes `edge_earnings_reactions` under — not on symbol alone, so
    a stale row from a prior quarter can never be mistaken for this one's."""
    derived = cli._earnings_view(
        "F",
        {"data": {"results": [quarter("2026-08-04", "1.55", "1.66")]}},
        date(2026, 8, 6),
        CALENDAR,
        {("F", "2026-08-04"): 0.037},
    )

    assert derived["reaction_pct"] == pytest.approx(0.037)


def test_reaction_pct_ignores_a_row_for_a_different_report_date():
    """A row left over from a prior quarter must not leak into this one's
    entry decision -- stale data should read as absent, not as confirmed."""
    derived = cli._earnings_view(
        "F",
        {"data": {"results": [quarter("2026-08-04", "1.55", "1.66")]}},
        date(2026, 8, 6),
        CALENDAR,
        {("F", "2026-05-01"): 0.09},
    )

    assert derived["reaction_pct"] is None


# --- the envelope ----------------------------------------------------------


def test_every_envelope_depth_the_prompt_might_paste_is_read():
    rows = [quarter("2026-11-03", "1.76", None)]
    expected = "2026-11-03"

    for raw in ({"data": {"results": rows}}, {"results": rows}, rows):
        derived = cli._earnings_view("F", raw, date(2026, 8, 6), CALENDAR)
        assert derived["next_report_date"] == expected


def test_an_unreadable_payload_vetoes_rather_than_raising():
    for raw in (None, {}, "nonsense", {"data": {}}, {"results": [None, 7]}):
        assert cli._earnings_view("F", raw, date(2026, 8, 6), CALENDAR)["status"] == "unknown"


def test_a_quarter_without_a_usable_date_is_ignored():
    derived = view(
        {"eps": {"estimate": "1.0", "actual": None}, "report": {"date": None}},
        quarter("2026-11-03", "1.76", None),
    )

    assert derived["next_report_date"] == "2026-11-03"


# --- the join, end to end --------------------------------------------------


def test_the_baseline_cycle_still_places_its_buy():
    """PFE reports in November. The gate is derived now rather than asserted by
    the fixture, and the plan is unchanged — which is the point."""
    result = decided()

    assert [o["symbol"] for o in result["plan"]["orders"]] == ["PFE"]


def test_decide_exposes_the_derived_earnings_view():
    """The reaction routine has to find candidates inside §6.1's window from `edge_runlog` without
    re-deriving earnings itself -- so this has to be `ctx["earnings"]` verbatim, the same dict
    `plays.py` judged against, not a summary of it."""
    result = decided()

    assert result["earnings"]["F"]["status"] == "known"
    assert result["earnings"]["PFE"]["status"] == "known"


def test_a_name_reporting_inside_the_window_never_reaches_the_plan():
    """The end-to-end version: raw quarters in, no order out. It stops at the
    play rather than at guard — `_earnings_veto`'s clearance is deliberately
    wider than guard's T-2, so the denial layer never has to see it. Before the
    derivation existed neither gate could fire from a real payload at all."""
    result = decided(
        earnings={
            "PFE": {
                "data": {
                    "results": [
                        quarter("2026-08-01", "0.610000", "0.620000"),
                        quarter("2026-08-04", "0.590000", None),
                    ]
                }
            }
        }
    )

    assert [o["symbol"] for o in result["plan"]["orders"]] == []


def test_guard_denies_from_the_derived_shape_inside_its_own_window():
    """Guard's T-2, reached directly, because the play gate above shields it in
    the pipeline. Raw quarters in, EARNINGS_WINDOW out."""
    derived = view(quarter("2026-08-07", "1.00", None), on="2026-08-06")

    result = guard.evaluate([order(symbol="F")], context(earnings={"F": derived}))

    assert guard.EARNINGS_WINDOW in codes(result)


def test_guard_still_vetoes_a_symbol_the_derivation_could_not_resolve():
    """The fail-safe survives the fix: derived-and-unknown denies exactly as
    absent-and-unknown did."""
    result = guard.evaluate(
        [order(symbol="F")],
        context(earnings={"F": dict(cli._EARNINGS_UNKNOWN)}),
    )

    assert guard.EARNINGS_DATE_UNAVAILABLE in codes(result)


def test_plays_veto_reads_the_same_derived_field():
    """guard and plays must agree about the same name, or a play proposes what
    guard will always deny and the runlog fills with orders that never had a
    chance."""
    derived = view(quarter("2026-08-07", "1.00", None), on="2026-08-06")
    skipped = plays._earnings_veto(
        "F", {"earnings": {"F": derived}}, plays.DIP, 3
    )

    assert skipped is not None
    assert skipped.code == plays.EARNINGS_VETO
