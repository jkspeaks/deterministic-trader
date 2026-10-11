"""§17's SURGE — the full-send momentum play.

SURGE proposes a name only when `full_send_enabled` is true in config and the
name is in a clean stacked-MA uptrend with momentum RSI and a move already
underway. It is the trigger half; `test_sizing_surge.py` covers what sizing
then does with the whole available cash, and `test_guard_concentration.py`
covers guard waiving §4.4's caps for it.
"""

from __future__ import annotations

import pytest

from src import plays
from tests.factories import plays_context, proposal, proposals, skips

# A clean uptrend: price 110 > 20d 105 > 50d 100 > 200d 90, RSI 65 (inside the
# 55-80 band), price 10% over the 50-day (clears the 2% "move underway" floor).
UP_SIGNALS = {"rsi": 65.0, "sma_20": 105.0, "sma_50": 100.0, "sma_200": 90.0, "atr": 2.0}
UP_QUOTE = {"bid": 109.95, "ask": 110.05, "last": 110.00}
UP_EARNINGS = {
    "status": "known",
    "next_report_date": "2026-10-28",
    "sessions_until_report": 40,
    "sessions_since_report": None,
    "eps_surprise_pct": None,
    "reaction_pct": None,
}


# These tests predate an earlier retirement and shelving of the event plays
# and use them as they then ran. See `all_plays_live` in conftest.py.
pytestmark = pytest.mark.usefixtures("all_plays_live")


def surge_context(**overrides):
    base = {
        "candidates": ["UP"],
        "signals": {"UP": dict(UP_SIGNALS)},
        "quotes": {"UP": dict(UP_QUOTE)},
        "sectors": {"UP": "Technology"},
        "earnings": {"UP": dict(UP_EARNINGS)},
        # The switch lives in edge_control (operator-flippable), not config.
        "control": {"full_send_enabled": True},
    }
    base.update(overrides)
    return plays_context(**base)


def test_the_switch_off_means_surge_never_proposes():
    result = plays.entries(surge_context(control={"full_send_enabled": False}))

    assert proposals(result, plays.SURGE) == []
    assert skips(result, "UP") == []


def test_a_clean_stacked_uptrend_fires():
    result = plays.entries(surge_context())

    assert proposals(result, plays.SURGE) == ["UP"]
    cand = proposal(result, "UP", plays.SURGE)
    assert cand.full_deployment is True
    # _scaled(1.10 - 1, 0.15) == 0.6667
    assert round(cand.conviction_raw, 4) == 0.6667


def test_a_broken_ma_stack_is_skipped():
    # 50-day above the 20-day: the trend is not stacked on every horizon.
    result = plays.entries(
        surge_context(signals={"UP": {**UP_SIGNALS, "sma_20": 99.0}})
    )

    assert proposals(result, plays.SURGE) == []
    assert plays.ENTRY_CONDITIONS_UNMET in skips(result, "UP")


def test_rsi_below_the_momentum_band_is_skipped():
    result = plays.entries(surge_context(signals={"UP": {**UP_SIGNALS, "rsi": 50.0}}))

    assert proposals(result, plays.SURGE) == []
    assert plays.ENTRY_CONDITIONS_UNMET in skips(result, "UP")


def test_rsi_above_the_band_is_skipped_as_exhausted():
    result = plays.entries(surge_context(signals={"UP": {**UP_SIGNALS, "rsi": 85.0}}))

    assert proposals(result, plays.SURGE) == []


def test_a_move_not_yet_clear_of_the_50_day_is_skipped():
    # Price only 1% over the 50-day, short of the 2% "underway" floor.
    result = plays.entries(
        surge_context(quotes={"UP": {"bid": 100.9, "ask": 101.0, "last": 101.0}})
    )

    assert proposals(result, plays.SURGE) == []
    assert plays.ENTRY_CONDITIONS_UNMET in skips(result, "UP")


def test_earnings_inside_the_window_veto_the_full_send():
    result = plays.entries(
        surge_context(
            earnings={"UP": {**UP_EARNINGS, "sessions_until_report": 2}}
        )
    )

    assert proposals(result, plays.SURGE) == []
    assert plays.EARNINGS_VETO in skips(result, "UP")


def test_surge_does_not_need_a_play_state_row():
    # Every other play fails closed on a missing regime row; SURGE reads an
    # absent row as on, because `full_send_enabled` is its switch.
    ctx = surge_context()
    ctx["play_state"] = {k: v for k, v in ctx["play_state"].items()}
    ctx["play_state"].pop("SURGE", None)
    result = plays.entries(ctx)

    assert proposals(result, plays.SURGE) == ["UP"]


def test_an_explicit_dark_regime_still_stops_surge():
    ctx = surge_context()
    ctx["play_state"]["SURGE"] = {"regime_fit": 0.0, "reason": "operator override"}
    result = plays.entries(ctx)

    assert proposals(result, plays.SURGE) == []
    assert plays.REGIME_DARK in skips(result, "UP")
