#!/usr/bin/env python3
"""ops/eval_selection.py — one evaluation path for selection evidence.

Two things produce evidence
about whether the system picks stocks better than chance:

* **offline** — `ops/placebo_backtest.py` and `ops/factor_study.py`, over
  years of daily bars in `.placebo_cache/`;
* **live** — the decision snapshot (`edge_decision_snapshot`: every candidate
  a cycle judged, its features, its forward returns) and the control arm in
  `edge_shadow_trades` (each signal beside a seeded random same-sector name).

They must be judged by the same arithmetic, or a result that appears offline
and not live is uninterpretable: it could be the market or it could be the
ruler. So the statistics live here once — rank IC, quantile spreads, turnover,
the date-clustered paired bootstrap, the trial-ledger deflation, the power
arithmetic — and both paths call them.

**Standard library only**, on purpose. The repo's test suite has no numpy and
the hosted core must never grow one; a statistic the suite cannot run is a
statistic nobody checks. The offline scripts keep numpy for building their
panels and hand plain sequences to the functions here.

Run:

    uv run --group control python ops/eval_selection.py live --from 2026-10-05
    uv run python ops/eval_selection.py power --sd 0.0465 --rate 1.0
    uv run python ops/eval_selection.py ledger

`live` reads Supabase through `ops/supabase_client.py` (the `control`
dependency group, the same credentials `ops/control.py` uses). It reports
what the evidence supports **and how many trials stand behind it**: every
significance figure is shown raw and deflated by the count in the trial ledger.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
from datetime import date, timedelta
from pathlib import Path
from statistics import NormalDist
from typing import Any, Callable, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "data" / "trial-ledger.csv"
LEDGER_FIELDS = ("date", "phase", "variant", "n", "metric", "result", "note")

BOOT = 10_000
SEED = 20261004

# The program's pre-registered bar for a factor (rules block): |t| >= 3.0 on
# the sector-neutral IC and the same sign in at least 4 of 5 calendar years.
T_BAR = 3.0
YEARS_BAR = (4, 5)

_NORMAL = NormalDist()


# ---------------------------------------------------------------------------
# Small numerics
# ---------------------------------------------------------------------------


def finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def stdev(values: Sequence[float]) -> float:
    """Sample standard deviation (n - 1). 0.0 for fewer than two values."""
    if len(values) < 2:
        return 0.0
    m = mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def t_stat(values: Sequence[float]) -> float | None:
    """mean / standard error. None where it is undefined."""
    sd = stdev(values)
    if len(values) < 2 or sd == 0.0:
        return None
    return mean(values) / (sd / math.sqrt(len(values)))


def p_two_sided(t: float | None) -> float | None:
    """Normal approximation. Honest for the hundreds of dates these tests
    run over; overstates significance for a handful, which `n` shows."""
    return None if t is None else 2.0 * (1.0 - _NORMAL.cdf(abs(t)))


def ranks(values: Sequence[float]) -> list[float]:
    """Average ranks, 1-based; ties share the mean of the ranks they span."""
    order = sorted(range(len(values)), key=values.__getitem__)
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[order[k]] = shared
        i = j + 1
    return out


def pearson(x: Sequence[float], y: Sequence[float]) -> float | None:
    if len(x) != len(y) or len(x) < 3:
        return None
    mx, my = mean(x), mean(y)
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx == 0.0 or syy == 0.0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / math.sqrt(sxx * syy)


def spearman(x: Sequence[float], y: Sequence[float]) -> float | None:
    """Rank correlation; None for fewer than three pairs or a constant side."""
    return pearson(ranks(x), ranks(y))


# ---------------------------------------------------------------------------
# Cross-sections
# ---------------------------------------------------------------------------


def demean_by(values: Sequence[float], groups: Sequence[Any]) -> list[float]:
    """Each value minus the mean of its group — sector-neutralising a return.

    A group of one is its own mean and comes back 0.0: a name alone in its
    sector on a date carries no within-sector information, and leaving its raw
    return in would smuggle the sector's return into a "neutral" statistic.
    """
    totals: dict[Any, list[float]] = {}
    for value, group in zip(values, groups):
        bucket = totals.setdefault(group, [0.0, 0.0])
        bucket[0] += value
        bucket[1] += 1.0
    return [value - totals[group][0] / totals[group][1] for value, group in zip(values, groups)]


def cross_section(
    factor: Sequence[Any],
    forward: Sequence[Any],
    sectors: Sequence[Any] | None = None,
    *,
    min_names: int = 20,
    quantiles: int = 5,
) -> dict | None:
    """One date's statistics: the IC and the top-minus-bottom quantile spread.

    Pairs with a missing side are dropped, never filled. With ``sectors`` the
    forward return is demeaned within sector first (over the pairs that
    survive, so the neutralisation uses the same names the IC does). None when
    fewer than ``min_names`` pairs remain.
    """
    rows = [
        (x, y, sectors[i] if sectors is not None else None, i)
        for i, (raw_x, raw_y) in enumerate(zip(factor, forward))
        if (x := finite(raw_x)) is not None and (y := finite(raw_y)) is not None
    ]
    if len(rows) < min_names:
        return None
    f = [row[0] for row in rows]
    r = [row[1] for row in rows]
    if sectors is not None:
        r = demean_by(r, [row[2] for row in rows])

    ic = spearman(f, r)
    if ic is None:
        return None
    order = sorted(range(len(f)), key=f.__getitem__)
    k = len(order) // quantiles
    spread = top = None
    top_rows: list[int] = []
    if k >= 1:
        top = mean([r[i] for i in order[-k:]])
        spread = top - mean([r[i] for i in order[:k]])
        # Positions in the caller's own sequences, so it can look up what the
        # top bucket's names cost to trade (S2's net long edge).
        top_rows = [rows[i][3] for i in order[-k:]]
    return {"ic": ic, "spread": spread, "top": top, "top_rows": top_rows, "n": len(rows)}


def sector_z(
    values: Sequence[Any],
    sectors: Sequence[Any],
    *,
    clip: float = 3.0,
    min_group: int = 5,
) -> list[float | None]:
    """Each value as a z-score within its sector, winsorized at ``clip``.

    S2's sector-neutral score: a name is compared with
    its own sector's names that day and nobody else's, so a sector's beta
    cannot pass for selection. None where the value is missing, where the
    sector has fewer than ``min_group`` valued names, or where they are all
    equal — a z-score from three names, or from no spread, ranks nothing.
    """
    groups: dict[Any, list[tuple[int, float]]] = {}
    for i, (raw, sector) in enumerate(zip(values, sectors)):
        value = finite(raw)
        if value is not None and sector is not None:
            groups.setdefault(sector, []).append((i, value))
    out: list[float | None] = [None] * len(values)
    for members in groups.values():
        if len(members) < min_group:
            continue
        numbers = [v for _, v in members]
        sd = stdev(numbers)
        if sd == 0.0:
            continue
        m = mean(numbers)
        for i, value in members:
            out[i] = max(-clip, min(clip, (value - m) / sd))
    return out


def rank_average(columns: Sequence[Sequence[float | None]], *, min_share: float = 0.5) -> list[float | None]:
    """The composite: each column ranked across the names that have it, the
    ranks scaled to 0..1 and averaged per name with equal weight.

    A name is scored on the columns it has, provided it has at least
    ``min_share`` of them; otherwise None. No fitted weight exists here to
    overfit: a column's only parameter is whether it is in the list.
    """
    if not columns:
        return []
    n = len(columns[0])
    totals = [0.0] * n
    counts = [0] * n
    for column in columns:
        have = [i for i, v in enumerate(column) if finite(v) is not None]
        if len(have) < 2:
            continue
        scaled = ranks([float(column[i]) for i in have])
        for i, rank in zip(have, scaled):
            totals[i] += (rank - 1.0) / (len(have) - 1.0)
            counts[i] += 1
    need = max(1, math.ceil(min_share * len(columns)))
    return [totals[i] / counts[i] if counts[i] >= need else None for i in range(n)]


def ic_summary(ics: Sequence[float]) -> dict:
    """Mean IC, its dispersion, ICIR (mean / sd), t, and the share positive."""
    if not ics:
        return {"n": 0, "mean": None, "sd": None, "icir": None, "t": None, "hit": None}
    sd = stdev(ics)
    return {
        "n": len(ics),
        "mean": mean(ics),
        "sd": sd,
        "icir": None if sd == 0.0 else mean(ics) / sd,
        "t": t_stat(ics),
        "hit": sum(1 for v in ics if v > 0) / len(ics),
    }


def sign_by_year(dated: Iterable[tuple[str, float]]) -> dict[str, float]:
    """Mean of a dated series per calendar year — the 4-of-5-years test."""
    by_year: dict[str, list[float]] = {}
    for day, value in dated:
        by_year.setdefault(str(day)[:4], []).append(value)
    return {year: mean(values) for year, values in sorted(by_year.items())}


def passes_bar(summary: Mapping[str, Any], years: Mapping[str, float]) -> bool:
    """The pre-registered bar: |t| >= `T_BAR`, and the sign of the mean IC
    repeated in at least 4 of the last 5 calendar years present."""
    t = summary.get("t")
    if t is None or abs(t) < T_BAR or not years:
        return False
    sign = 1.0 if summary["mean"] > 0 else -1.0
    last = list(years.values())[-YEARS_BAR[1]:]
    return len(last) >= YEARS_BAR[1] and sum(1 for v in last if v * sign > 0) >= YEARS_BAR[0]


def top_quantile_turnover(
    by_date: Mapping[str, Mapping[str, float]], quantiles: int = 5
) -> float | None:
    """Mean share of the top quantile replaced from one date to the next.

    ``by_date`` is `{date: {symbol: factor}}`. 0.0 is a list that never
    changes, 1.0 one that is entirely new each time. None with fewer than two
    usable dates. This is what the program nets against costs: a factor whose
    top bucket churns every rebalance pays the spread every rebalance.
    """
    tops: list[set[str]] = []
    for day in sorted(by_date):
        scored = [(s, v) for s, raw in by_date[day].items() if (v := finite(raw)) is not None]
        k = len(scored) // quantiles
        if k < 1:
            continue
        scored.sort(key=lambda pair: (pair[1], pair[0]))
        tops.append({symbol for symbol, _ in scored[-k:]})
    if len(tops) < 2:
        return None
    return mean([len(now - before) / len(now) for before, now in zip(tops, tops[1:])])


# ---------------------------------------------------------------------------
# Paired comparison against the control
# ---------------------------------------------------------------------------


def clustered_bootstrap(
    diffs: Sequence[Any],
    clusters: Sequence[Any],
    *,
    boot: int = BOOT,
    seed: int = SEED,
    min_n: int = 20,
) -> dict | None:
    """Mean paired difference with a cluster (date) bootstrap CI.

    Same-day events share the day's market move, so they are resampled
    together: whole dates are drawn with replacement and the mean is the ratio
    of the drawn dates' summed differences to their summed counts. Treating
    1,500 events on 900 dates as 1,500 independent draws is how a null result
    acquires a tight interval.

    Returns `{"n", "clusters", "mean", "lo", "hi", "p_le_0"}` — a 95%
    percentile interval and the share of resampled means at or below zero —
    or None when fewer than ``min_n`` finite differences exist.
    """
    sums: dict[Any, float] = {}
    counts: dict[Any, int] = {}
    total, n = 0.0, 0
    for raw, cluster in zip(diffs, clusters):
        value = finite(raw)
        if value is None:
            continue
        sums[cluster] = sums.get(cluster, 0.0) + value
        counts[cluster] = counts.get(cluster, 0) + 1
        total += value
        n += 1
    if n < min_n:
        return None

    keys = sorted(sums, key=str)
    s = [sums[key] for key in keys]
    c = [counts[key] for key in keys]
    k = len(keys)
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(boot):
        picks = rng.choices(range(k), k=k)
        means.append(sum(map(s.__getitem__, picks)) / sum(map(c.__getitem__, picks)))
    means.sort()
    return {
        "n": n,
        "clusters": k,
        "mean": total / n,
        "lo": means[int(0.025 * (boot - 1))],
        "hi": means[int(0.975 * (boot - 1))],
        "p_le_0": sum(1 for m in means if m <= 0.0) / boot,
    }


def clustered_bootstrap_difference(
    values: Sequence[Any],
    in_group: Sequence[bool],
    clusters: Sequence[Any],
    *,
    boot: int = BOOT,
    seed: int = SEED,
    min_n: int = 20,
) -> dict | None:
    """Mean of the events in a group minus the mean of the rest, with a
    cluster (date) bootstrap CI.

    Whole dates are drawn with replacement and both means are recomputed from
    the drawn dates, so a day that carries events of both kinds moves both
    means together, which is what a same-day market move does. A draw that
    leaves either side empty is drawn again.

    Returns `{"n_in", "n_out", "mean_in", "mean_out", "diff", "lo", "hi",
    "se", "t", "p_le_0"}`, or None when either side has fewer than ``min_n``
    finite values.
    """
    cells: dict[Any, list[float]] = {}
    totals = [0.0, 0, 0.0, 0]
    for raw, flag, cluster in zip(values, in_group, clusters):
        value = finite(raw)
        if value is None:
            continue
        cell = cells.setdefault(cluster, [0.0, 0, 0.0, 0])
        k = 0 if flag else 2
        cell[k] += value
        cell[k + 1] += 1
        totals[k] += value
        totals[k + 1] += 1
    if totals[1] < min_n or totals[3] < min_n:
        return None

    rows = [cells[key] for key in sorted(cells, key=str)]
    k = len(rows)
    rng = random.Random(seed)
    diffs: list[float] = []
    while len(diffs) < boot:
        a = b = 0.0
        na = nb = 0
        for i in rng.choices(range(k), k=k):
            row = rows[i]
            a += row[0]
            na += row[1]
            b += row[2]
            nb += row[3]
        if na and nb:
            diffs.append(a / na - b / nb)
    diffs.sort()
    mean_in, mean_out = totals[0] / totals[1], totals[2] / totals[3]
    se = stdev(diffs)
    return {
        "n_in": totals[1],
        "n_out": totals[3],
        "mean_in": mean_in,
        "mean_out": mean_out,
        "diff": mean_in - mean_out,
        "lo": diffs[int(0.025 * (boot - 1))],
        "hi": diffs[int(0.975 * (boot - 1))],
        "se": se,
        "t": (mean_in - mean_out) / se if se > 0.0 else None,
        "p_le_0": sum(1 for d in diffs if d <= 0.0) / boot,
    }


def deoverlap(events: Sequence[Mapping[str, Any]], hold_days: int, *, symbol: str = "symbol",
              day: str = "entry_day") -> list[Mapping[str, Any]]:
    """Keep a symbol's first event and drop any that follow within
    ``hold_days`` calendar days of the last one kept.

    A name that stays oversold for a week signals every day of it; those are
    one trade seen five times, and counting them five times narrows an
    interval that should stay wide. Calendar days, because the callers hold
    ISO dates and no market calendar.
    """
    last: dict[str, date] = {}
    kept = []
    for event in sorted(events, key=lambda e: (str(e[day]), str(e[symbol]))):
        when = date.fromisoformat(str(event[day])[:10])
        prior = last.get(str(event[symbol]))
        if prior is not None and (when - prior) <= timedelta(days=hold_days):
            continue
        last[str(event[symbol])] = when
        kept.append(event)
    return kept


CLOSED_REASONS = frozenset({"STOP", "TARGET", "TIME_STOP", "SIGNAL"})


def control_pairs(
    shadow_rows: Sequence[Mapping[str, Any]],
    signal_variant: str = "R_signal_norsi",
    control_variant: str = "R_random_sector_norsi",
) -> list[dict]:
    """Each closed signal trade joined to its closed stand-in.

    The join is the ledger's own: a control row's `pair_symbol` is its
    signal's symbol, on the same `entry_day`. A pair with either leg open,
    unpriced or absent is left out — and counted by the caller from the
    lengths, never assumed away.
    """
    def closed(row: Mapping[str, Any]) -> bool:
        return row.get("reason") in CLOSED_REASONS and finite(row.get("pnl_pct")) is not None

    signals = {
        (str(r["symbol"]), str(r["entry_day"])[:10]): r
        for r in shadow_rows if r.get("variant") == signal_variant and closed(r)
    }
    pairs = []
    for row in shadow_rows:
        if row.get("variant") != control_variant or not closed(row):
            continue
        key = (str(row.get("pair_symbol") or ""), str(row["entry_day"])[:10])
        signal = signals.get(key)
        if signal is None:
            continue
        pairs.append({
            "symbol": key[0],
            "control": row["symbol"],
            "entry_day": key[1],
            "play_id": signal.get("play_id"),
            "signal": float(signal["pnl_pct"]),
            "control_ret": float(row["pnl_pct"]),
            "diff": float(signal["pnl_pct"]) - float(row["pnl_pct"]),
        })
    return pairs


# ---------------------------------------------------------------------------
# The trial ledger and deflation
# ---------------------------------------------------------------------------


def ledger_rows(path: Path = LEDGER) -> list[dict]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return [row for row in csv.DictReader(handle) if row.get("variant")]


def ledger_count(path: Path = LEDGER, phase: str | None = None) -> int:
    """How many trials the program has run — the deflation's denominator.

    Every row counts, across phases: a hypothesis tested in S1 is still a
    look at the data when S3 reports a p-value. ``phase`` narrows it for
    display only.
    """
    return sum(1 for row in ledger_rows(path) if phase is None or row.get("phase") == phase)


def ledger_append(row: Mapping[str, Any], path: Path = LEDGER) -> None:
    """Append one trial. Creates the file with its header if absent."""
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.is_file()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=LEDGER_FIELDS, lineterminator="\n")
        if new:
            writer.writeheader()
        writer.writerow({field: row.get(field, "") for field in LEDGER_FIELDS})


def bonferroni(p: float | None, trials: int) -> float | None:
    """p times the number of trials, capped at 1. The blunt one, and the
    default: it needs no assumption about how the trials relate."""
    return None if p is None else min(1.0, p * max(trials, 1))


def bonferroni_t(trials: int, alpha: float = 0.05) -> float:
    """The |t| a single result needs for a family-wise ``alpha`` over
    ``trials`` looks (two-sided, normal)."""
    return _NORMAL.inv_cdf(1.0 - alpha / (2.0 * max(trials, 1)))


def expected_max_sharpe(trials: int, sharpe_sd: float) -> float:
    """The best Sharpe ratio ``trials`` strategies with no skill would show
    (Bailey and Lopez de Prado's false-strategy theorem). ``sharpe_sd`` is
    the standard deviation of the Sharpe ratios across the trials, in the
    same units as the Sharpe being deflated."""
    if trials < 2:
        return 0.0
    gamma = 0.5772156649015329
    return sharpe_sd * (
        (1.0 - gamma) * _NORMAL.inv_cdf(1.0 - 1.0 / trials)
        + gamma * _NORMAL.inv_cdf(1.0 - 1.0 / (trials * math.e))
    )


def deflated_sharpe(
    sharpe: float, n_obs: int, trials: int, sharpe_sd: float,
    skew: float = 0.0, kurtosis: float = 3.0,
) -> float | None:
    """Probability the true Sharpe exceeds what ``trials`` skill-less tries
    would have produced by luck (the Deflated Sharpe Ratio).

    ``sharpe`` is per observation (not annualised), ``n_obs`` the number of
    returns behind it, ``kurtosis`` the raw one (3 for a normal). Above 0.95
    is the conventional pass. None where the variance term is not positive.
    """
    if n_obs < 2:
        return None
    variance = 1.0 - skew * sharpe + (kurtosis - 1.0) / 4.0 * sharpe * sharpe
    if variance <= 0.0:
        return None
    benchmark = expected_max_sharpe(trials, sharpe_sd)
    return _NORMAL.cdf((sharpe - benchmark) * math.sqrt(n_obs - 1) / math.sqrt(variance))


# ---------------------------------------------------------------------------
# Power — how much evidence a claim needs
# ---------------------------------------------------------------------------


def required_n(sd: float, effect: float, alpha: float = 0.05, power: float = 0.80) -> int:
    """Paired observations needed to detect a mean difference of ``effect``
    when the differences have standard deviation ``sd`` (two-sided)."""
    z = _NORMAL.inv_cdf(1.0 - alpha / 2.0) + _NORMAL.inv_cdf(power)
    return math.ceil((z * sd / effect) ** 2)


def sessions_needed(n: int, events_per_session: float) -> float | None:
    return None if events_per_session <= 0 else n / events_per_session


# ---------------------------------------------------------------------------
# Live evaluation
# ---------------------------------------------------------------------------

# Decision-time features (`src/snapshot.py`) and the back-filled price-history
# ones (`src/forward.py`), each with the sign convention "higher is better" so
# a positive IC means the factor points the way its name says.
LIVE_FACTORS: dict[str, Callable[[Mapping[str, Any]], Any]] = {
    "mom_12_1": lambda r: (r.get("price_features") or {}).get("mom_12_1"),
    "resid_mom_12_1_z": lambda r: (r.get("price_features") or {}).get("resid_mom_12_1_z"),
    "mom_6_1": lambda r: (r.get("price_features") or {}).get("mom_6_1"),
    "rs_3m_vs_sector": lambda r: (r.get("price_features") or {}).get("rs_3m_vs_sector"),
    "near_52w_high": lambda r: (r.get("price_features") or {}).get("dist_52w_high"),
    "low_vol_60d": lambda r: _neg((r.get("price_features") or {}).get("vol_60d")),
    "oversold_neg_rsi": lambda r: _neg(r.get("rsi")),
    "above_200d": lambda r: r.get("dist_sma_200"),
    "tight_spread": lambda r: _neg(r.get("spread_pct")),
    "conviction": lambda r: r.get("conviction"),
    # S2, factor 12: recorded forward by the evidence job
    #, never backtested. Absent on a row the vendor would not answer.
    "eps_revision": lambda r: (r.get("estimates") or {}).get("eps_revision_90_7"),
    "revision_breadth": lambda r: (r.get("estimates") or {}).get("revision_breadth_1m"),
}

HORIZONS = (1, 3, 5, 8, 21)


def _neg(value: Any) -> float | None:
    number = finite(value)
    return None if number is None else -number


def first_per_day(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """One row per (symbol, trading day): the earliest cycle's.

    A monitor cycle re-judges the names the full cycle judged; counting both
    would double a day's cross-section with near-identical rows.
    """
    seen: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in sorted(rows, key=lambda r: (str(r.get("trading_day")), str(r.get("cycle_id")))):
        seen.setdefault((str(row.get("symbol")), str(row.get("trading_day"))[:10]), row)
    return list(seen.values())


def factor_panel(
    rows: Sequence[Mapping[str, Any]],
    *,
    return_key: str = "no_8",
    min_names: int = 8,
) -> dict[str, dict]:
    """Per-factor IC statistics over the snapshot rows, by decision day.

    ``return_key`` is a key of the row's `fwd` (`no_8`: the 8-session return
    from the next open, the program's primary). ``min_names`` is low because
    a live cycle judges a few dozen names, not five hundred; the summary's
    `n` and `names` say how thin each date was.
    """
    by_day: dict[str, list[Mapping[str, Any]]] = {}
    for row in first_per_day(rows):
        by_day.setdefault(str(row.get("trading_day"))[:10], []).append(row)

    out: dict[str, dict] = {}
    for name, read in LIVE_FACTORS.items():
        dated: list[tuple[str, float]] = []
        spreads: list[float] = []
        names = 0
        covered = 0
        total = 0
        scores: dict[str, dict[str, float]] = {}
        for day in sorted(by_day):
            day_rows = by_day[day]
            factor = [read(r) for r in day_rows]
            total += len(day_rows)
            covered += sum(1 for v in factor if finite(v) is not None)
            scores[day] = {
                str(r.get("symbol")): v for r, v in zip(day_rows, factor) if finite(v) is not None
            }
            stat = cross_section(
                factor,
                [(r.get("fwd") or {}).get(return_key) for r in day_rows],
                [r.get("sector") for r in day_rows],
                min_names=min_names,
            )
            if stat is None:
                continue
            dated.append((day, stat["ic"]))
            names += stat["n"]
            if stat["spread"] is not None:
                spreads.append(stat["spread"])
        summary = ic_summary([ic for _, ic in dated])
        years = sign_by_year(dated)
        out[name] = {
            **summary,
            "names": names,
            "coverage": None if not total else covered / total,
            "spread": mean(spreads) if spreads else None,
            "spread_t": t_stat(spreads),
            "turnover": top_quantile_turnover(scores),
            "by_year": years,
            "passes_bar": passes_bar(summary, years),
        }
    return out


def evaluate(
    snapshot_rows: Sequence[Mapping[str, Any]],
    shadow_rows: Sequence[Mapping[str, Any]],
    *,
    trials: int,
    hold_days: int = 12,
    boot: int = BOOT,
) -> dict:
    """Everything `live` prints, as data."""
    usable = [r for r in snapshot_rows if (r.get("fwd") or {}).get("no_8") is not None]
    panels = {f"no_{h}": factor_panel(snapshot_rows, return_key=f"no_{h}") for h in (8, 21)}

    arms = {}
    for label, signal, control in (
        ("no_rsi_exit (primary)", "R_signal_norsi", "R_random_sector_norsi"),
        ("with_rsi_exit", "R_signal", "R_random_sector"),
    ):
        pairs = control_pairs(shadow_rows, signal, control)
        independent = deoverlap(pairs, hold_days)
        stat = clustered_bootstrap(
            [p["diff"] for p in independent], [p["entry_day"] for p in independent], boot=boot
        )
        if stat is not None:
            stat["p_bonferroni"] = bonferroni(2.0 * min(stat["p_le_0"], 1.0 - stat["p_le_0"]), trials)
        arms[label] = {
            "pairs_closed": len(pairs),
            "pairs_independent": len(independent),
            "signal_mean": mean([p["signal"] for p in independent]) if independent else None,
            "control_mean": mean([p["control_ret"] for p in independent]) if independent else None,
            "diff_sd": stdev([p["diff"] for p in independent]) if len(independent) > 1 else None,
            "paired": stat,
        }

    beta = control_pairs(shadow_rows, "R_signal_norsi", "B_hold_spy")
    beta_independent = deoverlap(beta, hold_days)
    return {
        "snapshot_rows": len(snapshot_rows),
        "snapshot_days": len({str(r.get("trading_day"))[:10] for r in snapshot_rows}),
        "snapshot_rows_with_8d_return": len(usable),
        "verdicts": _counts(snapshot_rows, "verdict"),
        "trials": trials,
        "t_needed_bonferroni": bonferroni_t(trials),
        "factors": panels,
        "control_arm": arms,
        "vs_spy": {
            "pairs_independent": len(beta_independent),
            "paired": clustered_bootstrap(
                [p["diff"] for p in beta_independent],
                [p["entry_day"] for p in beta_independent], boot=boot,
            ),
        },
    }


def _counts(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        out[str(row.get(key))] = out.get(str(row.get(key)), 0) + 1
    return dict(sorted(out.items()))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def pct(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:+.{digits}%}"


def num(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:+.{digits}f}"


def interval(stat: Mapping[str, Any] | None) -> str:
    if stat is None:
        return "too few to test"
    return f"{pct(stat['mean'])} [{pct(stat['lo'])}, {pct(stat['hi'])}], n={stat['n']} on {stat['clusters']} dates"


def render(result: Mapping[str, Any]) -> str:
    lines = [
        "# Live selection evidence",
        "",
        f"Snapshot: {result['snapshot_rows']} rows over {result['snapshot_days']} decision days; "
        f"{result['snapshot_rows_with_8d_return']} have an 8-session forward return. "
        f"Verdicts: {result['verdicts']}.",
        f"Trials on the ledger: {result['trials']}. A single result needs |t| >= "
        f"{result['t_needed_bonferroni']:.2f} to stand at 5% after Bonferroni.",
        "",
        "## Control arm: signal minus seeded random same-sector name",
        "",
    ]
    for label, arm in result["control_arm"].items():
        stat = arm["paired"]
        lines.append(
            f"- **{label}**: {interval(stat)}. Signal {pct(arm['signal_mean'])}, control "
            f"{pct(arm['control_mean'])}; {arm['pairs_closed']} closed pairs, "
            f"{arm['pairs_independent']} after removing overlapping re-signals."
            + ("" if stat is None else f" Bonferroni p = {stat['p_bonferroni']:.3f}.")
        )
    lines += ["", f"- **vs holding SPY over the same dates**: {interval(result['vs_spy']['paired'])}.", ""]
    for key, panel in result["factors"].items():
        lines += [
            f"## Sector-neutral factor ICs, forward return `{key}`",
            "",
            "| factor | dates | mean IC | ICIR | t | Q5-Q1 | turnover | coverage | bar |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for name, s in sorted(panel.items(), key=lambda kv: -abs(kv[1]["t"] or 0.0)):
            lines.append(
                f"| {name} | {s['n']} | {num(s['mean'], 4)} | {num(s['icir'])} | {num(s['t'], 1)} | "
                f"{pct(s['spread'])} | {'n/a' if s['turnover'] is None else format(s['turnover'], '.0%')} | "
                f"{'n/a' if s['coverage'] is None else format(s['coverage'], '.0%')} | "
                f"{'PASS' if s['passes_bar'] else '-'} |"
            )
        lines.append("")
    lines.append(
        "The bar is |t| >= 3.0 and the same sign in 4 of the last 5 calendar years. "
        "A factor under 70% coverage is absent, not weak."
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _client():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from supabase_client import SupabaseClient, load_env  # noqa: PLC0415

    load_env()
    return SupabaseClient()


def read_live(start: str, end: str, client=None) -> tuple[list[dict], list[dict]]:
    """`(snapshot rows, shadow rows)` for decision / entry days in the window.

    Paged a month at a time: each RPC answers one jsonb value, which has no
    row limit to truncate it and so no limit to protect the caller either.
    """
    client = client or _client()
    snapshot: list[dict] = []
    shadow: list[dict] = []
    cursor = date.fromisoformat(start)
    last = date.fromisoformat(end)
    while cursor <= last:
        stop = min(last, cursor + timedelta(days=30))
        window = {"p_from": cursor.isoformat(), "p_to": stop.isoformat()}
        snapshot.extend(client._rpc("edge_decision_snapshot_read", window) or [])
        shadow.extend(client._rpc("edge_shadow_read", window) or [])
        cursor = stop + timedelta(days=1)
    return snapshot, shadow


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    live = sub.add_parser("live", help="evaluate the snapshot table and the control arm")
    live.add_argument("--from", dest="start", default="2026-10-05")
    live.add_argument("--to", dest="end", default=date.today().isoformat())
    live.add_argument("--json", action="store_true")
    live.add_argument("--boot", type=int, default=BOOT)

    power = sub.add_parser("power", help="sample sizes, and how long they take")
    power.add_argument("--sd", type=float, default=0.0465,
                       help="sd of the paired per-trade difference (offline: 4.65%%)")
    power.add_argument("--rate", type=float, default=1.0,
                       help="independent signals per trading day")
    power.add_argument("--effects", default="0.0030,0.0050,0.0100")

    sub.add_parser("ledger", help="count the trials run so far")

    args = parser.parse_args(argv)
    if args.command == "ledger":
        rows = ledger_rows()
        by_phase = _counts(rows, "phase")
        print(f"{len(rows)} trials on {LEDGER.relative_to(ROOT)}: {by_phase}")
        print(f"|t| needed at 5% family-wise: {bonferroni_t(len(rows)):.2f}")
        return 0
    if args.command == "power":
        print("| detectable edge per trade | pairs needed | trading days | years |")
        print("|---|---|---|---|")
        for effect in (float(x) for x in args.effects.split(",")):
            n = required_n(args.sd, effect)
            days = sessions_needed(n, args.rate)
            print(f"| {effect:.2%} | {n:,} | {days:,.0f} | {days / 252:.1f} |")
        return 0

    snapshot, shadow = read_live(args.start, args.end)
    result = evaluate(snapshot, shadow, trials=ledger_count(), boot=args.boot)
    print(json.dumps(result, indent=2, default=str) if args.json else render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
