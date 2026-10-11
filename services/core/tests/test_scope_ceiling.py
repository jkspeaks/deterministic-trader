"""The resolve ceiling.

An oversized `resolve` answer (149 symbols where 21-22 is normal) makes one
cycle take hours. The runner fetches one symbol's data at a time, so the size of
`resolve`'s answer *is* the cycle's cost.

Held under test:

* held positions and sector ETFs are never trimmed (Invariant 7);
* DIP-scan-only names go first, from the tail of the scan; event-driven names
  after, oldest first; a name with both kinds of trigger counts as event-driven;
* deterministic, and `decide` applies the identical trim so it never weighs a
  name the runner was not asked to fetch;
* the alert is fail-soft: the database may not know the title yet, and a notification
  must never be what fails a `resolve`.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from services.core.core import run as core
from services.core.core import steps
from services.core.tests.conftest import FakeSupabase, rows_from
from src import cli
from tests.factories import cli_payload

REPO = Path(__file__).resolve().parents[3]


def loaded(ceiling=40, cap=30):
    return {"config": {"resolve_max_symbols": ceiling, "dip_survivor_cap": cap}}


def dip(n, prefix="D"):
    return [{"symbol": f"{prefix}{i:03d}", "play_id": "DIP"} for i in range(n)]


def event(n, play="CATALYST", prefix="E"):
    return [{"symbol": f"{prefix}{i:03d}", "play_id": play} for i in range(n)]


def payload(held=5, events=0, dips=0, macro=1, extra_triggers=()):
    return {
        "positions": [{"symbol": f"H{i}"} for i in range(held)],
        "triggers": event(events) + dip(dips) + list(extra_triggers),
        "macro_targets": [{"symbol": f"XL{i}"} for i in range(macro)],
    }


def symbols_of(p):
    return set(core._symbols(p))


# -- _apply_scope_ceiling: the rules --------------------------------------------


def test_under_the_ceiling_nothing_is_touched():
    p = payload(events=4, dips=20)
    before = json.dumps(p, sort_keys=True)

    scope = core._apply_scope_ceiling(p, loaded(40))

    assert scope.trimmed == [] and not scope.mandatory_over
    assert json.dumps(p, sort_keys=True) == before


def test_dip_scan_names_go_first_from_the_tail_of_the_scan():
    p = payload(events=4, dips=60)  # 5 held + 1 macro + 4 events + 60 dip = 70

    scope = core._apply_scope_ceiling(p, loaded(40))

    assert scope.before == 70 and scope.after == 40
    kept_dip = [t["symbol"] for t in p["triggers"] if t["play_id"] == "DIP"]
    assert kept_dip == [f"D{i:03d}" for i in range(30)]  # the head of the scan survives
    assert [t["symbol"] for t in p["triggers"] if t["play_id"] == "CATALYST"] == [f"E{i:03d}" for i in range(4)]
    assert len(scope.trimmed) == 30 and scope.trimmed_dip == 30 and scope.trimmed_events == 0
    assert all("40-symbol resolve ceiling" in r["reason"] and "DIP scan only" in r["reason"] for r in scope.trimmed)


def test_event_names_are_trimmed_oldest_first_only_after_the_dip_names_are_gone():
    p = payload(held=5, events=30, dips=3, macro=1)  # 39 + macro... ceiling 20

    scope = core._apply_scope_ceiling(p, loaded(20))

    assert not any(t["play_id"] == "DIP" for t in p["triggers"])  # all 3 dip names went first
    kept_events = [t["symbol"] for t in p["triggers"]]
    assert kept_events == [f"E{i:03d}" for i in range(14)]  # newest (head) kept, oldest (tail) trimmed
    assert scope.after == 20 and scope.trimmed_dip == 3 and scope.trimmed_events == 16
    assert any("event trigger" in r["reason"] for r in scope.trimmed)


def test_a_name_with_both_an_event_and_a_scan_trigger_counts_as_event_driven():
    both = [{"symbol": "BOTH", "play_id": "CATALYST"}, {"symbol": "BOTH", "play_id": "DIP"}]
    p = payload(events=0, dips=45, extra_triggers=both)

    core._apply_scope_ceiling(p, loaded(40))

    assert "BOTH" in symbols_of(p), "a catalyst-backed name must outlast scan-only names"
    assert sum(1 for t in p["triggers"] if t["symbol"] == "BOTH") == 2  # kept whole


def test_held_positions_and_sector_etfs_are_never_trimmed():
    held_and_triggered = [{"symbol": "H0", "play_id": "DIP"}]
    p = payload(held=5, macro=3, dips=80, extra_triggers=held_and_triggered)

    core._apply_scope_ceiling(p, loaded(20))

    assert {f"H{i}" for i in range(5)} <= symbols_of(p)
    assert {f"XL{i}" for i in range(3)} <= symbols_of(p)
    assert any(t["symbol"] == "H0" for t in p["triggers"]), "a trigger on a held name keeps its row"


def test_the_core_sleeves_etf_is_never_trimmed():
    """Its trigger row is the last one in the list, which is where the trim
    starts. Trimmed, the sleeve would have no quote on exactly the busy days."""
    sleeve = [{"symbol": "SPYM", "play_id": "CORE"}]
    p = payload(held=5, macro=1, events=30, dips=60, extra_triggers=sleeve)

    core._apply_scope_ceiling(p, loaded(20))

    assert "SPYM" in symbols_of(p)
    assert len(symbols_of(p)) <= 20


def test_when_the_mandatory_set_alone_is_over_the_ceiling_nothing_is_trimmed():
    p = payload(held=45, macro=2, dips=10)

    scope = core._apply_scope_ceiling(p, loaded(40))

    assert scope.mandatory_over and scope.after > scope.ceiling
    assert symbols_of(p) >= {f"H{i}" for i in range(45)}
    assert scope.trimmed_dip == 10  # the scan names were trimmed; the mandatory ones cannot be


def test_the_trim_is_deterministic():
    a, b = payload(events=6, dips=70), payload(events=6, dips=70)

    sa, sb = core._apply_scope_ceiling(a, loaded(35)), core._apply_scope_ceiling(b, loaded(35))

    assert a == b and sa.trimmed == sb.trimmed


def test_the_ceiling_comes_from_config_and_defaults_to_fifty():
    p = payload(events=0, dips=60)
    core._apply_scope_ceiling(p, {"config": {}})
    assert len(symbols_of(p)) == 50


# -- resolve / decide end to end ------------------------------------------------


@pytest.fixture()
def wide_config(tmp_path):
    """A copy of `config/` with the survivor cap out of the way, so the *ceiling*
    is what the test exercises."""
    target = tmp_path / "config"
    shutil.copytree(REPO / "config", target)
    path = target / "config.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg["dip_survivor_cap"] = 500
    cfg["resolve_max_symbols"] = 40
    # The sleeve's ETF is one more protected symbol; this test counts the rest.
    cfg["core_sleeve_enabled"] = False
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return target


def wide_payload(n=186):
    return cli_payload(dip_survivors=[{"symbol": f"S{i:03d}", "play_id": "DIP"} for i in range(n)])


def run_phase(phase, p, config_dir=None, supabase=None):
    supabase = supabase or FakeSupabase(tables={"edge_cycle_input": rows_from(p, p["cycle_id"])})
    return core.run(p["cycle_id"], phase, supabase, config_dir), supabase


def test_resolve_trims_186_survivors_to_the_ceiling_and_keeps_the_book(wide_config):
    p = wide_payload()

    result, supabase = run_phase(core.RESOLVE, p, wide_config)

    assert result["status"] == core.STATUS_RESOLVED
    assert len(result["symbols"]) == 40
    assert "F" in result["symbols"] and "PFE" in result["symbols"]  # the held name and the event trigger

    alerts = [a for c in supabase.args("edge_alerts_enqueue") for a in c["p_alerts"]]
    assert [a["title"] for a in alerts] == ["Resolve scope trimmed"]
    assert "40" in alerts[0]["body"] and alerts[0]["cycle_id"] == p["cycle_id"]

    warns = [s for s in supabase.stamps if s[2] == steps.WARN]
    assert warns and warns[0][3]["scope"] == "trimmed" and warns[0][3]["ceiling"] == 40


def test_under_the_ceiling_resolve_is_silent():
    result, supabase = run_phase(core.RESOLVE, cli_payload())

    assert result["status"] == core.STATUS_RESOLVED
    assert supabase.args("edge_alerts_enqueue") == []
    assert not [s for s in supabase.stamps if s[2] == steps.WARN]


def test_the_default_cap_alone_keeps_186_survivors_under_the_ceiling():
    """The first line of defence: cap 30 + 2 baseline names = 32 symbols, no alert."""
    result, supabase = run_phase(core.RESOLVE, wide_payload())

    assert len(result["symbols"]) <= 50
    assert supabase.args("edge_alerts_enqueue") == []


def test_a_failing_alert_never_fails_resolve(wide_config):
    """The database may not know the title yet: `edge_alerts_enqueue` then raises 'title not
    canonical'. That must cost the alert, not the phase."""

    class Refuses(FakeSupabase):
        def rpc(self, function, params=None):
            if function == "edge_alerts_enqueue":
                raise RuntimeError("edge_alerts_enqueue: title not canonical: Resolve scope trimmed")
            return super().rpc(function, params)

    p = wide_payload()
    before = steps.STAMP_FAILURES
    result, _ = run_phase(
        core.RESOLVE, p, wide_config, Refuses(tables={"edge_cycle_input": rows_from(p, p["cycle_id"])})
    )

    assert result["status"] == core.STATUS_RESOLVED and len(result["symbols"]) == 40
    assert steps.STAMP_FAILURES > before


def test_decide_applies_the_same_trim_and_names_it_in_the_runlog(wide_config):
    p = wide_payload()
    resolved, _ = run_phase(core.RESOLVE, p, wide_config)

    decided, supabase = run_phase(core.DECIDE, p, wide_config)

    assert decided["status"] == core.STATUS_DECIDED
    row = next(r for r in supabase.args("edge_runlog_write") if r["p_command"] == "decide")
    dropped = row["p_stdout"]["candidates_dropped"]
    ceiling_rows = [d for d in dropped if "resolve ceiling" in d["reason"]]
    assert len(ceiling_rows) == 186 - (40 - 2)  # 186 scan names, of which 38 were kept
    # Never weighed: nothing `resolve` trimmed reaches decide's candidate set.
    trimmed = {d["symbol"] for d in ceiling_rows}
    assert trimmed.isdisjoint(set(resolved["symbols"]))
    assert trimmed.isdisjoint(set(row["p_stdout"]["universe"]["candidates"]))
    # decide raised no second alert; resolve already did.
    assert supabase.args("edge_alerts_enqueue") == []
