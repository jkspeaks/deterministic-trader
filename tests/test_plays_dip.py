"""§6.2 `DIP` — oversold inside an uptrend.

"Covers quiet weeks when nothing has a catalyst." Trigger RSI(14) <= 30; entry
requires price above the 200-day and no earnings within three sessions;
conviction scales with depth below the 20-day mean.

§15 asks for a fixture where the play fires and one where it does not. There
are four ways not to fire and each gets its own, because "DIP did not fire" is
a different fact depending on which clause refused.
"""

import pytest

from src import plays, regime as regimes
from tests.factories import (
    ETFS,
    SECTOR_ETFS,
    config,
    plays_context,
    position,
    proposal,
    proposals,
    regime_inputs,
    skips,
)


def test_an_oversold_name_holding_its_two_hundred_day_fires():
    result = plays.entries(plays_context())

    assert proposals(result, plays.DIP) == ["F"]


def test_an_rsi_above_thirty_does_not_trigger_at_all():
    """Not a skip — a silence.

    A note per non-triggering name would be 500 rows a cycle recording that
    nothing happened, which is how a runlog stops being read.
    """
    result = plays.entries(plays_context(signals={"F": {"rsi": 30.1}}))

    assert proposals(result, plays.DIP) == []
    assert skips(result) == []


def test_exactly_thirty_is_oversold_enough():
    """§6.2 says RSI <= 30, and a boundary written as `<` is how a threshold
    quietly becomes 29."""
    result = plays.entries(plays_context(signals={"F": {"rsi": 30.0}}))

    assert proposals(result, plays.DIP) == ["F"]


def test_a_name_well_below_its_two_hundred_day_is_a_falling_knife_and_is_refused():
    """The trigger fired, so this one is recorded: an oversold name that failed
    the uptrend filter is exactly what the filter exists to catch. F at 14.88
    is more than 8% under a 16.00 200-day, well past `dip_trend_band_pct`."""
    result = plays.entries(plays_context(signals={"F": {"sma_200": 16.00}}))

    assert proposals(result, plays.DIP) == []
    assert skips(result, "F") == [plays.ENTRY_CONDITIONS_UNMET]


def test_an_ordinary_pullback_inside_the_band_still_fires():
    """A strict `price > SMA200` rejected every 2-3%
    pullback as a 'falling knife' and starved the funnel. F at 14.88 against a
    15.18 200-day is ~2% under — inside the default 3% band, so DIP proposes."""
    result = plays.entries(plays_context(signals={"F": {"sma_200": 15.18}}))

    assert proposals(result, plays.DIP) == ["F"]


def test_the_band_is_config_and_zero_restores_the_strict_rule():
    """`dip_trend_band_pct` at 0.0 is the old `price > SMA200` gate: the same
    ~2% pullback that fires at the default band is a falling knife here."""
    ctx = plays_context(signals={"F": {"sma_200": 15.18}},
                        config=config(dip_trend_band_pct=0.0))

    result = plays.entries(ctx)

    assert proposals(result, plays.DIP) == []
    assert skips(result, "F") == [plays.ENTRY_CONDITIONS_UNMET]


def test_an_earnings_print_inside_three_sessions_refuses_the_dip():
    """§6.2's own clause, and not guard's T-2 window.

    DIP declines at three sessions where guard denies at two, so the play stops
    proposing before the denial layer has to. A trade denied by guard is a
    wasted cycle; a trade never proposed is not.
    """
    ctx = plays_context()
    ctx["earnings"]["F"]["sessions_until_report"] = 3

    result = plays.entries(ctx)

    assert proposals(result, plays.DIP) == []
    assert skips(result, "F") == [plays.EARNINGS_VETO]


def test_a_name_with_no_confirmed_earnings_date_is_refused():
    """§6.2 requires "no earnings within 3 sessions" and an unknown date cannot
    establish that. Guard fails the same way for the same reason."""
    ctx = plays_context()
    ctx["earnings"]["F"] = {"status": "unknown"}

    result = plays.entries(ctx)

    assert skips(result, "F") == [plays.EARNINGS_VETO]


def test_conviction_scales_with_depth_below_the_twenty_day_mean():
    # 14.88 against a 16.00 mean is 7% down, against a 10% full scale.
    assert proposal(plays.entries(plays_context()), "F").conviction_raw == pytest.approx(0.70)


def test_a_more_extreme_rsi_lifts_conviction_past_equal_depth():
    """Depth alone let an RSI-18 name and an RSI-30 name tie whenever their
    20-day gap matched, though RSI 18 is the stronger oversold read. Both
    fixtures here share the same 7% depth (0.70 conviction) so only the RSI
    difference can move the result."""
    mild = plays.entries(plays_context(signals={"F": {"rsi": 30.0}}))
    extreme = plays.entries(plays_context(signals={"F": {"rsi": 18.0}}))

    assert proposal(mild, "F").conviction_raw == pytest.approx(0.70)
    assert proposal(extreme, "F").conviction_raw > proposal(mild, "F").conviction_raw


def test_rsi_extremity_never_marks_conviction_down():
    """`max`, not an average: a name with genuine 20-day depth is never scored
    lower for an RSI that is merely at the trigger."""
    result = plays.entries(plays_context(signals={"F": {"sma_20": 24.80, "rsi": 30.0}}))

    assert proposal(result, "F").conviction_raw == 1.0


def test_rsi_extremity_cannot_buy_past_the_no_depth_refusal():
    """The gate is unchanged: a name with no depth below its 20-day mean is
    still refused outright, however extreme its RSI — extremity only ever
    raises a conviction that already has depth to raise."""
    result = plays.entries(plays_context(signals={"F": {"sma_20": 14.00, "rsi": 16.0}}))

    assert proposals(result, plays.DIP) == []
    assert skips(result, "F") == [plays.NO_CONVICTION]


def test_a_deeper_dip_is_a_stronger_one():
    shallow = plays.entries(plays_context(signals={"F": {"sma_20": 15.20}}))
    deep = plays.entries(plays_context(signals={"F": {"sma_20": 17.00}}))

    assert proposal(deep, "F").conviction_raw > proposal(shallow, "F").conviction_raw


def test_conviction_is_capped_at_one():
    """A 40% gap below the 20-day mean is not four times the conviction of a
    10% one. §6.1 caps PEAD explicitly and the same ceiling holds here: past
    the full scale the signal is no longer telling you more."""
    result = plays.entries(plays_context(signals={"F": {"sma_20": 24.80}}))

    assert proposal(result, "F").conviction_raw == 1.0


def test_a_name_above_its_twenty_day_mean_has_no_depth_and_no_conviction():
    """Possible with a low RSI on a name that just gapped up. Zero conviction
    is not a candidate — it ranks below a play that has an opinion."""
    result = plays.entries(plays_context(signals={"F": {"sma_20": 14.00}}))

    assert proposals(result, plays.DIP) == []
    assert skips(result, "F") == [plays.NO_CONVICTION]


def test_the_full_scale_is_a_module_constant_not_a_config_key():
    """§17: "adding one is a new entry in plays.py". A play's own shape belongs
    to the play, and only the cross-play terms of §6.0 are tunable."""
    assert "dip_depth_full_scale" not in config()
    assert plays.DIP_DEPTH_FULL_SCALE == 0.10


def test_a_name_with_no_signals_at_all_cannot_trigger():
    """cli.py fetches per-symbol data for the §5.3 pool; a name it failed to
    fetch must not read as RSI 0 and fire every dip in the book."""
    result = plays.entries(plays_context(candidates=["GME"]))

    assert result.candidates == []
    assert skips(result) == []


def test_the_declared_holding_period_is_eight_sessions():
    # §6.2's time stop, and §6.0's tiebreak input.
    assert proposal(plays.entries(plays_context()), "F").expected_holding_days == 8


# --- P1: the sector must itself be above its 200-day ----------

F_SECTOR_ETF = "XLY"  # F is Consumer Discretionary


def v2_context(v2: bool = True, **inputs):
    """The DIP baseline with a regime and the ETF map `cli.decide` supplies."""
    block = regime_inputs(**inputs)
    ctx = plays_context(config=config(regime_v2_enabled=v2))
    ctx["etfs"] = ETFS
    ctx["regime_inputs"] = block
    ctx["regime"] = regimes.evaluate(block, ctx["config"]).as_dict()
    return ctx


def strong_and_weak(strong: tuple[str, ...], sectors: dict | None = None) -> dict:
    """Sector returns: ``strong`` beat SPY over 1M and 3M, the rest lag."""
    blocks = {
        symbol: {
            "ret_1m": 0.04 if symbol in strong else -0.02,
            "ret_3m": 0.08 if symbol in strong else -0.03,
            **((sectors or {}).get(symbol) or {}),
        }
        for symbol in SECTOR_ETFS
    }
    return {"spy": {"ret_1m": 0.01, "ret_3m": 0.03}, "sectors": blocks}


def below_two_hundred(count: int, keep: str = F_SECTOR_ETF) -> dict:
    """``count`` sectors under their 200-day, never ``keep``."""
    others = [s for s in SECTOR_ETFS if s != keep]
    return {symbol: {"last": 93.0, "sma200": 95.0} for symbol in others[:count]}


def test_a_dip_in_a_sector_above_its_two_hundred_day_passes():
    result = plays.entries(v2_context())

    assert proposals(result, plays.DIP) == ["F"]


def test_a_dip_in_a_sector_below_its_two_hundred_day_is_skipped_under_the_flag():
    ctx = v2_context(sectors={F_SECTOR_ETF: {"last": 93.0, "sma200": 95.0}})
    assert ctx["regime"]["sector_states"][F_SECTOR_ETF] == regimes.OVERSOLD_OK

    result = plays.entries(ctx)

    assert proposals(result, plays.DIP) == []
    assert skips(result, "F") == [plays.SKIP_SECTOR_BELOW_200D]


def test_the_same_dip_still_fires_with_the_flag_off():
    ctx = v2_context(v2=False, sectors={F_SECTOR_ETF: {"last": 93.0, "sma200": 95.0}})

    result = plays.entries(ctx)

    assert proposals(result, plays.DIP) == ["F"]
    assert skips(result) == []


def test_a_dip_with_no_regime_or_no_sector_is_not_gated():
    """Absent input means the gate is not applied."""
    ctx = plays_context(config=config(regime_v2_enabled=True))
    assert proposals(plays.entries(ctx), plays.DIP) == ["F"]

    ctx = v2_context(sectors={F_SECTOR_ETF: {"last": 93.0, "sma200": 95.0}})
    ctx["sectors"] = {}
    assert proposals(plays.entries(ctx), plays.DIP) == ["F"]


def test_high_conviction_only_lets_a_dip_through_in_a_strong_sector():
    inputs = strong_and_weak((F_SECTOR_ETF, "XLK", "XLE"), below_two_hundred(7))
    ctx = v2_context(**inputs)
    assert ctx["regime"]["posture"] == regimes.HIGH_CONVICTION_ONLY

    result = plays.entries(ctx)

    assert proposals(result, plays.DIP) == ["F"]
    assert proposal(result, "F").notional_multiplier == pytest.approx(0.5)


def test_high_conviction_only_skips_a_dip_in_a_weak_sector():
    inputs = strong_and_weak(("XLK", "XLE", "XLV"), below_two_hundred(7))
    ctx = v2_context(**inputs)
    assert ctx["regime"]["posture"] == regimes.HIGH_CONVICTION_ONLY

    result = plays.entries(ctx)

    assert proposals(result, plays.DIP) == []
    assert skips(result, "F") == [plays.SKIP_SECTOR_RS_WEAK]


def test_high_conviction_only_with_no_relative_strength_skips_with_its_own_code():
    ctx = v2_context(sectors=below_two_hundred(7))
    assert ctx["regime"]["posture"] == regimes.HIGH_CONVICTION_ONLY
    assert ctx["regime"]["sector_rs"] == {}

    result = plays.entries(ctx)

    assert proposals(result, plays.DIP) == []
    assert skips(result, "F") == [plays.SKIP_RS_UNAVAILABLE]


def test_no_new_positions_skips_every_entry_and_names_the_posture_and_breadth():
    ctx = v2_context(sectors=below_two_hundred(9))
    assert ctx["regime"]["posture"] == regimes.NO_NEW_POSITIONS

    result = plays.entries(ctx)

    assert result.candidates == []
    assert skips(result, "F") == [plays.SKIP_REGIME]
    (skip,) = result.skipped
    assert "NO_NEW_POSITIONS" in skip.message and "18%" in skip.message


def test_reduced_size_halves_the_size_and_keeps_the_candidate():
    ctx = v2_context(sectors=below_two_hundred(5))
    assert ctx["regime"]["posture"] == regimes.REDUCED_SIZE

    result = plays.entries(ctx)

    assert proposals(result, plays.DIP) == ["F"]
    assert proposal(result, "F").notional_multiplier == pytest.approx(0.5)


def test_no_posture_is_enforced_with_the_flag_off():
    ctx = v2_context(v2=False, sectors=below_two_hundred(9))

    result = plays.entries(ctx)

    assert proposals(result, plays.DIP) == ["F"]
    assert proposal(result, "F").notional_multiplier == 1.0


def test_a_top_up_of_a_working_position_is_untouched_by_the_posture():
    """`top_up` adds to a winner already held; the posture gates new entries."""

    def topped(v2: bool):
        ctx = v2_context(v2=v2, sectors=below_two_hundred(9))
        ctx["candidates"] = []
        ctx["positions"] = [position(entry_price=14.00)]
        ctx["signals"]["F"].update({"rsi": 55.0, "sma_50": 14.20})
        return plays.entries(ctx)

    assert [c.as_dict() for c in topped(True).candidates] == [
        c.as_dict() for c in topped(False).candidates
    ]
    assert proposals(topped(True)) == ["F"]
