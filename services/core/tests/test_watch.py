"""`services/core/core/watch.py` — the 30-minute state-change gate.

Acceptance: a held whole-share
position with no `edge_protection` row, or one whose live quote lands within
`watch_band_pct` of its stop or target, sets `edge_control.needs_session`; a
quiet book (protection covers everything, no quote near a band, no resubmit,
no regime crossing) leaves it untouched and reports `needs_session: false`.
"""

from __future__ import annotations

from datetime import datetime

from services.core.core import watch
from services.core.core.watch import run_watch
from services.core.core.twelvedata import TwelveData

ET_NOW = datetime.fromisoformat("2026-09-11T15:00:00-04:00")  # 15:00 ET


class RoutedSupabase:
    """Answers `select` from a table -> rows map; records every `rpc` call."""

    def __init__(self, rows: dict[str, list] | None = None):
        self.rows = rows or {}
        self.calls: list[tuple[str, dict]] = []

    def rpc(self, function: str, params=None):
        self.calls.append((function, dict(params or {})))
        return None

    def select(self, table: str, query: str) -> list[dict]:
        return list(self.rows.get(table, []))

    def args(self, function: str) -> list[dict]:
        return [p for n, p in self.calls if n == function]

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]


class FakeFetcher:
    """`/price` only, by symbol."""

    def __init__(self, prices: dict[str, float]):
        self.prices = prices

    def get(self, url: str) -> tuple[int, bytes]:
        import json

        symbols = url.split("symbol=")[1].split("&")[0].split(",")
        if len(symbols) == 1:
            body = {"price": str(self.prices.get(symbols[0], 0))}
        else:
            body = {s: {"price": str(self.prices.get(s, 0))} for s in symbols}
        return 200, json.dumps(body).encode()


def _book(*positions: dict) -> dict:
    return {"edge_runlog": [{"book": {"positions": list(positions)}}]}


def _td(prices: dict[str, float]) -> TwelveData:
    return TwelveData("KEY", FakeFetcher(prices), sleep=lambda _s: None)


def _position(symbol: str, quantity: float = 5.0) -> dict:
    return {"symbol": symbol, "quantity": quantity}


def _protection_row(symbol: str, kind: str, price: float) -> dict:
    return {"symbol": symbol, "kind": kind, "price": price}


# ---------------------------------------------------------------------------
# Quiet cases
# ---------------------------------------------------------------------------


def test_no_positions_held_is_quiet_and_touches_nothing():
    supabase = RoutedSupabase({"edge_runlog": [{"book": {"positions": []}}]})

    result = run_watch(supabase, _td({}), now=ET_NOW)

    assert result == {
        "status": "ok", "trading_day": "2026-09-11",
        "needs_session": False, "checked": 0,
    }
    assert "edge_control_apply" not in supabase.names()


def test_protection_covers_the_position_and_price_is_nowhere_near_a_band():
    rows = _book(_position("MGM"))
    rows["edge_protection"] = [
        _protection_row("MGM", "stop", 40.0),
        _protection_row("MGM", "target", 46.0),
    ]
    supabase = RoutedSupabase(rows)

    result = run_watch(supabase, _td({"MGM": 43.0, "SPY": 600.0}), now=ET_NOW)

    assert result["needs_session"] is False
    assert result["checked"] == 1
    assert "edge_control_apply" not in supabase.names()


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


def test_a_quote_within_the_band_of_its_stop_sets_needs_session():
    rows = _book(_position("MGM"))
    rows["edge_protection"] = [_protection_row("MGM", "stop", 40.0)]
    supabase = RoutedSupabase(rows)

    # 40.30 is 0.75% above the 40.00 stop, inside the default 1% band.
    result = run_watch(supabase, _td({"MGM": 40.30, "SPY": 600.0}), now=ET_NOW)

    assert result["needs_session"] is True
    assert any(r["reason"] == "near_stop" and r["symbol"] == "MGM" for r in result["reasons"])
    updates = supabase.args("edge_control_apply")[0]["p_updates"]["needs_session"]
    assert updates["symbols"] == ["MGM"]
    assert "near_stop" in updates["reason"]
    assert updates["at"] == "2026-09-11T15:00:00-04:00"


def test_a_whole_share_position_with_no_resting_protection_is_flagged():
    rows = _book(_position("MGM"))
    rows["edge_protection"] = []
    supabase = RoutedSupabase(rows)

    result = run_watch(supabase, _td({"MGM": 43.0, "SPY": 600.0}), now=ET_NOW)

    assert result["needs_session"] is True
    assert any(r["reason"] == "missing_resting_protection" for r in result["reasons"])


def test_a_fractional_position_is_never_checked_for_missing_protection():
    rows = _book(_position("MGM", quantity=0.5))
    rows["edge_protection"] = []
    supabase = RoutedSupabase(rows)

    result = run_watch(supabase, _td({"MGM": 43.0, "SPY": 600.0}), now=ET_NOW)

    assert result["needs_session"] is False


def test_an_entry_resubmitted_today_sets_needs_session():
    rows = {
        "edge_runlog": [{"book": {"positions": []}}],
        "edge_control": [{
            "key": "resubmit_candidates",
            "payload": {
                "trading_day": "2026-09-11",
                "triggers": [{"symbol": "AMD"}],
            },
        }],
    }
    supabase = RoutedSupabase(rows)

    result = run_watch(supabase, _td({}), now=ET_NOW)

    assert result["needs_session"] is True
    assert any(r["reason"] == "entry_resubmitted" and r["symbol"] == "AMD"
               for r in result["reasons"])


def test_a_resubmit_from_a_stale_trading_day_is_ignored():
    rows = {
        "edge_runlog": [{"book": {"positions": []}}],
        "edge_control": [{
            "key": "resubmit_candidates",
            "payload": {
                "trading_day": "2026-09-10",
                "triggers": [{"symbol": "AMD"}],
            },
        }],
    }
    supabase = RoutedSupabase(rows)

    result = run_watch(supabase, _td({}), now=ET_NOW)

    assert result["needs_session"] is False


def test_spy_crossing_below_its_200_day_disagrees_with_a_risk_on_regime():
    rows = _book(_position("MGM"))
    rows["edge_protection"] = [
        _protection_row("MGM", "stop", 40.0),
        _protection_row("MGM", "target", 46.0),
    ]
    rows["edge_day_context"] = [{
        "market_data": {"SPY": {"signals": {"sma_200": 590.0}}},
    }]
    rows["edge_regime"] = [{"payload": {"regime": {"market_state": "risk_on"}}}]
    supabase = RoutedSupabase(rows)

    # SPY quoting below its own 200-day while the last computed regime still
    # says risk_on — the gate should not wait for the next full cycle to notice.
    result = run_watch(supabase, _td({"MGM": 43.0, "SPY": 585.0}), now=ET_NOW)

    assert result["needs_session"] is True
    assert any(r["reason"] == "regime_threshold_crossed" for r in result["reasons"])


def test_a_failed_read_fails_closed_not_open():
    class Boom:
        def rpc(self, *a, **kw):
            raise RuntimeError("db unreachable")

        def select(self, *a, **kw):
            raise RuntimeError("db unreachable")

    result = run_watch(Boom(), _td({}), now=ET_NOW)

    assert result["status"] == "failed"
    assert "db unreachable" in result["detail"]
