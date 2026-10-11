# /// script
# requires-python = ">=3.11"
# dependencies = ["yfinance", "pandas", "numpy", "lxml", "html5lib", "beautifulsoup4", "requests", "tzdata"]
# ///
"""Leaders monthly, recorded and not traded (S5).

Run:  uv run ops/leaders_shadow.py                  (this month, if not yet recorded)
      uv run ops/leaders_shadow.py --month 2026-09  (a named month; for checking)
      uv run ops/leaders_shadow.py --print          (compute and show; write nothing)

Once a month `.github/workflows/leaders.yml` runs this. It asks no model
anything and pays no vendor: daily bars from Yahoo, the rule in
`src/leaders.py`, one JSON file. It places no order and nothing in the trading
cycle reads what it writes.

For month M it reads closes strictly before M's first calendar day (so the day
it happens to run on cannot change the picks), records the picks beside a
seeded random same-sector control, and on later runs fills in what each side
and SPY returned from the open of M's first session to the open of the next
month's. That is the record the program's kill criteria are judged on.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from src import leaders  # noqa: E402

RECORD = ROOT / "data" / "leaders_shadow.json"
MARKET = "SPY"
HISTORY = "2y"
SEED = 20261005


def fetch(symbols: list[str]) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    out: dict[str, pd.DataFrame] = {}
    tickers = [s.replace(".", "-") for s in symbols]
    for i in range(0, len(tickers), 50):
        chunk = tickers[i: i + 50]
        frame = yf.download(chunk, period=HISTORY, auto_adjust=True, progress=False,
                            group_by="ticker", threads=True)
        for ticker in chunk:
            try:
                bars = frame[ticker].dropna(subset=["Close"])
            except KeyError:
                continue
            if len(bars):
                out[ticker.replace("-", ".")] = bars
    return out


def month_of(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def first_session(calendar: list[date], month: str) -> date | None:
    return next((d for d in calendar if month_of(d) == month), None)


def next_month(month: str) -> str:
    year, m = int(month[:4]), int(month[5:])
    return f"{year + (m == 12):04d}-{m % 12 + 1:02d}"


def decide(bars: dict[str, pd.DataFrame], universe: dict, etfs: dict, month: str) -> dict | None:
    """The picks for ``month`` from closes before its first calendar day."""
    cutoff = pd.Timestamp(f"{month}-01")
    calendar = bars[MARKET].index[bars[MARKET].index < cutoff]
    if len(calendar) < leaders.LOOKBACK + 1:
        return None

    def closes(symbol: str) -> list[float | None]:
        series = bars[symbol]["Close"].reindex(calendar)
        return [None if pd.isna(v) else float(v) for v in series]

    sector_of = {r["symbol"]: r["sector"] for r in universe["cross_check"]}
    etf_of = {sector: etf for etf, sector in etfs["sector"].items()}
    members = [s for s in sector_of if s in bars]
    ranked = leaders.rank(
        {s: closes(s) for s in members}, closes(MARKET),
        {sector: closes(etf) for sector, etf in etf_of.items() if etf in bars},
        sector_of, members=members,
    )
    return {
        "month": month,
        "as_of": str(calendar[-1].date()),
        "members_with_bars": len(members),
        "eligible": len(ranked["eligible"]),
        "absent": ranked["absent"],
        "picks": [{**row, "score": round(row["score"], 4)} for row in ranked["picks"]],
        "control": leaders.control(ranked, month, SEED),
        "result": None,
    }


def settle(entry: dict, bars: dict[str, pd.DataFrame]) -> dict | None:
    """Open-to-open returns for a recorded month, once both opens exist."""
    calendar = [ts.date() for ts in bars[MARKET].index]
    start = first_session(calendar, entry["month"])
    end = first_session(calendar, next_month(entry["month"]))
    if start is None or end is None:
        return None

    def gain(symbol: str) -> float | None:
        frame = bars.get(symbol)
        if frame is None:
            return None
        opens = frame["Open"]
        a, b = opens.get(pd.Timestamp(start)), opens.get(pd.Timestamp(end))
        if a is None or b is None or pd.isna(a) or pd.isna(b) or a <= 0:
            return None
        return float(b / a - 1.0)

    def side(rows: list[dict]) -> tuple[dict, float | None]:
        each = {row["symbol"]: gain(row["symbol"]) for row in rows}
        known = [v for v in each.values() if v is not None]
        return each, (sum(known) / len(known) if known else None)

    picks, picks_mean = side(entry["picks"])
    control, control_mean = side(entry["control"])
    return {"entry_day": str(start), "exit_day": str(end), "picks": picks, "picks_mean": picks_mean,
            "control": control, "control_mean": control_mean, "spy": gain(MARKET),
            "note": "open to open, before costs; a missing name is left out of its side's mean"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", help="YYYY-MM; default is the current month")
    parser.add_argument("--print", action="store_true", dest="show", help="write nothing")
    args = parser.parse_args()

    universe = json.loads((ROOT / "data" / "universe.json").read_text(encoding="utf-8"))
    etfs = json.loads((ROOT / "config" / "etfs.json").read_text(encoding="utf-8"))
    record = json.loads(RECORD.read_text(encoding="utf-8")) if RECORD.is_file() else {"months": []}
    month = args.month or month_of(datetime.now(timezone.utc).date())

    recorded = {m["month"]: m for m in record["months"]}
    before = json.dumps(record["months"], sort_keys=True)
    wanted = sorted({r["symbol"] for r in universe["cross_check"]}
                    | {row["symbol"] for m in record["months"] for row in [*m["picks"], *m["control"]]})
    bars = fetch([*wanted, MARKET, *sorted(etfs["sector"])])
    if MARKET not in bars:
        print("no market bars; nothing recorded", file=sys.stderr)
        return 1

    if month not in recorded:
        entry = decide(bars, universe, etfs, month)
        if entry is None:
            print(f"not enough history to decide {month}", file=sys.stderr)
            return 1
        recorded[month] = entry
    for entry in recorded.values():
        if entry.get("result") is None:
            entry["result"] = settle(entry, bars)

    record = {
        "rule": {"source": "src/leaders.py", "top_n": leaders.TOP_N,
                 "lookback": leaders.LOOKBACK, "skip": leaders.SKIP, "trend_sessions": leaders.TREND_SESSIONS,
                 "seed": SEED, "traded": False},
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "months": [recorded[m] for m in sorted(recorded)],
    }
    text = json.dumps(record, indent=1, sort_keys=True) + "\n"
    if args.show:
        print(json.dumps(recorded[month], indent=1))
        return 0
    if json.dumps(record["months"], sort_keys=True) == before:
        print(f"{month}: already recorded, nothing new to settle")
        return 0
    RECORD.write_text(text, encoding="utf-8", newline="\n")
    print(f"{month}: " + ", ".join(row["symbol"] for row in recorded[month]["picks"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
