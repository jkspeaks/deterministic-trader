"""`dip_recovery.watchlist` — the Universe Pick routine's two halves.

`pool` is every filter that needs no broker call; `pick` is the ranking that
runs after the per-symbol fetch. They are tested apart because that is how they
run: the whole reason the module splits in two is that the fetch, and therefore
`max_scan_survivors`, sits between them.
"""

from __future__ import annotations

from src.dip_recovery import core, watchlist
from tests.dip_recovery_factories import (
    dip_recovery_config,
    held_position,
    pick_context,
    pool_context,
    ranked_symbol,
    skip_codes,
)


def _skipped(result, symbol: str) -> list[str]:
    return [s.code for s in result.skipped if s.symbol == symbol]


# --- pool: the free filters -------------------------------------------------


def test_a_clean_scan_hit_reaches_the_fetch_list():
    result = watchlist.pool(pool_context())

    assert result.fetch == ["DIP1", "DIP2"]
    assert result.skipped == []


def test_a_scan_hit_outside_the_universe_is_dropped():
    """The design's first entry condition is membership. The scan is a
    Robinhood-wide screen and knows nothing about the issuer holdings file, so
    this is the ordinary case, not an error."""
    result = watchlist.pool(pool_context(scan=["DIP1", "NOTAMEMBER"]))

    assert result.fetch == ["DIP1"]
    assert _skipped(result, "NOTAMEMBER") == [core.NOT_IN_UNIVERSE]


def test_a_stale_universe_snapshot_picks_nothing():
    """Deliberately *not* the phase-7 `UNIVERSE_STALE` ruling reversed. Guard
    leaves that code to core because acting on an already-chosen watchlist says
    nothing about core's membership file — but choosing one is exactly a claim
    about membership, and an unknown fence admits nothing."""
    result = watchlist.pool(
        pool_context(universe={"members": [], "stale": True, "snapshot_age_days": 30})
    )

    assert result.fetch == []
    assert skip_codes(result) == [core.NOT_IN_UNIVERSE, core.NOT_IN_UNIVERSE]
    assert "30 days old" in result.reason


def test_a_never_fetched_snapshot_says_so_rather_than_reporting_an_age():
    result = watchlist.pool(
        pool_context(universe={"members": [], "stale": True, "snapshot_age_days": None})
    )

    assert result.fetch == []
    assert "unfetched" in result.reason


def test_a_symbol_this_side_already_tracks_is_not_re_offered():
    """The watchlist is a *new-entry* list. Dip Monitor unions the held names in
    itself (`engine._managed`), so a tracked symbol on the watchlist would only
    ever produce an `ALREADY_TRACKED` skip on every tick of the day."""
    result = watchlist.pool(
        pool_context(dip_recovery_positions={"DIP1": held_position()})
    )

    assert result.fetch == ["DIP2"]
    assert _skipped(result, "DIP1") == [core.ALREADY_TRACKED]


def test_a_symbol_the_other_side_owns_is_not_offered():
    result = watchlist.pool(pool_context(symbol_lock={"DIP1": "core"}))

    assert result.fetch == ["DIP2"]
    assert _skipped(result, "DIP1") == [core.SYMBOL_LOCKED]


def test_this_sides_own_lock_entry_is_not_read_as_the_other_sides():
    """`symbol_lock` carries both sides. A row naming `dip_recovery` is this
    side's own claim and must not exclude the symbol — the bug that would make
    this side unable to ever re-enter a name it once held."""
    result = watchlist.pool(pool_context(symbol_lock={"DIP1": "dip_recovery"}))

    assert result.fetch == ["DIP1", "DIP2"]


def test_a_symbol_core_holds_but_has_not_claimed_yet_is_still_excluded():
    """`reconcile.py` claims a symbol on the cycle *after* core's fill confirms.
    Between those two moments core's book is the only place the position
    exists, so the book is read alongside the lock rather than trusting the
    lock alone — the same pairing `reconcile._other_side_symbols` uses in the
    other direction."""
    result = watchlist.pool(pool_context(positions=[{"symbol": "DIP1", "quantity": 3}]))

    assert result.fetch == ["DIP2"]
    assert _skipped(result, "DIP1") == [core.SYMBOL_LOCKED]


def test_a_recent_loss_close_is_excluded_account_wide():
    """§9's wash-sale window attaches to the account, not to a strategy, so a
    name *core* closed at a loss is one this side may not repurchase either.
    Excluded here so the watchlist is not a name guard will deny all day."""
    result = watchlist.pool(
        pool_context(loss_closes=[{"symbol": "DIP1", "close_date": "2026-08-20"}])
    )

    assert result.fetch == ["DIP2"]
    assert _skipped(result, "DIP1") == [core.RECENT_LOSS_CLOSE]
    assert "11 day(s) ago" in result.skipped[0].message


def test_a_loss_close_past_the_window_does_not_exclude():
    result = watchlist.pool(
        pool_context(loss_closes=[{"symbol": "DIP1", "close_date": "2026-05-01"}])
    )

    assert result.fetch == ["DIP1", "DIP2"]


def test_the_fetch_ceiling_truncates_and_logs_every_symbol_it_drops():
    """A cost ceiling, not a judgment — so it is applied last (a name is never
    dropped for cost when a real rule would have dropped it anyway) and every
    casualty is logged, so a morning where it binds is legible in the runlog."""
    members = [f"S{i}" for i in range(10)]
    result = watchlist.pool(
        pool_context(
            scan=members,
            universe={"members": members, "stale": False, "snapshot_age_days": 1},
            dip_recovery_config=dip_recovery_config(
                universe_thresholds={"max_scan_survivors": 4}
            ),
        )
    )

    assert result.fetch == ["S0", "S1", "S2", "S3"]
    assert skip_codes(result) == [core.SCAN_POOL_TRUNCATED] * 6
    assert "6 past the fetch ceiling" in result.reason


def test_a_duplicated_scan_hit_is_counted_once():
    result = watchlist.pool(pool_context(scan=["DIP1", "DIP1", "DIP2"]))

    assert result.fetch == ["DIP1", "DIP2"]


# --- pick: the ranking ------------------------------------------------------


def test_the_pick_writes_the_watchlist_key_for_todays_trading_day():
    result = watchlist.pick(pick_context())

    assert result.symbols == ["DIP2", "DIP1"]
    assert result.control_updates == {
        "dip_recovery_watchlist": {
            "trading_day": "2026-08-31",
            "symbols": ["DIP2", "DIP1"],
        }
    }


def test_an_empty_pick_still_writes_todays_key():
    """Writing an empty watchlist is not the same as writing nothing. Dip
    Monitor reads a watchlist whose `trading_day` is not today as empty
    anyway — the explicit write is what distinguishes "this morning ran and
    found nothing" from "this morning may never have run"."""
    result = watchlist.pick(pick_context(fetch=[]))

    assert result.symbols == []
    assert result.control_updates == {
        "dip_recovery_watchlist": {"trading_day": "2026-08-31", "symbols": []}
    }


def test_higher_atr_wins_when_liquidity_ties():
    """The two factors are equal halves, so a tie in one hands the pick to the
    other. Volatility earns its half because this side has no stop: what
    bounds the downside is the averaging ladder and the never-below-cost rule,
    while what makes a 5% target reachable inside the 47-day clock is range."""
    result = watchlist.pick(
        pick_context(
            {
                "QUIET": ranked_symbol(atr=1.60),
                "LIVELY": ranked_symbol(atr=2.50),
            }
        )
    )

    assert result.symbols == ["LIVELY", "QUIET"]
    assert result.ranked[0]["volatility_rank"] == 1.0
    assert result.ranked[0]["liquidity_rank"] == 0.0


def test_a_thin_name_is_dropped_before_it_is_ranked():
    """The liquidity floor is about the *exit*: a whole position leaves in one
    limit order, so a name that cannot absorb it does not become tradable
    just because it dipped."""
    result = watchlist.pick(
        pick_context({"THIN": ranked_symbol(average_volume=10_000)})
    )

    assert result.symbols == []
    assert _skipped(result, "THIN") == [core.INSUFFICIENT_LIQUIDITY]


def test_an_atr_outside_the_band_is_dropped_at_either_end():
    result = watchlist.pick(
        pick_context(
            {
                "FLAT": ranked_symbol(atr=0.10),
                "WILD": ranked_symbol(atr=6.00),
                "OK": ranked_symbol(),
            }
        )
    )

    assert result.symbols == ["OK"]
    assert _skipped(result, "FLAT") == [core.ATR_OUT_OF_RANGE]
    assert _skipped(result, "WILD") == [core.ATR_OUT_OF_RANGE]


def test_a_missing_factor_fails_closed_rather_than_reading_as_zero():
    """`signal._bar_metrics`' rule, restated: a symbol whose ATR never arrived
    is not a symbol with no volatility. Ranking it at zero would quietly put
    it last forever; dropping it says so."""
    result = watchlist.pick(pick_context({"NOATR": ranked_symbol(atr=None)}))

    assert result.symbols == []
    assert _skipped(result, "NOATR") == [core.METRICS_UNAVAILABLE]


def test_an_earnings_print_inside_the_flatten_window_is_excluded():
    """The design's "existing event-risk exclusion" is guard's own rule, read
    from guard's own config key and applied a few hours early."""
    result = watchlist.pick(
        pick_context({"SOON": ranked_symbol(sessions_until_report=1)})
    )

    assert result.symbols == []
    assert _skipped(result, "SOON") == [core.EVENT_RISK]


def test_an_unknown_earnings_date_is_excluded_too():
    """Guard denies every buy without a confirmed date, so a name with none
    could not be entered today however well it ranked. Matching that here
    keeps the watchlist from filling with names that can never trade."""
    result = watchlist.pick(
        pick_context(earnings={"DIP1": {"status": "unknown"}, "DIP2": {"status": "unknown"}})
    )

    assert result.symbols == []
    assert skip_codes(result) == [core.EVENT_RISK, core.EVENT_RISK]


def test_names_past_the_cut_are_logged_as_rank_not_top():
    """"Nothing was wrong with it, four names simply scored higher" is the
    answer to the most common question this routine will be asked, so it is a
    logged rejection rather than an absence."""
    rows = {f"S{i}": ranked_symbol(atr=1.50 + i * 0.10) for i in range(7)}
    result = watchlist.pick(pick_context(rows))

    assert result.symbols == ["S6", "S5", "S4", "S3", "S2"]
    assert _skipped(result, "S1") == [core.RANK_NOT_TOP]
    assert _skipped(result, "S0") == [core.RANK_NOT_TOP]


def test_a_score_tie_breaks_on_symbol_so_two_runs_cannot_disagree():
    rows = {"ZZZ": ranked_symbol(), "AAA": ranked_symbol()}
    result = watchlist.pick(
        pick_context(
            rows,
            dip_recovery_config=dip_recovery_config(
                universe_thresholds={"watchlist_size": 1}
            ),
        )
    )

    assert result.symbols == ["AAA"]


def test_a_single_survivor_ranks_at_the_top_of_both_factors():
    """A percentile needs two points to mean anything. One survivor is the
    whole distribution, and reporting it as rank 0.0 would read as "worst"."""
    result = watchlist.pick(pick_context({"ONLY": ranked_symbol()}))

    assert result.symbols == ["ONLY"]
    assert result.ranked[0]["score"] == 100.0


def test_the_defaults_hold_when_db_015_has_not_been_applied():
    """`watchlist._DEFAULTS` is the `signal.py` Bollinger precedent: the pick
    has to be correct in a checkout whose Supabase has no `universe_thresholds`
    row, not only after the migration lands."""
    rows = {f"S{i}": ranked_symbol(atr=1.50 + i * 0.10) for i in range(7)}
    ctx = pick_context(rows)
    del ctx["dip_recovery_config"]["universe_thresholds"]

    assert len(watchlist.pick(ctx).symbols) == 5
