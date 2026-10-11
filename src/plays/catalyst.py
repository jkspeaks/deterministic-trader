"""§6.3 — event-driven, direction confirmed by price."""

from __future__ import annotations

import math
from typing import Any, Mapping

from src import cadence

from .core import (
    CATALYST,
    SKIP_BELOW_MIN_MOVE,
    SKIP_BELOW_RVOL,
    SKIP_BELOW_RVOL_TNORM,
    SKIP_NO_CHANGE,
    SKIP_NO_VOLUME,
    SKIP_TOO_EARLY_FOR_RVOL,
    Skipped,
    _events,
    _Proposal,
    _scaled,
    _sessions_ago,
    minutes_since_open,
    session_minutes,
)

# §6.3. An 8-K within two sessions or a headline cluster triggers; price must
# have risen 3% on 1.5x volume to confirm direction. §6.3 does not state a
# conviction scale, so it follows the other three and takes the magnitude of
# the confirming move, full at 10%.
CATALYST_WINDOW_SESSIONS = 2
CATALYST_MIN_REACTION_PCT = 0.03
CATALYST_MIN_VOLUME_MULTIPLE = 1.5
CATALYST_REACTION_FULL_SCALE_PCT = 0.10
CATALYST_CLUSTER_HEADLINES = 3

# The volume test divides today's volume *so far* by
# a *full day's* average, so at the 09:40 cycle it cannot reach 1.5x — a stock
# up 7.0% at 09:44 on 0.16x is refused. Under `catalyst_rvol_time_normalised`
# the multiple is scaled to a full session:
#
#     rvol_tnorm = (volume / average_volume) * (session_minutes / minutes_elapsed)
#
# and refused outright before `catalyst_min_minutes`, because a 14-minute
# print scaled 28x is noise rather than a reading. Off by default: with the
# switch off the raw multiple decides, exactly as before, and both figures are
# recorded on every verdict so the two rules can be compared.
RVOL_TIME_NORMALISED = "catalyst_rvol_time_normalised"
MIN_MINUTES = "catalyst_min_minutes"
MIN_MINUTES_DEFAULT = 30


def triggered(symbol: str, ctx: Mapping[str, Any]) -> bool:
    """Whether §6.3's event leg fires for `symbol` — an 8-K or a headline
    cluster inside the window. Needs only `events`, `cycle.trading_day` and
    `calendar` off `ctx`.

    The one question `get_equity_fundamentals` hangs on: `_catalyst` reads
    `volume`/`average_volume` only past this line, and no other play reads a
    live fundamentals field. The hosted core's `resolve` asks the same function
    which names the runner must fetch fundamentals for
    (`run._fundamentals_needed`), so the narrowing cannot drift from the play.
    """
    for event in _events(ctx, symbol):
        ago = _sessions_ago(event, ctx, "filed_day")
        if ago is None or int(ago) > CATALYST_WINDOW_SESSIONS:
            continue
        if event.get("type") == "8-K" or int(event.get("headline_count") or 0) >= CATALYST_CLUSTER_HEADLINES:
            return True
    return False


def _number(value: Any) -> float | None:
    """A finite float, or None. Vendor fields arrive as strings."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else number


def time_normalised(ctx: Mapping[str, Any]) -> bool:
    return bool((ctx.get("config") or {}).get(RVOL_TIME_NORMALISED))


def _min_minutes(ctx: Mapping[str, Any]) -> float:
    value = _number((ctx.get("config") or {}).get(MIN_MINUTES))
    return float(MIN_MINUTES_DEFAULT) if value is None else value


def relative_volume(symbol: str, ctx: Mapping[str, Any]) -> dict[str, Any]:
    """The volume reading, both ways, and the clock it was normalised on.

    ``rvol_raw`` is today's volume over the full-day average, or None when
    either is missing or zero. ``rvol_tnorm`` scales it to a full session and
    is None when the raw figure is, when there is no clock, or before the
    open. ``minutes_elapsed`` is as measured; the scaling clamps it to the
    session, so a reading after the close is the raw one.
    """
    fundamentals = (ctx.get("fundamentals") or {}).get(symbol) or {}
    minutes = minutes_since_open(ctx)
    raw, tnorm = cadence.time_normalised_volume(
        _number(fundamentals.get("volume")),
        _number(fundamentals.get("average_volume")),
        minutes,
        session_minutes(ctx),
    )
    return {"rvol_raw": raw, "rvol_tnorm": tnorm, "minutes_elapsed": minutes}


def _catalyst(symbol: str, ctx: Mapping[str, Any]) -> _Proposal | Skipped | None:
    """§6.3. Event-driven, and the one play whose direction comes from price.

    An item code says *something material happened*, never whether it was good.
    Requiring a confirming move on confirming volume avoids sentiment
    classification entirely — a deliberate refusal to build the least reliable
    component. It also sidesteps v2's Form 4 trap, where a broker's `side: buy`
    on a routine director grant manufactured bullish signal out of scheduled
    compensation.

    Every refusal names what it is: data that never arrived (`SKIP_NO_CHANGE`,
    `SKIP_NO_VOLUME`) is never reported as a move or a volume that failed.
    """
    if not triggered(symbol, ctx):
        return None

    quote = (ctx.get("quotes") or {}).get(symbol) or {}
    change = _number(quote.get("change_pct"))
    reading = relative_volume(symbol, ctx)
    raw, tnorm, minutes = reading["rvol_raw"], reading["rvol_tnorm"], reading["minutes_elapsed"]
    normalised = time_normalised(ctx)
    floor = _min_minutes(ctx)

    # What the *other* volume rule would say, recorded on every verdict.
    factors = {
        "change_pct": change,
        **reading,
        "rule": "tnorm" if normalised else "raw",
        "raw_would_pass": raw is not None and raw >= CATALYST_MIN_VOLUME_MULTIPLE,
        "tnorm_would_pass": (
            tnorm is not None and minutes is not None and minutes >= floor
            and tnorm >= CATALYST_MIN_VOLUME_MULTIPLE
        ),
    }

    def refuse(code: str, message: str) -> Skipped:
        return Skipped(symbol, CATALYST, code, message, factors)

    if change is None:
        return refuse(
            SKIP_NO_CHANGE,
            f"{symbol} has an event but no price change to read it against: "
            "the quote carries no last trade or no previous close",
        )
    if raw is None:
        return refuse(
            SKIP_NO_VOLUME,
            f"{symbol} has an event and moved {change:+.1%}, but today's volume "
            "or its average is missing or zero, so the move cannot be confirmed",
        )
    if change < CATALYST_MIN_REACTION_PCT:
        return refuse(
            SKIP_BELOW_MIN_MOVE,
            f"{symbol} has an event but the tape has not confirmed it: "
            f"{change:+.1%} against a {CATALYST_MIN_REACTION_PCT:+.0%} minimum move",
        )

    # No clock (only `would_open` / `mc_prefilter`) means the multiple cannot
    # be normalised; the raw rule decides rather than a guess or a block.
    if normalised and minutes is not None:
        if minutes < floor:
            return refuse(
                SKIP_TOO_EARLY_FOR_RVOL,
                f"{symbol} has an event and is {change:+.1%}, but it is minute "
                f"{max(minutes, 0.0):.0f} of the session; relative volume is not "
                f"extrapolated before minute {floor:.0f} ({raw:.2f}x of a full "
                "day so far)",
            )
        if tnorm < CATALYST_MIN_VOLUME_MULTIPLE:
            return refuse(
                SKIP_BELOW_RVOL_TNORM,
                f"{symbol} has an event and is {change:+.1%}, on "
                f"{tnorm:.2f}x time-normalised volume at minute {minutes:.0f} "
                f"({raw:.2f}x of a full day), under the "
                f"{CATALYST_MIN_VOLUME_MULTIPLE}x minimum",
            )
        volume_text = (
            f"{tnorm:.1f}x time-normalised volume at minute {minutes:.0f} "
            f"({raw:.2f}x of a full day)"
        )
    else:
        if raw < CATALYST_MIN_VOLUME_MULTIPLE:
            return refuse(
                SKIP_BELOW_RVOL,
                f"{symbol} has an event but the tape has not confirmed it: "
                f"{change:+.1%} on {raw:.1f}x average volume"
                + (
                    "" if tnorm is None
                    else f" ({tnorm:.1f}x time-normalised at minute {minutes:.0f})"
                ),
            )
        volume_text = f"{raw:.1f}x average volume"

    conviction = _scaled(change, CATALYST_REACTION_FULL_SCALE_PCT)

    return _Proposal(
        symbol,
        CATALYST,
        conviction,
        f"event confirmed by a {change:+.1%} move on {volume_text}",
    )
