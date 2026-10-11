# /// script
# requires-python = ">=3.11"
# dependencies = ["yfinance", "pandas", "numpy", "lxml", "html5lib", "beautifulsoup4", "requests", "tzdata"]
# ///
"""S3: is there a regime in which classic DIP beats its random control?

Run:  uv run ops/regime_dip.py            (the registered run; about 6 min warm)
      uv run ops/regime_dip.py --smoke    (plumbing only: counts, no edges)

Pre-registered. This file implements the S3 regime/dip study and decides
nothing. Events, pairing, exits, costs and the replay are S1's
(`ops/honest_backtest.py`, `ops/backtest_engine.py`). The residual-momentum
and quality factors are S2's (`ops/factor_panel.py`). Two things are new:

* **The posture is the shipped function.** `src.regime.evaluate` is called on
  each historical day's SPY, RSP and sector-ETF bars with the shipped config,
  so a day's posture here is what the live code would have read.
* **The gates are the shipped functions.** Every replay arm that applies the
  P1 rule or a variant of it calls `regime.dip_table_gate` with a table, the
  same call `plays.entries` makes. The arms differ only in the table.

Writes `.placebo_cache/s3_results.json` and prints the report tables; `--ledger`
replaces this script's rows in the trial ledger.
"""

from __future__ import annotations

import argparse
import json
import math
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
import factor_panel as fp  # noqa: E402
import honest_backtest as hb  # noqa: E402
import placebo_backtest as pb  # noqa: E402
from src import regime as regimes  # noqa: E402

CACHE = hb.CACHE
FIRST_TRADE = hb.FIRST_TRADE
OOS_START = hb.OOS_START
START_EQUITY = hb.START_EQUITY
BOOT = 10_000
SEED = 20261005
NOTE = "ops/regime_dip.py"
LIVE_RECORD = ROOT / "data" / "s3-live-record.json"

MIN_GROUP = 50            # events on each side, test window, for a hypothesis to hold
ACTION_N = 300            # the kill criterion's sample
COVERAGE_BAR = 0.70
SECTOR_MIN = 5            # valued sector members for a sector median
CLOSES_KEPT = 260         # what the pre-market job hands `regime.evaluate`
RV20_HIGH = "posture_reduced_rv20"
STRESS_RSI = 25.0         # R4's one registered ceiling

WINDOWS = {"fit_2021_2023": (FIRST_TRADE, OOS_START), "test_2024_on": (OOS_START, None),
           "full": (FIRST_TRADE, None)}
REPLAY_WINDOWS = {"full": (FIRST_TRADE, None), "oos_2024_on": (OOS_START, None)}

# name -> (the group flag, the statement). Registered sign: positive.
HYPOTHESES = {
    "H1": ("spy_below_200", "edge higher with SPY below its 200-day"),
    "H2a": ("vix_high", "edge higher with VIX above its trailing median"),
    "H2b": ("rv20_high", "edge higher with SPY realised vol at or over 0.20"),
    "H3a": ("resid_mom_high", "edge higher in names with residual momentum above the sector median"),
    "H3b": ("quality_high", "edge higher in names with gross profitability above the sector median"),
    "H4": ("sector_blocked", "the P1 sector rule removes better trades than it keeps"),
    "H4_narrow": ("sector_below_200", "descriptive: sector ETF under its 200-day"),
}
DESCRIPTIVE = {"H4_narrow"}
FAMILIES = {"H1": ("H1",), "H2": ("H2a", "H2b"), "H3": ("H3a", "H3b"), "H4": ("H4",)}

ARMS = {
    "R0_flags_off": "nothing gates DIP (S1's portfolio)",
    "R1_live_today": "risk-off tape dark; peak and breakdown sectors dropped",
    "R2_p1_rule": "R1 plus the P1 rule as shipped",
    "R3_p1_no_sector_rule": "R2 without the sector-above-200-day test",
    "R4_stress_half_size": "R2, but NO_NEW_POSITIONS takes RSI <= 25 at half size",
    "R5_stress_only": "R1, and only with SPY below its 200-day or VIX high",
}
RSI_TOO = ("R0_flags_off", "R2_p1_rule")


# ---------------------------------------------------------------------------
# The tape, one day at a time, through the shipped function
# ---------------------------------------------------------------------------


def fetch_unadjusted(symbols: list[str]) -> dict[str, pd.DataFrame]:
    """Daily bars adjusted for splits and NOT for dividends: the prices a
    quote vendor shows, which is what the pre-market job computes the live
    regime from. S2's cache is dividend-adjusted, which is right for a return
    and wrong for "is the ETF above its 200-day": it lowers every past price
    by the dividends since, so the average sits lower and the ETF reads
    further above it than it did on the day."""
    import yfinance as yf

    frame = yf.download(symbols, start=fp.START, end=fp.END, auto_adjust=False, progress=False,
                        group_by="ticker", threads=True)
    return {s: frame[s].dropna(subset=["Close"])[["Open", "High", "Low", "Close", "Volume"]]
            for s in symbols}


def number(value) -> float | None:
    return float(value) if value is not None and np.isfinite(value) else None


class Tape:
    """SPY, RSP, VIX and the sector ETFs on S2's calendar, and the regime the
    shipped `regime.evaluate` reads from each day's close."""

    def __init__(self, bars: dict[str, pd.DataFrame], sector_etfs: list[str], config: dict) -> None:
        self.calendar = bars["SPY"].index
        self.row = {ts.date(): i for i, ts in enumerate(self.calendar)}
        self.config = config
        self.sector_etfs = sector_etfs
        self.ind: dict[str, dict[str, np.ndarray]] = {}
        for symbol in ("SPY", "RSP", *sector_etfs):
            frame = bars[symbol].reindex(self.calendar)
            ind = pb.indicators(frame)
            closes = pd.Series(ind["c"])
            self.ind[symbol] = {"c": ind["c"], "rsi": ind["rsi"], "sma200": ind["sma200"],
                                "sma50": closes.rolling(50).mean().to_numpy()}
        self.vix = bars["VIX"]["Close"].reindex(self.calendar).to_numpy(float)
        prior = pd.Series(self.vix).shift(1).rolling(252, min_periods=252).median().to_numpy()
        with np.errstate(invalid="ignore"):
            self.vix_high = np.where(np.isfinite(self.vix) & np.isfinite(prior), self.vix > prior, np.nan)
        self._regime: dict[int, dict | None] = {}

    def block(self, symbol: str, i: int) -> dict | None:
        ind = self.ind[symbol]
        last = number(ind["c"][i])
        if last is None:
            return None
        closes = [float(c) for c in ind["c"][max(0, i - CLOSES_KEPT + 1): i + 1] if np.isfinite(c)]
        return {"last": last, "sma50": number(ind["sma50"][i]), "sma200": number(ind["sma200"][i]),
                "rsi14": number(ind["rsi"][i]), "closes": closes}

    def inputs(self, i: int) -> dict:
        sectors = {s: b for s in self.sector_etfs if (b := self.block(s, i)) is not None}
        out = {"spy": self.block("SPY", i) or {}, "sectors": sectors}
        rsp = self.block("RSP", i)
        if rsp is not None:
            out["rsp"] = rsp
        return out

    def regime(self, i: int) -> dict | None:
        """`Regime.as_dict()` as of the close of row ``i``, with the three
        market-wide flags S3 splits on. None where SPY cannot be read."""
        if i not in self._regime:
            inputs = self.inputs(i)
            verdict = regimes.evaluate(inputs, self.config, macro_event_day=None)
            if verdict is None:
                self._regime[i] = None
            else:
                spy = inputs["spy"]
                rv20 = regimes.realized_vol(spy.get("closes") or [])
                out = verdict.as_dict()
                out["_spy_below_200"] = (None if spy.get("sma200") is None
                                         else spy["last"] < spy["sma200"])
                out["_rv20"] = rv20
                out["_rv20_high"] = None if rv20 is None else rv20 >= float(
                    self.config.get(RV20_HIGH, regimes.POSTURE_DEFAULTS[RV20_HIGH]))
                out["_vix"] = number(self.vix[i])
                out["_vix_high"] = None if not np.isfinite(self.vix_high[i]) else bool(self.vix_high[i])
                self._regime[i] = out
        return self._regime[i]


class Context:
    """Everything a gate or a split needs for (symbol, decision day)."""

    def __init__(self, inputs, panel2: fp.Panel, funds, tape: Tape, etf_of: dict[str, str]) -> None:
        self.i, self.panel2, self.funds, self.tape, self.etf_of = inputs, panel2, funds, tape, etf_of
        self.days = inputs.days
        self.col = {s: j for j, s in enumerate(panel2.symbols)}
        self.by_sector: dict[str, list[int]] = {}
        for j, sector in enumerate(panel2.sector):
            self.by_sector.setdefault(sector, []).append(j)
        self._quality: dict[int, dict[str, float | None]] = {}

    def prior_row(self, t: int) -> int | None:
        """The S2-calendar row of the close a decision on day ``t`` reads."""
        return self.tape.row.get(self.days[t - 1]) if t >= 1 else None

    def regime(self, t: int) -> dict | None:
        row = self.prior_row(t)
        return None if row is None else self.tape.regime(row)

    def found(self, study: hb.Study, symbol: str) -> tuple[str, str] | None:
        sector = study.sectors.get(symbol)
        etf = self.etf_of.get(sector)
        return None if etf is None else (sector, etf)

    def above_sector_median(self, study: hb.Study, symbol: str, t: int, values) -> bool | None:
        """Whether the name's value is above the median of its sector's index
        members that day. None with no value or too thin a sector."""
        j = self.col.get(symbol)
        if j is None:
            return None
        mine = values(j)
        if mine is None:
            return None
        peers = [v for k in self.by_sector[self.panel2.sector[j]]
                 if study.member(self.panel2.symbols[k], t) and self.panel2.symbols[k] in study.panel
                 and (v := values(k)) is not None]
        if len(peers) < SECTOR_MIN:
            return None
        return mine > float(np.median(peers))

    def resid_mom_high(self, study: hb.Study, symbol: str, t: int) -> bool | None:
        row = self.prior_row(t)
        if row is None:
            return None
        vector = self.panel2.residual_momentum(row)
        return self.above_sector_median(study, symbol, t, lambda k: number(vector[k]))

    def quality_high(self, study: hb.Study, symbol: str, t: int) -> bool | None:
        if t not in self._quality:
            day = self.days[t]
            self._quality[t] = {
                s: (self.funds[s].asof(day)["gross_profitability"] if s in self.funds else None)
                for s in self.panel2.symbols}
        table = self._quality[t]
        return self.above_sector_median(study, symbol, t, lambda k: table[self.panel2.symbols[k]])

    def annotate(self, study: hb.Study, rows: list[dict]) -> list[dict]:
        out = []
        for r in rows:
            regime = self.regime(r["t"])
            if regime is None:
                continue
            found = self.found(study, r["symbol"])
            state = (regime["sector_states"].get(found[1]) if found else None)
            rs = (regime["sector_rs"].get(found[1]) if found else None)
            out.append({
                **r,
                "posture": regime["posture"],
                "spy_below_200": regime["_spy_below_200"],
                "vix_high": regime["_vix_high"],
                "rv20_high": regime["_rv20_high"],
                "vix": regime["_vix"],
                "sector_state": state,
                "sector_blocked": None if state is None else state in regimes.DIP_BLOCKED_SECTOR_STATES,
                "sector_below_200": None if state is None else state in (regimes.OVERSOLD_OK, regimes.BREAKDOWN),
                "sector_rs_upper_half": None if rs is None else bool(rs.get("upper_half")),
                "resid_mom_high": self.resid_mom_high(study, r["symbol"], r["t"]),
                "quality_high": self.quality_high(study, r["symbol"], r["t"]),
            })
        return out


# ---------------------------------------------------------------------------
# The gates: shipped code, one table per arm
# ---------------------------------------------------------------------------

TABLE_ON = {"regime_v2_enabled": True, regimes.DIP_REGIME_TABLE_ENABLED: True}


def tables(shipped: dict) -> dict[str, dict]:
    no_sector = {p: {**row, regimes.REQUIRE_SECTOR_ABOVE_200D: False} for p, row in shipped.items()}
    stress = {**shipped, regimes.NO_NEW_POSITIONS: {
        regimes.DIP_ALLOWED: True, regimes.DIP_SIZE_MULTIPLIER: 0.5,
        regimes.REQUIRE_SECTOR_ABOVE_200D: False, regimes.REQUIRE_SECTOR_RS_UPPER_HALF: False,
        regimes.MAX_RSI: STRESS_RSI}}
    return {"R2_p1_rule": shipped, "R3_p1_no_sector_rule": no_sector, "R4_stress_half_size": stress}


def make_gate(arm: str, ctx: Context, study: hb.Study):
    """``(symbol, t) -> (multiplier or None, why)`` for one arm. None is "not
    entered". A day with no readable regime is not gated, as live."""
    config = ctx.i.config
    table = tables(config[regimes.DIP_REGIME_TABLE]).get(arm)
    table_config = None if table is None else {**config, **TABLE_ON, regimes.DIP_REGIME_TABLE: table}

    def gate(symbol: str, t: int) -> tuple[float | None, str]:
        if arm == "R0_flags_off":
            return 1.0, "taken"
        regime = ctx.regime(t)
        if regime is None:
            return 1.0, "taken (no regime read)"
        found = ctx.found(study, symbol)
        state = regime["sector_states"].get(found[1]) if found else None
        if not regime["allow_entries"]:
            return None, "risk-off tape"
        if state in regimes.BLOCKED_SECTOR_STATES:
            return None, f"sector {state}"
        if arm == "R1_live_today":
            return 1.0, "taken"
        if arm == "R5_stress_only":
            if regime["_spy_below_200"] or regime["_vix_high"]:
                return 1.0, "taken"
            return None, "not a stress day"
        if regimes.dip_requires_sector_trend(regime, table_config):
            reason = regimes.sector_trend_reason(regime, found)
            if reason is not None:
                return None, "sector below its 200-day"
        rsi = study.panel[symbol].rsi[t - 1]
        scale, refusal = regimes.dip_table_gate(regime, found, table_config, rsi=rsi)
        if refusal is not None:
            return None, f"{refusal[0]} in {regime['posture']}"
        return scale, "taken" if scale == 1.0 else f"taken at {scale:g}x"

    return gate


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def in_window(rows: list[dict], first: date | None, last: date | None) -> list[dict]:
    return [r for r in rows if (first is None or r["day"] >= first) and (last is None or r["day"] < last)]


def difference(rows: list[dict], flag: str) -> dict:
    """Mean paired outcome where ``flag`` is true minus where it is false."""
    usable = [r for r in rows if r[flag] is not None]
    stat = es.clustered_bootstrap_difference(
        [r["diff"] for r in usable], [bool(r[flag]) for r in usable],
        [str(r["day"]) for r in usable], boot=BOOT, seed=SEED)
    return {"events": len(rows), "coverage": len(usable) / len(rows) if rows else 0.0, "stat": stat}


def hypothesis(rows: list[dict], flag: str) -> dict:
    out = {name: difference(in_window(rows, *span), flag) for name, span in WINDOWS.items()}
    fit, test = out["fit_2021_2023"], out["test_2024_on"]
    covered = fit["coverage"] >= COVERAGE_BAR and test["coverage"] >= COVERAGE_BAR
    f, s = fit["stat"], test["stat"]
    out["absent"] = not covered
    out["holds"] = bool(
        covered and f is not None and s is not None and f["diff"] > 0.0 and s["diff"] > 0.0
        and s["lo"] > 0.0 and s["n_in"] >= MIN_GROUP and s["n_out"] >= MIN_GROUP)
    return out


def dose_response(rows: list[dict]) -> dict:
    """H2c: across event days, the day's mean paired outcome against VIX."""
    out = {}
    for name, span in WINDOWS.items():
        by_day: dict[date, list[float]] = {}
        vix: dict[date, float] = {}
        for r in in_window(rows, *span):
            if r["vix"] is None:
                continue
            by_day.setdefault(r["day"], []).append(r["diff"])
            vix[r["day"]] = r["vix"]
        days = sorted(by_day)
        rho = es.spearman([vix[d] for d in days], [es.mean(by_day[d]) for d in days]) if len(days) > 3 else None
        t = None
        if rho is not None and abs(rho) < 1.0:
            t = rho * math.sqrt((len(days) - 2) / (1.0 - rho * rho))
        out[name] = {"days": len(days), "rho": rho, "t": t}
    return out


def cells(rows: list[dict], key, labels) -> dict:
    """Paired edge per cell per window. ``key(row)`` names the row's cell."""
    out = {}
    for label in labels:
        mine = [r for r in rows if key(r) == label]
        out[label] = {name: strip(hb.summarise(mine, *span)) for name, span in WINDOWS.items()}
    return out


def strip(summary: dict) -> dict:
    return {k: summary[k] for k in ("n", "dip_mean", "control_mean", "paired")}


def implied_t(stat: dict | None) -> float | None:
    """A cell mean's t from its bootstrap interval (width over 3.92)."""
    if stat is None or stat["hi"] <= stat["lo"]:
        return None
    return stat["mean"] / ((stat["hi"] - stat["lo"]) / 3.92)


# ---------------------------------------------------------------------------
# The replay, one arm
# ---------------------------------------------------------------------------


def run_arm(ctx: Context, study: hb.Study, arm: str, caps: bt.Caps, exits: bt.Exits,
            costs: bt.Costs, per_trade: bt.Costs, veto: bool, first: date, last: date | None,
            controls: int, spy: bt.Series) -> dict:
    """`honest_backtest.portfolio` with a gate: the arm, its own random
    controls built from the same gated list at the same multipliers, the
    paired edge on the trades it took, and SPY at its invested share."""
    days = study.days
    lo, hi = hb.window(days, first, last)
    gate = make_gate(arm, ctx, study)
    by_day: dict[int, list[tuple[str, float, float]]] = {}
    signals = refused = 0
    reasons: dict[str, int] = {}
    for t, rows in hb.signals_by_day(study, veto, lo, hi).items():
        for symbol, conviction in rows:
            signals += 1
            scale, why = gate(symbol, t)
            if scale is None or scale <= 0.0:
                refused += 1
                reasons[why] = reasons.get(why, 0) + 1
                continue
            by_day.setdefault(t, []).append((symbol, conviction, scale))

    def simulate(symbol: str, t: int):
        return study.trade(symbol, t, exits)

    def run(candidates):
        return bt.replay(days, candidates, study.panel, caps, exits, costs,
                         start_equity=START_EQUITY, first_day=lo, last_day=hi, simulate=simulate)

    own = run({t: [bt.Candidate(s, s, study.sectors[s], c, m) for s, c, m in rows]
               for t, rows in by_day.items()})
    metrics = bt.metrics(own, START_EQUITY)

    control_metrics = []
    for m in range(controls):
        candidates = {}
        for t, rows in by_day.items():
            picks = []
            for symbol, conviction, scale in rows:
                peer = bt.pick(study.pool(symbol, t, veto), SEED, m, t, symbol)
                if peer is not None:
                    picks.append(bt.Candidate(peer, symbol, study.sectors[symbol], conviction, scale))
            candidates[t] = picks
        control_metrics.append(bt.metrics(run(candidates), START_EQUITY))
    totals = [m["total_return"] for m in control_metrics]

    rows = []
    for trade in own.trades:
        if trade.net is None:
            continue
        peers = []
        for peer in study.pool(trade.signal, trade.entry_day, veto):
            theirs = study.trade(peer, trade.entry_day, exits)
            if theirs is not None and theirs.ret is not None:
                peers.append(study.net(peer, theirs, per_trade, trade.cost))
        if len(peers) < hb.MIN_PEERS:
            continue
        control = sum(peers) / len(peers)
        rows.append({"day": days[trade.entry_day], "ret": trade.net, "control": control,
                     "diff": trade.net - control})

    def avg(key):
        values = [m[key] for m in control_metrics if m[key] is not None]
        return es.mean(values) if values else None

    advs = [study.panel[tr.symbol].adv[tr.entry_day] for tr in own.trades
            if study.panel[tr.symbol].adv and study.panel[tr.symbol].adv[tr.entry_day]]
    taken = hb.summarise(rows)
    edge = (taken["paired"] or {}).get("mean")
    exposure = metrics["exposure"]
    beta = None
    if exposure and exposure > 0.0:
        held = bt.metrics(bt.hold(days, {"SPY": spy}, {"SPY": 1.0}, bt.Costs(),
                                  start_equity=START_EQUITY, first_day=lo, last_day=hi,
                                  fraction=exposure), START_EQUITY)
        beta = {"fraction": exposure, "total_return": held["total_return"],
                "max_drawdown": held["max_drawdown"], "sharpe": held["sharpe"]}
    return {
        "arm": metrics,
        "control": {k: avg(k) for k in ("total_return", "sharpe", "max_drawdown", "turnover",
                                        "hit_rate", "payoff", "mean_net", "exposure", "trades")},
        "control_total_p05": float(np.percentile(totals, 5)) if totals else None,
        "control_total_p95": float(np.percentile(totals, 95)) if totals else None,
        "beats_share": (sum(1 for x in totals if metrics["total_return"] > x) / len(totals)) if totals else None,
        "signals": signals, "refused": refused, "refusal_reasons": reasons,
        "taken": strip(taken),
        "median_adv": float(np.median(advs)) if advs else None,
        "capacity": bt.capacity(edge, float(np.median(advs)) if advs else None),
        "spy_matched": beta,
    }


# ---------------------------------------------------------------------------
# The live record: a list, not a statistic
# ---------------------------------------------------------------------------


def live_cases(ctx: Context, study: hb.Study) -> dict:
    record = json.loads(LIVE_RECORD.read_text(encoding="utf-8"))
    index = {d: t for t, d in enumerate(ctx.days)}
    gates = {arm: make_gate(arm, ctx, study) for arm in ARMS}
    cases = []
    for close in record["closes"]:
        entry = max(close["entries"], key=lambda e: e["quantity"])
        day = date.fromisoformat(entry["day"])
        t = index.get(day)
        symbol = close["symbol"]
        row = {"symbol": symbol, "entry_day": entry["day"], "close_day": close["close_day"],
               "other_entry_days": sorted({e["day"] for e in close["entries"]} - {entry["day"]}),
               "realized_return": close["realized_gain"] / close["cost_basis"],
               "realized_gain": close["realized_gain"], "note": close.get("note", "")}
        regime = None if t is None else ctx.regime(t)
        if regime is None or symbol not in study.panel:
            cases.append({**row, "posture": None, "arms": {}})
            continue
        found = ctx.found(study, symbol)
        series = study.panel[symbol]
        cases.append({
            **row,
            "posture": regime["posture"], "posture_reason": regime["posture_reason"],
            "breadth_200": regime["breadth"].get("breadth_200"),
            "sector": found[0] if found else None, "sector_etf": found[1] if found else None,
            "sector_state": regime["sector_states"].get(found[1]) if found else None,
            "rsi_prior_close": series.rsi[t - 1],
            "backtest_signal": bt.dip_signal(series, t, earnings_veto=ctx.i.veto) is not None,
            "arms": {arm: gates[arm](symbol, t)[1] for arm in ARMS},
        })
    return {"cases": cases, "realized_by_play": record["realized_by_play"],
            "snapshot": record["snapshot"]}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="plumbing only: counts, no edges")
    ap.add_argument("--ledger", action="store_true", help="write the reported trials to the ledger")
    ap.add_argument("--controls", type=int, default=hb.CONTROL_PORTFOLIOS)
    ap.add_argument("--tape", choices=("adjusted", "unadjusted"), default="adjusted",
                    help="the ETF bars the posture is read from; see fetch_unadjusted")
    args = ap.parse_args()
    global NOTE
    suffix = "" if args.tape == "adjusted" else f"_{args.tape}"
    NOTE = "ops/regime_dip.py" if args.tape == "adjusted" else f"ops/regime_dip.py --tape {args.tape}"
    t0 = time.time()

    inputs, panel2, funds, meta = fp.load(False)
    etfs = json.loads((ROOT / "config" / "etfs.json").read_text(encoding="utf-8"))
    etf_of = {sector: etf for etf, sector in etfs["sector"].items()}
    bars = hb.cached("s2_prices.pkl", lambda: {})
    rsp = hb.cached("s3_rsp.pkl", lambda: fp.fetch_bars(["RSP"]))
    tape_bars = {**bars, **rsp}
    if args.tape == "unadjusted":
        tape_bars.update(hb.cached("s3_tape_unadjusted.pkl", lambda: fetch_unadjusted(
            ["SPY", "RSP", *sorted(etf_of.values())])))
    tape = Tape(tape_bars, sorted(etf_of.values()), inputs.config)
    ctx = Context(inputs, panel2, funds, tape, etf_of)
    survivors, pit, days = inputs.survivors, inputs.pit, inputs.days
    veto = inputs.veto
    no_rsi, with_rsi = inputs.earn(inputs.no_rsi), inputs.earn(inputs.with_rsi)
    spy_frame = hb.cached("spy.pkl", lambda: hb.fetch_bars(["SPY"]))["SPY"]
    spy = hb.build_series(spy_frame, inputs.calendar, days, None)
    print(f"loaded {time.time() - t0:.0f}s", file=sys.stderr)

    first = next(i for i, d in enumerate(days) if d >= FIRST_TRADE)
    posture_days: dict[str, dict[str, int]] = {name: {} for name in WINDOWS}
    unread = 0
    for t in range(first, len(days)):
        regime = ctx.regime(t)
        if regime is None:
            unread += 1
            continue
        for name, (lo, hi) in WINDOWS.items():
            if days[t] >= lo and (hi is None or days[t] < hi):
                tally = posture_days[name]
                tally[regime["posture"]] = tally.get(regime["posture"], 0) + 1

    events = survivors.events(veto)
    if args.smoke:
        rows = ctx.annotate(survivors, survivors.paired(events[:150], no_rsi, inputs.net, veto))
        arm = run_arm(ctx, survivors, "R4_stress_half_size", hb.shipped_caps(inputs.config), no_rsi,
                      inputs.net_fill, inputs.net, veto, OOS_START, None, 3, spy)
        print(json.dumps({
            "events": len(events), "annotated": len(rows), "posture_days": posture_days,
            "unread_days": unread,
            "flags": {k: sum(1 for r in rows if r[k]) for k in
                      ("spy_below_200", "vix_high", "rv20_high", "sector_blocked", "resid_mom_high",
                       "quality_high")},
            "missing": {k: sum(1 for r in rows if r[k] is None) for k in
                        ("vix_high", "sector_blocked", "resid_mom_high", "quality_high")},
            "arm_trades": arm["arm"]["trades"], "arm_refused": arm["refused"],
            "refusal_reasons": arm["refusal_reasons"], "seconds": round(time.time() - t0),
        }, indent=1, default=str))
        return

    results: dict = {"generated": datetime.utcnow().isoformat(timespec="seconds") + "Z"}
    results["data"] = {
        **meta, "tape": args.tape, "first_trade": str(FIRST_TRADE), "oos_start": str(OOS_START), "last_day": str(days[-1]),
        "earnings_applied": inputs.use_earnings, "events_today_members": len(events),
        "unread_regime_days": unread, "posture_days": posture_days,
        "unobserved_member_day_share": inputs.unobserved_share,
    }

    # -- the events, annotated ---------------------------------------------------
    sets = {
        "today|no_rsi": ctx.annotate(survivors, survivors.paired(events, no_rsi, inputs.net, veto)),
        "today|rsi": ctx.annotate(survivors, survivors.paired(events, with_rsi, inputs.net, veto)),
        "pit|no_rsi": ctx.annotate(pit, pit.paired(pit.events(veto), no_rsi, inputs.net, veto)),
    }
    print(f"events annotated {time.time() - t0:.0f}s", file=sys.stderr)
    primary = sets["today|no_rsi"]
    results["baseline"] = {name: {w: strip(hb.summarise(rows, *span)) for w, span in WINDOWS.items()}
                           for name, rows in sets.items()}

    # -- hypotheses ----------------------------------------------------------------
    results["hypotheses"] = {
        name: {label: hypothesis(rows, flag) for label, rows in sets.items()}
        for name, (flag, _) in HYPOTHESES.items()
    }
    results["h2c_dose_response"] = dose_response(primary)
    results["sector_rs_upper_half"] = hypothesis(primary, "sector_rs_upper_half")
    results["families"] = {
        family: any(results["hypotheses"][m]["today|no_rsi"]["holds"] for m in members)
        for family, members in FAMILIES.items()
    }

    # -- the posture table -----------------------------------------------------------
    results["posture_cells"] = cells(primary, lambda r: r["posture"], regimes.POSTURES)
    split_labels = [f"{p}|{side}" for p in regimes.POSTURES for side in ("sector_ok", "sector_blocked")]
    results["posture_sector_cells"] = cells(
        primary,
        lambda r: None if r["sector_blocked"] is None
        else f"{r['posture']}|{'sector_blocked' if r['sector_blocked'] else 'sector_ok'}",
        split_labels)
    print(f"tables {time.time() - t0:.0f}s", file=sys.stderr)

    # -- replays ---------------------------------------------------------------------
    caps = hb.shipped_caps(inputs.config)
    replays = {}
    for arm in ARMS:
        for window, (lo, hi) in REPLAY_WINDOWS.items():
            for label, exits in (("no_rsi", no_rsi), ("rsi", with_rsi)):
                if label == "rsi" and arm not in RSI_TOO:
                    continue
                replays[f"{arm}|{window}|{label}"] = run_arm(
                    ctx, survivors, arm, caps, exits, inputs.net_fill, inputs.net, veto, lo, hi,
                    args.controls, spy)
                print(f"replay {arm} {window} {label} {time.time() - t0:.0f}s", file=sys.stderr)
    results["replays"] = replays

    # R0 must be S1's portfolio.
    s1_path = CACHE / "s1_results.json"
    if s1_path.exists() and args.controls == hb.CONTROL_PORTFOLIOS:
        s1 = json.loads(s1_path.read_text(encoding="utf-8"))["portfolio"]
        checks = {}
        for window in REPLAY_WINDOWS:
            for label in ("no_rsi", "rsi"):
                theirs = s1[f"{window}|{label}"]
                mine = replays[f"R0_flags_off|{window}|{label}"]
                checks[f"{window}|{label}"] = {
                    "s1_total": theirs["dip"]["total_return"], "s3_total": mine["arm"]["total_return"],
                    "s1_control": theirs["control"]["total_return"],
                    "s3_control": mine["control"]["total_return"],
                    "same": abs(theirs["dip"]["total_return"] - mine["arm"]["total_return"]) < 1e-9
                    and abs(theirs["control"]["total_return"] - mine["control"]["total_return"]) < 1e-9,
                }
        results["reproduces_s1"] = checks

    # -- survivorship, on any positive paired edge ---------------------------------------
    bounds = {}
    for key, r in replays.items():
        stat = r["taken"]["paired"]
        if stat is None or stat["mean"] <= 0.0:
            continue
        edge, control = stat["mean"], r["taken"]["control_mean"]
        bounds[key] = {
            "edge": edge, "control_mean": control,
            "bound_at_terminal_loss": bt.survivorship_bound(edge, control, inputs.unobserved_share,
                                                            hb.TERMINAL_LOSS),
            "breakeven_loss": bt.breakeven_loss(edge, control, inputs.unobserved_share),
        }
    results["survivorship"] = bounds

    # -- the action bar --------------------------------------------------------------
    action = {}
    for posture in regimes.POSTURES:
        cell = results["posture_cells"][posture]["test_2024_on"]
        stat = cell["paired"]
        action[posture] = {
            "n": cell["n"], "paired": stat,
            "reaches_bar": bool(stat is not None and stat["lo"] > 0.0 and cell["n"] >= ACTION_N),
            "negative_at_bar": bool(stat is not None and stat["hi"] < 0.0 and cell["n"] >= ACTION_N),
        }
    results["action_bar"] = action

    results["live"] = live_cases(ctx, survivors) if LIVE_RECORD.exists() else None

    trials_before = sum(1 for r in es.ledger_rows() if r.get("note") != NOTE)
    planned = len(ledger_rows(results))
    results["trials"] = {"before": trials_before, "this_phase": planned,
                         "t_needed_bonferroni": es.bonferroni_t(trials_before + planned)}

    (CACHE / f"s3_results{suffix}.json").write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
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
    return "n/a" if x is None else f"{x:.{d}f}"


def ci(stat):
    return "too few" if stat is None else f"{pct(stat['mean'])} [{pct(stat['lo'])}, {pct(stat['hi'])}]"


def gap(stat):
    if stat is None:
        return "too few"
    return f"{pct(stat['diff'])} [{pct(stat['lo'])}, {pct(stat['hi'])}]"


# What does not read the tape and so reproduces exactly on the corrected run:
# counted once, under the registered run.
TAPE_FREE = ("H2a", "H3a", "H3b")


def ledger_rows(r: dict) -> list[dict]:
    today = date.today().isoformat()
    out = []
    rerun = r["data"].get("tape", "adjusted") != "adjusted"

    def add(variant, n, metric, result):
        if rerun:
            variant = f"{r['data']['tape']} tape: {variant}"
        out.append({"date": today, "phase": "S3", "variant": variant, "n": n, "metric": metric,
                    "result": result, "note": NOTE})

    metric = "mean paired outcome in the group minus the rest, date-clustered bootstrap; fit 2021-2023 / test 2024 on"
    for name, by_set in r["hypotheses"].items():
        if rerun and name in TAPE_FREE:
            continue
        for label, h in by_set.items():
            fit, test = h["fit_2021_2023"]["stat"], h["test_2024_on"]["stat"]
            n = 0 if test is None else test["n_in"] + test["n_out"]
            add(f"{name} {HYPOTHESES[name][0]} | {label}", n, metric,
                f"fit {gap(fit)}; test {gap(test)}; holds {h['holds']}" + ("; absent (coverage)" if h["absent"] else ""))
    h = r["sector_rs_upper_half"]
    test = h["test_2024_on"]["stat"]
    add("descriptive sector_rs_upper_half | today|no_rsi", 0 if test is None else test["n_in"] + test["n_out"],
        metric, f"fit {gap(h['fit_2021_2023']['stat'])}; test {gap(test)}")
    d = r["h2c_dose_response"]
    if not rerun:
        add("H2c dose-response: day mean paired outcome vs VIX", d["test_2024_on"]["days"],
            "Spearman across event days; fit / test",
            f"fit rho {num(d['fit_2021_2023']['rho'], 3)} t {num(d['fit_2021_2023']['t'], 1)}; "
            f"test rho {num(d['test_2024_on']['rho'], 3)} t {num(d['test_2024_on']['t'], 1)}")
    for group in ("posture_cells", "posture_sector_cells"):
        for label, by_window in r[group].items():
            add(f"posture cell: {label}", by_window["test_2024_on"]["n"],
                "paired net mean diff per event vs control mean, date-clustered bootstrap; fit / test",
                f"fit {ci(by_window['fit_2021_2023']['paired'])}; test {ci(by_window['test_2024_on']['paired'])}")
    for key, p in r["replays"].items():
        if rerun and key.startswith("R0_"):
            continue
        add(f"replay taken trades: {key}", p["taken"]["n"],
            "paired net mean diff per taken trade vs control mean, date-clustered bootstrap",
            ci(p["taken"]["paired"]))
        add(f"replay total return: {key}", p["arm"]["trades"],
            "compounded net total return, arm vs mean of its random-control portfolios",
            f"arm {pct(p['arm']['total_return'])} vs control {pct(p['control']['total_return'])}; "
            f"beats {p['beats_share']:.0%}")
    return out


def write_ledger(results: dict) -> None:
    kept = [row for row in es.ledger_rows() if row.get("note") != NOTE]
    es.LEDGER.unlink(missing_ok=True)
    for row in kept:
        es.ledger_append(row)
    for row in ledger_rows(results):
        es.ledger_append(row)


def render(r: dict) -> str:
    out = []
    data = dict(r["data"])
    posture_days = data.pop("posture_days")
    out.append(f"## Data\n\n```json\n{json.dumps(data, indent=1)}\n```\n")
    out.append("## Days in each posture (shipped `regime.evaluate`, macro leg not available offline)\n")
    out.append("| posture | 2021 to 2023 | 2024 on | full |\n|---|---|---|---|")
    for posture in regimes.POSTURES:
        out.append(f"| {posture} | {posture_days['fit_2021_2023'].get(posture, 0)} | "
                   f"{posture_days['test_2024_on'].get(posture, 0)} | {posture_days['full'].get(posture, 0)} |")

    out.append("\n## Unconditional paired edge (S1's events, for reference)\n")
    out.append("| set | 2021 to 2023 | N | 2024 on | N | full | N |\n|---|---|---|---|---|---|---|")
    for name, by_window in r["baseline"].items():
        f, t, a = (by_window[w] for w in ("fit_2021_2023", "test_2024_on", "full"))
        out.append(f"| {name} | {ci(f['paired'])} | {f['n']} | {ci(t['paired'])} | {t['n']} | {ci(a['paired'])} | {a['n']} |")

    need = r["trials"]["t_needed_bonferroni"]
    out.append(f"\n## Hypotheses: group minus the rest (|t| needed after deflation: {need:.2f})\n")
    for label in ("today|no_rsi", "today|rsi", "pit|no_rsi"):
        out.append(f"\n### {label}\n")
        out.append("| | group | fit: in / out | fit difference [95% CI] | test: in / out | test: mean in | test: mean out | test difference [95% CI] | t | coverage | holds |")
        out.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for name, (flag, _) in HYPOTHESES.items():
            h = r["hypotheses"][name][label]
            f, t = h["fit_2021_2023"]["stat"], h["test_2024_on"]["stat"]

            def counts(s):
                return "n/a" if s is None else f"{s['n_in']} / {s['n_out']}"

            verdict = "absent" if h["absent"] else ("descriptive" if name in DESCRIPTIVE else str(h["holds"]))
            out.append(
                f"| {name} | {flag} | {counts(f)} | {gap(f)} | {counts(t)} | "
                f"{pct(t['mean_in']) if t else 'n/a'} | {pct(t['mean_out']) if t else 'n/a'} | {gap(t)} | "
                f"{num(t['t'], 1) if t else 'n/a'} | {h['test_2024_on']['coverage']:.0%} | {verdict} |")
    out.append("\nFamilies (primary version): " + ", ".join(f"{k} holds: {v}" for k, v in r["families"].items()))
    d = r["h2c_dose_response"]
    out.append("\nH2c, day mean paired outcome against VIX (Spearman): " + "; ".join(
        f"{w}: rho {num(d[w]['rho'], 3)}, t {num(d[w]['t'], 1)}, {d[w]['days']} days" for w in WINDOWS))
    h = r["sector_rs_upper_half"]
    out.append("\nDescriptive, sector in the upper half on relative strength minus the rest: "
               f"fit {gap(h['fit_2021_2023']['stat'])}; test {gap(h['test_2024_on']['stat'])}")

    for title, group in (("By posture", "posture_cells"), ("By posture and the P1 sector rule", "posture_sector_cells")):
        out.append(f"\n## {title}: DIP minus control, net, RSI exit off\n")
        out.append("| cell | fit N | fit edge [95% CI] | test N | test DIP | test control | test edge [95% CI] | t | full N | full edge [95% CI] |")
        out.append("|---|---|---|---|---|---|---|---|---|---|")
        for label, by_window in r[group].items():
            f, t, a = (by_window[w] for w in ("fit_2021_2023", "test_2024_on", "full"))
            out.append(f"| {label} | {f['n']} | {ci(f['paired'])} | {t['n']} | {pct(t['dip_mean'])} | "
                       f"{pct(t['control_mean'])} | {ci(t['paired'])} | {num(implied_t(t['paired']), 1)} | "
                       f"{a['n']} | {ci(a['paired'])} |")

    out.append(f"\n## The action bar (test window, lower bound above zero and N >= {ACTION_N})\n")
    out.append("| posture | N | edge [95% CI] | reaches the bar | negative at the bar |\n|---|---|---|---|---|")
    for posture, a in r["action_bar"].items():
        out.append(f"| {posture} | {a['n']} | {ci(a['paired'])} | {a['reaches_bar']} | {a['negative_at_bar']} |")

    out.append("\n## Replays\n")
    out.append("| arm, window, exit | total | control total [5%, 95%] | beats | SPY at the arm's exposure | Sharpe | ctl Sharpe | maxDD | ctl maxDD | SPY maxDD | signals | refused | trades | missed | paired edge on taken [95% CI] | N |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for key, p in r["replays"].items():
        a, c, b = p["arm"], p["control"], p["spy_matched"] or {}
        out.append(
            f"| {key} | {pct(a['total_return'], 1)} | {pct(c['total_return'], 1)} "
            f"[{pct(p['control_total_p05'], 1)}, {pct(p['control_total_p95'], 1)}] | {p['beats_share']:.0%} | "
            f"{pct(b.get('total_return'), 1)} at {pct(b.get('fraction'), 0).lstrip('+')} | {num(a['sharpe'])} | "
            f"{num(c['sharpe'])} | {pct(a['max_drawdown'], 1)} | {pct(c['max_drawdown'], 1)} | "
            f"{pct(b.get('max_drawdown'), 1)} | {p['signals']} | {p['refused']} | {a['trades']} | "
            f"{a['missed']} | {ci(p['taken']['paired'])} | {p['taken']['n']} |")
    out.append("\n## Replay detail\n")
    out.append("| arm, window, exit | turnover/yr | hit | payoff | mean net | mean gross | MAE | MFE | sessions | exposure | capacity | exit reasons | why refused |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for key, p in r["replays"].items():
        m = p["arm"]
        cap = "none" if p["capacity"] is None else f"${p['capacity']:,.0f}"
        out.append(
            f"| {key} | {num(m['turnover'], 1)}x | {pct(m['hit_rate'], 0).lstrip('+')} | {num(m['payoff'])} | "
            f"{pct(m['mean_net'])} | {pct(m['mean_gross'])} | {pct(m['mae'])} | {pct(m['mfe'])} | "
            f"{num(m['held'], 1)} | {pct(m['exposure'], 0).lstrip('+')} | {cap} | {m['reasons']} | "
            f"{p['refusal_reasons']} |")

    if "reproduces_s1" in r:
        out.append("\n## R0 against S1's stored result\n")
        out.append("| window, exit | S1 DIP | S3 R0 | S1 control | S3 control | same |\n|---|---|---|---|---|---|")
        for key, c in r["reproduces_s1"].items():
            out.append(f"| {key} | {pct(c['s1_total'], 3)} | {pct(c['s3_total'], 3)} | "
                       f"{pct(c['s1_control'], 3)} | {pct(c['s3_control'], 3)} | {c['same']} |")

    out.append("\n## Survivorship, positive paired edges only\n")
    if r["survivorship"]:
        out.append("| replay | edge | control mean | bound if the unobserved share each lost 30% | break-even loss |\n|---|---|---|---|---|")
        for key, b in r["survivorship"].items():
            out.append(f"| {key} | {pct(b['edge'])} | {pct(b['control_mean'])} | "
                       f"{pct(b['bound_at_terminal_loss'])} | {pct(b['breakeven_loss'], 1)} |")
    else:
        out.append("No replay has a positive paired edge.")

    if r["live"]:
        out.append("\n## Live DIP closes through each arm (a list of cases)\n")
        out.append("| symbol | entered | closed | realised | posture that day | sector, state | RSI | backtest signal | " +
                   " | ".join(a.split("_")[0] for a in ARMS) + " |")
        out.append("|---|---|---|---|---|---|---|---|" + "---|" * len(ARMS))
        for c in r["live"]["cases"]:
            if c["posture"] is None:
                out.append(f"| {c['symbol']} | {c['entry_day']} | {c['close_day']} | {pct(c['realized_return'])} | not readable |||||||||")
                continue
            out.append(
                f"| {c['symbol']} | {c['entry_day']} | {c['close_day']} | {pct(c['realized_return'])} | "
                f"{c['posture']} (breadth {pct(c['breadth_200'], 0).lstrip('+')}) | {c['sector_etf']}, {c['sector_state']} | "
                f"{num(c['rsi_prior_close'], 0)} | {c['backtest_signal']} | " +
                " | ".join(c["arms"][a] for a in ARMS) + " |")
        snap = r["live"]["snapshot"]
        out.append(f"\nS0 snapshot: {snap['rows']} rows over {snap['trading_days']} trading day(s) "
                   f"({snap['first_day']} to {snap['last_day']}). {snap['note']}")
    t = r["trials"]
    out.append(f"\n## Trials\n\n{t['before']} on the ledger before this phase, {t['this_phase']} added; "
               f"a single result needs |t| >= {t['t_needed_bonferroni']:.2f} at 5% family-wise.")
    return "\n".join(out)


if __name__ == "__main__":
    main()
