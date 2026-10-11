"""dip_recovery.engine — the state machine itself.

One call, `evaluate(ctx)`, run once per Dip Monitor cycle. Order of work
matters and mirrors `cli.decide`'s own reasoning for why cancels-then-sells-
then-buys is the right order there: a fill has to be applied before this
cycle's own signals are read against the position it created, or an
average-down fill landing this cycle would be judged against yesterday's
weighted average cost.

1. **Apply fills** (`_apply_fills`) — confirm what the broker actually did
   since the last cycle. A buy fill advances `ENTRY_PENDING`/`AVERAGE_PENDING`
   to `HOLDING`, recomputes the weighted average cost, and resets the T+1
   clock to the fill's own trading day. A sell fill closes the position:
   `dip_recovery_positions` drops the row entirely (the design doc's "a
   symbol that closes profitably just re-enters WATCHING" — see `core.py`'s
   docstring for why `CLOSED_PROFIT` is reported, not stored).
2. **Manage what is already held** (`_manage`) — for every symbol still
   tracked after step 1: is the profit target met and is T+1 clear (propose
   the exit), else is an average-down eligible (propose one), else has the
   47-trading-day hold escape been crossed (flag for manual review). Exits
   are evaluated unconditionally, on every cycle, exactly the way
   `plays.exits` never gates a sale on `entries_allowed` — pausing the
   strategy must never block managing a position it already opened.
3. **Propose new entries** (`_propose_entries`) — only for a watchlist symbol
   with no existing row, gated by the same `dip_recovery_enabled` flag
   `guard.strategy` checks at order approval (checked again here, before
   scoring, so a disabled strategy generates no evaluation-log noise about
   candidates it was never going to buy) and by this side's own 5-position /
   3-per-day caps.

Every order this produces is still just a proposal. `guard.evaluate` is the
sole authority on whether it may place — this module duplicates none of
guard's cash, concentration, legality or symbol-lock checks, matching the
same division of labour `plays.entries`/`plays.exits` already have with
`guard.py` for core's own side.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping, Sequence

from src.cadence import sessions_between
# Dip Recovery plan phase 7. Guard runs *inside* this engine rather than as a
# routine step after it, mirroring `cli.decide`, which has always called
# `guard.evaluate` itself. No import cycle: guard reaches into `sizing`,
# `reconcile` and `plays`, none of which know this package exists.
from src.guard import evaluate as guard_evaluate
from src import regime as regimes
from src import universe as universes
from src.sizing import FRACTIONAL, cash_reserve, select_path, settled_basis

from .core import (
    ALREADY_TRACKED,
    AVERAGE_PENDING,
    ENTRIES_DISABLED,
    ENTRY_PENDING,
    EXIT_LOCKED,
    EXIT_PENDING,
    HOLD_NO_NEW_BUYS,
    HOLDING,
    MAX_ACTIVE_POSITIONS,
    MAX_NEW_ENTRIES_PER_DAY,
    NOT_DOWN_ENOUGH,
    NOT_UNDERWATER_ENOUGH,
    SECTOR_INACTIVE,
    SKIP_REGIME,
    STRATEGY,
    SYMBOL_LOCKED,
    TOO_MANY_FILLS,
    TOO_SOON_SINCE_FILL,
    DipRecoveryResult,
    EntryProposal,
    ManualReview,
    Order,
    Skipped,
    _config,
    _entries_enabled,
    _price,
    _threshold_group,
    regime_v2_enabled,
)
from .signal import entry_signal

# Any row in this set holds shares and is a candidate for the exit/averaging
# checks. `WATCHING` never has a row (see `core.py`); `ENTRY_PENDING` does but
# owns no shares yet, so it is deliberately excluded — there is nothing to
# average down or exit on an order that has not filled.
_MANAGED_STATES = frozenset({HOLDING, AVERAGE_PENDING, EXIT_LOCKED, EXIT_PENDING, HOLD_NO_NEW_BUYS})

# Every row in `dip_recovery_positions` counts against the 5-position cap,
# `ENTRY_PENDING` included — an order resting at the broker has already
# claimed the slot it would occupy once filled, the same reasoning
# `sizing._Budget.has_slot_for` applies to a staged core entry.
_ACTIVE_STATES = _MANAGED_STATES | {ENTRY_PENDING}

_NEW_ENTRIES_KEY = "dip_recovery_new_positions"


def _ask(ctx: Mapping[str, Any], symbol: str) -> float | None:
    price = ((ctx.get("quotes") or {}).get(symbol) or {}).get("ask")
    return None if price is None or float(price) <= 0 else float(price)


def _now(ctx: Mapping[str, Any]) -> datetime:
    return datetime.fromisoformat(str(ctx["cycle"]["now"]).replace("Z", "+00:00"))


def _trading_day(ctx: Mapping[str, Any]) -> date:
    return date.fromisoformat(str(ctx["cycle"]["trading_day"]))


def _target_price(row: Mapping[str, Any], ctx: Mapping[str, Any]) -> float:
    """§exit — weighted average cost times one plus the target and the
    execution-cost buffer, both percentages in `exit_thresholds`.
    """
    thresholds = _threshold_group(ctx, "exit_thresholds")
    target_pct = float(thresholds.get("profit_target_pct", 5.0)) / 100.0
    buffer_pct = float(thresholds.get("cost_buffer_pct", 0.25)) / 100.0
    return float(row["weighted_avg_cost"]) * (1.0 + target_pct + buffer_pct)


def _t1_clear(row: Mapping[str, Any], ctx: Mapping[str, Any]) -> bool:
    clock = row.get("clock_trading_day")
    if clock is None:
        return False
    return sessions_between(date.fromisoformat(str(clock)), _trading_day(ctx), ctx.get("calendar") or {}) >= 1


def _sessions_since_clock(row: Mapping[str, Any], ctx: Mapping[str, Any]) -> int | None:
    clock = row.get("clock_trading_day")
    if clock is None:
        return None
    return sessions_between(date.fromisoformat(str(clock)), _trading_day(ctx), ctx.get("calendar") or {})


def _new_row(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "state": "WATCHING",
        "opened_trading_day": None,
        "clock_trading_day": None,
        "weighted_avg_cost": None,
        "total_quantity": 0.0,
        "total_fills": 0,
        "latest_fill_at": None,
        "fills": [],
    }


def _apply_fill(row: Mapping[str, Any], fill: Mapping[str, Any]) -> dict:
    """One confirmed fill, folded into ``row``. Buys recompute the weighted
    average cost from every confirmed fill (the plan doc's own wording) and
    reset the T+1 clock to this fill's trading day; a sell is read as closing
    the whole position, matching "the only normal exit is a whole-position
    sell" — this package never proposes a partial one.
    """
    quantity = float(fill["quantity"])
    price = float(fill["price"])

    if fill.get("side") == "sell":
        return {**_new_row(row["symbol"]), "_closed": True}

    old_quantity = float(row.get("total_quantity") or 0.0)
    old_cost = row.get("weighted_avg_cost")
    new_quantity = old_quantity + quantity
    new_cost = price if not old_cost or old_quantity <= 0 else (
        (old_quantity * float(old_cost) + quantity * price) / new_quantity
    )

    return {
        **row,
        "state": HOLDING,
        "opened_trading_day": row.get("opened_trading_day") or fill["trading_day"],
        "clock_trading_day": fill["trading_day"],
        "weighted_avg_cost": round(new_cost, 6),
        "total_quantity": round(new_quantity, 6),
        "total_fills": int(row.get("total_fills") or 0) + 1,
        "latest_fill_at": fill.get("filled_at"),
        "fills": [*row.get("fills", []), dict(fill)],
    }


def _apply_fills(
    positions: Mapping[str, Mapping[str, Any]], ctx: Mapping[str, Any]
) -> tuple[dict[str, dict], list[str], list[str], list[str]]:
    """Every confirmed fill since the last cycle, folded in.

    Returns the updated working set of positions, the symbols closed this
    cycle (a full sell confirmed), and the lock claims/releases this side's
    fills earn — `evaluate()` reports the latter two so the routine can call
    `edge_symbol_lock_claim`/`_release` the same cycle, the same
    way `reconcile.py` does for core's own fills.
    """
    working = {symbol: dict(row) for symbol, row in positions.items()}
    closed: list[str] = []
    claims: list[str] = []
    releases: list[str] = []

    for fill in ctx.get("dip_recovery_fills") or []:
        symbol = fill["symbol"]
        current = working.get(symbol) or _new_row(symbol)
        updated = _apply_fill(current, fill)

        if updated.pop("_closed", False):
            working.pop(symbol, None)
            closed.append(symbol)
            releases.append(symbol)
        else:
            working[symbol] = updated
            claims.append(symbol)

    return working, closed, claims, releases


def _average_down_skip(
    row: Mapping[str, Any], ctx: Mapping[str, Any], price: float | None
) -> Skipped | None:
    thresholds = _threshold_group(ctx, "sizing_thresholds")
    max_fills = int(thresholds.get("max_fills_before_stop_averaging", 4))
    trigger_pct = float(thresholds.get("average_down_trigger_pct", -3.0))
    min_drop_pct = float(thresholds.get("average_down_min_price_drop_pct", 2.5))
    min_minutes = float(thresholds.get("average_down_min_minutes_since_fill", 30))
    symbol = row["symbol"]

    if int(row.get("total_fills") or 0) >= max_fills:
        return Skipped(symbol, TOO_MANY_FILLS, f"{symbol} has already averaged down to the {max_fills}-fill limit")

    if price is None:
        return Skipped(symbol, NOT_UNDERWATER_ENOUGH, f"no live price for {symbol}")

    wac = float(row["weighted_avg_cost"])
    unrealized_pct = (price - wac) / wac * 100.0
    if unrealized_pct > trigger_pct:
        return Skipped(
            symbol, NOT_UNDERWATER_ENOUGH,
            f"{symbol} is {unrealized_pct:.2f}% unrealized, above the "
            f"{trigger_pct:g}% average-down trigger",
        )

    latest_fill_price = float(row["fills"][-1]["price"]) if row.get("fills") else wac
    drop_pct = (latest_fill_price - price) / latest_fill_price * 100.0
    if drop_pct < min_drop_pct:
        return Skipped(
            symbol, NOT_DOWN_ENOUGH,
            f"{symbol} is only {drop_pct:.2f}% below its latest fill, short "
            f"of the {min_drop_pct:g}% floor",
        )

    latest_fill_at = row.get("latest_fill_at")
    if latest_fill_at is not None:
        minutes = (_now(ctx) - datetime.fromisoformat(str(latest_fill_at).replace("Z", "+00:00"))).total_seconds() / 60.0
        if minutes < min_minutes:
            return Skipped(
                symbol, TOO_SOON_SINCE_FILL,
                f"{symbol}'s latest fill was {minutes:.0f} minutes ago, short "
                f"of the {min_minutes:g}-minute floor",
            )

    return None


class _Ledger:
    """What this cycle may still spend, and how many of the two Dip Recovery
    caps are left. A thin, single-strategy cousin of `sizing._Budget` — no
    per-sector headroom (the plan doc names no sector rule of its own for
    this strategy) and a fixed 5-slot ceiling rather than an equity-derived
    ladder, because that ceiling is a number the plan doc states outright
    rather than one `sizing.n_target` should be asked to re-derive.

    Constructed once per cycle and threaded through both the management loop
    and the new-entry loop, so a second average-down proposed later in the
    same cycle sees the cash the first one already spent rather than
    double-spending it.
    """

    def __init__(self, ctx: Mapping[str, Any], active_count: int) -> None:
        thresholds = _threshold_group(ctx, "sizing_thresholds")
        self.max_active = int(thresholds.get("max_active_positions", 5))
        self.max_new_per_day = int(thresholds.get("max_new_entries_per_day", 3))
        self.active_count = active_count

        control = ctx.get("control") or {}
        taken = control.get(_NEW_ENTRIES_KEY) or {}
        trading_day = str(ctx["cycle"]["trading_day"])
        self.new_today = int(taken.get("count") or 0) if taken.get("date") == trading_day else 0
        self.trading_day = trading_day

        account = ctx.get("account") or {}
        equity = float(account.get("equity_total") or 0.0)
        basis = settled_basis(account, ctx["config"])
        self.remaining = None if basis is None else max(0.0, basis - cash_reserve(equity, ctx["config"]))

    def has_room_for_new_entry(self) -> bool:
        return self.active_count < self.max_active and self.new_today < self.max_new_per_day

    def spend(self, notional: float, *, new_entry: bool) -> None:
        """Draw down the cash a sized order just committed. Only a brand-new
        symbol consumes a position slot and a day's entry allowance — an
        average-down adds to a slot the position already occupies.
        """
        if self.remaining is not None:
            self.remaining -= notional
        if new_entry:
            self.active_count += 1
            self.new_today += 1

    def refund(self, notional: float, *, new_entry: bool) -> None:
        """Give back what a proposal reserved once guard refused it.

        Sizing has to spend as it goes — `_size_buy` divides the remaining
        balance across the slots still open, so a second candidate sized
        against an unspent balance would let this cycle commit the same cash
        twice. But an order guard denies was never placed, and a cycle whose
        entries guard refused must not spend the day's allowance on nothing:
        `cli._new_positions` already settled that rule for core ("counting
        *proposals* would let a cycle whose entries guard refused spend the
        day's allowance on nothing at all"), and `control_update` is read
        after this pass, so refunding here is what makes the counter agree
        with the orders that actually exist.

        Without it, `_NEW_ENTRIES_KEY` advances on a denial and — far worse —
        the `ENTRY_PENDING` row would too: `_MANAGED_STATES` excludes that
        state so nothing ever revisits it, while `_ACTIVE_STATES` includes it
        so it holds a slot, and `ALREADY_TRACKED` would refuse the symbol
        forever. One denial, one permanently leaked slot.
        """
        if self.remaining is not None:
            self.remaining += notional
        if new_entry:
            self.active_count -= 1
            self.new_today -= 1

    def control_update(self) -> dict:
        return {_NEW_ENTRIES_KEY: {"date": self.trading_day, "count": self.new_today}}


def _size_buy(
    symbol: str, ctx: Mapping[str, Any], ledger: _Ledger, slots_left: int,
    scale: float = 1.0,
) -> Order | Skipped:
    """A dip entry or an average-down, priced and floored identically — both
    spend from the same deployable cash and the same per-slot share of it.
    ``slots_left`` is the divisor: a fresh entry divides the account's
    remaining Dip Recovery cash across the positions it does not yet hold,
    while an average-down (which occupies no new slot) still spends against
    that same per-slot share rather than the whole remaining balance, so one
    large add-on cannot starve every other candidate this cycle.

    ``scale`` is the day's posture multiplier (`regime.entry_size_multiplier`)
    on a new entry — 0.5 in REDUCED_SIZE and HIGH_CONVICTION_ONLY. It can only
    shrink the slot, and an average-down is never scaled.
    """
    floor = float(ctx["config"]["min_order_notional"])
    if ledger.remaining is None:
        return Skipped(symbol, "SETTLED_CASH_UNKNOWN", "settled cash could not be established")

    notional = min(ledger.remaining / max(1, slots_left), ledger.remaining)
    notional *= max(0.0, min(1.0, scale))
    if notional < floor:
        return Skipped(symbol, "DEPLOYABLE_EXHAUSTED", f"{notional:.2f} left per slot, below the {floor:.2f} floor")

    ask = _ask(ctx, symbol)
    if ask is None:
        return Skipped(symbol, "NO_PRICE", f"no ask available for {symbol}")

    path, quantity = select_path(ask, notional)
    if path == FRACTIONAL and bool(ctx["config"].get("whole_shares_only", True)):
        return Skipped(symbol, "WHOLE_SHARE_ONLY", f"{notional:.2f} per slot cannot afford one full share of {symbol} at {ask:.2f}; this account buys whole shares only")

    if quantity * ask < floor:
        return Skipped(symbol, "BELOW_MIN_NOTIONAL", f"{quantity:g} of {symbol} at {ask:.2f} is below the {floor:.2f} floor")

    return Order(symbol, "buy", quantity, ask, f"dip entry/average at {ask:.2f}")


def _manage(
    row: dict, ctx: Mapping[str, Any], entries_enabled: bool
) -> tuple[dict, Order | None, bool, ManualReview | None, Skipped | None]:
    """One already-held symbol, one cycle. Returns the row's new state, a
    sell order if the exit fired, whether an average-down is wanted (sized by
    the caller, which alone knows this cycle's remaining cash), a
    manual-review flag if the 47-day escape just fired, and why averaging did
    not fire (for the evaluations log) when it did not.
    """
    symbol = row["symbol"]
    price = _price(ctx, symbol)
    target = _target_price(row, ctx)
    hit_target = price is not None and price >= target
    t1_clear = _t1_clear(row, ctx)

    if hit_target and t1_clear:
        quantity = float(row["total_quantity"])
        order = Order(symbol, "sell", quantity, price, f"{symbol} cleared T+1 at/above its {target:.2f} target")
        return ({**row, "state": EXIT_PENDING}, order, False, None, None)

    if hit_target:
        return ({**row, "state": EXIT_LOCKED}, None, False, None, None)

    max_hold = int(_config(ctx).get("max_hold_trading_days", 47))
    sessions = _sessions_since_clock(row, ctx)
    stalled = row["state"] != HOLD_NO_NEW_BUYS and sessions is not None and sessions >= max_hold

    if stalled:
        review = ManualReview(
            symbol, sessions,
            f"{symbol} has gone {sessions} trading days since its last fill with "
            "no exit; it stops accepting new buys and waits for manual review "
            "(never force-sold below cost)",
        )
        return ({**row, "state": HOLD_NO_NEW_BUYS}, None, False, review, None)

    if row["state"] == HOLD_NO_NEW_BUYS or not entries_enabled:
        return (row, None, False, None, None)

    skip = _average_down_skip(row, ctx, price)
    if skip is not None:
        return ({**row, "state": HOLDING}, None, False, None, skip)

    return ({**row, "state": AVERAGE_PENDING}, None, True, None, None)


@dataclass
class _Pending:
    """One sized order and the state change that goes with it, held back until
    guard has ruled on the whole plan.

    Two phases rather than one because this engine, unlike `plays`/`sizing`,
    writes persistent state alongside its proposals. `position_updates` is
    applied by `edge_dip_recovery_positions_apply`, so committing a row for an
    order guard then refuses persists a lie about the broker — an
    `ENTRY_PENDING` with no order behind it, which nothing ever clears.
    ``on_deny`` is the row the same state machine would have reached had the
    order never been proposed: `EXIT_LOCKED` for a refused exit (the target is
    still hit, it simply has not been sold), `HOLDING` for a refused
    average-down (exactly what `_manage` returns when averaging is skipped),
    and no row at all for a refused new entry.
    """

    order: Order
    symbol: str
    notional: float
    new_entry: bool
    on_approve: dict | None = None
    on_deny: dict | None = None

    @property
    def key(self) -> tuple[str, str]:
        """(symbol, side), which is unique within one Dip Recovery tick — a
        symbol either exits or averages, never both, and a new entry is only
        ever proposed for a symbol with no row. `guard._PlanState.claimed`
        keys `DUPLICATE_OPEN_ORDER` on the same pair, so guard already treats
        it as a plan-level identity."""
        return (self.order.symbol, self.order.side)


def _guard_verdict(
    pending: Sequence[_Pending], ctx: Mapping[str, Any]
) -> tuple[set[tuple[str, str]], list[dict]]:
    """Run the whole plan past `guard.evaluate` once, not order by order.

    Guard evaluates a *plan*: `_PlanState` accumulates approved notional and
    claimed slots across orders precisely so two buys cannot independently pass
    the same cap. Calling it per order would defeat that and approve a pair
    that together breach §4.4.

    A halt is not a per-order denial — `guard.evaluate` returns empty
    `approved` and empty `denied` and puts the reason in `halts` — so the
    halted case is expanded here into one blocked entry per order. An order
    that vanished without a recorded reason is the failure mode this whole
    module is written against.
    """
    session = (ctx.get("cycle") or {}).get("session") or "regular_hours"
    verdict = guard_evaluate([p.order.as_order(session) for p in pending], ctx)

    if verdict.halted:
        halts = [d.as_dict() for d in verdict.halts]
        return set(), [
            {"order": p.order.as_order(session), "denials": halts} for p in pending
        ]

    approved = {(o.get("symbol"), o.get("side")) for o in verdict.approved}
    return approved, [d.as_dict() for d in verdict.denied]


def evaluate(ctx: Mapping[str, Any]) -> DipRecoveryResult:
    """The whole cycle: fills, then management, then new entries, then guard."""
    positions, closed, claims, releases = _apply_fills(ctx.get("dip_recovery_positions") or {}, ctx)
    entries_enabled = _entries_enabled(ctx)

    result = DipRecoveryResult(closed=closed, symbol_lock_claims=claims, symbol_lock_releases=releases)
    pending: list[_Pending] = []
    active_count = sum(1 for row in positions.values() if row.get("state") in _ACTIVE_STATES)
    ledger = _Ledger(ctx, active_count)
    # No new money into a sector the operator rotated out of. Gates the entry
    # pass and the average-down below; never `_manage`'s exits.
    rotation = universes.sector_rotation(
        (ctx.get("control") or {}).get("active_sectors"), ctx.get("etfs") or {},
        ctx.get("rotation_codes"), ctx.get("sub_industries"),
    )
    sectors = ctx.get("sectors") or {}

    # The day's regime, read through the same functions core DIP reads it
    # through (`regime.dip_entry_verdict`). Enforced under `regime_v2_enabled`;
    # otherwise each verdict is recorded as a shadow row and nothing changes.
    # No regime in the context (a tick that sent no `regime_inputs`) is "no
    # opinion": every function below returns None and the gate is not applied.
    regime = ctx.get("regime") or None
    enforce = regime_v2_enabled(ctx)
    hold_cash = regimes.no_entry_reason(regime)

    for symbol, row in list(positions.items()):
        if row.get("state") not in _MANAGED_STATES:
            continue

        new_row, order, wants_average, review, skip = _manage(row, ctx, entries_enabled)
        result.position_updates[symbol] = new_row

        if order is not None:
            pending.append(
                _Pending(
                    order, symbol, order.quantity * order.limit_price, new_entry=False,
                    on_approve=new_row, on_deny={**new_row, "state": EXIT_LOCKED},
                )
            )
        elif wants_average and (sector := rotation.inactive(symbol, sectors)) is not None:
            result.skipped.append(Skipped(symbol, SECTOR_INACTIVE,
                f"{sector} is outside the active sector rotation; {symbol} is "
                "held and still exits normally, but is not averaged down"))
            result.position_updates[symbol] = {**new_row, "state": HOLDING}
        elif wants_average and enforce and hold_cash is not None:
            # NO_NEW_POSITIONS / HOLD_CASH is "manage exits only": no new money,
            # which an average-down is. The row keeps being managed and exits
            # normally. The sector and strength rules are entry rules and are
            # not applied to a position already held.
            result.skipped.append(Skipped(symbol, SKIP_REGIME,
                f"{hold_cash}: {symbol} is held and still exits normally, but "
                "is not averaged down"))
            result.position_updates[symbol] = {**new_row, "state": HOLDING}
        elif wants_average:
            if hold_cash is not None:
                result.shadow_would_skip.append(
                    {"symbol": symbol, "code": SKIP_REGIME, "action": "average_down",
                     "message": f"{hold_cash}: {symbol} would not be averaged down"}
                )
            sized = _size_buy(symbol, ctx, ledger, ledger.max_active - ledger.active_count)
            if isinstance(sized, Order):
                notional = sized.quantity * sized.limit_price
                pending.append(
                    _Pending(
                        sized, symbol, notional, new_entry=False,
                        on_approve=new_row, on_deny={**new_row, "state": HOLDING},
                    )
                )
                ledger.spend(notional, new_entry=False)
            else:
                result.skipped.append(sized)
                result.position_updates[symbol] = {**new_row, "state": HOLDING}

        if review is not None:
            result.manual_review.append(review)
        if skip is not None:
            result.skipped.append(skip)

    for symbol in ctx.get("dip_recovery_watchlist") or []:
        if symbol in positions:
            result.skipped.append(Skipped(symbol, ALREADY_TRACKED, f"{symbol} already has an open Dip Recovery row"))
            continue

        owner = (ctx.get("symbol_lock") or {}).get(symbol)
        if owner is not None and owner != STRATEGY:
            result.skipped.append(Skipped(symbol, SYMBOL_LOCKED, f"{symbol} is locked to '{owner}'"))
            continue

        # Catches a rotation made after Universe Pick persisted this list —
        # skipped before `entry_signal`, so it writes no evaluations row.
        sector = rotation.inactive(symbol, sectors)
        if sector is not None:
            result.skipped.append(Skipped(symbol, SECTOR_INACTIVE,
                f"{sector} is outside the active sector rotation"))
            continue

        if not entries_enabled:
            result.skipped.append(Skipped(symbol, ENTRIES_DISABLED, "dip_recovery_enabled is off"))
            continue

        if not ledger.has_room_for_new_entry():
            code = MAX_ACTIVE_POSITIONS if ledger.active_count >= ledger.max_active else MAX_NEW_ENTRIES_PER_DAY
            result.skipped.append(Skipped(symbol, code, f"{symbol}: no room this cycle ({code})"))
            continue

        # Before `entry_signal`, like the rotation above: a name the regime
        # refuses is not evaluated and writes no evaluations row.
        verdict = regimes.dip_entry_verdict(
            regime, regimes.sector_and_etf(symbol, sectors, ctx.get("etfs")),
            weakest_in_reduced=True,
        )
        if enforce and verdict is not None:
            result.skipped.append(Skipped(symbol, verdict[0], f"{symbol}: {verdict[1]}"))
            continue

        outcome = entry_signal(symbol, ctx)
        result.evaluations.append(
            {
                "trading_day": str(ctx["cycle"]["trading_day"]),
                "symbol": symbol,
                "score": outcome.score if isinstance(outcome, EntryProposal) else None,
                "passed": isinstance(outcome, EntryProposal),
                "factors": outcome.factors if isinstance(outcome, EntryProposal) else {},
                "rejection_reasons": [] if isinstance(outcome, EntryProposal) else [outcome.message],
            }
        )

        if isinstance(outcome, Skipped):
            result.skipped.append(outcome)
            continue

        multiplier = regimes.entry_size_multiplier(regime)
        if not enforce and verdict is not None:
            result.shadow_would_skip.append(
                {"symbol": symbol, "code": verdict[0], "action": "entry",
                 "message": f"{symbol}: {verdict[1]}"}
            )
        elif not enforce and multiplier != 1.0:
            result.shadow_would_resize.append(
                {"symbol": symbol, "size_multiplier": multiplier}
            )

        sized = _size_buy(
            symbol, ctx, ledger, ledger.max_active - ledger.active_count,
            scale=multiplier if enforce else 1.0,
        )
        if isinstance(sized, Order):
            notional = sized.quantity * sized.limit_price
            pending.append(
                _Pending(
                    sized, symbol, notional, new_entry=True,
                    on_approve={**_new_row(symbol), "state": ENTRY_PENDING},
                    on_deny=None,
                )
            )
            ledger.spend(notional, new_entry=True)
        else:
            result.skipped.append(sized)

    # Phase two. Everything above proposed; nothing above is committed until
    # guard has ruled on the plan as a whole.
    approved, result.blocked = _guard_verdict(pending, ctx)

    for proposal in pending:
        if proposal.key in approved:
            result.orders.append(proposal.order)
            if proposal.on_approve is not None:
                result.position_updates[proposal.symbol] = proposal.on_approve
            continue

        ledger.refund(proposal.notional, new_entry=proposal.new_entry)
        if proposal.on_deny is not None:
            result.position_updates[proposal.symbol] = proposal.on_deny
        else:
            result.position_updates.pop(proposal.symbol, None)

    result.control_updates.update(ledger.control_update())
    return result
