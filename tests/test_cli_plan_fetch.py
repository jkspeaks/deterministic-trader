"""§13's cheap path, and the list that keeps the prompt from guessing.

The load-bearing property is the first test. §13: *"The no-op path must be
genuinely cheap. Read lock, pending events and unreconciled state in a single
Supabase query, then exit before touching Robinhood. A prompt that runs
`get_portfolio` 'just to check' first turns 14 invocations into 11 full MCP
sessions and the economics collapse."* `edge_control` has already been read by
the time this command runs, so a no-op must return **nothing to fetch at all**.

Everything else here is §5.3's asymmetry made operational: universe size is
free and candidate-set size costs a Robinhood call per symbol per cycle. The
checkout steps are free too, which is the whole point of a settled design
decision — CI commits its extraction to the repo and the routine reads a file.
"""

import pytest

from src import cadence, cli
from tests.factories import cli_payload


def fetched(payload: dict) -> dict:
    return cli.plan_fetch(payload, cli.load_config())


def steps(payload: dict, source: str | None = None) -> list[str]:
    return [
        step["step"]
        for step in fetched(payload)["fetch"]
        if source is None or step["source"] == source
    ]


# --- the cheap path --------------------------------------------------------


def test_a_noop_cycle_fetches_nothing_at_all():
    """The economics of the whole system. `edge_control` is already read, and
    a no-op that returned one Robinhood step would turn a cheap invocation into
    a full MCP session."""
    quiet = cli_payload(
        now="2026-08-03T14:40:00Z",
        control={
            "last_full_cycle": "2026-08-03T13:40:00Z",
            "positions_held": 0,
            "unreconciled": False,
            "pending_events": False,
            "new_positions": {"date": "2026-08-03", "count": 3},
        },
    )
    result = fetched(quiet)

    assert result["cycle"]["cycle_type"] == cadence.NOOP
    assert result["fetch"] == []
    assert result["estimate"]["calls"] == 0


def test_a_weekend_firing_fetches_nothing():
    """P5: there is no weekend equity session. Saturday 09:40."""
    assert fetched(cli_payload(now="2026-08-08T13:40:00Z"))["fetch"] == []


# --- what a cycle that trades needs ----------------------------------------


def test_a_full_cycle_fetches_what_guard_reads():
    """Guard fails closed on every one of these. A cycle that skipped
    tradability would have `_legality_denials` refuse every order as
    NOT_TRADABLE, which reads exactly like a broken account."""
    named = steps(cli_payload())

    assert {"positions", "orders", "tradability", "quotes", "earnings"} <= set(named)


def test_a_full_cycle_reconciles_before_it_decides():
    """§12.1: reconciliation runs at the start of every full, monitor and
    post-close cycle, and it needs the broker's orders and the tax lots that
    carry §9's basis."""
    named = steps(cli_payload())

    assert {"orders", "tax_lots", "runlog"} <= set(named)


def test_the_ci_extraction_arrives_as_files_not_as_calls():
    """The routine sandbox
    reaches `github.com` and nothing else, so CI commits its extraction to the
    repo and this is a file read inside Invariant 2."""
    checkout = steps(cli_payload(), source="checkout")

    assert {"universe", "events", "macro"} <= set(checkout)


def test_checkout_steps_cost_no_calls():
    """§7.4's arithmetic only counts what a session actually spends."""
    result = fetched(cli_payload())
    free = [s for s in result["fetch"] if s["source"] == "checkout"]

    assert free != []
    assert all(step["calls"] == 0 for step in free)


# --- §5.3: candidate set size is what costs -------------------------------


def test_a_per_candidate_step_does_not_pretend_to_know_its_own_cost():
    """The candidate set does not exist until `universe.resolve` runs, and it
    cannot run until `edge_events` has been read — which is one of the things
    this list is telling the routine to read. An invented count would be a
    number nobody could act on."""
    quotes = next(s for s in fetched(cli_payload())["fetch"] if s["step"] == "quotes")

    assert quotes["scope"] == "candidates"
    assert quotes["calls"] is None


def test_the_estimate_separates_what_is_known_from_what_is_not():
    result = fetched(cli_payload())

    assert result["estimate"]["calls"] > 0
    assert result["estimate"]["per_candidate_steps"] > 0


def test_a_position_scoped_step_is_costed_from_edge_control():
    """`positions_held` is already in hand — it is one of the four fields §13
    lets cadence reach the no-op decision from."""
    result = fetched(cli_payload(control={"positions_held": 3}))
    lots = next(s for s in result["fetch"] if s["step"] == "tax_lots")

    assert lots["scope"] == "positions"
    assert lots["calls"] == 3


# --- the cycle types that fetch less --------------------------------------


def test_an_overnight_cycle_does_not_pay_for_entry_data():
    """§13: overnight cycles place exits only. Loss closes gate a *buy* and
    the universe snapshot exists to admit one, so both are dead weight at
    04:30 — and the snapshot going unread is why `universe.resolve` degrades
    to positions-only rather than raising."""
    overnight = cli_payload(
        now="2026-08-04T00:30:00Z",
        control={"has_whole_share_position": True, "positions_held": 1},
    )

    assert fetched(overnight)["cycle"]["cycle_type"] == cadence.OVERNIGHT
    assert "loss_closes" not in steps(overnight)


def test_every_cycle_that_may_exit_fetches_the_resting_orders_it_would_cancel():
    """The overnight cycle does not reconcile, but an exit cancels first — so it
    fetches the resting row (and only that row: the recent-terminal window feeds
    reconcile's fills, which it skips)."""
    overnight = cli_payload(
        now="2026-08-04T00:30:00Z",
        control={"has_whole_share_position": True, "positions_held": 1},
    )
    result = fetched(overnight)
    rows = [s for s in result["fetch"] if s["tool"] == "get_equity_orders"]

    assert result["cycle"]["cycle_type"] == cadence.OVERNIGHT
    assert result["cycle"]["reconcile_required"] is False
    assert len(rows) == 1
    assert rows[0]["step"] == "orders"
    assert rows[0]["state"] == cli.OPEN_ORDER_STATE
    assert "created_at_gte" not in rows[0]


def test_an_overnight_cycle_still_prices_its_own_spread_gate():
    """§6.6: spread is measured live at the moment of the exit decision and
    never cached. An overnight cycle without quotes cannot exit anything."""
    overnight = cli_payload(
        now="2026-08-04T00:30:00Z",
        control={"has_whole_share_position": True, "positions_held": 1},
    )
    quotes = next(s for s in fetched(overnight)["fetch"] if s["step"] == "quotes")

    assert quotes["scope"] == "positions"


def test_a_postclose_cycle_fetches_the_frozen_book_it_has_to_price():
    """§1's second benchmark plus SPY — twelve names `report.py` cannot value
    the day without, and §8.4 says the series cannot be rebuilt afterwards.
    The routine is told which twelve rather than being expected to know."""
    postclose = cli_payload(now="2026-08-03T20:30:00Z")
    quotes = next(s for s in fetched(postclose)["fetch"] if s["step"] == "quotes")

    assert quotes["scope"] == "listed"
    assert "SPY" in quotes["symbols"]
    assert set(cli.load_config()["inception"]["inventory"]) <= set(quotes["symbols"])


def test_a_reconciling_cycle_with_no_entries_still_reads_the_loss_ledger():
    """`reconcile._anchored_entry` reads §9's ledger to tell a
    wash-sale-adjusted tax lot from a disputed one. Fetched on entry cycles
    only, every post-close cycle would re-dispute INVH and strip its stop."""
    postclose = cli_payload(now="2026-08-03T20:30:00Z")
    plan = fetched(postclose)

    assert plan["cycle"]["cycle_type"] == cadence.POSTCLOSE
    assert not plan["cycle"]["entries_allowed"]
    assert steps(postclose).count("loss_closes") == 1


def test_an_entry_cycle_reads_the_loss_ledger_exactly_once():
    assert steps(cli_payload(now="2026-08-03T13:40:00Z")).count("loss_closes") == 1


def test_reconcile_hands_the_loss_ledger_to_the_anchor():
    """The fetch is worthless if `cli.reconcile`'s explicit ctx drops the key."""
    from src import reconcile as reconciler

    seen = {}
    real = reconciler.run

    def spy(ctx):
        seen["loss_closes"] = ctx.get("loss_closes")
        return real(ctx)

    row = {"symbol": "F", "close_date": "2026-07-29", "loss_amount": 1.0,
           "basis_unknown": False}
    reconciler.run = spy
    try:
        cli.reconcile(cli_payload(loss_closes=[row]), cli.load_config())
    finally:
        reconciler.run = real

    assert seen["loss_closes"] == [row]


def test_a_premarket_cycle_places_nothing_and_pays_for_nothing_placeable():
    """§13: premarket sets the day's regime and live plays. Tradability gates
    an order, and this cycle has none."""
    premarket = cli_payload(now="2026-08-03T12:45:00Z")

    assert fetched(premarket)["cycle"]["cycle_type"] == cadence.PREMARKET
    assert "tradability" not in steps(premarket)


# --- the shape the routine reads ------------------------------------------


@pytest.mark.parametrize("source", ["robinhood", "supabase", "checkout"])
def test_every_step_names_a_source_a_tool_and_a_scope(source):
    """The prompt should never have to infer any of the three. §8.1: there is
    exactly one decider, and a routine choosing its own tools for a named step
    is a second one."""
    named = [s for s in fetched(cli_payload())["fetch"] if s["source"] == source]

    assert named != []
    assert all({"step", "source", "tool", "scope", "calls"} <= set(s) for s in named)


# --- the indicators step: response size is pinned, not chosen ---------------


def test_the_indicators_step_pins_its_own_call_parameters():
    """`get_equity_technical_indicators` defaults to a full series -- roughly
    24x the size of a single trimmed bar, on the highest-count step in the
    fetch plan. Naming `interval`, `output` and `start_time` here is §8.1's
    rule applied to the same step that motivated it: a routine choosing its
    own value is a second decider."""
    indicators = next(
        s for s in fetched(cli_payload())["fetch"] if s["step"] == "indicators"
    )

    assert indicators["interval"] == "day"
    assert indicators["output"] == "latest"
    assert "start_time" in indicators


def test_the_indicator_lookback_is_relative_to_this_cycles_now():
    """Not a calendar constant: a cycle firing on a different day must not
    reuse yesterday's window. Verified live against the broker: a 200-day
    SMA returns the identical value whether `start_time` reaches back a year
    or one day, so a short, fixed lookback is exact -- the tool back-extends
    for its own warm-up regardless of what is asked for."""
    early = fetched(cli_payload(now="2026-08-03T13:40:00Z"))
    later = fetched(cli_payload(now="2026-08-04T13:40:00Z"))

    early_start = next(
        s for s in early["fetch"] if s["step"] == "indicators"
    )["start_time"]
    later_start = next(
        s for s in later["fetch"] if s["step"] == "indicators"
    )["start_time"]

    assert early_start == "2026-07-29T13:40:00Z"
    assert later_start == "2026-07-30T13:40:00Z"


def test_a_cycle_that_only_sells_still_pins_indicator_parameters():
    """The pin is not conditional on scope. An overnight cycle degrades
    `indicators` to `positions` (§5.3) but still fetches `rsi` and `atr` on
    every held name, and those calls cost the same full series if left
    unpinned."""
    overnight = cli_payload(
        now="2026-08-04T00:30:00Z",
        control={"has_whole_share_position": True, "positions_held": 1},
    )
    indicators = next(
        s for s in fetched(overnight)["fetch"] if s["step"] == "indicators"
    )

    assert indicators["scope"] == "positions"
    assert indicators["output"] == "latest"


# --- fundamentals_live: CATALYST's volume confirmation, entry cycles only ---


def test_fundamentals_live_is_fetched_on_an_entry_allowed_cycle():
    """§6.3's volume-confirmation gate. Scoped to `candidates`, same as
    `indicators`/`tradability`/`earnings` on a cycle that may buy."""
    fundamentals = next(
        s for s in fetched(cli_payload())["fetch"] if s["step"] == "fundamentals_live"
    )

    assert fundamentals["source"] == "robinhood"
    assert fundamentals["tool"] == "get_equity_fundamentals"
    assert fundamentals["scope"] == "candidates"


def test_fundamentals_live_is_absent_on_an_exit_only_cycle():
    """CATALYST never proposes an exit, so a held position never needs it --
    unlike `indicators`, this step does not degrade to `positions`, it drops
    out of the plan entirely."""
    overnight = cli_payload(
        now="2026-08-04T00:30:00Z",
        control={"has_whole_share_position": True, "positions_held": 1},
    )

    assert "fundamentals_live" not in steps(overnight)
    assert "fundamentals" not in steps(overnight)


# --- earnings_reactions: §6.1's reaction routine, read back cheaply ---------


def test_earnings_reactions_is_read_on_an_entry_allowed_cycle():
    """PEAD never proposes an exit either, so this is scoped and gated the
    same way `fundamentals_live` is -- a cheap Supabase read, not a Robinhood
    call, same shape as `loss_closes`."""
    reactions = next(
        s for s in fetched(cli_payload())["fetch"] if s["step"] == "earnings_reactions"
    )

    assert reactions["source"] == "supabase"
    assert reactions["tool"] == "edge_earnings_reactions_recent"
    assert reactions["calls"] == 1


def test_earnings_reactions_is_absent_on_an_exit_only_cycle():
    overnight = cli_payload(
        now="2026-08-04T00:30:00Z",
        control={"has_whole_share_position": True, "positions_held": 1},
    )

    assert "earnings_reactions" not in steps(overnight)


# --- the day-context cache --------------------------------------------------
#
# Freshness moved per-symbol (see `db/schema.sql` and
# `src.cli.day_context_plan`) — the row-level `edge_control.last_data_fresh_at`
# stamp these tests used to exercise is no longer read by `_fetch_plan` at all.
# `day_context` is now always listed alongside `indicators`/`earnings` for any
# cycle that needs signals; the routine narrows the live calls to whichever symbols
# `day_context_plan` marks `stale`, once Step 5 has resolved the candidate set —
# `_fetch_plan` runs before that, so it cannot narrow anything itself (`_calls`'s
# own docstring on why CANDIDATES-scope cost is unknowable at plan_fetch time still
# applies, unchanged). Per-symbol behaviour itself is covered in
# `tests/test_cli_day_context.py`.


def test_day_context_indicators_and_earnings_all_appear_together():
    """No more either/or: `day_context` is a cheap, always-present read, and
    `indicators`/`earnings` are always listed too — the actual skip happens
    per-symbol, later, not as a step this list can omit."""
    named = steps(cli_payload())

    assert "day_context" in named
    assert "indicators" in named
    assert "earnings" in named


def test_the_old_row_level_control_fields_no_longer_affect_the_fetch_list():
    """`last_data_fresh_at`/`pending_events` used to gate whether `day_context`
    or `indicators`/`earnings` appeared. They no longer do anything here —
    the fetch list is identical whether they're absent, stale, or claim to be
    fresh."""
    bare = steps(cli_payload())
    claims_fresh = steps(
        cli_payload(
            control={
                "last_data_fresh_at": {"trading_day": "2026-08-03", "fetched_at": "2026-08-03T13:39:00Z"},
                "pending_events": False,
            }
        )
    )

    assert bare == claims_fresh


def test_the_live_step_order_is_stable():
    """`quotes`, then `day_context`, then `indicators`, then `tradability`,
    then `earnings`, then `events` — a reviewer catch on the original design
    found a draft that grouped `indicators`/`earnings` ahead of `tradability`,
    silently swapping their relative order; pinning the whole sequence here
    keeps that from recurring under the new always-present shape."""
    named = steps(cli_payload())

    assert (
        named.index("quotes")
        < named.index("day_context")
        < named.index("indicators")
        < named.index("tradability")
        < named.index("earnings")
        < named.index("events")
    )


def test_the_day_context_step_costs_one_call_not_one_per_candidate():
    result = fetched(cli_payload())
    day_context = next(s for s in result["fetch"] if s["step"] == "day_context")

    assert day_context["calls"] == 1
    assert day_context["scope"] == cli.ACCOUNT


def test_orders_are_fetched_as_two_bounded_calls_never_the_whole_history():
    """A bare `get_equity_orders` was 66k characters, the harness spilled it to a
    file the runner may not read, and the monitor aborted in Step 2. The list
    must name the resting state (of any age: a GTC target placed last week
    still rests) and a recent window (terminal orders), and no row may be the
    unfiltered call."""
    rows = [
        step
        for step in fetched(cli_payload())["fetch"]
        if step["tool"] == "get_equity_orders"
    ]

    assert len(rows) == 2
    assert all(row["step"] == "orders" for row in rows)
    assert {"state": "confirmed"}.items() <= rows[0].items()
    assert "created_at_gte" not in rows[0]
    assert "state" not in rows[1]
    trading_day = fetched(cli_payload())["cycle"]["trading_day"]
    assert rows[1]["created_at_gte"] == cli._orders_since(trading_day)
    assert rows[1]["created_at_gte"] < trading_day
