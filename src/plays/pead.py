"""§6.1 — post-earnings drift."""

from __future__ import annotations

from typing import Any, Mapping

from .core import (
    ENTRY_CONDITIONS_UNMET,
    PEAD,
    SKIP_NO_REACTION_ROW,
    SKIP_NO_SURPRISE,
    SKIP_NO_TREND_DATA,
    SKIP_REACTION_PENDING,
    Skipped,
    _price,
    _Proposal,
    _scaled,
    _signals,
)

# §6.1. A report 1–3 sessions back triggers; the beat must be positive and the
# tape must have confirmed it by +2%. Conviction is the surprise, full at 10%.
# The window opens at one session and not zero: the reaction *is* the entry
# condition, so there is nothing to confirm on the day it prints.
PEAD_WINDOW_SESSIONS = (1, 3)
PEAD_MIN_REACTION_PCT = 0.02
PEAD_SURPRISE_FULL_SCALE_PCT = 10.0
# Mirroring `catalyst.CATALYST_REACTION_FULL_SCALE_PCT`: the surprise alone
# let an 8%-gap reaction and a barely-confirming 2% one tie whenever their
# surprise magnitude matched, though the tape's own reaction is evidence too.
# Full conviction at the same 10% move CATALYST uses. `max`, not an average,
# so a name that already clears the gate on surprise alone is never marked
# *down* for a merely adequate reaction — this only lets an unusually strong
# tape reaction lift conviction past what the surprise says.
PEAD_REACTION_FULL_SCALE_PCT = 0.10

AM = "am"


def reaction_due(earnings: Mapping[str, Any]) -> bool:
    """Whether the reaction session has closed, so its row *should* exist.

    `pead_reaction.py`'s own rule: a report before the open (`am`) is digested
    the same session, so its reaction bar is complete by the next morning —
    one session after. Anything else (`pm`, or unknown, which takes the `pm`
    rule) reacts the *next* session, whose bar is complete two sessions after
    the report. Before that the row cannot exist and its absence is not a gap.
    """
    since = earnings.get("sessions_since_report")
    if since is None:
        return False
    return int(since) >= (1 if earnings.get("timing") == AM else 2)


def _pead(symbol: str, ctx: Mapping[str, Any]) -> _Proposal | Skipped | None:
    """§6.1. Post-earnings drift — the best-evidenced anomaly on this horizon.

    All three entry clauses are one idea: the market under-reacted to a real
    positive surprise and is still catching up. A beat the tape ignored is not
    drift, and a rally on a miss is somebody else's trade.

    **A missing input is named before any threshold is tested**. `reaction
    None` used to fold into "does not clear §6.1" — 67 of 79 PEAD skips —
    which read as the tape refusing a name the system had simply never
    measured. Nothing is inferred in its place: no reaction row means no
    entry.
    """
    earnings = (ctx.get("earnings") or {}).get(symbol) or {}
    since = earnings.get("sessions_since_report")
    first, last = PEAD_WINDOW_SESSIONS
    if since is None or not first <= int(since) <= last:
        return None

    surprise = earnings.get("eps_surprise_pct")
    reaction = earnings.get("reaction_pct")
    price = _price(ctx, symbol)
    sma_50 = _signals(ctx, symbol).get("sma_50")

    def refuse(code: str, message: str) -> Skipped:
        return Skipped(symbol, PEAD, code, message)

    if reaction is None:
        if not reaction_due(earnings):
            return refuse(
                SKIP_REACTION_PENDING,
                f"{symbol} reported {since} session(s) ago after the open; its "
                "reaction session has not closed, so there is no move to "
                "confirm yet",
            )
        return refuse(
            SKIP_NO_REACTION_ROW,
            f"{symbol} reported {since} session(s) ago and its reaction session "
            "has closed, but there is no reaction row for it; the pre-market "
            "job did not compute one",
        )
    if surprise is None:
        return refuse(
            SKIP_NO_SURPRISE,
            f"{symbol} reported {since} session(s) ago with no EPS surprise "
            "figure (no estimate, or a zero one), so the beat cannot be sized",
        )
    if price is None or sma_50 is None:
        return refuse(
            SKIP_NO_TREND_DATA,
            f"{symbol} reported {since} session(s) ago but has "
            f"{'no last price' if price is None else 'no 50-day'} to judge the "
            "trend against",
        )

    failed = []
    if float(surprise) <= 0:
        failed.append(f"surprise {float(surprise):+.1f}% is not a beat")
    if float(reaction) < PEAD_MIN_REACTION_PCT:
        failed.append(
            f"reaction {float(reaction):+.1%} is under the "
            f"{PEAD_MIN_REACTION_PCT:+.0%} confirmation"
        )
    if price <= float(sma_50):
        failed.append(f"price {price:.2f} is not above its 50-day {float(sma_50):.2f}")
    if failed:
        return refuse(
            ENTRY_CONDITIONS_UNMET,
            f"{symbol} reported {since} session(s) ago but does not clear §6.1: "
            + "; ".join(failed),
        )

    surprise_conviction = _scaled(float(surprise), PEAD_SURPRISE_FULL_SCALE_PCT)
    reaction_conviction = _scaled(float(reaction), PEAD_REACTION_FULL_SCALE_PCT)
    conviction = max(surprise_conviction, reaction_conviction)

    return _Proposal(
        symbol,
        PEAD,
        conviction,
        f"beat by {float(surprise):.1f}% {since} session(s) ago, tape confirmed "
        f"at {float(reaction):+.1%}, above the 50-day",
    )
