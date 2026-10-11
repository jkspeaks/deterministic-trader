# /// script
# requires-python = ">=3.11"
# dependencies = ["yfinance", "pandas", "numpy"]
# ///
"""Offline same-sector random-entry placebo backtest for the classic DIP rule.

Run:  uv run ops/placebo_backtest.py [--cache DIR] [--out report.md]

Why it exists: this backtest is the precondition for turning any behaviour
flag on. Nothing under `src/` imports
it; yfinance is a script-only dependency (PEP 723), not a repo one.

Question answered: does buying an oversold name (the DIP trigger) beat buying a
*random other name in the same sector on the same day*, with identical exits?
That strips out market/sector beta and the exit rules, leaving only the
selection edge.

Signal (src/plays/dip.py `_classic`): RSI(14) <= 30, close >= 0.97 x SMA200,
close < SMA20. Exits (src/plays/core.py, exits.py): resting limit target +6%
(fills on the day's high), ATR(14) x 3.0 stop checked at the close, RSI >= 50
signal exit, 8-session time stop. Entry at the signal day's close; exits cannot
fire on the entry day (same-day-fill guard).

Placebos, per signal event (symbol s, day t, sector S), K random draws from
peers in S on day t that are NOT themselves DIP-signalling:
  P1  any peer.
  P2  peers that pass the same trend filter (>= 0.97 x SMA200) but are not
      oversold (RSI > 30) -- isolates "oversold" from "in an uptrend".

Known limits, all stated in the report: survivorship (today's S&P 500 list),
no earnings veto, no 4-day loss-sell news hold, no costs/slippage, one-position-
per-symbol dedupe only (no portfolio caps).
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eval_selection as es  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

RSI_TRIGGER, RSI_EXIT = 30.0, 50.0
TREND_BAND, TARGET, STOP_MULT, HOLD = 0.03, 0.06, 3.0, 8
SECTOR_RSI_PEAK = 70.0
K_PLACEBO = 200
BOOT = 10_000
SEED = 20261004
START, END = "2020-06-01", None


def wilder(x: np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder smoothing seeded with the first `period` values, as src/indicators.py."""
    out = np.full(len(x), np.nan)
    if len(x) <= period:
        return out
    v = x[1 : period + 1].mean()
    out[period] = v
    for i in range(period + 1, len(x)):
        v = (v * (period - 1) + x[i]) / period
        out[i] = v
    return out


def indicators(df: pd.DataFrame) -> dict[str, np.ndarray]:
    c, h, l = (df[k].to_numpy(float) for k in ("Close", "High", "Low"))
    d = np.diff(c, prepend=np.nan)
    g, ls = np.where(d > 0, d, 0.0), np.where(d < 0, -d, 0.0)
    ag, al = wilder(np.nan_to_num(g)), wilder(np.nan_to_num(ls))
    with np.errstate(divide="ignore", invalid="ignore"):
        rsi = np.where(al == 0, np.where(ag == 0, 50.0, 100.0), 100 - 100 / (1 + ag / al))
    rsi[np.isnan(ag)] = np.nan
    pc = np.roll(c, 1)
    tr = np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)])
    tr[0] = np.nan
    atr = wilder(np.nan_to_num(tr))
    s = pd.Series(c)
    return {
        "c": c, "h": h, "o": df["Open"].to_numpy(float), "rsi": rsi, "atr": atr,
        "sma20": s.rolling(20).mean().to_numpy(), "sma200": s.rolling(200).mean().to_numpy(),
    }


def trade_returns(ind: dict[str, np.ndarray]) -> np.ndarray:
    """Return of a trade entered at each bar's close under the live exit stack."""
    c, h, o, rsi, atr = ind["c"], ind["h"], ind["o"], ind["rsi"], ind["atr"]
    n = len(c)
    ret = np.full(n, np.nan)
    valid = np.zeros(n, bool)
    valid[: n - HOLD] = True
    valid &= np.isfinite(atr) & np.isfinite(rsi)
    alive = valid.copy()
    idx = np.arange(n)
    tgt, stop = c * (1 + TARGET), c - STOP_MULT * atr
    for j in range(1, HOLD + 1):
        k = np.minimum(idx + j, n - 1)
        hit_t = alive & (h[k] >= tgt)
        # a gap open above the target fills at the open; otherwise at the limit
        ret[hit_t] = np.where(o[k][hit_t] > tgt[hit_t], o[k][hit_t], tgt[hit_t]) / c[hit_t] - 1
        alive &= ~hit_t
        out = alive & ((c[k] <= stop) | (rsi[k] >= RSI_EXIT) | (j == HOLD))
        ret[out] = c[k][out] / c[out] - 1
        alive &= ~out
    ret[~valid] = np.nan
    return ret


def fetch(symbols: list[str], cache: Path) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    cache.mkdir(parents=True, exist_ok=True)
    f = cache / "prices.pkl"
    if f.exists():
        return pickle.loads(f.read_bytes())
    out: dict[str, pd.DataFrame] = {}
    tickers = [s.replace(".", "-") for s in symbols]
    for i in range(0, len(tickers), 100):
        chunk = tickers[i : i + 100]
        d = yf.download(chunk, start=START, end=END, auto_adjust=True, progress=False,
                        group_by="ticker", threads=True)
        for t in chunk:
            try:
                x = d[t].dropna(subset=["Close"])
            except KeyError:
                continue
            if len(x) > 260:
                out[t.replace("-", ".")] = x
    f.write_bytes(pickle.dumps(out))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(ROOT / ".placebo_cache"))
    # Not `docs/`: the dated report there is a verdict and is not regenerated
    # in place. Pass --out to write a new one.
    ap.add_argument("--out", default=str(ROOT / ".placebo_cache" / "placebo-backtest.md"))
    a = ap.parse_args()

    uni = json.loads((ROOT / "data" / "universe.json").read_text())
    sector = {r["symbol"]: r["sector"] for r in uni["cross_check"]}
    etfs = json.loads((ROOT / "config" / "etfs.json").read_text())
    etf_sector = dict(etfs["sector"])
    sector_etf = {v: k for k, v in etf_sector.items()}

    syms = sorted(sector)
    px = fetch(syms + list(etf_sector), Path(a.cache))
    dates = sorted(set().union(*(set(d.index) for s, d in px.items() if s in sector)))
    dix = {d: i for i, d in enumerate(dates)}
    N = len(dates)

    # Align every symbol on the common calendar (NaN where it did not trade).
    RSI = {}; R = {}; TREND = {}; OVER = {}
    for s in syms:
        if s not in px:
            continue
        d = px[s].reindex(dates)
        if d["Close"].isna().mean() > 0.5:
            continue
        d = d.ffill(limit=3)
        ind = indicators(d)
        r = trade_returns(ind)
        c = ind["c"]
        with np.errstate(invalid="ignore"):
            trend = c >= (1 - TREND_BAND) * ind["sma200"]
            sig = (ind["rsi"] <= RSI_TRIGGER) & trend & (c < ind["sma20"])
        RSI[s] = ind["rsi"]; R[s] = r; TREND[s] = trend; OVER[s] = sig
        d_fwd = np.full(N, np.nan); d_fwd[: N - HOLD] = c[HOLD:] / c[: N - HOLD] - 1
        R[s + "#fwd"] = d_fwd
    live = [s for s in syms if s in OVER]

    # Sector ETF gate (regime_v2's DIP rule): ETF >= its 200d and not RSI-peaked.
    gate = {}
    for etf, sec in etf_sector.items():
        if etf not in px:
            continue
        ind = indicators(px[etf].reindex(dates).ffill(limit=3))
        with np.errstate(invalid="ignore"):
            gate[sec] = (ind["c"] >= ind["sma200"]) & (ind["rsi"] < SECTOR_RSI_PEAK)

    by_sec: dict[str, list[str]] = {}
    for s in live:
        by_sec.setdefault(sector[s], []).append(s)

    rng = np.random.default_rng(SEED)
    rows = []
    for s in live:
        sec = sector[s]
        last = -10**9
        for i in np.flatnonzero(OVER[s]):
            if i - last <= HOLD or not np.isfinite(R[s][i]):
                continue
            last = i
            peers = [p for p in by_sec[sec] if p != s and not OVER[p][i] and np.isfinite(R[p][i])]
            p2 = [p for p in peers if TREND[p][i] and RSI[p][i] > RSI_TRIGGER]
            if len(peers) < 5:
                continue
            draw = lambda pool: float(np.mean([R[p][i] for p in rng.choice(pool, K_PLACEBO)])) if len(pool) >= 3 else np.nan  # noqa: E731
            drawf = lambda pool: float(np.mean([R[p + "#fwd"][i] for p in rng.choice(pool, K_PLACEBO)])) if len(pool) >= 3 else np.nan  # noqa: E731
            rows.append(dict(
                sym=s, sector=sec, date=dates[i], year=dates[i].year, ret=R[s][i], fwd=R[s + "#fwd"][i],
                p1=draw(peers), p2=draw(p2), p1f=drawf(peers), p2f=drawf(p2),
                gate=bool(gate.get(sec, np.ones(N, bool))[i]),
            ))
    ev = pd.DataFrame(rows)
    ev.to_csv(Path(a.cache) / "events.csv", index=False)

    def boot(diff: pd.Series, dts: pd.Series) -> tuple[float, float, float, float]:
        """Mean paired diff with a date-clustered bootstrap (same-day events are correlated).

        `eval_selection.clustered_bootstrap` — the one implementation the live
        control arm is judged by too (S0)."""
        s = es.clustered_bootstrap(diff.tolist(), [str(d) for d in dts], boot=BOOT, seed=SEED, min_n=1)
        return s["mean"], s["lo"], s["hi"], s["p_le_0"]

    def block(name: str, e: pd.DataFrame) -> str:
        if len(e) < 30:
            return f"| {name} | {len(e)} | too few | | | | | | |\n"
        o = f"| {name} | {len(e)} | {e.ret.mean():+.2%} | {e.p1.mean():+.2%} | {e.p2.mean():+.2%} |"
        for col in ("p1", "p2"):
            m, lo, hi, pv = boot(e.ret - e[col], e.date)
            o += f" {m:+.2%} [{lo:+.2%}, {hi:+.2%}] p={pv:.3f} |"
        return o + f" {(e.ret > 0).mean():.0%} |\n"

    def fblock(name: str, e: pd.DataFrame) -> str:
        if len(e) < 30:
            return f"| {name} | {len(e)} | too few |\n"
        o = f"| {name} | {len(e)} | {e.fwd.mean():+.2%} |"
        for col in ("p1f", "p2f"):
            m, lo, hi, pv = boot(e.fwd - e[col], e.date)
            o += f" {m:+.2%} [{lo:+.2%}, {hi:+.2%}] p={pv:.3f} |"
        return o + "\n"

    head = "| slice | N | DIP mean | P1 mean | P2 mean | DIP-P1 [95% CI] | DIP-P2 [95% CI] | win% |\n|---|---|---|---|---|---|---|---|\n"
    hf = "| slice | N | DIP 8d fwd | DIP-P1 [95% CI] | DIP-P2 [95% CI] |\n|---|---|---|---|---|\n"
    slices = [("all", ev), ("v2 sector gate ON", ev[ev.gate]), ("v2 sector gate OFF (blocked)", ev[~ev.gate])]
    slices += [(f"year {y}", ev[ev.year == y]) for y in sorted(ev.year.unique())]
    t1 = head + "".join(block(n, e) for n, e in slices)
    t2 = hf + "".join(fblock(n, e) for n, e in slices)

    r_all = ev.ret
    rep = f"""# Placebo backtest — classic DIP vs same-sector random entry

Generated by `ops/placebo_backtest.py` (seed {SEED}, K={K_PLACEBO} placebo draws per event,
{BOOT:,} date-clustered bootstrap resamples). Data: yfinance adjusted daily bars,
{dates[0].date()} to {dates[-1].date()}, {len(live)} current S&P 500 names + 11 sector SPDRs.

## Setup
- Signal (`src/plays/dip.py::_classic`): RSI(14) <= {RSI_TRIGGER:.0f}, close >= {1-TREND_BAND:.2f} x SMA200, close < SMA20.
- Exits: resting +{TARGET:.0%} limit (fills on the high; gap-up fills at the open), {STOP_MULT:g}xATR(14) stop on the close,
  RSI >= {RSI_EXIT:.0f} signal exit, {HOLD}-session time stop. Entry at the signal close; no exit on the entry day.
- One event per symbol per {HOLD} sessions (a held name is not re-bought).
- P1 = random same-sector peer, same day, not DIP-signalling. P2 = same, restricted to peers that pass the trend
  filter but are not oversold (RSI > {RSI_TRIGGER:.0f}).
- "v2 sector gate" = the sector ETF at/above its 200-day and RSI < {SECTOR_RSI_PEAK:.0f} (the `regime_v2_enabled` DIP rule).
- Edge = DIP mean minus placebo mean per event; p = share of bootstrap means <= 0.

## Result: exit-stack returns per trade
{t1}
## Robustness: plain 8-session forward return (no exits)
{t2}
Raw signal-trade distribution: mean {r_all.mean():+.2%}, median {r_all.median():+.2%}, win {(r_all>0).mean():.0%},
target-hit share {(r_all >= TARGET - 1e-9).mean():.0%}, worst {r_all.min():+.1%}.

## Limits (all bias the same way or are unmeasured — read before trusting a number)
- **Survivorship:** today's index list. Names that fell out of the S&P after dips are absent, which flatters dips AND
  the placebo; the paired difference is less affected but not immune.
- No earnings veto, no 4-day loss-sell news hold, no costs or slippage, no portfolio caps (live max 2 new/day, 4-5 names).
- Close-of-signal-day entry assumes the live limit fills near the close.
- Events overlap in calendar time (clustered on market-wide selloffs); the bootstrap clusters by date to respect that.
"""
    Path(a.out).write_text(rep, encoding="utf-8")
    print(rep)


if __name__ == "__main__":
    main()
