"""Fixture builders for guard and sizing tests.

Every builder returns a *permissive* baseline — a context in which the order
would be approved, or the candidate sized — so each test can mutate exactly one
field and name the denial or refusal it expects. A test that fails therefore
points at one rule.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping

from src import cli, regime as regimes

CONFIG = Path(__file__).resolve().parents[1] / "config"

# The real sleeve, not a stand-in. §5.2 fixes it for a decade at a time, so a
# test sleeve would only prove the loader works against a file nobody ships.
ETFS = json.loads((CONFIG / "etfs.json").read_text(encoding="utf-8"))

# The real map, for the same reason. §6.4 puts the surprise→sector heuristic in
# a file so it can be revised without touching code; tests that invented their
# own would stop noticing when the shipped one is edited.
MACRO_MAP = json.loads((CONFIG / "macro_map.json").read_text(encoding="utf-8"))

# The real baseline, for the strongest version of that reason. §1's second
# benchmark is a promise about *this* account on one specific day, and a
# fixture inventory would prove only that the arithmetic runs.
INCEPTION = json.loads((CONFIG / "inception.json").read_text(encoding="utf-8"))


def _merge(base: dict, overrides: Mapping[str, Any]) -> dict:
    out = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


# The shipped config, read once at import and stripped of its `_`-comment keys
# exactly as `cli.load_config` does it in production. Derived rather than
# re-typed: the hand-copied dict this replaced had to be kept in lockstep with
# `config/config.json` by a test, and every value change meant editing two
# files. Now the suite tests exactly what ships, and the only way they can
# disagree is if the file itself is malformed — which `load_config` raises on.
# Tests that want a non-default value still pass it as an override.
SHIPPED_CONFIG = cli.load_config()["config"]


def config(**overrides: Any) -> dict:
    """The live `config/config.json`, with any keys a test names overridden."""
    return _merge(SHIPPED_CONFIG, overrides)


def core_off(**overrides: Any) -> dict:
    """`cli.load_config()` with the core sleeve switched off, for a test whose
    subject is something else.

    The sleeve is on in the shipped file, and on it adds one trigger row and
    one broad ETF to every cycle. A test about discovery, the rotation or the
    regime counts rows and members, and should not have to know that. The ETF
    listing is rebuilt from `config/etfs.json` itself, since `load_config`
    adds the sleeve's ETF to it while the switch is on.
    """
    loaded = cli.load_config()
    etfs = cli._documented(
        json.loads((cli.CONFIG_DIR / cli.CONFIG_FILES["etfs"]).read_text(encoding="utf-8"))
    )
    return {
        **loaded,
        "etfs": etfs,
        "config": {**loaded["config"], "core_sleeve_enabled": False, **overrides},
    }


# §5.2's eleven, read off the shipped file rather than retyped — the same
# reason `ETFS` itself is loaded above.
SECTOR_ETFS = tuple(ETFS["sector"])


def regime_inputs(**overrides: Any) -> dict:
    """A permissive tape: SPY stacked above both MAs, every sector trending.

    `regime.evaluate` reads this to `risk_on` with no sector blocked, so a test
    can move exactly one number and name the state or the drop it expects. The
    numbers are ordinary rather than dramatic on purpose — 20-day realized vol
    at 0.12 is a quiet market, well under the 0.35 ceiling, so a test that
    wants `risk_off` has to say so twice (below the 200-day *and* volatile),
    which is the rule.
    """
    return _merge(
        {
            "spy": {"last": 640.0, "sma50": 625.0, "sma200": 600.0,
                    "rsi14": 58.0, "rv20": 0.12},
            "sectors": {
                symbol: {"last": 100.0, "sma200": 95.0, "rsi14": 55.0}
                for symbol in SECTOR_ETFS
            },
        },
        overrides,
    )


def regime(**overrides: Any) -> dict:
    """The evaluated regime, as `cli.decide` puts it into `ctx["regime"]`."""
    return regimes.evaluate(regime_inputs(**overrides), config()).as_dict()


def cadence_context(**overrides: Any) -> dict:
    """Baseline: 09:40 ET Monday, nothing has run today, the book is empty.

    Everything here comes from ``edge_control`` or a checked-in file. §13
    requires the no-op decision be reachable in one Supabase read, before any
    Robinhood call, so cadence is given no broker state at all.
    """
    return _merge(
        {
            "now": "2026-08-03T13:40:00Z",
            "control": {
                "last_full_cycle": None,
                "last_overnight_cycle": None,
                "new_positions": {"date": None, "count": 0},
                "positions_held": 0,
                "has_whole_share_position": False,
                "unreconciled": False,
                "pending_events": False,
            },
            "calendar": {"closed": [], "early_close": {}},
            "config": config(),
        },
        overrides,
    )


def universe_context(**overrides: Any) -> dict:
    """Baseline: a one-day-old snapshot whose two sources agree, one DIP trigger.

    ``trading_day`` is handed over from ``cadence.decide``'s output rather than
    re-derived here. The session day is a 20:00-ET shift (`cadence.trading_day`)
    and deriving it twice is how the two would eventually disagree by a day at
    the only hour it matters.
    """
    return _merge(
        {
            "trading_day": "2026-08-03",
            "snapshot": {
                "as_of": "2026-08-02",
                "holdings": [
                    {"symbol": "AAPL", "sector": "Information Technology"},
                    {"symbol": "F", "sector": "Consumer Discretionary"},
                    {"symbol": "PFE", "sector": "Health Care"},
                    {"symbol": "XOM", "sector": "Energy"},
                ],
                "cross_check": [
                    {"symbol": "AAPL", "sector": "Information Technology"},
                    {"symbol": "F", "sector": "Consumer Discretionary"},
                    {"symbol": "PFE", "sector": "Health Care"},
                    {"symbol": "XOM", "sector": "Energy"},
                ],
            },
            "etfs": ETFS,
            "positions": [],
            "triggers": [{"symbol": "F", "play_id": "DIP"}],
            "macro_targets": [],
            "config": config(),
        },
        overrides,
    )


def notes(result, code: str | None = None) -> list[str]:
    """Note symbols from a Universe, optionally for one code only."""
    return [n.symbol for n in result.notes if code is None or n.code == code]


def plays_context(**overrides: Any) -> dict:
    """Baseline: a 09:40 full cycle in which `DIP` fires on F and nothing else.

    Every play is live at ``regime_fit`` 1.0 so a test can darken exactly one.
    ``candidates`` is the §5.3 pool `cli.py` assembled from scans, the earnings
    calendar and `edge_events` — plays judges that pool, it does not choose it,
    and one name in it is what a $236 account's day actually looks like.

    F is oversold (RSI 27) and 7% under its 20-day mean while holding its
    200-day, which is §6.2 exactly. The 7% is deliberate: it is `dip` full
    scale × 0.7, so the baseline conviction is a round 0.70.
    """
    return _merge(
        {
            "trading_day": "2026-08-03",
            "cycle": {
                "cycle_type": "full",
                "session": "regular_hours",
                "now": "2026-08-03T13:45:00Z",
                # Inside `cycle`, because that is where `cadence.Cycle.as_dict`
                # puts it and therefore where `plays._sessions_ago` finds it in
                # production. A fixture that only had the top-level copy would
                # exercise the frozen-count fallback in every test and never
                # the path the account actually runs.
                "trading_day": "2026-08-03",
                "entries_allowed": True,
            },
            "calendar": {"closed": [], "early_close": {}},
            "account": {"equity_total": 236.42},
            "control": {"high_water_mark": 236.42},
            "play_state": {
                "PEAD": {"regime_fit": 1.0, "reason": "drift is paying this month"},
                "DIP": {"regime_fit": 1.0, "reason": "range-bound tape"},
                # Day one: §6.3 sizes CATALYST at 0.75x until it has 20 closed
                # trades, and it has none.
                "CATALYST": {"regime_fit": 1.0, "reason": "news flow is live",
                             "closed_trades": 0},
                "MACRO": {"regime_fit": 1.0, "reason": "CPI lands Thursday"},
            },
            "candidates": ["F"],
            "signals": {
                "F": {
                    "rsi": 27.0,
                    "sma_20": 16.00,
                    "sma_50": 15.20,
                    "sma_200": 13.50,
                    "atr": 0.45,
                },
                # The eleven sector SPDRs, each above its own 200-day. A macro
                # target is always in the candidate pool by the time `plays.py`
                # sees it, so production never asks this play for a symbol whose
                # signal is missing — permissive here for the same reason every
                # other baseline in this file is.
                **{
                    symbol: {"sma_200": 90.0}
                    for symbol in (
                        "XLE", "XLF", "XLK", "XLV", "XLI", "XLY", "XLP", "XLU",
                        "XLB", "XLRE", "XLC",
                    )
                },
            },
            "quotes": {
                "F": {"bid": 14.85, "ask": 14.87, "last": 14.88},
                **{
                    symbol: {"bid": 99.95, "ask": 100.05, "last": 100.00}
                    for symbol in (
                        "XLE", "XLF", "XLK", "XLV", "XLI", "XLY", "XLP", "XLU",
                        "XLB", "XLRE", "XLC",
                    )
                },
            },
            "fundamentals": {"F": {"volume": 60_000_000, "average_volume": 55_000_000}},
            "earnings": {
                "F": {
                    "status": "known",
                    "next_report_date": "2026-10-28",
                    "sessions_until_report": 60,
                    "sessions_since_report": None,
                    "eps_surprise_pct": None,
                    "reaction_pct": None,
                }
            },
            "events": {},
            "macro": {"releases": []},
            "macro_map": MACRO_MAP,
            "positions": [],
            "sectors": {"F": "Consumer Discretionary"},
            "config": config(),
        },
        overrides,
    )


def position(**overrides: Any) -> dict:
    """A held one-share F that no §6.5 exit fires on.

    Bought by DIP at 15.00 two sessions ago and now 14.88. With ATR at 0.45 the
    3xATR stop sits at 13.65 and DIP's 6% target at 15.90, the time stop is six
    sessions away and neither price is anywhere near — so a test can move
    exactly one thing and name the exit it expects.

    ``high_since_entry`` is the entry price, which is where the ratchet starts
    and where it stays for a position that has never traded above its basis:
    the 3.5xATR trailing floor is 13.425, below the fixed stop, so
    `protection.stop_price` returns the fixed one and this fixture's resting
    stop is the same 13.65 §6.5 exit 4 evaluates.
    """
    return _merge(
        {
            "symbol": "F",
            "play_id": "DIP",
            "quantity": 1.0,
            "entry_price": 15.00,
            "sessions_held": 2,
            "market_value": 14.88,
            "high_since_entry": 15.00,
        },
        overrides,
    )


def broker_position(**overrides: Any) -> dict:
    """One row of `get_equity_positions`, in the shape the broker returns it.

    **Every numeric field is a string**, exactly as that probe's
    byte-identical `get_equity_orders` JSON shows Robinhood returning them;
    `reconcile._quantity`, `cli._market_value` and `_symbol_swap_findings` all
    `float()` what they read, and a fixture that handed them Python floats
    would prove nothing about the payload production actually sends. Every
    other ctx-shaped `positions` fixture in this file is the derived book, not
    this — see the `fixture-contract-drift` notes on why the two must not be
    confused.

    `market_value` is the one field the probe's own table does not print;
    `cycle-prompt.md` Appendix D names it as carried by this call and
    `cli._market_value` reads it off the broker row, so it is here with the
    same string typing as its neighbours.
    """
    return _merge(
        {
            "symbol": "F",
            "quantity": "1.000000",
            "average_buy_price": "15.000000",
            "market_value": "14.880000",
            "shares_available_for_sells": "1.000000",
            "shares_held_for_sells": "0.000000",
            "intraday_quantity": "0.000000",
        },
        overrides,
    )


def proposals(result, play_id: str | None = None) -> list[str]:
    """Candidate symbols from an EntryResult, optionally for one play only."""
    return [
        c.symbol for c in result.candidates if play_id is None or c.play_id == play_id
    ]


def proposal(result, symbol: str, play_id: str | None = None):
    """The candidate for ``symbol``, or None if no play proposed it."""
    for cand in result.candidates:
        if cand.symbol == symbol and (play_id is None or cand.play_id == play_id):
            return cand
    return None


def skips(result, symbol: str | None = None) -> list[str]:
    """Skip codes from an EntryResult, optionally for one symbol only."""
    return [s.code for s in result.skipped if symbol is None or s.symbol == symbol]


def exited(result, symbol: str):
    """The exit for ``symbol``, or None if the position is being held."""
    for leaving in result.exits:
        if leaving.symbol == symbol:
            return leaving
    return None


def reconcile_context(**overrides: Any) -> dict:
    """Baseline: a 09:40 full cycle in which the broker and the book agree.

    Nothing was placed last cycle, nothing filled this one, and no order is
    open — so every §12.2 class is silent and a test can introduce exactly one
    disagreement.

    The tax lot is the *only* source of the entry basis and the holding period
    (§12.2). Jul 31 and Aug 3 are the two open sessions after acquisition, so
    this position reconciles to ``entry_price`` 15.00 and ``sessions_held`` 2
    — which is exactly ``factories.position()``, the fixture every `plays.py`
    exit test was written against.
    """
    return _merge(
        {
            "cycle": {
                "cycle_type": "full",
                "session": "regular_hours",
                "now": "2026-08-03T13:40:00Z",
                "trading_day": "2026-08-03",
                "reconcile_required": True,
            },
            "calendar": {"closed": [], "early_close": {}},
            "control": {
                "last_full_cycle": "2026-07-31T13:40:00Z",
                "high_water_mark": 236.42,
                "unreconciled": True,
                "entries_blocked_until_cleared": False,
                "divergence_streak": 0,
                "open_order_cycles": {},
            },
            "account": {"equity_total": 236.42},
            "intent": {"placements": []},
            "orders": [],
            "fills": [],
            "positions": [{"symbol": "F", "quantity": 1.0}],
            "prior_positions": [{"symbol": "F", "play_id": "DIP", "quantity": 1.0}],
            "tax_lots": {
                "F": [
                    {
                        "quantity": 1.0,
                        "cost_basis": 15.00,
                        "acquired_date": "2026-07-30",
                    }
                ]
            },
            "events": {},
            "play_state": {"DIP": {"closed_trades": 4}},
            "config": config(),
        },
        overrides,
    )


def placement(**overrides: Any) -> dict:
    """One line of last cycle's runlog: what the plan said it placed."""
    return _merge(
        {
            "order_id": "a1b2",
            "symbol": "F",
            "side": "buy",
            "quantity": 1.0,
            "play_id": "DIP",
        },
        overrides,
    )


def broker_order(**overrides: Any) -> dict:
    """One row of `get_equity_orders` — the broker's side of the same story."""
    return _merge(
        {
            "order_id": "a1b2",
            "symbol": "F",
            "side": "buy",
            "quantity": 1.0,
            "state": "queued",
        },
        overrides,
    )


def fill(**overrides: Any) -> dict:
    """A fill, with the tax-lot basis §9 computes the realised result from."""
    return _merge(
        {
            "order_id": "a1b2",
            "symbol": "F",
            "side": "buy",
            "quantity": 1.0,
            "price": 15.00,
            "cost_basis": None,
        },
        overrides,
    )


def realized(**overrides: Any) -> dict:
    """One `get_pnl_trade_history` row, as `cli._realized` hands it on.

    Internal names, not the broker's — the rename is `cli._realized`'s job and
    `tests/test_cli_realized.py` is where the broker's field names are tested.
    Matched to `factories.fill()` by `order_id`, which is the only key that
    survives two sells of one symbol on one day.
    """
    return _merge(
        {
            "order_id": "a1b2",
            "symbol": "F",
            "quantity": 1.0,
            "price": 14.00,
            "proceeds": 14.00,
            "cost_basis": 15.00,
            "realized_gain": -1.00,
            "date": "2026-08-03",
            # What `broker_shapes.realized` keeps from the live `timestamp`
            # and `side` — 11:00 ET, the 08-03 session.
            "traded_at": "2026-08-03T15:00:00Z",
            "side": "sell",
        },
        overrides,
    )


def found(result, code: str | None = None) -> list[str]:
    """Finding codes from a ReconcileResult, or symbols for one code."""
    if code is None:
        return [f.code for f in result.findings]
    return [f.symbol for f in result.findings if f.code == code]


def reconciled(result, symbol: str):
    """The reconciled position for ``symbol``, or None if it is not held."""
    for held in result.positions:
        if held.symbol == symbol:
            return held
    return None


def candidate(**overrides: Any) -> dict:
    """A candidate F entry: the account's first reachable WHOLE-path name."""
    return _merge(
        {
            "symbol": "F",
            "conviction": 0.70,
            "play_id": "DIP",
            "limit_price": None,
            "session": None,
        },
        overrides,
    )


def order(**overrides: Any) -> dict:
    """A one-share F buy: the account's first reachable WHOLE-path order."""
    return _merge(
        {
            "symbol": "F",
            "side": "buy",
            "type": "limit",
            "quantity": 1.0,
            "limit_price": 15.00,
            "stop_price": None,
            "session": "regular_hours",
            "play_id": "DIP",
        },
        overrides,
    )


def context(**overrides: Any) -> dict:
    """Baseline: market open, kill switch on, one candidate, nothing held."""
    return _merge(
        {
            "cycle": {
                "cycle_type": "full",
                "session": "regular_hours",
                "now": "2026-08-03T13:45:00Z",
                "entries_allowed": True,
            },
            "control": {
                "readable": True,
                "trading_enabled": True,
                "high_water_mark": 236.42,
                "entries_blocked": False,
                "entries_blocked_reason": None,
            },
            "account": {
                "equity_total": 236.42,
                "cash": 100.00,
                "buying_power": 100.00,
                "settled_cash": 100.00,
                "sold_this_session": False,
            },
            "universe": {"snapshot_age_days": 1, "candidates": ["F"]},
            "positions": [],
            "open_orders": [],
            "loss_closes": [],
            "earnings": {
                "F": {
                    "status": "known",
                    "next_report_date": "2026-10-28",
                    "sessions_until_report": 60,
                }
            },
            "tradability": {
                "F": {
                    "tradeable": True,
                    "fractional_tradability": "tradable",
                    "all_day_tradability": "tradable",
                }
            },
            "quotes": {"F": {"bid": 14.42, "ask": 14.87, "last": 14.68}},
            "sizing": {"n_target": 3},
            "sectors": {"F": "Consumer Discretionary"},
            "config": config(),
        },
        overrides,
    )


def codes(result, symbol: str | None = None) -> list[str]:
    """Denial codes from a GuardResult, optionally for one symbol only."""
    out: list[str] = [d.code for d in result.halts]
    for denied in result.denied:
        if symbol is None or denied.order["symbol"] == symbol:
            out.extend(d.code for d in denied.denials)
    return out


def entry(result, symbol: str):
    """The sized entry for ``symbol``, or None if sizing refused it."""
    for sized in result.entries:
        if sized.symbol == symbol:
            return sized
    return None


def reasons(result, symbol: str | None = None) -> list[str]:
    """Refusal reasons from a SizingResult, optionally for one symbol only."""
    return [
        u.reason
        for u in result.unsized
        if symbol is None or u.symbol == symbol
    ]


# The A.4 close for every inception name, plus SPY. Quoting the frozen book at
# the prices it was frozen at makes the baseline a day on which nothing has
# moved, so a test that changes one quote moves exactly one benchmark. An
# earlier closes the inception book was snapshotted at.
INCEPTION_QUOTES = {
    "SPY": {"last": 771.33},
    "ADI": {"last": 380.29},
    "C": {"last": 136.87},
    "CAT": {"last": 876.54},
    "INTC": {"last": 100.86},
    "MRK": {"last": 128.00},
    "MS": {"last": 217.04},
    "MU": {"last": 892.67},
    "SMH": {"last": 575.71},
    "TXN": {"last": 283.63},
}


def report_context(**overrides: Any) -> dict:
    """Equity, SPY and the frozen book are all exactly where inception left them,
    so all three §1 benchmarks read 0.00% and a test that moves one number
    moves one return. Deriving the recorded equity from the same closes the
    book is valued at closed it, and `test_report_benchmark.py` now pins the
    agreement rather than tolerating the gap.

    Thursday is deliberately not the last session of the week, so the scorecard
    stays silent until a test moves the day to Friday, and it is two sessions
    after inception so every return has a denominator. Nothing has failed,
    reconciliation was clean, no play changed, and equity is at its high-water
    mark — every §8.3 alert is therefore off, and a test can switch on one.
    """
    return _merge(
        {
            "cycle": {
                "cycle_type": "postclose",
                "session": "extended_hours",
                "now": "2026-08-06T20:30:00Z",
                "trading_day": "2026-08-06",
                "entries_allowed": False,
            },
            "calendar": {"closed": [], "early_close": {}},
            "control": {
                "trading_enabled": True,
                "high_water_mark": 247.72,
                "last_scorecard_date": None,
                # Nothing writes `trading_enabled` any more,
                # so this is what keeps §10's urgent push to one per halt.
                "last_drawdown_alert": None,
            },
            "account": {"equity_total": 247.72},
            # §14's repeated rejection, decided by `cli.decide` and handed here
            # because §8.3's queue belongs to this module.
            "blocked": [],
            # `decide.entries`/`decide.exits`, verbatim — empty by default so
            # only a test that moves `cycle_type` to `full` and cares about
            # the 09:40 no-activity summary needs to override either.
            "entries": {},
            "exits": {},
            "quotes": INCEPTION_QUOTES,
            # The shipped inventory, deliberately — but never the shipped
            # `deposits`. That list is live operator data: it grows every time
            # the account is funded, and a suite that read it would turn each
            # recorded deposit into eight red tests, which is precisely the
            # manual work recording them automatically is meant to remove. The
            # baseline is "no flows", so a test that cares about one says so.
            "inception": {**INCEPTION, "deposits": []},
            # reconcile ran first and found nothing (§12.1). Its output arrives
            # as the dict it emits, not as a second computation of the same
            # facts — report reads reconciliation, it does not perform it.
            "reconcile": {"findings": [], "positions": []},
            "play_state": {
                "PEAD": {"regime_fit": 1.0, "reason": "drift is paying this month"},
                "DIP": {"regime_fit": 1.0, "reason": "range-bound tape",
                        "closed_trades": 4, "realized_pnl": -1.25},
                "CATALYST": {"regime_fit": 1.0, "reason": "news flow is live",
                             "closed_trades": 0},
                "MACRO": {"regime_fit": 1.0, "reason": "CPI lands Thursday"},
            },
            "prior_play_state": {
                "PEAD": {"regime_fit": 1.0},
                "DIP": {"regime_fit": 1.0},
                "CATALYST": {"regime_fit": 1.0},
                "MACRO": {"regime_fit": 1.0},
            },
            "failure": None,
            "config": config(),
        },
        overrides,
    )


def cli_payload(**overrides: Any) -> dict:
    """One routine invocation's stdin: 09:40 ET Monday, F held, PFE entering.

    The baseline deliberately produces **exactly one order**. F is the position
    every `plays.py` and `reconcile.py` fixture was written against — bought by
    DIP at 15.00, now 14.88, inside its stop, its time limit and its 200-day —
    and it is quiet here at RSI 45 so no §6.5 exit fires and DIP does not
    propose a top-up on it. PFE is the entry: oversold at RSI 27, 7% under its
    20-day mean and holding its 200-day, which is §6.2 exactly and prices to
    one whole share at this rung.

    Note that `positions` and `reconcile.positions` both appear, and that is
    not duplication. `get_equity_positions` supplies the quantity and the
    market value; `reconcile.py` supplies the play, the basis and the holding
    period. Joining them is `cli.py`'s job, and a fixture that carried only one
    would let the join silently stop happening.

    ``config`` and ``calendar`` are absent on purpose — a settled design
    decision puts both in the checkout, and a payload that carried them would
    be testing a path the routine does not have.
    """
    return _merge(
        {
            "cycle_id": "2026-08-03T13:40:00Z-full",
            "now": "2026-08-03T13:40:00Z",
            "control": {
                "readable": True,
                "trading_enabled": True,
                "high_water_mark": 236.42,
                "last_full_cycle": None,
                "last_overnight_cycle": None,
                "new_positions": {"date": None, "count": 0},
                "positions_held": 1,
                "has_whole_share_position": True,
                "unreconciled": False,
                "pending_events": False,
                "entries_blocked": False,
                "entries_blocked_reason": None,
                "entries_blocked_until_cleared": False,
                "divergence_streak": 0,
                "open_order_cycles": {},
                "last_scorecard_date": None,
                "last_drawdown_alert": None,
                "last_data_fresh_at": {"trading_day": None, "fetched_at": None},
            },
            "account": {
                "equity_total": 236.42,
                "cash": 100.00,
                "buying_power": 100.00,
                "settled_cash": 100.00,
                "sold_this_session": False,
            },
            # §5.1's weekly CI fetch, arriving as a file in the checkout
            # rather than as a Supabase round-trip.
            "snapshot": {
                "as_of": "2026-08-02",
                "holdings": [
                    {"symbol": "AAPL", "sector": "Information Technology"},
                    {"symbol": "F", "sector": "Consumer Discretionary"},
                    {"symbol": "PFE", "sector": "Health Care"},
                    {"symbol": "XOM", "sector": "Energy"},
                ],
                "cross_check": [
                    {"symbol": "AAPL", "sector": "Information Technology"},
                    {"symbol": "F", "sector": "Consumer Discretionary"},
                    {"symbol": "PFE", "sector": "Health Care"},
                    {"symbol": "XOM", "sector": "Energy"},
                ],
            },
            "triggers": [{"symbol": "PFE", "play_id": "DIP"}],
            "positions": [
                {"symbol": "F", "quantity": 1.0, "market_value": 14.88},
            ],
            "prior_positions": [{"symbol": "F", "play_id": "DIP", "quantity": 1.0}],
            "reconcile": {
                "findings": [],
                "cancels": [],
                "positions": [
                    {
                        "symbol": "F",
                        "play_id": "DIP",
                        "quantity": 1.0,
                        "entry_price": 15.00,
                        "sessions_held": 2,
                        # `reconcile.Position.as_dict` emits this on every row,
                        # so a payload fixture that omitted it would be one
                        # field narrower than anything production hands over.
                        # False here: F is a position DIP chose to open, so
                        # there is no adoption day to count a clock from.
                        "adopted": False,
                        "adopted_on": None,
                        # The trailing stop's high-water price, on the row for
                        # the same reason `adopted` is: `reconcile.Position`
                        # emits it every cycle. 15.00 is the entry, which is
                        # where the ratchet starts and where it stays for a
                        # position that has never traded above its basis — so
                        # `protection.stop_price`'s `max` keeps the fixed stop.
                        "high_since_entry": 15.00,
                    }
                ],
                # What `reconcile._protection_plan` hands `decide` for this
                # book: nothing rests at the broker (`open_orders` is empty
                # below), so F's one order is new. Written out rather than
                # derived because that is what the whole `reconcile` block here
                # is — the previous command's stdout, as the routine merges it
                # into `cycle.json`.
                #
                # 13.65 is 3xATR under a 15.00 entry, which beats the 13.425 the
                # 3.5xATR trailing floor gives from a high that is still the
                # entry. The 15.90 target that used to sit beside it was removed:
                # the broker reserves the whole position against the first resting
                # sell, so only one leg rests (`protection_rest_kind`).
                #
                # **`cli.decide` never recomputes this from config** — it
                # trusts `payload.reconcile.protection_plan` verbatim
                # (`cli._protection_orders`), the same way it trusts every
                # other `reconcile` field. This row is therefore fixed test
                # input for `decide`'s placement/blocking mechanics
                # (`tests/test_cli_protection.py`, `test_cli_golden.py`)
                # regardless of what `protection_rest_kind` currently ships as
                # — it is not a claim about what `plays.protection.desired`
                # would compute today (`"none"`; see
                # `tests/test_plays_protection.py::test_none_rests_nothing_at_all`
                # and `tests/test_reconcile_protection.py` for that).
                "protection_plan": {
                    "place": [
                        {
                            "symbol": "F",
                            "side": "sell",
                            "type": "stop_market",
                            "quantity": 1.0,
                            "limit_price": None,
                            "stop_price": 13.65,
                            "session": "regular_hours",
                            "time_in_force": "gtc",
                            "play_id": "DIP",
                            "protective_stop": True,
                            "protective_target": False,
                        },
                    ],
                    "replace": [],
                    "keep": [],
                    "notes": [],
                },
            },
            "open_orders": [],
            "orders": [],
            "fills": [],
            "intent": {"placements": []},
            # Raw broker shape (`get_equity_tax_lots`), not `reconcile.py`'s —
            # `broker_shapes.tax_lots` does the rename, and this fixture
            # exercises that path rather than assuming it already happened.
            "tax_lots": {
                "F": [
                    {"quantity": 1.0, "tax_cost_basis": 15.00, "open_date": "2026-07-30"}
                ]
            },
            "quotes": {
                "F": {"bid": 14.85, "ask": 14.87, "last": 14.88},
                "PFE": {"bid": 25.10, "ask": 25.13, "last": 25.11},
            },
            "signals": {
                # Quiet: above its 200-day, not oversold, nowhere near RSI 50.
                "F": {
                    "rsi": 45.0,
                    "sma_20": 15.10,
                    "sma_50": 15.20,
                    "sma_200": 13.50,
                    "atr": 0.45,
                },
                # §6.2: oversold, holding the 200-day, 7% under the 20-day mean
                # — `DIP_DEPTH_FULL_SCALE` x 0.7, so conviction is a round 0.70.
                "PFE": {
                    "rsi": 27.0,
                    "sma_20": 27.00,
                    "sma_50": 25.90,
                    "sma_200": 22.00,
                    "atr": 0.50,
                },
            },
            # **The broker's shape, not the derived one.** `get_earnings_results`
            # returns quarters; `cli._earnings` reduces them to the fields guard
            # and plays read. A fixture carrying the derived shape is what let
            # every test pass while production vetoed every entry — the payload
            # asserted a contract nothing in `src/` implemented. Both names
            # report far out, so neither is inside the flatten window and the
            # baseline cycle is unaffected by the earnings gate.
            "earnings": {
                "F": {
                    "data": {
                        "results": [
                            {
                                "symbol": "F",
                                "year": 2026,
                                "quarter": 2,
                                "eps": {"estimate": "0.470000", "actual": "0.510000"},
                                "report": {
                                    "date": "2026-07-23",
                                    "timing": "pm",
                                    "verified": True,
                                },
                            },
                            {
                                "symbol": "F",
                                "year": 2026,
                                "quarter": 3,
                                "eps": {"estimate": "0.360000", "actual": None},
                                "report": {
                                    "date": "2026-10-28",
                                    "timing": "pm",
                                    "verified": False,
                                },
                            },
                        ]
                    }
                },
                "PFE": {
                    "data": {
                        "results": [
                            {
                                "symbol": "PFE",
                                "year": 2026,
                                "quarter": 2,
                                "eps": {"estimate": "0.610000", "actual": "0.620000"},
                                "report": {
                                    "date": "2026-07-28",
                                    "timing": "am",
                                    "verified": True,
                                },
                            },
                            {
                                "symbol": "PFE",
                                "year": 2026,
                                "quarter": 3,
                                "eps": {"estimate": "0.590000", "actual": None},
                                "report": {
                                    "date": "2026-11-04",
                                    "timing": "am",
                                    "verified": False,
                                },
                            },
                        ]
                    }
                },
            },
            "tradability": {
                "F": {
                    "tradeable": True,
                    "fractional_tradability": "tradable",
                    "all_day_tradability": "tradable",
                },
                "PFE": {
                    "tradeable": True,
                    "fractional_tradability": "tradable",
                    "all_day_tradability": "tradable",
                },
            },
            "events": {},
            "macro": {"releases": []},
            "play_state": {
                "PEAD": {"regime_fit": 1.0, "reason": "drift is paying this month"},
                "DIP": {"regime_fit": 1.0, "reason": "range-bound tape",
                        "closed_trades": 4, "realized_pnl": -1.25},
                "CATALYST": {"regime_fit": 1.0, "reason": "news flow is live",
                             "closed_trades": 0},
                "MACRO": {"regime_fit": 1.0, "reason": "CPI lands Thursday"},
            },
            "prior_play_state": {
                "PEAD": {"regime_fit": 1.0},
                "DIP": {"regime_fit": 1.0},
                "CATALYST": {"regime_fit": 1.0},
                "MACRO": {"regime_fit": 1.0},
            },
            "loss_closes": [],
            # `get_equity_fundamentals`, fetched only for adopted non-members
            # the issuer holdings file does not carry.
            "sectors": {},
            # §14's "same rejection 2 cycles running", read back off the day's
            # runlog. Never stored as state.
            "rejections": [],
            "failure": None,
        },
        overrides,
    )


def planned(result, side: str | None = None) -> list[str]:
    """Symbols in the plan's order list, optionally for one side only."""
    return [
        o["symbol"]
        for o in result["plan"]["orders"]
        if side is None or o["side"] == side
    ]


NOTHING_TO_PROTECT = {
    "protection_plan": {"place": [], "replace": [], "keep": [], "notes": []}
}
"""A `reconcile` override for a payload that must place *nothing*.

`cli_payload`'s baseline book earns a resting stop and a resting target on
every cycle (§10.1), so "this cycle placed no order" is no longer
the same statement as "this cycle proposed no entry and no exit". A test about
the second has to say so, and this is how it says it.
"""


def with_inception(ctx: dict, **fields: Any) -> dict:
    """Replace ``inception`` outright rather than merging onto the real file.

    ``_merge`` deep-merges, so ``report_context(inception={"inventory": {...}})``
    would leave all ten shipped names in place and add to them. Every test that
    wants a book it can price by hand needs this instead.
    """
    return {**ctx, "inception": dict(fields)}


def alerted(result, title: str | None = None) -> list[str]:
    """Alert titles from a ReportResult, or bodies for one title."""
    if title is None:
        return [a.title for a in result.alerts]
    return [a.body for a in result.alerts if a.title == title]
