"""The operator's sector rotation, `edge_control.active_sectors`.

The properties this file exists to hold, in the order they could cost money:

* `[]` (the seed) changes nothing anywhere — the golden files prove the rest
  of the suite byte-identical; these tests prove the helpers inert.
* held positions keep every exit and every protective order in any rotation,
  and get no new money in an inactive sector (operator ruling);
* the filter runs *before* both cost caps, so they are never spent on names
  the rotation drops anyway;
* a typo fails open and loud — an all-unmatched rotation filters nothing;
* the live-session path, the runner path and the shadow ledger merge DIP
  survivors in one function, so none of them can see a different pool.
"""

from __future__ import annotations

import copy
import sys

import pytest

from services.core.core import assemble as core_assemble
from services.core.core import run as core_run
from src import candidates, cli, guard, plays
from src import universe as universes
from src.dip_recovery import core as dip_core
from src.dip_recovery import evaluate as dip_evaluate
from src.dip_recovery import watchlist
from src.guard.rotation import _sector_rotation_denials
from src.plays.core import Candidate
from src.plays.entries import _rotation_suppressed
from tests import test_cli_golden as golden
from tests.dip_recovery_factories import (
    dip_recovery_context,
    held_position,
    pool_context,
    skip_codes,
)
from tests.dip_recovery_factories import (
    orders as dip_orders,
)
from tests.factories import cli_payload, core_off

LOADED = core_off()
ETFS = LOADED["etfs"]
SECTORS = {
    "XOM": "Energy",
    "JPM": "Financials",
    "AAPL": "Information Technology",
    "PFE": "Health Care",
    "SPY": "Broad Market",
    "XLF": "Financials",
}


# These tests predate an earlier retirement and shelving of the event plays
# and use them as they then ran. See `all_plays_live` in conftest.py.
pytestmark = pytest.mark.usefixtures("all_plays_live")


def rotation(active):
    return universes.sector_rotation(active, ETFS)


def reasons(result, symbol):
    return [row["reason"] for row in result["candidates_dropped"] if row["symbol"] == symbol]


def with_rotation(payload, active):
    payload = copy.deepcopy(payload)
    payload["control"] = {**(payload.get("control") or {}), "active_sectors": active}
    return payload


# --- the helpers -------------------------------------------------------------


@pytest.mark.parametrize("active", [[], None])
def test_empty_or_absent_is_inert(active):
    rot = rotation(active)
    rows = [{"symbol": s} for s in SECTORS]
    assert rot.filtering is False
    assert universes.rotation_filter(rows, SECTORS, rot) == (rows, [])


def test_matching_is_case_insensitive_and_takes_sleeve_tickers():
    rot = rotation(["  energy ", "XLF"])
    assert rot.active == {"Energy", "Financials"}
    assert rot.unmatched == ()


def test_an_excluded_sector_is_dropped_with_its_reason():
    kept, dropped = universes.rotation_filter(["XOM", "AAPL"], SECTORS, rotation(["Energy"]))
    assert kept == ["XOM"]
    assert dropped == [("AAPL", "Information Technology is outside the active sector rotation")]


def test_broad_market_survives_every_rotation():
    kept, _ = universes.rotation_filter(["SPY"], SECTORS, rotation(["Energy"]))
    assert kept == ["SPY"]


def test_an_unknown_sector_is_kept():
    # CBOE is the one live member the cross-check carries no sector for.
    kept, _ = universes.rotation_filter(["CBOE"], SECTORS, rotation(["Energy"]))
    assert kept == ["CBOE"]


def test_an_all_unmatched_rotation_filters_nothing_and_says_so():
    rot = rotation(["Semiconductors", "Finacials"])
    assert rot.filtering is False
    assert rot.unmatched == ("Finacials", "Semiconductors")
    kept, dropped = universes.rotation_filter(list(SECTORS), SECTORS, rot)
    assert kept == list(SECTORS) and dropped == []


def test_a_partly_unmatched_rotation_filters_on_what_matched_and_notes_the_rest():
    rot = rotation(["Energy", "Finacials"])
    assert rot.filtering is True
    assert rot.unmatched == ("Finacials",)
    kept, _ = universes.rotation_filter(["XOM", "JPM"], SECTORS, rot)
    assert kept == ["XOM"]


@pytest.mark.parametrize("value", [{"Energy": True}, 7])
def test_a_malformed_value_fails_open(value):
    rot = rotation(value)
    assert rot.filtering is False
    assert rot.unmatched


def test_a_bare_string_is_a_one_name_rotation():
    assert rotation("energy").active == {"Energy"}


def test_the_filter_is_pure():
    rows = [{"symbol": "XOM", "play_id": "PEAD"}, {"symbol": "AAPL", "play_id": "PEAD"}]
    before = copy.deepcopy(rows)
    universes.rotation_filter(rows, SECTORS, rotation(["Energy"]))
    assert rows == before


def test_the_vocabulary_is_the_sleeves_eleven_and_not_broad_market():
    rot = rotation([])
    assert rot.vocabulary == set(ETFS["sector"].values())
    assert len(rot.vocabulary) == 11
    assert "Broad Market" not in rot.vocabulary
    assert rotation(["Broad Market"]).filtering is False


# --- discovery: `candidates.assemble` / `cli.candidates` ---------------------


def _snapshot(rows):
    return {"as_of": "2026-08-02", "holdings": rows, "cross_check": rows}


def test_full_cycle_drops_an_excluded_trigger_before_the_fetch():
    payload = cli_payload(
        events={
            "AAPL": [{"type": "8-K", "items": ["2.02"], "sessions_ago": 1}],
            "PFE": [{"type": "8-K", "items": ["2.02"], "sessions_ago": 1}],
        }
    )
    result = cli.candidates(with_rotation(payload, ["Health Care"]), LOADED)

    assert {t["symbol"] for t in result["triggers"]} == {"PFE"}
    assert reasons(result, "AAPL") == [
        "Information Technology is outside the active sector rotation"
    ] * 2  # PEAD and CATALYST both fired on it


def test_the_rotation_runs_before_the_20_name_cap():
    """20 fresh Tech names outrank 20 slightly older Health Care ones. With
    Health Care active, the cap must be spent on Health Care — not on Tech
    names the rotation then throws away — and nothing is mislabelled as a cap
    casualty."""
    tech = [f"T{i:02d}" for i in range(20)]
    health = [f"H{i:02d}" for i in range(20)]
    rows = [{"symbol": s, "sector": "Information Technology"} for s in tech] + [
        {"symbol": s, "sector": "Health Care"} for s in health
    ]
    # Item 8.01, not 2.02: CATALYST only, so 20 Health Care names are exactly
    # 20 triggers and any cap casualty would be a Tech name spending a slot.
    events = {s: [{"type": "8-K", "items": ["8.01"], "sessions_ago": 1}] for s in tech}
    events.update({s: [{"type": "8-K", "items": ["8.01"], "sessions_ago": 2}] for s in health})
    payload = {"events": events, "snapshot": _snapshot(rows), "control": {"active_sectors": ["Health Care"]}}

    result = candidates.assemble(payload, LOADED)

    assert len(result["triggers"]) == candidates.CANDIDATE_CAP
    assert {t["symbol"] for t in result["triggers"]} <= set(health)
    assert not any("discovery cap" in row["reason"] for row in result["candidates_dropped"])
    assert all(
        "outside the active sector rotation" in row["reason"]
        for row in result["candidates_dropped"]
    )


def test_the_cap_still_reports_its_own_casualties_under_a_rotation():
    health = [f"H{i:02d}" for i in range(23)]
    rows = [{"symbol": s, "sector": "Health Care"} for s in health]
    events = {s: [{"type": "8-K", "items": ["2.02"], "sessions_ago": 1}] for s in health}
    payload = {"events": events, "snapshot": _snapshot(rows), "control": {"active_sectors": ["Health Care"]}}

    result = candidates.assemble(payload, LOADED)

    # 23 names x (PEAD + CATALYST) = 46 triggers, 20 kept.
    capped = [r for r in result["candidates_dropped"] if "discovery cap" in r["reason"]]
    assert len(capped) == 46 - candidates.CANDIDATE_CAP


def _cpi(surprise):
    return {"releases": [{"series": "CPI", "surprise_pct": surprise, "sessions_ago": 0}]}


def test_a_sector_etf_macro_target_is_scoped_like_its_sector():
    unfiltered = cli.candidates(cli_payload(macro=_cpi(0.5)), LOADED)["macro_targets"]
    assert unfiltered, "fixture assumption: a CPI surprise tilts into at least one ETF"
    keep = ETFS["sector"][unfiltered[0]]

    result = cli.candidates(with_rotation(cli_payload(macro=_cpi(0.5)), [keep]), LOADED)

    assert result["macro_targets"] == [
        s for s in unfiltered if ETFS["sector"][s] == keep
    ]
    for symbol in set(unfiltered) - set(result["macro_targets"]):
        assert reasons(result, symbol) == [
            f"{ETFS['sector'][symbol]} is outside the active sector rotation"
        ]


def _monitor(control, **overrides):
    return cli_payload(
        now="2026-08-03T15:40:00Z",
        control={"last_full_cycle": "2026-08-03T13:40:00Z", **control},
        **overrides,
    )


def test_monitor_refilters_the_watchlist_resubmits_and_macro_targets():
    """A narrowing made after 09:40 applies at 11:47 — to MACRO and PEAD/
    CATALYST read back off the watchlist, and to reconcile's resubmits."""
    payload = _monitor(
        {
            "active_sectors": ["Energy"],
            "today_watchlist": {
                "trading_day": "2026-08-03",
                "triggers": [
                    {"symbol": "AAPL", "play_id": "CATALYST"},
                    {"symbol": "XOM", "play_id": "PEAD"},
                ],
                "macro_targets": ["XLE", "XLF"],
            },
            "resubmit_candidates": {
                "trading_day": "2026-08-03",
                "triggers": [{"symbol": "PFE", "play_id": "DIP"}],
            },
        }
    )
    result = cli.candidates(payload, LOADED)

    assert result["cycle"]["cycle_type"] == "monitor"
    assert result["triggers"] == [{"symbol": "XOM", "play_id": "PEAD"}]
    assert result["macro_targets"] == ["XLE"]
    assert {r["symbol"] for r in result["candidates_dropped"]} == {"AAPL", "PFE", "XLF"}


@pytest.mark.parametrize("monitor", [False, True])
def test_dip_survivors_are_merged_and_rotated_on_every_cycle_type(monitor):
    survivors = [{"symbol": "XOM", "play_id": "DIP"}, "AAPL", {"symbol": "PFE", "play_id": "DIP"}]
    if monitor:
        payload = _monitor({"active_sectors": ["Energy", "Health Care"]}, dip_survivors=survivors)
    else:
        payload = with_rotation(cli_payload(dip_survivors=survivors), ["Energy", "Health Care"])

    result = cli.candidates(payload, LOADED)

    assert {"symbol": "XOM", "play_id": "DIP"} in result["triggers"]
    assert {"symbol": "PFE", "play_id": "DIP"} in result["triggers"]
    assert all(t["symbol"] != "AAPL" for t in result["triggers"])
    assert reasons(result, "AAPL") == ["Information Technology is outside the active sector rotation"]


def test_survivors_never_enter_the_persisted_watchlist():
    payload = cli_payload(dip_survivors=[{"symbol": "XOM", "play_id": "DIP"}])
    result = cli.candidates(payload, LOADED)

    assert {"symbol": "XOM", "play_id": "DIP"} in result["triggers"]
    saved = result["control_updates"][cli.WATCHLIST_KEY]["triggers"]
    assert {"symbol": "XOM", "play_id": "DIP"} not in saved


def test_a_survivor_discovery_already_found_is_not_duplicated():
    payload = cli_payload(
        events={"XOM": [{"type": "8-K", "items": ["2.02"], "sessions_ago": 1}]},
        dip_survivors=[{"symbol": "XOM", "play_id": "PEAD"}, {"symbol": "XOM", "play_id": "DIP"}, "XOM"],
    )
    triggers = cli.candidates(payload, LOADED)["triggers"]
    assert triggers.count({"symbol": "XOM", "play_id": "PEAD"}) == 1
    assert triggers.count({"symbol": "XOM", "play_id": "DIP"}) == 1


# --- the runner and the shadow ledger share one merge ------------------------


def test_dip_survivors_are_a_cycle_json_key_not_an_extra():
    cycle, extras = core_assemble.assemble(
        [{"kind": "dip_survivors", "symbol": None, "payload": [{"symbol": "XOM", "play_id": "DIP"}]}]
    )
    assert cycle["dip_survivors"] == [{"symbol": "XOM", "play_id": "DIP"}]
    assert "dip_survivors" not in extras


def test_the_runner_no_longer_carries_its_own_merge():
    # One merge, in `cli.candidates`: a second copy is how the shadow ledger
    # ends up replaying a different function from the one production runs.
    assert not hasattr(core_run, "_union_triggers")


# --- decide: new money blocked, exits untouched -------------------------------


def _decide(name, active):
    return cli.decide(with_rotation(golden.SCENARIOS[name], active), LOADED)


def test_decide_carries_the_rotation_note():
    assert _decide("quiet_full_cycle", [])["rotation"] == {
        "active_sectors": [], "active_sub_industries": [], "unmatched": [], "filtering": False,
        "sub_industry_data": False,
    }
    assert _decide("quiet_full_cycle", ["energy", "Finacials"])["rotation"] == {
        "active_sectors": ["Energy"], "active_sub_industries": [], "unmatched": ["Finacials"],
        "filtering": True, "sub_industry_data": False,
    }


def test_an_inactive_sector_entry_is_skipped_but_protection_is_unchanged():
    baseline = _decide("quiet_full_cycle", [])
    rotated = _decide("quiet_full_cycle", ["Energy"])

    assert [o["symbol"] for o in baseline["plan"]["orders"]] == ["PFE"]
    assert rotated["plan"]["orders"] == []
    assert rotated["plan"]["protection"] == baseline["plan"]["protection"]
    assert any(
        s["symbol"] == "PFE" and s["code"] == plays.SECTOR_INACTIVE
        for s in rotated["entries"]["skipped"]
    )


@pytest.mark.parametrize("name", ["day_one", "overnight_exit", "drawdown_flatten", "blocked_placement"])
def test_held_positions_exit_exactly_as_before_in_any_rotation(name):
    """Invariant 7. No sell, stop or target depends on the rotation."""
    baseline = _decide(name, [])
    rotated = _decide(name, ["Energy"])
    assert rotated["plan"] == baseline["plan"]


def test_an_all_unmatched_rotation_leaves_entries_working():
    assert _decide("quiet_full_cycle", ["Finacials"])["plan"] == _decide("quiet_full_cycle", [])["plan"]


def _candidate(symbol, play_id="PEAD"):
    return Candidate(
        symbol=symbol, play_id=play_id, conviction=0.7, conviction_raw=0.7, regime_fit=1.0,
        crowding_penalty=0.0, expected_holding_days=5, notional_multiplier=1.0, detail="",
    )


def test_a_top_up_on_a_held_inactive_name_gets_no_new_money():
    """`_rotation_suppressed` runs after top-ups and per-symbol plays are
    merged, so it sees every buy — a top-up on a held name included."""
    ctx = {"control": {"active_sectors": ["Energy"]}, "etfs": ETFS, "sectors": SECTORS}
    kept, skipped = _rotation_suppressed([_candidate("XOM"), _candidate("AAPL", "DIP")], ctx)

    assert [c.symbol for c in kept] == ["XOM"]
    assert [(s.symbol, s.code) for s in skipped] == [("AAPL", plays.SECTOR_INACTIVE)]


def test_the_guard_backstop_denies_the_buy_if_the_plays_gate_is_ever_missed(monkeypatch):
    """Disable the plays-side gate: guard alone must still refuse the buy and
    must still approve the protective sell."""
    # `src.plays.entries` the attribute is the re-exported function, so the
    # module itself is patched through `sys.modules`.
    monkeypatch.setattr(
        sys.modules["src.plays.entries"], "_rotation_suppressed",
        lambda candidates, ctx: (candidates, []),
    )
    result = _decide("quiet_full_cycle", ["Energy"])

    assert result["plan"]["orders"] == []
    assert [o["symbol"] for o in result["plan"]["protection"]] == ["F"]
    denied = result["guard"]["denied"]
    assert any(
        row["order"]["symbol"] == "PFE"
        and guard.SECTOR_INACTIVE in {d["code"] for d in row["denials"]}
        for row in denied
    )


def test_the_guard_backstop_ignores_sells_and_fails_open():
    ctx = {"control": {"active_sectors": ["Energy"]}, "etfs": ETFS, "sectors": SECTORS}
    assert _sector_rotation_denials({"symbol": "AAPL", "side": "buy"}, ctx)
    assert _sector_rotation_denials({"symbol": "SPY", "side": "buy"}, ctx) == []
    assert _sector_rotation_denials({"symbol": "CBOE", "side": "buy"}, ctx) == []
    inert = {**ctx, "control": {"active_sectors": []}}
    assert _sector_rotation_denials({"symbol": "AAPL", "side": "buy"}, inert) == []


# --- Dip Recovery ----------------------------------------------------------------


def test_pool_drops_an_inactive_sector_before_the_fetch_ceiling():
    members = ["T1", "T2", "T3", "H1", "H2"]
    sectors = {"T1": "Information Technology", "T2": "Information Technology",
               "T3": "Information Technology", "H1": "Health Care", "H2": "Health Care"}
    ctx = pool_context(
        scan=members,
        universe={"members": members, "stale": False, "snapshot_age_days": 1, "sectors": sectors},
        control={"active_sectors": ["Health Care"]},
        etfs=ETFS,
    )
    ctx["dip_recovery_config"]["universe_thresholds"]["max_scan_survivors"] = 2

    result = watchlist.pool(ctx)

    # Filtered first: the two-name ceiling is spent on H1/H2, not on T1/T2.
    assert result.fetch == ["H1", "H2"]
    assert skip_codes(result) == [dip_core.SECTOR_INACTIVE] * 3


def test_pool_is_unchanged_with_no_rotation():
    assert watchlist.pool(pool_context(etfs=ETFS, control={"active_sectors": []})).fetch == ["DIP1", "DIP2"]


def _dip_ctx(active, **overrides):
    return dip_recovery_context(
        control={"readable": True, "trading_enabled": True, "high_water_mark": 5000.00,
                 "active_sectors": active},
        etfs=ETFS,
        sectors={"DIP1": "Information Technology"},
        **overrides,
    )


def test_evaluate_skips_an_inactive_watchlist_symbol_without_scoring_it():
    assert dip_orders(dip_evaluate(_dip_ctx([])), "buy") == ["DIP1"]

    result = dip_evaluate(_dip_ctx(["Energy"]))

    assert dip_orders(result, "buy") == []
    assert skip_codes(result, "DIP1") == [dip_core.SECTOR_INACTIVE]
    assert result.evaluations == []


def test_a_held_dip_position_in_an_inactive_sector_is_not_averaged_down():
    held = {
        "dip_recovery_positions": {"DIP1": held_position()},
        "dip_recovery_watchlist": [],
        "quotes": {"DIP1": {"bid": 44.50, "ask": 44.55, "last": 44.50}},
        "dip_recovery_config": {"dip_recovery_enabled": True},
    }
    assert dip_orders(dip_evaluate(_dip_ctx([], **held)), "buy") == ["DIP1"]

    result = dip_evaluate(_dip_ctx(["Energy"], **held))

    assert dip_orders(result) == []
    assert result.position_updates["DIP1"]["state"] == dip_core.HOLDING
    assert skip_codes(result, "DIP1") == [dip_core.SECTOR_INACTIVE]


def test_a_held_dip_position_in_an_inactive_sector_still_exits():
    held = {
        "dip_recovery_positions": {"DIP1": held_position()},
        "dip_recovery_watchlist": [],
        "quotes": {"DIP1": {"bid": 48.50, "ask": 48.55, "last": 48.50}},
    }
    result = dip_evaluate(_dip_ctx(["Energy"], **held))
    assert dip_orders(result, "sell") == ["DIP1"]
    assert result.position_updates["DIP1"]["state"] == dip_core.EXIT_PENDING


def test_the_pool_command_passes_control_and_the_sleeve_through():
    """`cli.dip_recovery_pool` dropped `control` at one point, which would
    have left the pool filter silently inert on the live routine."""
    payload = cli_payload(
        scan=["XOM", "AAPL"],
        control={"active_sectors": ["Energy"]},
    )
    result = cli.dip_recovery_pool(payload, LOADED)
    assert result["fetch"] == ["XOM"]
    assert [(s["symbol"], s["code"]) for s in result["skipped"]] == [("AAPL", dip_core.SECTOR_INACTIVE)]


# --- the weekly vocabulary check (ci/check_sectors.py) ------------------------


def test_the_vocabulary_check_flags_a_renamed_sector():
    # Deliberately not run against `data/universe.json`: that file changes
    # weekly, and the workflow step (not this suite) is what watches it.
    from ci.check_sectors import unknown_sectors

    renamed = {"cross_check": [{"symbol": "AAPL", "sector": "Technology"},
                               {"symbol": "XOM", "sector": "Energy"}]}
    assert unknown_sectors(renamed, ETFS) == {"Technology": ["AAPL"]}
    assert unknown_sectors({"cross_check": [{"symbol": "XOM", "sector": "Energy"}]}, ETFS) == {}
