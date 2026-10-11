"""reconcile.py — Dip Recovery plan phase 6: one ownership model, applied to
this side's book and to its order scan.

Phases 1 and 2 stopped core *trading* a Dip-Recovery-owned symbol
(`guard.lock` denies the order, `plays/exits.py` declines to propose one) but
left core still *accounting* for it. `ctx["positions"]` is the broker's
aggregate list and has no strategy concept, so a dip-owned name still landed
in core's `book` — and from there in `positions_held`, in the `ctx["positions"]`
guard and sizing measure §4.4's concentration caps against, and in `_adopt`,
which would bind it to one of core's own plays.

Every fixture here holds its positions in the shape `get_equity_positions`
actually returns (`factories.broker_position`, string-typed numerics and all)
rather than the derived book shape, for the reason `fixture-contract-drift`
gives: a filter proven only against hand-built floats proves nothing about the
payload production sends.

Each suppression below has a negative control immediately after it, running
the identical context with the lock removed, because "no finding fired" is
also what a filter that silently matched nothing would produce.

The order half is the same model with one deliberate asymmetry: a position the
other side owns is dropped, an *order* it owns is reclassified. See the
`OTHER_STRATEGY_ORDER` section for why — nothing on either side scans working
orders except this module.
"""

from __future__ import annotations

from src import cli, reconcile
from src import report as reporter
from tests.factories import (
    broker_order,
    broker_position,
    fill,
    found,
    placement,
    report_context,
    reconcile_context,
)

# Core's own F, plus a GME the other side owns. Core's `prior_positions` is
# core's last reconciled book, so in steady state it does not carry GME at
# all — that asymmetry between "this side's remembered book" and "the
# broker's aggregate list" is the whole hazard this module now handles.
_BOTH_SIDES = {
    "positions": [
        broker_position(),
        broker_position(
            symbol="GME",
            quantity="2.000000",
            average_buy_price="30.700000",
            market_value="61.400000",
            shares_available_for_sells="2.000000",
        ),
    ],
    "prior_positions": [{"symbol": "F", "play_id": "DIP", "quantity": 1.0}],
}

_DIP_OWNS_GME = {**_BOTH_SIDES, "symbol_lock": {"GME": "dip_recovery"}}


def _symbols(result) -> list[str]:
    return [p.symbol for p in result.positions]


# --- the book, the count and the concentration input ---------------------


def test_the_other_sides_symbol_is_not_in_the_book():
    result = reconcile.run(reconcile_context(**_DIP_OWNS_GME))
    assert _symbols(result) == ["F"]


def test_without_the_lock_the_same_position_is_booked():
    """The negative control for every suppression in this file: nothing about
    this context except the lock keeps GME out."""
    result = reconcile.run(reconcile_context(**_BOTH_SIDES))
    assert _symbols(result) == ["F", "GME"]


def test_positions_held_counts_only_this_sides_book():
    """`positions_held` is what `sizing`'s ladder works `n_target` out from
    and what `cli._calls` estimates the POSITIONS-scope fetch against — an
    inflated count spends core's slots on capital core does not manage."""
    locked = reconcile.run(reconcile_context(**_DIP_OWNS_GME))
    unlocked = reconcile.run(reconcile_context(**_BOTH_SIDES))
    assert locked.control_updates["positions_held"] == 1
    assert unlocked.control_updates["positions_held"] == 2


def test_the_decide_book_guard_measures_concentration_against_drops_it_too():
    """The end of the chain, not a restatement of the first test:
    `reconcile.positions` flows through `cli._book` into `decide`'s own
    `ctx["positions"]`, which is where `guard._PlanState` sums `market_value`
    into the §4.4 position and sector caps."""
    payload = {
        "cycle_id": "c1",
        "now": "2026-08-03T13:40:00Z",
        "reconcile": reconcile.run(reconcile_context(**_DIP_OWNS_GME)).as_dict(),
        "positions": _DIP_OWNS_GME["positions"],
    }
    loaded = cli.load_config()
    cycle = cli._cycle(payload, loaded)
    book = cli._book(payload, loaded, cycle)

    assert [p["symbol"] for p in book] == ["F"]
    assert sum(p["market_value"] for p in book) == 14.88


# --- adoption ------------------------------------------------------------


def test_the_other_sides_symbol_is_never_adopted_into_one_of_this_sides_plays():
    """`_adopt` binds an unrecognised name to `would_open()`'s answer or to
    `ADOPTED`. Applied to a Dip Recovery position that means core starts
    managing it to core's own exits — the failure the lock exists to prevent,
    arriving through the book rather than through an order."""
    result = reconcile.run(reconcile_context(**_DIP_OWNS_GME))
    assert "GME" not in {p.symbol for p in result.positions}

    unlocked = reconcile.run(reconcile_context(**_BOTH_SIDES))
    adopted = {p.symbol: p for p in unlocked.positions}["GME"]
    assert adopted.adopted is True
    assert adopted.play_id in (reconcile.ADOPTED, "DIP", "CATALYST", "PEAD", "MACRO")


def test_the_other_sides_buy_fill_is_not_adopted_either():
    """`get_equity_orders` is account-wide, so the other side's fill arrives in
    *this* side's `fills` with no placement of ours behind it — which is
    exactly the shape `_attribution`'s middle pass hands to `_adopt`."""
    ctx = reconcile_context(
        **_DIP_OWNS_GME,
        fills=[fill(order_id="dip-1", symbol="GME", quantity=2.0, price=30.70)],
    )
    result = reconcile.run(ctx)
    assert _symbols(result) == ["F"]
    # The untracked-fill log line still fires: it is a record, not a decision,
    # and losing sight of a fill on the account is never the safer direction.
    assert found(result, reconcile.UNTRACKED_FILL) == ["GME"]


def test_the_other_sides_realised_sale_credits_none_of_this_sides_plays():
    """Scorecard contamination is the mirror of adoption — `_play_state_updates`
    reads the same attribution, and a play credited for a trade it did not make
    is wrong in `edge_play_state` for as long as the account exists."""
    ctx = reconcile_context(
        **_DIP_OWNS_GME,
        fills=[
            fill(
                order_id="dip-2", symbol="GME", side="sell",
                quantity=2.0, price=33.00, cost_basis=61.40,
            )
        ],
    )
    result = reconcile.run(ctx)
    assert "GME" not in result.play_state_updates
    assert set(result.play_state_updates) <= {"DIP"}


# --- the findings that would otherwise fire forever ----------------------


def test_no_position_delta_mismatch_on_a_symbol_this_side_no_longer_books():
    """The landmine that makes this a two-sided filter. Once GME is out of the
    book, next cycle's `prior_positions` (core's own book) no longer carries it
    while the broker's aggregate list still does — so an unfiltered
    `_position_findings` reports a mismatch on GME every cycle, and
    `POSITION_DELTA_MISMATCH` is a `DIVERGENCE_CLASSES` member: two cycles and
    entries are blocked until an operator clears `edge_control` by hand."""
    result = reconcile.run(reconcile_context(**_DIP_OWNS_GME))
    assert found(result, reconcile.POSITION_DELTA_MISMATCH) == []
    assert result.control_updates["divergence_streak"] == 0
    assert result.control_updates["entries_blocked"] is False


def test_without_the_lock_that_mismatch_does_fire():
    result = reconcile.run(reconcile_context(**_BOTH_SIDES))
    assert found(result, reconcile.POSITION_DELTA_MISMATCH) == ["GME"]
    assert result.control_updates["divergence_streak"] == 1


def test_the_block_does_not_escalate_across_cycles():
    """Two consecutive identical cycles is exactly what
    `PERSISTENT_DIVERGENCE_CYCLES` counts; the filter has to hold on the second
    one too, or the permanent block simply arrives a cycle later."""
    first = reconcile.run(reconcile_context(**_DIP_OWNS_GME))
    second = reconcile.run(
        reconcile_context(
            **_DIP_OWNS_GME,
            control={"divergence_streak": first.control_updates["divergence_streak"]},
        )
    )
    assert second.control_updates["entries_blocked_until_cleared"] is False
    assert found(second, reconcile.RECONCILIATION_DIVERGENCE_PERSISTENT) == []


def test_no_basis_finding_on_a_position_this_side_does_not_manage():
    """`POSITION_BASIS_UNKNOWN` says "§6.5's stops cannot be evaluated on this
    name". On the other side's position that is true and irrelevant: the other
    side keeps its own basis and its own clock in `dip_recovery_positions`."""
    result = reconcile.run(reconcile_context(**_DIP_OWNS_GME))
    assert found(result, reconcile.POSITION_BASIS_UNKNOWN) == []

    unlocked = reconcile.run(reconcile_context(**_BOTH_SIDES))
    assert found(unlocked, reconcile.POSITION_BASIS_UNKNOWN) == ["GME"]


def test_no_symbol_swap_suspected_against_the_other_sides_position():
    """`_symbol_swap_findings` pairs "expected and gone" with "held and never
    mentioned" at the same quantity and price. Unfiltered, the other side's
    positions are permanently in the second set, waiting to coincide with a
    core name that closed — a false swap manufactured by the filtering itself.
    Here core's own F closes at 1 share / 15.00 and the other side's holding is
    priced to match it exactly."""
    ctx = reconcile_context(
        positions=[
            broker_position(
                symbol="GME", quantity="1.000000", average_buy_price="15.000000",
                market_value="15.000000", shares_available_for_sells="1.000000",
            )
        ],
        prior_positions=[
            {"symbol": "F", "play_id": "DIP", "quantity": 1.0, "entry_price": 15.00}
        ],
        tax_lots={},
        symbol_lock={"GME": "dip_recovery"},
    )
    assert found(reconcile.run(ctx), reconcile.SYMBOL_SWAP_SUSPECTED) == []
    assert found(
        reconcile.run({**ctx, "symbol_lock": {}}), reconcile.SYMBOL_SWAP_SUSPECTED
    ) == ["GME"]


# --- the adoption race ---------------------------------------------------


def test_a_pending_dip_entry_is_excluded_before_its_lock_exists():
    """`dip_recovery.engine` writes `ENTRY_PENDING` and places the order in one
    tick; `_apply_fills` does not claim the lock until the *next* tick confirms
    the fill. A core cycle landing in that window reads GME as unclaimed, and
    `in (None, owner)` correctly keeps an unclaimed symbol — so without this
    second source core adopts a Dip Recovery position minutes old and can
    propose an exit on it before the lock ever exists."""
    ctx = reconcile_context(
        **_BOTH_SIDES,
        symbol_lock={},
        dip_recovery_positions={"GME": {"state": "ENTRY_PENDING", "total_fills": 0}},
    )
    result = reconcile.run(ctx)
    assert _symbols(result) == ["F"]
    assert found(result, reconcile.POSITION_DELTA_MISMATCH) == []


def test_a_stray_dip_row_never_drops_a_symbol_this_side_already_owns():
    """The guard in the other direction. `dip_recovery_positions` is the other
    side's table and this side does not write it; if it ever names a symbol
    core's own lock already holds, core's book wins and keeps the position —
    dropping it would leave a real core holding unmanaged and unexitable."""
    ctx = reconcile_context(
        **_BOTH_SIDES,
        symbol_lock={"F": "core", "GME": "core"},
        dip_recovery_positions={"GME": {"state": "ENTRY_PENDING"}},
    )
    assert _symbols(reconcile.run(ctx)) == ["F", "GME"]


def test_the_pending_row_is_not_claimed_by_this_side_either():
    """A symbol the other side has an order out on must not be claimed out from
    under it by core's own `edge_symbol_lock_claim` call in the same window."""
    ctx = reconcile_context(
        **_BOTH_SIDES,
        symbol_lock={},
        dip_recovery_positions={"GME": {"state": "ENTRY_PENDING"}},
    )
    assert reconcile.run(ctx).symbol_lock_claims == ["F"]


# --- symmetry and backward compatibility ---------------------------------


def test_the_dip_side_excludes_cores_symbols_and_ignores_its_own_table():
    """`run` is strategy-parametrised, so the same filter has to read right
    from the other side: core's lock excludes, and `dip_recovery_positions` —
    this side's *own* table — must not exclude anything at all."""
    ctx = reconcile_context(
        **_BOTH_SIDES,
        strategy="dip_recovery",
        symbol_lock={"F": "core"},
        dip_recovery_positions={"GME": {"state": "HOLDING"}},
        tax_lots={
            "GME": [
                {"quantity": 2.0, "cost_basis": 61.40, "acquired_date": "2026-07-30"}
            ]
        },
    )
    assert _symbols(reconcile.run(ctx)) == ["GME"]


def test_a_payload_that_predates_all_of_this_is_unchanged():
    """Every reconcile payload written before phase 6 sends neither key. An
    absent lock and an absent positions table must leave the whole book, every
    finding and every control update exactly as they were."""
    baseline = reconcile.run(reconcile_context(**_BOTH_SIDES))
    explicit = reconcile.run(
        reconcile_context(**_BOTH_SIDES, symbol_lock={}, dip_recovery_positions={})
    )
    assert baseline.as_dict() == explicit.as_dict()


def test_cli_reconcile_passes_the_dip_positions_table_through():
    """The wiring the filter is inert without — `cli.reconcile` is the only
    thing that puts `dip_recovery_positions` into reconcile's ctx, and
    `cycle-prompt.md` Step 6 is the only thing that puts it into the payload."""
    payload = {
        "cycle_id": "c1",
        "now": "2026-08-03T13:40:00Z",
        "positions": _BOTH_SIDES["positions"],
        "prior_positions": _BOTH_SIDES["prior_positions"],
        "tax_lots": {},
        "dip_recovery_positions": {"GME": {"state": "ENTRY_PENDING"}},
    }
    result = cli.reconcile(payload, cli.load_config())
    assert [p["symbol"] for p in result["positions"]] == ["F"]


# --- the same ownership model, applied to orders -------------------------
#
# `PHANTOM_ORDER` means "an order *nobody* claims" — §13.1's two-deciders-one-
# account failure. Once the lock exists, the other side's resting order is
# claimed, by the side that placed it. Left as a phantom it is a
# `DIVERGENCE_CLASSES` member, so two core cycles with a dip order still
# working blocks core's entries until an operator clears `edge_control` by hand
# — the behaviour already confirmed live for manual orders.
#
# It is reclassified rather than filtered out, because nothing on either side
# scans *working* orders except this module (`dip-monitor-prompt.md` Step 5
# reads `get_equity_orders state=filled` only) — a silently dropped order is an
# order no part of the system is looking at, including an operator's own trade
# in a name the other strategy owns.

_DIP_ORDER = broker_order(
    order_id="dip-9", symbol="GME", side="buy", quantity=2.0, state="queued"
)


def test_the_other_sides_resting_order_is_not_a_phantom():
    result = reconcile.run(reconcile_context(**_DIP_OWNS_GME, orders=[_DIP_ORDER]))
    assert found(result, reconcile.PHANTOM_ORDER) == []
    assert found(result, reconcile.OTHER_STRATEGY_ORDER) == ["GME"]


def test_without_the_lock_the_same_order_is_a_phantom():
    result = reconcile.run(reconcile_context(**_BOTH_SIDES, orders=[_DIP_ORDER]))
    assert found(result, reconcile.PHANTOM_ORDER) == ["GME"]
    assert found(result, reconcile.OTHER_STRATEGY_ORDER) == []


def test_it_blocks_no_entries_and_counts_toward_no_divergence():
    result = reconcile.run(reconcile_context(**_DIP_OWNS_GME, orders=[_DIP_ORDER]))
    assert reconcile.OTHER_STRATEGY_ORDER not in reconcile.DIVERGENCE_CLASSES
    assert result.control_updates["entries_blocked"] is False
    assert result.control_updates["entries_blocked_reason"] is None
    assert result.control_updates["divergence_streak"] == 0


def test_a_dip_order_resting_across_two_cycles_never_escalates():
    """The actual blocker being closed. A phantom on cycle one leaves
    `divergence_streak` at 1; a second cycle with the same order still working
    reaches `PERSISTENT_DIVERGENCE_CYCLES` and blocks entries *until cleared*,
    which is an operator's hand on `edge_control`, not a self-lifting block."""
    ctx = reconcile_context(
        **_DIP_OWNS_GME,
        orders=[_DIP_ORDER],
        control={"divergence_streak": 1, "high_water_mark": 236.42},
    )
    result = reconcile.run(ctx)
    assert found(result, reconcile.RECONCILIATION_DIVERGENCE_PERSISTENT) == []
    assert result.control_updates["entries_blocked_until_cleared"] is False

    unlocked = reconcile.run({**ctx, "symbol_lock": {}})
    assert found(unlocked, reconcile.RECONCILIATION_DIVERGENCE_PERSISTENT) == [""]
    assert unlocked.control_updates["entries_blocked_until_cleared"] is True


def test_this_side_never_cancels_the_other_sides_order():
    """True before this change and after it — `cancels` is only ever built from
    orders in `ours`, which is `edge_prior()`'s record of this side's own
    placements. Pinned because Step 8c's postclose sweep reads that list and
    the reclassified finding no longer says "phantom", which is what the
    prompt's own never-cancel-a-phantom rule keys on."""
    postclose = {"cycle_type": "postclose", "trading_day": "2026-08-03",
                 "now": "2026-08-03T22:47:00Z", "session": "closed"}
    result = reconcile.run(
        reconcile_context(**_DIP_OWNS_GME, orders=[_DIP_ORDER], cycle=postclose)
    )
    assert result.cancels == []
    assert found(result, reconcile.STALE_OPEN_ORDER) == []


def test_a_resting_dip_order_is_excluded_before_its_lock_exists():
    """Amendment to the obvious implementation, and the reason this reads
    `_other_side_symbols` rather than the lock alone. A dip *entry* order is
    placed in the same tick its `ENTRY_PENDING` row is written, but the lock is
    not claimed until the next tick confirms the fill — so a lock-only read
    would still report `PHANTOM_ORDER` on precisely the order it is meant to
    exclude, and the block would arrive exactly as before."""
    result = reconcile.run(
        reconcile_context(
            **_BOTH_SIDES,
            orders=[_DIP_ORDER],
            symbol_lock={},
            dip_recovery_positions={"GME": {"state": "ENTRY_PENDING"}},
        )
    )
    assert found(result, reconcile.PHANTOM_ORDER) == []
    assert found(result, reconcile.OTHER_STRATEGY_ORDER) == ["GME"]


def test_a_genuine_phantom_still_fires_alongside_one():
    """Per symbol, not global. An order on a name neither side claims is still
    §13.1's two-deciders failure and still blocks entries for a cycle, in the
    very same cycle the dip order is excused."""
    result = reconcile.run(
        reconcile_context(
            **_DIP_OWNS_GME,
            orders=[
                _DIP_ORDER,
                broker_order(order_id="x-1", symbol="AMD", side="buy", state="queued"),
            ],
        )
    )
    assert found(result, reconcile.OTHER_STRATEGY_ORDER) == ["GME"]
    assert found(result, reconcile.PHANTOM_ORDER) == ["AMD"]
    assert result.control_updates["entries_blocked"] is True


def test_this_sides_own_order_paths_are_untouched():
    """Regression on the three classes that key off `ours` rather than off the
    broker's list — none of them can see the other side's order at all, and
    none of them may change because of it."""
    ctx = reconcile_context(
        **_DIP_OWNS_GME,
        intent={"placements": [placement(order_id="a1b2", symbol="F")]},
        orders=[_DIP_ORDER, broker_order(order_id="a1b2", state="queued")],
        control={"open_order_cycles": {"a1b2": 1}, "high_water_mark": 236.42},
    )
    result = reconcile.run(ctx)
    assert found(result, reconcile.STALE_OPEN_ORDER) == ["F"]
    assert [c["order_id"] for c in result.cancels] == ["a1b2"]
    assert result.control_updates["open_order_cycles"] == {"a1b2": 2}


def test_an_owned_fill_is_recorded_without_claiming_it_was_adopted():
    """The runlog sentence, corrected. `UNTRACKED_FILL` still fires — an
    account fill nobody records is never the safer direction — but since phase
    6 an owned symbol is deliberately not adopted, so the standing wording
    described the exact opposite of what happened."""
    owned = reconcile.run(
        reconcile_context(
            **_DIP_OWNS_GME,
            fills=[fill(order_id="dip-1", symbol="GME", quantity=2.0, price=30.70)],
        )
    )
    detail = next(f.detail for f in owned.findings if f.code == reconcile.UNTRACKED_FILL)
    assert "the other strategy owns" in detail
    assert "adopted" not in detail.replace("not adopted", "")

    manual = reconcile.run(
        reconcile_context(
            **_BOTH_SIDES,
            fills=[fill(order_id="op-1", symbol="AMD", quantity=1.0, price=100.0)],
        )
    )
    unowned = next(
        f.detail for f in manual.findings
        if f.code == reconcile.UNTRACKED_FILL and f.symbol == "AMD"
    )
    assert "adopted and managed on identical terms (Invariant 7)" in unowned


def test_a_resting_dip_order_pushes_no_reconciliation_divergence_alert():
    """`report.SILENT_FINDINGS` is deliberately an exclusion list so a new finding
    code alerts by default — this is the explicit decision that paragraph
    invites, not the default riding."""
    owned = reconcile.run(reconcile_context(**_DIP_OWNS_GME, orders=[_DIP_ORDER]))
    # The finding is present — silenced on the push channel, not suppressed.
    assert found(owned, reconcile.OTHER_STRATEGY_ORDER) == ["GME"]

    quiet = reporter.run(
        report_context(reconcile=owned.as_dict(), control={"high_water_mark": 236.42})
    )
    assert quiet.status == reporter.STATUS_SUCCESS
    assert "GME" not in quiet.alerts[0].body

    # The negative control: the same order unowned is a phantom, and a phantom
    # must still reach the operator, in the folded status push's body.
    phantom = reconcile.run(
        reconcile_context(**_BOTH_SIDES, orders=[_DIP_ORDER])
    ).as_dict()
    noisy = reporter.run(
        report_context(reconcile=phantom, control={"high_water_mark": 236.42})
    )
    assert noisy.status == reporter.STATUS_SUCCESS_WITH_WARNINGS
    assert "GME" in noisy.alerts[0].body
