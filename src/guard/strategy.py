"""guard.strategy — Dip Recovery plan, phase 3: the two per-strategy enable
flags.

The plan's draft proposed a third flag, `pause_all_strategies`. Review
dropped it: it would have duplicated `edge_control.trading_enabled`, which
`guard._account_halts` already checks for both sides. So there are exactly
two flags here, `core_strategies_enabled` and `dip_recovery_enabled`, each
read from `dip_recovery_config` and each gating only its own
side's entries.

Entries only, deliberately — the same reasoning as `_entry_permission_denials`
and the drawdown halt's "exits only until cleared". A strategy flipped off is
paused, not liquidated: it must go on managing and exiting whatever it
already holds, since force-closing every open position on a flag flip is not
what turning a strategy off means for either side.

Defaults preserve today's behavior exactly. `ctx["dip_recovery_config"]` is
absent from every payload that predates this phase, so `{}` is what every
existing cycle sends: core reads as enabled (unchanged from before this flag
existed) and Dip Recovery — which places no orders yet, on either flag's
value, since no routine has been built to call `guard.evaluate` with
`strategy="dip_recovery"` — reads as disabled, matching the fail-closed
default `dip_recovery_config`'s own seed row uses.
"""

from __future__ import annotations

from typing import Any, Mapping

from .core import Denial

STRATEGY_DISABLED = "STRATEGY_DISABLED"

_FLAG_BY_STRATEGY = {
    "core": "core_strategies_enabled",
    "dip_recovery": "dip_recovery_enabled",
}


def _strategy_enabled_denials(ctx: Mapping[str, Any]) -> list[Denial]:
    strategy = ctx.get("strategy") or "core"
    flag = _FLAG_BY_STRATEGY.get(strategy, "core_strategies_enabled")
    default = strategy == "core"

    config = ctx.get("dip_recovery_config") or {}
    if not bool(config.get(flag, default)):
        return [
            Denial(
                STRATEGY_DISABLED,
                f"{flag} is not enabled; '{strategy}' may not open new positions "
                "while it is off",
            )
        ]
    return []
