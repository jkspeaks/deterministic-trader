"""Ranking, and the whole-share preference (design v3 §4.3).

"Candidates on the WHOLE path receive a ranking preference, because the path
itself is worth something: it carries better risk control. It is a tiebreaker,
never an override of play quality."

The bonus is applied to the path implied by the *unclamped* notional, because
ranking has to be settled before the budget walk can consume headroom in rank
order. A candidate the walk later shrinks into a fractional order keeps the
rank it earned.
"""

import pytest

from src import sizing
from tests.factories import candidate, config, context, entry

# equity 236.42, 3 open slots -> a 31.7572 notional. F (14.87) is whole at
# that size; NVDA (198.90) cannot be.
WHOLE_NAME = "F"
FRACTIONAL_NAME = "NVDA"


def ctx_with(*symbols):
    ctx = context()
    quotes = {"NVDA": (198.85, 198.90), "PFE": (25.00, 25.03), "BAC": (61.90, 61.97)}
    sectors = {"NVDA": "Technology", "PFE": "Health Care", "BAC": "Financials"}
    for symbol in symbols:
        if symbol in quotes:
            bid, ask = quotes[symbol]
            ctx["quotes"][symbol] = {"bid": bid, "ask": ask, "last": ask}
            ctx["sectors"][symbol] = sectors[symbol]
    return ctx


def order_of(result):
    return [e.symbol for e in result.entries]


def test_the_stronger_play_ranks_first():
    result = sizing.size(
        [candidate(symbol="F", conviction=0.50), candidate(symbol="PFE", conviction=0.90)],
        ctx_with("PFE"),
    )

    assert order_of(result) == ["PFE", "F"]


def test_the_whole_path_wins_a_tie():
    # Both plays like the name equally; only one of them can carry a resting
    # stop, so that one goes first. whole_shares_only off: the ranking's own
    # bonus, not the buy-side refusal, is under test here.
    ctx = ctx_with(FRACTIONAL_NAME)
    ctx["config"] = config(whole_shares_only=False)
    result = sizing.size(
        [candidate(symbol=FRACTIONAL_NAME), candidate(symbol=WHOLE_NAME)],
        ctx,
    )

    assert order_of(result) == ["F", "NVDA"]


def test_the_whole_path_bonus_does_not_override_play_quality():
    """A 0.05 bonus must not promote a weak play over a strong one.

    NVDA at 0.90 beats F at 0.70 + 0.05. If this ever inverts, the bonus has
    stopped being a tiebreaker and become the ranking.
    """
    ctx = ctx_with(FRACTIONAL_NAME)
    ctx["config"] = config(whole_shares_only=False)
    result = sizing.size(
        [
            candidate(symbol=WHOLE_NAME, conviction=0.70),
            candidate(symbol=FRACTIONAL_NAME, conviction=0.90),
        ],
        ctx,
    )

    assert order_of(result) == ["NVDA", "F"]


def test_a_large_enough_bonus_would_override_and_is_therefore_config():
    # The guard against a badly chosen bonus is that it is visible and
    # testable, not that it is impossible.
    ctx = ctx_with(FRACTIONAL_NAME)
    ctx["config"] = config(whole_share_bonus=0.50, whole_shares_only=False)

    result = sizing.size(
        [
            candidate(symbol=FRACTIONAL_NAME, conviction=0.90),
            candidate(symbol=WHOLE_NAME, conviction=0.70),
        ],
        ctx,
    )

    assert order_of(result) == ["F", "NVDA"]


def test_an_exact_tie_breaks_toward_the_shorter_holding_period():
    """§6.0: "faster capital turnover matters more than expected edge when only
    three slots exist."

    F and PFE are both WHOLE at this rung and both at 0.70, so the scores are
    exactly equal and only the holding period separates them. CATALYST's seven
    days beats MACRO's fifteen, which is the opposite of the alphabetical order
    that would otherwise apply.
    """
    result = sizing.size(
        [
            candidate(symbol="F", play_id="MACRO", expected_holding_days=15),
            candidate(symbol="PFE", play_id="CATALYST", expected_holding_days=7),
        ],
        ctx_with("PFE"),
    )

    assert order_of(result) == ["PFE", "F"]


def test_the_holding_period_never_outranks_the_score():
    """A seven-day play at 0.60 still loses to a fifteen-day play at 0.90.

    The tiebreak is a tiebreak. If this inverts, turnover has quietly become
    the ranking.
    """
    result = sizing.size(
        [
            candidate(symbol="PFE", play_id="CATALYST", conviction=0.60,
                      expected_holding_days=7),
            candidate(symbol="F", play_id="MACRO", conviction=0.90,
                      expected_holding_days=15),
        ],
        ctx_with("PFE"),
    )

    assert order_of(result) == ["F", "PFE"]


def test_an_exact_tie_breaks_on_symbol_so_golden_files_are_stable():
    # Neither candidate declares a holding period, so the §6.0 tiebreak has
    # nothing to say and the stable fallback still decides.
    result = sizing.size(
        [candidate(symbol="PFE"), candidate(symbol="F")], ctx_with("PFE")
    )

    assert order_of(result) == ["F", "PFE"]


def test_a_declared_holding_period_outranks_an_undeclared_one():
    """An absent period sorts last, not first.

    The alternative — treating "unknown" as zero — would let a play that
    forgot the field jump every play that supplied it.
    """
    result = sizing.size(
        [candidate(symbol="F"), candidate(symbol="PFE", expected_holding_days=15)],
        ctx_with("PFE"),
    )

    assert order_of(result) == ["PFE", "F"]


def test_the_score_and_its_bonus_are_reported():
    result = sizing.size([candidate(conviction=0.70)], context())

    sized = entry(result, "F")
    assert sized.conviction == pytest.approx(0.70)
    assert sized.rank_bonus == pytest.approx(0.05)
    assert sized.rank_score == pytest.approx(0.75)


def test_a_fractional_candidate_earns_no_bonus():
    ctx = ctx_with(FRACTIONAL_NAME)
    ctx["config"] = config(whole_shares_only=False)
    result = sizing.size(
        [candidate(symbol=FRACTIONAL_NAME, conviction=0.70)], ctx
    )

    sized = entry(result, FRACTIONAL_NAME)
    assert sized.rank_bonus == 0.0
    assert sized.rank_score == pytest.approx(0.70)


def test_a_candidate_with_no_conviction_ranks_last_rather_than_crashing():
    # plays.py owns conviction; sizing must not be the thing that breaks when
    # a play forgets to supply it.
    result = sizing.size(
        [candidate(symbol="PFE", conviction=None), candidate(symbol="F", conviction=0.10)],
        ctx_with("PFE"),
    )

    assert order_of(result) == ["F", "PFE"]
