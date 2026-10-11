"""plays.py's output is universe.py's, sizing.py's and guard.py's input.

Three seams, none of which has a translator:

* an entry candidate is a `universe.resolve` **trigger** and then a
  `sizing.size` candidate;
* a `MACRO` candidate is a **macro target**, which universe fences against the
  rotation set rather than the membership list;
* an exit is a `guard.evaluate` **order**.

Every one of them was written before this module existed, so a rename here
fails quietly rather than loudly — an unrecognised key makes guard's candidate
check fall through to "not in the candidate set", which reads as a working
system that never trades.

The last seam is the one worth stating plainly: guard denies and plays emits.
§6.5 exit 2 flattens the book on a drawdown breach and guard denies buys on the
same breach, so the flatten has to survive the layer that is halting over it.
Guard's docstring says it makes the breach an order denial rather than a halt
for exactly this reason, and the test below is that claim.
"""

import pytest
import json

from src import cadence, guard, plays, reconcile, sizing, universe
from tests.factories import (
    codes,
    context,
    entry,
    plays_context,
    position,
    universe_context,
)


# These tests predate an earlier retirement and shelving of the event plays
# and use them as they then ran. See `all_plays_live` in conftest.py.
pytestmark = pytest.mark.usefixtures("all_plays_live")


def triggers_from(result) -> list[dict]:
    """What `cli.py` hands universe: symbol and play_id, nothing reshaped."""
    return [c.as_dict() for c in result.candidates]


# --- entry candidates into universe ---------------------------------------


def test_a_candidate_is_a_universe_trigger_without_reshaping():
    """§5.3's fence reads `symbol` and reports `play_id` in its refusal note.
    universe deliberately does not reshape play output, so the dict plays emits
    is the dict universe is handed.
    """
    proposed = plays.entries(plays_context())

    ctx = universe_context()
    ctx["triggers"] = triggers_from(proposed)
    resolved = universe.resolve(ctx)

    assert "F" in resolved.candidates


def test_universe_names_the_play_when_it_turns_a_trigger_away():
    """The note is only useful if the key survives. A trigger whose `play_id`
    went missing produces "a play triggered on GME", which is true and useless.
    """
    ctx = plays_context(candidates=["GME"])
    ctx["signals"]["GME"] = dict(ctx["signals"]["F"])
    ctx["quotes"]["GME"] = dict(ctx["quotes"]["F"])
    ctx["earnings"]["GME"] = dict(ctx["earnings"]["F"])
    ctx["sectors"]["GME"] = "Consumer Discretionary"

    universe_ctx = universe_context()
    universe_ctx["triggers"] = triggers_from(plays.entries(ctx))
    resolved = universe.resolve(universe_ctx)

    note = next(n for n in resolved.notes if n.symbol == "GME")
    assert note.code == universe.TRIGGER_OUTSIDE_UNIVERSE
    assert plays.DIP in note.detail


def test_a_macro_candidate_goes_in_as_a_macro_target_not_a_trigger():
    """§6.4 expresses macro through the sleeve, and universe fences macro
    targets against the eleven-name rotation set rather than the 500-name
    membership list. Routed as a trigger, XLE would be admitted for the wrong
    reason and a target outside the sleeve would be admitted at all.
    """
    ctx = plays_context(candidates=[])
    ctx["macro"]["releases"] = [
        {"series": "CPI", "surprise_pct": 0.20, "sessions_ago": 0}
    ]
    proposed = plays.entries(ctx)

    universe_ctx = universe_context()
    universe_ctx["macro_targets"] = [
        c.symbol for c in proposed.candidates if c.play_id == plays.MACRO
    ]
    resolved = universe.resolve(universe_ctx)

    assert ["XLE", "XLF"] == [s for s in resolved.candidates if s.startswith("XL")]


def test_every_macro_target_the_map_can_produce_clears_the_rotation_fence():
    """The two files have to agree. `config/macro_map.json` naming a symbol
    outside `config/etfs.json`'s sector list would produce a candidate universe
    silently drops, and the play would look like it never fired."""
    ctx = universe_context()
    ctx["macro_targets"] = sorted(
        {
            symbol
            for series in plays_context()["macro_map"]["releases"].values()
            for key in ("above", "below")
            for symbol in series[key]
        }
    )
    resolved = universe.resolve(ctx)

    assert not [
        n for n in resolved.notes if n.code == universe.MACRO_TARGET_NOT_IN_ROTATION
    ]


# --- exits into guard -----------------------------------------------------


def leaving(**overrides) -> dict:
    ctx = plays_context()
    ctx["positions"] = [position(sessions_held=8, **overrides)]
    return ctx


def test_an_exit_is_a_guard_order_without_a_translator():
    exits = plays.exits(leaving()).exits

    ctx = context()
    ctx["positions"] = [{"symbol": "F", "quantity": 1.0, "market_value": 14.88}]
    result = guard.evaluate([e.as_order() for e in exits], ctx)

    assert [o["symbol"] for o in result.approved] == ["F"]


def test_an_exit_order_is_a_sell():
    """The one field plays knows and the `Exit` record has no reason to carry:
    every §6.5 outcome is a sale, so `as_order` supplies it rather than making
    `cli.py` remember."""
    order = plays.exits(leaving()).exits[0].as_order()

    assert order["side"] == "sell"
    assert order["type"] == "limit"
    assert order["play_id"] == plays.DIP


def test_the_drawdown_flatten_survives_the_layer_that_is_halting():
    """§6.5 exit 2 against guard's drawdown denial, in one call.

    A halt that blocked its own flatten would trap the account in the position
    it is halting over. Guard makes the breach an order denial precisely so
    this sell gets through, and this is the only place both halves run together.
    """
    ctx = leaving()
    ctx["control"]["high_water_mark"] = 400.00
    flatten = plays.exits(ctx).exits

    guard_ctx = context()
    guard_ctx["positions"] = [{"symbol": "F", "quantity": 1.0, "market_value": 14.88}]
    guard_ctx["control"]["high_water_mark"] = 400.00
    result = guard.evaluate([e.as_order() for e in flatten], guard_ctx)

    assert [e.code for e in flatten] == [plays.DRAWDOWN_FLATTEN]
    assert [o["symbol"] for o in result.approved] == ["F"]


def test_a_fractional_exit_outside_regular_hours_is_not_emitted_for_guard():
    """Outside regular hours a fractional exit has no order path: a fractional
    limit is rejected outright and a fractional market order only queues to the
    open. `plays.exits` emits nothing and lands it in `manual_required`, so
    there is nothing for guard to refuse (`FRACTIONAL_OUTSIDE_REGULAR_HOURS`
    stays unreachable from an exit, though guard still owns it for an entry).
    """
    ctx = leaving(quantity=0.4)
    ctx["cycle"] = {"cycle_type": "overnight", "session": "all_day_hours",
                    "now": "2026-08-04T00:30:00Z", "entries_allowed": False}
    result = plays.exits(ctx)

    guard_ctx = context()
    guard_ctx["cycle"] = dict(ctx["cycle"])
    guard_ctx["positions"] = [{"symbol": "F", "quantity": 0.4, "market_value": 5.95}]
    guard_result = guard.evaluate([e.as_order() for e in result.exits], guard_ctx)

    assert result.exits == []
    assert [m.symbol for m in result.manual_required] == ["F"]
    assert guard_result.approved == []


def test_a_fractional_exit_in_regular_hours_is_a_market_order_guard_approves():
    """The fractional book's exit path. `plays.exits` emits a `market` sell for a
    fractional lot in regular hours, `Exit.as_order` carries it to guard
    unchanged, and guard's legality check approves a fractional market sell in
    regular hours — the seam that turns a stop on a TXN-style fractional lot
    into a placed order instead of an alert.
    """
    ctx = leaving(symbol="TXN", quantity=0.070371, entry_price=278.95)
    ctx["quotes"]["TXN"] = {"bid": 250.0, "ask": 250.4, "last": 250.2}
    ctx["signals"]["TXN"] = {"atr": 5.0}  # stop at 278.95 - 2*5 = 268.95 > 250.2

    result = plays.exits(ctx)
    order = result.exits[0].as_order()

    assert (order["type"], order["limit_price"]) == ("market", None)

    guard_ctx = context()
    guard_ctx["positions"] = [{"symbol": "TXN", "quantity": 0.070371,
                               "market_value": 17.61}]
    guard_ctx["tradability"] = {"TXN": {"tradeable": True,
                                        "fractional_tradability": "tradable",
                                        "all_day_tradability": "tradable"}}
    guard_ctx["quotes"] = {"TXN": {"bid": 250.0, "ask": 250.4, "last": 250.2}}
    guard_result = guard.evaluate([order], guard_ctx)

    assert [o["symbol"] for o in guard_result.approved] == ["TXN"]


# --- entry candidates into sizing -----------------------------------------


def test_a_candidate_is_a_sizing_candidate_without_reshaping():
    proposed = plays.entries(plays_context()).candidates

    result = sizing.size([c.as_dict() for c in proposed], context())

    assert entry(result, "F").play_id == plays.DIP


def test_every_field_sizing_reads_off_a_candidate_is_one_plays_emits():
    """Named directly rather than inferred from behaviour, because behaviour
    has defaults to fall back on and a name does not. `conviction` missing
    reads as 0.0, and `notional_multiplier` missing reads as a full rung —
    both of which look like a working system."""
    emitted = plays.entries(plays_context()).candidates[0].as_dict()

    assert {
        "symbol",
        "play_id",
        "conviction",
        "expected_holding_days",
        "notional_multiplier",
    } <= set(emitted)


# --- Invariant 2 ----------------------------------------------------------


def test_both_outputs_are_json():
    """"Pure function from JSON to JSON" is only true if the output survives a
    round trip — this is what `cli.py` writes to `runlog` before anything is
    placed."""
    ctx = leaving()
    ctx["macro"]["releases"] = [
        {"series": "CPI", "surprise_pct": 0.20, "sessions_ago": 0}
    ]

    for payload in (plays.entries(ctx).as_dict(), plays.exits(ctx).as_dict()):
        assert json.loads(json.dumps(payload)) == payload


def test_the_session_count_is_cadences_and_not_a_second_one():
    """One calendar, one counting rule. `plays.py` asks how long ago an event
    landed and `reconcile.py` asks how long a position has been held; they are
    the same arithmetic, and two copies would disagree about a holiday week —
    surfacing as a play that triggers a day late while its time stop fires a
    day early. Pinned with `is`, so a re-declared identical copy still fails.
    """
    assert plays.sessions_between is cadence.sessions_between
    assert reconcile.sessions_between is cadence.sessions_between
