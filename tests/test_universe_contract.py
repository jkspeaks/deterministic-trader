"""universe.py's output is guard.py's input, and nothing translates between.

`guard.py` reads two fields off `ctx["universe"]` — `candidates` at its
Invariant 3 check and `snapshot_age_days` at its entry-permission check — and
both were written months before this module existed. These tests pin the
handover so a rename here fails loudly rather than silently emptying the fence:
an unrecognised key would make every candidate check fall through to "not in
the candidate set", which reads as a working system that never trades.
"""

import json

from src import guard, universe
from tests.factories import codes, context, order, universe_context


def resolved(**overrides) -> dict:
    return universe.resolve(universe_context(**overrides)).as_dict()


def guard_ctx(resolution: dict, **overrides) -> dict:
    """Guard's context with `universe` *replaced* by what this module emitted.

    Assignment, not `context(universe=...)`. The factory deep-merges nested
    dicts, so a resolution missing `candidates` entirely would inherit the
    factory's default `["F"]` and every assertion below would pass against a
    key that no longer exists — which is precisely the failure these tests are
    here to catch.
    """
    ctx = context(**overrides)
    ctx["universe"] = resolution
    return ctx


def held(*symbols: str) -> list[dict]:
    return [{"symbol": s, "quantity": 1.0} for s in symbols]


# --- the handover ----------------------------------------------------------


def test_a_resolved_universe_drops_into_guards_context():
    result = guard.evaluate([order()], guard_ctx(resolved()))

    assert guard.NOT_IN_CANDIDATE_SET not in codes(result, "F")
    assert guard.UNIVERSE_STALE not in codes(result, "F")


def test_guard_reads_the_candidate_set_under_the_name_universe_emits_it():
    """The key itself, asserted directly. Everything else in this file infers
    the handover from guard's behaviour, and behaviour has a default to fall
    back on; the name does not."""
    assert "candidates" in resolved()
    assert "snapshot_age_days" in resolved()


def test_guard_denies_the_symbol_universe_left_out():
    """GME never entered the candidate set, so the order guard sees is one no
    plan could have emitted."""
    ctx = guard_ctx(
        resolved(triggers=[{"symbol": "GME", "play_id": "DIP"}]),
        sectors={"GME": "Consumer Discretionary"},
    )

    result = guard.evaluate([order(symbol="GME")], ctx)

    assert guard.NOT_IN_CANDIDATE_SET in codes(result, "GME")


def test_both_modules_read_the_same_staleness_boundary():
    """Fourteen days is fresh to both, fifteen is stale to both. If these ever
    disagree, one module spends calls the other will refuse to act on."""
    for as_of, expected in (("2026-07-20", False), ("2026-07-19", True)):
        resolution = universe.resolve(universe_context(snapshot={"as_of": as_of}))
        denied = guard.evaluate([order()], guard_ctx(resolution.as_dict()))

        assert resolution.stale is expected
        assert (guard.UNIVERSE_STALE in codes(denied, "F")) is expected


def test_a_stale_universe_still_lets_a_held_position_be_sold():
    """§14 proceeds on positions only, and the whole point of proceeding is
    that the exit stays placeable."""
    ctx = guard_ctx(
        resolved(snapshot={"as_of": "2026-07-01"}, positions=held("F"), triggers=[]),
        positions=[{"symbol": "F", "quantity": 1.0, "cost_basis": 14.00}],
    )

    result = guard.evaluate([order(side="sell", type="market", limit_price=None)], ctx)

    assert guard.NOT_IN_CANDIDATE_SET not in codes(result, "F")


def test_the_sector_map_covers_every_candidate_guard_will_price():
    """Saves `get_equity_fundamentals` per candidate: the holdings file already
    said which sector each name is in, and §4.4's cap is the only consumer."""
    result = universe.resolve(
        universe_context(
            positions=held("PFE"),
            triggers=[{"symbol": "AAPL", "play_id": "PEAD"}],
            macro_targets=["XLE"],
        )
    )

    assert all(s in result.sectors for s in result.candidates)


def test_an_adopted_non_member_has_no_sector_and_says_so_by_omission():
    """The operator's off-index buy is not in any holdings file, so its sector
    has to come from Robinhood. Inventing one here would look like knowledge."""
    result = universe.resolve(universe_context(positions=held("GME"), triggers=[]))

    assert "GME" in result.candidates
    assert "GME" not in result.sectors


# --- Invariant 2 -----------------------------------------------------------


def test_the_output_is_json():
    """"Pure function from JSON to JSON" is only true if the output survives a
    round trip — this is what gets written to `runlog` before anything places."""
    result = resolved(positions=held("PFE"), macro_targets=["XLE"])

    assert json.loads(json.dumps(result)) == result
