"""The one row a day §8.4 says cannot be reconstructed later.

Five columns, not §8.4's four: a deposit lands in the account as equity and is
indistinguishable from a gain the moment it does. §8.4's own argument for
writing this series daily — that it cannot be rebuilt after the fact — applies
with more force to the flow than to the prices, because a price has a public
history and a transfer does not.
"""

from src import report
from tests.factories import INCEPTION, report_context, with_inception


def one_name(**overrides) -> dict:
    """A book of two shares of F and $10 of cash, against a $20 quote.

    Small enough to price in the head: the frozen book is worth $50, so a test
    that reads 50.0 has proved the cash line is in and a test that reads 40.0
    has proved it was dropped.
    """
    quotes = overrides.pop("quotes", {"F": {"last": 20.00}})
    ctx = report_context(quotes=quotes, **overrides)
    return with_inception(
        ctx,
        inception_date="2026-08-04",
        equity=100.0,
        cash=10.0,
        spy_close=700.0,
        inventory={"F": 2.0},
        deposits=[],
    )


def test_the_frozen_book_prices_to_the_recorded_inception_equity():
    """The shipped baseline and the book it freezes must agree **on inception-day
    prices**, or §1's do-nothing control starts ahead of the account it is
    controlling for and stays there forever — §8.4 says the series cannot be
    rebuilt, so a baseline off by $1.04 is off by $1.04 for the life of the
    experiment. It was, at 237.46 against a recorded 236.42, because Appendix
    A.4's per-share prices were rounded to the cent while the quantities were
    the broker's own six decimals.

    The file now derives `equity` as `cash + Σ(quantity × close)` at the same
    closes `INCEPTION_QUOTES` holds, so this is an identity rather than a
    coincidence — and this test fails the moment someone edits one side of it.
    """
    result = report.run(report_context())

    assert result.benchmark.frozen_value == INCEPTION["equity"]


def test_the_post_close_writes_the_days_row():
    result = report.run(report_context())

    assert result.benchmark is not None
    assert result.benchmark.trading_day == "2026-08-06"
    assert result.benchmark.equity == 247.72


def test_the_market_benchmark_is_the_days_spy_quote():
    result = report.run(report_context())

    assert result.benchmark.spy_close == 771.33


def test_the_frozen_book_is_priced_from_the_inception_inventory():
    """§1's do-nothing control: the book as it stood, marked at today's prices.
    The cash it was holding is part of the book — a control that spent it on
    nothing would be a different, better-performing account than the one that
    actually existed."""
    assert report.run(one_name()).benchmark.frozen_value == 50.00


def test_a_deposit_is_recorded_rather_than_scored():
    """The whole reason this row has five columns. $25 arriving in the account
    raises equity by $25, and nothing else in the system can tell that apart
    from a $25 gain."""
    ctx = one_name()
    ctx["inception"]["deposits"] = [{"date": "2026-08-06", "amount": 25.00}]

    assert report.run(ctx).benchmark.net_deposits == 25.00


def test_a_deposit_that_has_not_landed_yet_is_not_counted():
    """Rows are written per day and read as a series. A deposit dated Tuesday
    counted into Monday's row would make Monday's return wrong forever."""
    ctx = one_name()
    ctx["inception"]["deposits"] = [{"date": "2026-08-10", "amount": 25.00}]

    assert report.run(ctx).benchmark.net_deposits == 0.00


# --- which cycles write one ------------------------------------------------


def test_only_the_post_close_writes_a_row():
    """§13 puts "write scorecard data" in the post-close and nowhere else. A
    monitor cycle at 11:40 would otherwise overwrite the day's row with a
    mid-session mark, and §1's benchmarks are close-to-close series."""
    ctx = report_context(cycle={"cycle_type": "monitor"})

    assert report.run(ctx).benchmark is None


def test_a_half_days_repeated_post_closes_write_one_row_not_four():
    """§13's slots are bands, so on a 13:00 half-day the 13:40 through 16:30
    firings are all `postclose`. They key on the same trading day, which is
    what makes the write an upsert rather than four rows for one Friday."""
    early = {"early_close": {"2026-08-06": "13:00"}}
    first = report_context(
        calendar=early, cycle={"now": "2026-08-06T17:40:00Z"}
    )
    last = report_context(calendar=early, cycle={"now": "2026-08-06T20:30:00Z"})

    assert report.run(first).benchmark.trading_day == "2026-08-06"
    assert report.run(last).benchmark.trading_day == "2026-08-06"


# --- the numbers that must refuse to exist ---------------------------------


def test_an_unquotable_name_voids_the_frozen_book_rather_than_shrinking_it():
    """The failure mode this guards is one-directional. A name that cannot be
    priced contributes zero to a sum, so a partial frozen book is always a
    *smaller* frozen book — the benchmark quietly gets easier to beat, on the
    days data is worst, and the row is unreconstructable afterwards."""
    ctx = one_name(quotes={"F": {"last": None}})

    result = report.run(ctx)

    assert result.benchmark.frozen_value is None
    assert [n.code for n in result.notes] == [report.QUOTE_UNAVAILABLE]


def test_a_missing_name_voids_it_too():
    """Absent and unpriced are the same fact. A quotes map that simply omits a
    delisted holding must not read as a book that no longer contains it."""
    ctx = one_name()
    ctx["quotes"] = {"SPY": {"last": 700.00}}

    assert report.run(ctx).benchmark.frozen_value is None


def test_a_missing_spy_quote_voids_only_the_market_benchmark():
    """One missing quote must not take the row with it. Equity and the frozen
    book are still true, and a day with a hole in one column beats no row."""
    ctx = one_name()
    ctx["quotes"] = {"F": {"last": 20.00}}

    result = report.run(ctx)

    assert result.benchmark.spy_close is None
    assert result.benchmark.frozen_value == 50.00
    assert result.benchmark.equity == 247.72
