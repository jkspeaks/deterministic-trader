"""The pre-market deterministic data job.

Once a day, ahead of the open, this fills three things a live cycle would
otherwise pay Robinhood calls for, or never get at all:

* **`edge_day_context`** — `signals.sma_20/50/200` and, daily
  `signals.rsi`/`signals.atr` (Wilder, 14) plus `bars_meta` per symbol. The
  runner fetches no indicators, so this is where a hosted cycle's RSI and ATR
  come from; `services/core/core/run._payload` copies them into `signals`.
  `src/cli.day_context_plan`'s `sma_fresh` split reads the SMAs.
* **`edge_regime`** — one row carrying the `regime_inputs` block
  `src/regime.evaluate` consumes (SPY vs its 50/200-day and 20-day realized
  vol; each sector ETF vs its 200-day and RSI). `regime_fit` used to be a
  number a pre-market *session* typed in by hand; that producer died
  silently. This is the same job done from prices.
* Ported to `src/pead_reaction.py` and run here from the same Twelve Data
  bars.

**No Robinhood, no orders, no lock.** The only network this job touches is
Twelve Data (`core/twelvedata.py`) and Supabase. It places nothing.

## Why this runs across many invocations, not one

Twelve Data's Basic grade throttles at **8 API credits per minute** — its own
error text, seen live on the first production fire: `"You have run out of API
credits for the current minute. 15 API credits were used, with the current
limit being 8."` One credit per symbol in a batched request, and SPY plus the
eleven sector ETFs alone is twelve symbols — the regime computation *by
itself* needs more than a minute's budget, before touching held positions or
PEAD names. A single 60-second Vercel invocation cannot legally spend more
than about one batch's worth of credits, so this job cannot fetch everything
in one call the way the first build (same day) assumed.

So the job is a small state machine, persisted in `edge_premarket_state`, and `edge_premarket_dispatch`'s pg_cron entry fires it every two
minutes across the pre-market window instead of once. Each invocation:

1. reads today's state (or builds the day's fetch plan, if none exists yet —
   the symbol universe, batched into ≤8-symbol steps, frozen for the day);
2. if the plan is already finished, does nothing;
3. otherwise executes **exactly one** step — one Twelve Data request, which
   is at most 8 credits and therefore always compliant with the per-minute
   limit no matter how tightly two invocations land — and persists the
   result;
4. once every step has run, computes and writes the three tables in one pass
   (Steps that follow all this: unchanged from the original design) and
   marks the day done.

A stuck run costs nothing beyond that day's `edge_premarket_state` row: the
table is keyed by `trading_day`, so a bad day never blocks the next one.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Mapping, Sequence

from src import cadence, candidates, cli, pead_reaction, plays, report
from src import regime as regimes
from src import universe as universes
from src.plays.pead import PEAD_WINDOW_SESSIONS

from . import run as core_run
from .supabase import Supabase
from .twelvedata import MAX_SYMBOLS_PER_CALL, TwelveData

SPY = "SPY"

# A `time_series` step asks for 250 daily bars. Twelve Data charges 1 credit
# per symbol whatever `outputsize` is (checked live: a 250-bar request cost the
# same single credit as a 2-bar one), so 250 bars cover the 200-day SMA locally
# and the separate `/sma` step — a second credit for every regime symbol — is
# gone. `SMA_PERIODS_FROM_TD`/`STEP_SMA` stay only so a plan frozen by the
# previous deploy can still finish its day.
PREMARKET_BARS = 250
# What is *kept* per symbol after the signals are computed: the regime inputs
# (SPY's closes feed `regime._spy_rv`) and the PEAD reaction read no further
# back than this, and keeping 60 keeps both byte-identical to before while the
# day's state row stays small with ~500 symbols in it.
KEPT_BARS = 60
SMA_PERIODS_FROM_TD = (200,)
SMA_PERIODS_LOCAL = (20, 50)
SMA_PERIODS_ALL = (20, 50, 200)

RSI_PERIOD = 14
# §6.5's stop distance, `entry - N x ATR(14)`. Wilder's smoothing, the same
# definition Robinhood's indicator tool documents; the live account's
# `atr_stop_multiple` was tuned against broker ATR (local usually the
# higher).
ATR_PERIOD = 14

STEP_TIME_SERIES = "time_series"
STEP_SMA = "sma"

# The job's own ceiling, well inside the plan's 800 credits a day: 70%,
# leaving 240 for anything else that touches the key. Core symbols (regime
# set, held, Dip Recovery watchlist, PEAD names, today's discovery candidates)
# always fit; the DIP screen over the rotation-filtered universe fills what is
# left and is truncated — heaviest index weight first — rather than failing
# the day.
DAILY_CREDIT_CAP = 560

# Symbols per `edge_day_context_write_symbols` call in finalize.
DAY_CONTEXT_BATCH = 100

STATUS_OK = "ok"                # the finishing tick: all three tables written
STATUS_IN_PROGRESS = "in_progress"
STATUS_FAILED = "failed"


class PremarketError(RuntimeError):
    """The job cannot run at all (no config, no clock, no Supabase). Terminal
    for this tick; the next tick tries again from the same persisted state."""


# ---------------------------------------------------------------------------
# Entry point — one invocation, at most one Twelve Data request
# ---------------------------------------------------------------------------


def run_premarket(
    supabase: Supabase,
    twelvedata: TwelveData,
    *,
    now: datetime | None = None,
    config_dir=None,
) -> dict:
    try:
        return _tick(supabase, twelvedata, now, config_dir)
    except Exception as exc:  # noqa: BLE001 — the boundary, like run.run
        detail = f"{type(exc).__name__}: {exc}"
        # O-3: this whole job never finished a single step -- the HTTP
        # response nothing reads (cron fires this fire-and-forget) was the
        # only record of that before this. High priority: unlike a
        # partial-errors degradation (`_finalize`, below), nothing here
        # completed at all.
        supabase.rpc(
            "edge_alerts_enqueue",
            {
                "p_alerts": [
                    {
                        "title": report.PREMARKET_DATA_DEGRADED,
                        "body": f"Pre-market job failed before finishing: {detail}",
                        "priority": "high",
                    }
                ]
            },
        )
        return {"status": STATUS_FAILED, "detail": detail}


def _tick(supabase: Supabase, twelvedata: TwelveData, now, config_dir) -> dict:
    loaded = cli.load_config(config_dir)
    moment = now or datetime.now(cadence.ET)
    now_iso = moment.astimezone(cadence.ET).replace(microsecond=0).isoformat()
    trading_day = cadence.trading_day(moment.astimezone(cadence.ET)).isoformat()

    state = _load_state(supabase, trading_day)
    if state is None:
        state = _build_plan(supabase, loaded, trading_day)

    if state["status"] in (STATUS_OK, STATUS_FAILED):
        # Already finished (by an earlier tick, possibly minutes ago) or
        # already refused (the credit cap). Every later tick in the window
        # is a cheap no-op — no Supabase write, no Twelve Data call.
        return {"status": state["status"], "trading_day": trading_day, **(
            {"detail": state["detail"]} if state["detail"] else {}
        )}

    plan = state["plan"]
    if state["next_index"] < len(plan):
        step = plan[state["next_index"]]
        _execute_step(twelvedata, step, state["accum"])
        state["next_index"] += 1
        state["credits_used"] += len(step["symbols"])
        _save_state(supabase, trading_day, state)

    if state["next_index"] >= len(plan):
        result = _finalize(supabase, loaded, trading_day, state, now_iso)
        state["status"] = STATUS_OK
        state["detail"] = None
        _save_state(supabase, trading_day, state)
        return result

    return {
        "status": STATUS_IN_PROGRESS,
        "trading_day": trading_day,
        "step": state["next_index"],
        "of": len(plan),
        "credits_used": state["credits_used"],
    }


# ---------------------------------------------------------------------------
# The plan — built once per trading day, frozen for every later tick
# ---------------------------------------------------------------------------


def _chunk(symbols: Sequence[str], size: int) -> list[list[str]]:
    ordered = list(dict.fromkeys(symbols))  # de-dup, order-preserving
    return [ordered[i : i + size] for i in range(0, len(ordered), size)]


def _build_plan(supabase: Supabase, loaded: Mapping[str, Any], trading_day: str) -> dict:
    sector_etfs = sorted((loaded["etfs"].get("sector") or {}).keys())
    held = _held_symbols(supabase, trading_day)
    survivors = _dip_survivors(supabase)
    # RSP is the equal-weight leg of the posture's RSP-vs-SPY read: one more
    # symbol on the same `time_series` batch, one credit a day.
    regime_symbols = sorted({SPY, regimes.RSP, *sector_etfs, *held, *survivors})

    pead_targets = _pead_targets(supabase, loaded, trading_day)
    discovered, screen_pool, prefetch_error = _prefetch_candidates(supabase, loaded)

    # Core first, in a fixed order, then the DIP screen with whatever the cap
    # leaves. Core symbols are the ones a cycle is certain to need today.
    core: list[str] = []
    for symbol in [*regime_symbols, *(t["symbol"] for t in pead_targets), *discovered]:
        if symbol not in core:
            core.append(symbol)
    core_set = set(core)
    room = max(0, DAILY_CREDIT_CAP - len(core))
    screen_all = [s for s in screen_pool if s not in core_set]
    screen = screen_all[:room]

    plan = [
        {"endpoint": STEP_TIME_SERIES, "symbols": batch}
        for batch in _chunk(core, MAX_SYMBOLS_PER_CALL)
    ] + [
        {"endpoint": STEP_TIME_SERIES, "symbols": batch, "screen": True}
        for batch in _chunk(screen, MAX_SYMBOLS_PER_CALL)
    ]
    credits_planned = sum(len(step["symbols"]) for step in plan)

    universe = {
        "regime_symbols": regime_symbols,
        "pead_targets": pead_targets,
        "discovered": discovered,
        "screened": len(screen),
        "screen_dropped": len(screen_all) - len(screen),
        # Bars are kept past the step only for the symbols finalize still reads
        # them for (the regime inputs and the PEAD reaction); every other
        # symbol keeps just its computed `signals`.
        "keep_bars": sorted(set(regime_symbols) | {t["symbol"] for t in pead_targets}),
        "prefetch_error": prefetch_error,
    }
    accum = {"bars": {}, "signals": {}, "sma200": {}, "errors": [], "screen_errors": 0,
             "universe": universe}

    if credits_planned > DAILY_CREDIT_CAP:
        detail = (
            f"planned {credits_planned} credits exceeds the "
            f"{DAILY_CREDIT_CAP}-credit daily cap; nothing was fetched"
        )
        # O-3. This state is never persisted (`_tick` returns before its one
        # `_save_state` call, since a fresh `_build_plan` this cheap costs
        # nothing to just recompute every tick — see
        # `test_a_failed_day_stays_failed_and_never_retries`), so this alert
        # would otherwise fire on every tick of the whole pre-market window
        # with nothing to stop it. Relying on `edge_alerts_enqueue`'s own
        # same-day (title, body) dedupe instead of adding new state just to
        # suppress a repeat this call already suppresses for free.
        supabase.rpc(
            "edge_alerts_enqueue",
            {
                "p_alerts": [
                    {
                        "title": report.PREMARKET_DATA_DEGRADED,
                        "body": f"Pre-market job failed before finishing: {detail}",
                        "priority": "high",
                    }
                ]
            },
        )
        return {
            "trading_day": trading_day, "plan": plan, "next_index": 0,
            "accum": accum, "credits_used": 0,
            "status": STATUS_FAILED,
            "detail": detail,
        }

    return {
        "trading_day": trading_day, "plan": plan, "next_index": 0,
        "accum": accum, "credits_used": 0,
        "status": STATUS_IN_PROGRESS, "detail": None,
    }


def _execute_step(twelvedata: TwelveData, step: Mapping[str, Any], accum: dict) -> None:
    """One Twelve Data request — never more than `MAX_SYMBOLS_PER_CALL`
    symbols, so never more than one request even inside `TwelveData` itself
    (a freshly-constructed instance's internal pacing only bites on a
    *second* batch within the same call, which this never asks for)."""
    symbols = step["symbols"]
    keep = set((accum.get("universe") or {}).get("keep_bars") or symbols)
    if step["endpoint"] == STEP_TIME_SERIES:
        bars = twelvedata.time_series(symbols, outputsize=PREMARKET_BARS)
        for symbol, rows in bars.items():
            # High and low ride along: ATR(14) needs the true range, and the
            # runner fetches no indicators of its own, so this is the only
            # place a runner cycle's ATR can come from.
            pairs = [
                {"date": row.get("datetime"), "close": close,
                 "high": _price_field(row, "high"), "low": _price_field(row, "low")}
                for row in rows
                if (close := _close(row)) is not None
            ]
            if not pairs:
                continue
            # Signals are computed here, from the full 250 bars, and only
            # what finalize still needs is kept.
            accum.setdefault("signals", {})[symbol] = _signals_from(pairs)
            if symbol in keep:
                accum["bars"][symbol] = pairs[-KEPT_BARS:]
                # The 3-month return needs 64 closes and only 60 are kept, so
                # it is taken here, off the full series, for the same symbols
                # the bars are kept for. No extra request.
                accum.setdefault("returns", {})[symbol] = _returns_from(
                    [pair["close"] for pair in pairs]
                )
    else:
        accum["sma200"].update(twelvedata.sma(symbols, SMA_PERIODS_FROM_TD[0]))
    if step.get("screen"):
        # The DIP screen is best-effort breadth: a symbol Twelve Data could not
        # serve is counted, not alerted, so one odd ticker among ~500 does not
        # page the operator every morning. Core-step errors alert as before.
        accum["screen_errors"] = int(accum.get("screen_errors") or 0) + len(twelvedata.errors)
    else:
        accum["errors"].extend(twelvedata.errors)


def _returns_from(closes: Sequence[float]) -> dict:
    """1-month and 3-month returns (`regime.RET_*_SESSIONS` sessions), the
    inputs to sector relative strength. Absent keys rather than nulls."""
    returns = {
        "ret_1m": regimes.period_return(closes, regimes.RET_1M_SESSIONS),
        "ret_3m": regimes.period_return(closes, regimes.RET_3M_SESSIONS),
        # Signed, zero or negative. Only SPY's is read (`_regime_inputs`).
        "drawdown_52w": regimes.drawdown_from_high(closes),
    }
    return {k: v for k, v in returns.items() if v is not None}


def _signals_from(pairs: Sequence[Mapping[str, Any]]) -> dict:
    """SMA 20/50/200, Wilder RSI(14) and ATR(14) from one symbol's bars.
    Absent keys rather than nulls (see `_finalize`'s write)."""
    closes = [p["close"] for p in pairs]
    signals: dict[str, Any] = {f"sma_{n}": _sma(closes, n) for n in SMA_PERIODS_ALL}
    signals["rsi"] = _rsi(closes)
    signals["atr"] = _atr(pairs)
    return {k: v for k, v in signals.items() if v is not None}


# ---------------------------------------------------------------------------
# Finalize — every step has run; compute and write the three tables once
# ---------------------------------------------------------------------------


def _finalize(
    supabase: Supabase, loaded: Mapping[str, Any], trading_day: str, state: Mapping[str, Any],
    now_iso: str,
) -> dict:
    accum = state["accum"]
    universe = accum["universe"]
    pead_targets: list[dict] = universe["pead_targets"]
    bars: dict[str, list[dict]] = accum["bars"]
    sma200: dict[str, float] = accum["sma200"]
    closes = {symbol: [pair["close"] for pair in pairs] for symbol, pairs in bars.items()}

    # Signals were computed per step (`_signals_from`); a plan frozen by the
    # previous deploy has only `bars` + Twelve Data's `sma200`, so those are
    # derived here instead. One code path either way.
    computed: dict[str, dict] = {k: dict(v) for k, v in (accum.get("signals") or {}).items()}
    for symbol, pairs in bars.items():
        if symbol not in computed:
            computed[symbol] = _signals_from(pairs)
        if sma200.get(symbol) is not None:
            computed[symbol]["sma_200"] = sma200[symbol]
    for symbol, signals in computed.items():
        if signals.get("sma_200") is not None:
            sma200.setdefault(symbol, signals["sma_200"])

    entries = {
        symbol: {
            "signals": signals,
            "bars_meta": {
                "last_bar": (bars.get(symbol) or [{}])[-1].get("date"),
                "source": "twelvedata",
                # RSI/ATR are *daily*, as of the prior close, not the intraday
                # reading a live indicator fetch returns.
                "indicators": "daily_close",
            },
            "fetched_at": now_iso,
        }
        for symbol, signals in sorted(computed.items())
        if signals
    }
    # One batched, merging write per DAY_CONTEXT_BATCH symbols: a
    # per-symbol RPC for ~500 names would not fit the 60-second tick.
    written = 0
    names = list(entries)
    for start in range(0, len(names), DAY_CONTEXT_BATCH):
        chunk = {name: entries[name] for name in names[start:start + DAY_CONTEXT_BATCH]}
        supabase.rpc(
            "edge_day_context_write_symbols",
            {"p_trading_day": trading_day, "p_symbols": chunk},
        )
        written += len(chunk)

    sector_etfs = sorted((loaded["etfs"].get("sector") or {}).keys())
    regime_inputs = _regime_inputs(
        closes, sma200, sector_etfs,
        returns=accum.get("returns") or {},
        sma50={k: v["sma_50"] for k, v in computed.items() if v.get("sma_50") is not None},
    )
    regime = regimes.evaluate(regime_inputs, loaded["config"])
    regime_payload = {
        "regime_inputs": regime_inputs,
        "regime": regime.as_dict() if regime else None,
        "sector_table": regime.sector_states if regime else {},
        "computed_at": now_iso,
        "source": "premarket-job",
        "symbols": len(bars),
        "credits_used": state["credits_used"],
    }
    supabase.rpc(
        "edge_regime_write",
        {"p_trading_day": trading_day, "p_payload": regime_payload},
    )

    reaction_rows = []
    for target in pead_targets:
        computed = pead_reaction.reaction(
            target["report_date"], target.get("timing"), bars.get(target["symbol"]) or []
        )
        if computed is not None:
            reaction_rows.append({"symbol": target["symbol"], **computed})
    if reaction_rows:
        supabase.rpc("edge_earnings_reactions_write", {"p_rows": reaction_rows})

    errors = list(accum["errors"])
    if errors:
        # O-3. `errors` was already persisted every tick (`_save_state` ->
        # `edge_premarket_state.accum.errors`, queryable) but never
        # surfaced to the operator -- a degraded day's data went unnoticed
        # unless someone thought to check the table by hand. Threshold: any
        # error at all. One bad symbol still means one candidate's
        # `day_context`/regime input never warmed, and Twelve Data errors
        # here are rare enough (§ "Why this runs across many invocations"
        # above) that even one is worth a look, not a count to tune a
        # cutoff against.
        supabase.rpc(
            "edge_alerts_enqueue",
            {
                "p_alerts": [
                    {
                        "title": report.PREMARKET_DATA_DEGRADED,
                        "body": (
                            f"{len(errors)} Twelve Data error(s) this run, "
                            f"{written} symbol(s) still written:\n"
                            + "\n".join(str(e) for e in errors)
                        ),
                        "priority": "default",
                    }
                ]
            },
        )

    return {
        "status": STATUS_OK,
        "trading_day": trading_day,
        "symbols": len(bars),
        "day_context_written": written,
        "regime": (regime.market_state if regime else None),
        "reactions_written": len(reaction_rows),
        "credits_used": state["credits_used"],
        "errors": errors,
        "discovered": len(universe.get("discovered") or []),
        "screened": universe.get("screened", 0),
        "screen_dropped": universe.get("screen_dropped", 0),
        "screen_errors": int(accum.get("screen_errors") or 0),
    }


# ---------------------------------------------------------------------------
# Persisted state — one row per trading day, read/written wholesale
# ---------------------------------------------------------------------------


def _load_state(supabase: Supabase, trading_day: str) -> dict | None:
    rows = supabase.select(
        "edge_premarket_state",
        f"trading_day=eq.{trading_day}&select=plan,next_index,accum,credits_used,status,detail",
    )
    if not rows:
        return None
    row = rows[0]
    return {
        "plan": row.get("plan") or [],
        "next_index": row.get("next_index") or 0,
        "accum": row.get("accum") or {"bars": {}, "sma200": {}, "errors": [],
                                       "universe": {"regime_symbols": [], "pead_targets": []}},
        "credits_used": row.get("credits_used") or 0,
        "status": row.get("status") or STATUS_IN_PROGRESS,
        "detail": row.get("detail"),
    }


def _save_state(supabase: Supabase, trading_day: str, state: Mapping[str, Any]) -> None:
    supabase.rpc(
        "edge_premarket_state_write",
        {
            "p_trading_day": trading_day,
            "p_state": {
                "plan": state["plan"],
                "next_index": state["next_index"],
                "accum": state["accum"],
                "credits_used": state["credits_used"],
                "status": state["status"],
                "detail": state["detail"],
            },
        },
    )


# ---------------------------------------------------------------------------
# Symbol universe
# ---------------------------------------------------------------------------


def _prefetch_candidates(
    supabase: Supabase, loaded: Mapping[str, Any]
) -> tuple[list[str], list[str], str | None]:
    """`(discovered, screen_pool, error)` for today's plan.

    * **discovered** — today's PEAD/CATALYST triggers, exactly as the 09:40
      `full` cycle will find them: `candidates.assemble` over the same
      `events`/`macro`/`snapshot` the runner reads (`run.checkout_document`)
      and the operator's sector rotation, 20-name cap and all.
    * **screen_pool** — every universe member the active rotation admits,
      heaviest index weight first, so a truncated screen keeps the names that
      matter most. This is what gives a DIP scanner survivor (found live by the
      session's `run_scan`, which this job cannot run) a cached RSI/ATR.

    Never raises: a failure here returns `([], [], detail)` and the regime job
    carries on exactly as it did before this existed.
    """
    try:
        control = supabase.rpc("edge_control_json") or {}
        docs = {key: (core_run.checkout_document(supabase, key)[0] or {})
                for key in ("snapshot", "events", "macro")}
        payload = {**docs, "control": control if isinstance(control, Mapping) else {}}

        discovered: list[str] = []
        for row in candidates.assemble(payload, loaded).get("triggers") or []:
            if row.get("symbol") and row["symbol"] not in discovered:
                discovered.append(row["symbol"])

        rotation = universes.sector_rotation(
            payload["control"].get("active_sectors"), loaded["etfs"],
            loaded.get("rotation_codes"), universes.sub_industry_map(docs["snapshot"]),
        )
        sectors = universes.sector_map({"snapshot": docs["snapshot"], "etfs": loaded["etfs"]})
        holdings = sorted(
            (r for r in docs["snapshot"].get("holdings") or [] if isinstance(r, Mapping)),
            key=lambda r: (-_weight(r), str(r.get("symbol") or "")),
        )
        screen: list[str] = []
        for row in holdings:
            symbol = row.get("symbol")
            if symbol and symbol not in screen and rotation.inactive(symbol, sectors) is None:
                screen.append(symbol)
        return discovered, screen, None
    except Exception as exc:  # noqa: BLE001 - breadth is optional; the regime is not
        return [], [], f"{type(exc).__name__}: {exc}"


def _weight(row: Mapping[str, Any]) -> float:
    try:
        return float(row.get("weight") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _held_symbols(supabase: Supabase, trading_day: str) -> list[str]:
    """Everything the last reconciled book carried. `edge_prior` returns the
    most recent runlog book regardless of which cycle wrote it — a job at
    06:30 reads yesterday's post-close book, which is exactly the holdings
    this morning opens with."""
    rows = supabase.select(
        "edge_runlog",
        "book=not.is.null&select=book&order=id.desc&limit=1",
    )
    book = rows[0].get("book") if rows else None
    positions = (book or {}).get("positions") or []
    return sorted(
        {p["symbol"] for p in positions if isinstance(p, Mapping) and p.get("symbol")}
    )


def _dip_survivors(supabase: Supabase) -> list[str]:
    """This morning's Universe Pick watchlist, as `dip_recovery.watchlist.pick`
    persisted it into `edge_control.dip_recovery_watchlist`
    (`WatchlistPick.control_updates`, `src/dip_recovery/watchlist.py:448`).

    That row is `{"trading_day", "symbols"}` — a flat list of tickers, not the
    `{"trading_day", "triggers", "macro_targets"}` shape `cli.candidates`
    writes under the *different* key `today_watchlist`. Reading `.triggers`
    off this key always returned `[]`: the key
    that name lives under simply never has it.
    """
    rows = supabase.select(
        "edge_control",
        "key=eq.dip_recovery_watchlist&select=payload",
    )
    payload = rows[0].get("payload") if rows else None
    symbols = (payload or {}).get("symbols") or []
    return sorted({s for s in symbols if s})


# How many recent `decide` rows the PEAD targets are gathered from. Four
# decides a day (overnight, full, monitor, post-close) times the three-session
# window, with room for a retried cycle.
PEAD_DECIDE_LOOKBACK = 16


def _recent_earnings(supabase: Supabase) -> dict[str, Mapping[str, Any]]:
    """Every symbol's earnings view from the recent `decide` rows, the one
    with the latest report per symbol.

    **Not the latest row alone** (the defect P2 fixed). The row
    this job finds at 05:30 ET is always the 22:47 overnight cycle, which is
    exit-only and carries the earnings of the held book and nothing else — so
    a name the 09:40 cycle had looked at as a PEAD candidate was never a
    target, its reaction was never computed, and PEAD skipped it "reaction
    None" on every cycle of its window. Reactions only ever landed on
    mornings where the last decide happened to be a full cycle.
    """
    rows = supabase.select(
        "edge_runlog",
        "command=eq.decide&stdout=not.is.null&select=stdout"
        f"&order=id.desc&limit={PEAD_DECIDE_LOOKBACK}",
    )
    merged: dict[str, Mapping[str, Any]] = {}
    for row in rows:  # newest first
        earnings = ((row.get("stdout") if isinstance(row, Mapping) else None) or {}).get(
            "earnings"
        ) or {}
        for symbol, view in earnings.items():
            if not isinstance(view, Mapping) or not view.get("last_report_date"):
                continue
            held = merged.get(symbol)
            if held is None or str(view["last_report_date"]) > str(held["last_report_date"]):
                merged[symbol] = view
    return merged


def _pead_targets(
    supabase: Supabase, loaded: Mapping[str, Any], trading_day: str
) -> list[dict]:
    """Symbols inside the PEAD window whose reaction has not been computed yet.

    **The window is counted against today**, from each view's
    `last_report_date`, not read off the stored `sessions_since_report`. That
    figure was true on the day the cycle ran; this job runs the next morning,
    and a name that reported before yesterday's open read `0` then (outside
    the window) and is `1` now — exactly the session PEAD first wants it.
    """
    # PEAD is retired (`plays.RETIRED`), and the reaction is a number only
    # PEAD reads. No targets means no Twelve Data bars fetched for them and no
    # rows written.
    if plays.play_off(loaded, plays.PEAD) is not None:
        return []

    earnings = _recent_earnings(supabase)
    today = date.fromisoformat(trading_day)
    calendar = loaded.get("calendar") or {}

    done = supabase.select(
        "edge_earnings_reactions",
        "select=symbol,report_date&order=reaction_session.desc&limit=200",
    )
    seen = {
        (r.get("symbol"), str(r.get("report_date"))[:10])
        for r in done
        if isinstance(r, Mapping)
    }

    first, last = PEAD_WINDOW_SESSIONS
    targets: list[dict] = []
    for symbol, view in earnings.items():
        if not isinstance(view, Mapping):
            continue
        if view.get("status") != "known":
            continue
        report_date = view.get("last_report_date")
        try:
            since = cadence.sessions_between(
                date.fromisoformat(str(report_date)[:10]), today, calendar
            )
        except (TypeError, ValueError):
            continue
        if not (first <= since <= last):
            continue
        if (symbol, str(report_date)[:10]) in seen:
            continue
        targets.append(
            {
                "symbol": symbol,
                "report_date": str(report_date)[:10],
                "timing": view.get("timing"),
            }
        )
    return targets


# ---------------------------------------------------------------------------
# regime_inputs — the shape src/regime.evaluate expects
# ---------------------------------------------------------------------------


def _regime_inputs(
    closes: Mapping[str, Sequence[float]],
    sma200: Mapping[str, float],
    sector_etfs: Sequence[str],
    returns: Mapping[str, Mapping[str, float]] | None = None,
    sma50: Mapping[str, float] | None = None,
) -> dict:
    """`returns` and `sma50` are P1's additions: what breadth over the 50-day
    and sector relative strength are computed from. Both are derived from
    bars this job already fetched. A plan frozen by the previous deploy
    carries no `returns`, so each is re-derived from the kept closes where
    they reach back far enough (the 1-month return does, the 3-month does
    not) and left out otherwise — `regime` reads an absent key as
    "unavailable", never as a number."""
    returns = returns or {}
    sma50 = sma50 or {}

    def trend(symbol: str, cs: Sequence[float]) -> dict:
        published = returns.get(symbol) or {}
        extra = {
            "ret_1m": published.get("ret_1m",
                                    regimes.period_return(cs, regimes.RET_1M_SESSIONS)),
            "ret_3m": published.get("ret_3m",
                                    regimes.period_return(cs, regimes.RET_3M_SESSIONS)),
        }
        return {k: v for k, v in extra.items() if v is not None}

    spy_closes = list(closes.get(SPY) or [])
    spy: dict[str, Any] = {}
    if spy_closes:
        spy = {
            "last": spy_closes[-1],
            "sma50": _sma(spy_closes, 50),
            "sma200": sma200.get(SPY),
            # `regime._spy_rv` derives 20-day realized vol from these when no
            # explicit `rv20` is given — the pure path, computed in `src/`.
            "closes": spy_closes,
            **trend(SPY, spy_closes),
        }
        # The 52-week high needs the whole fetched year, which only the step
        # sees; the 60 kept closes cannot stand in for it.
        drawdown = (returns.get(SPY) or {}).get("drawdown_52w")
        if drawdown is not None:
            spy["drawdown_52w"] = drawdown

    sectors: dict[str, dict] = {}
    for etf in sector_etfs:
        cs = list(closes.get(etf) or [])
        if not cs:
            continue
        sectors[etf] = {
            "last": cs[-1],
            "sma200": sma200.get(etf),
            "rsi14": _rsi(cs, RSI_PERIOD),
            **trend(etf, cs),
        }
        mean50 = sma50.get(etf, _sma(cs, 50))
        if mean50 is not None:
            sectors[etf]["sma50"] = mean50

    inputs: dict[str, Any] = {"spy": spy, "sectors": sectors}

    # Equal-weight against cap-weight over a month; positive is RSP leading.
    # Left out entirely when either leg is missing (or a vendor error on it).
    rsp_closes = list(closes.get(regimes.RSP) or [])
    rsp_1m = trend(regimes.RSP, rsp_closes).get("ret_1m") if rsp_closes else None
    spy_1m = spy.get("ret_1m")
    if rsp_1m is not None and spy_1m is not None:
        inputs["rsp"] = {"last": rsp_closes[-1], "ret_1m": rsp_1m}
        inputs["rsp_vs_spy_1m"] = rsp_1m - spy_1m

    return inputs


# ---------------------------------------------------------------------------
# Pure arithmetic on a close series
# ---------------------------------------------------------------------------


def _close(bar: Mapping[str, Any]) -> float | None:
    raw = bar.get("close")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 and not math.isinf(value) else None


def _price_field(bar: Mapping[str, Any], key: str) -> float | None:
    raw = bar.get(key)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 and not math.isinf(value) else None


def _atr(bars: Sequence[Mapping[str, Any]], period: int = ATR_PERIOD) -> float | None:
    """Wilder's ATR over daily bars (`{"high", "low", "close"}`, oldest first).

    True range is the largest of high-low and the gaps from the prior close;
    the first ATR is the mean of the first `period` true ranges, then
    Wilder-smoothed. `None` when fewer than `period + 1` bars carry a high and
    a low — a partial window is a different statistic, and an ATR stop measured
    from one would be a number nobody chose.
    """
    usable = [b for b in bars if b.get("high") is not None and b.get("low") is not None
              and b.get("close") is not None]
    if len(usable) < period + 1:
        return None
    ranges = []
    for prev, bar in zip(usable, usable[1:]):
        high, low, prior = float(bar["high"]), float(bar["low"]), float(prev["close"])
        ranges.append(max(high - low, abs(high - prior), abs(low - prior)))
    atr = sum(ranges[:period]) / period
    for tr in ranges[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def _sma(closes: Sequence[float], period: int) -> float | None:
    if len(closes) < period:
        return None
    window = closes[-period:]
    return sum(window) / period


def _rsi(closes: Sequence[float], period: int = RSI_PERIOD) -> float | None:
    """Wilder's RSI over the last `period` changes. `None` when the series is
    too short — a half-window RSI is a different statistic. Kept here rather
    than in `src/indicators.py` (parked) because it is the one oscillator this
    job needs and `regime._sector_state` treats it as optional anyway."""
    if len(closes) < period + 1:
        return None
    deltas = [b - a for a, b in zip(closes, closes[1:])]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))
