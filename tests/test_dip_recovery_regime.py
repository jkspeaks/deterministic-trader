"""The Dip Recovery sleeve on the day's regime.

The sleeve buys oversold names the way core DIP does, and until now it read no
regime at all — not even `risk_off`. It is wired to the same consumer core DIP
uses (`regime.dip_entry_verdict`), behind the same switch
(`regime_v2_enabled`), with the same codes. What these pin:

1. **New money only.** An exit is identical in every posture.
2. **Off is off.** The order is unchanged and the verdict is a shadow row.
3. **On enforces** the posture, the sector's own trend and its relative
   strength — and an absent input is "gate not applied", never a block.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import cli, plays, regime as regimes
from src.dip_recovery import core as dip_core, evaluate
from tests.dip_recovery_factories import (
    dip_payload,
    dip_recovery_context,
    held_position,
    orders,
    skip_codes,
)
from tests.factories import ETFS, SECTOR_ETFS, config, regime_inputs

RECORDED = Path(__file__).parent / "fixtures" / "regime"

# DIP1 is a Health Care name here, so its ETF is XLV.
SECTOR, ETF = "Health Care", "XLV"
UNDER = {"last": 93.0, "sma200": 95.0}
_OTHERS = [s for s in SECTOR_ETFS if s != ETF]


def recorded(day: str) -> dict:
    return json.loads((RECORDED / f"edge_regime_{day}.json").read_text(encoding="utf-8"))


def below(count: int) -> dict:
    """``count`` sectors under their 200-day, never DIP1's own."""
    return {symbol: dict(UNDER) for symbol in _OTHERS[:count]}


def ranked(order: list[str], sectors: dict | None = None) -> dict:
    """Sector returns that rank exactly in ``order``, strongest first."""
    blocks = {}
    for index, symbol in enumerate(order):
        edge = 0.10 - 0.02 * index
        blocks[symbol] = {"ret_1m": edge, "ret_3m": edge, **((sectors or {}).get(symbol) or {})}
    return {"spy": {"ret_1m": 0.0, "ret_3m": 0.0}, "sectors": blocks}


def context(v2: bool = True, inputs: dict | None = None, **overrides) -> dict:
    """The sleeve's entering baseline, with a regime and the maps the cli adds."""
    ctx = dip_recovery_context(**overrides)
    ctx["config"] = {**ctx["config"], "regime_v2_enabled": v2}
    ctx["etfs"] = ETFS
    ctx["sectors"] = {"DIP1": SECTOR}
    if inputs is not None:
        ctx["regime"] = regimes.evaluate(regime_inputs(**inputs), ctx["config"]).as_dict()
    return ctx


def quantity(result) -> float:
    (order,) = [o for o in result.orders if o.side == "buy"]
    return order.quantity


FULL = quantity(evaluate(context(inputs={})))


def test_the_codes_are_core_dips_own():
    for name in (
        "SKIP_REGIME", "SKIP_SECTOR_BELOW_200D", "SKIP_SECTOR_RS_WEAK",
        "SKIP_RS_UNAVAILABLE",
    ):
        assert getattr(dip_core, name) == getattr(plays, name) == getattr(regimes, name)


# --- on: the posture ---------------------------------------------------------


@pytest.mark.parametrize(
    "inputs, posture",
    [
        ({"sectors": below(9)}, regimes.NO_NEW_POSITIONS),
        ({"spy": {"last": 580.0, "rv20": 0.42}}, regimes.HOLD_CASH),
    ],
)
def test_a_no_entry_posture_skips_the_name_without_evaluating_it(inputs, posture):
    ctx = context(inputs=inputs)
    assert ctx["regime"]["posture"] == posture

    result = evaluate(ctx)

    assert orders(result, "buy") == []
    assert skip_codes(result, "DIP1") == [dip_core.SKIP_REGIME]
    assert result.evaluations == []
    assert posture in result.skipped[0].message


def test_trade_normally_keeps_the_sleeves_own_filters_and_full_size():
    result = evaluate(context(inputs={}))

    assert orders(result, "buy") == ["DIP1"]
    assert result.evaluations[0]["passed"] is True
    assert result.shadow_would_skip == [] and result.shadow_would_resize == []


# --- on: the sector's own trend, in every posture ----------------------------


@pytest.mark.parametrize("others_under", [0, 4, 6])
def test_a_sector_below_its_two_hundred_day_is_refused(others_under):
    """TRADE_NORMALLY, REDUCED_SIZE and HIGH_CONVICTION_ONLY alike: "recovery"
    does not justify buying a sector that is itself under trend."""
    ctx = context(inputs={"sectors": {**below(others_under), ETF: dict(UNDER)}})

    result = evaluate(ctx)

    assert orders(result, "buy") == []
    assert skip_codes(result, "DIP1") == [dip_core.SKIP_SECTOR_BELOW_200D]
    assert result.evaluations == []


# --- on: HIGH_CONVICTION_ONLY ------------------------------------------------


def test_high_conviction_only_takes_a_strong_sector_at_half_size():
    ctx = context(inputs=ranked([ETF, *_OTHERS], below(7)))
    assert ctx["regime"]["posture"] == regimes.HIGH_CONVICTION_ONLY

    result = evaluate(ctx)

    assert orders(result, "buy") == ["DIP1"]
    assert quantity(result) == FULL / 2


def test_high_conviction_only_refuses_a_sector_outside_the_upper_half():
    order = [*_OTHERS[:5], ETF, *_OTHERS[5:]]  # sixth of eleven
    ctx = context(inputs=ranked(order, below(7)))

    result = evaluate(ctx)

    assert skip_codes(result, "DIP1") == [dip_core.SKIP_SECTOR_RS_WEAK]
    assert "outside the upper half" in result.skipped[0].message


def test_high_conviction_only_without_a_ranking_has_its_own_code():
    ctx = context(inputs={"sectors": below(7)})
    assert ctx["regime"]["sector_rs"] == {}

    result = evaluate(ctx)

    assert skip_codes(result, "DIP1") == [dip_core.SKIP_RS_UNAVAILABLE]


# --- on: REDUCED_SIZE, the tighter bar ---------------------------------------


def test_reduced_size_refuses_the_weakest_third():
    order = [*_OTHERS[:8], ETF, *_OTHERS[8:]]  # ninth of eleven
    ctx = context(inputs=ranked(order, below(5)))
    assert ctx["regime"]["posture"] == regimes.REDUCED_SIZE

    result = evaluate(ctx)

    assert skip_codes(result, "DIP1") == [dip_core.SKIP_SECTOR_RS_WEAK]
    assert "weakest third" in result.skipped[0].message


def test_reduced_size_takes_a_middling_sector_at_half_size():
    order = [*_OTHERS[:6], ETF, *_OTHERS[6:]]  # seventh of eleven
    ctx = context(inputs=ranked(order, below(5)))

    result = evaluate(ctx)

    assert orders(result, "buy") == ["DIP1"]
    assert quantity(result) == FULL / 2


def test_reduced_size_without_a_ranking_still_enters_at_half_size():
    """The weakest-third rule needs a ranking to read. Without one it is not
    applied — only HIGH_CONVICTION_ONLY requires the ranking to exist."""
    ctx = context(inputs={"sectors": below(5)})
    assert ctx["regime"]["posture"] == regimes.REDUCED_SIZE

    result = evaluate(ctx)

    assert orders(result, "buy") == ["DIP1"]
    assert quantity(result) == FULL / 2


# --- absent input is not a block ---------------------------------------------


def test_no_regime_is_no_opinion():
    result = evaluate(context(inputs=None))

    assert orders(result, "buy") == ["DIP1"] and quantity(result) == FULL


def test_an_unknown_sector_is_not_gated_on_its_trend():
    ctx = context(inputs={"sectors": {ETF: dict(UNDER)}})
    ctx["sectors"] = {"DIP1": "Technology"}  # not a GICS name: no ETF speaks for it

    assert orders(evaluate(ctx), "buy") == ["DIP1"]


# --- off: the order is unchanged and the verdict is recorded -----------------


def test_with_the_flag_off_nothing_is_gated_and_the_skip_is_shadow():
    ctx = context(v2=False, inputs={"sectors": below(9)})

    result = evaluate(ctx)

    assert orders(result, "buy") == ["DIP1"] and quantity(result) == FULL
    (row,) = result.shadow_would_skip
    assert (row["symbol"], row["code"], row["action"]) == ("DIP1", "SKIP_REGIME", "entry")


def test_with_the_flag_off_a_resize_is_shadow():
    ctx = context(v2=False, inputs={"sectors": below(5)})

    result = evaluate(ctx)

    assert quantity(result) == FULL
    assert result.shadow_would_resize == [{"symbol": "DIP1", "size_multiplier": 0.5}]


# --- exits and the held book -------------------------------------------------

AT_TARGET = {"DIP1": {"bid": 48.50, "ask": 48.55, "last": 48.50}}
UNDERWATER = {"DIP1": {"bid": 44.50, "ask": 44.55, "last": 44.50}}

POSTURE_INPUTS = {
    regimes.TRADE_NORMALLY: {},
    regimes.REDUCED_SIZE: {"sectors": below(5)},
    regimes.HIGH_CONVICTION_ONLY: {"sectors": below(7)},
    regimes.NO_NEW_POSITIONS: {"sectors": {**below(9), ETF: dict(UNDER)}},
    regimes.HOLD_CASH: {"spy": {"last": 580.0, "rv20": 0.42}},
}


def held(inputs, quotes, v2=True):
    return context(
        v2=v2, inputs=inputs, quotes=quotes,
        dip_recovery_positions={"DIP1": held_position()}, dip_recovery_watchlist=[],
    )


@pytest.mark.parametrize("posture", regimes.POSTURES)
def test_the_exit_is_identical_in_every_posture(posture):
    baseline = evaluate(held(None, AT_TARGET)).as_dict()
    ctx = held(POSTURE_INPUTS[posture], AT_TARGET)
    assert ctx["regime"]["posture"] == posture

    result = evaluate(ctx)

    assert result.as_dict() == baseline
    (sell,) = result.orders
    assert sell.side == "sell" and sell.as_order("regular_hours")["type"] == "limit"
    assert sell.as_order("regular_hours")["stop_price"] is None


@pytest.mark.parametrize("posture", [regimes.NO_NEW_POSITIONS, regimes.HOLD_CASH])
def test_a_no_entry_posture_does_not_average_down_and_keeps_managing(posture):
    result = evaluate(held(POSTURE_INPUTS[posture], UNDERWATER))

    assert result.orders == []
    assert skip_codes(result, "DIP1") == [dip_core.SKIP_REGIME]
    assert result.position_updates["DIP1"]["state"] == "HOLDING"


@pytest.mark.parametrize(
    "others_under, posture",
    [
        (0, regimes.TRADE_NORMALLY),
        (4, regimes.REDUCED_SIZE),
        (6, regimes.HIGH_CONVICTION_ONLY),
    ],
)
def test_an_average_down_is_otherwise_untouched_and_unscaled(others_under, posture):
    """The sector and strength rules are entry rules; a held position is past
    them. Its own sector is under trend here and it is still averaged."""
    baseline = evaluate(held(None, UNDERWATER))
    ctx = held({"sectors": {**below(others_under), ETF: dict(UNDER)}}, UNDERWATER)
    assert ctx["regime"]["posture"] == posture

    result = evaluate(ctx)

    assert [o.as_dict() for o in result.orders] == [o.as_dict() for o in baseline.orders]
    assert orders(result, "buy") == ["DIP1"]


def test_the_flag_off_average_down_in_a_no_entry_posture_is_shadow():
    result = evaluate(held(POSTURE_INPUTS[regimes.HOLD_CASH], UNDERWATER, v2=False))

    assert orders(result, "buy") == ["DIP1"]
    (row,) = result.shadow_would_skip
    assert (row["code"], row["action"]) == ("SKIP_REGIME", "average_down")


# --- through the command, on recorded edge_regime rows -----------------------

LOADED = cli.load_config()
V2 = {**LOADED, "config": {**LOADED["config"], "regime_v2_enabled": True}}


def tick(loaded, **overrides) -> dict:
    payload = {
        **dip_payload(**overrides),
        "now": "2026-08-31T15:47:00Z",
        "cycle_id": "2026-08-31T15:47Z-dip",
    }
    return cli.dip_recovery_decide(payload, loaded)


def test_a_tick_that_posts_no_regime_inputs_behaves_exactly_as_before():
    for loaded in (LOADED, V2):
        result = tick(loaded)

        assert result["regime"] is None and result["shadow_posture"] is None
        assert [o["symbol"] for o in result["orders"]] == ["DIP1"]


def test_a_recorded_weak_breadth_row_is_shadow_with_the_flag_off():
    """The pre-market job's own row. 3 of 11 sectors above trend."""
    result = tick(LOADED, regime_inputs=recorded("2026-10-02")["regime_inputs"])

    assert result["regime"]["posture"] == regimes.NO_NEW_POSITIONS
    assert result["regime"]["breadth"]["breadth_200"] == pytest.approx(3 / 11)
    assert result["regime_v2_enabled"] is False
    assert result["shadow_posture"] == regimes.NO_NEW_POSITIONS
    assert [o["symbol"] for o in result["orders"]] == ["DIP1"]
    assert [r["code"] for r in result["shadow_would_skip"]] == ["SKIP_REGIME"]


def test_the_recorded_row_is_enforced_with_the_flag_on():
    result = tick(V2, regime_inputs=recorded("2026-10-02")["regime_inputs"])

    assert result["orders"] == []
    assert [s["code"] for s in result["skipped"]] == ["SKIP_REGIME"]
    assert result["shadow_posture"] is None and result["regime_v2_enabled"] is True


def test_the_whole_edge_regime_payload_pasted_unopened_still_reads():
    """`edge_regime_recent()` returns the stored payload, block one level
    down. A routine that posts it whole must not silently get no regime."""
    result = tick(V2, regime_inputs=recorded("2026-10-02"))

    assert result["regime"]["posture"] == regimes.NO_NEW_POSITIONS
    assert result["orders"] == []


def test_a_row_missing_sector_etfs_is_breadth_unavailable_and_not_gated():
    row = recorded("2026-10-02")["regime_inputs"]
    row["sectors"] = {etf: row["sectors"][etf] for etf in sorted(row["sectors"])[:7]}

    result = tick(V2, regime_inputs=row)

    assert result["regime"]["breadth"]["breadth_unavailable"] is True
    assert result["regime"]["posture"] == regimes.TRADE_NORMALLY
    assert [o["symbol"] for o in result["orders"]] == ["DIP1"]
