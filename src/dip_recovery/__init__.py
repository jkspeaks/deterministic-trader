"""dip_recovery — Dip Recovery plan: entries, the no-loss averaging state
machine, the full seven-condition entry gate, the guard pass, and the
overnight watchlist pick.

A second, isolated strategy that
buys controlled dip tranches and exits a whole position only at a locked-in
net profit, never below cost. It reads the same daily cache core's own
`plays`/`sizing`/`guard` already read and writes to none of core's tables;
the only touchpoint between the two sides is `strategy_symbol_lock`
and the two enable flags.

Two entry points, one per routine, matching the plan's own split:

* `evaluate(ctx) -> DipRecoveryResult` is the **Dip Monitor** tick, composed
  the same way `cli.decide` composes core's — a pure function of its context,
  and (phase 7) the one that runs `guard.evaluate` over the plan it proposes,
  so nothing leaves here that guard has not approved.
* `pool(ctx)` then `pick(ctx)` are the **Universe Pick** morning routine
  (phase 8), which chooses the watchlist `evaluate` is later handed. Two
  functions rather than one because the per-symbol broker fetch happens
  between them, and the cost ceiling has to sit ahead of it.

See `engine.py`'s module docstring for the steps inside one Dip Monitor tick,
`watchlist.py`'s for why every filter in the pick narrows rather than widens,
and `core.py`'s for why `PAUSED` and `CLOSED_PROFIT`, two of the design doc's
states, are deliberately never persisted.

Live as of phase 8: `cli.dip_recovery_decide` and `cli.dip_recovery_pool` /
`cli.dip_recovery_pick` compose the contexts, `docs/routine/dip-monitor-prompt.md`
and `docs/routine/universe-pick-prompt.md` are the two routines that run them,
and `db/schema.sql` seeds their configuration. The concentration caps and the
pick's own thresholds do not depend on those seed rows —
`guard.concentration` and `watchlist._DEFAULTS` carry the same numbers inline,
the `signal.py` Bollinger precedent.
"""

from __future__ import annotations

from .core import (
    AVERAGE_PENDING,
    ENTRY_PENDING,
    EXIT_LOCKED,
    EXIT_PENDING,
    HOLD_NO_NEW_BUYS,
    HOLDING,
    STRATEGY,
    WATCHING,
    DipRecoveryResult,
    ManualReview,
    Order,
    Skipped,
    regime_v2_enabled,
)
from .engine import evaluate
from .signal import entry_signal
from .watchlist import WATCHLIST_KEY, pick, pool

__all__ = [
    "WATCHLIST_KEY",
    "AVERAGE_PENDING",
    "ENTRY_PENDING",
    "EXIT_LOCKED",
    "EXIT_PENDING",
    "HOLD_NO_NEW_BUYS",
    "HOLDING",
    "STRATEGY",
    "WATCHING",
    "DipRecoveryResult",
    "ManualReview",
    "Order",
    "Skipped",
    "entry_signal",
    "evaluate",
    "pick",
    "pool",
    "regime_v2_enabled",
]
