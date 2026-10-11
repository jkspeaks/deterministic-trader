"""The shadow ledger.

Every rule this system trades on is an untested prior. §4.2 calls the fix P1
and a *precondition for strategy changes*: replay the days that already
happened against a set of named rule variants, price the hypothetical trades,
and put the answer somewhere a human can read it before anyone retunes a knob.
This module is that replay.

**It runs the shipped core, not a model of it.** For each stored cycle it
assembles the same `cycle.json` `run.py` assembles, from the same
`edge_cycle_input` rows, and calls the same `cli.candidates` / `cli.reconcile` /
`cli.decide`. A variant is a **config override** handed to `cli.load_config`'s
output and nothing else — there is no second `_dip`, no reimplemented ranking, no
parallel guard.

**Three of the four variants §4.3 names already shipped.** Since the variants were
written, the shipped code has adopted the
DIP trend band at 3% (`dip_trend_band_pct`), crowding as a pure tiebreak
(`entries._rank_key`), and the removal of 8-K item 5.02 from `FORCE_EXIT_ITEMS`.
Re-running them as specified would compare the baseline against itself. So each
is registered in the direction that still produces a contrast — the *old* rule,
against today's — and says so in its own `note`. The comparison §4.3 wanted is
the same comparison; only its sign changed.

**Fills and exits are assumptions, and they are named.** An entry fills at the
**next stored cycle's ask** for that symbol — the next real quote the system
saw, not this cycle's, because an order placed at 09:40 could not have filled
at 09:40's ask. An exit is the Phase 2.2 resting stop/target (`plays.protection`,
the identical functions the live account rests orders at) walked forward over
daily bars, plus the play's own time stop. Where the data for either is absent
the trade is written with `reason` saying so — `NO_FILL_QUOTE`, `NO_BARS`,
`OPEN` — and never with a number that was guessed.

**A day with no stored inputs is reported as such.** `edge_cycle_input` is
reaped at seven days, so a ten-day run will normally find
inputs for fewer than ten days. The summary names every requested trading day
and what was found for it, because "no trades" and "no data" are opposite
findings and a table that rendered them identically would be the third
instance of this project's dominant defect class.
"""

from __future__ import annotations

import os
import traceback
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Mapping, Sequence

from src import broker_shapes, cadence, cli, control_arm
from src import snapshot as snapshots
from src.plays.core import PLAYS, SECTOR, SECTOR_RSI_EXIT
from src.plays.dip import DIP, DIP_RSI_EXIT
from src.plays import protection

from . import premarket
from . import run as core_run
from .supabase import Supabase
from .twelvedata import TwelveData

# ---------------------------------------------------------------------------
# The variants
# ---------------------------------------------------------------------------

BASELINE = "baseline"


@dataclass(frozen=True)
class Variant:
    """One rule set: a name, a config override, and why it is worth a column.

    ``overrides`` is applied over `cli.load_config()["config"]` and nothing
    else is changed. A variant that needed more than that would be a code
    change, and a code change belongs in a branch, not in a ledger column.
    """

    name: str
    overrides: dict
    note: str


VARIANTS: tuple[Variant, ...] = (
    Variant(BASELINE, {}, "the shipped config, unchanged — every other row is read against this"),
    Variant(
        "A_dip_band",
        {"dip_trend_band_pct": 0.0},
        "§4.3 A. Specified as 'DIP trend band 3%', which config/config.json has "
        "shipped since 2026-09-08 — so the contrast is the strict pre-band rule "
        "(price >= SMA200), and a negative result here defends the band",
    ),
    Variant(
        "B_crowding_score",
        {"crowding_mode": "score"},
        "§4.3 B. Specified as 'crowding-as-tiebreak', which shipped 2026-09-08 — "
        "so the contrast is the retired reading, where the penalty subtracts "
        "from the score and can erase a candidate outright",
    ),
    Variant(
        "C_hard_event_502",
        {"force_exit_items": ["4.02", "5.02"]},
        "§4.3 C. Specified as 'no HARD_EVENT 5.02', which shipped 2026-09-09 — "
        "so the contrast puts 5.02 back and measures what the forced exit on a "
        "role-less EDGAR item code was actually costing",
    ),
    Variant(
        "D_no_trade",
        {"no_trade_conditions": True},
        "§4.3 F. The no-trade conditions, which ship switched off "
        "(src/plays/no_trade.py). Its suppressed trades are baseline's rows "
        "that have no row here",
    ),
    Variant(
        "E_dip_pullback",
        {"play_dip_pullback_enabled": True, "risk_sizing_enabled": True},
        "Built 2026-09-12 and shipped off. "
        "DIP reshaped as pullback-in-uptrend — RSI <= 35, price inside -5%..0% "
        "of the 50-day, a sector the regime allows, $20M ADV, 0.2% spread, "
        "target 1.5x stop distance — sized on the 1%-equity risk "
        "budget rather than on the even slot. The two switches ride together "
        "because A's target is stated in stop distances and its size is the "
        "same measurement: splitting them would produce a column that is "
        "neither the shipped rule nor A",
    ),
    Variant(
        "F_sector_play",
        {"play_sector_enabled": True},
        "Built 2026-09-12 and shipped off. "
        "Buys the sector ETF itself at RSI <= 30 while it holds its 200-day "
        "and SPY RSI is over 40; exits at RSI >= 55 or +4%, stop 1.5xATR, time "
        "10. Additive rather than a contrast — baseline has no such play, so "
        "every row here is a trade the shipped rules did not take",
    ),
    Variant(
        "G_regime_v2",
        {"regime_v2_enabled": True},
        "2026-10-02 audit §5.2, P1 (built 2026-10-03, shipped off). Breadth and "
        "the five postures enforced on entries, and DIP only where the sector "
        "is at or above its own 200-day. A suppression: its skipped trades are "
        "baseline's rows that have no row here, and its REDUCED_SIZE / "
        "HIGH_CONVICTION_ONLY rows are baseline's at half the size",
    ),
    Variant(
        "H_catalyst_rvol_tnorm",
        {"catalyst_rvol_time_normalised": True},
        "2026-10-02 audit W5, P2 (built 2026-10-03, shipped off). CATALYST's "
        "1.5x volume test applied to volume scaled to a full session rather "
        "than to volume-so-far against a full-day average, refused before "
        "minute 30. Additive in practice: baseline's CATALYST has never "
        "bought, so every row here is a trade the shipped rule did not take",
    ),
    Variant(
        "I_sizing_v2",
        {"sizing_v2_enabled": True},
        "2026-10-02 audit §5.6, P3 (built 2026-10-03, shipped off). One new "
        "name per sector per day, half a slot for a name in the same "
        "sub-industry or sector as a holding, the posture's size multiplier "
        "and its target-invested cap. A suppression and a resize: its "
        "skipped trades are baseline's rows with no row here",
    ),
)

# ---------------------------------------------------------------------------
# The control arm
# ---------------------------------------------------------------------------
#
# Not config overrides, so not `VARIANTS`: these five rows per signal answer a
# different question from "what would the other rule have bought". For every
# name a play ranks (`entries.candidates` under the shipped config — a signal,
# whether or not there was a slot or the cash to buy it):
#
# * `R_signal`            the signal itself
# * `R_random_sector`     a seeded random same-sector name that did not signal
#                         (`src/control_arm.py`), under the signal's play
# * `*_norsi`             each of those with the play's RSI exit removed. A
#                         parity check found 28% of random peers already
#                         past RSI 50 at entry, so the RSI exit cuts
#                         the control short and manufactures an edge; the
#                         program reads the no-RSI pair as primary.
# * `B_hold_spy`          SPY bought at the same open and sold on the day the
#                         no-RSI signal trade ended — the beta baseline.
#
# **One pricing rule for all five**, and it is deliberately not baseline's.
# Baseline fills at the next stored cycle's ask, which exists only for names
# the cycle quoted; a random stand-in was never quoted. So both arms fill at
# the **next session's open** off the same daily bars (the program's
# point-in-time rule), take their ATR from those bars as of the signal day,
# and are walked by the same `_walk`. A difference between `R_signal` and
# `R_random_sector` is then a difference between the names and nothing else.
R_SIGNAL = "R_signal"
R_RANDOM = "R_random_sector"
NO_RSI = "_norsi"
B_HOLD_SPY = "B_hold_spy"
MARKET = "SPY"
CONTROL_VARIANTS = (
    R_SIGNAL, R_SIGNAL + NO_RSI, R_RANDOM, R_RANDOM + NO_RSI, B_HOLD_SPY,
)

# The plays whose exits read an oscillator, and the level (`plays.exits`).
RSI_EXITS = {DIP: DIP_RSI_EXIT, SECTOR: SECTOR_RSI_EXIT}

# ---------------------------------------------------------------------------
# Knobs
# ---------------------------------------------------------------------------

# How far back to look. Ten trading days is the acceptance window;
# `edge_cycle_input`'s seven-day reap is the real ceiling and the summary says
# so per day rather than quietly returning a shorter table.
DEFAULT_DAYS = 10

# `time_series` costs one Twelve Data credit per symbol, and the vendor allows
# eight credits a minute — so this is three requests of eight, a minute apart
# (`api/shadow.py` builds the client with that interval), inside the
# function's 300 seconds with room for the replay itself. A symbol past the
# cap is not dropped: `_bar_order` puts the least recently walked first, so it
# is priced on a later night, and the summary counts what waited.
MAX_BAR_SYMBOLS = 24

# Daily bars per symbol. A hundred covers every play's holding period
# (`MACRO`'s fifteen sessions is the longest), a ten-day entry window in front
# of it, and enough history before that for the control arm's Wilder RSI and
# ATR to have converged. Twelve Data charges per symbol, not per bar.
BAR_COUNT = 100

# How far back an unresolved row is still walked forward, and how far back a
# closed one is recognised as closed (and so left alone).
OPEN_LOOKBACK_DAYS = 90

# How far past the entry intent a fill may be found. An entry the next cycle
# could not quote is not an entry that fills two days later at a price the
# system never looked at; one trading day is the whole of the benefit of the
# doubt.
MAX_FILL_DAYS = 1

# `reason` values. STOP/TARGET/TIME_STOP are outcomes; the other three are
# admissions.
STOP = "STOP"
TARGET = "TARGET"
TIME_STOP = "TIME_STOP"
# The play's own oscillator exit (`plays.exits`' RSI rule), at the close.
SIGNAL = "SIGNAL"
OPEN = "OPEN"
NO_FILL_QUOTE = "NO_FILL_QUOTE"
NO_BARS = "NO_BARS"
# Control arm only: bars exist and the session the entry fills on has not
# printed yet. Not `NO_BARS` — nothing is missing, it is just early.
AWAITING_OPEN = "AWAITING_OPEN"
# The control arm found no same-sector name that had not signalled.
NO_CONTROL = "NO_CONTROL"

CLOSED = frozenset({STOP, TARGET, TIME_STOP, SIGNAL})

STATUS_OK = "ok"
STATUS_FAILED = "failed"

# What a day's entry says about itself in the summary.
DAY_REPLAYED = "replayed"
DAY_NO_INPUTS = "no stored inputs (edge_cycle_input is reaped at 7 days)"
# Rows existed and none of them would assemble. A different finding from
# `DAY_NO_INPUTS` in every way that matters: the runner ran, it banked fetch
# rows, and something about them is wrong — see `unreadable` for the reason.
DAY_UNREADABLE = "inputs stored but none would assemble — see `unreadable`"


# ---------------------------------------------------------------------------
# One replayable cycle
# ---------------------------------------------------------------------------


@dataclass
class Cycle:
    """A stored cycle, assembled once and replayed once per variant."""

    cycle_id: str
    trading_day: str
    payload: dict
    # `assemble`'s non-`cycle.json` rows. Kept so a stored cycle still carries
    # everything `_payload` returned.
    extras: dict = field(default_factory=dict)


@dataclass
class Intent:
    """A buy one variant's `decide` approved, before it is known to have filled."""

    variant: str
    symbol: str
    play_id: str | None
    quantity: float
    entry_day: str
    cycle_id: str
    # Index into the replayed cycle sequence, so the fill can be taken from
    # the *next* one without re-deriving the ordering.
    cycle_index: int
    entry: float | None = None
    atr: float | None = None
    reason: str = NO_FILL_QUOTE


@dataclass(frozen=True)
class Pair:
    """One signal and its stand-in, before either is priced."""

    symbol: str
    play_id: str | None
    entry_day: str
    cycle_id: str | None
    control: str | None


@dataclass
class Summary:
    days: list[dict] = field(default_factory=list)
    cycles: int = 0
    unreadable: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "days": list(self.days),
            "cycles": self.cycles,
            "unreadable": list(self.unreadable),
        }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_shadow(
    supabase: Supabase,
    twelvedata: TwelveData | None,
    *,
    days: int = DEFAULT_DAYS,
    config_dir=None,
    now: str | None = None,
    write: bool = True,
) -> dict:
    """Replay the last ``days`` trading days for every variant.

    ``twelvedata`` may be None — the replay still runs and every trade is
    written `NO_BARS`, which is the honest answer for a deployment with no
    market-data key rather than a refusal to produce the entry side at all.
    """
    try:
        loaded = cli.load_config(config_dir)
        cycles, summary = _load_cycles(supabase, loaded, days=days, now=now)
        stored, state_error = _stored(supabase)
        rows, per_variant, control = _replay(cycles, loaded, twelvedata, stored)
    except Exception as exc:  # noqa: BLE001 — the boundary, as `run.run`'s is
        return {"status": STATUS_FAILED, "detail": f"{type(exc).__name__}: {exc}"}
    if state_error:
        control["stored_state"] = state_error

    written = 0
    if write and rows:
        written = supabase.rpc("edge_shadow_trades_write", {"p_rows": rows}) or 0

    return {
        "status": STATUS_OK,
        "days_requested": days,
        "trades": len(rows),
        "written": written,
        "variants": per_variant,
        "control": control,
        **summary.as_dict(),
    }


# ---------------------------------------------------------------------------
# Reading the stored cycles
# ---------------------------------------------------------------------------


def _trading_days(loaded: Mapping[str, Any], days: int, now: str | None) -> list[str]:
    """The last ``days`` trading days ending today, oldest first.

    From `config/market_holidays.json` via `cadence.is_open`, so a replay
    window never silently includes Thanksgiving.
    """
    today = date.fromisoformat((now or "")[:10]) if now else date.today()
    found: list[str] = []
    cursor = today
    # A hard walk-back bound: `days` trading days can never be more than
    # `days * 2 + 10` calendar days away, holidays included.
    for _ in range(days * 2 + 10):
        if cadence.is_open(cursor, loaded["calendar"]):
            found.append(cursor.isoformat())
            if len(found) >= days:
                break
        cursor -= timedelta(days=1)
    return sorted(found)


def _load_cycles(
    supabase: Supabase, loaded: Mapping[str, Any], *, days: int, now: str | None
) -> tuple[list[Cycle], Summary]:
    wanted = _trading_days(loaded, days, now)
    listed = supabase.rpc("edge_shadow_cycles", {"p_days": days}) or []

    summary = Summary()
    by_day: dict[str, list[Cycle]] = {day: [] for day in wanted}
    unreadable_by_day: dict[str, int] = {day: 0 for day in wanted}

    for row in listed:
        cycle_id = (row or {}).get("cycle_id")
        if not cycle_id:
            continue
        try:
            payload, extras = core_run._payload(cycle_id, supabase, loaded)
            trading_day = cli.plan_fetch(payload, loaded)["cycle"]["trading_day"]
        except Exception as exc:  # noqa: BLE001
            # One unreadable cycle is not a failed replay — it is a cycle the
            # runner died partway through, which is a thing that happens and
            # which the other nine cycles that day still describe.
            #
            # **Attributed to a day, approximately, and that matters.** The
            # trading day comes off the payload, which is the thing that just
            # failed, so `first_seen` (the row's own `created_at`) is the only
            # date available. It can disagree with `cadence.trading_day` for a
            # cycle that straddles a UTC midnight — but the alternative is
            # what an early run of this job produced: a day
            # holding one unassemblable cycle reported as "no stored inputs",
            # which is the exact conflation of "nothing happened" with
            # "nothing could be read" that this module exists to refuse.
            summary.unreadable.append(
                {"cycle_id": cycle_id, "detail": f"{type(exc).__name__}: {exc}"}
            )
            seen = str((row or {}).get("first_seen") or "")[:10]
            if seen in unreadable_by_day:
                unreadable_by_day[seen] += 1
            continue
        if trading_day in by_day:
            by_day[trading_day].append(
                Cycle(cycle_id, trading_day, payload, extras)
            )

    for day in wanted:
        found = by_day[day]
        broken = unreadable_by_day[day]
        if found:
            status = DAY_REPLAYED
        elif broken:
            status = DAY_UNREADABLE
        else:
            status = DAY_NO_INPUTS
        summary.days.append(
            {
                "trading_day": day,
                "cycles": len(found),
                "unreadable": broken,
                "status": status,
            }
        )

    ordered = [cycle for day in wanted for cycle in by_day[day]]
    summary.cycles = len(ordered)
    return ordered, summary


# ---------------------------------------------------------------------------
# The replay
# ---------------------------------------------------------------------------


def _variant_config(loaded: Mapping[str, Any], variant: Variant) -> dict:
    """`loaded`, with this variant's overrides on top of `config`.

    A shallow copy of the mapping and of `config` alone: `calendar`, `etfs`,
    `macro_map` and `inception` are shared, because a variant that changed one
    of those would not be a rule variant.
    """
    return {**loaded, "config": {**loaded["config"], **variant.overrides}}


def _decide(cycle: Cycle, loaded: Mapping[str, Any]) -> dict:
    """`run._decide_phase`'s own sequence, with every write removed.

    Step 5's candidate discovery and Step 7's reconcile both feed `decide` —
    `reconcile` most of all, since `decide` reads its cancels and its
    `protection_plan`. Skipping either would replay a different function from
    the one production runs, which is the one thing this module may not do.
    """
    payload = dict(cycle.payload)
    discovered = cli.candidates(payload, loaded)
    payload["triggers"] = list(discovered.get("triggers") or [])
    payload["macro_targets"] = list(discovered.get("macro_targets") or [])
    payload["fetch_meta"] = cli.day_context_plan(
        {**payload, "symbols": core_run._symbols(payload)}, loaded
    )

    plan_cycle = cli.plan_fetch(payload, loaded)["cycle"]
    if plan_cycle["reconcile_required"] and all(
        key in payload for key in core_run.BROKER_VIEW_KEYS
    ):
        payload["reconcile"] = cli.reconcile(payload, loaded)

    decided = cli.decide(payload, loaded)
    # Step 5's drop list, as `run._decide_phase` adds it: the control arm must
    # not draw a name discovery surfaced and then dropped.
    return {**decided, "candidates_dropped": list(discovered.get("candidates_dropped") or [])}


def _where(exc: BaseException) -> str:
    """The exception, with the line that raised it.

    A refusal reading `AttributeError: 'list' object has no attribute 'get'`
    names a defect and hides it: five variants report the identical sentence,
    every one of them unactionable, and the reader cannot tell an assembler
    problem from a play problem from a broker-schema problem. The last two
    frames are enough to say which, and they are the only part of a traceback
    worth putting in a summary line.
    """
    frames = traceback.extract_tb(exc.__traceback__)[-2:]
    at = " <- ".join(
        f"{os.path.basename(f.filename)}:{f.lineno} {f.name}" for f in reversed(frames)
    )
    return f"{type(exc).__name__}: {exc}" + (f" at {at}" if at else "")


def _intents(
    cycles: Sequence[Cycle],
    loaded: Mapping[str, Any],
    variant: Variant,
    decided_out: list | None = None,
) -> tuple[list[Intent], list[str]]:
    """Every buy this variant's `decide` approved, deduplicated per symbol-day.

    One symbol can enter once per trading day: `plays._best_per_symbol` already
    collapses a symbol to one position, and two cycles on the same day
    proposing the same name is the second cycle seeing an order that (in the
    real world) the first had already placed.
    """
    variant_loaded = _variant_config(loaded, variant)
    found: list[Intent] = []
    refused: list[str] = []
    seen: set[tuple[str, str]] = set()

    for index, cycle in enumerate(cycles):
        try:
            decided = _decide(cycle, variant_loaded)
        except Exception as exc:  # noqa: BLE001
            # A cycle `decide` refuses is usually the same finding for every
            # variant, and `_load_cycles` has already recorded the ones that
            # could not even be assembled — so skipping keeps one variant's
            # crash from erasing the other four's day. **Counted, never
            # silent**: a variant whose every cycle refused would otherwise
            # report zero entries, which is the same number a variant that
            # correctly declined to buy anything reports.
            refused.append(f"{cycle.cycle_id}: {_where(exc)}")
            continue
        if decided_out is not None:
            decided_out.append((cycle, decided))

        signals = cycle.payload.get("signals") or {}
        for order in (decided.get("plan") or {}).get("orders") or []:
            if order.get("side") != "buy" or not order.get("symbol"):
                continue
            key = (order["symbol"], cycle.trading_day)
            if key in seen:
                continue
            seen.add(key)
            found.append(
                Intent(
                    variant=variant.name,
                    symbol=order["symbol"],
                    play_id=order.get("play_id"),
                    quantity=float(order.get("quantity") or 0.0),
                    entry_day=cycle.trading_day,
                    cycle_id=cycle.cycle_id,
                    cycle_index=index,
                    atr=_number((signals.get(order["symbol"]) or {}).get("atr")),
                )
            )
    return found, refused


def _fill(intent: Intent, cycles: Sequence[Cycle]) -> None:
    """Price the entry at the next stored cycle's ask, in place.

    Forward only, and bounded by `MAX_FILL_DAYS`: a buy the system placed at
    11:40 is a buy against the market that existed after 11:40, and the next
    cycle's quote is the closest thing the stored record has to it.

    Read through `broker_shapes.quotes`, never off the payload: a runner-posted cycle
    stores `get_equity_quotes`'s own envelope, where `quotes[symbol]` is absent and
    every entry would have priced `NO_FILL_QUOTE` — a full ledger of unfilled trades
    that looks exactly like a week the market never offered a quote.
    """
    entry_day = date.fromisoformat(intent.entry_day)
    for cycle in cycles[intent.cycle_index + 1 :]:
        if (date.fromisoformat(cycle.trading_day) - entry_day).days > MAX_FILL_DAYS:
            return
        quotes = broker_shapes.quotes(cycle.payload.get("quotes"))
        ask = _number((quotes.get(intent.symbol) or {}).get("ask"))
        if ask is not None and ask > 0.0:
            intent.entry = ask
            intent.reason = OPEN
            return


def _replay(
    cycles: Sequence[Cycle],
    loaded: Mapping[str, Any],
    twelvedata: TwelveData | None,
    stored: Mapping[str, Any] | None = None,
) -> tuple[list[dict], dict, dict]:
    """`(rows to write, per-variant summary, control-arm summary)`.

    ``stored`` is what the ledger already holds (`_stored`): rows a previous
    night closed are **left alone** — neither re-fetched nor rewritten, so a
    closed trade's numbers never move — and rows still unresolved whose cycle
    can no longer be replayed are walked forward from their own stored entry.
    """
    stored = stored or {}
    closed: set[tuple] = set(stored.get("closed") or ())
    waiting: dict[tuple, Mapping[str, Any]] = dict(stored.get("open") or {})

    intents: list[Intent] = []
    refused: dict[str, list[str]] = {}
    baseline_decided: list = []
    for variant in VARIANTS:
        found, failures = _intents(
            cycles, loaded, variant,
            baseline_decided if variant.name == BASELINE else None,
        )
        intents.extend(found)
        refused[variant.name] = failures

    for intent in intents:
        _fill(intent, cycles)

    pairs = _pairs(baseline_decided, loaded)
    live_pairs = [p for p in pairs if not _pair_closed(p, closed)]
    fresh = {(i.variant, i.symbol, i.entry_day, "") for i in intents}
    fresh |= {key for pair in pairs for key in _pair_keys(pair)}
    # Stored rows nothing in this window regenerates: the cycle's inputs are
    # gone, the trade is not.
    orphans = [row for key, row in waiting.items() if key not in fresh]

    wanted = _bar_order(intents, live_pairs, orphans, closed, waiting)
    bars, deferred = _bars(twelvedata, wanted)

    rows: list[dict] = []
    per_variant: dict[str, dict] = {}
    for variant in VARIANTS:
        mine = [i for i in intents if i.variant == variant.name]
        variant_rows = [
            _trade(intent, bars, _variant_config(loaded, variant))
            for intent in mine
            if (intent.variant, intent.symbol, intent.entry_day, "") not in closed
        ]
        rows.extend(variant_rows)
        per_variant[variant.name] = {
            "note": variant.note,
            "entries": len(mine),
            "closed": sum(1 for row in variant_rows if row["reason"] in CLOSED),
            "already_closed": len(mine) - len(variant_rows),
            "cycles_refused": refused[variant.name],
        }

    control_rows = [
        row
        for pair in live_pairs
        for row in _pair_rows(pair, bars, loaded)
        if _row_key(row) not in closed
    ]
    resolved = [
        row for row in (_orphan_row(orphan, bars, loaded) for orphan in orphans)
        if row is not None
    ]
    rows.extend(control_rows)
    rows.extend(resolved)

    control = {
        "signals": len(pairs),
        "signals_already_closed": len(pairs) - len(live_pairs),
        "no_control": sum(1 for p in live_pairs if p.control is None),
        "rows": len(control_rows),
        "closed": sum(1 for row in control_rows if row["reason"] in CLOSED),
        "stored_unresolved": len(orphans),
        "stored_resolved": sum(1 for row in resolved if row["reason"] in CLOSED),
        "bars_deferred": deferred,
    }
    return rows, per_variant, control


# ---------------------------------------------------------------------------
# What the ledger already holds
# ---------------------------------------------------------------------------


def _row_key(row: Mapping[str, Any]) -> tuple:
    return (
        row.get("variant"),
        row.get("symbol"),
        str(row.get("entry_day") or "")[:10],
        row.get("pair_symbol") or "",
    )


def _stored(supabase: Supabase) -> tuple[dict, str | None]:
    """`({"closed": {key}, "open": {key: row}}, error)`.

    **A ledger that cannot be read is an empty one, and says so.** On a
    database without `edge_shadow_open` the replay prices exactly
    what it regenerates, as it always did, and the summary carries the reason
    rather than the run failing over a read that only saves work.
    """
    try:
        answer = supabase.rpc("edge_shadow_open", {"p_days": OPEN_LOOKBACK_DAYS}) or {}
    except Exception as exc:  # noqa: BLE001 — see the docstring
        return {}, f"unavailable: {type(exc).__name__}: {exc}"
    if not isinstance(answer, Mapping):
        return {}, None
    return {
        "closed": {_row_key(r) for r in answer.get("closed") or [] if isinstance(r, Mapping)},
        "open": {
            _row_key(r): r for r in answer.get("open") or [] if isinstance(r, Mapping)
        },
    }, None


# ---------------------------------------------------------------------------
# The control arm
# ---------------------------------------------------------------------------


def _pairs(
    decided_cycles: Sequence[tuple[Cycle, Mapping[str, Any]]], loaded: Mapping[str, Any]
) -> list[Pair]:
    """One `Pair` per (ranked name, trading day) under the shipped config.

    The first cycle of the day that ranked the name is the one that counts,
    as in `_intents`. The stand-in is drawn from that cycle's own view of who
    signalled, so the draw is a pure function of a stored cycle.
    """
    # `decide`'s sector map buckets the ETF sleeve with the stocks it holds
    # (XLV counts against Health Care), so the sleeve is excluded by name: a
    # sector fund is not a random member of its own sector.
    etfs = loaded.get("etfs") or {}
    sleeve = {s for group in ("sector", "broad") for s in (etfs.get(group) or {})}

    found: list[Pair] = []
    seen: set[tuple[str, str]] = set()
    for cycle, decided in decided_cycles:
        candidates = (decided.get("entries") or {}).get("candidates") or []
        if not candidates:
            continue
        excluded = snapshots.signalling(decided) | sleeve
        sectors = decided.get("sectors") or {}
        members = (decided.get("universe") or {}).get("members")
        for candidate in candidates:
            symbol = candidate.get("symbol")
            if not symbol or (symbol, cycle.trading_day) in seen:
                continue
            seen.add((symbol, cycle.trading_day))
            pool = control_arm.eligible(
                symbol, sectors, excluded,
                signals=cycle.payload.get("signals"),
                members=set(members) if members else None,
            )
            found.append(
                Pair(
                    symbol=symbol,
                    play_id=candidate.get("play_id"),
                    entry_day=cycle.trading_day,
                    cycle_id=cycle.cycle_id,
                    control=control_arm.pick(symbol, cycle.trading_day, pool),
                )
            )
    return found


def _pair_keys(pair: Pair) -> list[tuple]:
    keys = [
        (R_SIGNAL, pair.symbol, pair.entry_day, ""),
        (R_SIGNAL + NO_RSI, pair.symbol, pair.entry_day, ""),
        (B_HOLD_SPY, MARKET, pair.entry_day, pair.symbol),
    ]
    if pair.control:
        keys += [
            (R_RANDOM, pair.control, pair.entry_day, pair.symbol),
            (R_RANDOM + NO_RSI, pair.control, pair.entry_day, pair.symbol),
        ]
    return keys


def _pair_closed(pair: Pair, closed: set[tuple]) -> bool:
    return all(key in closed for key in _pair_keys(pair))


def _series(bars: Mapping[str, list[dict]], symbol: str | None) -> list[dict]:
    return [bar for bar in (_bar(r) for r in bars.get(symbol or "") or []) if bar is not None]


def _rsi_by_day(series: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """The daily RSI at each close, by the pre-market job's own `_rsi` — the
    function whose output the live exit reads off `day_context`."""
    closes = [bar["close"] for bar in series]
    out: dict[str, float] = {}
    for index, bar in enumerate(series):
        value = premarket._rsi(closes[: index + 1])
        if value is not None:
            out[bar["date"]] = value
    return out


def _control_row(variant: str, symbol: str, pair_symbol: str, play_id: str | None,
                 entry_day: str, cycle_id: str | None) -> dict:
    return {
        "variant": variant,
        "symbol": symbol,
        "entry_day": entry_day,
        "pair_symbol": pair_symbol,
        "play_id": play_id,
        "entry_cycle_id": cycle_id,
        "quantity": None,
        "entry": None,
        "exit_day": None,
        "exit": None,
        "pnl_pct": None,
        "reason": NO_BARS,
        "bars_source": None,
        "atr": None,
        "fill_day": None,
    }


def _control_trade(
    variant: str,
    symbol: str,
    pair_symbol: str,
    play_id: str | None,
    entry_day: str,
    cycle_id: str | None,
    bars: Mapping[str, list[dict]],
    loaded: Mapping[str, Any],
) -> dict:
    """One control-arm round trip, priced wholly from daily bars.

    Fill at the first session after ``entry_day``'s open; ATR from the bars
    before ``entry_day`` (what the entry cycle would have measured), held
    fixed; then `_walk`, with the fill day itself as session 0 — the resting
    target can fill that day, every per-cycle exit starts the day after, which
    is the live same-day-fill guard.
    """
    row = _control_row(variant, symbol, pair_symbol, play_id, entry_day, cycle_id)
    series = _series(bars, symbol)
    if not series:
        return row
    row["bars_source"] = "twelvedata"

    after = [bar for bar in series if bar["date"] > entry_day]
    if not after:
        row["reason"] = AWAITING_OPEN
        return row

    entry = after[0]["open"]
    atr = premarket._atr([bar for bar in series if bar["date"] < entry_day])
    row.update(entry=entry, fill_day=after[0]["date"],
               atr=None if atr is None else round(atr, 6), quantity=1.0)

    intent = Intent(
        variant=variant, symbol=symbol, play_id=play_id, quantity=1.0,
        entry_day=entry_day, cycle_id=cycle_id or "", cycle_index=0,
        entry=entry, atr=atr, reason=OPEN,
    )
    rsi = None if variant.endswith(NO_RSI) else _rsi_by_day(series)
    exit_day, exit_price, reason = _walk(intent, after, loaded, first_session=0, rsi=rsi)
    row["reason"] = reason
    if exit_price is not None:
        row["exit_day"] = exit_day
        row["exit"] = round(exit_price, 4)
        row["pnl_pct"] = round((exit_price - entry) / entry, 6)
    return row


def _spy_row(
    pair_symbol: str,
    play_id: str | None,
    entry_day: str,
    cycle_id: str | None,
    exit_day: str | None,
    signal_reason: str,
    bars: Mapping[str, list[dict]],
) -> dict:
    """SPY over the dates the no-RSI signal trade was held: in at the same
    open, out at the close of the day that trade ended."""
    row = _control_row(B_HOLD_SPY, MARKET, pair_symbol, play_id, entry_day, cycle_id)
    series = _series(bars, MARKET)
    if not series:
        return row
    row["bars_source"] = "twelvedata"
    after = [bar for bar in series if bar["date"] > entry_day]
    if not after:
        row["reason"] = AWAITING_OPEN
        return row
    entry = after[0]["open"]
    row.update(entry=entry, fill_day=after[0]["date"], quantity=1.0, reason=OPEN)
    if exit_day is None:
        # The signal trade has no bars or is still open; SPY waits with it.
        if signal_reason == NO_BARS:
            row["reason"] = NO_BARS
        return row
    out = next((bar for bar in after if bar["date"] == exit_day), None)
    if out is None:
        return row
    row.update(exit_day=exit_day, exit=round(out["close"], 4),
               pnl_pct=round((out["close"] - entry) / entry, 6), reason=TIME_STOP)
    return row


def _pair_rows(pair: Pair, bars: Mapping[str, list[dict]], loaded: Mapping[str, Any]) -> list[dict]:
    """The five rows for one signal — or four, one of them saying why, when no
    same-sector name was left to stand in."""
    args = (pair.play_id, pair.entry_day, pair.cycle_id, bars, loaded)
    signal = _control_trade(R_SIGNAL, pair.symbol, "", *args)
    twin = _control_trade(R_SIGNAL + NO_RSI, pair.symbol, "", *args)
    rows = [signal, twin]
    if pair.control:
        rows.append(_control_trade(R_RANDOM, pair.control, pair.symbol, *args))
        rows.append(_control_trade(R_RANDOM + NO_RSI, pair.control, pair.symbol, *args))
    else:
        # A row, not an absence: a sector too small to draw from is a finding
        # about the arm's coverage, and it is counted.
        missing = _control_row(R_RANDOM, "", pair.symbol, pair.play_id, pair.entry_day, pair.cycle_id)
        missing["reason"] = NO_CONTROL
        rows.append(missing)
    rows.append(
        _spy_row(pair.symbol, pair.play_id, pair.entry_day, pair.cycle_id,
                 twin["exit_day"], twin["reason"], bars)
    )
    return rows


def _orphan_row(
    stored: Mapping[str, Any], bars: Mapping[str, list[dict]], loaded: Mapping[str, Any]
) -> dict | None:
    """A stored, unresolved row whose cycle is no longer in the window, walked
    forward again from what the row itself holds. `None` leaves it untouched:
    no bars tonight, or a row this code does not know how to price.
    """
    variant, symbol = stored.get("variant"), stored.get("symbol")
    entry_day = str(stored.get("entry_day") or "")[:10]
    pair_symbol = stored.get("pair_symbol") or ""
    if not variant or not symbol or not entry_day or not _series(bars, symbol):
        return None

    if variant == B_HOLD_SPY:
        # `pair_exit_day` is the schema's lookup of the no-RSI signal row's exit.
        exit_day = stored.get("pair_exit_day")
        return _spy_row(pair_symbol, stored.get("play_id"), entry_day,
                        stored.get("entry_cycle_id"),
                        None if not exit_day else str(exit_day)[:10], OPEN, bars)
    if variant in CONTROL_VARIANTS:
        return _control_trade(variant, symbol, pair_symbol, stored.get("play_id"),
                              entry_day, stored.get("entry_cycle_id"), bars, loaded)

    by_name = {v.name: v for v in VARIANTS}
    entry = _number(stored.get("entry"))
    if variant not in by_name or entry is None:
        return None
    intent = Intent(
        variant=variant, symbol=symbol, play_id=stored.get("play_id"),
        quantity=_number(stored.get("quantity")) or 0.0, entry_day=entry_day,
        cycle_id=stored.get("entry_cycle_id") or "", cycle_index=0,
        entry=entry, atr=_number(stored.get("atr")), reason=OPEN,
    )
    return _trade(intent, bars, _variant_config(loaded, by_name[variant]))


def _bar_order(
    intents: Sequence[Intent],
    pairs: Sequence[Pair],
    orphans: Sequence[Mapping[str, Any]],
    closed: set[tuple],
    waiting: Mapping[tuple, Mapping[str, Any]],
) -> list[str]:
    """Every symbol tonight's pricing needs, most deserving first.

    The budget (`MAX_BAR_SYMBOLS`) is smaller than a busy fortnight's open
    trades, so the order decides who waits: SPY (every `B_hold_spy` row needs
    it), then rows the ledger has never held, oldest entry first, then
    unresolved rows by when they were last walked — least recently first, so a
    row deferred tonight is ahead of tonight's rows tomorrow and nothing
    starves.
    """
    def last_walked(key: tuple) -> str:
        return str((waiting.get(key) or {}).get("updated_at") or "")

    jobs: list[tuple[str, str, list[str]]] = []
    for intent in intents:
        key = (intent.variant, intent.symbol, intent.entry_day, "")
        if intent.entry is None or key in closed:
            continue
        jobs.append((last_walked(key), intent.entry_day, [intent.symbol]))
    for pair in pairs:
        symbols = [pair.symbol] + ([pair.control] if pair.control else [])
        stamp = min((last_walked(key) for key in _pair_keys(pair)), default="")
        jobs.append((stamp, pair.entry_day, symbols))
    for row in orphans:
        jobs.append((str(row.get("updated_at") or ""),
                     str(row.get("entry_day") or "")[:10], [str(row.get("symbol"))]))

    ordered = [MARKET] if pairs or any(r.get("variant") == B_HOLD_SPY for r in orphans) else []
    for _, _, symbols in sorted(jobs, key=lambda job: (job[0], job[1], job[2])):
        ordered.extend(s for s in symbols if s and s not in ordered)
    return ordered


# ---------------------------------------------------------------------------
# Bars and the exit walk
# ---------------------------------------------------------------------------


def _bars(
    twelvedata: TwelveData | None, symbols: Sequence[str]
) -> tuple[dict[str, list[dict]], int]:
    """`(daily OHLC per symbol, how many symbols were left for another night)`.

    `({}, 0)` when there is no market-data client. ``symbols`` arrives in
    priority order (`_bar_order`) and is cut at `MAX_BAR_SYMBOLS`; what is cut
    is counted, and comes round again because the order puts the least
    recently walked first.
    """
    ordered = list(dict.fromkeys(symbols))
    wanted = ordered[:MAX_BAR_SYMBOLS]
    if not wanted or twelvedata is None:
        return {}, 0
    return twelvedata.time_series(wanted, outputsize=BAR_COUNT), len(ordered) - len(wanted)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _bar(row: Mapping[str, Any]) -> dict | None:
    """One Twelve Data bar as four floats and a date, or None if incomplete.

    A bar missing its low is a bar that cannot answer "was the stop hit", and
    treating an absent low as "not hit" would turn a data gap into a winner.
    """
    day = str(row.get("datetime") or "")[:10]
    values = {key: _number(row.get(key)) for key in ("open", "high", "low", "close")}
    if not day or any(value is None for value in values.values()):
        return None
    return {"date": day, **values}


def _trade(
    intent: Intent, bars: Mapping[str, list[dict]], loaded: Mapping[str, Any]
) -> dict:
    """One `edge_shadow_trades` row: the entry, and where Phase 2.2 took it out."""
    row = {
        "variant": intent.variant,
        "symbol": intent.symbol,
        "entry_day": intent.entry_day,
        "play_id": intent.play_id,
        "entry_cycle_id": intent.cycle_id,
        "quantity": intent.quantity,
        "entry": intent.entry,
        "exit_day": None,
        "exit": None,
        "pnl_pct": None,
        "reason": intent.reason,
        "bars_source": None,
        "pair_symbol": "",
        # Stored so a later night can walk this trade again after the cycle
        # that produced it has been reaped.
        "atr": intent.atr,
        "fill_day": None,
    }
    if intent.entry is None:
        return row

    series = [
        bar
        for bar in (_bar(r) for r in bars.get(intent.symbol) or [])
        if bar is not None and bar["date"] > intent.entry_day
    ]
    if not series:
        row["reason"] = NO_BARS
        return row

    row["bars_source"] = "twelvedata"
    exit_day, exit_price, reason = _walk(intent, series, loaded)
    row["reason"] = reason
    if exit_price is None:
        return row

    row["exit_day"] = exit_day
    row["exit"] = round(exit_price, 4)
    row["pnl_pct"] = round((exit_price - intent.entry) / intent.entry, 6)
    return row


def _walk(
    intent: Intent,
    series: Sequence[Mapping[str, Any]],
    loaded: Mapping[str, Any],
    *,
    first_session: int = 1,
    rsi: Mapping[str, float] | None = None,
) -> tuple[str | None, float | None, str]:
    """Phase 2.2's resting stop and target, walked forward over daily bars.

    `protection.stop_price` and `protection.target_price` are the live
    account's own functions, called with the live account's own config — the
    trailing stop ratchets here exactly as `reconcile._high_since_entry`
    ratchets it there, which is the whole reason this does not reimplement
    `max(fixed, trailing)`.

    **Stop before target, and the high ratchets last.** Within one daily bar
    there is no way to know which level the tape reached first; taking the
    stop is the assumption that cannot flatter the result. The day's own high
    is added to the trailing floor only after that day's stop has been
    evaluated, so a stop is never tightened by the bar it is being tested on.

    Two additions for the control arm, both inert by default. ``first_session``
    0 means ``series`` starts on the fill day itself: only the resting target
    can act that day, and the stop, the oscillator exit and the time stop
    begin the session after — the live same-day-fill guard. ``rsi`` is
    `{date: daily RSI}`; where the play has an oscillator exit (`RSI_EXITS`)
    and the day's RSI is at or past it, the trade leaves at that close as
    `SIGNAL`, after the stop and the target have had the bar.
    """
    held = {
        "symbol": intent.symbol,
        "play_id": intent.play_id,
        "quantity": intent.quantity,
        "entry_price": intent.entry,
        "high_since_entry": intent.entry,
        "adopted": False,
    }
    # The ATR the entry cycle measured, held fixed for the life of the trade.
    # The live account re-reads ATR every cycle; the stored record has one
    # reading per symbol per cycle and no bar-derived substitute that would
    # agree with it, so this is an assumption and is recorded as one rather
    # than replaced by a second ATR definition.
    ctx = {"config": loaded["config"], "signals": {intent.symbol: {"atr": intent.atr}}}

    play = PLAYS.get(intent.play_id)
    target = protection.target_price(held, ctx)
    limit = None if target is None else target[0]

    rsi_exit = RSI_EXITS.get(intent.play_id) if rsi is not None else None

    for sessions, bar in enumerate(series, start=first_session):
        stop = protection.stop_price(held, ctx) if sessions >= 1 else None
        if stop is not None and bar["low"] <= stop:
            # A gap through the stop fills at the open, not at the stop: a
            # `stop_market` triggered below its price is a market order into
            # whatever is there.
            return bar["date"], min(stop, bar["open"]), STOP
        if limit is not None and bar["high"] >= limit:
            return bar["date"], limit, TARGET

        held["high_since_entry"] = max(held["high_since_entry"], bar["high"])
        if sessions < 1:
            continue
        if rsi_exit is not None and rsi.get(bar["date"], 0.0) >= rsi_exit:
            return bar["date"], bar["close"], SIGNAL
        if play is not None and sessions >= play.holding_days:
            return bar["date"], bar["close"], TIME_STOP

    return None, None, OPEN
