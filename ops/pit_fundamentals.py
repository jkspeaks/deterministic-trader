#!/usr/bin/env python3
"""ops/pit_fundamentals.py — fundamentals as they were known on a day.

Definitions frozen for the S2 factor panel. SEC EDGAR's XBRL "company facts"
carry, for every number a
company has filed, the period it describes and the date the filing was
accepted (`filed`). That second date is what makes a backtest of a
fundamental factor possible at all: a figure is usable on a decision day only
if its `filed` is **strictly before** that day.

Two rules, both here and both tested:

* **First filed wins.** A fiscal year's net income appears in that year's
  10-K and again, as a comparative, in the next two. Where the later filings
  restate it, using the latest value would put a correction into a day that
  could not have known it. Each (concept, period) keeps the value from the
  filing that reported it first.
* **Absent is absent.** A company that reports no cost of revenue has no
  gross profitability; a balance sheet more than `LEVERAGE_MAX_AGE` days old
  is not "the latest balance sheet". Each comes back None, never a default.

**Standard library only**, like `backtest_engine.py`, so the repo's suite can
run it. `ops/factor_panel.py` does the fetching and hands `reduce` the JSON.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from datetime import date
from typing import Any, Mapping, Sequence

# Concepts, each as the us-gaap tags that have carried it, in priority order:
# for a given period the first tag that has a value supplies it.
TAGS: dict[str, tuple[str, ...]] = {
    "revenue": (
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ),
    "cost": ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"),
    "gross": ("GrossProfit",),
    "net_income": ("NetIncomeLoss", "ProfitLoss"),
    "assets": ("Assets",),
    "equity": (
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ),
}
FLOWS = frozenset({"revenue", "cost", "gross", "net_income"})

# A fiscal year, in days. 52- and 53-week years and a leap day all fit.
YEAR_MIN_DAYS, YEAR_MAX_DAYS = 350, 380

# The S2 factor's freshness windows.
PROFITABILITY_MAX_AGE = 500     # days from the fiscal year's end to the decision
LEVERAGE_MAX_AGE = 200          # days from the balance sheet's date
STABILITY_YEARS = 5
STABILITY_MIN_YEARS = 4


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def reduce(companyfacts: Mapping[str, Any]) -> dict[str, dict[date, tuple[date, float]]]:
    """`{concept: {period end: (first filed, value)}}` from one company's
    EDGAR company-facts JSON.

    Flow concepts keep only fiscal-year periods (`YEAR_MIN_DAYS` to
    `YEAR_MAX_DAYS` long); balance-sheet concepts keep every date. Only USD
    facts. A fact with no usable `end`, `filed` or value is dropped.
    """
    gaap = ((companyfacts.get("facts") or {}).get("us-gaap")) or {}
    out: dict[str, dict[date, tuple[date, float]]] = {}
    for concept, tags in TAGS.items():
        merged: dict[date, tuple[date, float]] = {}
        # Lowest priority first, so a higher-priority tag overwrites it.
        for tag in reversed(tags):
            first: dict[date, tuple[date, float]] = {}
            for fact in (((gaap.get(tag) or {}).get("units")) or {}).get("USD") or []:
                end, filed, value = _day(fact.get("end")), _day(fact.get("filed")), _number(fact.get("val"))
                if end is None or filed is None or value is None:
                    continue
                if concept in FLOWS:
                    start = _day(fact.get("start"))
                    if start is None or not YEAR_MIN_DAYS <= (end - start).days <= YEAR_MAX_DAYS:
                        continue
                elif fact.get("start"):
                    continue
                held = first.get(end)
                if held is None or filed < held[0]:
                    first[end] = (filed, value)
            merged.update(first)
        out[concept] = merged
    return out


class Fundamentals:
    """One company's reduced facts, queryable as of a day."""

    def __init__(self, reduced: Mapping[str, Mapping[date, tuple[date, float]]]) -> None:
        get = lambda concept: reduced.get(concept) or {}  # noqa: E731
        assets, equity, income = get("assets"), get("equity"), get("net_income")
        gross, revenue, cost = get("gross"), get("revenue"), get("cost")

        # Each list is `(known from, period end, value)`, sorted by the day it
        # became known. "Known from" is the later of the filings its parts
        # came from: a ratio exists once both of its numbers do.
        self._leverage = sorted(
            (max(assets[end][0], equity[end][0]), end,
             (assets[end][1] - equity[end][1]) / assets[end][1])
            for end in assets if end in equity and assets[end][1] > 0.0
        )
        profit = []
        for end in assets:
            if assets[end][1] <= 0.0:
                continue
            if end in gross:
                profit.append((max(assets[end][0], gross[end][0]), end, gross[end][1] / assets[end][1]))
            elif end in revenue and end in cost:
                profit.append((max(assets[end][0], revenue[end][0], cost[end][0]), end,
                               (revenue[end][1] - cost[end][1]) / assets[end][1]))
        self._profit = sorted(profit)
        self._roe = sorted(
            (max(income[end][0], equity[end][0]), end, income[end][1] / equity[end][1])
            for end in income if end in equity and equity[end][1] > 0.0
        )
        self._roa = sorted(
            (max(income[end][0], assets[end][0]), end, income[end][1] / assets[end][1])
            for end in income if end in assets and assets[end][1] > 0.0
        )

    @staticmethod
    def _known(rows: Sequence[tuple[date, date, float]], day: date) -> list[tuple[date, float]]:
        """`(period end, value)` for rows known strictly before ``day``,
        oldest period first."""
        cut = bisect_left(rows, (day, date.min, -math.inf))
        return sorted((end, value) for _, end, value in rows[:cut])

    def _latest(self, rows, day: date, max_age: int) -> float | None:
        known = self._known(rows, day)
        if not known or (day - known[-1][0]).days > max_age:
            return None
        return known[-1][1]

    def _stability(self, rows, day: date) -> float | None:
        known = self._known(rows, day)
        if not known or (day - known[-1][0]).days > PROFITABILITY_MAX_AGE:
            return None
        values = [value for _, value in known[-STABILITY_YEARS:]]
        if len(values) < STABILITY_MIN_YEARS:
            return None
        mean = sum(values) / len(values)
        return -math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))

    def asof(self, day: date) -> dict[str, float | None]:
        """The four quality factors as a cycle on ``day`` could have known
        them, each signed so higher is the hypothesised better."""
        leverage = self._latest(self._leverage, day, LEVERAGE_MAX_AGE)
        return {
            "gross_profitability": self._latest(self._profit, day, PROFITABILITY_MAX_AGE),
            "roe_stability": self._stability(self._roe, day),
            "low_leverage": None if leverage is None else -leverage,
            "earnings_stability": self._stability(self._roa, day),
        }
