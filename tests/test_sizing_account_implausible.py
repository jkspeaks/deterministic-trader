"""`equity_total` came back
readable (669.53) but wrong — the account held more than that in open
positions alone. `sizing.account_implausible` is the cross-check that would
have caught it: an unmissable, independent floor under `equity_total` that
does not depend on trusting the field that was wrong.
"""

from src.sizing import account_implausible


def test_equity_below_the_sum_of_positions_is_implausible():
    account = {"equity_total": 669.53}
    positions = [
        {"symbol": "INVH", "market_value": 497.00},
        {"symbol": "SPG", "market_value": 400.00},
    ]

    assert account_implausible(account, positions)


def test_equity_covering_positions_plus_cash_is_plausible():
    account = {"equity_total": 1400.00}
    positions = [
        {"symbol": "INVH", "market_value": 497.00},
        {"symbol": "SPG", "market_value": 400.00},
    ]

    assert not account_implausible(account, positions)


def test_equity_exactly_covering_positions_is_not_implausible():
    """The boundary is `<`, not `<=` — a fully-invested account with ~$0 cash
    is a real, legal state."""
    account = {"equity_total": 100.00}
    positions = [{"symbol": "F", "market_value": 100.00}]

    assert not account_implausible(account, positions)


def test_sub_cent_noise_against_positions_is_not_implausible():
    account = {"equity_total": 100.00}
    positions = [{"symbol": "F", "market_value": 100.004}]

    assert not account_implausible(account, positions)


def test_no_positions_is_never_implausible_on_the_position_check():
    assert not account_implausible({"equity_total": 1.00}, [])
    assert not account_implausible({"equity_total": 1.00}, None)


def test_unreadable_equity_is_not_implausible_here():
    """A missing/zero/non-numeric read is `guard.ACCOUNT_UNREADABLE`, evaluated
    ahead of this — duplicating that halt under a second name would only make
    the two race to explain the same cycle."""
    for bad in ({}, {"equity_total": None}, {"equity_total": 0}, {"equity_total": "abc"}):
        assert not account_implausible(bad, [{"market_value": 50.0}]), bad


def test_below_half_of_yesterdays_benchmark_is_implausible():
    account = {"equity_total": 100.00}
    prior_benchmark = {"trading_day": "2026-09-10", "equity": 300.00}

    assert account_implausible(account, [], prior_benchmark)


def test_at_exactly_half_of_yesterdays_benchmark_is_not_implausible():
    account = {"equity_total": 150.00}
    prior_benchmark = {"trading_day": "2026-09-10", "equity": 300.00}

    assert not account_implausible(account, [], prior_benchmark)


def test_missing_prior_benchmark_skips_that_half_of_the_check():
    """The first post-close an account ever runs has no prior row to compare
    against, the same silence `report._integrity_alert` already treats this
    way."""
    account = {"equity_total": 1.00}

    assert not account_implausible(account, [], {})
    assert not account_implausible(account, [], None)
    assert not account_implausible(account, [], {"equity": None})


def test_an_unparseable_prior_benchmark_equity_skips_that_half_too():
    account = {"equity_total": 1.00}

    assert not account_implausible(account, [], {"equity": "abc"})
