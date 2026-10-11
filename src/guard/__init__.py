"""guard — every denial. The layer the agent cannot argue past.

Design v3 §11. Guard never raises and never repairs: it returns a decision, and
an order that is not explicitly approved is denied. Two tiers:

* **Halts** stop the whole plan. Nothing is placed, including exits.
* **Order denials** remove one order and leave the rest of the plan standing.

The drawdown breach is deliberately the second kind. §6.5 lists it as exit
priority 2, so it must flatten the book — a halt that blocked its own flatten
would trap the account in the position it is halting over.

This file is the package's public surface: every name the flat module used to
expose at module level — including the `sizing`/`reconcile` re-exports — is
re-exported here unchanged, because `cli.py` and the test suite both reach into
it by attribute (``guard.is_fractional``, ``guard.MONEY_EPS``,
``guard._legality_denials``, ...).
"""

from __future__ import annotations

# §4 is sizing's, including the caps guard denies against and the settled-cash
# basis it funds buys from. Importing them keeps one definition of each: a
# guard that disagreed with the sizer would deny the sizer's own orders.
from src.sizing import (  # noqa: F401 — re-exported as part of guard's surface
    DEFAULT_SETTLED_CASH_POLICY,
    MONEY_EPS,
    POLICY_BUYING_POWER_IS_SETTLED,
    POLICY_CONSERVATIVE,
    drawdown_breached,
    drawdown_floor,
    flow_adjusted_equity,
    meaningfully_less as _meaningfully_less,
    position_cap_fraction,
    sector_cap_fraction,
    settled_basis,
)

# What "open" means to Robinhood, defined in the module whose subject is
# broker order state. Guard's duplicate-order check and reconcile's stale-order
# sweep have to agree on it: a guard that thought an order was still working
# after reconcile had cancelled it would deny the replacement sell forever.
from src.reconcile import TERMINAL_ORDER_STATES as _TERMINAL_ORDER_STATES

from .core import (
    ACCOUNT_IMPLAUSIBLE,
    BELOW_MIN_NOTIONAL,
    DRAWDOWN_HALT,
    DUPLICATE_OPEN_ORDER,
    ORDER_NOT_PRICEABLE,
    DeniedOrder,
    Denial,
    GuardResult,
    _order_denials,
    _PlanState,
    evaluate,
)
from .halts import ACCOUNT_UNREADABLE, CLOSED, CONTROL_UNREADABLE, SESSION_CLOSED, TRADING_DISABLED, _account_halts, _drawdown_breached
from .cash import (
    INSUFFICIENT_SETTLED_CASH,
    SAME_SESSION_SELL,
    SETTLED_CASH_UNKNOWN,
    _settled_cash_denials,
)
from .concentration import (
    POSITION_CAP_EXCEEDED,
    SECTOR_CAP_EXCEEDED,
    SECTOR_UNKNOWN,
    SLOT_LIMIT_REACHED,
    _concentration_denials,
)
from .lock import SYMBOL_LOCKED_OTHER_SIDE, _symbol_lock_denials
from .legality import (
    EXTENDED_SESSION_NOT_TRADABLE,
    FRACTIONAL_NOT_TRADABLE,
    FRACTIONAL_OUTSIDE_REGULAR_HOURS,
    FRACTIONAL_STOP_UNVERIFIED,
    NOT_TRADABLE,
    ORDER_TYPE_OUTSIDE_REGULAR_HOURS,
    OVERNIGHT_CYCLE,
    OVERNIGHT_SPREAD_GATE,
    PROTECTION_CONTENDS,
    REGULAR_HOURS,
    TRADABLE,
    WHOLE_SHARE_ONLY,
    _legality_denials,
    _overnight_spread_denials,
    _position_for,
    _protection_contends_denials,
    is_fractional,
)
from .mc import (
    MC_DRAWDOWN_P95_EXCEEDED,
    MC_SIMULATION_UNAVAILABLE,
    MC_STOP_PROBABILITY_EXCEEDED,
    _mc_denials,
)
from .schedule import (
    EARNINGS_DATE_UNAVAILABLE,
    EARNINGS_WINDOW,
    ENTRIES_BLOCKED,
    ENTRIES_NOT_ALLOWED_THIS_CYCLE,
    NOT_IN_CANDIDATE_SET,
    UNIVERSE_STALE,
    WASH_SALE_COOLDOWN,
    _as_date,
    _candidate_denials,
    _earnings_denials,
    _entry_permission_denials,
    _held_symbols,
    _wash_sale_denials,
)
from .strategy import STRATEGY_DISABLED, _strategy_enabled_denials
from .rotation import SECTOR_INACTIVE, _sector_rotation_denials
from .pricing import _notional

__all__ = [
    "ACCOUNT_IMPLAUSIBLE",
    "ACCOUNT_UNREADABLE",
    "BELOW_MIN_NOTIONAL",
    "CLOSED",
    "CONTROL_UNREADABLE",
    "DEFAULT_SETTLED_CASH_POLICY",
    "DRAWDOWN_HALT",
    "DUPLICATE_OPEN_ORDER",
    "EARNINGS_DATE_UNAVAILABLE",
    "EARNINGS_WINDOW",
    "ENTRIES_BLOCKED",
    "ENTRIES_NOT_ALLOWED_THIS_CYCLE",
    "EXTENDED_SESSION_NOT_TRADABLE",
    "FRACTIONAL_NOT_TRADABLE",
    "FRACTIONAL_OUTSIDE_REGULAR_HOURS",
    "FRACTIONAL_STOP_UNVERIFIED",
    "INSUFFICIENT_SETTLED_CASH",
    "MC_DRAWDOWN_P95_EXCEEDED",
    "MC_SIMULATION_UNAVAILABLE",
    "MC_STOP_PROBABILITY_EXCEEDED",
    "MONEY_EPS",
    "NOT_IN_CANDIDATE_SET",
    "NOT_TRADABLE",
    "ORDER_NOT_PRICEABLE",
    "ORDER_TYPE_OUTSIDE_REGULAR_HOURS",
    "OVERNIGHT_CYCLE",
    "OVERNIGHT_SPREAD_GATE",
    "PROTECTION_CONTENDS",
    "POLICY_BUYING_POWER_IS_SETTLED",
    "POLICY_CONSERVATIVE",
    "POSITION_CAP_EXCEEDED",
    "REGULAR_HOURS",
    "SAME_SESSION_SELL",
    "SECTOR_CAP_EXCEEDED",
    "SECTOR_INACTIVE",
    "SECTOR_UNKNOWN",
    "SESSION_CLOSED",
    "SETTLED_CASH_UNKNOWN",
    "SLOT_LIMIT_REACHED",
    "STRATEGY_DISABLED",
    "SYMBOL_LOCKED_OTHER_SIDE",
    "TRADABLE",
    "TRADING_DISABLED",
    "UNIVERSE_STALE",
    "WASH_SALE_COOLDOWN",
    "WHOLE_SHARE_ONLY",
    "DeniedOrder",
    "Denial",
    "GuardResult",
    "_PlanState",
    "_account_halts",
    "_as_date",
    "_candidate_denials",
    "_concentration_denials",
    "_drawdown_breached",
    "_earnings_denials",
    "_entry_permission_denials",
    "_held_symbols",
    "_legality_denials",
    "_mc_denials",
    "_notional",
    "_order_denials",
    "_overnight_spread_denials",
    "_position_for",
    "_settled_cash_denials",
    "_strategy_enabled_denials",
    "_sector_rotation_denials",
    "_symbol_lock_denials",
    "_wash_sale_denials",
    "drawdown_breached",
    "drawdown_floor",
    "evaluate",
    "flow_adjusted_equity",
    "is_fractional",
    "position_cap_fraction",
    "sector_cap_fraction",
    "settled_basis",
]
