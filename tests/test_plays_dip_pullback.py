"""DIP reshaped as pullback-in-uptrend.

Two things this file has to establish and keeps separate:

1. **Off is off.** `play_dip_pullback_enabled` absent, or false, is §6.2
   unchanged — the same proposal, the same conviction, the same sentence. Every
   DIP golden depends on that and none of them says so out loud.
2. **On, each of A's six gates is load-bearing and is named in the refusal.**
   The whole reason to run this in the shadow ledger is being able to read
   which gate cost a candidate; a play that refuses in silence, or refuses with
   one generic message, answers nothing.

The fixture is `plays_context`'s F: RSI 27, last 14.88, 50-day 15.20, 200-day
13.50 — which is already a −2.1% give-back against the 50-day inside a trend,
so the baseline day *is* a valid A candidate and each test moves exactly one
number away from it.
"""

from __future__ import annotations

import pytest

from src import plays
from tests.factories import config, plays_context, position

# A's liquidity gate is stated in dollars, and `plays_context`'s F trades at
# ~$14.88 on 55M average shares — $818M a day, comfortably over the $20M floor.
# Named here so a test that wants to fail the gate moves the share count and
# not something that happens to also change the price.
THIN_AVERAGE_VOLUME = 1_000_000


def pullback_ctx(**overrides) -> dict:
    """`plays_context` with §4.3 A switched on, and nothing else changed."""
    ctx = plays_context(**overrides)
    ctx["config"] = config(play_dip_pullback_enabled=True)
    return ctx


def only(result) -> object:
    assert len(result.candidates) + len(result.skipped) == 1, result
    return (result.candidates + result.skipped)[0]


# --- 1. the switch ----------------------------------------------------------


def test_the_shipped_config_is_still_section_62():
    """Absent key, §6.2 rule. `plays_context` carries the shipped config."""
    assert plays.pullback_enabled(plays_context()) is False

    cand = plays.entries(plays_context()).candidates[0]

    # §6.2's own conviction: 7% below the 20-day at DIP_DEPTH_FULL_SCALE 0.10.
    assert cand.play_id == plays.DIP
    assert cand.conviction == 0.7
    assert "below the 20-day mean" in cand.detail


def test_an_explicit_false_is_the_same_as_absent():
    ctx = plays_context()
    ctx["config"] = config(play_dip_pullback_enabled=False)

    assert plays.entries(ctx).candidates[0].detail == (
        plays.entries(plays_context()).candidates[0].detail
    )


def test_switched_on_the_same_day_is_a_different_play():
    """Same name, same fixture, a proposal that measures something else.

    A is not a loosening of §6.2 and it is not a tightening either — it is a
    different question about the same tape, and the sentence in the runlog has
    to say which one was asked.
    """
    cand = plays.entries(pullback_ctx()).candidates[0]

    assert cand.play_id == plays.DIP
    assert "against the 50-day" in cand.detail
    assert "ADV" in cand.detail and "spread" in cand.detail
    assert "below the 20-day mean" not in cand.detail


# --- 2. the six gates -------------------------------------------------------


def test_rsi_over_the_ceiling_is_silent_not_a_skip():
    """The trigger, and the one refusal that stays silent.

    `plays.core`'s standing rule: a play whose *trigger* never fired records
    nothing, because a note per non-triggering name is hundreds of rows a cycle
    saying nothing happened.
    """
    ctx = pullback_ctx()
    ctx["signals"]["F"]["rsi"] = 36.0

    result = plays.entries(ctx)

    assert result.candidates == [] and result.skipped == []


def test_rsi_between_30_and_35_fires_where_section_62_would_not():
    """A's ceiling is 35, §6.2's is 30 — the one direction A is looser in."""
    ctx = pullback_ctx()
    ctx["signals"]["F"]["rsi"] = 34.0

    assert plays.entries(ctx).candidates[0].play_id == plays.DIP

    shipped = plays_context()
    shipped["signals"]["F"]["rsi"] = 34.0
    assert plays.entries(shipped).candidates == []


def test_a_name_below_the_200_day_band_is_a_falling_knife():
    ctx = pullback_ctx()
    # 12.00 is more than 3% under the 13.50 200-day.
    ctx["quotes"]["F"].update({"last": 12.00, "bid": 11.99, "ask": 12.01})
    ctx["signals"]["F"]["sma_50"] = 12.20

    skip = only(plays.entries(ctx))

    assert skip.code == plays.ENTRY_CONDITIONS_UNMET
    assert "falling knife" in skip.message


def test_a_name_over_its_50_day_has_no_pullback_to_buy():
    ctx = pullback_ctx()
    ctx["signals"]["F"]["sma_50"] = 14.00  # last 14.88 is +6.3% over it

    skip = only(plays.entries(ctx))

    assert "+6.3% against its 50-day" in skip.message
    assert "-5%..0% pullback band" in skip.message


def test_a_name_far_under_its_50_day_has_already_broken():
    """The half of the band §6.2 has no equivalent for, and A's whole point.

    §6.2 *rewards* this name — depth below the 20-day is its conviction scale —
    and A refuses it. A 9% hole inside a 3% band of the 200-day is not a
    give-back in a trend, it is arriving at the last support there is.
    """
    ctx = pullback_ctx()
    ctx["signals"]["F"]["sma_50"] = 16.35  # last 14.88 is -9.0% under it

    skip = only(plays.entries(ctx))

    assert "-9.0% against its 50-day" in skip.message

    shipped = plays_context()
    shipped["signals"]["F"]["sma_50"] = 16.35
    assert shipped_takes_it(shipped)


def shipped_takes_it(ctx) -> bool:
    return [c.play_id for c in plays.entries(ctx).candidates] == [plays.DIP]


def test_a_sector_the_regime_will_not_buy_into_is_refused_by_name():
    from src import regime as regimes
    from tests.factories import ETFS, regime_inputs

    ctx = pullback_ctx()
    ctx["etfs"] = ETFS
    # F is Consumer Discretionary, whose ETF is XLY. Put XLY through its own
    # 200-day by more than `sector_breakdown_pct` and the regime calls it a
    # BREAKDOWN.
    inputs = regime_inputs()
    inputs["sectors"]["XLY"] = {"last": 80.0, "sma200": 95.0, "rsi14": 30.0}
    ctx["regime"] = regimes.evaluate(inputs, ctx["config"]).as_dict()

    skip = only(plays.entries(ctx))

    assert "Consumer Discretionary (XLY) is breakdown today" in skip.message


def test_an_unknown_sector_is_kept_rather_than_refused():
    """Too generous is a wasted judgment; too strict is a play that starves.

    The same direction `cli._drop_blocked_sectors` and `regime._sector_state`
    both already fail in.
    """
    ctx = pullback_ctx()
    ctx["sectors"] = {}

    assert plays.entries(ctx).candidates[0].play_id == plays.DIP


def test_a_thin_name_is_under_the_adv_floor():
    ctx = pullback_ctx()
    ctx["fundamentals"]["F"]["average_volume"] = THIN_AVERAGE_VOLUME

    skip = only(plays.entries(ctx))

    assert "$14,880,000 a day" in skip.message
    assert "$20,000,000 floor" in skip.message


def test_an_unmeasurable_adv_is_refused_rather_than_assumed_liquid():
    """Opposite direction from the sector gate, and deliberately.

    An absent sector is a gap in a *classification*; an absent average volume
    is a gap in the number the gate is about. A liquidity floor that passes
    everything it cannot measure is not a liquidity floor.
    """
    ctx = pullback_ctx()
    ctx["fundamentals"]["F"] = {"volume": 60_000_000}

    skip = only(plays.entries(ctx))

    assert "no average volume on file" in skip.message


def test_a_wide_spread_is_refused_at_a_tighter_line_than_section_43_f():
    ctx = pullback_ctx()
    ctx["quotes"]["F"].update({"bid": 14.80, "ask": 14.92})  # 0.81% of the mid

    skip = only(plays.entries(ctx))

    assert "0.81% spread is over §4.3 A's 0.20% ceiling" in skip.message
    # F's own gate is tighter than the account-wide one F would also have
    # tripped, which is why it is a separate number and not a shared one.
    assert plays.no_trade.MAX_SPREAD_PCT > 0.002


def test_a_one_sided_quote_cannot_be_measured_and_is_refused():
    ctx = pullback_ctx()
    ctx["quotes"]["F"] = {"last": 14.88, "ask": 14.87}

    assert "no two-sided quote" in only(plays.entries(ctx)).message


def test_the_earnings_clearance_survives_the_reshape():
    """§4.3 A does not restate it, and it is not A's to drop: "the system never
    holds through a print" is an account rule, not a property of this trigger."""
    ctx = pullback_ctx()
    ctx["earnings"]["F"]["sessions_until_report"] = 2

    skip = only(plays.entries(ctx))

    assert skip.code == plays.EARNINGS_VETO


# --- 3. conviction ----------------------------------------------------------


@pytest.mark.parametrize(
    "sma_50,expected",
    [
        (15.20, 0.42),  # -2.1% into a 5% band -> 0.42 depth, beats extremity
        (15.65, 0.984),  # -4.9%, nearly the full band
    ],
)
def test_conviction_is_depth_into_as_own_band(sma_50, expected):
    ctx = pullback_ctx()
    ctx["signals"]["F"]["sma_50"] = sma_50

    assert plays.entries(ctx).candidates[0].conviction == pytest.approx(expected, abs=0.01)


def test_rsi_extremity_can_outrank_a_shallow_pullback():
    """The same `max`, not blend, §6.2 uses: a very low RSI raises conviction
    above what a 1% give-back alone would give, and never lowers it."""
    ctx = pullback_ctx()
    ctx["signals"]["F"]["sma_50"] = 15.00  # -0.8%, a 0.16 depth term
    ctx["signals"]["F"]["rsi"] = 18.0  # 17 points under the 35 ceiling -> 0.85

    assert plays.entries(ctx).candidates[0].conviction == pytest.approx(0.85, abs=0.01)


def test_sitting_exactly_on_the_50_day_is_no_pullback_at_all():
    ctx = pullback_ctx()
    ctx["signals"]["F"]["sma_50"] = 14.88
    ctx["signals"]["F"]["rsi"] = 35.0  # both terms zero

    skip = only(plays.entries(ctx))

    assert skip.code == plays.NO_CONVICTION
    assert "sitting exactly on its 50-day" in skip.message


# --- 4. the thresholds really are config ------------------------------------


def test_every_threshold_moves_from_config():
    """The §17 exception, exercised. This is the one play whose shape a config
    edit can change, and a test that never moved one would leave the exception
    undefended — `docs/TUNING.md` §4 records why it exists."""
    ctx = pullback_ctx()
    ctx["signals"]["F"]["rsi"] = 34.0
    ctx["config"] = config(play_dip_pullback_enabled=True, dip_pullback_rsi_max=30.0)

    assert plays.entries(ctx).skipped == [] and plays.entries(ctx).candidates == []

    ctx["config"] = config(play_dip_pullback_enabled=True, dip_pullback_max_spread_pct=0.0)
    assert "over §4.3 A's 0.00% ceiling" in only(plays.entries(ctx)).message

    ctx["config"] = config(play_dip_pullback_enabled=True, dip_pullback_min_adv_usd=1e12)
    assert "under §4.3 A's $1,000,000,000,000 floor" in only(plays.entries(ctx)).message

    ctx["config"] = config(play_dip_pullback_enabled=True, dip_pullback_sma50_band_pct=0.01)
    assert "outside §4.3 A's -1%..0% pullback band" in only(plays.entries(ctx)).message


def test_the_defaults_are_the_plays_own_numbers():
    """`would_open` and `mc_prefilter` carry no config at all, so the module
    fallbacks have to be A's stated shape rather than something looser."""
    assert plays.DIP_PULLBACK_RSI_MAX_DEFAULT == 35.0
    assert plays.DIP_PULLBACK_SMA50_BAND_PCT_DEFAULT == 0.05
    assert plays.DIP_TREND_BAND_PCT == 0.03


# --- 5. the target, which is A's other half ---------------------------------


def test_the_shipped_dip_target_is_a_flat_six_percent():
    from src.plays import protection

    held = position()  # DIP, entry 15.00, ATR 0.45

    assert protection.target_price(held, plays_context()) == (15.90, plays.DIP, 0.06)


def test_under_the_reshape_the_target_is_15x_the_stop_distance():
    """§4.3 A states its target as an R-multiple, and every other play in this
    system states a flat percentage. With a 3xATR stop on a 0.45 ATR the
    distance is 1.35, so the target is 2.025 over a 15.00 entry — 13.5%, not 6%."""
    from src.plays import protection

    ctx = pullback_ctx()
    held = position()

    assert plays.target_pct(held, ctx) == (0.135, "DIP 1.5R")
    assert protection.target_price(held, ctx) == (17.02, "DIP 1.5R", 0.135)


def test_the_r_multiple_moves_with_the_names_own_volatility():
    """The whole point of stating it in stop distances: a quiet name takes a
    smaller move to be right, a violent one a larger one, and both are the same
    reward-to-risk trade."""
    ctx = pullback_ctx()
    ctx["signals"]["F"]["atr"] = 0.90

    assert plays.target_pct(position(), ctx) == (0.27, "DIP 1.5R")


def test_the_multiple_is_configurable_with_the_rest_of_as_shape():
    ctx = pullback_ctx()
    ctx["config"] = config(
        play_dip_pullback_enabled=True, dip_pullback_target_stop_multiple=3.0
    )

    assert plays.target_pct(position(), ctx) == (0.27, "DIP 3R")


def test_the_exit_sells_at_the_price_the_resting_limit_rests_at():
    """`exits` and `protection` calling one resolver is the point of it. A
    target the broker rests at 17.02 while the cycle sells at 15.90 would
    double-exit the position on every cycle in between."""
    from src.plays import protection

    ctx = pullback_ctx(candidates=[])
    ctx["positions"] = [position(market_value=17.05)]
    ctx["quotes"]["F"] = {"bid": 17.03, "ask": 17.07, "last": 17.05}

    leaving = plays.exits(ctx).exits
    limit = protection.target_price(position(), ctx)[0]

    assert [(e.symbol, e.code) for e in leaving] == [("F", plays.TARGET)]
    assert "DIP 1.5R's 14% target of 17.02" in leaving[0].reason
    assert limit == 17.02


def test_at_1590_the_reshaped_dip_holds_where_the_shipped_one_sells():
    ctx = pullback_ctx(candidates=[])
    ctx["positions"] = [position(market_value=15.95)]
    ctx["quotes"]["F"] = {"bid": 15.93, "ask": 15.97, "last": 15.95}

    assert plays.exits(ctx).exits == []

    shipped = plays_context(candidates=[])
    shipped["positions"] = [position(market_value=15.95)]
    shipped["quotes"]["F"] = {"bid": 15.93, "ask": 15.97, "last": 15.95}
    assert [e.code for e in plays.exits(shipped).exits] == [plays.TARGET]


def test_a_missing_atr_falls_back_to_dips_flat_target_rather_than_to_none():
    """An R-multiple needs an ATR. Dropping the target on a data gap would
    cancel a resting broker-side limit, which is worse than resting at 6% —
    and the owner string says which of the two was used."""
    ctx = pullback_ctx()
    ctx["signals"]["F"] = {"rsi": 27.0, "sma_50": 15.20, "sma_200": 13.50}

    assert plays.target_pct(position(), ctx) == (0.06, plays.DIP)


# --- 6. A end to end, as the shadow ledger runs it ---------------------------


def test_the_shadow_variant_turns_on_exactly_a_and_its_sizing():
    """`E_dip_pullback` is the whole of row A: the reshaped trigger *and* the
    risk budget its target is stated in. Pinned here rather than only in
    `services/core` so `src/` notices if the two drift apart."""
    from services.core.core.shadow import VARIANTS

    variant = next(v for v in VARIANTS if v.name == "E_dip_pullback")
    assert variant.overrides == {
        "play_dip_pullback_enabled": True,
        "risk_sizing_enabled": True,
    }


def test_an_a_candidate_sizes_to_the_risk_budget_not_the_slot():
    """$10,000 of equity is a $392 rung. At a 3.20 ATR the 3xATR stop is $9.60
    away, ~63% of the ~$15.19 limit, so $100 of risk funds only ~$158 — well
    under the rung — and the order drops from the rung's 25 shares to 10.
    """
    from src import sizing

    def sized(atr: float, risk: bool):
        ctx = pullback_ctx()
        ctx["config"] = config(
            play_dip_pullback_enabled=True, risk_sizing_enabled=risk
        )
        ctx["account"] = {
            "equity_total": 10_000.0,
            "cash": 10_000.0,
            "buying_power": 10_000.0,
            "settled_cash": 10_000.0,
        }
        ctx["control"] = {"high_water_mark": 10_000.0}
        ctx["signals"]["F"]["atr"] = atr
        entering = plays.entries(ctx)
        assert [c.play_id for c in entering.candidates] == [plays.DIP]
        result = sizing.size([c.as_dict() for c in entering.candidates], ctx)
        return result.ladder, result.entries[0]

    ladder, on = sized(3.20, risk=True)
    _, off = sized(3.20, risk=False)

    assert ladder.notional == pytest.approx(392.0)
    assert off.notional == pytest.approx(392.0, abs=off.price)  # the whole rung
    assert on.notional < off.notional
    assert on.quantity < off.quantity
    # 1% of $10,000 over a (3 x 3.20 / 15.19) stop = $158.23 of exposure.
    assert on.notional == pytest.approx(0.01 * 10_000 / (3 * 3.20 / on.price),
                                        rel=0.07)
