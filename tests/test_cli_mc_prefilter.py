"""Step 6b's cheap gate, `src.cli.mc_prefilter` — cycle-prompt.md §"Step 6b".

A 24-candidate DIP scanner pool ran the Monte Carlo check on every name, then
every one of them failed the identical 200-day check inside `decide` a step
later — 48 wasted calls (a `get_equity_historicals` fetch plus a Vercel round
trip, per candidate). `plays.would_open` already answers "does this candidate's
own play trigger fire today" for free, off data Step 6 has already fetched, so
this narrows Step 6b's loop to the candidates that could not already be ruled
out."""

import pytest
from src import cli
from tests.factories import cli_payload


# These tests predate an earlier retirement and shelving of the event plays
# and use them as they then ran. See `all_plays_live` in conftest.py.
pytestmark = pytest.mark.usefixtures("all_plays_live")


def prefiltered(**overrides) -> dict:
    return cli.mc_prefilter(cli_payload(**overrides), cli.load_config())


def test_a_live_dip_trigger_is_checked():
    """PFE: RSI 27, holding its 200-day — §6.2 fires, so the two calls are worth
    spending."""
    result = prefiltered()
    assert result["checked"] == ["PFE"]
    assert result["skipped_gate_failed"] == []


def test_held_positions_never_enter_the_pool():
    """F is held (cli_payload's baseline position) and quiet (RSI 45) besides
    — it must not appear in either list, the same exclusion Step 6b's own text
    already gives `mc_check` for every position."""
    result = prefiltered()
    assert "F" not in result["checked"]
    assert "F" not in result["skipped_gate_failed"]


def test_a_candidate_below_its_200_day_is_skipped_not_checked():
    """Oversold at RSI 27 like PFE, but under its 200-day — a falling knife,
    §6.2's own veto. `would_open` returns None for it, same as `decide` would
    a step later, so no MC call is worth spending."""
    result = prefiltered(
        triggers=[{"symbol": "PFE", "play_id": "DIP"}],
        signals={
            "PFE": {
                "rsi": 27.0,
                "sma_20": 27.00,
                "sma_50": 25.90,
                "sma_200": 30.00,  # price (25.11) is below this now
                "atr": 0.50,
            }
        },
    )
    assert result["checked"] == []
    assert result["skipped_gate_failed"] == ["PFE"]


def test_a_macro_target_that_clears_its_gate_is_checked():
    """MACRO proposes off `macro`/`macro_map` rather than a per-symbol trigger
    row, so it reaches the pool through `macro_targets` instead of `triggers`
    — both feed the same union. `macro_map` is always the real checked-in
    `config/macro_map.json`, whose CPI row tilts `above` toward XLE/XLF."""
    result = prefiltered(
        triggers=[],
        macro_targets=["XLE"],
        macro={
            "releases": [
                {"series": "CPI", "first_seen": "2026-08-03", "surprise_pct": 0.30}
            ]
        },
        # MACRO now confirms the target is holding its 200-day the same way
        # DIP and PEAD already do, so `would_open` needs XLE's own
        # signal/quote — the same data Step 6 already fetched for every
        # name in the union, `macro_targets` included.
        signals={"XLE": {"sma_200": 90.0}},
        quotes={"XLE": {"bid": 90.95, "ask": 91.05, "last": 91.00}},
    )
    assert result["checked"] == ["XLE"]
    assert result["skipped_gate_failed"] == []


def test_no_triggers_or_macro_targets_is_an_empty_pool():
    result = prefiltered(triggers=[], macro_targets=[])
    assert result["checked"] == []
    assert result["skipped_gate_failed"] == []
