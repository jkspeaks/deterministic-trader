"""The core sleeve: idle cash held in one broad-market ETF.

Off unless `core_sleeve_enabled`.

What it does, each entry-capable cycle, after the plays have been sized:

* the plays take what they were sized for, exactly as before;
* from what is left of settled cash, the reserve and a buffer are kept back;
* the rest buys whole shares of `core_symbol`, up to `core_cap_pct` of equity.

What it never does:

* **It never rests, proposes or places a stop of any kind.** The sleeve's one
  resting order is `plays.protection`'s limit SELL above its buy price
  (`core_target_pct`), like every other position's. It has no per-cycle ATR
  check and no time stop either (`plays.core.PLAYS[CORE]`): S4 measured the
  sleeve without them, and a broad ETF sold on a 3-ATR dip is the trade the
  standing rule exists to prevent.
* **It never sells to fund a play.** The funding sale is not built.
  The cap is what leaves cash for the plays: at 50%, half the account is never
  the sleeve's. With the sleeve on, the plays are sized from the cash it has
  not taken, which after the first few cycles is the buffer and whatever a
  sale has freed.
* **It takes no play slot** and is outside the position and sector caps, which
  were written for single names. Its own cap replaces them, and `guard`
  enforces that cap against the same number this module sizes to.

The drawdown halt still applies to it, as to the whole book: that is an
account rule, not a play's.

This module has no opinion about whether the sleeve should be on. It answers
"how many shares, this cycle" and "is this order the sleeve's".
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from src import sizing

CORE = "CORE"

ENABLED = "core_sleeve_enabled"
SYMBOL = "core_symbol"
CAP_PCT = "core_cap_pct"
BUFFER_SLOTS = "core_buffer_slots"
TARGET_PCT = "core_target_pct"

CAP_PCT_DEFAULT = 0.50
BUFFER_SLOTS_DEFAULT = 1.0
TARGET_PCT_DEFAULT = 0.20

# The sleeve has no time stop (`plays.core.Play.time_stop` is False for it),
# so this number is never a deadline. It is the longest play's, for the one
# place a holding period is read without being acted on.
HOLDING_DAYS = 15

# The key on a buy order that says "this is the sleeve's". Guard honours it
# only for `core_symbol` with the switch on (`is_sleeve_order`), so a flag on
# any other order buys it nothing.
ORDER_FLAG = "core_sleeve"

CORE_CAP_EXCEEDED = "CORE_CAP_EXCEEDED"

BUY = "buy"
NONE = "none"

_TERMINAL = frozenset({"filled", "cancelled", "canceled", "rejected", "failed", "expired"})


def _config(source: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Accepts a ctx or a bare config: every caller has one or the other."""
    source = source or {}
    inner = source.get("config")
    return inner if isinstance(inner, Mapping) else source


def enabled(source: Mapping[str, Any] | None) -> bool:
    return bool(_config(source).get(ENABLED))


def symbol(source: Mapping[str, Any] | None) -> str | None:
    value = _config(source).get(SYMBOL)
    return str(value) if value else None


def cap_pct(source: Mapping[str, Any] | None) -> float:
    return float(_config(source).get(CAP_PCT, CAP_PCT_DEFAULT))


def buffer_slots(source: Mapping[str, Any] | None) -> float:
    return float(_config(source).get(BUFFER_SLOTS, BUFFER_SLOTS_DEFAULT))


def target_pct(source: Mapping[str, Any] | None) -> float:
    return float(_config(source).get(TARGET_PCT, TARGET_PCT_DEFAULT))


def is_sleeve_symbol(name: Any, source: Mapping[str, Any] | None) -> bool:
    """Whether ``name`` is the sleeve's ETF *and the sleeve is on*. Off, the
    ETF is an ordinary symbol and every rule treats it as one."""
    return enabled(source) and name is not None and name == symbol(source)


def is_sleeve_order(order: Mapping[str, Any], source: Mapping[str, Any] | None) -> bool:
    return (
        order.get(ORDER_FLAG) is True
        and order.get("side") == "buy"
        and is_sleeve_symbol(order.get("symbol"), source)
    )


def held_value(ctx: Mapping[str, Any]) -> float:
    name = symbol(ctx)
    return sum(
        float(p.get("market_value") or 0.0)
        for p in ctx.get("positions") or []
        if p.get("symbol") == name
    )


def cap_value(equity: float, source: Mapping[str, Any] | None) -> float:
    return max(0.0, float(equity)) * cap_pct(source)


BROAD_MARKET = "Broad Market"


def loaded_with_sleeve(loaded: Mapping[str, Any]) -> Mapping[str, Any]:
    """``loaded`` with the sleeve's ETF listed among the broad ETFs, while the
    sleeve is on. Unchanged, and the same object, when it is off or the ETF is
    already listed.

    `config/etfs.json` is the system's member list and sector map. Listing the
    ETF there by hand would add a member on every cycle whether or not the
    sleeve is on, and move numbers in records that have nothing to do with it.
    Deriving the listing from the switch keeps "off" byte-identical to before
    the sleeve existed.
    """
    config = loaded.get("config") or {}
    name = symbol(config)
    if not enabled(config) or name is None:
        return loaded
    etfs = loaded.get("etfs") or {}
    if name in (etfs.get("broad") or {}) or name in (etfs.get("sector") or {}):
        return loaded
    return {**loaded, "etfs": {**etfs, "broad": {**(etfs.get("broad") or {}), name: BROAD_MARKET}}}


def _resting_buy(ctx: Mapping[str, Any], name: str) -> bool:
    for order in ctx.get("open_orders") or []:
        if not isinstance(order, Mapping):
            continue
        if order.get("symbol") != name or order.get("side") != "buy":
            continue
        if str(order.get("state", "")).lower() not in _TERMINAL:
            return True
    return False


def plan(ctx: Mapping[str, Any], committed: float = 0.0) -> dict:
    """What the sleeve would buy this cycle, and why not when it would not.

    ``committed`` is the cash this cycle's sized play entries take; the sleeve
    comes after them. Pure: the same answer is the shadow record when the
    switch is off and the order when it is on.

    The dollar figures need no quote, so they are reported on every cycle. A
    share count needs the ETF's ask, which is fetched only while the sleeve is
    on (its trigger is what puts the symbol in the runner's fetch list).
    """
    config = _config(ctx)
    name = symbol(config)
    out: dict[str, Any] = {
        "symbol": name, "action": NONE, "reason": None, "quantity": 0, "limit_price": None,
        "notional": 0.0, "sleeve_value": None, "cap_value": None, "room": None,
        "cash_after_plays": None, "kept_back": None, "would_deploy": None,
    }

    def stop(reason: str) -> dict:
        out["reason"] = reason
        return out

    if name is None:
        return stop("no core_symbol is configured")

    account = ctx.get("account") or {}
    equity = float(account.get("equity_total") or 0.0)
    basis = sizing.settled_basis(account, config)
    if equity <= 0.0 or basis is None:
        return stop("equity or settled cash is unknown; the sleeve sizes nothing blind")

    held = held_value(ctx)
    cap = cap_value(equity, config)
    slot = equity / max(1, sizing.n_target(equity, config))
    kept = sizing.cash_reserve(equity, config) + buffer_slots(config) * slot
    after = basis - max(0.0, float(committed))
    room = cap - held
    amount = max(0.0, min(after - kept, room))
    out.update(
        sleeve_value=round(held, 2), cap_value=round(cap, 2), room=round(room, 2),
        cash_after_plays=round(after, 2), kept_back=round(kept, 2), would_deploy=round(amount, 2),
    )

    if not bool((ctx.get("cycle") or {}).get("entries_allowed")):
        return stop("this cycle cannot buy")
    if room <= 0.0:
        return stop(f"the sleeve holds {held:.2f} against a {cap:.2f} cap")
    if after - kept <= 0.0:
        return stop(
            f"{after:.2f} of settled cash after the plays does not exceed the "
            f"{kept:.2f} kept back (reserve plus buffer)"
        )
    if _resting_buy(ctx, name):
        return stop(f"a buy already rests on {name}")

    ask = sizing._ask(ctx, name)
    if ask is None:
        return stop(f"no ask for {name}; it is quoted only while the sleeve is on")
    limit = sizing._marketable_limit(ask, sizing._atr(ctx, name), config)
    quantity = math.floor(amount / limit + 1e-9)
    if quantity < 1:
        return stop(f"{amount:.2f} does not buy one share of {name} at {limit:.2f}")
    notional = quantity * limit
    if sizing.meaningfully_less(notional, float(config.get("min_order_notional", 0.0))):
        return stop(f"{notional:.2f} is under the broker's minimum order")

    out.update(action=BUY, quantity=int(quantity), limit_price=limit, notional=round(notional, 2),
               reason=f"{amount:.2f} of idle settled cash, {room:.2f} of room under the cap")
    return out


def order(planned: Mapping[str, Any], ctx: Mapping[str, Any]) -> dict:
    """The planned buy in `guard.evaluate`'s order shape."""
    return {
        "symbol": planned["symbol"],
        "side": "buy",
        "type": "limit",
        "quantity": planned["quantity"],
        "limit_price": planned["limit_price"],
        "stop_price": None,
        "session": (ctx.get("cycle") or {}).get("session"),
        "play_id": CORE,
        "full_deployment": False,
        ORDER_FLAG: True,
    }


def cap_denial(order_: Mapping[str, Any], held: float, notional: float, equity: float,
               source: Mapping[str, Any] | None) -> str | None:
    """Guard's side of the cap: the message when this buy would pass it."""
    cap = cap_value(equity, source)
    if sizing.meaningfully_less(cap, held + notional):
        return (f"{order_.get('symbol')} would reach {held + notional:.2f} against the "
                f"sleeve's {cap:.2f} cap ({cap_pct(source):.0%} of equity)")
    return None
