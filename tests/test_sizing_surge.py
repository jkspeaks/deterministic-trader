"""§17's SURGE full-send sizing.

When `edge_control.full_send_enabled` is true and `plays` hands sizing one or
more `full_deployment` candidates, `sizing.size` commits the entire available
settled cash across up to `SURGE_MAX_NAMES` of them, split by conviction,
ignoring §4.4's slot/position/sector caps. Every ordinary candidate in the
same cycle is displaced with `FULL_SEND_ACTIVE`.
"""

from __future__ import annotations

from src import sizing
from tests.factories import config


def ctx(buying_power=1000.0, **overrides):
    base = {
        "cycle": {"session": "regular_hours"},
        "account": {
            "equity_total": buying_power,
            "cash": buying_power,
            "buying_power": buying_power,
            "settled_cash": buying_power,
            "sold_this_session": False,
        },
        "positions": [],
        "quotes": {},
        "sectors": {},
        "config": config(),
        # The full-send switch lives in edge_control, not config.
        "control": {"full_send_enabled": True},
    }
    base.update(overrides)
    return base


def surge_cand(symbol, conviction, **overrides):
    return {
        "symbol": symbol,
        "play_id": "SURGE",
        "conviction": conviction,
        "full_deployment": True,
        "limit_price": None,
        "session": None,
        **overrides,
    }


def paths(result):
    return {e.symbol: e for e in result.entries}


def test_one_name_takes_the_whole_balance():
    c = ctx(buying_power=1000.0, quotes={"UP": {"ask": 100.0, "last": 100.0}})
    result = sizing.size([surge_cand("UP", 0.7)], c)

    assert len(result.entries) == 1
    entry = result.entries[0]
    assert entry.symbol == "UP"
    assert entry.quantity == 10.0
    assert entry.notional == 1000.0
    assert entry.path == sizing.WHOLE
    assert entry.full_deployment is True
    assert entry.order_type == "limit"
    assert entry.resting_stop_eligible is True


def test_two_names_split_the_cash_by_conviction():
    c = ctx(
        buying_power=1000.0,
        quotes={"UP": {"ask": 100.0, "last": 100.0}, "GO": {"ask": 50.0, "last": 50.0}},
    )
    result = sizing.size([surge_cand("UP", 0.9), surge_cand("GO", 0.6)], c)

    by = paths(result)
    # UP's weighted share is 1000 * 0.9/1.5 = 600 -> 6 shares.
    assert by["UP"].quantity == 6.0
    assert by["UP"].notional == 600.0
    # GO is last, so it takes the remainder: 400 -> 8 shares at 50.
    assert by["GO"].quantity == 8.0
    assert by["GO"].notional == 400.0


def test_whole_share_flooring_leaves_the_dust_unspent():
    c = ctx(buying_power=1000.0, quotes={"UP": {"ask": 300.0, "last": 300.0}})
    result = sizing.size([surge_cand("UP", 0.8)], c)

    assert result.entries[0].quantity == 3.0
    assert result.entries[0].notional == 900.0


def test_an_ordinary_candidate_is_displaced_by_the_full_send():
    c = ctx(
        buying_power=1000.0,
        quotes={"UP": {"ask": 100.0, "last": 100.0}, "F": {"ask": 15.0, "last": 15.0}},
        sectors={"F": "Consumer Discretionary"},
    )
    normal = {
        "symbol": "F",
        "play_id": "DIP",
        "conviction": 0.95,
        "limit_price": None,
        "session": None,
    }
    result = sizing.size([surge_cand("UP", 0.7), normal], c)

    assert [e.symbol for e in result.entries] == ["UP"]
    displaced = [u for u in result.unsized if u.symbol == "F"]
    assert displaced and displaced[0].reason == sizing.FULL_SEND_ACTIVE


def test_more_than_the_cap_are_displaced_too():
    c = ctx(
        buying_power=3000.0,
        quotes={s: {"ask": 100.0, "last": 100.0} for s in ("A", "B", "C", "D")},
    )
    cands = [surge_cand(s, conv) for s, conv in [("A", 0.9), ("B", 0.8), ("C", 0.7), ("D", 0.6)]]
    result = sizing.size(cands, c)

    assert len(result.entries) == sizing.SURGE_MAX_NAMES
    assert {e.symbol for e in result.entries} == {"A", "B", "C"}
    assert [u.symbol for u in result.unsized if u.reason == sizing.FULL_SEND_ACTIVE] == ["D"]


def test_a_full_send_name_that_cannot_hold_a_share_falls_back_not_freezes():
    c = ctx(buying_power=1000.0, quotes={"UP": {"ask": 2000.0, "last": 2000.0}})
    result = sizing.size([surge_cand("UP", 0.7)], c)

    assert result.entries == []
    assert [u.reason for u in result.unsized] == [sizing.WHOLE_SHARE_ONLY]


def test_the_switch_off_routes_full_deployment_through_the_ordinary_ladder():
    c = ctx(
        buying_power=1000.0,
        quotes={"UP": {"ask": 100.0, "last": 100.0}},
        sectors={"UP": "Technology"},
        control={"full_send_enabled": False},
    )
    result = sizing.size([surge_cand("UP", 0.7)], c)

    # Ordinary sizing splits by the ladder and clamps to §4.4's position cap,
    # so it never reaches the whole balance.
    if result.entries:
        assert result.entries[0].notional < 1000.0
        assert result.entries[0].full_deployment is False
