"""`ops/pit_fundamentals.py` — fundamentals as they were known on a day (S2).

A fundamental factor in a backtest is only as honest as its dates. Each test
pins one rule on a company small enough to read: a
filing is usable only from the day after it was accepted, the first-filed
value of a period survives a later restatement, a stale or missing figure is
absent rather than defaulted, and each factor's sign says "higher is better".
"""

from __future__ import annotations

import math
from datetime import date

import pytest

from ops import pit_fundamentals as pit


def fact(end, val, filed, start=None, form="10-K"):
    row = {"end": end, "val": val, "filed": filed, "form": form}
    if start is not None:
        row["start"] = start
    return row


def company(**tags) -> dict:
    return {"facts": {"us-gaap": {tag: {"units": {"USD": rows}} for tag, rows in tags.items()}}}


def year(n: int, income: float, assets: float, equity: float, gross: float | None = None) -> dict:
    """Fiscal year ``n`` (calendar), filed on 15 February of the next."""
    end, start, filed = f"{n}-12-31", f"{n}-01-01", f"{n + 1}-02-15"
    out = {
        "NetIncomeLoss": [fact(end, income, filed, start)],
        "Assets": [fact(end, assets, filed)],
        "StockholdersEquity": [fact(end, equity, filed)],
    }
    if gross is not None:
        out["GrossProfit"] = [fact(end, gross, filed, start)]
    return out


def merged(*years: dict) -> dict:
    tags: dict[str, list] = {}
    for one in years:
        for tag, rows in one.items():
            tags.setdefault(tag, []).extend(rows)
    return company(**tags)


FIVE_YEARS = merged(*(year(2016 + k, income, 1000.0, 500.0, 300.0)
                      for k, income in enumerate((50.0, 60.0, 40.0, 55.0, 45.0))))


# --- the filing date is the gate ----------------------------------------------


def test_a_filing_is_usable_only_strictly_after_the_day_it_was_accepted():
    funds = pit.Fundamentals(pit.reduce(merged(year(2020, 50.0, 1000.0, 400.0, 300.0))))

    # Accepted 2021-02-15. A cycle that morning cannot be assumed to have it.
    assert funds.asof(date(2021, 2, 15))["gross_profitability"] is None
    assert funds.asof(date(2021, 2, 16))["gross_profitability"] == pytest.approx(0.30)


def test_the_first_filed_value_survives_a_later_restatement():
    facts = merged(year(2020, 50.0, 1000.0, 400.0, 300.0))
    # The 2021 10-K repeats fiscal 2020 as a comparative, restated.
    facts["facts"]["us-gaap"]["GrossProfit"]["units"]["USD"].append(
        fact("2020-12-31", 999.0, "2022-02-15", "2020-01-01"))
    funds = pit.Fundamentals(pit.reduce(facts))

    assert funds.asof(date(2022, 3, 1))["gross_profitability"] == pytest.approx(0.30)


def test_a_ratio_exists_only_once_both_of_its_numbers_have_been_filed():
    facts = company(
        Assets=[fact("2020-12-31", 1000.0, "2021-02-15")],
        StockholdersEquity=[fact("2020-12-31", 400.0, "2021-03-10")],
    )
    funds = pit.Fundamentals(pit.reduce(facts))

    assert funds.asof(date(2021, 3, 1))["low_leverage"] is None
    assert funds.asof(date(2021, 3, 11))["low_leverage"] == pytest.approx(-0.60)


# --- what counts as a fiscal year ---------------------------------------------


def test_a_quarter_is_not_a_fiscal_year_and_a_53_week_year_is():
    facts = company(
        NetIncomeLoss=[
            fact("2020-03-31", 10.0, "2020-05-01", "2020-01-01", form="10-Q"),
            fact("2021-01-02", 80.0, "2021-02-20", "2019-12-29"),      # 371 days
        ],
    )
    assert list(pit.reduce(facts)["net_income"]) == [date(2021, 1, 2)]


def test_a_balance_sheet_number_keeps_every_date_and_ignores_duration_facts():
    facts = company(Assets=[
        fact("2020-12-31", 1000.0, "2021-02-15"),
        fact("2021-03-31", 1100.0, "2021-05-01", form="10-Q"),
        fact("2021-03-31", 5.0, "2021-05-01", "2021-01-01"),            # mis-tagged flow
    ])
    assert pit.reduce(facts)["assets"] == {
        date(2020, 12, 31): (date(2021, 2, 15), 1000.0),
        date(2021, 3, 31): (date(2021, 5, 1), 1100.0),
    }


def test_the_higher_priority_tag_supplies_a_period_both_tags_report():
    facts = company(
        Revenues=[fact("2020-12-31", 900.0, "2021-02-15", "2020-01-01")],
        SalesRevenueNet=[
            fact("2020-12-31", 111.0, "2021-02-15", "2020-01-01"),
            fact("2019-12-31", 800.0, "2020-02-15", "2019-01-01"),
        ],
    )
    revenue = pit.reduce(facts)["revenue"]

    assert revenue[date(2020, 12, 31)][1] == 900.0
    assert revenue[date(2019, 12, 31)][1] == 800.0       # only the older tag had it


# --- the four factors ----------------------------------------------------------


def test_gross_profit_falls_back_to_revenue_minus_cost_and_is_absent_without_either():
    with_parts = company(
        Revenues=[fact("2020-12-31", 900.0, "2021-02-15", "2020-01-01")],
        CostOfRevenue=[fact("2020-12-31", 500.0, "2021-02-15", "2020-01-01")],
        Assets=[fact("2020-12-31", 1000.0, "2021-02-15")],
    )
    a_bank = company(
        Revenues=[fact("2020-12-31", 900.0, "2021-02-15", "2020-01-01")],
        Assets=[fact("2020-12-31", 1000.0, "2021-02-15")],
    )
    day = date(2021, 6, 1)

    assert pit.Fundamentals(pit.reduce(with_parts)).asof(day)["gross_profitability"] == pytest.approx(0.40)
    assert pit.Fundamentals(pit.reduce(a_bank)).asof(day)["gross_profitability"] is None


def test_a_stale_figure_is_absent_not_carried_forward():
    funds = pit.Fundamentals(pit.reduce(merged(year(2020, 50.0, 1000.0, 400.0, 300.0))))

    # 200 days from the balance sheet for leverage; 500 from the year's end
    # for profitability.
    assert funds.asof(date(2021, 7, 19))["low_leverage"] == pytest.approx(-0.60)
    assert funds.asof(date(2021, 7, 20))["low_leverage"] is None
    assert funds.asof(date(2022, 5, 15))["gross_profitability"] == pytest.approx(0.30)
    assert funds.asof(date(2022, 5, 16))["gross_profitability"] is None


def test_stability_is_minus_the_sample_sd_of_the_last_five_years():
    funds = pit.Fundamentals(pit.reduce(FIVE_YEARS))
    roe = [v / 500.0 for v in (50.0, 60.0, 40.0, 55.0, 45.0)]
    mean = sum(roe) / 5
    sd = math.sqrt(sum((v - mean) ** 2 for v in roe) / 4)

    got = funds.asof(date(2021, 3, 1))
    assert got["roe_stability"] == pytest.approx(-sd)
    assert got["earnings_stability"] == pytest.approx(-sd / 2.0)       # assets are twice equity


def test_stability_needs_four_years_and_uses_only_the_years_filed_by_then():
    funds = pit.Fundamentals(pit.reduce(FIVE_YEARS))

    # Fiscal 2016-2018 filed by March 2019: three years is not a history.
    assert funds.asof(date(2019, 3, 1))["roe_stability"] is None
    # A year later there are four, and fiscal 2020 is not among them.
    roe = [v / 500.0 for v in (50.0, 60.0, 40.0, 55.0)]
    mean = sum(roe) / 4
    sd = math.sqrt(sum((v - mean) ** 2 for v in roe) / 3)
    assert funds.asof(date(2020, 3, 1))["roe_stability"] == pytest.approx(-sd)


def test_a_year_of_negative_equity_is_dropped_from_roe_and_kept_in_roa():
    facts = merged(
        year(2017, 50.0, 1000.0, 500.0), year(2018, 60.0, 1000.0, -20.0),
        year(2019, 40.0, 1000.0, 500.0), year(2020, 55.0, 1000.0, 500.0),
    )
    got = pit.Fundamentals(pit.reduce(facts)).asof(date(2021, 3, 1))

    assert got["roe_stability"] is None            # three usable years
    assert got["earnings_stability"] is not None   # four


def test_every_factor_is_absent_for_a_company_with_no_facts():
    assert pit.Fundamentals(pit.reduce({})).asof(date(2022, 1, 3)) == {
        "gross_profitability": None, "roe_stability": None,
        "low_leverage": None, "earnings_stability": None,
    }


def test_a_fact_with_no_usable_date_or_value_is_dropped():
    facts = company(Assets=[
        fact("2020-12-31", None, "2021-02-15"),
        fact("not a date", 5.0, "2021-02-15"),
        fact("2020-12-31", float("nan"), "2021-02-15"),
        {"end": "2020-12-31", "val": 7.0},
    ])
    assert pit.reduce(facts)["assets"] == {}
