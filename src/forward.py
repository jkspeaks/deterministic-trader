"""forward.py — forward returns and price-history features from daily bars.

The decision snapshot
(`src/snapshot.py`) records what every candidate looked like when `decide`
judged it; this module answers the two questions that can only be answered
later, from daily bars:

* **what happened next** — 1/3/5/8/21-session forward returns, measured two
  ways: close-to-close from the decision day's close, and from the **next
  session's open** (the program's point-in-time fill rule);
* **what the price history said at the time** — 12-1 momentum, residual
  momentum, relative strength against the sector ETF, distance from the
  52-week high — from bars **strictly before** the decision day, so nothing a
  cycle could not have known leaks in.

Pure: bars in, numbers out. `services/core/core/evidence.py` does the fetching.

**A number is never guessed.** Three situations look like a return and are not:

* the horizon has not happened yet — `PENDING`, tried again later;
* a bar the arithmetic needs is absent (a halt, a delisting, a vendor gap) —
  `MISSING_BAR`. Sessions are counted on the **market's** calendar, not on the
  symbol's own bars: stepping over a halted day would silently turn a
  five-session return into a six-session one;
* the window crosses what looks like an unadjusted split — `SPLIT_SUSPECT`. A
  2-for-1 reads as -50% on a series that was not adjusted, and one such row
  would dominate an average of a few hundred.

Each leaves the return `None` and says why in `status`.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any, Mapping, Sequence

from src import cadence

HORIZONS = (1, 3, 5, 8, 21)

OK = "ok"
PENDING = "pending"
MISSING_BAR = "missing_bar"
SPLIT_SUSPECT = "split_suspect"
# A status that will never change, however many more times the row is visited.
FINAL = frozenset({OK, SPLIT_SUSPECT})

# How long after a horizon's own date a missing bar is still worth retrying. A
# vendor that is a day late fills in; a delisted name never does, and without
# a bound its row would be re-fetched every night for ever.
MISSING_GRACE_DAYS = 10

# Split ratios a daily series plausibly shows when it was not adjusted, as
# new-price / old-price. Forward splits and their reverses.
_SPLIT_RATIOS = tuple(
    ratio
    for n, m in ((2, 1), (3, 1), (4, 1), (5, 1), (10, 1), (20, 1), (3, 2), (5, 4))
    for ratio in (m / n, n / m)
)
# How close the overnight gap must sit to one of those ratios, and how large a
# move has to be before it is compared at all. A 3-for-2 is a 33% drop; nothing
# smaller than a 5-for-4 (20%) is in the list.
_SPLIT_TOLERANCE = 0.03
_SPLIT_MIN_MOVE = 0.18

# Trading-day lookbacks, the conventional ones (and the ones
# `ops/factor_study.py` measured offline, so the live and offline panels agree).
MOM_SKIP = 21
MOM_12M = 252
MOM_6M = 126
RS_3M = 63
HIGH_52W = 252
HIGH_52W_MIN = 200
VOL_60D = 60


# ---------------------------------------------------------------------------
# Bars
# ---------------------------------------------------------------------------


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def bars_from(rows: Sequence[Mapping[str, Any]] | None) -> list[dict]:
    """Vendor rows as `{"date", "open", "high", "low", "close"}`, oldest first.

    Accepts Twelve Data's `{"datetime": ..., "open": "12.3", ...}` and the
    already-normalised shape. A bar with no date or no positive close is
    dropped — it cannot anchor a return. A missing open/high/low is kept as
    `None`: close-to-close arithmetic does not need them, and the
    next-open return says `MISSING_BAR` on its own when the open is absent.
    One bar per date; a repeated date keeps the later row.
    """
    by_day: dict[str, dict] = {}
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        day = str(row.get("date") or row.get("datetime") or "")[:10]
        close = _number(row.get("close"))
        if len(day) != 10 or close is None or close <= 0.0:
            continue
        by_day[day] = {
            "date": day,
            "open": _number(row.get("open")),
            "high": _number(row.get("high")),
            "low": _number(row.get("low")),
            "close": close,
        }
    return [by_day[day] for day in sorted(by_day)]


def sessions_after(day: str, count: int, calendar: Mapping[str, Any]) -> list[str]:
    """The next ``count`` open sessions strictly after ``day``, oldest first."""
    cursor = date.fromisoformat(day)
    found: list[str] = []
    # Bounded: `count` sessions are never more than `count * 2 + 15` calendar
    # days away, weekends and a holiday cluster included.
    for _ in range(count * 2 + 15):
        if len(found) >= count:
            break
        cursor += timedelta(days=1)
        if cadence.is_open(cursor, calendar):
            found.append(cursor.isoformat())
    return found


def sessions_elapsed(day: str, today: str, calendar: Mapping[str, Any]) -> int:
    """Open sessions strictly after ``day`` and strictly before ``today`` —
    the sessions whose daily bar can exist when a job runs on ``today``."""
    last = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    if last <= day:
        return 0
    return cadence.sessions_between(
        date.fromisoformat(day), date.fromisoformat(last), calendar
    )


# ---------------------------------------------------------------------------
# Forward returns
# ---------------------------------------------------------------------------


def _split_between(bars: Sequence[Mapping[str, Any]], start: str, end: str) -> bool:
    """Whether any overnight gap in ``(start, end]`` looks like an unadjusted
    split: the open against the prior close sits on a split ratio.

    The open rather than the close, because a split lands at the open exactly
    and the close has a day of ordinary movement on top. Where the open is
    absent the close stands in — a looser test, and the direction that flags
    more rather than fewer.
    """
    previous: Mapping[str, Any] | None = None
    for bar in bars:
        if bar["date"] > end:
            break
        if previous is not None and bar["date"] > start:
            price = bar["open"] if bar.get("open") else bar["close"]
            ratio = price / previous["close"]
            if abs(ratio - 1.0) >= _SPLIT_MIN_MOVE and any(
                abs(ratio / known - 1.0) <= _SPLIT_TOLERANCE for known in _SPLIT_RATIOS
            ):
                return True
        previous = bar
    return False


def forward_returns(
    bars: Sequence[Mapping[str, Any]],
    decision_day: str,
    calendar: Mapping[str, Any],
    today: str,
    horizons: Sequence[int] = HORIZONS,
) -> dict:
    """`{"fwd": {"cc_1": ..., "no_1": ...}, "status": {"1": ...}, "complete": bool}`.

    * ``cc_h`` — close of session ``h`` after the decision day over the
      decision day's own close, minus one.
    * ``no_h`` — the same close over the **next session's open**: what a buy
      placed after the decision and filled at the next open would have made.

    ``today`` is the day the job runs; a session counts as having happened
    only once it is strictly before ``today``, so a bar still being printed is
    never read as final.

    ``complete`` is true once every horizon has a status that cannot change:
    a value, a suspected split, or a missing bar past `MISSING_GRACE_DAYS`.
    """
    by_day = {bar["date"]: bar for bar in bars}
    sessions = sessions_after(decision_day, max(horizons), calendar)
    base = by_day.get(decision_day)
    first = by_day.get(sessions[0]) if sessions else None
    cutoff = date.fromisoformat(today)

    fwd: dict[str, float | None] = {}
    status: dict[str, str] = {}
    complete = True
    for horizon in horizons:
        key = str(horizon)
        fwd[f"cc_{horizon}"] = None
        fwd[f"no_{horizon}"] = None
        target_day = sessions[horizon - 1] if len(sessions) >= horizon else None
        if target_day is None or date.fromisoformat(target_day) >= cutoff:
            status[key] = PENDING
            complete = False
            continue

        target = by_day.get(target_day)
        if base is None or target is None:
            status[key] = MISSING_BAR
            if (cutoff - date.fromisoformat(target_day)).days <= MISSING_GRACE_DAYS:
                complete = False
            continue
        if _split_between(bars, decision_day, target_day):
            status[key] = SPLIT_SUSPECT
            continue

        status[key] = OK
        fwd[f"cc_{horizon}"] = round(target["close"] / base["close"] - 1.0, 6)
        # The next-open leg is absent, not zero, when the entry session printed
        # no open: the close-to-close leg above is still a real measurement.
        if first is not None and first.get("open"):
            fwd[f"no_{horizon}"] = round(target["close"] / first["open"] - 1.0, 6)

    return {"fwd": fwd, "status": status, "complete": complete}


# ---------------------------------------------------------------------------
# Price-history features, as of the decision
# ---------------------------------------------------------------------------


def _before(bars: Sequence[Mapping[str, Any]], decision_day: str) -> list[Mapping[str, Any]]:
    return [bar for bar in bars if bar["date"] < decision_day]


def _ratio(closes: Sequence[float], recent: int, old: int) -> float | None:
    """``closes[-1 - recent] / closes[-1 - old] - 1``, or None when short."""
    if len(closes) <= old:
        return None
    return closes[-1 - recent] / closes[-1 - old] - 1.0


def _std(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def _residual_momentum(
    stock: Sequence[Mapping[str, Any]], market: Sequence[Mapping[str, Any]]
) -> tuple[float | None, float | None]:
    """12-1 momentum with the market's part taken out, and the same scaled by
    its own noise.

    Daily returns of the stock regressed on the market's over the 12-1 window
    (the last `MOM_SKIP` sessions excluded, as for raw momentum), matched by
    date. The first number is the sum of ``r - beta * m`` — what the name did
    beyond what its beta explains; the second divides that by
    ``std(residual) * sqrt(n)``, the standardisation Blitz, Huij and Martens
    use, so a quiet name's drift and a noisy name's are comparable.

    `(None, None)` when fewer than 200 matched days exist or the market did
    not move: a beta from a short window is a different statistic.
    """
    market_close = {bar["date"]: bar["close"] for bar in market}
    days = [bar["date"] for bar in stock if bar["date"] in market_close]
    closes = {bar["date"]: bar["close"] for bar in stock}
    if len(days) <= MOM_SKIP + 1:
        return None, None
    window = days[-(MOM_12M + 1) : len(days) - MOM_SKIP]
    pairs = [
        (closes[b] / closes[a] - 1.0, market_close[b] / market_close[a] - 1.0)
        for a, b in zip(window, window[1:])
    ]
    if len(pairs) < 200:
        return None, None

    mean_r = sum(r for r, _ in pairs) / len(pairs)
    mean_m = sum(m for _, m in pairs) / len(pairs)
    var_m = sum((m - mean_m) ** 2 for _, m in pairs)
    if var_m <= 0.0:
        return None, None
    beta = sum((r - mean_r) * (m - mean_m) for r, m in pairs) / var_m

    excess = [r - beta * m for r, m in pairs]
    total = sum(excess)
    noise = _std(excess)
    scaled = None if not noise else total / (noise * math.sqrt(len(excess)))
    return total, scaled


def price_features(
    bars: Sequence[Mapping[str, Any]],
    decision_day: str,
    market: Sequence[Mapping[str, Any]] | None = None,
    sector: Sequence[Mapping[str, Any]] | None = None,
) -> dict:
    """What the daily bars said about a name **before** ``decision_day``.

    Every key is always present; a figure the history is too short for is
    `None`, never imputed (the program's rule for any factor: absent, not
    filled in). ``bars_used`` and ``last_bar`` say what the figures were read
    from, so a row computed from a thin history can be told apart later.

    ``market`` is SPY's bars and ``sector`` the name's sector ETF's; without
    them the two relative figures are `None` and the rest stand.
    """
    history = _before(bars, decision_day)
    closes = [bar["close"] for bar in history]
    out: dict[str, Any] = {
        "bars_used": len(closes),
        "last_bar": history[-1]["date"] if history else None,
        "mom_12_1": _ratio(closes, MOM_SKIP, MOM_12M),
        "mom_6_1": _ratio(closes, MOM_SKIP, MOM_6M),
        "ret_1m": _ratio(closes, 0, MOM_SKIP),
        "ret_3m": _ratio(closes, 0, RS_3M),
        "rs_3m_vs_sector": None,
        "rs_1m_vs_sector": None,
        "dist_52w_high": None,
        "vol_60d": None,
        "resid_mom_12_1": None,
        "resid_mom_12_1_z": None,
    }

    if len(closes) >= HIGH_52W_MIN:
        out["dist_52w_high"] = closes[-1] / max(closes[-HIGH_52W:]) - 1.0

    if len(closes) > VOL_60D:
        recent = closes[-(VOL_60D + 1) :]
        out["vol_60d"] = _std([b / a - 1.0 for a, b in zip(recent, recent[1:])])

    if sector:
        sector_closes = [bar["close"] for bar in _before(sector, decision_day)]
        for key, own, span in (
            ("rs_3m_vs_sector", out["ret_3m"], RS_3M),
            ("rs_1m_vs_sector", out["ret_1m"], MOM_SKIP),
        ):
            theirs = _ratio(sector_closes, 0, span)
            if own is not None and theirs is not None:
                out[key] = own - theirs

    if market:
        out["resid_mom_12_1"], out["resid_mom_12_1_z"] = _residual_momentum(
            history, _before(market, decision_day)
        )

    return {
        key: round(value, 6) if isinstance(value, float) else value
        for key, value in out.items()
    }


# ---------------------------------------------------------------------------
# Analyst estimates, as of the fetch
# ---------------------------------------------------------------------------

# The S2 factor 12 reading. The fiscal period both readings are taken on.
ESTIMATE_PERIOD = "next_year"


def _period(rows: Sequence[Mapping[str, Any]] | None, period: str) -> Mapping[str, Any]:
    for row in rows or []:
        if isinstance(row, Mapping) and row.get("period") == period:
            return row
    return {}


def estimate_features(
    trend: Sequence[Mapping[str, Any]] | None,
    revisions: Sequence[Mapping[str, Any]] | None,
) -> dict:
    """The two registered readings of estimate revisions, from Twelve Data's
    `eps_trend` and `eps_revisions` rows for one name.

    * ``eps_revision_90_7`` — the next-fiscal-year consensus 7 days before the
      fetch over the same 90 days before it, minus one. Both dates precede a
      decision made the day before the fetch. None when either is missing or
      the older estimate is not positive: a change from a loss to a smaller
      loss is not a percentage anyone can rank.
    * ``revision_breadth_1m`` — (up minus down) over (up plus down) revisions
      in the last month. None when nobody revised: no revisions is not the
      same finding as revisions that cancel.

    Both keys are always present; absent is `None`, never zero.
    """
    row = _period(trend, ESTIMATE_PERIOD)
    recent, base = _number(row.get("7_days_ago")), _number(row.get("90_days_ago"))
    change = None
    if recent is not None and base is not None and base > 0.0:
        change = round(recent / base - 1.0, 6)

    row = _period(revisions, ESTIMATE_PERIOD)
    up, down = _number(row.get("up_last_month")), _number(row.get("down_last_month"))
    breadth = None
    if up is not None and down is not None and up + down > 0.0:
        breadth = round((up - down) / (up + down), 6)

    return {"eps_revision_90_7": change, "revision_breadth_1m": breadth}


# Alpha Vantage's `EARNINGS_ESTIMATES` rows carry both readings on one row
# per fiscal period.
AV_FISCAL_YEAR = "fiscal year"


def fiscal_years_ahead(rows: Sequence[Mapping[str, Any]] | None, today: str) -> list[dict]:
    """The vendor's fiscal-year rows whose year ends after ``today``, nearest
    first. The first is the fiscal year in progress; the second is the "next
    fiscal year" the registered readings are taken on."""
    ahead = [
        dict(row) for row in rows or []
        if isinstance(row, Mapping) and row.get("horizon") == AV_FISCAL_YEAR
        and len(str(row.get("date") or "")) >= 10 and str(row["date"])[:10] > today[:10]
    ]
    return sorted(ahead, key=lambda row: str(row["date"]))


def estimate_features_alphavantage(rows: Sequence[Mapping[str, Any]] | None, today: str) -> dict:
    """`estimate_features`, read from Alpha Vantage's rows: the same two
    readings, on the same fiscal period, under the same keys.

    ``fiscal_year_end`` says which year they were read from, or is None when
    the vendor lists fewer than two fiscal years ahead — then both readings
    are None: the year in progress is not the registered period, and using it
    because it is the only one there would change the factor for some names
    and not others. A revision count the vendor sends as null is unknown, not
    zero, and leaves the breadth None.
    """
    ahead = fiscal_years_ahead(rows, today)
    row = ahead[1] if len(ahead) > 1 else {}
    recent = _number(row.get("eps_estimate_average_7_days_ago"))
    base = _number(row.get("eps_estimate_average_90_days_ago"))
    change = None
    if recent is not None and base is not None and base > 0.0:
        change = round(recent / base - 1.0, 6)

    up = _number(row.get("eps_estimate_revision_up_trailing_30_days"))
    down = _number(row.get("eps_estimate_revision_down_trailing_30_days"))
    breadth = None
    if up is not None and down is not None and up + down > 0.0:
        breadth = round((up - down) / (up + down), 6)

    return {
        "eps_revision_90_7": change,
        "revision_breadth_1m": breadth,
        "fiscal_year_end": str(row["date"])[:10] if row else None,
    }
