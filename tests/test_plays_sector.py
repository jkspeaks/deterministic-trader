"""Sector-relative oversold rotation.

    Buy the sector ETF (XL*) when its RSI <= 30 and it is above SMA200 while
    SPY RSI > 40; whole shares only. Exit at RSI >= 55 or +4%; stop 1.5xATR;
    time 10.

Every leg of that sentence gets a test, plus the two properties that are not in
it and matter more: the play is **off** unless `play_sector_enabled` says
otherwise, and it is the first play in this system to carry its own stop
multiple — so the stop it is exited on, the stop that rests at the broker and
the stop `sizing`'s risk budget measures against all have to be 1.5xATR and not
the account's 2.0.
"""

from __future__ import annotations

import pytest

from src import plays
from src.plays import protection
from tests.factories import ETFS, config, plays_context, position, regime_inputs

# XLE at RSI 24, holding a 95.00 200-day, on a tape whose SPY reads 58.
OVERSOLD = {"last": 100.0, "sma200": 95.0, "rsi14": 24.0}


# These tests predate an earlier retirement and shelving of the event plays
# and use them as they then ran. See `all_plays_live` in conftest.py.
pytestmark = pytest.mark.usefixtures("all_plays_live")


def sector_ctx(**overrides) -> dict:
    """`plays_context` with §4.3 C switched on and XLE oversold.

    `etfs` and `regime_inputs` are what this play reads and `plays_context` has
    neither by default, because until now no play needed either — MACRO reads
    the macro map and every other play reads per-symbol signals.
    """
    ctx = plays_context(**overrides)
    ctx["config"] = config(play_sector_enabled=True)
    ctx["etfs"] = ETFS
    ctx["regime_inputs"] = regime_inputs()
    ctx["regime_inputs"]["sectors"]["XLE"] = dict(OVERSOLD)
    ctx["sectors"] = {**ctx["sectors"], "XLE": "Energy"}
    return ctx


def sector_names(result) -> list[str]:
    return sorted(c.symbol for c in result.candidates if c.play_id == plays.SECTOR)


# --- the switch -------------------------------------------------------------


def test_the_shipped_config_proposes_nothing_at_all():
    """Not even a skip. A switched-off play has not looked at anything, and
    eleven rows a cycle saying so is how a runlog stops being read."""
    ctx = sector_ctx()
    ctx["config"] = config()

    assert plays._sector(ctx) == []
    assert plays.entries(ctx).candidates[0].play_id == plays.DIP


def test_the_shipped_config_file_leaves_it_off():
    assert config().get("play_sector_enabled") is False
    assert plays.sector.enabled({"config": config()}) is False


# --- the trigger ------------------------------------------------------------


def test_an_oversold_etf_holding_its_200_day_is_bought():
    result = plays.entries(sector_ctx())

    assert sector_names(result) == ["XLE"]
    cand = next(c for c in result.candidates if c.play_id == plays.SECTOR)
    assert "RSI 24 is oversold while holding its 95.00 200-day" in cand.detail
    assert "SPY RSI 58" in cand.detail


def test_an_etf_over_rsi_30_never_triggers_and_says_nothing():
    ctx = sector_ctx()
    ctx["regime_inputs"]["sectors"]["XLE"]["rsi14"] = 31.0

    assert plays._sector(ctx) == []


def test_rsi_exactly_30_still_qualifies_and_scores():
    """The boundary case of the play's own stated trigger. Without
    `SECTOR_MIN_CONVICTION` the extremity term is 0.0 here and `_arbitrate`
    would drop it as NO_CONVICTION — the play refusing its own rule."""
    ctx = sector_ctx()
    ctx["regime_inputs"]["sectors"]["XLE"]["rsi14"] = 30.0

    cand = next(c for c in plays.entries(ctx).candidates if c.play_id == plays.SECTOR)
    assert cand.conviction == plays.SECTOR_MIN_CONVICTION


def test_an_oversold_etf_below_its_own_200_day_is_the_named_failure_mode():
    ctx = sector_ctx()
    ctx["regime_inputs"]["sectors"]["XLE"] = {"last": 90.0, "sma200": 95.0, "rsi14": 24.0}

    skip = next(s for s in plays.entries(ctx).skipped if s.play_id == plays.SECTOR)

    assert skip.code == plays.ENTRY_CONDITIONS_UNMET
    assert "sector regime persisting" in skip.message


def test_a_weak_spy_stops_the_rotation():
    """"§4.3 C rotates between sectors, it does not catch a whole tape."""
    ctx = sector_ctx()
    ctx["regime_inputs"]["spy"]["rsi14"] = 38.0

    skip = next(s for s in plays.entries(ctx).skipped if s.play_id == plays.SECTOR)

    assert "SPY RSI 38 is at or under 40" in skip.message


def test_spy_exactly_at_40_is_not_over_40():
    ctx = sector_ctx()
    ctx["regime_inputs"]["spy"]["rsi14"] = 40.0

    assert sector_names(plays.entries(ctx)) == []


def test_an_etf_whose_price_never_arrived_is_refused_not_guessed():
    ctx = sector_ctx()
    ctx["regime_inputs"]["sectors"]["XLE"] = {"rsi14": 24.0}

    skip = next(s for s in plays.entries(ctx).skipped if s.play_id == plays.SECTOR)

    assert "never arrived" in skip.message


def test_no_spy_reading_means_no_proposals_and_no_noise():
    """Every payload predating `regime_inputs` lands here, which is what makes
    turning this play on inert on an old cycle rather than wrong."""
    ctx = sector_ctx()
    ctx["regime_inputs"] = {}

    assert plays._sector(ctx) == []


def test_only_the_eleven_sector_etfs_are_candidates():
    """SPY is the reference this play measures against; buying the thing you
    measure relative strength against is not a rotation."""
    ctx = sector_ctx()

    assert plays.sector.symbols(ctx) == sorted(ETFS["sector"])
    assert "SPY" not in plays.sector.symbols(ctx)
    assert set(plays.sector.symbols(ctx)) & set(ETFS["broad"]) == set()


def test_conviction_grades_how_far_below_the_ceiling_the_etf_is():
    ctx = sector_ctx()
    ctx["regime_inputs"]["sectors"]["XLE"]["rsi14"] = 15.0  # 15 under, full scale

    cand = next(c for c in plays.entries(ctx).candidates if c.play_id == plays.SECTOR)
    assert cand.conviction == 1.0


# --- how it interacts with the rest -----------------------------------------


def test_a_risk_off_tape_does_not_dark_it():
    """§4.3 C's market condition is literally "Any", so SECTOR is deliberately
    absent from `regime.PLAY_IDS` and `_regime_multiplier` reads 1.0. This is
    the most aggressive thing about the play and part of what the ledger is
    there to measure — it is asserted rather than left implicit so nobody
    "fixes" it by accident."""
    from src import regime as regimes

    assert plays.SECTOR not in regimes.PLAY_IDS

    ctx = sector_ctx()
    ctx["regime"] = {
        "play_multipliers": regimes._play_multipliers(regimes.RISK_OFF),
        "reason": "risk_off",
    }

    assert sector_names(plays.entries(ctx)) == ["XLE"]


def test_macro_and_sector_wanting_the_same_etf_is_one_position():
    """Both propose ETFs and both can name XLE on the same day.
    `_best_per_symbol` settles it the way it settles any double trigger."""
    ctx = sector_ctx()
    ctx["macro"] = {
        "releases": [
            {"series": "CPI", "surprise_pct": 0.30, "first_seen": "2026-08-03"}
        ]
    }
    ctx["signals"]["XLE"] = {"sma_200": 90.0}

    result = plays.entries(ctx)
    xle = [c for c in result.candidates if c.symbol == "XLE"]

    assert len(xle) == 1
    assert any(
        s.symbol == "XLE" and s.code == plays.OUTRANKED_ON_SYMBOL
        for s in result.skipped
    )


def test_an_inherited_etf_binds_to_sector_only_while_the_play_is_on():
    ctx = sector_ctx()

    assert plays.would_open("XLE", ctx) == plays.SECTOR

    ctx["config"] = config()
    assert plays.would_open("XLE", ctx) is None


# --- the exit terms ---------------------------------------------------------


def test_the_declared_terms_are_the_plays_own():
    play = plays.PLAYS[plays.SECTOR]

    assert (play.holding_days, play.target_pct, play.stop_atr_multiple) == (
        10,
        0.04,
        1.5,
    )


def test_the_stop_is_the_plays_15x_and_not_the_accounts_2x():
    """The first play in this system to declare its own stop. All three readers
    have to agree — `exits` sells through it, `protection` rests it at the
    broker, `sizing`'s risk budget divides by it."""
    ctx = sector_ctx()
    held = position(symbol="XLE", play_id=plays.SECTOR, entry_price=100.0,
                    high_since_entry=100.0, market_value=99.0)
    ctx["positions"] = [held]
    ctx["signals"]["XLE"] = {"atr": 2.0, "rsi": 30.0}
    ctx["quotes"]["XLE"] = {"bid": 98.95, "ask": 99.05, "last": 99.0}

    assert plays.stop_multiple(plays.SECTOR, ctx) == 1.5
    assert plays.stop_multiple(plays.DIP, ctx) == 3.0
    # 100 - 1.5x2.0 = 97.00, where the account's 3.0 would give 94.00.
    assert protection.stop_price(held, ctx) == 97.00


def test_the_target_rests_at_four_percent():
    ctx = sector_ctx()
    held = position(symbol="XLE", play_id=plays.SECTOR, entry_price=100.0)
    ctx["signals"]["XLE"] = {"atr": 2.0}

    assert protection.target_price(held, ctx) == (104.00, plays.SECTOR, 0.04)


def test_rsi_back_over_55_closes_the_rotation():
    ctx = sector_ctx()
    ctx["positions"] = [
        position(symbol="XLE", play_id=plays.SECTOR, entry_price=100.0,
                 market_value=101.0, high_since_entry=101.0)
    ]
    ctx["signals"]["XLE"] = {"atr": 2.0, "rsi": 56.0}
    ctx["quotes"]["XLE"] = {"bid": 100.95, "ask": 101.05, "last": 101.0}

    leaving = plays.exits(ctx).exits

    assert [(e.symbol, e.code) for e in leaving] == [("XLE", plays.SIGNAL_EXIT)]
    assert "rotated back" in leaving[0].reason


def test_rsi_54_holds():
    ctx = sector_ctx()
    ctx["positions"] = [
        position(symbol="XLE", play_id=plays.SECTOR, entry_price=100.0,
                 market_value=101.0, high_since_entry=101.0)
    ]
    ctx["signals"]["XLE"] = {"atr": 2.0, "rsi": 54.0}
    ctx["quotes"]["XLE"] = {"bid": 100.95, "ask": 101.05, "last": 101.0}

    assert plays.exits(ctx).exits == []


def test_the_time_stop_is_ten_sessions():
    ctx = sector_ctx()
    ctx["positions"] = [
        position(symbol="XLE", play_id=plays.SECTOR, entry_price=100.0,
                 market_value=101.0, high_since_entry=101.0, sessions_held=10)
    ]
    ctx["signals"]["XLE"] = {"atr": 2.0, "rsi": 40.0}
    ctx["quotes"]["XLE"] = {"bid": 100.95, "ask": 101.05, "last": 101.0}

    assert [e.code for e in plays.exits(ctx).exits] == [plays.TIME_STOP]


def test_a_position_survives_the_switch_being_turned_back_off():
    """`SECTOR` is in `PLAYS` unconditionally. A play id that vanished when the
    flag flipped would strand an open position on `_reason_to_leave`'s
    `play is None` branch — no time stop, no target, no signal exit."""
    ctx = sector_ctx()
    ctx["config"] = config()
    ctx["positions"] = [
        position(symbol="XLE", play_id=plays.SECTOR, entry_price=100.0,
                 market_value=101.0, high_since_entry=101.0)
    ]
    ctx["signals"]["XLE"] = {"atr": 2.0, "rsi": 56.0}
    ctx["quotes"]["XLE"] = {"bid": 100.95, "ask": 101.05, "last": 101.0}

    assert [e.code for e in plays.exits(ctx).exits] == [plays.SIGNAL_EXIT]


# --- discovery --------------------------------------------------------------


def test_discovery_adds_the_qualifying_etfs_to_the_fetch_list():
    """The play proposes for free off `regime_inputs`, but `sizing` needs a
    quote and an ATR — so the ETF has to earn a per-symbol fetch at discovery
    or the play can never actually place."""
    from src import candidates

    payload = {"regime_inputs": {"sectors": {"XLE": dict(OVERSOLD),
                                             "XLK": {"last": 100.0, "sma200": 95.0,
                                                     "rsi14": 55.0}}}}
    loaded = {"etfs": ETFS, "macro_map": {}, "config": config(play_sector_enabled=True)}

    assert candidates.assemble(payload, loaded)["macro_targets"] == ["XLE"]


def test_discovery_adds_nothing_while_the_play_is_off():
    from src import candidates

    payload = {"regime_inputs": {"sectors": {"XLE": dict(OVERSOLD)}}}
    loaded = {"etfs": ETFS, "macro_map": {}, "config": config()}

    assert candidates.assemble(payload, loaded)["macro_targets"] == []


def test_discovery_leaves_spys_leg_to_the_play():
    """The coarse screen may be looser than the play and must never be
    tighter: too generous is a wasted quote, too strict is a play that can
    never fire. So SPY's RSI is not consulted here."""
    from src import candidates

    payload = {"regime_inputs": {"spy": {"rsi14": 20.0},
                                 "sectors": {"XLE": dict(OVERSOLD)}}}
    loaded = {"etfs": ETFS, "macro_map": {}, "config": config(play_sector_enabled=True)}

    assert candidates.assemble(payload, loaded)["macro_targets"] == ["XLE"]


def test_a_macro_target_and_a_sector_target_are_one_fetch():
    from src import candidates
    from tests.factories import MACRO_MAP

    payload = {
        "macro": {"releases": [{"series": "CPI", "surprise_pct": 0.30,
                                "sessions_ago": 0}]},
        "regime_inputs": {"sectors": {"XLE": dict(OVERSOLD)}},
    }
    loaded = {"etfs": ETFS, "macro_map": MACRO_MAP,
              "config": config(play_sector_enabled=True)}

    targets = candidates.assemble(payload, loaded)["macro_targets"]

    assert targets == sorted(set(targets))
    assert targets.count("XLE") <= 1
