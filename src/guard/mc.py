"""Monte Carlo pre-trade denials. Buy-only, like `DRAWDOWN_HALT` — an
unfavorable or missing simulation read must never block an exit, only stop a
new entry from being added.

The simulation itself never runs in `src/`: the no-network invariant
(`tests/test_invariant_no_network.py`) forbids it, and the routine is the only
component with network +
judgment. The routine runs a bootstrap simulation per sizing candidate and
hands back a compact per-symbol result under
`ctx["mc_check"]` — the same arrival pattern `signals`/`tradability` already
use. This module only judges what the routine already fetched.

`ctx["mc_check"]` is treated as an opt-in key, not a required one: a cycle
that never populates it (every test written before this module, and any
routine build that hasn't wired the simulation yet) sees no denials from
here at all. Once the routine starts populating the key, a candidate missing
from it — the routine tried and the read did not come back — denies that one
buy. That distinction is what keeps this fail-closed for a cycle that is
actually running the check, without fail-closing every fixture and every
build that predates it.
"""

from __future__ import annotations

from typing import Any, Mapping

from .core import Denial

MC_SIMULATION_UNAVAILABLE = "MC_SIMULATION_UNAVAILABLE"
MC_STOP_PROBABILITY_EXCEEDED = "MC_STOP_PROBABILITY_EXCEEDED"
MC_DRAWDOWN_P95_EXCEEDED = "MC_DRAWDOWN_P95_EXCEEDED"


def _mc_denials(order: Mapping[str, Any], ctx: Mapping[str, Any]) -> list[Denial]:
    if "mc_check" not in ctx:
        return []

    symbol = order.get("symbol")
    mc = (ctx.get("mc_check") or {}).get(symbol)
    if mc is None:
        return [
            Denial(
                MC_SIMULATION_UNAVAILABLE,
                f"no Monte Carlo read for {symbol} this cycle; a buy the "
                "routine cannot size the downside of is denied, not waved "
                "through",
            )
        ]

    config = ctx["config"]
    denials: list[Denial] = []

    p_stop = mc.get("p_stop")
    if p_stop is not None:
        p_stop_max = float(config["mc_stop_probability_max"])
        if float(p_stop) > p_stop_max:
            denials.append(
                Denial(
                    MC_STOP_PROBABILITY_EXCEEDED,
                    f"simulated stop probability {float(p_stop):.2f} exceeds "
                    f"{p_stop_max:.2f}",
                )
            )

    drawdown_p95 = mc.get("drawdown_p95")
    if drawdown_p95 is not None:
        drawdown_max = float(config["mc_drawdown_p95_max"])
        if float(drawdown_p95) > drawdown_max:
            denials.append(
                Denial(
                    MC_DRAWDOWN_P95_EXCEEDED,
                    f"simulated p95 drawdown {float(drawdown_p95):.2f} "
                    f"exceeds {drawdown_max:.2f}",
                )
            )

    return denials
