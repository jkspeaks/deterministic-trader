"""report.py — benchmarks, the alert queue, and the weekly scorecard.

Design v3 §1, §8.3, §8.4 and §11. This module measures and it speaks. It
decides nothing, places nothing, and runs after the cycle has already traded —
§11 separates measurement from decision, and a measurement module that could
place an order would be a second decider on one account, which is the failure
§13.1's lock exists for and one that has already happened here once.

Three jobs, and they are one job:

* **The §8.4 daily row.** §1 asks whether an agentic system beats a competent
  human doing this by hand, against three bars — SPY from inception, the
  inception book held unchanged, and the operator's own 24%/year. Two of the
  three are prices on a particular day and cannot be recovered later, which is
  why §8.4 writes them daily rather than computing them on demand.
* **The §8.3 queue.** Six things are notified and four deliberately are not.
  The right-hand column of that table is the harder half: a system that pushes
  every fill is one the operator stops reading, and once it is unread the
  drawdown halt is unread with it. So "not notified" is a rule with tests.
* **The weekly scorecard**, which is the only row that fires when nothing is
  wrong — and therefore the only thing that gives the system's silence a
  meaning.

**What this module deliberately does not own.**

``edge_control.high_water_mark`` belongs to `reconcile.py`. §10 says the mark
is maintained by the code that reads it, but `guard.py` denies against it
mid-session and this module runs at the close; a mark written here would leave
guard measuring a stale floor all day.

§8.3's **"relay stalled / channel dead"** and §14's **"cycle did not run"** are
both outside this module's reach. An alert about the alert queue, written into
the alert queue, is the one alert that cannot be delivered; and a cycle that
did not run runs no code, so nothing inside it can report its absence. Catching
either takes an observer living outside the cycle, not this module.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import median
from typing import Any, Mapping, Sequence

from src import deployment as deployments
from src.cadence import ET, FULL, MONITOR, OVERNIGHT, PREMARKET, SATURDAY, is_open
from src.plays import ADOPTED, UNCHOSEN
from src.reconcile import (
    OTHER_STRATEGY_ORDER,
    RECONCILIATION_DIVERGENCE_PERSISTENT,
    UNTRACKED_FILL,
)
from src.sizing import (
    account_implausible,
    anchored_mark,
    drawdown_breached,
    drawdown_floor,
    flow_adjusted_equity,
    raw_equity,
)

# §13's post-close is the only cycle that writes the day's row. The three §1
# benchmarks are close-to-close series, and a monitor cycle that wrote one
# would mark the day at 11:40.
POSTCLOSE = "postclose"

# §1's first benchmark. The market proxy is a symbol like any other, quoted
# through the same call as everything else.
SPY = "SPY"

# A measurement this module declined to make, recorded rather than raised.
# §14's shape: degradation, not failure.
QUOTE_UNAVAILABLE = "QUOTE_UNAVAILABLE"

# §1's third benchmark — "the operator's own realised return in 2025, which is
# the bar that actually matters." A report-only threshold, so it lives here
# rather than in config, exactly as the play thresholds live in `plays.py`.
ANNUAL_TARGET = 0.24

# Julian year. The leap day is one part in 1,460 of the bar and costs nothing
# to get right.
DAYS_PER_YEAR = 365.25

# Returns are ratios, and a ratio printed to more places than this is claiming
# precision the underlying cent-rounded prices do not have.
RETURN_PRECISION = 6

# §8.3's left-hand column — everything this system will ever say unprompted.
# Five titles, because a queue whose titles are generated strings cannot be
# read at a glance and cannot be grepped in `edge_alerts` afterwards.
#
# `_status_alert` below folds all three into the one push `run()` now sends
# every cycle. Kept as constants only so an old `edge_alerts` row still reads
# back with a known title; `report.py` enqueues none of them any more.
RECONCILIATION_DIVERGENCE = "Reconciliation divergence"
PLACEMENT_BLOCKED = "Placement blocked"
NO_ACTIVITY_SUMMARY = "09:40 summary — no buy, no sell"

# `CYCLE_FAILED` is different from the three above: it is not `report.py`'s to
# retire, because `docs/routine/cycle-prompt.md` Step 0 enqueues it by hand
# before a cycle even has a `cycle_type` to build `"<Routine> cycle failed"`
# from (the checkout itself can fail), and again on any mid-cycle abort that
# never reaches Step 9. `report._status_alert` still derives its own
# `f"{routine} cycle failed"` for every failure it *can* see — this bare title
# is the fallback for the ones it structurally cannot.
CYCLE_FAILED = "Cycle failed"

# The
# pre-market data job (`services/core/core/premarket.py`) accumulated its
# per-symbol Twelve Data errors into `edge_premarket_state.accum.errors` —
# queryable, but never alerted, so a degraded day's data went unnoticed
# unless someone thought to check. `_finalize` (that module) enqueues this on
# any accumulated error (threshold: any error at all — one bad symbol still
# means one candidate's `day_context` never warmed), `priority: "default"`;
# `run_premarket`'s own top-level exception handler enqueues it too, at
# `priority: "high"`, since that path means the whole job never finished a
# single step. Not a `"<Routine> cycle <status>"` title — this job is not a
# `cadence`-typed cycle and never reaches `report.py` at all.
PREMARKET_DATA_DEGRADED = "Pre-market data job degraded"

# All three are enqueued by the database or by `services/core/core/steps.py`,
# never by `report.py` — the same reason `PREMARKET_DATA_DEGRADED` is here: they
# are not `"<Routine> cycle <status>"` titles and never reach `_status_alert`.
# `edge_step_watchdog()` (pg_cron) raises the first two; the finalize gate
# in `run._finalize_phase` the third.
CYCLE_STALLED = "Cycle stalled"
CYCLE_OVER_BUDGET = "Cycle over budget"
STEP_LOG_GAP = "Step log gap"

# Raised by the hosted core's `resolve` phase when the symbol ceiling
# (`config.resolve_max_symbols`) trims discovery names, or when held positions
# and sector ETFs alone exceed it. The schema teaches `edge_alerts_enqueue` the
# title; until it is applied the core's enqueue is fail-soft
# (`run._report_scope`), so the order of deploy and migration is safe.
RESOLVE_SCOPE_TRIMMED = "Resolve scope trimmed"

DRAWDOWN_HALT = "Drawdown halt"
# Fires instead of `DRAWDOWN_HALT` when the equity read itself cannot
# be trusted — a retyped `§10` inside a hand-built alert body is exactly the
# failure this title exists to stop repeating.
ACCOUNT_IMPLAUSIBLE_ALERT = "Account read implausible"
WEEKLY_SCORECARD = "Weekly scorecard"

# `_status_alert` absorbs a play-state change into the one push's body now.
PLAY_STATE_CHANGED = "Play state changed"

# §6.5 items 4/5's news-hold and the limit-only order policy it shipped
# alongside. Both are additions to the queue, not replacements — the original
# six titles are unaffected. Unlike a routine fill, neither is
# silent-by-design: a hold is the system declining its own stop, and a
# manual-required exit is one `exits.py` computed but cannot place at all, so
# both need the operator's attention every time, not just on divergence. The
# string is kept so an old `edge_alerts` row still reads back with a known
# title; nothing enqueues it.
LOSS_SELL_HELD = "Loss-sell held for news"

# The two free-text titles — the only ones
# a session/runner may still hand-compose outside `report.py` (Step 8a/8b's
# unconfirmed-cancel and manual-limit cases), because they fire from inside
# order placement, before `report.run()` ever sees the cycle. Every other
# title `edge_alerts_enqueue` accepts is generated, never typed.
ORDER_REFUSED = "Order refused"
MANUAL_ACTION_REQUIRED = "Manual action required"

# Kept so an old row still reads back with a known title; nothing enqueues it
# any more.
MANUAL_LIMIT_REQUIRED = "Manual limit required"

# How many shadow-variant lines the weekly
# scorecard may carry. Five, because the alert is read on a phone and the
# scorecard already spends seven lines on the account itself — a report nobody
# scrolls to the bottom of reports nothing. A sixth variant collapses into a
# "+N more" line rather than pushing the play lines off the screen.
SHADOW_MAX_LINES = 5
# The row every other variant is read against (`services/core/core/shadow.BASELINE`).
# Named here rather than imported: `src/` may not import `services/`, and one
# string in two places is cheaper than that dependency pointing the wrong way.
SHADOW_BASELINE = "baseline"
# The one line of chrome the block gets. Seven days because that is
# `edge_shadow_recent`'s default window and the scorecard is weekly.
_SHADOW_HEADING = "\nshadow 7d (unit-weighted, hypothetical)\n"
# Kept so an old `edge_alerts` row still reads back with a known title;
# nothing enqueues it.
ENTRY_PEAK_SKIPPED = "Entry skipped — peak risk"

# `edge_benchmark.equity` ran hundreds of dollars
# higher across a week with `net_deposits` flat and the broker reporting a few
# dollars realised in total, and nothing said so. §1's first
# benchmark is only answerable while every transfer is in
# `config/inception.json` — an unrecorded one lands as equity and is
# arithmetically a gain (a withdrawal, a loss), and the same move ratchets
# §10's high-water mark up onto money the strategy never made. This is the row
# that refuses to let that happen silently: post-close only, deterministic, and
# quiet unless two consecutive daily rows disagree by more than the recorded
# flows explain.
BENCHMARK_INTEGRITY = "Benchmark integrity — unexplained equity move"

# `edge_alerts.priority`, which the relay passes to `notify.push()` unchanged.
PRIORITY_URGENT = "urgent"
PRIORITY_HIGH = "high"
PRIORITY_DEFAULT = "default"

# §12.2's one finding that is not an error: "alerting on the operator's own
# deliberate action is exactly the noise that makes a system tiresome."
#
# Stated as an exclusion rather than a list of what does alert, so a finding
# code added to `reconcile.py` later is notified until someone decides
# otherwise. Silence is the failure that hides; noise is the failure that gets
# fixed. It is not silenced for being unimportant. `_divergence_alert` has
# exactly one envelope, titled `RECONCILIATION_DIVERGENCE`, and this finding is
# by construction *not* a divergence: it is the other strategy's own resting
# order, reported by `reconcile.py` precisely because nothing else scans
# working orders. Pushing it would assert a reconciliation divergence that did
# not happen, once per cycle for as long as an ordinary Dip Recovery limit
# order sits at the broker. A mislabelled push is worse than the noise it
# makes: it trains the operator to discount the title that matters. The finding
# stays in `reconcile.json` and in the runlog, which is where a fact this well
# explained belongs.
SILENT_FINDINGS = frozenset({UNTRACKED_FILL, OTHER_STRATEGY_ORDER})

# §6.4's regime flip, and `plays.py` agrees: only an explicit 0.0 switches a
# play off. Conviction drifting from 1.0 to 0.4 is the agent having an
# opinion, which is its job rather than news.
REGIME_OFF = 0.0

# The lowest alert priority. §3.3's "downgrade to `min` priority or log-only"
# for a quiet cycle — a push the operator's phone does not need to buzz for, but
# one that still proves the cycle ran, which a dropped push cannot.
PRIORITY_MIN = "min"

# The outcome word, derived here (never in the prompt) from the same signals
# `_status_alert` folds into the one push's body. Six of the eight
# `edge_session_stats.status` values: `stale_output` and `invalid_output`
# describe a JSON-shape failure this module never sees (it runs on state
# already loaded), so they are some other layer's word to write, not this
# one's.
STATUS_SUCCESS = "success"
STATUS_SUCCESS_WITH_WARNINGS = "success_with_warnings"
STATUS_PARTIAL_SUCCESS = "partial_success"
STATUS_BLOCKED = "blocked"
STATUS_FAILED = "failed"
# `run._session_status` returns this for a NOOP cycle (`SESSION_SKIPPED`,
# same string) and `_status_alert` now takes that word verbatim via
# `ctx["session_status"]` rather than only ever deriving one of the five
# above from `_cycle_status`'s own fallback ladder, which has no NOOP branch
# and would otherwise compute `success` for a cycle that fetched and decided
# nothing at all.
STATUS_SKIPPED = "skipped"

# The word before the dash in every generated title. Keyed off `cadence`'s own constants rather than repeating the literal
# strings, so a fifth cycle type added there cannot silently fall through to
# the title-cased fallback in `_routine_label` without someone noticing here.
_ROUTINE_LABELS = {
    FULL: "Full",
    PREMARKET: "Pre-market",
    MONITOR: "Monitor",
    POSTCLOSE: "Post-close",
    OVERNIGHT: "Overnight",
}

_STATUS_PHRASE = {
    STATUS_SUCCESS: "succeeded",
    STATUS_SUCCESS_WITH_WARNINGS: "succeeded with warnings",
    STATUS_PARTIAL_SUCCESS: "partial",
    STATUS_BLOCKED: "blocked",
    STATUS_FAILED: "failed",
    STATUS_SKIPPED: "skipped",
}

_STATUS_PRIORITY = {
    STATUS_SUCCESS: PRIORITY_MIN,
    STATUS_SUCCESS_WITH_WARNINGS: PRIORITY_DEFAULT,
    STATUS_PARTIAL_SUCCESS: PRIORITY_HIGH,
    STATUS_BLOCKED: PRIORITY_HIGH,
    STATUS_FAILED: PRIORITY_HIGH,
    # A NOOP cycle is not news — same quiet treatment as a plain success.
    STATUS_SKIPPED: PRIORITY_MIN,
}


def _routine_label(cycle_type: str | None) -> str:
    """The title's leading word. An unknown `cycle_type` degrades to a
    title-cased guess rather than raising — a report a test fixture forgot to
    stamp should still produce a readable title, not a crash on the one path
    that exists to say what went wrong.
    """
    if not cycle_type:
        return "Cycle"
    return _ROUTINE_LABELS.get(cycle_type, cycle_type.replace("_", " ").title())


# The canonical title set, generated rather than
# enumerated: 5 routine labels x 5 status phrases, plus the six titles that
# stay their own alert (`DRAWDOWN_HALT`, `ACCOUNT_IMPLAUSIBLE_ALERT`,
# `WEEKLY_SCORECARD`, `BENCHMARK_INTEGRITY`, `ORDER_REFUSED`,
# `MANUAL_ACTION_REQUIRED`). `db/schema.sql`'s
# `edge_alerts_enqueue` rejects anything else with the same two halves
# (a regex over the generated half, a literal list over the fixed half) —
# the SQL is the enforcement, this is the definition it must stay in sync
# with, named here because Python is where the definition actually lives.
CANONICAL_TITLES = frozenset(
    {
        f"{label} cycle {phrase}"
        for label in _ROUTINE_LABELS.values()
        for phrase in _STATUS_PHRASE.values()
    }
    | {
        DRAWDOWN_HALT,
        ACCOUNT_IMPLAUSIBLE_ALERT,
        WEEKLY_SCORECARD,
        BENCHMARK_INTEGRITY,
        ORDER_REFUSED,
        MANUAL_ACTION_REQUIRED,
        # The bootstrap/mid-abort fallback (see `CYCLE_FAILED`'s own comment)
        # — a session enqueues it by hand outside `report.py` entirely, so it
        # has to be canonical even though `_status_alert` never generates it.
        CYCLE_FAILED,
        # `services/core/core/premarket.py` enqueues this directly too, for
        # the same reason `CYCLE_FAILED` is here — it never reaches
        # `_status_alert`, since the pre-market job is not a `cadence` cycle.
        PREMARKET_DATA_DEGRADED,
        # Raised by `edge_step_watchdog()` and the finalize gate.
        CYCLE_STALLED,
        CYCLE_OVER_BUDGET,
        STEP_LOG_GAP,
        # The resolve ceiling trimmed discovery names.
        RESOLVE_SCOPE_TRIMMED,
    }
)


def _doc_slug(title: str) -> str:
    """GitHub's own `## Heading` -> `#heading` anchor rule: lowercase, drop
    anything that is not a letter, digit, space or hyphen, then turn each
    space into a hyphen **without** collapsing runs — an em dash stripped out
    of "Benchmark integrity — unexplained equity move" leaves the two spaces
    that surrounded it, which become two consecutive hyphens, exactly the
    anchor GitHub itself would generate from that `## ` heading. This is what
    makes a footer's doc link and `docs/OPERATIONS.md`'s heading resolve to
    the same anchor without hand-syncing them.
    """
    lowered = title.lower()
    kept = "".join(ch for ch in lowered if ch.isalnum() or ch in " -")
    return kept.replace(" ", "-")


def _doc_link(title: str) -> str:
    return f"docs/OPERATIONS.md#{_doc_slug(title)}"


@dataclass(frozen=True)
class Note:
    """Something the measurement could not do, said out loud."""

    code: str
    symbol: str
    detail: str

    def as_dict(self) -> dict:
        return {"code": self.code, "symbol": self.symbol, "detail": self.detail}


@dataclass(frozen=True)
class Benchmark:
    """One day of the §8.4 series.

    Five columns where §8.4 specified four, settled. A deposit arrives as
    equity and is arithmetically identical to a gain, so a series that records
    only equity credits the agent with the operator's own money — permanently,
    because §8.4's reason for writing this daily is that it cannot be rebuilt
    afterwards. ``net_deposits`` is the column that keeps §1's first benchmark
    answerable.

    Raw values, not returns. Every §1 comparison is derivable from these five
    plus the inception baseline, and a stored ratio would have to be recomputed
    from scratch the first time one of its inputs was corrected.
    """

    trading_day: str
    equity: float
    net_deposits: float
    spy_close: float | None
    frozen_value: float | None

    def as_dict(self) -> dict:
        return {
            "trading_day": self.trading_day,
            "equity": self.equity,
            "net_deposits": self.net_deposits,
            "spy_close": self.spy_close,
            "frozen_value": self.frozen_value,
        }


@dataclass(frozen=True)
class PlayLine:
    """One play's week. §1: a bad month must be attributable to a named play.

    ``closed_trades`` and ``realized_pnl`` are cumulative since inception and
    are read from `edge_play_state`, not recomputed here. ``realized_pnl``
    and ``near_flat_trades`` are credited there by
    `edge_realized_trades_append`, once per broker-reported closing trade;
    ``closed_trades`` is still `reconcile.py`'s counter.

    ``near_flat_trades`` counts closes small in dollars *and* against their
    basis (`reconcile._realized_trades`) — a result that exists but says
    nothing about the play's edge. Shown, never subtracted.
    """

    play_id: str
    regime_fit: float | None
    reason: str | None
    closed_trades: int
    realized_pnl: float
    near_flat_trades: int = 0

    def as_dict(self) -> dict:
        return {
            "play_id": self.play_id,
            "regime_fit": self.regime_fit,
            "reason": self.reason,
            "closed_trades": self.closed_trades,
            "realized_pnl": self.realized_pnl,
            "near_flat_trades": self.near_flat_trades,
        }


@dataclass(frozen=True)
class Scorecard:
    """§8.3's weekly row: where the account stands against §1's three bars.

    Returns rather than levels, because the whole point is a comparison and
    three dollar figures on three different bases is not one. Each is None
    exactly when the column it derives from is None — a bar that could not be
    measured says so instead of reading zero, which would look like a tie.
    """

    trading_day: str
    days: int
    equity: float
    net_deposits: float
    # `agent_return` is money-weighted against the *deposit-adjusted* base
    # (`inception_equity + net_deposits`), not the raw inception base — the
    # +222% an earlier audit caught was exactly this base missing the
    # operator's own contributed capital. `agent_gain` is the same comparison
    # in dollars, since a percentage alone cannot be told apart from a
    # percentage of a much smaller account.
    agent_return: float | None
    agent_gain: float | None
    spy_return: float | None
    frozen_return: float | None
    target_return: float
    # Execution, not return: over the trailing 7 days, the share of placed
    # orders that got any fill, and the median signed slippage against the
    # limit (positive is worse than the limit, whichever side). Both None when
    # `fill_quality_week` is absent or empty — no orders placed is not 0% fill.
    fill_rate: float | None
    median_slippage: float | None
    plays: list[PlayLine]
    # One line per closing trade this week whose position's entry basis was
    # disputed — see `_data_quality_lines`. Empty in every ordinary week, and
    # then the scorecard carries no such line at all.
    data_quality: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "trading_day": self.trading_day,
            "days": self.days,
            "equity": self.equity,
            "net_deposits": self.net_deposits,
            "agent_return": self.agent_return,
            "agent_gain": self.agent_gain,
            "spy_return": self.spy_return,
            "frozen_return": self.frozen_return,
            "target_return": self.target_return,
            "fill_rate": self.fill_rate,
            "median_slippage": self.median_slippage,
            "plays": [p.as_dict() for p in self.plays],
            "data_quality": list(self.data_quality),
        }


@dataclass(frozen=True)
class Alert:
    """One row of `edge_alerts`, in the shape the relay drains.

    `cycle_id` is stamped by `cli.py` on insert — this module knows what
    happened, not which invocation it happened in.
    """

    title: str
    body: str
    priority: str

    def as_dict(self) -> dict:
        return {"title": self.title, "body": self.body, "priority": self.priority}


@dataclass
class ReportResult:
    benchmark: Benchmark | None = None
    scorecard: Scorecard | None = None
    alerts: list[Alert] = field(default_factory=list)
    control_updates: dict = field(default_factory=dict)
    notes: list[Note] = field(default_factory=list)
    # The word `_status_alert` derived for this cycle, exposed here so
    # `cli.report()`'s output carries it straight through to whoever calls
    # `edge_session_status_apply` (the runner, or the session prompt) — a
    # second reading of the alerts list to re-derive it would risk
    # disagreeing with the title already pushed.
    status: str = STATUS_SUCCESS

    def as_dict(self) -> dict:
        return {
            "benchmark": None if self.benchmark is None else self.benchmark.as_dict(),
            "scorecard": None if self.scorecard is None else self.scorecard.as_dict(),
            "alerts": [a.as_dict() for a in self.alerts],
            "control_updates": dict(self.control_updates),
            "notes": [n.as_dict() for n in self.notes],
            "status": self.status,
        }


def _quote(ctx: Mapping[str, Any], symbol: str) -> float | None:
    """Today's price, or None. Absent and unpriced are deliberately one case.

    A quotes map that omits a delisted holding says nothing about whether the
    frozen book still contains it, and treating the omission as a zero would
    answer that question the flattering way.
    """
    last = ((ctx.get("quotes") or {}).get(symbol) or {}).get("last")
    return None if last is None else float(last)


def net_deposits(inception: Mapping[str, Any], trading_day: str) -> float:
    """Capital added on or before ``trading_day``, cumulative since inception.

    Dated rather than running, so a missed cycle cannot lose one and a replayed
    cycle cannot double one. Withdrawals are negative amounts; there are none
    by design, but the arithmetic does not care.
    """
    return round(
        math.fsum(
            float(row["amount"])
            for row in inception.get("deposits") or []
            if row["date"] <= trading_day
        ),
        2,
    )


def _frozen_value(ctx: Mapping[str, Any]) -> tuple[float | None, list[Note]]:
    """§1's second benchmark: the inception book, marked at today's prices.

    The cash line is part of the book. The do-nothing control held $14.52 in
    cash and earned nothing on it, and a control that quietly dropped it would
    be a smaller, better-performing account than the one that existed.

    **All of it or none of it.** A missing quote contributes zero to a sum, so
    a partly-priced book is always a *cheaper* book — the control gets easier
    to beat exactly on the days the data is worst, and §8.4 says nobody can go
    back and check. One name short is not a benchmark, it is a number that
    looks like one.
    """
    inception = ctx["inception"]
    total = float(inception["cash"])
    notes: list[Note] = []

    for symbol, shares in sorted((inception.get("inventory") or {}).items()):
        price = _quote(ctx, symbol)
        if price is None:
            notes.append(
                Note(
                    QUOTE_UNAVAILABLE,
                    symbol,
                    f"{symbol} is in the inception book and has no quote today; "
                    "the frozen benchmark is unavailable rather than short one "
                    "holding",
                )
            )
            continue
        total += float(shares) * price

    return (None if notes else round(total, 2)), notes


def _benchmark(ctx: Mapping[str, Any]) -> tuple[Benchmark, list[Note]]:
    trading_day = ctx["cycle"]["trading_day"]
    frozen, notes = _frozen_value(ctx)
    spy = _quote(ctx, SPY)

    if spy is None:
        # One hole does not void the row. Equity and the frozen book are still
        # true today and unobtainable tomorrow.
        notes.append(
            Note(
                QUOTE_UNAVAILABLE,
                SPY,
                "SPY has no quote today; the market benchmark has a gap for "
                f"{trading_day} and the rest of the row stands",
            )
        )

    return (
        Benchmark(
            trading_day=trading_day,
            equity=float(ctx["account"]["equity_total"]),
            net_deposits=net_deposits(ctx["inception"], trading_day),
            spy_close=spy,
            frozen_value=frozen,
        ),
        notes,
    )


def _integrity_alert(ctx: Mapping[str, Any], benchmark: Benchmark) -> list[Alert]:
    """`BENCHMARK_INTEGRITY`: an equity move two daily rows apart that the
    recorded deposits do not account for.

    The gap is ``(equity_today − equity_prev) − (net_deposits_today −
    net_deposits_prev)``. Both deposit figures are read from
    `config/inception.json`, so a transfer back-dated before ``equity_prev``'s
    day sits on both sides and nets out; only a flow that is missing outright,
    or mis-dated into this one interval, survives the subtraction.

    Silent unless ``|gap|`` clears ``max(3% of equity_prev, $25)`` **and** no
    fill landed that day — a move on a day the account traded is explained by
    the trading, and §8.4's series cannot be rebuilt to tell the two apart
    later. ``prior_benchmark`` absent — the first row the account ever writes —
    is silent too: one row is not a series.
    """
    prior = ctx.get("prior_benchmark") or {}
    if prior.get("equity") is None:
        return []
    if ctx.get("fills"):
        return []

    equity_prev = float(prior["equity"])
    equity_today = benchmark.equity
    equity_move = round(equity_today - equity_prev, 2)
    deposits_today = benchmark.net_deposits
    deposits_prev = net_deposits(ctx["inception"], prior.get("trading_day") or "")
    deposits_move = round(deposits_today - deposits_prev, 2)

    unexplained = round(equity_move - deposits_move, 2)
    threshold = round(max(0.03 * abs(equity_prev), 25.0), 2)
    if abs(unexplained) <= threshold:
        return []

    return [
        Alert(
            BENCHMARK_INTEGRITY,
            f"Equity moved {_money(equity_move)} ({_money(equity_prev)} on "
            f"{prior.get('trading_day')} -> {_money(equity_today)} on "
            f"{benchmark.trading_day}) while recorded net deposits moved "
            f"{_money(deposits_move)} ({_money(deposits_prev)} -> "
            f"{_money(deposits_today)}). {_money(unexplained)} is unexplained, "
            f"past the {_money(threshold)} threshold (max of 3% of the prior "
            "equity and $25) on a day with no fill. A transfer is missing from "
            "config/inception.json or the equity read is wrong; §10's drawdown "
            "floor and §1's scorecard are both derived from this series.\n\n"
            + _doc_link(BENCHMARK_INTEGRITY),
            PRIORITY_HIGH,
        )
    ]


def _is_week_end(day: date, calendar: Mapping[str, Any]) -> bool:
    """Whether ``day`` is the last session the week has.

    Derived from the calendar rather than from the weekday, so a Good Friday
    moves the scorecard to Thursday instead of cancelling it. The scorecard is
    the only §8.3 row that fires when nothing is wrong, so a week that skipped
    one would teach the operator that silence means nothing in particular.

    The scan stops at Saturday because P5 established there is no weekend
    equity session — the calendar cannot make one open.
    """
    following = day + timedelta(days=1)
    while following.weekday() < SATURDAY:
        if is_open(following, calendar):
            return False
        following += timedelta(days=1)
    return True


def _ratio(value: float | None, base: float) -> float | None:
    """A return against ``base``, or None when the level could not be measured.

    None rather than 0.0. A bar that could not be priced reads as a tie if it
    reads as zero, and a tie is a claim.
    """
    if value is None or not base:
        return None
    return round(value / base - 1.0, RETURN_PRECISION)


def _play_lines(ctx: Mapping[str, Any]) -> list[PlayLine]:
    """Every play the agent has an opinion about, traded or not.

    A play that is live and has not fired is a fact about the week. Listing
    only plays with closed trades would make one that stopped triggering
    indistinguishable from one that was switched off, which is the exact
    distinction §1 wants the scorecard to preserve.
    """
    state = ctx.get("play_state") or {}
    return [
        PlayLine(
            play_id=play_id,
            regime_fit=row.get("regime_fit"),
            reason=row.get("reason"),
            closed_trades=int(row.get("closed_trades") or 0),
            realized_pnl=round(float(row.get("realized_pnl") or 0.0), 2),
            near_flat_trades=int(row.get("near_flat_trades") or 0),
        )
        for play_id, row in sorted(state.items())
    ]


def _data_quality_lines(ctx: Mapping[str, Any]) -> tuple[str, ...]:
    """This week's closes that were reached off a disputed entry basis.

    ``realized_trades_week`` is `edge_realized_trades_recent(7)`, verbatim. A
    trade here happened and its money is in its play's total — it is named so
    the operator does not draw a conclusion about the play from it, which is
    the case of an exit for a few cents off a basis two feeds disagreed about.
    """
    lines = []
    for row in ctx.get("realized_trades_week") or []:
        if not row.get("entry_disputed"):
            continue
        day = str(row.get("trading_day") or "")[5:10]
        gain = float(row.get("realized_gain") or 0.0)
        sign = "+" if gain >= 0 else "-"
        lines.append(
            f"  Data-quality: {row.get('symbol')} {day} {sign}{_money(abs(gain))}, "
            "disputed entry basis, excluded from strategy conclusions"
        )
    return tuple(lines)


def _fill_quality_summary(ctx: Mapping[str, Any]) -> tuple[float | None, float | None]:
    """The trailing week's fill rate and median slippage, or ``(None, None)``.

    ``fill_quality_week`` is the routine's copy of `edge_fill_quality_recent(7)`
    — one row per terminal order the core placed. "Filled" is any fill at all:
    a partially filled entry still traded, and the number the operator wants is
    how often an order the system placed did *nothing*. Absent or empty is not
    a zero — a week that placed no orders has no fill rate to report.
    """
    rows = ctx.get("fill_quality_week") or []
    if not rows:
        return None, None

    filled = sum(1 for r in rows if float(r.get("filled_qty") or 0.0) > 0.0)
    slips = [float(r["slippage"]) for r in rows if r.get("slippage") is not None]
    return (
        round(filled / len(rows), RETURN_PRECISION),
        round(median(slips), RETURN_PRECISION) if slips else None,
    )


def _max_drawdown(pnls: list[float]) -> float:
    """The deepest peak-to-trough of the cumulative P&L, as a negative number.

    Summed rather than compounded: the shadow ledger carries no position
    sizing — every variant's trades are unit-weighted by construction, because
    the point of the comparison is the *rule*, and letting `sizing` scale them
    would make a variant look better for having been luckier about which day
    the cash happened to be free. ``0.0`` for a series that never gave
    anything back.
    """
    peak = running = worst = 0.0
    for pnl in pnls:
        running += pnl
        peak = max(peak, running)
        worst = min(worst, running - peak)
    return worst


def _shadow_lines(ctx: Mapping[str, Any]) -> list[str]:
    """One line per shadow variant: count, win rate, mean/median, drawdown.

    ``shadow_week`` is `edge_shadow_recent(7)`, verbatim — the rows
    `services/core/core/shadow.py` wrote by replaying the week's stored cycles
    against each rule variant. The arithmetic is here
    for the same reason `_fill_quality_summary`'s is: measurement is this
    module's job, and a SQL function that returned an average would be a
    second place the definition of "win rate" lived.

    **Only closed trades count.** A row with no `pnl_pct` is an entry that
    never filled, never found bars, or is still open — real findings, and
    readable in `edge_shadow_trades`, but not returns. Averaging them in as
    zeros would drag every variant toward the baseline it exists to be
    distinguishable from.

    `baseline` sorts first because every other row is read against it; the
    rest sort by name, so the block does not reshuffle week to week.
    """
    by_variant: dict[str, list[float]] = {}
    for row in ctx.get("shadow_week") or []:
        variant, pnl = row.get("variant"), row.get("pnl_pct")
        if not variant or pnl is None:
            continue
        try:
            by_variant.setdefault(variant, []).append(float(pnl))
        except (TypeError, ValueError):
            continue

    ordered = sorted(by_variant, key=lambda name: (name != SHADOW_BASELINE, name))
    lines = [
        f"  {name} {len(pnls)}t "
        f"{_rate(sum(1 for pnl in pnls if pnl > 0) / len(pnls))}w "
        f"avg {_pct(sum(pnls) / len(pnls))} med {_pct(median(pnls))} "
        f"dd {_pct(_max_drawdown(pnls))}"
        for name, pnls in ((name, by_variant[name]) for name in ordered)
    ]
    if len(lines) > SHADOW_MAX_LINES:
        kept = SHADOW_MAX_LINES - 1
        lines = lines[:kept] + [
            f"  +{len(lines) - kept} more variants (edge_shadow_trades)"
        ]
    return lines


def _scorecard(ctx: Mapping[str, Any], benchmark: Benchmark) -> Scorecard:
    """§1's three bars, plus the account, plus who did it.

    Computed from the row written moments ago rather than from the context
    again, so the numbers pushed to the operator are the numbers stored in
    `edge_benchmark`. Two readings of one cycle that disagreed would be
    discovered by the operator, in a notification, which is the worst place.
    """
    inception = ctx["inception"]
    base = float(inception["equity"])
    days = (
        date.fromisoformat(benchmark.trading_day)
        - date.fromisoformat(inception["inception_date"])
    ).days
    fill_rate, median_slippage = _fill_quality_summary(ctx)

    # Money-weighted against the *deposit-adjusted* base, not the raw
    # inception base.
    deposit_adjusted_base = base + benchmark.net_deposits
    agent_gain = (
        None
        if deposit_adjusted_base == 0
        else round(benchmark.equity - deposit_adjusted_base, 2)
    )

    return Scorecard(
        trading_day=benchmark.trading_day,
        days=days,
        equity=benchmark.equity,
        net_deposits=benchmark.net_deposits,
        agent_return=_ratio(benchmark.equity, deposit_adjusted_base),
        agent_gain=agent_gain,
        spy_return=_ratio(benchmark.spy_close, float(inception["spy_close"])),
        frozen_return=_ratio(benchmark.frozen_value, base),
        # Compounded and pro-rated: 24%/year is an annual rate, and six days of
        # it is 0.35%, not 24% and not a linear 24/52.
        target_return=round(
            (1.0 + ANNUAL_TARGET) ** (days / DAYS_PER_YEAR) - 1.0, RETURN_PRECISION
        ),
        fill_rate=fill_rate,
        median_slippage=median_slippage,
        plays=_play_lines(ctx),
        data_quality=_data_quality_lines(ctx),
    )


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.2%}"


def _rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _money(value: float) -> str:
    return f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"


def _failure_fragment(ctx: Mapping[str, Any]) -> str | None:
    """§14's first rows, folded into `_status_alert`'s one push.

    The detail is passed through verbatim. §14: the exact broker rejection
    string is written to the runlog "never paraphrased and never bucketed" —
    "Limit order quantity cannot include fractional shares" is the sentence
    that diagnosed an entire invariant, and `ORDER_REJECTED` would have
    diagnosed nothing. The same argument applies to what reaches the operator.
    """
    failure = ctx.get("failure")
    if not failure:
        return None
    return f"{failure.get('step')} failed: {failure.get('detail')}"


def _divergence_findings(ctx: Mapping[str, Any]) -> list[dict]:
    """§12.2's findings, `SILENT_FINDINGS` dropped. Shared by `_cycle_status`
    (which only needs to know whether a `PERSISTENT` one is in here) and
    `_status_alert`'s body fragment (which needs the detail lines).
    """
    return [
        f
        for f in (ctx.get("reconcile") or {}).get("findings") or []
        if f.get("code") not in SILENT_FINDINGS
    ]


def _divergence_fragment(findings: list[dict]) -> str | None:
    """One line per finding, folded into `_status_alert`'s one push.

    Five findings on a cycle is one thing wrong, not five. A push per finding
    is how a bad afternoon spends the operator's attention before the
    important message arrives.
    """
    if not findings:
        return None
    return "\n".join(
        f"{f.get('code')} {f.get('symbol')}: {f.get('detail')}" for f in findings
    )


def _drawdown_alert(ctx: Mapping[str, Any]) -> tuple[list[Alert], dict]:
    """§10's halt, alerted on the transition rather than on the state.

    The breach stays true for as long as the operator takes to decide what to
    do, so alerting on the state would push it eleven times a day throughout
    that deliberation — which is both useless and the surest way to have the
    next one ignored.

    **The dedupe is `edge_control.last_drawdown_alert`, not
    ``trading_enabled``**. Nothing in the system writes the kill switch: §10's
    halt is already latched by the ratcheting high-water mark and enforced by
    `guard.py` denying every buy beneath the floor, while exits stay permitted
    so §6.5's flatten can still run. Writing ``TRADING_ENABLED=FALSE`` would
    break the property that matters most — funding the account lifts equity
    back over ``mark x 0.80`` and the halt clears with nothing for the
    operator to remember, whereas `guard._account_halts` refuses everything
    regardless of equity.

    The stamp is cleared the moment equity clears the floor again, so a second
    drawdown after a recovery is a new event and alerts as one. `edge_alerts`
    is append-only with a serial id and cannot be upserted the way the
    benchmark row is, which is why this needs a stamp at all — the same
    mechanism, and the same reason, as ``last_scorecard_date``.

    The line itself is `sizing.drawdown_breached`, the same function object
    `guard.py` denies against. Two readings of §10's 20% that merely agreed
    today would eventually disagree, and the day they did the operator would
    get an alert about a halt that had not happened, or a halt with no alert.
    """
    control = ctx.get("control") or {}
    account = ctx.get("account") or {}
    mark = control.get("high_water_mark")
    stamped = control.get("last_drawdown_alert")

    # A missing, zero or non-numeric equity read is `guard.ACCOUNT_UNREADABLE`,
    # not a drawdown — there is nothing here to alert on, and `drawdown_breached`
    # would (correctly) return False for it anyway.
    if raw_equity(account) is None or mark is None:
        return [], {}

    # Implausible outranks breached. An earlier incident was a
    # readable-but-wrong `equity_total` that happened to also read as a
    # drawdown breach — `Account read implausible` is what should have fired,
    # not `Drawdown halt`, and the two must never fire in the same cycle.
    # Dedup'd the same way as the drawdown stamp, under its own key, so a
    # recovered implausible read does not suppress a later, real breach.
    implausible_stamped = control.get("last_account_implausible_alert")
    if account_implausible(
        account, ctx.get("positions") or [], ctx.get("prior_benchmark") or {}
    ):
        if implausible_stamped:
            return [], {}
        position_total = sum(
            float(p.get("market_value") or 0.0)
            for p in (ctx.get("positions") or [])
        )
        return (
            [
                Alert(
                    ACCOUNT_IMPLAUSIBLE_ALERT,
                    f"Equity {_money(raw_equity(account))} does not cover "
                    f"{_money(position_total)} of open positions, or has "
                    "collapsed against yesterday's benchmark. Buys are "
                    "denied for this cycle; nothing was sold on this alone.\n\n"
                    + _doc_link(ACCOUNT_IMPLAUSIBLE_ALERT),
                    PRIORITY_URGENT,
                )
            ],
            {"last_account_implausible_alert": ctx["cycle"]["trading_day"]},
        )
    if implausible_stamped:
        return [], {"last_account_implausible_alert": None}

    if not drawdown_breached(account, float(mark), ctx["config"]):
        # Recovered, or never breached. Only the first of those has anything to
        # write, and a key written on every cycle is a write on every cycle.
        return [], ({"last_drawdown_alert": None} if stamped else {})

    if stamped:
        return [], {}

    return (
        [
            Alert(
                DRAWDOWN_HALT,
                f"Equity {_money(flow_adjusted_equity(account))} net of deposits, "
                f"against a high-water mark of "
                f"{_money(float(anchored_mark(account, mark)))} and below the "
                f"{_money(drawdown_floor(float(mark), ctx['config'], account))} "
                "drawdown floor (§10): the account is down 20% from its peak, "
                "deposits excluded. Buys are denied and exits still place. "
                "The halt lifts by itself once equity clears the floor; there "
                "is no flag to reset.\n\n"
                + _doc_link(DRAWDOWN_HALT),
                PRIORITY_URGENT,
            )
        ],
        {"last_drawdown_alert": ctx["cycle"]["trading_day"]},
    )


def _blocked_fragment(blocked: list[dict]) -> str | None:
    """§14's "same rejection 2 cycles running ⇒ escalate", folded into
    `_status_alert`'s one push.

    `cli.py` decides the block, because the broker's rejection text exists only
    after placement and it is the only module that sees the same order refused
    across two cycles. The *message* is this module's, because §8.3's queue is
    — a notify rule belongs in tested code rather than in prompt text.

    One line per order, not one push per order, for the same reason §12.2's
    findings collapse into one: a push per blocked order is how a bad
    afternoon spends the operator's attention before the important message
    arrives.
    """
    if not blocked:
        return None
    return "\n".join(
        f"{row['order'].get('symbol')} {row['order'].get('side')} refused "
        f"{row.get('occurrences')}x: {row.get('detail')}"
        for row in blocked
    )


def _denied_exits_fragment(denied: list[dict]) -> str | None:
    """Sells `guard` refused. They never reach the broker, so no rejection row
    carries them, and `blocked` is the broker's refusals only; without this
    line a position the plan meant to sell reads as "Nothing to report" (four
    TIME_STOPs). Supplied by `services/core`'s finalize phase; absent on the
    live-session path."""
    if not denied:
        return None
    return "\n".join(
        f"{d.get('symbol')} sell x{d.get('quantity')} denied by guard "
        f"({d.get('code')}): {d.get('message')}"
        for d in denied
    )


def _placed_fragment(placed: list[dict]) -> str | None:
    """Orders this cycle sent to the broker. `_activity_fragment` stays quiet
    when a cycle traded ("a fill is the news"), but the runner path has no
    other fill alert, so four TIME_STOP sells at a loss
    read "Nothing to report." Supplied by `services/core`'s finalize phase;
    absent on the live-session path."""
    if not placed:
        return None
    lines = []
    for p in placed:
        price = p.get("limit_price")
        at = f" @ {float(price):g} limit" if price not in (None, "") else ""
        play = f" ({p.get('play_id')})" if p.get("play_id") else ""
        lines.append(
            f"Placed: {p.get('side')} {p.get('quantity')} {p.get('symbol')}{at}{play}"
        )
    return "\n".join(lines)


def _stale_data_fragment(stale: list[str]) -> str | None:
    if not stale:
        return None
    return "Stale data source(s): " + ", ".join(stale) + "."


# `plays.core.SKIP_NO_REACTION_ROW`, restated because `report.py` reads
# `decide`'s output as data and imports no play. `tests/test_report_alerts.py`
# pins the two together.
PEAD_NO_REACTION_ROW = "SKIP_NO_REACTION_ROW"
_PEAD_SYMBOLS_SHOWN = 5


def _pead_reaction_fragment(ctx: Mapping[str, Any]) -> str | None:
    """PEAD could not evaluate a name because the reaction row the pre-market job
    owes it does not exist.

    One line per run however many names it is — the names share one cause (the
    job did not compute them), so three symbols are one fact, not three
    alerts. Only the *overdue* rows are counted: `SKIP_REACTION_PENDING` (the
    reaction session has not closed yet) is the ordinary first-day case and is
    not reported. Informational; it does not change the status word.
    """
    skipped = (ctx.get("entries") or {}).get("skipped") or []
    symbols = sorted(
        {
            str(row.get("symbol"))
            for row in skipped
            if isinstance(row, Mapping) and row.get("code") == PEAD_NO_REACTION_ROW
        }
    )
    if not symbols:
        return None
    shown = ", ".join(symbols[:_PEAD_SYMBOLS_SHOWN])
    extra = len(symbols) - _PEAD_SYMBOLS_SHOWN
    if extra > 0:
        shown += f" and {extra} more"
    return (
        f"PEAD: no reaction row for {len(symbols)} name(s) inside the window "
        f"({shown}); not evaluated. The pre-market job did not compute them."
    )


def _deployment_fragment(ctx: Mapping[str, Any]) -> str | None:
    """What an entry-capable cycle did with the cash, in one line — posture,
    breadth, the funnel and the decision. Present on every such cycle, bought
    or not: "held cash because the posture said to" is a decision and reads
    as one. A failed write of the record itself is named on the next line, so
    a missing row is never silent.
    """
    row = ctx.get("deployment")
    lines = []
    if isinstance(row, Mapping) and row.get("decision"):
        lines.append(deployments.summary_line(row))
    error = ctx.get("deployment_write_error")
    if error:
        lines.append(f"Deployment record not written: {error}")
    # S0: the same rule for the decision snapshot. It is the evidence every
    # later selection test reads, so a cycle that left none must say so.
    error = ctx.get("snapshot_write_error")
    if error:
        lines.append(f"Decision snapshot not written: {error}")
    return "\n".join(lines) or None


def _dead_play_fragment(ctx: Mapping[str, Any]) -> str | None:
    """P3: a play that has put no new name into the ranked list for
    `dead_play_alert_days` consecutive trading days. `decide` names it once,
    on the cycle its streak reaches the threshold, so this line appears in
    exactly one alert per dead spell."""
    dead = [d for d in ctx.get("dead_plays") or [] if isinstance(d, Mapping)]
    if not dead:
        return None
    return "\n".join(
        f"Dead play: {d.get('play_id')} has produced no entry candidate for "
        f"{d.get('days')} consecutive trading days."
        for d in dead
    )


def _manual_required_fragment(manual: list[dict]) -> str | None:
    """The operator's own "notify me, I'll place the limit myself" fallback
    — an exit `exits.py` computed but a fractional
    quantity or a missing bid keeps it from placing at all. Folded into
    `_status_alert`'s one push; §10.1 gives a FRACTIONAL position no
    protection between cycles, which is why this always ranks the cycle at
    least `partial_success` rather than waiting to be read as routine.
    """
    if not manual:
        return None
    return "\n".join(f"{m.get('symbol')} {m.get('code')}: {m.get('reason')}" for m in manual)


# "cap at 3 reasons + and N more". A
# quiet full cycle that reviewed a dozen candidates and printed all twelve
# skip reasons is the same truncation bug the old push had, just moved.
_SKIPPED_REASONS_SHOWN = 3


def _runner_fetch_fragment(ctx: Mapping[str, Any]) -> str | None:
    """The hosted runner fetched more, or wrote it less cheaply, than it was asked
    to. Counted by `services/core` from the step log (`steps.fetch_warnings`);
    warn-only, so it never changes the status word. Absent on the live path and
    on a clean cycle."""
    counts = ctx.get("runner_fetch") or {}
    parts = []
    if counts.get("outside_need"):
        parts.append(f"{counts['outside_need']} fundamentals row(s) outside resolve's fundamentals_needed")
    if counts.get("per_row_writes"):
        parts.append(f"{counts['per_row_writes']} single-row Step 2a write(s) instead of one batch")
    if counts.get("oversize_batches"):
        parts.append(f"{counts['oversize_batches']} batch(es) over the size limit")
    if not parts:
        return None
    return "Runner fetch: " + "; ".join(parts) + "."


def _activity_fragment(ctx: Mapping[str, Any]) -> str | None:
    """The 09:40 line for a cycle that traded neither side, `FULL` only.

    An order placed on either side already has its own alert — a fill is the
    news, per §8.3's "routine entries and exits are not notified" — so this
    only says something when `decide` opened nothing and closed nothing.
    Three lines, always in this order: why no buy, why no sell, whether
    entries were even on the table today. Every other cycle type has nothing
    to say here — `MONITOR`/`OVERNIGHT`/`PREMARKET` neither screen the
    universe nor decide entries, so a copy of the same three lines on each
    would be the noise this fragment exists to replace.
    """
    if ctx.get("cycle", {}).get("cycle_type") != FULL:
        return None

    entries = ctx.get("entries") or {}
    exits = ctx.get("exits") or {}
    if entries.get("candidates") or exits.get("exits"):
        return None

    skipped = entries.get("skipped") or []
    if skipped:
        shown = skipped[:_SKIPPED_REASONS_SHOWN]
        reasons = "; ".join(f"{s.get('symbol')}: {s.get('message')}" for s in shown)
        extra = len(skipped) - len(shown)
        if extra > 0:
            reasons += f"; and {extra} more"
        buy_line = f"Buy: {len(skipped)} candidate(s) reviewed, 0 opened — {reasons}"
    else:
        buy_line = "Buy: no candidates surfaced"

    manual = ctx.get("manual_required") or []
    held = len((ctx.get("reconcile") or {}).get("positions") or [])
    if manual:
        sell_line = (
            f"Sell: {len(manual)} exit(s) triggered but need a manual limit "
            f"order (see {MANUAL_ACTION_REQUIRED})"
        )
    elif held:
        sell_line = f"Sell: {held} position(s) held, 0 met an exit condition"
    else:
        sell_line = "Sell: no positions held"

    control = ctx.get("control") or {}
    account = ctx.get("account") or {}
    mark = control.get("high_water_mark")
    if account_implausible(
        account, ctx.get("positions") or [], ctx.get("prior_benchmark") or {}
    ):
        status_line = "Entries: blocked — account read implausible"
    elif mark is not None and drawdown_breached(account, float(mark), ctx["config"]):
        status_line = "Entries: blocked — drawdown halt (equity below floor)"
    elif control.get("entries_blocked"):
        reason = control.get("entries_blocked_reason") or "edge_control"
        status_line = f"Entries: blocked — {reason}"
    else:
        status_line = "Entries: permitted, none qualified"

    return f"{buy_line}\n{sell_line}\n{status_line}"


def _play_fragment(ctx: Mapping[str, Any]) -> str | None:
    """§8.3's "play enabled/disabled by the agent", folded into
    `_status_alert`'s one push.

    §1 puts the agent's judgment in exactly one place — choosing which plays
    are live today, not inventing trades. This is therefore the only
    discretionary decision the system makes, and the only one worth telling a
    person about while it is still reversible.

    Which is exactly why `UNCHOSEN` is skipped. `ADOPTED` gets an
    `edge_play_state` row the first time an inherited position closes, and
    `edge_play_state_apply` inserts it at `regime_fit` 0.0 like any new row —
    so without this it would read as newly-off against an absent prior and say
    "ADOPTED disabled" about a play the agent does not control and that can
    never place an entry. This is about a decision; that would be an artefact
    of a row appearing.
    """
    now = ctx.get("play_state") or {}
    before = ctx.get("prior_play_state") or {}

    changed = []
    for play_id, row in sorted(now.items()):
        if play_id in UNCHOSEN:
            continue
        was_off = (before.get(play_id) or {}).get("regime_fit") == REGIME_OFF
        is_off = row.get("regime_fit") == REGIME_OFF
        if was_off == is_off:
            continue
        state = "disabled" if is_off else "enabled"
        changed.append(f"{play_id} {state}: {row.get('reason') or 'no reason given'}")

    if not changed:
        return None
    return "Play state changed:\n" + "\n".join(changed)


def _cycle_status(
    ctx: Mapping[str, Any], findings: list[dict], blocked: list[dict], manual: list[dict]
) -> str:
    """The outcome word, in the precedence it gives:
    "any rejection ⇒ partial_success; any PHANTOM_ORDER/PERSISTENT ⇒ blocked".

    Every signal here already has a real producer somewhere in `ctx` — a TD
    rate-limit fallback or a chunked runlog write would also qualify for
    `success_with_warnings` per §2.4, but nothing upstream surfaces either of
    those into `ctx` yet, and deriving a status from a key nothing populates is
    exactly the `prior_benchmark` mistake this same prompt fixes elsewhere. A
    later ctx key for those degradations can join this ladder when something
    produces it.

    **Fallback only.** `_status_alert` prefers `ctx["session_status"]` —
    `run._session_status`'s own word, computed server-side from the same runlog
    data plus placement rejections this function has no visibility into at all —
    and only calls this when that key is absent (a bare `cli.report()` call, or
    one of this file's own tests). This function's own precedence is
    deliberately narrower than `_session_status`'s (a lone
    `POSITION_DELTA_MISMATCH`/`MISSING_ORDER`/
    `QUANTITY_DRIFT`/`SYMBOL_SWAP_SUSPECTED` reads `success_with_warnings` here,
    `blocked` there) — `test_reconciliation_divergence_is_notified`
    (`tests/test_report_alerts.py`) pins that difference on purpose, and nothing
    about closing the `run.py`/`report.py` divergence for the live hosted-core
    path required narrowing this fallback to match.
    """
    if ctx.get("failure"):
        return STATUS_FAILED

    persistent = any(
        f.get("code") == RECONCILIATION_DIVERGENCE_PERSISTENT for f in findings
    )
    loudest_blocked = any(row.get("priority") == PRIORITY_HIGH for row in blocked)
    if persistent or loudest_blocked:
        return STATUS_BLOCKED

    if manual or blocked:
        return STATUS_PARTIAL_SUCCESS

    if findings:
        return STATUS_SUCCESS_WITH_WARNINGS

    return STATUS_SUCCESS


def _et_time(now_iso: str | None) -> str:
    """`ctx["cycle"]["now"]`, an ISO8601 UTC stamp, as `HH:MM` ET for the
    footer. A missing or malformed stamp (a test fixture that never set one)
    degrades to a placeholder rather than raising — the footer is the last
    thing built and must not be why a real alert never sends.
    """
    if not now_iso:
        return "??:??"
    try:
        return (
            datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
            .astimezone(ET)
            .strftime("%H:%M")
        )
    except ValueError:
        return "??:??"


def _footer(ctx: Mapping[str, Any], routine: str) -> str:
    """`cycle <8 chars> · <HH:MM ET> · <routine> · runlog <ids>`
    `cycle_id` and `now` are stamped by every
    real caller (`cli.py`, `services/core`); a fixture missing either degrades
    rather than crashing, matching this module's existing "absent is not an
    error" convention (`Scorecard`'s `funded` line, `Note`'s silent skips).

    `runlog_ids` is optional and appended only when present — this module runs
    before its own `report` runlog row is written, so it can only ever name
    the reconcile/decide/place rows the caller already wrote and chose to pass
    forward; omitting it is not the same claim as an empty list would be.
    """
    cycle_id = str(ctx.get("cycle_id") or "unknown")
    footer = f"cycle {cycle_id[:8]} · {_et_time(ctx.get('cycle', {}).get('now'))} ET · {routine}"
    runlog_ids = ctx.get("runlog_ids") or []
    if runlog_ids:
        footer += " · runlog " + ",".join(str(i) for i in runlog_ids)
    return footer


def _footer_with_link(ctx: Mapping[str, Any], routine: str, title: str) -> str:
    """`_footer` plus the doc-link fragment (`_doc_link`) — every titled alert
    this module builds ends with a link into `docs/OPERATIONS.md` at the
    anchor `_doc_slug(title)` generates, which is the same anchor GitHub
    renders that title's own `## ` heading into there.
    """
    return _footer(ctx, routine) + " · " + _doc_link(title)


def _status_alert(ctx: Mapping[str, Any]) -> tuple[Alert, str]:
    """The principle: **one** end-of-cycle push per cycle, titled with its
    status, everything else folded into its body. Replaces `_failure_alert`,
    `_divergence_alert`, `_blocked_alert`, `_manual_required_alert`,
    `_play_alert` and `_no_activity_alert` as separate pushes — those were
    exactly the "three overlapping high pushes" an earlier audit found.

    `DRAWDOWN_HALT`, `ACCOUNT_IMPLAUSIBLE_ALERT`, `WEEKLY_SCORECARD` and
    `BENCHMARK_INTEGRITY` stay their own alerts (`_drawdown_alert`,
    `_integrity_alert`, `_scorecard_alert`) — each is already deduplicated on
    its own stamp or its own weekly cadence, and folding a halt into a routine
    status line is how a halt gets missed.

    Returns the alert and the status word together so `run()` can hand the
    same word to `ReportResult.status` without re-deriving it from the body.
    """
    findings = _divergence_findings(ctx)
    blocked = ctx.get("blocked") or []
    manual = ctx.get("manual_required") or []
    cycle_type = ctx.get("cycle", {}).get("cycle_type")
    routine = _routine_label(cycle_type)

    # Prefer the word `run._session_status` already computed -- same data,
    # same precedence, the one place `edge_session_stats.status` and this
    # alert's title can no longer disagree. `_cycle_status` is the fallback
    # for a caller that never set `session_status` at all (a bare
    # `cli.report()` call, or this file's own tests).
    status = ctx.get("session_status") or _cycle_status(ctx, findings, blocked, manual)

    fragments = [
        _failure_fragment(ctx),
        _divergence_fragment(findings),
        _blocked_fragment(blocked),
        _manual_required_fragment(manual),
        _denied_exits_fragment(ctx.get("denied_exits") or []),
        _placed_fragment(ctx.get("placed_orders") or []),
        _stale_data_fragment(ctx.get("stale_data_sources") or []),
        _pead_reaction_fragment(ctx),
        _deployment_fragment(ctx),
        _dead_play_fragment(ctx),
        _play_fragment(ctx),
        _activity_fragment(ctx),
        _runner_fetch_fragment(ctx),
    ]
    body_lines = [f for f in fragments if f]
    if not body_lines:
        body_lines = ["Nothing to report."]

    title = f"{routine} cycle {_STATUS_PHRASE[status]}"
    body = "\n\n".join(body_lines) + "\n\n" + _footer_with_link(ctx, routine, title)
    return Alert(title, body, _STATUS_PRIORITY[status]), status


def _scorecard_alert(card: Scorecard, shadow: Sequence[str] = ()) -> Alert:
    """§8.3's one good-news row, rendered for a phone.

    The push is the whole deliverable — §8.3 sends it precisely so the operator
    does not have to go and look at anything.
    """
    plays = "\n".join(
        f"  {p.play_id}: {p.closed_trades} closed, {_money(p.realized_pnl)}"
        # ADOPTED never shows "(disabled)" — the agent does not choose
        # ADOPTED's regime_fit (`_play_fragment`'s docstring has the same
        # reasoning), so a permanent 0.0 there is not a decision to flag.
        f"{' (disabled)' if p.regime_fit == REGIME_OFF and p.play_id not in UNCHOSEN else ''}"
        # Named only when there is one, same economy as `funded`.
        f"{f' ({p.near_flat_trades} near-flat)' if p.near_flat_trades else ''}"
        for p in card.plays
    )
    if card.data_quality:
        plays += "\n" + "\n".join(card.data_quality)
    # Named only when there is one. §1's question is what the agent did, and a
    # line reading "incl. $0.00 deposited" every week would train the operator
    # to skip the line that matters on the week it is not zero.
    funded = (
        f" (incl. {_money(card.net_deposits)} deposited)" if card.net_deposits else ""
    )
    agent_gain = "n/a" if card.agent_gain is None else _money(card.agent_gain)
    return Alert(
        WEEKLY_SCORECARD,
        f"{card.days} days in. Equity {_money(card.equity)}{funded}.\n"
        f"  agent   {agent_gain} ({_pct(card.agent_return)} money-weighted)\n"
        f"  SPY     {_pct(card.spy_return)}\n"
        f"  frozen  {_pct(card.frozen_return)}\n"
        f"  24%/yr  {_pct(card.target_return)}\n"
        f"  fill    {_rate(card.fill_rate)}\n"
        f"  slip    {_pct(card.median_slippage)}\n"
        f"{plays}"
        # §4.2 P1's evidence, under the account's own numbers rather than over
        # them — the scorecard's first question is still what the account did.
        # Absent entirely when the replay produced no closed trades this week:
        # a bare heading would read as "every variant broke even".
        + (_SHADOW_HEADING + "\n".join(shadow) if shadow else "")
        + "\n\n" + _doc_link(WEEKLY_SCORECARD),
        PRIORITY_DEFAULT,
    )


def run(ctx: Mapping[str, Any]) -> ReportResult:
    """The cycle's measurements. Pure function of JSON (Invariant 2)."""
    # §8.3 is every cycle's job; the benchmark row is only the post-close's. A
    # divergence found at 10:40 that waited for 16:30 would be six hours of the
    # operator not knowing entries were blocked.
    halt, updates = _drawdown_alert(ctx)
    status_alert, status = _status_alert(ctx)
    # Exactly one status push per cycle, always — `status_alert` is never
    # gated on cycle_type or on having anything to say (`Nothing to report.`
    # is itself the point: a missing push is now as legible as a bad one).
    # `halt`/`_integrity_alert`/`_scorecard_alert` stay separate pushes.
    alerts = [status_alert, *halt]

    if (ctx.get("cycle") or {}).get("cycle_type") != POSTCLOSE:
        # §10's floor is crossed intraday and guard denies against it from that
        # moment, so the stamp is written by whichever cycle finds the breach.
        # Holding it for the post-close would let a 10:40 halt alert again at
        # 11:40, which is the noise the stamp exists to prevent.
        return ReportResult(alerts=alerts, control_updates=updates, status=status)

    benchmark, notes = _benchmark(ctx)
    result = ReportResult(
        benchmark=benchmark,
        alerts=alerts,
        notes=notes,
        control_updates=updates,
        status=status,
    )
    # §8.4's daily row exists; the integrity check compares it against the last
    # one and speaks only when the flows do not add up.
    result.alerts.extend(_integrity_alert(ctx, benchmark))

    trading_day = benchmark.trading_day
    already_sent = (ctx.get("control") or {}).get("last_scorecard_date")
    if already_sent == trading_day:
        # §13's post-close is a band, so a half-day fires it four times. The
        # benchmark row above absorbs that by keying on the day; `edge_alerts`
        # is append-only with a serial id and cannot, so the stamp is what
        # keeps one week to one push.
        return result

    if _is_week_end(date.fromisoformat(trading_day), ctx.get("calendar") or {}):
        result.scorecard = _scorecard(ctx, benchmark)
        result.alerts.append(_scorecard_alert(result.scorecard, _shadow_lines(ctx)))
        # Merged, not substituted. A drawdown on a Friday must not cost the
        # week its scorecard dedupe, and vice versa.
        result.control_updates["last_scorecard_date"] = trading_day

    return result
