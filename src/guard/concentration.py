"""§4.4's slot, position and sector caps. ``state: _PlanState`` is a deferred
annotation (see `cash.py`'s docstring for why this module does not import it).

Strategy-scoped since Dip Recovery phase 7, the same way `guard.lock`
(phase 1), `guard.strategy` (phase 3) and `guard.schedule` (phases 4.5 and
4.5b) already branch on ``ctx["strategy"]``. What changes between the sides is
only *where the three numbers come from*; the arithmetic below is shared on
purpose, because "how much of the account may sit in one name" is one question
however many strategies are asking it.

Core's n is `sizing.n_target`, the equity-derived ladder. Dip Recovery's is the
flat `max_active_positions` its plan doc states outright, and feeding core's
ladder to it was not a conservative default but a broken one: an absent
`sizing` key reads `n_target` 0, so `len(open_symbols) >= 0` denied every buy
`SLOT_LIMIT_REACHED` on arithmetic rather than on risk.
"""

from __future__ import annotations

from typing import Any, Mapping

from src import core_sleeve
from src.sizing import (
    meaningfully_less as _meaningfully_less,
    position_cap_fraction,
    sector_cap_fraction,
)

from .core import Denial

SLOT_LIMIT_REACHED = "SLOT_LIMIT_REACHED"
POSITION_CAP_EXCEEDED = "POSITION_CAP_EXCEEDED"
SECTOR_CAP_EXCEEDED = "SECTOR_CAP_EXCEEDED"
SECTOR_UNKNOWN = "SECTOR_UNKNOWN"

DIP_RECOVERY = "dip_recovery"

# The schema's seeded values, inline so the gate is right on a checkout the
# migration has not reached — the same discipline `dip_recovery/signal.py`
# applies to the Bollinger defaults, and for the same reason: a threshold that
# only exists in Supabase is a threshold that fails differently in tests than
# it does in production.
_DIP_MAX_ACTIVE = 5


def _dip_caps(ctx: Mapping[str, Any]) -> tuple[int, float, float]:
    """This side's slot count and its two cap fractions.

    The slot count is read from `sizing_thresholds`, where `engine._Ledger`
    already reads it, rather than restated under `concentration` — one ceiling,
    one home. The fractions default to §4.4's own formulas evaluated at that
    slot count, so the seeded numbers in `db/schema.sql` are a knob over the shared
    rule rather than a second, independent rule.
    """
    config = ctx.get("dip_recovery_config") or {}
    sizing_thresholds = config.get("sizing_thresholds") or {}
    caps = config.get("concentration") or {}

    n = int(sizing_thresholds.get("max_active_positions", _DIP_MAX_ACTIVE) or _DIP_MAX_ACTIVE)
    position = float(caps.get("position_cap_pct") or position_cap_fraction(n))
    sector = float(caps.get("sector_cap_pct") or sector_cap_fraction(n))
    return n, position, sector


def _concentration_denials(
    order: Mapping[str, Any],
    ctx: Mapping[str, Any],
    state: _PlanState,
    notional: float,
) -> list[Denial]:
    symbol = order.get("symbol")
    denials: list[Denial] = []

    # §17's SURGE. A full-send order is allowed to ignore §4.4's slot, position
    # and sector caps by design — `sizing` has already committed the whole
    # available cash to it. Every other guard category still runs; this is the
    # one that is deliberately waived.
    if order.get("full_deployment"):
        return denials

    # The core sleeve has one cap of its own in place of the slot, position
    # and sector caps, which were written for single names. A named exemption
    # with its own ceiling, as full-send is, and not a raised cap.
    if core_sleeve.is_sleeve_order(order, ctx):
        message = core_sleeve.cap_denial(
            order, state.position_value.get(symbol, 0.0), notional, state.equity, ctx
        )
        if message is not None:
            denials.append(Denial(core_sleeve.CORE_CAP_EXCEEDED, message))
        if (ctx.get("sectors") or {}).get(symbol) is None:
            denials.append(
                Denial(
                    SECTOR_UNKNOWN,
                    f"no sector known for {symbol}; the sleeve's ETF is listed "
                    "by `core_sleeve.loaded_with_sleeve`, which this payload skipped",
                )
            )
        return denials

    if (ctx.get("strategy") or "core") == DIP_RECOVERY:
        slots, position_fraction, sector_fraction = _dip_caps(ctx)
    else:
        slots = state.n_target
        position_fraction = position_cap_fraction(state.n_target)
        sector_fraction = sector_cap_fraction(state.n_target)

    # `state.open_symbols`, `position_value` and `sector_value` are built from
    # `ctx["positions"]`, which on the Dip Recovery path is that side's own
    # book (`cli.dip_recovery_decide` shapes `dip_recovery_positions` into it)
    # rather than core's. The *equity* it is all measured against stays the
    # whole account's, deliberately: a Dip Recovery position is exposure of the
    # account, not of a notional sleeve, which is the entire point of a
    # concentration cap.
    if symbol not in state.open_symbols and len(state.open_symbols) >= slots:
        denials.append(
            Denial(
                SLOT_LIMIT_REACHED,
                f"{len(state.open_symbols)} of {slots} slots are already "
                f"held; {symbol} would be the n+1-th position",
            )
        )

    position_cap = state.equity * position_fraction
    prospective = state.position_value.get(symbol, 0.0) + notional
    if _meaningfully_less(position_cap, prospective):
        denials.append(
            Denial(
                POSITION_CAP_EXCEEDED,
                f"{symbol} would reach {prospective:.2f} against a "
                f"{position_cap:.2f} position cap at n={slots}",
            )
        )

    sector = (ctx.get("sectors") or {}).get(symbol)
    if sector is None:
        denials.append(
            Denial(
                SECTOR_UNKNOWN,
                f"no sector known for {symbol}; the sector cap cannot be "
                "evaluated, so the buy fails closed",
            )
        )
        return denials

    sector_cap = state.equity * sector_fraction
    prospective_sector = state.sector_value.get(sector, 0.0) + notional
    if _meaningfully_less(sector_cap, prospective_sector):
        denials.append(
            Denial(
                SECTOR_CAP_EXCEEDED,
                f"{sector} would reach {prospective_sector:.2f} against a "
                f"{sector_cap:.2f} sector cap at n={slots}",
            )
        )

    return denials
