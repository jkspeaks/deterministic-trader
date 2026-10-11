"""Dip Recovery plan phase 7 — the guard pass, and what it may not leave behind.

`dip_recovery.evaluate` now calls `guard.evaluate` itself, exactly as
`cli.decide` always has. Two things about this path are unlike core's and both
are what these tests exist for.

**Guard's fail direction is not uniform.** Four categories deny on an *absent*
ctx key rather than passing — the correct direction, but it means a ctx that
merely forgot a key produces a strategy that silently never trades, which is an
earlier failure this project has already had once. Every one of those is
asserted here as a denial *code*, not as "no order was proposed", because those
two are indistinguishable from the outside and only one of them is the bug.

**A denial has to roll back.** Unlike `plays`/`sizing`, this engine writes
persistent state alongside its proposals: `position_updates` goes to
`edge_dip_recovery_positions_apply` and the ledger advances
`dip_recovery_new_positions`. An `ENTRY_PENDING` row committed for an order
guard refused is a row nothing ever clears — `_MANAGED_STATES` excludes that
state so `_manage` never revisits it, `_ACTIVE_STATES` includes it so it holds
one of five slots, and `ALREADY_TRACKED` then refuses that symbol forever. One
denial, one permanently leaked slot.
"""

from __future__ import annotations

from src import cli, guard
from src.dip_recovery import evaluate
from tests.dip_recovery_factories import (
    dip1_bars,
    dip_recovery_config,
    dip_recovery_context,
    held_position,
    orders,
)


def denial_codes(result, symbol: str | None = None) -> list[str]:
    return [
        d["code"]
        for entry in result.blocked
        for d in entry["denials"]
        if symbol is None or entry["order"]["symbol"] == symbol
    ]


def without(ctx: dict, *keys: str) -> dict:
    """A ctx with `keys` genuinely absent.

    The factories deep-merge, so passing `earnings={}` merges an empty dict
    into the baseline and changes nothing — which would make every fail-closed
    test below silently assert the baseline instead. Absent is also the shape
    production actually produces when a fetch step is skipped or fails.
    """
    return {k: v for k, v in ctx.items() if k not in keys}


def two_candidates(**overrides):
    """DIP1 and DIP2, identical and both qualifying, in one sector.

    Needed to prove guard is asked about the *plan* rather than about each
    order — `_PlanState` accumulates approved notional across orders precisely
    so two buys cannot independently pass the same cap.
    """
    base = dict(
        dip_recovery_watchlist=["DIP1", "DIP2"],
        quotes={
            "DIP1": {"bid": 45.90, "ask": 46.00, "last": 45.95},
            "DIP2": {"bid": 45.90, "ask": 46.00, "last": 45.95},
        },
        signals={
            "DIP1": {"rsi": 20.0, "sma_20": 50.0, "sma_200": 40.0},
            "DIP2": {"rsi": 20.0, "sma_20": 50.0, "sma_200": 40.0},
        },
        fundamentals={
            "DIP1": {"volume": 1_500_000, "average_volume": 1_000_000},
            "DIP2": {"volume": 1_500_000, "average_volume": 1_000_000},
        },
        dip_recovery_bars={"DIP1": dip1_bars(), "DIP2": dip1_bars()},
        sectors={"DIP1": "Technology", "DIP2": "Technology"},
        earnings={
            "DIP1": {"status": "known", "sessions_until_report": 20},
            "DIP2": {"status": "known", "sessions_until_report": 20},
        },
        tradability={
            "DIP1": {"tradeable": True, "all_day_tradability": "tradable",
                     "fractional_tradability": "tradable"},
            "DIP2": {"tradeable": True, "all_day_tradability": "tradable",
                     "fractional_tradability": "tradable"},
        },
    )
    return dip_recovery_context(**{**base, **overrides})


# --- the pass runs at all ------------------------------------------------


def test_the_baseline_entry_clears_guard_and_is_proposed():
    result = evaluate(dip_recovery_context())
    assert orders(result, "buy") == ["DIP1"]
    assert result.blocked == []
    assert result.position_updates["DIP1"]["state"] == "ENTRY_PENDING"


def test_guard_is_asked_about_the_plan_not_about_each_order():
    """Two buys that each fit the sector cap alone but breach it together. Per
    order they would both pass; as a plan the second is refused."""
    ctx = two_candidates(
        dip_recovery_config=dip_recovery_config(
            concentration={"position_cap_pct": 0.50, "sector_cap_pct": 0.10}
        )
    )
    result = evaluate(ctx)

    assert orders(result, "buy") == ["DIP1"]
    assert denial_codes(result, "DIP2") == [guard.SECTOR_CAP_EXCEEDED]


# --- the four that fail closed -------------------------------------------


def test_absent_earnings_denies_rather_than_passes():
    result = evaluate(without(dip_recovery_context(), "earnings"))
    assert denial_codes(result) == [guard.EARNINGS_DATE_UNAVAILABLE]
    assert orders(result) == []


def test_absent_tradability_denies_rather_than_passes():
    result = evaluate(without(dip_recovery_context(), "tradability"))
    assert denial_codes(result) == [guard.NOT_TRADABLE]


def test_absent_sectors_denies_rather_than_passes():
    result = evaluate(without(dip_recovery_context(), "sectors"))
    assert denial_codes(result) == [guard.SECTOR_UNKNOWN]


def test_an_imminent_print_denies_the_entry():
    """Not a plumbing test — the reason earnings is kept for this side at all.
    Averaging down into a print is how a strategy that never sells at a loss
    gets stuck below cost indefinitely."""
    ctx = dip_recovery_context(
        earnings={"DIP1": {"status": "known", "sessions_until_report": 1}}
    )
    assert denial_codes(evaluate(ctx)) == [guard.EARNINGS_WINDOW]


def test_mc_check_present_and_empty_would_deny_every_buy():
    """The one key whose absent and empty meanings are inverted, pinned so the
    house `payload.get(x) or {}` idiom cannot be applied to it later. Dip
    Recovery runs no Monte Carlo pass, so the ctx must omit the key entirely."""
    ctx = dip_recovery_context()
    assert "mc_check" not in ctx
    assert denial_codes(evaluate({**ctx, "mc_check": {}})) == [
        guard.MC_SIMULATION_UNAVAILABLE
    ]


def test_cli_does_not_put_an_empty_mc_check_in_the_ctx():
    """The assertion above is only worth anything if the real composer agrees."""
    payload = {"cycle_id": "c1", "now": "2026-08-31T15:47:00Z"}
    assert "mc_check" not in cli.dip_recovery_decide(payload, cli.load_config())


# --- rollback ------------------------------------------------------------


def test_a_denied_entry_leaves_no_position_row_behind():
    """The slot leak. An `ENTRY_PENDING` row for an order that was never placed
    is never managed, never cleared, holds a slot and blocks the symbol."""
    result = evaluate(without(dip_recovery_context(), "tradability"))
    assert result.orders == []
    assert result.position_updates == {}


def test_a_denied_entry_does_not_spend_the_days_allowance():
    """`cli._new_positions` settled this for core: "counting *proposals* would
    let a cycle whose entries guard refused spend the day's allowance on
    nothing at all"."""
    denied = evaluate(without(dip_recovery_context(), "tradability"))
    approved = evaluate(dip_recovery_context())

    assert denied.control_updates["dip_recovery_new_positions"]["count"] == 0
    assert approved.control_updates["dip_recovery_new_positions"]["count"] == 1


def test_a_denied_average_down_reverts_the_row_to_holding():
    """`AVERAGE_PENDING` claims an order is resting at the broker. Refused, the
    honest state is the one `_manage` already returns when averaging is
    skipped."""
    ctx = dip_recovery_context(
        dip_recovery_positions={"DIP1": held_position()},
        dip_recovery_watchlist=[],
        quotes={"DIP1": {"bid": 43.00, "ask": 43.10, "last": 43.05}},
    )
    result = evaluate(without(ctx, "tradability"))

    assert result.orders == []
    assert result.position_updates["DIP1"]["state"] == "HOLDING"
    assert denial_codes(result) == [guard.NOT_TRADABLE]


def test_a_denied_exit_reverts_the_row_to_exit_locked():
    """The target is still hit — that fact does not stop being true because the
    sell was refused. `EXIT_LOCKED` is exactly what `_manage` returns when the
    target is met and the position cannot be sold yet."""
    ctx = dip_recovery_context(
        dip_recovery_positions={"DIP1": held_position(clock_trading_day="2026-08-24")},
        dip_recovery_watchlist=[],
        quotes={"DIP1": {"bid": 48.50, "ask": 48.60, "last": 48.55}},
    )
    result = evaluate(without(ctx, "tradability"))

    assert orders(result, "sell") == []
    assert result.position_updates["DIP1"]["state"] == "EXIT_LOCKED"


def test_an_approved_exit_still_reaches_exit_pending():
    """The control for the two above — rollback must not be the only path."""
    ctx = dip_recovery_context(
        dip_recovery_positions={"DIP1": held_position(clock_trading_day="2026-08-24")},
        dip_recovery_watchlist=[],
        quotes={"DIP1": {"bid": 48.50, "ask": 48.60, "last": 48.55}},
    )
    result = evaluate(ctx)

    assert orders(result, "sell") == ["DIP1"]
    assert result.position_updates["DIP1"]["state"] == "EXIT_PENDING"


# --- halts ---------------------------------------------------------------


def test_a_halt_blocks_every_order_with_its_reason_recorded():
    """`guard.evaluate` returns empty `approved` *and* empty `denied` on a halt,
    putting the reason in `halts`. Expanded per order here, because an order
    that vanished with no recorded reason is the failure this engine is written
    against."""
    ctx = dip_recovery_context(
        control={"readable": True, "trading_enabled": False, "high_water_mark": 5000.0}
    )
    result = evaluate(ctx)

    assert result.orders == []
    assert result.position_updates == {}
    assert denial_codes(result) == [guard.TRADING_DISABLED]


def test_a_halt_does_not_stop_the_fill_bookkeeping():
    """Fills are applied before anything is proposed, and a halt must not
    unwind what the broker already did — the lock claims and the closed list
    are facts, not intentions."""
    ctx = dip_recovery_context(
        control={"readable": True, "trading_enabled": False, "high_water_mark": 5000.0},
        dip_recovery_positions={"DIP1": held_position(state="ENTRY_PENDING")},
        dip_recovery_fills=[
            {"symbol": "DIP1", "side": "buy", "quantity": 10.0, "price": 46.00,
             "trading_day": "2026-08-31", "filled_at": "2026-08-31T14:00:00Z"}
        ],
        dip_recovery_watchlist=[],
    )
    result = evaluate(ctx)
    assert result.symbol_lock_claims == ["DIP1"]


def test_a_halt_rolls_a_managed_row_back_the_same_way_a_denial_does():
    """A halt reaches the commit phase with an empty `approved` set, so every
    proposal takes the `on_deny` branch — the rollback is one code path, not
    two. Pinned separately because the two arrive by different routes
    (`verdict.halted` vs. `verdict.denied`) and only this one leaves `denied`
    empty, so a future short-circuit that returned early on a halt would skip
    the commit loop and persist `EXIT_PENDING` for a sell nothing placed.

    What is *not* rolled back is deliberate: a managed row whose transition
    carried no order (`HOLD_NO_NEW_BUYS`, or `HOLDING` after averaging was
    skipped) is a recomputation from the calendar and this side's own
    thresholds, true whether or not the account may trade."""
    ctx = dip_recovery_context(
        control={"readable": True, "trading_enabled": False, "high_water_mark": 5000.0},
        dip_recovery_positions={"DIP1": held_position(clock_trading_day="2026-08-24")},
        dip_recovery_watchlist=[],
        quotes={"DIP1": {"bid": 48.50, "ask": 48.60, "last": 48.55}},
    )
    result = evaluate(ctx)

    assert orders(result, "sell") == []
    assert result.position_updates["DIP1"]["state"] == "EXIT_LOCKED"
    assert denial_codes(result) == [guard.TRADING_DISABLED]


# --- strategy-aware concentration ----------------------------------------


def test_the_slot_cap_is_this_sides_own_not_cores_ladder():
    """Core's `n_target` is an equity-derived ladder that means nothing here,
    and an absent `sizing` key reads 0 — so the pre-phase-7 arithmetic denied
    every buy `SLOT_LIMIT_REACHED` for a reason that was not about risk."""
    ctx = two_candidates(
        dip_recovery_positions={"DIP1": held_position()},
        dip_recovery_watchlist=["DIP2"],
        positions=[{"symbol": "DIP1", "quantity": 10.0, "market_value": 460.0}],
    )
    assert guard.SLOT_LIMIT_REACHED not in denial_codes(evaluate(ctx))


def test_the_slot_cap_still_binds_at_this_sides_own_ceiling():
    held = {f"HELD{i}": held_position(symbol=f"HELD{i}") for i in range(5)}
    ctx = two_candidates(
        dip_recovery_positions=held,
        dip_recovery_watchlist=["DIP2"],
        positions=[
            {"symbol": s, "quantity": 10.0, "market_value": 460.0} for s in held
        ],
    )
    # The engine's own 5-position ledger stops it before guard is even asked;
    # both ceilings read the same `max_active_positions`, which is the point of
    # `_dip_caps` reading `sizing_thresholds` rather than restating the number.
    result = evaluate(ctx)
    assert orders(result, "buy") == []


def test_the_position_cap_is_configurable_from_dip_recovery_config():
    ctx = dip_recovery_context(
        dip_recovery_config=dip_recovery_config(
            concentration={"position_cap_pct": 0.01, "sector_cap_pct": 0.30}
        )
    )
    assert denial_codes(evaluate(ctx)) == [guard.POSITION_CAP_EXCEEDED]


def test_the_caps_default_to_section_4_4s_formulas_at_this_sides_n():
    """The schema seeds 0.24/0.30, which are `position_cap_fraction(5)` and
    `sector_cap_fraction(5)` — the same rule as core, this strategy's own n.
    Pinned so a checkout without the migration behaves identically to one with
    it, the `signal.py` Bollinger precedent."""
    from src.guard.concentration import _dip_caps
    from src.sizing import position_cap_fraction, sector_cap_fraction

    config = dip_recovery_config()
    config.pop("concentration", None)
    slots, position, sector = _dip_caps({"dip_recovery_config": config})

    assert slots == 5
    assert position == position_cap_fraction(5) == 0.24
    assert sector == sector_cap_fraction(5) == 0.30


def test_core_still_measures_against_its_own_ladder():
    """One-directional carve-out — nothing about core's concentration moves."""
    from tests.factories import context

    ctx = context(sizing={"n_target": 3}, positions=[], sectors={"F": "Industrials"})
    result = guard.evaluate(
        [{"symbol": "F", "side": "buy", "type": "limit", "quantity": 1.0,
          "limit_price": 14.87, "session": "regular_hours"}],
        ctx,
    )
    assert not any(
        d.code == guard.SLOT_LIMIT_REACHED
        for denied in result.denied for d in denied.denials
    )


# --- the dip book guard measures against ---------------------------------


def test_the_book_guard_sees_is_this_sides_own_not_cores():
    book = cli._dip_book(
        {"DIP1": held_position()},
        {"quotes": {"DIP1": {"last": 47.00}}},
    )
    assert book == [{"symbol": "DIP1", "quantity": 10.0, "market_value": 470.0}]


def test_a_position_with_no_quote_is_valued_at_cost_not_at_zero():
    """Same overstate-rather-than-understate direction `cli._market_value`
    documents: a position read as worthless silently widens every cap."""
    book = cli._dip_book({"DIP1": held_position()}, {})
    assert book == [{"symbol": "DIP1", "quantity": 10.0, "market_value": 460.0}]


def test_a_pending_entry_holds_a_slot_at_no_value():
    """It occupies a slot (`_ACTIVE_STATES`) because an order resting at the
    broker has already claimed the position it will become, but no shares
    exist yet."""
    book = cli._dip_book(
        {"DIP1": held_position(state="ENTRY_PENDING", total_quantity=0.0)}, {}
    )
    assert book == [{"symbol": "DIP1", "quantity": 0.0, "market_value": 0.0}]
