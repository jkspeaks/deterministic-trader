"""dip_recovery.watchlist — the Universe Pick routine: which five symbols this
side is allowed to look at tomorrow.

The design doc is deliberately modest here ("Overnight pick, simplified for
v1"): *no new 11-factor ranking model*. It filters the universe core already
maintains down to names with a qualifying dip and no open position on either
side of the lock, applies the event-risk exclusions core already applies, and
ranks whatever survives on two numbers that are already in the cache — average
daily **dollar** volume and 20-day **ATR%**. The top `watchlist_size` become
`edge_control.dip_recovery_watchlist`, which is the only list
`engine.evaluate` will ever consider for a new entry.

**Two functions, because the fetch sits between them.** `pool` runs on data
that costs nothing — the scan's tickers, the checked-in universe snapshot, the
symbol lock, this side's own rows and the account-level loss ledger — and
returns the symbols worth spending Robinhood calls on. `pick` runs after those
calls and does the ranking. Splitting them is what keeps the cost ceiling
(`max_scan_survivors`) *ahead* of the per-symbol fetch instead of behind it;
the same shape `day-context-plan` already uses, and the same lesson an earlier
cycle taught when ~280 sequential broker calls ran the org into its monthly
spend limit mid-cycle.

**Every exclusion here narrows, and that is the safe direction.** Nothing this
module admits can place an order: `signal.entry_signal` re-checks all seven
entry conditions per tick and `guard.evaluate` still rules on the order itself.
So a name wrongly dropped here costs an opportunity, and a name wrongly kept
costs nothing at all — the exact inverse of `src/candidates.py`, whose
discovery pass must be generous because nothing downstream can re-widen it.
The exclusions are nonetheless real ones rather than decoration: they are
guard's own rules, read from guard's own config keys, applied a few hours early
so the watchlist is not five names every Dip Monitor tick will deny all day.

**The universe snapshot's staleness gates this, and does not contradict phase
7.** `guard.schedule` deliberately leaves `UNIVERSE_STALE` to core, because
acting on an already-chosen Dip Recovery watchlist says nothing about core's
membership file. *Choosing* that watchlist is the opposite case: the design's
first entry condition is "symbol is in the active fortnight universe", so a
snapshot nobody has refreshed inside `universe_max_age_days` means membership
is unknown, and an unknown fence admits nothing. A stale snapshot therefore
writes an empty watchlist for the day rather than yesterday's names.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Mapping, Sequence

from src import universe as universes
from src.reconcile import reentry_block

from .core import (
    ALREADY_TRACKED,
    ATR_OUT_OF_RANGE,
    EVENT_RISK,
    INSUFFICIENT_LIQUIDITY,
    METRICS_UNAVAILABLE,
    NOT_IN_UNIVERSE,
    RANK_NOT_TOP,
    RECENT_LOSS_CLOSE,
    SCAN_POOL_TRUNCATED,
    SECTOR_INACTIVE,
    STRATEGY,
    SYMBOL_LOCKED,
    Skipped,
    _price,
    _threshold_group,
)

WATCHLIST_KEY = "dip_recovery_watchlist"

# Half liquidity, half volatility. The same 50/50 the dip score uses, and for
# the same stated reason: the doc asks for a *ranking* over two factors it
# names as equals, not a tiebreak where one only matters when the other ties.
_LIQUIDITY_WEIGHT = 0.5
_VOLATILITY_WEIGHT = 0.5

# Inline against a database without the seed row, the `signal.py` Bollinger precedent —
# the gate has to be correct in a fresh checkout, not only after a migration.
_DEFAULTS: dict[str, float] = {
    # Not `sizing_thresholds.max_active_positions`, which is also 5 today.
    # That one is how many positions may be *open at once*; this one is how
    # many names are offered to the entry gate each morning. They coincide at
    # v1's numbers and are free to diverge — a watchlist deliberately wider
    # than the slot count is the obvious first tuning move once this side has
    # a track record, and it must not require editing the position cap to
    # get there.
    "watchlist_size": 5,
    # Liquidity floor, in dollars of average daily turnover. Every Dip
    # Recovery order is a limit order and a whole position exits in one, so
    # the cost of a thin name is not a wide spread on the way in — it is an
    # exit that does not fill at all while the profit target is briefly true.
    "min_dollar_volume": 20_000_000.0,
    # This strategy has no stop. What bounds the downside is the averaging
    # ladder and the rule that it never sells below cost, so the thing
    # volatility actually buys is *reach*: at 0.5% daily range a 5% profit
    # target is weeks of trend away and the 47-day `max_hold_trading_days`
    # clock runs out first. `max_atr_pct` is the other side of that — past
    # some width, "will recover" stops being a dip thesis.
    "min_atr_pct": 1.5,
    "max_atr_pct": 8.0,
    # A cost ceiling, not a judgment. On an ordinary day the scan returns a
    # handful of names and this never binds; on a broad selloff it is what
    # keeps one morning's pick from turning into a few hundred sequential
    # broker calls. Truncation is logged per symbol rather than applied
    # silently — see `pool`.
    "max_scan_survivors": 30,
}


def _thresholds(ctx: Mapping[str, Any]) -> dict[str, float]:
    return {**_DEFAULTS, **_threshold_group(ctx, "universe_thresholds")}


def _trading_day(ctx: Mapping[str, Any]) -> str:
    return ctx["cycle"]["trading_day"]


def _as_date(value: str) -> date:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


@dataclass
class WatchlistPool:
    """`pool`'s answer: who is worth fetching, and why everyone else is not."""

    fetch: list[str] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "fetch": list(self.fetch),
            "skipped": [s.as_dict() for s in self.skipped],
            "reason": self.reason,
        }


@dataclass
class WatchlistPick:
    """`pick`'s answer: tomorrow's watchlist, its ranking, and the rejections.

    ``control_updates`` is shaped for `edge_control_apply` and always carries
    today's `trading_day`, empty list included. Writing an empty watchlist is
    not the same as writing nothing: Dip Monitor reads a watchlist whose
    `trading_day` is not today as empty anyway, so the explicit write says
    "this morning ran and found nothing" where a skipped write would say
    "this morning may never have run".
    """

    symbols: list[str] = field(default_factory=list)
    ranked: list[dict] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    control_updates: dict = field(default_factory=dict)
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "symbols": list(self.symbols),
            "ranked": list(self.ranked),
            "skipped": [s.as_dict() for s in self.skipped],
            "control_updates": dict(self.control_updates),
            "reason": self.reason,
        }


def _core_symbols(ctx: Mapping[str, Any]) -> set[str]:
    """Everything the other side of the lock owns or holds.

    Two sources because they close at different times. `symbol_lock` is
    authoritative and is what `guard.lock` reads, but `reconcile.py` claims a
    symbol on the cycle *after* core's fill confirms — so between those two
    moments core's book is the only place the position exists. Both, unioned,
    the same pairing `reconcile._other_side_symbols` uses in the other
    direction.
    """
    locked = {
        symbol
        for symbol, owner in (ctx.get("symbol_lock") or {}).items()
        if owner and owner != STRATEGY
    }
    held = {
        position.get("symbol")
        for position in ctx.get("positions") or []
        if position.get("symbol")
    }
    return locked | held


def _wash_sale_symbols(ctx: Mapping[str, Any]) -> dict[str, int]:
    """Symbols inside `wash_sale_days`, and how many days ago each closed.

    Account-level, exactly as `guard._wash_sale_denials` reads it: the IRS rule
    attaches to the account and the symbol, so a name *core* closed at a loss
    is one this side may not repurchase either. `reconcile.wash_sale_blocks` is
    shared with that reader on purpose — the two had the same rule written
    twice and were already drifting on scope, and a policy knob
    (`wash_sale_block_unknown_basis`) added to one and not the other would be
    the next drift.
    """
    today = _as_date(ctx["cycle"]["now"])
    rows = ctx.get("loss_closes") or []

    blocked: dict[str, int] = {}
    for symbol in {row.get("symbol") for row in rows if row.get("symbol") is not None}:
        hit = reentry_block(symbol, rows, today, ctx["config"])
        if hit is not None:
            blocked[symbol] = hit[0]
    return blocked


def pool(ctx: Mapping[str, Any]) -> WatchlistPool:
    """Scan tickers in, fetch list out — every filter that needs no broker call.

    Order matters only for which reason a symbol is logged under, and it runs
    cheapest-and-most-absolute first: membership is a set test against a file
    in the checkout, the lock is a fact about who owns the symbol, and the
    wash-sale ledger is one row set already read for the cycle. Truncation is
    last, so a name is never dropped for the cost ceiling when a real rule
    would have dropped it anyway.
    """
    thresholds = _thresholds(ctx)
    universe = ctx.get("universe") or {}
    tracked = set((ctx.get("dip_recovery_positions") or {}).keys())
    others = _core_symbols(ctx)
    wash_sale = _wash_sale_symbols(ctx)

    skipped: list[Skipped] = []
    if universe.get("stale", True):
        age = universe.get("snapshot_age_days")
        return WatchlistPool(
            fetch=[],
            skipped=[
                Skipped(symbol, NOT_IN_UNIVERSE, "the universe snapshot is stale; "
                        "membership is unknown and an unknown fence admits nothing")
                for symbol in _unique(ctx.get("scan") or [])
            ],
            reason=(
                "universe snapshot is "
                f"{'unfetched' if age is None else _plural(int(age), 'day') + ' old'}; "
                "no watchlist can be picked against an unknown membership"
            ),
        )

    members = set(universe.get("members") or [])
    # `universe` is `resolve().as_dict()`, so its `sectors` is already
    # `universe.sector_map`'s answer — no second derivation.
    rotation = universes.sector_rotation(
        (ctx.get("control") or {}).get("active_sectors"), ctx.get("etfs") or {},
        ctx.get("rotation_codes"), ctx.get("sub_industries"),
    )
    sectors = universe.get("sectors") or {}
    kept: list[str] = []

    for symbol in _unique(ctx.get("scan") or []):
        if symbol not in members:
            skipped.append(Skipped(symbol, NOT_IN_UNIVERSE,
                f"{symbol} is not in the universe snapshot; the fence admits "
                "only what the issuer holdings file lists"))
        elif symbol in tracked:
            skipped.append(Skipped(symbol, ALREADY_TRACKED,
                f"{symbol} already has a dip_recovery_positions row; the "
                "watchlist is for new entries and Dip Monitor unions the held "
                "names in itself"))
        elif symbol in others:
            skipped.append(Skipped(symbol, SYMBOL_LOCKED,
                f"{symbol} is owned or held by the other strategy; a symbol "
                "belongs to one side at a time"))
        elif symbol in wash_sale:
            skipped.append(Skipped(symbol, RECENT_LOSS_CLOSE,
                f"{symbol} was closed at a loss {wash_sale[symbol]} day(s) "
                "ago; repurchase is blocked account-wide"))
        elif (sector := rotation.inactive(symbol, sectors)) is not None:
            # Before the truncation below, so `max_scan_survivors` is spent
            # on sectors the operator wants rather than on names dropped anyway.
            skipped.append(Skipped(symbol, SECTOR_INACTIVE,
                f"{sector} is outside the active sector rotation"))
        else:
            kept.append(symbol)

    cap = int(thresholds["max_scan_survivors"])
    for symbol in kept[cap:]:
        skipped.append(Skipped(symbol, SCAN_POOL_TRUNCATED,
            f"{symbol} is past the {cap}-symbol fetch ceiling for this pick; "
            "dropped for call cost, not for anything about the symbol"))

    fetch = kept[:cap]
    return WatchlistPool(
        fetch=fetch,
        skipped=skipped,
        reason=(
            f"{_plural(len(fetch), 'symbol')} to fetch from "
            f"{_plural(len(_unique(ctx.get('scan') or [])), 'scan hit')}"
            + (f", {len(kept) - cap} past the fetch ceiling" if len(kept) > cap else "")
        ),
    )


def _unique(symbols: Sequence[str]) -> list[str]:
    """The scan's order, deduplicated. Order is load-bearing exactly once —
    `max_scan_survivors` truncates from the end — so it is preserved rather
    than sorted, and the routine is told to pass `run_scan`'s tickers through
    unreordered."""
    seen: set[str] = set()
    out: list[str] = []
    for symbol in symbols:
        if symbol not in seen:
            seen.add(symbol)
            out.append(symbol)
    return out


def _event_risk(symbol: str, ctx: Mapping[str, Any]) -> Skipped | None:
    """The design's "existing event-risk exclusion", which is guard's own.

    Both branches match `guard._earnings_denials` exactly, including the
    unknown case: guard denies a buy outright when there is no confirmed date
    ("the T-2 flatten is a promise that cannot be kept blind"), so a name with
    no date is one this side could not enter today no matter how well it
    ranked.
    """
    entry = (ctx.get("earnings") or {}).get(symbol) or {}
    sessions = entry.get("sessions_until_report")

    if entry.get("status") != "known" or sessions is None:
        return Skipped(symbol, EVENT_RISK,
            f"no confirmed earnings date for {symbol}; guard denies every buy "
            "without one, so it cannot be entered today either")

    window = int(ctx["config"]["earnings_flatten_days"])
    if int(sessions) <= window:
        return Skipped(symbol, EVENT_RISK,
            f"{symbol} reports in {sessions} session(s), inside the T-{window} "
            "window; averaging down into a print is how a no-loss strategy "
            "gets stuck below cost")

    return None


def _metrics(symbol: str, ctx: Mapping[str, Any]) -> tuple[float, float] | None:
    """`(dollar_volume, atr_pct)`, or ``None`` when any input is missing.

    Fail-closed on a missing input, the same rule `signal._bar_metrics` holds
    to: a symbol whose ATR never arrived is not a symbol with zero volatility.
    """
    price = _price(ctx, symbol)
    atr = (ctx.get("signals") or {}).get(symbol, {}).get("atr")
    average_volume = (ctx.get("fundamentals") or {}).get(symbol, {}).get("average_volume")

    if not price or atr is None or average_volume is None:
        return None

    return float(average_volume) * price, float(atr) / price * 100.0


def _rank(values: list[float], value: float) -> float:
    """Where `value` sits in `values`, 0.0 (lowest) to 1.0 (highest).

    A percentile rather than a scaled magnitude because the two factors do not
    share a scale — dollar volume spans orders of magnitude across the S&P and
    ATR% spans a handful of points, so any fixed normalisation would make one
    of them decide every pick. Ties take the lower rank (the count of strictly
    smaller values), so two identical numbers can never order by accident.
    """
    if len(values) < 2:
        return 1.0
    return sum(1 for other in values if other < value) / (len(values) - 1)


def pick(ctx: Mapping[str, Any]) -> WatchlistPick:
    """The fetched pool, ranked down to `watchlist_size` names.

    `skipped` carries every rejection with its reason, which is the design's
    "every rejection reason logged" — including the ones that lost purely on
    rank, since "nothing was wrong with it, four names simply scored higher"
    is the answer to the most common question this routine will ever be asked.
    """
    thresholds = _thresholds(ctx)
    trading_day = _trading_day(ctx)

    skipped: list[Skipped] = []
    measured: list[tuple[str, float, float]] = []

    for symbol in _unique(ctx.get("fetch") or []):
        risk = _event_risk(symbol, ctx)
        if risk is not None:
            skipped.append(risk)
            continue

        metrics = _metrics(symbol, ctx)
        if metrics is None:
            skipped.append(Skipped(symbol, METRICS_UNAVAILABLE,
                f"{symbol} is missing a price, ATR or average volume; a "
                "missing factor is not a zero one"))
            continue

        dollar_volume, atr_pct = metrics
        if dollar_volume < float(thresholds["min_dollar_volume"]):
            skipped.append(Skipped(symbol, INSUFFICIENT_LIQUIDITY,
                f"{symbol} turns over ${dollar_volume:,.0f} a day against a "
                f"${float(thresholds['min_dollar_volume']):,.0f} floor; a whole "
                "position has to exit in one limit order"))
            continue

        if not (float(thresholds["min_atr_pct"]) <= atr_pct <= float(thresholds["max_atr_pct"])):
            skipped.append(Skipped(symbol, ATR_OUT_OF_RANGE,
                f"{symbol}'s 20-day ATR is {atr_pct:.2f}% of price, outside the "
                f"{float(thresholds['min_atr_pct']):g}-"
                f"{float(thresholds['max_atr_pct']):g}% band this side ranks in"))
            continue

        measured.append((symbol, dollar_volume, atr_pct))

    volumes = [row[1] for row in measured]
    atr_pcts = [row[2] for row in measured]

    ranked = sorted(
        (
            {
                "symbol": symbol,
                "dollar_volume": round(dollar_volume, 2),
                "atr_pct": round(atr_pct, 4),
                "liquidity_rank": round(_rank(volumes, dollar_volume), 4),
                "volatility_rank": round(_rank(atr_pcts, atr_pct), 4),
                "score": round(
                    100.0
                    * (
                        _LIQUIDITY_WEIGHT * _rank(volumes, dollar_volume)
                        + _VOLATILITY_WEIGHT * _rank(atr_pcts, atr_pct)
                    ),
                    4,
                ),
            }
            for symbol, dollar_volume, atr_pct in measured
        ),
        # Symbol breaks a score tie, so two runs over the same data cannot
        # disagree about which name made the cut.
        key=lambda row: (-row["score"], row["symbol"]),
    )

    size = int(thresholds["watchlist_size"])
    symbols = [row["symbol"] for row in ranked[:size]]

    for row in ranked[size:]:
        skipped.append(Skipped(row["symbol"], RANK_NOT_TOP,
            f"{row['symbol']} scored {row['score']:.1f} and placed outside the "
            f"top {size}; nothing is wrong with it, the cut is a fixed width"))

    return WatchlistPick(
        symbols=symbols,
        ranked=ranked,
        skipped=skipped,
        control_updates={WATCHLIST_KEY: {"trading_day": trading_day, "symbols": symbols}},
        reason=(
            f"{_plural(len(symbols), 'symbol')} picked from "
            f"{_plural(len(measured), 'ranked candidate')}, "
            f"{_plural(len(skipped), 'rejection')} logged"
        ),
    )
