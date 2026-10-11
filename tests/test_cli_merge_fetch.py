"""This is a mechanical union of `{symbol: {...}}` fragments, one per
background-agent group, with no retyping possible."""

from src import cli
from tests.factories import cli_payload


def merged(**overrides) -> dict:
    return cli.merge_fetch(cli_payload(**overrides), cli.load_config())


def test_unions_disjoint_fragments():
    result = merged(
        fragments=[
            {"F": {"tradability": "tradable"}},
            {"PFE": {"tradability": "tradable"}},
        ]
    )
    assert result["merged"] == {
        "F": {"tradability": "tradable"},
        "PFE": {"tradability": "tradable"},
    }
    assert result["conflicts"] == []


def test_no_fragments_merges_to_empty():
    result = merged(fragments=[])
    assert result["merged"] == {}
    assert result["conflicts"] == []


def test_a_symbol_in_two_fragments_is_a_conflict_not_a_guess():
    """The groups were supposed to partition the symbol set. A symbol
    appearing twice means they overlapped — picking one fragment's value
    over the other would be a guess, so neither survives."""
    result = merged(
        fragments=[
            {"F": {"tradability": "tradable"}},
            {"F": {"tradability": "not_tradable"}},
            {"PFE": {"tradability": "tradable"}},
        ]
    )
    assert "F" not in result["merged"]
    assert result["merged"]["PFE"] == {"tradability": "tradable"}
    assert result["conflicts"] == ["F"]


def test_a_symbol_in_three_fragments_is_still_one_conflict_entry():
    result = merged(
        fragments=[
            {"F": {"tradability": "tradable"}},
            {"F": {"tradability": "not_tradable"}},
            {"F": {"tradability": "tradable"}},
        ]
    )
    assert "F" not in result["merged"]
    assert result["conflicts"] == ["F"]


def test_an_empty_fragment_contributes_nothing():
    result = merged(fragments=[{}, {"F": {"tradability": "tradable"}}, {}])
    assert result["merged"] == {"F": {"tradability": "tradable"}}
    assert result["conflicts"] == []


def test_the_cycle_is_still_derived_not_trusted():
    """Every command shares Invariant 6 — this one carries no cycle-gated
    behaviour of its own, but it still must not echo a forged cycle."""
    result = cli.merge_fetch(
        cli_payload(
            cycle={"cycle_type": "full", "entries_allowed": True},
            now="2026-08-03T20:30:00Z",
        ),
        cli.load_config(),
    )
    assert result["cycle"]["cycle_type"] != "full"
