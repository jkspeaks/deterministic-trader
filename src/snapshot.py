"""snapshot.py — the decision snapshot: every candidate, as `decide` saw it.

Until now the only record
of a cycle's candidates was `edge_runlog.stdout`: the names, a verdict, a
message. The numbers the verdict was read from — the RSI, the averages, the
spread, the sector's state — were in the payload and nowhere afterwards, so no
question of the form "did the names we ranked higher go on to do better" could
be asked of live operation at all.

`rows` turns one cycle into one row per **(symbol, play)**, the rejected ones
included: a name a play skipped, a name sizing could not size, a name guard
denied and a name discovery dropped before any play saw it are each a row with
the stage it stopped at and the code that stopped it. A selection rule can
only be judged against the names it turned down.

**It derives nothing the plays did not read.** Every feature is copied from
the same `payload` / `decide` output the plan was built from, or is a ratio of
two such numbers (`atr_pct`, `spread_pct`, the distance to an average). The
figures that need a year of daily bars — momentum, relative strength, the
52-week high — are not here: `src/forward.py` computes them later, from bars
strictly before the decision day, and `services/core/core/evidence.py` writes
them onto the same row.

**Absent is absent.** A figure the cycle did not have is `None`, never zero
and never a default; a row with no RSI says the play judged without one.

Pure, and it runs after `decide`: nothing here can change a plan.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from src import regime as regimes

# Where a candidate stopped. Ordered: each is further than the one before.
DROPPED = "DROPPED"      # discovery took it out before any play judged it
UNTRIGGERED = "UNTRIGGERED"  # fetched and handed to the plays; none spoke
SKIPPED = "SKIPPED"      # a play triggered and declined
UNSIZED = "UNSIZED"      # ranked, and sizing could not size it
DENIED = "DENIED"        # sized, and guard refused the buy
RANKED = "RANKED"        # ranked, and no slot / cash reached it
APPROVED = "APPROVED"    # a buy is in the plan

# A dropped name was never assigned to a play; the key still needs a value.
NO_PLAY = "NONE"

DISCOVERY_DROP = "DISCOVERY_DROP"
NO_PLAY_VERDICT = "NO_PLAY_VERDICT"


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or not denominator:
        return None
    return round(numerator / denominator, 6)


def _price(quote: Mapping[str, Any]) -> tuple[float | None, float | None]:
    """`(reference price, spread as a fraction of the mid)`.

    The mid where both sides are quoted, else the last trade. The spread only
    where both sides exist and are sane — a crossed or one-sided quote has no
    spread to report, and the program's cost model must fall back to its
    measured median rather than read a zero here.
    """
    bid, ask = _number(quote.get("bid")), _number(quote.get("ask"))
    if bid and ask and ask >= bid > 0.0:
        mid = (bid + ask) / 2.0
        return round(mid, 6), round((ask - bid) / mid, 6)
    return _number(quote.get("last")), None


def _features(symbol: str, payload: Mapping[str, Any], decided: Mapping[str, Any],
              etfs: Mapping[str, Any]) -> dict:
    signal = (payload.get("signals") or {}).get(symbol) or {}
    quote = (payload.get("quotes") or {}).get(symbol) or {}
    fundamentals = (payload.get("fundamentals") or {}).get(symbol) or {}
    earnings = (decided.get("earnings") or {}).get(symbol) or {}
    regime = decided.get("regime") or {}
    breadth = regime.get("breadth") or {}

    resolved = regimes.sector_and_etf(symbol, decided.get("sectors"), etfs)
    sector, etf = resolved if resolved else ((decided.get("sectors") or {}).get(symbol), None)

    price, spread = _price(quote)
    rsi, atr = _number(signal.get("rsi")), _number(signal.get("atr"))
    sma_20, sma_50, sma_200 = (_number(signal.get(k)) for k in ("sma_20", "sma_50", "sma_200"))
    volume = _number(fundamentals.get("average_volume"))

    def distance(average: float | None) -> float | None:
        return None if price is None or not average else round(price / average - 1.0, 6)

    return {
        "sector": sector,
        "sector_etf": etf,
        "sector_state": (regime.get("sector_states") or {}).get(etf) if etf else None,
        "market_state": regime.get("market_state"),
        "posture": regime.get("posture"),
        "breadth_200": _number(breadth.get("breadth_200")),
        "breadth_50": _number(breadth.get("breadth_50")),
        "price": price,
        "spread_pct": spread,
        "rsi": rsi,
        "atr": atr,
        "atr_pct": _ratio(atr, price),
        "sma_20": sma_20,
        "sma_50": sma_50,
        "sma_200": sma_200,
        "dist_sma_20": distance(sma_20),
        "dist_sma_50": distance(sma_50),
        "dist_sma_200": distance(sma_200),
        "average_volume": volume,
        "adv_usd": None if volume is None or price is None else round(volume * price, 2),
        "market_cap": _number(fundamentals.get("market_cap")),
        "sessions_until_report": _number(earnings.get("sessions_until_report")),
        "sessions_since_report": _number(earnings.get("sessions_since_report")),
    }


def _is_security(symbol: str, payload: Mapping[str, Any], decided: Mapping[str, Any]) -> bool:
    """Whether anything at all is known about ``symbol`` as a tradable name.

    `MACRO` records its skip against the *event* ("FOMC printed on
    consensus"), so `entries.skipped` carries rows whose `symbol` is not a
    ticker. Such a row has no sector, no quote and no indicators; it would sit
    in the table with every feature null and cost the back-fill a credit a
    night looking for bars that cannot exist.
    """
    return any(
        symbol in (source or {})
        for source in (decided.get("sectors"), payload.get("quotes"), payload.get("signals"))
    )


def _buys(orders: Sequence[Mapping[str, Any]] | None) -> set[str]:
    return {
        str(o["symbol"]) for o in orders or []
        if isinstance(o, Mapping) and o.get("side") == "buy" and o.get("symbol")
    }


def _denials(guard: Mapping[str, Any] | None) -> dict[str, str]:
    """`{symbol: first denial code}` for every buy guard refused."""
    out: dict[str, str] = {}
    for row in (guard or {}).get("denied") or []:
        order = (row or {}).get("order") or {}
        if order.get("side") != "buy" or not order.get("symbol"):
            continue
        codes = [d.get("code") for d in row.get("denials") or [] if isinstance(d, Mapping)]
        out.setdefault(str(order["symbol"]), str(codes[0]) if codes else DENIED)
    return out


def rows(
    payload: Mapping[str, Any], decided: Mapping[str, Any], etfs: Mapping[str, Any]
) -> list[dict]:
    """One row per (symbol, play) the cycle judged, and one per dropped name.

    ``payload`` is the `cycle.json` `decide` was given; ``decided`` is what it
    returned (with `candidates_dropped`, which the hosted core adds). `[]` for
    a cycle that could not buy: there was no selection to record, and a table
    of exit-only cycles would dilute every rate computed from it.
    """
    cycle = decided.get("cycle") or {}
    if not cycle.get("entries_allowed"):
        return []

    entries = decided.get("entries") or {}
    sizing = decided.get("sizing") or {}
    approved = _buys((decided.get("plan") or {}).get("orders"))
    denied = _denials(decided.get("guard"))
    unsized = {
        str(u["symbol"]): str(u.get("reason") or UNSIZED)
        for u in sizing.get("unsized") or [] if isinstance(u, Mapping) and u.get("symbol")
    }

    base = {
        "cycle_id": decided.get("cycle_id") or payload.get("cycle_id"),
        "trading_day": cycle.get("trading_day"),
        "decided_at": cycle.get("now"),
        "cycle_type": cycle.get("cycle_type"),
    }
    cache: dict[str, dict] = {}

    def row(symbol: str, play_id: str, verdict: str, code: str | None,
            detail: str | None, extra: Mapping[str, Any] | None = None) -> dict:
        if symbol not in cache:
            cache[symbol] = _features(symbol, payload, decided, etfs)
        return {
            **base,
            "symbol": symbol,
            "play_id": play_id,
            "verdict": verdict,
            "reason_code": code,
            "detail": detail,
            "placed": None,
            **cache[symbol],
            **(extra or {}),
        }

    out: dict[tuple[str, str], dict] = {}

    for rank, candidate in enumerate(entries.get("candidates") or [], start=1):
        symbol, play_id = candidate.get("symbol"), candidate.get("play_id")
        if not symbol or not play_id:
            continue
        if symbol in approved:
            verdict, code = APPROVED, None
        elif symbol in denied:
            verdict, code = DENIED, denied[symbol]
        elif symbol in unsized:
            verdict, code = UNSIZED, unsized[symbol]
        else:
            verdict, code = RANKED, None
        out[(symbol, play_id)] = row(
            symbol, play_id, verdict, code, candidate.get("detail"),
            {
                "rank": rank,
                "conviction": _number(candidate.get("conviction")),
                "conviction_raw": _number(candidate.get("conviction_raw")),
            },
        )

    for skip in entries.get("skipped") or []:
        symbol, play_id = skip.get("symbol"), skip.get("play_id")
        if not symbol or not play_id or (symbol, play_id) in out:
            continue
        if not _is_security(symbol, payload, decided):
            continue
        extra = {"factors": dict(skip["factors"])} if isinstance(skip.get("factors"), Mapping) else None
        out[(symbol, play_id)] = row(
            symbol, play_id, SKIPPED, skip.get("code"), skip.get("message"), extra
        )

    # A pool name no play said anything about. Several plays return no skip
    # when their first condition is simply not met (a scanner survivor whose
    # RSI has since recovered is not "oversold and then rejected", it is just
    # not a DIP) — so without this the names a rule passed over most quietly
    # would be the ones missing from the record of what it passed over.
    judged = {symbol for symbol, _ in out}
    for symbol in (decided.get("universe") or {}).get("candidates") or []:
        if symbol in judged or not isinstance(symbol, str):
            continue
        out[(symbol, NO_PLAY)] = row(symbol, NO_PLAY, UNTRIGGERED, NO_PLAY_VERDICT, None)

    judged = {symbol for symbol, _ in out}
    for drop in decided.get("candidates_dropped") or []:
        symbol = (drop or {}).get("symbol")
        if not symbol or symbol in judged or (symbol, NO_PLAY) in out:
            continue
        out[(symbol, NO_PLAY)] = row(symbol, NO_PLAY, DROPPED, DISCOVERY_DROP, drop.get("reason"))

    return list(out.values())


def with_placements(
    snapshot: Sequence[Mapping[str, Any]], placements: Sequence[Mapping[str, Any]] | None
) -> list[dict]:
    """The same rows, each `APPROVED` one marked with whether the broker took it.

    `placed` stays `None` on every other row: "not approved" and "approved and
    not placed" are different findings and only the second is a `False`.
    """
    placed = _buys(placements)
    return [
        {**row, "placed": row["symbol"] in placed} if row.get("verdict") == APPROVED else dict(row)
        for row in snapshot
    ]


def signalling(decided: Mapping[str, Any]) -> set[str]:
    """Every name this cycle's discovery or plays touched, plus the book.

    What the control arm (`src/control_arm.py`) may **not** draw: a "random
    same-sector name" that had itself just triggered a play, or that the
    account already holds, is not a control.
    """
    entries = decided.get("entries") or {}
    names: set[str] = set()
    for key in ("candidates", "skipped"):
        names.update(str(r["symbol"]) for r in entries.get(key) or [] if r.get("symbol"))
    names.update(
        str(r["symbol"]) for r in decided.get("candidates_dropped") or []
        if isinstance(r, Mapping) and r.get("symbol")
    )
    names.update(str(r["symbol"]) for r in decided.get("book") or [] if r.get("symbol"))
    names.update(str(s) for s in ((decided.get("universe") or {}).get("candidates") or []))
    return names
