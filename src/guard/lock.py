"""guard.lock — Dip Recovery plan, phase 1: symbol-level ownership between
strategy sets.

Robinhood sells draw from a symbol's aggregate share count, not from
strategy-tagged sub-lots, so there is no broker-level way to mark "these
shares are Dip Recovery's" or "these are core's". `strategy_symbol_lock` is
the substitute: one row per held symbol, naming which side owns it. This
module is the only place that lock is consulted before an order is placed —
on both sides of the book, and on **both buys and sells**, because an exit is
exactly as capable of crossing the ownership boundary as an entry is. Core's
own generic exits (stop-loss, time-stop, target) evaluate every held
position by symbol with no notion of who opened it, so without this check
core would sweep a Dip Recovery position into its own exit logic the moment
reconcile saw it on the book.

Read-only here. The lock's writes belong to `reconcile.py`, at the one point
a fill is confirmed — never to guard, and never to the order that merely
proposes a trade the broker may still reject.
"""

from __future__ import annotations

from typing import Any, Mapping

from .core import Denial

SYMBOL_LOCKED_OTHER_SIDE = "SYMBOL_LOCKED_OTHER_SIDE"


def _symbol_lock_denials(order: Mapping[str, Any], ctx: Mapping[str, Any]) -> list[Denial]:
    """Deny any order on a symbol the other strategy owns.

    `ctx["strategy"]` names which side is asking and defaults to `"core"`, so
    every call site that predates Dip Recovery keeps behaving exactly as it
    always has with no ctx change required — an absent key reads as core
    asking about its own book, exactly as it always has.

    A missing lock row means nobody has claimed the symbol yet, which is not
    a denial for either side: the first fill is what claims it, not the order
    that precedes it. Two sides racing to open the same unclaimed symbol on
    the same cycle cannot happen — §13.1's cycle lock serializes every
    routine cycle, core's and Dip Recovery's alike, so at most one side is
    ever placing orders at a time.
    """
    owner = (ctx.get("symbol_lock") or {}).get(order.get("symbol"))
    asking = ctx.get("strategy") or "core"

    if owner is not None and owner != asking:
        return [
            Denial(
                SYMBOL_LOCKED_OTHER_SIDE,
                f"{order.get('symbol')} is locked to '{owner}'; '{asking}' may "
                "not buy or sell it while that lock stands",
            )
        ]
    return []
