"""The Manual Check's tool responses -> `cycle.json`, with no script in between.

    uv run python -m src.manual_assemble inputs > cycle.json

The same idea and the same discipline as `src/dip_assemble.py`, which this borrows
its readers from: **the session writes each tool response to a file exactly as the
tool returned it, and this module does every rename, trim and merge.** Nothing is
retyped into code. See that module's docstring for an earlier incident that made the
rule.

The difference is the subject. Dip Monitor's rows come from a Supabase table
the strategy keeps; the Manual Check looks at **the real book**, so each row is
built here from what the broker says is held:

* `positions` -> quantity and `average_buy_price` (the cost the app shows);
* `taxlots.<SYMBOL>` -> the newest lot's `open_date`, which is the T+1 clock,
  and how many lots there are, which stands in for the fill count;
* `fills` / `orders` -> the newest buy's execution time and price, so the
  minutes-since-fill and the drop-from-last-fill gates have real figures.

## The input directory

| kind | what goes in it | required |
|---|---|---|
| `seed` | `{"cycle_id": ..., "now": ...}` | yes |
| `state` | the one Supabase read | yes |
| `portfolio` | `get_portfolio` | yes |
| `accounts` | `get_accounts` | yes |
| `positions` | `get_equity_positions` | yes |
| `taxlots` | `get_equity_tax_lots`, one file per symbol, `taxlots.<SYMBOL>.json` | |
| `quotes` | `get_equity_quotes` | |
| `tradability` | `get_equity_tradability` | |
| `fundamentals` | `get_equity_fundamentals` | |
| `earnings` | `get_earnings_results`, `earnings.<SYMBOL>.json` | |
| `orders` | `get_equity_orders`, the `confirmed` and the `created_at_gte` call | |
| `fills` | `get_equity_orders state=filled` | |

## Fail closed, per symbol

A held symbol with no tax lots has no T+1 clock. It is **left out of the
rows** and named under `excluded`, so it is neither sold nor averaged on a
guess. A missing quote, tradability or earnings answer leaves that symbol's
orders for guard to refuse (`ORDER_NOT_PRICEABLE`, `NOT_TRADABLE`,
`EARNINGS_DATE_UNAVAILABLE`), the same way a forgotten fetch always has.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from src import broker_shapes, cadence, cli
from src.dip_assemble import (
    EXIT_BAD_INPUT,
    EXIT_OK,
    EXIT_USAGE,
    INPUTS_INCOMPLETE,
    SNAPSHOT,
    UNKNOWN_KIND,
    AssemblyError,
    _data,
    _earnings,
    _moment,
    _responses,
    _single,
    _state,
    _symbol_rows,
)

COMMAND = "manual-assemble"

REQUIRED = ("seed", "state", "portfolio", "accounts", "positions")
OPTIONAL = (
    "taxlots",
    "quotes",
    "tradability",
    "fundamentals",
    "earnings",
    "orders",
    "fills",
)
KINDS = frozenset(REQUIRED + OPTIONAL)


def read_inputs(directory: Path) -> dict[str, list[tuple[str, Any]]]:
    """`{kind: [(file name, parsed JSON), ...]}`, files in name order."""
    if not directory.is_dir():
        raise AssemblyError(INPUTS_INCOMPLETE, f"{directory} is not a directory")

    found: dict[str, list[tuple[str, Any]]] = {}
    for path in sorted(directory.glob("*.json")):
        kind = path.name.split(".", 1)[0]
        if kind not in KINDS:
            raise AssemblyError(
                UNKNOWN_KIND,
                f"{path.name}: {kind!r} is not one of {', '.join(sorted(KINDS))}",
            )
        try:
            parsed = json.loads(path.read_text(encoding="utf-8-sig"))
        except ValueError as exc:
            raise AssemblyError(cli.BAD_INPUT, f"{path.name}: {exc}") from exc
        found.setdefault(kind, []).append((path.name, parsed))

    missing = [kind for kind in REQUIRED if kind not in found]
    if missing:
        raise AssemblyError(
            INPUTS_INCOMPLETE,
            f"no {', '.join(k + '.json' for k in missing)} in {directory}",
        )
    return found


def _positions(responses: Sequence[Any]) -> list[dict]:
    """Every held position, quantity above zero, one row per symbol."""
    out: dict[str, dict] = {}
    for response in responses:
        for row in broker_shapes.positions(_data(response)):
            symbol = row.get("symbol")
            try:
                quantity = float(row.get("quantity"))
            except (TypeError, ValueError):
                continue
            if isinstance(symbol, str) and quantity > 0:
                out[symbol] = dict(row)
    return [out[symbol] for symbol in sorted(out)]


def _lots(files: Sequence[tuple[str, Any]]) -> dict[str, list[dict]]:
    """`{symbol: [lot, ...]}` from the per-symbol `get_equity_tax_lots` files."""
    out: dict[str, list[dict]] = {}
    for name, parsed in files:
        for response in parsed if isinstance(parsed, list) else [parsed]:
            data = _data(response)
            if not isinstance(data, Mapping):
                continue
            symbol = data.get("symbol")
            if not isinstance(symbol, str):
                parts = name.split(".")
                symbol = parts[1].upper() if len(parts) >= 3 and parts[1] else None
            if not symbol:
                raise AssemblyError(
                    INPUTS_INCOMPLETE,
                    f"{name}: a tax-lot file is named taxlots.<SYMBOL>.json",
                )
            out.setdefault(symbol, []).extend(
                dict(lot) for lot in data.get("tax_lots") or [] if isinstance(lot, Mapping)
            )
    return out


def _buys(order_rows: Sequence[Mapping[str, Any]]) -> dict[str, list[dict]]:
    """Every buy execution the orders carry, per symbol, oldest first."""
    found: dict[str, list[tuple[Any, dict]]] = {}
    for order in order_rows:
        if order.get("side") != "buy":
            continue
        for execution in order.get("executions") or []:
            if not isinstance(execution, Mapping):
                continue
            when = _moment(execution.get("timestamp"))
            if when is None or execution.get("price") is None or execution.get("quantity") is None:
                continue
            found.setdefault(str(order.get("symbol")), []).append(
                (
                    when,
                    {
                        "filled_at": execution["timestamp"],
                        "price": execution["price"],
                        "quantity": execution["quantity"],
                        "trading_day": when.astimezone(cadence.ET).date().isoformat(),
                    },
                )
            )
    return {
        symbol: [fill for _when, fill in sorted(pairs, key=lambda pair: pair[0])]
        for symbol, pairs in found.items()
    }


def _row(
    position: Mapping[str, Any], lots: Sequence[Mapping[str, Any]], buys: Sequence[Mapping[str, Any]]
) -> dict | None:
    """One position as the Dip engine's row, or None when it has no T+1 clock.

    The clock is the newest lot's `open_date` (the broker's own date of the
    buy). When the newest buy execution falls on that same day, its timestamp
    and price are the latest fill; otherwise the position's last buy is older
    than the order window fetched, so there is no timestamp, and the minutes
    gate has nothing to measure and is skipped (a buy more than a few weeks
    old is far past 30 minutes).
    """
    try:
        cost = float(position.get("average_buy_price"))
        quantity = float(position.get("quantity"))
    except (TypeError, ValueError):
        return None
    dated = sorted(
        (lot for lot in lots if isinstance(lot.get("open_date"), str) and lot["open_date"]),
        key=lambda lot: lot["open_date"],
    )
    if cost <= 0 or quantity <= 0 or not dated:
        return None

    newest, oldest = dated[-1]["open_date"], dated[0]["open_date"]
    lot_price = dated[-1].get("cost_per_share")
    latest = buys[-1] if buys and buys[-1].get("trading_day") == newest else None
    fill = {
        "price": float(latest["price"]) if latest else float(lot_price if lot_price is not None else cost),
        "quantity": float(latest["quantity"]) if latest else float(dated[-1].get("quantity") or 0.0),
        "trading_day": newest,
        "filled_at": latest["filled_at"] if latest else None,
    }
    return {
        "symbol": position["symbol"],
        "state": "HOLDING",
        "opened_trading_day": oldest,
        "clock_trading_day": newest,
        "weighted_avg_cost": cost,
        "total_quantity": quantity,
        "total_fills": len(dated),
        "latest_fill_at": fill["filled_at"],
        "fills": [fill],
    }


def _book(positions: Sequence[Mapping[str, Any]], quotes: Mapping[str, Any]) -> list[dict]:
    """Guard's view of the real book: symbol, quantity, market value, and the
    shares the broker says are free to sell. Valued at the live last, else at
    cost, the same overstate-rather-than-understate order `cli._market_value`
    documents."""
    book = []
    for position in positions:
        symbol = position["symbol"]
        quantity = float(position["quantity"])
        last = (quotes.get(symbol) or {}).get("last")
        price = float(last) if last is not None else float(position.get("average_buy_price") or 0.0)
        row: dict[str, Any] = {
            "symbol": symbol,
            "quantity": quantity,
            "market_value": round(quantity * price, 6),
        }
        available = position.get("shares_available_for_sells")
        if available is not None:
            row["shares_available_for_sells"] = float(available)
        book.append(row)
    return book


def assemble(
    inputs: Mapping[str, Sequence[tuple[str, Any]]],
    loaded: Mapping[str, Any],
    snapshot: Any,
) -> tuple[dict, dict]:
    """`(cycle_json, notes)`. `notes` is for the operator, not for `decide`."""
    seed = _single(inputs["seed"], "seed")
    if not isinstance(seed, Mapping) or not seed.get("cycle_id") or not seed.get("now"):
        raise AssemblyError(INPUTS_INCOMPLETE, "seed.json needs both cycle_id and now")

    state = _state(_single(inputs["state"], "state"))
    control = state.get("control") or {}

    resting = broker_shapes.order_rows(_responses(inputs.get("orders", [])))
    every_order = broker_shapes.order_rows([*resting, *_responses(inputs.get("fills", []))])

    portfolio = _data(_single(inputs["portfolio"], "portfolio"))
    raw_accounts = _data(_single(inputs["accounts"], "accounts"))
    if isinstance(raw_accounts, Mapping) and isinstance(raw_accounts.get("accounts"), list):
        raw_accounts = raw_accounts["accounts"]
    accounts = broker_shapes._rows(raw_accounts, ("account_number",))
    shaped = cli.shape_account(
        {
            "now": seed["now"],
            "control": control,
            "portfolio": portfolio,
            "accounts": accounts,
            "orders": list(every_order),
        },
        loaded,
    )

    held = _positions(_responses(inputs["positions"]))
    lots = _lots(inputs.get("taxlots", []))
    buys = _buys(every_order)
    quotes = _merged_quotes(inputs.get("quotes", []))

    rows: dict[str, dict] = {}
    excluded: list[str] = []
    for position in held:
        row = _row(position, lots.get(position["symbol"], []), buys.get(position["symbol"], []))
        if row is None:
            excluded.append(position["symbol"])
        else:
            rows[position["symbol"]] = row

    fundamentals = _symbol_rows(_responses(inputs.get("fundamentals", [])))
    symbols = [p["symbol"] for p in held]

    out: dict[str, Any] = {
        "cycle_id": seed["cycle_id"],
        "now": seed["now"],
        "control": control,
        "account": shaped["account"],
        "portfolio": portfolio,
        "accounts": accounts,
        "quotes": quotes,
        "positions": _book(held, quotes),
        "manual_rows": rows,
        "earnings": _earnings(inputs.get("earnings", []), state.get("day_context") or {}),
        "tradability": _symbol_rows(_responses(inputs.get("tradability", []))),
        "sectors": {
            symbol: row["sector"]
            for symbol, row in fundamentals.items()
            if isinstance(row, Mapping) and row.get("sector")
        },
        "snapshot": snapshot,
        "loss_closes": state.get("loss_closes") or [],
        "open_orders": [
            order
            for order in broker_shapes.orders(resting)
            if str(order.get("state", "")).lower() not in broker_shapes._TERMINAL_ORDER_STATES
        ],
        "dip_recovery_config": state.get("dip_recovery_config") or {},
    }
    if state.get("regime_inputs"):
        out["regime_inputs"] = state["regime_inputs"]

    notes = {
        "symbols": symbols,
        "excluded_no_tax_lots": excluded,
        "account_problems": shaped["problems"],
        "missing": {
            key: [s for s in symbols if s not in pool]
            for key, pool in (
                ("quotes", quotes),
                ("tradability", out["tradability"]),
                ("earnings", out["earnings"]),
                ("sectors", out["sectors"]),
            )
            if any(s not in pool for s in symbols)
        },
        "open_orders": len(out["open_orders"]),
    }
    return out, notes


def _merged_quotes(files: Sequence[tuple[str, Any]]) -> Any:
    """Every quote response folded into one `{symbol: {last, bid, ask, ...}}`.

    A session may have made more than one call (twenty symbols to a call).
    `cli._quotes` accepts the symbol-keyed map as is, which is also what
    `dip_assemble` hands it.
    """
    merged: dict[str, Any] = {}
    for response in _responses(files):
        merged.update(broker_shapes.quotes(response))
    return merged


def main(argv: Sequence[str], stdout, stderr) -> int:
    if len(argv) != 1:
        json.dump(
            cli._refusal(COMMAND, cli.UNKNOWN_COMMAND, "usage: python -m src.manual_assemble <inputs directory>"),
            stdout,
        )
        return EXIT_USAGE

    try:
        inputs = read_inputs(Path(argv[0]))
        snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        cycle, notes = assemble(inputs, cli.load_config(), snapshot)
    except AssemblyError as exc:
        json.dump(cli._refusal(COMMAND, exc.code, exc.detail), stdout)
        return EXIT_BAD_INPUT

    json.dump(cycle, stdout)
    json.dump(notes, stderr)
    stderr.write("\n")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main(sys.argv[1:], sys.stdout, sys.stderr))
