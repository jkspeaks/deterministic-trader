"""P3 of an earlier audit fixes: portfolio construction and posture sizing.

Everything here is behind `sizing_v2_enabled`, off by default. Three promises:

1. **Off is off** — the result is the ladder's, byte for byte, and none of the
   new fields exist.
2. **On, new money only** — one new name per sector per day, no re-entry into
   a recent loser, a share limit, half a slot for a name that is the same bet
   as a holding, the posture's multiplier and its target-invested ceiling.
3. **One re-entry rule** — `guard` and `sizing` read the same window off the
   same ledger through the same function.
"""

import json
import math
from pathlib import Path

import pytest

from src import guard, reconcile, regime as regimes, sizing
from tests.factories import candidate, config, context, entry, order, reasons, regime_inputs

RECORDED = Path(__file__).parent / "fixtures" / "decide"
CD = "Consumer Discretionary"
IT = "Information Technology"


def book(equity=1000.0, cash=1000.0, holdings=None, v2=True, **overrides):
    """A $1,000 account (n_target 5, $196 slots) holding ``holdings`` as
    ``{symbol: (market_value, sector, extra position fields)}``."""
    holdings = holdings or {}
    ctx = context(
        account={"equity_total": equity, "cash": cash, "buying_power": cash,
                 "settled_cash": cash},
        control={"high_water_mark": equity},
        positions=[
            {"symbol": s, "quantity": 1.0, "market_value": row[0],
             **(row[2] if len(row) > 2 else {"sessions_held": 5})}
            for s, row in holdings.items()
        ],
        config=config(sizing_v2_enabled=v2, **overrides),
    )
    ctx["sectors"] = {"F": CD, "GM": CD, "AAPL": IT, "MSFT": IT}
    for symbol, row in holdings.items():
        ctx["sectors"][symbol] = row[1]
    ctx["quotes"] = {
        "F": {"bid": 14.85, "ask": 14.87, "last": 14.86},
        "GM": {"bid": 39.95, "ask": 40.00, "last": 39.98},
        "AAPL": {"bid": 49.95, "ask": 50.00, "last": 49.98},
        "MSFT": {"bid": 59.95, "ask": 60.00, "last": 59.98},
    }
    ctx["universe"]["candidates"] = ["F", "GM", "AAPL", "MSFT"]
    return ctx


def cands(*symbols):
    return [
        candidate(symbol=s, conviction=0.9 - 0.1 * i) for i, s in enumerate(symbols)
    ]


def with_regime(ctx, **inputs):
    ctx["regime"] = regimes.evaluate(regime_inputs(**inputs), ctx["config"]).as_dict()
    return ctx


UNDER = {"last": 93.0, "sma200": 95.0}


def under(count):
    from tests.factories import SECTOR_ETFS

    return {"sectors": {s: dict(UNDER) for s in SECTOR_ETFS[:count]}}


# --- 1. off is off -----------------------------------------------------------


@pytest.mark.parametrize("inputs", [None, {}, under(5), under(9)])
def test_with_the_flag_off_sizing_is_exactly_the_ladder(inputs):
    """Same names, same quantities, same dict — whatever the regime says, and
    with a held name in the candidate's own sector."""
    def sized(**cfg):
        ctx = book(holdings={"GM": (200.0, CD)}, v2=False, **cfg)
        if inputs is not None:
            with_regime(ctx, **inputs)
        return sizing.size(cands("F", "AAPL", "MSFT"), ctx).as_dict()

    result = sized()

    # The keys P3 added to config are simply absent from the baseline.
    stripped = {
        k: v for k, v in config().items()
        if k not in ("sizing_v2_enabled", "one_per_sector_day", "reentry_block_days",
                     "corr_holdings_budget_fraction", "dead_play_alert_days")
    }
    ctx = book(holdings={"GM": (200.0, CD)}, v2=False)
    ctx["config"] = stripped
    if inputs is not None:
        with_regime(ctx, **inputs)
    assert result == sizing.size(cands("F", "AAPL", "MSFT"), ctx).as_dict()
    assert "v2" not in result
    assert all("adjustments" not in row for row in result["entries"])


def test_the_switch_ships_off():
    assert config()["sizing_v2_enabled"] is False
    assert sizing.v2_enabled({"config": {}}) is False
    assert sizing.v2_enabled({}) is False


# --- 2a. one new name per sector per day -------------------------------------


def test_two_candidates_in_one_sector_size_one_and_name_why_for_the_other():
    result = sizing.size(cands("AAPL", "MSFT", "F"), book())

    assert [e.symbol for e in result.entries] == ["AAPL", "F"]
    assert reasons(result, "MSFT") == [sizing.ONE_PER_SECTOR_DAY_REACHED]
    (unsized,) = result.unsized
    assert IT in unsized.message and "AAPL" in unsized.message


def test_a_name_already_opened_today_takes_its_sectors_one_entry():
    ctx = book(holdings={"MSFT": (60.0, IT, {"sessions_held": 0, "adopted": False})})

    assert reasons(sizing.size(cands("AAPL"), ctx), "AAPL") == [
        sizing.ONE_PER_SECTOR_DAY_REACHED
    ]


def test_a_name_merely_adopted_today_does_not():
    """An inherited position is not an entry this system made."""
    ctx = book(holdings={"MSFT": (60.0, IT, {"sessions_held": 0, "adopted": True})})

    assert [e.symbol for e in sizing.size(cands("AAPL"), ctx).entries] == ["AAPL"]


def test_a_buy_still_resting_takes_its_sectors_one_entry():
    ctx = book()
    ctx["open_orders"] = [{"symbol": "MSFT", "side": "buy", "quantity": 1, "limit_price": 60.0}]

    assert reasons(sizing.size(cands("AAPL"), ctx), "AAPL") == [
        sizing.ONE_PER_SECTOR_DAY_REACHED
    ]


def test_a_top_up_is_not_a_new_entry_for_its_sector():
    ctx = book(holdings={"AAPL": (50.0, IT, {"sessions_held": 0, "adopted": False})})

    assert [e.symbol for e in sizing.size(cands("AAPL"), ctx).entries] == ["AAPL"]


def test_the_sector_day_rule_has_its_own_switch():
    result = sizing.size(cands("AAPL", "MSFT"), book(one_per_sector_day=False))

    assert [e.symbol for e in result.entries] == ["AAPL", "MSFT"]


# --- 2b. correlated with a holding -------------------------------------------


def test_a_name_in_a_held_names_sector_gets_half_a_slot():
    alone = entry(sizing.size(cands("F"), book()), "F")
    result = sizing.size(cands("F"), book(holdings={"GM": (40.0, CD)}))
    beside = entry(result, "F")

    # Half of this book's own slot ($245 with four open slots), at 14.87.
    assert beside.quantity == math.floor(result.ladder.notional * 0.5 / 14.87) == 8
    assert beside.adjustments == (sizing.ADJ_CORRELATED,)
    assert alone.quantity == 13 and alone.adjustments == ()


def test_the_same_sub_industry_counts_even_across_a_sector_mapping_gap():
    ctx = book(holdings={"GM": (40.0, "Industrials")})
    ctx["sub_industries"] = {"F": "Automobile Manufacturers", "GM": "Automobile Manufacturers"}

    assert entry(sizing.size(cands("F"), ctx), "F").adjustments == (sizing.ADJ_CORRELATED,)


def test_a_name_unrelated_to_the_book_keeps_its_whole_slot():
    result = sizing.size(cands("F"), book(holdings={"AAPL": (50.0, IT)}))

    # The whole slot, bounded only by the $240 position cap.
    assert entry(result, "F").quantity == 16
    assert entry(result, "F").adjustments == ()


def test_the_fraction_is_config():
    ctx = book(holdings={"GM": (40.0, CD)}, corr_holdings_budget_fraction=0.25)

    result = sizing.size(cands("F"), ctx)

    assert entry(result, "F").quantity == math.floor(result.ladder.notional * 0.25 / 14.87) == 4


# --- 2c. the share limit -----------------------------------------------------


def test_one_share_over_the_position_cap_is_named_as_a_share_limit():
    """$1,000 at n=5 caps a position at $240."""
    ctx = book()
    ctx["quotes"]["AAPL"] = {"bid": 259.0, "ask": 260.0, "last": 259.5}

    result = sizing.size(cands("AAPL"), ctx)

    assert reasons(result, "AAPL") == [sizing.SHARE_LIMIT]
    assert "position cap" in result.unsized[0].message


def test_no_sized_position_exceeds_the_position_cap():
    ctx = book(holdings={"F": (200.0, CD)})

    sized = entry(sizing.size(cands("F"), ctx), "F")

    cap = sizing.position_cap_fraction(5) * 1000.0
    assert 200.0 + sized.quantity * sized.price <= cap + sizing.MONEY_EPS
    assert sized.quantity == 2          # $40 of room at 14.87


# --- 2d. posture -------------------------------------------------------------


def test_reduced_size_halves_the_slot():
    ctx = with_regime(book(), **under(5))
    assert ctx["regime"]["posture"] == regimes.REDUCED_SIZE

    sized = entry(sizing.size(cands("F"), ctx), "F")

    assert sized.quantity == 6 and sized.adjustments == (sizing.ADJ_POSTURE,)


def test_the_multiplier_is_not_applied_twice_when_the_plays_already_did():
    """Under `regime_v2_enabled` `plays.entries` folds the posture into the
    candidate's own `notional_multiplier`."""
    ctx = with_regime(book(regime_v2_enabled=True), **under(5))
    already = [candidate(symbol="F", conviction=0.9, notional_multiplier=0.5)]

    sized = entry(sizing.size(already, ctx), "F")

    assert sized.quantity == 6 and sizing.ADJ_POSTURE not in sized.adjustments


@pytest.mark.parametrize(
    "inputs, posture",
    [(under(9), regimes.NO_NEW_POSITIONS), ({"spy": {"last": 580.0, "rv20": 0.42}}, regimes.HOLD_CASH)],
)
def test_a_no_entry_posture_sizes_no_new_position(inputs, posture):
    ctx = with_regime(book(), **inputs)

    result = sizing.size(cands("F", "AAPL"), ctx)

    assert result.entries == []
    assert set(reasons(result)) == {sizing.POSTURE_NO_ENTRY}
    assert posture in result.unsized[0].message


def test_new_deployment_stops_at_the_target_invested_ceiling():
    """REDUCED_SIZE targets 50% invested. $460 is held of $1,000, so $40 of
    room is left: two shares of F, not the six the halved slot would buy."""
    ctx = with_regime(book(cash=540.0, holdings={"MSFT": (460.0, IT)}), **under(5))

    result = sizing.size(cands("F"), ctx)

    sized = entry(result, "F")
    assert sized.quantity == 2
    assert sizing.ADJ_TARGET_INVESTED in sized.adjustments
    assert result.v2["deploy_room"] == pytest.approx(40.0)


def test_at_the_target_nothing_more_is_sized_top_ups_included():
    ctx = with_regime(
        book(cash=500.0, holdings={"MSFT": (300.0, IT), "F": (200.0, CD)}), **under(5)
    )

    result = sizing.size(cands("F", "AAPL"), ctx)

    assert result.entries == []
    assert set(reasons(result)) == {sizing.TARGET_INVESTED_REACHED}


def test_a_resting_buy_counts_toward_what_is_invested():
    ctx = with_regime(book(cash=600.0, holdings={"MSFT": (400.0, IT)}), **under(5))
    ctx["open_orders"] = [{"symbol": "GM", "side": "buy", "quantity": 2, "limit_price": 40.0}]

    assert sizing.invested_value(ctx) == pytest.approx(480.0)
    assert sizing.deployment_room(ctx) == pytest.approx(20.0)


def test_no_regime_means_no_multiplier_and_no_ceiling():
    result = sizing.size(cands("F"), book())

    assert entry(result, "F").quantity == 13
    assert result.v2["deploy_room"] is None and result.v2["size_multiplier"] == 1.0


def test_full_send_stands_aside_outside_trade_normally():
    ctx = with_regime(book(), **under(5))
    ctx["control"]["full_send_enabled"] = True
    surge = [candidate(symbol="F", conviction=0.9, play_id="SURGE", full_deployment=True)]

    result = sizing.size(surge, ctx)

    assert result.entries == []
    assert reasons(result, "F") == [sizing.POSTURE_NO_ENTRY]


def test_full_send_still_runs_in_trade_normally():
    ctx = with_regime(book())
    ctx["control"]["full_send_enabled"] = True
    surge = [candidate(symbol="F", conviction=0.9, play_id="SURGE", full_deployment=True)]

    assert sizing.size(surge, ctx).entries[0].full_deployment is True


# --- 3. one re-entry rule ----------------------------------------------------


def loss_row(days_ago, loss=-1.25):
    from datetime import date, timedelta

    row = {"symbol": "F", "close_date": (date(2026, 8, 3) - timedelta(days=days_ago)).isoformat()}
    if loss is not None:
        row["loss_amount"] = loss
    return row


@pytest.mark.parametrize(
    "v2, reentry_days, days_ago, loss, unknown_blocks, blocked",
    [
        # the shipped numbers: wash-sale's 31 contains re-entry's 14
        (False, 14, 5, -1.25, True, True),
        (True, 14, 5, -1.25, True, True),
        (False, 14, 31, -1.25, True, True),
        (True, 14, 31, -1.25, True, True),
        (False, 14, 32, -1.25, True, False),
        (True, 14, 32, -1.25, True, False),
        # a longer re-entry window extends the one rule, only under the switch
        (False, 45, 40, -1.25, True, False),
        (True, 45, 40, -1.25, True, True),
        (True, 45, 46, -1.25, True, False),
        # an unpriced close follows `wash_sale_block_unknown_basis` in both
        (True, 14, 5, None, True, True),
        (True, 14, 5, None, False, False),
        (False, 14, 5, None, False, False),
    ],
)
def test_guard_and_sizing_agree_on_the_re_entry_window(
    v2, reentry_days, days_ago, loss, unknown_blocks, blocked
):
    cfg = dict(reentry_block_days=reentry_days, wash_sale_block_unknown_basis=unknown_blocks)
    ctx = book(v2=v2, **cfg)
    ctx["loss_closes"] = [loss_row(days_ago, loss)]

    window = reconcile.reentry_window_days(ctx["config"])
    assert window == (max(31, reentry_days) if v2 else 31)

    denied = "WASH_SALE_COOLDOWN" in [
        d.code for row in guard.evaluate([order()], ctx).denied for d in row.denials
    ]
    assert denied is blocked

    if v2:
        # ...and sizing declines the same name for the same reason, so the
        # slot goes to the next candidate instead of to a denial.
        result = sizing.size(cands("F", "AAPL"), ctx)
        assert (reasons(result, "F") == [sizing.REENTRY_BLOCKED]) is blocked
        assert entry(result, "AAPL") is not None
    else:
        assert sizing.REENTRY_BLOCKED not in reasons(sizing.size(cands("F"), ctx))


def test_the_dip_recovery_watchlist_reads_the_same_window():
    from src.dip_recovery import watchlist

    ctx = book(reentry_block_days=45)
    ctx["loss_closes"] = [loss_row(40)]

    assert watchlist._wash_sale_symbols(ctx) == {"F": 40}
    ctx["config"] = config(sizing_v2_enabled=False, reentry_block_days=45)
    assert watchlist._wash_sale_symbols(ctx) == {}


# --- a recorded cohort --------------------------------------------------------


def cohort(v2):
    """A recorded 09:40 cycle, re-sized from its own runlog row: the
    ladder's account, the book, each name's sector and the limit it bought at."""
    row = json.loads(
        (RECORDED / "full_cycles_2026-09-18_and_10-02.json").read_text(encoding="utf-8")
    )["2026-09-18"]
    ladder = row["sizing"]["ladder"]
    cash = ladder["deployable"] + ladder["cash_reserve"]
    ctx = context(
        account={"equity_total": ladder["equity_total"], "cash": cash,
                 "buying_power": cash, "settled_cash": cash},
        positions=[{**p, "quantity": 1.0} for p in row["book"]],
        config=config(sizing_v2_enabled=v2),
    )
    ctx["cycle"] = {**ctx["cycle"], **row["cycle"], "session": "regular_hours"}
    ctx["sectors"] = row["sectors"]
    ctx["universe"]["candidates"] = row["pool"]
    limits = {o["symbol"]: o["limit_price"] for o in row["approved"]}
    ranked = [
        candidate(symbol=c["symbol"], play_id=c["play_id"], conviction=0.9 - 0.1 * i,
                  limit_price=limits[c["symbol"]])
        for i, c in enumerate(row["entries"]["candidates"])
    ]
    return row, sizing.size(ranked, ctx)


def test_the_recorded_cohort_is_reproduced_exactly_with_the_flag_off():
    row, result = cohort(False)

    assert [(e.symbol, e.quantity) for e in result.entries] == [
        (e["symbol"], e["quantity"]) for e in row["sizing"]["entries"]
    ]
    assert [e.notional for e in result.entries] == pytest.approx(
        [e["notional"] for e in row["sizing"]["entries"]]
    )
    assert result.ladder.notional == pytest.approx(row["sizing"]["ladder"]["notional"])


def test_the_recorded_cohort_under_sizing_v2():
    """Four DIP buys in one cycle: INVH and KIM both Real Estate beside a held
    SPG, RF Financials beside a held TROW, RTX alone in Industrials."""
    _, result = cohort(True)

    sized = {e.symbol: e for e in result.entries}
    assert sorted(sized) == ["INVH", "RF", "RTX"]
    assert sized["INVH"].quantity == 3 and sized["INVH"].adjustments == (sizing.ADJ_CORRELATED,)
    assert sized["RF"].quantity == 3 and sized["RF"].adjustments == (sizing.ADJ_CORRELATED,)
    assert sized["RTX"].quantity == 1 and sized["RTX"].adjustments == ()
    assert reasons(result, "KIM") == [sizing.ONE_PER_SECTOR_DAY_REACHED]
    # $360 committed where the ladder committed $577.
    assert sum(e.notional for e in result.entries) == pytest.approx(360.39)
