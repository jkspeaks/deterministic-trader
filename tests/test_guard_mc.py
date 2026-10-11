"""guard/mc.py — the Monte Carlo pre-trade denials.

`ctx["mc_check"]` is opt-in (module docstring): a context that never mentions
the key — every fixture in the rest of the suite, and any routine build that
predates the Vercel wiring — must see no denials from this module at all.
Only once the key is present does a missing-per-symbol read start denying.
"""

from src import guard
from tests.factories import codes, context, order


def _mc(**overrides):
    return {"p_stop": 0.10, "drawdown_p95": 0.08, **overrides}


# --- opt-in: absent key changes nothing -----------------------------------


def test_absent_mc_check_key_denies_nothing():
    result = guard.evaluate([order()], context())

    assert codes(result) == []
    assert result.approved == [order()]


# --- present key, missing symbol: fails closed -----------------------------


def test_symbol_missing_from_a_present_mc_check_denies_the_buy():
    result = guard.evaluate([order()], context(mc_check={}))

    assert codes(result, "F") == [guard.MC_SIMULATION_UNAVAILABLE]


def test_a_favorable_read_approves_the_buy():
    result = guard.evaluate([order()], context(mc_check={"F": _mc()}))

    assert codes(result, "F") == []
    assert result.approved == [order()]


# --- threshold breaches -----------------------------------------------------


def test_stop_probability_above_the_configured_max_is_denied():
    result = guard.evaluate(
        [order()], context(mc_check={"F": _mc(p_stop=0.40)})
    )

    assert codes(result, "F") == [guard.MC_STOP_PROBABILITY_EXCEEDED]


def test_drawdown_p95_above_the_configured_max_is_denied():
    result = guard.evaluate(
        [order()], context(mc_check={"F": _mc(drawdown_p95=0.20)})
    )

    assert codes(result, "F") == [guard.MC_DRAWDOWN_P95_EXCEEDED]


def test_both_thresholds_breached_denies_with_both_codes():
    result = guard.evaluate(
        [order()],
        context(mc_check={"F": _mc(p_stop=0.40, drawdown_p95=0.20)}),
    )

    assert set(codes(result, "F")) == {
        guard.MC_STOP_PROBABILITY_EXCEEDED,
        guard.MC_DRAWDOWN_P95_EXCEEDED,
    }


def test_a_read_missing_one_field_only_judges_the_field_present():
    # A degraded Vercel response (e.g. target never set, so p_target/p_stop
    # come back but drawdown was not computed) should not deny on a field it
    # never received an opinion about.
    result = guard.evaluate(
        [order()], context(mc_check={"F": {"p_stop": 0.10}})
    )

    assert codes(result, "F") == []


# --- exits are never touched ------------------------------------------------


def test_a_missing_mc_read_does_not_block_an_exit():
    sell = order(side="sell", type="market", limit_price=None)
    result = guard.evaluate([sell], context(mc_check={}))

    assert result.approved == [sell]


def test_a_breached_threshold_does_not_block_an_exit():
    sell = order(side="sell", type="market", limit_price=None)
    result = guard.evaluate(
        [sell], context(mc_check={"F": _mc(p_stop=0.99, drawdown_p95=0.99)})
    )

    assert result.approved == [sell]
