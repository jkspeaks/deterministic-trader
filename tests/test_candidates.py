"""Step 5's coarse discovery (`src/candidates.py`), run without Robinhood.

DIP is deliberately absent from every case here — it has no local-file
discovery path, only a live scan the caller must merge in separately.
"""

import pytest
from src import candidates


# These tests predate an earlier retirement and shelving of the event plays
# and use them as they then ran. See `all_plays_live` in conftest.py.
pytestmark = pytest.mark.usefixtures("all_plays_live")


def events(**by_symbol):
    return by_symbol


def event(type="8-K", items=None, filed_day_ago=1, headline_count=0):
    # Mirrors ci/fetch_events.py's row shape: `filed_day` is an ISO date
    # string, `sessions_ago` is the derived int the caller actually windows
    # on. A fixture that set `filed_day` to a bare int (as this used to) is
    # not the shape production ever produces, and let a `filed_day`/`sessions_ago`
    # mix-up in src/candidates.py ship undetected.
    return {
        "type": type,
        "items": items or [],
        "filed_day": "2026-08-01",
        "sessions_ago": filed_day_ago,
        "headline_count": headline_count,
    }


# --- PEAD --------------------------------------------------------------


def test_pead_triggers_on_earnings_item_inside_window():
    result = candidates.assemble(
        {"events": events(AAPL=[event(items=["2.02"], filed_day_ago=2)])},
        {"macro_map": {}, "etfs": {"sector": {}}},
    )
    assert {"symbol": "AAPL", "play_id": "PEAD"} in result["triggers"]


def test_pead_ignores_a_report_outside_the_window():
    result = candidates.assemble(
        {"events": events(AAPL=[event(items=["2.02"], filed_day_ago=4)])},
        {"macro_map": {}, "etfs": {"sector": {}}},
    )
    assert result["triggers"] == []


def test_pead_ignores_day_zero_same_as_the_live_play():
    """PEAD_WINDOW_SESSIONS opens at 1, not 0 — the reaction is the entry
    condition, so there is nothing to confirm on the day it prints. (The
    event still qualifies for CATALYST, which has its own, wider window.)"""
    result = candidates.assemble(
        {"events": events(AAPL=[event(items=["2.02"], filed_day_ago=0)])},
        {"macro_map": {}, "etfs": {"sector": {}}},
    )
    assert {"symbol": "AAPL", "play_id": "PEAD"} not in result["triggers"]


def test_pead_requires_the_earnings_item_not_just_any_8k():
    """Still qualifies for CATALYST — any 8-K inside its own window does —
    but item 9.01 alone is not an earnings result, so PEAD stays silent."""
    result = candidates.assemble(
        {"events": events(AAPL=[event(items=["9.01"], filed_day_ago=1)])},
        {"macro_map": {}, "etfs": {"sector": {}}},
    )
    assert {"symbol": "AAPL", "play_id": "PEAD"} not in result["triggers"]


# --- CATALYST ------------------------------------------------------------


def test_catalyst_triggers_on_an_8k_inside_its_window():
    result = candidates.assemble(
        {"events": events(TSLA=[event(filed_day_ago=2)])},
        {"macro_map": {}, "etfs": {"sector": {}}},
    )
    assert {"symbol": "TSLA", "play_id": "CATALYST"} in result["triggers"]


def test_catalyst_triggers_on_a_headline_cluster_without_an_8k():
    result = candidates.assemble(
        {
            "events": events(
                TSLA=[event(type="news", filed_day_ago=1, headline_count=3)]
            )
        },
        {"macro_map": {}, "etfs": {"sector": {}}},
    )
    assert {"symbol": "TSLA", "play_id": "CATALYST"} in result["triggers"]


def test_catalyst_item_4_02_vetoes_the_symbol_entirely():
    result = candidates.assemble(
        {"events": events(TSLA=[event(items=["4.02"], filed_day_ago=1)])},
        {"macro_map": {}, "etfs": {"sector": {}}},
    )
    assert result["triggers"] == []


def test_catalyst_veto_holds_even_alongside_a_clean_event_same_symbol():
    result = candidates.assemble(
        {
            "events": events(
                TSLA=[
                    event(items=["4.02"], filed_day_ago=1),
                    event(items=[], filed_day_ago=1),
                ]
            )
        },
        {"macro_map": {}, "etfs": {"sector": {}}},
    )
    assert result["triggers"] == []


# --- MACRO -----------------------------------------------------------------


def test_macro_target_follows_the_surprise_direction():
    result = candidates.assemble(
        {
            "macro": {
                "releases": [
                    {
                        "series": "CPI",
                        "surprise_pct": 0.5,
                        "first_seen": "2026-08-01",
                        "sessions_ago": 1,
                    }
                ]
            }
        },
        {
            "macro_map": {"releases": {"CPI": {"above": ["XLE"], "below": ["XLK"]}}},
            "etfs": {"sector": {"XLE": "Energy", "XLK": "Technology"}},
        },
    )
    assert result["macro_targets"] == ["XLE"]


def test_macro_target_outside_the_sector_sleeve_is_dropped():
    result = candidates.assemble(
        {
            "macro": {
                "releases": [
                    {
                        "series": "CPI",
                        "surprise_pct": 0.5,
                        "first_seen": "2026-08-02",
                        "sessions_ago": 0,
                    }
                ]
            }
        },
        {
            "macro_map": {"releases": {"CPI": {"above": ["QQQ"], "below": []}}},
            "etfs": {"sector": {"XLE": "Energy"}},
        },
    )
    assert result["macro_targets"] == []


def test_macro_release_outside_its_window_is_ignored():
    result = candidates.assemble(
        {
            "macro": {
                "releases": [
                    {
                        "series": "CPI",
                        "surprise_pct": 0.5,
                        "first_seen": "2026-07-28",
                        "sessions_ago": 5,
                    }
                ]
            }
        },
        {
            "macro_map": {"releases": {"CPI": {"above": ["XLE"], "below": []}}},
            "etfs": {"sector": {"XLE": "Energy"}},
        },
    )
    assert result["macro_targets"] == []


# --- cap and drop ------------------------------------------------------


def test_triggers_beyond_the_cap_are_dropped_not_silently_lost():
    by_symbol = {
        f"SYM{i}": [event(filed_day_ago=1)] for i in range(candidates.CANDIDATE_CAP + 3)
    }
    result = candidates.assemble(
        {"events": by_symbol}, {"macro_map": {}, "etfs": {"sector": {}}}
    )
    assert len(result["triggers"]) == candidates.CANDIDATE_CAP
    assert len(result["candidates_dropped"]) == 3
    assert all("discovery cap" in row["reason"] for row in result["candidates_dropped"])


def test_more_recent_events_are_kept_over_older_ones_when_capped():
    by_symbol = {
        f"OLD{i}": [event(filed_day_ago=2)] for i in range(candidates.CANDIDATE_CAP)
    }
    by_symbol["FRESH"] = [event(filed_day_ago=1)]
    result = candidates.assemble(
        {"events": by_symbol}, {"macro_map": {}, "etfs": {"sector": {}}}
    )
    kept_symbols = {row["symbol"] for row in result["triggers"]}
    assert "FRESH" in kept_symbols
    # Read through the row, not against the list: `candidates_dropped` carries
    # `{"symbol", "reason"}` dicts, and a bare `in` on the list would pass
    # against any shape at all.
    assert "FRESH" not in {row["symbol"] for row in result["candidates_dropped"]}
