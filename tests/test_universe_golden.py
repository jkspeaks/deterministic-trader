"""Four cycles recorded whole (§15).

The unit tests each pin one rule; these pin the shape of what comes out when
the rules run together, which is what `cli.py` writes to `runlog` before
anything is placed. A golden file is the cheapest way to notice that a change
to the merge quietly moved a name in or out of the fence.

The four are chosen to be the states that actually recur:

* `ordinary_day` — two positions, two triggers, a live rotation. The shape of
  a normal 09:40 full cycle.
* `disputed_sources` — the issuer file and the cross-check disagree three
  different ways at once, and membership is unmoved by all three.
* `stale_snapshot` — CI has been down a month; §14 narrows to positions.
* `adopted_manual_buy` — the operator bought something off-index in the app.
"""

import json
from pathlib import Path

import pytest

from src import universe
from tests.factories import universe_context

GOLDEN = Path(__file__).parent / "golden" / "universe"

# A wider snapshot than the unit-test baseline: eight constituents across six
# sectors, so a cap bucket in the output has more than one name to hold.
HOLDINGS = [
    {"symbol": "AAPL", "sector": "Information Technology"},
    {"symbol": "BAC", "sector": "Financials"},
    {"symbol": "CAT", "sector": "Industrials"},
    {"symbol": "F", "sector": "Consumer Discretionary"},
    {"symbol": "MRK", "sector": "Health Care"},
    {"symbol": "NVDA", "sector": "Information Technology"},
    {"symbol": "PFE", "sector": "Health Care"},
    {"symbol": "XOM", "sector": "Energy"},
]


def snapshot(**overrides) -> dict:
    return {"as_of": "2026-08-02", "holdings": HOLDINGS, "cross_check": HOLDINGS, **overrides}


def held(*symbols: str) -> list[dict]:
    return [{"symbol": s, "quantity": 1.0} for s in symbols]


SCENARIOS = {
    "ordinary_day": universe_context(
        snapshot=snapshot(),
        positions=held("F", "PFE"),
        triggers=[
            {"symbol": "AAPL", "play_id": "PEAD"},
            {"symbol": "XOM", "play_id": "DIP"},
        ],
        macro_targets=["XLE", "XLF"],
    ),
    "disputed_sources": universe_context(
        snapshot=snapshot(
            cross_check=[
                # CAT is absent entirely — membership stands on the issuer file.
                {"symbol": "AAPL", "sector": "Information Technology"},
                {"symbol": "BAC", "sector": "Financials"},
                {"symbol": "F", "sector": "Industrials"},  # sector disputed
                {"symbol": "MRK", "sector": "Health Care"},
                {"symbol": "NVDA", "sector": "Information Technology"},
                {"symbol": "PFE", "sector": "Health Care"},
                {"symbol": "XOM", "sector": "Energy"},
                {"symbol": "ENRN", "sector": "Energy"},  # cross-check only
            ]
        ),
        triggers=[
            {"symbol": "CAT", "play_id": "DIP"},
            {"symbol": "ENRN", "play_id": "DIP"},
        ],
    ),
    "stale_snapshot": universe_context(
        snapshot=snapshot(as_of="2026-07-01"),
        positions=held("F", "PFE"),
        triggers=[{"symbol": "AAPL", "play_id": "PEAD"}],
        macro_targets=["XLE"],
    ),
    "adopted_manual_buy": universe_context(
        snapshot=snapshot(),
        positions=held("GME"),
        triggers=[{"symbol": "F", "play_id": "DIP"}],
    ),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_the_cycle_matches_its_golden_file(name):
    expected = json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))

    assert universe.resolve(SCENARIOS[name]).as_dict() == expected


# --- the properties the golden files are there to protect ------------------


def test_an_ordinary_day_reasons_about_six_of_twenty_three_members():
    """§5.3's economics, as a number. Universe size is free; this is the part
    that costs a Robinhood call per name, every cycle."""
    result = universe.resolve(SCENARIOS["ordinary_day"])

    assert result.candidates == ["AAPL", "F", "PFE", "XLE", "XLF", "XOM"]
    assert len(result.members) == 23


MERGE_CODES = (
    universe.CROSS_CHECK_MISSING,
    universe.NOT_IN_ISSUER_FILE,
    universe.SECTOR_DISAGREEMENT,
)


def test_three_disagreements_move_no_names():
    """CAT missing from the cross-check, F's sector disputed, ENRN listed only
    there — all three logged, membership identical to the agreeing case."""
    disputed = universe.resolve(SCENARIOS["disputed_sources"])
    agreed = universe.resolve(universe_context(snapshot=snapshot(), triggers=[]))

    assert disputed.members == agreed.members
    assert len([n for n in disputed.notes if n.code in MERGE_CODES]) == 3


def test_a_cross_check_only_name_a_play_triggered_on_is_logged_twice():
    """Once by the merge for not being in the issuer file, once by the fence
    for the play that tried to trade it. They are different facts: the first
    is a data-source discrepancy, the second is a play reaching outside the
    universe, and a reader chasing either one wants both."""
    notes = [n for n in universe.resolve(SCENARIOS["disputed_sources"]).notes
             if n.symbol == "ENRN"]

    assert sorted(n.code for n in notes) == [
        universe.NOT_IN_ISSUER_FILE,
        universe.TRIGGER_OUTSIDE_UNIVERSE,
    ]


def test_the_disputed_cycle_still_admits_the_name_only_the_issuer_listed():
    """CAT triggered and is a member on the issuer file's word alone."""
    assert "CAT" in universe.resolve(SCENARIOS["disputed_sources"]).candidates


def test_a_month_of_dead_ci_leaves_exactly_the_positions():
    result = universe.resolve(SCENARIOS["stale_snapshot"])

    assert result.stale
    assert result.candidates == ["F", "PFE"]
