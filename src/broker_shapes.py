"""One pass that turns a broker response into the vocabulary this core reads.

**Why this module exists.** Appendix D describes `cycle.json` in names a *live
session* invented while hand-copying tool output. The runner holds no rules and
posts each response verbatim, so the two vocabularies meet at this boundary and
have now produced five production failures in three days — `balances`, `intent`
(`dfedcfd`), the quote fields, the MMM day-context shape, and `orders.id`,
which blocked entries for five cycles and left three `TIME_STOP`/target exits
unplaceable because every one of this system's own resting GTC stops read as a
`PHANTOM_ORDER`.

The fix that was attempted four times was a renamer per key, added where the
key was first read: `cli._quote_map`, `cli._tax_lots`, `cli._realized`,
`cli.shape_account`. That is why `orders` had none — nobody had been bitten by
`orders` yet. **This module is the sixth attempt and the last one worth making:
one function, every key, called once at the top of every command.** A new
vocabulary gap is now an edit to one table in one file rather than a new
function in whichever module happened to notice.

**Three rules, and they are the whole contract.**

* **Both shapes are accepted.** A live session's hand-built value passes
  through untouched; the tool's own envelope is unwrapped and renamed. Neither
  caller has to know which one it got, which is the point — the live path and
  the hosted path stop being two vocabularies.
* **Unknown fields pass through.** A broker response that grows a column needs
  no edit here. Only the names this core actually reads are renamed.
* **Nothing is defaulted.** A field the broker did not send is absent, never
  `None` and never `0.0`. `cli._quote_map` learned this the expensive way and
  its reasoning is general: downstream, an absent figure reads as unreadable
  and a null reads as a real zero to something.

**Idempotent by construction.** `run._payload` normalises what it assembles and
then hands it to `cli.reconcile`, which normalises again; every rename below
prefers the internal name over the broker's for exactly that reason. A
normaliser that clobbered on a second pass would be a worse bug than the one
this replaces, because it would only appear on the hosted path.

**Field names are recorded, not guessed.** Where no live response has been
recorded — `earnings`, and the inner shape of
`fundamentals`/`tradability`/`signals` — this module unwraps envelopes and
renames nothing, because §14's rule is that a field which cannot be read is not
a field that may be guessed.
"""

from __future__ import annotations

from typing import Any, Mapping

# ---------------------------------------------------------------------------
# Envelopes
# ---------------------------------------------------------------------------

# Every wrapper a list-shaped tool result has been seen to arrive inside.
# `data`, `results` and `trades` are the broker's own three
# (`get_pnl_trade_history` nests `data.trades`, `get_equity_quotes` returns
# `results`); `orders`/`positions` are the names a paginated page would use;
# `rows`/`items` are slack.
_LIST_ENVELOPES = (
    "data",
    "results",
    "trades",
    "orders",
    "positions",
    "rows",
    "items",
)

# How far `_rows` will descend. The deepest live nesting recorded is two
# (`data.trades`); three is slack, and a bound is what keeps an unfamiliar
# payload a wrong answer rather than a hang — the same reasoning, and the same
# number, as `cli._REALIZED_MAX_DEPTH`, which this replaces.
_MAX_DEPTH = 3


def _rows(raw: Any, marks: tuple[str, ...], depth: int = 0) -> list[Mapping[str, Any]]:
    """The row list, however the tool wrapped it.

    Four shapes reach here and all four are real:

    * the list itself — a live session's `cycle.json`, or a test;
    * ``{"results": [...]}`` / ``{"data": {"orders": [...]}}`` — the tool's own
      envelope, which is what the runner posts;
    * a single row — a tool asked about one symbol, pasted without its list;
    * ``{"AAPL": [...], "MSFT": [...]}`` — a per-symbol map, which is what
      `assemble` builds out of `edge_cycle_input` rows that carry a `symbol`.

    `marks` names the fields that identify a bare row, so the single-row case
    can be told apart from the per-symbol map: an order carries `state`, a
    position carries `quantity`. A Mapping that is neither an envelope, nor a
    row, nor a map of rows yields nothing — which every caller treats as "the
    broker sent none", the same answer as an absent key.
    """
    if isinstance(raw, list):
        return [row for row in raw if isinstance(row, Mapping)]
    if not isinstance(raw, Mapping) or depth >= _MAX_DEPTH:
        return []

    for envelope in _LIST_ENVELOPES:
        if envelope in raw:
            found = _rows(raw[envelope], marks, depth + 1)
            if found:
                return found

    if any(mark in raw for mark in marks):
        return [raw]

    out: list[Mapping[str, Any]] = []
    for value in raw.values():
        out.extend(_rows(value, marks, depth + 1))
    return out


def _renamed(row: Mapping[str, Any], fields: Mapping[str, tuple[str, ...]]) -> dict:
    """One row, with `fields`' internal names filled from the first broker key
    that carries a value. Everything else is copied through untouched.

    The internal name is always first in its own candidate tuple, which is what
    makes a second pass a no-op rather than a clobber.
    """
    out = dict(row)
    for internal, candidates in fields.items():
        value = next((row[key] for key in candidates if row.get(key) is not None), None)
        if value is not None:
            out[internal] = value
    return out


# ---------------------------------------------------------------------------
# orders
# ---------------------------------------------------------------------------

# **`get_equity_orders` returns the order id as `id`.** `cycle.json`'s Appendix
# D, `reconcile._order_findings`, `reconcile._ours`, `cli._protection_orders`
# and `protection.is_ours_to_replace` all read `order_id`.
#
# The remaining fields are listed for the record rather than renamed: `id` is
# the only name this core disagrees with the broker about. Read-back
# vocabulary: a `stop_market` comes back as `type: "market"` with `trigger:
# "stop"` and a `stop_price`, and a limit's price is `price` rather than
# `limit_price` — both are `protection.resting_kind`/ `resting_price`'s
# business and are deliberately not flattened here, because the placement
# vocabulary and the read-back vocabulary are two real shapes and collapsing
# them would lose which one an order came from.
ORDER_FIELDS = {"order_id": ("order_id", "id")}

# What a bare `get_equity_orders` row carries, so a single order pasted without
# its envelope is recognised as one row rather than walked as a map.
_ORDER_MARKS = ("state", "side", "order_id", "id")


def _order_key(row: Mapping[str, Any]) -> Any:
    return row.get("id", row.get("order_id"))


def order_rows(raw: Any) -> list[Mapping[str, Any]]:
    """`get_equity_orders`' rows, unwrapped from whatever envelope, not renamed,
    and with an order that appears twice kept once (first copy wins).

    The fetch plan asks for orders twice — the open ones, and the recent ones
    (`cli._fetch_plan`) — so a response set reaches here in three real shapes:
    one response (an envelope, or its bare list), **a list of responses**
    (what a live session posts under `orders`, and what `assemble` hands over
    after a merge), and the legacy list of rows. A list element that carries an
    order's own fields is a row; any other element is a response and is
    unwrapped the way a lone one would be. An order both windows returned is
    the same order, so it is kept once.
    """
    if isinstance(raw, list):
        found: list[Mapping[str, Any]] = []
        for item in raw:
            if isinstance(item, Mapping) and any(m in item for m in _ORDER_MARKS):
                found.append(item)
            else:
                found.extend(order_rows(item))
    else:
        found = _rows(raw, _ORDER_MARKS)

    seen: set[Any] = set()
    rows: list[Mapping[str, Any]] = []
    for row in found:
        key = _order_key(row)
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        rows.append(row)
    return rows


def orders(raw: Any) -> list[dict]:
    """`get_equity_orders`, in the vocabulary `reconcile` reads."""
    return [_renamed(row, ORDER_FIELDS) for row in order_rows(raw)]


# ---------------------------------------------------------------------------
# positions
# ---------------------------------------------------------------------------

# `get_equity_positions` and this core already agree on every name — `symbol`,
# `quantity`, `average_buy_price`, `market_value`. Nothing is renamed; what
# this key needed was the envelope unwrap, and what it needed *more* was for
# two fields to stop being dropped on the floor:
#
# **`shares_available_for_sells` / `shares_held_for_sells`.** The core could
# not see the reservation because nothing carried it past this boundary.
# Carried onto `reconcile.Position` now so the rule that fixes it has a number
# to read; nothing consumes it yet, and a broker that omits the field leaves it
# absent rather than 0.
_POSITION_MARKS = ("quantity", "average_buy_price", "symbol")


def positions(raw: Any) -> list[dict]:
    """`get_equity_positions`, unwrapped. No field is renamed."""
    return [dict(row) for row in _rows(raw, _POSITION_MARKS)]


# ---------------------------------------------------------------------------
# quotes
# ---------------------------------------------------------------------------

# `last_trade_price` is the regular-session print; the response also carries
# `last_non_reg_trade_price` for extended hours, and this system places
# `regular_hours` only, so the regular print is the one that matches the
# sessions it can actually trade in.
QUOTE_FIELDS = {
    "last": "last_trade_price",
    "bid": "bid_price",
    "ask": "ask_price",
    "adjusted_previous_close": "adjusted_previous_close",
}


def quotes(raw: Any) -> dict[str, Mapping[str, Any]]:
    """Symbol -> quote, from either shape this key arrives in.

    A live session read `get_equity_quotes` and hand-built Appendix D's
    ``{symbol: {bid, ask, last, adjusted_previous_close}}``. The runner cannot:
    it pastes the response as it came, which is ``{"results": [{"close": {...},
    "quote": {...}}, ...]}`` under the broker's own field names — so the reader
    unpacked a list as a mapping and took the cycle down (third of the
    vocabulary gaps in this boundary after `balances` and `intent`).

    **A field the broker did not send is omitted, never nulled** — `_catalyst`
    and every downstream reader already treat an absent figure as unreadable,
    and a `None` would read as a real zero to something.
    """
    if not isinstance(raw, Mapping):
        return {}

    # The live tool wraps the list once more — ``{"data": {"results":
    # [...]}}``. Without this unwrap `data` read as a *symbol* in Appendix D's
    # shape: the core got no bid for anything, and every session reshaped
    # quotes by hand to get around it — the same envelope `_unwrap_account`
    # already strips.
    if isinstance(raw.get("data"), Mapping) and "results" in raw["data"]:
        raw = raw["data"]

    results = raw.get("results")
    if not isinstance(results, list):
        # A symbol-keyed map: a live session's cycle.json, a test, or the
        # core's own `assemble` of the runner's per-symbol `quotes` rows. Each
        # value is either already Appendix D's `{bid, ask, last, ...}` (kept
        # as is — this is what keeps `normalize` idempotent) or one raw
        # response row, `{"close": {...}, "quote": {...}}`, which still has
        # the broker's names. Every runner cycle since the resolve phase
        # passed those rows through untouched, so `exits.py` read no `bid` and
        # four TIME_STOPs went to manual action in a cycle that reported
        # success_with_warnings.
        return {
            s: _quote_fields(q["quote"])
            if isinstance(q.get("quote"), Mapping)
            else q
            for s, q in raw.items()
            if isinstance(q, Mapping)
        }

    out: dict[str, Mapping[str, Any]] = {}
    for row in results:
        if not isinstance(row, Mapping):
            continue
        quote = row.get("quote")
        if not isinstance(quote, Mapping):
            continue
        symbol = quote.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            continue
        out[symbol] = _quote_fields(quote)
    return out


def _quote_fields(quote: Mapping[str, Any]) -> dict[str, Any]:
    return {
        name: quote[source]
        for name, source in QUOTE_FIELDS.items()
        if quote.get(source) is not None
    }


# ---------------------------------------------------------------------------
# tax lots
# ---------------------------------------------------------------------------

LOT_FIELDS = {
    "cost_basis": ("cost_basis", "tax_cost_basis"),
    "acquired_date": ("acquired_date", "open_date"),
}

_LOT_MARKS = ("quantity", "cost_basis", "tax_cost_basis", "open_date")


def tax_lots(raw: Any) -> dict[str, list[dict]]:
    """Per-symbol lots, renamed from the broker's field names to reconcile's.

    Unlike every other key here the *outer* shape is this core's own — Appendix
    D's `{symbol: [lot, ...]}`, built by the session or the runner from one
    call per symbol — so the symbol keys are taken as given and only each
    symbol's own list is unwrapped.

    A lot missing both names keeps neither key rather than gaining a null:
    `_basis` reads an absent `cost_basis` as `0.0` inside a sum it is already
    weighting, and `_held_since` drops a lot with no `acquired_date`, so the
    absent field degrades to "this lot contributes nothing" — which is the
    honest reading of a lot the broker sent no basis for.
    """
    if not isinstance(raw, Mapping):
        return {}
    return {
        symbol: [_renamed(lot, LOT_FIELDS) for lot in _rows(lots, _LOT_MARKS)]
        for symbol, lots in raw.items()
    }


# ---------------------------------------------------------------------------
# realized
# ---------------------------------------------------------------------------

# What one live `get_pnl_trade_history` response actually carries, per trade:
# `timestamp`, `symbol`, `side`, `quantity`, `price`, `realized_gain` — and
# nothing else. Three of the internal names below therefore have no source at
# all, and that is a fact about the broker rather than a bug:
#
# * `order_id` — the tool returns no identifier of any kind, so `_Realized`'s
#   preferred match is permanently unavailable and every lookup goes through
#   the `(symbol, date)` fallback. That fallback was written for exactly this.
# * `proceeds` / `cost_basis` — absent, but `realized_gain` is present on every
#   row, so the subtraction they exist to reconstruct is never needed.
#
# **The sign convention is signed**, also confirmed live: losses come back
# negative (`-0.93`, `-8.53`), not as positive magnitudes. This was the
# assumption the whole ledger inverts on if it is wrong.
REALIZED_FIELDS = {
    "order_id": ("order_id", "execution_id", "id"),
    "symbol": ("symbol",),
    "quantity": ("quantity",),
    "price": ("price",),
    "proceeds": ("proceeds",),
    "cost_basis": ("cost_basis", "basis"),
    "realized_gain": ("realized_gain", "realized_pnl", "realized_profit_loss"),
    # `timestamp` is the live one — the other four were guesses and none of
    # them is what the tool returns. Normalised to a calendar day by
    # `reconcile.calendar_day`, because the value is an instant
    # (`2026-09-04T16:33:05Z`) and the fill it has to match is dated by day.
    "date": ("date", "trade_date", "executed_at", "settlement_date", "timestamp"),
    # The same instant, *not* truncated. `edge_realized_trades` keys one
    # closing trade on `(symbol, traded_at, quantity)`, because the week-wide
    # `span` re-offers every trade on every reconciling cycle and a day is
    # not unique enough to tell two same-day sells apart.
    "traded_at": ("traded_at", "timestamp", "executed_at", "trade_date", "date"),
    "side": ("side",),
}

# The four that are money or shares. A missing one becomes None, never 0.0.
REALIZED_NUMERIC = ("quantity", "price", "proceeds", "cost_basis", "realized_gain")

_REALIZED_MARKS = ("realized_gain", "proceeds", "timestamp")


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def realized(raw: Any, calendar_day) -> list[dict]:
    """Per-fill realized results, renamed from the broker's names to reconcile's.

    `get_pnl_trade_history` answers ``{"data": {"account_number": ..., "span":
    ..., "trades": [...]}, "guide": "..."}`` and both the prompt and the runner
    paste that response whole. A single-level unwrap found `data`, saw a Mapping
    rather than a list, and fell through to the empty list — so `realized` was
    inert in production from the day it shipped: no wrong numbers, no inverted
    signs, just every fill silently falling back to the tax-lot arithmetic this
    path exists to replace.

    Two rules make this survive a schema it has never seen: **unknown extra
    fields are ignored**, and **a missing, null or unparseable numeric field
    becomes `None`, never `0.0`** — a zero realized gain is a real break-even,
    an absent one is a failed read, and `reconcile` treats them differently.

    `calendar_day` is passed in rather than imported so this module stays free
    of `reconcile`: one rule for what a day is, shared with the fill side of
    the match in `reconcile._Realized.gain`.
    """
    rows = _rows(raw, _REALIZED_MARKS) if not isinstance(raw, list) else [
        row for row in raw if isinstance(row, Mapping)
    ]

    out = []
    for row in rows:
        renamed: dict[str, Any] = {}
        for internal, candidates in REALIZED_FIELDS.items():
            value = next(
                (row[key] for key in candidates if row.get(key) is not None), None
            )
            if internal in REALIZED_NUMERIC:
                renamed[internal] = _number(value)
            elif internal == "date":
                renamed[internal] = calendar_day(value)
            else:
                renamed[internal] = value
        out.append(renamed)
    return out


# ---------------------------------------------------------------------------
# symbol-keyed maps
# ---------------------------------------------------------------------------


def symbol_map(raw: Any) -> dict[str, Any]:
    """`{symbol: response}`, from either the map or the tool's list envelope.

    `fundamentals`, `tradability` and `signals` are all read as
    ``ctx[key][symbol]``, and all three are filled from tools that answer a
    batch of symbols at once. A session builds the map while copying; a runner
    posting one batched response verbatim would hand the reader a `results`
    list keyed by nothing.

    **Nothing inside a row is renamed.** No raw `get_equity_fundamentals` or
    `get_equity_tradability` response has been recorded in a run log, so this
    core does not know which of its inner names are the broker's and which a
    session invented — and §14's rule is that an unreadable field may not be
    guessed. The one inner fact that *is* confirmed lives in
    `guard/legality.py`: the headline field is `tradeable`, a boolean, and the
    `"tradability"` string it replaced is gone.

    `earnings` is deliberately not routed through here: its per-symbol value is
    a whole `get_earnings_results` response and `cli._quarters` already
    descends `data` → `results` to find the quarters, so unwrapping it a second
    time here would be a second answer to a settled question.
    """
    if not isinstance(raw, Mapping):
        return {}

    results = raw.get("results")
    if isinstance(raw.get("data"), Mapping) and not isinstance(results, list):
        results = raw["data"].get("results")

    if not isinstance(results, list):
        return dict(raw)

    out: dict[str, Any] = {}
    for row in results:
        if isinstance(row, Mapping) and isinstance(row.get("symbol"), str):
            out[row["symbol"]] = row
    return out


# ---------------------------------------------------------------------------
# account
# ---------------------------------------------------------------------------

# Appendix D has one `account` key and two account-scope tools feed it:
# `get_portfolio` (`total_value`, `cash`, `buying_power.buying_power`) and
# `get_accounts` (`unsettled_funds`, per account). The merge is
# `cli.shape_account`'s — it needs the trading day and `config/inception.json`
# and so cannot live in a pure pass — but *deciding that a merge is owed* is
# vocabulary, and that is this module's job.
#
# **Detected, not assumed.** An `account` carrying `equity_total` came from a
# live session that did the merge by hand, or from `merge-fetch` session
# recovery, and reshaping it would be a second answer to a settled question.
# `total_value` present with `equity_total` absent is the raw shape and nothing
# else's — an earlier runner cycle died on exactly this, and an earlier
# overnight session hand-merged the wrong field and woke the operator with a
# false urgent Drawdown halt.
def _unwrap_account(account: Mapping[str, Any]) -> Mapping[str, Any]:
    """`get_portfolio`'s response, minus the tool's own `data` envelope.

    The runner posts the tool result verbatim (`docs/routine/runner-prompt.md`
    — "paste the response as it came"), and `get_portfolio` wraps its object in
    `{"data": {...}}`, the same envelope `_LIST_ENVELOPES`/`_rows` already
    strip for list-shaped tools. `account` is a single object rather than a
    list, so it never went through `_rows` and got its own one-level unwrap
    here instead — its absence is what let a runner-posted `account` sail past
    `account_is_raw` unrecognised and die in `finalize` on a `KeyError:
    'equity_total'`.
    """
    inner = account.get("data")
    return inner if isinstance(inner, Mapping) else account


def account_is_raw(payload: Mapping[str, Any]) -> bool:
    """Whether `payload["account"]` is still `get_portfolio`'s own response."""
    account = payload.get("account")
    if not isinstance(account, Mapping):
        return False
    account = _unwrap_account(account)
    return "equity_total" not in account and "total_value" in account


def _relocate_account(payload: dict) -> None:
    """Move a raw `get_portfolio` response to where `cli.shape_account` reads it.

    The raw responses are kept beside the shaped account under
    `portfolio`/`accounts` rather than consumed: `cli.settled_cash` names those
    two keys, and a runlog reader chasing a settled-cash figure should be able
    to see what it was computed from.
    """
    if not account_is_raw(payload):
        return
    payload.setdefault("portfolio", dict(_unwrap_account(payload["account"])))
    if "balances" in payload and "accounts" not in payload:
        balances = payload["balances"]
        payload["accounts"] = list(balances) if isinstance(balances, list) else balances


# ---------------------------------------------------------------------------
# the one pass
# ---------------------------------------------------------------------------

# Every Appendix D key whose vocabulary differs from the broker's, and the
# function that reconciles the two. A key absent from this table is one where
# the broker and this core already agree — adding a renamer for it is an edit
# here and nowhere else, which is the entire reason this table exists.
_SIMPLE = {
    "orders": orders,
    # The working subset of `orders`, carrying each order's `type`, `quantity`,
    # `time_in_force` and whichever of `stop_price`/`limit_price` it has.
    # Same rows, same rename, same `order_id` that `guard._PlanState` matches on.
    "open_orders": orders,
    "positions": positions,
    "quotes": quotes,
    "tax_lots": tax_lots,
    "fundamentals": symbol_map,
    "tradability": symbol_map,
    "signals": symbol_map,
}


# `reconcile.TERMINAL_ORDER_STATES`, copied because this module cannot import
# the one that imports it; `test_broker_shapes.py` pins the two equal.
_TERMINAL_ORDER_STATES = frozenset({"filled", "cancelled", "canceled", "rejected"})


def _derive_open_orders(payload: dict) -> None:
    """Appendix D's `open_orders`: "the working subset of `orders`".

    A live session derives it by hand; the runner posts `get_equity_orders`
    under `orders` only and never posts `open_orders` at all. `cli.decide` reads
    `open_orders` — for the cancel of a resting target an exit is about to sell
    against, and for guard's view of what is resting — so on the runner path an
    exit was planned with no cancel and guard denied it `PROTECTION_CONTENDS`
    ("0 of 7 shares available"). A posted `open_orders` is left alone.
    """
    if "open_orders" in payload or "orders" not in payload:
        return
    payload["open_orders"] = [
        order
        for order in payload["orders"]
        if str(order.get("state", "")).lower() not in _TERMINAL_ORDER_STATES
    ]


def _derive_fills(payload: dict) -> None:
    """`fills`: "this cycle's fills", read off the executions on `orders`.

    A live session hand-builds `fills`; the runner posts `get_equity_orders`
    under `orders` only and never posts `fills` at all, so every reconcile saw
    none. A sell placed at 09:41 and filled at once then read, at the 15:08
    monitor cycle, as `POSITION_DELTA_MISMATCH` (broker holds 0, expected 7 —
    the prior book with no sell netted off) plus `QUANTITY_DRIFT` ("filled 0
    against an intended 7"), and the cycle was reported *blocked* on a
    successful exit.

    Only executions of orders this side placed (`intent.placements`) and has not
    already adjudicated (`control.retired_order_ids`) count. `intent` is the
    most recent cycle that placed something and can be days old; without the
    retirement filter the same fill would be netted against a prior book that
    already reflects it. A fill with no originating placement (an operator
    trade) is not derived — that was the behaviour before, and nothing here
    can bound it to "since the last cycle". A posted `fills` is left alone.

    Rows are marked `derived` so the loss-close pass does not read a missing
    `realized` row on a derived sell as "basis unknown" (a profitable sell
    would otherwise block its symbol for the wash-sale window).
    """
    if "fills" in payload or "orders" not in payload:
        return
    intent = payload.get("intent")
    placements = intent.get("placements") if isinstance(intent, Mapping) else None
    ours = {
        p.get("order_id") for p in placements or [] if p.get("order_id")
    }
    if not ours:
        return
    control = payload.get("control")
    retired = set(
        (control.get("retired_order_ids") if isinstance(control, Mapping) else None)
        or []
    )
    fills: list[dict] = []
    for order in payload["orders"]:
        order_id = order.get("order_id")
        if order_id not in ours or order_id in retired:
            continue
        for execution in order.get("executions") or []:
            fills.append(
                {
                    "order_id": order_id,
                    "symbol": order.get("symbol"),
                    "side": order.get("side"),
                    "quantity": execution.get("quantity"),
                    "price": execution.get("price"),
                    "timestamp": execution.get("timestamp"),
                    "derived": True,
                }
            )
    if fills:
        payload["fills"] = fills


def normalize(payload: Mapping[str, Any], calendar_day) -> dict:
    """`cycle.json`, in this core's vocabulary, whoever assembled it.

    Called at the top of every command that reads a broker payload
    (`cli.reconcile`, `cli.decide`, `cli.report`, `cli.plan_fetch`) and once
    more in `run._payload`, so the live path and the hosted path are the same
    path by the time anything reads a field.

    Only the keys in `_SIMPLE` (plus `realized` and `account`) are touched.
    Everything else — `control`, `events`, `snapshot`, `play_state`, the
    routine's own `triggers` — is copied through by reference, because this
    module has no opinion about anything that did not come from the broker.

    `calendar_day` is `reconcile.calendar_day`, passed by the caller for the
    one key that needs it; importing it here would make this module depend on
    the module that depends on it.
    """
    out = dict(payload)

    for key, shaper in _SIMPLE.items():
        if key in out:
            out[key] = shaper(out[key])

    _derive_open_orders(out)
    _derive_fills(out)

    if "realized" in out:
        out["realized"] = realized(out["realized"], calendar_day)

    _relocate_account(out)
    _derive_prior_positions(out)
    return out


# `edge_prior()`'s whole result lands under `intent`/`prior_book` — the
# session's own vocabulary table (cycle-prompt.md) names `prior_positions` as
# `prior_book.positions` but nothing ever enforced the copy. Deriving it here,
# once, removes the possibility of a session forgetting the copy — the same
# reasoning as every other renamer in `_SIMPLE`.
def _derive_prior_positions(payload: dict) -> None:
    if "prior_positions" in payload:
        return
    prior_book = payload.get("prior_book")
    if not isinstance(prior_book, Mapping):
        return
    positions = prior_book.get("positions")
    if isinstance(positions, list):
        payload["prior_positions"] = positions
