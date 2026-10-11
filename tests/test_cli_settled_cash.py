"""Step 6's one permitted subtraction, moved out of the agent's own arithmetic
and into `src.cli.settled_cash` — Absolute 2 forbids computing it by hand."""

from src import cli
from tests.factories import cli_payload


def settled(**overrides) -> float | None:
    result = cli.settled_cash(cli_payload(**overrides), cli.load_config())
    return result["settled_cash"]


def test_subtracts_unsettled_funds_from_cash():
    assert settled(
        account_number="<your-cash-account-number>",
        portfolio={"cash": 100.0},
        accounts=[{"account_number": "<your-cash-account-number>", "unsettled_funds": 15.0}],
    ) == 85.0


def test_also_subtracts_pending_deposits_when_given():
    assert settled(
        account_number="<your-cash-account-number>",
        portfolio={"cash": 100.0},
        accounts=[{"account_number": "<your-cash-account-number>", "unsettled_funds": 15.0}],
        pending_deposits=20.0,
    ) == 65.0


def test_missing_cash_is_null_not_a_guess():
    assert settled(
        account_number="<your-cash-account-number>",
        portfolio={},
        accounts=[{"account_number": "<your-cash-account-number>", "unsettled_funds": 15.0}],
    ) is None


def test_no_matching_account_is_null_not_a_guess():
    assert settled(
        account_number="<your-cash-account-number>",
        portfolio={"cash": 100.0},
        accounts=[{"account_number": "999999999", "unsettled_funds": 15.0}],
    ) is None


def test_ambiguous_duplicate_account_entries_is_null_not_a_guess():
    """A wrong account's unsettled figure is wrong in a direction nothing
    downstream can detect, so two entries claiming the same account number is
    treated the same as none."""
    assert settled(
        account_number="<your-cash-account-number>",
        portfolio={"cash": 100.0},
        accounts=[
            {"account_number": "<your-cash-account-number>", "unsettled_funds": 15.0},
            {"account_number": "<your-cash-account-number>", "unsettled_funds": 999.0},
        ],
    ) is None


def test_no_account_number_given_is_null_not_a_guess():
    assert settled(
        portfolio={"cash": 100.0},
        accounts=[{"account_number": "<your-cash-account-number>", "unsettled_funds": 15.0}],
    ) is None


def test_the_cycle_is_still_derived_not_trusted():
    """Every command shares Invariant 6 — this one carries no cycle-gated
    behaviour of its own, but it still must not echo a forged cycle."""
    result = cli.settled_cash(
        cli_payload(
            cycle={"cycle_type": "full", "entries_allowed": True},
            now="2026-08-03T20:30:00Z",
        ),
        cli.load_config(),
    )
    assert result["cycle"]["cycle_type"] != "full"
