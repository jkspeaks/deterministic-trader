"""PEAD and SURGE retired, CATALYST and MACRO shelved.

Retired is off in code with no
switch. Shelved is off behind a config key that ships false. The contexts
below are the ones each play's own test file uses to make it fire, so "it
proposes nothing" is tested against a cycle on which it would have proposed.

What must not move: a position already held under one of these plays keeps
its exits and its one resting limit sell above its buy price, and DIP buys as
before.
"""

from __future__ import annotations

import pytest

from src import candidates as discovery
from src import cli, plays
from src.plays import protection
from tests.factories import cli_payload, config, core_off, plays_context, position, proposals
from tests.test_plays_catalyst import catalyst_ctx
from tests.test_plays_macro import macro_ctx
from tests.test_plays_pead import pead_ctx
from tests.test_plays_surge import surge_context

OFF = (plays.PEAD, plays.SURGE, plays.CATALYST, plays.MACRO)
FIRING = {
    plays.PEAD: pead_ctx,
    plays.SURGE: surge_context,
    plays.CATALYST: catalyst_ctx,
    plays.MACRO: macro_ctx,
}
KEYS = {plays.CATALYST: plays.PLAY_CATALYST_ENABLED, plays.MACRO: plays.PLAY_MACRO_ENABLED}

EVENTS = {
    "AAPL": [{"type": "8-K", "items": ["2.02"], "sessions_ago": 1}],
    "MSFT": [{"type": "8-K", "items": ["8.01"], "sessions_ago": 1}],
}
MACRO_TAPE = {"releases": [{"series": "CPI", "surprise_pct": 0.20, "sessions_ago": 0}]}


def with_keys(ctx: dict, **keys) -> dict:
    return {**ctx, "config": {**ctx["config"], **keys}}


# --- as shipped ----------------------------------------------------------------


def test_the_shipped_file_has_both_shelved_plays_off():
    shipped = cli.load_config()["config"]

    assert shipped[plays.PLAY_CATALYST_ENABLED] is False
    assert shipped[plays.PLAY_MACRO_ENABLED] is False


def test_which_plays_are_retired_and_which_are_shelved():
    assert set(plays.RETIRED) == {plays.PEAD, plays.SURGE}
    assert plays.SHELVED == KEYS


@pytest.mark.parametrize("play_id", OFF)
def test_each_play_is_off_on_the_shipped_config_and_says_why(play_id):
    reason = plays.play_off(plays_context(), play_id)

    assert reason and play_id in reason


def test_dip_and_the_inherited_terms_are_not_off():
    for play_id in (plays.DIP, plays.ADOPTED, plays.CORE, plays.SECTOR, None):
        assert plays.play_off(plays_context(), play_id) is None


# --- no new money --------------------------------------------------------------


@pytest.mark.parametrize("play_id", OFF)
def test_a_cycle_on_which_the_play_would_fire_proposes_nothing_for_it(play_id):
    result = plays.entries(FIRING[play_id]())

    assert proposals(result, play_id) == []
    assert [s for s in result.skipped if s.play_id == play_id] == []


@pytest.mark.parametrize("play_id", OFF)
def test_the_same_cycle_did_fire_before(play_id, all_plays_live):
    assert proposals(plays.entries(FIRING[play_id]()), play_id)


@pytest.mark.parametrize("play_id", OFF)
def test_a_held_position_under_the_play_is_not_topped_up(play_id):
    winner = position(symbol="F", play_id=play_id, entry_price=13.0, market_value=14.88,
                      quantity=1.0)
    signals = {"F": {"rsi": 60.0, "sma_20": 14.5, "sma_50": 14.0, "sma_200": 13.5, "atr": 0.45}}

    result = plays.entries(plays_context(positions=[winner], candidates=[], signals=signals))

    assert result.candidates == []
    assert [(s.symbol, s.play_id) for s in result.skipped] == [("F", play_id)]
    assert "retired" in result.skipped[0].message or "shelved" in result.skipped[0].message


def test_dip_still_buys_on_the_baseline_cycle():
    result = cli.decide(cli_payload(), core_off())

    buys = [(o["symbol"], o["play_id"]) for o in result["plan"]["orders"] if o["side"] == "buy"]
    assert buys == [("PFE", "DIP")]


@pytest.mark.parametrize("play_id", OFF)
def test_an_inherited_name_is_not_bound_to_the_play(play_id):
    ctx = FIRING[play_id]()
    symbol = "XLE" if play_id == plays.MACRO else ctx["candidates"][0]

    assert plays.would_open(symbol, ctx) != play_id


# --- retired has no switch -----------------------------------------------------


@pytest.mark.parametrize("play_id", (plays.PEAD, plays.SURGE))
def test_no_config_key_brings_a_retired_play_back(play_id):
    keys = {
        "play_pead_enabled": True, "play_surge_enabled": True,
        plays.PLAY_CATALYST_ENABLED: True, plays.PLAY_MACRO_ENABLED: True,
    }
    ctx = with_keys(FIRING[play_id](), **keys)

    assert plays.play_off(ctx, play_id) is not None
    assert proposals(plays.entries(ctx), play_id) == []


def test_the_surge_switch_no_longer_does_anything():
    ctx = surge_context()
    assert ctx["control"]["full_send_enabled"] is True

    assert plays.entries(ctx).candidates == []


def test_a_row_in_play_state_cannot_bring_any_of_them_back():
    for play_id in OFF:
        ctx = FIRING[play_id]()
        ctx["play_state"] = {**(ctx.get("play_state") or {}), play_id: {"regime_fit": 1.0}}

        assert proposals(plays.entries(ctx), play_id) == []


# --- shelved comes back with its key, and only with an explicit true ----------


@pytest.mark.parametrize("play_id", (plays.CATALYST, plays.MACRO))
def test_a_shelved_play_fires_again_with_its_own_key_true(play_id):
    ctx = with_keys(FIRING[play_id](), **{KEYS[play_id]: True})

    assert plays.play_off(ctx, play_id) is None
    assert proposals(plays.entries(ctx), play_id)


def test_one_shelved_plays_key_does_not_bring_back_the_other():
    assert plays.play_off(config(**{plays.PLAY_CATALYST_ENABLED: True}), plays.MACRO)
    assert plays.play_off(config(**{plays.PLAY_MACRO_ENABLED: True}), plays.CATALYST)


@pytest.mark.parametrize("play_id", (plays.CATALYST, plays.MACRO))
def test_an_absent_key_reads_as_off(play_id):
    assert plays.play_off({}, play_id)
    assert plays.play_off(None, play_id)
    assert plays.play_off({"config": {}}, play_id)


@pytest.mark.parametrize("key", sorted(KEYS.values()))
@pytest.mark.parametrize("bad", ["true", 1, 0, "on"])
def test_a_mistyped_key_refuses_to_load(key, bad):
    with pytest.raises(ValueError, match=key):
        cli._validate_config({**core_off()["config"], key: bad})


@pytest.mark.parametrize("key", sorted(KEYS.values()))
def test_a_truthy_value_that_is_not_true_leaves_the_play_off(key):
    play_id = next(p for p, k in KEYS.items() if k == key)

    assert plays.play_off({key: 1}, play_id)
    assert plays.play_off({key: "true"}, play_id)


# --- a position already held is managed exactly as before ----------------------


@pytest.mark.parametrize("play_id", OFF)
def test_a_held_position_keeps_its_exits(play_id, monkeypatch):
    cases = (
        ({"entry_price": 15.0}, {"F": {"rsi": 45.0, "sma_200": 13.5, "atr": 0.02}}),
        ({"entry_price": 15.0, "sessions_held": 99}, {"F": {"rsi": 45.0, "sma_200": 13.5, "atr": 5.0}}),
    )
    for fields, signals in cases:
        ctx = plays_context(positions=[position(symbol="F", play_id=play_id, **fields)],
                            candidates=[], signals=signals)
        now = plays.exits(ctx).as_dict()
        assert now["exits"]

        with monkeypatch.context() as before:
            before.setattr(plays.core, "RETIRED", {})
            before.setattr(plays.core, "SHELVED", {})
            assert plays.exits(ctx).as_dict() == now


@pytest.mark.parametrize("play_id", OFF)
def test_a_held_position_still_rests_one_limit_sell_above_its_buy(play_id):
    ctx = plays_context(positions=[position(symbol="F", play_id=play_id, entry_price=15.0)],
                        candidates=[])

    [order] = protection.desired(ctx).orders
    assert order.kind == protection.TARGET and order.order_type == protection.LIMIT
    assert order.limit_price > 15.0 and order.stop_price is None


def test_the_exit_terms_of_all_four_plays_are_still_declared():
    for play_id in OFF:
        assert plays.PLAYS[play_id].holding_days > 0
        assert plays.PLAYS[play_id].target_pct > 0


# --- the work that only feeds these plays --------------------------------------


def test_discovery_finds_no_event_triggers_and_no_macro_etfs():
    found = discovery.assemble({"events": EVENTS, "macro": MACRO_TAPE}, core_off())

    assert found["triggers"] == []
    assert found["macro_targets"] == []
    assert found["candidates_dropped"] == []


def test_the_same_events_were_triggers_before(all_plays_live):
    found = discovery.assemble({"events": EVENTS, "macro": MACRO_TAPE}, core_off())

    assert {t["play_id"] for t in found["triggers"]} == {plays.PEAD, plays.CATALYST}
    assert found["macro_targets"] == ["XLE", "XLF"]


def test_a_shelved_plays_discovery_returns_with_its_key():
    catalyst = discovery.assemble({"events": EVENTS}, core_off(**{plays.PLAY_CATALYST_ENABLED: True}))
    macro = discovery.assemble({"macro": MACRO_TAPE}, core_off(**{plays.PLAY_MACRO_ENABLED: True}))

    assert {t["play_id"] for t in catalyst["triggers"]} == {plays.CATALYST}
    assert macro["macro_targets"] == ["XLE", "XLF"]


def test_a_dip_survivor_is_still_fetched():
    found = cli.candidates(cli_payload(dip_survivors=["PFE"], events=EVENTS), core_off())

    assert found["triggers"] == [{"symbol": "PFE", "play_id": "DIP"}]
    assert "plays_off_dropped" not in found


def test_a_watchlist_or_resubmit_row_written_before_the_change_is_dropped():
    day = "2026-08-03"
    payload = cli_payload(
        now="2026-08-03T15:40:00Z",
        control={
            "last_full_cycle": day,
            "today_watchlist": {"trading_day": day, "macro_targets": [],
                                "triggers": [{"symbol": "AAPL", "play_id": "PEAD"},
                                             {"symbol": "MSFT", "play_id": "CATALYST"}]},
            "resubmit_candidates": {"trading_day": day,
                                    "triggers": [{"symbol": "PFE", "play_id": "DIP"}]},
        },
    )

    found = cli.candidates(payload, core_off())

    assert found["triggers"] == [{"symbol": "PFE", "play_id": "DIP"}]
    assert found["plays_off_dropped"] == 2


def test_none_of_the_four_is_counted_as_a_dead_play():
    payload = cli_payload(control={"full_send_enabled": True})

    activity = cli.decide(payload, core_off())["control_updates"]["play_activity"]

    assert set(activity) == {plays.DIP}
