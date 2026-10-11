"""`edge_cycle_input` rows -> `cycle.json`.

The assembler is the hosted core's copy-verbatim step, so these are about the
two things a copier can get wrong: putting a value somewhere it does not
belong, and quietly accepting a key nobody will read.
"""

import pytest

from services.core.core.assemble import AssemblyError, assemble


def row(kind, payload, symbol=None):
    return {"kind": kind, "symbol": symbol, "payload": payload}


def test_a_whole_value_lands_under_its_own_key():
    cycle, extras = assemble([row("control", {"trading_enabled": True})])

    assert cycle == {"control": {"trading_enabled": True}}
    assert extras == {}


def test_per_symbol_rows_rebuild_the_map_a_batched_call_arrived_as():
    cycle, _ = assemble(
        [
            row("quotes", {"last": 14.88}, symbol="F"),
            row("quotes", {"last": 27.10}, symbol="PFE"),
        ]
    )

    assert cycle["quotes"] == {"F": {"last": 14.88}, "PFE": {"last": 27.10}}


def test_two_list_rows_for_one_key_concatenate_rather_than_replace():
    cycle, _ = assemble([row("orders", [{"id": 1}]), row("orders", [{"id": 2}])])

    assert cycle["orders"] == [{"id": 1}, {"id": 2}]


def test_two_object_rows_for_one_key_merge():
    cycle, _ = assemble(
        [row("day_context", {"F": {"rsi": 45}}), row("day_context", {"PFE": {"rsi": 27}})]
    )

    assert cycle["day_context"] == {"F": {"rsi": 45}, "PFE": {"rsi": 27}}


def test_post_placement_rows_never_enter_cycle_json():
    cycle, extras = assemble(
        [
            row("control", {"trading_enabled": True}),
            row("placements", [{"order_id": "abc"}]),
            row("placement_rejections", [{"order": {}, "detail": "no"}]),
            row("placement_alerts", [{"title": "t", "body": "b"}]),
        ]
    )

    assert set(cycle) == {"control"}
    assert set(extras) == {"placements", "placement_rejections", "placement_alerts"}


def test_an_unknown_kind_fails_the_cycle_rather_than_being_ignored():
    with pytest.raises(AssemblyError) as exc:
        assemble([row("sentiment_score", {"F": 0.8})])

    assert "Appendix D" in str(exc.value)


def test_a_key_the_core_derives_cannot_be_posted_by_the_runner():
    # `triggers` is `cli.candidates`' output merged with the DIP scanner's
    # survivors. A runner that posted its own would be deciding the candidate
    # set, which is Step 5's job and not the hands'.
    with pytest.raises(AssemblyError) as exc:
        assemble([row("triggers", [{"symbol": "PFE", "play_id": "DIP"}])])

    assert "derived by the core" in str(exc.value)


def test_a_list_key_that_then_arrives_per_symbol_is_refused_not_guessed_at():
    # `positions` is a list. A per-symbol row for it means the runner and the
    # core disagree about that key's shape, and there is no merge that is
    # obviously right -- so there is no merge.
    with pytest.raises(AssemblyError) as exc:
        assemble([row("positions", [{"symbol": "F"}]), row("positions", {"quantity": 1}, symbol="F")])

    assert "per-symbol" in str(exc.value)


def test_a_whole_map_and_a_later_per_symbol_row_for_the_same_key_do_merge():
    # Not the case above: both are maps keyed by symbol, and a second fetch
    # adding one name to a batch is the ordinary path, not a conflict.
    cycle, _ = assemble(
        [row("quotes", {"F": {"last": 1}}), row("quotes", {"last": 2}, symbol="PFE")]
    )

    assert cycle["quotes"] == {"F": {"last": 1}, "PFE": {"last": 2}}


def test_a_row_with_no_kind_is_refused():
    with pytest.raises(AssemblyError):
        assemble([{"kind": None, "symbol": None, "payload": {}}])


def test_two_enveloped_order_responses_concatenate_and_dedupe():
    """The runner posts `get_equity_orders` twice (open, then recent), each in
    the tool's own envelope. A dict-merge would let the second `data` replace
    the first and drop the resting GTC targets; an order both windows returned
    must appear once."""
    open_row = {"id": "a", "symbol": "INVH", "state": "confirmed"}
    recent_row = {"id": "b", "symbol": "KIM", "state": "filled"}

    cycle, _ = assemble(
        [
            row("orders", {"data": {"orders": [open_row]}}),
            row("orders", {"data": {"orders": [recent_row, open_row]}}),
        ]
    )

    assert cycle["orders"] == [open_row, recent_row]
