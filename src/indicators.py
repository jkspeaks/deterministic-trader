"""indicators.py — RSI(14)/SMA(20,50,200)/ATR(14) computed from raw daily bars.

Written to replace `get_equity_technical_indicators` (one call per symbol per
indicator — 5 calls/symbol: rsi, sma x3, atr, the dominant line item in
§5.3's per-candidate budget) with `get_equity_historicals` (raw OHLCV bars for
up to 10 symbols in one call) plus this module's own computation. **Not wired
into the fetch plan — do not use for a live entry/exit decision.**

`sma_*` is therefore trustworthy as implemented. Wilder-smoothed `rsi`/ `atr`
are not: this file's Wilder recursion read 41.92/8.81 against the broker's
33.02/9.15 on the same bars; a second implementation using a standard EMA
(alpha = 2/(period+1)) instead of Wilder's (alpha = 1/period) came far closer —
33.17/8.75 — but still off by ~0.15 RSI points and ~4.5% on ATR, converged (280
bars is far past either recursion's warm-up), so the gap is a real formula
difference against Robinhood's undocumented internals, not a seeding artifact a
longer lookback would fix. A ~0.15-point RSI error is exactly wide enough to
flip a `<= 30` gate near the boundary, which is why this stays unwired until
someone either finds the exact formula or accepts the residual explicitly —
silently trading on a recomputed number that doesn't match the broker's is
precisely what Absolute 2 exists to prevent.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

RSI_PERIOD = 14
ATR_PERIOD = 14
SMA_PERIODS = (20, 50, 200)


def _closes(bars: Sequence[Mapping[str, Any]]) -> list[float]:
    return [float(bar["close"]) for bar in bars]


def sma(closes: Sequence[float], period: int) -> float | None:
    """The plain average of the last `period` closes, or `None` if short."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    return sum(window) / period


def rsi(closes: Sequence[float], period: int = RSI_PERIOD) -> float | None:
    """The textbook Wilder RSI — smoothed over the whole series supplied.

    Needs `period + 1` closes at minimum (one lost to the first diff) to
    produce anything at all; the seed average uses the first `period`
    changes and every change after that updates it by Wilder's recurrence
    (alpha = 1/period). **This is not confirmed to be what
    `get_equity_technical_indicators` itself computes** — see the module
    docstring's live comparison — so a caller matching the broker's own RSI
    exactly should not use this function yet.
    """
    if len(closes) < period + 1:
        return None

    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_gain == 0 and avg_loss == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(bars: Sequence[Mapping[str, Any]], period: int = ATR_PERIOD) -> float | None:
    """Wilder's ATR — the same true-range definition and smoothing as RSI's,
    applied to `max(high-low, |high-prev_close|, |low-prev_close|)`.
    """
    if len(bars) < period + 1:
        return None

    true_ranges: list[float] = []
    for i in range(1, len(bars)):
        high = float(bars[i]["high"])
        low = float(bars[i]["low"])
        prev_close = float(bars[i - 1]["close"])
        true_ranges.append(
            max(high - low, abs(high - prev_close), abs(low - prev_close))
        )

    value = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def compute_signals(bars: Sequence[Mapping[str, Any]]) -> dict:
    """One symbol's `signals` entry, from its bars alone — bars sorted
    oldest first, matching `get_equity_historicals`' own return order.

    A period `sma`/`rsi`/`atr` can't reach (too little history) comes back
    `None`, the same way a `get_equity_technical_indicators` call `cli.py`
    never got to make would have left the key absent — `plays.py` already
    treats a missing/`None` signal as "do not trigger", not as zero.
    """
    closes = _closes(bars)
    return {
        **{f"sma_{period}": sma(closes, period) for period in SMA_PERIODS},
        "rsi": rsi(closes),
        "atr": atr(bars),
    }
