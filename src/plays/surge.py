"""§17's SURGE — the full-send momentum play.

When `edge_control`'s `full_send_enabled` is true and a name is in a clean,
quickly-monetisable uptrend, the cycle may commit the *entire* available cash
across up to `SURGE_MAX_NAMES` such names, ignoring §4.4's slot, position and
sector caps. This module owns the trigger and the conviction scale; the
capital bypass is a `full_deployment` flag `sizing.size` and
`guard.concentration` both honour.

The switch lives in `edge_control`, not `config.json`, so an operator flips it
at runtime (`surge_on`/`surge_off`) with no deploy — it is an account-wide
risk switch like `trading_enabled`.

The gate is deliberately strict, because it unlocks concentration:

* **Stacked moving averages** — `price > sma_20 > sma_50 > sma_200`. An
  established uptrend on every horizon the daily cache carries, not a bounce.
* **Momentum RSI band** — `SURGE_RSI_MIN <= RSI(14) <= SURGE_RSI_MAX`. Trending,
  not chopping, and not blown off the top.
* **Move already underway** — `price >= sma_50 * (1 + SURGE_MOMENTUM_MIN)`. A
  short profit-booking dip inside the trend still clears this; a name only just
  reclaiming its 50-day does not.

The three thresholds are the documented loosening levers. Dropping
`SURGE_RSI_MIN` toward the fallback (stacked MAs alone) is the operator's own
"Option 2" from an earlier discussion — use it if pick counts run too low.

Governed by the switch, not by a daily `edge_play_state` regime row:
`entries._arbitrate` reads an absent SURGE regime as 1.0, though an explicit
0.0 still darks it like any play.
"""

from __future__ import annotations

from typing import Any, Mapping

from .core import (
    ENTRY_CONDITIONS_UNMET,
    NO_CONVICTION,
    SURGE,
    Skipped,
    _earnings_veto,
    _price,
    _Proposal,
    _scaled,
    _signals,
)

SURGE_RSI_MIN = 55.0
SURGE_RSI_MAX = 80.0
# Price must be at least this far above its 50-day for the move to count as
# established rather than nascent.
SURGE_MOMENTUM_MIN = 0.02
# Full conviction at 15% above the 50-day; linear below that.
SURGE_CONVICTION_FULL_SCALE = 0.15
# Same three-session earnings clearance DIP uses — a full-send position is the
# last one that should be open across a print.
SURGE_EARNINGS_CLEARANCE_SESSIONS = 3


def _surge(symbol: str, ctx: Mapping[str, Any]) -> _Proposal | Skipped | None:
    """§17. A clean uptrend the cycle is allowed to go all-in on."""
    if not bool((ctx.get("control") or {}).get("full_send_enabled", False)):
        return None

    signal = _signals(ctx, symbol)
    price = _price(ctx, symbol)
    sma_20 = signal.get("sma_20")
    sma_50 = signal.get("sma_50")
    sma_200 = signal.get("sma_200")
    rsi = signal.get("rsi")

    if price is None or sma_20 is None or sma_50 is None or sma_200 is None or rsi is None:
        return None

    price = float(price)
    sma_20 = float(sma_20)
    sma_50 = float(sma_50)
    sma_200 = float(sma_200)
    rsi = float(rsi)

    if not (price > sma_20 > sma_50 > sma_200):
        return Skipped(
            symbol,
            SURGE,
            ENTRY_CONDITIONS_UNMET,
            f"{symbol} is not in a stacked uptrend (price {price:.2f}, 20d "
            f"{sma_20:.2f}, 50d {sma_50:.2f}, 200d {sma_200:.2f})",
        )

    if not (SURGE_RSI_MIN <= rsi <= SURGE_RSI_MAX):
        return Skipped(
            symbol,
            SURGE,
            ENTRY_CONDITIONS_UNMET,
            f"{symbol} RSI {rsi:.0f} is outside the "
            f"{SURGE_RSI_MIN:.0f}-{SURGE_RSI_MAX:.0f} momentum band",
        )

    if price < sma_50 * (1.0 + SURGE_MOMENTUM_MIN):
        return Skipped(
            symbol,
            SURGE,
            ENTRY_CONDITIONS_UNMET,
            f"{symbol} at {price:.2f} is under {SURGE_MOMENTUM_MIN:.0%} above "
            f"its 50-day ({sma_50:.2f}); the move is not yet established",
        )

    veto = _earnings_veto(symbol, ctx, SURGE, SURGE_EARNINGS_CLEARANCE_SESSIONS)
    if veto is not None:
        return veto

    conviction = _scaled((price / sma_50) - 1.0, SURGE_CONVICTION_FULL_SCALE)
    if conviction <= 0.0:
        return Skipped(
            symbol,
            SURGE,
            NO_CONVICTION,
            f"{symbol} clears the gate but scores no conviction above its 50-day",
        )

    return _Proposal(
        symbol,
        SURGE,
        conviction,
        f"stacked uptrend, RSI {rsi:.0f}, {(price / sma_50) - 1:.1%} above the "
        "50-day — full-send candidate",
    )
