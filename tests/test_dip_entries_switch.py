"""`dip_entries_enabled`: stop DIP's new money with a switch, not a deletion.

The key ships true, which is today's
behaviour, so the first tests pin that nothing moves until it is set to
false. The rest pin what false stops (new DIP positions, DIP top-ups, the
discovery and fetch work that only feeds DIP) and what it must leave alone
(a held DIP position's exits and resting target, the play a position is
bound to, every other play).
"""

from __future__ import annotations

import pytest

from src import cli, plays
from src.plays import protection
from tests.factories import cli_payload, config, core_off, plays_context, position, proposals

KEY = plays.DIP_ENTRIES_ENABLED


def loaded(**overrides):
    base = core_off()
    return {**base, "config": {**base["config"], KEY: False, **overrides}}


def buys(result):
    return [o for o in result["plan"]["orders"] if o["side"] == "buy"]


# --- as shipped ----------------------------------------------------------------


def test_the_shipped_switch_leaves_dip_buying():
    assert cli.load_config()["config"][KEY] is True


def test_an_absent_key_reads_as_on():
    assert plays.dip_entries_enabled({}) is True
    assert plays.dip_entries_enabled(None) is True
    assert plays.dip_entries_enabled({"config": {}}) is True
    assert plays.dip_entries_enabled({KEY: False}) is False
    assert plays.dip_entries_enabled({"config": {KEY: False}}) is False


def test_on_the_baseline_cycle_buys_its_dip_and_carries_no_new_field():
    base = core_off()
    on = {**base, "config": {**base["config"], KEY: True}}

    result = cli.decide(cli_payload(), on)
    assert [(o["symbol"], o["play_id"]) for o in buys(result)] == [("PFE", "DIP")]

    found = cli.candidates(cli_payload(dip_survivors=["PFE"]), on)
    assert {"symbol": "PFE", "play_id": "DIP"} in found["triggers"]
    assert "dip_entries_off" not in found


@pytest.mark.parametrize("bad", ["false", 0, 1, "off"])
def test_a_mistyped_switch_refuses_to_load(bad):
    with pytest.raises(ValueError, match=KEY):
        cli._validate_config({**core_off()["config"], KEY: bad})


# --- off: no new money ---------------------------------------------------------


def test_off_the_same_cycle_buys_nothing_and_says_why():
    result = cli.decide(cli_payload(), loaded())

    assert buys(result) == []
    assert result["entries"]["candidates"] == []
    skipped = [s for s in result["entries"]["skipped"] if s["symbol"] == "PFE"]
    assert [s["play_id"] for s in skipped] == ["DIP"]
    assert KEY in skipped[0]["message"]


def test_off_a_row_in_play_state_cannot_turn_dip_back_on():
    ctx = plays_context(config=config(**{KEY: False}))
    assert ctx["play_state"]["DIP"]["regime_fit"] == 1.0

    assert proposals(plays.entries(ctx)) == []
    assert plays._operator_dark(ctx, plays.DIP) == plays.DIP_ENTRIES_OFF_REASON


def test_off_a_held_dip_position_is_not_topped_up():
    winner = position(symbol="F", play_id="DIP", entry_price=13.0, market_value=14.88, quantity=1.0)
    signals = {"F": {"rsi": 60.0, "sma_20": 14.5, "sma_50": 14.0, "sma_200": 13.5, "atr": 0.45}}

    on = plays_context(positions=[winner], candidates=[], signals=signals)
    off = plays_context(positions=[winner], candidates=[], signals=signals,
                        config=config(**{KEY: False}))

    assert [c.symbol for c in plays.entries(on).candidates] == ["F"]
    result = plays.entries(off)
    assert result.candidates == []
    assert [(s.symbol, s.play_id) for s in result.skipped] == [("F", "DIP")]
    assert KEY in result.skipped[0].message


def test_off_every_other_play_is_untouched():
    for play_id in (plays.PEAD, plays.CATALYST, plays.MACRO, plays.SURGE):
        assert plays._operator_dark(plays_context(config=config(**{KEY: False})), play_id) is None


# --- off: a held DIP position is managed exactly as before ---------------------


def held(**fields):
    return position(symbol="F", play_id="DIP", **fields)


def test_off_a_held_dip_position_keeps_its_exits():
    for fields, signals in (
        # through the per-cycle check
        ({"entry_price": 15.0}, {"F": {"rsi": 45.0, "sma_200": 13.5, "atr": 0.02}}),
        # past its holding period
        ({"entry_price": 15.0, "sessions_held": 99}, {"F": {"rsi": 45.0, "sma_200": 13.5, "atr": 5.0}}),
    ):
        on = plays_context(positions=[held(**fields)], candidates=[], signals=signals)
        off = plays_context(positions=[held(**fields)], candidates=[], signals=signals,
                            config=config(**{KEY: False}))

        assert plays.exits(on).as_dict()["exits"]
        assert plays.exits(off).as_dict() == plays.exits(on).as_dict()


def test_off_a_held_dip_position_still_rests_one_limit_sell_above_its_buy():
    book = [held(entry_price=15.0)]
    on = plays_context(positions=book, candidates=[])
    off = plays_context(positions=book, candidates=[], config=config(**{KEY: False}))

    assert protection.desired(off).as_dict() == protection.desired(on).as_dict()
    [order] = protection.desired(off).orders
    assert order.kind == protection.TARGET and order.order_type == protection.LIMIT
    assert order.limit_price > 15.0 and order.stop_price is None


def test_off_an_inherited_name_is_still_recognised_as_a_dip():
    ctx = plays_context(config=config(**{KEY: False}))

    assert plays.would_open("F", ctx) == plays.DIP


# --- off: the work that only feeds DIP -----------------------------------------


def test_off_the_scanners_survivors_are_not_fetched_and_the_count_is_recorded():
    found = cli.candidates(cli_payload(dip_survivors=["PFE", {"symbol": "XOM", "play_id": "DIP"}]),
                           loaded())

    assert [t for t in found["triggers"] if t["play_id"] == "DIP"] == []
    assert found["dip_entries_off"] == {
        "reason": plays.DIP_ENTRIES_OFF_REASON,
        "survivors_ignored": 2,
        "triggers_dropped": 0,
    }


def test_off_a_dip_row_from_the_watchlist_or_a_resubmit_is_dropped_and_others_kept(
    all_plays_live,
):
    day = "2026-08-03"
    payload = cli_payload(
        now="2026-08-03T15:40:00Z",
        control={
            "last_full_cycle": day,
            "today_watchlist": {"trading_day": day, "macro_targets": [],
                                "triggers": [{"symbol": "AAPL", "play_id": "PEAD"}]},
            "resubmit_candidates": {"trading_day": day,
                                    "triggers": [{"symbol": "PFE", "play_id": "DIP"}]},
        },
    )

    on = cli.candidates(payload, core_off())
    off = cli.candidates(payload, loaded())

    assert {"symbol": "PFE", "play_id": "DIP"} in on["triggers"]
    assert off["triggers"] == [{"symbol": "AAPL", "play_id": "PEAD"}]
    assert off["dip_entries_off"]["triggers_dropped"] == 1


def test_off_the_silent_scan_count_neither_advances_nor_resets():
    quiet = cli_payload(triggers=[], play_state={plays.DIP: {"silent_entry_cycles": 7}})

    assert cli.decide(quiet, core_off())["play_state_updates"] == {
        plays.DIP: {"silent_entry_cycles": 8}}
    off = cli.decide(quiet, loaded())
    assert off["play_state_updates"] == {plays.DIP: {"silent_entry_cycles": 7}}
    assert "DIP_SILENT_STREAK" not in str(off["universe"])


def test_off_dip_is_not_counted_as_a_dead_play():
    ctx = plays_context(config=config(**{KEY: False}))

    assert plays._operator_dark(ctx, plays.DIP) is not None
