"""`ops/backtest_engine.py` — the honest backtest's arithmetic (S1).

Every number in the S1 verdict comes through these functions, so each test
pins one rule with a series small enough to work by
hand: what a cycle can know at 09:40, which exit acts when, what a trade
costs, what the account may take, who was in the index, and how far
survivorship could move an edge.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from ops import backtest_engine as bt
from src import sizing


def series(closes, opens=None, highs=None, lows=None, rsi=None, atr=None,
           sma20=None, sma200=None, adv=None, until=None) -> bt.Series:
    n = len(closes)
    opens = list(opens) if opens is not None else list(closes)
    return bt.Series(
        o=opens,
        h=list(highs) if highs is not None else [max(o, c) if o is not None else None for o, c in zip(opens, closes)],
        l=list(lows) if lows is not None else [min(o, c) if o is not None else None for o, c in zip(opens, closes)],
        c=list(closes),
        rsi=list(rsi) if rsi is not None else [40.0] * n,
        atr=list(atr) if atr is not None else [1.0] * n,
        sma20=list(sma20) if sma20 is not None else [110.0] * n,
        sma200=list(sma200) if sma200 is not None else [100.0] * n,
        adv=list(adv) if adv is not None else None,
        until=list(until) if until is not None else None,
    )


FLAT = [100.0] * 14


# --- the signal: what a 09:40 cycle can know ---------------------------------


def test_the_signal_reads_yesterdays_indicators_and_todays_open():
    s = series(FLAT, rsi=[40, 28, 40] + [40] * 11)

    assert bt.dip_signal(s, 2) is not None        # RSI 28 at the close of day 1
    assert bt.dip_signal(s, 1) is None            # day 0's RSI was 40
    assert bt.dip_signal(s, 3) is None            # day 2's RSI recovered
    assert bt.dip_signal(s, 0) is None            # no prior day at all


def test_the_trend_band_and_the_twenty_day_are_tested_against_the_open():
    low_rsi = [28.0] * 14
    holding = series(FLAT, opens=[97.0] * 14, rsi=low_rsi)            # exactly 3% under
    knife = series(FLAT, opens=[96.9] * 14, rsi=low_rsi)
    not_dipped = series(FLAT, opens=[111.0] * 14, rsi=low_rsi)        # above its 20-day

    assert bt.dip_signal(holding, 5) is not None
    assert bt.dip_signal(knife, 5) is None
    assert bt.dip_signal(not_dipped, 5) is None


def test_conviction_is_the_larger_of_depth_and_rsi_extremity():
    # 5.5% under a 20-day of 110 is 0.5 of the 10% scale... (110-104)/110 = 5.45%.
    shallow = series(FLAT, opens=[104.0] * 14, rsi=[29.0] * 14)
    extreme = series(FLAT, opens=[109.0] * 14, rsi=[15.0] * 14)

    assert bt.dip_signal(shallow, 5) == pytest.approx((110 - 104) / 110 / 0.10)
    assert bt.dip_signal(extreme, 5) == pytest.approx(1.0)            # 15 points under 30


def test_a_missing_indicator_or_open_is_no_signal():
    assert bt.dip_signal(series(FLAT, rsi=[None] * 14), 5) is None
    assert bt.dip_signal(series(FLAT, opens=[None] * 14, rsi=[28.0] * 14), 5) is None
    assert bt.dip_signal(series(FLAT, rsi=[28.0] * 14, sma200=[None] * 14), 5) is None


def test_the_earnings_veto_blocks_inside_three_sessions_and_not_on_an_unknown_date():
    rsi = [28.0] * 14
    near = series(FLAT, rsi=rsi, until=[3] * 14)
    clear = series(FLAT, rsi=rsi, until=[4] * 14)
    unknown = series(FLAT, rsi=rsi, until=[None] * 14)

    assert bt.dip_signal(near, 5, earnings_veto=True) is None
    assert bt.dip_signal(clear, 5, earnings_veto=True) is not None
    assert bt.dip_signal(unknown, 5, earnings_veto=True) is not None
    assert bt.dip_signal(near, 5) is not None                         # veto off: not applied
    assert bt.blocked_by_earnings(near, 5) and not bt.blocked_by_earnings(unknown, 5)


# --- earnings dates ----------------------------------------------------------

WEEK = [date(2024, 1, d) for d in (8, 9, 10, 11, 12, 16, 17, 18)]     # the 15th is a holiday


def test_sessions_until_a_report_count_sessions_not_calendar_days():
    until = bt.sessions_until_report(WEEK, [date(2024, 1, 17)])

    # Friday the 12th: Tuesday the 16th, then the 17th. Two sessions, five days.
    assert until == [6, 5, 4, 3, 2, 1, 0, None]


def test_a_report_dated_on_a_non_session_counts_as_the_next_one():
    until = bt.sessions_until_report(WEEK, [date(2024, 1, 14)])           # a Sunday

    assert until[:6] == [5, 4, 3, 2, 1, 0] and until[6] is None


def test_the_next_report_is_used_once_the_last_has_passed():
    until = bt.sessions_until_report(WEEK, [date(2024, 1, 9), date(2024, 1, 18)])

    assert until == [1, 0, 5, 4, 3, 2, 1, 0]


def test_no_report_inside_the_horizon_is_unknown_and_no_dates_is_no_history():
    far = bt.sessions_until_report(WEEK, [date(2024, 9, 1)])

    assert far == [None] * 8
    assert bt.sessions_until_report(WEEK, []) is None
    assert bt.sessions_until_report(WEEK, None) is None


def test_coverage_needs_a_report_on_each_side():
    reports = [date(2023, 10, 20), date(2024, 1, 17)]

    assert bt.earnings_covered(reports, date(2024, 1, 8))
    assert not bt.earnings_covered(reports, date(2023, 10, 1))        # nothing before
    assert not bt.earnings_covered(reports, date(2024, 2, 1))         # nothing after
    assert not bt.earnings_covered([date(2023, 1, 1), date(2024, 12, 1)], date(2024, 1, 8))
    assert not bt.earnings_covered(None, date(2024, 1, 8))


# --- one trade ---------------------------------------------------------------

NO_RSI = bt.Exits(rsi_exit=None)


def test_the_fill_is_the_open_of_the_signal_day():
    s = series(FLAT, opens=[100.0, 100.0, 98.0] + [100.0] * 11)

    trade = bt.simulate_trade(s, 2, NO_RSI)

    assert trade.entry == 98.0 and trade.entry_day == 2


def test_no_open_no_trade():
    assert bt.simulate_trade(series(FLAT, opens=[None] * 14), 2) is None


def test_the_time_stop_sells_at_the_open_of_the_eighth_session():
    closes = [100.0 + i for i in range(14)]
    s = series(closes, atr=[50.0] * 14)                 # stop far away, target far away
    exits = bt.Exits(rsi_exit=None, target=5.0)

    trade = bt.simulate_trade(s, 2, exits)

    assert (trade.reason, trade.exit_day, trade.held) == (bt.TIME_STOP, 10, 8)
    assert trade.exit == s.o[10] and trade.ret == pytest.approx(110 / 102 - 1)


def test_the_resting_target_can_fill_on_the_fill_day_and_nothing_else_can():
    hit = series(FLAT, highs=[100.0, 100.0, 106.5] + [100.0] * 11)
    # A collapse on the fill day: through any stop, RSI high, and still held.
    dropped = series([100.0, 100.0, 70.0] + [70.0] * 11,
                     opens=[100.0, 100.0, 100.0] + [70.0] * 11, rsi=[90.0] * 14)

    target = bt.simulate_trade(hit, 2, NO_RSI)
    held = bt.simulate_trade(dropped, 2)

    assert (target.reason, target.exit_day, target.exit, target.held) == (bt.TARGET, 2, 106.0, 0)
    assert target.ret == pytest.approx(0.06)
    # Not sold on day 2 at its 70 close; sold at the next open's cycle.
    assert held.exit_day == 3 and held.exit == 70.0 and held.reason == bt.ATR_STOP


def test_a_gap_above_the_target_fills_at_the_open_not_at_the_limit():
    s = series(FLAT, opens=[100.0, 100.0, 100.0, 109.0] + [100.0] * 10)

    trade = bt.simulate_trade(s, 2, NO_RSI)

    assert (trade.reason, trade.exit_day, trade.exit) == (bt.TARGET, 3, 109.0)


def test_the_rsi_exit_reads_the_prior_close_and_sells_at_the_open():
    rsi = [40.0, 28.0, 45.0, 55.0, 40.0] + [40.0] * 9      # 55 at the close of day 3
    s = series(FLAT, opens=[100.0] * 4 + [101.0] + [100.0] * 9, rsi=rsi, atr=[50.0] * 14)

    with_rsi = bt.simulate_trade(s, 2)
    without = bt.simulate_trade(s, 2, NO_RSI)

    assert (with_rsi.reason, with_rsi.exit_day, with_rsi.exit) == (bt.SIGNAL, 4, 101.0)
    assert without.reason == bt.TIME_STOP and without.exit_day == 10


def test_the_stop_is_checked_at_the_open_and_again_at_the_close():
    # Entry 100, ATR 2: fixed stop 94.
    gap = series([100.0] * 3 + [93.0] * 11, opens=[100.0] * 3 + [93.0] * 11, atr=[2.0] * 14)
    fade = series([100.0] * 3 + [93.5] + [93.5] * 10, opens=[100.0] * 4 + [93.5] * 10, atr=[2.0] * 14)

    at_open = bt.simulate_trade(gap, 2, NO_RSI)
    at_close = bt.simulate_trade(fade, 2, NO_RSI)

    assert (at_open.reason, at_open.exit_day, at_open.exit) == (bt.ATR_STOP, 3, 93.0)
    assert (at_close.reason, at_close.exit_day, at_close.exit) == (bt.ATR_STOP, 3, 93.5)


def test_an_intraday_dip_through_the_stop_that_closes_above_it_is_not_a_stop():
    """The stop is a per-cycle check, not a resting order: a low the two
    cycles never saw sells nothing."""
    s = series(FLAT, lows=[100.0] * 3 + [80.0] + [100.0] * 10, atr=[2.0] * 14)

    trade = bt.simulate_trade(s, 2, NO_RSI)

    assert trade.reason == bt.TIME_STOP
    assert trade.mae == pytest.approx(-0.20)            # and the excursion is still recorded


def test_the_trailing_stop_ratchets_from_the_high_since_entry():
    # Entry 100, ATR 1. Runs to 105 (trail 105 - 3.5 = 101.5), then closes 101.
    closes = [100.0, 100.0, 100.0, 105.0, 101.0] + [101.0] * 9
    s = series(closes, opens=[100.0] * 3 + [104.0, 104.0] + [101.0] * 9, atr=[1.0] * 14)

    trade = bt.simulate_trade(s, 2, NO_RSI)

    assert (trade.reason, trade.exit_day, trade.exit) == (bt.ATR_STOP, 4, 101.0)
    assert trade.ret == pytest.approx(0.01)             # a stop that locks in a gain
    assert trade.mfe == pytest.approx(0.05)


def test_the_earnings_deadline_flattens_at_the_open_two_sessions_out():
    until = [9, 8, 7, 6, 5, 4, 3, 2, 1, 0] + [60] * 4
    s = series(FLAT, opens=[100.0] * 7 + [99.0] + [100.0] * 6, atr=[50.0] * 14, until=until)

    flat = bt.simulate_trade(s, 2, bt.Exits(rsi_exit=None, earnings_flatten=2))
    ignored = bt.simulate_trade(s, 2, NO_RSI)

    assert (flat.reason, flat.exit_day, flat.exit) == (bt.EARNINGS, 7, 99.0)
    assert ignored.reason == bt.TIME_STOP


def test_a_day_with_no_bar_is_skipped_and_does_not_count_as_a_session():
    closes = list(FLAT)
    closes[5] = None
    s = series(closes, opens=closes, highs=closes, lows=closes, atr=[50.0] * 14)

    trade = bt.simulate_trade(s, 2, NO_RSI)

    assert (trade.reason, trade.held, trade.exit_day) == (bt.TIME_STOP, 8, 11)


def test_a_trade_the_data_ends_on_is_open_with_no_return():
    trade = bt.simulate_trade(series([100.0] * 6, atr=[50.0] * 6), 2, NO_RSI)

    assert trade.reason == bt.OPEN and trade.ret is None and trade.exit_day is None


def test_a_missing_atr_means_no_stop_rather_than_a_guessed_one():
    s = series([100.0] * 3 + [50.0] * 11, opens=[100.0] * 3 + [50.0] * 11, atr=[None] * 14)

    assert bt.simulate_trade(s, 2, NO_RSI).reason == bt.TIME_STOP


# --- costs -------------------------------------------------------------------


def test_the_half_spread_comes_from_the_measured_buckets():
    costs = bt.Costs()

    assert costs.half_spread(900e6) == pytest.approx(0.000284)
    assert costs.half_spread(300e6) == pytest.approx(0.000419)
    assert costs.half_spread(50e6) == pytest.approx(0.001029)
    assert costs.half_spread(10e6) == pytest.approx(0.001165 + 0.00075)     # the thin haircut
    # Unknown volume is charged as the worst case, never as free.
    assert costs.half_spread(None) == pytest.approx(0.001165 + 0.00075)


def test_slippage_is_ten_bps_per_percent_of_adv():
    costs = bt.Costs()

    assert costs.slippage(1_000_000.0, 100e6) == pytest.approx(0.0010)
    assert costs.slippage(200.0, 100e6) == pytest.approx(2e-7)              # the live size: nothing


def test_a_target_exit_pays_no_spread_and_every_other_exit_does():
    costs = bt.Costs()

    assert costs.exit(200.0, 300e6, bt.TARGET) == pytest.approx(costs.slippage(200.0, 300e6))
    assert costs.exit(200.0, 300e6, bt.TIME_STOP) == pytest.approx(0.000419, abs=1e-6)
    assert costs.entry(200.0, 300e6) == pytest.approx(0.000419, abs=1e-6)


def test_net_return_is_bought_dearer_and_sold_cheaper():
    costs = bt.Costs()
    half = 0.000419

    net = costs.net_return(0.02, 200.0, 300e6, 300e6, bt.TIME_STOP)
    passive = costs.net_return(0.06, 200.0, 300e6, 300e6, bt.TARGET)

    assert net == pytest.approx(1.02 * (1 - half) / (1 + half) - 1, abs=1e-6)
    assert passive == pytest.approx(1.06 / (1 + half) - 1, abs=1e-6)
    assert bt.Costs(enabled=False).net_return(0.02, 200.0, 300e6, 300e6, bt.TIME_STOP) == pytest.approx(0.02)


def test_the_fill_draw_is_seeded_and_keyed_on_the_signal():
    costs = bt.Costs(fill_probability=bt.OBSERVED_FILL_PROBABILITY)
    draws = [costs.fills(date(2024, 1, 1) + timedelta(days=i), "MU") for i in range(2000)]

    # A second object with the same seed draws the same: a re-run of the
    # backtest must miss the same entries.
    again = bt.Costs(fill_probability=bt.OBSERVED_FILL_PROBABILITY)
    assert draws == [again.fills(date(2024, 1, 1) + timedelta(days=i), "MU") for i in range(2000)]
    other_seed = bt.Costs(fill_probability=bt.OBSERVED_FILL_PROBABILITY, seed=1)
    assert draws != [other_seed.fills(date(2024, 1, 1) + timedelta(days=i), "MU") for i in range(2000)]
    assert sum(draws) / 2000 == pytest.approx(6 / 13, abs=0.03)
    assert all(bt.Costs().fills(date(2024, 1, 1), s) for s in ("MU", "ON"))   # p = 1


def test_capacity_is_where_round_trip_slippage_equals_the_edge():
    # A 20 bps edge is eaten by 10 bps a side: 1% of ADV.
    assert bt.capacity(0.0020, 100e6) == pytest.approx(1e6)
    assert bt.capacity(0.0, 100e6) is None and bt.capacity(-0.001, 100e6) is None


def test_the_hashed_pick_is_stable_and_spread_over_the_pool():
    pool = ["AAA", "BBB", "CCC", "DDD"]
    picks = [bt.pick(pool, 1, m, "2024-01-02", "MU") for m in range(400)]

    assert picks == [bt.pick(pool, 1, m, "2024-01-02", "MU") for m in range(400)]
    assert set(picks) == set(pool)
    assert bt.pick([], 1, 0) is None


# --- the portfolio replay ----------------------------------------------------

CONFIG = {"min_position_size": 200.0}
SHIPPED = bt.Caps(
    n_target=lambda equity: sizing.n_target(equity, CONFIG),
    position_cap_fraction=sizing.position_cap_fraction,
    sector_cap_fraction=sizing.sector_cap_fraction,
)
DAYS = [date(2024, 1, 1) + timedelta(days=i) for i in range(14)]
GROSS = bt.Costs(enabled=False)


def rising(n=14, start=100.0, step=1.0):
    closes = [start + step * i for i in range(n)]
    return series(closes, atr=[50.0] * n)


def cand(symbol, sector="Energy", conviction=0.5, signal=None):
    return bt.Candidate(symbol, signal or symbol, sector, conviction)


def test_the_slot_count_is_the_shipped_ladder():
    assert SHIPPED.n_target(1400.0) == 7
    assert SHIPPED.n_target(1000.0) == 5


def test_one_trade_moves_cash_and_equity_by_exactly_its_return():
    panel = {"AAA": rising()}
    exits = bt.Exits(rsi_exit=None, target=5.0)

    result = bt.replay(DAYS, {2: [cand("AAA")]}, panel, SHIPPED, exits, GROSS)

    (trade,) = result.trades
    # 7 slots, 2% reserve: (1400 - 28) / 7 = 196.
    assert trade.cost == pytest.approx(196.0)
    assert (trade.entry_day, trade.exit_day, trade.reason) == (2, 10, bt.TIME_STOP)
    assert trade.net == pytest.approx(110 / 102 - 1)
    assert result.equity[-1] == pytest.approx(1400.0 + 196.0 * (110 / 102 - 1))
    # Marked daily: on day 5 the position is worth 196 * 105/102.
    assert result.equity[5] == pytest.approx(1400.0 - 196.0 + 196.0 * 105 / 102)


def test_costs_come_out_of_the_trade_and_the_equity():
    panel = {"AAA": series([100.0] * 14, atr=[50.0] * 14, adv=[300e6] * 14)}
    exits = bt.Exits(rsi_exit=None, target=5.0)

    result = bt.replay(DAYS, {2: [cand("AAA")]}, panel, SHIPPED, exits, bt.Costs())

    (trade,) = result.trades
    assert trade.gross == pytest.approx(0.0)
    assert trade.net == pytest.approx((1 - 0.000419) / (1 + 0.000419) - 1, abs=1e-6)
    assert result.equity[-1] < 1400.0


def test_candidates_are_taken_highest_conviction_first_up_to_the_daily_limit():
    names = ["AAA", "BBB", "CCC", "DDD"]
    panel = {n: rising() for n in names}
    today = [cand(n, sector=f"S{i}", conviction=c) for i, (n, c) in
             enumerate(zip(names, (0.2, 0.9, 0.5, 0.7)))]

    result = bt.replay(DAYS, {2: today}, panel, SHIPPED, NO_RSI, GROSS)

    assert sorted(t.symbol for t in result.trades) == ["BBB", "CCC", "DDD"]     # 3 a day
    assert result.skipped == {"daily_limit": 1}


def test_a_held_name_is_not_bought_again():
    panel = {"AAA": rising()}

    result = bt.replay(DAYS, {2: [cand("AAA")], 3: [cand("AAA")]}, panel, SHIPPED,
                       bt.Exits(rsi_exit=None, target=5.0), GROSS)

    assert len(result.trades) == 1 and result.skipped == {"held": 1}


def test_a_name_closed_at_a_loss_is_barred_for_the_wash_sale_window():
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(60)]
    falling = series([100.0, 100.0, 100.0] + [80.0] * 57, opens=[100.0] * 3 + [80.0] * 57,
                     atr=[2.0] * 60)

    result = bt.replay(days, {2: [cand("AAA")], 20: [cand("AAA")], 40: [cand("AAA")]},
                       {"AAA": falling}, SHIPPED, NO_RSI, GROSS)

    assert [t.entry_day for t in result.trades] == [2, 40]       # day 20 is 17 days after the loss
    assert result.skipped == {"reentry_block": 1}


def test_the_sector_cap_binds_before_the_slots_do():
    """Seven slots and 30% of equity per sector: the third Energy name gets
    what is left under the cap, not a full slot."""
    names = ["AAA", "BBB", "CCC"]
    panel = {n: rising() for n in names}

    result = bt.replay(DAYS, {2: [cand(n, conviction=0.9 - i / 10) for i, n in enumerate(names)]},
                       panel, SHIPPED, bt.Exits(rsi_exit=None, target=5.0), GROSS)

    costs = [t.cost for t in sorted(result.trades, key=lambda t: t.symbol)]
    assert sum(costs) <= 0.30 * 1400.0 + 1e-6
    assert costs[0] == pytest.approx(196.0) and costs[2] < 60.0


def test_one_per_sector_a_day_is_a_named_variant_not_the_default():
    names = ["AAA", "BBB"]
    panel = {n: rising() for n in names}
    variant = bt.Caps(n_target=lambda e: 5, position_cap_fraction=sizing.position_cap_fraction,
                      sector_cap_fraction=sizing.sector_cap_fraction, max_new_per_day=2,
                      one_per_sector_day=True)

    result = bt.replay(DAYS, {2: [cand(n) for n in names]}, panel, variant, NO_RSI, GROSS)

    assert len(result.trades) == 1 and result.skipped == {"sector_day": 1}
    assert SHIPPED.one_per_sector_day is False and SHIPPED.max_new_per_day == 3


def test_an_unfilled_entry_is_counted_and_uses_no_slot_or_cash():
    panel = {"AAA": rising()}
    never = bt.Costs(enabled=False, fill_probability=0.0)

    result = bt.replay(DAYS, {2: [cand("AAA")]}, panel, SHIPPED, NO_RSI, never)

    assert result.trades == [] and result.missed == [(2, "AAA")]
    assert result.equity[-1] == pytest.approx(1400.0)


def test_a_control_misses_the_same_slots_as_the_signal_it_stands_in_for():
    """The fill draw is keyed on the signal, so swapping in a stand-in cannot
    change which slots fill."""
    costs = bt.Costs(enabled=False, fill_probability=0.5)
    panel = {"AAA": rising(), "ZZZ": rising(start=50.0)}
    signals = {d: [cand("AAA")] for d in range(2, 12)}
    controls = {d: [cand("ZZZ", signal="AAA")] for d in range(2, 12)}

    dip = bt.replay(DAYS, signals, panel, SHIPPED, bt.Exits(rsi_exit=None, hold=1, target=5.0), costs)
    ctl = bt.replay(DAYS, controls, panel, SHIPPED, bt.Exits(rsi_exit=None, hold=1, target=5.0), costs)

    assert [d for d, _ in dip.missed] == [d for d, _ in ctl.missed]
    assert [t.entry_day for t in dip.trades] == [t.entry_day for t in ctl.trades]
    assert 0 < len(dip.missed) < 10


def test_exit_proceeds_are_usable_the_next_session_not_the_same_one():
    one_slot = bt.Caps(n_target=lambda e: 1, position_cap_fraction=lambda n: 1.0,
                       sector_cap_fraction=lambda n: 1.0)
    panel = {"AAA": rising(), "BBB": rising()}
    exits = bt.Exits(rsi_exit=None, hold=1, target=5.0)

    result = bt.replay(DAYS, {2: [cand("AAA")], 3: [cand("BBB")], 4: [cand("BBB")]},
                       panel, one_slot, exits, GROSS)

    # AAA is sold on day 3; BBB cannot be funded until day 4.
    assert [(t.symbol, t.entry_day) for t in result.trades] == [("AAA", 2), ("BBB", 4)]


def test_a_position_still_open_at_the_end_is_marked_not_closed():
    result = bt.replay(DAYS[:6], {2: [cand("AAA")]}, {"AAA": rising(6)}, SHIPPED,
                       bt.Exits(rsi_exit=None, target=5.0), GROSS)

    (trade,) = result.trades
    assert trade.net is None and trade.reason == bt.OPEN
    assert result.equity[-1] == pytest.approx(1400.0 - 196.0 + 196.0 * 105 / 102)
    assert bt.metrics(result, 1400.0)["trades"] == 0 and bt.metrics(result, 1400.0)["open"] == 1


# --- metrics -----------------------------------------------------------------


def test_max_drawdown_is_the_deepest_fall_from_a_prior_peak():
    assert bt.max_drawdown([100, 120, 90, 110, 80, 130]) == pytest.approx(80 / 120 - 1)
    assert bt.max_drawdown([100, 110, 120]) == 0.0


def test_sharpe_is_the_annualised_daily_ratio():
    equity = [101.0, 102.01, 103.0301]              # +1% a day, no variance
    assert bt.sharpe(equity, 100.0) is None
    # +1%, -1%, +1%, -1% (to rounding): mean near zero, so a Sharpe near zero.
    returns = [0.01, 100 / 101 - 1, 0.01, 100 / 101 - 1]
    mean = sum(returns) / 4
    sd = (sum((r - mean) ** 2 for r in returns) / 3) ** 0.5
    assert bt.sharpe([101.0, 100.0, 101.0, 100.0], 100.0) == pytest.approx(mean / sd * 252 ** 0.5)
    assert bt.sharpe([100.0], 100.0) is None


def test_metrics_report_what_the_program_asks_for():
    panel = {"AAA": rising(), "BBB": series([100.0, 100.0, 100.0] + [90.0] * 11,
                                            opens=[100.0] * 3 + [90.0] * 11, atr=[2.0] * 14)}
    result = bt.replay(DAYS, {2: [cand("AAA", "S1", 0.9), cand("BBB", "S2", 0.5)]}, panel,
                       SHIPPED, bt.Exits(rsi_exit=None, target=5.0), GROSS)

    m = bt.metrics(result, 1400.0)

    assert m["trades"] == 2 and m["hit_rate"] == 0.5
    assert m["payoff"] == pytest.approx((110 / 102 - 1) / 0.10)
    assert m["reasons"] == {bt.ATR_STOP: 1, bt.TIME_STOP: 1}
    assert m["total_return"] == pytest.approx(result.equity[-1] / 1400.0 - 1)
    assert m["max_drawdown"] <= 0.0 and m["turnover"] > 0 and 0 < m["exposure"] < 1
    assert m["mae"] <= 0.0 <= m["mfe"] and m["held"] == pytest.approx((8 + 1) / 2)


# --- membership by date ------------------------------------------------------

START, END = date(2021, 1, 1), date(2026, 10, 2)
CHANGES = [
    (date(2026, 9, 21), "BE", "TAP"),
    (date(2024, 6, 24), "KKR", "RHI"),
    (date(2023, 3, 15), "TAP", "SIVB"),
    (date(2019, 1, 2), "OLD", "GONE"),        # before the window
]


def test_membership_is_rebuilt_by_walking_the_changes_back():
    m = bt.Membership(["BE", "KKR", "AAPL"], CHANGES, START, END)

    # A current member with no change in the window: always in.
    assert m.is_member("AAPL", date(2021, 6, 1)) and m.is_member("AAPL", END)
    # Added mid-window: not before its date, in from it.
    assert not m.is_member("KKR", date(2024, 6, 21)) and m.is_member("KKR", date(2024, 6, 24))
    # Removed mid-window: in until its date, out from it.
    assert m.is_member("RHI", date(2024, 6, 21)) and not m.is_member("RHI", date(2024, 6, 24))
    # Added and later removed: one interval with both ends.
    assert not m.is_member("TAP", date(2023, 3, 14))
    assert m.is_member("TAP", date(2023, 3, 15)) and m.is_member("TAP", date(2026, 9, 18))
    assert not m.is_member("TAP", date(2026, 9, 21))
    # Removed, never seen added inside the window: a member from the start.
    assert m.is_member("SIVB", START) and not m.is_member("SIVB", date(2023, 3, 15))


def test_changes_outside_the_window_do_not_invent_members():
    m = bt.Membership(["BE", "KKR", "AAPL"], CHANGES, START, END)

    assert not m.is_member("GONE", date(2021, 6, 1))
    assert "GONE" not in m.ever() and "OLD" not in m.ever()
    assert not m.is_member("NEVER", date(2022, 1, 1))


def test_member_days_count_only_days_inside_the_interval():
    m = bt.Membership(["BE", "KKR", "AAPL"], CHANGES, START, END)
    days = [date(2024, 6, 20), date(2024, 6, 21), date(2024, 6, 24), date(2024, 6, 25)]

    assert (m.member_days("RHI", days), m.member_days("KKR", days), m.member_days("AAPL", days)) == (2, 2, 4)


# --- the survivorship bound --------------------------------------------------


def test_the_bound_replaces_a_share_of_dip_events_with_the_terminal_loss():
    # Edge +0.20%, control mean +0.60%, 2% of events unobserved at -30%.
    bound = bt.survivorship_bound(0.0020, 0.0060, 0.02)

    assert bound == pytest.approx(0.98 * 0.0020 + 0.02 * (-0.30 - 0.0060))
    assert bound < 0.0                                # two percent is enough to erase it
    assert bt.survivorship_bound(0.0020, 0.0060, 0.0) == pytest.approx(0.0020)


def test_the_break_evens_are_the_two_ways_of_reading_the_same_line():
    edge, control = 0.0020, 0.0060

    share = bt.breakeven_share(edge, control)
    loss = bt.breakeven_loss(edge, control, 0.02)

    assert bt.survivorship_bound(edge, control, share) == pytest.approx(0.0, abs=1e-12)
    assert bt.survivorship_bound(edge, control, 0.02, loss) == pytest.approx(0.0, abs=1e-12)
    assert share == pytest.approx(0.0020 / 0.3080)
    # No edge to defend, or nobody unobserved: no break-even to report.
    assert bt.breakeven_share(-0.001, control) is None
    assert bt.breakeven_loss(edge, control, 0.0) is None


# --- S4: whole shares ---------------------------------------------------------


def flat(price, n=14, **kw):
    return series([price] * n, atr=[50.0] * n, **kw)


def test_whole_shares_buys_what_the_slot_affords_rounded_down():
    result = bt.replay(DAYS, {2: [cand("AAA")]}, {"AAA": flat(60.0)}, SHIPPED, NO_RSI, GROSS,
                       whole_shares=True)

    (trade,) = result.trades
    assert trade.shares == 3.0          # a 196 slot at 60
    assert trade.cost == pytest.approx(180.0)


def test_most_of_a_share_is_rounded_up_to_one_inside_the_position_cap():
    # 196 / 230 is 0.85 of a share, and 230 is inside the 240 position cap.
    near = bt.replay(DAYS, {2: [cand("AAA")]}, {"AAA": flat(230.0)}, SHIPPED, NO_RSI, GROSS,
                     whole_shares=True)
    # 196 / 300 is 0.65 of a share, but one share is over the cap.
    over = bt.replay(DAYS, {2: [cand("AAA")]}, {"AAA": flat(300.0)}, SHIPPED, NO_RSI, GROSS,
                     whole_shares=True)
    # 196 / 500 is under half a share: never promoted.
    far = bt.replay(DAYS, {2: [cand("AAA")]}, {"AAA": flat(500.0)}, SHIPPED, NO_RSI, GROSS,
                    whole_shares=True)

    assert [(t.shares, t.cost) for t in near.trades] == [(1.0, pytest.approx(230.0))]
    assert over.trades == [] and over.skipped == {"whole_share": 1}
    assert far.trades == [] and far.skipped == {"whole_share": 1}


def test_a_refused_whole_share_is_not_a_missed_fill():
    never = bt.Costs(enabled=False, fill_probability=1e-12)

    result = bt.replay(DAYS, {2: [cand("AAA")]}, {"AAA": flat(500.0)}, SHIPPED, NO_RSI, never,
                       whole_shares=True)

    assert result.missed == [] and result.skipped == {"whole_share": 1}


def test_fractional_is_the_default_and_buys_the_whole_slot():
    result = bt.replay(DAYS, {2: [cand("AAA")]}, {"AAA": flat(500.0)}, SHIPPED, NO_RSI, GROSS)

    assert result.trades[0].cost == pytest.approx(196.0)


# --- S4: cash that earns interest ----------------------------------------------


def test_idle_cash_earns_the_daily_rate_and_no_rate_is_no_interest():
    rate = [0.001] * 14
    rate[3] = None

    result = bt.replay(DAYS, {}, {}, SHIPPED, NO_RSI, GROSS, cash_rate=rate)

    # Thirteen nights, one of them with no rate.
    assert result.equity[-1] == pytest.approx(1400.0 * 1.001 ** 12)
    assert result.interest == pytest.approx(1400.0 * 1.001 ** 12 - 1400.0)
    assert bt.replay(DAYS, {}, {}, SHIPPED, NO_RSI, GROSS).interest == 0.0


# --- S4: the beta arms ----------------------------------------------------------


def test_hold_buys_a_fraction_at_the_first_open_and_never_adds_the_cash():
    spy = series([110.0] * 13 + [220.0], opens=[100.0] + [110.0] * 13)

    result = bt.hold(DAYS, {"SPY": spy}, {"SPY": 1.0}, GROSS, fraction=0.5)

    # 700 buys 7 shares at the 100 open; the other 700 stays cash throughout.
    assert result.invested[0] == pytest.approx(770.0)
    assert result.equity[0] == pytest.approx(1470.0)
    assert result.equity[-1] == pytest.approx(700.0 + 7 * 220.0)
    assert result.extra_bought == pytest.approx(700.0)
    assert result.trades == [] and result.missed == []


def test_hold_starts_on_the_first_day_of_its_window():
    spy = series([100.0 + i for i in range(14)])

    result = bt.hold(DAYS, {"SPY": spy}, {"SPY": 1.0}, GROSS, first_day=4, last_day=9)

    assert len(result.equity) == 6
    assert result.equity[-1] == pytest.approx(1400.0 * 109 / 104)


def test_hold_pays_the_spread_on_the_way_in():
    spy = series([100.0] * 14, adv=[600e6] * 14)

    result = bt.hold(DAYS, {"SPY": spy}, {"SPY": 1.0}, bt.Costs())

    assert result.equity[-1] == pytest.approx(1400.0 / (1 + 0.000284))


QUARTER = [date(2024, 3, 27), date(2024, 3, 28), date(2024, 4, 1), date(2024, 4, 2)]


def test_the_basket_is_rebalanced_at_the_first_session_of_a_quarter_only():
    up = series([100.0, 200.0, 200.0, 400.0])
    same = series([100.0, 100.0, 100.0, 100.0])
    panel = {"UP": up, "SAME": same}

    kept = bt.hold(QUARTER, panel, {"UP": 1.0, "SAME": 1.0}, GROSS)
    rebalanced = bt.hold(QUARTER, panel, {"UP": 1.0, "SAME": 1.0}, GROSS, rebalance=True)

    # 700 each; UP doubles to 1400 in March and again on April 2nd.
    assert kept.equity == pytest.approx([1400.0, 2100.0, 2100.0, 3500.0])
    assert kept.extra_bought == pytest.approx(1400.0)
    # One rebalance, on April 1st, moving 350 across: 1050 each going into
    # the 2nd. Not one on March 28th, and not another on April 2nd.
    assert rebalanced.equity == pytest.approx([1400.0, 2100.0, 2100.0, 3150.0])
    assert rebalanced.extra_bought == pytest.approx(1400.0 + 350.0)


def test_a_rebalance_trades_only_the_differences_and_pays_for_them():
    up = series([100.0, 200.0, 200.0, 200.0], adv=[600e6] * 4)
    same = series([100.0, 100.0, 100.0, 100.0], adv=[600e6] * 4)
    half = 0.000284

    result = bt.hold(QUARTER, {"UP": up, "SAME": same}, {"UP": 1.0, "SAME": 1.0}, bt.Costs(),
                     rebalance=True)

    start = 2100.0 / (1 + half)                 # both legs bought across the spread
    moved = start / 6.0                         # a sixth of the basket changes hands
    lost = moved * half + moved * (1 - half) * (1 - 1 / (1 + half))
    assert result.equity[-1] == pytest.approx(start - lost)


def test_hold_weights_are_normalised_and_a_missing_bar_is_carried():
    a = series([100.0, None, 120.0, 120.0], opens=[100.0, None, 120.0, 120.0])
    b = series([50.0, 50.0, 50.0, 50.0])

    result = bt.hold(QUARTER, {"A": a, "B": b}, {"A": 3.0, "B": 1.0}, GROSS)

    assert result.invested[0] == pytest.approx(1400.0)
    assert result.equity[1] == pytest.approx(1400.0)          # A carried at 100
    assert result.equity[-1] == pytest.approx(1050.0 * 1.2 + 350.0)


def test_hold_cash_earns_interest_when_given_a_rate():
    spy = series([100.0] * 14)

    result = bt.hold(DAYS, {"SPY": spy}, {"SPY": 1.0}, GROSS, fraction=0.5, cash_rate=[0.001] * 14)

    assert result.equity[-1] == pytest.approx(700.0 + 700.0 * 1.001 ** 13)


# --- S4: the core sleeve ---------------------------------------------------------

ALL_IN = bt.Sleeve("SPY", cap=1.0, buffer_slots=1.0, target=5.0)


def test_cash_above_the_buffer_is_swept_into_the_sleeve_up_to_its_cap():
    panel = {"SPY": flat(100.0)}

    half = bt.replay(DAYS, {}, panel, SHIPPED, NO_RSI, GROSS, sleeve=bt.Sleeve("SPY", target=5.0))
    full = bt.replay(DAYS, {}, panel, SHIPPED, NO_RSI, GROSS, sleeve=ALL_IN)

    # Reserve 28 plus one 200 slot stays cash. The 50% cap binds first.
    assert half.sleeve_value[0] == pytest.approx(700.0)
    assert full.sleeve_value[0] == pytest.approx(1172.0)
    assert half.sleeve["buys"] == 1 and half.sleeve["sells"] == 0
    assert half.equity == pytest.approx([1400.0] * 14)
    assert half.extra_bought == pytest.approx(700.0)


def test_a_satellite_is_sized_against_the_sleeve_but_paid_from_settled_cash():
    panel = {"SPY": flat(100.0), "AAA": rising(), "BBB": rising()}
    today = [cand("AAA", "S1", 0.9), cand("BBB", "S2", 0.5)]

    result = bt.replay(DAYS, {2: today}, panel, SHIPPED, bt.Exits(rsi_exit=None, target=5.0),
                       GROSS, sleeve=ALL_IN)

    # The full 196 slot, not a seventh of the 200 that happens to be in cash.
    assert [(t.symbol, t.cost) for t in result.trades] == [("AAA", pytest.approx(196.0))]
    # The second name's slot is not in settled cash: refused, and counted.
    assert result.skipped == {"no_settled_cash": 1}


def test_the_sleeve_is_sold_to_restore_the_buffer_and_the_cash_arrives_next_session():
    panel = {"SPY": flat(100.0), "AAA": rising(), "BBB": rising()}
    exits = bt.Exits(rsi_exit=None, target=5.0)

    result = bt.replay(DAYS, {2: [cand("AAA", "S1")], 3: [cand("BBB", "S2")]}, panel, SHIPPED,
                       exits, GROSS, sleeve=ALL_IN)

    # Day 2 leaves 32 of cash against a 228 buffer: 1.96 shares are sold.
    assert result.sleeve_value[2] == pytest.approx(1172.0 - 196.0)
    assert result.sleeve["sells"] >= 1 and result.sleeve["loss_sells"] == 0
    # ... which is what pays for the next day's entry.
    assert sorted(t.symbol for t in result.trades) == ["AAA", "BBB"]
    assert result.skipped == {}


def test_the_sleeves_one_resting_order_is_a_target_above_its_buy_price():
    closes = [100.0] * 4 + [110.0] * 10
    highs = [100.0] * 4 + [125.0] + [110.0] * 9
    panel = {"SPY": series(closes, highs=highs)}

    result = bt.replay(DAYS, {}, panel, SHIPPED, NO_RSI, GROSS,
                       sleeve=bt.Sleeve("SPY", cap=0.5, target=0.20))

    # Seven shares at 100; the day-4 high crosses 120 and the limit fills there.
    assert result.sleeve["target_fills"] == 1
    assert result.sleeve_value[4] == 0.0
    assert result.equity[4] == pytest.approx(700.0 + 7 * 120.0)
    # Idle again, so it is swept back in at the next open.
    assert result.sleeve["buys"] == 2 and result.sleeve_value[5] > 0.0
    assert result.sleeve["sells"] == 0 and result.sleeve["loss_sells"] == 0


def test_a_gap_over_the_sleeve_target_fills_at_the_open():
    opens = [100.0] * 4 + [130.0] + [110.0] * 9
    panel = {"SPY": series([100.0] * 4 + [110.0] * 10, opens=opens, highs=[130.0 if i == 4 else 110.0 for i in range(14)])}

    result = bt.replay(DAYS, {}, panel, SHIPPED, NO_RSI, GROSS,
                       sleeve=bt.Sleeve("SPY", cap=0.5, target=0.20, honour_reentry=False))

    assert result.sleeve["target_fills"] == 1
    assert result.equity[4] == pytest.approx(700.0 + 7 * 130.0 - 472.0 + 472.0 / 130.0 * 110.0)
    # The sale's proceeds settle tomorrow, but the other half of the account
    # is idle cash today, and the day's own sweep puts it back to work.
    assert result.sleeve_value[4] == pytest.approx(472.0 / 130.0 * 110.0)


def test_a_target_fill_pays_no_spread_and_a_funding_sale_does():
    costs = bt.Costs()
    closes = [100.0] * 4 + [110.0] * 10
    highs = [100.0] * 4 + [125.0] + [110.0] * 9
    panel = {"SPY": series(closes, highs=highs, adv=[600e6] * 14)}

    result = bt.replay(DAYS, {}, panel, SHIPPED, NO_RSI, costs,
                       sleeve=bt.Sleeve("SPY", cap=0.5, target=0.20))

    shares = 700.0 / (100.0 * (1 + 0.000284))
    assert result.equity[4] == pytest.approx(700.0 + shares * 120.0)


def falling_sleeve():
    return {"SPY": series([100.0, 100.0] + [90.0] * 12, atr=[50.0] * 14), "AAA": rising()}


def test_a_sleeve_sale_at_a_loss_starts_the_no_rebuy_window():
    exits = bt.Exits(rsi_exit=None, target=5.0)
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(14)]

    honoured = bt.replay(days, {2: [cand("AAA")]}, falling_sleeve(), SHIPPED, exits, GROSS,
                         sleeve=ALL_IN)
    exempt = bt.replay(days, {2: [cand("AAA")]}, falling_sleeve(), SHIPPED, exits, GROSS,
                       sleeve=bt.Sleeve("SPY", cap=1.0, buffer_slots=1.0, target=5.0,
                                        honour_reentry=False))

    # Sold at 90 against a 100 buy to fund AAA. When AAA's proceeds come back
    # on day 11 the barred sleeve leaves them idle; the exempt one takes them.
    assert honoured.sleeve["loss_sells"] >= 1 and honoured.sleeve["blocked_days"] > 0
    assert honoured.sleeve["buys"] == 1
    assert exempt.sleeve["blocked_days"] == 0 and exempt.sleeve["buys"] >= 2
    assert exempt.sleeve_value[-1] > honoured.sleeve_value[-1]


def test_the_sleeve_has_no_stop_however_far_it_falls():
    panel = {"SPY": series([100.0] * 3 + [50.0] * 11)}

    result = bt.replay(DAYS, {}, panel, SHIPPED, NO_RSI, GROSS, sleeve=ALL_IN)

    assert result.sleeve["sells"] == 0 and result.sleeve["target_fills"] == 0
    assert result.sleeve["loss_sells"] == 0
    assert result.sleeve_value[-1] > 0.0
    # The account's 20% halt is reported as reached, not simulated.
    assert result.sleeve["halt_reached"] is True
    assert bt.replay(DAYS, {}, {"SPY": flat(100.0)}, SHIPPED, NO_RSI, GROSS,
                     sleeve=ALL_IN).sleeve["halt_reached"] is False


def test_a_whole_share_sleeve_buys_and_sells_whole_shares():
    panel = {"SPY": flat(90.0), "AAA": rising()}
    sleeve = bt.Sleeve("SPY", cap=1.0, buffer_slots=1.0, target=5.0, whole_shares=True)

    result = bt.replay(DAYS, {2: [cand("AAA")]}, panel, SHIPPED, bt.Exits(rsi_exit=None, target=5.0),
                       GROSS, sleeve=sleeve)

    assert result.sleeve_value[0] == pytest.approx(13 * 90.0)        # 1172 buys 13
    # Day 2: cash 230 - 196 = 34 against 228; 194 short is three shares, not 2.16.
    assert result.sleeve_value[2] == pytest.approx(10 * 90.0)


def test_a_day_the_sleeve_etf_has_no_bar_nothing_is_swept_and_it_is_carried():
    closes = [100.0, None, 100.0] + [100.0] * 11
    panel = {"SPY": series(closes, opens=closes, highs=closes, lows=closes)}

    result = bt.replay(DAYS[1:], {}, {"SPY": series(closes[1:], opens=closes[1:], highs=closes[1:], lows=closes[1:])},
                       SHIPPED, NO_RSI, GROSS, sleeve=ALL_IN)
    carried = bt.replay(DAYS, {}, panel, SHIPPED, NO_RSI, GROSS, sleeve=ALL_IN)

    assert result.sleeve_value[0] == 0.0 and result.sleeve_value[1] == pytest.approx(1172.0)
    assert carried.sleeve_value[1] == pytest.approx(1172.0)


def test_metrics_count_the_sleeve_in_turnover_exposure_and_idle_cash():
    result = bt.replay(DAYS, {}, {"SPY": flat(100.0)}, SHIPPED, NO_RSI, GROSS,
                       sleeve=bt.Sleeve("SPY", target=5.0))

    m = bt.metrics(result, 1400.0)

    assert m["exposure"] == pytest.approx(0.5)
    assert m["cash_idle"] == pytest.approx(0.5)
    assert m["sleeve_share"] == pytest.approx(0.5)
    assert m["turnover"] == pytest.approx(700.0 / 1400.0 / (14 / 252.0))
    assert m["sleeve"]["buys"] == 1
    plain = bt.metrics(bt.replay(DAYS, {}, {}, SHIPPED, NO_RSI, GROSS), 1400.0)
    assert plain["cash_idle"] == pytest.approx(1.0) and plain["sleeve_share"] is None


def test_without_a_sleeve_the_replay_is_what_s1_ran():
    panel = {"AAA": rising(), "SPY": flat(100.0)}
    exits = bt.Exits(rsi_exit=None, target=5.0)

    before = bt.replay(DAYS, {2: [cand("AAA")]}, panel, SHIPPED, exits, bt.Costs())
    after = bt.replay(DAYS, {2: [cand("AAA")]}, panel, SHIPPED, exits, bt.Costs(),
                      whole_shares=False, cash_rate=None, sleeve=None)

    assert before.equity == after.equity and before.skipped == after.skipped
    assert before.sleeve == {} and before.sleeve_value == [] and before.extra_bought == 0.0


# --- S4: the decision rule -------------------------------------------------------


def arm(total, drawdown):
    return {"total_return": total, "max_drawdown": drawdown}


POLICY = {
    "full": {"rsi": arm(0.28, -0.20), "norsi": arm(0.25, -0.17)},
    "oos": {"rsi": arm(0.12, -0.14), "norsi": arm(0.11, -0.15)},
}


def rule(**arms):
    beta = {w: {name: values[k] for name, values in arms.items()} for k, w in enumerate(("full", "oos"))}
    return bt.beta_rule(POLICY, beta, ("full", "oos"))


def test_the_bar_is_the_better_return_and_the_shallower_drawdown_of_the_two_versions():
    result = rule(**{"B|50%": (arm(0.30, -0.16), arm(0.20, -0.10))})

    leg = result["arms"]["B|50%"]["windows"]["full"]
    # 28% is the RSI version's return; -17% is the no-RSI version's drawdown.
    assert (leg["policy_return"], leg["policy_drawdown"]) == (0.28, -0.17)
    assert result["fires"] and result["satisfied_by"] == ["B|50%"]


def test_an_arm_must_clear_both_legs_in_both_windows():
    result = rule(**{
        "more_return_deeper_drawdown|100%": (arm(1.20, -0.25), arm(0.68, -0.19)),
        "one_window_only|50%": (arm(0.30, -0.16), arm(0.10, -0.10)),
        "shallower_but_less_return|matched": (arm(0.20, -0.05), arm(0.20, -0.05)),
    })

    assert not result["fires"] and result["satisfied_by"] == []
    legs = result["arms"]["more_return_deeper_drawdown|100%"]["windows"]
    assert legs["full"]["return_ok"] and not legs["full"]["drawdown_ok"]
    assert not result["arms"]["one_window_only|50%"]["windows"]["oos"]["return_ok"]


def test_matching_exactly_is_enough_on_both_legs():
    result = rule(**{"B|50%": (arm(0.28, -0.17), arm(0.12, -0.14))})

    assert result["fires"]


def test_the_literal_reading_counts_only_the_fully_invested_arms():
    scaled = rule(**{"B|100%": (arm(1.20, -0.25), arm(0.68, -0.19)),
                     "B|50%": (arm(0.60, -0.13), arm(0.34, -0.10)),
                     "B|matched": (arm(0.40, -0.09), arm(0.24, -0.07))})
    whole = rule(**{"B|100%": (arm(1.20, -0.15), arm(0.68, -0.12))})

    assert scaled["fires"] and not scaled["literal_fires"]
    assert scaled["literal_satisfied_by"] == []
    assert whole["literal_fires"] and whole["literal_satisfied_by"] == ["B|100%"]


# --- S4: the live record -----------------------------------------------------------


def bench(day, equity):
    return {"trading_day": day, "equity": equity}


def test_a_deposit_is_not_a_return():
    rows = [bench("2026-08-07", 500.0), bench("2026-08-10", 747.0), bench("2026-08-11", 747.0)]

    plain = bt.time_weighted(rows, [])
    funded = bt.time_weighted(rows, [{"date": "2026-08-10", "amount": 247.0}])

    assert plain[-1] == pytest.approx(747.0 / 500.0)       # scored as a 49% gain
    assert funded == pytest.approx([1.0, 1.0, 1.0])        # which it was not


def test_a_flow_lands_in_the_interval_that_contains_its_day():
    # No row on the 12th: the deposit belongs to the 11th-to-13th interval.
    rows = [bench("2026-08-10", 1000.0), bench("2026-08-11", 1010.0), bench("2026-08-13", 1160.0)]

    growth = bt.time_weighted(rows, [{"date": "2026-08-12", "amount": 147.0}])

    assert growth[1] == pytest.approx(1.01)
    assert growth[2] == pytest.approx(1.01 * (1160.0 - 147.0) / 1010.0)


def test_a_flow_on_or_before_the_first_row_is_already_in_its_equity():
    rows = [bench("2026-08-07", 500.0), bench("2026-08-10", 510.0)]

    growth = bt.time_weighted(rows, [{"date": "2026-08-05", "amount": 247.14},
                                     {"date": "2026-08-07", "amount": 10.0}])

    assert growth[-1] == pytest.approx(1.02)


def test_two_flows_in_one_interval_add_and_a_withdrawal_is_negative():
    rows = [bench("2026-08-13", 900.0), bench("2026-08-14", 1000.0)]

    growth = bt.time_weighted(rows, [{"date": "2026-08-14", "amount": 147.0},
                                     {"date": "2026-08-14", "amount": -47.0}])

    assert growth[-1] == pytest.approx(1.0)


def test_calendar_years_chain_from_the_starting_equity():
    days = [date(2021, 12, 30), date(2021, 12, 31), date(2022, 1, 3), date(2022, 12, 30), date(2023, 1, 3)]

    years = bt.yearly([1500.0, 1540.0, 1500.0, 1386.0, 1400.0], days, 1400.0)

    assert years == {2021: pytest.approx(0.10), 2022: pytest.approx(-0.10),
                     2023: pytest.approx(1400.0 / 1386.0 - 1)}


# --- S3: the posture size multiplier ------------------------------------------


def test_a_multiplier_of_one_is_the_same_replay():
    panel = {"AAA": rising()}
    exits = bt.Exits(rsi_exit=None, target=5.0)
    plain = bt.replay(DAYS, {2: [cand("AAA")]}, panel, SHIPPED, exits, GROSS)
    explicit = bt.replay(DAYS, {2: [bt.Candidate("AAA", "AAA", "Energy", 0.5, 1.0)]},
                         panel, SHIPPED, exits, GROSS)

    assert explicit.equity == plain.equity
    assert explicit.trades[0].cost == plain.trades[0].cost


def test_a_half_multiplier_halves_the_slot_and_nothing_else():
    panel = {"AAA": rising()}
    exits = bt.Exits(rsi_exit=None, target=5.0)

    result = bt.replay(DAYS, {2: [bt.Candidate("AAA", "AAA", "Energy", 0.5, 0.5)]},
                       panel, SHIPPED, exits, GROSS)

    (trade,) = result.trades
    assert trade.cost == pytest.approx(98.0)                 # half of the 196 slot
    assert (trade.exit_day, trade.reason) == (10, bt.TIME_STOP)
    assert trade.net == pytest.approx(110 / 102 - 1)


def test_a_multiplier_can_never_grow_a_slot():
    panel = {"AAA": rising()}
    exits = bt.Exits(rsi_exit=None, target=5.0)

    result = bt.replay(DAYS, {2: [bt.Candidate("AAA", "AAA", "Energy", 0.5, 3.0)]},
                       panel, SHIPPED, exits, GROSS)

    assert result.trades[0].cost == pytest.approx(196.0)


# ---------------------------------------------------------------------------
# S5: the event plays' entry rules on daily bars, and the
# monthly rebalance replay.
# ---------------------------------------------------------------------------

from src.plays import catalyst as live_catalyst  # noqa: E402
from src.plays import pead as live_pead  # noqa: E402
from src.plays import surge as live_surge  # noqa: E402

MARCH_WEEK = [date(2026, 3, 2) + timedelta(days=i) for i in range(5)]      # Monday to Friday


def test_the_offline_thresholds_are_the_shipped_plays_own():
    assert bt.PEAD_MIN_REACTION == live_pead.PEAD_MIN_REACTION_PCT
    assert bt.PEAD_SURPRISE_FULL_SCALE == live_pead.PEAD_SURPRISE_FULL_SCALE_PCT
    assert bt.PEAD_REACTION_FULL_SCALE == live_pead.PEAD_REACTION_FULL_SCALE_PCT
    assert bt.CATALYST_WINDOW == live_catalyst.CATALYST_WINDOW_SESSIONS
    assert bt.CATALYST_MIN_MOVE == live_catalyst.CATALYST_MIN_REACTION_PCT
    assert bt.CATALYST_MIN_VOLUME == live_catalyst.CATALYST_MIN_VOLUME_MULTIPLE
    assert bt.CATALYST_FULL_SCALE == live_catalyst.CATALYST_REACTION_FULL_SCALE_PCT
    assert bt.SURGE_RSI == (live_surge.SURGE_RSI_MIN, live_surge.SURGE_RSI_MAX)
    assert bt.SURGE_MOMENTUM_MIN == live_surge.SURGE_MOMENTUM_MIN
    assert bt.SURGE_FULL_SCALE == live_surge.SURGE_CONVICTION_FULL_SCALE


def test_a_report_before_the_open_reacts_that_session_and_any_other_the_next():
    assert bt.reaction_session(MARCH_WEEK, MARCH_WEEK[1], 6.0) == 1
    assert bt.reaction_session(MARCH_WEEK, MARCH_WEEK[1], 16.0) == 2
    assert bt.reaction_session(MARCH_WEEK, MARCH_WEEK[1], 11.0) == 2       # during the day: the pm rule
    assert bt.reaction_session(MARCH_WEEK, MARCH_WEEK[1], None) == 2       # unknown: the pm rule


def test_a_report_on_a_closed_day_reacts_the_first_session_after_it():
    monday_after = [*MARCH_WEEK, date(2026, 3, 9)]
    saturday = date(2026, 3, 7)
    assert bt.reaction_session(monday_after, saturday, 6.0) == 5
    assert bt.reaction_session(monday_after, saturday, 16.0) == 5


def test_a_reaction_session_past_the_data_is_none():
    assert bt.reaction_session(MARCH_WEEK, MARCH_WEEK[4], 16.0) is None
    assert bt.reaction_session(MARCH_WEEK, date(2026, 4, 1), 6.0) is None


def test_an_8k_accepted_after_the_close_is_usable_the_next_session():
    assert bt.filing_session(MARCH_WEEK, MARCH_WEEK[1], 9.0) == 1
    assert bt.filing_session(MARCH_WEEK, MARCH_WEEK[1], 15.99) == 1
    assert bt.filing_session(MARCH_WEEK, MARCH_WEEK[1], 16.0) == 2
    assert bt.filing_session(MARCH_WEEK, MARCH_WEEK[1], None) == 1
    assert bt.filing_session(MARCH_WEEK, MARCH_WEEK[4], 17.0) is None


def test_pead_needs_a_beat_a_two_percent_reaction_and_the_fifty_day():
    closes, trend = [100.0, 103.0], [100.0, 100.0]
    assert bt.pead_signal(closes, trend, 1, 5.0) == pytest.approx(0.5)        # surprise 5 of 10
    assert bt.pead_signal(closes, trend, 1, 1.0) == pytest.approx(0.3)        # reaction 3% of 10%
    assert bt.pead_signal(closes, trend, 1, 0.0) is None                      # not a beat
    assert bt.pead_signal(closes, trend, 1, -4.0) is None
    assert bt.pead_signal(closes, trend, 1, None) is None                     # nothing inferred
    assert bt.pead_signal([100.0, 101.9], trend, 1, 5.0) is None              # under +2%
    assert bt.pead_signal(closes, [100.0, 103.0], 1, 5.0) is None             # not above the 50-day
    assert bt.pead_signal(closes, [100.0, None], 1, 5.0) is None


def test_pead_without_the_surprise_clause_reads_the_reaction_alone():
    assert bt.pead_signal([100.0, 104.0], [90.0, 90.0], 1, None, need_surprise=False) == pytest.approx(0.4)
    assert bt.pead_signal([100.0, 104.0], [90.0, 90.0], 1, -9.0, need_surprise=False) == pytest.approx(0.4)


def test_the_timing_agnostic_reaction_spans_two_sessions():
    closes, trend = [100.0, 99.0, 103.0], [90.0] * 3
    assert bt.pead_signal(closes, trend, 2, 20.0, span=2) == pytest.approx(1.0)
    assert bt.pead_signal(closes, trend, 2, 1.0, span=2) == pytest.approx(0.3)   # 103 over 100
    assert bt.pead_signal(closes, trend, 1, 1.0, span=2) is None                 # no bar two back


def test_catalyst_needs_an_8k_in_the_window_a_move_and_volume():
    ret = [0.0, 0.0, 0.0, 0.05, 0.05, 0.05, 0.05]
    vol = [1.0, 1.0, 1.0, 2.0, 2.0, 2.0, 1.4]
    usable = [False, False, False, True, False, False, False]
    assert bt.catalyst_signal(ret, vol, usable, 3) == pytest.approx(0.5)
    assert bt.catalyst_signal(ret, vol, usable, 5) == pytest.approx(0.5)      # two sessions back
    assert bt.catalyst_signal(ret, vol, usable, 6) is None                    # three back, and thin
    assert bt.catalyst_signal(ret, vol, [False] * 7, 3) is None               # a move with no event
    assert bt.catalyst_signal([0.029] * 7, vol, usable, 3) is None
    assert bt.catalyst_signal(ret, [1.49] * 7, usable, 3) is None
    assert bt.catalyst_signal(ret, [None] * 7, usable, 3) is None             # missing is not a pass


def test_catalyst_never_reads_a_filing_that_comes_later():
    ret, vol = [0.05] * 4, [2.0] * 4
    assert bt.catalyst_signal(ret, vol, [False, False, False, True], 2) is None


def surge_series(close=110.0, sma20=105.0, sma200=90.0, rsi=60.0, until=None):
    return series([close, close], rsi=[rsi, rsi], sma20=[sma20, sma20], sma200=[sma200, sma200],
                  until=until)


def test_surge_reads_the_prior_close_and_scales_on_the_fifty_day():
    assert bt.surge_signal(surge_series(), [100.0, 100.0], 1) == pytest.approx(0.10 / 0.15)
    assert bt.surge_signal(surge_series(close=130.0, sma20=120.0), [100.0, 100.0], 1) == pytest.approx(1.0)
    assert bt.surge_signal(surge_series(), [100.0, 100.0], 0) is None          # no prior close


def test_surge_refuses_anything_short_of_the_shipped_gate():
    sma50 = [100.0, 100.0]
    assert bt.surge_signal(surge_series(sma20=99.0), sma50, 1) is None         # not stacked
    assert bt.surge_signal(surge_series(sma200=101.0), sma50, 1) is None
    assert bt.surge_signal(surge_series(rsi=54.9), sma50, 1) is None
    assert bt.surge_signal(surge_series(rsi=80.1), sma50, 1) is None
    assert bt.surge_signal(surge_series(close=101.9, sma20=101.0), sma50, 1) is None   # under 2% above
    assert bt.surge_signal(surge_series(), [None, None], 1) is None
    assert bt.surge_signal(surge_series(until=[2, 1]), sma50, 1) is None       # a report inside clearance
    assert bt.surge_signal(surge_series(until=[2, 1]), sma50, 1, earnings_veto=False) is not None


def test_month_starts_are_each_months_first_session_in_range():
    days = [date(2026, 1, 29), date(2026, 1, 30), date(2026, 2, 2), date(2026, 2, 3),
            date(2026, 3, 2), date(2026, 3, 3)]
    assert bt.month_starts(days, date(2026, 1, 30), date(2026, 3, 2)) == [2, 4]
    assert bt.month_starts(days, date(2026, 1, 1), date(2026, 2, 28)) == [0, 2]


class Quotes:
    def __init__(self, o, h=None, c=None, adv=None):
        self.o = list(o)
        self.c = list(c) if c is not None else list(o)
        self.h = list(h) if h is not None else [None if a is None else max(a, b)
                                                for a, b in zip(self.o, self.c)]
        self.adv = adv


NO_COSTS = bt.Costs(enabled=False)
SIX = [date(2026, 3, 2) + timedelta(days=i) for i in range(6)]


def test_the_rebalance_splits_the_fraction_equally_and_marks_at_the_close():
    panel = {"A": Quotes([10.0] * 6, c=[11.0] * 6), "B": Quotes([20.0] * 6, c=[20.0] * 6)}
    result = bt.rebalance_replay(SIX, panel, {0: ["A", "B"]}, NO_COSTS, start_equity=1000.0, fraction=0.5)
    assert result.invested[0] == pytest.approx(250.0 * 1.1 + 250.0)
    assert result.equity[0] == pytest.approx(1025.0)
    assert result.extra_bought == pytest.approx(500.0)


def test_a_name_that_leaves_is_sold_at_the_open_and_its_cash_rebought():
    panel = {"A": Quotes([10.0, 10.0, 12.0, 12.0, 12.0, 12.0]), "B": Quotes([20.0] * 6)}
    result = bt.rebalance_replay(SIX, panel, {0: ["A"], 2: ["B"]}, NO_COSTS, start_equity=1000.0)
    assert result.equity[1] == pytest.approx(1000.0)
    assert result.equity[2] == pytest.approx(1200.0)            # A sold at 12, all of it into B
    assert result.invested[2] == pytest.approx(1200.0)
    assert result.extra_bought == pytest.approx(2200.0)


def test_a_stayer_is_trimmed_back_to_equal_weight():
    panel = {"A": Quotes([10.0, 20.0, 20.0, 20.0, 20.0, 20.0]), "B": Quotes([10.0] * 6)}
    result = bt.rebalance_replay(SIX, panel, {0: ["A", "B"], 1: ["A", "B"]}, NO_COSTS, start_equity=1000.0)
    assert result.equity[1] == pytest.approx(1500.0)
    assert result.extra_bought == pytest.approx(1000.0 + 250.0)  # B topped up from A's trim


def test_the_resting_target_sells_above_the_buy_price_and_the_cash_waits():
    highs = [10.0, 10.0, 12.6, 14.0, 14.0, 14.0]
    panel = {"A": Quotes([10.0] * 6, h=highs, c=[10.0, 10.0, 12.0, 14.0, 14.0, 14.0])}
    result = bt.rebalance_replay(SIX, panel, {0: ["A"]}, NO_COSTS, start_equity=1000.0, target=0.25)
    assert result.sleeve["target_fills"] == 1
    assert result.equity[2] == pytest.approx(1250.0)             # filled at 12.50, not the close
    assert result.invested[2:] == [0.0, 0.0, 0.0, 0.0]           # not rebought until scheduled
    assert result.equity[-1] == pytest.approx(1250.0)


def test_a_gap_over_the_rebalance_target_fills_at_the_open():
    panel = {"A": Quotes([10.0, 13.0, 13.0, 13.0, 13.0, 13.0])}
    result = bt.rebalance_replay(SIX, panel, {0: ["A"]}, NO_COSTS, start_equity=1000.0, target=0.25)
    assert result.equity[1] == pytest.approx(1300.0)


def test_the_rebalance_replay_has_no_stop_however_far_a_name_falls():
    panel = {"A": Quotes([10.0, 5.0, 2.0, 1.0, 1.0, 1.0])}
    result = bt.rebalance_replay(SIX, panel, {0: ["A"]}, NO_COSTS, start_equity=1000.0, target=0.25)
    assert result.invested[-1] == pytest.approx(100.0)           # still held, every share
    assert result.sleeve["target_fills"] == 0
    assert result.sleeve["halt_reached"] is True


def test_a_name_with_no_bar_is_carried_at_its_last_price_and_not_sold():
    panel = {"A": Quotes([10.0, 10.0, None, None, 10.0, 10.0]), "B": Quotes([10.0] * 6)}
    result = bt.rebalance_replay(SIX, panel, {0: ["A"], 2: ["B"]}, NO_COSTS, start_equity=1000.0)
    assert result.invested[2] == pytest.approx(1000.0)           # A could not be sold: no cash for B
    assert result.equity[3] == pytest.approx(1000.0)


def test_rebalance_costs_are_paid_on_every_dollar_traded():
    panel = {"A": Quotes([10.0] * 6, adv=[1e9] * 6)}
    free = bt.rebalance_replay(SIX, panel, {0: ["A"]}, NO_COSTS, start_equity=1000.0)
    paid = bt.rebalance_replay(SIX, panel, {0: ["A"]}, bt.Costs(), start_equity=1000.0)
    assert paid.equity[0] < free.equity[0]
    assert paid.equity[0] == pytest.approx(1000.0 / (1.0 + bt.Costs().entry(1000.0, 1e9)))


def test_the_rebalance_replay_never_spends_more_than_its_cash():
    panel = {"A": Quotes([10.0] * 6, adv=[1e9] * 6), "B": Quotes([10.0] * 6, adv=[1e9] * 6)}
    result = bt.rebalance_replay(SIX, panel, {0: ["A"], 1: ["A", "B"], 2: ["B"]}, bt.Costs(),
                                 start_equity=1000.0)
    assert all(e >= i - 1e-9 for e, i in zip(result.equity, result.invested))


def test_a_weighted_schedule_splits_the_fraction_by_weight():
    panel = {"A": Quotes([10.0] * 6), "B": Quotes([10.0] * 6)}
    result = bt.rebalance_replay(SIX, panel, {0: {"A": 3.0, "B": 1.0}}, NO_COSTS, start_equity=1000.0,
                                 fraction=0.8)
    equal = bt.rebalance_replay(SIX, panel, {0: ["A", "B"]}, NO_COSTS, start_equity=1000.0, fraction=0.8)
    assert result.invested[0] == pytest.approx(800.0) == pytest.approx(equal.invested[0])
    moved = {"A": Quotes([10.0, 20.0, 20.0, 20.0, 20.0, 20.0]), "B": Quotes([10.0] * 6)}
    heavy = bt.rebalance_replay(SIX, moved, {0: {"A": 3.0, "B": 1.0}}, NO_COSTS, start_equity=1000.0)
    light = bt.rebalance_replay(SIX, moved, {0: {"A": 1.0, "B": 3.0}}, NO_COSTS, start_equity=1000.0)
    assert heavy.equity[1] == pytest.approx(1750.0) and light.equity[1] == pytest.approx(1250.0)


def test_a_zero_weight_name_is_not_bought_and_a_held_one_is_sold():
    panel = {"A": Quotes([10.0] * 6), "B": Quotes([10.0] * 6)}
    result = bt.rebalance_replay(SIX, panel, {0: ["A", "B"], 2: {"A": 1.0, "B": 0.0}}, NO_COSTS,
                                 start_equity=1000.0)
    assert result.invested[2] == pytest.approx(1000.0)
    assert result.extra_bought == pytest.approx(1500.0)          # B's 500 moved into A
