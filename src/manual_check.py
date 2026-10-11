"""manual_check — the on-demand Manual Check routine's decision.

The operator runs this by hand, any hour an equity session is open (Sunday
20:00 ET through Friday 20:00 ET), to look at **every share the account holds**
and ask two questions of each position:

* is it at its profit target with T+1 clear, so the whole position can be sold;
* has it fallen far enough below cost, far enough below its latest buy, and
  long enough since that buy to average down once more.

It is not a new strategy. Every rule here is the Dip Recovery engine's own
(`src/dip_recovery/engine.py`), applied to the real book instead of to the
strategy's private ledger:

* the exit is `engine._manage`'s: price at or above weighted average cost times
  one plus the profit target and the cost buffer (`exit_thresholds`), and
  `_t1_clear`, at least one trading session since the newest buy;
* the average-down is `engine._average_down_skip`'s: the trigger, the minimum
  drop from the latest fill, the minutes since it and the fill cap
  (`sizing_thresholds`);
* sector rotation and the regime's hold-cash verdict gate the average-down and
  never the exit, exactly as `engine.evaluate` does;
* every order still goes through `guard.evaluate`.

What differs, and why each difference is deliberate:

* **Nothing is persisted.** `engine.evaluate` returns position updates, lock
  claims and a daily-entries counter for the Dip routine to write. This module
  returns none of them, so a manual run cannot move a Dip row, a symbol lock or
  a counter that the scheduled routines read. Reconcile adopts whatever the
  broker shows on core's next cycle, as it does for any fill.
* **Symbol locks are not consulted.** The operator chose to include every
  holding. The run holds the shared §13.1 lock (the prompt's Step 1), so no
  scheduled cycle can place against the same shares while this runs.
* **A weekend is not closed.** A market holiday stays closed.
* **No loss is ever realised.** `_manage` only sells at or above the target,
  and `_never_below_cost` re-checks every sell against the basis before it
  leaves this module. A position through its ATR stop is not this routine's
  business (profit first: README.md).
* **An average-down is sized as if the book were empty.** `engine._size_buy`
  divides the deployable cash by the slots still open; with four real
  positions that divisor would be 1 and a single add-on would spend all of it.
  Here the divisor is the strategy's whole slot count, so one average-down is
  at most one fifth of the deployable cash, and guard's position and sector
  caps still apply on top.
* **The take-profit is handled, not forgotten.** The broker reserves every
  share against the first resting sell, so selling needs that resting target
  cancelled first. A resting sell is cancelled only if it is the system's own
  (`placed_agent == "agentic"`) and a take-profit limit; if any resting sell on
  the symbol is anything else, the symbol is left alone and guard's
  `PROTECTION_CONTENDS` reports it.
"""

from __future__ import annotations

from typing import Any, Mapping

from src import regime as regimes
from src import universe as universes
from src.dip_recovery import engine
from src.dip_recovery.core import (
    EXIT_LOCKED,
    SECTOR_INACTIVE,
    SKIP_REGIME,
    Order,
    Skipped,
    _price,
    regime_v2_enabled,
)
from src.plays import protection
from src.reconcile import TERMINAL_ORDER_STATES

COMMAND = "manual-check-decide"

# Decisions a holding can end the run with, for the operator's table.
SELL = "SELL"
AVERAGE_DOWN = "AVERAGE_DOWN"
HOLD = "HOLD"
TARGET_T1_LOCKED = "TARGET_REACHED_T1_LOCKED"
BLOCKED = "BLOCKED_BY_GUARD"
NO_PRICE = "NO_PRICE"

# Reasons this module (not the engine, not guard) drops an order.
BELOW_COST = "BELOW_COST"

# TIF: a sell is a take-profit, so it rests until it fills (never a day order
# that expires and leaves the position with no target once the old one is
# cancelled); a buy is a day order so an unfilled add-on cannot linger over a
# weekend as an order nobody is watching.
_SELL_TIF = "gtc"
_BUY_TIF = "gfd"

_OVERNIGHT_SESSION = "all_day_hours"


def _resting_sells(ctx: Mapping[str, Any]) -> dict[str, list[Mapping[str, Any]]]:
    out: dict[str, list[Mapping[str, Any]]] = {}
    for order in ctx.get("open_orders") or []:
        if order.get("side") != "sell":
            continue
        if str(order.get("state", "")).lower() in TERMINAL_ORDER_STATES:
            continue
        out.setdefault(str(order.get("symbol")), []).append(order)
    return out


def _cancellable(resting: list[Mapping[str, Any]]) -> bool:
    """Every resting sell on the symbol is the system's own take-profit.

    All, not any: cancelling one of two resting sells frees nothing if the
    other still holds the shares, and would leave the position without its
    target for no gain.
    """
    return bool(resting) and all(
        protection.resting_kind(o) == protection.TARGET
        and protection.is_ours_to_replace(o, {})
        for o in resting
    )


def _never_below_cost(order: Order, row: Mapping[str, Any]) -> bool:
    """A sell leaves only at a price above what the position cost."""
    cost = row.get("weighted_avg_cost")
    return cost is not None and order.limit_price > float(cost)


def _unrealized_pct(price: float | None, cost: Any) -> float | None:
    if price is None or not cost or float(cost) <= 0:
        return None
    return round((price - float(cost)) / float(cost) * 100.0, 4)


def evaluate(ctx: Mapping[str, Any]) -> dict:
    """One manual run: every held symbol, then guard over the whole plan.

    ``ctx`` is `dip_recovery.evaluate`'s, with ``dip_recovery_positions`` being
    one synthesised row per real broker position (see `manual_assemble`).
    """
    rows = {
        symbol: {**row, "state": row.get("state") or "HOLDING"}
        for symbol, row in (ctx.get("dip_recovery_positions") or {}).items()
    }
    resting = _resting_sells(ctx)
    cancel_ids: dict[str, list[str]] = {
        symbol: [str(o.get("order_id")) for o in orders]
        for symbol, orders in resting.items()
        if _cancellable(orders)
    }
    # Guard is told which resting orders this plan is about to cancel, so a
    # sell does not deny itself against the very target it will release.
    guard_ctx = {
        **ctx,
        "cancelling_symbols": sorted(cancel_ids),
        "cancelling_order_ids": sorted(i for ids in cancel_ids.values() for i in ids),
    }

    entries_enabled = engine._entries_enabled(ctx)
    ledger = engine._Ledger(ctx, active_count=len(rows))
    rotation = universes.sector_rotation(
        (ctx.get("control") or {}).get("active_sectors"), ctx.get("etfs") or {},
        ctx.get("rotation_codes"), ctx.get("sub_industries"),
    )
    sectors = ctx.get("sectors") or {}
    regime = ctx.get("regime") or None
    enforce = regime_v2_enabled(ctx)
    hold_cash = regimes.no_entry_reason(regime)

    holdings: list[dict] = []
    skipped: list[dict] = []
    manual_review: list[dict] = []
    pending: list[engine._Pending] = []
    by_symbol: dict[str, dict] = {}

    for symbol in sorted(rows):
        row = rows[symbol]
        price = _price(ctx, symbol)
        target = engine._target_price(row, ctx)
        entry: dict[str, Any] = {
            "symbol": symbol,
            "quantity": row.get("total_quantity"),
            "weighted_avg_cost": row.get("weighted_avg_cost"),
            "price": price,
            "unrealized_pct": _unrealized_pct(price, row.get("weighted_avg_cost")),
            "target_price": round(target, 4),
            "newest_buy_day": row.get("clock_trading_day"),
            "t1_clear": engine._t1_clear(row, ctx),
            "decision": HOLD,
            "detail": "",
        }
        by_symbol[symbol] = entry
        holdings.append(entry)

        if price is None:
            entry.update(decision=NO_PRICE, detail=f"no live price for {symbol}")
            continue

        new_row, order, wants_average, review, skip = engine._manage(row, ctx, entries_enabled)

        if review is not None:
            manual_review.append(review.as_dict())
            entry["detail"] = review.message

        if order is not None:
            if not _never_below_cost(order, row):
                skipped.append(Skipped(symbol, BELOW_COST,
                    f"{symbol} sell at {order.limit_price:g} is not above its "
                    f"{row.get('weighted_avg_cost')} cost; never sold at a loss").as_dict())
                entry.update(decision=HOLD, detail="sell would not clear cost; held")
                continue
            pending.append(engine._Pending(order, symbol, order.quantity * order.limit_price, new_entry=False))
            continue

        if new_row.get("state") == EXIT_LOCKED:
            entry.update(
                decision=TARGET_T1_LOCKED,
                detail=f"{symbol} is at or above its {target:.2f} target but bought "
                       f"{row.get('clock_trading_day')}; T+1 has not cleared, so no sale",
            )
            continue

        if wants_average:
            inactive = rotation.inactive(symbol, sectors)
            if inactive is not None:
                skipped.append(Skipped(symbol, SECTOR_INACTIVE,
                    f"{inactive} is outside the active sector rotation; {symbol} "
                    "is not averaged down").as_dict())
                entry["detail"] = f"{inactive} is outside the active sector rotation"
                continue
            if enforce and hold_cash is not None:
                skipped.append(Skipped(symbol, SKIP_REGIME,
                    f"{hold_cash}: {symbol} is not averaged down").as_dict())
                entry["detail"] = hold_cash
                continue

            sized = engine._size_buy(symbol, ctx, ledger, ledger.max_active)
            if isinstance(sized, Order):
                notional = sized.quantity * sized.limit_price
                pending.append(engine._Pending(sized, symbol, notional, new_entry=False))
                ledger.spend(notional, new_entry=False)
            else:
                skipped.append(sized.as_dict())
                entry["detail"] = sized.message
            continue

        if skip is not None:
            skipped.append(skip.as_dict())
            entry["detail"] = skip.message
        elif not entry["detail"]:
            entry["detail"] = "between its target and the average-down trigger; nothing to do"

    approved, blocked = engine._guard_verdict(pending, guard_ctx)
    session = (ctx.get("cycle") or {}).get("session") or "regular_hours"

    orders: list[dict] = []
    cancels: list[dict] = []
    # Sells first, as every routine here argues: the cancel that frees a
    # position's shares comes before the sell that needs them.
    for proposal in sorted(pending, key=lambda p: 0 if p.order.side == "sell" else 1):
        symbol = proposal.symbol
        if proposal.key not in approved:
            ledger.refund(proposal.notional, new_entry=False)
            by_symbol[symbol].update(
                decision=BLOCKED,
                detail="; ".join(
                    f"{d['code']}: {d['message']}"
                    for b in blocked if b["order"].get("symbol") == symbol
                    for d in b["denials"]
                ) or "guard refused the order",
            )
            continue

        order = proposal.order
        entry = by_symbol[symbol]
        placed = {
            "symbol": symbol,
            "side": order.side,
            "type": "limit",
            "quantity": order.quantity,
            "limit_price": order.limit_price,
            "market_hours": session,
            "time_in_force": _SELL_TIF if order.side == "sell" else _BUY_TIF,
            "reason": order.reason,
            "cancel_order_ids": list(cancel_ids.get(symbol, [])) if order.side == "sell" else [],
        }
        orders.append(placed)
        if order.side == "sell":
            for order_id in placed["cancel_order_ids"]:
                cancels.append({"symbol": symbol, "order_id": order_id,
                                "reason": f"frees {symbol}'s shares for the take-profit sale"})
            entry.update(decision=SELL, detail=order.reason)
        else:
            entry.update(decision=AVERAGE_DOWN, detail=order.reason)

    return {
        "session": session,
        "orders": orders,
        "cancels": cancels,
        "blocked": blocked,
        "holdings": holdings,
        "skipped": skipped,
        "manual_review": manual_review,
    }
