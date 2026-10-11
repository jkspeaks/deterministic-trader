"""Rules about the order itself, which review never validates, plus the
overnight-spread gate (§6.6)."""

from __future__ import annotations

from typing import Any, Mapping

from .core import Denial

NOT_TRADABLE = "NOT_TRADABLE"
FRACTIONAL_OUTSIDE_REGULAR_HOURS = "FRACTIONAL_OUTSIDE_REGULAR_HOURS"
ORDER_TYPE_OUTSIDE_REGULAR_HOURS = "ORDER_TYPE_OUTSIDE_REGULAR_HOURS"
FRACTIONAL_STOP_UNVERIFIED = "FRACTIONAL_STOP_UNVERIFIED"
EXTENDED_SESSION_NOT_TRADABLE = "EXTENDED_SESSION_NOT_TRADABLE"
FRACTIONAL_NOT_TRADABLE = "FRACTIONAL_NOT_TRADABLE"
OVERNIGHT_SPREAD_GATE = "OVERNIGHT_SPREAD_GATE"
WHOLE_SHARE_ONLY = "WHOLE_SHARE_ONLY"
PROTECTION_CONTENDS = "PROTECTION_CONTENDS"

REGULAR_HOURS = "regular_hours"
OVERNIGHT_CYCLE = "overnight"

# Only limit orders reach a non-regular session. Market and stop orders are
# documented regular-hours-only and are *not* enforced at review, so this set
# is the only thing standing between the plan and a rejection.
_REGULAR_HOURS_ONLY_TYPES = frozenset({"market", "stop_market", "stop_limit"})
_STOP_TYPES = frozenset({"stop_market", "stop_limit"})

# The API returns the bare string. The guide documents
# "all_day_tradability_tradable", which matches nothing and would deny every
# symbol for every extended session.
TRADABLE = "tradable"

# Get_equity_tradability's headline field was the string "tradability" until
# a live schema change replaced it with a boolean "tradeable"
# -- every held symbol's response lost the "tradability" key entirely that
# day, blocking every order (entry and exit) until caught.
# "fractional_tradability"/"all_day_tradability" are unaffected: still the
# bare TRADABLE string, confirmed live against the broker the same day.


def is_fractional(order: Mapping[str, Any]) -> bool:
    """Whether this order's quantity is a fraction of a share.

    Public because `cli.py` imports it. §14's repeated-rejection key turns on
    whole-versus-fractional — a fractional market buy the broker refused says
    nothing about a whole-share limit buy — and two answers to this question
    would eventually disagree, leaving a block key treating as whole an order
    guard was refusing as fractional. Same rule as §4.4's cap fractions and
    `TERMINAL_ORDER_STATES`: defined once, imported.

    Only reached from `_legality_denials` after the order has been priced, so
    the quantity is known to be present there: an order without one is denied
    ORDER_NOT_PRICEABLE first.
    """
    value = float(order["quantity"])
    return abs(value - round(value)) > 1e-9


def _legality_denials(order: Mapping[str, Any], ctx: Mapping[str, Any]) -> list[Denial]:
    """Rules about the order itself, which review never validates.

    Runbook §7: the broker checks facts it can look up about this account and
    does not check whether the order is legal. Everything in this function
    lives in that gap.
    """
    symbol = order.get("symbol")
    session = order.get("session")
    order_type = order.get("type")
    fractional = is_fractional(order)
    denials: list[Denial] = []

    tradability = (ctx.get("tradability") or {}).get(symbol)
    if not tradability or tradability.get("tradeable") is not True:
        denials.append(
            Denial(NOT_TRADABLE, f"{symbol} is not tradable on this account")
        )

    if session != REGULAR_HOURS:
        if fractional:
            # P1: rejected client-side with "fractional and dollar-based
            # orders are only allowed in regular_hours". The per-symbol
            # extended_hours_fractional_tradability flag is not honoured and
            # is deliberately never read.
            denials.append(
                Denial(
                    FRACTIONAL_OUTSIDE_REGULAR_HOURS,
                    f"fractional quantity {order.get('quantity')} cannot be "
                    f"placed in {session}; P1 closed this at the tool surface",
                )
            )
        if order_type in _REGULAR_HOURS_ONLY_TYPES:
            denials.append(
                Denial(
                    ORDER_TYPE_OUTSIDE_REGULAR_HOURS,
                    f"{order_type} orders are regular-hours-only and review "
                    f"does not enforce it; {session} was requested",
                )
            )
        if (tradability or {}).get("all_day_tradability") != TRADABLE:
            denials.append(
                Denial(
                    EXTENDED_SESSION_NOT_TRADABLE,
                    f"{symbol} is not flagged tradable for {session}",
                )
            )

    if fractional and order_type in _STOP_TYPES:
        # §10.1 asserts the broker refuses this; an early probe downgrades
        # that to unverified in both directions. Conservative behaviour holds
        # until a live placement settles it.
        denials.append(
            Denial(
                FRACTIONAL_STOP_UNVERIFIED,
                f"a {order_type} on a fractional quantity is unverified at "
                "placement; §10.1 gives fractional positions no resting stop",
            )
        )

    if fractional and (tradability or {}).get("fractional_tradability") != TRADABLE:
        denials.append(
            Denial(
                FRACTIONAL_NOT_TRADABLE,
                f"{symbol} is not flagged tradable in fractional quantities",
            )
        )

    # Sizing already refuses these (WHOLE_SHARE_ONLY in sizing.py and
    # dip_recovery/engine.py), so this is defense-in-depth against a
    # hand-placed or replayed order that bypassed sizing. Scoped to side
    # == "buy" so fractional sells -- exits.py's own path, untouched here
    # -- stay legal.
    if (
        fractional
        and order.get("side") == "buy"
        and bool(ctx["config"].get("whole_shares_only", True))
    ):
        denials.append(
            Denial(
                WHOLE_SHARE_ONLY,
                f"{symbol} buy quantity {order.get('quantity')} is fractional; "
                "this account buys whole shares only",
            )
        )

    return denials


def _position_for(ctx: Mapping[str, Any], symbol: str) -> Mapping[str, Any]:
    for position in ctx.get("positions") or []:
        if position.get("symbol") == symbol:
            return position
    return {}


def _protection_contends_denials(
    order: Mapping[str, Any], ctx: Mapping[str, Any]
) -> list[Denial]:
    """The shares this sell needs are already reserved by a resting one.

    **The broker fact.** Robinhood reserves 100% of a position's shares against
    the first resting sell it accepts — `get_equity_positions` then reports
    `shares_available_for_sells: "0.00000000"` and `shares_held_for_sells` at
    the full quantity — and refuses any further sell on the name with *"Not
    enough shares to sell."* There is no partial reservation and no override.

    Read rather than derived, which is the point: this is the broker's own
    number, carried onto `reconcile.Position` by `src/broker_shapes.py`, and
    §14's "a field that cannot be read is not a field that may be guessed"
    applies in the permissive direction — an absent `shares_available_for_sells`
    denies nothing, because a book row written before that field existed is not
    evidence of a reservation.

    **A cancel in the same plan clears it.** `cli._protection_orders` emits a
    cancel for every resting agentic sell on a symbol this cycle is selling,
    and Step 8a performs cancels before orders, so the shares are back before
    the sell is sent. Without this clause the exit path would deny itself
    against the very reservation it is about to release.

    What this catches is the case with no cancel: a reservation held by an order
    that is not ours to cancel (§13 — it may be the operator's), or a second
    protective leg proposed over a position that already has one.
    """
    if order.get("side") != "sell":
        return []

    symbol = order.get("symbol")
    if symbol in set(ctx.get("cancelling_symbols") or []):
        return []

    available = _position_for(ctx, symbol).get("shares_available_for_sells")
    if available is None:
        return []

    quantity = float(order.get("quantity") or 0.0)
    epsilon = float((ctx.get("config") or {}).get("reconcile_epsilon_quantity") or 0.0)
    if float(available) + epsilon >= quantity:
        return []

    return [
        Denial(
            PROTECTION_CONTENDS,
            f"{symbol} has {float(available):g} of {quantity:g} shares available to "
            "sell — the rest are reserved against a resting sell this plan does not "
            "cancel; the broker would refuse this with 'Not enough shares to sell.'",
        )
    ]


def _overnight_spread_denials(
    order: Mapping[str, Any], ctx: Mapping[str, Any]
) -> list[Denial]:
    """§6.6. Overnight cycles only sell, so the cost is one-way: exiting at the
    bid gives up roughly half the spread against mid.

    The catastrophe override matters — paying a 2% spread to escape a position
    15% past its stop is correct, and a flat gate would forbid it.
    """
    if (ctx.get("cycle") or {}).get("cycle_type") != OVERNIGHT_CYCLE:
        return []
    if order.get("side") != "sell":
        return []

    symbol = order.get("symbol")
    quote = (ctx.get("quotes") or {}).get(symbol) or {}
    bid, ask = quote.get("bid"), quote.get("ask")

    if bid is None or ask is None or (float(bid) + float(ask)) <= 0:
        # The spread is measured live at the moment of the decision and never
        # cached, so an absent quote is not a reason to assume a tight one.
        return [
            Denial(
                OVERNIGHT_SPREAD_GATE,
                f"no live overnight quote for {symbol}; the spread cannot be "
                "measured, so the exit defers to the open",
            )
        ]

    mid = (float(bid) + float(ask)) / 2.0
    spread_pct = (float(ask) - float(bid)) / mid
    max_spread = float(ctx["config"]["overnight_max_spread_pct"])
    if spread_pct <= max_spread:
        return []

    loss = float(_position_for(ctx, symbol).get("loss_pct_below_stop") or 0.0)
    multiple = float(ctx["config"]["catastrophe_multiple"])
    if loss >= multiple * spread_pct:
        return []

    return [
        Denial(
            OVERNIGHT_SPREAD_GATE,
            f"{symbol} overnight spread is {spread_pct:.2%} against a "
            f"{max_spread:.2%} gate, and the {loss:.2%} loss below stop does "
            f"not reach the {multiple:g}x catastrophe override; defer to the open",
        )
    ]
