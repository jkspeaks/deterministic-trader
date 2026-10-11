"""§6.5 — the seven exits, evaluated top down, on every cycle including
overnight ones."""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping

from src.cadence import sessions_between
from src.sizing import (
    account_implausible,
    drawdown_breached,
    drawdown_floor,
    flow_adjusted_equity,
)

from . import protection
from .catalyst import CATALYST_CLUSTER_HEADLINES, CATALYST_WINDOW_SESSIONS
from .core import (
    ATR_STOP,
    DRAWDOWN_FLATTEN,
    EARNINGS_DEADLINE,
    force_exit_items,
    HARD_EVENT_EXIT,
    ITEM_OFFICER_DEPARTURE,
    MACRO,
    PLAYS,
    SECTOR,
    SECTOR_RSI_EXIT,
    REGULAR_HOURS,
    SIGNAL_EXIT,
    TARGET,
    TARGET_CATALYST,
    TIME_STOP,
    CoveredByResting,
    Exit,
    ExitResult,
    ManualRequired,
    Play,
    _events,
    _is_whole_share,
    _items,
    _operator_dark,
    _price,
    _sessions_ago,
    _signals,
    stop_multiple,
    target_pct as _target_pct,
)
from .dip import DIP, DIP_RSI_EXIT

# §6.5's exits that a resting broker-side order can already be covering. The
# stop covers 4, the target limit covers 6 — and nothing covers 5, because no
# order type at this broker has a clock in it. Exits 1, 2 and 3 are deliberately
# absent: a forced filing, the drawdown flatten and the earnings deadline all
# sell *now*, at whatever the bid is, and a resting order priced for a
# different reason is not cover for any of them.
#
# **A resting stop only ever covers `ATR_STOP`** — the one exit that wants to
# sell at the same price for the same reason. This map is the whole enforcement
# of that: `_covering_order` looks only for a resting order of the *matching*
# kind, so `TIME_STOP`, `HARD_EVENT_EXIT` and `EARNINGS_DEADLINE` are not in it
# at all and `TARGET`/`TARGET_CATALYST` look for a resting limit and never for a
# stop. That matters more, when `protection_rest_kind` made the stop the only
# leg that rests: every one of those four exits now fires on a position carrying
# a resting stop, and reading that stop as cover would leave the position unsold
# on a filing, a clock or an earnings date. What the resting stop does instead
# is *hold the shares* — so `cli._protection_orders` cancels it and
# `plan.orders[i].requires_cancel_of` makes the sell wait for that cancel to be
# acknowledged.
_COVERABLE_CODES = {ATR_STOP: protection.STOP,
                    TARGET: protection.TARGET,
                    TARGET_CATALYST: protection.TARGET}


def _managed_sessions(held: Mapping[str, Any], ctx: Mapping[str, Any]) -> int | None:
    """How long *this system* has held the position — §6.5's time stop clock.

    For a position the system opened, that is ``sessions_held`` and nothing
    more. For an inherited one the two numbers are not the same and the
    difference is the whole point: ``sessions_held`` is derived from the
    broker's oldest tax lot, so an adopted name arrives carrying a clock that
    expired long before deterministic-trader existed. Reading it here would time-stop
    every inherited winner on the first cycle that bound it a play — the
    momentum book sold for a few percent, and the proceeds stranded as cash
    while §4.2's `open_slots` is 0 and nothing can redeploy them.

    Counted from the absolute adoption date rather than from a carried counter,
    for `_sessions_ago`'s reason: a date cannot go stale across a missed cycle,
    and a counter drifts by under-counting, which is the unsafe direction.

    Fails closed. An adopted position with no ``adopted_on`` — a book row
    written before reconcile stamped one — yields None and simply holds, which
    is the same posture the rest of this exit takes on a missing number.
    """
    if not held.get("adopted"):
        return held.get("sessions_held")

    since = held.get("adopted_on")
    trading_day = (ctx.get("cycle") or {}).get("trading_day")
    if since is None or trading_day is None:
        return None

    return sessions_between(
        date.fromisoformat(str(since)),
        date.fromisoformat(str(trading_day)),
        ctx.get("calendar") or {},
    )


def _reason_to_leave(
    held: Mapping[str, Any], ctx: Mapping[str, Any]
) -> tuple[int, str, str] | None:
    """§6.5, evaluated top down. The first rule that fires is the reason.

    Order is the whole content of this function. An account past its drawdown
    halt that is also two sessions from a print leaves for the drawdown, and a
    record that said "time stop" because that check ran first would misattribute
    every position closed in a bad week.
    """
    symbol = held.get("symbol")
    play = PLAYS.get(held.get("play_id"))
    price = _price(ctx, symbol)

    # 1. Hard event exit. Item 4.02 (non-reliance) on a name already held.
    # 5.02 was dropped from `FORCE_EXIT_ITEMS`: the EDGAR Atom summary CI
    # parses carries the item *code* only, not the officer's role, so a
    # routine director election looked identical to a CEO/CFO departure and
    # forced a noise exit. A held name's 5.02 is surfaced as a note in
    # `exits()` now, not an exit.
    forcing = force_exit_items(ctx)
    for event in _events(ctx, symbol):
        forced = _items(event) & forcing
        if forced:
            return (
                1,
                HARD_EVENT_EXIT,
                (f"{symbol} filed an 8-K item {', '.join(sorted(forced))}; §6.5 "
                "exits on the filing and does not wait for the price"),
            )

    # 2. Drawdown halt. The same floor guard denies buys against, measured on
    # the same flow-adjusted equity — this exit flattens the book, so a figure
    # that moved when the operator moved money was the sharpest defect in the
    # system.
    #
    # **The one exit the operator can override, per name.**
    # `edge_control.operator_holds` is a map the operator edits by hand, the
    # same way `entries_blocked_until_cleared` already is — nothing in this
    # codebase writes it. Scoped to this rule alone: item 1 (a forced filing)
    # and item 3 (an earnings deadline) stay mandatory below, because both are
    # "the system never holds through X" commitments, not a risk judgment call
    # the operator's own read of the tape can outweigh the way a portfolio-wide
    # equity floor can. AMD held through this halt and was up
    # $16 the next session — the flatten is a rule, not a certainty, and §10's
    # floor was always a default the operator could see past, not a fact.
    account = ctx.get("account") or {}
    high_water = (ctx.get("control") or {}).get("high_water_mark")
    held_by_operator = symbol in ((ctx.get("control") or {}).get("operator_holds") or {})
    # `guard.ACCOUNT_IMPLAUSIBLE` already denies the buy side of this cycle;
    # this is the exit side of the same suppression.
    implausible = account_implausible(
        account, ctx.get("positions") or [], ctx.get("prior_benchmark") or {}
    )
    if (
        not held_by_operator
        and not implausible
        and high_water is not None
        and drawdown_breached(account, high_water, ctx["config"])
    ):
        floor = drawdown_floor(high_water, ctx["config"], account)
        return (
            2,
            DRAWDOWN_FLATTEN,
            (f"equity {flow_adjusted_equity(account):.2f} net of deposits is "
            f"below the {floor:.2f} drawdown floor; §10 flattens the book"),
        )

    # 3. Earnings deadline. Unlike guard's entry check this stays silent on an
    # unknown date: denying a buy the system cannot promise to exit is prudent,
    # while selling on a missing field would liquidate the book on a bad feed.
    earnings = (ctx.get("earnings") or {}).get(symbol) or {}
    until = earnings.get("sessions_until_report")
    window = int(ctx["config"]["earnings_flatten_days"])
    if earnings.get("status") == "known" and until is not None and int(until) <= window:
        return (
            3,
            EARNINGS_DEADLINE,
            (f"{symbol} reports in {until} session(s); the system never holds "
            "through a print"),
        )

    # 3.5. Same-day-fill guard (after a same-day manual buy-then-sell on MU
    # realised what should have been an avoidable loss). A position that also
    # picked up a fill on today's trading day cannot leave via items 4-7 — the
    # ATR stop, the time stop, the target and the signal exit are all judgment
    # calls about a position's trajectory, and a same-day round trip has not
    # had one yet. Items 1-3 stay mandatory for the same reason item 2's
    # operator-hold carve-out already stops at exactly these three: a forced
    # filing, the drawdown flatten and the earnings deadline are "the system
    # never waits on X" commitments, not a trajectory judgment this guard has
    # any business overriding.
    last_fill_on = held.get("last_fill_on")
    trading_day = (ctx.get("cycle") or {}).get("trading_day")
    if last_fill_on is not None and trading_day is not None and str(last_fill_on) == str(trading_day):
        return None

    # 4. The 2xATR stop. §10.1 is blunt that this is the only protection a
    # FRACTIONAL position has, and that it is evaluated per cycle rather than
    # resting at the broker — so a gap straight through it is not caught.
    atr = _signals(ctx, symbol).get("atr")
    # **A disputed entry price is treated as no entry price at all**.
    # `reconcile._anchored_entry` sets the flag when the broker's
    # `average_buy_price` and its tax lots disagree about what this position
    # cost by more than a percent. When a lot feed reads several percent above
    # the real fill, a 2xATR stop measured down from the wrong number can land
    # at the position's own entry and stop it out the same afternoon.
    #
    # Items 4, 5 and 6 below are the three that read `entry_price`, and all
    # three already have a correct answer for not having one: 4 and 6 do not
    # fire, and 5's `at_a_loss` fails closed and holds. Reusing that is the
    # whole change here — no new branch, no new exit path, just the number
    # withdrawn from the rules that cannot be trusted with it. `exits` raises
    # the `manual_required` row that tells the operator so; items 1-3 (a forced
    # filing, the drawdown flatten, the earnings deadline) are untouched,
    # exactly as they are for the operator-hold carve-out above, because none
    # of them is a judgment about a position's cost.
    entry_price = None if held.get("entry_price_disputed") else held.get("entry_price")
    # Withheld for a play that declares no stop check (`CORE`, the sleeve).
    stop_checked = play is None or play.stop_check
    if stop_checked and atr is not None and entry_price is not None and price is not None:
        # The play's own multiple where it declares one (`SECTOR`, 1.5x
        # per §4.3 C) and `config.atr_stop_multiple` otherwise, which is every
        # position the live account holds. `protection.stop_price` and
        # `sizing`'s risk budget read the same resolver, so the stop this sells
        # through, the stop resting at the broker and the stop the size was
        # measured against are one number.
        multiple = stop_multiple(held.get("play_id"), ctx)
        stop = float(entry_price) - multiple * float(atr)
        if price <= stop:
            return (
                4,
                ATR_STOP,
                (f"{symbol} at {price:.2f} is through its {stop:.2f} stop "
                f"({multiple:g}xATR from a {float(entry_price):.2f} entry"
                f"{_entry_source(held)})"),
            )

    if play is None:
        # §12.2. An adopted trade has no play, so it has no time stop, no
        # target and no signal exit. Inventing one would be the system deciding
        # how long the operator meant to hold something it never chose.
        return None

    # 5. The play's time stop, counted over the sessions *this system* has held
    # the position — see `_managed_sessions`, which is not `sessions_held` for
    # an adopted name.
    #
    # **On an adopted position it cannot realise a loss — until the exemption
    # runs out.** A name inherited underwater is not sold here purely because
    # time passed, which would be "realise a loss to free capital", the churn
    # the operator ruled out: a clock expiring is not a broken thesis. But the
    # exemption is not forever (Phase 0.5 ruling): once the adopted clock
    # reaches `max_adopted_hold_sessions`, an inherited loser that has neither
    # recovered nor tripped a protective exit is occupying a slot indefinitely,
    # and the loss is realised so the capital can be redeployed. Above water
    # the ordinary rule applies at `play.holding_days` regardless. Fails closed
    # on a missing price or entry: no P&L means no evidence the position is in
    # profit, so it holds.
    sessions = _managed_sessions(held, ctx)
    if play.time_stop and sessions is not None and int(sessions) >= play.holding_days:
        at_a_loss = (
            price is None
            or entry_price is None
            or price < float(entry_price)
        )
        adopted_cap = int(ctx["config"]["max_adopted_hold_sessions"])
        exempt = (
            bool(held.get("adopted"))
            and at_a_loss
            and int(sessions) < adopted_cap
        )
        if not exempt:
            capped = bool(held.get("adopted")) and at_a_loss
            tail = (
                f"; the adopted no-loss exemption ends at session {adopted_cap}"
                if capped
                else "; the thesis has had its run"
            )
            return (
                5,
                TIME_STOP,
                (f"{play.play_id} allows {play.holding_days} sessions and this is "
                f"session {sessions}{tail}"),
            )

    # 6. The play's target, or adoption's floor where the play declares none —
    # and, under §4.3 A, DIP's 1.5x-stop-distance R-multiple instead of a flat
    # percentage. `core.target_pct` is the one resolver; `protection.desired`
    # rests a limit at exactly the price this sells at because both call it.
    resolved = _target_pct(held, ctx)
    target_pct, owner = (None, None) if resolved is None else resolved
    if target_pct is not None and entry_price is not None and price is not None:
        target = float(entry_price) * (1.0 + target_pct)
        if price >= target:
            catalyst = _catalyst_window_event(ctx, symbol)
            if catalyst is not None:
                return (
                    6,
                    TARGET_CATALYST,
                    (f"{symbol} at {price:.2f} has made {owner}'s "
                    f"{target_pct:.0%} target of {target:.2f}, and a "
                    f"qualifying {catalyst.get('type') or 'event'} confirms "
                    "it — exiting now on confirmed momentum instead of "
                    "waiting for a possible pullback"),
                )
            return (
                6,
                TARGET,
                (f"{symbol} at {price:.2f} has made {owner}'s "
                f"{target_pct:.0%} target of {target:.2f}"),
            )

    # 7. The play's own signal. Only DIP, SECTOR and MACRO declare one.
    return _signal_exit(held, ctx, play, price)


def _catalyst_window_event(
    ctx: Mapping[str, Any], symbol: str
) -> Mapping[str, Any] | None:
    """§6.5 item 6's refinement: the same trigger `_catalyst`'s entry side
    checks (an 8-K, or a headline cluster, within `CATALYST_WINDOW_SESSIONS`),
    read here as a confirmation rather than a proposal.

    Deliberately does not re-check `_catalyst`'s price/volume reaction test —
    that test exists to confirm a *proposed* entry has real conviction behind
    it; here the position is already past its target, which is the exit
    side's own confirmation that the tape agrees.
    """
    for event in _events(ctx, symbol):
        ago = _sessions_ago(event, ctx, "filed_day")
        if ago is None or int(ago) > CATALYST_WINDOW_SESSIONS:
            continue
        if (
            event.get("type") == "8-K"
            or int(event.get("headline_count") or 0) >= CATALYST_CLUSTER_HEADLINES
        ):
            return event
    return None


def _signal_exit(
    held: Mapping[str, Any], ctx: Mapping[str, Any], play: Play, price: float | None
) -> tuple[int, str, str] | None:
    """§6.5 exit 7 — "e.g. `DIP` RSI >= 50"."""
    symbol = held.get("symbol")

    if play.play_id == DIP:
        rsi = _signals(ctx, symbol).get("rsi")
        if rsi is not None and float(rsi) >= DIP_RSI_EXIT:
            return (
                7,
                SIGNAL_EXIT,
                (f"{symbol} has recovered to RSI {float(rsi):.0f}; the dip DIP "
                "bought has closed"),
            )

    if play.play_id == SECTOR:
        # §4.3 C's "exit at RSI >= 55". Read off the ETF's own oscillator the
        # same way DIP's is — `signals`, not `regime_inputs`: by the time a
        # position exists the symbol is in the book and therefore in the
        # per-symbol fetch, and the exit path must not depend on the premarket
        # job having run.
        rsi = _signals(ctx, symbol).get("rsi")
        if rsi is not None and float(rsi) >= SECTOR_RSI_EXIT:
            return (
                7,
                SIGNAL_EXIT,
                (f"{symbol} has recovered to RSI {float(rsi):.0f}; the "
                f"oversold sector §4.3 C bought has rotated back"),
            )

    if play.play_id == MACRO:
        # §6.4's "regime flip", and the *operator's* switch alone — an explicit
        # `regime_fit = 0.0` in `edge_play_state`. An unwritten row is not a
        # flip, and neither is the deterministic regime going risk-off: that
        # one stops new entries (`plays.entries`), and letting it also liquidate
        # every rotation already on the book would turn a vol spike into a
        # forced sale of the sleeve that exists to ride the cycle out. Exits
        # stay the operator's to force, the same asymmetry this check has
        # always had, now that the entry side no longer fails closed at all.
        dark = _operator_dark(ctx, MACRO)
        if dark is not None:
            reason = dark
            return (
                7,
                SIGNAL_EXIT,
                f"the macro regime {symbol} was bought for has flipped: {reason}",
            )

    return None


def _covering_order(
    held: Mapping[str, Any], ctx: Mapping[str, Any], code: str
) -> Mapping[str, Any] | None:
    """A resting broker order that already sells this position for this reason.

    §6.5 keeps being evaluated on every cycle after an earlier resting orders,
    deliberately — belt and braces, because a resting order can be cancelled by
    the operator, rejected at placement, or simply never have reached the
    broker, and a system that stopped checking would learn about any of those
    the next time it read a position that should not still be there. What
    changes is only whether the exit is *placed*: an `ATR_STOP` behind a
    resting stop at or above the same price, or a `TARGET` behind a resting
    limit at or below the same price, is a second sale of shares the first one
    already sold. `guard` would refuse it `DUPLICATE_OPEN_ORDER` and the cycle
    would carry a denial that means nothing was wrong.

    Three conditions, and the price one is the load-bearing one. A resting
    stop *below* the computed stop has not triggered and is not cover — it is
    an out-of-date order that `reconcile.protection_plan` is about to replace,
    and until it does the position genuinely needs selling. The quantity test
    is the same argument: a resting order for 1 of 3 shares covers a third of
    the position and none of the exit.
    """
    kind = _COVERABLE_CODES.get(code)
    if kind is None:
        return None

    symbol = held.get("symbol")
    quantity = float(held.get("quantity") or 0.0)
    epsilon = float(ctx["config"]["reconcile_epsilon_quantity"])

    if kind == protection.STOP:
        wanted = protection.stop_price(held, ctx)
    else:
        target = protection.target_price(held, ctx)
        wanted = None if target is None else target[0]
    if wanted is None:
        return None

    for order in ctx.get("open_orders") or []:
        if order.get("symbol") != symbol:
            continue
        if protection.resting_kind(order) != kind:
            continue
        price = protection.resting_price(order)
        if price is None:
            continue
        if float(order.get("quantity") or 0.0) + epsilon < quantity:
            continue
        if (price >= wanted) if kind == protection.STOP else (price <= wanted):
            return order

    return None


# The loss-sell news-hold (`_news_hold_gate`/`_hold_day`) and the average-down
# offer (`_average_down_gate`) were removed. Both were
# prompt-resident judgments — `exits.py`
# computed a mechanical gate, then a live session decided the outcome from
# untrusted news text with no schema and no audit trail. The news-hold never
# opened once in production (0 `held_for_news` rows) and the average-down offer
# contradicted `plays/top_up.py`'s "never average down". `HeldForNews`,
# `AverageDownEligible`, `LOSS_HOLD_MAX_DAY` and `LOSS_HOLD_EXTENDED_MAX_DAY`
# are kept in `plays.core` (deprecated) so an old runlog row still parses;
# `ExitResult.held_for_news` and `.average_down_eligible` stay in the output
# shape as permanently-empty lists so nothing downstream has to change.


# The code `manual_required` carries for a position standing down on a basis
# its two feeds disagree about. Mirrors `reconcile.POSITION_ENTRY_DISPUTED`,
# which is the finding side of the same fact — one for the reconciliation
# report, one for the operator alert, because they are read by different people
# at different times and `report.py` only walks the second.
ENTRY_PRICE_DISPUTED = "ENTRY_PRICE_DISPUTED"


def _entry_source(held: Mapping[str, Any]) -> str:
    """`", <source>"` for an exit reason, or `""` where the book predates it.

    Written into every `ATR_STOP` reason so the next audit of a stop that fired
    at a surprising price can read where its anchor came from off the runlog row
    itself. A reason that says only `2xATR from a <price> entry` cannot say
    whether that price was a tax-lot figure or the position's own fill without
    a source trace of four modules.

    Empty rather than "unknown" on an older book row: a reason string is read by
    a person, and a parenthetical that says nothing is noise in every line it
    appears in.
    """
    source = held.get("entry_price_source")
    return "" if not source else f", {source}"


def exits(ctx: Mapping[str, Any]) -> ExitResult:
    """§6.5 for every held position. Unconditional, on every cycle type.

    Entries are gated by cadence and denied by guard; exits are not gated at
    all. The 04:30 and 20:00 cycles exist precisely to catch a post-close
    reaction, and a module that checked ``entries_allowed`` here would make
    both pointless.
    """
    cycle = ctx.get("cycle") or {}
    session = cycle.get("session")
    fractional_market_ok = bool(ctx["config"].get("fractional_exit_market_ok", True))
    leaving: list[Exit] = []
    covered: list[CoveredByResting] = []
    manual_required: list[ManualRequired] = []
    notes: list[str] = []

    # §6.5 item 2 cannot be evaluated on an unreadable equity, and must not be
    # guessed at: an absent/zero/non-numeric `equity_total` once became a large
    # negative flow-adjusted figure and flattened the whole book.
    # `drawdown_breached` now returns False on that read, so no DRAWDOWN_FLATTEN
    # is emitted below — guard halts the cycle with ACCOUNT_UNREADABLE instead.
    # The note records that §10 was skipped for a data reason, not because the
    # book was healthy.
    account = ctx.get("account") or {}
    if (
        (ctx.get("control") or {}).get("high_water_mark") is not None
        and flow_adjusted_equity(account) is None
    ):
        notes.append(
            "DRAWDOWN_FLATTEN not evaluated: account equity_total is missing, "
            "zero or non-numeric; §10 cannot be measured and the cycle halts "
            "with ACCOUNT_UNREADABLE rather than flattening the book"
        )
    elif account_implausible(
        account, ctx.get("positions") or [], ctx.get("prior_benchmark") or {}
    ):
        notes.append(
            "DRAWDOWN_FLATTEN not evaluated: account equity_total does not "
            "cover the value of open positions (or has collapsed against "
            "yesterday's benchmark); ACCOUNT_IMPLAUSIBLE denies buys this "
            "cycle rather than flattening the book on a read that has not "
            "been confirmed"
        )

    symbol_lock = ctx.get("symbol_lock") or {}
    strategy = ctx.get("strategy") or "core"

    for held in ctx.get("positions") or []:
        symbol = held.get("symbol")

        # Dip Recovery plan phase 2, defense in depth. `guard.lock` is the
        # hard gate and denies the sell regardless — this only keeps a
        # position the lock says belongs to the other side from being
        # proposed at all, so it never generates a denial or an alert about
        # an exit that was never going to be allowed to place.
        owner = symbol_lock.get(symbol)
        if owner is not None and owner != strategy:
            continue

        # 8-K item 5.02 stopped forcing an exit: CI's EDGAR Atom parse carries
        # the item code without the officer's role, so a routine director
        # election read the same as a CEO/CFO departure. A held name's 5.02 is
        # now a note for the operator to eyeball, not an automatic sale.
        if ITEM_OFFICER_DEPARTURE not in force_exit_items(ctx) and any(
            ITEM_OFFICER_DEPARTURE in _items(event) for event in _events(ctx, symbol)
        ):
            notes.append(
                f"{symbol} filed an 8-K item {ITEM_OFFICER_DEPARTURE} (officer or "
                "director change); no longer a forced exit — review by hand if it "
                "names a principal officer"
            )

        # The alert half of the entry-price hold. `_reason_to_leave` has
        # already withheld `entry_price` from items 4-6 for this symbol; this
        # is what stops that from being silent. Raised before the reason is
        # computed, and independently of it, because the point is precisely
        # that no exit will be proposed — a hold nobody is told about is
        # indistinguishable from a position with nothing wrong.
        if held.get("entry_price_disputed"):
            manual_required.append(
                ManualRequired(
                    symbol,
                    ENTRY_PRICE_DISPUTED,
                    (
                        f"{symbol}'s entry price is disputed between the "
                        "broker's average_buy_price and its tax lots; §6.5's "
                        "ATR stop, time stop and target are not evaluated on "
                        "it and no protective order is placed, replaced or "
                        "cancelled behind it. Check the position's cost basis "
                        "at the broker and, if a resting stop is at the wrong "
                        "price, move it by hand — this system will not correct "
                        "a live order off data it cannot reconcile"
                    ),
                )
            )

        reason = _reason_to_leave(held, ctx)
        if reason is None:
            continue

        priority, code, message = reason

        # Ahead of everything else deliberately: there is no placement to
        # intercept when the sale is already resting at the broker.
        covering = _covering_order(held, ctx, code)
        if covering is not None:
            covered.append(
                CoveredByResting(
                    symbol=symbol,
                    code=code,
                    order_id=covering.get("order_id"),
                    reason=(
                        f"{message}; a resting {covering.get('type')} sell "
                        f"({covering.get('order_id')}) already covers it, so no "
                        "second order is placed"
                    ),
                )
            )
            continue

        quantity = float(held.get("quantity") or 0.0)
        bid = ((ctx.get("quotes") or {}).get(symbol) or {}).get("bid")

        # A whole-share exit is a limit at the bid, in every session: no market
        # fallback for a whole position, ever.
        #
        # It now places as a MARKET sell when `fractional_exit_market_ok` is set
        # and the cycle is in regular hours, which the broker does allow for a
        # fractional quantity. `manual_required` still catches the two cases a
        # market order cannot cover: no bid to price it against (guard would
        # deny it `ORDER_NOT_PRICEABLE`), and a fractional exit outside regular
        # hours (a fractional market order queues to the next open rather than
        # filling, so a hand-placed limit at the open is the honest fallback).
        if not _is_whole_share(quantity):
            if fractional_market_ok and session == REGULAR_HOURS and bid is not None:
                leaving.append(
                    Exit(
                        symbol=symbol,
                        play_id=held.get("play_id"),
                        priority=priority,
                        code=code,
                        reason=(
                            f"{message}; {quantity:g} shares is fractional — a "
                            "market sell in regular hours, the broker's own "
                            "fractional path (§4.3)"
                        ),
                        quantity=quantity,
                        order_type="market",
                        session=session,
                        limit_price=None,
                    )
                )
                continue
            if bid is None:
                detail = "no bid available to price the exit"
            elif not fractional_market_ok:
                detail = (
                    "fractional_exit_market_ok is off, so a fractional lot has "
                    "no automatic exit path"
                )
            else:
                detail = (
                    "a fractional limit is rejected (§4.3) and a fractional "
                    "market order only queues outside regular hours"
                )
            manual_required.append(
                ManualRequired(
                    symbol,
                    code,
                    f"{message}; {quantity:g} shares is a fractional quantity — "
                    f"{detail} — place a limit sell manually",
                )
            )
            continue

        if bid is None:
            manual_required.append(
                ManualRequired(
                    symbol, code, f"{message}; no bid available to price a limit order"
                )
            )
            continue

        leaving.append(
            Exit(
                symbol=symbol,
                play_id=held.get("play_id"),
                priority=priority,
                code=code,
                reason=message,
                quantity=quantity,
                order_type="limit",
                session=session,
                limit_price=float(bid),
            )
        )

    return ExitResult(
        exits=sorted(leaving, key=lambda e: (e.priority, e.symbol)),
        covered_by_resting=sorted(covered, key=lambda c: (c.code, c.symbol)),
        held_for_news=[],
        manual_required=manual_required,
        average_down_eligible=[],
        notes=notes,
    )
