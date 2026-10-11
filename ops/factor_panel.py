# /// script
# requires-python = ">=3.11"
# dependencies = ["yfinance", "pandas", "numpy", "lxml", "html5lib", "beautifulsoup4", "requests", "tzdata"]
# ///
"""S2: the factor panel — does anything rank S&P 500 names within a sector?

Run:  uv run ops/factor_panel.py             (the registered run; ~15 min cold)
      uv run ops/factor_panel.py --smoke     (plumbing only: counts, no ICs)
      uv run ops/factor_panel.py --ledger    (and append the trials)

Pre-registered. This file implements the S2 factor-panel study and decides
nothing. Twelve factors, each with its sign fixed in advance,
scored as a sector-neutral z and judged by the rank IC against the 8-session
return from the next open, on index members by date.

The arithmetic that can be tested without numpy is elsewhere and in the
suite: the statistics and the z-score in `ops/eval_selection.py`, fundamentals
as known on a day in `ops/pit_fundamentals.py`, costs and membership in
`ops/backtest_engine.py`. What is here is data and loops.

Everything fetched is cached under `.placebo_cache/` (`s2_*.pkl`). Delete a
file to refetch it. The run writes `.placebo_cache/s2_results.json`.
"""

from __future__ import annotations

import argparse
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
import honest_backtest as hb  # noqa: E402
import pit_fundamentals as pit  # noqa: E402

CACHE = ROOT / ".placebo_cache"
START, END = "2019-01-02", "2026-10-03"
FIRST_TRADE = date(2021, 1, 4)
OOS_START = date(2024, 1, 1)
PRIMARY, SECONDARY = 8, 21
MIN_NAMES = 100
COVERAGE_BAR = 0.70
CHURN_BAR = 0.40
NOTIONAL = 200.0
BOOT = 10_000
SEED = 20261005
NOTE = "ops/factor_panel.py"
UA = {"User-Agent": "deterministic-trader research (ops/factor_panel.py)"}

PRICE_FACTORS = ("mom_12_1", "resid_mom_12_1", "rev_1m", "near_52w_high", "rs_3m_vs_sector",
                 "low_vol_60d")
QUALITY_FACTORS = ("gross_profitability", "roe_stability", "low_leverage", "earnings_stability")
CONDITIONAL = "rev_5d_high_vix"
FACTORS = (*PRICE_FACTORS, *QUALITY_FACTORS, CONDITIONAL)
FORWARD_ONLY = "eps_revision"


# ---------------------------------------------------------------------------
# Data, cached
# ---------------------------------------------------------------------------


def fetch_bars(symbols: list[str]) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    out: dict[str, pd.DataFrame] = {}
    tickers = [s.replace(".", "-") for s in symbols]
    for i in range(0, len(tickers), 50):
        chunk = tickers[i : i + 50]
        frame = yf.download(chunk, start=START, end=END, auto_adjust=True, progress=False,
                            group_by="ticker", threads=True)
        for t in chunk:
            try:
                x = frame[t].dropna(subset=["Close"])
            except KeyError:
                continue
            if len(x) > 260:
                out[t.replace("-", ".").replace("^", "")] = x
        print(f"  bars {min(i + 50, len(tickers))}/{len(tickers)}", file=sys.stderr)
    return out


def fetch_facts(ciks: dict[str, str]) -> dict[str, dict]:
    """`{symbol: pit.reduce(company facts)}`. A company EDGAR has nothing for
    is absent, which is what coverage is measured from."""
    import requests

    out: dict[str, dict] = {}
    session = requests.Session()
    session.headers.update(UA)
    for n, (symbol, cik) in enumerate(sorted(ciks.items())):
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json"
        for attempt in range(4):
            try:
                response = session.get(url, timeout=60)
                if response.status_code == 200:
                    out[symbol] = pit.reduce(response.json())
                    break
                if response.status_code == 404:
                    break
            except Exception:  # noqa: BLE001 — retried, then left absent
                pass
            time.sleep(2.0 * (attempt + 1))
        time.sleep(0.15)                      # EDGAR's fair-access limit is 10 a second
        if n % 50 == 0:
            print(f"  facts {n}/{len(ciks)}", file=sys.stderr)
    return out


# ---------------------------------------------------------------------------
# The panel
# ---------------------------------------------------------------------------


class Panel:
    """Opens, closes and dollar volume on one calendar, and every price
    factor "as of the close of row i". A decision on day ``t`` reads row
    ``t - 1``."""

    def __init__(self, bars: dict[str, pd.DataFrame], symbols: list[str], sectors: dict[str, str],
                 etf_of: dict[str, str]) -> None:
        calendar = bars["SPY"].index
        self.days = [ts.date() for ts in calendar]
        self.symbols = symbols
        self.sector = [sectors[s] for s in symbols]
        n, m = len(calendar), len(symbols)
        self.O, self.C, volume = (np.full((n, m), np.nan) for _ in range(3))
        for j, s in enumerate(symbols):
            frame = bars[s].reindex(calendar)
            self.O[:, j] = frame["Open"].to_numpy(float)
            self.C[:, j] = frame["Close"].to_numpy(float)
            volume[:, j] = frame["Volume"].to_numpy(float)
        C = self.C
        self.adv = pd.DataFrame(C * volume).rolling(20).mean().to_numpy()

        def close(symbol: str) -> np.ndarray:
            return bars[symbol]["Close"].reindex(calendar).to_numpy(float)

        def lag(a: np.ndarray, k: int) -> np.ndarray:
            out = np.full_like(a, np.nan)
            out[k:] = a[:-k]
            return out

        self.spy = close("SPY")
        self.etf = {etf: close(etf) for etf in sorted(set(etf_of.values()))}
        self.etf_col = [etf_of.get(sec) for sec in self.sector]
        etf_matrix = np.column_stack([
            self.etf[e] if e is not None else np.full(n, np.nan) for e in self.etf_col])

        with np.errstate(invalid="ignore", divide="ignore"):
            self.R = C / lag(C, 1) - 1.0
            self.R_spy = self.spy / lag(self.spy, 1) - 1.0
            self.R_etf = {e: c / lag(c, 1) - 1.0 for e, c in self.etf.items()}
            self.F: dict[str, np.ndarray] = {
                "mom_12_1": lag(C, 21) / lag(C, 252) - 1.0,
                "rev_1m": -(C / lag(C, 21) - 1.0),
                "near_52w_high": C / pd.DataFrame(C).rolling(252, min_periods=200).max().to_numpy(),
                "rs_3m_vs_sector": (C / lag(C, 63) - 1.0) - (etf_matrix / lag(etf_matrix, 63) - 1.0),
                "low_vol_60d": -pd.DataFrame(self.R).rolling(60, min_periods=60).std().to_numpy(),
                "rev_5d": -(C / lag(C, 5) - 1.0),
            }
        vix = close("VIX")
        # Above the median of its *prior* 252 closes: today's is not in it.
        prior = pd.Series(vix).shift(1).rolling(252, min_periods=252).median().to_numpy()
        with np.errstate(invalid="ignore"):
            self.high_vix = vix > prior
        self._resid: dict[int, np.ndarray] = {}

    def residual_momentum(self, i: int) -> np.ndarray:
        """Factor 2 as of row ``i``: the 12-1 window's daily returns regressed
        on SPY's and the sector SPDR's, the sum of what is left (intercept
        included) over its standard deviation times root n."""
        if i in self._resid:
            return self._resid[i]
        out = np.full(len(self.symbols), np.nan)
        lo, hi = i - 251, i - 21 + 1          # returns ending on closes i-251 .. i-21
        if lo >= 1:
            market = self.R_spy[lo:hi]
            for etf in self.etf:
                cols = [j for j, e in enumerate(self.etf_col) if e == etf]
                sector = self.R_etf[etf][lo:hi]
                base = np.isfinite(market) & np.isfinite(sector)
                for j in cols:
                    y = self.R[lo:hi, j]
                    ok = base & np.isfinite(y)
                    if ok.sum() < 200:
                        continue
                    X = np.column_stack([np.ones(ok.sum()), market[ok], sector[ok]])
                    beta, *_ = np.linalg.lstsq(X, y[ok], rcond=None)
                    eps = y[ok] - X @ beta
                    noise = eps.std(ddof=1)
                    if noise > 0.0:
                        out[j] = (eps + beta[0]).sum() / (noise * np.sqrt(ok.sum()))
        self._resid[i] = out
        return out

    def price_factor(self, name: str, i: int) -> np.ndarray:
        if name == "resid_mom_12_1":
            return self.residual_momentum(i)
        if name == CONDITIONAL:
            if not bool(self.high_vix[i]):
                return np.full(len(self.symbols), np.nan)
            return self.F["rev_5d"][i]
        return self.F[name][i]

    def forward(self, t: int, horizon: int) -> np.ndarray:
        """Close of session ``t - 1 + horizon`` over the open of ``t``."""
        end = t - 1 + horizon
        if end >= len(self.days):
            return np.full(len(self.symbols), np.nan)
        with np.errstate(invalid="ignore", divide="ignore"):
            return self.C[end] / self.O[t] - 1.0


def round_trip_cost(adv: float | None, costs: bt.Costs, multiple: float = 1.0) -> float:
    """What a $200 position bought at an open and sold 8 sessions later pays:
    S1's half-spread and slippage, both sides."""
    return multiple * (costs.entry(NOTIONAL, adv) + costs.exit(NOTIONAL, adv, bt.TIME_STOP))


# ---------------------------------------------------------------------------
# One factor, one horizon, one universe
# ---------------------------------------------------------------------------


def sample_days(panel: Panel, horizon: int) -> list[int]:
    first = next(i for i, d in enumerate(panel.days) if d >= FIRST_TRADE)
    return [t for t in range(first, len(panel.days), horizon) if t - 1 + horizon < len(panel.days)]


def values_on(panel: Panel, funds: dict[str, pit.Fundamentals], name: str, t: int) -> list:
    if name in QUALITY_FACTORS:
        day = panel.days[t]
        return [funds[s].asof(day)[name] if s in funds else None for s in panel.symbols]
    return [float(v) if np.isfinite(v) else None for v in panel.price_factor(name, t - 1)]


def score_day(panel: Panel, member, values: list, t: int, horizon: int, costs: bt.Costs) -> dict | None:
    """One day's cross-section for one column of values. None when the factor
    is not scored that day at all (the conditional factor on a calm day)."""
    fwd = panel.forward(t, horizon)
    rows = [j for j in range(len(panel.symbols)) if member(panel.symbols[j], t) and np.isfinite(fwd[j])]
    if not rows or all(values[j] is None for j in rows):
        return None
    sectors = [panel.sector[j] for j in rows]
    z = es.sector_z([values[j] for j in rows], sectors)
    scored = sum(1 for v in z if v is not None)
    out = {"day": str(panel.days[t]), "coverage": scored / len(rows), "ic": None, "z": {}}
    stat = es.cross_section(z, [float(fwd[j]) for j in rows], sectors, min_names=MIN_NAMES)
    if stat is None:
        return out
    top = [rows[k] for k in stat["top_rows"]]
    advs = [float(panel.adv[t - 1, j]) if np.isfinite(panel.adv[t - 1, j]) else None for j in top]
    out.update(
        ic=stat["ic"], spread=stat["spread"], top=stat["top"], n=stat["n"],
        cost=es.mean([round_trip_cost(a, costs) for a in advs]) if advs else None,
        top_adv=float(np.median([a for a in advs if a])) if any(advs) else None,
        z={panel.symbols[j]: v for j, v in zip(rows, z) if v is not None},
    )
    return out


def summarise(days: list[dict], first: date | None, last: date | None) -> dict:
    rows = [d for d in days if (first is None or d["day"] >= str(first)) and (last is None or d["day"] < str(last))]
    scored = [d for d in rows if d["ic"] is not None]
    ics = [d["ic"] for d in scored]
    tops = [d["top"] for d in scored if d.get("top") is not None]
    costs = [d["cost"] for d in scored if d.get("cost") is not None]
    summary = es.ic_summary(ics)
    gross = es.mean(tops) if tops else None
    cost = es.mean(costs) if costs else None
    advs = [d["top_adv"] for d in scored if d.get("top_adv")]
    net = None if gross is None or cost is None else gross - cost
    return {
        **summary,
        "active_days": len(rows),
        "coverage": es.mean([d["coverage"] for d in rows]) if rows else 0.0,
        "names": es.mean([float(d["n"]) for d in scored]) if scored else None,
        "spread": es.mean([d["spread"] for d in scored if d.get("spread") is not None]) if scored else None,
        "spread_t": es.t_stat([d["spread"] for d in scored if d.get("spread") is not None]),
        "top": gross,
        "top_t": es.t_stat(tops),
        "cost": cost,
        "net": net,
        "net_2x": None if net is None else gross - 2.0 * cost,
        "capacity": bt.capacity(net, float(np.median(advs))) if advs else None,
    }


def evaluate(days: list[dict], churn: float | None) -> dict:
    """The windows, the years and the five-part bar."""
    fit = summarise(days, FIRST_TRADE, OOS_START)
    test = summarise(days, OOS_START, None)
    full = summarise(days, None, None)
    years = es.sign_by_year([(d["day"], d["ic"]) for d in days if d["ic"] is not None])
    checks = {
        "fit_t": bool(fit["t"] is not None and fit["t"] >= es.T_BAR),          # the registered sign is +
        "test_sign": bool(test["mean"] is not None and test["mean"] > 0.0),
        "years": bool(fit["t"] is not None and fit["mean"] > 0.0 and es.passes_bar(
            {"t": max(abs(fit["t"]), es.T_BAR), "mean": fit["mean"]}, years)),
        "coverage": bool(fit["coverage"] >= COVERAGE_BAR and test["coverage"] >= COVERAGE_BAR),
        "net": bool(fit["net"] is not None and test["net"] is not None
                    and fit["net"] > 0.0 and test["net"] > 0.0),
    }
    return {"fit": fit, "test": test, "full": full, "by_year": years, "churn_21": churn,
            "checks": checks, "qualifies": all(checks.values())}


def run_factor(panel: Panel, funds, member, name: str, horizon: int, costs: bt.Costs) -> list[dict]:
    out = []
    for t in sample_days(panel, horizon):
        day = score_day(panel, member, values_on(panel, funds, name, t), t, horizon, costs)
        if day is not None:
            out.append(day)
    return out


def churn_of(days: list[dict]) -> float | None:
    return es.top_quantile_turnover({d["day"]: d["z"] for d in days if d["z"]})


# ---------------------------------------------------------------------------
# Conviction among the day's DIP signals (registered whatever happens)
# ---------------------------------------------------------------------------


def conviction_test(inputs) -> dict:
    """Does the incumbent ordering key rank the day's classic-DIP signals?
    S1's panel, S1's primary universe (today's members), S1's earnings veto."""
    study, days = inputs.survivors, inputs.days
    signals = study.signals(inputs.veto)
    by_day: dict[int, list[tuple[float, float]]] = {}
    for symbol, row in signals.items():
        s = study.panel[symbol]
        for t, conviction in enumerate(row):
            end = t - 1 + PRIMARY
            if conviction is None or end >= len(days) or s.o[t] is None or s.c[end] is None:
                continue
            by_day.setdefault(t, []).append((conviction, s.c[end] / s.o[t] - 1.0))

    out = {}
    for label, first, last in (("full", None, None), ("fit_2021_2023", None, OOS_START),
                               ("test_2024_on", OOS_START, None)):
        ics, halves, dates = [], [], []
        for t, rows in sorted(by_day.items()):
            if len(rows) < 2 or (first and days[t] < first) or (last and days[t] >= last):
                continue
            rows = sorted(rows)
            k = len(rows) // 2
            halves.append(es.mean([r for _, r in rows[-k:]]) - es.mean([r for _, r in rows[:k]]))
            dates.append(str(days[t]))
            ic = es.spearman([c for c, _ in rows], [r for _, r in rows])
            if ic is not None:
                ics.append(ic)
        out[label] = {
            "days": len(halves),
            "signals": sum(len(by_day[t]) for t in by_day if len(by_day[t]) >= 2
                           and not (first and days[t] < first) and not (last and days[t] >= last)),
            "ic": es.ic_summary(ics),
            "top_minus_bottom_half": es.clustered_bootstrap(halves, dates, boot=BOOT, seed=SEED),
        }
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def load(smoke: bool):
    inputs = hb.load_inputs(False)
    config = json.loads((ROOT / "config" / "etfs.json").read_text(encoding="utf-8"))
    etf_of = {sector: etf for etf, sector in config["sector"].items()}
    universe = json.loads((ROOT / "data" / "universe.json").read_text(encoding="utf-8"))
    ciks = {r["symbol"]: r["cik"] for r in universe["cross_check"] if r.get("cik")}

    wanted = sorted(s for s in inputs.panel if s in inputs.sectors_all)
    bars = hb.cached("s2_prices.pkl", lambda: fetch_bars([*wanted, "SPY", "^VIX", *sorted(etf_of.values())]))
    facts = hb.cached("s2_facts.pkl", lambda: fetch_facts(ciks))
    symbols = [s for s in wanted if s in bars and inputs.sectors_all[s] in etf_of]
    if smoke:
        symbols = symbols[::4]
    panel = Panel(bars, symbols, inputs.sectors_all, etf_of)
    funds = {s: pit.Fundamentals(facts[s]) for s in symbols if s in facts}
    return inputs, panel, funds, {"symbols_wanted": len(wanted), "symbols_with_bars": len(symbols),
                                  "symbols_with_facts": len(funds), "ciks": len(ciks),
                                  "first_bar": str(panel.days[0]), "last_bar": str(panel.days[-1])}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="a quarter of the names; prints counts only")
    ap.add_argument("--ledger", action="store_true", help="append the reported trials to the ledger")
    args = ap.parse_args()
    t0 = time.time()

    inputs, panel, funds, data = load(args.smoke)
    membership, sector_now = inputs.membership, inputs.sector_now
    by_date = lambda symbol, t: membership.is_member(symbol, panel.days[t])  # noqa: E731
    today = lambda symbol, t: symbol in sector_now  # noqa: E731
    costs = bt.Costs()
    print(f"panel {len(panel.symbols)} symbols, {len(panel.days)} days, {time.time() - t0:.0f}s", file=sys.stderr)

    if args.smoke:
        t = sample_days(panel, PRIMARY)[40]
        counts = {name: sum(v is not None for v in values_on(panel, funds, name, t)) for name in FACTORS}
        print(json.dumps({**data, "sampled_days_8": len(sample_days(panel, PRIMARY)),
                          "sampled_days_21": len(sample_days(panel, SECONDARY)),
                          "day": str(panel.days[t]), "valued_names": counts,
                          "high_vix_share": float(np.nanmean(panel.high_vix[252:])),
                          "seconds": round(time.time() - t0)}, indent=1))
        return

    results: dict = {"generated": datetime.utcnow().isoformat(timespec="seconds") + "Z", "data": data}
    results["data"].update(
        sampled_days_8=len(sample_days(panel, PRIMARY)), sampled_days_21=len(sample_days(panel, SECONDARY)),
        unobserved_member_day_share=inputs.unobserved_share,
        high_vix_share=float(np.nanmean(panel.high_vix[252:])),
    )

    scored: dict[str, dict[str, list[dict]]] = {}
    factors: dict[str, dict] = {}
    for name in FACTORS:
        runs = {
            "pit_8": run_factor(panel, funds, by_date, name, PRIMARY, costs),
            "pit_21": run_factor(panel, funds, by_date, name, SECONDARY, costs),
            "today_8": run_factor(panel, funds, today, name, PRIMARY, costs),
        }
        scored[name] = runs
        churn = churn_of(runs["pit_21"])
        factors[name] = {key: evaluate(days, churn) for key, days in runs.items()}
        print(f"{name} {time.time() - t0:.0f}s", file=sys.stderr)
    results["factors"] = factors
    results["factors"][FORWARD_ONLY] = {"status": "absent", "coverage": 0.0,
                                        "reason": "no point-in-time history; measured forward"}

    # Factors 8 and 10 are relatives by construction; say how close.
    pairs = []
    for a, b in zip(scored["roe_stability"]["pit_8"], scored["earnings_stability"]["pit_8"]):
        common = sorted(set(a["z"]) & set(b["z"]))
        rho = es.spearman([a["z"][s] for s in common], [b["z"][s] for s in common]) if len(common) >= 3 else None
        if rho is not None:
            pairs.append(rho)
    results["roe_vs_earnings_stability_rank_corr"] = es.mean(pairs) if pairs else None

    qualifying = [name for name in FACTORS if factors[name]["pit_8"]["qualifies"]]
    results["qualifying"] = qualifying
    results["conviction"] = conviction_test(inputs)
    if qualifying:
        # "Conditional on a composite existing". Written
        # only if a factor qualifies, and not before: a replay of a ranker
        # that does not exist would be an unregistered look.
        raise NotImplementedError(f"factors qualify ({qualifying}); the composite stage must be built")

    before = sum(1 for r in es.ledger_rows() if r.get("note") != NOTE)
    trials = before + len(FACTORS) * 3 + 2
    results["trials_on_ledger"] = trials
    results["t_needed_bonferroni"] = es.bonferroni_t(trials)
    (CACHE / "s2_results.json").write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    if args.ledger:
        write_ledger(results)
    print(render(results))
    print(f"done {time.time() - t0:.0f}s", file=sys.stderr)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def pct(x, d=2):
    return "n/a" if x is None else f"{x:+.{d}%}"


def num(x, d=2):
    return "n/a" if x is None else f"{x:+.{d}f}"


def share(x):
    return "n/a" if x is None else f"{x:.0%}"


RUNS = {"pit_8": "8 sessions, members by date (primary)", "pit_21": "21 sessions, members by date",
        "today_8": "8 sessions, today's members"}


def write_ledger(results: dict) -> None:
    today = date.today().isoformat()
    kept = [r for r in es.ledger_rows() if r.get("note") != NOTE]
    es.LEDGER.unlink(missing_ok=True)
    for row in kept:
        es.ledger_append(row)
    for name in FACTORS:
        for key, label in RUNS.items():
            f = results["factors"][name][key]
            es.ledger_append({
                "date": today, "phase": "S2", "variant": f"factor IC: {name} | {label}",
                "n": f["fit"]["n"] + f["test"]["n"],
                "metric": "sector-neutral rank IC, non-overlapping, next-open forward return; fit 2021-2023 / test 2024 on",
                "result": f"fit IC {num(f['fit']['mean'], 4)} t {num(f['fit']['t'], 1)}; test IC "
                          f"{num(f['test']['mean'], 4)} t {num(f['test']['t'], 1)}; qualifies {f['qualifies']}",
                "note": NOTE})
    c = results["conviction"]["full"]
    es.ledger_append({"date": today, "phase": "S2", "variant": "conviction among same-day DIP signals: rank IC",
                      "n": c["ic"]["n"], "metric": "mean per-day Spearman of conviction vs 8-session next-open return, days with 3+ signals",
                      "result": f"IC {num(c['ic']['mean'], 4)} t {num(c['ic']['t'], 1)}", "note": NOTE})
    half = c["top_minus_bottom_half"]
    es.ledger_append({"date": today, "phase": "S2", "variant": "conviction among same-day DIP signals: top half minus bottom half",
                      "n": c["days"], "metric": "mean per-day return difference, days with 2+ signals, date bootstrap",
                      "result": "too few" if half is None else f"{pct(half['mean'])} [{pct(half['lo'])}, {pct(half['hi'])}]",
                      "note": NOTE})


def render(r: dict) -> str:
    out = [f"## Data\n\n{json.dumps(r['data'], indent=1)}\n"]
    out.append(f"Trials on the ledger after this run: {r['trials_on_ledger']}; a single result needs "
               f"|t| >= {r['t_needed_bonferroni']:.2f} after Bonferroni. Entry bar: t >= {es.T_BAR:.1f} in the fit window, registered sign.\n")
    for key, label in RUNS.items():
        out.append(f"## Sector-neutral IC, {label}\n")
        out.append("| factor | fit IC | fit t | fit days | test IC | test t | test days | full t | coverage fit / test | Q5-Q1 full | by year |")
        out.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for name in FACTORS:
            f = r["factors"][name][key]
            years = " ".join(f"{y[2:]}:{v:+.3f}" for y, v in f["by_year"].items())
            out.append(
                f"| {name} | {num(f['fit']['mean'], 4)} | {num(f['fit']['t'], 1)} | {f['fit']['n']} | "
                f"{num(f['test']['mean'], 4)} | {num(f['test']['t'], 1)} | {f['test']['n']} | {num(f['full']['t'], 1)} | "
                f"{share(f['fit']['coverage'])} / {share(f['test']['coverage'])} | {pct(f['full']['spread'])} | {years} |")
        out.append("")
    out.append("## Long-only edge and the bar, 8 sessions, members by date\n")
    out.append("| factor | top quintile vs sector, fit | test | round-trip cost | net fit | net test | net at 2x cost, test | churn / month | fit t | test sign | years | coverage | net | qualifies |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for name in FACTORS:
        f = r["factors"][name]["pit_8"]
        c = f["checks"]
        mark = lambda ok: "pass" if ok else "fail"  # noqa: E731
        out.append(
            f"| {name} | {pct(f['fit']['top'])} | {pct(f['test']['top'])} | {pct(f['full']['cost'], 3)} | "
            f"{pct(f['fit']['net'])} | {pct(f['test']['net'])} | {pct(f['test']['net_2x'])} | {share(f['churn_21'])} | "
            f"{mark(c['fit_t'])} | {mark(c['test_sign'])} | {mark(c['years'])} | {mark(c['coverage'])} | {mark(c['net'])} | "
            f"{'YES' if f['qualifies'] else 'no'} |")
    out.append(f"\n| {FORWARD_ONLY} | absent: 0% point-in-time coverage; measured forward through the decision snapshot |\n")
    out.append(f"Qualifying factors: {r['qualifying'] or 'none'}.")
    out.append(f"Rank correlation of roe_stability and earnings_stability scores: {num(r['roe_vs_earnings_stability_rank_corr'])}.\n")
    out.append("## Conviction among the day's classic-DIP signals\n")
    out.append("| window | days (2+ signals) | signals | mean rank IC (days with 3+) | t | IC days | top half minus bottom half [95% CI] |")
    out.append("|---|---|---|---|---|---|---|")
    for label, c in r["conviction"].items():
        h = c["top_minus_bottom_half"]
        ci = "too few" if h is None else f"{pct(h['mean'])} [{pct(h['lo'])}, {pct(h['hi'])}]"
        out.append(f"| {label} | {c['days']} | {c['signals']} | {num(c['ic']['mean'], 4)} | {num(c['ic']['t'], 1)} | {c['ic']['n']} | {ci} |")
    return "\n".join(out)


if __name__ == "__main__":
    main()
