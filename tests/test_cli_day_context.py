"""The day-context cache's per-symbol split (`day_context_plan`) and its
per-symbol write shaping (`day_context_write_symbol`) — added after the
wholesale write design turned out to leave
nothing behind when a cycle died partway through its live fetch.
"""

from src import cli
from tests.factories import cli_payload


def plan(**overrides) -> dict:
    return cli.day_context_plan(cli_payload(**overrides), cli.load_config())


def write_symbol(**overrides) -> dict:
    return cli.day_context_write_symbol(cli_payload(**overrides), cli.load_config())


FRESH_NOW = "2026-08-03T14:00:00Z"


def _entry(fetched_at: str) -> dict:
    return {"signals": {"rsi": 50}, "earnings": {}, "fetched_at": fetched_at}


def _entry_with_statics(signals_at: str, statics_at: str) -> dict:
    return {
        "signals": {"rsi": 50},
        "earnings": {},
        "fetched_at": signals_at,
        "fundamentals_static": {
            "average_volume": 55_000_000,
            "sector": "Consumer Discretionary",
            "market_cap": 48_000_000_000,
            "fetched_at": statics_at,
        },
    }


# --- day_context_plan --------------------------------------------------


def test_a_symbol_fetched_within_the_window_is_fresh():
    result = plan(
        now=FRESH_NOW,
        symbols=["AAPL"],
        day_context={"AAPL": _entry("2026-08-03T13:00:00Z")},
    )
    assert result["fresh"] == ["AAPL"]
    assert result["stale"] == []


def test_a_symbol_fetched_outside_the_window_is_stale():
    """13h old — past `DAY_CONTEXT_MAX_AGE`'s 12-hour window, e.g. carried
    over from the prior trading day rather than merely stale within today's."""
    result = plan(
        now=FRESH_NOW,
        symbols=["AAPL"],
        day_context={"AAPL": _entry("2026-08-03T01:00:00Z")},
    )
    assert result["fresh"] == []
    assert result["stale"] == ["AAPL"]


def test_a_symbol_entirely_missing_from_the_cache_is_stale():
    result = plan(now=FRESH_NOW, symbols=["AAPL"], day_context={})
    assert result["fresh"] == []
    assert result["stale"] == ["AAPL"]


def test_a_symbol_present_but_with_no_fetched_at_is_stale():
    """Defensive: a malformed or partially-written entry must degrade to
    stale, not raise and not count as fresh."""
    result = plan(
        now=FRESH_NOW,
        symbols=["AAPL"],
        day_context={"AAPL": {"signals": {}, "earnings": {}}},
    )
    assert result["stale"] == ["AAPL"]


def test_a_symbol_whose_entry_is_not_a_mapping_is_stale_not_a_crash():
    """`edge_day_context_write_symbol`'s `||` merge then promoted the existing `{}` entry
    into a one-element array and concatenated, leaving `market_data->'AAPL'` as a bare
    JSON array (`[{}, {...quarter...}, ...]`). `entry.get(...)` on that list raised
    AttributeError and crashed `day-context-plan` for the whole cycle. Any non-mapping
    entry must degrade to stale instead."""
    result = plan(
        now=FRESH_NOW,
        symbols=["AAPL"],
        day_context={"AAPL": [{}, {"eps": {"actual": "1.0"}, "quarter": 4}]},
    )
    assert result["stale"] == ["AAPL"]


def test_mixed_fresh_and_stale_split_correctly():
    result = plan(
        now=FRESH_NOW,
        symbols=["AAPL", "MSFT", "TSLA"],
        day_context={
            "AAPL": _entry("2026-08-03T13:00:00Z"),  # fresh, 1h old
            "MSFT": _entry("2026-08-03T01:00:00Z"),  # stale, 13h old
            # TSLA: missing entirely -> stale
        },
    )
    assert result["fresh"] == ["AAPL"]
    assert set(result["stale"]) == {"MSFT", "TSLA"}


def test_a_cycle_that_stopped_after_n_of_m_candidates_only_asks_for_the_rest():
    """The actual production scenario this design fixes: a prior cycle died
    after writing 13 of 20 candidates. The next cycle's plan should mark only
    the missing 7 as stale, not repeat all 20."""
    written = [f"SYM{i}" for i in range(13)]
    missing = [f"SYM{i}" for i in range(13, 20)]
    day_context = {s: _entry("2026-08-03T13:30:00Z") for s in written}

    result = plan(now=FRESH_NOW, symbols=written + missing, day_context=day_context)

    assert set(result["fresh"]) == set(written)
    assert set(result["stale"]) == set(missing)


def test_no_symbols_at_all_returns_two_empty_lists():
    result = plan(now=FRESH_NOW, symbols=[], day_context={})
    assert result == {**result, "fresh": [], "stale": []}


def test_the_cycle_is_still_derived_not_trusted():
    """Matches `test_cli_settled_cash.py`'s own version of this test — a
    postclose-hours timestamp so the derived cycle type is unambiguously not
    `full` no matter what the forged payload claims."""
    result = cli.day_context_plan(
        cli_payload(
            cycle={"cycle_type": "full", "entries_allowed": True},
            now="2026-08-03T20:30:00Z",
        ),
        cli.load_config(),
    )
    assert result["cycle"]["cycle_type"] != "full"


# --- day_context_plan: the fundamentals_static split ----------------------
#
# `average_volume`/`sector`/`market_cap` are daily-stable,
# so they get their own freshness stamp and their own fresh/stale split,
# judged independently of the intraday `signals`/`earnings` one.


def test_statics_are_split_on_their_own_stamp_not_the_signals_one():
    """A symbol whose signals aged out overnight but whose statics were pulled
    an hour ago: `stale` for signals, `statics_fresh` for fundamentals."""
    result = plan(
        now=FRESH_NOW,
        symbols=["AAPL"],
        day_context={
            "AAPL": _entry_with_statics(
                signals_at="2026-08-03T01:00:00Z",   # 13h — stale
                statics_at="2026-08-03T13:00:00Z",   # 1h  — fresh
            )
        },
    )
    assert result["stale"] == ["AAPL"]
    assert result["statics_fresh"] == ["AAPL"]
    assert result["statics_stale"] == []


def test_fresh_signals_do_not_make_absent_statics_fresh():
    """The mirror case: a plain `_entry` (no `fundamentals_static` at all) is
    `fresh` for signals and `statics_stale` — an entries-allowed cycle still
    owes it one `get_equity_fundamentals` call."""
    result = plan(
        now=FRESH_NOW,
        symbols=["AAPL"],
        day_context={"AAPL": _entry("2026-08-03T13:00:00Z")},
    )
    assert result["fresh"] == ["AAPL"]
    assert result["statics_stale"] == ["AAPL"]
    assert result["statics_fresh"] == []


def test_statics_age_out_on_the_same_twelve_hour_window():
    result = plan(
        now=FRESH_NOW,
        symbols=["AAPL"],
        day_context={
            "AAPL": _entry_with_statics(
                signals_at="2026-08-03T13:00:00Z",
                statics_at="2026-08-03T00:30:00Z",   # 13.5h — stale
            )
        },
    )
    assert result["statics_stale"] == ["AAPL"]


def test_a_malformed_statics_block_is_stale_not_an_error():
    result = plan(
        now=FRESH_NOW,
        symbols=["AAPL"],
        day_context={
            "AAPL": {
                "signals": {"rsi": 50},
                "fetched_at": "2026-08-03T13:00:00Z",
                "fundamentals_static": {"average_volume": 1},  # no fetched_at
            }
        },
    )
    assert result["statics_stale"] == ["AAPL"]


def test_a_warm_cache_asks_for_no_fundamentals_for_held_positions():
    """Acceptance: on a warm cache a monitor cycle's plan pulls zero
    `get_equity_fundamentals` calls for held positions — their statics are
    `statics_fresh` and a held name is never a CATALYST entry candidate."""
    held = ["AAPL", "MSFT", "TSLA"]
    warm = {
        s: _entry_with_statics(
            signals_at="2026-08-03T13:00:00Z",
            statics_at="2026-08-03T12:30:00Z",
        )
        for s in held
    }
    result = plan(now=FRESH_NOW, symbols=held, day_context=warm)

    assert result["statics_stale"] == []
    assert set(result["statics_fresh"]) == set(held)


# --- day_context_plan: the sma_fresh split --------------
#
# The pre-market data job writes `signals.sma_20/50/200` + `bars_meta` before
# the open. Those moving averages do not move intraday; the RSI and ATR in the
# same `signals` object are not written by the job and stay a live fetch. So a
# symbol can be `sma_fresh` and still `stale`.


def _premarket_entry(fetched_at: str, *, smas=True) -> dict:
    """What `services/core/core/premarket.py` writes: SMAs and bars_meta, no
    RSI, no ATR."""
    signals = {"sma_20": 101.0, "sma_50": 99.0, "sma_200": 90.0} if smas else {}
    return {
        "signals": signals,
        "bars_meta": {"last_bar": "2026-08-02", "bars": 60, "source": "twelvedata"},
        "fetched_at": fetched_at,
    }


def _full_entry(fetched_at: str) -> dict:
    """What a live technicals fetch leaves: RSI + ATR + the SMAs, one bundle."""
    return {
        "signals": {"rsi": 55.0, "atr": 2.1, "sma_20": 101.0, "sma_50": 99.0,
                    "sma_200": 90.0},
        "earnings": {},
        "fetched_at": fetched_at,
    }


def test_a_premarket_written_symbol_is_sma_fresh_but_still_stale():
    result = plan(
        now=FRESH_NOW,
        symbols=["AAPL"],
        day_context={"AAPL": _premarket_entry("2026-08-03T13:00:00Z")},
    )
    assert result["sma_fresh"] == ["AAPL"]
    # No RSI/ATR in the entry, so the indicator fetch still owes this one.
    assert result["stale"] == ["AAPL"]
    assert result["fresh"] == []


def test_an_entry_without_all_three_smas_is_not_sma_fresh():
    result = plan(
        now=FRESH_NOW,
        symbols=["AAPL"],
        day_context={"AAPL": _premarket_entry("2026-08-03T13:00:00Z", smas=False)},
    )
    assert result["sma_fresh"] == []


def test_stale_smas_from_yesterday_are_not_sma_fresh():
    result = plan(
        now=FRESH_NOW,
        symbols=["AAPL"],
        day_context={"AAPL": _premarket_entry("2026-08-03T00:30:00Z")},  # 13.5h old
    )
    assert result["sma_fresh"] == []
    assert result["stale"] == ["AAPL"]


def test_a_fully_fetched_symbol_is_both_fresh_and_sma_fresh():
    result = plan(
        now=FRESH_NOW,
        symbols=["AAPL"],
        day_context={"AAPL": _full_entry("2026-08-03T13:00:00Z")},
    )
    assert result["fresh"] == ["AAPL"]
    assert result["sma_fresh"] == ["AAPL"]


# --- day_context_write_symbol -------------------------------------------
#
# `signals`/`earnings` are keyed by symbol in `cycle.json`, the same shape
# `decide()` itself reads (`payload.get("signals")`) — not a one-off shape
# for this command. `cli_payload()`'s own defaults already carry "F"/"PFE"
# entries under both keys; overrides merge alongside them rather than
# replacing the dict outright, so these tests add a distinct "AAPL" entry
# instead of colliding with the fixture's own symbols.


def test_shapes_signals_and_earnings_with_a_fetched_at_stamp():
    result = write_symbol(
        now=FRESH_NOW,
        symbol="AAPL",
        signals={"AAPL": {"rsi": 61.2}},
        earnings={"AAPL": {"eps_surprise_pct": 3.1}},
    )
    assert result["symbol"] == "AAPL"
    assert result["trading_day"] == result["cycle"]["trading_day"]
    assert result["symbol_data"] == {
        "signals": {"rsi": 61.2},
        "earnings": {"eps_surprise_pct": 3.1},
        "fetched_at": FRESH_NOW,
    }


def test_only_the_named_symbol_is_extracted_not_the_whole_payload():
    """The fixture's default `signals`/`earnings` carry F and PFE — shaping
    AAPL's entry must not leak either of them into `symbol_data`."""
    result = write_symbol(
        now=FRESH_NOW,
        symbol="AAPL",
        signals={"AAPL": {"rsi": 61.2}},
    )
    assert "F" not in str(result["symbol_data"])
    assert "PFE" not in str(result["symbol_data"])


def test_missing_earnings_defaults_to_an_empty_dict_not_an_error():
    """A POSITIONS-scope or exit-only cycle's per-symbol fetch may carry only
    `signals` — this must not raise, and a symbol with no earnings entry at
    all must not be confused with one whose earnings happen to be `{}`."""
    result = write_symbol(now=FRESH_NOW, symbol="AAPL", signals={"AAPL": {"rsi": 61.2}})
    assert result["symbol_data"]["earnings"] == {}


def test_a_symbol_with_no_signals_entry_either_is_still_shaped_not_an_error():
    """The candidate this cycle is writing through might have failed its
    `indicators` call but still needs a durable (empty) record rather than a
    crash — degrade to `{}`, matching `earnings`'s own default."""
    result = write_symbol(now=FRESH_NOW, symbol="ZZZZ")
    assert result["symbol_data"] == {
        "signals": {},
        "earnings": {},
        "fetched_at": FRESH_NOW,
    }


def test_the_cycle_is_still_derived_not_trusted_for_write_symbol():
    result = cli.day_context_write_symbol(
        cli_payload(
            cycle={"cycle_type": "full", "entries_allowed": True},
            now="2026-08-03T20:30:00Z",
        ),
        cli.load_config(),
    )
    assert result["cycle"]["cycle_type"] != "full"


# --- day_context_write_symbol: the fundamentals_static block --------------


def test_fundamentals_static_is_written_when_the_cycle_fetched_it():
    result = write_symbol(
        now=FRESH_NOW,
        symbol="AAPL",
        signals={"AAPL": {"rsi": 61.2}},
        fundamentals={
            "AAPL": {
                "volume": 70_000_000,
                "average_volume": 55_000_000,
                "sector": "Information Technology",
                "market_cap": 3_100_000_000_000,
            }
        },
    )
    assert result["symbol_data"]["fundamentals_static"] == {
        "average_volume": 55_000_000,
        "sector": "Information Technology",
        "market_cap": 3_100_000_000_000,
        "fetched_at": FRESH_NOW,
    }
    # today's volume is deliberately not cached
    assert "volume" not in result["symbol_data"]["fundamentals_static"]


def test_fundamentals_static_carries_its_own_fetched_at():
    """Judged independently of signals freshness, so it needs its own stamp
    inside the block, not just the entry-level one."""
    result = write_symbol(
        now=FRESH_NOW,
        symbol="AAPL",
        fundamentals={"AAPL": {"average_volume": 1, "sector": "X", "market_cap": 2}},
    )
    block = result["symbol_data"]["fundamentals_static"]
    assert block["fetched_at"] == FRESH_NOW
    assert result["symbol_data"]["fetched_at"] == FRESH_NOW


def test_no_fundamentals_for_the_symbol_means_no_statics_block():
    """A monitor cycle that skipped the fundamentals call (statics already
    fresh) writes signals/earnings alone — an empty statics block would read
    back as 'fresh but null', and the write's merge preserves the cached one."""
    result = write_symbol(now=FRESH_NOW, symbol="AAPL", signals={"AAPL": {"rsi": 61.2}})
    assert "fundamentals_static" not in result["symbol_data"]


def test_a_missing_static_field_is_carried_as_null_not_dropped():
    result = write_symbol(
        now=FRESH_NOW,
        symbol="AAPL",
        fundamentals={"AAPL": {"average_volume": 55_000_000}},  # no sector/market_cap
    )
    assert result["symbol_data"]["fundamentals_static"] == {
        "average_volume": 55_000_000,
        "sector": None,
        "market_cap": None,
        "fetched_at": FRESH_NOW,
    }
