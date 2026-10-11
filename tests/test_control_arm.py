"""`src/control_arm.py` — which name stands in for a signal.

The arm is only worth having if two properties hold for ever: the draw is
reproducible (the nightly replay re-derives every row inside its window, so a
draw that moved would silently resample the experiment), and the stand-in
really is a non-signalling same-sector name.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import control_arm, snapshot

ROOT = Path(__file__).resolve().parents[1]
RECORDED = json.loads(
    (ROOT / "tests" / "fixtures" / "decide" / "full_cycles_2026-09-18_and_10-02.json")
    .read_text(encoding="utf-8")
)["2026-09-18"]
UNIVERSE = json.loads((ROOT / "data" / "universe.json").read_text(encoding="utf-8"))
SECTORS = {row["symbol"]: row["sector"] for row in UNIVERSE["cross_check"]}
MEMBERS = {row["symbol"] for row in UNIVERSE["holdings"]}

DAY = "2026-09-18"
SIGNALS = [c["symbol"] for c in RECORDED["entries"]["candidates"]]


def excluded() -> set[str]:
    # The recorded row keeps `pool` where a full decide row has
    # `universe.candidates`; `signalling` reads the latter.
    return snapshot.signalling({**RECORDED, "universe": {"candidates": RECORDED["pool"]}})


def draw(symbol: str, **kw) -> str | None:
    pool = control_arm.eligible(symbol, SECTORS, excluded(), members=MEMBERS)
    return control_arm.pick(symbol, DAY, pool, **kw)


# --- the recorded cohort -----------------------------------------------------


def test_the_recorded_cycle_ranked_the_cohort_this_test_is_about():
    assert sorted(SIGNALS) == ["INVH", "KIM", "RF", "RTX"]


@pytest.mark.parametrize("symbol", SIGNALS)
def test_each_signal_gets_a_same_sector_stand_in_that_did_not_signal(symbol):
    control = draw(symbol)

    assert control is not None and control != symbol
    assert SECTORS[control] == SECTORS[symbol]
    assert control in MEMBERS
    # Nothing the cycle's discovery, plays or book touched.
    assert control not in excluded()
    assert control not in RECORDED["pool"]
    assert control not in {row["symbol"] for row in RECORDED["book"]}


@pytest.mark.parametrize("symbol", SIGNALS)
def test_the_draw_is_the_same_every_time_it_is_asked(symbol):
    assert len({draw(symbol) for _ in range(5)}) == 1


def test_the_draw_does_not_depend_on_the_order_the_universe_arrives_in():
    shuffled = dict(reversed(list(SECTORS.items())))
    for symbol in SIGNALS:
        pool = control_arm.eligible(symbol, shuffled, excluded(), members=MEMBERS)
        assert control_arm.pick(symbol, DAY, pool) == draw(symbol)


def test_a_different_seed_day_or_draw_number_is_a_different_experiment():
    """Over the four signals, at least one stand-in moves under each change —
    the hash is doing something — while the default stays put."""
    base = [draw(s) for s in SIGNALS]

    def pool(symbol):
        return control_arm.eligible(symbol, SECTORS, excluded(), members=MEMBERS)

    assert [draw(s, seed=control_arm.SEED + 1) for s in SIGNALS] != base
    assert [draw(s, draw=1) for s in SIGNALS] != base
    assert [control_arm.pick(s, "2026-09-21", pool(s)) for s in SIGNALS] != base
    assert [draw(s) for s in SIGNALS] == base


# --- eligibility -------------------------------------------------------------

TOY = {"AAA": "Energy", "BBB": "Energy", "CCC": "Energy", "DDD": "Energy", "ZZZ": "Utilities"}


def test_only_the_signals_own_sector_and_never_the_signal_itself():
    assert control_arm.eligible("AAA", TOY, set()) == ["BBB", "CCC", "DDD"]


def test_a_name_the_cycle_touched_is_not_a_control():
    assert control_arm.eligible("AAA", TOY, {"BBB"}) == ["CCC", "DDD"]


def test_an_oversold_name_is_signalling_even_if_discovery_never_saw_it():
    signals = {"BBB": {"rsi": 28.0}, "CCC": {"rsi": 30.0}, "DDD": {"rsi": 30.1}}

    assert control_arm.eligible("AAA", TOY, set(), signals=signals) == ["DDD"]


def test_an_unknown_rsi_does_not_exclude():
    signals = {"BBB": {"rsi": None}, "CCC": {}, "DDD": {"rsi": "n/a"}}

    assert control_arm.eligible("AAA", TOY, set(), signals=signals) == ["BBB", "CCC", "DDD"]


def test_the_index_fence_keeps_out_what_is_only_bucketed_with_the_sector():
    assert control_arm.eligible("AAA", {**TOY, "XLE": "Energy"}, set(),
                                members={"AAA", "BBB", "CCC"}) == ["BBB", "CCC"]


def test_no_sector_or_nobody_left_is_no_control_rather_than_a_guess():
    assert control_arm.eligible("QQQQ", TOY, set()) == []
    assert control_arm.eligible("ZZZ", TOY, set()) == []
    assert control_arm.pick("ZZZ", DAY, []) is None
