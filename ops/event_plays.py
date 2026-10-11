# /// script
# requires-python = ">=3.11"
# dependencies = ["yfinance", "pandas", "numpy", "lxml", "html5lib", "beautifulsoup4", "requests", "tzdata"]
# ///
"""S5: the event plays against history, and Leaders monthly.

Run:  uv run ops/event_plays.py             (the registered run; ~15 min warm)
      uv run ops/event_plays.py --smoke     (plumbing only: counts, no returns)
      uv run ops/event_plays.py --ledger    (also writes this run's trial rows)

Pre-registered. This file implements the S5 event-play study and decides
nothing. The entry rules and the monthly replay are
`ops/backtest_engine.py` (tested in the suite); the control, the costs, the
caps and the per-event statistic are S1's (`ops/honest_backtest.py`); the
factor is S2's (`ops/factor_panel.py`); the history is `ops/event_data.py`.

Writes `.placebo_cache/s5_results.json` and `.placebo_cache/s5_report.md`.
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
import time
from bisect import bisect_left
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import backtest_engine as bt  # noqa: E402
import beta_baseline as bb  # noqa: E402
import eval_selection as es  # noqa: E402
import event_data as ed  # noqa: E402
import factor_panel as fp  # noqa: E402
import honest_backtest as hb  # noqa: E402

NOTE = "ops/event_plays.py"
CACHE = hb.CACHE
FIRST, OOS = hb.FIRST_TRADE, hb.OOS_START
WINDOWS = {"fit_2021_2023": (FIRST, OOS), "test_2024_on": (OOS, None), "full": (FIRST, None)}
REPLAY_WINDOWS = {"test_2024_on": (OOS, None), "full": (FIRST, None)}
TEST = "test_2024_on"
FIT = "fit_2021_2023"
CONTROLS = 200
POWER_GATE = 300
QUIET_SESSIONS = 3
B_MIN_MATCHES = 3
B_MIN_EVENTS = 100
B_TOLERANCE = (0.01, 0.25)
SURPRISE_COVERAGE = 0.70
RETIRE_UPPER = 0.003
# The replay is offered only a day's highest-conviction signals: the account
# takes at most three new names a day, and SURGE signals by the hundred.
OFFERED_PER_DAY = 20
LEADERS_LAST = date(2026, 10, 1)
LEADERS_STAT_N = 10
LEADERS_HELD_N = 7
LEADERS_TARGET = 0.25
LEADERS_MIN_PEERS = 5
RANKERS = ("resid_mom_12_1", "mom_12_1", "blend")
BLEND = (("r3", 0.35), ("r1", 0.25), ("r6", 0.20), ("high", 0.20))


# ---------------------------------------------------------------------------
# Part A: the extra series and the event rows
# ---------------------------------------------------------------------------


class Aux:
    """Per symbol, on the study's calendar: the 50-day average, the one-day
    return and the day's volume over its prior 20-session mean."""

    def __init__(self, inputs) -> None:
        prices = pickle.loads((CACHE / "prices.pkl").read_bytes())
        bars = {**{s: prices[s] for s in inputs.sector_now if s in prices}, **inputs.removed_bars}
        self.sma50, self.ret, self.volx = {}, {}, {}
        for symbol in inputs.panel:
            frame = bars[symbol]
            close, volume = frame["Close"], frame["Volume"].astype(float)
            own = pd.DataFrame({
                "sma50": close.rolling(50).mean(),
                "ret": close / close.shift(1) - 1.0,
                "volx": volume / volume.rolling(20).mean().shift(1),
            }).reindex(inputs.calendar)
            self.sma50[symbol] = hb.none_list(own["sma50"].to_numpy(float))
            self.ret[symbol] = hb.none_list(own["ret"].to_numpy(float))
            self.volx[symbol] = hb.none_list(own["volx"].to_numpy(float))


def eastern_hours(filings: dict[str, list[dict]]) -> tuple[bool, float]:
    """Whether EDGAR's acceptance hour is Eastern wall-clock as recorded: the
    registered check is that at least half of them fall in 16:00 to 17:59."""
    hours = [f["accepted_utc_hour"] for rows in filings.values() for f in rows
             if f["accepted_utc_hour"] is not None]
    share = sum(1 for h in hours if 16.0 <= h < 18.0) / len(hours) if hours else 0.0
    return share >= 0.5, share


def filing_hour(filing: dict, eastern: bool) -> float | None:
    hour = filing["accepted_utc_hour"]
    if hour is None or eastern:
        return hour
    stamp = pd.Timestamp(filing["day"]).tz_localize("UTC") + pd.Timedelta(hours=hour)
    local = stamp.tz_convert("America/New_York")
    return local.hour + local.minute / 60.0


class Marks:
    """Where each symbol's reports and 8-Ks fall on the calendar."""

    def __init__(self, inputs, earnings, filings) -> None:
        days, n = inputs.days, len(inputs.days)
        self.eastern, self.eastern_share = eastern_hours(filings)
        self.report_day: dict[str, list[tuple[int, dict]]] = {}
        self.usable: dict[str, list[bool]] = {}
        self.usable_ex: dict[str, list[bool]] = {}
        event = {}
        for symbol in inputs.panel:
            seen = [False] * n
            reports = []
            for report in earnings.get(symbol, ()):
                i = bisect_left(days, report["day"])
                if i < n:
                    reports.append((i, report))
                    seen[i] = True
                    r = bt.reaction_session(days, report["day"], report["hour"])
                    if r is not None:
                        seen[r] = True
            self.report_day[symbol] = reports
            usable, usable_ex = [False] * n, [False] * n
            for filing in filings.get(symbol, ()):
                u = bt.filing_session(days, filing["day"], filing_hour(filing, self.eastern))
                if u is None:
                    continue
                usable[u] = True
                seen[u] = True
                if "2.02" not in filing["items"]:
                    usable_ex[u] = True
            self.usable[symbol], self.usable_ex[symbol] = usable, usable_ex
            event[symbol] = seen
        # busy[t]: an event of the name's own in the trailing sessions.
        self.busy = {s: [any(row[max(0, t - QUIET_SESSIONS): t]) for t in range(n)]
                     for s, row in event.items()}


def surprise_coverage(inputs, marks: Marks) -> dict[str, float]:
    out = {}
    for name, (lo, hi) in WINDOWS.items():
        total = have = 0
        for symbol in inputs.today_only:
            for i, report in marks.report_day[symbol]:
                day = inputs.days[i]
                if day < lo or (hi is not None and day >= hi):
                    continue
                total += 1
                have += report["surprise_pct"] is not None
        out[name] = have / total if total else 0.0
    return out


def blank(symbols, n: int) -> dict[str, list]:
    return {s: [None] * n for s in symbols}


def pead_rows(inputs, aux: Aux, marks: Marks, symbols, member, first: int, coverage: dict[str, float],
              *, agnostic: bool = False) -> dict[str, list]:
    days, n = inputs.days, len(inputs.days)
    need = {FIT: coverage[FIT] >= SURPRISE_COVERAGE, TEST: coverage[TEST] >= SURPRISE_COVERAGE}
    rows = blank(symbols, n)
    for symbol in symbols:
        closes = inputs.panel[symbol].c
        for i, report in marks.report_day[symbol]:
            if agnostic:
                r, span = i + 1, 2
            else:
                r, span = bt.reaction_session(days, report["day"], report["hour"]), 1
            if r is None or r + 1 >= n:
                continue
            t = r + 1
            if t < first or not member(symbol, t):
                continue
            window = FIT if days[t] < OOS else TEST
            conviction = bt.pead_signal(closes, aux.sma50[symbol], r, report["surprise_pct"],
                                        need_surprise=need[window], span=span)
            if conviction is not None:
                rows[symbol][t] = conviction
    return rows


def catalyst_rows(inputs, aux: Aux, usable: dict[str, list[bool]], symbols, member, first: int) -> dict[str, list]:
    n = len(inputs.days)
    rows = blank(symbols, n)
    for symbol in symbols:
        flags = usable[symbol]
        if not any(flags):
            continue
        for trigger in range(first - 1, n - 1):
            conviction = bt.catalyst_signal(aux.ret[symbol], aux.volx[symbol], flags, trigger)
            if conviction is not None and member(symbol, trigger + 1):
                rows[symbol][trigger + 1] = conviction
    return rows


def surge_rows(inputs, aux: Aux, symbols, member, first: int) -> dict[str, list]:
    n = len(inputs.days)
    rows = blank(symbols, n)
    for symbol in symbols:
        series = inputs.panel[symbol]
        for t in range(first, n):
            if member(symbol, t):
                rows[symbol][t] = bt.surge_signal(series, aux.sma50[symbol], t)
    return rows


def offered(rows: dict[str, list], limit: int) -> dict[str, list]:
    """The same rows with each day cut to its ``limit`` highest convictions."""
    n = len(next(iter(rows.values())))
    out = blank(rows, n)
    for t in range(n):
        today = sorted(((row[t], s) for s, row in rows.items() if row[t] is not None), reverse=True)
        for conviction, symbol in today[:limit]:
            out[symbol][t] = conviction
    return out


class EventStudy(hb.Study):
    """S1's study with the signal swapped: ``rows[symbol][t]`` is a play's
    conviction for a buy at the open of ``t``. The control pool also leaves
    out a name with an event of its own in the trailing sessions."""

    def __init__(self, panel, sectors, days, member, rows, busy, limit: int | None = None):
        super().__init__(panel, sectors, days, member)
        self.rows = rows
        self.busy = busy
        self.offered = rows if limit is None else offered(rows, limit)

    def signals(self, veto: bool) -> dict[str, list]:
        return self.offered

    def eligible(self, peer: str, t: int) -> bool:
        series = self.panel[peer]
        return (self.member(peer, t) and series.o[t] is not None and self.rows[peer][t] is None
                and not bt.blocked_by_earnings(series, t)
                and not (self.busy is not None and self.busy[peer][t]))

    def pool(self, symbol: str, t: int, veto: bool) -> list[str]:
        key = (symbol, t)
        if key not in self._pools:
            self._pools[key] = [peer for peer in self.by_sector.get(self.sectors[symbol], ())
                                if peer != symbol and self.eligible(peer, t)]
        return self._pools[key]

    def events(self, veto: bool = True) -> list[tuple[str, int]]:
        out = []
        for symbol, row in self.rows.items():
            last = -10 ** 9
            for t, conviction in enumerate(row):
                if conviction is None or t - last <= hb.DEDUPE:
                    continue
                last = t
                out.append((symbol, t))
        return out


def study_for(inputs, which: str, rows, busy, limit=None) -> EventStudy:
    if which == "today":
        return EventStudy(inputs.today_only, inputs.sector_now, inputs.days, None, rows, busy, limit)
    days, membership = inputs.days, inputs.membership
    return EventStudy(inputs.panel, inputs.sectors_all, days,
                      lambda symbol, t: membership.is_member(symbol, days[t]), rows, busy, limit)


def symbols_of(inputs, which: str) -> list[str]:
    if which == "today":
        return sorted(inputs.today_only)
    return sorted(s for s in inputs.panel if s in inputs.sectors_all)


def member_of(inputs, which: str):
    if which == "today":
        return lambda symbol, t: True
    days, membership = inputs.days, inputs.membership
    return lambda symbol, t: membership.is_member(symbol, days[t])


def in_window(day: date, span) -> bool:
    lo, hi = span
    return day >= lo and (hi is None or day < hi)


def control_b(study: EventStudy, aux: Aux, events, exits: bt.Exits, costs: bt.Costs) -> list[dict]:
    """Per event: its net return minus the mean of names that moved about as
    much on the trigger session with no event of their own."""
    by_day: dict[int, list[tuple[str, float]]] = {}
    rows = []
    for symbol, t in events:
        move = aux.ret[symbol][t - 1]
        mine = study.trade(symbol, t, exits)
        if move is None or mine is None or mine.ret is None:
            continue
        if t not in by_day:
            by_day[t] = [(peer, aux.ret[peer][t - 1]) for peer in study.rows
                         if aux.ret[peer][t - 1] is not None and study.eligible(peer, t)]
        tolerance = max(B_TOLERANCE[0], B_TOLERANCE[1] * abs(move))
        peers = []
        for peer, other in by_day[t]:
            if peer == symbol or abs(other - move) > tolerance:
                continue
            theirs = study.trade(peer, t, exits)
            if theirs is not None and theirs.ret is not None:
                peers.append(study.net(peer, theirs, costs, hb.PER_TRADE_NOTIONAL))
        if len(peers) < B_MIN_MATCHES:
            continue
        ret = study.net(symbol, mine, costs, hb.PER_TRADE_NOTIONAL)
        control = sum(peers) / len(peers)
        rows.append({"symbol": symbol, "t": t, "day": study.days[t], "ret": ret, "control": control,
                     "diff": ret - control, "reason": mine.reason, "peers": len(peers)})
    return rows


def strip(summary: dict) -> dict:
    return {k: summary[k] for k in ("n", "dip_mean", "control_mean", "paired")}


def by_window(rows: list[dict]) -> dict:
    return {name: strip(hb.summarise(rows, *span)) for name, span in WINDOWS.items()}


def measure(study: EventStudy, aux: Aux, inputs, exits: bt.Exits, *, with_b: bool, smoke: bool) -> dict:
    """The power gate first, on counts alone; returns only if it is passed."""
    events = study.events()
    counts = {name: sum(1 for _, t in events if in_window(study.days[t], span))
              for name, span in WINDOWS.items()}
    gate = sum(1 for s, t in events
               if in_window(study.days[t], WINDOWS[TEST]) and len(study.pool(s, t, True)) >= hb.MIN_PEERS)
    out = {"events": counts, "gate_events": gate, "testable": gate >= POWER_GATE}
    if smoke or not out["testable"]:
        return out
    rows = study.paired(events, exits, inputs.net, True)
    out["control_a"] = by_window(rows)
    reasons: dict[str, int] = {}
    for row in rows:
        reasons[row["reason"]] = reasons.get(row["reason"], 0) + 1
    out["reasons"] = dict(sorted(reasons.items()))
    if with_b:
        matched = control_b(study, aux, events, exits, inputs.net)
        out["control_b"] = by_window(matched)
        out["control_b_measured"] = out["control_b"][TEST]["n"] >= B_MIN_EVENTS
    return out


def replay(study: EventStudy, inputs, exits: bt.Exits, etfs) -> dict:
    caps = hb.shipped_caps(inputs.config)
    out = {}
    for name, (lo, hi) in REPLAY_WINDOWS.items():
        p = hb.portfolio(study, caps, exits, inputs.net_fill, True, lo, hi, CONTROLS)
        exposure = p["dip"]["exposure"] or 0.0
        spy = bt.metrics(bb.beta_arm(inputs, etfs, {bb.BROAD: 1.0}, exposure, lo, hi, rebalance=False),
                         hb.START_EQUITY)
        out[name] = {
            "arm": p["dip"], "control": p["control"],
            "control_total_p05": p["control_total_p05"], "control_total_p95": p["control_total_p95"],
            "beats_share": p["beats_share"], "taken": strip(p["taken"]),
            "spy_matched": {k: spy[k] for k in ("total_return", "max_drawdown", "sharpe", "exposure")},
        }
    return out


def verdict(primary: dict, replays: dict | None, *, needs_b: bool) -> dict:
    """The pre-registered verdicts."""
    if not primary.get("testable"):
        return {"verdict": "shelve", "why": f"untestable: {primary['gate_events']} test-window events with a control, under {POWER_GATE}"}
    test, fit = primary["control_a"][TEST], primary["control_a"][FIT]
    stat, fit_stat = test["paired"], fit["paired"]
    if stat is None:
        return {"verdict": "shelve", "why": "too few test-window events for an interval"}
    checks = {
        "lower_bound_above_zero": stat["lo"] > 0.0,
        "n_at_least_300": test["n"] >= POWER_GATE,
        "fit_same_sign": fit_stat is not None and (fit_stat["mean"] > 0.0) == (stat["mean"] > 0.0),
    }
    if needs_b:
        b = primary.get("control_b", {}).get(TEST, {}).get("paired")
        checks["control_b_measured_and_positive"] = bool(primary.get("control_b_measured")) and b is not None and b["mean"] > 0.0
    if replays is not None:
        r = replays[TEST]
        checks["replay_not_under_spy_at_matched_exposure"] = r["arm"]["total_return"] >= r["spy_matched"]["total_return"]
    if all(checks.values()):
        return {"verdict": "keep", "checks": checks}
    retire = stat["hi"] < RETIRE_UPPER or (stat["mean"] < 0.0 and fit_stat is not None and fit_stat["mean"] < 0.0)
    return {"verdict": "retire" if retire else "shelve", "checks": checks}


# ---------------------------------------------------------------------------
# Part B: Leaders monthly
# ---------------------------------------------------------------------------


class Bars:
    """What `bt.rebalance_replay` reads for one name."""

    __slots__ = ("o", "h", "c", "adv")

    def __init__(self, o, h, c, adv) -> None:
        self.o, self.h, self.c, self.adv = o, h, c, adv


class Leaders:
    def __init__(self, inputs, panel: fp.Panel, bars: dict[str, pd.DataFrame]) -> None:
        self.inputs, self.panel = inputs, panel
        self.days = panel.days
        calendar = bars["SPY"].index
        C = panel.C
        self.sma200 = pd.DataFrame(C).rolling(200, min_periods=200).mean().to_numpy()
        self.member = np.array([[inputs.membership.is_member(s, d) for s in panel.symbols]
                                for d in self.days])
        self.starts = bt.month_starts(self.days, FIRST, LEADERS_LAST)
        self.periods = list(zip(self.starts[:-1], self.starts[1:]))
        self.costs = bt.Costs()
        high = np.column_stack([bars[s]["High"].reindex(calendar).to_numpy(float) for s in panel.symbols])
        self.book = {
            s: Bars(hb.none_list(panel.O[:, j]), hb.none_list(high[:, j]), hb.none_list(C[:, j]),
                    hb.none_list(np.roll(panel.adv[:, j], 1)))
            for j, s in enumerate(panel.symbols)
        }
        spy = bars["SPY"].reindex(calendar)
        self.book[bb.BROAD] = Bars(hb.none_list(spy["Open"].to_numpy(float)),
                                   hb.none_list(spy["High"].to_numpy(float)),
                                   hb.none_list(spy["Close"].to_numpy(float)),
                                   [1e11] * len(self.days))   # the tightest spread bucket
        self._eligible: dict[int, np.ndarray] = {}

    def score(self, ranker: str, i: int) -> np.ndarray:
        panel = self.panel
        if ranker == "resid_mom_12_1":
            return panel.residual_momentum(i)
        if ranker == "mom_12_1":
            return panel.F["mom_12_1"][i]
        C = panel.C
        with np.errstate(invalid="ignore", divide="ignore"):
            parts = {"r3": C[i] / C[i - 63] - 1.0, "r1": C[i] / C[i - 21] - 1.0,
                     "r6": C[i] / C[i - 126] - 1.0, "high": panel.F["near_52w_high"][i]}
        ok = np.all([np.isfinite(v) for v in parts.values()], axis=0) & self.base(i + 1)
        out = np.full(len(panel.symbols), np.nan)
        if ok.sum() > 1:
            total = np.zeros(int(ok.sum()))
            for name, weight in BLEND:
                total += weight * pd.Series(parts[name][ok]).rank(pct=True).to_numpy()
            out[ok] = total
        return out

    def base(self, t: int) -> np.ndarray:
        """Eligible but for the score: a member on ``t``, above its 200-day at
        the close of ``t - 1``, an open on ``t``."""
        if t not in self._eligible:
            i = t - 1
            with np.errstate(invalid="ignore"):
                self._eligible[t] = (self.member[t] & (self.panel.C[i] > self.sma200[i])
                                     & np.isfinite(self.panel.O[t]))
        return self._eligible[t]

    def eligible(self, ranker: str, t: int, t2: int) -> tuple[np.ndarray, np.ndarray]:
        score = self.score(ranker, t - 1)
        ok = self.base(t) & np.isfinite(score) & np.isfinite(self.panel.O[t2])
        return ok, score

    def top(self, ranker: str, t: int, t2: int, k: int) -> list[int]:
        ok, score = self.eligible(ranker, t, t2)
        columns = np.flatnonzero(ok)
        return [int(j) for j in columns[np.argsort(-score[columns], kind="stable")][:k]]

    def per_name(self, ranker: str) -> list[dict]:
        panel, rows = self.panel, []
        for t, t2 in self.periods:
            ok, _ = self.eligible(ranker, t, t2)
            with np.errstate(invalid="ignore", divide="ignore"):
                gross = panel.O[t2] / panel.O[t] - 1.0
            net = {int(j): float(gross[j]) - fp.round_trip_cost(_adv(panel.adv[t - 1, j]), self.costs)
                   for j in np.flatnonzero(ok)}
            by_sector: dict[str, list[int]] = {}
            for j in net:
                by_sector.setdefault(panel.sector[j], []).append(j)
            for j in self.top(ranker, t, t2, LEADERS_STAT_N):
                peers = [net[k] for k in by_sector[panel.sector[j]] if k != j]
                if len(peers) < LEADERS_MIN_PEERS:
                    continue
                control = sum(peers) / len(peers)
                rows.append({"symbol": panel.symbols[j], "t": t, "day": self.days[t], "ret": net[j],
                             "control": control, "diff": net[j] - control, "peers": len(peers)})
        return rows

    def schedule(self, ranker: str, k: int, lo: int, hi: int) -> dict[int, list[str]]:
        symbols = self.panel.symbols
        return {t: [symbols[j] for j in self.top(ranker, t, t2, k)]
                for t, t2 in self.periods if lo <= t <= hi}

    def control_schedule(self, ranker: str, k: int, lo: int, hi: int, m: int) -> dict[int, list[str]]:
        panel, out = self.panel, {}
        for t, t2 in self.periods:
            if not lo <= t <= hi:
                continue
            ok, _ = self.eligible(ranker, t, t2)
            chosen: list[str] = []
            for j in self.top(ranker, t, t2, k):
                pool = [panel.symbols[x] for x in np.flatnonzero(ok)
                        if panel.sector[x] == panel.sector[j] and x != j and panel.symbols[x] not in chosen]
                peer = bt.pick(pool, hb.SEED, m, t, panel.symbols[j])
                if peer is not None:
                    chosen.append(peer)
            out[t] = chosen
        return out

    def run(self, schedule, lo: int, hi: int, *, fraction: float, target: float | None) -> dict:
        result = bt.rebalance_replay(self.days, self.book, schedule, self.costs,
                                     start_equity=hb.START_EQUITY, fraction=fraction, target=target,
                                     first_day=lo, last_day=hi)
        m = bt.metrics(result, hb.START_EQUITY)
        return {**{k: m[k] for k in ("total_return", "max_drawdown", "sharpe", "turnover", "exposure")},
                "target_fills": result.sleeve["target_fills"], "halt_reached": result.sleeve["halt_reached"]}

    def spy(self, lo: int, hi: int, fraction: float) -> dict:
        result = bt.rebalance_replay(self.days, self.book, {lo: [bb.BROAD]}, self.costs,
                                     start_equity=hb.START_EQUITY, fraction=fraction, target=None,
                                     first_day=lo, last_day=hi)
        m = bt.metrics(result, hb.START_EQUITY)
        return {k: m[k] for k in ("total_return", "max_drawdown", "sharpe", "exposure")}

    def arm(self, ranker: str, lo: int, hi: int, *, fraction: float, target: float | None,
            controls: int) -> dict:
        mine = self.run(self.schedule(ranker, LEADERS_HELD_N, lo, hi), lo, hi, fraction=fraction, target=target)
        totals, drawdowns = [], []
        for m in range(controls):
            other = self.run(self.control_schedule(ranker, LEADERS_HELD_N, lo, hi, m), lo, hi,
                             fraction=fraction, target=target)
            totals.append(other["total_return"])
            drawdowns.append(other["max_drawdown"])
        return {
            "arm": mine,
            "control": {"total_return": float(np.mean(totals)) if totals else None,
                        "max_drawdown": float(np.mean(drawdowns)) if drawdowns else None,
                        "p05": float(np.percentile(totals, 5)) if totals else None,
                        "p95": float(np.percentile(totals, 95)) if totals else None},
            "beats_share": (sum(1 for x in totals if mine["total_return"] > x) / len(totals)) if totals else None,
            "spy_matched": self.spy(lo, hi, mine["exposure"] or 0.0),
        }


def _adv(value) -> float | None:
    return float(value) if np.isfinite(value) else None


def leaders_verdict(stat_rows: dict, replays: dict) -> dict:
    test, fit = stat_rows[TEST], stat_rows[FIT]
    stat, fit_stat = test["paired"], fit["paired"]
    if stat is None:
        return {"verdict": "shelve", "why": "too few names for an interval"}
    r = replays["L7"][TEST]
    checks = {
        "lower_bound_above_zero": stat["lo"] > 0.0,
        "n_at_least_300": test["n"] >= POWER_GATE,
        "fit_same_sign": fit_stat is not None and (fit_stat["mean"] > 0.0) == (stat["mean"] > 0.0),
        "replay_not_under_spy_at_matched_exposure": r["arm"]["total_return"] >= r["spy_matched"]["total_return"],
    }
    if all(checks.values()):
        return {"verdict": "keep", "checks": checks}
    retire = stat["hi"] < RETIRE_UPPER or (stat["mean"] < 0.0 and fit_stat is not None and fit_stat["mean"] < 0.0)
    return {"verdict": "retire" if retire else "shelve", "checks": checks}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true", help="counts only; no return is computed")
    parser.add_argument("--ledger", action="store_true")
    args = parser.parse_args()
    t0 = time.time()

    inputs, panel2, _, info = fp.load(False)
    earnings, filings = ed.load()
    aux = Aux(inputs)
    marks = Marks(inputs, earnings, filings)
    coverage = surprise_coverage(inputs, marks)
    first = next(i for i, d in enumerate(inputs.days) if d >= FIRST)
    exits = inputs.earn(inputs.no_rsi)
    long_exits = bt.Exits(target=0.15, hold=21, rsi_exit=None, earnings_flatten=exits.earnings_flatten)
    print(f"inputs {time.time() - t0:.0f}s", file=sys.stderr)

    reports = [(inputs.days[i], r) for s in inputs.today_only for i, r in marks.report_day[s]
               if inputs.days[i] >= FIRST]
    results: dict = {"data": {
        **info,
        "earnings_symbols": len(earnings), "reports_in_window": len(reports),
        "reports_before_open": sum(1 for _, r in reports if r["hour"] < bt.BEFORE_OPEN_HOUR),
        "reports_by_year": {str(y): sum(1 for d, _ in reports if d.year == y) for y in range(2021, 2027)},
        "surprise_coverage": coverage,
        "surprise_clause_applied": {w: coverage[w] >= SURPRISE_COVERAGE for w in (FIT, TEST)},
        "filing_companies": len(filings),
        "filings": sum(len(v) for v in filings.values()),
        "filings_ex_earnings": sum(1 for v in filings.values() for f in v if "2.02" not in f["items"]),
        "acceptance_hours_16_to_18_share": marks.eastern_share,
        "acceptance_hour_read_as_eastern": marks.eastern,
        "controls_per_replay": CONTROLS, "offered_per_day": OFFERED_PER_DAY,
        "smoke": args.smoke,
    }}

    def build(play: str, which: str) -> EventStudy:
        symbols, member = symbols_of(inputs, which), member_of(inputs, which)
        if play.startswith("PEAD"):
            rows = pead_rows(inputs, aux, marks, symbols, member, first, coverage,
                             agnostic=play == "PEAD-agnostic")
        elif play == "CATALYST":
            rows = catalyst_rows(inputs, aux, marks.usable, symbols, member, first)
        elif play == "CATALYST-ex-earnings":
            rows = catalyst_rows(inputs, aux, marks.usable_ex, symbols, member, first)
        else:
            rows = surge_rows(inputs, aux, symbols, member, first)
        return study_for(inputs, which, rows, marks.busy, OFFERED_PER_DAY)

    variants = {
        "PEAD": ("PEAD", "today", exits, True),
        "PEAD-agnostic": ("PEAD-agnostic", "today", exits, False),
        "PEAD-21": ("PEAD", "today", long_exits, True),
        "PEAD|members by date": ("PEAD", "pit", exits, False),
        "CATALYST": ("CATALYST", "today", exits, True),
        "CATALYST-ex-earnings": ("CATALYST-ex-earnings", "today", exits, True),
        "SURGE": ("SURGE", "today", exits, False),
        "SURGE|members by date": ("SURGE", "pit", exits, False),
    }
    studies: dict[tuple[str, str], EventStudy] = {}
    plays = {}
    for label, (play, which, stack, with_b) in variants.items():
        key = (play, which)
        if key not in studies:
            studies[key] = build(play, which)
        plays[label] = measure(studies[key], aux, inputs, stack, with_b=with_b, smoke=args.smoke)
        print(f"{label}: {plays[label]['events']} gate {plays[label]['gate_events']} "
              f"{time.time() - t0:.0f}s", file=sys.stderr)
    results["plays"] = plays
    results["plays"]["MACRO"] = {"events": {w: 0 for w in WINDOWS}, "gate_events": 0, "testable": False}

    replays: dict = {}
    if not args.smoke:
        etfs = bb.etf_panel(inputs, [])
        for label in ("PEAD", "PEAD-21", "CATALYST", "SURGE"):
            play, which, stack, _ = variants[label]
            if plays[label]["testable"]:
                replays[label] = replay(studies[(play, which)], inputs, stack, etfs)
                print(f"replay {label} {time.time() - t0:.0f}s", file=sys.stderr)
    results["replays"] = replays
    results["verdicts"] = {
        "PEAD": verdict(plays["PEAD"], replays.get("PEAD"), needs_b=True),
        "CATALYST": verdict(plays["CATALYST"], replays.get("CATALYST"), needs_b=True),
        "SURGE": verdict(plays["SURGE"], replays.get("SURGE"), needs_b=False),
        "MACRO": verdict(plays["MACRO"], None, needs_b=False),
    } if not args.smoke else {}

    # -- Leaders monthly -------------------------------------------------------
    bars = hb.cached("s2_prices.pkl", lambda: {})
    leaders = Leaders(inputs, panel2, bars)
    sizes = []
    for t, t2 in leaders.periods:
        ok, _ = leaders.eligible("resid_mom_12_1", t, t2)
        sizes.append(int(ok.sum()))
    results["leaders"] = {
        "periods": {name: sum(1 for t, _ in leaders.periods if in_window(leaders.days[t], span))
                    for name, span in WINDOWS.items()},
        "eligible_names_mean": float(np.mean(sizes)) if sizes else None,
        "eligible_names_min": min(sizes) if sizes else None,
    }
    if not args.smoke:
        results["leaders"]["per_name"] = {r: by_window(leaders.per_name(r)) for r in RANKERS}
        reserve = 1.0 - float(inputs.config["cash_reserve_pct"])
        arms = {"L7": (reserve, LEADERS_TARGET), "L7-half": (0.5, LEADERS_TARGET),
                "L7-no-target": (reserve, None)}
        out = {}
        for name, (fraction, target) in arms.items():
            out[name] = {}
            for window, (lo_day, hi_day) in REPLAY_WINDOWS.items():
                lo = next(i for i, d in enumerate(leaders.days) if d >= lo_day)
                out[name][window] = leaders.arm("resid_mom_12_1", lo, len(leaders.days) - 1,
                                                fraction=fraction, target=target,
                                                controls=CONTROLS if name != "L7-no-target" else 0)
            print(f"leaders {name} {time.time() - t0:.0f}s", file=sys.stderr)
        for ranker in RANKERS[1:]:
            out[f"L7|{ranker}"] = {}
            for window, (lo_day, hi_day) in REPLAY_WINDOWS.items():
                lo = next(i for i, d in enumerate(leaders.days) if d >= lo_day)
                out[f"L7|{ranker}"][window] = leaders.arm(ranker, lo, len(leaders.days) - 1, fraction=reserve,
                                                          target=LEADERS_TARGET, controls=0)
        results["leaders"]["replays"] = out
        results["verdicts"]["LEADERS"] = leaders_verdict(results["leaders"]["per_name"]["resid_mom_12_1"], out)
        picks = leaders.schedule("resid_mom_12_1", LEADERS_HELD_N, 0, len(leaders.days) - 1)
        last = max(picks)
        results["leaders"]["last_picks"] = {"day": str(leaders.days[last]), "names": picks[last]}

    prior = es.ledger_count() - sum(1 for row in es.ledger_rows() if row.get("note") == NOTE)
    rows = [] if args.smoke else ledger_rows(results)
    results["trials"] = {"ledger_before": prior, "this_run": len(rows), "ledger_after": prior + len(rows),
                         "t_needed_bonferroni": es.bonferroni_t(prior + len(rows))}
    name = "s5_smoke.json" if args.smoke else "s5_results.json"
    (CACHE / name).write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    text = render(results)
    if not args.smoke:
        (CACHE / "s5_report.md").write_text(text, encoding="utf-8")
        if args.ledger:
            write_ledger(rows)
    sys.stdout.reconfigure(encoding="utf-8")
    print(text)
    print(f"done {time.time() - t0:.0f}s", file=sys.stderr)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def pct(x, d=2):
    return "n/a" if x is None else f"{x:+.{d}%}"


def num(x, d=2):
    return "n/a" if x is None else f"{x:.{d}f}"


def ci(stat):
    return "too few" if stat is None else f"{pct(stat['mean'])} [{pct(stat['lo'])}, {pct(stat['hi'])}]"


def implied_t(stat) -> float | None:
    if stat is None or stat["hi"] <= stat["lo"]:
        return None
    return stat["mean"] / ((stat["hi"] - stat["lo"]) / 3.92)


def ledger_rows(r: dict) -> list[dict]:
    today = date.today().isoformat()
    out = []

    def add(variant, n, metric, result):
        out.append({"date": today, "phase": "S5", "variant": variant, "n": n, "metric": metric,
                    "result": result, "note": NOTE})

    paired = "paired net mean diff per event vs control mean, date-clustered bootstrap; fit / test"
    for label, p in r["plays"].items():
        if not p.get("testable"):
            add(f"{label}: power gate", p["gate_events"], "test-window events with a control; 300 needed",
                "untestable; no return read")
            continue
        a = p["control_a"]
        add(f"{label}: control A (same sector)", a[TEST]["n"], paired,
            f"fit {ci(a[FIT]['paired'])}; test {ci(a[TEST]['paired'])}")
        if "control_b" in p:
            b = p["control_b"]
            add(f"{label}: control B (move-matched)", b[TEST]["n"], paired,
                f"fit {ci(b[FIT]['paired'])}; test {ci(b[TEST]['paired'])}")
    for label, by in r["replays"].items():
        for window, x in by.items():
            add(f"replay {label} | {window}", x["arm"]["trades"],
                "compounded net total return: arm vs mean random control vs SPY at the arm's exposure",
                f"arm {pct(x['arm']['total_return'])}; control {pct(x['control']['total_return'])}; "
                f"SPY {pct(x['spy_matched']['total_return'])}; beats {x['beats_share']:.0%}")
    for ranker, by in r["leaders"]["per_name"].items():
        add(f"Leaders monthly per name: top {LEADERS_STAT_N} by {ranker}", by[TEST]["n"],
            "open-to-open monthly net return minus same-sector eligible mean, clustered by month; fit / test",
            f"fit {ci(by[FIT]['paired'])}; test {ci(by[TEST]['paired'])}")
    for label, by in r["leaders"]["replays"].items():
        for window, x in by.items():
            beats = "n/a" if x["beats_share"] is None else f"{x['beats_share']:.0%}"
            add(f"Leaders replay {label} | {window}", LEADERS_HELD_N,
                "compounded net total return: arm vs mean random control vs SPY at the arm's exposure",
                f"arm {pct(x['arm']['total_return'])}; control {pct(x['control']['total_return'])}; "
                f"SPY {pct(x['spy_matched']['total_return'])}; beats {beats}")
    return out


def write_ledger(rows: list[dict]) -> None:
    kept = [row for row in es.ledger_rows() if row.get("note") != NOTE]
    es.LEDGER.unlink(missing_ok=True)
    for row in [*kept, *rows]:
        es.ledger_append(row)


def render(r: dict) -> str:
    out = [f"## Data\n\n```json\n{json.dumps(r['data'], indent=1, default=str)}\n```\n"]
    out.append("## Event counts and the power gate (300 test-window events with a control)\n")
    out.append("| variant | fit events | test events | test events with a control | testable |\n|---|---|---|---|---|")
    for label, p in r["plays"].items():
        out.append(f"| {label} | {p['events'][FIT]} | {p['events'][TEST]} | {p['gate_events']} | {p['testable']} |")
    if r["data"]["smoke"]:
        out.append(f"\nLeaders periods: {r['leaders']['periods']}; eligible names a month: mean "
                   f"{num(r['leaders']['eligible_names_mean'], 0)}, min {r['leaders']['eligible_names_min']}")
        return "\n".join(out)

    need = r["trials"]["t_needed_bonferroni"]
    out.append(f"\n## Per event: play minus control, net (|t| needed after deflation: {need:.2f})\n")
    out.append("| variant | control | fit [95% CI] | N | test [95% CI] | N | test t | test: play mean | test: control mean | full [95% CI] |")
    out.append("|---|---|---|---|---|---|---|---|---|---|")
    for label, p in r["plays"].items():
        for name, key in (("A: same sector", "control_a"), ("B: move-matched", "control_b")):
            if key not in p:
                continue
            f, t, a = p[key][FIT], p[key][TEST], p[key]["full"]
            out.append(f"| {label} | {name} | {ci(f['paired'])} | {f['n']} | {ci(t['paired'])} | {t['n']} | "
                       f"{num(implied_t(t['paired']), 1)} | {pct(t['dip_mean'])} | {pct(t['control_mean'])} | "
                       f"{ci(a['paired'])} |")
    out.append("\nExit reasons, all windows: " + "; ".join(
        f"{label}: {p['reasons']}" for label, p in r["plays"].items() if "reasons" in p))

    out.append("\n## Replays: shipped caps, $1,400, 46% fill, net\n")
    out.append("| play, window | arm total | max DD | Sharpe | trades | exposure | control mean [5%, 95%] | beats | taken trades vs control [95% CI] | SPY at the arm's exposure | SPY max DD |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for label, by in r["replays"].items():
        for window, x in by.items():
            arm = x["arm"]
            out.append(
                f"| {label}, {window} | {pct(arm['total_return'], 1)} | {pct(arm['max_drawdown'], 1)} | "
                f"{num(arm['sharpe'])} | {arm['trades']} | {arm['exposure']:.0%} | "
                f"{pct(x['control']['total_return'], 1)} [{pct(x['control_total_p05'], 1)}, {pct(x['control_total_p95'], 1)}] | "
                f"{x['beats_share']:.0%} | {ci(x['taken']['paired'])} | "
                f"{pct(x['spy_matched']['total_return'], 1)} | {pct(x['spy_matched']['max_drawdown'], 1)} |")

    lead = r["leaders"]
    out.append(f"\n## Leaders monthly\n\nPeriods: {lead['periods']}. Eligible names a month: mean "
               f"{num(lead['eligible_names_mean'], 0)}, min {lead['eligible_names_min']}. "
               f"Most recent picks ({lead['last_picks']['day']}): {', '.join(lead['last_picks']['names'])}.\n")
    out.append(f"| ranker (top {LEADERS_STAT_N}) | fit [95% CI] | N | test [95% CI] | N | test t | full [95% CI] |\n|---|---|---|---|---|---|---|")
    for ranker, by in lead["per_name"].items():
        f, t, a = by[FIT], by[TEST], by["full"]
        out.append(f"| {ranker} | {ci(f['paired'])} | {f['n']} | {ci(t['paired'])} | {t['n']} | "
                   f"{num(implied_t(t['paired']), 1)} | {ci(a['paired'])} |")
    out.append(f"\n| arm (top {LEADERS_HELD_N}), window | total | max DD | Sharpe | turnover | exposure | target fills | 20% halt reached | control mean [5%, 95%] | beats | SPY at the arm's exposure | SPY max DD |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for label, by in lead["replays"].items():
        for window, x in by.items():
            arm, control = x["arm"], x["control"]
            beats = "n/a" if x["beats_share"] is None else f"{x['beats_share']:.0%}"
            spread = "n/a" if control["total_return"] is None else (
                f"{pct(control['total_return'], 1)} [{pct(control['p05'], 1)}, {pct(control['p95'], 1)}]")
            out.append(
                f"| {label}, {window} | {pct(arm['total_return'], 1)} | {pct(arm['max_drawdown'], 1)} | "
                f"{num(arm['sharpe'])} | {num(arm['turnover'], 1)}x | {arm['exposure']:.0%} | {arm['target_fills']} | "
                f"{arm['halt_reached']} | {spread} | {beats} | {pct(x['spy_matched']['total_return'], 1)} | "
                f"{pct(x['spy_matched']['max_drawdown'], 1)} |")

    out.append("\n## Verdicts\n")
    for label, v in r["verdicts"].items():
        detail = v.get("why") or "; ".join(f"{k}: {x}" for k, x in v["checks"].items())
        out.append(f"- **{label}: {v['verdict']}**. {detail}")
    t = r["trials"]
    out.append(f"\nTrials: {t['ledger_before']} before, {t['this_run']} this run, {t['ledger_after']} after.")
    return "\n".join(out)


if __name__ == "__main__":
    main()
