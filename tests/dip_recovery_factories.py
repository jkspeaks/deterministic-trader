"""Fixture builders for `src.dip_recovery` tests.

Every builder returns a *permissive* baseline the way `tests/factories.py`'s
own docstring describes: a context in which DIP1 would enter (or, once held,
would sit quietly doing nothing), so a test can move exactly one field and
name the outcome it expects.
"""

from __future__ import annotations

import copy
from typing import Any, Mapping

from tests.factories import config as core_config


def _merge(base: dict, overrides: Mapping[str, Any]) -> dict:
    out = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def dip_recovery_config(**overrides: Any) -> dict:
    """The typed shape `edge_dip_recovery_config_json()` returns."""
    return _merge(
        {
            "core_strategies_enabled": True,
            "dip_recovery_enabled": True,
            "max_hold_trading_days": 47,
            "scan_interval_minutes": 90,
            "entry_thresholds": {
                "min_dip_pct_intraday": 2.0,
                "min_dip_pct_5day": 4.0,
                "rsi_max": 35,
                "relvol_min": 1.2,
                "dip_score_min": 70,
                "bollinger_period": 20,
                "bollinger_k": 2.0,
                "trend_band_pct": 0.10,
                "freefall_drop_pct": 15.0,
                "freefall_window_sessions": 10,
            },
            "sizing_thresholds": {
                "max_active_positions": 5,
                "max_new_entries_per_day": 3,
                "max_fills_before_stop_averaging": 4,
                "average_down_trigger_pct": -3.0,
                "average_down_min_price_drop_pct": 2.5,
                "average_down_min_minutes_since_fill": 30,
            },
            "exit_thresholds": {"profit_target_pct": 5.0, "cost_buffer_pct": 0.25},
            "universe_thresholds": {
                "watchlist_size": 5,
                "min_dollar_volume": 20_000_000,
                "min_atr_pct": 1.5,
                "max_atr_pct": 8.0,
                "max_scan_survivors": 30,
            },
        },
        overrides,
    )


def pool_context(**overrides: Any) -> dict:
    """Baseline for `watchlist.pool`: a fresh universe of four members, a scan
    that hit two of them, nothing locked, held or recently closed at a loss —
    so both scan hits survive to the fetch list and a test can move one field.
    """
    return _merge(
        {
            "cycle": {
                "cycle_type": "premarket",
                "session": "pre_market",
                "now": "2026-08-31T11:50:00Z",
                "trading_day": "2026-08-31",
            },
            "config": core_config(),
            "dip_recovery_config": dip_recovery_config(),
            "universe": {
                "members": ["DIP1", "DIP2", "DIP3", "CORE1"],
                "stale": False,
                "snapshot_age_days": 1,
            },
            "scan": ["DIP1", "DIP2"],
            "symbol_lock": {},
            "dip_recovery_positions": {},
            "positions": [],
            "loss_closes": [],
        },
        overrides,
    )


def ranked_symbol(**overrides: Any) -> dict:
    """One symbol's per-symbol reads for `watchlist.pick`, flattened: the quote,
    the ATR off `get_equity_technical_indicators`, the average volume off
    `get_equity_fundamentals` and a confirmed earnings date well outside the
    flatten window. $50m of daily turnover and a 3% ATR both sit inside the
    default bands.
    """
    return _merge(
        {
            "last": 50.00,
            "atr": 1.50,
            "average_volume": 1_000_000,
            "sessions_until_report": 20,
        },
        overrides,
    )


def pick_context(symbols: Mapping[str, Any] | None = None, **overrides: Any) -> dict:
    """Baseline for `watchlist.pick`, assembled from a `{symbol: ranked_symbol}`
    map so a test writes what it means about one symbol rather than reaching
    into four parallel dicts to say it.
    """
    rows = dict(symbols or {"DIP1": ranked_symbol(), "DIP2": ranked_symbol(atr=2.00)})
    return _merge(
        {
            "cycle": {
                "cycle_type": "premarket",
                "session": "pre_market",
                "now": "2026-08-31T11:50:00Z",
                "trading_day": "2026-08-31",
            },
            "config": core_config(),
            "dip_recovery_config": dip_recovery_config(),
            "fetch": list(rows),
            "quotes": {s: {"last": r["last"]} for s, r in rows.items()},
            "signals": {s: {"atr": r["atr"]} for s, r in rows.items()},
            "fundamentals": {s: {"average_volume": r["average_volume"]} for s, r in rows.items()},
            "earnings": {
                s: {"status": "known", "sessions_until_report": r["sessions_until_report"]}
                for s, r in rows.items()
            },
        },
        overrides,
    )


def dip1_bars(n: int = 20) -> list[dict]:
    """`n` daily bars for DIP1, oldest first, in the shape
    `ctx["dip_recovery_bars"][symbol]` — every high 53.00, closes alternating
    48.50/51.50 (mean 50.00, population stdev 1.50, so the lower Bollinger
    Band sits at 50.00 - 2 * 1.50 = 47.00). DIP1's 45.95 last trade is then
    13.3% below the 53.00 intraday and 5-day highs and 1.05 under the lower
    band — every phase-5 bar gate clears with margin.
    """
    return [
        {"high": 53.00, "low": 47.00, "close": 48.50 if i % 2 == 0 else 51.50}
        for i in range(n)
    ]


def dip_recovery_context(**overrides: Any) -> dict:
    """Baseline: a 90-minute Dip Monitor tick, DIP1 qualifies to enter, nothing
    else is held or on the watchlist.

    DIP1 at RSI 20 (below the 35 gate), 1.5x relative volume (above the 1.2x
    floor), 8.1% below its 20-day mean, above its 200-day, 13.3% below both
    its intraday and 5-day highs and under its lower Bollinger Band (see
    `dip1_bars`) — every hard gate clears and the score comes to 71.4, above
    the 70 floor.

    **Since phase 7 this is also a guard-clearing baseline**, because
    `engine.evaluate` runs `guard.evaluate` itself. Four of guard's categories
    fail *closed* on a missing key, so a context that merely omitted them would
    make every one of these tests assert on an empty order list for a reason
    that had nothing to do with what the test was about:

    * `earnings` — absent is `EARNINGS_DATE_UNAVAILABLE`, not "no earnings soon"
    * `tradability` — absent is `NOT_TRADABLE`
    * `sectors` — absent is `SECTOR_UNKNOWN`
    * `positions` + `dip_recovery_config.sizing_thresholds` — the slot and
      cap arithmetic §4.4 measures against

    `mc_check` is deliberately **not** here, and must not be added: `guard.mc`
    skips itself entirely when the key is absent but denies every buy when it
    is present and empty. Dip Recovery runs no Monte Carlo pass.
    """
    return _merge(
        {
            "cycle": {
                "cycle_type": "monitor",
                "session": "regular_hours",
                "now": "2026-08-31T15:47:00Z",
                "trading_day": "2026-08-31",
            },
            "calendar": {"closed": [], "early_close": {}},
            "config": {
                **core_config(),
                "settled_cash_policy": "buying_power_is_settled",
            },
            "control": {"readable": True, "trading_enabled": True, "high_water_mark": 5000.00},
            "account": {
                "equity_total": 5000.00,
                "buying_power": 2000.00,
                "settled_cash": 2000.00,
            },
            "quotes": {"DIP1": {"bid": 45.90, "ask": 46.00, "last": 45.95}},
            "signals": {
                "DIP1": {"rsi": 20.0, "sma_20": 50.0, "sma_200": 40.0},
            },
            "fundamentals": {"DIP1": {"volume": 1_500_000, "average_volume": 1_000_000}},
            "dip_recovery_config": dip_recovery_config(),
            "dip_recovery_positions": {},
            "dip_recovery_watchlist": ["DIP1"],
            "dip_recovery_fills": [],
            "dip_recovery_bars": {"DIP1": dip1_bars()},
            "symbol_lock": {},
            # `guard` reads all of these. `strategy` in particular: without it
            # every category takes its core branch and a Dip Recovery buy is
            # refused for not being in core's scan (phase 4.5's own bug).
            "strategy": "dip_recovery",
            "positions": [],
            "sectors": {"DIP1": "Technology"},
            "earnings": {"DIP1": {"status": "known", "sessions_until_report": 20}},
            "tradability": {
                "DIP1": {
                    "tradeable": True,
                    "all_day_tradability": "tradable",
                    "fractional_tradability": "tradable",
                }
            },
            "loss_closes": [],
            "open_orders": [],
        },
        overrides,
    )


def dip1_earnings_raw(report_date: str = "2026-11-20") -> dict:
    """`get_earnings_results` for DIP1, **in the broker's own envelope**.

    `dip_recovery_context` is a ctx and may hold the derived earnings shape;
    a `cycle.json` payload may not, because `cli._earnings` is what does the
    deriving and a payload that arrived pre-derived asserts a contract nothing
    in `src/` implements. That is an earlier defect exactly — every test
    passing while production vetoed every entry — and it caught this build too:
    the first run of the phase-7 cli tests denied DIP1
    `EARNINGS_DATE_UNAVAILABLE` because the payload carried the ctx shape.
    """
    return {
        "data": {
            "results": [
                {
                    "symbol": "DIP1",
                    "year": 2026,
                    "quarter": 2,
                    "eps": {"estimate": "0.400000", "actual": "0.380000"},
                    "report": {"date": "2026-07-21", "timing": "pm", "verified": True},
                },
                {
                    "symbol": "DIP1",
                    "year": 2026,
                    "quarter": 3,
                    "eps": {"estimate": "0.410000", "actual": None},
                    "report": {"date": report_date, "timing": "pm", "verified": False},
                },
            ]
        }
    }


def dip_payload(**overrides: Any) -> dict:
    """One Dip Monitor tick's `cycle.json`, as the routine actually sends it.

    The same permissive baseline as `dip_recovery_context`, with every key that
    `cli.dip_recovery_decide` *derives* replaced by what the broker returns —
    today that is `earnings` alone; `tradability`, `sectors` and `quotes` are
    already passed through unchanged.
    """
    return _merge(
        {**dip_recovery_context(), "earnings": {"DIP1": dip1_earnings_raw()}},
        overrides,
    )


def held_position(**overrides: Any) -> dict:
    """One `dip_recovery_positions` row: DIP1, one fill, HOLDING, no target."""
    return _merge(
        {
            "symbol": "DIP1",
            "state": "HOLDING",
            "opened_trading_day": "2026-08-24",
            "clock_trading_day": "2026-08-24",
            "weighted_avg_cost": 46.00,
            "total_quantity": 10.0,
            "total_fills": 1,
            "latest_fill_at": "2026-08-24T14:00:00Z",
            "fills": [{"trading_day": "2026-08-24", "filled_at": "2026-08-24T14:00:00Z", "price": 46.00, "quantity": 10.0, "side": "buy"}],
        },
        overrides,
    )


def fill(**overrides: Any) -> dict:
    return _merge(
        {
            "symbol": "DIP1",
            "side": "buy",
            "quantity": 10.0,
            "price": 46.00,
            "trading_day": "2026-08-24",
            "filled_at": "2026-08-24T14:00:00Z",
        },
        overrides,
    )


def orders(result, side: str | None = None) -> list[str]:
    return [o.symbol for o in result.orders if side is None or o.side == side]


def skip_codes(result, symbol: str | None = None) -> list[str]:
    return [s.code for s in result.skipped if symbol is None or s.symbol == symbol]
