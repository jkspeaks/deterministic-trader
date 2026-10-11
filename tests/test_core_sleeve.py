"""The core sleeve (`src/core_sleeve.py`): idle cash in one broad ETF.

These tests run the sleeve the way production does, through `cli.decide` and `guard.evaluate`,
with the switch turned on in the test. The shipped switch has been on; the first tests pin what
"off" means: nothing ordered, nothing listed, nothing moved.

The rule the owner set for every position applies to this one and is pinned
here as well as in `tests/test_profit_first_policy.py`: its only resting order
is a limit sell above its buy price, and it has no stop of any kind.
"""

from __future__ import annotations

import pytest

from src import cli, core_sleeve, guard, plays, reconcile as reconciler, sizing
from src.plays import protection
from tests.factories import cli_payload, config, core_off, plays_context, position

ETF = "SPYM"
OFF = {core_sleeve.ENABLED: False}
TRADABLE = {"tradeable": True, "fractional_tradability": "tradable", "all_day_tradability": "tradable"}


def loaded(**overrides):
    base = cli.load_config()
    return {**base, "config": {**base["config"], core_sleeve.ENABLED: True, **overrides}}


def payload(**overrides):
    """A $1,400 account with $1,000 settled, F held, and the ETF quoted at
    90.00 with a 1.00 ATR. No play has anything to buy.

    Seven slots of $200; a $28 reserve; so $228 is kept back and $772 is idle.
    The cap is $700, which buys seven shares at the 90.10 marketable limit.
    """
    base = dict(
        account={"equity_total": 1400.0, "cash": 1000.0, "buying_power": 1000.0, "settled_cash": 1000.0},
        control={"high_water_mark": 1400.0},
        triggers=[{"symbol": ETF, "play_id": "CORE"}],
        quotes={ETF: {"bid": 89.98, "ask": 90.00, "last": 89.99}},
        signals={ETF: {"rsi": 50.0, "sma_20": 89.0, "sma_50": 88.0, "sma_200": 80.0, "atr": 1.0}},
        tradability={ETF: TRADABLE},
    )
    base.update(overrides)
    return cli_payload(**base)


def sleeve_buys(result):
    return [o for o in result["plan"]["orders"] if o.get("symbol") == ETF and o["side"] == "buy"]


def denials(result, symbol=ETF):
    return [d["code"] for row in result["guard"]["denied"] if row["order"]["symbol"] == symbol
            for d in row["denials"]]


# --- off ---------------------------------------------------------------------


def test_the_shipped_switch_is_on():
    """The tests in this group pin what setting it back to false does."""
    assert cli.load_config()["config"][core_sleeve.ENABLED] is True


def test_off_it_orders_nothing_and_still_records_what_it_would_deploy():
    result = cli.decide(payload(), core_off())

    assert sleeve_buys(result) == []
    record = result["core_sleeve"]
    assert record["enabled"] is False and record["ordered"] is False and record["placed"] is False
    assert record["would_deploy"] == 700.0
    assert record["kept_back"] == 228.0 and record["cap_value"] == 700.0


def test_off_the_etf_is_not_listed_and_the_config_object_is_untouched():
    base = core_off()
    assert ETF not in base["etfs"]["broad"]
    assert core_sleeve.loaded_with_sleeve(base) is base


def test_off_no_trigger_is_added_and_on_one_is():
    off = cli.candidates(cli_payload(), core_off())
    on = cli.candidates(cli_payload(), loaded())

    assert {"symbol": ETF, "play_id": "CORE"} not in off["triggers"]
    assert on["triggers"].count({"symbol": ETF, "play_id": "CORE"}) == 1
    assert [t for t in on["triggers"] if t["symbol"] != ETF] == off["triggers"]


# --- on: the buy ---------------------------------------------------------------


def test_on_it_buys_whole_shares_with_idle_cash_up_to_its_cap():
    result = cli.decide(payload(), loaded())

    [order] = sleeve_buys(result)
    assert order["quantity"] == 7 and order["limit_price"] == 90.10
    assert order["type"] == "limit" and order["stop_price"] is None
    assert order["play_id"] == "CORE" and order[core_sleeve.ORDER_FLAG] is True
    assert result["core_sleeve"]["placed"] is True
    assert result["core_sleeve"]["notional"] == pytest.approx(630.70)


def test_the_plays_come_first_and_the_sleeve_takes_what_is_left():
    with_dip = payload(
        triggers=[{"symbol": "PFE", "play_id": "DIP"}, {"symbol": ETF, "play_id": "CORE"}]
    )
    result = cli.decide(with_dip, loaded())

    spent = sum(e["notional"] for e in result["sizing"]["entries"])
    assert spent > 0.0 and "PFE" in [o["symbol"] for o in result["plan"]["orders"]]
    assert result["core_sleeve"]["cash_after_plays"] == pytest.approx(1000.0 - spent, abs=0.01)
    [order] = sleeve_buys(result)
    assert order["quantity"] * order["limit_price"] <= 1000.0 - spent - 228.0 + 1e-6


def test_the_buffer_and_the_reserve_are_never_spent():
    thin = payload(account={"equity_total": 1400.0, "cash": 300.0, "buying_power": 300.0,
                            "settled_cash": 300.0})
    result = cli.decide(thin, loaded())

    assert sleeve_buys(result) == []                      # 300 - 228 = 72, under one share
    assert "does not buy one share" in result["core_sleeve"]["reason"]

    bare = payload(account={"equity_total": 1400.0, "cash": 200.0, "buying_power": 200.0,
                            "settled_cash": 200.0})
    assert "kept back" in cli.decide(bare, loaded())["core_sleeve"]["reason"]


def held_sleeve(quantity=7.0, entry=90.0, **extra):
    """The sleeve already holding ``quantity`` shares."""
    return dict(
        positions=[{"symbol": "F", "quantity": 1.0, "market_value": 14.88},
                   {"symbol": ETF, "quantity": quantity, "market_value": quantity * 90.0}],
        prior_positions=[{"symbol": "F", "play_id": "DIP", "quantity": 1.0},
                         {"symbol": ETF, "play_id": "CORE", "quantity": quantity}],
        reconcile={
            "findings": [], "cancels": [],
            "positions": [
                {"symbol": "F", "play_id": "DIP", "quantity": 1.0, "entry_price": 15.00,
                 "sessions_held": 2, "adopted": False, "adopted_on": None, "high_since_entry": 15.00},
                {"symbol": ETF, "play_id": "CORE", "quantity": quantity, "entry_price": entry,
                 "sessions_held": 400, "adopted": False, "adopted_on": None,
                 "high_since_entry": entry, **extra},
            ],
            "protection_plan": {"place": [], "replace": [], "keep": [], "notes": []},
        },
        tax_lots={"F": [{"quantity": 1.0, "tax_cost_basis": 15.00, "open_date": "2026-07-30"}],
                  ETF: [{"quantity": quantity, "tax_cost_basis": entry * quantity,
                         "open_date": "2025-01-02"}]},
    )


def test_at_its_cap_it_buys_nothing():
    result = cli.decide(payload(**held_sleeve(quantity=8.0)), loaded())      # 720 held, 700 cap

    assert sleeve_buys(result) == []
    assert "cap" in result["core_sleeve"]["reason"]


def test_under_its_cap_it_buys_only_the_room():
    result = cli.decide(payload(**held_sleeve(quantity=5.0)), loaded())      # 450 held, 250 of room

    [order] = sleeve_buys(result)
    assert order["quantity"] == 2                                            # 250 // 90.10


def test_an_overnight_cycle_orders_nothing():
    result = cli.decide(payload(now="2026-08-04T00:00:00Z", cycle_id="2026-08-04T00:00:00Z-overnight"),
                        loaded())

    assert sleeve_buys(result) == []
    assert result["core_sleeve"]["ordered"] is False
    assert result["core_sleeve"]["reason"] == "this cycle cannot buy"


def test_a_buy_already_resting_is_not_doubled():
    resting = {"order_id": "b1", "symbol": ETF, "side": "buy", "type": "limit", "price": 90.10,
               "quantity": 7.0, "state": "confirmed", "time_in_force": "gfd", "placed_agent": "agentic"}
    result = cli.decide(payload(open_orders=[resting]), loaded())

    assert sleeve_buys(result) == []
    assert "already rests" in result["core_sleeve"]["reason"]


# --- on: what guard does with it -----------------------------------------------


def guard_ctx(**overrides):
    ctx = {
        "config": config(**{core_sleeve.ENABLED: True}),
        "cycle": {"now": "2026-08-03T13:40:00Z", "trading_day": "2026-08-03",
                  "session": "regular_hours", "entries_allowed": True, "cycle_type": "full"},
        "account": {"equity_total": 1400.0, "buying_power": 1000.0, "settled_cash": 1000.0},
        "positions": [], "sectors": {ETF: core_sleeve.BROAD_MARKET},
        "universe": {"candidates": [ETF], "snapshot_age_days": 1},
        "quotes": {ETF: {"bid": 89.98, "ask": 90.00, "last": 89.99}},
        "tradability": {ETF: TRADABLE}, "control": {"high_water_mark": 1400.0, "readable": True,
                                                    "trading_enabled": True},
        "earnings": {}, "loss_closes": [], "open_orders": [], "sizing": {"n_target": 7},
        "calendar": {"closed": [], "early_close": {}},
    }
    ctx.update(overrides)
    return ctx


def sleeve_order(quantity=7, **extra):
    planned = {"symbol": ETF, "quantity": quantity, "limit_price": 90.10}
    return {**core_sleeve.order(planned, guard_ctx()), **extra}


def codes(result):
    return [d.code for row in result.denied for d in row.denials]


def test_guard_approves_the_sleeves_buy_with_no_earnings_date_and_no_simulation():
    result = guard.evaluate([sleeve_order()], guard_ctx(mc_check={}))

    assert codes(result) == []
    assert [o["symbol"] for o in result.approved] == [ETF]


def test_guard_enforces_the_sleeves_own_cap():
    result = guard.evaluate([sleeve_order(quantity=8)], guard_ctx())          # 720.80 against 700

    assert codes(result) == [core_sleeve.CORE_CAP_EXCEEDED]


def test_the_cap_counts_what_the_sleeve_already_holds():
    ctx = guard_ctx(positions=[{"symbol": ETF, "quantity": 5.0, "market_value": 450.0}])

    assert codes(guard.evaluate([sleeve_order(quantity=3)], ctx)) == [core_sleeve.CORE_CAP_EXCEEDED]
    assert codes(guard.evaluate([sleeve_order(quantity=2)], ctx)) == []


def test_the_flag_buys_nothing_on_any_other_symbol():
    ctx = guard_ctx(universe={"candidates": ["PFE"], "snapshot_age_days": 1},
                    sectors={"PFE": "Health Care"},
                    quotes={"PFE": {"bid": 25.10, "ask": 25.13, "last": 25.11}},
                    tradability={"PFE": TRADABLE})
    forged = {**sleeve_order(quantity=20), "symbol": "PFE", "limit_price": 25.20}

    denied = codes(guard.evaluate([forged], ctx))
    assert guard.EARNINGS_DATE_UNAVAILABLE in denied
    assert guard.POSITION_CAP_EXCEEDED in denied


def test_the_flag_buys_nothing_while_the_switch_is_off():
    ctx = guard_ctx(config=config(**OFF))

    denied = codes(guard.evaluate([sleeve_order()], ctx))
    assert guard.EARNINGS_DATE_UNAVAILABLE in denied and guard.POSITION_CAP_EXCEEDED in denied


def test_an_unflagged_buy_of_the_etf_meets_every_ordinary_rule():
    plain = {k: v for k, v in sleeve_order().items() if k != core_sleeve.ORDER_FLAG}

    assert guard.EARNINGS_DATE_UNAVAILABLE in codes(guard.evaluate([plain], guard_ctx()))


def test_the_sleeve_takes_no_play_slot_in_guard_or_in_sizing():
    seven = [{"symbol": f"N{i}", "quantity": 1.0, "market_value": 100.0} for i in range(6)]
    book = [*seven, {"symbol": ETF, "quantity": 5.0, "market_value": 450.0}]
    ctx = guard_ctx(positions=book)

    state = guard._PlanState(ctx)
    assert len(state.open_symbols) == 6 and ETF not in state.open_symbols
    assert state.position_value[ETF] == 450.0

    ladder = sizing._ladder(ctx)
    assert ladder.n_target == 7 and ladder.open_slots == 1
    off = sizing._ladder({**ctx, "config": config(**OFF)})
    assert off.open_slots == 0


def test_the_loss_re_entry_window_still_applies_to_the_sleeve():
    closed = [{"symbol": ETF, "close_date": "2026-07-20", "loss_amount": 12.0}]
    result = cli.decide(payload(loss_closes=closed), loaded())

    assert sleeve_buys(result) == []
    assert guard.WASH_SALE_COOLDOWN in denials(result)
    assert result["core_sleeve"]["ordered"] is True and result["core_sleeve"]["placed"] is False


def test_the_drawdown_halt_still_stops_it():
    result = cli.decide(payload(control={"high_water_mark": 2000.0}), loaded())

    assert sleeve_buys(result) == []


# --- on: no play may touch it ----------------------------------------------------


def test_no_play_proposes_the_etf_however_oversold_it_is():
    oversold = payload(signals={ETF: {"rsi": 22.0, "sma_20": 97.0, "sma_50": 95.0,
                                      "sma_200": 80.0, "atr": 1.0}})
    result = cli.decide(oversold, loaded())

    named = [c["symbol"] for c in result["entries"]["candidates"]] + \
            [s["symbol"] for s in result["entries"]["skipped"]]
    assert ETF not in named
    assert [o["play_id"] for o in sleeve_buys(result)] == ["CORE"]


def test_the_sleeve_is_never_topped_up_by_the_top_up_rule():
    ctx = plays_context(config=config(**{core_sleeve.ENABLED: True}))
    winner = position(symbol=ETF, play_id="CORE", entry_price=80.0, market_value=630.0, quantity=7.0)
    ctx["positions"] = [winner]
    ctx["quotes"] = {ETF: {"bid": 89.98, "ask": 90.00, "last": 89.99}}
    ctx["signals"] = {ETF: {"rsi": 60.0, "sma_20": 88.0, "sma_50": 85.0, "sma_200": 80.0, "atr": 1.0}}

    assert plays.top_up._top_up(winner, ctx) is None
    assert plays.top_up._top_up({**winner, "play_id": "DIP"}, ctx) is not None


# --- on: how a held sleeve is managed --------------------------------------------


def exit_codes(result, symbol=ETF):
    return [e["code"] for e in result["exits"]["exits"] if e["symbol"] == symbol]


def test_a_held_sleeve_has_no_stop_check_and_no_time_stop():
    """Down a third on a 1.00 ATR, 400 sessions in: any other play would have
    sold it twice over. The sleeve holds."""
    crashed = payload(**held_sleeve(), quotes={ETF: {"bid": 59.98, "ask": 60.00, "last": 60.00}})
    result = cli.decide(crashed, loaded())

    assert exit_codes(result) == []
    assert not [o for o in result["plan"]["orders"] if o["symbol"] == ETF and o["side"] == "sell"]


def test_the_same_position_under_another_play_would_have_been_stopped():
    """The control for the test above: the exemption is the play's, not luck."""
    as_dip = held_sleeve()
    as_dip["reconcile"]["positions"][1]["play_id"] = "DIP"
    as_dip["prior_positions"][1]["play_id"] = "DIP"
    crashed = payload(**as_dip, quotes={ETF: {"bid": 59.98, "ask": 60.00, "last": 60.00}})
    result = cli.decide(crashed, core_off())

    assert exit_codes(result) == ["ATR_STOP"]


def test_a_held_sleeve_is_sold_at_its_target():
    at_target = payload(**held_sleeve(), quotes={ETF: {"bid": 108.10, "ask": 108.12, "last": 108.10}})
    result = cli.decide(at_target, loaded())

    assert exit_codes(result) == ["TARGET"]


def test_a_sleeve_being_sold_is_not_bought_in_the_same_cycle():
    """Two shares held, so there is room and cash for more; the target is
    reached, so they are leaving. Buying more as they leave would be a round
    trip the cycle made with itself."""
    small = payload(**held_sleeve(quantity=2.0),
                    quotes={ETF: {"bid": 108.10, "ask": 108.12, "last": 108.10}})
    result = cli.decide(small, loaded())

    assert exit_codes(result) == ["TARGET"]
    assert result["core_sleeve"]["action"] == "buy"       # the arithmetic alone would buy
    assert result["core_sleeve"]["ordered"] is False
    assert sleeve_buys(result) == []


def test_the_target_follows_the_config_value():
    at_ten = payload(**held_sleeve(), quotes={ETF: {"bid": 99.10, "ask": 99.12, "last": 99.10}})

    assert exit_codes(cli.decide(at_ten, loaded())) == []
    assert exit_codes(cli.decide(at_ten, loaded(**{core_sleeve.TARGET_PCT: 0.10}))) == ["TARGET"]


def test_the_drawdown_halt_flattens_the_sleeve_with_the_rest_of_the_book():
    result = cli.decide(payload(**held_sleeve(), control={"high_water_mark": 2000.0}), loaded())

    assert exit_codes(result) == ["DRAWDOWN_FLATTEN"]


def test_its_one_resting_order_is_a_limit_sell_above_its_buy_and_never_a_stop():
    ctx = plays_context(config=config(**{core_sleeve.ENABLED: True}))
    ctx["positions"] = [position(symbol=ETF, play_id="CORE", entry_price=90.0, quantity=7.0,
                                 market_value=630.0, high_since_entry=90.0)]
    ctx["quotes"] = {ETF: {"bid": 89.98, "ask": 90.00, "last": 89.99}}
    ctx["signals"] = {ETF: {"rsi": 50.0, "sma_20": 89.0, "sma_50": 88.0, "sma_200": 80.0, "atr": 1.0}}

    [order] = protection.desired(ctx).orders
    assert order.kind == protection.TARGET and order.order_type == protection.LIMIT
    assert order.stop_price is None
    assert order.limit_price == pytest.approx(108.0)


def test_the_play_declares_no_stop_check_and_no_time_stop():
    play = plays.PLAYS["CORE"]

    assert play.stop_check is False and play.time_stop is False
    assert all(p.stop_check and p.time_stop for name, p in plays.PLAYS.items() if name != "CORE")
    assert not [name for name in vars(core_sleeve) if "STOP" in name.upper()]


# --- attribution, listing, validation ---------------------------------------------


def attribution(config_, prior):
    ctx = {"config": config_, "positions": [{"symbol": ETF, "quantity": 7.0}],
           "prior_positions": prior, "fills": [], "cycle": {"trading_day": "2026-08-03"}}
    return reconciler._attribution(ctx, {}, frozenset())


def test_the_etf_is_the_sleeves_however_the_shares_arrived():
    on = config(**{core_sleeve.ENABLED: True})
    adopted = [{"symbol": ETF, "play_id": "ADOPTED", "adopted": True, "adopted_on": "2026-07-01"}]

    assert attribution(on, adopted)[ETF].play_id == "CORE"
    assert attribution(on, adopted)[ETF].adopted is False
    assert attribution(on, [{"symbol": ETF, "play_id": "CORE"}])[ETF].play_id == "CORE"


def test_off_an_etf_position_keeps_whatever_owner_it_had():
    adopted = [{"symbol": ETF, "play_id": "ADOPTED", "adopted": True, "adopted_on": "2026-07-01"}]

    assert attribution(config(**OFF), adopted)[ETF].play_id == "ADOPTED"
    # A sleeve bought while the switch was on keeps its terms after it is off.
    assert attribution(config(**OFF), [{"symbol": ETF, "play_id": "CORE"}])[ETF].play_id == "CORE"


def test_on_the_etf_is_listed_as_broad_market_without_editing_the_file():
    on = core_sleeve.loaded_with_sleeve(loaded())

    assert on["etfs"]["broad"][ETF] == core_sleeve.BROAD_MARKET
    assert ETF not in core_off()["etfs"]["broad"]
    assert core_sleeve.loaded_with_sleeve(on) is on       # already listed: unchanged


def test_sizing_and_the_sleeve_agree_on_which_symbol_it_is():
    for cfg in (config(), config(**{core_sleeve.ENABLED: True}),
                config(**{core_sleeve.ENABLED: True, core_sleeve.SYMBOL: "VOO"})):
        for name in (ETF, "VOO", "F", None):
            assert sizing._is_sleeve(name, {"config": cfg}) == core_sleeve.is_sleeve_symbol(name, cfg)


@pytest.mark.parametrize("key, value", [
    (core_sleeve.ENABLED, "yes"),
    (core_sleeve.CAP_PCT, 50),
    (core_sleeve.CAP_PCT, 0.0),
    (core_sleeve.BUFFER_SLOTS, -1),
    (core_sleeve.TARGET_PCT, 0.0),
    (core_sleeve.TARGET_PCT, -0.05),
    (core_sleeve.TARGET_PCT, "20%"),
    (core_sleeve.SYMBOL, ""),
])
def test_a_mistyped_value_stops_the_config_loading(key, value):
    with pytest.raises(ValueError):
        cli._validate_core_sleeve({**config(), key: value})


def test_switching_it_on_with_no_symbol_stops_the_config_loading():
    with pytest.raises(ValueError):
        cli._validate_core_sleeve({**config(), core_sleeve.ENABLED: True, core_sleeve.SYMBOL: None})


def test_the_shipped_values_load():
    cli._validate_core_sleeve(config())
    cli._validate_core_sleeve(config(**{core_sleeve.ENABLED: True}))
