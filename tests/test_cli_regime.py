"""`regime_inputs` at the two seams `cli.py` owns: Step 5's candidate drop and
`decide`'s exposure of what it judged on.

The whole point of doing this in `cli.candidates` rather than in `decide` is
economic — a sector the tape has taken off the board should not cost six
broker calls per name before `plays` declines to buy it — so the tests that
matter are the ones about *which list a name ends up in*, not about the
arithmetic (that is `tests/test_regime.py`).
"""

import pytest
from src import cli, regime as regimes
from tests.factories import cli_payload, core_off, regime_inputs

LOADED = core_off()


# These tests predate an earlier retirement and shelving of the event plays
# and use them as they then ran. See `all_plays_live` in conftest.py.
pytestmark = pytest.mark.usefixtures("all_plays_live")


def dropped_for(result, symbol: str) -> str | None:
    for row in result["candidates_dropped"]:
        if row["symbol"] == symbol:
            return row["reason"]
    return None


def payload(**overrides):
    """The baseline plus a PEAD trigger on AAPL, whose sector is XLK's.

    AAPL rather than the fixture's own PFE because Information Technology is
    the sector these tests peak and break; PFE stays in Health Care as the
    control that must survive every one of them. Each 8-K is inside both the
    PEAD and the CATALYST window, so every symbol here produces two triggers —
    the assertions read the symbol set, not the list.
    """
    base = cli_payload(**overrides)
    base["events"] = {
        "AAPL": [{"type": "8-K", "items": ["2.02"], "sessions_ago": 1}],
        "PFE": [{"type": "8-K", "items": ["2.02"], "sessions_ago": 1}],
    }
    return base


# --- the sector drop -------------------------------------------------------


def test_no_regime_inputs_drops_nothing():
    """Inert until the routine supplies the block. Every cycle that predates
    this key behaves exactly as it did."""
    result = cli.candidates(payload(), LOADED)

    assert sorted({t["symbol"] for t in result["triggers"]}) == ["AAPL", "PFE"]
    assert result["candidates_dropped"] == []


def test_a_peaked_sector_takes_its_names_out_before_the_fetch():
    """The operator's own ask, applied where it saves money: AAPL never
    reaches Step 6's per-symbol loop at all."""
    result = cli.candidates(
        payload(regime_inputs=regime_inputs(sectors={"XLK": {"rsi14": 74.0}})), LOADED
    )

    assert sorted({t["symbol"] for t in result["triggers"]}) == ["PFE"]
    assert "peak" in dropped_for(result, "AAPL")


def test_a_broken_sector_takes_its_names_out_too():
    result = cli.candidates(
        payload(
            regime_inputs=regime_inputs(sectors={"XLK": {"last": 80.0, "sma200": 95.0}})
        ),
        LOADED,
    )

    assert sorted({t["symbol"] for t in result["triggers"]}) == ["PFE"]
    assert "breakdown" in dropped_for(result, "AAPL")


def test_the_drop_names_the_play_it_cost():
    """`candidates_dropped` is the list that answers "why did this play stop
    firing", so a row that named only the symbol would answer half of it."""
    result = cli.candidates(
        payload(regime_inputs=regime_inputs(sectors={"XLK": {"rsi14": 74.0}})), LOADED
    )

    assert "PEAD" in dropped_for(result, "AAPL")


def test_an_ordinary_pullback_in_a_sector_keeps_its_names():
    """`oversold_ok` is tradeable. The band exists so a sector dipping through
    its 200-day is the setup rather than the disqualification."""
    result = cli.candidates(
        payload(
            regime_inputs=regime_inputs(sectors={"XLK": {"last": 93.0, "sma200": 95.0}})
        ),
        LOADED,
    )

    assert sorted({t["symbol"] for t in result["triggers"]}) == ["AAPL", "PFE"]


def test_a_macro_target_is_judged_on_its_own_ticker():
    """A sector ETF *is* the sector — buying XLE while XLE is the thing at a
    peak is the exact case this rule was asked for."""
    base = payload(
        macro={
            "releases": [
                {"series": "CPI", "surprise_pct": 0.5, "sessions_ago": 0},
            ]
        },
        regime_inputs=regime_inputs(sectors={"XLE": {"rsi14": 74.0}}),
    )
    without = cli.candidates(
        {**base, "regime_inputs": None}, LOADED
    )["macro_targets"]

    result = cli.candidates(base, LOADED)

    assert "XLE" in without
    assert "XLE" not in result["macro_targets"]
    # The reason names the sleeve, not the play: `macro_targets` can also carry
    # §4.3 C's oversold ETFs, and the list says which ETFs need prices rather
    # than which play asked for them.
    assert "ETF sleeve does not rotate into it" in dropped_for(result, "XLE")


def test_a_candidate_whose_sector_is_unknown_is_kept():
    """`src/candidates.py`'s own rule: discovery can be too generous and
    cannot afford to be too strict. `plays` still judges what survives."""
    base = payload(regime_inputs=regime_inputs(sectors={"XLK": {"rsi14": 74.0}}))
    base["snapshot"] = {"as_of": "2026-08-02", "holdings": [], "cross_check": []}

    result = cli.candidates(base, LOADED)

    assert sorted({t["symbol"] for t in result["triggers"]}) == ["AAPL", "PFE"]


def test_a_monitor_cycle_replays_the_watchlist_without_re_dropping():
    """Monitor reads back what `full` wrote and re-scans nothing, which is the
    behaviour that keeps a monitor cycle cheap. The sector filter rides along with discovery
    and therefore does not run here either."""
    result = cli.candidates(
        cli_payload(
            now="2026-08-03T15:40:00Z",
            control={
                "last_full_cycle": "2026-08-03T13:40:00Z",
                "today_watchlist": {
                    "trading_day": "2026-08-03",
                    "triggers": [{"symbol": "AAPL", "play_id": "PEAD"}],
                    "macro_targets": [],
                },
            },
            regime_inputs=regime_inputs(sectors={"XLK": {"rsi14": 74.0}}),
        ),
        LOADED,
    )

    assert result["triggers"] == [{"symbol": "AAPL", "play_id": "PEAD"}]
    assert result["candidates_dropped"] == []


# --- decide's own exposure -------------------------------------------------


def test_decide_carries_the_regime_it_judged_on():
    """§6.0's multiplier used to be reconstructable only by reading
    `edge_play_state` as it stood at the moment the cycle ran, which is why
    nobody could see it had been frozen."""
    result = cli.decide(cli_payload(regime_inputs=regime_inputs()), LOADED)

    assert result["regime"]["market_state"] == regimes.RISK_ON
    assert result["regime"]["play_multipliers"]["DIP"] == 1.0
    assert result["regime"]["sector_states"]["XLK"] == regimes.TREND_OK


def test_decide_carries_a_null_regime_when_none_was_supplied():
    """Present and null, not absent: a reader of the runlog can tell "no
    inputs were sent" from "this key predates the change"."""
    assert cli.decide(cli_payload(), LOADED)["regime"] is None


def test_a_risk_off_tape_reaches_the_plays_and_stops_the_entry():
    """End to end at the seam that matters: `decide` puts the regime into the
    one context all three modules read, and PFE — the baseline's only entry —
    is not bought."""
    quiet = cli.decide(cli_payload(), LOADED)
    risk_off = cli.decide(
        cli_payload(regime_inputs=regime_inputs(spy={"last": 580.0, "rv20": 0.42})),
        LOADED,
    )

    assert [o["symbol"] for o in quiet["plan"]["orders"]] == ["PFE"]
    assert risk_off["plan"]["orders"] == []
    assert risk_off["entries"]["candidates"] == []
    assert {s["code"] for s in risk_off["entries"]["skipped"]} == {"REGIME_DARK"}


def test_a_noop_cycle_records_no_regime_even_if_one_was_sent():
    """§13's cheap path fetched nothing, so there is no tape to have judged."""
    result = cli.decide(
        cli_payload(
            now="2026-08-03T14:40:00Z",
            control={
                "last_full_cycle": "2026-08-03T13:40:00Z",
                "positions_held": 0,
                "new_positions": {"date": "2026-08-03", "count": 3},
            },
            regime_inputs=regime_inputs(),
        ),
        LOADED,
    )

    assert result["cycle"]["cycle_type"] == "noop"
    assert result["regime"] is None
