"""`src/forward.py` — forward returns and price-history features from bars.

S0's back-fill writes these numbers onto the decision snapshot, and every
later selection test reads them. The failures worth a test are the ones that
look like data: a return measured over the wrong number of sessions because a
halted day was stepped over, a -50% that is a split, a feature that saw the
future.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src import cli, forward

CALENDAR = cli.load_config()["calendar"]

DAY = "2026-08-03"


def sessions(start: str, count: int) -> list[str]:
    return forward.sessions_after(start, count, CALENDAR)


def bar(day: str, close: float, open_: float | None = None) -> dict:
    open_ = close if open_ is None else open_
    return {"date": day, "open": open_, "high": max(open_, close), "low": min(open_, close), "close": close}


def series(closes: dict[str, float], opens: dict[str, float] | None = None) -> list[dict]:
    return [bar(day, close, (opens or {}).get(day)) for day, close in sorted(closes.items())]


def steady(start: str = DAY, days: int = 22, first: float = 100.0, step: float = 1.0) -> dict[str, float]:
    """A close on the decision day and on each of the next ``days`` sessions."""
    return {day: first + step * i for i, day in enumerate([start, *sessions(start, days)])}


TODAY = (date.fromisoformat(sessions(DAY, 22)[-1]) + timedelta(days=1)).isoformat()


# --- forward returns ---------------------------------------------------------


def test_close_to_close_and_next_open_returns_are_what_they_say():
    closes = steady()
    first = sessions(DAY, 1)[0]
    result = forward.forward_returns(series(closes, {first: 100.5}), DAY, CALENDAR, TODAY)

    assert result["status"] == {"1": "ok", "3": "ok", "5": "ok", "8": "ok", "21": "ok"}
    assert result["complete"] is True
    # close of session h over the decision day's close ...
    assert result["fwd"]["cc_1"] == pytest.approx(101 / 100 - 1)
    assert result["fwd"]["cc_8"] == pytest.approx(108 / 100 - 1)
    assert result["fwd"]["cc_21"] == pytest.approx(121 / 100 - 1)
    # ... and the same close over the NEXT session's open, not the signal close.
    assert result["fwd"]["no_1"] == pytest.approx(101 / 100.5 - 1, abs=1e-6)
    assert result["fwd"]["no_8"] == pytest.approx(108 / 100.5 - 1, abs=1e-6)


def test_a_horizon_that_has_not_happened_is_pending_not_zero():
    closes = steady(days=3)
    today = (date.fromisoformat(sessions(DAY, 3)[-1]) + timedelta(days=1)).isoformat()

    result = forward.forward_returns(series(closes), DAY, CALENDAR, today)

    assert result["status"]["1"] == "ok" and result["status"]["3"] == "ok"
    assert result["status"]["5"] == result["status"]["21"] == "pending"
    assert result["fwd"]["cc_5"] is None and result["fwd"]["no_21"] is None
    assert result["complete"] is False


def test_todays_own_bar_is_never_read_as_final():
    """The job runs on `today`; a session counts only once it is strictly
    before it, so a bar still printing cannot be taken for a close."""
    target = sessions(DAY, 1)[0]

    result = forward.forward_returns(series(steady(days=1)), DAY, CALENDAR, today=target)

    assert result["status"]["1"] == "pending" and result["fwd"]["cc_1"] is None


def test_a_halted_session_is_a_missing_bar_and_the_window_is_not_stretched():
    """Sessions are the market's, not the symbol's. With no bar on session 5
    the 5-session return is unknown — reading session 6's close instead would
    quietly measure a different horizon."""
    closes = steady()
    del closes[sessions(DAY, 5)[-1]]

    result = forward.forward_returns(series(closes), DAY, CALENDAR, TODAY)

    assert result["status"]["5"] == "missing_bar"
    assert result["fwd"]["cc_5"] is None and result["fwd"]["no_5"] is None
    # Its neighbours are untouched.
    assert result["fwd"]["cc_3"] == pytest.approx(0.03)
    assert result["fwd"]["cc_8"] == pytest.approx(0.08)


def test_a_missing_bar_is_retried_inside_the_grace_and_closed_after_it():
    closes = steady()
    last = sessions(DAY, 21)[-1]
    del closes[last]
    soon = (date.fromisoformat(last) + timedelta(days=2)).isoformat()
    late = (date.fromisoformat(last) + timedelta(days=forward.MISSING_GRACE_DAYS + 1)).isoformat()

    assert forward.forward_returns(series(closes), DAY, CALENDAR, soon)["complete"] is False
    result = forward.forward_returns(series(closes), DAY, CALENDAR, late)
    assert result["status"]["21"] == "missing_bar" and result["complete"] is True


def test_no_bar_on_the_decision_day_means_no_base_to_measure_from():
    closes = steady()
    del closes[DAY]

    result = forward.forward_returns(series(closes), DAY, CALENDAR, TODAY)

    assert set(result["status"].values()) == {"missing_bar"}
    assert all(value is None for value in result["fwd"].values())


def test_a_missing_entry_open_costs_only_the_next_open_leg():
    closes = steady()
    bars = series(closes)
    first = sessions(DAY, 1)[0]
    next(b for b in bars if b["date"] == first)["open"] = None

    result = forward.forward_returns(forward.bars_from(bars), DAY, CALENDAR, TODAY)

    assert result["status"]["8"] == "ok"
    assert result["fwd"]["cc_8"] == pytest.approx(0.08)
    assert result["fwd"]["no_8"] is None


def test_an_unadjusted_split_is_flagged_and_never_recorded_as_a_loss():
    """A 2-for-1 on session 4 of an unadjusted series reads as -50%."""
    days = [DAY, *sessions(DAY, 22)]
    closes = {day: (100.0 if i < 4 else 50.0) + 0.1 * i for i, day in enumerate(days)}
    opens = {days[4]: 50.2}

    result = forward.forward_returns(series(closes, opens), DAY, CALENDAR, TODAY)

    # Before the split: ordinary returns.
    assert result["status"]["1"] == "ok" and result["status"]["3"] == "ok"
    # Any window that crosses it: flagged, no number.
    for horizon in ("5", "8", "21"):
        assert result["status"][horizon] == "split_suspect"
    assert result["fwd"]["cc_5"] is None and result["fwd"]["cc_21"] is None
    # A verdict that cannot change: the row can be closed.
    assert result["complete"] is True


def test_an_ordinary_crash_is_a_return_not_a_split():
    days = [DAY, *sessions(DAY, 22)]
    closes = {day: (100.0 if i < 4 else 88.0) for i, day in enumerate(days)}

    result = forward.forward_returns(series(closes), DAY, CALENDAR, TODAY)

    assert result["status"]["5"] == "ok"
    assert result["fwd"]["cc_5"] == pytest.approx(-0.12)


def test_sessions_skip_weekends_and_market_holidays():
    assert forward.sessions_after("2026-09-04", 2, CALENDAR) == ["2026-09-08", "2026-09-09"]
    assert forward.sessions_elapsed("2026-09-04", "2026-09-09", CALENDAR) == 1
    assert forward.sessions_elapsed("2026-09-04", "2026-09-05", CALENDAR) == 0


def test_vendor_rows_are_normalised_and_unusable_ones_dropped():
    rows = [
        {"datetime": "2026-08-04", "open": "10.1", "high": "10.5", "low": "9.9", "close": "10.2"},
        {"datetime": "2026-08-03 00:00:00", "open": "10", "high": "10.2", "low": "9.8", "close": "10.0"},
        {"datetime": "2026-08-05", "open": "10", "high": "10", "low": "10", "close": None},
        {"datetime": "2026-08-06", "open": "10", "high": "10", "low": "10", "close": "0"},
        {"open": "10", "close": "10"},
        "not a row",
    ]

    bars = forward.bars_from(rows)

    assert [b["date"] for b in bars] == ["2026-08-03", "2026-08-04"]        # sorted, two survive
    assert bars[1] == {"date": "2026-08-04", "open": 10.1, "high": 10.5, "low": 9.9, "close": 10.2}


# --- price-history features --------------------------------------------------


def history(count: int, end_before: str = DAY, first: float = 100.0, growth: float = 1.001) -> list[dict]:
    """``count`` consecutive weekday bars ending the weekday before ``end_before``."""
    days: list[str] = []
    cursor = date.fromisoformat(end_before)
    while len(days) < count:
        cursor -= timedelta(days=1)
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
    days.reverse()
    return [bar(day, first * growth ** i) for i, day in enumerate(days)]


def test_momentum_and_the_52_week_high_come_out_of_a_known_series():
    bars = history(300)
    closes = [b["close"] for b in bars]

    features = forward.price_features(bars, DAY)

    assert features["bars_used"] == 300 and features["last_bar"] == bars[-1]["date"]
    assert features["mom_12_1"] == pytest.approx(closes[-22] / closes[-253] - 1, abs=1e-6)
    assert features["mom_6_1"] == pytest.approx(closes[-22] / closes[-127] - 1, abs=1e-6)
    assert features["ret_3m"] == pytest.approx(closes[-1] / closes[-64] - 1, abs=1e-6)
    assert features["dist_52w_high"] == pytest.approx(0.0)        # rising: at its high
    assert features["vol_60d"] == pytest.approx(0.0, abs=1e-9)    # constant growth


def test_features_see_nothing_on_or_after_the_decision_day():
    """Point-in-time: a bar dated the decision day, or later, changes nothing."""
    bars = history(300)
    future = [bar(DAY, 5000.0), bar(sessions(DAY, 1)[0], 1.0)]

    assert forward.price_features(bars + future, DAY) == forward.price_features(bars, DAY)


def test_a_short_history_leaves_a_figure_absent_rather_than_guessed():
    features = forward.price_features(history(120), DAY)

    assert features["mom_12_1"] is None and features["mom_6_1"] is None
    assert features["dist_52w_high"] is None
    assert features["resid_mom_12_1"] is None
    assert features["ret_3m"] is not None and features["ret_1m"] is not None
    assert features["bars_used"] == 120


def test_relative_strength_is_the_gap_to_the_sector_etf():
    stock = history(300, growth=1.002)
    sector = history(300, growth=1.001)

    features = forward.price_features(stock, DAY, sector=sector)

    own = stock[-1]["close"] / stock[-64]["close"] - 1
    theirs = sector[-1]["close"] / sector[-64]["close"] - 1
    assert features["rs_3m_vs_sector"] == pytest.approx(own - theirs, abs=1e-6)
    assert features["rs_3m_vs_sector"] > 0
    assert forward.price_features(stock, DAY)["rs_3m_vs_sector"] is None      # no ETF bars


def test_residual_momentum_removes_what_beta_explains():
    """A stock that is exactly 2x the market every day has no residual; one
    with a daily drift on top of that keeps the drift and nothing else."""
    market = history(300)
    # A market that moves: alternate +1.2% / -0.8% days.
    level = 100.0
    for i, b in enumerate(market):
        level *= 1.012 if i % 2 else 0.992
        b["close"] = level

    def stock(alpha: float) -> list[dict]:
        out, price = [], 50.0
        for prev, cur in zip(market, market[1:]):
            price *= 1.0 + 2.0 * (cur["close"] / prev["close"] - 1.0) + alpha
            out.append(bar(cur["date"], price))
        return out

    flat = forward.price_features(stock(0.0), DAY, market=market)
    drift = forward.price_features(stock(0.001), DAY, market=market)

    assert flat["resid_mom_12_1"] == pytest.approx(0.0, abs=1e-6)
    # 231 daily returns in the 12-1 window (252 back to 21 back), 0.1% of
    # drift in each.
    assert drift["resid_mom_12_1"] == pytest.approx(0.231, abs=1e-3)
    # Raw momentum cannot tell the two apart from the market; the residual can.
    assert drift["mom_12_1"] > 0 and flat["mom_12_1"] > 0


def test_every_feature_key_is_always_present():
    assert set(forward.price_features([], DAY)) == set(forward.price_features(history(300), DAY))
