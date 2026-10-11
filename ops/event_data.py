# /// script
# requires-python = ">=3.11"
# dependencies = ["yfinance", "pandas", "numpy", "lxml", "html5lib", "beautifulsoup4", "requests", "tzdata"]
# ///
"""S5's event history: earnings reports with their surprise, and 8-K filings.

Run:  uv run ops/event_data.py     (fetches whatever is not cached; ~20 min cold)

Data only. Nothing here reads a return or decides anything; `ops/event_plays.py`
does that. Both sources are free and need no key:

* Yahoo's earnings calendar per symbol: the report's timestamp (which is how a
  before-the-open report is told from an after-the-close one), the consensus
  estimate, the reported figure and the surprise.
* EDGAR's submissions index per company: every 8-K with its filing date, its
  acceptance time and its item codes.

Cached under `.placebo_cache/` as `s5_earnings.pkl` and `s5_8k.pkl`. Delete a
file to refetch it.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import honest_backtest as hb  # noqa: E402

UA = {"User-Agent": "deterministic-trader research (ops/event_data.py)"}
FIRST_FILING = "2020-06-01"
EARNINGS_ROWS = 60


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def fetch_earnings_detail(symbols: list[str]) -> dict[str, list[dict]]:
    """`{symbol: [{day, hour, estimate, reported, surprise_pct}]}`, oldest
    first. ``hour`` is the report's local hour as Yahoo records it. A symbol
    Yahoo has nothing for is absent."""
    import yfinance as yf

    out: dict[str, list[dict]] = {}
    for n, symbol in enumerate(symbols):
        for attempt in range(3):
            try:
                frame = yf.Ticker(symbol.replace(".", "-")).get_earnings_dates(limit=EARNINGS_ROWS)
                if frame is not None and len(frame):
                    rows = {}
                    for ts, row in frame.iterrows():
                        rows[ts.date()] = {
                            "day": ts.date(), "hour": ts.hour + ts.minute / 60.0,
                            "estimate": _number(row.get("EPS Estimate")),
                            "reported": _number(row.get("Reported EPS")),
                            "surprise_pct": _number(row.get("Surprise(%)")),
                        }
                    out[symbol] = [rows[d] for d in sorted(rows)]
                break
            except Exception:  # noqa: BLE001 — retried, then left absent
                time.sleep(2.0 * (attempt + 1))
        if n % 50 == 0:
            print(f"  earnings {n}/{len(symbols)}", file=sys.stderr)
    return out


def _filings(block: dict) -> list[dict]:
    out = []
    forms = block.get("form") or []
    for i, form in enumerate(forms):
        if form != "8-K":
            continue
        day = block["filingDate"][i]
        if day < FIRST_FILING:
            continue
        accepted = (block.get("acceptanceDateTime") or [None] * len(forms))[i]
        hour = None
        if accepted:
            try:
                # EDGAR stamps acceptance in UTC ("...Z"); the filing desk runs
                # on Eastern time. Kept as the UTC hour; the reader converts.
                stamp = datetime.fromisoformat(accepted.replace("Z", "+00:00"))
                hour = stamp.hour + stamp.minute / 60.0
            except ValueError:
                hour = None
        items = [x for x in str((block.get("items") or [""] * len(forms))[i] or "").split(",") if x]
        out.append({"day": date.fromisoformat(day), "accepted_utc_hour": hour, "items": items,
                    "accession": block["accessionNumber"][i]})
    return out


def fetch_8k(ciks: dict[str, str]) -> dict[str, list[dict]]:
    """`{symbol: [{day, accepted_utc_hour, items, accession}]}` for every 8-K
    filed from `FIRST_FILING`, oldest first. A company EDGAR does not answer
    for is absent."""
    import requests

    session = requests.Session()
    session.headers.update(UA)

    def get(url: str) -> dict | None:
        for attempt in range(4):
            try:
                response = session.get(url, timeout=60)
                if response.status_code == 200:
                    return response.json()
                if response.status_code == 404:
                    return None
            except Exception:  # noqa: BLE001
                pass
            time.sleep(2.0 * (attempt + 1))
        return None

    out: dict[str, list[dict]] = {}
    for n, (symbol, cik) in enumerate(sorted(ciks.items())):
        body = get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json")
        time.sleep(0.15)                      # EDGAR's fair-access limit is 10 a second
        if body is None:
            continue
        rows = _filings((body.get("filings") or {}).get("recent") or {})
        for older in (body.get("filings") or {}).get("files") or []:
            if str(older.get("filingTo") or "9999") < FIRST_FILING:
                continue
            block = get(f"https://data.sec.gov/submissions/{older['name']}")
            time.sleep(0.15)
            if block:
                rows.extend(_filings(block))
        seen = {}
        for row in rows:
            seen[row["accession"]] = row
        out[symbol] = sorted(seen.values(), key=lambda r: (r["day"], r["accession"]))
        if n % 50 == 0:
            print(f"  8-K {n}/{len(ciks)}", file=sys.stderr)
    return out


def load() -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """Both caches, fetched on first use."""
    universe = json.loads((ROOT / "data" / "universe.json").read_text(encoding="utf-8"))
    ciks = {r["symbol"]: r["cik"] for r in universe["cross_check"] if r.get("cik")}
    dates = hb.cached("earnings.pkl", lambda: {})
    symbols = sorted(set(dates) | set(ciks))
    earnings = hb.cached("s5_earnings.pkl", lambda: fetch_earnings_detail(symbols))
    filings = hb.cached("s5_8k.pkl", lambda: fetch_8k(ciks))
    return earnings, filings


if __name__ == "__main__":
    earnings, filings = load()
    reports = sum(len(v) for v in earnings.values())
    with_surprise = sum(1 for v in earnings.values() for r in v if r["surprise_pct"] is not None)
    print(f"earnings: {len(earnings)} symbols, {reports} reports, {with_surprise} with a surprise")
    print(f"8-K: {len(filings)} companies, {sum(len(v) for v in filings.values())} filings")
