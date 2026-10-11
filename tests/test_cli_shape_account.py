"""`shape-account` — Appendix D's `account` from the two raw broker responses.

The bug this closes is a whole-cycle one. `cli._fetch_plan` names two
account-scope steps, `account` (`get_portfolio`) and `balances`
(`get_accounts`); Appendix D has one `account` key that both feed; and the
merge existed only as four rules of prose a live session followed by hand. The
runner holds no rules, so on the hosted path nobody performed it. The first
live runner cycle died on `assemble` refusing the `balances` row — and had it
not, `guard.halts` would have refused the cycle anyway on an `account`
carrying `total_value` and no `equity_total`.
"""

import pytest

from src import cli
from tests.factories import cli_payload

# `get_portfolio`, verbatim, as the broker returned it — including the
# string-typed numbers, which is the point of several of these.
PORTFOLIO = {
    "cash": "765.62",
    "currency": "USD",
    "total_value": "1400.00",
    "buying_power": {
        "buying_power": "765.6200",
        "display_currency": "USD",
        "unleveraged_buying_power": "765.6200",
    },
    "equity_value": "669.53",
    "pending_deposits": "0",
}

# `get_accounts`, verbatim. **Two accounts**, and the settled figure is per
# account: the margin one is not the one this system trades and its
# `unsettled_funds` is a different number on any day either is non-zero.
ACCOUNTS = [
    {
        "type": "margin",
        "account_number": "<your-margin-account-number>",
        "agentic_allowed": False,
        "unsettled_funds": "40.0000",
    },
    {
        "type": "cash",
        "nickname": "Agentic",
        "account_number": "<your-cash-account-number>",
        "agentic_allowed": True,
        "unsettled_funds": "5.0000",
    },
]

TRADED = "<your-cash-account-number>"


def shaped(**overrides):
    payload = cli_payload(**{"portfolio": PORTFOLIO, "accounts": ACCOUNTS, **overrides})
    return cli.shape_account(payload, cli.load_config())


# ---------------------------------------------------------------------------
# Three copies
# ---------------------------------------------------------------------------


def test_the_three_portfolio_fields_are_copied_under_appendix_ds_names():
    account = shaped()["account"]

    assert account["equity_total"] == "1400.00"
    assert account["cash"] == "765.62"
    assert account["buying_power"] == "765.6200"


def test_the_broker_strings_stay_strings():
    """Absolute 2 has no exception for a cast that looks harmless.

    `guard` and `sizing` already `float()` everything they read off this, so
    converting here buys nothing and costs the one property that makes a
    runlog row auditable: that the figure in it is the figure the broker sent.
    """
    account = shaped()["account"]

    assert all(
        isinstance(account[field], str)
        for field in ("equity_total", "cash", "buying_power")
    )


def test_nothing_but_appendix_ds_own_fields_comes_through():
    # `equity_value`, `currency`, `pending_deposits` and the rest of
    # `get_portfolio` are not Appendix D fields and do not become them.
    assert set(shaped()["account"]) == {
        "equity_total", "cash", "buying_power", "settled_cash", "sold_this_session",
    }


# ---------------------------------------------------------------------------
# The one subtraction, and the account it is made against
# ---------------------------------------------------------------------------


def test_settled_cash_is_measured_against_the_account_this_system_trades():
    # 765.62 cash - 5.00 unsettled on the *cash* account - 0 pending. Reading
    # the margin account's 40.00 instead would understate it by 35.
    result = shaped()

    assert result["account_number"] == TRADED
    assert result["account"]["settled_cash"] == pytest.approx(760.62)


def test_the_account_number_comes_from_inception_json_when_none_is_passed():
    """It has to live somewhere the hosted core can read.

    The live-session path passes it on stdin; the runner cannot, because it
    holds no rules and no config. `config/inception.json` is the rest of this
    account's identity and is already loaded on every command.
    """
    inception = cli.load_config()["inception"]

    assert inception["account_number"] == TRADED
    assert shaped()["account_number"] == TRADED


def test_a_number_passed_on_stdin_wins_over_the_file():
    result = shaped(account_number="<your-margin-account-number>")

    assert result["account_number"] == "<your-margin-account-number>"
    assert result["account"]["settled_cash"] == pytest.approx(725.62)


def test_no_account_number_anywhere_is_a_null_and_a_named_problem():
    payload = cli_payload(portfolio=PORTFOLIO, accounts=ACCOUNTS)
    loaded = cli.load_config()
    loaded = {**loaded, "inception": {}}

    result = cli.shape_account(payload, loaded)

    assert result["account"]["settled_cash"] is None
    assert any("account_number" in p for p in result["problems"])
    # The other three still arrive: sizing falls back to buying_power alone,
    # which is the documented safe degradation, not a halted cycle.
    assert result["account"]["equity_total"] == "1400.00"


def test_an_account_number_matching_nothing_refuses_rather_than_picking_one():
    result = shaped(account_number="000000000")

    assert result["account"]["settled_cash"] is None
    assert any("0 of 2" in p for p in result["problems"])


def test_a_missing_balances_row_costs_settled_cash_and_nothing_else():
    result = shaped(accounts=[])

    assert result["account"]["settled_cash"] is None
    assert result["account"]["equity_total"] == "1400.00"
    assert any("settled_cash" in p for p in result["problems"])


def test_get_portfolios_own_pending_deposits_is_subtracted():
    result = shaped(
        portfolio={**PORTFOLIO, "pending_deposits": "100"}
    )

    assert result["account"]["settled_cash"] == pytest.approx(660.62)


def test_an_explicit_pending_deposits_still_wins():
    result = shaped(pending_deposits=25.0)

    assert result["account"]["settled_cash"] == pytest.approx(735.62)


def test_an_unparseable_pending_deposits_subtracts_nothing_rather_than_crashing():
    result = shaped(portfolio={**PORTFOLIO, "pending_deposits": "n/a"})

    assert result["account"]["settled_cash"] == pytest.approx(760.62)


# ---------------------------------------------------------------------------
# A missing field is omitted, not nulled — that is what ACCOUNT_UNREADABLE is
# ---------------------------------------------------------------------------


def test_a_portfolio_with_no_total_value_omits_equity_total_and_says_so():
    result = shaped(portfolio={k: v for k, v in PORTFOLIO.items() if k != "total_value"})

    assert "equity_total" not in result["account"]
    assert any("equity_total" in p for p in result["problems"])


def test_an_omitted_equity_total_is_what_guard_halts_the_cycle_on():
    from src import guard

    account = shaped(
        portfolio={k: v for k, v in PORTFOLIO.items() if k != "total_value"}
    )["account"]
    ctx = {
        **cli_payload(),
        "config": cli.load_config()["config"],
        "account": account,
        "control": {},
        "positions": [],
        "quotes": {},
    }
    verdict = guard.evaluate(
        [{"symbol": "PFE", "side": "buy", "type": "limit", "quantity": 1,
          "limit_price": 25.0, "session": "regular_hours"}],
        ctx,
    )

    assert verdict.approved == []


def test_a_nested_buying_power_that_is_not_a_mapping_is_a_problem_not_a_crash():
    result = shaped(portfolio={**PORTFOLIO, "buying_power": "765.62"})

    assert "buying_power" not in result["account"]
    assert any("buying_power.buying_power" in p for p in result["problems"])


# ---------------------------------------------------------------------------
# sold_this_session, from the executions the broker already attaches
# ---------------------------------------------------------------------------


def execution(day="2026-08-03T14:00:00Z"):
    return {"timestamp": day, "quantity": "1.0", "price": "25.00"}


def test_no_sell_executions_is_false():
    assert shaped(orders=[])["account"]["sold_this_session"] is False


def test_a_resting_sell_that_has_not_filled_is_not_a_sale():
    # An earlier book's own case: a GTC protective stop, `state: confirmed`,
    # `executions: []`. Counting it would deny every buy under the conservative
    # policy for as long as the stop rests.
    orders = [{"side": "sell", "state": "confirmed", "executions": []}]

    assert shaped(orders=orders)["account"]["sold_this_session"] is False


def test_a_sell_filled_today_is_a_sale():
    orders = [{"side": "sell", "executions": [execution()]}]

    assert shaped(orders=orders)["account"]["sold_this_session"] is True


def test_a_sell_filled_on_an_earlier_day_is_not():
    orders = [{"side": "sell", "executions": [execution("2026-07-31T14:00:00Z")]}]

    assert shaped(orders=orders)["account"]["sold_this_session"] is False


def test_a_buy_filled_today_is_not_a_sale():
    orders = [{"side": "buy", "executions": [execution()]}]

    assert shaped(orders=orders)["account"]["sold_this_session"] is False


def test_an_old_gtc_sell_filling_today_counts_on_its_execution_not_its_age():
    """The reason this reads executions rather than `created_at`.

    A protective stop placed three weeks ago that trips today is exactly the
    sale §2.4's flag is about, and its order-level date is three weeks old.
    """
    orders = [
        {
            "side": "sell",
            "created_at": "2026-07-13T16:10:34Z",
            "executions": [execution()],
        }
    ]

    assert shaped(orders=orders)["account"]["sold_this_session"] is True


def test_an_unreadable_execution_timestamp_counts_as_today():
    # The flag denies buys under the conservative policy, so raising it is the
    # safe direction for input nobody can parse.
    for stamp in ("not-a-date", None):
        orders = [{"side": "sell", "executions": [{"timestamp": stamp}]}]
        assert shaped(orders=orders)["account"]["sold_this_session"] is True