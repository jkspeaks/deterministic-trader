"""P1 of an earlier audit fixes, at the seam `cli.decide` owns.

Three promises, in the order they matter:

1. **Exits are untouched.** The posture gates new entries and nothing else. In
   every posture a held position still rests its take-profit limit sell and a
   position through its ATR stop still gets an ordinary limit sell
   (profit first: README.md).
2. **Off is off.** With `regime_v2_enabled` false the plan is the plan the old
   rule produced, and the new rule's verdict rides beside it as shadow fields.
3. **On enforces**, and records what the old rule would have entered.
"""

import pytest

from src import cli, regime as regimes
from tests.factories import SECTOR_ETFS, cli_payload, regime_inputs

LOADED = cli.load_config()
V2 = {**LOADED, "config": {**LOADED["config"], "regime_v2_enabled": True}}

UNDER = {"last": 93.0, "sma200": 95.0}

# PFE, the baseline's one entry, is Health Care (XLV); it is kept above trend
# so the only thing that can stop the entry is the posture itself.
_OTHERS = [s for s in SECTOR_ETFS if s != "XLV"]


def tape(posture: str) -> dict:
    """A `regime_inputs` block that reads to exactly ``posture``."""
    under = {
        regimes.TRADE_NORMALLY: 0,
        regimes.REDUCED_SIZE: 5,
        regimes.HIGH_CONVICTION_ONLY: 7,
        regimes.NO_NEW_POSITIONS: 9,
        regimes.HOLD_CASH: 0,
    }[posture]
    overrides = {"sectors": {symbol: dict(UNDER) for symbol in _OTHERS[:under]}}
    if posture == regimes.HOLD_CASH:
        overrides["spy"] = {"last": 580.0, "rv20": 0.42}
    return regime_inputs(**overrides)


def decided(loaded, **overrides) -> dict:
    """`reconcile` then `decide`, the way a cycle runs them — so the resting
    target in the plan is the one `plays.protection` computes today."""
    payload = cli_payload(**overrides)
    payload = {**payload, "reconcile": cli.reconcile(payload, loaded)}
    return cli.decide(payload, loaded)


THROUGH_THE_STOP = {"quotes": {"F": {"bid": 13.38, "ask": 13.40, "last": 13.39}}}


@pytest.mark.parametrize("posture", regimes.POSTURES)
def test_every_posture_reads_as_itself(posture):
    result = decided(V2, regime_inputs=tape(posture))

    assert result["regime"]["posture"] == posture


# --- 1. exits are unaffected ----------------------------------------------


@pytest.mark.parametrize("posture", regimes.POSTURES)
def test_the_take_profit_sell_still_rests_in_every_posture(posture):
    baseline = decided(LOADED)
    result = decided(V2, regime_inputs=tape(posture))

    assert result["plan"]["protection"] == baseline["plan"]["protection"]
    (target,) = result["plan"]["protection"]
    assert target["symbol"] == "F" and target["side"] == "sell"
    assert target["type"] == "limit" and target["limit_price"] > 15.00
    assert target["stop_price"] is None


@pytest.mark.parametrize("posture", regimes.POSTURES)
def test_a_position_through_its_stop_is_still_sold_with_a_limit_in_every_posture(posture):
    baseline = decided(LOADED, **THROUGH_THE_STOP)
    result = decided(V2, regime_inputs=tape(posture), **THROUGH_THE_STOP)

    assert result["exits"] == baseline["exits"]
    sells = [o for o in result["plan"]["orders"] if o["side"] == "sell"]
    assert sells == [o for o in baseline["plan"]["orders"] if o["side"] == "sell"]
    (sell,) = sells
    assert sell["symbol"] == "F" and sell["type"] == "limit"
    assert sell["stop_price"] is None
    assert result["exits"]["exits"][0]["code"] == "ATR_STOP"
    # Nothing in the plan is a resting stop of any kind.
    for order in result["plan"]["orders"] + result["plan"]["protection"]:
        assert order.get("stop_price") is None
        assert "stop" not in str(order.get("type"))


SIZING_V2 = {**LOADED, "config": {**LOADED["config"], "sizing_v2_enabled": True}}
BOTH = {**V2, "config": {**V2["config"], "sizing_v2_enabled": True}}


@pytest.mark.parametrize("loaded", [SIZING_V2, BOTH], ids=["sizing_v2", "both"])
@pytest.mark.parametrize("posture", regimes.POSTURES)
def test_exits_and_protection_are_untouched_by_sizing_v2_in_every_posture(posture, loaded):
    """P3: portfolio construction and posture sizing are about new money. The
    resting take-profit and a through-the-stop limit sell are identical to the
    no-regime baseline, and nothing in the plan is a stop of any kind."""
    baseline = decided(LOADED, **THROUGH_THE_STOP)
    result = decided(loaded, regime_inputs=tape(posture), **THROUGH_THE_STOP)

    assert result["exits"] == baseline["exits"]
    assert [o for o in result["plan"]["orders"] if o["side"] == "sell"] == [
        o for o in baseline["plan"]["orders"] if o["side"] == "sell"
    ]
    assert result["plan"]["cancels"] == baseline["plan"]["cancels"]

    resting = decided(loaded, regime_inputs=tape(posture))
    assert resting["plan"]["protection"] == decided(LOADED)["plan"]["protection"]
    for order in (
        result["plan"]["orders"] + result["plan"]["protection"] + resting["plan"]["protection"]
    ):
        assert order.get("stop_price") is None
        assert "stop" not in str(order.get("type"))


# --- 2. off is off, and the new rule is logged beside it -------------------


def test_with_the_flag_off_the_plan_is_the_old_plan_and_the_posture_is_shadow():
    quiet = decided(LOADED)
    result = decided(LOADED, regime_inputs=tape(regimes.NO_NEW_POSITIONS))

    assert result["plan"]["orders"] == quiet["plan"]["orders"]
    assert [o["symbol"] for o in result["plan"]["orders"]] == ["PFE"]
    assert result["regime_v2_enabled"] is False
    assert result["shadow_posture"] == regimes.NO_NEW_POSITIONS
    assert [row["symbol"] for row in result["shadow_dip_would_block"]] == ["PFE"]
    assert result["shadow_dip_would_block"][0]["code"] == "SKIP_REGIME"


def test_the_shadow_names_a_dip_the_sector_rule_would_block():
    """PFE's own sector under its 200-day on an otherwise normal tape."""
    result = decided(LOADED, regime_inputs=regime_inputs(sectors={"XLV": dict(UNDER)}))

    assert [o["symbol"] for o in result["plan"]["orders"]] == ["PFE"]
    assert result["shadow_posture"] == regimes.TRADE_NORMALLY
    (row,) = result["shadow_dip_would_block"]
    assert (row["symbol"], row["code"]) == ("PFE", "SKIP_SECTOR_BELOW_200D")


def test_a_normal_tape_shadows_nothing():
    result = decided(LOADED, regime_inputs=tape(regimes.TRADE_NORMALLY))

    assert result["shadow_posture"] == regimes.TRADE_NORMALLY
    assert result["shadow_dip_would_block"] == []
    assert result["shadow_entries"] == {
        "rule": "regime_v2", "would_skip": [], "would_resize": [],
    }


def test_no_regime_inputs_means_no_shadow_and_no_gate():
    for loaded in (LOADED, V2):
        result = decided(loaded)

        assert result["regime"] is None
        assert result["shadow_posture"] is None
        assert result["shadow_dip_would_block"] == []
        assert [o["symbol"] for o in result["plan"]["orders"]] == ["PFE"]


# --- 3. on enforces, and logs the old rule ---------------------------------


def test_with_the_flag_on_no_new_positions_places_no_entry():
    result = decided(V2, regime_inputs=tape(regimes.NO_NEW_POSITIONS))

    assert [o for o in result["plan"]["orders"] if o["side"] == "buy"] == []
    assert {s["code"] for s in result["entries"]["skipped"]} == {"SKIP_REGIME"}
    assert result["regime_v2_enabled"] is True
    assert result["shadow_posture"] is None
    assert result["shadow_entries"]["rule"] == "legacy"
    assert result["shadow_entries"]["would_enter"] == ["PFE"]


def test_a_macro_event_day_reads_reduced_size_through_decide():
    """`data/macro.json`'s own rows: a release first seen today."""
    result = decided(
        LOADED,
        regime_inputs=tape(regimes.TRADE_NORMALLY),
        macro={"releases": [
            {"series": "FOMC", "first_seen": "2026-08-03", "surprise_pct": None},
        ]},
    )

    assert result["shadow_posture"] == regimes.REDUCED_SIZE
    assert "macro" in result["regime"]["posture_reason"]


def test_a_noop_cycle_carries_the_keys_and_no_verdict():
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

    assert result["shadow_posture"] is None
    assert result["shadow_dip_would_block"] == []
