"""The DIP scanner's survivors get a ceiling.

`CANDIDATE_CAP` bounds PEAD/CATALYST events before the DIP merge; nothing
bounded the scan. 186 survivors -> 128 after rotation -> a 149-symbol `resolve`
and a 3 h 03 m cycle (21-22 symbols is normal).

What must hold, in the order it could cost money:

* the cap is applied **after** the sector rotation, so the slots
  are spent on sectors the operator wants and a name the rotation removed is
  never mislabelled "outside the cap";
* every name over the cap is *named*, with a reason, in `candidates_dropped`;
* A candidate the runner never fetched data for is **no entry** and changes
  nothing else — exits and protective orders are untouched.
"""

from __future__ import annotations

import copy

import pytest

from src import cli
from tests.factories import cli_payload

LOADED = cli.load_config()


def loaded_with(**config):
    return {**LOADED, "config": {**LOADED["config"], **config}}


def survivors(n, prefix="S"):
    return [{"symbol": f"{prefix}{i:03d}", "play_id": "DIP"} for i in range(n)]


def dip_symbols(result):
    return [t["symbol"] for t in result["triggers"] if t["play_id"] == "DIP"]


def cap_reasons(result):
    return [r for r in result["candidates_dropped"] if "DIP survivor cap" in r["reason"]]


def with_rotation(payload, active):
    payload = copy.deepcopy(payload)
    payload["control"] = {**(payload.get("control") or {}), "active_sectors": active}
    return payload


# -- the shipped defaults -------------------------------------------------------


def test_the_shipped_defaults():
    assert cli.dip_survivor_cap(LOADED) == 30
    assert cli.resolve_max_symbols(LOADED) == 50


def test_a_missing_key_falls_back_to_the_default():
    assert cli.dip_survivor_cap({"config": {}}) == cli.DEFAULT_DIP_SURVIVOR_CAP
    assert cli.resolve_max_symbols({}) == cli.DEFAULT_RESOLVE_MAX_SYMBOLS


# -- the cap ---------------------------------------------------------------------


def cached(rsi_by_symbol):
    """`payload["day_context"]` as the pre-market job leaves it: per symbol,
    `signals` with the daily RSI(14)."""
    return {s: {"signals": {"rsi": v}} for s, v in rsi_by_symbol.items()}


def test_186_survivors_without_cached_rsi_are_capped_in_scan_order_and_the_overflow_is_named():
    payload = cli_payload(dip_survivors=survivors(186))

    result = cli.candidates(payload, LOADED)

    assert dip_symbols(result) == [f"S{i:03d}" for i in range(30)]
    dropped = cap_reasons(result)
    assert [r["symbol"] for r in dropped] == [f"S{i:03d}" for i in range(30, 186)]
    assert all("30-name DIP survivor cap" in r["reason"] for r in dropped)


def test_the_cap_is_configurable():
    result = cli.candidates(cli_payload(dip_survivors=survivors(50)), loaded_with(dip_survivor_cap=5))

    assert dip_symbols(result) == [f"S{i:03d}" for i in range(5)]
    assert len(cap_reasons(result)) == 45


def test_under_the_cap_nothing_is_dropped():
    result = cli.candidates(cli_payload(dip_survivors=survivors(12)), LOADED)

    assert len(dip_symbols(result)) == 12
    assert cap_reasons(result) == []


def test_event_triggers_are_never_counted_against_the_scan_cap():
    """PFE is the baseline payload's event-discovered trigger; the DIP cap bounds
    the scanner's rows only."""
    baseline = cli.candidates(cli_payload(), LOADED)["triggers"]
    capped = cli.candidates(cli_payload(dip_survivors=survivors(100)), loaded_with(dip_survivor_cap=3))

    non_dip = [t for t in capped["triggers"] if t["play_id"] != "DIP"]
    assert non_dip == [t for t in baseline if t["play_id"] != "DIP"]


def test_the_cap_runs_after_the_rotation_not_before():
    """AAPL (Information Technology) is out of an Energy rotation. It is named as a
    *rotation* drop and never eats one of the cap's slots: the cap is spent on XOM
    and the unknown-sector names (an unknown sector is kept)."""
    rows = [{"symbol": "AAPL", "play_id": "DIP"}, {"symbol": "XOM", "play_id": "DIP"}] + survivors(40)
    payload = with_rotation(cli_payload(dip_survivors=rows), ["Energy"])

    result = cli.candidates(payload, loaded_with(dip_survivor_cap=5))

    assert dip_symbols(result) == ["XOM", "S000", "S001", "S002", "S003"]
    aapl = [r["reason"] for r in result["candidates_dropped"] if r["symbol"] == "AAPL"]
    assert aapl and "DIP survivor cap" not in aapl[0]
    assert len(cap_reasons(result)) == 36  # S004..S039


def test_survivors_still_never_enter_the_persisted_watchlist():
    result = cli.candidates(cli_payload(dip_survivors=survivors(50)), LOADED)

    saved = result["control_updates"][cli.WATCHLIST_KEY]["triggers"]
    assert not any(t["play_id"] == "DIP" and t["symbol"].startswith("S") for t in saved)


# -- validation ------------------------------------------------------------------


@pytest.mark.parametrize("key", ["dip_survivor_cap", "resolve_max_symbols"])
@pytest.mark.parametrize("bad", [0, -3, 1.5, "30", True, None])
def test_a_bad_ceiling_is_refused_not_defaulted(key, bad):
    """A typo must not fall back to the default and run for weeks as a policy
    nobody chose."""
    with pytest.raises(ValueError, match=key):
        cli._validate_config({**LOADED["config"], key: bad})


def test_a_good_ceiling_is_accepted():
    cli._validate_config({**LOADED["config"], "dip_survivor_cap": 1, "resolve_max_symbols": 500})


# -- fail-closed on missing candidate data --------------------------------------


def _unquoted_in_universe(payload):
    quoted = set(payload.get("quotes") or {})
    holdings = [h["symbol"] if isinstance(h, dict) else h for h in payload["snapshot"]["holdings"]]
    unquoted = [s for s in holdings if s not in quoted]
    assert unquoted, "the fixture changed: every universe name now has a quote"
    return unquoted


def test_a_candidate_with_no_live_data_is_no_entry_and_changes_nothing_else():
    """The runner may be told to stop fetching or a name may be trimmed by the
    resolve ceiling; either way `decide` must treat a candidate with no
    quote/tradability as no entry, never as an error and never as a reason to
    touch exits or protection."""
    baseline = cli.decide(cli_payload(), LOADED)

    payload = cli_payload()
    payload["dip_survivors"] = [{"symbol": s, "play_id": "DIP"} for s in _unquoted_in_universe(payload)]
    payload["dip_survivors"] += survivors(5)  # and names outside the universe altogether
    result = cli.decide(payload, LOADED)

    assert result["plan"] == baseline["plan"]  # orders, protection and cancels, byte for byte
    assert result["exits"] == baseline["exits"]
    assert result["blocked"] == baseline["blocked"]
    ordered = {o["symbol"] for o in result["plan"]["orders"]}
    assert ordered.isdisjoint({r["symbol"] for r in payload["dip_survivors"]})


# -- ranking: most oversold first ----------------------------------------------


def test_the_cap_keeps_the_most_oversold_names_not_the_first_in_scan_order():
    """Scan order is not a quality order. The pre-market job has already cached every
    screened name's daily RSI, so the cap can rank for free — the same RSI extremity
    `plays/dip.py` sizes on."""
    rows = survivors(10)  # S000..S009 in scan order
    rsi = {f"S{i:03d}": 30.0 - i for i in range(10)}  # S009 is the most oversold (RSI 21)
    payload = cli_payload(dip_survivors=rows, day_context=cached(rsi))

    result = cli.candidates(payload, loaded_with(dip_survivor_cap=3))

    assert dip_symbols(result) == ["S009", "S008", "S007"]
    dropped = {r["symbol"]: r["reason"] for r in cap_reasons(result)}
    assert set(dropped) == {f"S{i:03d}" for i in range(7)}
    assert dropped["S006"] == "outside the 3-name DIP survivor cap, RSI 24.0, ranked most oversold first"


def test_names_without_a_cached_rsi_rank_after_every_name_that_has_one_in_scan_order():
    rows = survivors(6)
    payload = cli_payload(
        dip_survivors=rows,
        day_context=cached({"S004": 28.0, "S005": 29.5}),  # S000-S003 have nothing cached
    )

    result = cli.candidates(payload, loaded_with(dip_survivor_cap=4))

    assert dip_symbols(result) == ["S004", "S005", "S000", "S001"]
    reasons = {r["symbol"]: r["reason"] for r in cap_reasons(result)}
    assert set(reasons) == {"S002", "S003"}
    assert all("no cached RSI" in r and "in scan order" in r for r in reasons.values())


def test_equal_rsi_keeps_the_scanners_order():
    rows = survivors(5)
    payload = cli_payload(dip_survivors=rows, day_context=cached({r["symbol"]: 27.0 for r in rows}))

    result = cli.candidates(payload, loaded_with(dip_survivor_cap=3))

    assert dip_symbols(result) == ["S000", "S001", "S002"]


def test_a_malformed_cached_rsi_is_treated_as_unknown_not_an_error():
    rows = survivors(4)
    day_context = {
        "S000": {"signals": {"rsi": "27"}},   # a string
        "S001": {"signals": {"rsi": True}},   # a bool
        "S002": {"signals": None},
        "S003": "not a mapping",
    }
    result = cli.candidates(cli_payload(dip_survivors=rows, day_context=day_context), loaded_with(dip_survivor_cap=2))

    assert dip_symbols(result) == ["S000", "S001"]  # all unknown -> scan order


def test_under_the_cap_nothing_is_reordered():
    rows = survivors(5)
    rsi = {f"S{i:03d}": 30.0 - i for i in range(5)}

    result = cli.candidates(cli_payload(dip_survivors=rows, day_context=cached(rsi)), LOADED)

    assert dip_symbols(result) == [f"S{i:03d}" for i in range(5)]  # the scanner's order, untouched
