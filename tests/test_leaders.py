"""`src/leaders.py` — Leaders monthly, the picker S5 tested.

The rule is small enough to pin with series worked by hand: what the score is,
which month of prices it must not read, who is eligible, and that the control
arm is a fair and repeatable stand-in. The last tests pin the standing rule: if
this is ever traded it rests a limit sell above the buy and has no stop.
"""

from __future__ import annotations

import math

import pytest

from src import leaders

N = 300


def path(returns, start=100.0):
    out = [start]
    for r in returns:
        out.append(out[-1] * (1.0 + r))
    return out


def wiggle(k, size=0.004):
    return size * math.sin(k * 1.7) + size * 0.5 * math.cos(k * 0.61)


MARKET_R = [wiggle(k) for k in range(N)]
SECTOR_R = [wiggle(k + 40, 0.005) for k in range(N)]
MARKET = path(MARKET_R)
SECTOR = path(SECTOR_R)


def name(drift, noise=0.002, beta_m=1.0, beta_s=0.5, last_month=None):
    returns = [drift + beta_m * m + beta_s * s + (noise if k % 2 else -noise)
               for k, (m, s) in enumerate(zip(MARKET_R, SECTOR_R))]
    if last_month is not None:
        returns[-leaders.SKIP:] = [last_month] * leaders.SKIP
    return path(returns)


def test_the_score_is_the_drift_left_after_the_market_and_the_sector():
    up = leaders.residual_momentum(name(0.001), MARKET, SECTOR)
    flat = leaders.residual_momentum(name(0.0), MARKET, SECTOR)
    down = leaders.residual_momentum(name(-0.001), MARKET, SECTOR)
    assert up > 5.0 and down < -5.0
    assert abs(flat) < 1.0
    # 231 days of +0.1% over alternating +/-0.2% noise: 0.001 * 231 / (0.002 * sqrt(231)), about 7.6.
    assert up == pytest.approx(0.001 * 231 / (0.002 * math.sqrt(231)), rel=0.02)


def test_beta_to_the_market_or_the_sector_is_not_momentum():
    plain = leaders.residual_momentum(name(0.0005), MARKET, SECTOR)
    geared = leaders.residual_momentum(name(0.0005, beta_m=2.0, beta_s=1.5), MARKET, SECTOR)
    assert geared == pytest.approx(plain, rel=1e-6)


def test_the_last_month_is_not_read():
    quiet = leaders.residual_momentum(name(0.0005), MARKET, SECTOR)
    spiked = leaders.residual_momentum(name(0.0005, last_month=0.03), MARKET, SECTOR)
    crashed = leaders.residual_momentum(name(0.0005, last_month=-0.03), MARKET, SECTOR)
    assert spiked == pytest.approx(quiet) and crashed == pytest.approx(quiet)


def test_too_little_history_is_no_score_not_a_low_one():
    short = name(0.001)[-(leaders.LOOKBACK - 1):]
    assert leaders.residual_momentum(short, MARKET[-len(short):], SECTOR[-len(short):]) is None
    holes = name(0.001)
    for k in range(40, 80):
        holes[k] = None
    assert leaders.residual_momentum(holes, MARKET, SECTOR) is None      # under 200 usable days
    assert leaders.residual_momentum(name(0.001), MARKET[:-1], SECTOR) is None   # calendars differ


def test_a_name_with_no_noise_at_all_has_no_score():
    exact = path([m + 0.5 * s for m, s in zip(MARKET_R, SECTOR_R)])
    assert leaders.residual_momentum(exact, MARKET, SECTOR) is None


def test_above_trend_needs_all_two_hundred_closes():
    assert leaders.above_trend([100.0] * 199 + [101.0]) is True
    assert leaders.above_trend([100.0] * 199 + [99.0]) is False
    assert leaders.above_trend([100.0] * 200) is False                    # level with it is not above
    assert leaders.above_trend([100.0] * 150) is None
    assert leaders.above_trend([100.0] * 100 + [None] + [100.0] * 99 + [120.0]) is None


def book():
    closes = {
        "AAA": name(0.0015), "BBB": name(0.0010), "CCC": name(0.0005), "DDD": name(0.0002),
        "LOW": name(-0.0015),                                 # below its 200-session average
        "NEW": name(0.0020)[-150:],                           # too short for the average
        "ORF": name(0.0020),                                  # no sector series
        "OUT": name(0.0030),                                  # not a member
    }
    sector_of = {s: "Tech" for s in closes}
    sector_of["DDD"] = "Energy"
    sector_of["ORF"] = "Unmapped"
    return closes, sector_of


def ranked(**options):
    closes, sector_of = book()
    members = [s for s in closes if s != "OUT"]
    return leaders.rank(closes, MARKET, {"Tech": SECTOR, "Energy": SECTOR}, sector_of,
                        members=members, **options)


def test_rank_takes_the_highest_scores_among_the_eligible():
    result = ranked(top_n=2)
    assert [row["symbol"] for row in result["picks"]] == ["AAA", "BBB"]
    assert [row["symbol"] for row in result["eligible"]] == ["AAA", "BBB", "CCC", "DDD"]
    scores = [row["score"] for row in result["eligible"]]
    assert scores == sorted(scores, reverse=True)


def test_every_name_left_out_is_counted_under_its_reason():
    assert ranked()["absent"] == {
        leaders.ABSENT_BELOW_TREND: 1,
        leaders.ABSENT_NO_SECTOR: 1,
        leaders.ABSENT_NO_TREND: 1,
        leaders.ABSENT_NOT_MEMBER: 1,
    }


def test_without_a_member_list_every_series_is_a_candidate():
    closes, sector_of = book()
    result = leaders.rank(closes, MARKET, {"Tech": SECTOR, "Energy": SECTOR}, sector_of)
    assert result["picks"][0]["symbol"] == "OUT"


def test_the_default_is_the_accounts_seven_slots():
    assert leaders.TOP_N == 7
    assert len(ranked()["picks"]) == 4                         # fewer eligible than slots: no padding


def test_the_control_is_an_eligible_same_sector_name_that_is_not_a_pick():
    result = ranked(top_n=2)
    stand_ins = leaders.control(result, "2026-10")
    assert [c["for"] for c in stand_ins] == ["AAA"]            # one Tech name left, and it is used once
    assert stand_ins[0]["symbol"] == "CCC" and stand_ins[0]["sector"] == "Tech"


def test_a_pick_with_no_same_sector_name_left_gets_no_control():
    result = ranked(top_n=4)
    assert leaders.control(result, "2026-10") == []


def test_the_control_is_the_same_every_time_and_moves_with_the_period():
    closes = {f"S{k:02d}": name(0.0002 * (k + 1)) for k in range(20)}
    sector_of = {s: "Tech" for s in closes}
    result = leaders.rank(closes, MARKET, {"Tech": SECTOR}, sector_of, top_n=5)
    first = leaders.control(result, "2026-10")
    assert first == leaders.control(result, "2026-10")
    picked = {row["symbol"] for row in result["picks"]}
    assert len({c["symbol"] for c in first}) == 5 and not picked & {c["symbol"] for c in first}
    months = {tuple(c["symbol"] for c in leaders.control(result, f"2026-{m:02d}")) for m in range(1, 13)}
    assert len(months) > 1


def test_the_only_exit_order_this_rule_knows_is_a_limit_sell_above_the_buy():
    assert leaders.TARGET_PCT > 0.0
    assert not [n for n in vars(leaders) if "STOP" in n.upper()]


def test_the_entry_path_does_not_read_this_module():
    import pathlib

    root = pathlib.Path(leaders.__file__).resolve().parent
    readers = [p.name for p in root.rglob("*.py")
               if p.name != "leaders.py" and "leaders" in p.read_text(encoding="utf-8")]
    assert readers == []
