"""dip_recovery.signal — all seven entry gates and the 0-100 score.

Phase 5 added the last two conditions: the bar-derived intraday/5-day-high
depth and the lower Bollinger Band. Both are hard gates, both fail closed on
missing bars.
"""

from __future__ import annotations

from src.dip_recovery import Skipped, entry_signal
from src.dip_recovery.core import ENTRY_CONDITIONS_UNMET, METRICS_UNAVAILABLE, EntryProposal
from tests.dip_recovery_factories import dip1_bars, dip_recovery_context


def signal_of(**overrides):
    ctx = dip_recovery_context(**overrides)
    return entry_signal("DIP1", ctx)


def test_baseline_passes():
    outcome = signal_of()
    assert isinstance(outcome, EntryProposal)
    assert outcome.score >= 70


def test_rsi_above_gate_is_skipped():
    outcome = signal_of(signals={"DIP1": {"rsi": 40.0, "sma_20": 50.0, "sma_200": 40.0}})
    assert isinstance(outcome, Skipped)
    # A real reading that fails the threshold is ENTRY_CONDITIONS_UNMET, never
    # METRICS_UNAVAILABLE — and the value is in the message, not just implied.
    assert outcome.code == ENTRY_CONDITIONS_UNMET
    assert "40.0" in outcome.message


def test_missing_rsi_is_skipped_as_metrics_unavailable_not_conditions_unmet():
    # This used to share ENTRY_CONDITIONS_UNMET and one message with the
    # above case, so a real RSI outage would have read identically to an
    # ordinary, expected rejection.
    outcome = signal_of(signals={"DIP1": {"rsi": None, "sma_20": 50.0, "sma_200": 40.0}})
    assert isinstance(outcome, Skipped)
    assert outcome.code == METRICS_UNAVAILABLE
    assert outcome.code != ENTRY_CONDITIONS_UNMET


def test_relvol_below_floor_is_skipped():
    outcome = signal_of(fundamentals={"DIP1": {"volume": 900_000, "average_volume": 1_000_000}})
    assert isinstance(outcome, Skipped)
    assert outcome.code == ENTRY_CONDITIONS_UNMET
    assert "0.9" in outcome.message


def test_missing_relvol_is_skipped_as_metrics_unavailable_not_conditions_unmet():
    outcome = signal_of(fundamentals={"DIP1": {"volume": None, "average_volume": None}})
    assert isinstance(outcome, Skipped)
    assert outcome.code == METRICS_UNAVAILABLE
    assert outcome.code != ENTRY_CONDITIONS_UNMET


def test_far_below_the_200_day_is_skipped():
    # The structural filter is a band: more than trend_band_pct (10%) below the
    # 200-day is a breakdown, not a dip. sma_200 55.0 puts the floor at 49.5,
    # above DIP1's 45.95 last trade.
    outcome = signal_of(signals={"DIP1": {"rsi": 20.0, "sma_20": 50.0, "sma_200": 55.0}})
    assert isinstance(outcome, Skipped)
    assert "200-day" in outcome.message


def test_just_below_the_200_day_still_passes():
    # The relaxation's whole point: a pullback through the long trend is in
    # scope. sma_200 48.0 -> floor 43.2, and DIP1's 45.95 sits inside the band.
    outcome = signal_of(signals={"DIP1": {"rsi": 20.0, "sma_20": 50.0, "sma_200": 48.0}})
    assert isinstance(outcome, EntryProposal)


def test_freefall_is_skipped():
    # The close ten sessions back (closes[-11]) is 56.0; DIP1's 45.95 last trade
    # is 17.9% under it, past the 15% freefall veto — a crash, not a dip, and it
    # trips before any of the bar-depth gates.
    closes = [56.0] * 10 + [54.0, 52.0, 51.0, 50.0, 49.0, 48.0, 47.5, 47.0, 46.5, 46.0]
    bars = [{"high": 58.0, "low": 45.0, "close": c} for c in closes]
    outcome = signal_of(dip_recovery_bars={"DIP1": bars})
    assert isinstance(outcome, Skipped)
    assert "freefall" in outcome.message


def test_not_below_20_day_mean_is_skipped():
    outcome = signal_of(quotes={"DIP1": {"bid": 50.0, "ask": 50.1, "last": 50.05}})
    assert isinstance(outcome, Skipped)


def test_score_below_floor_is_skipped():
    """RSI just inside the gate and only just below the 20-day mean: neither
    factor carries enough weight alone to clear 70.
    """
    outcome = signal_of(
        signals={"DIP1": {"rsi": 34.0, "sma_20": 46.5, "sma_200": 40.0}},
        quotes={"DIP1": {"bid": 45.90, "ask": 46.00, "last": 45.95}},
    )
    assert isinstance(outcome, Skipped)


# --- phase 5: the two bar-derived gates -----------------------------------


def test_missing_bars_is_skipped():
    outcome = signal_of(dip_recovery_bars={"DIP1": []})
    assert isinstance(outcome, Skipped)


def test_too_few_bars_for_the_bollinger_period_is_skipped():
    outcome = signal_of(dip_recovery_bars={"DIP1": dip1_bars(10)})
    assert isinstance(outcome, Skipped)


def test_fewer_than_the_five_day_window_is_skipped():
    # A short Bollinger period would clear the period check; the 5-bar
    # intraday/5-day-high lookback still needs five bars.
    outcome = signal_of(
        dip_recovery_config={"entry_thresholds": {"bollinger_period": 3}},
        dip_recovery_bars={"DIP1": dip1_bars(4)},
    )
    assert isinstance(outcome, Skipped)


def test_bar_missing_a_required_key_is_skipped():
    bars = dip1_bars(20)
    del bars[-1]["high"]
    outcome = signal_of(dip_recovery_bars={"DIP1": bars})
    assert isinstance(outcome, Skipped)


def test_bar_that_is_not_a_mapping_is_skipped():
    bars = dip1_bars(20)
    bars[-1] = None
    outcome = signal_of(dip_recovery_bars={"DIP1": bars})
    assert isinstance(outcome, Skipped)


def test_bar_with_a_non_numeric_value_is_skipped():
    bars = dip1_bars(20)
    bars[-1]["close"] = "n/a"
    outcome = signal_of(dip_recovery_bars={"DIP1": bars})
    assert isinstance(outcome, Skipped)


def test_not_far_enough_below_intraday_high_is_skipped():
    bars = [{"high": 46.30, "low": 45.0, "close": c} for c in
            [48.5 if i % 2 == 0 else 51.5 for i in range(20)]]
    outcome = signal_of(dip_recovery_bars={"DIP1": bars})
    assert isinstance(outcome, Skipped)
    assert "intraday high" in outcome.message


def test_below_intraday_but_not_below_5day_high_is_skipped():
    # 3.26% below every high — clears the 2.0% intraday floor, short of the
    # 4.0% five-day floor.
    bars = [{"high": 47.50, "low": 45.0, "close": c} for c in
            [48.5 if i % 2 == 0 else 51.5 for i in range(20)]]
    outcome = signal_of(dip_recovery_bars={"DIP1": bars})
    assert isinstance(outcome, Skipped)
    assert "5-day high" in outcome.message


def test_price_above_lower_bollinger_band_is_skipped():
    # Highs at 53.00 clear both depth floors; closes alternating 45/55 widen
    # the band (stdev 5.0) so its lower edge sits at 40.00, below the 45.95
    # last trade.
    # Parity flipped so closes[-11] is 45.0, not 55.0 — the last trade is then
    # 2% *above* the ten-sessions-back close, clear of the freefall veto, so the
    # Bollinger gate is the one that fires.
    bars = [{"high": 53.00, "low": 40.0, "close": 55.0 if i % 2 == 0 else 45.0}
            for i in range(20)]
    outcome = signal_of(dip_recovery_bars={"DIP1": bars})
    assert isinstance(outcome, Skipped)
    assert "Bollinger Band" in outcome.message


def test_passing_proposal_carries_the_bar_factors():
    outcome = signal_of()
    assert isinstance(outcome, EntryProposal)
    assert round(outcome.factors["pct_below_intraday_high"], 1) == 13.3
    assert round(outcome.factors["pct_below_5day_high"], 1) == 13.3
    assert outcome.factors["bollinger_lower"] == 47.0


# --- P2: the relvol gate, time-normalised behind a flag ---------
#
# The baseline tick is 11:47 ET — minute 137 of 390. 0.5x of a full day by
# then is 1.42x on a full-session basis.


def _relvol_ctx(tnorm, volume, now="2026-08-31T15:47:00Z", **config_overrides):
    from tests.dip_recovery_factories import dip_recovery_context

    ctx = dip_recovery_context(
        fundamentals={"DIP1": {"volume": volume, "average_volume": 1_000_000}}
    )
    ctx["config"] = {
        **ctx["config"], "dip_recovery_relvol_time_normalised": tnorm, **config_overrides,
    }
    ctx["cycle"]["now"] = now
    return ctx


def test_with_the_flag_off_the_raw_ratio_decides_as_before():
    from src.dip_recovery import entry_signal

    refused = entry_signal("DIP1", _relvol_ctx(False, 500_000))
    assert refused.code == ENTRY_CONDITIONS_UNMET
    # ...and the record says what the other rule would have read.
    assert "0.5000) is below the 1.2x floor; 1.4234 time-normalised at minute 137" in refused.message

    assert isinstance(entry_signal("DIP1", _relvol_ctx(False, 1_200_000)), EntryProposal)


def test_with_the_flag_on_half_a_days_volume_by_late_morning_passes():
    from src.dip_recovery import entry_signal

    outcome = entry_signal("DIP1", _relvol_ctx(True, 500_000))

    assert isinstance(outcome, EntryProposal)
    assert outcome.factors["relvol"] == 0.5
    assert round(outcome.factors["relvol_tnorm"], 4) == 1.4234


def test_with_the_flag_on_a_thin_reading_is_still_refused():
    from src.dip_recovery import entry_signal

    outcome = entry_signal("DIP1", _relvol_ctx(True, 400_000))  # 1.14x normalised

    assert outcome.code == ENTRY_CONDITIONS_UNMET
    assert "time-normalised relative volume (1.1387 at minute 137" in outcome.message


def test_with_the_flag_on_the_first_minutes_are_not_extrapolated():
    from src.dip_recovery import entry_signal
    from src.dip_recovery.core import SKIP_TOO_EARLY_FOR_RVOL

    early = _relvol_ctx(True, 200_000, now="2026-08-31T13:45:00Z")  # 09:45 ET

    assert entry_signal("DIP1", early).code == SKIP_TOO_EARLY_FOR_RVOL


def test_after_the_close_the_normalised_reading_is_the_raw_one():
    """The 18:00 ET tick: the day's volume is the whole day's."""
    from src.dip_recovery import entry_signal

    late = "2026-08-31T22:00:00Z"
    assert entry_signal("DIP1", _relvol_ctx(True, 1_100_000, now=late)).code == ENTRY_CONDITIONS_UNMET
    assert isinstance(entry_signal("DIP1", _relvol_ctx(True, 1_200_000, now=late)), EntryProposal)


def test_missing_volume_is_still_metrics_unavailable_under_the_flag():
    from src.dip_recovery import entry_signal

    assert entry_signal("DIP1", _relvol_ctx(True, None)).code == METRICS_UNAVAILABLE


def test_the_sleeves_too_early_code_is_catalysts_own():
    from src import plays
    from src.dip_recovery.core import SKIP_TOO_EARLY_FOR_RVOL

    assert SKIP_TOO_EARLY_FOR_RVOL == plays.SKIP_TOO_EARLY_FOR_RVOL
