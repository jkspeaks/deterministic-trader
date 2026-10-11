"""dip_recovery.signal — the entry gate and its 0-100 dip score.

The design doc's entry checklist names seven conditions. Five are cleared from
the daily indicator cache core already fetches and trusts: RSI(14) and
relative volume are hard gates, price below its 20-day mean is a hard gate,
and the deterministic 0-100 score (>= `dip_score_min`) blends RSI extremity
with 20-day depth.

The structural filter is a hard gate too, relaxed (operator). `plays.dip`
still demands a bare 200-day uptrend ("a falling knife, not a dip"), which by
design never fires on a down tape — the exact tape this strategy exists to
trade. Here the gate is instead a *band*: price may sit up to `trend_band_pct`
(default 10%) below its 200-day and still qualify, so a name that has pulled
back through its long trend is in scope while one in a genuine structural
collapse is not. A second, independent guard vetoes a freefall directly — a
drop of `freefall_drop_pct` (default 15%) or more from the close
`freefall_window_sessions` (default 10) bars back is a crash, not a dip,
whatever the 200-day says. Both thresholds live in `entry_thresholds` with
inline defaults, so the gate is correct on a database that lacks them.

The remaining two — "price >=2.0% below intraday high, >=4.0% below 5-day
high" and "price at or below the lower Bollinger Band" — need a raw daily bar
series. Phase 5 wires that: the Dip Monitor routine runs one
`get_equity_historicals` call per watchlist symbol (`interval=day`, enough
history to cover `bollinger_period`) and passes the bars in as
`ctx["dip_recovery_bars"][symbol]`, oldest first, each bar shaped
`{"high": ..., "low": ..., "close": ...}` — the same bar contract
`indicators.compute_signals` already defines for that tool. From those bars
this module derives the intraday high (the last bar's high), the 5-day high
(the max high of the last five bars) and the lower Bollinger Band (a
`bollinger_period`-day close SMA minus `bollinger_k` population standard
deviations). All three are hard gates, matching the doc's "a new position
opens only when all of the following pass". `entry_thresholds` carries every
threshold: `min_dip_pct_intraday` / `min_dip_pct_5day` and
`bollinger_period` / `bollinger_k`, each with an inline default so
the gate is correct even before `013` is applied.

Missing or too-short bars fail the gate closed (a `Skipped`, never a silent
pass) — the same "a missing signal means do not trigger, not zero" rule
`plays.py` holds itself to. The 0-100 score still scores 20-day depth, not the
new bar-derived depth: the score's internals are this package's own choice
(the doc specifies only the >= 70 threshold), the 20-day figure is the one
already live and tested, and folding the bar depth in would re-tune a shipped
score for no gain the gates do not already deliver.

"Every existing portfolio, account, concentration, and guardrail check
passes" is not re-implemented here at all — `guard.evaluate` already applies
its full suite to any order carrying `ctx["strategy"] = "dip_recovery"`, and a
second copy of cash/concentration/legality logic in this package could only
ever drift from guard's, never improve on it.
"""

from __future__ import annotations

from typing import Any, Mapping

from .core import (
    ENTRY_CONDITIONS_UNMET,
    METRICS_UNAVAILABLE,
    EntryProposal,
    Skipped,
    _price,
    _relvol,
    _relvol_reading,
    _signals,
    _threshold_group,
    SKIP_TOO_EARLY_FOR_RVOL,
)

# Half the score from how oversold RSI reads relative to its own gate, half
# from how far price has fallen below its 20-day mean relative to the plan's
# own 5-day-depth threshold (the nearest cache-native proxy — the bar-derived
# 5-day-high depth is a hard gate below, deliberately not a score input; see
# the module docstring). Deliberately the same 50/50 split as neither factor
# alone deciding "is this a dip": `plays.dip`'s own `max(depth, extremity)`
# lets one strong signal carry the trade, but the plan doc asks for a *score*,
# which reads as "how much of the case is here", not "is any of it here" — a
# sum rather than a max.
_RSI_WEIGHT = 50.0
_DEPTH_WEIGHT = 50.0

# The "5-day high" lookback: the highest high over the last five daily bars.
# A fixed part of the rule as the design doc states it ("≥4.0% below 5-day
# high"), deliberately not a `dip_recovery_config` tunable — unlike the
# Bollinger period, changing it would change what "5-day high" *means*, not
# just tighten a threshold. Named rather than inlined because the sibling
# intraday-high gate is "the last bar" and this one is "the last five", so the
# 5 is a real parameter of the rule, not a magic slice bound.
_FIVE_DAY_WINDOW = 5


def _scaled(value: float, full_scale: float) -> float:
    if full_scale <= 0:
        return 0.0
    return max(0.0, min(1.0, value / full_scale))


def _bar_metrics(symbol: str, ctx: Mapping[str, Any]) -> dict[str, float] | None:
    """Intraday high, 5-day high and the lower Bollinger Band, from the daily
    bars `ctx["dip_recovery_bars"][symbol]` the Dip Monitor routine fetches.

    ``None`` when there is not enough history for a `bollinger_period`-day
    band — the caller turns that into a `Skipped`, never a pass.
    """
    bars = (ctx.get("dip_recovery_bars") or {}).get(symbol) or []
    thresholds = _threshold_group(ctx, "entry_thresholds")
    period = int(thresholds.get("bollinger_period", 20))
    k = float(thresholds.get("bollinger_k", 2.0))
    freefall_window = int(thresholds.get("freefall_window_sessions", 10))

    if len(bars) < period or len(bars) < _FIVE_DAY_WINDOW:
        return None

    try:
        highs = [float(bar["high"]) for bar in bars]
        closes = [float(bar["close"]) for bar in bars]
    except (KeyError, TypeError, ValueError):
        return None

    window = closes[-period:]
    mean = sum(window) / period
    std = (sum((close - mean) ** 2 for close in window) / period) ** 0.5

    # The close `freefall_window` bars back, clamped to the oldest bar on hand
    # so a short series still yields a comparison rather than an index error.
    freefall_ref = closes[-(freefall_window + 1)] if len(closes) > freefall_window else closes[0]

    return {
        "intraday_high": highs[-1],
        "five_day_high": max(highs[-_FIVE_DAY_WINDOW:]),
        "bollinger_lower": round(mean - k * std, 6),
        "freefall_ref_close": freefall_ref,
    }


def _entry_score(
    symbol: str, ctx: Mapping[str, Any], metrics: Mapping[str, float]
) -> tuple[float, dict[str, float]]:
    """The 0-100 figure `dip_score_min` gates against, plus its factors for
    `dip_recovery_evaluations`. Callers must have already checked the hard
    gates below — this does not repeat them.

    ``metrics`` is `_bar_metrics`' result, passed in rather than recomputed:
    the bar gates run before scoring and have already established it is not
    ``None``, so taking it as a parameter both saves the second pass over the
    bar series and makes that ordering a fact of the signature instead of an
    assumption in prose.
    """
    signal = _signals(ctx, symbol)
    thresholds = _threshold_group(ctx, "entry_thresholds")
    rsi_max = float(thresholds.get("rsi_max", 35))
    min_dip_pct_5day = float(thresholds.get("min_dip_pct_5day", 4.0))

    rsi = float(signal["rsi"])
    price = _price(ctx, symbol)
    sma_20 = signal.get("sma_20")
    depth_pct = 0.0 if not sma_20 or price is None else (float(sma_20) - price) / float(sma_20) * 100.0

    rsi_component = _RSI_WEIGHT * _scaled(rsi_max - rsi, rsi_max)
    depth_component = _DEPTH_WEIGHT * _scaled(depth_pct, min_dip_pct_5day)
    score = round(rsi_component + depth_component, 4)

    factors = {
        "rsi": rsi,
        "relvol": _relvol(ctx, symbol),
        "relvol_tnorm": _relvol_reading(ctx, symbol)["tnorm"],
        "depth_pct_below_sma20": round(depth_pct, 4),
        "rsi_component": round(rsi_component, 4),
        "depth_component": round(depth_component, 4),
    }

    if price is not None:
        intraday_high = metrics["intraday_high"]
        five_day_high = metrics["five_day_high"]
        factors["pct_below_intraday_high"] = round((intraday_high - price) / intraday_high * 100.0, 4)
        factors["pct_below_5day_high"] = round((five_day_high - price) / five_day_high * 100.0, 4)
        factors["bollinger_lower"] = metrics["bollinger_lower"]

    return score, factors


def entry_signal(symbol: str, ctx: Mapping[str, Any]) -> EntryProposal | Skipped:
    """§entry conditions, all seven this phase evaluates. A proposal here is
    not yet an order — `engine.evaluate` still owns the 5-position and
    3-per-day caps, and `guard.evaluate` still owns every legality/account
    check before anything places.
    """
    thresholds = _threshold_group(ctx, "entry_thresholds")
    rsi_max = float(thresholds.get("rsi_max", 35))
    relvol_min = float(thresholds.get("relvol_min", 1.2))
    dip_score_min = float(thresholds.get("dip_score_min", 70))
    min_dip_pct_intraday = float(thresholds.get("min_dip_pct_intraday", 2.0))
    min_dip_pct_5day = float(thresholds.get("min_dip_pct_5day", 4.0))
    trend_band_pct = float(thresholds.get("trend_band_pct", 0.10))
    freefall_drop_pct = float(thresholds.get("freefall_drop_pct", 15.0))
    freefall_window = int(thresholds.get("freefall_window_sessions", 10))

    # `rsi is None` (the reading is missing) and `rsi > rsi_max` (the reading is
    # present and simply too high) used to collapse into one message and one
    # code — indistinguishable from each other in `dip_recovery_evaluations` and
    # in the alert text, so a real RSI outage would have read exactly like an
    # ordinary, expected rejection (where the reading was in fact present at
    # 39.7/39.6 legitimate, but the message gave no way to tell that from here).
    # `watchlist.py`'s `_metrics` gate already draws this line with
    # `METRICS_UNAVAILABLE` ("a missing factor is not a zero one"); this gate
    # now follows the same convention.
    signal = _signals(ctx, symbol)
    rsi = signal.get("rsi")
    if rsi is None:
        return Skipped(
            symbol, METRICS_UNAVAILABLE,
            f"{symbol} has no cached RSI reading",
        )
    if float(rsi) > rsi_max:
        return Skipped(
            symbol, ENTRY_CONDITIONS_UNMET,
            f"{symbol}'s RSI ({float(rsi):.1f}) is above the {rsi_max:g} ceiling",
        )

    reading = _relvol_reading(ctx, symbol)
    relvol, tnorm, minutes = reading["raw"], reading["tnorm"], reading["minutes"]
    if relvol is None:
        return Skipped(
            symbol, METRICS_UNAVAILABLE,
            f"{symbol} has no relative volume — today's volume or its average "
            "is missing from fundamentals",
        )
    # What the time-normalised reading is, on every relvol rejection whichever
    # rule decided, so the two can be compared from the evaluations log.
    scaled = (
        "" if tnorm is None
        else f"; {tnorm:.4f} time-normalised at minute {minutes:.0f}"
    )
    if reading["normalised"]:
        if minutes < reading["min_minutes"]:
            return Skipped(
                symbol, SKIP_TOO_EARLY_FOR_RVOL,
                f"{symbol} is at minute {max(minutes, 0.0):.0f} of the session; "
                "relative volume is not extrapolated before minute "
                f"{reading['min_minutes']:.0f} ({relvol:.4f} of a full day so far)",
            )
        if tnorm < relvol_min:
            return Skipped(
                symbol, ENTRY_CONDITIONS_UNMET,
                f"{symbol}'s time-normalised relative volume ({tnorm:.4f} at "
                f"minute {minutes:.0f}; {relvol:.4f} of a full day) is below "
                f"the {relvol_min:g}x floor",
            )
    elif relvol < relvol_min:
        return Skipped(
            symbol, ENTRY_CONDITIONS_UNMET,
            f"{symbol}'s relative volume ({relvol:.4f}) is below the "
            f"{relvol_min:g}x floor{scaled}",
        )

    price = _price(ctx, symbol)
    sma_200 = signal.get("sma_200")
    if price is None or sma_200 is None:
        return Skipped(
            symbol, ENTRY_CONDITIONS_UNMET,
            f"{symbol} has no price or no 200-day to size the structural "
            "filter against",
        )
    trend_floor = float(sma_200) * (1.0 - trend_band_pct)
    if price <= trend_floor:
        return Skipped(
            symbol, ENTRY_CONDITIONS_UNMET,
            f"{symbol} at {price:.2f} is more than {trend_band_pct:.0%} below "
            f"its 200-day ({float(sma_200):.2f}); that is a structural "
            "breakdown, not a dip",
        )

    sma_20 = signal.get("sma_20")
    if not sma_20 or price >= float(sma_20):
        return Skipped(
            symbol, ENTRY_CONDITIONS_UNMET,
            f"{symbol} is not below its 20-day mean, so there is no depth to "
            "score a dip against",
        )

    metrics = _bar_metrics(symbol, ctx)
    if metrics is None:
        return Skipped(
            symbol, ENTRY_CONDITIONS_UNMET,
            f"{symbol} has no usable daily bar history for the intraday/5-day "
            "high and Bollinger checks",
        )

    freefall_ref = metrics["freefall_ref_close"]
    freefall_pct = (freefall_ref - price) / freefall_ref * 100.0 if freefall_ref > 0 else 0.0
    if freefall_pct >= freefall_drop_pct:
        return Skipped(
            symbol, ENTRY_CONDITIONS_UNMET,
            f"{symbol} is {freefall_pct:.1f}% below its close {freefall_window} "
            f"sessions ago, past the {freefall_drop_pct:g}% freefall veto; a "
            "crash is not a dip",
        )

    intraday_high = metrics["intraday_high"]
    below_intraday = (intraday_high - price) / intraday_high * 100.0 if intraday_high > 0 else 0.0
    if below_intraday < min_dip_pct_intraday:
        return Skipped(
            symbol, ENTRY_CONDITIONS_UNMET,
            f"{symbol} is only {below_intraday:.2f}% below its intraday high, "
            f"short of the {min_dip_pct_intraday:g}% floor",
        )

    five_day_high = metrics["five_day_high"]
    below_5day = (five_day_high - price) / five_day_high * 100.0 if five_day_high > 0 else 0.0
    if below_5day < min_dip_pct_5day:
        return Skipped(
            symbol, ENTRY_CONDITIONS_UNMET,
            f"{symbol} is only {below_5day:.2f}% below its 5-day high, short "
            f"of the {min_dip_pct_5day:g}% floor",
        )

    # A degenerate band (zero-variance closes → `lower_band == mean`, or bad
    # data pushing it negative) still resolves this comparison the fail-closed
    # way: any real `price` sits at or above it, so the symbol is skipped, not
    # bought on a band that means nothing.
    lower_band = metrics["bollinger_lower"]
    if price > lower_band:
        return Skipped(
            symbol, ENTRY_CONDITIONS_UNMET,
            f"{symbol} at {price:.2f} is above its lower Bollinger Band "
            f"({lower_band:.2f}); no band-relative dip to buy",
        )

    score, factors = _entry_score(symbol, ctx, metrics)
    if score < dip_score_min:
        return Skipped(
            symbol, ENTRY_CONDITIONS_UNMET,
            f"{symbol} scores {score:.1f}, below the {dip_score_min:g} floor",
        )

    return EntryProposal(
        symbol,
        score,
        factors,
        f"RSI {rsi:.0f}, {below_intraday:.1f}% below the intraday high, "
        f"{below_5day:.1f}% below the 5-day high, at/under the lower "
        f"Bollinger Band ({lower_band:.2f}), {relvol:.2f}x relative volume, "
        "holding the 200-day",
    )
