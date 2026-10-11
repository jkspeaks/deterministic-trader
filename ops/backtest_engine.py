#!/usr/bin/env python3
"""ops/backtest_engine.py — the honest backtest's arithmetic, without the data.

The S1 backtest's arithmetic. `ops/honest_backtest.py` fetches bars, earnings
dates and
index history and builds the panel; everything that turns a panel into a
number lives here, in plain Python over plain lists, so the repo's suite (which
has no numpy) can test it:

* `dip_signal`      — the shipped classic-DIP rule, at the time a cycle sees it
* `simulate_trade`  — one round trip under the live exit stack and its timing
* `Costs`           — spread, slippage and the non-fill draw
* `replay`          — a day-by-day portfolio under the shipped caps
* `Sleeve`          — S4's core sleeve: idle cash held in one broad ETF
* `hold`            — S4's beta arms: a basket bought once and held
* `beta_rule`       — S4's pre-registered decision rule
* `time_weighted`   — the live account's return with deposits taken out
* `metrics`         — total return, Sharpe, drawdown, turnover, hit rate, ...
* `Membership`      — S&P 500 membership by date, from the change history
* `survivorship_*`  — the bound and its break-evens

**The timing is the point.** An earlier backtest entered at the signal's close
and judged exits on that day's close. The account does neither: it decides at
about 09:40 ET on indicators the pre-market job computed from the *prior*
close, and looks again at about 15:07. So here a signal on day `t` reads RSI
and the averages as of `t-1`, tests them against the open of `t`, and fills at
that open; and every per-cycle exit is checked at the open and at the close,
with the prior close's ATR and RSI.

**Nothing here can touch the live system.** No import of `services/`, no
network, no order vocabulary. It reads `src.sizing` only through callables the
caller passes in.
"""

from __future__ import annotations

import hashlib
import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Mapping, Sequence

# Exit reasons, the live names.
TARGET = "TARGET"
ATR_STOP = "ATR_STOP"
TIME_STOP = "TIME_STOP"
SIGNAL = "SIGNAL"
EARNINGS = "EARNINGS_DEADLINE"
OPEN = "OPEN"          # the data ended before the trade did

# The shipped classic-DIP constants (`src/plays/dip.py`, `config/config.json`).
RSI_TRIGGER = 30.0
TREND_BAND = 0.03
DEPTH_FULL_SCALE = 0.10
RSI_EXTREMITY_FULL_SCALE = 15.0
EARNINGS_CLEARANCE = 3

# Money is compared to the cent, as `src.sizing` does.
MONEY_EPS = 0.005

# The account's drawdown halt (`config.drawdown_halt_pct`). Not simulated: a
# path that reaches it is flagged, because live the book would be flattened
# there and nothing after that point in the replay would have happened.
HALT_DRAWDOWN = 0.20


def _ok(value: Any) -> bool:
    return value is not None and isinstance(value, (int, float)) and math.isfinite(value)


# ---------------------------------------------------------------------------
# The panel
# ---------------------------------------------------------------------------


@dataclass
class Series:
    """One symbol on the common calendar. Every list has one entry per day;
    a day the symbol did not trade holds None (never a carried-forward price:
    a fill or an exit needs a bar that printed).

    ``until`` is sessions until the next earnings report as of each day, or
    None where no later report date is known. ``adv`` is the trailing 20-day
    average dollar volume as of the prior close.
    """

    o: Sequence[float | None]
    h: Sequence[float | None]
    l: Sequence[float | None]
    c: Sequence[float | None]
    rsi: Sequence[float | None]
    atr: Sequence[float | None]
    sma20: Sequence[float | None]
    sma200: Sequence[float | None]
    adv: Sequence[float | None] | None = None
    until: Sequence[int | None] | None = None

    def __len__(self) -> int:
        return len(self.c)


# ---------------------------------------------------------------------------
# The signal
# ---------------------------------------------------------------------------


def _scaled(value: float, full_scale: float) -> float:
    return max(0.0, min(1.0, value / full_scale))


def dip_signal(s: Series, t: int, *, earnings_veto: bool = False) -> float | None:
    """Conviction if classic DIP would propose this name at the open of day
    ``t``, else None.

    As `plays.dip._classic` reads it at a 09:40 cycle: the prior close's RSI at
    or under 30; today's price (the open) no more than 3% under the prior
    close's 200-day average, and below its 20-day. Conviction is the larger of
    depth under the 20-day (10% is full) and RSI extremity (15 points under
    the trigger is full) — the number `entries._rank_key` sorts on.

    With ``earnings_veto``: a report inside `EARNINGS_CLEARANCE` sessions
    refuses. An unknown date does **not** refuse here — live it does, but
    offline an unknown date is a gap in the vendor's history, not a fact about
    the day, and refusing on it would drop the uncovered events instead of
    measuring the veto. Coverage is reported beside the result.
    """
    if t < 1 or t >= len(s):
        return None
    price, rsi, sma200, sma20 = s.o[t], s.rsi[t - 1], s.sma200[t - 1], s.sma20[t - 1]
    if not (_ok(price) and _ok(rsi) and _ok(sma200) and _ok(sma20)):
        return None
    if rsi > RSI_TRIGGER or price < sma200 * (1.0 - TREND_BAND):
        return None
    if earnings_veto and blocked_by_earnings(s, t):
        return None
    depth = _scaled((sma20 - price) / sma20, DEPTH_FULL_SCALE)
    if depth <= 0.0:
        return None
    return max(depth, _scaled(RSI_TRIGGER - rsi, RSI_EXTREMITY_FULL_SCALE))


def blocked_by_earnings(s: Series, t: int) -> bool:
    """A known report inside the play's clearance. Applied to controls too."""
    if s.until is None:
        return False
    until = s.until[t]
    return until is not None and until <= EARNINGS_CLEARANCE


EARNINGS_HORIZON_DAYS = 120


def sessions_until_report(days: Sequence[date], reports: Sequence[date] | None,
                          horizon_days: int = EARNINGS_HORIZON_DAYS) -> list[int | None] | None:
    """For each day, sessions until the next report on or after it: 0 on the
    report's own session, None where no report is known inside the horizon.

    ``reports`` must be sorted. A report dated on a non-session (a weekend
    filing date in the vendor's table) counts as the next session. None for a
    symbol with no report dates at all — "no history", which the caller
    reports as uncovered, as distinct from a list of Nones.
    """
    if not reports:
        return None
    # Each report as the session it lands on: its own date, or the first
    # session after it.
    sessions: list[tuple[int, date]] = []
    k = 0
    for report in reports:
        while k < len(days) and days[k] < report:
            k += 1
        if k < len(days):
            sessions.append((k, report))
    out: list[int | None] = [None] * len(days)
    j = 0
    for i, day in enumerate(days):
        while j < len(sessions) and sessions[j][0] < i:
            j += 1
        if j < len(sessions) and (sessions[j][1] - day).days <= horizon_days:
            out[i] = sessions[j][0] - i
    return out


def earnings_covered(reports: Sequence[date] | None, day: date,
                     horizon_days: int = EARNINGS_HORIZON_DAYS) -> bool:
    """Whether the vendor's history brackets ``day``: a report on or before
    it, and one after it inside the horizon. An event that is not covered has
    an earnings veto nobody can measure."""
    if not reports:
        return False
    return any(r <= day for r in reports) and any(
        0 < (r - day).days <= horizon_days for r in reports)


# ---------------------------------------------------------------------------
# One trade
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Exits:
    """The exit stack. Defaults are the shipped values."""

    target: float = 0.06
    stop_mult: float = 3.0
    trail_mult: float = 3.5
    hold: int = 8
    rsi_exit: float | None = 50.0          # None removes the RSI exit
    earnings_flatten: int | None = None    # 2 applies the live earnings deadline


@dataclass(frozen=True)
class Trade:
    entry_day: int
    exit_day: int | None
    entry: float
    exit: float | None
    reason: str
    ret: float | None          # gross, exit / entry - 1
    mae: float                 # worst low against the entry while held (<= 0)
    mfe: float                 # best high against the entry while held (>= 0)
    held: int                  # sessions from the fill to the exit


def simulate_trade(s: Series, t: int, exits: Exits = Exits()) -> Trade | None:
    """A buy filled at the open of day ``t``, walked forward. None if day
    ``t`` has no open to fill at.

    Day ``t`` is session 0. Only the resting target acts on it (the
    same-day-fill guard). From session 1, in the order a day unfolds:

    1. a gap open at or above the target fills the resting order at the open;
    2. the 09:40 cycle, at the open's price: the earnings deadline, the ATR
       stop, the time stop, the RSI exit — live's own priority order;
    3. the resting target, if the day's high reaches it;
    4. the 15:07 cycle, at the close's price: the ATR stop.

    The stop is the higher of ``entry - stop_mult * ATR`` and
    ``high since entry - trail_mult * ATR`` with the *prior close's* ATR, which
    is what the pre-market job hands the cycle. A day with no bar is skipped:
    nothing can be judged or sold on it. If the data ends first the trade is
    `OPEN` with no return.
    """
    entry = s.o[t] if t < len(s) else None
    if not _ok(entry) or entry <= 0.0:
        return None
    target = entry * (1.0 + exits.target)
    high = entry
    low = entry

    def done(day: int, price: float, reason: str, session: int) -> Trade:
        return Trade(t, day, entry, price, reason, price / entry - 1.0,
                     low / entry - 1.0, high / entry - 1.0, session)

    session = -1
    for day in range(t, len(s)):
        o, h, l, c = s.o[day], s.h[day], s.l[day], s.c[day]
        if not (_ok(o) and _ok(h) and _ok(l) and _ok(c)):
            continue
        session += 1

        if session == 0:
            high, low = max(high, h), min(low, l)
            if h >= target:
                return done(day, target, TARGET, 0)
            continue

        if o >= target:
            return done(day, o, TARGET, session)

        atr = s.atr[day - 1]
        stop = None
        if _ok(atr):
            stop = max(entry - exits.stop_mult * atr, high - exits.trail_mult * atr)

        # The 09:40 cycle. The open is the price; the day's range is not known.
        low, high = min(low, o), max(high, o)
        if exits.earnings_flatten is not None and s.until is not None:
            until = s.until[day]
            if until is not None and until <= exits.earnings_flatten:
                return done(day, o, EARNINGS, session)
        if stop is not None and o <= stop:
            return done(day, o, ATR_STOP, session)
        if session >= exits.hold:
            return done(day, o, TIME_STOP, session)
        if exits.rsi_exit is not None and _ok(s.rsi[day - 1]) and s.rsi[day - 1] >= exits.rsi_exit:
            return done(day, o, SIGNAL, session)

        high, low = max(high, h), min(low, l)
        if h >= target:
            return done(day, target, TARGET, session)

        # The 15:07 cycle, with the day's high now part of the trailing stop.
        if _ok(atr):
            stop = max(entry - exits.stop_mult * atr, high - exits.trail_mult * atr)
            if c <= stop:
                return done(day, c, ATR_STOP, session)

    return Trade(t, None, entry, None, OPEN, None, low / entry - 1.0, high / entry - 1.0,
                 max(session, 0))


# ---------------------------------------------------------------------------
# Costs
# ---------------------------------------------------------------------------

# Half the median quoted spread, per side, by 20-day average dollar volume.
# `(upper bound of the bucket, half-spread)`.
SPREAD_BUCKETS = (
    (20e6, 0.001165),
    (100e6, 0.001029),
    (500e6, 0.000419),
    (math.inf, 0.000284),
)
THIN_ADV = 20e6
THIN_HAIRCUT = 0.00075
SLIPPAGE_PER_PCT_ADV = 0.0010
# With no ADV for a name the cost is the thin bucket's, haircut included: an
# unknown is charged as the worst case, never as free.
PASSIVE_EXITS = frozenset({TARGET})

# 6 of 13 live buy orders filled (`edge_fill_quality`).
OBSERVED_FILL_PROBABILITY = 6 / 13


@dataclass(frozen=True)
class Costs:
    """The cost model. `Costs(enabled=False)` is the gross run."""

    enabled: bool = True
    fill_probability: float = 1.0
    seed: int = 20261005

    def half_spread(self, adv: float | None) -> float:
        if not self.enabled:
            return 0.0
        if not _ok(adv) or adv <= 0.0:
            return SPREAD_BUCKETS[0][1] + THIN_HAIRCUT
        for bound, half in SPREAD_BUCKETS:
            if adv < bound:
                return half + (THIN_HAIRCUT if adv < THIN_ADV else 0.0)
        return SPREAD_BUCKETS[-1][1]

    def slippage(self, notional: float, adv: float | None) -> float:
        """Per side: 10 bps for each 1% of the day's dollar volume taken."""
        if not self.enabled or not _ok(adv) or adv <= 0.0:
            return 0.0
        return SLIPPAGE_PER_PCT_ADV * (notional / adv) * 100.0

    def entry(self, notional: float, adv: float | None) -> float:
        return self.half_spread(adv) + self.slippage(notional, adv)

    def exit(self, notional: float, adv: float | None, reason: str) -> float:
        """A resting target is a passive limit: it pays no spread."""
        spread = 0.0 if reason in PASSIVE_EXITS else self.half_spread(adv)
        return spread + self.slippage(notional, adv)

    def net_return(self, gross: float, notional: float, adv_in: float | None,
                   adv_out: float | None, reason: str) -> float:
        """Buy at the price marked up by the entry cost, sell marked down."""
        paid = 1.0 + self.entry(notional, adv_in)
        got = (1.0 + gross) * (1.0 - self.exit(notional, adv_out, reason))
        return got / paid - 1.0

    def fills(self, day: Any, symbol: str) -> bool:
        """Whether the entry order for this signal filled. A hash of the seed,
        the day and the *signal's* symbol, so the DIP portfolio and every
        control portfolio miss exactly the same slots."""
        if self.fill_probability >= 1.0:
            return True
        return _uniform(self.seed, "fill", day, symbol) < self.fill_probability


def _uniform(*parts: Any) -> float:
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2.0 ** 64


def pick(pool: Sequence[str], *parts: Any) -> str | None:
    """One name from a sorted pool, by hash. None for an empty pool."""
    if not pool:
        return None
    return pool[int(_uniform(*parts) * len(pool)) % len(pool)]


def capacity(edge: float | None, adv: float | None) -> float | None:
    """The position size at which round-trip slippage equals the gross edge.

    Slippage is `SLIPPAGE_PER_PCT_ADV` per 1% of ADV per side, so a round trip
    costs twice that; the size where it eats ``edge`` is
    ``edge / (2 * rate) * 1% * ADV``. None when there is no positive edge to
    consume — a capacity of a strategy with no edge is not a number.
    """
    if edge is None or edge <= 0.0 or not _ok(adv) or adv <= 0.0:
        return None
    return edge / (2.0 * SLIPPAGE_PER_PCT_ADV) * 0.01 * adv


# ---------------------------------------------------------------------------
# The portfolio replay
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Caps:
    """What the account may take. Each callable is the shipped function
    (`src.sizing`) or a constant standing in for a named variant."""

    n_target: Callable[[float], int]
    position_cap_fraction: Callable[[int], float]
    sector_cap_fraction: Callable[[int], float]
    max_new_per_day: int = 3
    cash_reserve_pct: float = 0.02
    cash_reserve_floor: float = 1.0
    reentry_days: int = 31
    one_per_sector_day: bool = False


@dataclass
class Candidate:
    """A signal on a day. ``symbol`` is what is bought (the signal itself, or
    its stand-in in a control portfolio); ``signal`` is always the DIP name,
    which keys the fill draw and the pairing."""

    symbol: str
    signal: str
    sector: str
    conviction: float
    # S3. The posture's size multiplier for this entry, in (0, 1]: it shrinks
    # the slot and can never grow it. 1.0 leaves every earlier result unchanged.
    multiplier: float = 1.0


@dataclass
class Position:
    symbol: str
    signal: str
    sector: str
    entry_day: int
    exit_day: int | None
    entry: float              # the fill price, before costs
    shares: float
    cost: float               # cash paid, entry costs included
    proceeds: float | None    # cash received, exit costs taken
    gross: float | None
    net: float | None
    reason: str
    mae: float
    mfe: float
    held: int


@dataclass
class Replay:
    days: Sequence[Any]
    equity: list[float]
    invested: list[float]
    trades: list[Position]
    missed: list[tuple[int, str]]      # (day index, signal) for unfilled entries
    skipped: dict[str, int] = field(default_factory=dict)
    # S4. Dollars bought outside `trades` (a sleeve's or a basket's
    # purchases), so turnover counts them; the sleeve's own value each day and
    # what happened to it; interest credited on idle cash.
    extra_bought: float = 0.0
    sleeve_value: list[float] = field(default_factory=list)
    sleeve: dict[str, Any] = field(default_factory=dict)
    interest: float = 0.0


@dataclass(frozen=True)
class Sleeve:
    """The S4 core sleeve: idle cash parked in one broad
    ETF, with the satellite keeping first call on it.

    * ``cap`` — the most of equity the sleeve may hold.
    * ``buffer_slots`` — cash kept ready for the satellite, in slots, on top
      of the reserve. Cash above it is swept in; cash under it is restored by
      selling sleeve shares, whose proceeds are spendable the next session.
    * ``target`` — the one resting order: a limit sell this far above the
      sleeve's average buy price. When it fills the whole sleeve is sold.
    * ``honour_reentry`` — a sleeve sale under its average buy price starts
      the account's ordinary no-rebuy window (`Caps.reentry_days`).

    There is no stop of any kind here, by rule and by construction: the only
    sells are the resting target and an ordinary sale the cycle makes when the
    satellite needs cash.
    """

    symbol: str
    cap: float = 0.50
    buffer_slots: float = 1.0
    target: float = 0.20
    honour_reentry: bool = True
    whole_shares: bool = False
    min_trade: float = 1.0


def replay(
    days: Sequence[date],
    candidates: Mapping[int, Sequence[Candidate]],
    panel: Mapping[str, Series],
    caps: Caps,
    exits: Exits = Exits(),
    costs: Costs = Costs(),
    *,
    start_equity: float = 1400.0,
    first_day: int = 0,
    last_day: int | None = None,
    simulate: Callable[[str, int], "Trade | None"] | None = None,
    whole_shares: bool = False,
    cash_rate: Sequence[float | None] | None = None,
    sleeve: Sleeve | None = None,
) -> Replay:
    """The account, one day at a time.

    At each open: yesterday's exit proceeds become cash; candidates are taken
    highest conviction first until a cap stops them. At each close: positions
    whose trade ended today are sold and the book is marked.

    A candidate is passed over (and counted in ``skipped``) when the name is
    held, when it closed at a loss inside the re-entry window, when the day's
    new-name limit or the slot count is reached, when its sector is full, or
    when there is no cash for it. A candidate whose order does not fill is
    ``missed``: it used no slot and no cash.

    ``simulate`` is ``(symbol, day) -> Trade``; the default walks the panel
    with ``exits``. A caller running hundreds of control portfolios over the
    same bars passes a memoised one.

    S4's three additions, each off by default so S1's numbers do not move:

    * ``whole_shares`` — the account's `whole_shares_only` rule. A slot buys
      the whole shares it affords; a slot that covers at least half a share is
      rounded up to one where cash and the caps allow; anything less is
      skipped as ``whole_share``.
    * ``cash_rate`` — a daily rate per day; cash (settled or not) earns it at
      each open after the first. None on a day earns nothing.
    * ``sleeve`` — see `Sleeve`. With one, a satellite slot is sized as if the
      sleeve were cash (it can be sold), but an entry is paid for from settled
      cash only and is skipped as ``no_settled_cash`` when there is too
      little. The order within a day: the sleeve's resting target on a gap
      open; the satellite's entries; the sweep in or out of the sleeve at the
      open; the resting target on the day's high.
    """
    last_day = len(days) - 1 if last_day is None else last_day
    cash = start_equity
    pending = 0.0                     # exit proceeds, usable next session
    open_positions: list[Position] = []
    closed: list[Position] = []
    missed: list[tuple[int, str]] = []
    skipped: dict[str, int] = {}
    loss_closed: dict[str, date] = {}
    equity_curve: list[float] = []
    invested_curve: list[float] = []
    equity = start_equity
    last_price: dict[str, float] = {}
    interest = 0.0

    # The sleeve's book. `core_basis` is what its shares were bought at, costs
    # excluded: the "buy price" the resting target sits above.
    core = sleeve
    core_series = panel.get(core.symbol) if core is not None else None
    core_shares = 0.0
    core_basis = 0.0
    core_mark = 0.0
    core_blocked_until: date | None = None
    core_bought = 0.0
    core_curve: list[float] = []
    core_stats: dict[str, Any] = {"buys": 0, "sells": 0, "loss_sells": 0, "target_fills": 0,
                                  "blocked_days": 0, "sold": 0.0, "halt_reached": False}

    def skip(why: str) -> None:
        skipped[why] = skipped.get(why, 0) + 1

    def core_sell(d: int, quantity: float, price: float, reason: str) -> None:
        """Sell sleeve shares at ``price``; the proceeds settle tomorrow."""
        nonlocal core_shares, core_basis, pending, core_blocked_until
        quantity = min(quantity, core_shares)
        if quantity <= 0.0:
            return
        average = core_basis / core_shares
        gross_value = quantity * price
        adv = None if core_series.adv is None else core_series.adv[d]
        pending += gross_value * (1.0 - costs.exit(gross_value, adv, reason))
        core_basis -= average * quantity
        core_shares -= quantity
        if core_shares < 1e-9:
            core_shares, core_basis = 0.0, 0.0
        core_stats["sold"] += gross_value
        if reason == TARGET:
            core_stats["target_fills"] += 1
            return
        core_stats["sells"] += 1
        if price < average:
            core_stats["loss_sells"] += 1
            if core.honour_reentry:
                core_blocked_until = days[d] + timedelta(days=caps.reentry_days)

    for d in range(first_day, last_day + 1):
        cash += pending
        pending = 0.0
        # Interest is for a night held, so the first day earns none.
        if cash_rate is not None and d > first_day and _ok(cash_rate[d]) and cash > 0.0:
            earned = cash * cash_rate[d]
            cash += earned
            interest += earned

        core_open = core_high = core_close = None
        if core_series is not None:
            core_open, core_high, core_close = core_series.o[d], core_series.h[d], core_series.c[d]
            if not (_ok(core_open) and _ok(core_high) and _ok(core_close)):
                core_open = core_high = core_close = None
        # The sleeve's resting target, on a gap open.
        if core_open is not None and core_shares > 0.0:
            if core_open >= core_basis / core_shares * (1.0 + core.target):
                core_sell(d, core_shares, core_open, TARGET)

        # -- entries, at the open ------------------------------------------
        target = caps.n_target(equity)
        new_today = 0
        sectors_today: set[str] = set()
        for cand in sorted(candidates.get(d, ()), key=lambda c: (-c.conviction, c.signal)):
            held = {p.symbol for p in open_positions}
            if cand.symbol in held:
                skip("held")
                continue
            closed_on = loss_closed.get(cand.symbol)
            if closed_on is not None and (days[d] - closed_on).days < caps.reentry_days:
                skip("reentry_block")
                continue
            if new_today >= caps.max_new_per_day:
                skip("daily_limit")
                continue
            slots = target - len(open_positions)
            if slots <= 0:
                skip("no_slot")
                continue
            if caps.one_per_sector_day and cand.sector in sectors_today:
                skip("sector_day")
                continue

            reserve = max(caps.cash_reserve_floor, caps.cash_reserve_pct * equity)
            deployable = cash - reserve
            # A sleeve can be sold, so it sizes as cash; it does not pay as cash.
            sizeable = deployable + (core_shares * core_mark if core is not None else 0.0)
            notional = min(sizeable / slots, equity * caps.position_cap_fraction(target))
            in_sector = sum(p.cost for p in open_positions if p.sector == cand.sector)
            notional = min(notional, equity * caps.sector_cap_fraction(target) - in_sector)
            if cand.multiplier != 1.0:
                notional *= max(0.0, min(1.0, cand.multiplier))
            if notional < 1.0:
                skip("sector_cap" if sizeable / slots >= 1.0 else "no_cash")
                continue
            if core is not None and notional > deployable + MONEY_EPS:
                skip("no_settled_cash")
                continue

            series = panel.get(cand.symbol)
            if series is None:
                trade = None
            elif simulate is not None:
                trade = simulate(cand.symbol, d)
            else:
                trade = simulate_trade(series, d, exits)
            if trade is None:
                skip("no_bar")
                continue
            adv_in = None if series.adv is None else series.adv[d]
            if whole_shares:
                per_share = trade.entry * (1.0 + costs.entry(notional, adv_in))
                quantity = math.floor(notional / per_share + 1e-9)
                if quantity < 1 and notional / per_share >= 0.5:
                    # The near-miss promotion: most of a share is rounded up
                    # to one, inside the same cash and the same two caps.
                    room = min(deployable, equity * caps.position_cap_fraction(target),
                               equity * caps.sector_cap_fraction(target) - in_sector)
                    if per_share <= room + MONEY_EPS:
                        quantity = 1
                if quantity < 1:
                    skip("whole_share")
                    continue
                notional = quantity * per_share
            if not costs.fills(days[d], cand.signal):
                missed.append((d, cand.signal))
                continue

            shares =notional / (trade.entry * (1.0 + costs.entry(notional, adv_in)))
            cash -= notional
            new_today += 1
            sectors_today.add(cand.sector)
            last_price[cand.symbol] = trade.entry
            open_positions.append(Position(
                cand.symbol, cand.signal, cand.sector, d, trade.exit_day, trade.entry, shares, notional,
                None, trade.ret, None, trade.reason, trade.mae, trade.mfe, trade.held,
            ))

        # -- the sleeve: the sweep at the open, then the target on the high --
        if core is not None and core_open is not None:
            target_n = caps.n_target(equity)
            reserve = max(caps.cash_reserve_floor, caps.cash_reserve_pct * equity)
            slot = min(equity / max(1, target_n), equity * caps.position_cap_fraction(target_n))
            want = reserve + core.buffer_slots * slot
            adv = None if core_series.adv is None else core_series.adv[d]
            blocked = core_blocked_until is not None and days[d] < core_blocked_until
            if blocked:
                core_stats["blocked_days"] += 1
            if cash < want - core.min_trade and core_shares > 0.0:
                quantity = (want - cash) / core_open
                if core.whole_shares:
                    quantity = math.ceil(quantity - 1e-9)
                core_sell(d, quantity, core_open, "FUND")
            elif cash > want + core.min_trade and not blocked:
                spend = min(cash - want, core.cap * equity - core_shares * core_open)
                if spend >= core.min_trade:
                    per_share = core_open * (1.0 + costs.entry(spend, adv))
                    quantity = spend / per_share
                    if core.whole_shares:
                        quantity = math.floor(quantity + 1e-9)
                    if quantity > 0.0:
                        cash -= quantity * per_share
                        core_bought += quantity * per_share
                        core_shares += quantity
                        core_basis += quantity * core_open
                        core_stats["buys"] += 1
            if core_shares > 0.0 and core_high >= core_basis / core_shares * (1.0 + core.target):
                core_sell(d, core_shares, core_basis / core_shares * (1.0 + core.target), TARGET)

        # -- exits and the mark, at the close -------------------------------
        still: list[Position] = []
        for p in open_positions:
            series = panel[p.symbol]
            if p.exit_day is not None and p.exit_day <= d:
                gross_value = p.shares * p.entry * (1.0 + p.gross)
                adv_out = None if series.adv is None else series.adv[p.exit_day]
                p.proceeds = gross_value * (1.0 - costs.exit(p.cost, adv_out, p.reason))
                p.net = p.proceeds / p.cost - 1.0
                pending += p.proceeds
                if p.net < 0.0:
                    loss_closed[p.symbol] = days[d]
                closed.append(p)
            else:
                still.append(p)
        open_positions = still

        marked = 0.0
        for p in open_positions:
            close = panel[p.symbol].c[d]
            if _ok(close):
                last_price[p.symbol] = close
            marked += p.shares * last_price[p.symbol]
        if core is not None:
            if core_close is not None:
                core_mark = core_close
            core_curve.append(core_shares * core_mark)
            marked += core_shares * core_mark
        equity = cash + pending + marked
        equity_curve.append(equity)
        invested_curve.append(marked)

    result = Replay(days[first_day : last_day + 1], equity_curve, invested_curve,
                    closed + open_positions, missed, skipped)
    result.interest = interest
    if core is not None:
        result.extra_bought = core_bought
        result.sleeve_value = core_curve
        core_stats["halt_reached"] = max_drawdown([start_equity, *equity_curve]) <= -HALT_DRAWDOWN
        result.sleeve = core_stats
    return result


def hold(
    days: Sequence[date],
    panel: Mapping[str, Series],
    weights: Mapping[str, float],
    costs: Costs = Costs(),
    *,
    start_equity: float = 1400.0,
    first_day: int = 0,
    last_day: int | None = None,
    fraction: float = 1.0,
    rebalance: bool = False,
    cash_rate: Sequence[float | None] | None = None,
) -> Replay:
    """A basket bought at the first open and held: S4's beta arms.

    ``fraction`` of the starting equity goes into the basket by ``weights``
    (which are normalised); the rest stays in cash and is never added. With
    ``rebalance`` the basket is brought back to its weights at the first
    session of each calendar quarter, trading only the differences; cash is
    not touched. Fractional shares. Every purchase and every rebalancing sale
    crosses the spread.

    A day a member has no bar, it is marked at its last price and is left out
    of that day's rebalance.
    """
    last_day = len(days) - 1 if last_day is None else last_day
    total = sum(weights.values())
    weight = {s: w / total for s, w in weights.items() if w > 0.0}
    shares = {s: 0.0 for s in weight}
    price: dict[str, float] = {}
    cash = start_equity
    bought = 0.0
    interest = 0.0
    equity_curve: list[float] = []
    invested_curve: list[float] = []

    def adv(symbol: str, d: int) -> float | None:
        series = panel[symbol]
        return None if series.adv is None else series.adv[d]

    started = False
    for d in range(first_day, last_day + 1):
        if cash_rate is not None and d > first_day and _ok(cash_rate[d]) and cash > 0.0:
            earned = cash * cash_rate[d]
            cash += earned
            interest += earned

        if not started:
            for symbol, w in weight.items():
                dollars = start_equity * fraction * w
                opened = panel[symbol].o[d]
                if dollars <= 0.0 or not _ok(opened):
                    continue
                shares[symbol] = dollars / (opened * (1.0 + costs.entry(dollars, adv(symbol, d))))
                price[symbol] = opened
                cash -= dollars
                bought += dollars
            started = True
        elif rebalance and (days[d].month - 1) // 3 != (days[d - 1].month - 1) // 3:
            opens = {s: panel[s].o[d] for s in weight if _ok(panel[s].o[d])}
            value = {s: shares[s] * opens[s] for s in opens}
            basket = sum(value.values())
            share = sum(weight[s] for s in opens)
            # Sells first, so the buys are paid from what the sells raise.
            raised = 0.0
            wants: dict[str, float] = {}
            for symbol in opens:
                gap = basket * weight[symbol] / share - value[symbol]
                if gap < 0.0:
                    shares[symbol] -= -gap / opens[symbol]
                    raised += -gap * (1.0 - costs.exit(-gap, adv(symbol, d), "REBALANCE"))
                elif gap > 0.0:
                    wants[symbol] = gap
            wanted = sum(wants.values())
            for symbol, gap in wants.items():
                dollars = raised * gap / wanted
                shares[symbol] += dollars / (opens[symbol] * (1.0 + costs.entry(dollars, adv(symbol, d))))
                bought += dollars

        marked = 0.0
        for symbol in weight:
            close = panel[symbol].c[d]
            if _ok(close):
                price[symbol] = close
            marked += shares[symbol] * price.get(symbol, 0.0)
        equity_curve.append(cash + marked)
        invested_curve.append(marked)

    result = Replay(days[first_day : last_day + 1], equity_curve, invested_curve, [], [], {})
    result.extra_bought = bought
    result.interest = interest
    return result


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def max_drawdown(equity: Sequence[float]) -> float:
    """The deepest fall from a prior peak, as a negative fraction (0 if none)."""
    peak, worst = -math.inf, 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0.0:
            worst = min(worst, value / peak - 1.0)
    return worst


def sharpe(equity: Sequence[float], start: float, periods: int = 252) -> float | None:
    """Annualised Sharpe of the daily equity returns, no risk-free rate."""
    values = [start, *equity]
    returns = [b / a - 1.0 for a, b in zip(values, values[1:]) if a > 0.0]
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    sd = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1))
    return None if sd == 0.0 else mean / sd * math.sqrt(periods)


def metrics(result: Replay, start_equity: float) -> dict:
    """What the program's "primary outcome" rule asks for, from one replay."""
    closed = [t for t in result.trades if t.net is not None]
    wins = [t.net for t in closed if t.net > 0.0]
    losses = [t.net for t in closed if t.net <= 0.0]
    years = max(len(result.equity) / 252.0, 1e-9)
    final = result.equity[-1] if result.equity else start_equity
    average_equity = sum(result.equity) / len(result.equity) if result.equity else start_equity
    bought = sum(t.cost for t in result.trades) + result.extra_bought
    idle = (1.0 - sum(i / e for i, e in zip(result.invested, result.equity) if e > 0)
            / len(result.equity)) if result.equity else None

    def avg(values: Sequence[float]) -> float | None:
        return sum(values) / len(values) if values else None

    reasons: dict[str, int] = {}
    for t in closed:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1
    return {
        "total_return": final / start_equity - 1.0,
        "cagr": (final / start_equity) ** (1.0 / years) - 1.0 if final > 0 else -1.0,
        "sharpe": sharpe(result.equity, start_equity),
        "max_drawdown": max_drawdown([start_equity, *result.equity]),
        "turnover": bought / average_equity / years if average_equity > 0 else None,
        "trades": len(closed),
        "open": len(result.trades) - len(closed),
        "missed": len(result.missed),
        "hit_rate": len(wins) / len(closed) if closed else None,
        "payoff": (avg(wins) / -avg(losses)) if wins and losses and avg(losses) < 0 else None,
        "mean_net": avg([t.net for t in closed]),
        "mean_gross": avg([t.gross for t in closed]),
        "mae": avg([t.mae for t in closed]),
        "mfe": avg([t.mfe for t in closed]),
        "held": avg([float(t.held) for t in closed]),
        "exposure": (sum(i / e for i, e in zip(result.invested, result.equity) if e > 0)
                     / len(result.equity)) if result.equity else None,
        "cash_idle": idle,
        "sleeve_share": (sum(v / e for v, e in zip(result.sleeve_value, result.equity) if e > 0)
                         / len(result.equity)) if result.sleeve_value else None,
        "interest": result.interest,
        "sleeve": dict(result.sleeve),
        "reasons": dict(sorted(reasons.items())),
        "skipped": dict(sorted(result.skipped.items())),
    }


# ---------------------------------------------------------------------------
# S4: the beta rule and the live record's arithmetic
# ---------------------------------------------------------------------------


def beta_rule(policy: Mapping[str, Mapping[str, Mapping[str, Any]]],
              beta: Mapping[str, Mapping[str, Mapping[str, Any]]],
              windows: Sequence[str]) -> dict:
    """The S4 decision rule, points 1 to 4.

    ``policy[window]`` is `{version: metrics}` (the policy with and without
    its RSI exit); ``beta[window]`` is `{arm: metrics}`. An arm satisfies the
    rule when, in **every** window named, its net total return is at or above
    the better policy version's and its drawdown is no deeper than the
    shallower one's. The rule fires if any arm satisfies it. The literal
    reading counts only arms whose name ends ``|100%``.

    Drawdowns are negative fractions, so "no deeper" is ``>=``.
    """
    arms: dict[str, dict] = {}
    for arm in beta[windows[0]]:
        legs = {}
        for name in windows:
            versions = list(policy[name].values())
            bar_return = max(v["total_return"] for v in versions)
            bar_drawdown = max(v["max_drawdown"] for v in versions)
            b = beta[name][arm]
            legs[name] = {
                "beta_return": b["total_return"], "policy_return": bar_return,
                "beta_drawdown": b["max_drawdown"], "policy_drawdown": bar_drawdown,
                "return_ok": b["total_return"] >= bar_return,
                "drawdown_ok": b["max_drawdown"] >= bar_drawdown,
            }
        arms[arm] = {"windows": legs,
                     "satisfies": all(leg["return_ok"] and leg["drawdown_ok"] for leg in legs.values())}
    literal = {a: v for a, v in arms.items() if a.endswith("|100%")}
    return {
        "arms": arms,
        "fires": any(v["satisfies"] for v in arms.values()),
        "satisfied_by": sorted(a for a, v in arms.items() if v["satisfies"]),
        "literal_fires": any(v["satisfies"] for v in literal.values()),
        "literal_satisfied_by": sorted(a for a, v in literal.items() if v["satisfies"]),
    }


def time_weighted(rows: Sequence[Mapping[str, Any]], flows: Sequence[Mapping[str, Any]]) -> list[float]:
    """An account's growth factor at each row, with deposits taken out.

    ``rows`` are `{"trading_day": iso date, "equity": number}` in date order,
    possibly with days missing; ``flows`` are `{"date": iso date, "amount":
    number}` (a withdrawal is negative). Returns are chained between
    consecutive rows. A flow counts in the interval it landed in — after the
    earlier row's day, on or before the later one's — and is treated as
    arriving at the end of it, so it earns nothing until the next interval.
    A flow on or before the first row is already inside that row's equity.
    """
    by_day: dict[str, float] = {}
    for flow in flows:
        by_day[str(flow["date"])] = by_day.get(str(flow["date"]), 0.0) + float(flow["amount"])
    growth = [1.0]
    for prev, row in zip(rows, rows[1:]):
        landed = sum(amount for day, amount in by_day.items()
                     if str(prev["trading_day"]) < day <= str(row["trading_day"]))
        growth.append(growth[-1] * (float(row["equity"]) - landed) / float(prev["equity"]))
    return growth


def yearly(curve: Sequence[float], days: Sequence[date], start: float) -> dict[int, float]:
    """Calendar-year returns of an equity curve that began at ``start``."""
    out: dict[int, float] = {}
    prev = start
    for k, day in enumerate(days):
        if k + 1 == len(days) or days[k + 1].year != day.year:
            out[day.year] = curve[k] / prev - 1.0
            prev = curve[k]
    return out


# ---------------------------------------------------------------------------
# Index membership by date
# ---------------------------------------------------------------------------


class Membership:
    """Who was in the index on a day, rebuilt from today's list and the
    change history.

    Walks the changes backwards from today: undoing an addition removes the
    name before that date, undoing a removal puts it back. A name is a member
    on ``day`` if it sits inside one of its `[from, to)` intervals.

    The history is a public table kept by hand; a change it omits leaves a
    name looking like a member for too long (or too short). `intervals` is
    exposed so the caller can report how many names and member-days it found.
    """

    def __init__(self, current: Sequence[str],
                 changes: Sequence[tuple[date, str | None, str | None]],
                 start: date, end: date) -> None:
        open_since: dict[str, date] = {s: end + timedelta(days=1) for s in current}
        self.intervals: dict[str, list[tuple[date, date]]] = {}
        for when, added, removed in sorted(changes, key=lambda c: c[0], reverse=True):
            if when > end:
                continue
            if when < start:
                break
            if added and added in open_since:
                # In the index from `when` until the end of its open interval.
                self.intervals.setdefault(added, []).append((when, open_since.pop(added)))
            if removed and removed not in open_since:
                # Was a member up to `when`; its start is found further back.
                open_since[removed] = when
        for symbol, until in open_since.items():
            self.intervals.setdefault(symbol, []).append((date.min, until))
        self._starts = {s: sorted(iv) for s, iv in self.intervals.items()}

    def is_member(self, symbol: str, day: date) -> bool:
        spans = self._starts.get(symbol)
        if not spans:
            return False
        i = bisect_right(spans, (day, date.max)) - 1
        return i >= 0 and spans[i][0] <= day < spans[i][1]

    def member_days(self, symbol: str, days: Sequence[date]) -> int:
        return sum(1 for day in days if self.is_member(symbol, day))

    def ever(self) -> list[str]:
        return sorted(self.intervals)


# ---------------------------------------------------------------------------
# The survivorship bound
# ---------------------------------------------------------------------------


def survivorship_bound(edge: float, control_mean: float, share: float,
                       loss: float = -0.30) -> float:
    """The paired edge if ``share`` of DIP events were names that have no bars
    and each of those returned ``loss``, the control keeping its observed mean.

    Pessimistic on purpose, twice over: most names that leave the index with
    no surviving price series were acquired, usually at a premium, and the
    control would have drawn from the same missing names. It is a bound, not
    an estimate.
    """
    return (1.0 - share) * edge + share * (loss - control_mean)


def breakeven_loss(edge: float, control_mean: float, share: float) -> float | None:
    """The return on the unobserved share that takes the edge to zero. None
    when there is no unobserved share to carry it."""
    if share <= 0.0:
        return None
    return control_mean - (1.0 - share) / share * edge


def breakeven_share(edge: float, control_mean: float, loss: float = -0.30) -> float | None:
    """The unobserved share that zeroes the edge at ``loss``. None when the
    edge is already at or under zero, or when ``loss`` would not hurt."""
    drag = edge + control_mean - loss
    if edge <= 0.0 or drag <= 0.0:
        return None
    return edge / drag


# ---------------------------------------------------------------------------
# S5: the event plays' entry rules and the monthly rebalance replay.
# Each rule restates a shipped play on daily bars; the
# thresholds are the plays' own constants, repeated here so this module stays
# free of imports from `src`.
# ---------------------------------------------------------------------------

PEAD_MIN_REACTION = 0.02
PEAD_SURPRISE_FULL_SCALE = 10.0
PEAD_REACTION_FULL_SCALE = 0.10
BEFORE_OPEN_HOUR = 9.5
CATALYST_WINDOW = 2
CATALYST_MIN_MOVE = 0.03
CATALYST_MIN_VOLUME = 1.5
CATALYST_FULL_SCALE = 0.10
SURGE_RSI = (55.0, 80.0)
SURGE_MOMENTUM_MIN = 0.02
SURGE_FULL_SCALE = 0.15
AFTER_CLOSE_HOUR = 16.0
REBALANCE = "REBALANCE"


def reaction_session(days: Sequence[date], day: date, hour: float | None) -> int | None:
    """The session that digests a report made on ``day`` at ``hour``.

    Before the open it is that day's session; anything else (after the close,
    during the day, or an unknown hour, which takes the after-the-close rule
    as `pead.reaction_due` does) reacts the next session. A report on a day
    the market was shut reacts on the first session after it either way.
    """
    i = bisect_left(days, day)
    if i >= len(days):
        return None
    if days[i] != day or (hour is not None and hour < BEFORE_OPEN_HOUR):
        return i
    return i + 1 if i + 1 < len(days) else None


def filing_session(days: Sequence[date], day: date, hour: float | None) -> int | None:
    """The first session an 8-K filed on ``day`` at Eastern ``hour`` can be
    acted on: that day, or the next session when it was accepted at or after
    the close."""
    i = bisect_left(days, day)
    if i >= len(days):
        return None
    if days[i] == day and hour is not None and hour >= AFTER_CLOSE_HOUR:
        return i + 1 if i + 1 < len(days) else None
    return i


def pead_signal(c: Sequence[float | None], sma50: Sequence[float | None], r: int,
                surprise: float | None, *, need_surprise: bool = True, span: int = 1) -> float | None:
    """PEAD's conviction at the close of session ``r``, or None.

    The reaction is ``c[r] / c[r - span] - 1``. A beat (when ``need_surprise``),
    a reaction of at least +2% and a close above the 50-day; conviction is the
    larger of the scaled surprise and the scaled reaction.
    """
    if r - span < 0 or r >= len(c):
        return None
    last, prior, trend = c[r], c[r - span], sma50[r]
    if not (_ok(last) and _ok(prior) and _ok(trend)) or prior <= 0.0:
        return None
    reaction = last / prior - 1.0
    if reaction < PEAD_MIN_REACTION or last <= trend:
        return None
    by_reaction = _scaled(reaction, PEAD_REACTION_FULL_SCALE)
    if not need_surprise:
        return by_reaction
    if not _ok(surprise) or surprise <= 0.0:
        return None
    return max(_scaled(surprise, PEAD_SURPRISE_FULL_SCALE), by_reaction)


def catalyst_signal(ret: Sequence[float | None], volume_multiple: Sequence[float | None],
                    usable: Sequence[bool], t: int) -> float | None:
    """CATALYST's conviction at the close of session ``t``, or None: an 8-K
    usable on ``t`` or in the two sessions before it, a day up at least 3% on
    at least 1.5 times average volume."""
    if t < 0 or t >= len(ret):
        return None
    if not any(usable[max(0, t - CATALYST_WINDOW): t + 1]):
        return None
    move, volume = ret[t], volume_multiple[t]
    if not (_ok(move) and _ok(volume)):
        return None
    if move < CATALYST_MIN_MOVE or volume < CATALYST_MIN_VOLUME:
        return None
    return _scaled(move, CATALYST_FULL_SCALE)


def surge_signal(s: Series, sma50: Sequence[float | None], t: int, *,
                 earnings_veto: bool = True) -> float | None:
    """SURGE's conviction for a buy at the open of ``t``, read at the close of
    ``t - 1``: stacked averages, RSI in the band, at least 2% above the
    50-day, clear of a report."""
    i = t - 1
    if i < 0 or t >= len(s):
        return None
    price, a20, a50, a200, rsi = s.c[i], s.sma20[i], sma50[i], s.sma200[i], s.rsi[i]
    if not all(_ok(x) for x in (price, a20, a50, a200, rsi)):
        return None
    if not price > a20 > a50 > a200:
        return None
    if not SURGE_RSI[0] <= rsi <= SURGE_RSI[1]:
        return None
    if price < a50 * (1.0 + SURGE_MOMENTUM_MIN):
        return None
    if earnings_veto and blocked_by_earnings(s, t):
        return None
    conviction = _scaled(price / a50 - 1.0, SURGE_FULL_SCALE)
    return conviction if conviction > 0.0 else None


def month_starts(days: Sequence[date], first: date, last: date) -> list[int]:
    """The index of each calendar month's first session in ``[first, last]``."""
    out = []
    for i, day in enumerate(days):
        if day < first or day > last:
            continue
        if i == 0 or (days[i - 1].year, days[i - 1].month) != (day.year, day.month):
            out.append(i)
    return out


def rebalance_replay(
    days: Sequence[date],
    panel: Mapping[str, Any],
    schedule: Mapping[int, Sequence[str]],
    costs: Costs = Costs(),
    *,
    start_equity: float = 1400.0,
    fraction: float = 1.0,
    target: float | None = None,
    first_day: int = 0,
    last_day: int | None = None,
) -> Replay:
    """A book rebuilt on scheduled days: S5's Leaders monthly.

    ``schedule[d]`` is the names to hold from the open of day ``d``: a list
    for equal weights, or ``{name: weight}`` (S6) for unequal ones. At that
    open the names no longer wanted are sold, and ``fraction`` of equity is
    spread equally over the wanted ones (trims first, then buys, both at the
    open and both paying the cost model). Fractional shares.

    ``target`` rests one limit sell that far above a name's first buy price;
    when a day's high reaches it the whole position is sold there (or at the
    open, on a gap through it) and the cash waits for the next scheduled day.
    There is no stop of any kind.

    ``panel[symbol]`` needs ``o``, ``h``, ``c`` and ``adv``. A day a held name
    has no bar it is marked at its last price and cannot be sold.
    """
    last_day = len(days) - 1 if last_day is None else last_day
    cash = start_equity
    shares: dict[str, float] = {}
    entry: dict[str, float] = {}
    price: dict[str, float] = {}
    bought = 0.0
    fills = 0
    equity_curve: list[float] = []
    invested_curve: list[float] = []

    def adv(symbol: str, d: int) -> float | None:
        series = panel[symbol].adv
        return series[d] if series is not None else None

    def sell(symbol: str, d: int, quantity: float, at: float, reason: str) -> None:
        nonlocal cash
        value = quantity * at
        cash += value * (1.0 - costs.exit(value, adv(symbol, d), reason))
        shares[symbol] -= quantity
        if shares[symbol] * at < MONEY_EPS:
            del shares[symbol]
            entry.pop(symbol, None)

    for d in range(first_day, last_day + 1):
        if d in schedule:
            # S6: a mapping gives each name a weight; a plain list is equal weight.
            spec = schedule[d]
            weight = dict(spec) if isinstance(spec, Mapping) else {s: 1.0 for s in spec}
            wanted = [s for s, w in weight.items() if w > 0.0 and s in panel and _ok(panel[s].o[d])]
            total_weight = sum(weight[s] for s in wanted)
            for s in shares:
                if _ok(panel[s].o[d]):
                    price[s] = panel[s].o[d]
            for s in [x for x in shares if x not in wanted]:
                if _ok(panel[s].o[d]):
                    sell(s, d, shares[s], price[s], REBALANCE)
            equity = cash + sum(q * price[s] for s, q in shares.items())
            each = {s: fraction * equity * weight[s] / total_weight for s in wanted}
            for s in wanted:
                held = shares.get(s, 0.0) * price.get(s, 0.0)
                if held > each[s] + MONEY_EPS:
                    sell(s, d, (held - each[s]) / price[s], price[s], REBALANCE)
            for s in wanted:
                at = panel[s].o[d]
                spend = min(each[s] - shares.get(s, 0.0) * at, cash)
                if spend <= MONEY_EPS:
                    continue
                shares[s] = shares.get(s, 0.0) + spend / (at * (1.0 + costs.entry(spend, adv(s, d))))
                cash -= spend
                bought += spend
                entry.setdefault(s, at)
                price[s] = at
        for s in list(shares):
            bar = panel[s]
            if target is not None and _ok(bar.h[d]) and bar.h[d] >= entry[s] * (1.0 + target):
                at = entry[s] * (1.0 + target)
                if _ok(bar.o[d]) and bar.o[d] > at:
                    at = bar.o[d]
                sell(s, d, shares[s], at, TARGET)
                fills += 1
            elif _ok(bar.c[d]):
                price[s] = bar.c[d]
        invested = sum(q * price[s] for s, q in shares.items())
        equity_curve.append(cash + invested)
        invested_curve.append(invested)

    result = Replay(days[first_day:last_day + 1], equity_curve, invested_curve, [], [])
    result.extra_bought = bought
    result.sleeve = {"target_fills": fills,
                     "halt_reached": max_drawdown([start_equity, *equity_curve]) <= -HALT_DRAWDOWN}
    return result
