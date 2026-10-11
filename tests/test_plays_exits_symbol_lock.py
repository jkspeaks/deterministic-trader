"""plays.exits — Dip Recovery plan phase 2, defense in depth.

`guard.lock` is the hard gate against a sell crossing the ownership boundary
and denies it regardless of what runs here. This is the softer half: keeping
core's own exit loop from proposing a sell on a symbol the lock says belongs
to Dip Recovery in the first place, so a locked-away symbol never generates a
denial or an alert about an exit that was never going to be allowed to place.
"""

from __future__ import annotations

from src import plays
from tests.factories import exited, plays_context, position


def holding(**overrides) -> dict:
    ctx = plays_context()
    ctx["positions"] = [position(**overrides)]
    return ctx


def test_a_position_locked_to_the_other_side_is_never_proposed_for_exit():
    ctx = holding()
    ctx["quotes"]["F"]["last"] = 13.64  # three ATR below entry — would ATR-stop
    ctx["symbol_lock"] = {"F": "dip_recovery"}

    assert exited(plays.exits(ctx), "F") is None


def test_a_position_locked_to_this_side_still_exits_normally():
    ctx = holding()
    ctx["quotes"]["F"]["last"] = 13.64
    ctx["symbol_lock"] = {"F": "core"}

    assert exited(plays.exits(ctx), "F").code == plays.ATR_STOP


def test_an_unclaimed_symbol_still_exits_normally():
    ctx = holding()
    ctx["quotes"]["F"]["last"] = 13.64
    ctx["symbol_lock"] = {}

    assert exited(plays.exits(ctx), "F").code == plays.ATR_STOP
