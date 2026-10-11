# /// script
# requires-python = ">=3.11"
# dependencies = ["yfinance", "pandas", "numpy", "lxml", "html5lib", "beautifulsoup4", "requests", "tzdata"]
# ///
"""S4: the beta baseline. Is the bigger lever time in the market?

Run:  uv run ops/beta_baseline.py            (the registered run; ~4 min warm)
      uv run ops/beta_baseline.py --smoke    (plumbing only: counts, no returns)

Pre-registered. This file implements the S4 beta-baseline study and decides
nothing. The policy arm is S1's portfolio, loaded through
`honest_backtest.load_inputs` and replayed by the same `backtest_engine.replay`;
the beta arms are `backtest_engine.hold`; the core-satellite variants are
`replay` with a `Sleeve`. What is here is the list of arms and the tables.

Writes `.placebo_cache/s4_results.json` and `.placebo_cache/s4_curves.csv`,
prints the report tables, and with `--ledger` appends one row per reported
comparison to the trial ledger (replacing this script's earlier S4 rows).

Nothing here can touch the live system: no import of `services/`, no order
vocabulary, no write outside the cache directory and the ledger.
"""

from __future__ import annotations

import argparse
import csv
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

CACHE = hb.CACHE
START_EQUITY = hb.START_EQUITY
FIRST_TRADE = hb.FIRST_TRADE
OOS_START = hb.OOS_START
LIVE_RECORD = ROOT / "data" / "s4-live-record.json"
NOTE = "ops/beta_baseline.py"

BROAD = "SPY"
BROAD_WHOLE = "SPYM"          # the same index at about a ninth of the price
WINDOWS = {"full": (FIRST_TRADE, None), "is_2021_2023": (FIRST_TRADE, OOS_START),
           "oos_2024_on": (OOS_START, None)}
RULE_WINDOWS = ("full", "oos_2024_on")
SIZES = ("100%", "50%", "matched")

# The S4 arm table, verbatim. `satellite` False is option (c).
VARIANTS = {
    "CS1": dict(symbol=BROAD, cap=0.50, buffer_slots=1.0, target=0.20, honour_reentry=True, whole_shares=False),
    "CS2": dict(symbol=BROAD_WHOLE, cap=0.50, buffer_slots=1.0, target=0.20, honour_reentry=True, whole_shares=True),
    "CS3": dict(symbol=BROAD, cap=1.00, buffer_slots=1.0, target=0.20, honour_reentry=True, whole_shares=False),
    "CS4": dict(symbol=BROAD, cap=0.50, buffer_slots=1.0, target=0.06, honour_reentry=True, whole_shares=False),
    "CS5": dict(symbol=BROAD, cap=0.50, buffer_slots=1.0, target=0.20, honour_reentry=False, whole_shares=False),
    "CS6": dict(symbol=BROAD, cap=0.50, buffer_slots=3.0, target=0.20, honour_reentry=True, whole_shares=False),
    "CS7": dict(symbol=BROAD, cap=0.50, buffer_slots=0.0, target=0.20, honour_reentry=True, whole_shares=False),
    "CS8": dict(symbol=BROAD_WHOLE, cap=1.00, buffer_slots=0.0, target=0.20, honour_reentry=True, whole_shares=True),
    # NOT in the pre-registration. Added after the registered run was read, to
    # separate what CS2's sleeve earns from what its satellite adds: CS2 with
    # the satellite switched off. Named as unplanned wherever it is printed.
    "CS9": dict(symbol=BROAD_WHOLE, cap=0.50, buffer_slots=1.0, target=0.20, honour_reentry=True, whole_shares=True),
}
NO_SATELLITE = {"CS8", "CS9"}
UNPLANNED = {"CS9"}
VARIANT_LABEL = {
    "CS1": "reference: cap 50%, 1-slot buffer, +20% target, SPY fractional",
    "CS2": "as deployable: CS1 in whole shares of SPYM",
    "CS3": "cap 100%",
    "CS4": "+6% target",
    "CS5": "sleeve exempt from the loss re-buy window",
    "CS6": "3-slot buffer",
    "CS7": "no buffer",
    "CS8": "option (c) as deployable: sleeve only, whole shares of SPYM",
    "CS9": "UNPLANNED: CS2 with no satellite",
}


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


def fetch_irx() -> pd.Series:
    """The 13-week Treasury bill yield, percent a year, daily."""
    import yfinance as yf

    frame = yf.download("^IRX", start=hb.START, auto_adjust=True, progress=False)
    close = frame["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    return close.dropna()


def daily_rate(irx: pd.Series, days: list[date]) -> list[float | None]:
    """The bill yield as a rate per session: percent / 100 / 252, the last
    known value carried over a day the yield did not print."""
    by_day = {ts.date(): float(v) for ts, v in irx.items()}
    out: list[float | None] = []
    last: float | None = None
    for day in days:
        if day in by_day:
            last = by_day[day]
        out.append(None if last is None else max(0.0, last) / 100.0 / 252.0)
    return out


def etf_panel(inputs, sector_etfs: list[str]) -> dict[str, bt.Series]:
    """SPY, SPYM and the sector SPDRs on the study's calendar."""
    prices = pickle.loads((CACHE / "prices.pkl").read_bytes())
    spy = hb.cached("spy.pkl", lambda: hb.fetch_bars([BROAD]))
    extra = hb.cached("s4_etf.pkl", lambda: hb.fetch_bars([BROAD_WHOLE]))
    frames = {BROAD: spy[BROAD], **extra, **{s: prices[s] for s in sector_etfs if s in prices}}
    return {s: hb.build_series(f, inputs.calendar, inputs.days, None) for s, f in frames.items()}


# ---------------------------------------------------------------------------
# Arms
# ---------------------------------------------------------------------------


def summary(result: bt.Replay) -> dict:
    """The S4 metrics of one replay, and its equity curve for the tables."""
    m = bt.metrics(result, START_EQUITY)
    return {
        "total_return": m["total_return"], "cagr": m["cagr"], "sharpe": m["sharpe"],
        "max_drawdown": m["max_drawdown"], "turnover": m["turnover"],
        "cash_idle": m["cash_idle"], "exposure": m["exposure"], "trades": m["trades"],
        "missed": m["missed"], "hit_rate": m["hit_rate"], "mean_net": m["mean_net"],
        "sleeve_share": m["sleeve_share"], "interest": m["interest"], "sleeve": m["sleeve"],
        "skipped": m["skipped"], "final": result.equity[-1] if result.equity else START_EQUITY,
    }


class Runner:
    """The policy, replayed under S4's options. One per (window, exit)."""

    def __init__(self, inputs, panel: dict[str, bt.Series], first: date, last: date | None,
                 exits: bt.Exits, costs: bt.Costs):
        self.i, self.panel = inputs, panel
        self.study = inputs.survivors
        self.lo, self.hi = hb.window(inputs.days, first, last)
        self.exits, self.costs = inputs.earn(exits), costs
        self.caps = hb.shipped_caps(inputs.config)
        by_day = hb.signals_by_day(self.study, inputs.veto, self.lo, self.hi)
        self.candidates = hb.dip_candidates(self.study, by_day)

    def run(self, *, satellite: bool = True, **options) -> bt.Replay:
        def simulate(symbol: str, t: int):
            return self.study.trade(symbol, t, self.exits)

        return bt.replay(self.i.days, self.candidates if satellite else {}, self.panel, self.caps,
                         self.exits, self.costs, start_equity=START_EQUITY, first_day=self.lo,
                         last_day=self.hi, simulate=simulate, **options)


def beta_arm(inputs, panel, weights: dict[str, float], fraction: float, first: date,
             last: date | None, *, rebalance: bool, cash_rate=None) -> bt.Replay:
    lo, hi = hb.window(inputs.days, first, last)
    return bt.hold(inputs.days, panel, weights, bt.Costs(), start_equity=START_EQUITY,
                   first_day=lo, last_day=hi, fraction=fraction, rebalance=rebalance,
                   cash_rate=cash_rate)


def size_fraction(size: str, matched: float, reserve: float) -> float:
    """What share of starting equity an arm of this size puts into its ETFs."""
    return {"100%": 1.0 - reserve, "50%": 0.50}.get(size, matched)


def beta_arms(inputs, panel, sector_etfs, first, last, matched: float, cash_rate=None) -> dict:
    """The six beta arms of one window: two baskets at three sizes."""
    reserve = float(inputs.config["cash_reserve_pct"])
    baskets = {"B_hold_spy": ({BROAD: 1.0}, False),
               "E_sector_basket": ({s: 1.0 for s in sector_etfs}, True)}
    out = {}
    for basket, (weights, rebalance) in baskets.items():
        for size in SIZES:
            fraction = size_fraction(size, matched, reserve)
            result = beta_arm(inputs, panel, weights, fraction, first, last,
                              rebalance=rebalance, cash_rate=cash_rate)
            out[f"{basket}|{size}"] = {**summary(result), "fraction": fraction,
                                      "_curve": result.equity}
    return out


# ---------------------------------------------------------------------------
# The live record
# ---------------------------------------------------------------------------


def live_record(record: dict, closes: dict[str, dict[date, float]], sector_etfs: list[str]) -> dict:
    rows, flows = record["benchmark"], record["flows"]
    days = [date.fromisoformat(r["trading_day"]) for r in rows]
    growth = bt.time_weighted(rows, flows)

    def path(symbol: str) -> list[float] | None:
        series = closes.get(symbol) or {}
        values = [series.get(day) for day in days]
        return None if any(v is None for v in values) else [v / values[0] for v in values]

    def stats(curve: list[float] | None) -> dict | None:
        if not curve:
            return None
        return {"total_return": curve[-1] - 1.0, "max_drawdown": bt.max_drawdown(curve)}

    spy = path(BROAD)
    members = [path(s) for s in sector_etfs]
    basket = None if any(m is None for m in members) else [
        sum(m[k] for m in members) / len(members) for k in range(len(days))]
    frozen_rows = [r for r in rows if r.get("frozen_value") is not None]
    frozen = [float(r["frozen_value"]) / float(frozen_rows[0]["frozen_value"]) for r in frozen_rows]
    quoted = [r for r in rows if r.get("spy_close") is not None]
    share = record.get("invested_share", {}).get("mean")
    scaled = None if spy is None or share is None else [1.0 + share * (v - 1.0) for v in spy]

    total_flows = sum(float(f["amount"]) for f in flows)
    recorded = sum(float(f["amount"]) for f in flows if f.get("recorded"))
    last_equity = float(rows[-1]["equity"])
    inception = float(record["inception"]["equity"])
    return {
        "first_day": rows[0]["trading_day"], "last_day": rows[-1]["trading_day"], "rows": len(rows),
        "account": stats(growth),
        "spy": stats(spy),
        "spy_from_benchmark_quotes": float(quoted[-1]["spy_close"]) / float(quoted[0]["spy_close"]) - 1.0,
        "sector_basket": stats(basket),
        "frozen_book": stats(frozen),
        "spy_at_account_exposure": stats(scaled), "account_invested_share": share,
        "control_arm_rows": 0,
        "dollars": {
            "equity": last_equity, "inception_equity": inception,
            "deposits_true": total_flows, "deposits_recorded": recorded,
            "deposits_unrecorded": total_flows - recorded,
            "gain_true": last_equity - inception - total_flows,
            "gain_as_scored": last_equity - inception - recorded,
        },
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def strip(value):
    """Drop the private `_curve` keys before the results are written."""
    if isinstance(value, dict):
        return {k: strip(v) for k, v in value.items() if not str(k).startswith("_")}
    return value


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="a small subset; prints counts only")
    ap.add_argument("--ledger", action="store_true", help="append the reported trials to the ledger")
    ap.add_argument("--controls", type=int, default=hb.CONTROL_PORTFOLIOS)
    args = ap.parse_args()
    t0 = time.time()

    inputs = hb.load_inputs(args.smoke)
    config, days = inputs.config, inputs.days
    etfs = json.loads((ROOT / "config" / "etfs.json").read_text(encoding="utf-8"))
    sector_etfs = sorted(etfs["sector"])
    etf = etf_panel(inputs, sector_etfs)
    missing = [s for s in [BROAD, BROAD_WHOLE, *sector_etfs] if s not in etf]
    if missing:
        raise SystemExit(f"no bars for {missing}; S4 cannot run its registered arms")
    panel = {**inputs.survivors.panel, **etf}
    rate = daily_rate(hb.cached("s4_irx.pkl", fetch_irx), days)

    if args.smoke:
        runner = Runner(inputs, panel, FIRST_TRADE, None, inputs.with_rsi, inputs.net_fill)
        plain = runner.run()
        sleeve = runner.run(sleeve=bt.Sleeve(**VARIANTS["CS2"]))
        whole = runner.run(whole_shares=True)
        held = beta_arm(inputs, etf, {s: 1.0 for s in sector_etfs}, 0.98, FIRST_TRADE, None, rebalance=True)
        print(json.dumps({
            "symbols": len(panel), "days": len(days), "etfs": len(etf),
            "rate_days": sum(1 for r in rate if r is not None),
            "policy_trades": len(plain.trades), "sleeve_trades": len(sleeve.trades),
            "sleeve_events": {k: v for k, v in sleeve.sleeve.items() if k != "sold"},
            "sleeve_skipped": sleeve.skipped, "whole_trades": len(whole.trades),
            "whole_skipped": whole.skipped, "basket_days": len(held.equity),
            "seconds": round(time.time() - t0),
        }, indent=1))
        return

    results: dict = {"generated": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                     "data": {"first_day": str(days[0]), "last_day": str(days[-1]),
                              "first_trade": str(FIRST_TRADE), "oos_start": str(OOS_START),
                              "sector_etfs": sector_etfs, "broad": BROAD, "broad_whole": BROAD_WHOLE,
                              "earnings_applied": inputs.use_earnings,
                              "fill_probability": inputs.net_fill.fill_probability}}
    exit_sets = {"rsi": inputs.with_rsi, "norsi": inputs.no_rsi}
    caps = hb.shipped_caps(config)
    curves: dict[str, list[float]] = {}

    # -- P and R: S1's portfolios, with the control beside them ----------------
    policy: dict = {}
    control: dict = {}
    for name, (first, last) in WINDOWS.items():
        policy[name], control[name] = {}, {}
        for label, exits in exit_sets.items():
            p = hb.portfolio(inputs.survivors, caps, inputs.earn(exits), inputs.net_fill, inputs.veto,
                             first, last, args.controls)
            gross = Runner(inputs, panel, first, last, exits,
                           bt.Costs(enabled=False, fill_probability=inputs.net_fill.fill_probability,
                                    seed=inputs.net_fill.seed)).run()
            net = Runner(inputs, panel, first, last, exits, inputs.net_fill).run()
            row = summary(net)
            # The same replay S1 printed, by two routes. They must agree.
            assert abs(row["total_return"] - p["dip"]["total_return"]) < 1e-12, (name, label)
            row["gross_total_return"] = gross.equity[-1] / START_EQUITY - 1.0
            row["_curve"] = net.equity
            policy[name][label] = row
            control[name][label] = {
                **{k: p["control"][k] for k in ("total_return", "cagr", "sharpe", "max_drawdown",
                                                "turnover", "cash_idle", "exposure", "trades")},
                "total_p05": p["control_total_p05"], "total_p95": p["control_total_p95"],
                "drawdown_p05": p["control_dd_p05"], "drawdown_p95": p["control_dd_p95"],
                "policy_beats_share": p["beats_share"],
            }
            print(f"policy {name} {label} {time.time() - t0:.0f}s", file=sys.stderr)
    results["policy"], results["control"] = policy, control

    # -- B and E: the six beta arms per window ----------------------------------
    beta = {name: beta_arms(inputs, etf, sector_etfs, first, last, policy[name]["rsi"]["exposure"])
            for name, (first, last) in WINDOWS.items()}
    results["beta"] = beta
    results["rule"] = bt.beta_rule(policy, beta, RULE_WINDOWS)

    # -- secondaries ------------------------------------------------------------
    whole = {}
    interest: dict = {}
    for name in RULE_WINDOWS:
        first, last = WINDOWS[name]
        whole[name] = summary(Runner(inputs, panel, first, last, inputs.with_rsi, inputs.net_fill)
                              .run(whole_shares=True))
        interest[name] = {"policy": {}, "beta": None}
        for label, exits in exit_sets.items():
            interest[name]["policy"][label] = summary(
                Runner(inputs, panel, first, last, exits, inputs.net_fill).run(cash_rate=rate))
        interest[name]["beta"] = beta_arms(inputs, etf, sector_etfs, first, last,
                                           policy[name]["rsi"]["exposure"], cash_rate=rate)
    results["whole_shares"] = whole
    results["interest"] = interest
    results["interest_rule"] = bt.beta_rule({n: interest[n]["policy"] for n in RULE_WINDOWS},
                                            {n: interest[n]["beta"] for n in RULE_WINDOWS}, RULE_WINDOWS)
    lo, hi = hb.window(days, FIRST_TRADE, None)
    known = [r for r in rate[lo : hi + 1] if r is not None]
    results["data"]["bill_yield_mean_annual"] = float(np.mean(known)) * 252.0 if known else None
    print(f"secondaries {time.time() - t0:.0f}s", file=sys.stderr)

    # -- the core-satellite variants --------------------------------------------
    cs: dict = {}
    for name in RULE_WINDOWS:
        first, last = WINDOWS[name]
        runner = Runner(inputs, panel, first, last, inputs.with_rsi, inputs.net_fill)
        for variant, spec in VARIANTS.items():
            result = runner.run(satellite=variant not in NO_SATELLITE, sleeve=bt.Sleeve(**spec))
            cs[f"{variant}|{name}"] = {**summary(result), "_curve": result.equity,
                                       "satellite_trades_alone": policy[name]["rsi"]["trades"]}
        result = Runner(inputs, panel, first, last, inputs.no_rsi, inputs.net_fill).run(
            sleeve=bt.Sleeve(**VARIANTS["CS1"]))
        cs[f"CS1_norsi|{name}"] = {**summary(result), "_curve": result.equity,
                                   "satellite_trades_alone": policy[name]["norsi"]["trades"]}
    results["core_satellite"] = cs
    print(f"core-satellite {time.time() - t0:.0f}s", file=sys.stderr)

    # -- the live record ----------------------------------------------------------
    if LIVE_RECORD.exists():
        record = json.loads(LIVE_RECORD.read_text(encoding="utf-8"))
        closes = {s: {d: c for d, c in zip(days, etf[s].c) if c is not None} for s in [BROAD, *sector_etfs]}
        results["live"] = live_record(record, closes, sector_etfs)
    else:
        results["live"] = None

    # -- curves and calendar years ------------------------------------------------
    window_days = days[lo : hi + 1]
    curves = {
        "P_rsi": policy["full"]["rsi"]["_curve"], "P_norsi": policy["full"]["norsi"]["_curve"],
        **{arm: row["_curve"] for arm, row in beta["full"].items()},
        **{v: cs[f"{v}|full"]["_curve"] for v in ("CS1", "CS2", "CS8", "CS9")},
    }
    results["years"] = {arm: bt.yearly(curve, window_days, START_EQUITY) for arm, curve in curves.items()}
    with (CACHE / "s4_curves.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["month_end", *curves])
        for k, day in enumerate(window_days):
            if k + 1 == len(window_days) or window_days[k + 1].month != day.month:
                writer.writerow([day.isoformat(), *(f"{curve[k]:.2f}" for curve in curves.values())])

    results = strip(results)
    results["trials_on_ledger_before"] = sum(1 for r in es.ledger_rows() if r.get("note") != NOTE)
    (CACHE / "s4_results.json").write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    if args.ledger:
        write_ledger(results)
    print(render(results))
    print(f"done {time.time() - t0:.0f}s", file=sys.stderr)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def pct(x, d=1):
    return "n/a" if x is None else f"{x:+.{d}%}"


def num(x, d=2):
    return "n/a" if x is None else f"{x:.{d}f}"


def share(x):
    return "n/a" if x is None else f"{x:.0%}"


def line(m: dict) -> str:
    return (f"{pct(m['total_return'])} | {pct(m['cagr'])} | {num(m['sharpe'])} | "
            f"{pct(m['max_drawdown'])} | {num(m['turnover'], 1)}x | {share(m['cash_idle'])}")


def ledger_rows(r: dict) -> list[dict]:
    today = date.today().isoformat()
    rows = []

    def add(variant, n, metric, result):
        rows.append({"date": today, "phase": "S4", "variant": variant, "n": n, "metric": metric,
                     "result": result, "note": NOTE})

    held = "net total return and max drawdown, buy and hold vs the policy (better of RSI / no RSI)"
    for window, arms in r["beta"].items():
        best = max(r["policy"][window]["rsi"]["total_return"], r["policy"][window]["norsi"]["total_return"])
        for arm, m in arms.items():
            add(f"beta arm: {arm} | {window}", r["policy"][window]["rsi"]["trades"], held,
                f"beta {pct(m['total_return'])} dd {pct(m['max_drawdown'])} vs policy {pct(best)}")
    for arm, v in r["rule"]["arms"].items():
        add(f"decision rule: {arm}", "", "S4 rule: return at or above and drawdown no deeper, full and 2024 on",
            "satisfies" if v["satisfies"] else "does not satisfy")
    for window, m in r["whole_shares"].items():
        add(f"policy, whole shares | {window}", m["trades"], "net total return, whole-share sizing vs fractional",
            f"whole {pct(m['total_return'])} vs fractional {pct(r['policy'][window]['rsi']['total_return'])}")
    for window, block in r["interest"].items():
        for label, m in block["policy"].items():
            add(f"cash earns the bill yield: policy {label} | {window}", m["trades"],
                "net total return with idle cash at the 13-week bill yield", pct(m["total_return"]))
        for arm, m in block["beta"].items():
            add(f"cash earns the bill yield: {arm} | {window}", "",
                "net total return with idle cash at the 13-week bill yield", pct(m["total_return"]))
    for key, m in r["core_satellite"].items():
        variant, window = key.split("|")
        base = r["policy"][window]["norsi" if variant.endswith("norsi") else "rsi"]["total_return"]
        add(f"core-satellite: {variant}{' (UNPLANNED)' if variant in UNPLANNED else ''} | {window}", m["trades"],
            "net total return and max drawdown, sleeve plus satellite vs the satellite alone",
            f"{pct(m['total_return'])} dd {pct(m['max_drawdown'])} vs alone {pct(base)}")
    live = r["live"]
    if live:
        add("live record: account vs SPY, same dates", live["rows"],
            "time-weighted return between benchmark rows, corrected deposits; descriptive only",
            f"account {pct(live['account']['total_return'], 2)} vs SPY {pct(live['spy']['total_return'], 2)}")
    return rows


def write_ledger(results: dict) -> None:
    kept = [r for r in es.ledger_rows() if r.get("note") != NOTE]
    es.LEDGER.unlink(missing_ok=True)
    for row in kept:
        es.ledger_append(row)
    for row in ledger_rows(results):
        es.ledger_append(row)


def render(r: dict) -> str:
    out = [f"## Data\n\n{json.dumps(r['data'], indent=1)}\n"]
    head = "| arm | total | CAGR | Sharpe | max DD | turnover | cash idle |\n|---|---|---|---|---|---|---|"
    for window in WINDOWS:
        out.append(f"\n## Arms, {window}\n\n{head}")
        for label, m in r["policy"][window].items():
            out.append(f"| P-{label} (gross {pct(m['gross_total_return'])}) | {line(m)} |")
        for label, m in r["control"][window].items():
            out.append(
                f"| R-{label}, mean of control portfolios | {pct(m['total_return'])} "
                f"[{pct(m['total_p05'])}, {pct(m['total_p95'])}] | {pct(m['cagr'])} | {num(m['sharpe'])} | "
                f"{pct(m['max_drawdown'])} [{pct(m['drawdown_p05'])}, {pct(m['drawdown_p95'])}] | "
                f"{num(m['turnover'], 1)}x | {share(m['cash_idle'])} |")
        for arm, m in r["beta"][window].items():
            out.append(f"| {arm} ({share(m['fraction'])} in) | {line(m)} |")

    rule = r["rule"]
    out.append("\n## The decision rule\n")
    out.append("| beta arm | window | beta total | policy total (better) | beta DD | policy DD (shallower) | return | drawdown |")
    out.append("|---|---|---|---|---|---|---|---|")
    for arm, v in rule["arms"].items():
        for window, leg in v["windows"].items():
            out.append(f"| {arm} | {window} | {pct(leg['beta_return'])} | {pct(leg['policy_return'])} | "
                       f"{pct(leg['beta_drawdown'])} | {pct(leg['policy_drawdown'])} | "
                       f"{'yes' if leg['return_ok'] else 'no'} | {'yes' if leg['drawdown_ok'] else 'no'} |")
    out.append(f"\nFires: {rule['fires']} (satisfied by {rule['satisfied_by'] or 'none'}). "
               f"Literal reading, 100% arms only: {rule['literal_fires']} "
               f"({rule['literal_satisfied_by'] or 'none'}).")

    out.append("\n## Secondary: whole shares (P-rsi)\n")
    out.append("| window | total | max DD | trades | cash idle | skipped |\n|---|---|---|---|---|---|")
    for window, m in r["whole_shares"].items():
        out.append(f"| {window} | {pct(m['total_return'])} | {pct(m['max_drawdown'])} | {m['trades']} | "
                   f"{share(m['cash_idle'])} | {m['skipped']} |")

    out.append(f"\n## Secondary: idle cash earns the 13-week bill yield "
               f"(mean {pct(r['data']['bill_yield_mean_annual'], 2)} a year)\n")
    out.append("| arm | window | total | max DD | interest |\n|---|---|---|---|---|")
    for window, block in r["interest"].items():
        for label, m in block["policy"].items():
            out.append(f"| P-{label} | {window} | {pct(m['total_return'])} | {pct(m['max_drawdown'])} | ${m['interest']:,.0f} |")
        for arm, m in block["beta"].items():
            out.append(f"| {arm} | {window} | {pct(m['total_return'])} | {pct(m['max_drawdown'])} | ${m['interest']:,.0f} |")
    ir = r["interest_rule"]
    out.append(f"\nRule under this assumption: fires {ir['fires']} ({ir['satisfied_by'] or 'none'}); "
               f"literal {ir['literal_fires']}.")

    out.append("\n## Core-satellite variants\n")
    out.extend(f"- **{v}**: {label}" for v, label in VARIANT_LABEL.items())
    out.append("")
    out.append("| variant | window | total | CAGR | Sharpe | max DD | turnover | cash idle | sleeve share | "
               "satellite trades (alone) | refused for cash | sleeve buys | funding sales | at a loss | "
               "target fills | days barred | 20% halt reached |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for key, m in r["core_satellite"].items():
        variant, window = key.split("|")
        s = m["sleeve"]
        out.append(
            f"| {variant}{' (unplanned)' if variant in UNPLANNED else ''} | {window} | {line(m)} | "
            f"{share(m['sleeve_share'])} | "
            f"{m['trades']} ({m['satellite_trades_alone']}) | {m['skipped'].get('no_settled_cash', 0)} | "
            f"{s['buys']} | {s['sells']} | {s['loss_sells']} | {s['target_fills']} | {s['blocked_days']} | "
            f"{'yes' if s['halt_reached'] else 'no'} |")

    out.append("\n## Calendar years (full window)\n")
    years = sorted({y for v in r["years"].values() for y in v})
    out.append("| arm | " + " | ".join(str(y) for y in years) + " |\n|---|" + "---|" * len(years))
    for arm, v in r["years"].items():
        out.append(f"| {arm} | " + " | ".join(pct(v.get(y) if y in v else v.get(str(y))) for y in years) + " |")

    live = r["live"]
    if live:
        out.append(f"\n## The live record, {live['first_day']} to {live['last_day']} ({live['rows']} rows)\n")
        out.append("| series | total | max DD |\n|---|---|---|")
        for label, key in (("account, time-weighted", "account"), ("SPY", "spy"),
                           ("equal-weight sector basket", "sector_basket"),
                           (f"SPY at the account's invested share ({share(live['account_invested_share'])})",
                            "spy_at_account_exposure"), ("frozen inception book", "frozen_book")):
            m = live[key]
            out.append(f"| {label} | {pct(m['total_return'], 2) if m else 'n/a'} | "
                       f"{pct(m['max_drawdown'], 2) if m else 'n/a'} |")
        out.append(f"\nSPY from the benchmark's own quotes: {pct(live['spy_from_benchmark_quotes'], 2)}. "
                   f"Live control-arm rows: {live['control_arm_rows']}.")
        out.append("\n" + json.dumps(live["dollars"], indent=1))
    return "\n".join(out)


if __name__ == "__main__":
    main()
