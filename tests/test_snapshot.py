"""`src/snapshot.py` — the decision snapshot, from a recorded cycle.

That cycle bought nothing — which is the point of S0: every one of its 20 pool
names was rejected, and before this module none of the numbers they were
rejected on survived the cycle. The second half uses the synthetic payload for
the stages that cycle did not reach (an approved buy, a denial, an unsized
name).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import broker_shapes, cli, snapshot
from tests.factories import cli_payload

FIXTURE = Path(__file__).parent / "fixtures" / "decide" / "snapshot_inputs_2026-10-02.json"
ETFS = cli.load_config()["etfs"]


@pytest.fixture(scope="module")
def recorded() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def rows(recorded) -> list[dict]:
    payload = {
        # The runner's rows, through the same normaliser `run._payload` uses:
        # the snapshot must read `bid`/`ask`, not the broker's `bid_price`.
        "quotes": broker_shapes.quotes(recorded["quotes"]),
        "signals": recorded["signals"],
        "fundamentals": recorded["fundamentals"],
    }
    return snapshot.rows(payload, recorded["decide"], ETFS)


def one(rows, symbol, play_id):
    (row,) = [r for r in rows if r["symbol"] == symbol and r["play_id"] == play_id]
    return row


# --- the recorded cycle ------------------------------------------------------


def test_every_judged_name_is_a_row_including_the_ones_nobody_bought(rows, recorded):
    skipped = {(s["symbol"], s["play_id"]) for s in recorded["decide"]["entries"]["skipped"]}
    skipped.discard(("FOMC", "MACRO"))            # an event, not a security
    judged = {(r["symbol"], r["play_id"]) for r in rows if r["verdict"] == snapshot.SKIPPED}

    assert judged == skipped
    assert len(judged) == 30
    # The cycle ranked nothing, so no row got further than a skip or a drop.
    assert {r["verdict"] for r in rows} == {snapshot.SKIPPED, snapshot.DROPPED}


def test_one_row_per_symbol_and_play(rows):
    keys = [(r["cycle_id"], r["symbol"], r["play_id"]) for r in rows]

    assert len(keys) == len(set(keys))
    assert {r["cycle_id"] for r in rows} == {"498c4d88-cfe2-4de8-b220-970f723de131"}
    assert {r["trading_day"] for r in rows} == {"2026-10-02"}
    assert {r["decided_at"] for r in rows} == {"2026-10-02T13:41:40Z"}


def test_a_rejected_dip_carries_the_numbers_it_was_rejected_on(rows):
    """GIS: oversold at RSI 23 and 18% under its 200-day."""
    row = one(rows, "GIS", "DIP")

    assert (row["verdict"], row["reason_code"]) == ("SKIPPED", "ENTRY_CONDITIONS_UNMET")
    assert row["rsi"] == pytest.approx(23.36048765010915)
    assert row["sma_200"] == pytest.approx(38.87899997)
    assert row["atr"] == pytest.approx(1.132041606211855)
    # bid 31.81 / ask 31.84: the mid, and the spread as a fraction of it.
    assert row["price"] == pytest.approx(31.825)
    assert row["spread_pct"] == pytest.approx(0.03 / 31.825, abs=1e-6)
    assert row["atr_pct"] == pytest.approx(1.132041606211855 / 31.825, abs=1e-6)
    assert row["dist_sma_200"] == pytest.approx(31.825 / 38.87899997 - 1, abs=1e-6)
    assert row["dist_sma_200"] < -0.03                      # the falling knife
    assert row["average_volume"] == pytest.approx(12549171.841787)
    assert row["adv_usd"] == pytest.approx(12549171.841787 * 31.825, rel=1e-9)
    assert (row["sessions_since_report"], row["sessions_until_report"]) == (7, 52)


def test_the_sector_and_its_etfs_state_are_on_the_row(rows):
    row = one(rows, "GIS", "DIP")
    assert (row["sector"], row["sector_etf"], row["sector_state"]) == (
        "Consumer Staples", "XLP", "oversold_ok")

    held = one(rows, "KIM", "DIP")
    assert (held["sector"], held["sector_etf"], held["sector_state"]) == (
        "Real Estate", "XLRE", "breakdown")
    assert {r["market_state"] for r in rows} == {"risk_on"}


def test_a_figure_the_cycle_did_not_have_is_absent_not_zero(rows):
    """An earlier regime row predates P1: no posture, no breadth. And the runner
    fetched fundamentals for 15 of the 20 pool names."""
    assert {r["posture"] for r in rows} == {None}
    assert {r["breadth_200"] for r in rows} == {None}

    row = one(rows, "KIM", "DIP")                 # held: quoted, no fundamentals row
    assert row["price"] == pytest.approx(22.405)
    assert row["average_volume"] is None and row["adv_usd"] is None


def test_a_name_discovery_dropped_is_a_row_once_with_its_reason(rows):
    dropped = [r for r in rows if r["verdict"] == snapshot.DROPPED]

    assert all(r["play_id"] == snapshot.NO_PLAY for r in dropped)
    assert all(r["reason_code"] == snapshot.DISCOVERY_DROP for r in dropped)
    symbols = [r["symbol"] for r in dropped]
    assert symbols.count("ACN") == 1 and symbols.count("NKE") == 1     # listed twice each
    assert one(rows, "ACN", "NONE")["detail"].endswith("outside the active sector rotation")
    # Dropped before the fetch: no quote, but the cached daily indicators exist.
    regn = one(rows, "REGN", "NONE")
    assert regn["price"] is None and regn["spread_pct"] is None
    assert regn["rsi"] == pytest.approx(34.71523565734549)
    assert regn["sector_etf"] == "XLV"


def test_a_name_both_dropped_and_judged_is_only_the_judgement(rows):
    """MU and WDAY fell outside the 20-name cap on one trigger and were judged
    on another. The verdict is the more informative row."""
    for symbol in ("MU", "WDAY"):
        assert [r["verdict"] for r in rows if r["symbol"] == symbol] != []
        assert snapshot.DROPPED not in [r["verdict"] for r in rows if r["symbol"] == symbol]


def test_a_macro_event_is_not_a_security_and_gets_no_row(rows):
    assert "FOMC" not in {r["symbol"] for r in rows}
    # The ETFs MACRO weighed are securities, and are rows.
    assert one(rows, "XLE", "MACRO")["sector_etf"] == "XLE"


def test_the_rotation_skip_keeps_its_own_code(rows):
    assert one(rows, "SPG", "DIP")["reason_code"] == "SECTOR_INACTIVE"


def test_rows_are_json_and_nothing_is_placed_yet(rows):
    json.dumps(rows)
    assert {r["placed"] for r in rows} == {None}


def test_a_cycle_that_cannot_buy_records_no_selection(recorded):
    decided = {**recorded["decide"], "cycle": {**recorded["decide"]["cycle"], "entries_allowed": False}}

    assert snapshot.rows({}, decided, ETFS) == []


def test_the_recorded_cycles_names_are_what_the_control_arm_may_not_draw(recorded):
    names = snapshot.signalling(recorded["decide"])

    assert {"GIS", "MKC", "HPE"} <= names          # judged
    assert {"ACN", "REGN", "NKE"} <= names         # dropped
    assert {"INVH", "KIM", "RF", "RTX", "SPG"} <= names   # held


# --- the stages that cycle did not reach -------------------------------------


def decided_for(payload):
    return cli.decide(payload, cli.load_config())


def test_an_approved_buy_is_approved_and_ranked_first():
    payload = cli_payload()
    decided = decided_for(payload)
    rows = snapshot.rows(cli.normalized(payload, cli.load_config()), decided, ETFS)

    row = one(rows, "PFE", "DIP")
    assert (row["verdict"], row["reason_code"], row["rank"]) == ("APPROVED", None, 1)
    assert row["conviction"] is not None and row["rsi"] == pytest.approx(27.0)
    assert row["cycle_id"] == payload["cycle_id"] and row["trading_day"] == "2026-08-03"


def test_finalize_marks_only_approved_rows_with_what_the_broker_took():
    payload = cli_payload()
    rows = snapshot.rows(cli.normalized(payload, cli.load_config()), decided_for(payload), ETFS)

    taken = snapshot.with_placements(rows, [{"symbol": "PFE", "side": "buy"}])
    refused = snapshot.with_placements(rows, [])

    assert one(taken, "PFE", "DIP")["placed"] is True
    assert one(refused, "PFE", "DIP")["placed"] is False
    # "Not approved" stays unknown — it is not the same finding as "refused".
    assert all(r["placed"] is None for r in taken if r["verdict"] != snapshot.APPROVED)
    assert one(rows, "PFE", "DIP")["placed"] is None       # the input is not mutated


def stage(**changes) -> dict:
    """A minimal `decide` output with one ranked DIP candidate, AAA."""
    base = {
        "cycle_id": "c-1",
        "cycle": {"now": "2026-08-03T13:40:00Z", "cycle_type": "full",
                  "trading_day": "2026-08-03", "entries_allowed": True},
        "entries": {"candidates": [{"symbol": "AAA", "play_id": "DIP", "conviction": 0.4,
                                    "conviction_raw": 0.5, "detail": "oversold"}],
                    "skipped": []},
        "sizing": {"entries": [], "unsized": []},
        "guard": {"denied": []},
        "plan": {"orders": []},
        "sectors": {"AAA": "Energy"},
    }
    return {**base, **changes}


def test_each_stage_a_ranked_name_can_stop_at_has_its_own_verdict():
    ranked = snapshot.rows({}, stage(), ETFS)
    unsized = snapshot.rows({}, stage(sizing={"unsized": [{"symbol": "AAA", "reason": "NO_PRICE"}]}), ETFS)
    denied = snapshot.rows({}, stage(guard={"denied": [
        {"order": {"side": "buy", "symbol": "AAA"}, "denials": [{"code": "SECTOR_CAP"}, {"code": "X"}]}
    ]}), ETFS)
    approved = snapshot.rows({}, stage(plan={"orders": [{"side": "buy", "symbol": "AAA"}]}), ETFS)

    assert (ranked[0]["verdict"], ranked[0]["reason_code"]) == ("RANKED", None)
    assert (unsized[0]["verdict"], unsized[0]["reason_code"]) == ("UNSIZED", "NO_PRICE")
    assert (denied[0]["verdict"], denied[0]["reason_code"]) == ("DENIED", "SECTOR_CAP")
    assert (approved[0]["verdict"], approved[0]["reason_code"]) == ("APPROVED", None)


def test_a_pool_name_no_play_spoke_about_is_still_a_row():
    """DIP returns no skip for a scanner survivor that is simply not oversold
    any more. The name was fetched and weighed; it must not vanish."""
    decided = stage(universe={"candidates": ["AAA", "BBB"]}, sectors={"AAA": "Energy", "BBB": "Energy"})
    payload = {"signals": {"BBB": {"rsi": 55.0}}}

    rows = snapshot.rows(payload, decided, ETFS)

    quiet = one(rows, "BBB", "NONE")
    assert (quiet["verdict"], quiet["reason_code"]) == ("UNTRIGGERED", "NO_PLAY_VERDICT")
    assert quiet["rsi"] == 55.0
    assert [r["verdict"] for r in rows if r["symbol"] == "AAA"] == ["RANKED"]   # not doubled


def test_a_denied_sell_does_not_mark_a_buy_candidate_denied():
    rows = snapshot.rows({}, stage(guard={"denied": [
        {"order": {"side": "sell", "symbol": "AAA"}, "denials": [{"code": "PROTECTION_CONTENDS"}]}
    ]}), ETFS)

    assert rows[0]["verdict"] == "RANKED"


def test_a_plays_recorded_factors_ride_on_its_skip():
    decided = stage(entries={"candidates": [], "skipped": [
        {"symbol": "AAA", "play_id": "CATALYST", "code": "ENTRY_CONDITIONS_UNMET",
         "message": "tape", "factors": {"rvol_raw": 0.16, "rvol_tnorm": 2.1}}
    ]})

    (row,) = snapshot.rows({}, decided, ETFS)

    assert row["factors"] == {"rvol_raw": 0.16, "rvol_tnorm": 2.1}


def test_a_one_sided_quote_has_a_price_and_no_spread():
    payload = {"quotes": {"AAA": {"ask": 10.1, "last": 10.0}}}

    (row,) = snapshot.rows(payload, stage(), ETFS)

    assert row["price"] == 10.0 and row["spread_pct"] is None
