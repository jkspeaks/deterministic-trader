# /// script
# requires-python = ">=3.11"
# dependencies = ["yfinance", "pandas", "numpy", "lxml", "html5lib", "beautifulsoup4", "requests", "tzdata"]
# ///
"""S1: the honest offline backtest of classic DIP against a random control.

Run:  uv run ops/honest_backtest.py            (the registered run; ~10 min cold)
      uv run ops/honest_backtest.py --smoke    (plumbing only: counts, no edges)

Pre-registered. This file implements the S1 study and decides nothing. The
arithmetic is `ops/backtest_engine.py` (tested in
the suite); the statistics are `ops/eval_selection.py`; the sizing rules are
the shipped `src.sizing` functions. What is here is data: bars, earnings
dates, index history, and the loops that hand them to those three.

Everything fetched is cached under `.placebo_cache/`. Delete a cache file to
refetch it. The run writes `.placebo_cache/s1_results.json` and prints the
report tables; `--ledger` appends one row per reported comparison to
the trial ledger (replacing this script's earlier S1 rows, so a
re-run of the same registered list does not count twice).
"""

from __future__ import annotations

import argparse
import io
import json
import pickle
import sys
import time
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import backtest_engine as bt  # noqa: E402
import eval_selection as es  # noqa: E402
import placebo_backtest as pb  # noqa: E402
from src import sizing  # noqa: E402

CACHE = ROOT / ".placebo_cache"
START = "2020-06-01"
FIRST_TRADE = date(2021, 1, 4)          # a 200-day average needs the bars before it
OOS_START = date(2024, 1, 1)
START_EQUITY = 1400.0
PER_TRADE_NOTIONAL = 200.0              # `min_position_size`: the size costs are charged at
CONTROL_PORTFOLIOS = 500
BOOT = 10_000
SEED = 20261005
MIN_PEERS = 5
DEDUPE = 8
COVERAGE_BAR = 0.80
EARNINGS_HORIZON_DAYS = 120
TERMINAL_LOSS = -0.30
UA = {"User-Agent": "deterministic-trader research (ops/honest_backtest.py)"}

# Yahoo's sector names, for removed index members the universe file does not
# carry, as the GICS names `data/universe.json` uses.
YAHOO_SECTOR = {
    "Technology": "Information Technology", "Financial Services": "Financials",
    "Consumer Cyclical": "Consumer Discretionary", "Consumer Defensive": "Consumer Staples",
    "Healthcare": "Health Care", "Basic Materials": "Materials",
    "Communication Services": "Communication Services", "Energy": "Energy",
    "Industrials": "Industrials", "Real Estate": "Real Estate", "Utilities": "Utilities",
}


# ---------------------------------------------------------------------------
# Data, cached
# ---------------------------------------------------------------------------


def cached(name: str, build):
    path = CACHE / name
    if path.exists():
        return pickle.loads(path.read_bytes())
    value = build()
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pickle.dumps(value))
    return value


def fetch_changes() -> list[tuple[date, str | None, str | None]]:
    """`(effective date, ticker added, ticker removed)` from Wikipedia's
    "Historical components of the S&P 500"."""
    import requests

    html = requests.get(
        "https://en.wikipedia.org/wiki/Historical_components_of_the_S%26P_500",
        headers=UA, timeout=30,
    ).text
    table = max(pd.read_html(io.StringIO(html)), key=len)
    table.columns = ["date", "added", "added_name", "removed", "removed_name", "reason", "refs"][: len(table.columns)]
    out = []
    for row in table.itertuples(index=False):
        try:
            when = datetime.strptime(str(row.date).split("[")[0].strip(), "%B %d, %Y").date()
        except ValueError:
            continue

        def ticker(value):
            text = str(value).strip()
            return None if text in ("", "nan", "None", "—", "-") else text.split("[")[0].strip()

        out.append((when, ticker(row.added), ticker(row.removed)))
    return out


def fetch_bars(symbols: list[str]) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    out: dict[str, pd.DataFrame] = {}
    tickers = [s.replace(".", "-") for s in symbols]
    for i in range(0, len(tickers), 50):
        chunk = tickers[i : i + 50]
        frame = yf.download(chunk, start=START, auto_adjust=True, progress=False,
                            group_by="ticker", threads=True)
        for t in chunk:
            try:
                x = frame[t].dropna(subset=["Close"])
            except KeyError:
                continue
            # A removed name may have traded for only part of the window; 260
            # bars is what a 200-day average and a few trades need.
            if len(x) > 260:
                out[t.replace("-", ".")] = x
    return out


def fetch_sectors(symbols: list[str]) -> dict[str, str]:
    import yfinance as yf

    out = {}
    for symbol in symbols:
        try:
            sector = yf.Ticker(symbol.replace(".", "-")).info.get("sector")
        except Exception:  # noqa: BLE001 — a name Yahoo has dropped has no info
            sector = None
        if sector in YAHOO_SECTOR:
            out[symbol] = YAHOO_SECTOR[sector]
    return out


def fetch_earnings(symbols: list[str]) -> dict[str, list[date]]:
    """Report dates per symbol, sorted. A symbol Yahoo has nothing for is
    absent — which is what coverage is measured from."""
    import yfinance as yf

    out: dict[str, list[date]] = {}
    for n, symbol in enumerate(symbols):
        for attempt in range(3):
            try:
                frame = yf.Ticker(symbol.replace(".", "-")).get_earnings_dates(limit=40)
                if frame is not None and len(frame):
                    out[symbol] = sorted({ts.date() for ts in frame.index})
                break
            except Exception:  # noqa: BLE001
                time.sleep(2.0 * (attempt + 1))
        if n % 50 == 0:
            print(f"  earnings {n}/{len(symbols)}", file=sys.stderr)
    return out


# ---------------------------------------------------------------------------
# The panel
# ---------------------------------------------------------------------------


def none_list(values: np.ndarray) -> list:
    return [float(v) if np.isfinite(v) else None for v in values]


def build_series(frame: pd.DataFrame, calendar: pd.DatetimeIndex, days: list[date],
                 reports: list[date] | None) -> bt.Series:
    """Indicators on the symbol's own trading days, then laid on the common
    calendar with None where it did not trade."""
    ind = pb.indicators(frame)
    dollar = (frame["Close"] * frame["Volume"]).rolling(20).mean().shift(1)
    own = pd.DataFrame(
        {"o": ind["o"], "h": ind["h"], "l": frame["Low"].to_numpy(float), "c": ind["c"],
         "rsi": ind["rsi"], "atr": ind["atr"], "sma20": ind["sma20"], "sma200": ind["sma200"],
         "adv": dollar.to_numpy(float)},
        index=frame.index,
    ).reindex(calendar)

    until = bt.sessions_until_report(days, reports, EARNINGS_HORIZON_DAYS)
    return bt.Series(*(none_list(own[c].to_numpy(float)) for c in
                       ("o", "h", "l", "c", "rsi", "atr", "sma20", "sma200", "adv")), until)


def covered(reports: list[date] | None, day: date) -> bool:
    return bt.earnings_covered(reports, day, EARNINGS_HORIZON_DAYS)


# ---------------------------------------------------------------------------
# Signals, pools and the per-trade comparison
# ---------------------------------------------------------------------------


class Study:
    """One universe (today's members, or members by date) and everything
    computed from it. ``member(symbol, day_index)`` gates who is eligible."""

    def __init__(self, panel, sectors, days, member=None):
        self.panel, self.sectors, self.days = panel, sectors, days
        self.member = member or (lambda symbol, t: True)
        self.first = next(i for i, d in enumerate(days) if d >= FIRST_TRADE)
        self.by_sector: dict[str, list[str]] = {}
        for symbol in sorted(panel):
            if symbol in sectors:
                self.by_sector.setdefault(sectors[symbol], []).append(symbol)
        self._signals: dict[bool, dict[str, list]] = {}
        self._trades: dict[tuple, bt.Trade | None] = {}
        self._pools: dict[tuple, list[str]] = {}

    def signals(self, veto: bool) -> dict[str, list]:
        if veto not in self._signals:
            n = len(self.days)
            self._signals[veto] = {
                symbol: [bt.dip_signal(s, t, earnings_veto=veto)
                         if t >= self.first and self.member(symbol, t) else None
                         for t in range(n)]
                for symbol, s in self.panel.items() if symbol in self.sectors
            }
        return self._signals[veto]

    def trade(self, symbol: str, t: int, exits: bt.Exits) -> bt.Trade | None:
        key = (symbol, t, exits)
        if key not in self._trades:
            self._trades[key] = bt.simulate_trade(self.panel[symbol], t, exits)
        return self._trades[key]

    def pool(self, symbol: str, t: int, veto: bool) -> list[str]:
        """Same-sector names that are in the universe that day, opened that
        day, did not signal and (with the veto) are clear of earnings."""
        key = (symbol, t, veto)
        if key not in self._pools:
            sig = self.signals(veto)
            out = []
            for peer in self.by_sector.get(self.sectors[symbol], ()):
                if peer == symbol or not self.member(peer, t):
                    continue
                s = self.panel[peer]
                if s.o[t] is None or sig[peer][t] is not None:
                    continue
                # Not a signal merely because of the veto is still a signal
                # the play would have wanted: exclude it either way.
                if veto and (bt.blocked_by_earnings(s, t) or self.signals(False)[peer][t] is not None):
                    continue
                out.append(peer)
            self._pools[key] = out
        return self._pools[key]

    def net(self, symbol: str, trade: bt.Trade, costs: bt.Costs, notional: float) -> float:
        s = self.panel[symbol]
        adv = s.adv or [None] * len(s)
        return costs.net_return(trade.ret, notional, adv[trade.entry_day],
                                adv[trade.exit_day], trade.reason)

    def events(self, veto: bool) -> list[tuple[str, int]]:
        """One event per symbol per `DEDUPE` sessions."""
        out = []
        for symbol, row in self.signals(veto).items():
            last = -10 ** 9
            for t, conviction in enumerate(row):
                if conviction is None or t - last <= DEDUPE:
                    continue
                last = t
                out.append((symbol, t))
        return out

    def paired(self, events, exits: bt.Exits, costs: bt.Costs, veto: bool) -> list[dict]:
        """Per event: the signal's return, the mean of its eligible controls'."""
        rows = []
        for symbol, t in events:
            mine = self.trade(symbol, t, exits)
            if mine is None or mine.ret is None:
                continue
            peers = []
            for peer in self.pool(symbol, t, veto):
                theirs = self.trade(peer, t, exits)
                if theirs is not None and theirs.ret is not None:
                    peers.append(self.net(peer, theirs, costs, PER_TRADE_NOTIONAL))
            if len(peers) < MIN_PEERS:
                continue
            ret = self.net(symbol, mine, costs, PER_TRADE_NOTIONAL)
            control = sum(peers) / len(peers)
            rows.append({"symbol": symbol, "t": t, "day": self.days[t], "ret": ret,
                         "control": control, "diff": ret - control, "reason": mine.reason,
                         "peers": len(peers)})
        return rows


def summarise(rows: list[dict], first: date | None = None, last: date | None = None) -> dict:
    rows = [r for r in rows if (first is None or r["day"] >= first) and (last is None or r["day"] < last)]
    stat = es.clustered_bootstrap([r["diff"] for r in rows], [str(r["day"]) for r in rows],
                                  boot=BOOT, seed=SEED)
    return {
        "n": len(rows),
        "dip_mean": es.mean([r["ret"] for r in rows]) if rows else None,
        "control_mean": es.mean([r["control"] for r in rows]) if rows else None,
        "diff_sd": es.stdev([r["diff"] for r in rows]) if len(rows) > 1 else None,
        "paired": stat,
    }


# ---------------------------------------------------------------------------
# The portfolio
# ---------------------------------------------------------------------------


def shipped_caps(config: dict) -> bt.Caps:
    return bt.Caps(
        n_target=lambda equity: sizing.n_target(equity, config),
        position_cap_fraction=sizing.position_cap_fraction,
        sector_cap_fraction=sizing.sector_cap_fraction,
        max_new_per_day=int(config["max_new_positions_per_day"]),
        cash_reserve_pct=float(config["cash_reserve_pct"]),
        cash_reserve_floor=float(config["cash_reserve_floor"]),
        reentry_days=int(config["wash_sale_days"]),
    )


def program_text_caps(config: dict) -> bt.Caps:
    """The caps as the program's S1 paragraph words them, not as shipped."""
    return bt.Caps(
        n_target=lambda equity: 5,
        position_cap_fraction=sizing.position_cap_fraction,
        sector_cap_fraction=sizing.sector_cap_fraction,
        max_new_per_day=2,
        cash_reserve_pct=float(config["cash_reserve_pct"]),
        cash_reserve_floor=float(config["cash_reserve_floor"]),
        reentry_days=int(config["wash_sale_days"]),
        one_per_sector_day=True,
    )


def window(days: list[date], first: date, last: date | None) -> tuple[int, int]:
    """The first and last day indices of ``[first, last)``."""
    lo = next(i for i, d in enumerate(days) if d >= first)
    hi = len(days) - 1 if last is None else max(i for i, d in enumerate(days) if d < last)
    return lo, hi


def signals_by_day(study: Study, veto: bool, lo: int, hi: int) -> dict[int, list[tuple[str, float]]]:
    """`{day: [(symbol, conviction)]}` for every DIP signal in the window."""
    by_day: dict[int, list[tuple[str, float]]] = {}
    for symbol, row in study.signals(veto).items():
        for t in range(lo, hi + 1):
            if row[t] is not None:
                by_day.setdefault(t, []).append((symbol, row[t]))
    return by_day


def dip_candidates(study: Study, by_day: dict[int, list[tuple[str, float]]]) -> dict[int, list[bt.Candidate]]:
    """The DIP portfolio's candidates: each signal buys itself."""
    return {t: [bt.Candidate(s, s, study.sectors[s], c) for s, c in rows]
            for t, rows in by_day.items()}


def portfolio(study: Study, caps: bt.Caps, exits: bt.Exits, costs: bt.Costs, veto: bool,
              first: date, last: date | None, controls: int) -> dict:
    """The DIP portfolio, ``controls`` random-control portfolios, and the
    paired statistic over the trades the DIP portfolio took."""
    days = study.days
    lo, hi = window(days, first, last)
    by_day = signals_by_day(study, veto, lo, hi)

    def simulate(symbol: str, t: int):
        return study.trade(symbol, t, exits)

    def run(candidates):
        return bt.replay(days, candidates, study.panel, caps, exits, costs,
                         start_equity=START_EQUITY, first_day=lo, last_day=hi, simulate=simulate)

    dip = run(dip_candidates(study, by_day))
    dip_metrics = bt.metrics(dip, START_EQUITY)

    control_totals, control_metrics = [], []
    for m in range(controls):
        candidates = {}
        for t, rows in by_day.items():
            picks = []
            for symbol, conviction in rows:
                peer = bt.pick(study.pool(symbol, t, veto), SEED, m, t, symbol)
                if peer is not None:
                    picks.append(bt.Candidate(peer, symbol, study.sectors[symbol], conviction))
            candidates[t] = picks
        metrics = bt.metrics(run(candidates), START_EQUITY)
        control_totals.append(metrics["total_return"])
        control_metrics.append(metrics)

    # Paired, over the trades the DIP portfolio actually took.
    rows = []
    for trade in dip.trades:
        if trade.net is None:
            continue
        peers = []
        for peer in study.pool(trade.signal, trade.entry_day, veto):
            theirs = study.trade(peer, trade.entry_day, exits)
            if theirs is not None and theirs.ret is not None:
                peers.append(study.net(peer, theirs, costs, trade.cost))
        if len(peers) < MIN_PEERS:
            continue
        control = sum(peers) / len(peers)
        rows.append({"symbol": trade.signal, "t": trade.entry_day, "day": days[trade.entry_day],
                     "ret": trade.net, "control": control, "diff": trade.net - control,
                     "reason": trade.reason, "peers": len(peers)})

    # What the unfilled entries would have returned (gross of the miss).
    missed = []
    for t, symbol in dip.missed:
        trade = study.trade(symbol, t, exits)
        if trade is not None and trade.ret is not None:
            missed.append(study.net(symbol, trade, costs, PER_TRADE_NOTIONAL))

    def avg(key):
        values = [m[key] for m in control_metrics if m[key] is not None]
        return es.mean(values) if values else None

    advs = [study.panel[tr.symbol].adv[tr.entry_day] for tr in dip.trades
            if study.panel[tr.symbol].adv and study.panel[tr.symbol].adv[tr.entry_day]]
    median_adv = float(np.median(advs)) if advs else None
    return {
        "dip": dip_metrics,
        "control": {k: avg(k) for k in ("total_return", "cagr", "sharpe", "max_drawdown",
                                        "turnover", "hit_rate", "payoff", "mean_net", "mae",
                                        "mfe", "held", "exposure", "cash_idle", "trades")},
        "control_dd_p05": float(np.percentile([m["max_drawdown"] for m in control_metrics], 5))
        if control_metrics else None,
        "control_dd_p95": float(np.percentile([m["max_drawdown"] for m in control_metrics], 95))
        if control_metrics else None,
        "control_total_p05": float(np.percentile(control_totals, 5)) if control_totals else None,
        "control_total_p95": float(np.percentile(control_totals, 95)) if control_totals else None,
        "beats_share": (sum(1 for x in control_totals if dip_metrics["total_return"] > x)
                        / len(control_totals)) if control_totals else None,
        "signal_days": sum(len(v) for v in by_day.values()),
        "taken": summarise(rows),
        "taken_rows": rows,
        "missed_mean": es.mean(missed) if missed else None,
        "median_adv": median_adv,
    }


def spy_hold(spy: pd.Series, days: list[date], first: date, last: date | None) -> dict | None:
    values = [(d, float(spy.get(pd.Timestamp(d), np.nan))) for d in days
              if d >= first and (last is None or d < last)]
    values = [(d, v) for d, v in values if np.isfinite(v)]
    if len(values) < 2:
        return None
    curve = [v / values[0][1] * START_EQUITY for _, v in values]
    return {"total_return": curve[-1] / START_EQUITY - 1.0,
            "max_drawdown": bt.max_drawdown(curve),
            "sharpe": bt.sharpe(curve[1:], curve[0])}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


class Inputs:
    """Everything a run reads, loaded once: the config, the calendar, the
    panel, both universes and the earnings coverage that decides step (d).
    `ops/beta_baseline.py` (S4) starts from the same object, so its policy arm
    is S1's portfolio and not a re-implementation of it."""


def load_inputs(smoke: bool = False) -> Inputs:
    t0 = time.time()
    args = argparse.Namespace(smoke=smoke)
    config = {k: v for k, v in json.loads((ROOT / "config" / "config.json").read_text(encoding="utf-8")).items()
              if not k.startswith("_")}
    universe = json.loads((ROOT / "data" / "universe.json").read_text(encoding="utf-8"))
    sector_now = {r["symbol"]: r["sector"] for r in universe["cross_check"]}
    current = sorted(sector_now)

    prices = pickle.loads((CACHE / "prices.pkl").read_bytes())
    changes = cached("sp500_changes.pkl", fetch_changes)
    calendar = pd.DatetimeIndex(sorted(set().union(*(set(prices[s].index) for s in current if s in prices))))
    days = [ts.date() for ts in calendar]
    membership = bt.Membership(current, changes, days[0], days[-1])

    removed = [s for s in membership.ever() if s not in sector_now]
    removed_bars = cached("removed_prices.pkl", lambda: fetch_bars(removed))
    removed_sectors = cached("removed_sectors.pkl", lambda: fetch_sectors(sorted(removed_bars)))
    everyone = sorted(set(s for s in current if s in prices) | set(removed_bars))
    if args.smoke:
        everyone = everyone[:70]
    earnings = cached("earnings.pkl", lambda: fetch_earnings(
        sorted(set(s for s in current if s in prices) | set(removed_bars))))

    bars = {**{s: prices[s] for s in current if s in prices}, **removed_bars}
    panel = {s: build_series(bars[s], calendar, days, earnings.get(s)) for s in everyone}
    sectors_all = {**removed_sectors, **sector_now}
    print(f"panel {len(panel)} symbols, {len(days)} days, {time.time() - t0:.0f}s", file=sys.stderr)

    today_only = {s: v for s, v in panel.items() if s in sector_now}
    survivors = Study(today_only, sector_now, days)
    pit = Study(panel, sectors_all, days,
                member=lambda symbol, t: membership.is_member(symbol, days[t]))

    with_rsi, no_rsi = bt.Exits(), bt.Exits(rsi_exit=None)
    gross = bt.Costs(enabled=False)
    net = bt.Costs()
    net_fill = bt.Costs(fill_probability=bt.OBSERVED_FILL_PROBABILITY, seed=SEED)

    # -- earnings coverage decides whether step (d) exists -------------------
    base_events = survivors.events(False)
    covered_events = sum(1 for s, t in base_events if covered(earnings.get(s), days[t]))
    coverage = covered_events / len(base_events) if base_events else 0.0
    use_earnings = coverage >= COVERAGE_BAR
    veto = use_earnings
    earn = (lambda e: bt.Exits(rsi_exit=e.rsi_exit, earnings_flatten=int(config["earnings_flatten_days"]))) \
        if use_earnings else (lambda e: e)

    # -- survivorship inputs -------------------------------------------------
    window = [d for d in days if d >= FIRST_TRADE]
    member_days = {s: membership.member_days(s, window) for s in membership.ever()}
    total_member_days = sum(member_days.values())
    unobserved = {s: n for s, n in member_days.items() if s not in panel or s not in sectors_all}
    if args.smoke:
        unobserved = {}
    unobserved_share = sum(unobserved.values()) / total_member_days if total_member_days else 0.0

    inputs = Inputs()
    for name, value in (
        ("config", config), ("days", days), ("panel", panel), ("today_only", today_only),
        ("sector_now", sector_now), ("sectors_all", sectors_all), ("survivors", survivors),
        ("pit", pit), ("membership", membership), ("changes", changes), ("removed", removed),
        ("removed_bars", removed_bars), ("earnings", earnings), ("calendar", calendar),
        ("with_rsi", with_rsi), ("no_rsi", no_rsi), ("gross", gross), ("net", net),
        ("net_fill", net_fill), ("base_events", base_events), ("coverage", coverage),
        ("use_earnings", use_earnings), ("veto", veto), ("earn", earn),
        ("unobserved", unobserved), ("unobserved_share", unobserved_share),
    ):
        setattr(inputs, name, value)
    return inputs


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="a small subset; prints counts only")
    ap.add_argument("--ledger", action="store_true", help="append the reported trials to the ledger")
    ap.add_argument("--controls", type=int, default=CONTROL_PORTFOLIOS)
    args = ap.parse_args()
    t0 = time.time()

    i = load_inputs(args.smoke)
    config, days, panel, today_only, sectors_all = i.config, i.days, i.panel, i.today_only, i.sectors_all
    survivors, pit, membership, changes = i.survivors, i.pit, i.membership, i.changes
    removed, removed_bars, earnings = i.removed, i.removed_bars, i.earnings
    with_rsi, no_rsi, gross, net, net_fill = i.with_rsi, i.no_rsi, i.gross, i.net, i.net_fill
    base_events, coverage, use_earnings, veto, earn = i.base_events, i.coverage, i.use_earnings, i.veto, i.earn
    unobserved, unobserved_share = i.unobserved, i.unobserved_share

    if args.smoke:
        rows = survivors.paired(base_events, with_rsi, gross, False)
        p = portfolio(survivors, shipped_caps(config), earn(no_rsi), net_fill, veto,
                      FIRST_TRADE, None, 5)
        print(json.dumps({
            "symbols": len(panel), "days": len(days), "events": len(base_events),
            "paired_rows": len(rows), "coverage": round(coverage, 3),
            "changes": len(changes), "members_ever": len(membership.ever()),
            "portfolio_trades": p["dip"]["trades"], "missed": p["dip"]["missed"],
            "taken_pairs": p["taken"]["n"], "skipped": p["dip"]["skipped"],
            "reasons": p["dip"]["reasons"], "seconds": round(time.time() - t0),
        }, indent=1))
        return

    results: dict = {"generated": datetime.utcnow().isoformat(timespec="seconds") + "Z"}
    results["data"] = {
        "symbols_today": len(today_only), "symbols_pit": len(panel),
        "first_day": str(days[0]), "last_day": str(days[-1]),
        "first_trade": str(FIRST_TRADE), "oos_start": str(OOS_START),
        "changes_in_window": sum(1 for c in changes if days[0] <= c[0] <= days[-1]),
        "members_ever": len(membership.ever()), "removed_names": len(removed),
        "removed_with_bars": len(removed_bars),
        "removed_with_bars_and_sector": sum(1 for s in removed_bars if s in sectors_all),
        "unobserved_names": len(unobserved), "unobserved_member_day_share": unobserved_share,
        "earnings_symbols": len(earnings), "earnings_event_coverage": coverage,
        "earnings_applied": use_earnings,
    }

    # -- steps (a)-(d): per-trade, all events, today's members ----------------
    steps = {}
    steps["a_next_open_rsi_gross"] = summarise(survivors.paired(base_events, with_rsi, gross, False))
    steps["b_no_rsi_gross"] = summarise(survivors.paired(base_events, no_rsi, gross, False))
    steps["c_net_rsi"] = summarise(survivors.paired(base_events, with_rsi, net, False))
    steps["c_net_no_rsi"] = summarise(survivors.paired(base_events, no_rsi, net, False))
    if use_earnings:
        vetoed = survivors.events(True)
        results["data"]["events_after_veto"] = len(vetoed)
        steps["d_earnings_net_rsi"] = summarise(survivors.paired(vetoed, earn(with_rsi), net, True))
        steps["d_earnings_net_no_rsi"] = summarise(survivors.paired(vetoed, earn(no_rsi), net, True))
    results["data"]["events"] = len(base_events)
    results["steps"] = steps
    print(f"steps done {time.time() - t0:.0f}s", file=sys.stderr)

    # -- steps (e), (f): the portfolio, shipped caps --------------------------
    caps = shipped_caps(config)
    windows = {"full": (FIRST_TRADE, None), "is_2021_2023": (FIRST_TRADE, OOS_START),
               "oos_2024_on": (OOS_START, None)}
    port = {}
    for name, (first, last) in windows.items():
        for label, exits in (("no_rsi", no_rsi), ("rsi", with_rsi)):
            port[f"{name}|{label}"] = portfolio(survivors, caps, earn(exits), net_fill, veto,
                                                first, last, args.controls)
            print(f"portfolio {name} {label} {time.time() - t0:.0f}s", file=sys.stderr)
    spy = cached("spy.pkl", lambda: fetch_bars(["SPY"]))["SPY"]["Close"]
    results["spy"] = {name: spy_hold(spy, days, first, last) for name, (first, last) in windows.items()}

    # -- registered secondaries ------------------------------------------------
    port["oos_2024_on|no_rsi|program_text_caps"] = portfolio(
        survivors, program_text_caps(config), earn(no_rsi), net_fill, veto, OOS_START, None, args.controls)
    port["oos_2024_on|no_rsi|all_fill"] = portfolio(
        survivors, caps, earn(no_rsi), net, veto, OOS_START, None, args.controls)
    port["full|no_rsi|all_fill"] = portfolio(
        survivors, caps, earn(no_rsi), net, veto, FIRST_TRADE, None, args.controls)
    print(f"secondaries {time.time() - t0:.0f}s", file=sys.stderr)

    # -- step (g): members by date ----------------------------------------------
    port["oos_2024_on|no_rsi|pit_membership"] = portfolio(
        pit, caps, earn(no_rsi), net_fill, veto, OOS_START, None, args.controls)
    port["full|no_rsi|pit_membership"] = portfolio(
        pit, caps, earn(no_rsi), net_fill, veto, FIRST_TRADE, None, args.controls)
    print(f"pit {time.time() - t0:.0f}s", file=sys.stderr)

    # The bound, on the gate statistic and on the full-period one.
    bounds = {}
    for key in ("oos_2024_on|no_rsi", "full|no_rsi", "oos_2024_on|no_rsi|pit_membership",
                "full|no_rsi|pit_membership"):
        taken = port[key]["taken"]
        if taken["paired"] is None:
            continue
        edge, control = taken["paired"]["mean"], taken["control_mean"]
        bounds[key] = {
            "edge": edge, "control_mean": control, "unobserved_share": unobserved_share,
            "bound_at_terminal_loss": bt.survivorship_bound(edge, control, unobserved_share, TERMINAL_LOSS),
            "breakeven_loss": bt.breakeven_loss(edge, control, unobserved_share),
            "breakeven_share": bt.breakeven_share(edge, control, TERMINAL_LOSS),
        }
    results["survivorship"] = bounds

    for value in port.values():
        value["capacity"] = bt.capacity(
            (value["taken"]["paired"] or {}).get("mean"), value["median_adv"])
        value.pop("taken_rows")
    results["portfolio"] = port

    gate = port["oos_2024_on|no_rsi"]["taken"]
    trials_before = sum(1 for r in es.ledger_rows() if r.get("phase") != "S1")
    planned = len(steps) + 2 * len(port)
    results["gate"] = {
        "n": gate["n"], "paired": gate["paired"],
        "passes": bool(gate["paired"] and gate["paired"]["lo"] > 0.0 and gate["n"] >= 300),
        "trials_on_ledger": trials_before + planned,
        "t_needed_bonferroni": es.bonferroni_t(trials_before + planned),
    }

    (CACHE / "s1_results.json").write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    if args.ledger:
        write_ledger(results)
    print(render(results))
    print(f"done {time.time() - t0:.0f}s", file=sys.stderr)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def pct(x, d=2):
    return "n/a" if x is None else f"{x:+.{d}%}"


def ci(stat):
    return "too few" if stat is None else f"{pct(stat['mean'])} [{pct(stat['lo'])}, {pct(stat['hi'])}]"


def write_ledger(results: dict) -> None:
    today = date.today().isoformat()
    kept = [r for r in es.ledger_rows() if not (r.get("phase") == "S1" and r.get("note") == "ops/honest_backtest.py")]
    es.LEDGER.unlink(missing_ok=True)
    for row in kept:
        es.ledger_append(row)
    for name, s in results["steps"].items():
        es.ledger_append({"date": today, "phase": "S1", "variant": f"per-trade all events: {name}",
                          "n": s["n"], "metric": "paired mean diff per trade vs same-sector control mean, date-clustered bootstrap",
                          "result": ci(s["paired"]), "note": "ops/honest_backtest.py"})
    for name, p in results["portfolio"].items():
        es.ledger_append({"date": today, "phase": "S1", "variant": f"portfolio taken trades: {name}",
                          "n": p["taken"]["n"], "metric": "paired net mean diff per taken trade vs control mean, date-clustered bootstrap",
                          "result": ci(p["taken"]["paired"]), "note": "ops/honest_backtest.py"})
        es.ledger_append({"date": today, "phase": "S1", "variant": f"portfolio total return: {name}",
                          "n": p["dip"]["trades"], "metric": "compounded net total return, DIP vs mean of random-control portfolios",
                          "result": f"DIP {pct(p['dip']['total_return'])} vs control {pct(p['control']['total_return'])}; beats {p['beats_share']:.0%}",
                          "note": "ops/honest_backtest.py"})


def render(r: dict) -> str:
    d, out = r["data"], []
    out.append(f"## Data\n\n{json.dumps(d, indent=1)}\n")
    out.append("## Per-trade paired edge after each step (all events, today's members)\n")
    out.append("| step | N | DIP mean | control mean | DIP - control [95% CI] |\n|---|---|---|---|---|")
    for name, s in r["steps"].items():
        out.append(f"| {name} | {s['n']} | {pct(s['dip_mean'])} | {pct(s['control_mean'])} | {ci(s['paired'])} |")
    out.append("\n## Portfolio replay\n")
    out.append("| run | DIP total | control total [5%, 95%] | beats | SPY | DIP Sharpe | ctl Sharpe | DIP maxDD | ctl maxDD | trades | missed | taken-trade edge [95% CI] | N |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for name, p in r["portfolio"].items():
        spy = (r["spy"].get(name.split("|")[0]) or {}).get("total_return")
        dm, cm = p["dip"], p["control"]

        def f(x, digits=2):
            return "n/a" if x is None else f"{x:.{digits}f}"

        out.append(
            f"| {name} | {pct(dm['total_return'], 1)} | {pct(cm['total_return'], 1)} "
            f"[{pct(p['control_total_p05'], 1)}, {pct(p['control_total_p95'], 1)}] | {p['beats_share']:.0%} | "
            f"{pct(spy, 1)} | {f(dm['sharpe'])} | {f(cm['sharpe'])} | {pct(dm['max_drawdown'], 1)} | "
            f"{pct(cm['max_drawdown'], 1)} | {dm['trades']} | {dm['missed']} | {ci(p['taken']['paired'])} | {p['taken']['n']} |")
    out.append("\n## Portfolio detail\n")
    out.append("| run | turnover/yr | hit | payoff | mean net | mean gross | MAE | MFE | sessions | exposure | capacity | reasons | missed mean |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for name, p in r["portfolio"].items():
        m = p["dip"]
        cap = "none" if p["capacity"] is None else f"${p['capacity']:,.0f}"
        out.append(
            f"| {name} | {m['turnover']:.1f}x | {m['hit_rate']:.0%} | "
            f"{'n/a' if m['payoff'] is None else format(m['payoff'], '.2f')} | {pct(m['mean_net'])} | "
            f"{pct(m['mean_gross'])} | {pct(m['mae'])} | {pct(m['mfe'])} | {m['held']:.1f} | "
            f"{m['exposure']:.0%} | {cap} | {m['reasons']} | {pct(p['missed_mean'])} |")
    out.append("\n## Survivorship\n")
    out.append("| run | edge | control mean | unobserved share | bound at -30% | break-even loss | break-even share |")
    out.append("|---|---|---|---|---|---|---|")
    for name, b in r["survivorship"].items():
        out.append(f"| {name} | {pct(b['edge'])} | {pct(b['control_mean'])} | {b['unobserved_share']:.2%} | "
                   f"{pct(b['bound_at_terminal_loss'])} | {pct(b['breakeven_loss'], 1)} | "
                   f"{'n/a' if b['breakeven_share'] is None else format(b['breakeven_share'], '.2%')} |")
    g = r["gate"]
    out.append(f"\n## Gate\n\nN={g['n']}, {ci(g['paired'])}. Passes: {g['passes']}. "
               f"Trials on ledger: {g['trials_on_ledger']}; |t| needed {g['t_needed_bonferroni']:.2f}.\n")
    out.append("## SPY buy and hold\n\n" + json.dumps(r["spy"], indent=1))
    return "\n".join(out)


if __name__ == "__main__":
    main()
