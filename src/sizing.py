"""sizing.py — the capital ladder: count, notional, path selection.

Design v3 §4 and §11. The system must work at $236 and at $10,000 with no code
change, so every structural number is derived from current equity and current
per-symbol broker flags at each cycle (Invariant 5). There is no "small account
mode" to switch on.

The module also owns the §4.4 concentration fractions and the §2.4 settled-cash
basis, because both are ladder arithmetic that `guard.py` merely enforces.
Guard imports them from here so each is defined once, and the dependency runs
one way — sizing never imports guard.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

WHOLE = "WHOLE"
FRACTIONAL = "FRACTIONAL"

REGULAR_HOURS = "regular_hours"
EXTENDED_HOURS = "extended_hours"

# §4.3. Overnight is a play decision (§6.3), never sizing's, so it is absent
# from both tuples.
WHOLE_SESSIONS = (REGULAR_HOURS, EXTENDED_HOURS)
FRACTIONAL_SESSIONS = (REGULAR_HOURS,)

# Where Robinhood's share precision stops. Quantities are floored to it:
# rounding up would spend more than the cycle budgeted.
FRACTIONAL_PRECISION = 6

SETTLED_CASH_UNKNOWN = "SETTLED_CASH_UNKNOWN"
NO_PRICE = "NO_PRICE"
STAGED_ORDER_NEEDS_LIMIT = "STAGED_ORDER_NEEDS_LIMIT"
STAGED_FRACTIONAL_MARKET = "STAGED_FRACTIONAL_MARKET"
WHOLE_SHARE_ONLY = "WHOLE_SHARE_ONLY"
PATH_SESSION_INELIGIBLE = "PATH_SESSION_INELIGIBLE"
NO_OPEN_SLOTS = "NO_OPEN_SLOTS"
SECTOR_UNKNOWN = "SECTOR_UNKNOWN"
NO_CAP_HEADROOM = "NO_CAP_HEADROOM"
DEPLOYABLE_EXHAUSTED = "DEPLOYABLE_EXHAUSTED"
BELOW_MIN_NOTIONAL = "BELOW_MIN_NOTIONAL"
FULL_SEND_ACTIVE = "FULL_SEND_ACTIVE"

# §17's SURGE. At most this many names share the whole available cash on a
# full-send cycle — the operator's "1 or 2 or 3". Mirrors
# `plays.core.SURGE_MAX_NAMES`; kept here too so sizing has no import into
# plays.
SURGE_MAX_NAMES = 3

# Below three, §4.4's caps stop being satisfiable. Structural, and unchanged.
MIN_POSITIONS = 3

# What replaced `MAX_POSITIONS = 12` (operator: "if you have to go up to 20 or
# 25, go for it, as long as it is going to deliver gains").
#
# **A count ceiling is a weight floor wearing the wrong units**, and that is why
# the old constant was worth removing rather than raising. Above $960 the clamp
# bound, so `n` stopped tracking equity and every name settled at exactly
# equity/12 — 8.3% each, at $1,000 and at $1,000,000 alike. That is a rule about
# how thin a position may get, expressed as a number of slots, and the two only
# coincide because `n` is what the ladder happens to divide by.
#
# Saying it as a weight makes it hold at any equity and makes the operator's
# number legible: at 4% no position may be thinner than a twenty-fifth of the
# account, so the book tops out at 25 names. It now binds from $2,000 rather
# than $960, which is the whole of the change the operator asked for.
#
# Note the direction. This is a floor under each name's *size*, and therefore a
# ceiling on the *count* — it is not `position_cap_fraction`, which is the hard
# ceiling on one name and floors at 15%. Concentration and dilution are two
# different failures and they need two different numbers.
MIN_POSITION_WEIGHT = 0.04

# §2.4. The conservative policy bound until probe P3 measured whether
# buying_power excludes unsettled proceeds. It does, so the live
# `config/config.json` now runs `buying_power_is_settled`. Conservative stays
# reachable and tested: it is what to set if this ever stops being a cash
# account, because both its rules assume proceeds that cannot be spent.
POLICY_CONSERVATIVE = "conservative"
POLICY_BUYING_POWER_IS_SETTLED = "buying_power_is_settled"
DEFAULT_SETTLED_CASH_POLICY = POLICY_CONSERVATIVE

# --- volatility-aware sizing, off by default ------------
#
# ``notional = min(slot notional, risk_per_trade_pct x equity / stop_distance_pct)``
#
# The even split asks "how many names does this account hold"; the risk budget
# asks "how much is this account willing to lose if this one is wrong". They
# answer different questions and the tighter of the two wins, which is the
# whole of the rule: a $12 stock with a 9% stop and a $400 stock with a 2% stop
# get the same *dollar* exposure to being wrong rather than the same slot.
#
# The stop distance is the play's own — `plays.core.stop_distance`, the same
# resolver `exits` and `protection` use — so a `SECTOR` position sized against
# its 1.5xATR stop is not later exited on a 2xATR one.
#
# **Off by default.** `risk_sizing_enabled` ships false, so every existing golden is
# byte-identical and the live ladder is untouched. The shadow ledger runs it as part
# of the `A_dip_pullback` variant.
RISK_SIZING_ENABLED = "risk_sizing_enabled"
RISK_PER_TRADE_PCT = "risk_per_trade_pct"
RISK_PER_TRADE_PCT_DEFAULT = 0.01

RISK_BUDGET = "RISK_BUDGET"

# --- portfolio construction and posture sizing (P3), off by default ---------
#
# The ladder above asks how big one position is; nothing asked what the *set*
# of new positions looks like, and the answer was four buys in one cycle — two
# REITs, a regional bank and a defence name — three of them the same rate bet
# in three different sector buckets. Under `sizing_v2_enabled`:
#
# * **one_per_sector_day** — at most one *new* name per sector per trading
#   day, counting names already opened today and buys still resting.
# * **re-entry block** — a name closed at a loss inside
#   `reconcile.reentry_window_days` is not sized at all. `guard` has always
#   denied it afterwards; refusing here gives the slot to the next candidate.
# * **share limit** — a whole-share order is capped at the shares that fit
#   under the position cap, and a name whose single share does not fit says so.
# * **correlated with a holding** — a new name sharing a GICS sub-industry or
#   a sector with something already held gets `corr_holdings_budget_fraction`
#   (half) of its slot. A proxy: no return correlation is computed, because no
#   daily closes for held and candidate names are in the payload.
# * **posture** — the slot is multiplied by the regime's `size_multiplier`
#   (unless `plays.entries` already did, under `regime_v2_enabled`) and total
#   new deployment is capped so invested stays at or under
#   `target_invested_pct` of equity.
#
# A top-up of a name already held is a second tranche, not a new position:
# the sector-day, correlation and posture-multiplier rules do not apply to it.
# The deployment cap does — it bounds *all* new money.
#
# **Off is off.** Every branch below is behind `v2_enabled`, the new fields
# are absent from `as_dict()` while it is off, and every golden is
# byte-identical.
SIZING_V2_ENABLED = "sizing_v2_enabled"
ONE_PER_SECTOR_DAY = "one_per_sector_day"
CORR_HOLDINGS_BUDGET_FRACTION = "corr_holdings_budget_fraction"
CORR_HOLDINGS_BUDGET_FRACTION_DEFAULT = 0.5

ONE_PER_SECTOR_DAY_REACHED = "ONE_PER_SECTOR_DAY"
REENTRY_BLOCKED = "REENTRY_BLOCKED"
SHARE_LIMIT = "SHARE_LIMIT"
TARGET_INVESTED_REACHED = "TARGET_INVESTED_REACHED"
POSTURE_NO_ENTRY = "POSTURE_NO_ENTRY"

# The adjustments a sized entry records when one of the rules above cut it.
ADJ_POSTURE = "posture_size_multiplier"
ADJ_CORRELATED = "correlated_with_holding"
ADJ_TARGET_INVESTED = "target_invested_cap"
ADJ_SHARE_LIMIT = "share_limit"

_TRADE_NORMALLY = "TRADE_NORMALLY"


def v2_enabled(ctx: Mapping[str, Any]) -> bool:
    return bool((ctx.get("config") or {}).get(SIZING_V2_ENABLED))


def _regime_number(ctx: Mapping[str, Any], key: str) -> float | None:
    value = (ctx.get("regime") or {}).get(key)
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def posture_multiplier(ctx: Mapping[str, Any]) -> float:
    """What `sizing` multiplies a new entry's slot by for the day's posture.

    1.0 when the switch is off, when there is no regime, and when
    `regime_v2_enabled` is on — `plays.entries` has then already folded the
    posture into each candidate's `notional_multiplier`, and applying it again
    here would halve a half.
    """
    if not v2_enabled(ctx) or bool((ctx.get("config") or {}).get("regime_v2_enabled")):
        return 1.0
    value = _regime_number(ctx, "size_multiplier")
    return 1.0 if value is None else max(0.0, min(1.0, value))


def invested_value(ctx: Mapping[str, Any]) -> float:
    """What is already committed: the book at market, plus buys still resting.

    A resting buy is counted at its limit because it will become exposure the
    moment it fills, and a cycle that ignored it could deploy the same room
    twice before the first fill landed.
    """
    total = sum(float(p.get("market_value") or 0.0) for p in ctx.get("positions") or [])
    for order in ctx.get("open_orders") or []:
        if not isinstance(order, Mapping) or order.get("side") != "buy":
            continue
        try:
            quantity = float(order.get("quantity") or 0.0)
            price = float(order.get("limit_price") or order.get("price") or 0.0)
        except (TypeError, ValueError):
            continue
        total += max(0.0, quantity * price)
    return total


def deployment_room(ctx: Mapping[str, Any]) -> float | None:
    """New money the posture's `target_invested_pct` still allows, or None
    when no target applies (switch off, no regime, unreadable equity)."""
    if not v2_enabled(ctx):
        return None
    target = _regime_number(ctx, "target_invested_pct")
    equity = raw_equity(ctx.get("account") or {})
    if target is None or equity is None:
        return None
    return max(0.0, equity * max(0.0, min(1.0, target)) - invested_value(ctx))


def risk_budget(
    cand: Mapping[str, Any], ctx: Mapping[str, Any], price: float
) -> float | None:
    """What the risk budget allows this candidate, or None when it does not bind.

    None on four counts, and each is the safe direction:

    * the switch is off, which is every cycle the live account runs today;
    * no readable equity, in which case `guard.halts` is already denying the
      order and a budget derived from a missing number would be an invention;
    * no ATR for the symbol, so there is no stop distance to divide by — the
      order still sizes on the even split, exactly as it did before this
      existed, and `_marketable_limit` already treats a missing ATR the same
      way;
    * a non-positive price, which `_size_one`'s own `NO_PRICE` check owns.

    Never *raises* a size. The caller takes ``min`` of this and the slot, so a
    wide-stop name is cut and a tight-stop name is left on the ladder's own
    number rather than levered up to the budget.
    """
    config = ctx.get("config") or {}
    if not bool(config.get(RISK_SIZING_ENABLED)):
        return None

    # Imported here rather than at module scope because `plays.exits` imports
    # this module (`drawdown_breached`, `drawdown_floor`,
    # `flow_adjusted_equity`) and a top-level import back into `plays` closes
    # the loop — `src.sizing` first would then leave `plays.exits` reading a
    # half-built `sizing`. The dependency this module's own docstring states
    # runs one way, and this keeps it that way at import time.
    from src.plays.core import stop_distance

    if price <= 0:
        return None

    equity = ((ctx.get("account") or {}).get("equity_total"))
    try:
        equity = float(equity)
    except (TypeError, ValueError):
        return None
    if equity <= 0.0:
        return None

    distance = stop_distance(cand.get("play_id"), _atr(ctx, cand.get("symbol")), ctx)
    if distance is None:
        return None

    stop_distance_pct = distance / price
    if stop_distance_pct <= 0.0:
        return None

    return float(config.get(RISK_PER_TRADE_PCT, RISK_PER_TRADE_PCT_DEFAULT)) * equity / stop_distance_pct


# Money is compared to the cent. Half a cent of slack keeps binary
# floating-point noise from reading as a breach: 0.8 * 295.525 is
# 236.42000000000002, and an exact `<` would halt the system on its own
# high-water mark.
MONEY_EPS = 0.005


def meaningfully_less(a: float, b: float) -> bool:
    """``a < b``, ignoring sub-cent float noise."""
    return a < b - MONEY_EPS


def _half_up(value: float) -> int:
    """Round half away from zero, for the non-negative values §4 produces.

    Python's built-in is banker's rounding: ``round(8.5)`` is 8 and
    ``round(9.5)`` is 10, so the slot count would depend on the parity of a
    number nobody chose. Equity is never negative here.
    """
    return math.floor(value + 0.5)


def n_target(equity_total: float, config) -> int:
    """How many positions this equity supports (§4.1).

    Fewer positions at low capital is the safe choice, not the risky one: three
    positions of $79 can hold whole shares, which unlocks limit orders,
    extended-hours eligibility and resting stops. Concentration buys capability.

    The ceiling is `MIN_POSITION_WEIGHT` expressed as a count: 25 names at 4%
    apiece, up from the flat 12 it replaced. `min_position_size` remains the
    operator's control over the shape of the book below that, and it is config —
    raising it asks for fewer, larger positions without touching code.
    """
    minimum = float(config["min_position_size"])
    raw = _half_up(float(equity_total) / minimum)
    ceiling = math.floor(1.0 / MIN_POSITION_WEIGHT)
    return max(MIN_POSITIONS, min(ceiling, raw))


def cash_reserve(equity_total: float, config) -> float:
    """Cash held back from every cycle (§4.2)."""
    floor = float(config["cash_reserve_floor"])
    pct = float(config["cash_reserve_pct"])
    return max(floor, pct * float(equity_total))


def settled_basis(account, config) -> float | None:
    """Cash that Invariant 1 will let a buy draw on, before the reserve.

    Returns ``None`` when it cannot be established — the signal that no buy may
    be sized at all. Zero would be wrong: zero is a legitimate measured balance
    and an absent figure is not.
    """
    policy = config.get("settled_cash_policy", DEFAULT_SETTLED_CASH_POLICY)
    buying_power = account.get("buying_power")
    settled = account.get("settled_cash")

    if policy == POLICY_BUYING_POWER_IS_SETTLED:
        # P3 measured one thing and it is worth being precise about which:
        # **sale proceeds** do not inflate `buying_power` (and
        # `get_accounts.unsettled_funds` exposes the split directly). It
        # measured nothing about **deposits**, where the broker credits
        # spendable buying power ahead of the ACH on purpose — so a figure
        # that is authoritative the day after a sale is not automatically
        # authoritative the hour after a transfer.
        #
        # So `settled_cash` stops being *required* and does not stop being
        # *binding*: absent, `buying_power` is trusted alone, which is the
        # relaxation §2.4 promises; present and lower, it still wins. Under
        # P3's answer the two are equal in the ordinary case and this costs
        # nothing, and on the one day they diverge it is the difference
        # between a funded buy and a Good Faith Violation.
        if buying_power is None:
            return None
        if settled is None:
            return float(buying_power)
        return min(float(buying_power), float(settled))

    if settled is None or buying_power is None:
        return None

    return min(float(buying_power), float(settled))


def deployable(account, equity_total: float, config) -> float | None:
    """What this cycle may actually spend (§4.2).

    Guard deliberately does not subtract the reserve: the reserve is sizing
    policy and settled cash is legality, and a guard that enforced policy would
    deny orders that are perfectly legal.
    """
    basis = settled_basis(account, config)
    if basis is None:
        return None
    return max(0.0, basis - cash_reserve(equity_total, config))


def open_slots(target: int, held_symbols) -> int:
    """Slots a new name could occupy. Never negative.

    Ten inherited positions against a target of three is the account's day-one
    state (Appendix A.1), not a hypothetical.
    """
    return max(0, int(target) - len(set(held_symbols)))


def position_cap_fraction(n: int) -> float:
    """Max share of equity in one position: 40% at n=3, 15% at n>=8 (§4.4)."""
    return max(0.15, 1.2 / max(1, int(n)))


def sector_cap_fraction(n: int) -> float:
    """Max share of equity in one sector: 50% at n=3, 30% at n>=5 (§4.4)."""
    return max(0.30, 1.5 / max(1, int(n)))


def anchored_mark(account, high_water_mark):
    """§10's peak as the deposit record reads **today**, or the stored figure
    when the mark has no anchor.

    The mark lives in flow-adjusted space (equity less net deposits), which
    makes it only as right as the deposit record was on the day it was set.

    So the mark is anchored to what cannot be restated — the account's raw
    equity on a day — and re-derived each cycle:
    ``equity then - net deposits as recorded now for that day``. A deposit
    back-dated before the anchor lowers the mark by exactly its amount, in the
    same cycle that it lowers flow-adjusted equity. `cli._account` joins the
    two anchor figures (from `edge_control`, or from `config/inception.json`'s
    one-time `high_water_mark_anchor` until `reconcile` has written them);
    absent either, the stored mark stands, which is every test and every
    account that has never had its record corrected.
    """
    equity_then = (account or {}).get("high_water_mark_equity")
    deposits_then = (account or {}).get("net_deposits_at_mark")
    if equity_then is None or deposits_then is None:
        return high_water_mark
    return round(float(equity_then) - float(deposits_then), 6)


def drawdown_floor(high_water_mark: float, config, account=None) -> float:
    """Flow-adjusted equity below which §10's drawdown halt binds: 20% of the
    account off the peak.

    Here rather than in `guard.py` because two modules read it: guard denies
    buys beneath it, and plays emits the §6.5 exit-2 flatten that takes the
    book down. Two arithmetics would let plays flatten at a floor guard still
    considered safe.

    **20% of the account, not 20% of the gain**. The mark and the equity it is
    compared with both have net deposits taken out, so a flow moves neither.
    But the *size* of a 20% fall used to be taken from the mark alone — and on
    an account funded mostly after inception the mark is a fraction of the
    money at work. This one began at $248 and holds $1,100 of deposits: a $50
    fall from its peak is 3.7% of the account and read as 20.0% of the mark.
    The fall is therefore measured against what the account would hold at its
    peak with today's deposits in it, ``mark + net deposits``:

        floor = mark - pct * (mark + net_deposits)

    With no deposits that is the old ``mark * (1 - pct)`` exactly, so an
    unfunded account and every caller that passes no ``account`` are unchanged.
    A withdrawal is a negative deposit and shrinks the base the same way.
    """
    pct = float(config["drawdown_halt_pct"])
    mark = float(anchored_mark(account, high_water_mark))
    base = max(0.0, mark + float((account or {}).get("net_deposits") or 0.0))
    return mark - pct * base


def raw_equity(account) -> float | None:
    """``equity_total`` as a usable positive number, or ``None`` when the read
    did not produce one.

    Missing, ``null``, zero, negative and non-numeric all collapse to ``None``:
    an account under agentic control always holds *some* equity, so a zero or
    unparseable figure is a failed read, not a balance. An earlier incident was
    exactly this — an absent ``equity_total`` became a large negative
    flow-adjusted figure and `drawdown_breached` read it as a 20% breach,
    flattening the whole book. An unreadable balance is
    `guard.halts.ACCOUNT_UNREADABLE`, never a breach.
    """
    value = account.get("equity_total")
    if value is None:
        return None
    try:
        equity = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(equity) or equity <= 0.0:
        return None
    return equity


def account_implausible(
    account: Mapping[str, Any],
    positions: Sequence[Mapping[str, Any]] | None,
    prior_benchmark: Mapping[str, Any] | None = None,
) -> bool:
    """Whether ``equity_total`` should be trusted at all, independent of §10.

    Two cheap cross-checks, either one enough: a cash account's equity can
    never be less than the market value of the positions sitting inside it —
    that would require negative cash — and it should not collapse to under
    half of yesterday's recorded figure in one read. Both are what an earlier
    incident violated: `equity_total` came back 669.53 while the account held
    more than that in open positions alone, and `guard.halts` had no way to
    tell that apart from a real balance.

    ``None`` on `raw_equity` is a different failure (`ACCOUNT_UNREADABLE`,
    evaluated ahead of this) and reads as plausible here — a missing figure is
    not a wrong one, and duplicating that halt under a second name would only
    make the two race to explain the same cycle.

    ``prior_benchmark`` absent or unreadable skips its half of the check
    silently, the same way `report._integrity_alert` treats a missing prior
    row: the first benchmark an account ever writes has nothing to compare
    against yet.
    """
    equity = raw_equity(account)
    if equity is None:
        return False

    position_total = sum(
        float(p.get("market_value") or 0.0) for p in (positions or [])
    )
    if meaningfully_less(equity, position_total):
        return True

    prior_equity = (prior_benchmark or {}).get("equity")
    if prior_equity is not None:
        try:
            prior_equity = float(prior_equity)
        except (TypeError, ValueError):
            prior_equity = None
    if prior_equity is not None and prior_equity > 0 and meaningfully_less(
        equity, 0.5 * prior_equity
    ):
        return True

    return False


def flow_adjusted_equity(account) -> float | None:
    """Equity with the operator's own money taken back out (§10).

    §10's halt asks one question — *has the strategy lost 20%* — and raw equity
    answers a different one, because equity moves when money is added or taken
    out and neither is a result. Subtracting cumulative net deposits leaves the
    part of the account the agent is actually responsible for.

    A withdrawal lowers equity and lowers the net-deposit total by the same
    amount, so this figure does not move: taking $50 out of $236 used to leave
    $186 against a $189 floor and flatten the entire book with nothing having
    gone wrong. A deposit is the mirror image and is why the round trip was
    worse than the withdrawal alone — the mark ratcheted to the funded equity
    and never came back down.

    ``net_deposits`` absent reads as 0.0, which is exactly the old behaviour:
    an account that has seen no flows is unaffected by this at all.

    Returns ``None`` when `raw_equity` cannot establish a balance. Every caller
    treats that as an account halt, not as a number — a bad read must never
    reach §10's comparison.
    """
    equity = raw_equity(account)
    if equity is None:
        return None
    # Rounded because this figure is *stored* as the high-water mark and then
    # compared against itself for the life of the account. 336.42 - 100.00 is
    # 236.42000000000002 in binary floating point, and a mark carrying that
    # tail would never equal the equity it came from.
    return round(equity - float(account.get("net_deposits") or 0.0), 6)


def drawdown_breached(account, high_water_mark: float, config) -> bool:
    """Whether §10's halt binds. Sub-cent noise is not a breach.

    Takes the account rather than a bare equity figure so the flow adjustment
    cannot be forgotten at one of the four call sites. Passing raw equity used
    to be possible and correct-looking, and the failure it produced was a
    liquidation.

    An unreadable equity (`flow_adjusted_equity` is ``None``) is **not** a
    breach: it is `guard.halts.ACCOUNT_UNREADABLE`, evaluated ahead of every
    order. Returning ``False`` here is what keeps a missing account read from
    flattening the book at each of the four call sites.
    """
    adjusted = flow_adjusted_equity(account)
    if adjusted is None:
        return False
    return meaningfully_less(adjusted, drawdown_floor(high_water_mark, config, account))


def select_path(price: float, notional: float) -> tuple[str, float]:
    """Which execution path ``notional`` buys at ``price`` (§4.3).

    §4.3 asks for ``price <= notional and whole_shares >= 1``; the second
    clause is implied by the first, since a price inside the notional can
    never floor to zero shares.
    """
    if price <= notional:
        return WHOLE, math.floor(notional / price)

    scale = 10**FRACTIONAL_PRECISION
    return FRACTIONAL, math.floor(notional / price * scale) / scale


@dataclass(frozen=True)
class Ladder:
    """The rung this cycle sized at. Reported so the runlog records why."""

    equity_total: float
    n_target: int
    open_slots: int
    cash_reserve: float
    deployable: float | None
    notional: float | None
    position_cap: float
    sector_cap: float

    def as_dict(self) -> dict:
        return {
            "equity_total": self.equity_total,
            "n_target": self.n_target,
            "open_slots": self.open_slots,
            "cash_reserve": self.cash_reserve,
            "deployable": self.deployable,
            "notional": self.notional,
            "position_cap": self.position_cap,
            "sector_cap": self.sector_cap,
        }


@dataclass(frozen=True)
class SizedEntry:
    symbol: str
    play_id: str | None
    path: str
    quantity: float
    price: float
    price_source: str
    limit_price: float | None
    notional: float
    order_type: str
    session: str
    sessions_eligible: tuple[str, ...]
    resting_stop_eligible: bool
    conviction: float
    rank_bonus: float
    rank_score: float
    notional_multiplier: float
    # §17's SURGE. Carried onto the order so `guard.concentration` skips §4.4's
    # caps for it. Default false — every other path is untouched.
    full_deployment: bool = False
    # P3: which `sizing_v2` rules cut this entry below its ladder slot. Empty
    # (and absent from the dict) whenever the switch is off.
    adjustments: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        out = {
            "symbol": self.symbol,
            "play_id": self.play_id,
            "path": self.path,
            "quantity": self.quantity,
            "price": self.price,
            "price_source": self.price_source,
            "limit_price": self.limit_price,
            "notional": self.notional,
            "order_type": self.order_type,
            "session": self.session,
            "sessions_eligible": list(self.sessions_eligible),
            "resting_stop_eligible": self.resting_stop_eligible,
            "conviction": self.conviction,
            "rank_bonus": self.rank_bonus,
            "rank_score": self.rank_score,
            "notional_multiplier": self.notional_multiplier,
            "full_deployment": self.full_deployment,
        }
        if self.adjustments:
            out["adjustments"] = list(self.adjustments)
        return out


@dataclass(frozen=True)
class UnsizedEntry:
    """A candidate sizing declined to turn into an order.

    Not a denial — guard owns those. This is sizing saying it has no legal
    order to offer, which is a different and earlier thing.
    """

    symbol: str
    reason: str
    message: str

    def as_dict(self) -> dict:
        return {"symbol": self.symbol, "reason": self.reason, "message": self.message}


@dataclass
class SizingResult:
    ladder: Ladder
    entries: list[SizedEntry] = field(default_factory=list)
    unsized: list[UnsizedEntry] = field(default_factory=list)
    # P3: what `sizing_v2` sized against — posture, multiplier, target and the
    # room it left. None (and absent from the dict) while the switch is off.
    v2: dict | None = None

    def as_dict(self) -> dict:
        out = {
            "ladder": self.ladder.as_dict(),
            "entries": [e.as_dict() for e in self.entries],
            "unsized": [u.as_dict() for u in self.unsized],
        }
        if self.v2 is not None:
            out["v2"] = dict(self.v2)
        return out


def _is_sleeve(symbol: Any, ctx: Mapping[str, Any]) -> bool:
    """`core_sleeve.is_sleeve_symbol`, restated on the two config keys so this
    module keeps importing nothing from the package (that module imports this
    one). `tests/test_core_sleeve.py` pins the two to each other."""
    config = ctx.get("config") or {}
    return bool(config.get("core_sleeve_enabled")) and symbol is not None         and symbol == config.get("core_symbol")


def _ladder(ctx: Mapping[str, Any]) -> Ladder:
    account = ctx.get("account") or {}
    config = ctx["config"]
    equity = float(account.get("equity_total") or 0.0)

    target = n_target(equity, config)
    # The core sleeve takes no play slot (`src/core_sleeve.py`).
    slots = open_slots(target, [p.get("symbol") for p in ctx.get("positions") or []
                                if not _is_sleeve(p.get("symbol"), ctx)])
    spendable = deployable(account, equity, config)

    return Ladder(
        equity_total=equity,
        n_target=target,
        open_slots=slots,
        cash_reserve=cash_reserve(equity, config),
        deployable=spendable,
        notional=None if spendable is None else spendable / max(1, slots),
        position_cap=equity * position_cap_fraction(target),
        sector_cap=equity * sector_cap_fraction(target),
    )


def _ask(ctx: Mapping[str, Any], symbol: str) -> float | None:
    price = ((ctx.get("quotes") or {}).get(symbol) or {}).get("ask")
    if price is None or float(price) <= 0:
        return None
    return float(price)


def _atr(ctx: Mapping[str, Any], symbol: str) -> float | None:
    """ATR(14) off the per-symbol signal, or None when it is absent or unusable."""
    value = ((ctx.get("signals") or {}).get(symbol) or {}).get("atr")
    if value is None:
        return None
    try:
        atr = float(value)
    except (TypeError, ValueError):
        return None
    return atr if atr > 0 else None


def _marketable_limit(
    ask: float | None, atr: float | None, config: Mapping[str, Any]
) -> float | None:
    """The ask marked up by ``entry_limit_offset_atr`` x ATR(14), to the cent (§4.3).

    A bare ask is not a marketable limit — the book moves a penny and the order sits
    unfilled, which is what happened to 3 of 4 entries. The markup is what makes the
    WHOLE path's limit actually cross.

    No ATR (a name whose signal fetch missed) falls back to the plain ask rather
    than refusing the entry: an un-marked limit that might not fill is still a
    better outcome than no order at all. ``ask`` itself absent or non-positive
    is passed straight back so the caller's own NO_PRICE check still fires.
    """
    if ask is None or ask <= 0:
        return ask
    if atr is None:
        return round(ask, 2)
    offset = float(config.get("entry_limit_offset_atr", 0.10))
    return round(ask + offset * atr, 2)


class _Budget:
    """What is left to spend, and how much room each name has left.

    Approvals draw all three down as they are granted, for the same reason
    guard's plan state does: two entries sized independently against one
    sector's headroom breach it together.
    """

    def __init__(self, ctx: Mapping[str, Any], ladder: Ladder) -> None:
        sectors = ctx.get("sectors") or {}
        self.ladder = ladder
        self.remaining = ladder.deployable
        self.position_value: dict[str, float] = {}
        self.sector_value: dict[str, float] = {}
        self.open_symbols: set[str] = set()
        # P3. The names held when the cycle began — what "already held" means
        # to the top-up exemption and the correlation proxy, whatever this
        # cycle goes on to size.
        self.held: set[str] = set()
        # Sectors that already took a new name today: opened this session
        # (`sessions_held == 0`, not merely adopted), or a buy still resting
        # on a name not yet held. This cycle's own entries join as they size.
        self.sectors_entered_today: dict[str, str] = {}
        self.deploy_room = deployment_room(ctx)

        for position in ctx.get("positions") or []:
            symbol = position.get("symbol")
            value = float(position.get("market_value") or 0.0)
            if not _is_sleeve(symbol, ctx):
                self.open_symbols.add(symbol)
            self.held.add(symbol)
            self.position_value[symbol] = self.position_value.get(symbol, 0.0) + value
            sector = sectors.get(symbol)
            if sector is not None:
                self.sector_value[sector] = self.sector_value.get(sector, 0.0) + value
                if position.get("sessions_held") == 0 and not position.get("adopted"):
                    self.sectors_entered_today.setdefault(sector, symbol)

        for order in ctx.get("open_orders") or []:
            if not isinstance(order, Mapping) or order.get("side") != "buy":
                continue
            symbol = order.get("symbol")
            sector = sectors.get(symbol)
            if symbol not in self.held and sector is not None:
                self.sectors_entered_today.setdefault(sector, symbol)

    def has_slot_for(self, symbol: str) -> bool:
        """A top-up consumes no slot; only a new name does."""
        return (
            symbol in self.open_symbols
            or len(self.open_symbols) < self.ladder.n_target
        )

    def headroom(self, symbol: str, sector: str) -> float:
        return min(
            self.ladder.position_cap - self.position_value.get(symbol, 0.0),
            self.ladder.sector_cap - self.sector_value.get(sector, 0.0),
        )

    def take(self, symbol: str, sector: str, notional: float) -> None:
        self.remaining -= notional
        if symbol not in self.held:
            self.sectors_entered_today.setdefault(sector, symbol)
        if self.deploy_room is not None:
            self.deploy_room = max(0.0, self.deploy_room - notional)
        self.open_symbols.add(symbol)
        self.position_value[symbol] = self.position_value.get(symbol, 0.0) + notional
        self.sector_value[sector] = self.sector_value.get(sector, 0.0) + notional


def _conviction(cand: Mapping[str, Any]) -> float:
    """plays.py owns conviction; sizing must not break when one omits it.

    What arrives here is §6.0's arbitrated score less the whole-share bonus —
    conviction already multiplied by ``regime_fit`` and net of
    ``crowding_penalty``. Sizing adds the one term plays cannot know, because
    only the ladder knows what a rung is worth.
    """
    value = cand.get("conviction")
    return 0.0 if value is None else float(value)


def _holding_days(cand: Mapping[str, Any]) -> float:
    """§6.0's tiebreak, in days. Absent sorts last, deliberately.

    Treating an undeclared period as zero would let a play that forgot the
    field jump every play that supplied one.
    """
    value = cand.get("expected_holding_days")
    return math.inf if value is None else float(value)


def _crowding(cand: Mapping[str, Any]) -> float:
    """§6.0's crowding term, which is a tiebreak here and nothing more.

    It stopped being subtracted from the score (see `plays.entries._arbitrate`)
    and became a sort term instead. This function exists so `_rank` sorts on
    the same term `plays._rank_key` does: the two keys agreeing by construction
    is what keeps the runlog's stated order and the order capital is committed
    in from drifting apart, and adding a term to one of them alone is exactly
    how that guarantee would be lost.
    """
    value = cand.get("crowding_penalty")
    return 0.0 if value is None else float(value)


def _multiplier(cand: Mapping[str, Any]) -> float:
    """§6.3's 0.75x, and the only sizing input a play controls.

    Everything else on the ladder is a function of equity and broker flags
    (Invariant 5); a play that could widen its own cap could argue past §4.4.
    This one can only ever shrink a position that the ladder had already
    approved, which is why it is safe to hand over.
    """
    value = cand.get("notional_multiplier")
    return 1.0 if value is None else float(value)


def _rank(
    candidates: Sequence[Mapping[str, Any]], ctx: Mapping[str, Any], notional: float
) -> list[tuple[Mapping[str, Any], float, float]]:
    """Order candidates best-first, with the §4.3 whole-share preference.

    The bonus is decided on the path the *cap-unclamped* notional implies,
    because the walk that clamps has to know its own order before it starts.
    The play's own multiplier is applied first: unlike headroom it is known
    before the walk, and a 0.75x candidate that can no longer hold a whole
    share has not earned the bonus. It is a tiebreaker on play quality, never
    a substitute for it.
    """
    bonus_value = float((ctx["config"]).get("whole_share_bonus", 0.0))
    scored: list[tuple[Mapping[str, Any], float, float]] = []

    for cand in candidates:
        limit = cand.get("limit_price")
        price = float(limit) if limit is not None else _ask(ctx, cand.get("symbol"))
        priced = price is not None and price > 0
        whole = priced and select_path(price, notional * _multiplier(cand))[0] == WHOLE
        bonus = bonus_value if whole else 0.0
        scored.append((cand, bonus, _conviction(cand) + bonus))

    # §6.0 breaks a tie first toward the less crowded sector, then toward the
    # shorter holding period, because faster capital turnover matters more than
    # expected edge when only three slots exist. Symbol breaks what is still
    # tied, so golden files do not depend on dict order.
    scored.sort(
        key=lambda row: (
            -row[2],
            _crowding(row[0]),
            _holding_days(row[0]),
            row[0].get("symbol") or "",
        )
    )
    return scored


def _size_one(
    cand: Mapping[str, Any],
    ctx: Mapping[str, Any],
    budget: _Budget,
    bonus: float,
    score: float,
) -> SizedEntry | UnsizedEntry:
    symbol = cand.get("symbol")
    floor = float(ctx["config"]["min_order_notional"])

    if not budget.has_slot_for(symbol):
        return UnsizedEntry(
            symbol,
            NO_OPEN_SLOTS,
            f"{len(budget.open_symbols)} of {budget.ladder.n_target} slots are "
            f"held; {symbol} would be the n+1-th position",
        )

    sector = (ctx.get("sectors") or {}).get(symbol)
    if sector is None:
        return UnsizedEntry(
            symbol,
            SECTOR_UNKNOWN,
            f"no sector known for {symbol}, so the sector cap cannot be sized "
            "against; guard would fail it closed anyway",
        )

    # --- P3, behind `sizing_v2_enabled` ---------------------------------
    v2 = v2_enabled(ctx)
    new_name = symbol not in budget.held
    adjustments: list[str] = []
    slot_scale = 1.0

    if v2:
        refusal = _v2_refusal(symbol, sector, new_name, ctx, budget, floor)
        if refusal is not None:
            return refusal
        if new_name:
            scale = posture_multiplier(ctx)
            if scale < 1.0:
                slot_scale *= scale
                adjustments.append(ADJ_POSTURE)
            if _correlated_holding(symbol, sector, ctx, budget) is not None:
                slot_scale *= float(
                    ctx["config"].get(
                        CORR_HOLDINGS_BUDGET_FRACTION, CORR_HOLDINGS_BUDGET_FRACTION_DEFAULT
                    )
                )
                adjustments.append(ADJ_CORRELATED)

    headroom = budget.headroom(symbol, sector)
    if headroom < floor:
        return UnsizedEntry(
            symbol,
            NO_CAP_HEADROOM,
            f"{symbol} has {headroom:.2f} of room under its position and "
            f"{sector} caps, below the {floor:.2f} broker floor",
        )

    if budget.remaining < floor:
        return UnsizedEntry(
            symbol,
            DEPLOYABLE_EXHAUSTED,
            f"{budget.remaining:.2f} left to spend this cycle, below the "
            f"{floor:.2f} broker floor",
        )

    # The even split is the starting point; the play's own multiplier, the caps
    # and the cash are what it actually has to fit inside.
    multiplier = _multiplier(cand)
    notional = min(
        budget.ladder.notional * multiplier * slot_scale, headroom, budget.remaining
    )
    if v2 and budget.deploy_room is not None and budget.deploy_room < notional:
        notional = budget.deploy_room
        adjustments.append(ADJ_TARGET_INVESTED)
    if notional < floor:
        return UnsizedEntry(
            symbol,
            BELOW_MIN_NOTIONAL,
            f"a {notional:.2f} slot is below the {floor:.2f} broker floor; the "
            "account is too thin to open a position this cycle",
        )

    cycle_session = (ctx.get("cycle") or {}).get("session")
    session = cand.get("session") or cycle_session
    staged = session != cycle_session

    limit = cand.get("limit_price")
    if staged and limit is None:
        return UnsizedEntry(
            symbol,
            STAGED_ORDER_NEEDS_LIMIT,
            f"{symbol} is queued for {session} from a {cycle_session} cycle "
            "with no limit; an order that waits must name its price",
        )

    ask = _ask(ctx, symbol)
    # A limit is what the order will transact at, so it is what sizing budgets
    # against. Guard costs limits the same way, which is what keeps the two
    # from disagreeing at placement.
    #
    # With no limit from the play, the WHOLE path's limit is the ask marked up
    # by `entry_limit_offset_atr` x ATR(14), rounded to the cent (§4.3). The
    # markup rides on `basis`, so `select_path`, the near-miss promotion and
    # every cap check below all budget against the price the order will cross
    # at rather than against a bare ask the order would never actually fill on.
    basis = float(limit) if limit is not None else _marketable_limit(
        ask, _atr(ctx, symbol), ctx["config"]
    )
    if basis is None or basis <= 0:
        return UnsizedEntry(
            symbol, NO_PRICE, f"no limit and no usable ask for {symbol}"
        )

    # Applied here rather than up beside the even split because it needs the
    # price the order will actually cross at, and that is `basis` — the same
    # number `select_path` and every cap check below budget against. `min`,
    # never `max`: this can only cut a slot.
    cap = risk_budget(cand, ctx, basis)
    if cap is not None and cap < notional:
        cap_distance_pct = float(ctx["config"].get(
            RISK_PER_TRADE_PCT, RISK_PER_TRADE_PCT_DEFAULT
        )) * float(ctx["account"]["equity_total"]) / cap
        notional = cap
        if notional < floor:
            return UnsizedEntry(
                symbol,
                RISK_BUDGET,
                f"{symbol}'s stop is {cap_distance_pct:.1%} "
                f"away, so a {float((ctx['config']).get(RISK_PER_TRADE_PCT, RISK_PER_TRADE_PCT_DEFAULT)):.0%} "
                f"equity risk budget funds only {notional:.2f}, below the "
                f"{floor:.2f} broker floor",
            )

    path, quantity = select_path(basis, notional)

    # P3 share limit. The position cap is already inside `headroom`, so a
    # floored whole-share quantity cannot exceed it; what was missing was the
    # *name* for the case where one share alone does not fit — it used to read
    # `WHOLE_SHARE_ONLY`, the same words as a slot that is merely too small.
    room = budget.ladder.position_cap - budget.position_value.get(symbol, 0.0)
    if v2 and basis > room + MONEY_EPS:
        return UnsizedEntry(
            symbol,
            SHARE_LIMIT,
            f"one share of {symbol} at {basis:.2f} is over the "
            f"{room:.2f} left under its {budget.ladder.position_cap:.2f} "
            "position cap",
        )
    if v2 and path == WHOLE:
        most = math.floor((room + MONEY_EPS) / basis)
        if quantity > most:
            quantity = most
            adjustments.append(ADJ_SHARE_LIMIT)

    # "Full shares only, as much as possible", scoped to a near-miss rather
    # than a reallocation: `quantity >= 0.5` means this slot's own even-split
    # notional already buys most of a share, so rounding up spends a little
    # more of *this name's* room, not a different name's. A candidate whose
    # fair slice only reaches 0.05 shares is left fractional — forcing that
    # one whole would spend many times its own slot's budget and break §4's
    # even split for names ranked after it, which is the ladder discipline
    # this is not supposed to touch. Bounded by the same two ceilings §4.4
    # already enforces either way.
    promote_room = min(headroom, budget.remaining)
    if v2 and budget.deploy_room is not None:
        # Rounding up to a whole share must not round past the target either.
        promote_room = min(promote_room, budget.deploy_room)
    if path == FRACTIONAL and quantity >= 0.5 and basis <= promote_room:
        path, quantity = WHOLE, 1.0

    # The near-miss promotion above already gets first crack at rescuing a
    # candidate into WHOLE; anything still FRACTIONAL past that point is a
    # slot that cannot afford one full share at basis, and is refused rather
    # than routed to a fractional market order. Fractional sells are
    # untouched — this gate sits only on the buy path sizing builds here.
    if path == FRACTIONAL and bool(ctx["config"].get("whole_shares_only", True)):
        return UnsizedEntry(
            symbol,
            WHOLE_SHARE_ONLY,
            f"{symbol}'s slot cannot afford one full share at {basis:.2f}; "
            "this account buys whole shares only",
        )

    if path == WHOLE:
        price = limit_price = basis
        source = "limit" if limit is not None else "ask"
        sessions, order_type, stop = WHOLE_SESSIONS, "limit", True
    else:
        # §4.3 makes the fractional path market-only, so a limit on such a
        # candidate is unplaceable: it informs the path choice and is then
        # dropped. The order is costed at the ask, the way the broker fills it.
        if ask is None:
            return UnsizedEntry(
                symbol,
                NO_PRICE,
                f"{symbol} sizes fractional, which places at market, and there "
                "is no ask to cost it against",
            )
        price, source, limit_price = ask, "ask", None
        _, quantity = select_path(ask, notional)
        sessions, order_type, stop = FRACTIONAL_SESSIONS, "market", False

        if staged:
            return UnsizedEntry(
                symbol,
                STAGED_FRACTIONAL_MARKET,
                f"{symbol} sizes fractional and so places at market, which "
                f"cannot carry the limit that staging into {session} requires",
            )

    if session not in sessions:
        return UnsizedEntry(
            symbol,
            PATH_SESSION_INELIGIBLE,
            f"the {path} path cannot reach {session}",
        )

    # Flooring a quantity can land under the floor even when the budget clears
    # it, so the last word belongs to the order rather than to the budget.
    filled = quantity * price
    if filled < floor:
        return UnsizedEntry(
            symbol,
            BELOW_MIN_NOTIONAL,
            f"{quantity:g} of {symbol} at {price:.2f} is {filled:.2f}, below "
            f"the {floor:.2f} broker floor",
        )

    budget.take(symbol, sector, filled)

    return SizedEntry(
        symbol=symbol,
        play_id=cand.get("play_id"),
        path=path,
        quantity=quantity,
        price=price,
        price_source=source,
        limit_price=limit_price,
        notional=filled,
        order_type=order_type,
        session=session,
        sessions_eligible=sessions,
        resting_stop_eligible=stop,
        conviction=_conviction(cand),
        rank_bonus=bonus,
        rank_score=score,
        notional_multiplier=multiplier,
        adjustments=tuple(adjustments),
    )


def _correlated_holding(
    symbol: str, sector: str, ctx: Mapping[str, Any], budget: _Budget
) -> str | None:
    """A held name this candidate is "the same bet" as, or None.

    Return correlation is not computed — nothing in the payload carries
    daily closes for held and candidate names, and this phase adds no
    fetch.
    """
    sectors = ctx.get("sectors") or {}
    subs = ctx.get("sub_industries") or {}
    own_sub = subs.get(symbol)
    for held in sorted(s for s in budget.held if s is not None):
        if held == symbol:
            continue
        if own_sub is not None and subs.get(held) == own_sub:
            return held
        if sectors.get(held) == sector:
            return held
    return None


def _v2_refusal(
    symbol: str,
    sector: str,
    new_name: bool,
    ctx: Mapping[str, Any],
    budget: _Budget,
    floor: float,
) -> UnsizedEntry | None:
    """The `sizing_v2` rules that refuse a candidate outright, in the order a
    reader chasing the name wants them: the ledger, the posture, the sector's
    day, then the deployment target."""
    # Imported here for the reason `risk_budget` imports `plays.core` here:
    # `reconcile` reaches `plays`, which reaches this module.
    from datetime import datetime

    from src.reconcile import reentry_block

    now = (ctx.get("cycle") or {}).get("now")
    if now:
        # The UTC date of the cycle clock, the same day `guard` counts from.
        today = datetime.fromisoformat(str(now).replace("Z", "+00:00")).date()
        blocked = reentry_block(symbol, ctx.get("loss_closes"), today, ctx["config"])
        if blocked is not None:
            return UnsizedEntry(
                symbol,
                REENTRY_BLOCKED,
                f"{symbol} was closed at a loss {blocked[0]} day(s) ago; no "
                f"re-entry for {blocked[1]} days",
            )

    posture = (ctx.get("regime") or {}).get("posture")
    if new_name and posture_multiplier(ctx) <= 0.0:
        return UnsizedEntry(
            symbol,
            POSTURE_NO_ENTRY,
            f"the posture is {posture}; no new position is sized",
        )

    if new_name and bool(ctx["config"].get(ONE_PER_SECTOR_DAY, True)):
        first = budget.sectors_entered_today.get(sector)
        if first is not None:
            return UnsizedEntry(
                symbol,
                ONE_PER_SECTOR_DAY_REACHED,
                f"{sector} already has a new entry today ({first}); at most "
                "one new name per sector per day",
            )

    if budget.deploy_room is not None and budget.deploy_room < floor:
        target = _regime_number(ctx, "target_invested_pct") or 0.0
        return UnsizedEntry(
            symbol,
            TARGET_INVESTED_REACHED,
            f"invested is at or over the {target:.0%} the {posture} posture "
            f"targets; {budget.deploy_room:.2f} of room is below the "
            f"{floor:.2f} broker floor",
        )

    return None


def _size_full_send(
    picks: list[tuple[Mapping[str, Any], float, float]],
    displaced: list[tuple[Mapping[str, Any], float, float]],
    ctx: Mapping[str, Any],
    result: SizingResult,
) -> bool:
    """§17's SURGE path: commit the whole available cash across ``picks``.

    Returns True when at least one full-send order was produced — in which case
    the caller must not run the ordinary ladder, because there is no cash left
    for it. Returns False when nothing priced or nothing could hold a whole
    share, and the caller falls back to ordinary sizing so a broken fetch does
    not freeze the cycle.

    §4.4's slot, position and sector caps are not consulted here at all; the
    `full_deployment` flag on each entry is what tells `guard.concentration` to
    skip them too. Every *other* guard check still runs.
    """
    account = ctx.get("account") or {}
    config = ctx["config"]
    cycle_session = (ctx.get("cycle") or {}).get("session")

    # The whole settled balance, reserve included — "spend everything available"
    # (operator). `settled_basis` is not None past `size`'s own Invariant 1
    # guard, but it can be zero.
    pot = settled_basis(account, config)
    remaining = 0.0 if pot is None else max(0.0, float(pot))

    over_cap = picks[SURGE_MAX_NAMES:]
    picks = picks[:SURGE_MAX_NAMES]
    produced = False

    for index, (cand, bonus, score) in enumerate(picks):
        rest = picks[index:]
        weights = [max(0.0, _conviction(c)) for c, _, _ in rest]
        total_weight = sum(weights)
        if len(rest) == 1:
            share = remaining
        elif total_weight > 0:
            share = remaining * weights[0] / total_weight
        else:
            share = remaining / len(rest)

        symbol = cand.get("symbol")
        limit = cand.get("limit_price")
        ask = _ask(ctx, symbol)
        basis = float(limit) if limit is not None else ask
        if basis is None or basis <= 0:
            result.unsized.append(
                UnsizedEntry(symbol, NO_PRICE, f"no limit and no usable ask for {symbol}")
            )
            continue

        quantity = math.floor(share / basis)
        if quantity < 1:
            result.unsized.append(
                UnsizedEntry(
                    symbol,
                    WHOLE_SHARE_ONLY,
                    f"{symbol}'s full-send share of {share:.2f} cannot hold one "
                    f"whole share at {basis:.2f}",
                )
            )
            continue

        filled = quantity * basis
        remaining -= filled
        produced = True
        result.entries.append(
            SizedEntry(
                symbol=symbol,
                play_id=cand.get("play_id"),
                path=WHOLE,
                quantity=float(quantity),
                price=basis,
                price_source="limit" if limit is not None else "ask",
                limit_price=basis,
                notional=filled,
                order_type="limit",
                session=cycle_session,
                sessions_eligible=WHOLE_SESSIONS,
                resting_stop_eligible=True,
                conviction=_conviction(cand),
                rank_bonus=bonus,
                rank_score=score,
                notional_multiplier=1.0,
                full_deployment=True,
            )
        )

    if produced:
        for cand, _bonus, _score in displaced + over_cap:
            result.unsized.append(
                UnsizedEntry(
                    cand.get("symbol"),
                    FULL_SEND_ACTIVE,
                    "the cycle committed all available cash to the full-send names",
                )
            )

    return produced


def size(
    candidates: Sequence[Mapping[str, Any]], ctx: Mapping[str, Any]
) -> SizingResult:
    """Turn ``candidates`` into sized entries at this cycle's rung."""
    ladder = _ladder(ctx)
    result = SizingResult(ladder=ladder)

    if ladder.notional is None:
        # Invariant 1 fails closed: an unestablished balance sizes nothing.
        result.unsized = [
            UnsizedEntry(
                cand.get("symbol"),
                SETTLED_CASH_UNKNOWN,
                "settled cash could not be established; no entry may be sized",
            )
            for cand in candidates
        ]
        return result

    ranked = _rank(candidates, ctx, ladder.notional)

    v2 = v2_enabled(ctx)
    posture = (ctx.get("regime") or {}).get("posture")
    if v2:
        result.v2 = {
            "posture": posture,
            "size_multiplier": posture_multiplier(ctx),
            "target_invested_pct": _regime_number(ctx, "target_invested_pct"),
            "invested": round(invested_value(ctx), 2),
            "deploy_room": deployment_room(ctx),
        }

    # The full-send switch lives in `edge_control` (operator-flippable), not in
    # `config.json`. `plays.surge` has already checked it before proposing, so
    # a `full_deployment` candidate should not reach here with the switch off —
    # this is the belt-and-braces re-check at the point cash is committed.
    if bool((ctx.get("control") or {}).get("full_send_enabled", False)):
        surge = [row for row in ranked if row[0].get("full_deployment")]
        if surge and v2 and posture is not None and posture != _TRADE_NORMALLY:
            # P3: committing the whole balance cannot be reconciled with a
            # posture that says to size down, so full-send stands aside and
            # its names are not put through the ordinary ladder either.
            for cand, _bonus, _score in surge:
                result.unsized.append(
                    UnsizedEntry(
                        cand.get("symbol"),
                        POSTURE_NO_ENTRY,
                        f"the posture is {posture}; full-send runs only in "
                        f"{_TRADE_NORMALLY}",
                    )
                )
            ranked = [row for row in ranked if not row[0].get("full_deployment")]
        elif surge:
            others = [row for row in ranked if not row[0].get("full_deployment")]
            if _size_full_send(surge, others, ctx, result):
                return result
            # Nothing sizeable in the full-send set: fall back to the ordinary
            # ladder on the remaining candidates so the cycle is not frozen.
            ranked = others

    budget = _Budget(ctx, ladder)
    for cand, bonus, score in ranked:
        outcome = _size_one(cand, ctx, budget, bonus, score)
        if isinstance(outcome, SizedEntry):
            result.entries.append(outcome)
        else:
            result.unsized.append(outcome)

    return result
