"""`edge_control.active_sectors` names the GICS sectors new money may go into.
Discovery (`cli.candidates`, `dip_recovery.pool`) keeps inactive-sector names
out of the pool, and `plays.entries` / `dip_recovery.engine.evaluate` stop a
buy on a name already held. This is the backstop behind all of them, for both
strategies: a buy that reaches guard in an inactive sector means one of those
gates was missed, and it is denied rather than placed.

Buys only, deliberately — the same reasoning as `_strategy_enabled_denials`.
Rotating a sector out is "no new money", never "liquidate": a held position
keeps its protection and every exit, whatever its sector.

Fails open, exactly as the discovery gates do (`universe.SectorRotation`):
no denial when the rotation is `[]`, when every name in it is unmatched, for
Broad Market, and for a symbol with no known sector — that last one already
meets `SECTOR_UNKNOWN` in `_concentration_denials`, which fails closed on its
own terms and must not be double-counted here.
"""

from __future__ import annotations

from typing import Any, Mapping

from src import universe as universes

from .core import Denial

SECTOR_INACTIVE = "SECTOR_INACTIVE"


def _sector_rotation_denials(order: Mapping[str, Any], ctx: Mapping[str, Any]) -> list[Denial]:
    rotation = universes.sector_rotation(
        (ctx.get("control") or {}).get("active_sectors"), ctx.get("etfs") or {},
        ctx.get("rotation_codes"), ctx.get("sub_industries"),
    )
    symbol = order.get("symbol")
    sector = rotation.inactive(symbol, ctx.get("sectors") or {}) if symbol else None
    if sector is None:
        return []
    return [
        Denial(
            SECTOR_INACTIVE,
            f"{sector} is outside the active sector rotation; no new money "
            f"into {symbol}",
        )
    ]
