"""guard.core — the shared result types, the per-plan budget, and the
per-order dispatcher.

`Denial` is defined first, deliberately: every category module (`halts`,
`cash`, `concentration`, `legality`, `schedule`) constructs `Denial` objects
and is imported *by* this module, so `Denial` has to already exist in this
module's namespace before those imports run. That is the whole resolution to
what would otherwise be a two-way need — this module calls into the category
modules, and they build the type this module returns. Nothing below relies on
import order beyond that one constraint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from src import core_sleeve as _core_sleeve
from src.plays import protection as _protection
from src.reconcile import TERMINAL_ORDER_STATES as _TERMINAL_ORDER_STATES
from src.sizing import (
    DEFAULT_SETTLED_CASH_POLICY,
    account_implausible as _account_implausible,
    meaningfully_less as _meaningfully_less,
    settled_basis,
)


@dataclass(frozen=True)
class Denial:
    code: str
    message: str

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


from .halts import _account_halts, _drawdown_breached  # noqa: E402 — see module docstring
from .cash import _settled_cash_denials  # noqa: E402
from .concentration import _concentration_denials  # noqa: E402
from .legality import (  # noqa: E402
    _legality_denials,
    _overnight_spread_denials,
    _protection_contends_denials,
)
from .lock import _symbol_lock_denials  # noqa: E402
from .mc import _mc_denials  # noqa: E402
from .schedule import (  # noqa: E402
    _candidate_denials,
    _earnings_denials,
    _entry_permission_denials,
    _wash_sale_denials,
)
from .strategy import _strategy_enabled_denials  # noqa: E402
from .rotation import _sector_rotation_denials  # noqa: E402
from .pricing import _notional  # noqa: E402

# §14's "the routine does not retry a blind second order". A duplicate open
# order on the same symbol and side is refused here rather than left to the
# broker, because the broker's own open-order list cannot see an order this
# same plan is about to place.
DUPLICATE_OPEN_ORDER = "DUPLICATE_OPEN_ORDER"
# The same rule for §10.1's resting orders, which needed their own because the
# duplicate test is a different one. Two sells on a symbol are a doubled
# position; a stop and a target on the same symbol are the two ends of one
# trade and must both be allowed to rest. What may not be duplicated is *this*
# order — a second stop at the same price, which sells the position twice if
# both trigger and leaves an orphan working order if only one does.
DUPLICATE_PROTECTION = "DUPLICATE_PROTECTION"
ORDER_NOT_PRICEABLE = "ORDER_NOT_PRICEABLE"
BELOW_MIN_NOTIONAL = "BELOW_MIN_NOTIONAL"
DRAWDOWN_HALT = "DRAWDOWN_HALT"
# A readable but implausible
# `equity_total` (669.53 against an account holding more than that in open
# positions) raised a false urgent drawdown alert. This is a buy-only denial,
# unlike `guard.halts.ACCOUNT_UNREADABLE` which stops exits too — a figure
# that is merely *implausible* is not the same failure as one that is
# missing, and §6.5's flatten must still be free to run on a real breach.
ACCOUNT_IMPLAUSIBLE = "ACCOUNT_IMPLAUSIBLE"


@dataclass(frozen=True)
class DeniedOrder:
    order: dict
    denials: list[Denial]

    def as_dict(self) -> dict:
        return {
            "order": self.order,
            "denials": [d.as_dict() for d in self.denials],
        }


@dataclass
class GuardResult:
    approved: list[dict] = field(default_factory=list)
    denied: list[DeniedOrder] = field(default_factory=list)
    halts: list[Denial] = field(default_factory=list)

    @property
    def halted(self) -> bool:
        return bool(self.halts)

    def as_dict(self) -> dict:
        return {
            "halted": self.halted,
            "halts": [d.as_dict() for d in self.halts],
            "approved": list(self.approved),
            "denied": [d.as_dict() for d in self.denied],
        }


class _PlanState:
    """Budget consumed by orders already approved in this plan.

    Guard evaluates a plan, not an order. Two buys that each fit the settled
    balance alone can breach Invariant 1 together, so approvals draw the
    balance down as they are granted and denials leave it untouched.
    """

    def __init__(self, ctx: Mapping[str, Any]) -> None:
        account = ctx.get("account") or {}
        config = ctx.get("config") or {}
        self.policy = config.get("settled_cash_policy", DEFAULT_SETTLED_CASH_POLICY)
        self.sold_this_session = bool(account.get("sold_this_session", False))

        # Orders this plan is cancelling in the same breath. A resting order
        # about to be cancelled is not competition for the order replacing it,
        # and without this every `protection_plan.replace` would deny itself
        # against the very order it exists to supersede.
        cancelling = set(ctx.get("cancelling_order_ids") or [])

        open_orders = [
            o
            for o in (ctx.get("open_orders") or [])
            if str(o.get("state", "")).lower() not in _TERMINAL_ORDER_STATES
            and o.get("order_id") not in cancelling
        ]

        # The broker's open-order list cannot see an order this same plan is
        # about to place, so approvals are recorded here as well.
        #
        # **A resting protective order is deliberately not a claim.** The
        # exits that a resting order genuinely *does* cover never reach guard
        # at all; `plays.exits._covering_order` reports them as
        # `covered_by_resting` instead, and `cli.decide` cancels the resting
        # order for any symbol whose exit is placing this cycle.
        self.claimed: set[tuple[str, str]] = {
            (o.get("symbol"), o.get("side"))
            for o in open_orders
            if _protection.resting_kind(o) is None
        }

        # And the protective ones, keyed by what makes two of them the same
        # order: symbol, kind and the price it rests at.
        self.protection: dict[tuple[str, str], list[float]] = {}
        for order in open_orders:
            kind = _protection.resting_kind(order)
            price = _protection.resting_price(order)
            if kind is not None and price is not None:
                self.protection.setdefault((order.get("symbol"), kind), []).append(price)

        # Concentration is measured against the book as it *would* stand, so
        # approvals accumulate here too. Two buys sized independently against
        # the same cap would breach it together.
        sectors = ctx.get("sectors") or {}
        self.equity = float(account.get("equity_total") or 0.0)
        self.n_target = int((ctx.get("sizing") or {}).get("n_target") or 0)
        self.position_value: dict[str, float] = {}
        self.sector_value: dict[str, float] = {}
        self.open_symbols: set[str] = set()
        # The core sleeve takes no play slot. Its value is still counted, so
        # its own cap is measured against the book as it would stand.
        self.sleeve = _core_sleeve.symbol(config) if _core_sleeve.enabled(config) else None

        for position in ctx.get("positions") or []:
            symbol = position.get("symbol")
            value = float(position.get("market_value") or 0.0)
            if symbol != self.sleeve:
                self.open_symbols.add(symbol)
            self.position_value[symbol] = self.position_value.get(symbol, 0.0) + value
            sector = sectors.get(symbol)
            if sector is not None:
                self.sector_value[sector] = self.sector_value.get(sector, 0.0) + value

        # §14: settled cash unknown -> deny all buys, allow exits. Guard
        # deliberately does not subtract sizing's cash reserve: the reserve is
        # policy and this is legality, and denying a legal order over a policy
        # buffer would be guard repairing the plan rather than judging it.
        self.deployable = settled_basis(account, config)

    def spend(self, amount: float) -> None:
        if self.deployable is not None:
            self.deployable -= amount

    def claim(self, order: Mapping[str, Any]) -> None:
        kind = _protection.order_kind(order)
        if kind is None:
            self.claimed.add((order.get("symbol"), order.get("side")))
            return

        price = _protection.resting_price(order)
        if price is not None:
            self.protection.setdefault((order.get("symbol"), kind), []).append(price)

    def is_claimed(self, order: Mapping[str, Any]) -> bool:
        return (order.get("symbol"), order.get("side")) in self.claimed

    def protection_at(self, symbol: str, kind: str) -> list[float]:
        return self.protection.get((symbol, kind)) or []

    def take_position(self, symbol: str, sector: str | None, notional: float) -> None:
        if symbol != self.sleeve:
            self.open_symbols.add(symbol)
        self.position_value[symbol] = self.position_value.get(symbol, 0.0) + notional
        if sector is not None:
            self.sector_value[sector] = self.sector_value.get(sector, 0.0) + notional


def _duplicate_protection_denials(
    order: Mapping[str, Any],
    ctx: Mapping[str, Any],
    state: _PlanState,
    kind: str,
) -> list[Denial]:
    """§10.1's resting orders, refused when an equivalent one already rests.

    `reconcile._protection_plan` should have caught this first — it emits
    `keep` precisely for the case where the right order is already there — but
    guard is where the refusal has to be enforceable, for the same reason
    every other rule is duplicated across the two: the plan is computed once
    and the routine may be replaying it, resuming a died-mid-loop session, or
    running against a broker list that moved since.

    "Equivalent" is `protection_replace_min_pct`, the same band the plan's
    no-churn rule uses, so the two cannot disagree about whether a resting
    order is the one that was wanted.
    """
    price = _protection.resting_price(order)
    if price is None:
        # Not priceable as protection at all. `_notional` has already passed,
        # so this is a stop order with no stop price — a shape the broker
        # would take and never trigger.
        return [
            Denial(
                ORDER_NOT_PRICEABLE,
                f"{order.get('symbol')} protective {kind} carries no price to "
                "rest at",
            )
        ]

    band = float(ctx["config"]["protection_replace_min_pct"])
    for resting in state.protection_at(order.get("symbol"), kind):
        if _protection.within_band(price, resting, band):
            return [
                Denial(
                    DUPLICATE_PROTECTION,
                    f"a protective {kind} already rests on {order.get('symbol')} "
                    f"at {resting:.2f}, within {band:.2%} of the {price:.2f} "
                    "wanted; replacing it would leave the position uncovered "
                    "for no gain",
                )
            ]

    return []


def _order_denials(
    order: Mapping[str, Any], ctx: Mapping[str, Any], state: _PlanState
) -> list[Denial]:
    notional = _notional(order, ctx)
    if notional is None:
        # Nothing downstream can be judged on an order that cannot be costed,
        # so this denial stands alone rather than joining a list.
        return [
            Denial(
                ORDER_NOT_PRICEABLE,
                f"no price available for {order.get('symbol')} "
                f"{order.get('type')} order",
            )
        ]

    denials: list[Denial] = []
    denials.extend(_symbol_lock_denials(order, ctx))
    denials.extend(_candidate_denials(order, ctx))
    denials.extend(_legality_denials(order, ctx))

    if _meaningfully_less(notional, float(ctx["config"]["min_order_notional"])):
        denials.append(
            Denial(
                BELOW_MIN_NOTIONAL,
                f"notional {notional:.2f} is below the "
                f"{float(ctx['config']['min_order_notional']):.2f} broker floor",
            )
        )

    kind = _protection.order_kind(order)
    if kind is None:
        if state.is_claimed(order):
            denials.append(
                Denial(
                    DUPLICATE_OPEN_ORDER,
                    f"an open {order.get('side')} already exists on "
                    f"{order.get('symbol')}; a blind second order is how a position "
                    "doubles",
                )
            )
    else:
        denials.extend(_duplicate_protection_denials(order, ctx, state, kind))

    denials.extend(_overnight_spread_denials(order, ctx))
    denials.extend(_protection_contends_denials(order, ctx))

    if order.get("side") != "buy":
        return denials

    denials.extend(_entry_permission_denials(ctx))
    denials.extend(_strategy_enabled_denials(ctx))
    denials.extend(_sector_rotation_denials(order, ctx))

    if _account_implausible(
        ctx.get("account") or {},
        ctx.get("positions") or [],
        ctx.get("prior_benchmark") or {},
    ):
        denials.append(
            Denial(
                ACCOUNT_IMPLAUSIBLE,
                "account equity_total does not cover the value of open "
                "positions (or has collapsed against yesterday's benchmark); "
                "buys are denied until the read is confirmed",
            )
        )
        return denials

    breached, equity, floor = _drawdown_breached(ctx)
    if breached:
        denials.append(
            Denial(
                DRAWDOWN_HALT,
                f"equity {equity:.2f} is below the drawdown floor {floor:.2f}; "
                "exits only until cleared",
            )
        )
        return denials

    denials.extend(_settled_cash_denials(order, ctx, state, notional))
    # The core sleeve's buy (`src/core_sleeve.py`) skips exactly two checks,
    # both of which ask a question an index ETF has no answer to: it has no
    # earnings date to confirm, and the Monte Carlo read is the probability of
    # hitting a stop the sleeve does not have. Everything else stands, settled
    # cash and the loss re-entry window included.
    sleeve = _core_sleeve.is_sleeve_order(order, ctx)
    if not sleeve:
        denials.extend(_earnings_denials(order, ctx))
    denials.extend(_wash_sale_denials(order, ctx))
    denials.extend(_concentration_denials(order, ctx, state, notional))
    if not sleeve:
        denials.extend(_mc_denials(order, ctx))
    return denials


def evaluate(
    orders: Sequence[Mapping[str, Any]], ctx: Mapping[str, Any]
) -> GuardResult:
    """Decide which of ``orders`` may be placed under ``ctx``."""
    halts = _account_halts(ctx)
    if halts:
        return GuardResult(approved=[], denied=[], halts=halts)

    state = _PlanState(ctx)
    result = GuardResult()

    for order in orders:
        denials = _order_denials(order, ctx, state)
        if denials:
            result.denied.append(DeniedOrder(order=dict(order), denials=denials))
            continue

        result.approved.append(dict(order))
        state.claim(order)
        if order.get("side") == "buy":
            notional = _notional(order, ctx) or 0.0
            state.spend(notional)
            state.take_position(
                order.get("symbol"), (ctx.get("sectors") or {}).get(order.get("symbol")),
                notional,
            )

    return result
