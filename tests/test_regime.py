"""§6.0's regime, now that it is arithmetic instead of an opinion.

The module this tests exists because the opinion producer died and nothing
detected it for a week. So the tests that matter most here are not the happy
states — they are the three that pin what an *absent* or half-present input
does, because "a stale number and a considered one are the same JSON" is the
failure this replaces.
"""

import json
import math
from pathlib import Path

import pytest

from src import plays, regime as regimes
from tests.factories import ETFS, SECTOR_ETFS, config, regime_inputs


def evaluate(**overrides):
    return regimes.evaluate(regime_inputs(**overrides), config())


# --- the three market states ----------------------------------------------


def test_a_stacked_spy_is_risk_on():
    """Above both moving averages. The only tape SURGE is allowed to run in."""
    assert evaluate().market_state == regimes.RISK_ON


def test_above_the_two_hundred_but_under_the_fifty_is_neutral():
    """Trend intact, momentum is not. Entries continue; SURGE does not."""
    result = evaluate(spy={"last": 610.0})

    assert result.market_state == regimes.NEUTRAL
    assert result.allow_entries is True


def test_below_the_two_hundred_in_a_volatile_tape_is_risk_off():
    """§4.3 F, both legs. This is the no-trade condition the whole module is
    for, and it is the one state that stops entries outright."""
    result = evaluate(spy={"last": 580.0, "rv20": 0.42})

    assert result.market_state == regimes.RISK_OFF
    assert result.allow_entries is False


def test_below_the_two_hundred_in_a_quiet_tape_is_only_a_correction():
    """The second leg is not decoration. An ordinary drawdown under the
    200-day is exactly the tape DIP exists for, and suppressing it would
    starve the funnel the way the frozen multiplier already did."""
    result = evaluate(spy={"last": 580.0, "rv20": 0.12})

    assert result.market_state == regimes.NEUTRAL
    assert result.allow_entries is True


def test_the_vol_ceiling_is_config_and_not_a_module_constant():
    """`regime_vol_max` is the operator's leash on how volatile a correction
    has to get before the system stands down."""
    inputs = regime_inputs(spy={"last": 580.0, "rv20": 0.20})

    assert regimes.evaluate(inputs, config(regime_vol_max=0.50)).market_state == regimes.NEUTRAL
    assert regimes.evaluate(inputs, config(regime_vol_max=0.10)).market_state == regimes.RISK_OFF


def test_the_reason_names_the_numbers_it_judged_on():
    """The runlog has to be able to answer "why was nothing bought" without
    re-running the cycle — the question nobody could answer for the week the
    old producer was dead."""
    reason = evaluate(spy={"last": 580.0, "rv20": 0.42}).reason

    assert "580.00" in reason and "600.00" in reason and "0.42" in reason


# --- the four sector states -----------------------------------------------


def test_a_sector_above_its_two_hundred_day_is_tradeable():
    assert evaluate().sector_states["XLK"] == regimes.TREND_OK


def test_a_sector_over_the_rsi_ceiling_is_a_peak():
    """The operator's own ask: ignore the sector at a peak."""
    assert evaluate(sectors={"XLK": {"rsi14": 74.0}}).sector_states["XLK"] == regimes.PEAK


def test_a_sector_far_below_its_two_hundred_day_is_a_breakdown():
    result = evaluate(sectors={"XLE": {"last": 88.0, "sma200": 95.0}})

    assert result.sector_states["XLE"] == regimes.BREAKDOWN


def test_a_sector_just_under_its_two_hundred_day_is_still_tradeable():
    """`oversold_ok`, and the reason the breakdown test is a band rather than
    a line: an ordinary pullback through the 200-day is the setup, not the
    thing to avoid."""
    result = evaluate(sectors={"XLE": {"last": 93.0, "sma200": 95.0}})

    assert result.sector_states["XLE"] == regimes.OVERSOLD_OK
    assert "XLE" not in regimes.blocked_sectors(result.as_dict(), ETFS)


def test_the_breakdown_band_is_config():
    inputs = regime_inputs(sectors={"XLE": {"last": 93.0, "sma200": 95.0}})

    tight = regimes.evaluate(inputs, config(sector_breakdown_pct=0.01))
    assert tight.sector_states["XLE"] == regimes.BREAKDOWN


def test_the_rsi_ceiling_is_config():
    inputs = regime_inputs(sectors={"XLK": {"rsi14": 65.0}})

    assert regimes.evaluate(inputs, config(sector_rsi_peak=60.0)).sector_states["XLK"] == regimes.PEAK


def test_a_broken_sector_is_broken_whatever_its_oscillator_says():
    """Breakdown is tested first. The two states cannot both be acted on, and
    a structural break outranks a momentum reading."""
    result = evaluate(sectors={"XLE": {"last": 80.0, "sma200": 95.0, "rsi14": 90.0}})

    assert result.sector_states["XLE"] == regimes.BREAKDOWN


def test_blocked_sectors_are_named_in_the_reason():
    result = evaluate(sectors={"XLK": {"rsi14": 74.0}})

    assert "XLK not tradeable today" in result.reason


# --- what missing data does, which is the whole point ----------------------


def test_no_inputs_at_all_is_no_opinion_rather_than_a_dark_regime():
    """The inversion of the old `regime_fit`. An absent producer must not read
    as a considered "stop": that is what let a dead premarket session halve
    every conviction for a week without alerting anyone."""
    assert regimes.evaluate(None, config()) is None
    assert regimes.evaluate({}, config()) is None


def test_an_unreadable_spy_block_yields_no_regime_at_all():
    """A regime derived from half its inputs is the stale-number problem in a
    new costume, so a missing 200-day takes the whole verdict down rather than
    producing a confident-looking one."""
    assert regimes.evaluate({"spy": {"last": 640.0}}, config()) is None
    assert regimes.evaluate({"spy": {"sma200": 600.0}}, config()) is None


def test_a_missing_fifty_day_can_still_be_neutral_but_never_risk_on():
    """`risk_on` is the state that unlocks SURGE's all-in mode. It is not
    granted on an assumption."""
    result = regimes.evaluate(
        {"spy": {"last": 640.0, "sma200": 600.0, "rv20": 0.12}}, config()
    )

    assert result.market_state == regimes.NEUTRAL
    assert result.play_multipliers[plays.SURGE] == 0.0


def test_an_unmeasurable_vol_under_the_two_hundred_day_is_not_risk_off():
    """Both legs are required and one of them is unknown, so the state is the
    one that keeps trading and says so."""
    result = regimes.evaluate({"spy": {"last": 580.0, "sma200": 600.0}}, config())

    assert result.market_state == regimes.NEUTRAL
    assert "unmeasurable" in result.reason


def test_a_sector_with_no_readable_data_is_omitted_rather_than_blocked():
    """Absent is not blocked. Failing this one closed would let a single
    missing ETF quote silently remove a ninth of the universe from the day."""
    result = evaluate(sectors={"XLK": {"last": None, "sma200": None}})

    assert "XLK" not in result.sector_states
    assert "XLK" not in {
        etf for etf, _ in regimes.blocked_sectors(result.as_dict(), ETFS).items()
    }


def test_a_string_that_will_not_parse_is_missing_data_and_not_a_crash():
    """Every field here was pasted through from a vendor response."""
    result = evaluate(sectors={"XLK": {"rsi14": "n/a"}})

    assert result.sector_states["XLK"] == regimes.TREND_OK


# --- the play multipliers, which is where this bites -----------------------


def test_risk_off_darks_every_play():
    multipliers = evaluate(spy={"last": 580.0, "rv20": 0.42}).play_multipliers

    assert set(multipliers.values()) == {0.0}
    assert sorted(multipliers) == sorted(regimes.PLAY_IDS)


def test_surge_alone_needs_risk_on():
    """The only play the dead producer left unaffected — an absent
    `edge_play_state` row reads as 1.0 for SURGE by design — is now the only
    play with a regime gate stricter than "entries are allowed"."""
    neutral = evaluate(spy={"last": 610.0}).play_multipliers

    assert neutral[plays.SURGE] == 0.0
    assert neutral[plays.DIP] == 1.0
    assert neutral[plays.PEAD] == 1.0


def test_a_tradeable_tape_runs_every_play_at_full_weight():
    """No fractional damping anywhere. Every finer number the old `regime_fit`
    invited was one nobody could defend, and the sector question is answered
    per candidate rather than by damping a play across the board."""
    assert set(evaluate().play_multipliers.values()) == {1.0}


def test_the_play_ids_are_the_ones_plays_actually_dispatches():
    """This module restates the five ids rather than importing them (the
    import would close a loop). That is only safe while they agree."""
    assert set(regimes.PLAY_IDS) == {
        plays.DIP, plays.PEAD, plays.CATALYST, plays.MACRO, plays.SURGE
    }


# --- realized volatility ---------------------------------------------------


def test_realized_vol_is_the_annualized_deviation_of_daily_log_returns():
    """A constant 1%-a-day zigzag: the twenty returns are ±ln(1.01) about a
    mean of zero, so the annualized figure is that deviation — carrying the
    n−1 correction, which is not cosmetic at n=20 — times sqrt(252)."""
    closes = [100.0 * (1.01 if i % 2 else 1.0) for i in range(40)]

    step = math.log(1.01)
    expected = step * math.sqrt(regimes.RV_WINDOW / (regimes.RV_WINDOW - 1))
    expected *= math.sqrt(regimes.TRADING_DAYS_PER_YEAR)
    assert regimes.realized_vol(closes) == pytest.approx(expected, rel=1e-3)


def test_a_flat_series_has_no_volatility():
    assert regimes.realized_vol([100.0] * 40) == pytest.approx(0.0)


def test_a_short_series_measures_nothing_rather_than_measuring_less():
    """A half-window vol is a different statistic wearing the same name, and
    `regime_vol_max` is calibrated against the twenty-day one."""
    assert regimes.realized_vol([100.0] * regimes.RV_WINDOW) is None
    assert regimes.realized_vol([]) is None


def test_closes_are_used_when_no_rv_is_published():
    """The routine pastes `get_equity_historicals` through; the arithmetic is
    this module's, per the prompt's second absolute."""
    volatile = [100.0 * (1.04 if i % 2 else 1.0) for i in range(40)]
    result = regimes.evaluate(
        {"spy": {"last": 580.0, "sma200": 600.0, "closes": volatile}}, config()
    )

    assert result.market_state == regimes.RISK_OFF


def test_a_published_figure_wins_over_the_closes_it_came_with():
    """A caller that supplies `rv20` has already decided what window it
    measured, and this module does not overrule it."""
    volatile = [100.0 * (1.04 if i % 2 else 1.0) for i in range(40)]
    result = regimes.evaluate(
        {"spy": {"last": 580.0, "sma200": 600.0, "rv20": 0.10, "closes": volatile}},
        config(),
    )

    assert result.market_state == regimes.NEUTRAL


# --- the sector-name mapping ----------------------------------------------


def test_the_sector_map_inverts_the_shipped_etf_file():
    """One authoritative copy of "which ETF speaks for which sector", derived
    rather than restated (applied to a map)."""
    assert regimes.sector_etf(ETFS)["Information Technology"] == "XLK"
    assert len(regimes.sector_etf(ETFS)) == len(SECTOR_ETFS)


def test_blocked_sectors_is_keyed_by_gics_name_so_a_candidate_can_be_looked_up():
    """A candidate carries a sector name from the issuer holdings file, never
    a ticker."""
    result = evaluate(sectors={"XLK": {"rsi14": 74.0}})

    assert regimes.blocked_sectors(result.as_dict(), ETFS) == {
        "Information Technology": regimes.PEAK
    }


def test_blocked_sectors_is_empty_for_an_absent_regime():
    """What makes it safe to call unconditionally in `cli.candidates`."""
    assert regimes.blocked_sectors(None, ETFS) == {}


# --- P1: DIP's sector rule, breadth and the five postures -----
#
# Everything below is computed on every cycle and enforced only under `regime_v2_enabled`.

RECORDED = Path(__file__).parent / "fixtures" / "regime"

# One sector just under its own 200-day: `oversold_ok`, inside the 5% band.
UNDER = {"last": 93.0, "sma200": 95.0}


def recorded(day: str) -> dict:
    """An `edge_regime.payload` exactly as the pre-market job wrote it."""
    return json.loads((RECORDED / f"edge_regime_{day}.json").read_text(encoding="utf-8"))


def under(count: int) -> dict:
    """``count`` of the eleven sectors under their 200-day, the rest above."""
    return {symbol: dict(UNDER) for symbol in SECTOR_ETFS[:count]}


def test_oversold_ok_is_still_produced_and_still_not_a_candidate_drop():
    """The diagnostic state survives: `sectors` and the discovery drop
    read exactly what they read before."""
    result = evaluate(sectors={"XLE": dict(UNDER)})

    assert result.sector_states["XLE"] == regimes.OVERSOLD_OK
    assert regimes.blocked_sectors(result.as_dict(), ETFS) == {}


def test_blocked_sector_states_is_unchanged_and_dip_has_its_own_wider_set():
    """CATALYST and PEAD are event plays; a sector's 200-day must not take
    their names out at discovery. Only DIP reads the wider set."""
    assert regimes.BLOCKED_SECTOR_STATES == frozenset({regimes.PEAK, regimes.BREAKDOWN})
    assert regimes.DIP_BLOCKED_SECTOR_STATES == frozenset(
        {regimes.PEAK, regimes.BREAKDOWN, regimes.OVERSOLD_OK}
    )


def test_breadth_with_all_eleven_readable():
    blocks = regime_inputs(sectors=under(4))["sectors"]

    result = regimes.breadth(blocks)

    assert result["n_available"] == 11
    assert result["breadth_200"] == pytest.approx(7 / 11)
    assert result["breadth_unavailable"] is False


def test_breadth_with_eight_readable_uses_eight_as_the_denominator():
    blocks = regime_inputs(sectors=under(2))["sectors"]
    for symbol in SECTOR_ETFS[-3:]:
        blocks[symbol] = {"last": None, "sma200": None}

    result = regimes.breadth(blocks)

    assert result["n_available"] == 8
    assert result["breadth_200"] == pytest.approx(6 / 8)


def test_breadth_with_seven_readable_is_unavailable_and_never_a_number():
    """A thin sample is not gated on."""
    blocks = regime_inputs()["sectors"]
    for symbol in SECTOR_ETFS[-4:]:
        del blocks[symbol]

    result = regimes.breadth(blocks)

    assert result["n_available"] == 7
    assert result["breadth_200"] is None and result["breadth_50"] is None
    assert result["breadth_unavailable"] is True


def test_breadth_fifty_needs_its_own_readable_fifty_days():
    """The rows the pre-market job wrote before P1 carry no `sma50`."""
    blocks = regime_inputs()["sectors"]
    assert regimes.breadth(blocks)["breadth_50"] is None

    for index, symbol in enumerate(SECTOR_ETFS):
        blocks[symbol]["sma50"] = 98.0 if index < 8 else 104.0
    assert regimes.breadth(blocks)["breadth_50"] == pytest.approx(8 / 11)


def test_breadth_rides_on_the_regime_dict():
    result = evaluate(sectors=under(4)).as_dict()

    assert result["breadth"]["breadth_200"] == pytest.approx(7 / 11)
    assert result["breadth"]["n_available"] == 11
    assert result["posture"] in regimes.POSTURES


@pytest.mark.parametrize(
    "overrides, macro_day, expected",
    [
        ({}, None, "TRADE_NORMALLY"),
        ({"sectors": under(5)}, None, "REDUCED_SIZE"),            # 6/11 = 55%
        ({}, True, "REDUCED_SIZE"),                                # macro-event day
        ({"spy": {"rv20": 0.24}}, None, "REDUCED_SIZE"),           # vol 0.20-0.30
        ({"spy": {"last": 610.0}}, None, "REDUCED_SIZE"),          # under the 50-day
        ({"sectors": under(7)}, None, "HIGH_CONVICTION_ONLY"),     # 4/11 = 36%
        ({"sectors": under(9)}, None, "NO_NEW_POSITIONS"),         # 2/11 = 18%
        ({"spy": {"last": 580.0, "rv20": 0.12}}, None, "NO_NEW_POSITIONS"),
        ({"spy": {"last": 580.0, "rv20": 0.42}}, None, "HOLD_CASH"),
    ],
)
def test_the_posture_table(overrides, macro_day, expected):
    result = regimes.evaluate(
        regime_inputs(**overrides), config(), macro_event_day=macro_day
    )

    assert result.posture == expected


def test_each_posture_carries_its_size_and_its_target():
    normal = evaluate()
    reduced = evaluate(sectors=under(5))
    high = evaluate(sectors=under(7))
    none = evaluate(sectors=under(9))

    assert (normal.size_multiplier, normal.target_invested_pct) == (1.0, 0.80)
    assert (reduced.size_multiplier, reduced.target_invested_pct) == (0.5, 0.50)
    assert (high.size_multiplier, high.target_invested_pct) == (0.5, 0.30)
    assert (none.size_multiplier, none.target_invested_pct) == (0.0, 0.0)


def test_a_recorded_mixed_tape_is_reduced_size():
    """5 of 11 above trend (45.5%), SPY above both averages."""
    result = regimes.evaluate(recorded("2026-09-18")["regime_inputs"], config())

    assert result.breadth["breadth_200"] == pytest.approx(5 / 11)
    assert result.posture == regimes.REDUCED_SIZE
    # What the old rule said about the same tape, unchanged.
    assert result.market_state == regimes.RISK_ON and result.allow_entries is True


def test_a_recorded_weak_tape_is_no_new_positions():
    """3 of 11 above trend (27%) while SPY reads `risk_on`: a recorded
    day."""
    result = regimes.evaluate(recorded("2026-10-02")["regime_inputs"], config())

    assert result.breadth["breadth_200"] == pytest.approx(3 / 11)
    assert result.posture == regimes.NO_NEW_POSITIONS
    assert "27%" in result.posture_reason
    assert result.market_state == regimes.RISK_ON and result.allow_entries is True


def test_the_posture_thresholds_are_config():
    inputs = recorded("2026-10-02")["regime_inputs"]

    result = regimes.evaluate(inputs, config(posture_no_new_breadth_200=0.25))

    assert result.posture == regimes.HIGH_CONVICTION_ONLY


def test_an_unavailable_breadth_is_not_gated_on():
    """Seven readable ETFs, all under trend: no breadth, so no breadth rule
    fires — the posture comes from SPY alone and the notes say so."""
    sectors = {symbol: dict(UNDER) for symbol in SECTOR_ETFS[:7]}
    inputs = {"spy": regime_inputs()["spy"], "sectors": sectors}

    result = regimes.evaluate(inputs, config())

    assert result.breadth["breadth_unavailable"] is True
    assert result.posture == regimes.TRADE_NORMALLY
    assert "breadth_unavailable" in result.notes


def test_the_optional_inputs_are_absent_safe_and_bite_when_supplied():
    """RSP-vs-SPY and the 52-week drawdown are not supplied by the runner yet."""
    assert evaluate().posture == regimes.TRADE_NORMALLY
    assert evaluate(rsp_vs_spy_1m=-0.03).posture == regimes.HIGH_CONVICTION_ONLY
    assert evaluate(
        spy={"drawdown_52w": -0.09, "rv20": 0.27}
    ).posture == regimes.NO_NEW_POSITIONS
    assert evaluate(rsp_vs_spy_1m="n/a").posture == regimes.TRADE_NORMALLY


def with_returns(strong: tuple[str, ...]) -> dict:
    """Every sector with 1M/3M returns; ``strong`` ones beat SPY, the rest lag."""
    inputs = regime_inputs(spy={"ret_1m": 0.01, "ret_3m": 0.03})
    for symbol in SECTOR_ETFS:
        lead = symbol in strong
        inputs["sectors"][symbol].update(
            {"ret_1m": 0.04 if lead else -0.02, "ret_3m": 0.08 if lead else -0.03}
        )
    return inputs


def test_sector_relative_strength_ranks_the_eleven_and_marks_the_upper_half():
    strong = SECTOR_ETFS[:5]

    result = regimes.evaluate(with_returns(strong), config())

    assert {s for s, row in result.sector_rs.items() if row["upper_half"]} == set(strong)
    assert result.sector_rs[strong[0]]["rs_1m"] == pytest.approx(0.03)
    assert result.sector_rs[strong[0]]["rs_3m"] == pytest.approx(0.05)


def test_relative_strength_is_unavailable_on_the_recorded_rows():
    result = regimes.evaluate(recorded("2026-10-02")["regime_inputs"], config())

    assert result.sector_rs == {}
    assert "rs_unavailable" in result.notes


def test_the_weakest_third_of_the_ranking_is_marked():
    """Three of eleven. Read by the Dip Recovery sleeve in REDUCED_SIZE."""
    result = regimes.evaluate(with_returns(SECTOR_ETFS[:5]), config())

    weakest = [s for s, row in result.sector_rs.items() if row["weakest"]]
    assert len(weakest) == 3
    assert all(result.sector_rs[s]["rank"] >= 9 for s in weakest)
    assert not any(result.sector_rs[s]["upper_half"] for s in weakest)


# --- the two optional inputs ------------------------------


def test_the_drawdown_is_measured_from_the_highest_close_of_the_year():
    closes = [100.0] * 100 + [125.0] + [110.0] * 149

    assert regimes.drawdown_from_high(closes) == pytest.approx(110.0 / 125.0 - 1.0)
    assert regimes.drawdown_from_high([100.0] * 250) == pytest.approx(0.0)


def test_a_drawdown_from_less_than_a_year_of_closes_is_not_measured():
    """Sixty closes would measure against a recent, lower high."""
    assert regimes.drawdown_from_high([100.0] * 239) is None
    assert regimes.drawdown_from_high([100.0] * 240) == pytest.approx(0.0)


@pytest.mark.parametrize(
    "drawdown, rv20, expected",
    [
        (-0.09, 0.27, "NO_NEW_POSITIONS"),   # both legs
        (-0.09, 0.24, "REDUCED_SIZE"),       # deep but quiet: only the vol rule
        (-0.07, 0.27, "REDUCED_SIZE"),       # volatile but shallow
        (-0.08, 0.27, "REDUCED_SIZE"),       # exactly 8% is not "more than 8%"
        (0.09, 0.27, "REDUCED_SIZE"),        # positive is not a drawdown
    ],
)
def test_the_drawdown_rule_needs_both_legs_and_a_signed_drawdown(drawdown, rv20, expected):
    result = evaluate(spy={"drawdown_52w": drawdown, "rv20": rv20})

    assert result.posture == expected


def test_a_positive_drawdown_reads_as_missing_and_says_so():
    result = evaluate(spy={"drawdown_52w": 0.09})

    assert regimes.DRAWDOWN_52W_UNAVAILABLE in result.notes


@pytest.mark.parametrize(
    "rsp_1m, expected",
    [
        (-0.02, "HIGH_CONVICTION_ONLY"),  # 3 points behind SPY's +1%
        (-0.01, "TRADE_NORMALLY"),        # exactly 2 behind is not "more than 2%"
        (0.04, "TRADE_NORMALLY"),         # equal-weight leading
    ],
)
def test_rsp_vs_spy_is_derived_from_the_two_one_month_returns(rsp_1m, expected):
    result = evaluate(spy={"ret_1m": 0.01}, rsp={"ret_1m": rsp_1m})

    assert result.posture == expected
    assert regimes.RSP_VS_SPY_UNAVAILABLE not in result.notes


def test_the_whole_edge_regime_payload_is_opened_rather_than_ignored():
    """`edge_regime_recent()` returns the stored payload with the block one
    level down."""
    result = regimes.evaluate(recorded("2026-10-02"), config())

    assert result is not None and result.posture == regimes.NO_NEW_POSITIONS


def test_period_return_is_measured_over_whole_sessions_or_not_at_all():
    closes = [100.0] * 21 + [110.0]

    assert regimes.period_return(closes, 21) == pytest.approx(0.10)
    assert regimes.period_return(closes[1:], 21) is None
