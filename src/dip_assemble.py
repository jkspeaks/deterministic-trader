"""The Dip Monitor's tool responses -> `cycle.json`, with no script in between.

    uv run python -m src.dip_assemble inputs > cycle.json

**Why this module exists.** `dip-monitor-prompt.md` Step 6 used to say "assemble `cycle.json`"
and nothing about how. Every tick the session improvised: it retyped the day's responses into
a throwaway Python script, renamed and trimmed fields inside it, and ran that. The three ticks
that day took 11, 31 and 24 minutes for a decision that runs in under a second.

The hosted core solved the same problem for the main cycle with
`services/core/core/assemble.py`. This is that idea for the routine that still
runs in a checkout: **the session writes each response to a file exactly as
the tool returned it, and this module does every rename, trim and merge.**
Nothing is retyped into code, and the only thing executed is checked in.

## The input directory

One file per tool response, named `<kind>[.<anything>].json`. The part before
the first dot is the kind; the rest only keeps names apart (`quotes.2.json`,
`indicators.BMY.rsi.json`). A file may hold one response or a JSON array of
responses.

| kind | what goes in it | required |
|---|---|---|
| `seed` | `{"cycle_id": ..., "now": ...}`, Step 0.5's own file | yes |
| `state` | the one Supabase read, Step 2 | yes |
| `portfolio` | `get_portfolio` | yes |
| `accounts` | `get_accounts` | yes |
| `quotes` | `get_equity_quotes` | |
| `indicators` | `get_equity_technical_indicators`, any number | |
| `fundamentals` | `get_equity_fundamentals` | |
| `historicals` | `get_equity_historicals` | |
| `tradability` | `get_equity_tradability` | |
| `earnings` | `get_earnings_results`, one file per symbol, named `earnings.<SYMBOL>.json` | |
| `orders` | `get_equity_orders`, the `confirmed` and the `created_at_gte` call | |
| `fills` | `get_equity_orders state=filled` | |

A kind not in this table is refused: it means the prompt and this module have
drifted, which is the failure this module exists to make loud.

## What it does to them

Only what `cli.dip_recovery_decide` cannot do for itself, and nothing that is
a judgment: the watchlist, the caps, the entry conditions and guard all stay
in `src/dip_recovery` and `src/guard`.

* **Field names are recorded, not guessed.** Every broker name read here comes from a response
  in `tests/fixtures/broker/`, recorded.
* **Figures are copied, never recomputed.** A price stays the string the
  broker sent.
* **A missing optional file is a missing key**, and the decide command fails
  that symbol's entry closed, exactly as it did when a session forgot a fetch.
* **`mc_check` is never written** (see the prompt: present-and-empty denies
  every buy).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from src import broker_shapes, cadence, cli

EXIT_OK = 0
EXIT_BAD_INPUT = 1
EXIT_USAGE = 2

COMMAND = "dip-assemble"
INPUTS_INCOMPLETE = "INPUTS_INCOMPLETE"
UNKNOWN_KIND = "UNKNOWN_KIND"

REQUIRED = ("seed", "state", "portfolio", "accounts")
OPTIONAL = (
    "quotes",
    "indicators",
    "fundamentals",
    "historicals",
    "tradability",
    "earnings",
    "orders",
    "fills",
)
KINDS = frozenset(REQUIRED + OPTIONAL)

SNAPSHOT = Path(__file__).resolve().parent.parent / "data" / "universe.json"

# `get_equity_technical_indicators` names an indicator by `type` and its
# `params.period`; `src/dip_recovery/signal.py` reads `rsi`, `sma_20` and
# `sma_200`. A moving average is keyed by its own period so a wrong-period
# call lands under a name nothing reads instead of under one something does.
_PERIODLESS = {"rsi": "rsi", "atr": "atr"}
_PERIODED = ("sma", "ema")

# `get_equity_historicals`' bar fields -> the `{high, low, close}` contract
# `indicators.compute_signals` and `dip_recovery/signal.py` share.
_BAR_FIELDS = {"high": "high_price", "low": "low_price", "close": "close_price"}

# The states `dip_recovery_positions` rows carry while an order is working and
# no lock has been claimed yet (the prompt's Step 5).
_PENDING_STATES = frozenset({"ENTRY_PENDING", "AVERAGE_PENDING"})


class AssemblyError(ValueError):
    """The directory does not describe a tick this command will assemble."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# reading the directory
# ---------------------------------------------------------------------------


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
            # utf-8-sig: a file written on Windows may open with a BOM.
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


def _responses(files: Sequence[tuple[str, Any]]) -> list[Any]:
    """Every response of one kind: a file holds one, or a list of them."""
    out: list[Any] = []
    for _name, parsed in files:
        out.extend(parsed if isinstance(parsed, list) else [parsed])
    return out


def _data(response: Any) -> Any:
    """A tool response minus its `{"data": ..., "guide": ...}` envelope."""
    if isinstance(response, Mapping) and isinstance(response.get("data"), (Mapping, list)):
        return response["data"]
    return response


def _single(files: Sequence[tuple[str, Any]], kind: str) -> Any:
    if len(files) != 1:
        raise AssemblyError(
            INPUTS_INCOMPLETE,
            f"exactly one {kind}.json is expected, found "
            f"{', '.join(name for name, _ in files)}",
        )
    return files[0][1]


def _state(parsed: Any) -> Mapping[str, Any]:
    """Step 2's read, however much of `execute_sql`'s wrapping came with it.

    The statement returns one row with one column, `state`, so the result
    arrives as `[{"state": {...}}]`. A session may paste that, the row, or the
    object itself; all three are the same answer.
    """
    if isinstance(parsed, list) and len(parsed) == 1:
        parsed = parsed[0]
    if isinstance(parsed, Mapping) and isinstance(parsed.get("state"), Mapping):
        parsed = parsed["state"]
    if not isinstance(parsed, Mapping) or "control" not in parsed:
        raise AssemblyError(
            INPUTS_INCOMPLETE,
            "state.json is not Step 2's result: it has no `control`",
        )
    return parsed


# ---------------------------------------------------------------------------
# one shaper per key
# ---------------------------------------------------------------------------


def _symbol_rows(responses: Sequence[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for response in responses:
        out.update(broker_shapes.symbol_map(response if isinstance(response, Mapping) else {}))
    return out


def _quotes(responses: Sequence[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for response in responses:
        out.update(broker_shapes.quotes(response))
    return out


def _signals(responses: Sequence[Any]) -> dict[str, dict[str, Any]]:
    """`{symbol: {rsi, sma_20, sma_200, ...}}` from the indicator calls.

    One call answers (as fetched) one indicator, with the latest bar last in
    `series`. A null or absent reading is left out, so the entry gate reports
    it as missing rather than reading a zero.

    The broker answers in two shapes and both are read: the symbol's row
    directly under `data`, or a `data.results` list of such rows, which is
    what the live tool returned for one symbol and for five.
    """
    out: dict[str, dict[str, Any]] = {}
    rows: list[Any] = []
    for response in responses:
        data = _data(response)
        if isinstance(data, Mapping) and isinstance(data.get("results"), list):
            rows.extend(data["results"])
        else:
            rows.append(data)
    for data in rows:
        if not isinstance(data, Mapping) or not isinstance(data.get("symbol"), str):
            continue
        for indicator in data.get("indicators") or []:
            if not isinstance(indicator, Mapping):
                continue
            kind = indicator.get("type")
            if kind in _PERIODLESS:
                name = _PERIODLESS[kind]
            elif kind in _PERIODED:
                period = (indicator.get("params") or {}).get("period")
                if period is None:
                    continue
                name = f"{kind}_{period}"
            else:
                continue
            series = [p for p in indicator.get("series") or [] if isinstance(p, Mapping)]
            if not series or series[-1].get("value") is None:
                continue
            out.setdefault(data["symbol"], {})[name] = series[-1]["value"]
    return out


def _bars(responses: Sequence[Any]) -> dict[str, list[dict]]:
    """`{symbol: [{high, low, close}, ...]}`, oldest first.

    A gap-filled bar (`interpolated: true`) carries no trade and is dropped;
    so is a bar missing any of the three figures, because
    `dip_recovery/signal.py` fails a short series closed and a bar with a hole
    in it would raise instead.
    """
    out: dict[str, list[dict]] = {}
    for response in responses:
        data = _data(response)
        results = data.get("results") if isinstance(data, Mapping) else None
        for result in results or []:
            if not isinstance(result, Mapping) or not isinstance(result.get("symbol"), str):
                continue
            bars = [
                b for b in result.get("bars") or []
                if isinstance(b, Mapping) and not b.get("interpolated")
                and all(b.get(source) is not None for source in _BAR_FIELDS.values())
            ]
            bars.sort(key=lambda b: str(b.get("begins_at") or ""))
            out[result["symbol"]] = [
                {name: b[source] for name, source in _BAR_FIELDS.items()} for b in bars
            ]
    return out


def _earnings(files: Sequence[tuple[str, Any]], day_context: Mapping[str, Any]) -> dict[str, Any]:
    """Each symbol's whole `get_earnings_results` response.

    The day's cache first (`state.day_context`, already narrowed to this
    tick's symbols), then whatever this tick fetched for the misses, which
    wins. The response is passed whole: `cli._earnings` derives
    `sessions_until_report` from the quarter rows.
    """
    out: dict[str, Any] = {}
    for symbol, entry in (day_context or {}).items():
        if isinstance(entry, Mapping) and entry.get("earnings") is not None:
            out[symbol] = entry["earnings"]
    for name, parsed in files:
        parts = name.split(".")
        if len(parts) < 3 or not parts[1]:
            raise AssemblyError(
                INPUTS_INCOMPLETE,
                f"{name}: an earnings file is named earnings.<SYMBOL>.json",
            )
        out[parts[1].upper()] = parsed
    return out


def _moment(stamp: Any) -> datetime | None:
    """A broker or database timestamp as an aware datetime, or None.

    The broker writes fractions of any length (`...38.06Z`, `...58.017Z`,
    nanoseconds on quotes); `fromisoformat` on this Python takes three or six
    digits only, so the fraction is cut to microseconds first.
    """
    if not isinstance(stamp, str) or not stamp:
        return None
    text = stamp.strip().replace("Z", "+00:00")
    head, sep, tail = text.partition(".")
    if sep:
        digits = ""
        while tail and tail[0].isdigit():
            digits, tail = digits + tail[0], tail[1:]
        text = f"{head}.{digits[:6].ljust(6, '0')}{tail}"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _fills(
    order_rows: Sequence[Mapping[str, Any]], state: Mapping[str, Any]
) -> list[dict]:
    """Confirmed executions on this side's symbols since the last decision.

    Step 5's three rules, in code instead of in a session's reading of a
    21-day order list:

    * **this side's symbol** — locked to `dip_recovery`, or carrying a
      position row (a pending entry has a row before it has a lock);
    * **after the last decision** — `state.last_decide_now` is the `now` the
      latest `dip-recovery-decide` runlog row was decided at. An execution at
      or before it was in front of that tick;
    * **not already folded in** — a buy whose timestamp is already in the
      position row's own `fills` was applied by a tick whose runlog write and
      order fetch straddled it.

    Oldest first, because `engine._apply_fills` folds them in order and the
    weighted average cost depends on it.
    """
    lock = state.get("symbol_lock") or {}
    positions = state.get("dip_recovery_positions") or {}
    cut = _moment(state.get("last_decide_now"))

    found: list[tuple[datetime, dict]] = []
    for order in order_rows:
        symbol = order.get("symbol")
        row = positions.get(symbol)
        if lock.get(symbol) != cli.dip_recovery.STRATEGY and not isinstance(row, Mapping):
            continue
        applied = {
            f.get("filled_at") for f in (row or {}).get("fills") or [] if isinstance(f, Mapping)
        }
        for execution in order.get("executions") or []:
            if not isinstance(execution, Mapping):
                continue
            stamp = execution.get("timestamp")
            when = _moment(stamp)
            if when is None or execution.get("quantity") is None or execution.get("price") is None:
                continue
            if cut is not None and when <= cut:
                continue
            if order.get("side") == "buy" and stamp in applied:
                continue
            found.append(
                (
                    when,
                    {
                        "symbol": symbol,
                        "side": order.get("side"),
                        "quantity": execution["quantity"],
                        "price": execution["price"],
                        "trading_day": when.astimezone(cadence.ET).date().isoformat(),
                        "filled_at": stamp,
                    },
                )
            )
    found.sort(key=lambda pair: pair[0])
    return [fill for _when, fill in found]


def _watchlist(control: Mapping[str, Any], trading_day: str) -> list[str]:
    """Today's Universe Pick list, or nothing. A stale list is never trusted
    into an entry (the prompt's Step 2)."""
    row = control.get("dip_recovery_watchlist")
    if not isinstance(row, Mapping) or row.get("trading_day") != trading_day:
        return []
    return [s for s in row.get("symbols") or [] if isinstance(s, str)]


# ---------------------------------------------------------------------------
# the assembly
# ---------------------------------------------------------------------------


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

    cycle = cli.cycle_of({"now": seed["now"], "control": control}, loaded)
    watchlist = _watchlist(control, cycle.trading_day)
    positions = state.get("dip_recovery_positions") or {}

    resting = broker_shapes.order_rows(_responses(inputs.get("orders", [])))
    every_order = broker_shapes.order_rows(
        [*resting, *_responses(inputs.get("fills", []))]
    )

    portfolio = _data(_single(inputs["portfolio"], "portfolio"))
    # `get_accounts` answers `{"data": {"accounts": [...]}}`: every account on
    # the login. `cli._settled_cash_value` picks this system's own by number.
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

    fundamentals = _symbol_rows(_responses(inputs.get("fundamentals", [])))

    out: dict[str, Any] = {
        "cycle_id": seed["cycle_id"],
        "now": seed["now"],
        "control": control,
        "account": shaped["account"],
        "portfolio": portfolio,
        "accounts": accounts,
        "signals": _signals(_responses(inputs.get("indicators", []))),
        "quotes": _quotes(_responses(inputs.get("quotes", []))),
        "fundamentals": fundamentals,
        "symbol_lock": state.get("symbol_lock") or {},
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
        "dip_recovery_positions": positions,
        "dip_recovery_watchlist": watchlist,
        "dip_recovery_fills": _fills(every_order, state),
        "dip_recovery_bars": _bars(_responses(inputs.get("historicals", []))),
    }
    # Absent, not null: a tick with no regime row has no regime opinion.
    if state.get("regime_inputs"):
        out["regime_inputs"] = state["regime_inputs"]

    symbols = sorted({*watchlist, *positions})
    notes = {
        "trading_day": cycle.trading_day,
        "symbols": symbols,
        "account_problems": shaped["problems"],
        "missing": {
            key: [s for s in pool if s not in out[key]]
            for key, pool in (
                ("quotes", symbols),
                ("signals", symbols),
                ("earnings", symbols),
                ("tradability", symbols),
                ("fundamentals", watchlist),
                ("dip_recovery_bars", watchlist),
            )
            if any(s not in out[key] for s in pool)
        },
        "open_orders": len(out["open_orders"]),
        "fills": len(out["dip_recovery_fills"]),
    }
    return out, notes


def main(argv: Sequence[str], stdout, stderr) -> int:
    if len(argv) != 1:
        json.dump(
            cli._refusal(COMMAND, cli.UNKNOWN_COMMAND, "usage: python -m src.dip_assemble <inputs directory>"),
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
