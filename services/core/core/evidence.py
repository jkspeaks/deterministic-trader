"""The evidence back-fill.

`run.py` writes one `edge_decision_snapshot` row per candidate per
entry-capable cycle (`src/snapshot.py`). This job comes back later and puts on
each row the two things a cycle cannot know: what the name's price history
said (`forward.price_features`, from bars strictly before the decision day)
and what happened next (`forward.forward_returns`, 1/3/5/8/21 sessions).

**It is never on a cycle's path.** pg_cron posts to `/api/evidence` in the
small hours; the function reads Supabase and Twelve Data, updates
rows nothing that decides ever reads, and returns a summary. A night it does
not run costs nothing but a day's delay — every row it did not reach is still
pending tomorrow.

**The budget is the design.** Twelve Data Basic allows 8 credits a minute and
one credit per symbol, so one pass is four requests of eight, a minute apart
(`handler` builds the client with that interval), and `MAX_SYMBOLS` is 32.
Two rules keep the nightly spend near the number of *new* names rather than
the number of open rows:

* a row is **visited only when it has something to gain** — it has no
  price features yet, or a horizon in `VISIT_AT` has matured since the last
  visit. Five horizons are filled in three visits (after 1, 8 and 21
  sessions), not five;
* one fetched symbol fills **every** pending row for that symbol, across
  days and plays.

**Nothing is guessed.** A symbol with no bars gets `missing_bar` on each
matured horizon and no features; `forward.py` owns what counts as a return.
A row whose last horizon is past `forward.MISSING_GRACE_DAYS` with bars still
absent is closed as it stands, so a delisted name is not fetched for ever.

**Analyst estimates come from a second vendor, after the bars** (S2).
No source this project can reach keeps a consensus estimate as it stood on a
past date, so the estimate-revision factor cannot be backtested; it can only
be recorded from now on. A row a play judged gets Alpha Vantage's
`EARNINGS_ESTIMATES` for its name (`core/alphavantage.py`), once, within
`ESTIMATE_MAX_AGE_DAYS` of its decision. Twelve Data has the same figures and
refuses them to this account's plan (403, "ultra or enterprise"), which is why
a second vendor is here at all. Four rules:

* **the bars come first.** Estimates are fetched after them, on a separate
  key with its own limits, inside a time budget that leaves the function's
  five minutes to the bars;
* **the free key is 25 requests a day.** A pass asks about at most
  `alphavantage.MAX_REQUESTS_PER_PASS` names, oldest decision first. When the
  day's requests are gone the vendor says so, the walk stops, and the names
  not reached are asked about another night while they are still in window.
  A name that ages out unasked simply has no estimate; nothing is filled in;
* **throttled is "later", anything else is the answer.** A name the vendor
  has nothing for is written as `unavailable` with the reason and is not
  asked about again. A refusal of the *key* is the answer for every name the
  pass was going to ask about, and costs one request;
* **absent-safe.** No key on the deployment means no request and no write.
  The selection reads `has_estimates` and `verdict` from the pending rows; a
  database that does not send them gets no request either.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Sequence

from src import cli, forward

from .supabase import Supabase
from . import alphavantage
from .alphavantage import AlphaVantage
from .twelvedata import TwelveData

STATUS_OK = "ok"
STATUS_FAILED = "failed"

# One pass's ceiling, in Twelve Data credits (= symbols). Four requests.
MAX_SYMBOLS = 32

# Daily bars per symbol. 12-1 momentum needs 253 closes before the decision
# day; 300 leaves the 21-session forward window and a fortnight's slack in
# front of it. Twelve Data charges per symbol whatever the depth.
BAR_COUNT = 300

# The horizons whose maturity earns a row a visit. Every horizon that has
# matured by then is filled on the same visit.
VISIT_AT = (1, 8, 21)

MARKET = "SPY"
BARS_SOURCE = "twelvedata"

# How long after its decision a row may still be given estimates. The
# registered reading is "7 days before the fetch over 90 days before it";
# inside this window both of those dates still precede the decision.
ESTIMATE_MAX_AGE_DAYS = 5
# A name discovery dropped before any play saw it is not a candidate the
# ranker would ever order, and is most of a day's rows.
ESTIMATE_SKIP_VERDICTS = frozenset({"DROPPED"})
ESTIMATES_OK = "ok"
ESTIMATES_UNAVAILABLE = "unavailable"


def run_backfill(
    supabase: Supabase,
    twelvedata: TwelveData | None,
    *,
    estimator: AlphaVantage | None = None,
    config_dir=None,
    now: str | None = None,
    max_symbols: int = MAX_SYMBOLS,
    write: bool = True,
) -> dict:
    """One pass. Returns a summary; never raises.

    ``twelvedata`` None (no key on the deployment) still closes the rows that
    are past saving and reports how much is owed — the honest answer for a
    deployment with no market-data key, as in `shadow.run_shadow`.
    """
    try:
        loaded = cli.load_config(config_dir)
        today = (now or "")[:10] or date.today().isoformat()
        pending = [
            r for r in supabase.rpc("edge_decision_snapshot_pending", {}) or []
            if isinstance(r, Mapping) and r.get("symbol") and r.get("trading_day")
        ]
        plans = [_plan(row, loaded["calendar"], today) for row in pending]
        due = [p for p in plans if p["due"]]
        expired = [p for p in plans if p["expired"] and not p["due"]]

        symbols, extras = _choose(due, max_symbols)
        bars: dict[str, list[dict]] = {}
        if twelvedata is not None and symbols:
            fetched = twelvedata.time_series([*symbols, *extras], outputsize=BAR_COUNT)
            bars = {symbol: forward.bars_from(rows) for symbol, rows in fetched.items()}

        updates = [
            _update(plan, bars, loaded["calendar"], today)
            for plan in due
            if plan["row"]["symbol"] in symbols and twelvedata is not None
        ]
        updates += [_close(plan) for plan in expired]

        # Estimates, after the bars on purpose: whatever this vendor does,
        # the returns have already been fetched.
        wanting = _wanting_estimates(pending, today)
        estimates: dict[str, dict] = {}
        if estimator is not None and wanting:
            estimates = _fetch_estimates(estimator, list(wanting), today)
        updates = _with_estimates(updates, wanting, estimates)

        written = 0
        if write and updates:
            written = supabase.rpc("edge_decision_forward_write", {"p_rows": updates}) or 0
    except Exception as exc:  # noqa: BLE001 — the boundary, as `run_shadow`'s is
        return {"status": STATUS_FAILED, "detail": f"{type(exc).__name__}: {exc}"}

    return {
        "status": STATUS_OK,
        "today": today,
        "pending": len(pending),
        "due": len(due),
        "symbols_due": len({p["row"]["symbol"] for p in due}),
        "symbols_fetched": sorted(symbols) if twelvedata is not None else [],
        "symbols_without_bars": sorted(s for s in symbols if not bars.get(s))
        if twelvedata is not None else [],
        "closed_unfilled": len(expired),
        "updated": len(updates),
        "written": written,
        "estimates_wanted": len(wanting),
        "estimates_fetched": sorted(s for s, e in estimates.items() if e["status"] == ESTIMATES_OK),
        "estimates_unavailable": sorted(
            s for s, e in estimates.items() if e["status"] == ESTIMATES_UNAVAILABLE),
        "estimate_source": None if estimator is None else alphavantage.SOURCE,
        "estimate_requests": 0 if estimator is None else estimator.requests,
        "credits": 0 if twelvedata is None else twelvedata.budget.used,
        "errors": ([] if twelvedata is None else list(twelvedata.errors))
        + ([] if estimator is None else list(estimator.errors)),
    }


# ---------------------------------------------------------------------------
# Which rows are worth a credit tonight
# ---------------------------------------------------------------------------


def _plan(row: Mapping[str, Any], calendar: Mapping[str, Any], today: str) -> dict:
    """Whether this row gains anything from a fetch, and whether it is past
    gaining anything at all."""
    day = str(row["trading_day"])[:10]
    status = row.get("fwd_status") or {}
    sessions = forward.sessions_after(day, max(forward.HORIZONS), calendar)
    cutoff = date.fromisoformat(today)

    matured: list[int] = []
    waiting: list[int] = []      # matured, unresolved, still worth a retry
    for horizon in forward.HORIZONS:
        target = date.fromisoformat(sessions[horizon - 1])
        if target >= cutoff:
            continue
        matured.append(horizon)
        seen = status.get(str(horizon))
        if seen in forward.FINAL:
            continue
        if seen == forward.MISSING_BAR and (cutoff - target).days > forward.MISSING_GRACE_DAYS:
            continue
        waiting.append(horizon)

    gate = max((v for v in VISIT_AT if v in matured), default=None)
    owes_returns = gate is not None and any(h <= gate for h in waiting)
    # Every horizon has had its chance and nothing is still retryable: the row
    # is closed with whatever it has — including no features, for a name the
    # vendor never had bars for.
    expired = len(matured) == len(forward.HORIZONS) and not waiting
    return {
        "row": row,
        "day": day,
        "due": owes_returns or (not row.get("has_features") and not expired),
        "expired": expired,
    }


def _choose(due: Sequence[Mapping[str, Any]], max_symbols: int) -> tuple[list[str], list[str]]:
    """`(symbols, extras)` for one pass: the names with the oldest pending
    row first, and the SPY / sector-ETF series their features need, all inside
    ``max_symbols``.

    A name is taken only if it and whatever new reference series it needs
    still fit — a stock fetched without its sector ETF would be written with a
    null relative strength and never revisited for it.
    """
    order: list[str] = []
    needs: dict[str, set[str]] = {}
    for plan in sorted(due, key=lambda p: (p["day"], p["row"]["symbol"])):
        row = plan["row"]
        symbol = row["symbol"]
        if symbol not in needs:
            order.append(symbol)
            needs[symbol] = set()
        if not row.get("has_features"):
            needs[symbol].add(MARKET)
            if row.get("sector_etf"):
                needs[symbol].add(str(row["sector_etf"]))

    symbols: list[str] = []
    extras: list[str] = []
    for symbol in order:
        new = [e for e in sorted(needs[symbol]) if e not in extras and e not in symbols]
        fresh = 0 if symbol in extras else 1
        if len(symbols) + len(extras) + fresh + len(new) > max_symbols:
            break
        symbols.append(symbol)
        extras.extend(new)
    # A name that is also a reference series (a sector ETF that is itself a
    # candidate) is fetched once.
    return symbols, [e for e in extras if e not in symbols]


# ---------------------------------------------------------------------------
# One row's update
# ---------------------------------------------------------------------------


def _key(row: Mapping[str, Any]) -> dict:
    return {key: row[key] for key in ("cycle_id", "symbol", "play_id")}


def _update(
    plan: Mapping[str, Any],
    bars: Mapping[str, list[dict]],
    calendar: Mapping[str, Any],
    today: str,
) -> dict:
    row = plan["row"]
    series = bars.get(row["symbol"]) or []
    result = forward.forward_returns(series, plan["day"], calendar, today)

    out = {
        **_key(row),
        "fwd": result["fwd"],
        "fwd_status": result["status"],
        "complete": result["complete"],
        "bars_source": BARS_SOURCE if series else None,
        "price_features": None,
    }
    # Only from real bars, and only once: "as of the decision" must not be
    # recomputed from a history that has since been re-adjusted or extended.
    if series and not row.get("has_features"):
        out["price_features"] = forward.price_features(
            series,
            plan["day"],
            market=bars.get(MARKET),
            sector=bars.get(str(row.get("sector_etf") or "")),
        )
    return out


# ---------------------------------------------------------------------------
# Analyst estimates
# ---------------------------------------------------------------------------


def _wanting_estimates(pending: Sequence[Mapping[str, Any]], today: str) -> dict[str, list[dict]]:
    """`{symbol: [row key, ...]}` for rows that should be given estimates
    tonight, the oldest decision first (it is the nearest to ageing out).

    `has_estimates` must be an explicit False: a pending row that does not
    carry the key at all comes from a database without the estimate columns, which has
    nowhere to put an estimate.
    """
    cutoff = date.fromisoformat(today)
    wanted: dict[str, list[dict]] = {}
    for row in sorted(pending, key=lambda r: (str(r["trading_day"]), str(r["symbol"]))):
        if row.get("has_estimates") is not False:
            continue
        if not row.get("verdict") or row["verdict"] in ESTIMATE_SKIP_VERDICTS:
            continue
        age = (cutoff - date.fromisoformat(str(row["trading_day"])[:10])).days
        if 0 <= age <= ESTIMATE_MAX_AGE_DAYS:
            wanted.setdefault(str(row["symbol"]), []).append(_key(row))
    return wanted


def _fetch_estimates(estimator: AlphaVantage, symbols: Sequence[str], today: str) -> dict[str, dict]:
    """What to store per symbol: the two registered readings and the vendor's
    fiscal-year rows they were read from, or that the vendor has no answer and
    why. A symbol the walk never reached, or that was throttled, is not in the
    result — it is asked about again on a later pass."""
    rows, failed = estimator.estimates(symbols)
    out: dict[str, dict] = {}
    for symbol, got in rows.items():
        out[symbol] = {
            "status": ESTIMATES_OK,
            "fetched_on": today,
            "source": alphavantage.SOURCE,
            **forward.estimate_features_alphavantage(got, today),
            "estimates": forward.fiscal_years_ahead(got, today),
        }

    def unavailable(failure: Mapping[str, Any]) -> dict:
        return {"status": ESTIMATES_UNAVAILABLE, "fetched_on": today,
                "source": alphavantage.SOURCE, "kind": failure.get("kind"),
                "reason": failure.get("message")}

    for symbol, failure in failed.items():
        if failure.get("kind") == alphavantage.SYMBOL:
            out[symbol] = unavailable(failure)
        elif failure.get("kind") == alphavantage.REFUSED:
            # The key was refused, not the name: the same answer stands for
            # every name this pass was going to ask about.
            for other in symbols:
                out.setdefault(other, unavailable(failure))
    return out


def _with_estimates(
    updates: Sequence[dict], wanting: Mapping[str, Sequence[Mapping[str, Any]]],
    estimates: Mapping[str, dict],
) -> list[dict]:
    """The pass's updates with each fetched estimate on its rows.

    One update per row key, always: the write is a single `update ... from`
    over the batch, and two source rows for one key would apply only one of
    them. A row with nothing else to write tonight gets an update that carries
    the estimate alone; every other field is absent and so left as stored.
    """
    by_key = {(u["cycle_id"], u["symbol"], u["play_id"]): u for u in updates}
    out = list(updates)
    for symbol, estimate in estimates.items():
        for key in wanting.get(symbol) or []:
            ident = (key["cycle_id"], key["symbol"], key["play_id"])
            if ident not in by_key:
                by_key[ident] = dict(key)
                out.append(by_key[ident])
            by_key[ident]["estimates"] = estimate
    return out


def _close(plan: Mapping[str, Any]) -> dict:
    """A row past every horizon's grace: closed, its statuses left as they are."""
    return {**_key(plan["row"]), "fwd": None, "fwd_status": None, "complete": True,
            "bars_source": None, "price_features": None}
