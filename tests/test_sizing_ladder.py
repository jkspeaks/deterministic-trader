"""The account-level rungs of the capital ladder (design v3 §4.1, §4.2).

Everything here is derived from equity and the broker's cash figures at the
moment of the cycle. Invariant 5: capability is computed, never configured —
so there is no rung these functions cannot reach and no threshold to maintain.
"""

import pytest

from src import sizing
from tests.factories import config


# --- position count (§4.1) ------------------------------------------------


@pytest.mark.parametrize(
    "equity,expected",
    [
        (236.42, 3),  # today's account
        (500.00, 3),
        (960.00, 5),
        (1_000.00, 5),
        (2_000.00, 10),
        (5_000.00, 25),
        (10_000.00, 25),
    ],
)
def test_position_count_walks_the_ladder(equity, expected):
    # §4.1: clamp(round(equity / min_position_size), 3, 25) at $200 a
    # position. The ceiling is `MIN_POSITION_WEIGHT` in count form and binds
    # from $5,000.
    assert sizing.n_target(equity, config()) == expected


def test_no_position_is_ever_thinner_than_the_minimum_weight():
    """What the count ceiling actually protects, stated as the rule it is.

    The old `MAX_POSITIONS = 12` said the same thing in slots, which only held
    because `n` is what the ladder divides by. Said as a weight it holds at any
    equity, and it is the reason `n` stops climbing rather than a coincidence of
    where it stops.
    """
    for equity in (960.0, 2_000.0, 10_000.0, 1_000_000.0):
        assert 1.0 / sizing.n_target(equity, config()) >= sizing.MIN_POSITION_WEIGHT


def test_position_count_floors_at_three():
    # $80 of equity is well under one position by the formula. Three is the
    # floor, and §4.4's caps are built to be satisfiable there.
    assert sizing.n_target(80.00, config()) == 3


def test_position_count_survives_an_empty_account():
    assert sizing.n_target(0.0, config()) == 3


def test_a_half_position_rounds_up_not_to_even():
    """$900 is 4.5 positions, which must be 5.

    Python's ``round`` is banker's rounding: ``round(4.5)`` is 4, so the
    built-in would quietly cost a slot at some equities and not at others.
    """
    assert sizing.n_target(900.00, config()) == 5


def test_a_larger_minimum_position_thins_the_book():
    # min_position_size is config, and raising it is how the operator asks
    # for fewer, larger positions without touching code.
    assert sizing.n_target(1_000.00, config(min_position_size=250.0)) == 4


# --- cash reserve (§4.2) --------------------------------------------------


def test_the_cash_reserve_is_two_percent_of_equity():
    assert sizing.cash_reserve(10_000.00, config()) == pytest.approx(200.00)


def test_the_cash_reserve_never_falls_below_a_dollar():
    # 2% of $236.42 is $4.73, but at very low equity the percentage floor
    # would round the reserve away entirely.
    assert sizing.cash_reserve(10.00, config()) == pytest.approx(1.00)


def test_the_cash_reserve_takes_the_percentage_when_it_is_larger():
    assert sizing.cash_reserve(236.42, config()) == pytest.approx(4.7284)


# --- settled basis (§2.4, shared with guard) ------------------------------


def test_the_settled_basis_is_the_lesser_of_buying_power_and_settled_cash():
    account = {"buying_power": 100.00, "settled_cash": 60.00}

    assert sizing.settled_basis(account, config()) == pytest.approx(60.00)


def test_the_settled_basis_is_unknown_when_settled_cash_is_missing():
    # §14: settled cash unknown -> no buy may be sized. None is the signal,
    # not zero, because zero is a legitimate measured balance.
    #
    # Conservative said so explicitly, because it had no verified figure to
    # fall back to. P3 gave it one, so this is now the conservative policy's
    # rule rather than the system's — stated here rather than inherited from
    # the baseline, which follows the live config.
    account = {"buying_power": 100.00, "settled_cash": None}
    cfg = config(settled_cash_policy=sizing.POLICY_CONSERVATIVE)

    assert sizing.settled_basis(account, cfg) is None


def test_the_resolved_policy_still_takes_the_lesser_when_both_are_known():
    # P3 retired the *requirement* for settled_cash, not its authority. A
    # deposit the broker has credited to buying_power ahead of the ACH is the
    # case that survives the probe, and it is exactly the shape below.
    account = {"buying_power": 114.52, "settled_cash": 14.52}

    assert sizing.settled_basis(account, config()) == pytest.approx(14.52)


def test_the_settled_basis_is_unknown_when_buying_power_is_missing():
    account = {"buying_power": None, "settled_cash": 60.00}

    assert sizing.settled_basis(account, config()) is None


def test_the_permissive_policy_trusts_buying_power_alone():
    # The policy P3 unlocks: if buying_power provably excludes unsettled
    # proceeds, settled_cash stops being the binding figure.
    account = {"buying_power": 100.00, "settled_cash": None}
    cfg = config(settled_cash_policy=sizing.POLICY_BUYING_POWER_IS_SETTLED)

    assert sizing.settled_basis(account, cfg) == pytest.approx(100.00)


# --- deployable (§4.2) ----------------------------------------------------


def test_deployable_subtracts_the_reserve_from_the_basis():
    account = {"buying_power": 100.00, "settled_cash": 100.00}

    # 2% of $236.42 is $4.7284, leaving $95.2716 of the $100 basis.
    assert sizing.deployable(account, 236.42, config()) == pytest.approx(95.2716)


def test_deployable_never_goes_negative():
    # A reserve larger than the whole balance means nothing is deployable,
    # not that the account owes the reserve.
    account = {"buying_power": 2.00, "settled_cash": 2.00}

    assert sizing.deployable(account, 10_000.00, config()) == 0.0


def test_deployable_is_unknown_when_the_basis_is_unknown():
    account = {"buying_power": None, "settled_cash": None}

    assert sizing.deployable(account, 236.42, config()) is None


# --- open slots -----------------------------------------------------------


def test_open_slots_is_the_target_less_the_names_already_held():
    assert sizing.open_slots(3, ["TSLA"]) == 2


def test_a_full_book_has_no_open_slots():
    assert sizing.open_slots(3, ["TSLA", "PFE", "XLE"]) == 0


def test_an_overfull_book_reports_zero_rather_than_a_negative():
    # Ten inherited positions against a target of three is the account's
    # actual day-one state (Appendix A.1), so this is not a hypothetical.
    assert sizing.open_slots(3, [f"SYM{i}" for i in range(10)]) == 0


# --- concentration caps (§4.4) --------------------------------------------


@pytest.mark.parametrize(
    "n,expected",
    [(1, 1.20), (3, 0.40), (4, 0.30), (6, 0.20), (8, 0.15), (12, 0.15)],
)
def test_position_cap_is_max_of_fifteen_percent_and_twelve_tenths_over_n(n, expected):
    assert sizing.position_cap_fraction(n) == pytest.approx(expected)


@pytest.mark.parametrize(
    "n,expected",
    [(1, 1.50), (3, 0.50), (4, 0.375), (5, 0.30), (8, 0.30), (12, 0.30)],
)
def test_sector_cap_is_max_of_thirty_percent_and_fifteen_tenths_over_n(n, expected):
    assert sizing.sector_cap_fraction(n) == pytest.approx(expected)


def test_guard_enforces_the_very_same_functions_sizing_sizes_to():
    """One definition each, not two that happen to agree today.

    Sizing shrinks a notional to fit these caps and guard denies anything that
    breaches them. Were the two to drift, sizing would emit an order guard
    refuses on every cycle — and the account would simply stop trading.
    """
    from src import guard

    assert guard.position_cap_fraction is sizing.position_cap_fraction
    assert guard.sector_cap_fraction is sizing.sector_cap_fraction
    assert guard.settled_basis is sizing.settled_basis
