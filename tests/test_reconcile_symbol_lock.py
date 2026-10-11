"""reconcile.py — Dip Recovery plan phase 2: claiming and releasing
`strategy_symbol_lock`.

The rule is "claim everything this side currently holds, every cycle" rather
than "claim on the fill event" — a fill-triggered claim would leave every
position that existed before phase 1's lock table did permanently unclaimed,
since nothing ever fires a first-fill event for them. Claiming the whole book
every cycle is what self-bootstraps those legacy positions on the first
reconciling run after deploy, and it costs nothing on every cycle after that
because the claim is idempotent.

Release is the mirror: whatever this side's lock map says it owns that this
cycle's book no longer holds, regardless of why the two fell out of sync —
and never a symbol the *other* side owns, which core's reconcile pass must
never touch.
"""

from __future__ import annotations

from src import reconcile
from tests.factories import reconcile_context


def test_every_held_symbol_is_claimed_every_cycle():
    """Not only a symbol that just filled — the quiet-cycle baseline holds F
    with no fill this cycle at all, and it is still claimed."""
    result = reconcile.run(reconcile_context())
    assert result.symbol_lock_claims == ["F"]


def test_a_symbol_owned_by_the_other_side_is_never_claimed():
    """The broker's book is aggregate: core's `positions` still lists a
    symbol Dip Recovery owns, since nothing upstream of this filters it out.
    Without this scoping, core's reconcile would call
    `edge_symbol_lock_claim(symbol, 'core')` on it every cycle, the SQL would
    correctly refuse it, and that refusal would read as a fresh
    cross-strategy conflict on every single cycle both sides hold anything —
    not a benign no-op."""
    ctx = reconcile_context(
        positions=[
            {"symbol": "F", "quantity": 1.0},
            {"symbol": "GME", "quantity": 2.0},
        ],
        tax_lots={
            "F": [{"quantity": 1.0, "cost_basis": 15.00, "acquired_date": "2026-07-30"}],
            "GME": [{"quantity": 2.0, "cost_basis": 61.40, "acquired_date": "2026-08-03"}],
        },
        symbol_lock={"GME": "dip_recovery"},
    )
    result = reconcile.run(ctx)
    assert result.symbol_lock_claims == ["F"]


def test_claiming_is_idempotent_regardless_of_what_the_lock_already_says():
    """Re-claiming a symbol this side already owns is a no-op at the SQL
    layer (`edge_symbol_lock_claim`); reconcile's own job is just to always
    ask, whether or not the lock already agrees."""
    result = reconcile.run(reconcile_context(symbol_lock={"F": "core"}))
    assert result.symbol_lock_claims == ["F"]


def test_a_symbol_no_longer_held_is_released():
    ctx = reconcile_context(
        symbol_lock={"F": "core"},
        positions=[],
        prior_positions=[],
        tax_lots={},
    )
    result = reconcile.run(ctx)
    assert result.symbol_lock_claims == []
    assert result.symbol_lock_releases == ["F"]


def test_a_symbol_still_held_is_not_released():
    result = reconcile.run(reconcile_context(symbol_lock={"F": "core"}))
    assert result.symbol_lock_releases == []


def test_release_never_touches_the_other_sides_lock():
    """F is not in this cycle's book under `dip_recovery`'s ownership — core's
    reconcile pass must leave that lock alone; releasing it would be exactly
    the cross-strategy write phase 1's design forbids."""
    ctx = reconcile_context(
        symbol_lock={"F": "dip_recovery"},
        positions=[],
        prior_positions=[],
        tax_lots={},
    )
    result = reconcile.run(ctx)
    assert result.symbol_lock_releases == []


def test_strategy_defaults_to_core_when_absent():
    """Every reconcile payload that predates Dip Recovery never sends
    `strategy` at all, and must behave exactly as if it said `"core"`."""
    ctx = reconcile_context(symbol_lock={"F": "core"}, positions=[], prior_positions=[], tax_lots={})
    default_owner = reconcile.run(ctx)
    explicit_owner = reconcile.run({**ctx, "strategy": "core"})
    assert default_owner.symbol_lock_releases == explicit_owner.symbol_lock_releases == ["F"]
