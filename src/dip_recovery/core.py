"""dip_recovery.core — states, shared types, and the handful of primitives
`signal.py` and `engine.py` both need.

**Two of the design doc's states are deliberately never persisted here:
``PAUSED`` and ``CLOSED_PROFIT``.**

``PAUSED`` is the draft's "any active state, interrupted by a flag off".
Phase 3 already built that rule (`guard.strategy._strategy_enabled_denials`) as an entries-only denial at the point of order approval — the
single place every other account-level pause in this codebase lives (compare
`guard._account_halts`'s `TRADING_DISABLED`). A second, persisted "PAUSED"
row state would duplicate that gate with its own copy of the same rule, and
a position sitting in a stored `PAUSED` state would need every downstream
reader to know it secretly means "resume checking exits as if this were
still HOLDING" — which is just `_strategy_enabled_denials` again, reimplemented
worse. So the flag is checked once, at proposal time, by `engine.py` deciding
whether to *propose* a new entry or average-down; it never touches a
position's stored `state`, and an exit is evaluated on every cycle regardless
of the flag, exactly as core's own drawdown halt still lets its own flatten
sell go out.

``CLOSED_PROFIT`` is a one-cycle fact, not a state to sit in. The moment a
sell fill confirms, the design doc's own settlement rule fires: "a symbol
that closes profitably just re-enters WATCHING". Persisting a
``CLOSED_PROFIT`` row that every future cycle would have to recognise and
skip past is a state nothing downstream needs — `dip_recovery_positions`
already defines "no row" as WATCHING, so the closed position's row is simply
deleted. `evaluate()`'s result still reports the closed symbol for the one
cycle it happens, so the routine can alert on it; nothing reads it back off
the table on cycle two.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping

from src import cadence
from src import regime as regimes

# The five states this package actually persists. `ENTRY_PENDING` and
# `AVERAGE_PENDING` both mean "a buy has been proposed and placed but no fill
# has confirmed it yet" — the plan doc draws them as one path because that is
# exactly what they are once a fill lands (both resolve to `HOLDING`), and
# nothing here needs to tell an initial fill from an average-down fill after
# the fact except the fill count, which `total_fills` already carries.
WATCHING = "WATCHING"
ENTRY_PENDING = "ENTRY_PENDING"
HOLDING = "HOLDING"
AVERAGE_PENDING = "AVERAGE_PENDING"
EXIT_LOCKED = "EXIT_LOCKED"
EXIT_PENDING = "EXIT_PENDING"
HOLD_NO_NEW_BUYS = "HOLD_NO_NEW_BUYS"

STRATEGY = "dip_recovery"

# Why a watchlist symbol did not become a candidate, or why a held position
# did not average down or exit this cycle. Mirrors `plays.core`'s `Skipped`
# reason codes in spirit — "a rule looked at this and said no", not a denial,
# which stays guard's alone.
ENTRY_CONDITIONS_UNMET = "ENTRY_CONDITIONS_UNMET"
ALREADY_TRACKED = "ALREADY_TRACKED"
SYMBOL_LOCKED = "SYMBOL_LOCKED"
MAX_ACTIVE_POSITIONS = "MAX_ACTIVE_POSITIONS"
MAX_NEW_ENTRIES_PER_DAY = "MAX_NEW_ENTRIES_PER_DAY"
ENTRIES_DISABLED = "ENTRIES_DISABLED"
TOO_MANY_FILLS = "TOO_MANY_FILLS"
NOT_DOWN_ENOUGH = "NOT_DOWN_ENOUGH"
TOO_SOON_SINCE_FILL = "TOO_SOON_SINCE_FILL"
NOT_UNDERWATER_ENOUGH = "NOT_UNDERWATER_ENOUGH"
# The operator's `edge_control.active_sectors` excludes the symbol's sector:
# not picked into the pool, not entered, and — for a row this side already
# holds — not averaged down. Exits are never gated by it.
SECTOR_INACTIVE = "SECTOR_INACTIVE"

# The day's regime, in core DIP's own words and from the same definitions:
# `src/regime.py` owns the codes and the rules, `plays.core` re-exports them
# for core, this module for the sleeve. Emitted only under
# `regime_v2_enabled`; with it off the same verdicts are recorded as
# `shadow_would_skip`. New money only — an exit is never gated by any of them.
SKIP_REGIME = regimes.SKIP_REGIME
SKIP_SECTOR_BELOW_200D = regimes.SKIP_SECTOR_BELOW_200D
SKIP_SECTOR_RS_WEAK = regimes.SKIP_SECTOR_RS_WEAK
SKIP_RS_UNAVAILABLE = regimes.SKIP_RS_UNAVAILABLE

REGIME_V2_ENABLED = "regime_v2_enabled"


def regime_v2_enabled(ctx: Mapping[str, Any]) -> bool:
    """The same switch core reads (`plays.core.regime_v2_enabled`), off
    `config/config.json` — not a `dip_recovery_config` row: one regime, one
    switch, so the two strategies cannot be on different rules by accident."""
    return bool((ctx.get("config") or {}).get(REGIME_V2_ENABLED))

# `watchlist.py`'s own reasons — why a scan hit did not become one of the
# morning's five. Same `Skipped` type and the same meaning ("a rule looked at
# this and said no"), kept here rather than in `watchlist.py` so there is one
# place to read what this package can say about a symbol. `SYMBOL_LOCKED` and
# `ALREADY_TRACKED` above are reused verbatim: they mean exactly what they mean
# on the entry path, and a second spelling of "core owns it" would make the
# evaluations log depend on which routine happened to notice.
NOT_IN_UNIVERSE = "NOT_IN_UNIVERSE"
RECENT_LOSS_CLOSE = "RECENT_LOSS_CLOSE"
SCAN_POOL_TRUNCATED = "SCAN_POOL_TRUNCATED"
EVENT_RISK = "EVENT_RISK"
METRICS_UNAVAILABLE = "METRICS_UNAVAILABLE"
INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
ATR_OUT_OF_RANGE = "ATR_OUT_OF_RANGE"
RANK_NOT_TOP = "RANK_NOT_TOP"


def _signals(ctx: Mapping[str, Any], symbol: str) -> Mapping[str, Any]:
    return (ctx.get("signals") or {}).get(symbol) or {}


def _price(ctx: Mapping[str, Any], symbol: str) -> float | None:
    """Last trade — the same comparison basis `plays.core._price` uses, and
    for the same reason: guard costs an *order* at the far side of the
    spread, but a threshold comparison against a moving average or a fill
    price is not an order.
    """
    last = ((ctx.get("quotes") or {}).get(symbol) or {}).get("last")
    return None if last is None else float(last)


def _relvol(ctx: Mapping[str, Any], symbol: str) -> float | None:
    """Today's volume over its own average — the same ratio `plays.catalyst`
    already reads off `fundamentals`, reused rather than re-derived.
    """
    data = (ctx.get("fundamentals") or {}).get(symbol) or {}
    volume, average = data.get("volume"), data.get("average_volume")
    if volume is None or average is None or float(average) == 0:
        return None
    return float(volume) / float(average)


# The sleeve's 1.2x relative-volume gate had CATALYST's defect: `_relvol` is
# today's volume *so far* over a *full day's* average, read at the 11:47 and
# 12:47 ET ticks, so a name needed more than three times its normal volume by
# late morning to pass. Under `dip_recovery_relvol_time_normalised` the
# multiple is scaled to a full session first
# (`cadence.time_normalised_volume`). Off by default; both figures are in
# every relvol rejection and every proposal's factors.
RELVOL_TIME_NORMALISED = "dip_recovery_relvol_time_normalised"
RELVOL_MIN_MINUTES = "dip_recovery_relvol_min_minutes"
RELVOL_MIN_MINUTES_DEFAULT = 30
SKIP_TOO_EARLY_FOR_RVOL = "SKIP_TOO_EARLY_FOR_RVOL"


def _relvol_reading(ctx: Mapping[str, Any], symbol: str) -> dict[str, Any]:
    """`{raw, tnorm, minutes, normalised, min_minutes}` for the relvol gate.

    ``normalised`` is whether the scaled figure decides: the switch is on
    *and* there is a clock to scale against. With no clock the raw figure
    decides, as before.
    """
    cycle = ctx.get("cycle") or {}
    config = ctx.get("config") or {}
    data = (ctx.get("fundamentals") or {}).get(symbol) or {}
    minutes = cadence.minutes_since_open(cycle.get("now"), cycle.get("trading_day"))
    try:
        session = cadence.session_minutes(
            date.fromisoformat(str(cycle.get("trading_day"))), ctx.get("calendar") or {}
        )
    except (TypeError, ValueError):
        session = cadence.SESSION_MINUTES
    raw, tnorm = cadence.time_normalised_volume(
        data.get("volume"), data.get("average_volume"), minutes, session
    )
    floor = config.get(RELVOL_MIN_MINUTES)
    return {
        "raw": raw,
        "tnorm": tnorm,
        "minutes": minutes,
        "normalised": bool(config.get(RELVOL_TIME_NORMALISED)) and minutes is not None,
        "min_minutes": float(RELVOL_MIN_MINUTES_DEFAULT if floor is None else floor),
    }


def _clean(value: float) -> float:
    return round(value, 6)


def _config(ctx: Mapping[str, Any]) -> Mapping[str, Any]:
    return ctx.get("dip_recovery_config") or {}


def _entries_enabled(ctx: Mapping[str, Any]) -> bool:
    """Mirrors `guard.strategy._strategy_enabled_denials`'s own default —
    absent config reads as disabled, fail-closed, for the same reason: no
    routine existed to legitimately need it on before this phase.
    """
    return bool(_config(ctx).get("dip_recovery_enabled", False))


def _threshold_group(ctx: Mapping[str, Any], group: str) -> Mapping[str, Any]:
    return _config(ctx).get(group) or {}


def _position(ctx: Mapping[str, Any], symbol: str) -> Mapping[str, Any] | None:
    return (ctx.get("dip_recovery_positions") or {}).get(symbol)


@dataclass(frozen=True)
class Skipped:
    """A watchlist symbol or held position a rule looked at and passed on.

    Not a denial — guard owns those, and every proposal this package emits
    still has to clear `guard.evaluate` before it places. This is dip_recovery
    declining to *propose* something at all, logged for
    `dip_recovery_evaluations` the same way `plays.Skipped` feeds the runlog.
    """

    symbol: str
    code: str
    message: str

    def as_dict(self) -> dict:
        return {"symbol": self.symbol, "code": self.code, "message": self.message}


@dataclass(frozen=True)
class EntryProposal:
    """A new WATCHING symbol whose dip signal fired. Not yet an order —
    `engine.evaluate` still has to size it against the 5-position/3-per-day
    caps before it becomes one.
    """

    symbol: str
    score: float
    factors: Mapping[str, float]
    detail: str


@dataclass(frozen=True)
class Order:
    """A proposed buy or sell, in `guard.evaluate`'s order shape.

    Always a limit order — the design doc's exit section is explicit
    ("placed as a single limit order") and this package extends the same
    discipline to the entry and averaging buys: a market order on a
    strategy whose entire thesis is "buy at a controlled discount" would
    let a fast-moving quote buy the dip away before the order prices it.
    """

    symbol: str
    side: str
    quantity: float
    limit_price: float
    reason: str

    def as_order(self, session: str) -> dict:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "type": "limit",
            "quantity": self.quantity,
            "limit_price": self.limit_price,
            "stop_price": None,
            "session": session,
            "strategy": STRATEGY,
        }

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "limit_price": self.limit_price,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ManualReview:
    """A `HOLD_NO_NEW_BUYS` transition — the plan doc's "flagged notification
    for manual review". Never a sell: the position is never force-closed at
    a loss, it simply stops being offered new capital.
    """

    symbol: str
    trading_days_held: int
    message: str

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "trading_days_held": self.trading_days_held,
            "message": self.message,
        }


@dataclass
class DipRecoveryResult:
    orders: list[Order] = field(default_factory=list)
    manual_review: list[ManualReview] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    evaluations: list[dict] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    position_updates: dict[str, dict] = field(default_factory=dict)
    control_updates: dict = field(default_factory=dict)

    # Mirrors `reconcile.ReconcileResult`'s own two fields, and is meant to
    # be handed to the same `edge_symbol_lock_claim`/`_release` RPCs
    # `reconcile.py` calls for core's side. Phase 1 named the rule
    # as "claimed... at the one point a fill is confirmed" — not "claimed by
    # `reconcile.py` specifically" — and confirming a fill is exactly what
    # `engine._apply_fills` does for this side.
    symbol_lock_claims: list[str] = field(default_factory=list)
    symbol_lock_releases: list[str] = field(default_factory=list)

    # Dip Recovery plan phase 7. Every order `guard.evaluate` refused, in
    # guard's own `DeniedOrder` shape — the same key `decide.json` carries and
    # `report._blocked_alert` reads, so a Dip Recovery denial is as legible in
    # the runlog as a core one. Distinct from `skipped`, which is why a symbol
    # never became an order at all: `blocked` is an order this side proposed,
    # sized and then was not allowed to place.
    blocked: list[dict] = field(default_factory=list)

    # What the regime gate would have done while `regime_v2_enabled` is off:
    # the entries and average-downs it would have skipped (each with the code
    # and message it would have carried) and the entries it would have sized
    # down. Both empty when the gate is enforced, and when there is no regime.
    shadow_would_skip: list[dict] = field(default_factory=list)
    shadow_would_resize: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "orders": [o.as_dict() for o in self.orders],
            "blocked": list(self.blocked),
            "manual_review": [m.as_dict() for m in self.manual_review],
            "closed": list(self.closed),
            "evaluations": list(self.evaluations),
            "skipped": [s.as_dict() for s in self.skipped],
            "position_updates": dict(self.position_updates),
            "control_updates": dict(self.control_updates),
            "symbol_lock_claims": list(self.symbol_lock_claims),
            "symbol_lock_releases": list(self.symbol_lock_releases),
            "shadow_would_skip": list(self.shadow_would_skip),
            "shadow_would_resize": list(self.shadow_would_resize),
        }
