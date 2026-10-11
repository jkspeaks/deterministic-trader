"""`src/pead_reaction.reaction` — the retired reaction routine's Step 4, ported.

Absolute 2 — the one division, no rounding — is checked by asserting an exact
ratio.
"""

from __future__ import annotations

from src.pead_reaction import reaction


def _bars(*pairs):
    return [{"date": d, "close": c} for d, c in pairs]


SERIES = _bars(
    ("2026-09-01", 100.0),
    ("2026-09-02", 102.0),
    ("2026-09-03", 108.0),  # report date
    ("2026-09-04", 111.0),
    ("2026-09-05", 110.0),
)


def test_am_report_measures_its_own_session_against_the_day_before():
    out = reaction("2026-09-03", "am", SERIES)
    assert out["reaction_session"] == "2026-09-03"
    assert out["prior_close"] == 102.0
    assert out["reaction_close"] == 108.0
    assert out["reaction_pct"] == (108.0 - 102.0) / 102.0


def test_pm_report_measures_the_next_session_against_the_report_day():
    out = reaction("2026-09-03", "pm", SERIES)
    assert out["reaction_session"] == "2026-09-04"
    assert out["prior_close"] == 108.0
    assert out["reaction_close"] == 111.0
    assert out["reaction_pct"] == (111.0 - 108.0) / 108.0


def test_unknown_timing_takes_the_conservative_pm_branch():
    unknown = reaction("2026-09-03", None, SERIES)
    pm = reaction("2026-09-03", "pm", SERIES)
    # `timing` is copied through verbatim (Absolute 2); the measured session
    # and prices are what must match.
    for key in ("reaction_session", "prior_close", "reaction_close", "reaction_pct"):
        assert unknown[key] == pm[key]
    assert reaction("2026-09-03", "", SERIES)["reaction_session"] == "2026-09-04"


def test_report_on_a_non_trading_day_am_uses_the_next_bar_and_the_one_before():
    series = _bars(("2026-09-04", 50.0), ("2026-09-08", 53.0), ("2026-09-09", 52.0))
    out = reaction("2026-09-05", "am", series)
    assert out["reaction_session"] == "2026-09-08"
    assert out["prior_close"] == 50.0
    assert out["reaction_close"] == 53.0


def test_report_on_a_non_trading_day_pm_uses_next_bar_against_closest_before():
    series = _bars(("2026-09-04", 50.0), ("2026-09-08", 53.0), ("2026-09-09", 52.0))
    out = reaction("2026-09-06", "pm", series)
    assert out["reaction_session"] == "2026-09-08"
    assert out["prior_close"] == 50.0


def test_a_window_that_cannot_place_both_sessions_yields_none():
    # pm report with nothing after the report day: no reaction session.
    assert reaction("2026-09-03", "pm", _bars(("2026-09-02", 1.0), ("2026-09-03", 2.0))) is None
    # a single bad bar: fewer than two usable sessions.
    assert reaction("2026-09-03", "pm", _bars(("2026-09-03", 2.0))) is None
    # am report at the very start of the series: no prior session.
    assert reaction("2026-09-01", "am", SERIES) is None


def test_datetime_alias_and_string_numbers_are_accepted():
    series = [
        {"datetime": "2026-09-02", "close": "102.00"},
        {"datetime": "2026-09-03", "close": "108.00"},
        {"datetime": "2026-09-04", "close": "111.00"},
    ]
    out = reaction("2026-09-03", "am", series)
    assert out["prior_close"] == 102.0 and out["reaction_close"] == 108.0


def test_unparseable_and_nonpositive_bars_are_dropped_not_fatal():
    series = _bars(("2026-09-02", 102.0), ("2026-09-03", 108.0)) + [
        {"date": "2026-09-04", "close": "not-a-number"},
        {"date": "2026-09-05", "close": 0.0},
        {"date": None, "close": 5.0},
    ]
    out = reaction("2026-09-03", "am", series)
    assert out["reaction_pct"] == (108.0 - 102.0) / 102.0


def test_nothing_rounds():
    series = _bars(("2026-09-02", 3.0), ("2026-09-03", 10.0))
    out = reaction("2026-09-03", "am", series)
    # 7/3 is the kind of value a rounding bug would flatten.
    assert out["reaction_pct"] == (10.0 - 3.0) / 3.0
    assert repr(out["reaction_pct"]).startswith("2.333333")


def test_duplicate_latest_bar_does_not_create_a_zero_gap():
    series = _bars(
        ("2026-09-02", 102.0),
        ("2026-09-03", 108.0),
        ("2026-09-03", 108.0),
        ("2026-09-04", 111.0),
    )
    out = reaction("2026-09-03", "pm", series)
    assert out["reaction_session"] == "2026-09-04"
    assert out["prior_close"] == 108.0
