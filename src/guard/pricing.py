"""What an order costs, priced the way the broker prices it."""

from __future__ import annotations

from typing import Any, Mapping

_LIMIT_PRICED = frozenset({"limit", "stop_limit"})


def _notional(order: Mapping[str, Any], ctx: Mapping[str, Any]) -> float | None:
    """What this order costs, priced the way the broker prices it.

    Limit orders are costed at the limit, not at the quote: runbook §6 row 2
    records the broker computing an F shortfall against a $15.00 limit rather
    than the $14.68 ask. Market orders are costed at the far side of the
    spread — costing a buy at the bid understates it and leaves the order
    short at placement.

    Returns ``None`` when the order cannot be priced at all.
    """
    quantity = order.get("quantity")
    if quantity is None:
        return None

    if order.get("type") in _LIMIT_PRICED:
        price = order.get("limit_price")
    else:
        quote = (ctx.get("quotes") or {}).get(order.get("symbol")) or {}
        price = quote.get("ask") if order.get("side") == "buy" else quote.get("bid")

    if price is None:
        return None
    return float(quantity) * float(price)
