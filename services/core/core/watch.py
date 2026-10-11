"""The 30-minute state-change gate.

Every RemoteTrigger between 10:00 and 15:30 ET costs a full Claude session
today, whether or not anything happened. This job is the cheap alternative:
one Twelve Data quote batch (at most one credit per held symbol plus SPY,
well inside the per-minute limit `core/twelvedata.py` paces for) compared
against what should be resting behind the book, writing one boolean-shaped
signal (`edge_control.needs_session`) that `edge_should_run` reads before a monitor slot decides whether to run a real
cycle at all.

**What sets `needs_session`, in order of how cheaply each is checked:**

1. An entry was resubmitted this trading day (`edge_control.resubmit_candidates`,
   written by `reconcile` read here, not recomputed);
2. A held whole-share position has no `edge_protection` row at all — the position
   the guard-evaluated resting-stop design (`src/plays/protection.py`) exists to
   cover, uncovered;
3. a held whole-share position's live quote is within `watch_band_pct` of its
   `edge_protection` stop or target price;
4. SPY's live quote crosses the regime's own risk-on/risk-off line (its
   200-day SMA, the one threshold `src/regime.py` classifies risk_off from)
   on a side the last-computed `edge_regime` row did not have it on.

**What this deliberately does not implement.** "The daily scanner-survivor
list changed" is a `full`-cycle-only
signal, and `edge_should_run` already always runs a `full` slot regardless of
`needs_session` — there is no monitor slot this would need to wake
that isn't already covered by triggers 1-4, and nothing outside a `full`
cycle recomputes the scanner list for this to diff against. TODO [V] —
revisit if a future change makes the survivor list change intraday.

**No Robinhood, no orders, no book beyond one read of the last runlog.** The
only network this touches is Twelve Data and Supabase, the same boundary
`premarket.py` keeps. `edge_protection` is read, never derived here — it is
written by the hosted core's reconcile phase (`run.py._write_reconcile`)
from `src/plays/protection.desired`, the exact same computation a live cycle
already makes for the whole book every time it reconciles, so this job never
touches ATR, signals or entry price and cannot drift from what the core would
compute for the same book.

**Clearing `needs_session` is not this job's to do.** `run.py._decide_phase`
clears it the moment a non-noop cycle actually reaches decide — the gate's
promise is "something changed, a session should look", and only a session
that looked can honestly say it stopped being true.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from src import cadence, cli
from src.plays.core import _is_whole_share

from . import premarket
from .supabase import Supabase
from .twelvedata import TwelveData

SPY = "SPY"

STATUS_OK = "ok"
STATUS_FAILED = "failed"


def run_watch(
    supabase: Supabase,
    twelvedata: TwelveData,
    *,
    now: datetime | None = None,
    config_dir=None,
) -> dict:
    try:
        return _tick(supabase, twelvedata, now, config_dir)
    except Exception as exc:  # noqa: BLE001 — the boundary, like run.run
        return {"status": STATUS_FAILED, "detail": f"{type(exc).__name__}: {exc}"}


def _tick(
    supabase: Supabase, twelvedata: TwelveData, now, config_dir
) -> dict:
    loaded = cli.load_config(config_dir)
    moment = now or datetime.now(cadence.ET)
    now_iso = moment.astimezone(cadence.ET).replace(microsecond=0).isoformat()
    trading_day = cadence.trading_day(moment.astimezone(cadence.ET)).isoformat()

    reasons: list[dict] = _resubmit_reasons(supabase, trading_day)

    positions = _held_positions(supabase)
    whole = [p for p in positions if _is_whole_share(float(p.get("quantity") or 0.0))]
    held_symbols = sorted({p["symbol"] for p in positions if p.get("symbol")})

    if held_symbols:
        symbols = sorted({*held_symbols, SPY})
        prices = twelvedata.price(symbols)
        protection = _protection_by_symbol(supabase)
        band = float(loaded["config"]["watch_band_pct"])

        reasons += _protection_reasons(whole, prices, protection, band)
        reasons += _regime_reasons(supabase, trading_day, prices.get(SPY))
    else:
        prices = {}

    if reasons:
        symbols_flagged = sorted({r["symbol"] for r in reasons if r.get("symbol")})
        supabase.rpc(
            "edge_control_apply",
            {
                "p_updates": {
                    "needs_session": {
                        "reason": "; ".join(sorted({r["reason"] for r in reasons})),
                        "symbols": symbols_flagged,
                        "at": now_iso,
                    }
                }
            },
        )
        return {
            "status": STATUS_OK,
            "trading_day": trading_day,
            "needs_session": True,
            "reasons": reasons,
        }

    return {
        "status": STATUS_OK,
        "trading_day": trading_day,
        "needs_session": False,
        "checked": len(held_symbols),
    }


# ---------------------------------------------------------------------------
# Reasons
# ---------------------------------------------------------------------------


def _resubmit_reasons(supabase: Supabase, trading_day: str) -> list[dict]:
    """Reconcile already knows an entry was resubmitted — this reads that
    verdict rather than recomputing it. Same-day only: a stale
    `resubmit_candidates` row from a prior trading day is not this cycle's
    news."""
    rows = supabase.select(
        "edge_control", "key=eq.resubmit_candidates&select=payload"
    )
    payload = (rows[0].get("payload") if rows else None) or {}
    if payload.get("trading_day") != trading_day:
        return []
    triggers = payload.get("triggers") or []
    symbols = sorted({t["symbol"] for t in triggers if isinstance(t, Mapping) and t.get("symbol")})
    if not symbols:
        return []
    return [{"symbol": s, "reason": "entry_resubmitted"} for s in symbols]


def _protection_reasons(
    whole: list[Mapping[str, Any]],
    prices: Mapping[str, float],
    protection: Mapping[str, Mapping[str, float]],
    band: float,
) -> list[dict]:
    out: list[dict] = []
    for position in whole:
        symbol = position.get("symbol")
        price = prices.get(symbol)
        if price is None:
            continue
        levels = protection.get(symbol)
        if not levels:
            out.append({"symbol": symbol, "reason": "missing_resting_protection"})
            continue
        for kind in ("stop", "target"):
            level = levels.get(kind)
            if level is None or level <= 0:
                continue
            if abs(price - level) / level <= band:
                out.append({
                    "symbol": symbol,
                    "reason": f"near_{kind}",
                    "price": price,
                    "level": level,
                })
    return out


def _regime_reasons(
    supabase: Supabase, trading_day: str, spy_price: float | None
) -> list[dict]:
    """A cheap proxy for "the regime moved", not a re-run of `src/regime.py`:
    compares SPY's live quote against its own 200-day SMA (the one line
    `regime.evaluate`'s risk_off condition is anchored to) and flags a
    disagreement with the side the last-computed `edge_regime` row had it on.
    A full regime re-evaluation still only happens inside a real cycle; this
    only decides whether one should be allowed to."""
    if spy_price is None:
        return []
    day_context = supabase.select(
        "edge_day_context", f"trading_day=eq.{trading_day}&select=market_data"
    )
    sma_200 = (
        ((day_context[0].get("market_data") if day_context else None) or {})
        .get(SPY, {})
        .get("signals", {})
        .get("sma_200")
    )
    if sma_200 is None:
        return []

    live_side = "risk_off" if spy_price < float(sma_200) else "risk_on"

    regime_rows = supabase.select(
        "edge_regime", f"trading_day=eq.{trading_day}&select=payload"
    )
    market_state = (
        ((regime_rows[0].get("payload") if regime_rows else None) or {})
        .get("regime", {})
        .get("market_state")
    )
    if market_state is None:
        return []

    known_side = "risk_off" if market_state == "risk_off" else "risk_on"
    if live_side != known_side:
        return [{
            "symbol": SPY,
            "reason": "regime_threshold_crossed",
            "price": spy_price,
            "level": float(sma_200),
        }]
    return []


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _held_positions(supabase: Supabase) -> list[dict]:
    """The last reconciled book's positions, symbol + quantity — the same
    query `premarket._held_symbols` makes, minus the projection to symbols
    only, because this job needs quantity to tell a whole-share position from
    a fractional one (`src/plays/protection.py`'s own `_coverable` rule)."""
    rows = supabase.select(
        "edge_runlog", "book=not.is.null&select=book&order=id.desc&limit=1"
    )
    book = rows[0].get("book") if rows else None
    positions = (book or {}).get("positions") or []
    return [p for p in positions if isinstance(p, Mapping) and p.get("symbol")]


def _protection_by_symbol(supabase: Supabase) -> dict[str, dict[str, float]]:
    """`edge_protection`, shaped `{symbol: {"stop": price, "target": price}}`.
    Written by `run.py._write_reconcile` from `src/plays/protection.desired`
    every reconciling cycle; this job only reads it."""
    rows = supabase.select("edge_protection", "select=symbol,kind,price")
    out: dict[str, dict[str, float]] = {}
    for row in rows:
        symbol = row.get("symbol")
        kind = row.get("kind")
        price = row.get("price")
        if not symbol or kind not in ("stop", "target") or price is None:
            continue
        out.setdefault(symbol, {})[kind] = float(price)
    return out
