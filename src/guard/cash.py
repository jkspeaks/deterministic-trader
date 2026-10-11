"""Settled-cash denials. ``state: _PlanState`` below is a deferred annotation
(``from __future__ import annotations``) and is never evaluated at runtime, so
this module does not import `._PlanState` — importing it here would be
circular, since `core.py` imports this module before `_PlanState` is defined."""

from __future__ import annotations

from typing import Any, Mapping

from src.sizing import POLICY_CONSERVATIVE, meaningfully_less as _meaningfully_less

from .core import Denial

SAME_SESSION_SELL = "SAME_SESSION_SELL"
SETTLED_CASH_UNKNOWN = "SETTLED_CASH_UNKNOWN"
INSUFFICIENT_SETTLED_CASH = "INSUFFICIENT_SETTLED_CASH"


def _settled_cash_denials(
    order: Mapping[str, Any],
    ctx: Mapping[str, Any],
    state: _PlanState,
    notional: float | None,
) -> list[Denial]:
    if state.policy == POLICY_CONSERVATIVE and state.sold_this_session:
        return [
            Denial(
                SAME_SESSION_SELL,
                "a sale settled earlier in this session may have inflated "
                "buying_power with unsettled proceeds; §2.4 denies buys "
                "outright until probe P3 resolves",
            )
        ]

    if state.deployable is None:
        return [
            Denial(
                SETTLED_CASH_UNKNOWN,
                "settled cash could not be established; Invariant 1 fails closed",
            )
        ]

    assert notional is not None  # callers deny unpriceable orders first
    if _meaningfully_less(state.deployable, notional):
        return [
            Denial(
                INSUFFICIENT_SETTLED_CASH,
                f"buy needs {notional:.2f} of settled cash, "
                f"{state.deployable:.2f} deployable",
            )
        ]

    return []
