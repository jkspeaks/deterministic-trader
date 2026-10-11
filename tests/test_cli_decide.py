"""The pipeline: cadence → universe → plays → sizing → guard → a plan.

`decide` is composition, and every rule it appears to apply belongs to a module
underneath it. These tests are therefore about the **joins** — the four places
where a value leaves one module and has to arrive intact at another, and where
a rename would not raise but would produce a system that quietly stops working.

The joins, in the order they happen:

1. `plays.py` emits triggers as rows; `universe.resolve` takes MACRO's targets
   and everything else's symbols through different doors and returns only the
   survivors. `cli.py` intersects them into §5.3's pool.
2. `reconcile.py`'s book carries the play, the basis and the holding period;
   `get_equity_positions` carries the market value. `sizing` and `guard` need
   both and neither module has both.
3. §6.6's catastrophe override reads ``loss_pct_below_stop``, which nothing
   anywhere computes.
4. `guard.evaluate` approves; the plan orders what it approved so that a sale
   is never queued behind a buy that failed.
"""

import pytest

from src import cadence, cli, guard, plays, sizing
from tests.factories import NOTHING_TO_PROTECT, cli_payload, planned


def decided(**overrides) -> dict:
    return cli.decide(cli_payload(**overrides), cli.load_config())


def order_for(result: dict, symbol: str) -> dict | None:
    for placed in result["plan"]["orders"]:
        if placed["symbol"] == symbol:
            return placed
    return None


def held(result: dict, symbol: str) -> dict | None:
    for position in result["book"]:
        if position["symbol"] == symbol:
            return position
    return None


OVERNIGHT = {
    "now": "2026-08-04T00:30:00Z",
    "control": {"has_whole_share_position": True, "positions_held": 1},
}


# --- the baseline ----------------------------------------------------------


def test_the_baseline_cycle_places_one_buy():
    """F is held and quiet; PFE is oversold inside its uptrend and prices to
    one whole share at this rung. Every test below moves exactly one thing
    away from here."""
    result = decided()

    assert planned(result) == ["PFE"]
    assert order_for(result, "PFE")["side"] == "buy"


def test_the_entry_takes_the_whole_share_path_at_this_rung():
    """§4.3, and the reason §4.1 prefers three positions of $79 to ten of $23:
    the WHOLE path is what carries a limit order and a resting stop."""
    placed = order_for(decided(), "PFE")

    assert placed["type"] == "limit"
    assert placed["quantity"] == 1.0


def test_a_noop_cycle_produces_an_empty_plan_and_looks_at_nothing():
    """§13. The universe is not resolved and no play is asked, because the
    routine has not fetched anything for either to read."""
    result = decided(
        now="2026-08-03T14:40:00Z",
        control={
            "last_full_cycle": "2026-08-03T13:40:00Z",
            "positions_held": 0,
            "new_positions": {"date": "2026-08-03", "count": 3},
        },
    )

    assert result["cycle"]["cycle_type"] == cadence.NOOP
    assert result["plan"] == {"cancels": [], "orders": [], "protection": []}
    assert result["universe"] is None


# --- the day-context write --------------------------------------------------
#
# Persistence moved off `decide()` entirely — the wholesale write it used to gate
# (`should_write`, `control_updates["last_data_fresh_at"]`) left nothing behind
# when a cycle died before `decide` was ever called, which in production was
# every cycle for several days straight. Writing is now incremental, per
# candidate, via `cli.day_context_write_symbol` during Step 6 (see
# `tests/test_cli_day_context.py`). `decide()`'s `day_context_write` key is
# informational only now — a count of this cycle's cache coverage for the runlog,
# nothing downstream reads it to decide whether to write anything.


def test_a_cycle_with_no_fetch_meta_reports_zero_coverage():
    """A payload without `fetch_meta` (every `cli_payload()` call written
    before this feature, and any cycle where the routine failed to set it)
    must report zero for both counts — never guess at coverage that was
    never recorded."""
    result = decided()

    assert result["day_context_write"] == {
        "trading_day": "2026-08-03",
        "fresh_count": 0,
        "stale_count": 0,
    }
    assert "last_data_fresh_at" not in result["control_updates"]


def test_fetch_meta_fresh_and_stale_counts_are_reported():
    """`fetch_meta` now carries `day_context_plan`'s own output verbatim —
    `{"fresh": [...], "stale": [...]}` — and `decide()` just counts each
    list; it does not re-derive or second-guess what the routine reported."""
    result = decided(
        fetch_meta={"fresh": ["AAPL", "MSFT"], "stale": ["TSLA"]}
    )

    assert result["day_context_write"] == {
        "trading_day": "2026-08-03",
        "fresh_count": 2,
        "stale_count": 1,
    }
    assert "last_data_fresh_at" not in result["control_updates"]


def test_a_malformed_fetch_meta_reports_zero_rather_than_raising():
    """`fresh`/`stale` not being lists (a routine that emitted something
    unexpected) must degrade to zero, the same as absent — never raise, and
    never count something that is not actually a list of symbols."""
    result = decided(fetch_meta={"fresh": "AAPL", "stale": None})

    assert result["day_context_write"] == {
        "trading_day": "2026-08-03",
        "fresh_count": 0,
        "stale_count": 0,
    }


def test_a_noop_cycle_reports_zero_coverage():
    """§13's cheap path fetched nothing at all, so both counts must be zero
    even if `fetch_meta` somehow carries symbols (NOOP fetched nothing, so
    this shouldn't normally happen — but this module trusts explicit state,
    not "shouldn't"). Coverage counting here is unconditional on cycle type,
    unlike the old `should_write` gate, which had a dedicated NOOP exclusion
    — a NOOP's `fetch_meta` is simply always going to be empty in practice,
    so nothing extra needs to special-case it."""
    result = decided(
        now="2026-08-03T14:40:00Z",
        control={
            "last_full_cycle": "2026-08-03T13:40:00Z",
            "positions_held": 0,
            "new_positions": {"date": "2026-08-03", "count": 3},
        },
        fetch_meta={"fresh": ["AAPL"], "stale": []},
    )

    assert result["cycle"]["cycle_type"] == cadence.NOOP
    assert result["day_context_write"] == {
        "trading_day": "2026-08-03",
        "fresh_count": 1,
        "stale_count": 0,
    }
    assert "last_data_fresh_at" not in result["control_updates"]


# --- join 1: the §5.3 pool -------------------------------------------------


def test_a_macro_trigger_goes_through_the_rotation_door():
    """§6.4 expresses macro through the sector sleeve and nothing else, and
    `universe.resolve` refuses a macro target outside the eleven. Sending a
    MACRO row through `triggers` instead would ask the membership fence a
    question about XLE that the S&P holdings file cannot answer."""
    result = decided(triggers=[{"symbol": "XLE", "play_id": "MACRO"}])

    assert "XLE" in result["universe"]["candidates"]


def test_a_macro_target_outside_the_sleeve_is_refused_not_carried():
    result = decided(triggers=[{"symbol": "AAPL", "play_id": "MACRO"}])
    codes = [n["code"] for n in result["universe"]["notes"]]

    assert "AAPL" not in result["universe"]["candidates"]
    assert "MACRO_TARGET_NOT_IN_ROTATION" in codes


def test_a_trigger_outside_the_universe_never_reaches_a_play():
    """Invariant 3. A play selects from the universe and may not extend it, so
    the fence has to be the thing that decides what plays even look at."""
    result = decided(triggers=[{"symbol": "GME", "play_id": "DIP"}])

    assert "GME" not in result["universe"]["candidates"]
    assert planned(result) == []


def test_the_pool_plays_judges_is_the_pool_universe_returned():
    """`plays.py`'s docstring: it judges the pool and does not choose it. If
    these two ever diverge, Invariant 3's fence has a gap on one side of it."""
    result = decided()
    proposed = {c["symbol"] for c in result["entries"]["candidates"]}

    assert proposed <= set(result["universe"]["candidates"])


# --- join 2: the book ------------------------------------------------------


def test_the_book_carries_the_market_value_the_caps_are_measured_against():
    """§4.4's caps are fractions of equity measured against position value, and
    `reconcile.Position` has no such field. Losing it would read every held
    position as worthless and silently widen every cap in the system."""
    assert held(decided(), "F")["market_value"] == 14.88


def test_the_book_carries_the_basis_and_the_holding_period():
    """The two fields `plays.py` was blocked on. Three of §6.5's seven exits
    read them and all three fail silently when they are missing."""
    position = held(decided(), "F")

    assert position["entry_price"] == 15.00
    assert position["sessions_held"] == 2


def test_a_missing_market_value_falls_back_rather_than_reading_as_zero():
    """Understating a position loosens §4.4's caps, which is the wrong
    direction to fail in. The quote is the next best measurement of the same
    thing, and it is measured against the *book's* quantity — the broker's
    figure is what goes forward everywhere else, per §12.2."""
    result = decided(
        positions=[{"symbol": "F", "quantity": 1.0}],
        quotes={"F": {"last": 20.00}},
    )

    assert held(result, "F")["market_value"] == 20.00


def test_an_unquoted_position_falls_back_to_what_it_cost():
    """Last resort, and still a real number rather than a zero that would read
    as a position with no weight against any cap."""
    payload = cli_payload(positions=[{"symbol": "F", "quantity": 1.0}])
    payload["quotes"] = {s: q for s, q in payload["quotes"].items() if s != "F"}

    result = cli.decide(payload, cli.load_config())

    assert held(result, "F")["market_value"] == 15.00


# --- join 3: §6.6's catastrophe override -----------------------------------


def test_the_book_carries_how_far_past_its_stop_a_position_is():
    """§6.6's override — "paying a 2% spread to escape a position 15% past its
    stop is correct" — reads `loss_pct_below_stop`, and `guard.py` defaults it
    to 0.0. Nothing else in the system computes it, so without this the
    override can never fire and a flat gate forbids every wide-spread exit."""
    result = decided(
        **OVERNIGHT, quotes={"F": {"bid": 11.90, "ask": 12.10, "last": 12.00}}
    )

    # Entry 15.00, ATR 0.45, so the 3xATR stop is 13.65 and 12.00 is 12.1% under it.
    assert held(result, "F")["loss_pct_below_stop"] == round((13.65 - 12.00) / 13.65, 6)


def test_a_position_inside_its_stop_is_not_past_it():
    assert held(decided(), "F")["loss_pct_below_stop"] == 0.0


def test_an_unknown_basis_cannot_trigger_the_override():
    """The override buys a worse fill. It must not be reachable on absent data."""
    result = decided(reconcile={"positions": [{"symbol": "F", "quantity": 1.0}]})

    assert held(result, "F")["loss_pct_below_stop"] == 0.0


# --- the book on a cycle that does not reconcile ---------------------------


def test_an_overnight_cycle_carries_the_last_reconciled_book_forward():
    """§12.1 runs reconciliation on full, monitor and post-close only, so the
    04:30 and 20:00 cycles have no fresh book and would otherwise exit against
    nothing."""
    result = decided(
        **OVERNIGHT,
        reconcile=None,
        prior_book={
            "trading_day": "2026-08-03",
            "positions": [
                {"symbol": "F", "play_id": "DIP", "quantity": 1.0,
                 "entry_price": 15.00, "sessions_held": 2}
            ],
        },
    )

    assert held(result, "F")["entry_price"] == 15.00


def test_the_carried_book_ages_its_holding_period_forward():
    """A book carried unchanged would hold every position one session longer
    than §6.5 allows, every night. The count is `reconcile.sessions_between`'s,
    reused rather than reimplemented — two holding-period rules would fire a
    time stop a day early in one module and a day late in the other."""
    result = decided(
        **OVERNIGHT,
        reconcile=None,
        prior_book={
            "trading_day": "2026-08-03",
            "positions": [
                {"symbol": "F", "play_id": "DIP", "quantity": 1.0,
                 "entry_price": 15.00, "sessions_held": 2}
            ],
        },
    )

    assert held(result, "F")["sessions_held"] == 3


# --- join 4: what gets placed, and in what order ---------------------------


def test_a_sale_is_never_queued_behind_a_buy():
    """Under T+1 the proceeds are not redeployable this cycle anyway, so the
    ordering buys nothing on the funding side — but a sell placed after a buy
    that failed is a position left open for no reason at all."""
    result = decided(
        # F is through its 3xATR stop at 13.65, so §6.5 exit 4 fires.
        quotes={"F": {"bid": 13.43, "ask": 13.47, "last": 13.45}},
    )
    sides = [o["side"] for o in result["plan"]["orders"]]

    assert sides == sorted(sides, key=lambda side: side != "sell")
    assert planned(result, "sell") == ["F"]


def test_reconciles_cancels_are_carried_into_the_plan_unjudged():
    """Cancels are their own list because guard judges
    placement legality and a cancel has none. They are still part of the one
    thing the routine acts on."""
    result = decided(
        reconcile={
            "positions": [],
            "cancels": [{"order_id": "a1b2", "symbol": "F", "reason": "stale"}],
        }
    )

    assert result["plan"]["cancels"] == [
        {"order_id": "a1b2", "symbol": "F", "reason": "stale"}
    ]


def test_a_denied_order_is_recorded_rather_than_dropped_silently():
    """Invariant 3's audit trail. The plan's stdout is the runlog, and an order
    that vanished without a denial attached is the failure mode §12.2 spends
    its whole table refusing."""
    result = decided(loss_closes=[{"symbol": "PFE", "close_date": "2026-08-01"}])
    codes = [d["code"] for row in result["guard"]["denied"] for d in row["denials"]]

    assert planned(result) == []
    assert guard.WASH_SALE_COOLDOWN in codes


def test_a_halt_stops_the_whole_plan_including_exits():
    """§14's first row. Something that cannot hear "stop" must not act."""
    result = decided(control={"readable": False})

    assert result["guard"]["halted"] is True
    assert result["plan"]["orders"] == []


# --- entries are gated; exits are not --------------------------------------


def test_a_cycle_past_its_daily_cap_still_exits():
    """§13: entries are capped and exits are evaluated on every cycle. A cap
    that also stopped selling would trap the book for the rest of the day."""
    result = decided(
        control={"new_positions": {"date": "2026-08-03", "count": 3}},
        quotes={"F": {"bid": 13.43, "ask": 13.47, "last": 13.45}},
    )

    assert result["cycle"]["entries_allowed"] is False
    assert planned(result, "sell") == ["F"]
    assert planned(result, "buy") == []


def test_a_cycle_that_cannot_buy_does_not_ask_a_play_to_propose():
    """Every proposal costs nothing here, but the runlog would carry a ranked
    list of entries the cycle was never going to place — which is how a record
    stops being read."""
    result = decided(control={"new_positions": {"date": "2026-08-03", "count": 3}})

    assert result["entries"] == {"candidates": [], "skipped": []}


def test_an_overnight_cycle_exits_and_never_enters():
    result = decided(
        **OVERNIGHT,
        reconcile=None,
        prior_book={
            "trading_day": "2026-08-03",
            "positions": [
                {"symbol": "F", "play_id": "DIP", "quantity": 1.0,
                 "entry_price": 15.00, "sessions_held": 2}
            ],
        },
        quotes={"F": {"bid": 13.43, "ask": 13.47, "last": 13.45}},
    )

    assert result["cycle"]["exits_allowed"] is True
    assert planned(result, "buy") == []
    assert planned(result, "sell") == ["F"]


# --- what the routine writes back ------------------------------------------


def test_the_clock_is_stamped_for_the_routine_to_write_at_the_end():
    result = decided()

    assert result["control_updates"]["last_full_cycle"] == "2026-08-03T13:40:00Z"


def test_placing_anything_marks_the_cycle_unreconciled():
    """§12.1: an unreconciled prior cycle that placed orders is itself an
    event, and promotes the next cycle out of no-op. `reconcile.py` clears the
    flag; only this module can set it, because only this module knows an order
    is about to exist."""
    assert decided()["control_updates"]["unreconciled"] is True


def test_a_cycle_that_places_nothing_leaves_the_flag_alone():
    """Setting it unconditionally would keep every later cycle out of §13's
    cheap path forever.

    `NOTHING_TO_PROTECT` is what makes this cycle genuinely empty: the book's
    resting stop and target are orders, and a cycle that placed them has
    something for §12.1 to reconcile next time.
    """
    result = decided(triggers=[], reconcile=NOTHING_TO_PROTECT)

    assert planned(result) == []
    assert "unreconciled" not in result["control_updates"]


def test_the_daily_entry_counter_counts_the_names_that_opened_or_were_approved():
    """§10's new-positions-a-day cap. PFE is approved this cycle but has not
    reconciled yet, so it counts through `approved_new` and carries no entry in
    `filled` — a stale unfilled approval stops counting once its cycle passes."""
    counted = decided()["control_updates"]["new_positions"]

    assert counted == {"date": "2026-08-03", "count": 1, "filled": []}


def test_an_unfilled_approval_from_earlier_today_no_longer_holds_a_slot():
    """The 09:40→11:47 case from the acceptance criterion: MU was approved this
    morning and never filled, so it is not in the reconciled book and not in
    `filled`. This cycle proposes nothing, so the count falls back to what has
    actually opened — zero — and the day's allowance is intact again."""
    result = cli.decide(
        cli_payload(
            triggers=[],
            reconcile=NOTHING_TO_PROTECT,
            control={"new_positions": {"date": "2026-08-03", "count": 1, "filled": []}},
        ),
        cli.load_config(),
    )

    assert result["control_updates"]["new_positions"] == {
        "date": "2026-08-03", "count": 0, "filled": [],
    }


def test_a_name_that_filled_today_stays_counted_even_with_nothing_proposed():
    """The other half: a position the reconciled book shows opened this session
    (oldest lot today, not `adopted`) is counted and persisted in `filled`, so
    it keeps its slot for the rest of the day even on a cycle that proposes
    nothing and even if it is scratched later."""
    result = cli.decide(
        cli_payload(
            triggers=[],
            reconcile={
                "findings": [], "cancels": [],
                "positions": [
                    {"symbol": "F", "play_id": "DIP", "quantity": 1.0,
                     "entry_price": 15.00, "sessions_held": 2, "adopted": False,
                     "adopted_on": None, "high_since_entry": 15.00},
                    {"symbol": "NEE", "play_id": "DIP", "quantity": 1.0,
                     "entry_price": 60.00, "sessions_held": 0, "adopted": False,
                     "adopted_on": None, "high_since_entry": 60.00},
                ],
                "protection_plan": {"place": [], "replace": [], "keep": [], "notes": []},
            },
        ),
        cli.load_config(),
    )

    counted = result["control_updates"]["new_positions"]
    assert counted["count"] == 1
    assert counted["filled"] == ["NEE"]


def test_a_denied_buy_does_not_spend_the_days_allowance():
    result = decided(loss_closes=[{"symbol": "PFE", "close_date": "2026-08-01"}])

    assert result["control_updates"]["new_positions"]["count"] == 0


def test_a_loss_reconcile_writes_this_cycle_still_blocks_a_same_cycle_rebuy():
    """The MGM bypass this audit found: Step 6's `loss_closes` fetch runs
    *before* Step 7's `reconcile`, so a loss `reconcile` realizes this same
    cycle is invisible to the fetched list `decide` otherwise reads. `decide`
    must also read `reconcile.loss_closes` — the rows this cycle's own
    reconcile just computed — or a same-cycle stop-out and re-entry both
    clear the guard."""
    result = decided(
        loss_closes=[],
        reconcile={"loss_closes": [{"symbol": "PFE", "close_date": "2026-08-03"}]},
    )
    codes = [d["code"] for row in result["guard"]["denied"] for d in row["denials"]]

    assert planned(result) == []
    assert guard.WASH_SALE_COOLDOWN in codes


def test_a_fetched_loss_close_still_blocks_alongside_a_same_cycle_one():
    """The two sources are additive, not either/or: a loss fetched at Step 6
    for one symbol must still block that symbol even when this cycle's own
    `reconcile` reports no loss at all."""
    result = decided(
        loss_closes=[{"symbol": "PFE", "close_date": "2026-08-01"}],
        reconcile={"loss_closes": []},
    )
    codes = [d["code"] for row in result["guard"]["denied"] for d in row["denials"]]

    assert planned(result) == []
    assert guard.WASH_SALE_COOLDOWN in codes


def test_a_top_up_on_a_held_name_is_not_a_new_position():
    """§4.2 counts *names*, and the cap is "max new positions per day". A
    second tranche of something already owned opens nothing."""
    result = decided(triggers=[{"symbol": "F", "play_id": "DIP"}],
                     signals={"F": {"rsi": 27.0, "sma_20": 16.00}})

    assert planned(result, "buy") == ["F"]
    assert result["control_updates"]["new_positions"]["count"] == 0


# --- the sector map --------------------------------------------------------


def test_the_issuer_file_supplies_the_sectors_the_caps_use():
    """The holdings file wins every source disagreement, and it
    already carries sector — which saves `get_equity_fundamentals` on every
    candidate in the pool."""
    assert decided()["sectors"]["PFE"] == "Health Care"


def test_robinhood_fills_the_gap_for_an_adopted_non_member():
    """Invariant 7: a position the operator opened by hand can be any symbol at
    all, and `sizing.py` refuses a sectorless candidate outright while
    `guard.py` fails it closed. The fallback is the only way an adopted name
    stays manageable."""
    result = decided(
        positions=[{"symbol": "GME", "quantity": 1.0, "market_value": 30.00}],
        reconcile={"positions": [{"symbol": "GME", "play_id": plays.MANUAL,
                                  "quantity": 1.0, "entry_price": 28.00,
                                  "sessions_held": 4}]},
        sectors={"GME": "Consumer Discretionary"},
    )

    assert result["sectors"]["GME"] == "Consumer Discretionary"


def test_the_fetched_map_cannot_overrule_the_issuer_file():
    """A disagreement is a note, never a resolution — and letting the broker's
    answer win here would make §4.4's sector cap depend on which of two sources
    the routine happened to read last."""
    result = decided(sectors={"PFE": "Energy"})

    assert result["sectors"]["PFE"] == "Health Care"


# --- what guard was told ---------------------------------------------------


def test_guard_is_given_the_rung_sizing_actually_used():
    """§4.4: the caps scale with `n`, and guard denies against the same `n`
    sizing sized against. Two different rungs would have guard refuse the
    sizer's own orders — an account that silently stops trading."""
    result = decided()

    assert result["sizing"]["ladder"]["n_target"] == sizing.n_target(236.42,
                                                                    cli.load_config()["config"])


# --- §6.3's confirming move: derived, not pasted ----------------------------


def test_change_pct_is_derived_off_the_brokers_own_adjusted_close():
    """`get_equity_quotes`'s own guide names `adjusted_previous_close` as the
    daily-change denominator. Arithmetic belongs in `cli.py` (Absolute 2), so
    `_catalyst` must see a number already computed, never the two raw prices
    it would otherwise have to subtract itself."""
    quote = {"last": 105.0, "adjusted_previous_close": 100.0}

    assert cli._change_pct(quote) == pytest.approx(0.05)


def test_change_pct_is_none_without_a_broker_close_to_compare_against():
    """A quote missing the close field — the routine copied `bid`/`ask`/`last`
    only, or the batch's `closes_error` fired — must not silently read as 0%
    change, which would let `_catalyst`'s reaction gate refuse to fire for the
    wrong reason."""
    assert cli._change_pct({"last": 105.0}) is None


# --- the DIP silent-streak counter -----------------------------
#
# `candidates.py`'s own docstring says DIP is the one play it never discovers:
# the live cycle merges the RSI scanner's survivors into `payload["triggers"]`
# itself, and a harness step that silently skips that merge is indistinguishable,
# from inside this repo, from a quiet market with nothing oversold above its
# 200-day. This does not try to tell those apart — only to make repeated zeros
# visible instead of silent, the same way `reconcile._play_state_updates`
# already persists `closed_trades` into `edge_play_state`.


def test_a_dip_trigger_resets_the_streak_to_zero():
    result = decided(play_state={plays.DIP: {"silent_entry_cycles": 4}})

    assert result["play_state_updates"] == {plays.DIP: {"silent_entry_cycles": 0}}


def test_no_dip_trigger_advances_the_streak():
    result = decided(triggers=[], play_state={plays.DIP: {"silent_entry_cycles": 2}})

    assert result["play_state_updates"] == {plays.DIP: {"silent_entry_cycles": 3}}


def test_a_dip_trigger_that_is_later_skipped_still_resets_the_streak():
    """The question is "did discovery run", not "did DIP enter" — a name the
    live re-check refuses is still proof the scan executed."""
    ctx_overrides = {
        "triggers": [{"symbol": "PFE", "play_id": plays.DIP}],
        "signals": {"PFE": {"rsi": 27.0, "sma_20": 27.00, "sma_50": 25.90,
                             "sma_200": 30.00, "atr": 0.50}},  # below its 200-day
        "play_state": {plays.DIP: {"silent_entry_cycles": 4}},
    }
    result = decided(**ctx_overrides)

    assert result["play_state_updates"] == {plays.DIP: {"silent_entry_cycles": 0}}


def test_an_exit_only_cycle_does_not_move_the_streak():
    """Not evidence either way: an overnight cycle never asks DIP's discovery
    step to run at all, so it neither advances nor resets what's there."""
    result = decided(triggers=[], play_state={plays.DIP: {"silent_entry_cycles": 3}}, **OVERNIGHT)

    assert result["play_state_updates"] == {plays.DIP: {"silent_entry_cycles": 3}}


def test_a_noop_cycle_reports_no_play_state_update():
    result = decided(
        now="2026-08-03T14:40:00Z",
        control={
            "last_full_cycle": "2026-08-03T13:40:00Z",
            "positions_held": 0,
            "new_positions": {"date": "2026-08-03", "count": 3},
        },
    )

    assert result["universe"] is None
    assert result["play_state_updates"] == {}


def test_a_stale_universe_does_not_count_against_the_streak():
    """§14: a stale snapshot already proceeds on positions only and pays for
    no discovery of any kind, so a silent DIP here is not evidence of a
    missed harness step and must not count toward the streak."""
    result = decided(
        triggers=[],
        play_state={plays.DIP: {"silent_entry_cycles": 3}},
        snapshot={"as_of": "2026-07-01", "holdings": [], "cross_check": []},
    )

    assert result["play_state_updates"] == {plays.DIP: {"silent_entry_cycles": 3}}


def test_the_streak_crossing_the_alert_threshold_is_noted():
    result = decided(
        triggers=[],
        play_state={plays.DIP: {"silent_entry_cycles": cli.DIP_SILENT_STREAK_ALERT_AT - 1}},
    )

    notes = result["universe"]["notes"]
    assert any(n["code"] == cli.DIP_SILENT_STREAK for n in notes)


def test_the_streak_below_the_threshold_is_not_noted():
    result = decided(
        triggers=[],
        play_state={plays.DIP: {"silent_entry_cycles": cli.DIP_SILENT_STREAK_ALERT_AT - 2}},
    )

    notes = result["universe"]["notes"]
    assert not any(n["code"] == cli.DIP_SILENT_STREAK for n in notes)


def test_the_note_is_a_note_not_a_halt():
    """Consistent with every other §5.3 observation `universe.py` records
    (`TRIGGER_OUTSIDE_UNIVERSE`, `MACRO_TARGET_NOT_IN_ROTATION`, ...): recorded
    rather than raised, and the cycle still places whatever it would have."""
    result = decided(
        triggers=[],
        play_state={plays.DIP: {"silent_entry_cycles": cli.DIP_SILENT_STREAK_ALERT_AT}},
    )

    assert result["universe"]["stale"] is False
    assert result["guard"]["halted"] is False
