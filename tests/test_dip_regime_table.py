"""S3: `dip_regime_table`, the DIP decision table (off by default).

The table restates the P1 rule's answers for DIP as one
config row per posture. What is pinned here:

1. **The identity.** As shipped, every row is the P1 rule: with both switches
   on, the plan is the plan `regime_v2_enabled` alone produces, on every
   posture and on the two `edge_regime` rows recorded from production.
2. **Off is off.** With the table's switch off, or `regime_v2_enabled` off,
   no row is read, whatever it says.
3. **The limits no row can lift.** A multiplier never exceeds 1.0; HOLD_CASH
   never opens; a missing or unreadable field is the P1 value.
4. **New DIP entries only.** Exits, the resting take-profit and other plays
   are the same with any table.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import cli, plays, regime as regimes
from tests.factories import SECTOR_ETFS, proposal, proposals, regime_inputs, skips
from tests.test_plays_dip import F_SECTOR_ETF, below_two_hundred, strong_and_weak, v2_context
from tests.test_regime_v2_cycle import LOADED, THROUGH_THE_STOP, V2, decided, tape

RECORDED = Path(__file__).parent / "fixtures" / "regime"
SHIPPED_TABLE = LOADED["config"][regimes.DIP_REGIME_TABLE]


def with_table(loaded: dict, table: dict | None = None, enabled: bool = True) -> dict:
    config = {**loaded["config"], regimes.DIP_REGIME_TABLE_ENABLED: enabled}
    if table is not None:
        config[regimes.DIP_REGIME_TABLE] = table
    return {**loaded, "config": config}


def table_context(rows: dict, enabled: bool = True, **inputs) -> dict:
    """`test_plays_dip.v2_context` with the table switched on and ``rows``
    laid over the shipped table."""
    ctx = v2_context(**inputs)
    ctx["config"] = {
        **ctx["config"],
        regimes.DIP_REGIME_TABLE_ENABLED: enabled,
        regimes.DIP_REGIME_TABLE: {**SHIPPED_TABLE, **rows},
    }
    return ctx


def recorded_inputs(day: str) -> dict:
    row = json.loads((RECORDED / f"edge_regime_{day}.json").read_text(encoding="utf-8"))
    return row["regime_inputs"]


def comparable(result: dict) -> dict:
    """Everything `decide` returns except the switch's own echo."""
    return {k: v for k, v in result.items() if k != "shadow_dip_table"}


# --- the shipped file ---------------------------------------------------------


def test_the_switch_ships_off():
    assert LOADED["config"][regimes.DIP_REGIME_TABLE_ENABLED] is False
    assert regimes.dip_table_enabled(LOADED["config"]) is False
    # Both switches are needed.
    assert regimes.dip_table_enabled({regimes.DIP_REGIME_TABLE_ENABLED: True}) is False
    assert regimes.dip_table_enabled(
        {"regime_v2_enabled": True, regimes.DIP_REGIME_TABLE_ENABLED: True}
    ) is True


def test_the_shipped_table_names_every_posture_and_every_field():
    assert sorted(k for k in SHIPPED_TABLE if not k.startswith("_")) == sorted(regimes.POSTURES)
    for posture in regimes.POSTURES:
        assert set(SHIPPED_TABLE[posture]) == {
            regimes.DIP_ALLOWED, regimes.DIP_SIZE_MULTIPLIER,
            regimes.REQUIRE_SECTOR_ABOVE_200D, regimes.REQUIRE_SECTOR_RS_UPPER_HALF,
            regimes.MAX_RSI,
        }


@pytest.mark.parametrize("posture", regimes.POSTURES)
def test_each_shipped_row_is_the_p1_rule(posture):
    """Read with the shipped table and with no table at all: the same row."""
    block = tape(posture)
    regime = regimes.evaluate(block, LOADED["config"]).as_dict()
    assert regime["posture"] == posture

    shipped = regimes.dip_table_row(regime, LOADED["config"])
    p1 = regimes.dip_table_row(regime, {})

    assert shipped == p1
    assert shipped[regimes.DIP_SIZE_MULTIPLIER] == regime["size_multiplier"]
    assert shipped[regimes.DIP_ALLOWED] is (posture not in regimes.NO_ENTRY_POSTURES)
    assert shipped["notes"] == []


# --- 1. the identity -----------------------------------------------------------


@pytest.mark.parametrize("posture", regimes.POSTURES)
def test_with_the_shipped_table_on_the_decision_is_the_p1_decision(posture):
    p1 = decided(V2, regime_inputs=tape(posture))
    table = decided(with_table(V2), regime_inputs=tape(posture))

    assert comparable(table) == comparable(p1)
    assert table["shadow_dip_table"]["enabled"] is True
    assert table["shadow_dip_table"]["differs_from_p1"] is False


@pytest.mark.parametrize("day", ["2026-09-18", "2026-10-02"])
def test_the_identity_holds_on_the_recorded_regime_rows(day):
    """The `edge_regime` rows the pre-market job wrote on the two days the
    recorded cases were bought, read the way a cycle reads them."""
    inputs = recorded_inputs(day)

    p1 = decided(V2, regime_inputs=inputs)
    table = decided(with_table(V2), regime_inputs=inputs)

    assert p1["regime"]["posture"] in regimes.POSTURES
    assert comparable(table) == comparable(p1)


@pytest.mark.parametrize("build", [
    lambda: {"sectors": {F_SECTOR_ETF: {"last": 93.0, "sma200": 95.0}}},
    lambda: strong_and_weak((F_SECTOR_ETF, "XLK", "XLE"), below_two_hundred(7)),
    lambda: strong_and_weak(("XLK", "XLE", "XLV"), below_two_hundred(7)),
    lambda: {"sectors": below_two_hundred(7)},
    lambda: {"sectors": below_two_hundred(9)},
    lambda: {"sectors": below_two_hundred(5)},
], ids=["sector_below", "hco_strong", "hco_weak", "hco_no_rs", "no_new", "reduced"])
def test_the_identity_holds_skip_for_skip_and_word_for_word(build):
    p1 = plays.entries(v2_context(**build()))
    table = plays.entries(table_context({}, **build()))

    assert [c.as_dict() for c in table.candidates] == [c.as_dict() for c in p1.candidates]
    assert [s.as_dict() for s in table.skipped] == [s.as_dict() for s in p1.skipped]


# --- 2. off is off ------------------------------------------------------------

OPEN_EVERYTHING = {
    posture: {
        regimes.DIP_ALLOWED: True, regimes.DIP_SIZE_MULTIPLIER: 1.0,
        regimes.REQUIRE_SECTOR_ABOVE_200D: False,
        regimes.REQUIRE_SECTOR_RS_UPPER_HALF: False, regimes.MAX_RSI: None,
    }
    for posture in regimes.POSTURES
}


def test_a_table_is_not_read_while_its_switch_is_off():
    inputs = {"sectors": below_two_hundred(9)}

    result = plays.entries(table_context(OPEN_EVERYTHING, enabled=False, **inputs))

    assert proposals(result, plays.DIP) == []
    assert skips(result, "F") == [plays.SKIP_REGIME]


def test_a_table_is_not_read_while_regime_v2_is_off():
    """No posture is enforced on anything, so nothing is resized either."""
    ctx = table_context(
        {regimes.TRADE_NORMALLY: {**OPEN_EVERYTHING[regimes.TRADE_NORMALLY],
                                  regimes.DIP_SIZE_MULTIPLIER: 0.25}}
    )
    ctx["config"] = {**ctx["config"], "regime_v2_enabled": False}

    result = plays.entries(ctx)

    assert proposal(result, "F").notional_multiplier == 1.0


def test_with_both_switches_off_the_plan_is_untouched_and_the_row_is_recorded():
    quiet = decided(LOADED)
    result = decided(with_table(LOADED, OPEN_EVERYTHING, enabled=False),
                     regime_inputs=tape(regimes.NO_NEW_POSITIONS))

    assert result["plan"]["orders"] == quiet["plan"]["orders"]
    shadow = result["shadow_dip_table"]
    assert shadow["enabled"] is False and shadow["posture"] == regimes.NO_NEW_POSITIONS
    assert shadow["differs_from_p1"] is True
    # P1 would not enter PFE in this posture; this table would, at full size.
    assert shadow["would_change"] == [{"symbol": "PFE", "p1": None, "table": 1.0}]


def test_the_shipped_table_records_no_difference():
    result = decided(LOADED, regime_inputs=tape(regimes.NO_NEW_POSITIONS))

    shadow = result["shadow_dip_table"]
    assert shadow["differs_from_p1"] is False and shadow["would_change"] == []
    assert shadow["row"][regimes.DIP_ALLOWED] is False


def test_no_regime_and_a_noop_cycle_carry_the_key_as_null():
    assert decided(LOADED)["shadow_dip_table"] is None
    assert "shadow_dip_table" in decided(with_table(V2))


# --- a row that departs from P1 ------------------------------------------------


def test_a_row_can_open_dip_in_no_new_positions_at_a_smaller_size():
    row = {regimes.NO_NEW_POSITIONS: {
        regimes.DIP_ALLOWED: True, regimes.DIP_SIZE_MULTIPLIER: 0.5,
        regimes.REQUIRE_SECTOR_ABOVE_200D: False,
        regimes.REQUIRE_SECTOR_RS_UPPER_HALF: False, regimes.MAX_RSI: None,
    }}
    ctx = table_context(row, sectors=below_two_hundred(9))
    assert ctx["regime"]["posture"] == regimes.NO_NEW_POSITIONS

    result = plays.entries(ctx)

    assert proposals(result, plays.DIP) == ["F"]
    assert proposal(result, "F").notional_multiplier == pytest.approx(0.5)


def test_a_row_can_drop_the_sector_rule_for_its_posture_only():
    below = {"sectors": {F_SECTOR_ETF: {"last": 93.0, "sma200": 95.0}}}
    row = {regimes.TRADE_NORMALLY: {**SHIPPED_TABLE[regimes.TRADE_NORMALLY],
                                    regimes.REQUIRE_SECTOR_ABOVE_200D: False}}

    assert proposals(plays.entries(table_context(row, **below)), plays.DIP) == ["F"]
    # The same row does nothing for a day that reads to another posture.
    other = {"sectors": {**below_two_hundred(5), **below["sectors"]}}
    ctx = table_context(row, **other)
    assert ctx["regime"]["posture"] == regimes.REDUCED_SIZE
    assert skips(plays.entries(ctx), "F") == [plays.SKIP_SECTOR_BELOW_200D]


def test_an_rsi_ceiling_refuses_a_dip_over_it_with_its_own_code():
    def row(ceiling):
        return {regimes.TRADE_NORMALLY: {**SHIPPED_TABLE[regimes.TRADE_NORMALLY],
                                         regimes.MAX_RSI: ceiling}}

    rsi = v2_context()["signals"]["F"]["rsi"]

    kept = plays.entries(table_context(row(rsi)))
    refused = plays.entries(table_context(row(rsi - 1.0)))

    assert proposals(kept, plays.DIP) == ["F"]
    assert skips(refused, "F") == [plays.SKIP_DIP_TABLE_RSI]
    assert f"{rsi - 1.0:.0f}" in refused.skipped[0].message


def test_an_rsi_ceiling_with_no_rsi_to_read_refuses():
    regime = regimes.evaluate(regime_inputs(), LOADED["config"]).as_dict()
    config = {regimes.DIP_REGIME_TABLE: {
        regimes.TRADE_NORMALLY: {regimes.MAX_RSI: 25.0}}}

    scale, refusal = regimes.dip_table_gate(regime, None, config, rsi=None)

    assert scale == 0.0 and refusal[0] == regimes.SKIP_DIP_TABLE_RSI


def test_a_row_can_require_relative_strength_in_any_posture():
    inputs = strong_and_weak(("XLK", "XLE", "XLV"))
    row = {regimes.TRADE_NORMALLY: {**SHIPPED_TABLE[regimes.TRADE_NORMALLY],
                                    regimes.REQUIRE_SECTOR_RS_UPPER_HALF: True}}
    ctx = table_context(row, **inputs)
    assert ctx["regime"]["posture"] == regimes.TRADE_NORMALLY

    assert skips(plays.entries(ctx), "F") == [plays.SKIP_SECTOR_RS_WEAK]


# --- 3. the limits no row can lift ---------------------------------------------


@pytest.mark.parametrize("given, expected", [(3.0, 1.0), (1.5, 1.0), (-2.0, 0.0), (0.25, 0.25)])
def test_the_multiplier_is_clamped_to_zero_and_one(given, expected):
    regime = regimes.evaluate(regime_inputs(), LOADED["config"]).as_dict()
    config = {regimes.DIP_REGIME_TABLE: {
        regimes.TRADE_NORMALLY: {regimes.DIP_SIZE_MULTIPLIER: given}}}

    assert regimes.dip_table_row(regime, config)[regimes.DIP_SIZE_MULTIPLIER] == expected


def test_a_row_can_never_size_a_dip_above_the_ladder():
    row = {regimes.TRADE_NORMALLY: {**SHIPPED_TABLE[regimes.TRADE_NORMALLY],
                                    regimes.DIP_SIZE_MULTIPLIER: 5.0}}

    result = plays.entries(table_context(row))

    assert proposal(result, "F").notional_multiplier == 1.0


def test_hold_cash_cannot_be_opened():
    block = tape(regimes.HOLD_CASH)
    regime = regimes.evaluate(block, LOADED["config"]).as_dict()

    row = regimes.dip_table_row(regime, {regimes.DIP_REGIME_TABLE: OPEN_EVERYTHING})

    assert row[regimes.DIP_ALLOWED] is False
    assert row["notes"] == ["hold_cash_cannot_be_opened"]
    result = decided(with_table(V2, OPEN_EVERYTHING), regime_inputs=block)
    assert [o for o in result["plan"]["orders"] if o["side"] == "buy"] == []


@pytest.mark.parametrize("table", [
    None, "nonsense", [], {}, {regimes.NO_NEW_POSITIONS: None},
    {regimes.NO_NEW_POSITIONS: "open"}, {"SOME_OTHER_POSTURE": {regimes.DIP_ALLOWED: True}},
])
def test_a_missing_or_malformed_table_is_the_p1_rule(table):
    regime = regimes.evaluate(tape(regimes.NO_NEW_POSITIONS), LOADED["config"]).as_dict()

    row = regimes.dip_table_row(regime, {regimes.DIP_REGIME_TABLE: table})

    assert row == regimes.dip_table_row(regime, {})


def test_an_unreadable_field_is_the_p1_value_and_is_named():
    regime = regimes.evaluate(tape(regimes.NO_NEW_POSITIONS), LOADED["config"]).as_dict()
    config = {regimes.DIP_REGIME_TABLE: {regimes.NO_NEW_POSITIONS: {
        regimes.DIP_ALLOWED: "yes", regimes.DIP_SIZE_MULTIPLIER: "half",
        regimes.REQUIRE_SECTOR_ABOVE_200D: 0, regimes.MAX_RSI: "low",
    }}}

    row = regimes.dip_table_row(regime, config)

    assert row[regimes.DIP_ALLOWED] is False                    # "yes" is not a boolean
    assert row[regimes.DIP_SIZE_MULTIPLIER] == 0.0              # the posture's own
    assert row[regimes.REQUIRE_SECTOR_ABOVE_200D] is True
    assert row[regimes.MAX_RSI] is None
    assert sorted(row["notes"]) == [
        "dip_allowed_unreadable", "dip_size_multiplier_unreadable",
        "max_rsi_unreadable", "require_sector_above_200d_unreadable",
    ]


def test_no_posture_means_no_row_and_no_gate():
    assert regimes.dip_table_row(None, LOADED["config"]) is None
    assert regimes.dip_table_row({"posture": "SOMETHING_NEW"}, LOADED["config"]) is None
    assert regimes.dip_requires_sector_trend(None, with_table(V2)["config"]) is True
    assert regimes.dip_table_gate({}, None, {}) == (1.0, None)


# --- 4. new DIP entries only ---------------------------------------------------


@pytest.mark.parametrize("posture", regimes.POSTURES)
def test_exits_and_the_resting_take_profit_are_the_same_under_any_table(posture):
    loaded = with_table(V2, OPEN_EVERYTHING)
    baseline = decided(LOADED, **THROUGH_THE_STOP)
    result = decided(loaded, regime_inputs=tape(posture), **THROUGH_THE_STOP)

    assert result["exits"] == baseline["exits"]
    sells = [o for o in result["plan"]["orders"] if o["side"] == "sell"]
    assert sells == [o for o in baseline["plan"]["orders"] if o["side"] == "sell"]
    for order in result["plan"]["orders"] + result["plan"]["protection"]:
        assert order.get("stop_price") is None
        assert "stop" not in str(order.get("type"))

    quiet = decided(loaded, regime_inputs=tape(posture))
    assert quiet["plan"]["protection"] == decided(LOADED)["plan"]["protection"]
    (target,) = quiet["plan"]["protection"]
    assert target["side"] == "sell" and target["type"] == "limit"
    assert target["limit_price"] > 15.00 and target["stop_price"] is None


def test_the_table_never_admits_another_play():
    """A CATALYST candidate in NO_NEW_POSITIONS is refused whatever the DIP
    row says, and one in REDUCED_SIZE keeps the posture's own multiplier: the
    row is read for DIP candidates and nothing else."""
    import sys

    entries_module = sys.modules["src.plays.entries"]     # `plays.entries` is the function

    def catalyst():
        return plays.Candidate(
            symbol="XYZ", play_id=plays.CATALYST, conviction=0.8, conviction_raw=0.8,
            regime_fit=1.0, crowding_penalty=0.0, expected_holding_days=5,
            notional_multiplier=1.0, detail="an event",
        )

    closed = table_context(OPEN_EVERYTHING, sectors=below_two_hundred(9))
    kept, skipped = entries_module._posture_gate([catalyst()], closed)
    assert kept == [] and [s.code for s in skipped] == [plays.SKIP_REGIME]

    reduced = table_context(OPEN_EVERYTHING, sectors=below_two_hundred(5))
    assert reduced["regime"]["posture"] == regimes.REDUCED_SIZE
    kept, skipped = entries_module._posture_gate([catalyst()], reduced)
    assert skipped == [] and kept[0].notional_multiplier == pytest.approx(0.5)
