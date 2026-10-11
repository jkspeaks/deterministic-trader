# /// script
# requires-python = ">=3.11"
# dependencies = ["yfinance", "pandas", "numpy", "lxml", "html5lib", "beautifulsoup4", "requests", "tzdata"]
# ///
"""S6: what the exits cost, and two placeable forms of Leaders monthly.

Run:  uv run ops/exit_study.py             (the registered run)
      uv run ops/exit_study.py --ledger    (also writes this run's trial rows)

Pre-registered. This file implements the S6 exit study and decides nothing.
The trade walk, the replay and the costs are `ops/backtest_engine.py`; the
events and the control pool are S1's (`ops/honest_backtest.py`); Leaders is
S5's (`ops/event_plays.py`).

No variant here rests a stop of any kind. H8 and H21 rest nothing at all and
are measuring sticks, not proposals.

Writes `.placebo_cache/s6_results.json` and `.placebo_cache/s6_report.md`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import backtest_engine as bt  # noqa: E402
import beta_baseline as bb  # noqa: E402
import eval_selection as es  # noqa: E402
import event_plays as ep  # noqa: E402
import factor_panel as fp  # noqa: E402
import honest_backtest as hb  # noqa: E402

NOTE = "ops/exit_study.py"
CACHE = hb.CACHE
WINDOWS, REPLAY_WINDOWS, FIT, TEST = ep.WINDOWS, ep.REPLAY_WINDOWS, ep.FIT, ep.TEST
NEVER = 1e9
AFTER = 21
REPLAY_CONTROLS = 100
LEADER_CONTROLS = 200
CAP_HELD, CAP_STAT = 2, 3            # 30% of seven slots, and of ten, rounded down
REPLAYED = ("V0", "V1", "V2", "V3", "V4", "V5")
GATED = ("V1", "V2", "V3", "V4", "V5")
LABELS = {
    "V0": "baseline (shipped, no RSI exit)", "V1": "target 10%", "V2": "target 15%",
    "V3": "time stop 21 sessions", "V4": "ATR check off", "V5": "earnings deadline off",
    "V6": "RSI exit on (DIP entries only)", "H8": "hold 8, no rule", "H21": "hold 21, no rule",
}
LIVE_RECORD = ROOT / "data" / "s3-live-record.json"


def variants(flatten: int) -> dict[str, bt.Exits]:
    base = dict(rsi_exit=None, earnings_flatten=flatten)
    return {
        "V0": bt.Exits(**base),
        "V1": bt.Exits(target=0.10, **base),
        "V2": bt.Exits(target=0.15, **base),
        "V3": bt.Exits(hold=21, **base),
        "V4": bt.Exits(stop_mult=NEVER, trail_mult=NEVER, **base),
        "V5": bt.Exits(rsi_exit=None, earnings_flatten=None),
        "V6": bt.Exits(rsi_exit=50.0, earnings_flatten=flatten),
        "H8": bt.Exits(target=NEVER, stop_mult=NEVER, trail_mult=NEVER, hold=8, rsi_exit=None),
        "H21": bt.Exits(target=NEVER, stop_mult=NEVER, trail_mult=NEVER, hold=21, rsi_exit=None),
    }


# ---------------------------------------------------------------------------
# Part A
# ---------------------------------------------------------------------------


def after_exit(series: bt.Series, spy: bt.Series, trade: bt.Trade) -> tuple[float, float] | None:
    """From the exit price to the close `AFTER` sessions later: the name's
    return, and that return minus SPY's close-to-close over the same span."""
    end = trade.exit_day + AFTER
    if end >= len(series) or series.c[end] is None or spy.c[end] is None or spy.c[trade.exit_day] is None:
        return None
    raw = series.c[end] / trade.exit - 1.0
    return raw, raw - (spy.c[end] / spy.c[trade.exit_day] - 1.0)


def collect(study: hb.Study, events, stacks: dict[str, bt.Exits], costs: bt.Costs, veto: bool,
            spy: bt.Series) -> tuple[list[dict], dict, list[dict]]:
    """Per event: each variant's net return for the signal and the mean over
    its control peers; pooled descriptive sums per variant; and, for the
    baseline, what each entry did after it was sold."""
    names = [v for v in stacks if v != "V6"]
    rows, after = [], []
    pooled = {v: {"n": 0, "net": 0.0, "mae": 0.0, "mfe": 0.0, "held": 0.0, "reasons": {}} for v in names}

    def nets(symbol: str, t: int, which) -> dict[str, tuple[float, bt.Trade]] | None:
        out = {}
        for v in which:
            trade = study.trade(symbol, t, stacks[v])
            if trade is None or trade.ret is None:
                return None
            out[v] = (study.net(symbol, trade, costs, hb.PER_TRADE_NOTIONAL), trade)
        return out

    for symbol, t in events:
        mine = nets(symbol, t, list(stacks))
        if mine is None:
            continue
        peers = [(peer, x) for peer in study.pool(symbol, t, veto) for x in [nets(peer, t, names)] if x is not None]
        if len(peers) < hb.MIN_PEERS:
            continue
        day = study.days[t]
        rows.append({"day": day, "dip": {v: mine[v][0] for v in stacks},
                     "control": {v: sum(x[v][0] for _, x in peers) / len(peers) for v in names}})
        for _, x in peers:
            for v in names:
                net, trade = x[v]
                p = pooled[v]
                p["n"] += 1
                p["net"] += net
                p["mae"] += trade.mae
                p["mfe"] += trade.mfe
                p["held"] += trade.held
                p["reasons"][trade.reason] = p["reasons"].get(trade.reason, 0) + 1
        by_reason: dict[str, list[tuple[float, float]]] = {}
        for peer, x in peers:
            trade = x["V0"][1]
            later = after_exit(study.panel[peer], spy, trade)
            if later is not None:
                by_reason.setdefault(trade.reason, []).append(later)
        for reason, values in by_reason.items():
            after.append({"day": day, "who": "control", "reason": reason,
                          "raw": sum(v[0] for v in values) / len(values),
                          "adj": sum(v[1] for v in values) / len(values), "entries": len(values)})
        trade = mine["V0"][1]
        later = after_exit(study.panel[symbol], spy, trade)
        if later is not None:
            after.append({"day": day, "who": "dip", "reason": trade.reason, "raw": later[0],
                          "adj": later[1], "entries": 1})
    described = {v: {"entries": p["n"], "mean_net": p["net"] / p["n"], "mae": p["mae"] / p["n"],
                     "mfe": p["mfe"] / p["n"], "held": p["held"] / p["n"],
                     "reasons": dict(sorted(p["reasons"].items()))}
                 for v, p in pooled.items() if p["n"]}
    return rows, described, after


def boot(values, days):
    return es.clustered_bootstrap(values, [str(d) for d in days], boot=hb.BOOT, seed=hb.SEED)


def difference(rows: list[dict], who: str, a: str, b: str) -> dict:
    out = {}
    for name, span in WINDOWS.items():
        use = [r for r in rows if ep.in_window(r["day"], span)]
        out[name] = {"n": len(use), "stat": boot([r[who][a] - r[who][b] for r in use], [r["day"] for r in use])}
    return out


def after_table(after: list[dict], who: str) -> dict:
    out = {}
    for name, span in WINDOWS.items():
        use = [r for r in after if r["who"] == who and ep.in_window(r["day"], span)]
        cells = {}
        for reason in sorted({r["reason"] for r in use}):
            sub = [r for r in use if r["reason"] == reason]
            cells[reason] = {"events": len(sub), "entries": sum(r["entries"] for r in sub),
                             "raw": boot([r["raw"] for r in sub], [r["day"] for r in sub]),
                             "adj": boot([r["adj"] for r in sub], [r["day"] for r in sub]),
                             "share_up": sum(1 for r in sub if r["raw"] > 0) / len(sub) if sub else None}
        pair = [r for r in use if r["reason"] in (bt.ATR_STOP, bt.TIME_STOP)]
        gap = es.clustered_bootstrap_difference(
            [r["adj"] for r in pair], [r["reason"] == bt.ATR_STOP for r in pair],
            [str(r["day"]) for r in pair], boot=hb.BOOT, seed=hb.SEED) if pair else None
        out[name] = {"by_reason": cells, "atr_minus_time": gap}
    return out


def replays(inputs, study, stacks, etfs) -> dict:
    caps = hb.shipped_caps(inputs.config)
    out = {}
    for v in REPLAYED:
        out[v] = {}
        for name, (lo, hi) in REPLAY_WINDOWS.items():
            p = hb.portfolio(study, caps, stacks[v], inputs.net_fill, inputs.veto, lo, hi, REPLAY_CONTROLS)
            exposure = p["control"]["exposure"] or 0.0
            spy = bt.metrics(bb.beta_arm(inputs, etfs, {bb.BROAD: 1.0}, exposure, lo, hi, rebalance=False),
                             hb.START_EQUITY)
            out[v][name] = {
                "dip": {k: p["dip"][k] for k in ("total_return", "max_drawdown", "sharpe", "exposure", "trades")},
                "control": {k: p["control"][k] for k in ("total_return", "max_drawdown", "sharpe", "exposure")},
                "control_p05": p["control_total_p05"], "control_p95": p["control_total_p95"],
                "spy_at_control_exposure": spy["total_return"], "spy_max_drawdown": spy["max_drawdown"],
            }
    return out


def decide(control_diffs: dict, replay: dict) -> dict:
    out = {}
    for v in GATED:
        test, fit = control_diffs[v][TEST]["stat"], control_diffs[v][FIT]["stat"]
        checks = {
            "test_lower_bound_above_zero": test is not None and test["lo"] > 0.0,
            "fit_same_sign": test is not None and fit is not None and (fit["mean"] > 0.0) == (test["mean"] > 0.0),
            "replay_control_at_least_baseline":
                replay[v][TEST]["control"]["total_return"] >= replay["V0"][TEST]["control"]["total_return"],
        }
        out[v] = {"recommended": all(checks.values()), "checks": checks}
    return out


def live_closes(inputs) -> list[dict]:
    record = json.loads(LIVE_RECORD.read_text(encoding="utf-8"))
    out = []
    for close in record["closes"]:
        series = inputs.panel.get(close["symbol"])
        last = next((c for c in reversed(series.c) if c is not None), None) if series else None
        out.append({"symbol": close["symbol"], "close_day": close["close_day"],
                    "sell_price": close["sell_price"], "realized_gain": close["realized_gain"],
                    "last_close": last,
                    "since_sale": None if last is None else last / close["sell_price"] - 1.0})
    return out


# ---------------------------------------------------------------------------
# Part C
# ---------------------------------------------------------------------------

RANKER = "resid_mom_12_1"


class Forms:
    """S5's Leaders with a per-sector limit and inverse-volatility weights."""

    def __init__(self, leaders: ep.Leaders) -> None:
        self.l = leaders

    def picks(self, t: int, t2: int, k: int, cap: int | None) -> list[int]:
        ok, score = self.l.eligible(RANKER, t, t2)
        columns = np.flatnonzero(ok)
        out, count = [], {}
        for j in columns[np.argsort(-score[columns], kind="stable")]:
            sector = self.l.panel.sector[j]
            if cap is not None and count.get(sector, 0) >= cap:
                continue
            out.append(int(j))
            count[sector] = count.get(sector, 0) + 1
            if len(out) == k:
                break
        return out

    def weights(self, t: int, columns: list[int], invvol: bool) -> dict[str, float]:
        symbols = self.l.panel.symbols
        if not invvol:
            return {symbols[j]: 1.0 for j in columns}
        vol = -self.l.panel.F["low_vol_60d"][t - 1]
        raw = {j: (1.0 / vol[j] if np.isfinite(vol[j]) and vol[j] > 0.0 else None) for j in columns}
        known = [w for w in raw.values() if w is not None]
        fallback = sum(known) / len(known) if known else 1.0
        return {symbols[j]: (w if w is not None else fallback) for j, w in raw.items()}

    def schedule(self, lo: int, hi: int, cap: int | None, invvol: bool, m: int | None = None) -> dict:
        panel, out = self.l.panel, {}
        for t, t2 in self.l.periods:
            if not lo <= t <= hi:
                continue
            columns = self.picks(t, t2, ep.LEADERS_HELD_N, cap)
            if m is not None:
                ok, _ = self.l.eligible(RANKER, t, t2)
                chosen: list[int] = []
                index = {s: j for j, s in enumerate(panel.symbols)}
                for j in columns:
                    pool = [panel.symbols[x] for x in np.flatnonzero(ok)
                            if panel.sector[x] == panel.sector[j] and x != j and x not in chosen]
                    peer = bt.pick(pool, hb.SEED, m, t, panel.symbols[j])
                    if peer is not None:
                        chosen.append(index[peer])
                columns = chosen
            out[t] = self.weights(t, columns, invvol)
        return out

    def arm(self, lo: int, hi: int, *, cap: int | None, invvol: bool, fraction: float) -> dict:
        run = lambda schedule: self.l.run(schedule, lo, hi, fraction=fraction, target=ep.LEADERS_TARGET)  # noqa: E731
        mine = run(self.schedule(lo, hi, cap, invvol))
        totals = [run(self.schedule(lo, hi, cap, invvol, m))["total_return"] for m in range(LEADER_CONTROLS)]
        return {"arm": mine,
                "control": {"total_return": float(np.mean(totals)), "p05": float(np.percentile(totals, 5)),
                            "p95": float(np.percentile(totals, 95))},
                "beats_share": sum(1 for x in totals if mine["total_return"] > x) / len(totals),
                "spy_matched": self.l.spy(lo, hi, mine["exposure"] or 0.0)}

    def per_name(self, cap: int | None) -> list[dict]:
        l, panel, rows = self.l, self.l.panel, []
        for t, t2 in l.periods:
            ok, _ = l.eligible(RANKER, t, t2)
            with np.errstate(invalid="ignore", divide="ignore"):
                gross = panel.O[t2] / panel.O[t] - 1.0
            net = {int(j): float(gross[j]) - fp.round_trip_cost(ep._adv(panel.adv[t - 1, j]), l.costs)
                   for j in np.flatnonzero(ok)}
            by_sector: dict[str, list[int]] = {}
            for j in net:
                by_sector.setdefault(panel.sector[j], []).append(j)
            for j in self.picks(t, t2, ep.LEADERS_STAT_N, cap):
                peers = [net[k] for k in by_sector[panel.sector[j]] if k != j]
                if len(peers) < ep.LEADERS_MIN_PEERS:
                    continue
                control = sum(peers) / len(peers)
                rows.append({"symbol": panel.symbols[j], "t": t, "day": l.days[t], "ret": net[j],
                             "control": control, "diff": net[j] - control, "peers": len(peers)})
        return rows

    def sector_counts(self, cap: int | None) -> dict:
        """How concentrated the uncapped picks were: the largest sector's
        count among the seven, by month."""
        worst = []
        for t, t2 in self.l.periods:
            count: dict[str, int] = {}
            for j in self.picks(t, t2, ep.LEADERS_HELD_N, cap):
                count[self.l.panel.sector[j]] = count.get(self.l.panel.sector[j], 0) + 1
            worst.append(max(count.values()) if count else 0)
        return {"months": len(worst), "mean_largest_sector": float(np.mean(worst)),
                "months_over_two": sum(1 for x in worst if x > CAP_HELD), "max": max(worst)}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", action="store_true")
    args = parser.parse_args()
    t0 = time.time()

    inputs, panel2, _, info = fp.load(False)
    study, veto = inputs.survivors, inputs.veto
    stacks = variants(int(inputs.config["earnings_flatten_days"]))
    etfs = bb.etf_panel(inputs, [])
    events = study.events(veto)
    rows, described, after = collect(study, events, stacks, inputs.net, veto, etfs[bb.BROAD])
    print(f"collected {len(rows)} events {time.time() - t0:.0f}s", file=sys.stderr)

    results: dict = {"data": {**info, "events": len(events), "events_with_control": len(rows),
                              "replay_controls": REPLAY_CONTROLS, "leader_controls": LEADER_CONTROLS}}
    results["described"] = described
    results["vs_baseline"] = {
        "control": {v: difference(rows, "control", v, "V0") for v in GATED},
        "dip": {v: difference(rows, "dip", v, "V0") for v in (*GATED, "V6")},
    }
    results["vs_holding"] = {who: {h: difference(rows, who, "V0", h) for h in ("H8", "H21")}
                             for who in ("control", "dip")}
    results["after_exit"] = {who: after_table(after, who) for who in ("control", "dip")}
    results["replays"] = replays(inputs, study, stacks, etfs)
    print(f"replays {time.time() - t0:.0f}s", file=sys.stderr)
    results["decisions"] = decide(results["vs_baseline"]["control"], results["replays"])
    results["live_closes"] = live_closes(inputs) if LIVE_RECORD.exists() else []

    bars = hb.cached("s2_prices.pkl", lambda: {})
    forms = Forms(ep.Leaders(inputs, panel2, bars))
    reserve = 1.0 - float(inputs.config["cash_reserve_pct"])
    last = len(forms.l.days) - 1
    shapes = {"L7 (S5, restated)": (None, False), "L7-cap": (CAP_HELD, False),
              "L7-invvol": (None, True), "L7-cap-invvol": (CAP_HELD, True)}
    leaders_out: dict = {"replays": {}, "concentration": forms.sector_counts(None)}
    for label, (cap, invvol) in shapes.items():
        for size, fraction in (("full", reserve), ("half", 0.5)):
            key = f"{label} | {size}"
            leaders_out["replays"][key] = {}
            for window, (lo_day, _) in REPLAY_WINDOWS.items():
                lo = next(i for i, d in enumerate(forms.l.days) if d >= lo_day)
                leaders_out["replays"][key][window] = forms.arm(lo, last, cap=cap, invvol=invvol, fraction=fraction)
        print(f"leaders {label} {time.time() - t0:.0f}s", file=sys.stderr)
    leaders_out["per_name"] = {"top 10, no cap (S5, restated)": ep.by_window(forms.per_name(None)),
                               "top 10, at most 3 a sector": ep.by_window(forms.per_name(CAP_STAT))}
    capped = leaders_out["per_name"]["top 10, at most 3 a sector"][TEST]["paired"]
    leaders_out["placeable"] = {}
    for label in ("L7-cap", "L7-invvol", "L7-cap-invvol"):
        for size in ("full", "half"):
            x = leaders_out["replays"][f"{label} | {size}"][TEST]
            checks = {"beats_control_mean": x["arm"]["total_return"] > x["control"]["total_return"],
                      "beats_spy_at_matched_exposure": x["arm"]["total_return"] > x["spy_matched"]["total_return"]}
            if "cap" in label:
                checks["capped_per_name_lower_bound_above_zero"] = capped is not None and capped["lo"] > 0.0
            leaders_out["placeable"][f"{label} | {size}"] = {"passes": all(checks.values()), "checks": checks}
    results["leaders"] = leaders_out

    prior = es.ledger_count() - sum(1 for row in es.ledger_rows() if row.get("note") == NOTE)
    trials = ledger_rows(results)
    results["trials"] = {"ledger_before": prior, "this_run": len(trials), "ledger_after": prior + len(trials),
                         "t_needed_bonferroni": es.bonferroni_t(prior + len(trials))}
    (CACHE / "s6_results.json").write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    text = render(results)
    (CACHE / "s6_report.md").write_text(text, encoding="utf-8")
    if args.ledger:
        kept = [row for row in es.ledger_rows() if row.get("note") != NOTE]
        es.LEDGER.unlink(missing_ok=True)
        for row in [*kept, *trials]:
            es.ledger_append(row)
    sys.stdout.reconfigure(encoding="utf-8")
    print(text)
    print(f"done {time.time() - t0:.0f}s", file=sys.stderr)


pct, num, ci, implied_t = ep.pct, ep.num, ep.ci, ep.implied_t


def gap(stat):
    return "too few" if stat is None else f"{pct(stat['diff'])} [{pct(stat['lo'])}, {pct(stat['hi'])}]"


def ledger_rows(r: dict) -> list[dict]:
    today = date.today().isoformat()
    out = []

    def add(variant, n, metric, result):
        out.append({"date": today, "phase": "S6", "variant": variant, "n": n, "metric": metric,
                    "result": result, "note": NOTE})

    paired = "per-entry net return under the variant minus under the baseline, date-clustered bootstrap; fit / test"
    for who, by in r["vs_baseline"].items():
        for v, d in by.items():
            add(f"exit {v} {LABELS[v]} | {who} entries", d[TEST]["n"], paired,
                f"fit {ci(d[FIT]['stat'])}; test {ci(d[TEST]['stat'])}")
    for who, by in r["vs_holding"].items():
        for h, d in by.items():
            add(f"exit baseline vs {h} {LABELS[h]} | {who} entries", d[TEST]["n"],
                "per-entry net return under the baseline minus under the yardstick; fit / test",
                f"fit {ci(d[FIT]['stat'])}; test {ci(d[TEST]['stat'])}")
    for who, by in r["after_exit"].items():
        d = by[TEST]["atr_minus_time"]
        add(f"after exit: ATR_STOP minus TIME_STOP, SPY-adjusted 21 sessions | {who} entries",
            0 if d is None else d["n_in"] + d["n_out"], "difference in means, date-clustered bootstrap; test",
            gap(d))
    for v, by in r["replays"].items():
        for window, x in by.items():
            add(f"exit replay {v} | {window}", x["dip"]["trades"],
                "compounded net total return: DIP vs mean random control vs SPY at the control's exposure",
                f"DIP {pct(x['dip']['total_return'])}; control {pct(x['control']['total_return'])}; "
                f"SPY {pct(x['spy_at_control_exposure'])}")
    for label, by in r["leaders"]["per_name"].items():
        if "restated" in label:
            continue
        add(f"Leaders per name: {label}", by[TEST]["n"],
            "open-to-open monthly net return minus same-sector eligible mean, clustered by month; fit / test",
            f"fit {ci(by[FIT]['paired'])}; test {ci(by[TEST]['paired'])}")
    for label, by in r["leaders"]["replays"].items():
        if "restated" in label:
            continue
        for window, x in by.items():
            add(f"Leaders form {label} | {window}", ep.LEADERS_HELD_N,
                "compounded net total return: arm vs mean random control vs SPY at the arm's exposure",
                f"arm {pct(x['arm']['total_return'])}; control {pct(x['control']['total_return'])}; "
                f"SPY {pct(x['spy_matched']['total_return'])}; beats {x['beats_share']:.0%}")
    return out


def render(r: dict) -> str:
    out = [f"## Data\n\n```json\n{json.dumps(r['data'], indent=1, default=str)}\n```\n"]
    need = r["trials"]["t_needed_bonferroni"]

    out.append("## Each variant, described (control entries, every peer, all windows)\n")
    out.append("| variant | entries | mean net | MAE | MFE | sessions | exits by reason |\n|---|---|---|---|---|---|---|")
    for v, d in r["described"].items():
        out.append(f"| {v} {LABELS[v]} | {d['entries']} | {pct(d['mean_net'])} | {pct(d['mae'])} | {pct(d['mfe'])} | "
                   f"{num(d['held'], 1)} | {d['reasons']} |")

    out.append(f"\n## Variant minus baseline, per entry, net (|t| needed after deflation: {need:.2f})\n")
    for who in ("control", "dip"):
        out.append(f"\n### {who} entries\n")
        out.append("| variant | fit [95% CI] | N | test [95% CI] | N | test t | full [95% CI] |\n|---|---|---|---|---|---|---|")
        for v, d in r["vs_baseline"][who].items():
            out.append(f"| {v} {LABELS[v]} | {ci(d[FIT]['stat'])} | {d[FIT]['n']} | {ci(d[TEST]['stat'])} | "
                       f"{d[TEST]['n']} | {num(implied_t(d[TEST]['stat']), 1)} | {ci(d['full']['stat'])} |")

    out.append("\n## The baseline against simply holding (baseline minus yardstick, per entry, net)\n")
    out.append("| entries | yardstick | fit [95% CI] | test [95% CI] | test t | full [95% CI] |\n|---|---|---|---|---|---|")
    for who, by in r["vs_holding"].items():
        for h, d in by.items():
            out.append(f"| {who} | {h} {LABELS[h]} | {ci(d[FIT]['stat'])} | {ci(d[TEST]['stat'])} | "
                       f"{num(implied_t(d[TEST]['stat']), 1)} | {ci(d['full']['stat'])} |")

    out.append(f"\n## After the exit: the next {AFTER} sessions from the exit price (baseline)\n")
    for who in ("control", "dip"):
        for window in (FIT, TEST, "full"):
            block = r["after_exit"][who][window]
            out.append(f"\n### {who} entries, {window}\n")
            out.append("| exit | events | entries | raw [95% CI] | minus SPY [95% CI] | share up |\n|---|---|---|---|---|---|")
            for reason, c in block["by_reason"].items():
                share = "n/a" if c["share_up"] is None else f"{c['share_up']:.0%}"
                out.append(f"| {reason} | {c['events']} | {c['entries']} | {ci(c['raw'])} | {ci(c['adj'])} | {share} |")
            d = block["atr_minus_time"]
            out.append(f"\nATR_STOP minus TIME_STOP, SPY-adjusted: {gap(d)}"
                       + ("" if d is None else f", t {num(d['t'], 1)}"))

    out.append("\n## Replays: shipped caps, $1,400, 46% fill, net\n")
    out.append("| variant, window | DIP total | DIP max DD | DIP exposure | control mean [5%, 95%] | control max DD | control Sharpe | control exposure | SPY at the control's exposure | SPY max DD |")
    out.append("|---|---|---|---|---|---|---|---|---|---|")
    for v, by in r["replays"].items():
        for window, x in by.items():
            out.append(
                f"| {v} {LABELS[v]}, {window} | {pct(x['dip']['total_return'], 1)} | {pct(x['dip']['max_drawdown'], 1)} | "
                f"{x['dip']['exposure']:.0%} | {pct(x['control']['total_return'], 1)} "
                f"[{pct(x['control_p05'], 1)}, {pct(x['control_p95'], 1)}] | {pct(x['control']['max_drawdown'], 1)} | "
                f"{num(x['control']['sharpe'])} | {x['control']['exposure']:.0%} | "
                f"{pct(x['spy_at_control_exposure'], 1)} | {pct(x['spy_max_drawdown'], 1)} |")

    out.append("\n## Decisions\n")
    for v, d in r["decisions"].items():
        out.append(f"- **{v} {LABELS[v]}: {'recommended' if d['recommended'] else 'not recommended'}**. "
                   + "; ".join(f"{k}: {x}" for k, x in d["checks"].items()))

    if r["live_closes"]:
        out.append("\n## Live DIP closes (cases, not a sample)\n")
        out.append("| symbol | closed | sale price | realised | last cached close | since the sale |\n|---|---|---|---|---|---|")
        for c in r["live_closes"]:
            out.append(f"| {c['symbol']} | {c['close_day']} | {c['sell_price']:.2f} | {c['realized_gain']:+.2f} | "
                       f"{num(c['last_close'])} | {pct(c['since_sale'], 1)} |")

    lead = r["leaders"]
    k = lead["concentration"]
    out.append(f"\n## Leaders monthly, placeable forms\n\nUncapped picks: the largest sector held "
               f"{num(k['mean_largest_sector'], 1)} of seven names on average, more than two in "
               f"{k['months_over_two']} of {k['months']} months, at most {k['max']}.\n")
    out.append("| per name | fit [95% CI] | N | test [95% CI] | N | test t |\n|---|---|---|---|---|---|")
    for label, by in lead["per_name"].items():
        f, t = by[FIT], by[TEST]
        out.append(f"| {label} | {ci(f['paired'])} | {f['n']} | {ci(t['paired'])} | {t['n']} | "
                   f"{num(implied_t(t['paired']), 1)} |")
    out.append("\n| form, window | total | max DD | Sharpe | turnover | exposure | 20% halt reached | control mean [5%, 95%] | beats | SPY at the arm's exposure | SPY max DD |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for label, by in lead["replays"].items():
        for window, x in by.items():
            arm, control = x["arm"], x["control"]
            out.append(
                f"| {label}, {window} | {pct(arm['total_return'], 1)} | {pct(arm['max_drawdown'], 1)} | "
                f"{num(arm['sharpe'])} | {num(arm['turnover'], 1)}x | {arm['exposure']:.0%} | {arm['halt_reached']} | "
                f"{pct(control['total_return'], 1)} [{pct(control['p05'], 1)}, {pct(control['p95'], 1)}] | "
                f"{x['beats_share']:.0%} | {pct(x['spy_matched']['total_return'], 1)} | "
                f"{pct(x['spy_matched']['max_drawdown'], 1)} |")
    out.append("")
    for label, d in lead["placeable"].items():
        out.append(f"- **{label}: {'passes' if d['passes'] else 'does not pass'}**. "
                   + "; ".join(f"{k}: {x}" for k, x in d["checks"].items()))
    t = r["trials"]
    out.append(f"\nTrials: {t['ledger_before']} before, {t['this_run']} this run, {t['ledger_after']} after.")
    return "\n".join(out)


if __name__ == "__main__":
    main()
