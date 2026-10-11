"""`src.cli.candidates` — a thin pass-through to `src/candidates.py`, wired
to the real checked-in `config/macro_map.json` and `config/etfs.json`."""

import pytest
from src import cli
from tests.factories import cli_payload, core_off


# These tests predate an earlier retirement and shelving of the event plays
# and use them as they then ran. See `all_plays_live` in conftest.py.
pytestmark = pytest.mark.usefixtures("all_plays_live")


def test_wires_the_real_macro_map_and_etf_sleeve():
    result = cli.candidates(
        cli_payload(
            macro={
                "releases": [
                    {
                        "series": "CPI",
                        "surprise_pct": 0.5,
                        "first_seen": "2026-08-02",
                        "sessions_ago": 0,
                    }
                ]
            }
        ),
        core_off(),
    )
    assert result["macro_targets"]
    assert set(result["macro_targets"]) <= set(cli.load_config()["etfs"]["sector"])


def test_a_bare_payload_produces_an_empty_but_well_shaped_result():
    result = cli.candidates(cli_payload(), core_off())
    assert result["triggers"] == []
    assert result["macro_targets"] == []
    assert result["candidates_dropped"] == []


def test_the_cycle_is_still_derived_not_trusted():
    result = cli.candidates(
        cli_payload(
            cycle={"cycle_type": "full", "entries_allowed": True},
            now="2026-08-03T20:30:00Z",
        ),
        core_off(),
    )
    assert result["cycle"]["cycle_type"] != "full"


# --- the monitor watchlist -------------------------------


def _monitor_payload(*, control=None, **overrides):
    """11:40 ET on the same Monday `cli_payload` defaults to, with a `full`
    cycle already recorded earlier that day so `cadence` doesn't promote
    this firing back to `full` (`src/cadence.py:254`)."""
    merged_control = {"last_full_cycle": "2026-08-03T13:40:00Z", **(control or {})}
    return cli_payload(
        now="2026-08-03T15:40:00Z",
        control=merged_control,
        **overrides,
    )


def test_monitor_reads_the_watchlist_instead_of_rediscovering():
    payload = _monitor_payload(
        control={
            "today_watchlist": {
                "trading_day": "2026-08-03",
                "triggers": [{"symbol": "TSLA", "play_id": "CATALYST"}],
                "macro_targets": ["XLE"],
            }
        },
        # If monitor discovered live instead of reading the watchlist, this
        # would produce its own (different) triggers/macro_targets.
        events={"AAPL": [{"type": "8-K", "items": ["2.02"], "sessions_ago": 1}]},
        macro={"releases": [{"series": "CPI", "surprise_pct": 0.5, "sessions_ago": 0}]},
    )
    result = cli.candidates(payload, core_off())

    assert result["cycle"]["cycle_type"] == "monitor"
    assert result["triggers"] == [{"symbol": "TSLA", "play_id": "CATALYST"}]
    assert result["macro_targets"] == ["XLE"]
    assert result["candidates_dropped"] == []
    assert "control_updates" not in result


def test_monitor_without_a_same_day_watchlist_is_empty_not_a_guess():
    """No `full` cycle has written today's watchlist yet (or it's still
    yesterday's) — degrade to positions-only rather than fall back to live
    discovery, per Absolute 7."""
    payload = _monitor_payload(
        control={
            "today_watchlist": {
                "trading_day": "2026-08-02",
                "triggers": [{"symbol": "TSLA", "play_id": "CATALYST"}],
                "macro_targets": ["XLE"],
            }
        }
    )
    result = cli.candidates(payload, core_off())

    assert result["triggers"] == []
    assert result["macro_targets"] == []
    assert result["candidates_dropped"] == []


def test_monitor_unions_a_reconcile_resubmit_onto_the_watchlist():
    """An entry `reconcile` cancelled unfilled last cycle
    (`control_updates.resubmit_candidates`) re-enters the trigger set so this
    cycle's `decide` re-prices and re-proposes it."""
    payload = _monitor_payload(
        control={
            "today_watchlist": {
                "trading_day": "2026-08-03",
                "triggers": [{"symbol": "TSLA", "play_id": "CATALYST"}],
                "macro_targets": [],
            },
            "resubmit_candidates": {
                "trading_day": "2026-08-03",
                "triggers": [{"symbol": "MU", "play_id": "DIP"}],
            },
        },
    )
    result = cli.candidates(payload, core_off())

    assert {"symbol": "MU", "play_id": "DIP"} in result["triggers"]
    assert {"symbol": "TSLA", "play_id": "CATALYST"} in result["triggers"]


def test_a_stale_resubmit_list_from_another_day_is_ignored():
    payload = _monitor_payload(
        control={
            "today_watchlist": {
                "trading_day": "2026-08-03", "triggers": [], "macro_targets": [],
            },
            "resubmit_candidates": {
                "trading_day": "2026-08-02",
                "triggers": [{"symbol": "MU", "play_id": "DIP"}],
            },
        },
    )
    result = cli.candidates(payload, core_off())

    assert result["triggers"] == []


def test_a_resubmit_that_duplicates_a_live_trigger_is_not_added_twice():
    payload = _monitor_payload(
        control={
            "today_watchlist": {
                "trading_day": "2026-08-03",
                "triggers": [{"symbol": "MU", "play_id": "DIP"}],
                "macro_targets": [],
            },
            "resubmit_candidates": {
                "trading_day": "2026-08-03",
                "triggers": [{"symbol": "MU", "play_id": "DIP"}],
            },
        },
    )
    result = cli.candidates(payload, core_off())

    assert result["triggers"] == [{"symbol": "MU", "play_id": "DIP"}]


def test_full_cycle_persists_todays_watchlist_for_monitor_to_read():
    payload = cli_payload(
        events={"AAPL": [{"type": "8-K", "items": ["2.02"], "sessions_ago": 1}]},
        macro={"releases": [{"series": "CPI", "surprise_pct": 0.5, "sessions_ago": 0}]},
    )
    result = cli.candidates(payload, core_off())

    assert result["cycle"]["cycle_type"] == "full"
    watchlist = result["control_updates"]["today_watchlist"]
    assert watchlist["trading_day"] == result["cycle"]["trading_day"]
    assert watchlist["triggers"] == result["triggers"]
    assert watchlist["macro_targets"] == result["macro_targets"]
