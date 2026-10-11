"""Account-wide halts. Nothing is placed, including exits."""

from __future__ import annotations

from typing import Any, Mapping

from src.sizing import (
    drawdown_breached,
    drawdown_floor,
    flow_adjusted_equity,
    raw_equity,
)

from .core import Denial

CONTROL_UNREADABLE = "CONTROL_UNREADABLE"
ACCOUNT_UNREADABLE = "ACCOUNT_UNREADABLE"
TRADING_DISABLED = "TRADING_DISABLED"
SESSION_CLOSED = "SESSION_CLOSED"

CLOSED = "closed"


def _account_halts(ctx: Mapping[str, Any]) -> list[Denial]:
    control = ctx.get("control") or {}

    if not control.get("readable", False):
        return [
            Denial(
                CONTROL_UNREADABLE,
                "edge_control could not be read; something that cannot hear "
                "'stop' must not act",
            )
        ]

    # Same fail-closed posture as CONTROL_UNREADABLE. A missing, null, zero or non-numeric
    # `equity_total` is a broken account read, not a $0 balance — an absent figure became a
    # large negative flow-adjusted number and §10 flattened the whole book. A system that
    # cannot read its own equity halts; it does not liquidate.
    if raw_equity(ctx.get("account") or {}) is None:
        return [
            Denial(
                ACCOUNT_UNREADABLE,
                "account equity_total is missing, zero or non-numeric; a system "
                "that cannot read its own balance must not act",
            )
        ]

    if not control.get("trading_enabled", False):
        return [Denial(TRADING_DISABLED, "TRADING_ENABLED is not TRUE")]

    # P5: equities are 24/5, Sunday 20:00 ET through Friday 20:00 ET. Review
    # succeeded at 02:30 ET on a Saturday, so the clock is guard's to keep.
    if (ctx.get("cycle") or {}).get("session") == CLOSED:
        return [
            Denial(
                SESSION_CLOSED,
                "no equity session is open; orders must not be queued into a "
                "closed market",
            )
        ]

    return []


def _drawdown_breached(ctx: Mapping[str, Any]) -> tuple[bool, float, float]:
    account = ctx["account"]
    high_water = float(ctx["control"]["high_water_mark"])
    config = ctx["config"]
    floor = drawdown_floor(high_water, config, account)
    adjusted = flow_adjusted_equity(account)
    if adjusted is None:
        # Unreadable equity is ACCOUNT_UNREADABLE, evaluated ahead of every
        # order in `_account_halts`; it never reaches here in the normal flow
        # and is never a breach. 0.0 is a placeholder the caller does not
        # format, since `breached` is False.
        return (False, 0.0, floor)
    return (
        drawdown_breached(account, high_water, config),
        # The denial quotes the figure it judged, not the raw balance. An
        # operator reading "equity 336.42 is below the 269.14 floor" would
        # reasonably conclude the system was broken.
        adjusted,
        floor,
    )
