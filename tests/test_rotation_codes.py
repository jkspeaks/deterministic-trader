"""Rotation short codes: sectors, GICS sub-industries, baskets.

`config/rotation_codes.json` is the owner-approved table. These tests hold
its shape, keep its two copies (ops/verbs.json, docs page) honest, and prove
a sub-industry code filters the same way a sector does — on every path —
while failing open where the data is missing.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from ci.check_sectors import sub_industry_drift
from ci.fetch_universe import _wikipedia_cross_check
from src import cli
from src import universe as universes
from src.dip_recovery import core as dip_core
from src.dip_recovery import watchlist
from tests import test_cli_golden as golden
from tests.dip_recovery_factories import pool_context
from tests.factories import cli_payload, core_off

REPO = Path(__file__).resolve().parent.parent
LOADED = core_off()
CODES = LOADED["rotation_codes"]
ETFS = LOADED["etfs"]


# --- the table itself -----------------------------------------------------------


# These tests predate an earlier retirement and shelving of the event plays
# and use them as they then ran. See `all_plays_live` in conftest.py.
pytestmark = pytest.mark.usefixtures("all_plays_live")


def test_every_code_is_unique_short_and_upper_case():
    every = [*CODES["sectors"], *CODES["sub_industries"], *CODES["baskets"]]
    assert len(every) == len(set(every))
    assert all(re.fullmatch(r"[A-Z0-9]{2,4}", code) for code in every)
    assert not set(every) & set(ETFS["sector"]), "a code must not shadow a sleeve ticker"


def test_sector_codes_are_the_sleeves_eleven():
    assert set(CODES["sectors"].values()) == set(ETFS["sector"].values())


def test_every_sub_industry_belongs_to_a_known_sector_and_baskets_are_valid():
    sectors = set(CODES["sectors"].values())
    assert all(e["sector"] in sectors for e in CODES["sub_industries"].values())
    for basket in CODES["baskets"].values():
        assert basket["includes"] and all(p in CODES["sub_industries"] for p in basket["includes"])
    semi = {CODES["sub_industries"][p]["name"] for p in CODES["baskets"]["SEMI"]["includes"]}
    assert semi == {"Semiconductors", "Semiconductor Materials & Equipment"}


def test_the_docs_page_lists_every_code():
    page = (REPO / "docs" / "ROTATION_CODES.md").read_text(encoding="utf-8")
    for group in ("sectors", "sub_industries", "baskets"):
        for code in CODES[group]:
            assert f"`{code}`" in page, code
    for entry in CODES["sub_industries"].values():
        assert entry["name"] in page


# --- the weekly fetch keeps the sub-industry ------------------------------------

WIKITEXT = """{| class="wikitable sortable" id="constituents"
! Symbol !! Security !! GICS Sector !! GICS Sub-Industry !! Headquarters Location !! Date added !! CIK !! Founded
|-
| {{NyseSymbol|MMM}} || [[3M]] || Industrials || Industrial Conglomerates || [[Saint Paul, Minnesota]] || 1957-03-04 || 0000066740 || 1902
|-
| {{NasdaqSymbol|NVDA}} || [[Nvidia]] || Information Technology || Semiconductors || [[Santa Clara, California]] || 2001-11-30 || 0001045810 || 1993
|}"""


def test_the_wikipedia_parser_keeps_the_sub_industry():
    blob = json.dumps({"parse": {"wikitext": {"*": WIKITEXT}}}).encode()
    rows = {r["symbol"]: r for r in _wikipedia_cross_check(blob)}
    assert rows["NVDA"]["sub_industry"] == "Semiconductors"
    assert rows["MMM"]["sub_industry"] == "Industrial Conglomerates"
    assert rows["NVDA"]["sector"] == "Information Technology"
    assert rows["NVDA"]["cik"] == "0001045810"


def test_sub_industry_map_counts_members_only():
    snapshot = {
        "holdings": [{"symbol": "NVDA"}],
        "cross_check": [{"symbol": "NVDA", "sub_industry": "Semiconductors"},
                        {"symbol": "ZZZ", "sub_industry": "Semiconductors"}],
    }
    assert universes.sub_industry_map(snapshot) == {"NVDA": "Semiconductors"}


# --- the rotation reads codes ---------------------------------------------------

SUBS = {"NVDA": "Semiconductors", "AMAT": "Semiconductor Materials & Equipment",
        "MSFT": "Systems Software", "AAPL": "Technology Hardware, Storage & Peripherals",
        "DLR": "Data Center REITs", "JPM": "Diversified Banks"}
SECTORS = {"NVDA": "Information Technology", "AMAT": "Information Technology",
           "MSFT": "Information Technology", "AAPL": "Information Technology",
           "DLR": "Real Estate", "JPM": "Financials", "XLK": "Information Technology",
           "XLRE": "Real Estate", "SPY": "Broad Market", "NEWCO": "Information Technology"}


def kept(active, subs=SUBS):
    rotation = universes.sector_rotation(active, ETFS, CODES, subs)
    return sorted(s for s in SECTORS if rotation.inactive(s, SECTORS) is None)


def test_a_basket_selects_its_sub_industries_only():
    # NEWCO has no known sub-industry and is kept (fail open); SPY is never filtered.
    assert kept(["SEMI"]) == ["AMAT", "NEWCO", "NVDA", "SPY"]


def test_codes_names_and_tickers_mix():
    assert kept(["chip", "Data Center REITs", "FIN"]) == ["DLR", "JPM", "NEWCO", "NVDA", "SPY"]


def test_a_sector_code_is_the_whole_sector_including_its_etf():
    assert kept(["IT"]) == ["AAPL", "AMAT", "MSFT", "NEWCO", "NVDA", "SPY", "XLK"]


def test_a_sector_etf_needs_its_whole_sector():
    assert "XLRE" not in kept(["DC"])
    assert "XLRE" in kept(["RE"])


def test_without_sub_industry_data_sub_codes_fail_open_and_say_so():
    rotation = universes.sector_rotation(["SEMI"], ETFS, CODES, {})
    assert rotation.inactive("AAPL", SECTORS) is None
    assert rotation.as_dict()["sub_industry_data"] is False


def test_the_reason_names_the_sub_industry():
    rotation = universes.sector_rotation(["SEMI"], ETFS, CODES, SUBS)
    _, dropped = universes.rotation_filter(["AAPL"], SECTORS, rotation)
    assert dropped == [("AAPL", "Technology Hardware, Storage & Peripherals is outside the active sector rotation")]


# --- through the real pipeline --------------------------------------------------


def _with_subs(payload, subs, active):
    payload = copy.deepcopy(payload)
    snap = payload["snapshot"]
    for row in snap.get("cross_check") or []:
        if row["symbol"] in subs:
            row["sub_industry"] = subs[row["symbol"]]
    payload["control"] = {**payload["control"], "active_sectors": active}
    return payload


def test_discovery_filters_on_a_basket():
    payload = cli_payload(events={
        "AAPL": [{"type": "8-K", "items": ["2.02"], "sessions_ago": 1}],
        "PFE": [{"type": "8-K", "items": ["2.02"], "sessions_ago": 1}],
    })
    payload = _with_subs(payload, {"AAPL": "Technology Hardware, Storage & Peripherals",
                                   "PFE": "Pharmaceuticals"}, ["PHRM"])
    result = cli.candidates(payload, LOADED)
    assert {t["symbol"] for t in result["triggers"]} == {"PFE"}


@pytest.mark.parametrize("active,buys", [(["PHRM"], ["PFE"]), (["BIO"], []), (["HC"], ["PFE"])])
def test_the_entries_gate_reads_sub_industries(active, buys):
    payload = _with_subs(golden.SCENARIOS["quiet_full_cycle"], {"PFE": "Pharmaceuticals"}, active)
    result = cli.decide(payload, LOADED)
    assert [o["symbol"] for o in result["plan"]["orders"] if o["side"] == "buy"] == buys
    assert result["rotation"]["sub_industry_data"] is True


def test_the_dip_recovery_pool_reads_sub_industries():
    members = ["NVDA", "AAPL"]
    ctx = pool_context(
        scan=members,
        universe={"members": members, "stale": False, "snapshot_age_days": 1,
                  "sectors": {s: SECTORS[s] for s in members}},
        control={"active_sectors": ["SEMI"]},
        etfs=ETFS, rotation_codes=CODES, sub_industries=SUBS,
    )
    result = watchlist.pool(ctx)
    assert result.fetch == ["NVDA"]
    assert [(s.symbol, s.code) for s in result.skipped] == [("AAPL", dip_core.SECTOR_INACTIVE)]


# --- the weekly drift check -----------------------------------------------------


def _snap(*pairs):
    rows = [{"symbol": s, "sub_industry": sub} for s, sub in pairs]
    return {"holdings": [{"symbol": s} for s, _ in pairs], "cross_check": rows}


def test_drift_flags_uncoded_vanished_and_missing_sub_industries():
    coded = {row["name"] for row in CODES["sub_industries"].values()}
    everything = _snap(*((f"S{i}", name) for i, name in enumerate(sorted(coded))))
    assert sub_industry_drift(everything, CODES) == {"uncoded": [], "vanished": [], "missing": []}

    renamed = _snap(*((f"S{i}", "Quantum Computing" if name == "Semiconductors" else name)
                      for i, name in enumerate(sorted(coded))))
    drift = sub_industry_drift(renamed, CODES)
    assert drift["uncoded"] == ["Quantum Computing"]
    assert drift["vanished"] == ["Semiconductors"]

    assert sub_industry_drift({"holdings": [], "cross_check": []}, CODES)["missing"]
