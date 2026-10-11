"""§10.1 at the broker — what should be *resting* behind every whole-share
position, and how that differs from what already is.

* A resting stop existed only for a position the system had *just bought*.
  `cycle-prompt.md` Step 8b placed one on a fill, so a position adopted from
  the operator's own book, or one whose stop was cancelled, or one held across
  an earlier postclose sweep, simply had none — and nothing looked again on
  any later cycle. §10.1 promises a `WHOLE` position "continuous protection
  between cycles"; what it had was protection between *fills*.
* Nothing rested on the way up at all. `DIP` and `MACRO` declared no target,
  and the ATR stop was measured from the entry price forever (rule 15), so a
  position that ran 30% and gave it all back exited at the same price it would
  have on day one.

This module answers "what should be resting" for the whole book on every
cycle, from the book itself rather than from a fill that happened once. It
places nothing: `reconcile.protection_plan` diffs this against the broker's
open orders, `guard.evaluate` rules on each proposed order like any other,
and the routine places what survives from `plan.protection`.

**Pure, and deliberately not a decision.** Every number here is derived from
`entry_price`, the position's own high-water price and the same ATR
`plays.exits` measures its stop from — two ATRs would put the resting stop and
the evaluated stop in different places, which was the whole reason
`cli.protective_stops` exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .core import (
    REGULAR_HOURS,
    _is_whole_share,
    _price,
    _signals,
    stop_multiple,
    target_pct as _target_pct,
)

# The two shapes, named because three modules pass them around: this one
# computes them, `reconcile` diffs them and `guard` refuses a duplicate of one.
STOP = "stop"
TARGET = "target"

# **Rest nothing at the broker at all** (owner's call, after a resting stop
# realised a loss on a dip the position would otherwise have recovered from).
# §6.5's `ATR_STOP` and `TARGET` are both already evaluated on every cycle by
# `exits.py` regardless of what rests — `desired()` below simply proposes no
# order at all in this mode, so `reconcile.protection_plan` cancels anything
# already resting and places nothing new, and every exit, stop or target, is a
# `plays.exits` decision made once per cycle rather than a broker-side order
# that can fire on an intraday wick nobody would have sold into by hand.
NONE = "none"

STOP_MARKET = "stop_market"
LIMIT = "limit"

# **At most one of the two may rest, and `NONE` means neither does.** The
# broker reserves 100% of a position's shares against the first resting sell
# it accepts — `shares_available_for_sells` goes to 0 and every later sell on
# the name is refused "Not enough shares to sell."
#
# That protects against a gap down, but it also means the stop can trigger on
# an ordinary intraday dip the position would have recovered from by the next
# cycle, with no equivalent mechanism giving a winner room to run to its
# target — the resting stop fires on every touch, the cycle-evaluated target
# only fires if it is still through on the next check. `NONE` trades the gap
# protection away for that: nothing rests, and both the stop and the target
# are `plays.exits` decisions, evaluated fresh once per cycle, same cadence,
# same footing.
REST_KINDS = (STOP, TARGET, NONE)


def rest_kind(ctx: Mapping[str, Any]) -> str:
    """Which single leg rests at the broker, per `protection_rest_kind`.

    Falls back to `STOP` on anything unrecognised. `cli._validate_config`
    raises on a typo before a cycle ever runs, so this default is for a caller
    that built its own config rather than for an operator mistake — and of the
    two legs the stop is the one whose absence costs money.
    """
    kind = (ctx.get("config") or {}).get("protection_rest_kind")
    return kind if kind in REST_KINDS else STOP

# The whole point of a resting order. `time_in_force: gtc` is what makes it
# survive to the next cycle, and it is also what `reconcile._order_findings`
# and §13's postclose sweep have to be told to leave alone — see the
# `protective_stop` skip there, which this change extends to `protective_target`.
GTC = "gtc"

# `stop_market` is regular-hours-only at this broker (`guard.legality`'s
# `_REGULAR_HOURS_ONLY_TYPES`), and an order tagged to another session queues
# rather than rejects, which is worse: it looks placed and protects nothing.
# The target limit takes the same session for no reason beyond symmetry —
# there is nothing to gain from a take-profit that fills into an overnight
# book five cents better and a thin overnight print to lose to.
SESSION = REGULAR_HOURS


@dataclass(frozen=True)
class Protection:
    """One order that should be resting at the broker, and why.

    Order-shaped for `guard.evaluate`'s sake, the same way `plays.Exit` is: it
    is still only an intent, and guard decides whether it may be placed.
    """

    symbol: str
    play_id: str | None
    kind: str
    quantity: float
    order_type: str
    stop_price: float | None
    limit_price: float | None
    reason: str

    @property
    def price(self) -> float:
        """The one number that decides whether a resting order is *this* order."""
        return self.stop_price if self.kind == STOP else self.limit_price

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "play_id": self.play_id,
            "kind": self.kind,
            "quantity": self.quantity,
            "order_type": self.order_type,
            "stop_price": self.stop_price,
            "limit_price": self.limit_price,
            "reason": self.reason,
        }

    def as_order(self) -> dict:
        """Exactly `guard.evaluate`'s order shape, plus the two flags that make
        this order recognisable to everything downstream.

        `protective_stop` already had a meaning — `reconcile` skips a placement
        carrying it when building the postclose cancel list, so a resting
        stop is not swept up with the unfilled entries.
        `protective_target` is the same promise for the other side of the
        trade, and `guard.core` reads both to tell a contingent resting order
        apart from a competing intent on the same symbol.
        """
        return {
            "symbol": self.symbol,
            "side": "sell",
            "type": self.order_type,
            "quantity": self.quantity,
            "limit_price": self.limit_price,
            "stop_price": self.stop_price,
            "session": SESSION,
            "time_in_force": GTC,
            "play_id": self.play_id,
            "protective_stop": self.kind == STOP,
            "protective_target": self.kind == TARGET,
        }


@dataclass(frozen=True)
class Desired:
    """What should be resting, and what could not be computed.

    ``notes`` is not decoration. Every case this module declines to protect is
    a position running without a broker-side stop, which is precisely the
    silence §10.1 was written against — a missing ATR reads identically to a
    healthy book unless something says so out loud.
    """

    orders: list[Protection]
    notes: list[str]

    def as_dict(self) -> dict:
        return {
            "orders": [o.as_dict() for o in self.orders],
            "notes": list(self.notes),
        }


def high_since_entry(held: Mapping[str, Any]) -> float | None:
    """The position's own peak, which is what a trailing stop trails.

    Maintained by `reconcile._high_since_entry` and ratcheted there, so this
    is a read rather than a computation. **Fails closed to the entry price**:
    a book row written before that field existed, or one whose quotes never
    arrived, yields a floor of ``entry − trail × ATR`` measured from entry,
    which is looser than the fixed stop and therefore never tightens a stop
    on evidence the system does not have.
    """
    entry = held.get("entry_price")
    high = held.get("high_since_entry")

    if high is None:
        return None if entry is None else float(entry)
    if entry is None:
        return float(high)

    # The ratchet is reconcile's, but a floor of "at least the entry price"
    # costs nothing to restate and makes this function correct on its own.
    return max(float(high), float(entry))


def stop_price(held: Mapping[str, Any], ctx: Mapping[str, Any]) -> float | None:
    """§6.5 exit 4's stop, at the higher of the fixed floor and the trailing one.

    ``max`` rather than a choice between the two: the fixed stop is the entry
    thesis being wrong, and the trailing stop is a gain being given back. A
    position 30% up whose fixed stop still sits below its entry is not
    protected by that stop in any sense the word usually carries, and a
    position that has never been up has no trailing floor above its fixed one,
    so the same expression covers both without a branch.

    Ratcheting is a property of the *input*, not of this function: `high` only
    rises, so the number this returns only rises, and a stop is never widened
    by a bad day. Returns ``None`` on a missing ATR or entry price — the same
    refusal to guess `cli.protective_stops` already makes.
    """
    atr = _signals(ctx, held.get("symbol")).get("atr")
    entry = held.get("entry_price")
    # A disputed entry price is no entry price, the same reading
    # `exits._reason_to_leave` makes of the same flag: the resting stop and the
    # cycle-time sell are supposed to be one number, so a basis one of them
    # refuses to measure from cannot be one the other measures from anyway.
    # `reconcile._protection_plan` already drops the symbol from both halves of
    # its diff before it gets here; this is the second lock, for
    # `cli.protective_stops`, which calls this function directly.
    if atr is None or entry is None or held.get("entry_price_disputed"):
        return None

    atr = float(atr)
    # The play's own multiple where it declares one (`SECTOR`, 1.5x per §4.3 C)
    # and `config.atr_stop_multiple` otherwise. `exits`'s item 4 resolves it
    # identically, which is what keeps the resting order and the cycle-time
    # sell at the same price.
    fixed = float(entry) - stop_multiple(held.get("play_id"), ctx) * atr
    trailing = high_since_entry(held) - float(ctx["config"]["atr_trail_multiple"]) * atr
    return round(max(fixed, trailing), 2)


def target_price(
    held: Mapping[str, Any], ctx: Mapping[str, Any]
) -> tuple[float, str, float] | None:
    """§6.5 exit 6's target as a price, or ``None`` where the play has none.

    The same resolution `exits._reason_to_leave` makes — the play's own target,
    adoption's floor where the play declares none and the position was
    inherited, §4.3 A's 1.5x-stop-distance R-multiple for a `DIP` taken under
    the pullback reshape, nothing at all for a `MANUAL` row with no play — kept
    in step with it by both calling `core.target_pct` rather than by either
    restating the percentages.

    Grew a ``ctx`` argument, because an R-multiple target needs the position's
    ATR and a config flag and neither is on the book row.
    """
    entry = held.get("entry_price")
    resolved = _target_pct(held, ctx)
    if entry is None or resolved is None or held.get("entry_price_disputed"):
        return None

    pct, owner = resolved
    return round(float(entry) * (1.0 + pct), 2), owner, pct


def _coverable(held: Mapping[str, Any], ctx: Mapping[str, Any]) -> bool:
    """Whether this position may carry resting orders at all.

    Both refusals are silent — neither is this cycle's news. §10.1 gives a
    fractional position no resting order and exits it at cycle granularity,
    which is a standing condition of the book rather than an event; and a
    symbol the Dip Recovery side owns is not this side's to place against at
    all (`guard.lock` denies it regardless, the same defense in depth
    `exits.exits` already applies).
    """
    quantity = float(held.get("quantity") or 0.0)
    if quantity < 1.0 or not _is_whole_share(quantity):
        return False

    owner = (ctx.get("symbol_lock") or {}).get(held.get("symbol"))
    return owner is None or owner == (ctx.get("strategy") or "core")


def desired(ctx: Mapping[str, Any]) -> Desired:
    """Every resting order the book should have behind it, this cycle.

    Computed for the whole book on every cycle rather than once per fill,
    which is the change: a position adopted from the operator, or one whose
    stop was cancelled three weeks ago, is indistinguishable here from one
    bought this morning.

    **At most one order per position, never two** — see `REST_KINDS`. Which
    leg it is is `protection_rest_kind`'s to say; everything below is written
    so that the leg that does not rest costs nothing at all, not even a note.

    **`NONE` proposes nothing, for any position, full stop.** No stop price is
    computed, no note is raised for a missing ATR or a disputed entry — there
    is no resting order to have an opinion about. `reconcile.protection_plan`
    still runs against an empty `orders` list, which is exactly what makes it
    cancel anything left resting from before this mode was set.
    """
    orders: list[Protection] = []
    notes: list[str] = []
    wanted = rest_kind(ctx)

    if wanted == NONE:
        return Desired(orders=orders, notes=notes)

    for held in ctx.get("positions") or []:
        if not _coverable(held, ctx):
            continue

        symbol = held.get("symbol")
        play_id = held.get("play_id")
        quantity = float(held.get("quantity") or 0.0)
        price = _price(ctx, symbol)

        if wanted == TARGET:
            target = target_price(held, ctx)
            if target is None:
                # Not a note. `MANUAL` is the only row that reaches here
                # without a target, and §12.2 is explicit that the system does
                # not invent exit terms for a position it never chose.
                continue
            limit, owner, pct = target
            orders.append(
                Protection(
                    symbol=symbol,
                    play_id=play_id,
                    kind=TARGET,
                    quantity=quantity,
                    order_type=LIMIT,
                    stop_price=None,
                    limit_price=limit,
                    reason=(
                        f"{symbol} rests at {limit:.2f}: {owner}'s {pct:.0%} target "
                        f"above a {float(held['entry_price']):.2f} entry"
                    ),
                )
            )
            continue

        stop = stop_price(held, ctx)
        if stop is None:
            notes.append(
                f"{symbol}: no resting stop — entry price or ATR is missing "
                "(or the entry price is disputed between the broker's average "
                "and its tax lots), and §10.1's protection is not a number to "
                "guess at"
            )
        elif price is not None and stop >= price:
            # A sell stop at or above the market triggers the moment it is
            # accepted, and a triggered `stop_market` is a market order — the
            # one order shape an earlier limit-only ruling removed from this
            # system entirely. The position is already through its stop, so
            # `exits._reason_to_leave` is the right instrument: it prices the
            # same sale as a limit at the bid.
            notes.append(
                f"{symbol}: trailing stop {stop:.2f} is at or above the "
                f"{price:.2f} market, so it would place as a market sell; "
                "left to §6.5's exit path this cycle"
            )
        else:
            entry = float(held["entry_price"])
            high = high_since_entry(held)
            orders.append(
                Protection(
                    symbol=symbol,
                    play_id=play_id,
                    kind=STOP,
                    quantity=quantity,
                    order_type=STOP_MARKET,
                    stop_price=stop,
                    limit_price=None,
                    reason=(
                        f"{symbol} rests at {stop:.2f}: the higher of "
                        f"{stop_multiple(play_id, ctx):g}xATR below a "
                        f"{entry:.2f} entry and "
                        f"{float(ctx['config']['atr_trail_multiple']):g}xATR below "
                        f"its {high:.2f} high"
                    ),
                )
            )

    return Desired(orders=orders, notes=notes)


# --- what already rests -----------------------------------------------------


def resting_kind(order: Mapping[str, Any]) -> str | None:
    """Which kind of resting protection a broker order looks like, if any.

    Read from the order's own shape because the broker returns no flag of
    ours: `placements` carries `protective_stop`/`protective_target` and
    `get_equity_orders` does not, so anything reading the broker's list — and
    `guard._PlanState` is the one that matters — has to recognise the shape.

    **`place_equity_order` takes a `stop_market` and `get_equity_orders` hands
    it back as `type: "market"` with `trigger: "stop"` and a `stop_price`.**
    The two are different vocabularies for the same order, confirmed against
    a resting stop on a live account, and matching only
    on the placement word was the first version of this function — which
    recognised nothing the broker actually returns. That failed twice over:
    the resting stop read as an ordinary competing sell, so every exit on the
    name would have been denied `DUPLICATE_OPEN_ORDER`, and the protection
    plan saw no stop resting and would have placed a second one over a
    position that already had five of its six shares committed.

    So a stop is recognised three ways, any of which is sufficient: the
    placement type, the read-back trigger, or simply carrying a stop price. A
    sell limit is protection only if it is `gtc` — every order this system
    places from `plan.orders` is a day order, so a resting good-till-cancelled
    sell limit is either one of ours or the operator's own take-profit, and
    both are contingent cover rather than a competing intent. Anything else —
    a buy, a day limit — is left to the duplicate-order rule that has always
    governed it.
    """
    if order.get("side") != "sell":
        return None
    if (
        order.get("type") in (STOP_MARKET, "stop_limit")
        or str(order.get("trigger", "")).lower() == "stop"
        or order.get("stop_price") is not None
    ):
        return STOP
    if order.get("type") == LIMIT and str(order.get("time_in_force", "")).lower() == GTC:
        return TARGET
    return None


def resting_price(order: Mapping[str, Any]) -> float | None:
    """The price a resting order rests at, under either field name.

    A limit's price is `limit_price` on the way out and **`price`** on the way
    back — the same placement/read-back split `resting_kind` documents, and
    the same live-verified source (the byte-identical F limit order). Reading
    only one name would classify a live take-profit correctly and then fail to
    price it, which reads downstream as "no equivalent order rests" and places
    a duplicate.
    """
    kind = resting_kind(order)
    if kind is None:
        return None
    if kind == STOP:
        price = order.get("stop_price")
    else:
        price = order.get("limit_price")
        if price is None:
            price = order.get("price")
    return None if price is None else float(price)


AGENTIC = "agentic"


def is_ours_to_replace(order: Mapping[str, Any], ours: Mapping[str, Any]) -> bool:
    """Whether this side may cancel a resting order, as opposed to merely
    reading it.

    **§13's oldest safety rule: "a phantom order is never cancelled, at any
    hour — it may be the operator's."** Without this the protection diff
    cancels an operator's own take-profit and replaces it with the system's,
    which on a name the operator is holding out for is a sale tens of percent
    below where they meant to sell. Confirmed by simulation: a manual GTC
    limit sell at 25.00 produced `replace(operator-1 -> 15.90)` in the same
    cycle reconciliation flagged it `PHANTOM_ORDER`.

    Two ways to qualify, because neither alone is enough:

    * ``order_id`` in this cycle's own placements. Exact, but short-lived —
      `edge_prior()` hands back only the most recent batch that placed
      something, and a resting stop outlives that by design. It is the whole
      reason this module exists.
    * ``placed_agent == "agentic"``, the broker's own provenance flag, which
      is durable. Verified live on a resting stop.

    Anything else — `placed_agent: "user"`, or simply absent — is read as the
    operator's. That direction is deliberate: an order of unknown provenance
    left alone costs one stale resting order that `reconcile`'s ordinary
    `STALE_OPEN_ORDER` path still reports, while cancelling one costs the
    operator a trade they placed on purpose.
    """
    order_id = order.get("order_id")
    if order_id is not None and order_id in ours:
        return True
    return str(order.get("placed_agent", "")).lower() == AGENTIC


def order_kind(order: Mapping[str, Any]) -> str | None:
    """Which kind of protection an order *of ours* is, from its own flags.

    Distinct from `resting_kind` on purpose: this reads the two booleans
    `Protection.as_order` sets, so a proposed order is classified by what it
    was built to be rather than by what its shape resembles. Guard needs both
    — one for the plan it is judging, one for the broker list it is judging
    against.
    """
    if order.get("protective_stop"):
        return STOP
    if order.get("protective_target"):
        return TARGET
    return None


def within_band(desired_price: float, resting: float, band: float) -> bool:
    """Whether a resting order is close enough to the desired one to leave alone.

    `protection_replace_min_pct`, measured against the desired price. Without
    a band, a trailing stop that ratchets by a cent a day would cancel and
    replace a resting order every cycle — six broker round trips a day per
    position, each one a window in which the position is unprotected, to move
    a stop by less than a tick of most spreads.
    """
    if desired_price is None or resting is None or desired_price <= 0:
        return False
    return abs(resting - desired_price) / desired_price <= band
