"""Sector-relative oversold rotation. Off by default.

    | Market condition | Any |
    | Entry | Buy the sector ETF (XL*) when its RSI <= 30 and it is above
    |       | SMA200 while SPY RSI > 40; whole shares only |
    | Exit  | RSI >= 55 or +4%; stop 1.5xATR; time 10 |
    | Data  | TD indicators on 11 ETFs (33 calls/day) |

The shadow ledger runs it as the `F_sector_play` variant; a config flag flips
it live once `edge_shadow_trades` says something.

**Why it is a separate play and not a MACRO mode.** Both propose sector ETFs
and both are called once per cycle rather than per symbol, which is the whole
of the resemblance. MACRO rotates on a macro *release* mapped through
`config/macro_map.json`, holds fifteen sessions and takes the 2xATR stop; C
rotates on the ETF's own oscillator with no release involved, holds ten and
takes 1.5xATR. Folding them would give one play two triggers, two clocks and
two stops, and `edge_play_state` would credit the wrong one for every outcome.
They can propose the same symbol on the same day; `entries._best_per_symbol`
settles that the way it settles every other double-trigger — one name is one
position.

**The trigger reads `regime_inputs`, not `signals`.** §4.3 C's stated data
source is the eleven ETFs the premarket job already reads from Twelve Data,
which is exactly what `regime.evaluate` was handed — so the trigger costs no
call at all, and it is measured in the same numbers the sector states in
`ctx["regime"]` were measured in. `ctx["signals"]` is the per-symbol block for
names the cycle *fetched*, and an ETF only lands there once something has
already proposed it; sizing and the exits read it for the ATR, but a trigger
that depended on it would fire only for ETFs some other play had already put
in the pool.

**"Any market condition" is honoured literally.** `SECTOR` is deliberately
absent from `regime.PLAY_IDS`, so `_regime_multiplier` returns 1.0 for it and a
risk-off tape does not dark it. That is what the row says, and it is the
hypothesis the ledger is meant to test: buying a sector that is oversold but
still above its own 200-day is a bet that does not need the broad tape's
permission. It is also the most aggressive thing about this play, which is
another reason it ships off.
"""

from __future__ import annotations

from typing import Any, Mapping

from .core import (
    ENTRY_CONDITIONS_UNMET,
    SECTOR,
    Skipped,
    _Proposal,
    _scaled,
)

# The switch. Absent from `config/config.json` on purpose — see the docstring.
CONFIG_KEY = "play_sector_enabled"

# §4.3 C's own numbers. Module constants and not config keys, per §17: the
# numbers are the hypothesis. (§4.3 A's are in `config/config.json` by an
# explicit, documented exception — see `dip.py` and docs/TUNING.md §4 — and
# that exception is not this play's.)
SECTOR_RSI_MAX = 30.0
SPY_RSI_MIN = 40.0

# How far below the ceiling is full conviction. Fifteen points — RSI 15 — is
# the same half-the-distance-to-the-floor reasoning `DIP_RSI_EXTREMITY_FULL_SCALE`
# uses, applied to this play's own ceiling.
SECTOR_RSI_FULL_SCALE = 15.0

# What an ETF sitting exactly on the RSI 30 line is worth. §4.3 C's trigger is
# a *binary* condition — "RSI <= 30 and above SMA200 while SPY RSI > 40" — with
# no conviction scale stated, so the oscillator here only grades how strongly a
# qualifying name qualifies. Without a floor an ETF at exactly 30.0 would score
# 0.0 and `entries._arbitrate` would drop it as NO_CONVICTION, which would make
# the play refuse the boundary case of its own stated trigger.
SECTOR_MIN_CONVICTION = 0.2


def enabled(ctx: Mapping[str, Any]) -> bool:
    """Whether §4.3 C is switched on for this cycle.
    """
    return bool((ctx.get("config") or {}).get(CONFIG_KEY))


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def symbols(ctx: Mapping[str, Any]) -> list[str]:
    """§5.2's eleven, in file order, or empty when the file is not in ctx.

    `config/etfs.json` rather than a list here, for a settled design
    decision's reason applied to a map: one authoritative copy. `broad` is
    excluded — SPY is the *reference* this play measures against, and buying
    the thing you measure relative strength against is not a rotation.
    """
    return sorted((ctx.get("etfs") or {}).get("sector") or {})


def _sector(ctx: Mapping[str, Any]) -> list[_Proposal | Skipped]:
    """§4.3 C's proposals for this cycle, or `[]` when the play is off.

    Empty rather than a list of skips when the play is off: a switched-off play
    has not looked at anything, and eleven `Skipped` rows a cycle saying so is
    how a runlog stops being read (`plays.core`'s own rule about non-triggering
    names).
    """
    if not enabled(ctx):
        return []

    inputs = ctx.get("regime_inputs") or {}
    spy_rsi = _number((inputs.get("spy") or {}).get("rsi14"))
    if spy_rsi is None:
        # No tape reading, no proposals — and silently, because this is a
        # property of the cycle's inputs rather than of any one ETF. Every
        # payload that predates `regime_inputs` lands here, which is what
        # makes turning this play on inert on an old cycle rather than wrong.
        return []

    blocks = inputs.get("sectors") or {}
    out: list[_Proposal | Skipped] = []

    for symbol in symbols(ctx):
        block = blocks.get(symbol) or {}
        rsi = _number(block.get("rsi14"))
        if rsi is None or rsi > SECTOR_RSI_MAX:
            continue

        last = _number(block.get("last"))
        sma200 = _number(block.get("sma200"))
        if last is None or sma200 is None:
            out.append(
                Skipped(
                    symbol,
                    SECTOR,
                    ENTRY_CONDITIONS_UNMET,
                    f"{symbol} is oversold at RSI {rsi:.0f} but its price or "
                    "200-day never arrived; §4.3 C will not buy an oversold "
                    "sector it cannot confirm the trend of",
                )
            )
            continue

        if last <= sma200:
            out.append(
                Skipped(
                    symbol,
                    SECTOR,
                    ENTRY_CONDITIONS_UNMET,
                    f"{symbol} is oversold at RSI {rsi:.0f} but at {last:.2f} "
                    f"is below its {sma200:.2f} 200-day; that is the sector "
                    "regime persisting, which is this play's own named failure "
                    "mode",
                )
            )
            continue

        if spy_rsi <= SPY_RSI_MIN:
            out.append(
                Skipped(
                    symbol,
                    SECTOR,
                    ENTRY_CONDITIONS_UNMET,
                    f"{symbol} is a clean oversold sector but SPY RSI "
                    f"{spy_rsi:.0f} is at or under {SPY_RSI_MIN:.0f}; §4.3 C "
                    "rotates between sectors, it does not catch a whole tape",
                )
            )
            continue

        out.append(
            _Proposal(
                symbol,
                SECTOR,
                max(
                    SECTOR_MIN_CONVICTION,
                    _scaled(SECTOR_RSI_MAX - rsi, SECTOR_RSI_FULL_SCALE),
                ),
                f"{symbol} at RSI {rsi:.0f} is oversold while holding its "
                f"{sma200:.2f} 200-day, and SPY RSI {spy_rsi:.0f} says the "
                "tape has not rolled over",
            )
        )

    return out
