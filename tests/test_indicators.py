"""src/indicators.py — RSI(14)/SMA/ATR computed from raw bars, no broker call.

Property checks here (monotone series, constant series) catch a coding bug
in the Wilder recursion — they cannot confirm the recursion matches the
broker's own `get_equity_technical_indicators`, which turned out not to
match closely enough to trust; see the module docstring's live comparison.
Not wired into the fetch plan for exactly that reason.
"""

import pytest

from src import indicators


def bar(close, high=None, low=None):
    return {
        "close": close,
        "high": high if high is not None else close,
        "low": low if low is not None else close,
    }


# --- sma ---------------------------------------------------------------


def test_sma_is_the_plain_average_of_the_window():
    closes = [float(i) for i in range(1, 21)]  # 1..20
    assert indicators.sma(closes, 20) == 10.5


def test_sma_ignores_history_outside_the_window():
    closes = [1000.0] * 5 + [float(i) for i in range(1, 21)]
    assert indicators.sma(closes, 20) == 10.5


def test_sma_is_none_when_history_is_too_short():
    assert indicators.sma([1.0, 2.0, 3.0], 20) is None


# --- rsi -----------------------------------------------------------------


def test_rsi_is_100_when_every_change_is_a_gain():
    closes = [float(i) for i in range(1, 30)]
    assert indicators.rsi(closes, period=14) == 100.0


def test_rsi_is_0_when_every_change_is_a_loss():
    closes = [float(i) for i in range(30, 1, -1)]
    assert indicators.rsi(closes, period=14) == 0.0


def test_rsi_is_50_on_a_flat_series():
    closes = [100.0] * 20
    assert indicators.rsi(closes, period=14) == 50.0


def test_rsi_is_none_when_history_is_too_short():
    assert indicators.rsi([1.0] * 10, period=14) is None


def test_rsi_matches_independent_wilder_recursion():
    """A second, differently-written implementation of the same Wilder
    recursion (EMA form: alpha = 1/period, seeded by the simple average of
    the first `period` changes) — catches an indicators.py coding bug that
    a property test on monotone/flat series would not."""
    closes = [
        44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84,
        46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41,
        46.22, 45.64,
    ]
    period = 14
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(c, 0.0) for c in changes]
    losses = [max(-c, 0.0) for c in changes]
    alpha = 1.0 / period
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = avg_gain + alpha * (g - avg_gain)
        avg_loss = avg_loss + alpha * (l - avg_loss)
    expected = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

    assert indicators.rsi(closes, period=period) == pytest.approx(expected)


# --- atr -------------------------------------------------------------------


def test_atr_of_a_constant_true_range_is_that_range():
    bars = [bar(close=100.0, high=105.0, low=95.0) for _ in range(20)]
    assert indicators.atr(bars, period=14) == 10.0


def test_atr_is_none_when_history_is_too_short():
    bars = [bar(close=100.0, high=101.0, low=99.0) for _ in range(10)]
    assert indicators.atr(bars, period=14) is None


def test_atr_uses_true_range_not_just_the_days_own_high_low():
    """A gap up whose prior close sits above the day's own high/low widens
    true range beyond `high - low` — the classic case Wilder's formula
    exists to cover, and a naive `high - low` ATR would understate it."""
    bars = [bar(close=100.0, high=101.0, low=99.0) for _ in range(14)]
    bars.append(bar(close=120.0, high=121.0, low=119.0))  # gapped up hard
    result = indicators.atr(bars, period=14)
    naive = sum(b["high"] - b["low"] for b in bars[1:]) / 14
    assert result > naive


# --- compute_signals ---------------------------------------------------


def test_compute_signals_returns_all_five_keys():
    bars = [bar(close=float(100 + i), high=float(101 + i), low=float(99 + i)) for i in range(210)]
    result = indicators.compute_signals(bars)
    assert set(result) == {"sma_20", "sma_50", "sma_200", "rsi", "atr"}
    assert all(value is not None for value in result.values())


def test_compute_signals_leaves_unreachable_periods_none():
    bars = [bar(close=float(i)) for i in range(30)]
    result = indicators.compute_signals(bars)
    assert result["sma_20"] is not None
    assert result["sma_50"] is None
    assert result["sma_200"] is None
