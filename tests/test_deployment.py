"""P3 of an earlier audit fixes: the deployment record.

One row per entry-capable cycle saying what it did with the cash and why —
including the cycles that did nothing. The funnel has to reconcile (each stage
no larger than the one before, and every triggered name accounted for), a
cycle with no proposals still has to produce a row, and none of it may change
the plan.
"""

import json
from pathlib import Path

import pytest

from src import cli, deployment, regime as regimes, report
from tests.factories import SECTOR_ETFS, cli_payload, regime_inputs, report_context

RECORDED = json.loads(
    (Path(__file__).parent / "fixtures" / "decide" / "full_cycles_2026-09-18_and_10-02.json")
    .read_text(encoding="utf-8")
)
LOADED = cli.load_config()
UNDER = {"last": 93.0, "sma200": 95.0}
_OTHERS = [s for s in SECTOR_ETFS if s != "XLV"]


# These tests predate an earlier retirement and shelving of the event plays
# and use them as they then ran. See `all_plays_live` in conftest.py.
pytestmark = pytest.mark.usefixtures("all_plays_live")


def with_config(**overrides):
    return {**LOADED, "config": {**LOADED["config"], **overrides}}


def recorded_funnel(day):
    row = RECORDED[day]
    # `members` was reduced to a count in the fixture. Every pool and dropped
    # name was a member on the day, so the list is rebuilt around them.
    known = sorted(set(row["pool"]) | {d["symbol"] for d in row["candidates_dropped"]})
    members = known + [f"M{i}" for i in range(row["members_count"] - len(known))]
    counts = deployment.funnel(
        members=members,
        pool=row["pool"],
        dropped=row["candidates_dropped"],
        entries=row["entries"],
        sized=row["sizing"]["entries"],
        approved=row["approved"],
    )
    return row, counts


def recorded_record(day, **kwargs):
    row, counts = recorded_funnel(day)
    return deployment.record(
        cycle_id=row["cycle_id"], cycle=row["cycle"], regime=kwargs.pop("regime", None),
        counts=counts, entries=row["entries"], sizing=row["sizing"], guard=row["guard"],
        account={"equity_total": row["sizing"]["ladder"]["equity_total"],
                 "cash": row["sizing"]["ladder"]["deployable"]},
        positions=row["book"], posture_enforced=kwargs.pop("posture_enforced", False),
    )


# --- the funnel reconciles, on recorded full cycles --------------------------


@pytest.mark.parametrize("day", ["2026-09-18", "2026-10-02"])
def test_every_stage_is_no_larger_than_the_one_before(day):
    _, counts = recorded_funnel(day)

    values = [counts[stage] for stage in deployment.STAGES]
    assert values == sorted(values, reverse=True), dict(zip(deployment.STAGES, values))


@pytest.mark.parametrize("day", ["2026-09-18", "2026-10-02"])
def test_skips_plus_proposals_equal_the_names_that_triggered(day):
    _, counts = recorded_funnel(day)

    assert counts["ranked"] + sum(counts["stopped"].values()) == counts["triggered"]
    assert counts["passed_plays"] == counts["triggered"] - counts["stopped"]["plays"]


def test_a_recorded_full_cycle_deployed():
    """36 names looked at, 31 triggered, four DIP buys ranked, sized, approved."""
    row, counts = recorded_funnel("2026-09-18")

    assert (counts["universe"], counts["pool"], counts["triggered"]) == (517, 36, 31)
    assert (counts["ranked"], counts["sized"], counts["approved"]) == (4, 4, 4)
    record = recorded_record("2026-09-18")
    assert (record["decision"], record["reason"]) == ("DEPLOY", "DEPLOYED")
    assert deployment.funnel_line(counts) == "517 > 36 > 31 > 4 > 4 > 4"
    # The denied *sell* on SPG is not a deployment fact.
    assert record["skip_counts"]["guard"] == {}


def test_a_recorded_cycle_that_held_cash_says_why():
    """A recorded day: 20 names fetched, every one a
    play looked at declined, nothing ranked, $607 deployable left idle."""
    _, counts = recorded_funnel("2026-10-02")

    assert (counts["universe"], counts["discovered"], counts["pool"]) == (518, 44, 20)
    assert counts["triggered"] == 20 and counts["ranked"] == 0
    assert counts["stopped"] == {"plays": 19, "regime": 0, "arbitration": 1}

    record = recorded_record("2026-10-02")
    assert (record["decision"], record["reason"]) == ("NONE", "NO_CANDIDATES_PASS")
    assert record["intended"] is True
    assert record["skip_counts"]["plays"] == {
        "ENTRY_CONDITIONS_UNMET": 29, "NO_CONVICTION": 1, "SECTOR_INACTIVE": 1,
    }
    assert record["invested_pct"] == pytest.approx(757.8 / 1392.0111, abs=1e-4)


def test_an_enforced_no_entry_posture_is_a_regime_block():
    regime = regimes.evaluate(
        regime_inputs(sectors={s: dict(UNDER) for s in SECTOR_ETFS[:9]}), LOADED["config"]
    ).as_dict()

    record = recorded_record("2026-10-02", regime=regime, posture_enforced=True)

    assert (record["decision"], record["reason"], record["intended"]) == (
        "NONE", "REGIME_BLOCK", True
    )
    assert record["posture"] == "NO_NEW_POSITIONS" and record["target_invested_pct"] == 0.0
    assert record["breadth_200"] == pytest.approx(2 / 11)
    assert "NO_NEW_POSITIONS" in deployment.summary_line(record)


# --- the decision, case by case ----------------------------------------------


def counts(**overrides):
    base = {"universe": 500, "discovered": 30, "pool": 20, "triggered": 10,
            "passed_plays": 4, "passed_regime": 4, "ranked": 4, "sized": 4,
            "approved": 4, "stopped": {"plays": 6, "regime": 0, "arbitration": 0}}
    return {**base, **overrides}


@pytest.mark.parametrize(
    "funnel, unsized, denied, expected",
    [
        (counts(), {}, {}, ("DEPLOY", "DEPLOYED")),
        (counts(sized=2, approved=2), {"NO_OPEN_SLOTS": 2}, {}, ("PARTIAL", "SLOT_CAP")),
        (counts(sized=3, approved=3), {"ONE_PER_SECTOR_DAY": 1}, {}, ("PARTIAL", "SECTOR_CAP")),
        (counts(approved=1), {}, {"WASH_SALE_COOLDOWN": 3}, ("PARTIAL", "GUARD_DENIED")),
        (counts(pool=0, triggered=0, ranked=0, sized=0, approved=0,
                stopped={"plays": 0, "regime": 0, "arbitration": 0}), {}, {},
         ("NONE", "NO_CANDIDATES")),
        (counts(ranked=0, sized=0, approved=0,
                stopped={"plays": 10, "regime": 0, "arbitration": 0}), {}, {},
         ("NONE", "NO_CANDIDATES_PASS")),
        (counts(ranked=0, sized=0, approved=0,
                stopped={"plays": 6, "regime": 4, "arbitration": 0}), {}, {},
         ("NONE", "REGIME_BLOCK")),
        (counts(triggered=4, ranked=0, sized=0, approved=0,
                stopped={"plays": 0, "regime": 0, "arbitration": 4}), {}, {},
         ("NONE", "ROTATION_BLOCK")),
        (counts(sized=0, approved=0), {"NO_OPEN_SLOTS": 4}, {}, ("NONE", "SLOT_CAP")),
        (counts(sized=0, approved=0), {"WHOLE_SHARE_ONLY": 3, "SHARE_LIMIT": 1}, {},
         ("NONE", "SHARE_LIMIT")),
        (counts(sized=0, approved=0), {"TARGET_INVESTED_REACHED": 4}, {},
         ("NONE", "REGIME_BLOCK")),
        (counts(sized=0, approved=0), {"REENTRY_BLOCKED": 4}, {}, ("NONE", "REENTRY_BLOCK")),
        (counts(sized=0, approved=0), {"NO_PRICE": 4}, {}, ("NONE", "DATA_STALE")),
        (counts(approved=0), {}, {"DRAWDOWN_HALT": 4}, ("NONE", "GUARD_DENIED")),
    ],
)
def test_the_decision_and_its_reason(funnel, unsized, denied, expected):
    decision, reason, detail = deployment._verdict(funnel, unsized, denied, None, False)

    assert (decision, reason) == expected
    assert detail


def test_only_a_regime_block_or_an_empty_field_is_an_intended_hold():
    assert deployment.INTENDED_REASONS == {
        "REGIME_BLOCK", "NO_CANDIDATES", "NO_CANDIDATES_PASS", "DEPLOYED"
    }


# --- placements complete the row ---------------------------------------------

BUY = {"symbol": "PFE", "side": "buy", "protective_stop": False, "protective_target": False}
TARGET = {"symbol": "F", "side": "sell", "protective_stop": False, "protective_target": True}


def test_placements_complete_the_row_without_changing_a_deployment():
    row = {"decision": "DEPLOY", "reason": "DEPLOYED", "intended": True,
           "funnel": counts(ranked=1, sized=1, approved=1)}

    final = deployment.with_placements(row, [BUY, TARGET], [])

    assert final["placed"] == 1 and final["funnel"]["placed"] == 1
    assert (final["decision"], final["reason"]) == ("DEPLOY", "DEPLOYED")
    assert deployment.funnel_line(final["funnel"]).endswith("> 1 > 1")
    assert row["funnel"].get("placed") is None        # the input is not mutated


@pytest.mark.parametrize("placed, expected", [([], "NONE"), ([BUY], "PARTIAL")])
def test_an_approved_buy_the_broker_did_not_take_is_an_exec_fail(placed, expected):
    row = {"decision": "DEPLOY", "reason": "DEPLOYED", "intended": True,
           "funnel": counts(ranked=2, sized=2, approved=2)}
    rejection = {"order": {"symbol": "KO", "side": "buy"}, "detail": "not enough buying power"}

    final = deployment.with_placements(row, placed, [rejection])

    assert (final["decision"], final["reason"], final["intended"]) == (expected, "EXEC_FAIL", False)


def test_a_cycle_that_planned_no_buy_stays_as_it_was():
    row = {"decision": "NONE", "reason": "REGIME_BLOCK", "intended": True,
           "funnel": counts(ranked=0, sized=0, approved=0)}

    final = deployment.with_placements(row, [TARGET], [])

    assert (final["decision"], final["reason"], final["placed"]) == ("NONE", "REGIME_BLOCK", 0)


# --- through decide ----------------------------------------------------------


def test_an_ordinary_cycle_records_a_deployment():
    result = cli.decide(cli_payload(), LOADED)

    row = result["deployment"]
    assert (row["decision"], row["reason"]) == ("DEPLOY", "DEPLOYED")
    assert row["cycle_id"] == result["cycle_id"]
    assert row["trading_day"] == "2026-08-03" and row["posture"] is None
    assert row["funnel"]["approved"] == 1
    assert row["shadow"]["sizing"]["rule"] == "sizing_v2"


def test_a_cycle_with_zero_proposals_still_records_none_with_a_reason():
    """PFE's trigger no longer fires: nothing is proposed, nothing is bought,
    and the row says so rather than not existing."""
    payload = cli_payload()
    payload["signals"]["PFE"]["rsi"] = 55.0

    result = cli.decide(payload, LOADED)

    assert [o for o in result["plan"]["orders"] if o["side"] == "buy"] == []
    row = result["deployment"]
    assert (row["decision"], row["reason"], row["intended"]) == ("NONE", "NO_CANDIDATES", True)
    assert row["detail"]


def test_idle_cash_under_no_new_positions_is_the_intended_outcome():
    tape = regime_inputs(sectors={s: dict(UNDER) for s in _OTHERS[:9]})
    for loaded in (with_config(regime_v2_enabled=True), with_config(sizing_v2_enabled=True)):
        result = cli.decide(cli_payload(regime_inputs=tape), loaded)

        assert [o for o in result["plan"]["orders"] if o["side"] == "buy"] == []
        row = result["deployment"]
        assert (row["decision"], row["reason"], row["intended"]) == (
            "NONE", "REGIME_BLOCK", True
        )
        assert row["posture"] == "NO_NEW_POSITIONS" and row["posture_enforced"] is True
        assert row["target_invested_pct"] == 0.0


def test_a_cycle_that_cannot_buy_records_no_deployment():
    overnight = cli.decide(
        cli_payload(now="2026-08-04T00:10:00Z",
                    control={"last_full_cycle": "2026-08-03T13:40:00Z"}), LOADED
    )

    assert overnight["cycle"]["entries_allowed"] is False
    assert overnight["deployment"] is None and overnight["shadow_sizing"] is None


def test_the_record_changes_nothing_in_the_plan():
    """Dropping the keys P3 added leaves the decide output P2 produced."""
    result = cli.decide(cli_payload(), LOADED)

    assert result["sizing_v2_enabled"] is False
    assert "v2" not in result["sizing"]
    assert [o["symbol"] for o in result["plan"]["orders"]] == ["PFE"]


def test_the_shadow_names_what_the_other_sizing_rule_would_have_bought():
    off = cli.decide(cli_payload(), LOADED)["shadow_sizing"]
    on = cli.decide(cli_payload(), with_config(sizing_v2_enabled=True))["shadow_sizing"]

    assert off["rule"] == "sizing_v2" and on["rule"] == "legacy"
    assert [e["symbol"] for e in on["entries"]] == ["PFE"]
    assert on["notional"] == pytest.approx(on["entries"][0]["notional"])


# --- dead plays --------------------------------------------------------------

PLAYS = ["DIP", "PEAD", "CATALYST", "MACRO"]


def step(state, alive, day, threshold=3):
    return deployment.play_activity(
        state, plays=PLAYS, alive=set(alive), trading_day=day, threshold=threshold
    )


def test_a_play_with_nothing_but_skips_for_n_days_is_named_once():
    state, dead = step(None, {"DIP"}, "2026-10-05")
    assert dead == [] and state["CATALYST"]["streak"] == 1 and state["DIP"]["streak"] == 0

    state, dead = step(state, {"DIP"}, "2026-10-06")
    assert dead == []
    state, dead = step(state, {"DIP"}, "2026-10-07")
    assert sorted(d["play_id"] for d in dead) == ["CATALYST", "MACRO", "PEAD"]
    assert dead[0]["days"] == 3

    # The streak runs on; the alert does not repeat.
    state, dead = step(state, {"DIP"}, "2026-10-08")
    assert dead == [] and state["CATALYST"]["streak"] == 4


def test_the_streak_counts_trading_days_not_cycles():
    state, _ = step(None, set(), "2026-10-05")
    state, dead = step(state, set(), "2026-10-05")
    state, dead = step(state, set(), "2026-10-05")

    assert state["DIP"]["streak"] == 1 and dead == []


def test_firing_resets_the_streak_even_later_the_same_day():
    state, _ = step(None, set(), "2026-10-05")
    state, _ = step(state, set(), "2026-10-06")
    state, _ = step(state, {"CATALYST"}, "2026-10-06")
    assert state["CATALYST"]["streak"] == 0

    # ...and a later quiet cycle that day does not start a new streak.
    state, _ = step(state, set(), "2026-10-06")
    assert state["CATALYST"]["streak"] == 0 and state["DIP"]["streak"] == 2


def test_a_play_that_is_switched_off_is_not_tracked():
    state, _ = deployment.play_activity(
        {"SURGE": {"streak": 9}}, plays=["DIP"], alive=set(),
        trading_day="2026-10-05", threshold=3,
    )

    assert set(state) == {"DIP"}


def test_decide_advances_the_streaks_and_persists_them_in_control():
    payload = cli_payload()
    payload["control"]["play_activity"] = {
        "CATALYST": {"streak": 4, "streak_day": "2026-07-31", "alive_day": None}
    }

    result = cli.decide(payload, LOADED)

    activity = result["control_updates"]["play_activity"]
    assert activity["DIP"]["streak"] == 0 and activity["DIP"]["alive_day"] == "2026-08-03"
    assert activity["CATALYST"]["streak"] == 5
    assert result["dead_plays"] == [{"play_id": "CATALYST", "days": 5}]


def test_a_play_the_operator_darked_is_not_reported_dead():
    payload = cli_payload()
    payload["play_state"]["CATALYST"] = {"regime_fit": 0.0, "reason": "off this week"}
    payload["control"]["play_activity"] = {"CATALYST": {"streak": 4}}

    result = cli.decide(payload, LOADED)

    assert "CATALYST" not in result["control_updates"]["play_activity"]
    assert result["dead_plays"] == []


def test_surge_is_tracked_only_while_full_send_is_on():
    off = cli.decide(cli_payload(), LOADED)["control_updates"]["play_activity"]
    payload = cli_payload()
    payload["control"]["full_send_enabled"] = True
    on = cli.decide(payload, LOADED)["control_updates"]["play_activity"]

    assert "SURGE" not in off and "SURGE" in on


# --- the status alert --------------------------------------------------------


def alert_body(**overrides):
    result = report.run(report_context(**overrides))
    return "\n".join(a.body for a in result.alerts)


def test_the_status_alert_carries_one_deployment_line():
    row = recorded_record("2026-10-02")

    body = alert_body(deployment=row)

    assert body.count("Deployment:") == 1
    assert "Deployment: no regime, breadth n/a · funnel 518 > 20 > 20 > 0 > 0 > 0 · NONE (NO_CANDIDATES_PASS)" in body


def test_a_cycle_with_no_deployment_says_nothing_about_one():
    assert "Deployment" not in alert_body()


def test_a_failed_write_is_named_in_the_alert():
    body = alert_body(deployment_write_error="RuntimeError: relation does not exist")

    assert "Deployment record not written: RuntimeError: relation does not exist" in body


def test_a_dead_play_is_one_line():
    body = alert_body(dead_plays=[{"play_id": "CATALYST", "days": 5}])

    assert body.count("Dead play:") == 1
    assert "CATALYST has produced no entry candidate for 5 consecutive trading days" in body
