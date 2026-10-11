"""reconcile.py — intent versus broker truth.

Design v3 §12 and §11. Manual trades must absorb with no operator
intervention, ever — an explicit requirement, and Invariant 7 in practice. The
module compares what the last cycle said it did against what the broker says
happened, classifies every disagreement into one of §12.2's six kinds, and
carries the broker's answer forward.

**Everything is reported, never silently repaired. The broker wins every
decision.** The only action this module takes is a cancel, which §12.2 names
outright; it never emits a buy or a sell. Placing an order to close a position
delta would be repairing the difference rather than reporting it, and the
difference is the one piece of evidence that something is wrong.

Reconciliation is also where three other modules get numbers they cannot
obtain for themselves:

* `plays.py` reads ``entry_price`` and ``sessions_held`` off the book here.
  Three of §6.5's seven exits depend on them, and all three fail *silently*
  when they are absent — a system that reads as one holding forever.
* `cadence.py` reads four ``edge_control`` fields to reach §13's no-op
  decision from one Supabase query, before touching Robinhood.
* `guard.py` reads the §9 loss ledger and the entry block.
* `report.py` reads ``edge_play_state.realized_pnl``, which is accumulated
  here because this is the only module that sees a fill, the broker's own
  realized figure for it (`ctx["realized"]`) or its tax-lot basis, and the
  play that owns the symbol at the same moment.

§12.1 places this in the routine rather than in CI, because GitHub Actions
cannot see Robinhood and can therefore only verify that reconciliation
happened, never its numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence

# One calendar, one counting rule. `sessions_between` moved to `cadence.py`
# when `plays.py` needed it too — this module still owns the *meaning* of
# a held session, but not a second implementation of the arithmetic.
from src.cadence import ET, is_open, sessions_between
from src.cadence import trading_day as session_day
from src import core_sleeve
from src.plays import ADOPTED, CORE, MANUAL, protection, would_open
from src.sizing import MONEY_EPS, anchored_mark, flow_adjusted_equity, raw_equity

# §12.2's table, verbatim. Six classes, three responses: alert and proceed,
# block entries, or absorb.
MISSING_ORDER = "MISSING_ORDER"
PHANTOM_ORDER = "PHANTOM_ORDER"
UNTRACKED_FILL = "UNTRACKED_FILL"
QUANTITY_DRIFT = "QUANTITY_DRIFT"
POSITION_DELTA_MISMATCH = "POSITION_DELTA_MISMATCH"
STALE_OPEN_ORDER = "STALE_OPEN_ORDER"
RECONCILIATION_DIVERGENCE_PERSISTENT = "RECONCILIATION_DIVERGENCE_PERSISTENT"

# The one class §12.2 does not name, added because the alternative is silence.
# A position whose basis the broker did not hand over has no ATR stop and no
# target in `plays.py`, and both are skipped without a word — the failure shape
# this module exists to make impossible. §12.2's "everything is reported"
# covers it even though its table does not.
POSITION_BASIS_UNKNOWN = "POSITION_BASIS_UNKNOWN"

# `POSITION_BASIS_UNKNOWN` is "no feed gave a basis"; this is "two feeds gave
# one and they do not agree", which until now was indistinguishable from a
# healthy position because only one of the two was ever read.
#
# The case this guards against: a book carries a position at an `entry_price`
# well above the price its only lot was actually bought at. The 2xATR stop
# measured down from that wrong entry lands a few cents under the position's
# own fill, and the position stops out the same afternoon. It is not a blend
# of a stale lot with a fresh one: there is one lot, and its cost is simply
# wrong. Nothing else can catch that, because `entry_price`, `last_fill_price`
# and `high_since_entry` are all derived from that same one lot and all agree
# with each other.
#
# The broker's `average_buy_price` on `get_equity_positions` does not: it is
# the average cost of the shares *currently held* and cannot see a closed lot. That is a second, independent
# witness to the same number, arriving on the one call that establishes the
# position exists at all — and this class is what it says when it disagrees.
#
# Deliberately outside `DIVERGENCE_CLASSES`: it is a fact about one position's
# cost basis, not a contradiction about what the account holds, and blocking
# every entry in the book over one name's disputed basis would be a much larger
# response than the evidence supports. The response it does get is narrower and
# sharper — `plays.exits` and `plays.protection` stand every entry-anchored
# judgment down on that symbol, and `manual_required` says so by name.
POSITION_ENTRY_DISPUTED = "POSITION_ENTRY_DISPUTED"

# A second class §12.2 does not name, added after a live incident: a DIP entry
# intended for one symbol filled under another instead — same quantity, same price to the
# cent, a different symbol, because a session resolved the ticker by company
# name instead of using the plan's own exact symbol. Read alone, that incident
# was two ordinary `POSITION_DELTA_MISMATCH` rows (one expected and missing,
# one held and unexpected) and nothing tied them together; a session had to
# notice the coincidence by eye and halted the whole cycle out of caution
# rather than trust the ordinary divergence handling, which was already
# equipped to absorb it (`_attribution`'s adoption pass). Naming the
# fingerprint — same quantity, same price, different symbol — turns "eyeball
# the noticed it" into something reconciliation reports on its own, so the next
# occurrence reads as one finding with both symbols in it rather than a
# scramble to spot the pattern under time pressure.
SYMBOL_SWAP_SUSPECTED = "SYMBOL_SWAP_SUSPECTED"

# A third class §12.2 does not name, added with Dip Recovery phase 6. The
# order-shaped half of `_other_side_symbols`: an open order on a symbol the
# other strategy owns is not a `PHANTOM_ORDER`, because a phantom is by
# definition an order *nobody* claims, and this one is claimed — by the side
# that placed it, in the same lock this module already reads for the book.
#
# It is reported rather than dropped, and that is the whole point of it being
# its own class instead of a filter. Positions could simply be excluded because
# the other side reconciles its own book; **nothing on either side scans
# working orders except this module** (`dip-monitor-prompt.md` Step 5 reads
# `get_equity_orders state=filled` only), so an order silently filtered out
# here is an order no part of the system is looking at. That includes the
# narrow case this class exists to keep visible: an operator trading by hand in
# a name the other strategy owns.
#
# Deliberately outside `DIVERGENCE_CLASSES`, exactly like `UNTRACKED_FILL` and
# for the same reason — it is a fully explained fact, not a contradiction, and
# counting it would block core's entries for as long as the other side happens
# to have an order resting.
OTHER_STRATEGY_ORDER = "OTHER_STRATEGY_ORDER"

# What "open" means to Robinhood, defined here because broker order state is
# this module's subject. `guard.py` imports it: two definitions that merely
# agreed today would drift, and a guard that disagreed with its reconciler
# about which orders are live would deny a second sell on a symbol whose first
# one had already been cancelled.
TERMINAL_ORDER_STATES = frozenset({"filled", "cancelled", "canceled", "rejected"})

# §12.2's "still open after 2 cycles", counted in §12.1's cycles — full,
# monitor and postclose, the only three this module runs on. Settled. The
# weekend ambiguity the count could have had never arises: §13's
# post-close cycle cancels every unfilled order, so no order of ours lives
# to see a second session.
STALE_ORDER_CYCLES = 2

# §12.2's "persistent divergence across two consecutive cycles".
PERSISTENT_DIVERGENCE_CYCLES = 2

# Which classes count as divergence, and the two that deliberately do not.
# `UNTRACKED_FILL` is the operator trading, which §12.2 calls a supported
# workflow; `STALE_OPEN_ORDER` is an order that has not filled yet. Both are
# fully explained, and counting either would block entries on a working system.
# `POSITION_BASIS_UNKNOWN` is a missing input, not a contradicted one.
# `SYMBOL_SWAP_SUSPECTED` always accompanies a `POSITION_DELTA_MISMATCH` pair
# rather than replacing it, so including it here adds no new way to block
# entries — it only makes the existing block's reason legible.
DIVERGENCE_CLASSES = frozenset(
    {
        MISSING_ORDER,
        PHANTOM_ORDER,
        QUANTITY_DRIFT,
        POSITION_DELTA_MISMATCH,
        SYMBOL_SWAP_SUSPECTED,
    }
)

# The unfilled-entry sweep §12.2 does not name, added. A GFD entry limit at
# the ask sat unfilled on 3 of 4 buys and then died at the post-close with
# nothing re-tried. This cancels an entry that has worked longer than
# `entry_resubmit_after_min` while the day can still act on it, and re-queues
# the symbol for the next cycle's `cli.candidates` to re-price and re-propose.
# Deliberately outside `DIVERGENCE_CLASSES`, exactly like `STALE_OPEN_ORDER`:
# it is an action this side took on its own order, fully explained, not a
# contradiction.
ENTRY_RESUBMIT = "ENTRY_RESUBMIT"

# `edge_control.operator_holds`
# used to say "No expires after N days" — this is the finding that says one
# just did. Deliberately outside `DIVERGENCE_CLASSES` and outside
# `report.SILENT_FINDINGS`: it is not a disagreement (nothing contradicts
# anything) but it is not silent either, because it changes what
# `plays.exits` will do to a symbol the operator once protected by hand — the
# one class of finding this file's own precedent (`ENTRY_RESUBMIT`,
# `STALE_OPEN_ORDER`) says gets a Finding without joining the divergence set.
OPERATOR_HOLD_EXPIRED = "OPERATOR_HOLD_EXPIRED"

POSTCLOSE = "postclose"
BUY = "buy"
SELL = "sell"


@dataclass(frozen=True)
class Finding:
    """One disagreement, named and left for a human to read."""

    code: str
    symbol: str
    detail: str
    order_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "symbol": self.symbol,
            "detail": self.detail,
            "order_id": self.order_id,
        }


@dataclass(frozen=True)
class Position:
    """A position as the broker reports it, plus what `plays.py` needs to exit it.

    ``entry_price`` and ``sessions_held`` are both derived from
    `get_equity_tax_lots` and the market calendar rather than remembered from
    intent. The broker's lots survive a missed cycle, a restarted routine and an
    adopted manual buy; a counter survives none of those, and it fails by
    under-counting, which delays every time stop at once.

    ``adopted`` is the other half of that inheritance, and `plays.py` has been
    reading it while nothing wrote it — §6.5's exits 5 and 6 both branch on it, so
    an inherited position silently had neither a time stop nor a target. It means
    *the system did not choose to open this*, which stays true for the position's
    whole life: it is carried forward off the prior book rather than re-derived,
    because by the second cycle the name is in ``prior_positions`` and no longer
    looks inherited at all.
    """

    symbol: str
    play_id: str
    quantity: float
    entry_price: float | None
    sessions_held: int | None
    adopted: bool = False
    adopted_on: str | None = None
    # The newest open lot's acquisition date — the mirror image of
    # `_held_since`'s oldest. `plays.exits`'s same-day-fill guard (after a
    # same-day MU buy-then-sell) reads this to refuse a risk-judgment exit
    # on a position that also picked up a fill today: the position has no
    # trajectory yet for a stop, a target or a signal to be evidence about.
    # Scaling into an existing position moves this forward the same way an
    # average-down should re-arm the guard; `_held_since`'s clock
    # deliberately does not move, and the two answer different questions.
    last_fill_on: str | None = None
    # The average-down floor's other two numbers (§6.5.4). `last_fill_price`
    # is None on exactly the cycles `last_fill_on` is — no lot, no price.
    # `open_lot_count` defaults to 0 for the same reason `last_fill_on`
    # defaults to None: a position with no tax lot has nothing here to report.
    last_fill_price: float | None = None
    open_lot_count: int = 0
    # The position's own peak since it was opened, ratcheted by
    # `_high_since_entry` on every reconciling cycle. `plays.protection` trails
    # the resting stop from this rather than from the entry price, which is the
    # whole of what makes the stop a trailing one ("fixed from entry; no
    # trailing").
    #
    # Carried on the position rather than in an `edge_control` map for one
    # reason: it must not outlive the position. A symbol sold and re-bought a
    # month later is a new thesis at a new basis, and a peak inherited from the
    # old one would put the new position's stop above its own entry on day one.
    # The book is rebuilt from the broker's positions every cycle, so a closed
    # position drops its high with no cleanup pass and no key to prune.
    high_since_entry: float | None = None
    # What the broker will actually let this side sell right now, and what it
    # has already committed to a resting order. **Nothing reads these yet.**
    # They are carried so that the rule which fixes it (one resting order per
    # position, and a guard denial when a plan proposes a second sell of
    # reserved shares) has a number to read rather than an inference to make.
    #
    # `None`, never `0.0`, for a broker that did not send them: a real zero
    # means "every share is committed" and would deny every exit on the name.
    shares_available_for_sells: float | None = None
    shares_held_for_sells: float | None = None
    # --- the entry anchor -------------------------
    #
    # Which feed `entry_price` above actually came from, and when it was taken.
    # `_anchored_entry` is the whole story; these three fields are what let it
    # be *carried* rather than recomputed, and what let a future audit answer
    # "where did that number come from" without a code trace — which is
    # precisely what an unexplained stop-out costs.
    #
    # `entry_anchored_on` is the `_held_since` date the anchor was taken
    # against. It is the round-trip detector: while the oldest open lot is the
    # same lot, these are the same shares and the anchor still describes them;
    # the moment it moves forward, the position those shares belonged to is
    # gone and the anchor must be retaken.
    entry_price_source: str | None = None
    entry_anchored_on: str | None = None
    # The two feeds disagree about this position's cost by more than
    # `entry_price_divergence_pct`. Every entry-anchored judgment — the ATR
    # stop, the target, the resting stop — stands down on it rather than
    # picking a side. See `_anchored_entry`.
    entry_price_disputed: bool = False

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "play_id": self.play_id,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "sessions_held": self.sessions_held,
            "adopted": self.adopted,
            "adopted_on": self.adopted_on,
            "last_fill_on": self.last_fill_on,
            "last_fill_price": self.last_fill_price,
            "open_lot_count": self.open_lot_count,
            "high_since_entry": self.high_since_entry,
            "shares_available_for_sells": self.shares_available_for_sells,
            "shares_held_for_sells": self.shares_held_for_sells,
            "entry_price_source": self.entry_price_source,
            "entry_anchored_on": self.entry_anchored_on,
            "entry_price_disputed": self.entry_price_disputed,
        }


@dataclass
class ReconcileResult:
    findings: list[Finding] = field(default_factory=list)
    positions: list[Position] = field(default_factory=list)
    cancels: list[dict] = field(default_factory=list)
    control_updates: dict = field(default_factory=dict)
    loss_closes: list[dict] = field(default_factory=list)
    play_state_updates: dict = field(default_factory=dict)
    # Dip Recovery plan phase 2. Every symbol in `positions`, not just a
    # symbol that just filled — see `_symbol_lock_claims` for why "claim
    # everything held, every cycle" is the rule rather than "claim on fill".
    # The routine calls `edge_symbol_lock_claim` once per entry, idempotently;
    # a symbol already locked to this side is a no-op, and one already locked
    # to the other side stays that way (the call returns false, which is a
    # cross-strategy conflict worth surfacing, not silently overwriting).
    symbol_lock_claims: list[str] = field(default_factory=list)
    # The mirror image: a symbol this side's lock map says it owns that is no
    # longer in `positions` — the position closed, and the lock should not
    # outlive it. The routine calls `edge_symbol_lock_release` once per exit.
    symbol_lock_releases: list[str] = field(default_factory=list)
    # §1.3/§4.2 telemetry: one dict per terminal order this side placed —
    # intended vs filled quantity, limit vs average fill price, minutes to
    # fill. Written to `edge_fill_quality` beside the reconcile runlog row.
    # Observational: it adds no finding, no cancel and no control update, and
    # reads only the `orders`/`fills` reconciliation already has.
    fills_quality: list[dict] = field(default_factory=list)
    # §10.1's resting orders, diffed rather than placed — see
    # `_protection_plan`. `cli.decide` puts the survivors of `guard.evaluate`
    # into `plan.protection`; this module still never places anything, and a
    # `replace` entry is a cancel this side already had the authority to make.
    protection_plan: dict = field(default_factory=dict)
    # One row per closing trade the broker's `realized` read reports, for
    # `edge_realized_trades_append` — see `_realized_trades`. The RPC is the
    # only writer of `edge_play_state.realized_pnl`.
    realized_trades: list[dict] = field(default_factory=list)
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "findings": [f.as_dict() for f in self.findings],
            "positions": [p.as_dict() for p in self.positions],
            "cancels": [dict(c) for c in self.cancels],
            "control_updates": dict(self.control_updates),
            "loss_closes": [dict(r) for r in self.loss_closes],
            "play_state_updates": {
                play: dict(state) for play, state in self.play_state_updates.items()
            },
            "symbol_lock_claims": list(self.symbol_lock_claims),
            "symbol_lock_releases": list(self.symbol_lock_releases),
            "fills_quality": [dict(r) for r in self.fills_quality],
            "protection_plan": dict(self.protection_plan),
            "realized_trades": [dict(r) for r in self.realized_trades],
            "reason": self.reason,
        }


def _moment(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _quantity(row: Mapping[str, Any]) -> float:
    return float(row.get("quantity") or 0.0)


def _same(a: float, b: float, epsilon: float) -> bool:
    """§12.2's `reconcile_epsilon`. Six-decimal quantities make a bare `!=` a
    permanent false positive on every fractional order the account holds."""
    return abs(a - b) <= epsilon


def _is_open_order(order: Mapping[str, Any]) -> bool:
    return str(order.get("state", "")).lower() not in TERMINAL_ORDER_STATES


def _lots(ctx: Mapping[str, Any], symbol: str) -> list[Mapping[str, Any]]:
    return list((ctx.get("tax_lots") or {}).get(symbol) or [])


def _basis(lots: Sequence[Mapping[str, Any]]) -> float | None:
    """One position, one entry price: the weighted average of its open lots.

    Sizing can scale into a name across cycles, and the 2xATR stop belongs to
    the position rather than to whichever lot the broker happens to list first.
    """
    shares = sum(_quantity(lot) for lot in lots)
    if shares <= 0:
        return None
    cost = sum(float(lot.get("cost_basis") or 0.0) for lot in lots)
    return round(cost / shares, 6)


# Where an anchored entry price came from, in the order they are preferred.
ENTRY_FROM_CARRY = "carried"
ENTRY_FROM_BROKER_AVERAGE = "broker_average"
ENTRY_FROM_TAX_LOTS = "tax_lots"
# The anchor above, cleared against a tax-lot basis that sits *above* it by no
# more than the losses §9's ledger holds on the symbol nearby — see
# `_wash_sale_excess`. Recorded as its own source so a runlog row says why a
# gap that would otherwise be a dispute was let through.
ENTRY_FROM_WASH_SALE_ADJUSTED = "wash_sale_adjusted"

# `config.near_flat_max_dollars` / `near_flat_max_pct` when the config does
# not carry them. See `_realized_trades`.
NEAR_FLAT_MAX_DOLLARS = 1.00
NEAR_FLAT_MAX_PCT = 0.001

# IRC §1091's window: a loss sale within 30 days either side of a purchase of
# the same security. Calendar days, the same unit `wash_sale_days` and
# `edge_loss_closes.close_date` already use.
WASH_SALE_WINDOW_DAYS = 30
# What the broker's two-decimal `tax_cost_basis` and the ledger's two-decimal
# `loss_amount` can disagree by without the gap meaning anything. A real
# case sat $0.015 inside the loss (5.90 of a pro-rated 5.915).
WASH_SALE_ROUNDING_USD = 0.05

# `config.entry_price_divergence_pct` when the config does not carry it. One
# percent is well outside anything the two feeds disagree by in practice —
# checked live across a book of open positions, where the largest gap between
# `average_buy_price` and the tax-lot weighted basis was 0.005% — and well
# inside the 4.5% of a basis that is actually wrong.
ENTRY_DIVERGENCE_PCT_DEFAULT = 0.01


@dataclass(frozen=True)
class _Anchor:
    """One position's entry price, where it came from, and whether it is trusted."""

    price: float | None
    source: str | None
    anchored_on: str | None
    disputed: bool
    detail: str | None = None


def _diverges(a: float, b: float, tolerance: float) -> bool:
    """Whether two readings of the same price disagree by more than `tolerance`.

    Measured against the larger of the two so the answer does not depend on
    which one is passed first — a 4% gap is a 4% gap in either direction, and a
    check that said otherwise would fire on half the pairs it should.
    """
    scale = max(abs(a), abs(b))
    if scale <= 0.0:
        return False
    return abs(a - b) / scale > tolerance


def _wash_sale_excess(
    symbol: Any,
    lots: Sequence[Mapping[str, Any]],
    price: float,
    ctx: Mapping[str, Any],
) -> float | None:
    """How far the tax lots sit above `price`, when a wash sale explains it.

    **Why a tax lot can legitimately cost more than the fill.** When a loss
    sale falls within 30 days of a purchase of the same symbol, the IRS
    disallows the loss and the broker adds it to the replacement lot's
    `tax_cost_basis`. `get_equity_tax_lots` carries no field saying so. Sell 8
    shares at a $6.76 loss and buy 7 back at 27.15, and the new lot reads
    195.95, or 27.99 a share — $5.90 over the fill, 7/8 of the loss. Against
    `entry_price_divergence_pct` that reads as a disputed entry, and a
    disputed entry has no stop management at all, so the position would sit
    with nothing resting behind it.

    Inferred, never assumed: the excess must be positive (a disallowed loss
    only ever *raises* basis) and no larger than the measured losses
    `edge_loss_closes` holds for the symbol within the window of *any* open lot
    — the adjusted lot is the replacement, which on a top-up is the newest one,
    not the oldest. A `basis_unknown` row carries no amount and counts for
    nothing. Returns ``None`` when the ledger does not account for the gap,
    which leaves the dispute exactly as it was.
    """
    shares = sum(_quantity(lot) for lot in lots)
    if shares <= 0:
        return None
    excess = sum(float(lot.get("cost_basis") or 0.0) for lot in lots) - price * shares
    if excess <= 0:
        return None

    opened = [
        date.fromisoformat(str(lot["acquired_date"])[:10])
        for lot in lots
        if lot.get("acquired_date")
    ]
    if not opened:
        return None

    window = timedelta(days=WASH_SALE_WINDOW_DAYS)
    explained = 0.0
    for row in ctx.get("loss_closes") or []:
        amount = _num(row.get("loss_amount"))
        if row.get("symbol") != symbol or amount is None or row.get("basis_unknown"):
            continue
        closed = row.get("close_date")
        if not closed:
            continue
        closed_on = date.fromisoformat(str(closed)[:10])
        if any(abs(closed_on - day) <= window for day in opened):
            explained += amount

    if excess <= explained + WASH_SALE_ROUNDING_USD:
        return round(excess, 2)
    return None


def _anchored_entry(
    held: Mapping[str, Any],
    lots: Sequence[Mapping[str, Any]],
    prior: Mapping[str, Any],
    ctx: Mapping[str, Any],
    epsilon: float,
) -> _Anchor:
    """The entry price §6.5's stop is measured down from, and where it came from.

    Three things were wrong with that, and one audited exit hit all three at
    once.

    **The tax-lot feed is not the only witness, and it is not the best one.**
    `get_equity_positions` carries `average_buy_price` — the average cost of
    the shares *currently held*, which the broker resets on a full close and
    reblends on an add, and which arrives on the same single call that says the
    position exists. It is preferred here. Tax lots keep the two jobs they are
    uniquely good at and that `average_buy_price` cannot do at all: dating the
    position (`_held_since`) and counting how often it has been added to.

    **A stop's anchor should not be recomputed from a feed every cycle.** Once
    a position's entry price is known it is a fact about shares that have
    already been bought; re-deriving it six times a day only creates six
    chances for a bad read to move it. So it is *taken* once and carried on the
    book (`Position.entry_price_source == "carried"` on every cycle after the
    first), and retaken only on the two events that genuinely change it:

    * **the position was added to** — quantity is above the prior book's, which
      is a legitimate entry built from two fills a week apart. The blend
      is correct there and must survive: these are the same never-closed
      position, and `average_buy_price` blends them exactly as it should.
    * **the oldest open lot moved forward** — `_held_since` is not the date it
      was anchored against any more, so the shares the anchor described have
      been sold and what is held now is a different position in the same
      symbol. This is the close-and-reopen case, and it is why a round trip
      cannot inherit the closed lot's basis no matter how the feeds lag.

    **Two feeds that disagree are not a number.** Where both readings are
    available and they diverge by more than `entry_price_divergence_pct`, this
    returns the anchor it has *and* marks it disputed, and every entry-anchored
    judgment downstream stands down on that symbol rather than picking a side:
    `plays.exits` skips the ATR stop and the target exactly as it does on a
    missing entry price, `_protection_plan` neither places nor cancels anything
    behind it, and `manual_required` puts it in front of the operator.

    **One gap is not a dispute.** A tax-lot basis *above* the anchor by no more
    than the losses §9's ledger holds on the symbol within 30 days of an open lot
    is the IRS wash-sale adjustment, not a bad read — `_wash_sale_excess` has the
    worked example. The anchor stands and is recorded as
    `wash_sale_adjusted`. Every other disagreement still disputes, including a
    lot basis *below* the anchor, which no wash sale produces.
    """
    broker_average = _num(held.get("average_buy_price"))
    lot_basis = _basis(lots)
    since = _held_since(lots)
    anchored_on = None if since is None else since.isoformat()

    tolerance = float(
        (ctx.get("config") or {}).get(
            "entry_price_divergence_pct", ENTRY_DIVERGENCE_PCT_DEFAULT
        )
    )

    carried = _num(prior.get("entry_price"))
    carried_on = prior.get("entry_anchored_on")
    prior_quantity = _quantity(prior) if prior else 0.0
    added = _quantity(held) > prior_quantity + epsilon
    # A carried anchor with no `entry_anchored_on` predates this field: the
    # book row was written by an older build, or by `cli._carried` off one. It
    # is retaken rather than trusted, which costs one cycle of re-derivation
    # and is the only reading that cannot silently keep a pre-fix number alive.
    same_shares = (
        carried is not None
        and carried_on is not None
        and anchored_on is not None
        and str(carried_on) == anchored_on
    )

    if same_shares and not added:
        price, source = carried, ENTRY_FROM_CARRY
        anchored_on = str(carried_on)
    elif broker_average is not None:
        price, source = broker_average, ENTRY_FROM_BROKER_AVERAGE
    elif lot_basis is not None:
        price, source = lot_basis, ENTRY_FROM_TAX_LOTS
    else:
        return _Anchor(None, None, None, False)

    # Every witness this cycle has, against the anchor actually in use —
    # including on a carried anchor, which is the case that matters most: a
    # feed that goes wrong days into a position is exactly the case seen live, and
    # a check that only ran at anchor time would have looked the other way.
    detail = None
    disputed = False
    for name, witness, is_lot_basis in (
        ("the broker's average_buy_price", broker_average, False),
        ("the tax-lot weighted basis", lot_basis, True),
    ):
        if witness is None or witness == price:
            continue
        if not _diverges(float(price), float(witness), tolerance):
            continue
        if is_lot_basis and _wash_sale_excess(
            held.get("symbol"), lots, float(price), ctx
        ) is not None:
            # Not a second opinion about the entry — the fill plus a
            # disallowed loss the ledger already holds. The anchor stands.
            source = ENTRY_FROM_WASH_SALE_ADJUSTED
            continue
        disputed = True
        detail = (
            f"{held.get('symbol')}'s entry price of {float(price):.4f} "
            f"({source}) and {name} of {float(witness):.4f} differ by more "
            f"than {tolerance:.1%}; §6.5's stop and target are not "
            "evaluated on a basis two feeds disagree about, and whatever "
            "rests behind the position is left exactly as it is"
        )
        break

    return _Anchor(round(float(price), 6), source, anchored_on, disputed, detail)


def _held_since(lots: Sequence[Mapping[str, Any]]) -> date | None:
    """The oldest open lot. Scaling into a position must not reset its clock."""
    days = [
        date.fromisoformat(lot["acquired_date"])
        for lot in lots
        if lot.get("acquired_date")
    ]
    return min(days) if days else None


def _last_fill_on(lots: Sequence[Mapping[str, Any]]) -> date | None:
    """The newest open lot. Unlike `_held_since`, an average-down *should*
    move this forward — it is what tells `plays.exits`'s same-day-fill guard
    a position picked up a fill today, whether that fill opened it or added
    to it."""
    days = [
        date.fromisoformat(lot["acquired_date"])
        for lot in lots
        if lot.get("acquired_date")
    ]
    return max(days) if days else None


def _last_fill_price(lots: Sequence[Mapping[str, Any]]) -> float | None:
    """The weighted price of the newest open lot(s) — every lot dated
    `_last_fill_on`, rather than just one, in case more than one buy landed
    on the same trading day. This is the reference `plays.exits`'s
    average-down floor drops price against, not the position's overall
    `_basis`: "how far below its latest fill" and "how far below its whole
    cost basis" are different questions, and the floor asks the first one so
    it does not fire again on a name that is merely still below its very
    first entry.
    """
    newest = _last_fill_on(lots)
    if newest is None:
        return None
    matching = [lot for lot in lots if lot.get("acquired_date") == newest.isoformat()]
    return _basis(matching)


def _open_lot_count(lots: Sequence[Mapping[str, Any]]) -> int:
    """A mechanical proxy for how many times this position has been added to
    — `plays.exits`'s average-down cap reads it the way `dip_recovery`'s own
    engine reads its exact `total_fills`. It is advisory rather than exact:
    the broker consolidates or splits lots at its own discretion, so two
    same-day fills are not guaranteed to be two lots. That is the acceptable
    direction to be wrong in for a cap meant to stop indefinite averaging,
    not to count trades for the ledger.
    """
    return len(lots)


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fill_minutes(order: Mapping[str, Any]) -> float | None:
    """Wall-clock minutes from the order reaching the broker to its last
    execution, when `get_equity_orders` carried both stamps; None otherwise.
    Advisory — it never blocks anything and a missing stamp is not a fault."""
    start = order.get("created_at")
    end = order.get("last_transaction_at") or order.get("updated_at")
    if not start or not end:
        return None
    try:
        delta = _moment(str(end)) - _moment(str(start))
    except ValueError:
        return None
    return round(delta.total_seconds() / 60.0, 2)


def _fills_quality(
    ctx: Mapping[str, Any], ours: Mapping[str, Mapping[str, Any]]
) -> list[dict]:
    """One row per terminal order this side placed (§1.3/§4.2).

    Terminal only, and only orders in `ours` — a partial fill on a *working*
    order is an order still working, the same reading `_order_findings` takes
    for QUANTITY_DRIFT. `limit_price` prefers the broker's own order row and
    falls back to the placement; `avg_fill_price` is the quantity-weighted
    mean of the order's fills, or None when nothing filled (which is the whole
    point — an entry that never filled is otherwise invisible).
    """
    fills = _fills_by_order(ctx)
    rows: list[dict] = []
    for order in ctx.get("orders") or []:
        order_id = order.get("order_id")
        mine = ours.get(order_id)
        if mine is None or _is_open_order(order):
            continue

        these = fills.get(order_id) or []
        priced = [
            (_quantity(f), _num(f.get("price")))
            for f in these
        ]
        priced = [(q, p) for q, p in priced if p is not None and q > 0]
        avg_fill = (
            round(sum(q * p for q, p in priced) / sum(q for q, _ in priced), 6)
            if priced
            else None
        )
        limit_price = next(
            (
                v
                for v in (
                    _num(order.get("limit_price")),
                    _num(order.get("price")),
                    _num(mine.get("limit_price")),
                )
                if v is not None
            ),
            None,
        )
        rows.append(
            {
                "symbol": order.get("symbol") or mine.get("symbol"),
                "side": order.get("side") or mine.get("side"),
                "intended_qty": _quantity(mine),
                "filled_qty": round(sum(_quantity(f) for f in these), 6),
                "limit_price": limit_price,
                "avg_fill_price": avg_fill,
                "minutes_to_fill": _fill_minutes(order),
            }
        )
    rows.sort(key=lambda r: (r["symbol"] or "", r["side"] or ""))
    return rows


def _placements(ctx: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return list((ctx.get("intent") or {}).get("placements") or [])


def _ours(ctx: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Last cycle's placements, by the order id the broker handed back.

    A placement with no id is not in here, and cannot be — which is the
    correct reading of `MISSING_ORDER`: nothing handed back is the same fact
    as nothing placed.
    """
    return {p["order_id"]: p for p in _placements(ctx) if p.get("order_id")}


def _fills_by_order(ctx: Mapping[str, Any]) -> dict[str, list[Mapping[str, Any]]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in ctx.get("fills") or []:
        grouped.setdefault(row.get("order_id"), []).append(row)
    return grouped


def _is_own_protection(
    order: Mapping[str, Any], ours: Mapping[str, Mapping[str, Any]]
) -> bool:
    """Whether a broker order this cycle's placements do not name is
    nevertheless one of this system's own resting protective orders.

    **The worst outage this module has had.** A `stop_market` placed by
    `plan.protection` is `time_in_force: gtc` precisely so it outlives the cycle
    that placed it — and `edge_prior()` hands back only the most recent batch that
    placed *something*, so within a day or two the stop is no longer in `ours` at
    all. Until now the only thing keeping it out of the `PHANTOM_ORDER` branch was
    `order_id` matching a placement. That drove `divergence_streak` to 5 and
    blocked entries until an operator cleared `edge_control`, and it nulled the
    `keep` rows' ids so `cli._protection_orders` could not emit the cancel an exit
    needs — which is why three exits in a row were rejected "Not enough shares to
    sell."

    `src/broker_shapes.py` fixes the rename. This fixes the classification,
    because the rename was never the only thing holding it up: two modules were
    answering the same question two ways. `protection.is_ours_to_replace` has
    known that ``placed_agent == "agentic"`` is the broker's own durable
    provenance flag, and `protection.resting_kind` has known what a resting stop
    looks like on the way back. Both are reused here rather than restated — a
    second copy of either rule is how the two answers happened in the first place.

    **Both conditions, never one.** Provenance alone would exempt an ordinary
    agentic day limit that really has gone missing from the plan; shape alone
    would exempt the operator's own GTC take-profit, which §13's oldest safety
    rule says must be *reported* and never touched. A phantom of unknown
    provenance is still a phantom and still blocks entries for a cycle.
    """
    return protection.resting_kind(order) is not None and protection.is_ours_to_replace(
        order, ours
    )


def _order_findings(
    ctx: Mapping[str, Any],
    ours: Mapping[str, Mapping[str, Any]],
    excluded: frozenset[str],
    resubmitting: frozenset[str] = frozenset(),
) -> tuple[list[Finding], list[dict], dict[str, int], list[str]]:
    """§12.2's four order-shaped classes, plus the cancels and the stale counter.

    One pass over the broker's orders and one over our placements, because the
    two lists answer different halves of the same question: an order we have
    that they do not is `MISSING_ORDER`, and one they have that we do not is
    `PHANTOM_ORDER`.

    `retired_order_ids` (§12.2 gap) excludes order ids whose fate this module has
    already adjudicated. `edge_prior()` hands back "the most recent cycle that
    placed something" — deliberately, so a quiet monitor cycle does not lose
    track of an order still working — but that means an order which has since
    gone terminal (filled, cancelled) is the *same* placement on every cycle
    until a new one is sent, sometimes for days. An order is only ever retired
    once its fate stops being able to change, i.e. once it is no longer open —
    `cycles` (below) already tracks exactly the open ones, so "retired" is
    precisely "examined this cycle and not in `cycles`".

    `excluded` reaches exactly one branch — the `PHANTOM_ORDER` one, which is
    the only place an order **not** in `ours` can produce a finding. Every
    other class here (`MISSING_ORDER`, `QUANTITY_DRIFT`, `STALE_OPEN_ORDER`),
    the `cancels` list and the `open_order_cycles` counter all key off `ours`,
    which is `edge_prior()`'s record of *this side's own* placements and can
    therefore never contain the other side's order at all. That is also why
    this module has never been able to cancel one, and why §13's blanket
    post-close cancel (Step 8c) cannot reach it either: it cancels
    `reconcile.cancels`, not the broker's raw order list.
    """
    config = ctx["config"]
    epsilon = float(config["reconcile_epsilon_quantity"])
    prior_cycles = (ctx.get("control") or {}).get("open_order_cycles") or {}
    retired = set((ctx.get("control") or {}).get("retired_order_ids") or [])
    postclose = (ctx.get("cycle") or {}).get("cycle_type") == POSTCLOSE
    fills = _fills_by_order(ctx)
    active = {oid: p for oid, p in ours.items() if oid not in retired}

    findings: list[Finding] = []
    cancels: list[dict] = []
    cycles: dict[str, int] = {}
    newly_retired: list[str] = []

    for order in ctx.get("orders") or []:
        order_id = order.get("order_id")
        symbol = order.get("symbol")
        mine = active.get(order_id)

        if mine is None:
            # §13.1's failure, which has already happened on this account once:
            # two deciders, one account. An order nothing in the runlog claims
            # is the only evidence of it this module can see, so it blocks
            # entries for a cycle even though the likelier explanation is the
            # operator using the app.
            #
            # Unless it is this system's own resting protection — see
            # `_is_own_protection`. That case is not a divergence at all and
            # produced the worst outage this module has had.
            if _is_open_order(order) and not _is_own_protection(order, ours):
                findings.append(
                    Finding(
                        OTHER_STRATEGY_ORDER,
                        symbol,
                        f"the broker has an open {order.get('side')} of "
                        f"{_quantity(order):g} {symbol} ({order_id}) on a symbol "
                        "the other strategy owns; recorded, not this side's to "
                        "cancel, and not a divergence",
                        order_id,
                    )
                    if symbol in excluded
                    else Finding(
                        PHANTOM_ORDER,
                        symbol,
                        f"the broker has an open {order.get('side')} of "
                        f"{_quantity(order):g} {symbol} ({order_id}) that no "
                        "plan proposed; entries are blocked for one cycle",
                        order_id,
                    )
                )
            continue

        if not _is_open_order(order):
            # Terminal, so the quantity is final and worth comparing. A
            # partial fill on a *working* order is an order still working, not
            # a broker that lost half the trade.
            filled = sum(_quantity(f) for f in fills.get(order_id) or [])
            if not _same(filled, _quantity(mine), epsilon):
                findings.append(
                    Finding(
                        QUANTITY_DRIFT,
                        symbol,
                        f"{symbol} filled {filled:g} against an intended "
                        f"{_quantity(mine):g}",
                        order_id,
                    )
                )
            newly_retired.append(order_id)
            continue

        seen = int(prior_cycles.get(order_id) or 0) + 1
        cycles[order_id] = seen

        # §10.1's resting protective stop (cycle-prompt.md Step 8b) is placed
        # `time_in_force: gtc` specifically so it survives to the next cycle —
        # "continuous protection between cycles" is its entire purpose. It is
        # tracked in `ours` like any other placement (Step 8c writes it into
        # `placements` the same as a plan order), so without this exclusion it
        # reads as an ordinary stale order and both branches below would sweep
        # it up: `postclose` as a blanket cancel and the 2-cycle rule the first
        # time a monitor cycle ran twice before it triggered. Only a
        # `postclose` cycle's own STALE_ORDER_CYCLES-triggered cancel is
        # guarded here; `postclose` itself never reaches the 2-cycle branch
        # because it always takes the `postclose` arm first.
        # `protective_target` joined it with `plan.protection`. The take-profit
        # limit is `gtc` for exactly the same reason and would be swept up by
        # exactly the same two branches — and losing it is the quieter failure
        # of the two, because an uncancelled stop at least still protects the
        # downside while a cancelled target simply never sells and nothing
        # looks wrong until the position gives the move back.
        if mine.get("protective_stop") or mine.get("protective_target"):
            continue

        if (postclose or seen >= STALE_ORDER_CYCLES) and order_id not in resubmitting:
            why = (
                "§13 cancels unfilled orders at the close"
                if postclose
                else f"open across {seen} reconciling cycles"
            )
            findings.append(
                Finding(STALE_OPEN_ORDER, symbol, f"{symbol}: {why}", order_id)
            )
            cancels.append({"order_id": order_id, "symbol": symbol, "reason": why})

    seen_ids = {o.get("order_id") for o in ctx.get("orders") or []}
    # The other half of `_is_own_protection`: a `(symbol, kind)` a resting
    # agentic protective order already covers. `MISSING_ORDER` means "the plan
    # placed this and the broker has no such order", and a stop this side
    # placed last week, still resting, still agentic, is not that — it is the
    # same order under an id `edge_prior()` no longer carries.
    covered = {
        (order.get("symbol"), protection.resting_kind(order))
        for order in ctx.get("orders") or []
        if _is_open_order(order) and _is_own_protection(order, ours)
    }
    for order_id, mine in active.items():
        if order_id in seen_ids or fills.get(order_id):
            continue
        # Not retired: this order's fate has not stopped being able to change,
        # which is the whole of what `retired_order_ids` means. Left alone, it
        # is silently re-matched here on every cycle for as long as it rests.
        if (mine.get("symbol"), protection.order_kind(mine)) in covered:
            continue
        findings.append(
            Finding(
                MISSING_ORDER,
                mine.get("symbol"),
                f"the plan placed a {mine.get('side')} of {_quantity(mine):g} "
                f"{mine.get('symbol')} and the broker has no such order",
                order_id,
            )
        )
        newly_retired.append(order_id)

    for placement in _placements(ctx):
        if placement.get("order_id"):
            continue
        findings.append(
            Finding(
                MISSING_ORDER,
                placement.get("symbol"),
                f"a {placement.get('side')} of {_quantity(placement):g} "
                f"{placement.get('symbol')} was attempted and the broker "
                "returned no order id",
                None,
            )
        )

    return findings, cancels, cycles, newly_retired


def _entry_resubmissions(
    ctx: Mapping[str, Any], ours: Mapping[str, Mapping[str, Any]]
) -> tuple[list[Finding], list[dict], list[dict], list[str]]:
    """Cancel a stale-but-still-actionable entry limit and re-queue its symbol.

    Only this side's own **buy** orders (`ours`), only while
    `cycle.entries_allowed` (re-queueing for a window that has already closed
    buys nothing), and only past `entry_resubmit_after_min` wall-clock minutes
    with nothing filled against them — a partial fill is an order that is
    working, the same reading `_order_findings` takes for QUANTITY_DRIFT. A
    protective resting order is never swept: it is `gtc` on purpose.

    Returns the findings, the cancels, the re-queue list (`{symbol, play_id}`,
    the shape `cli.candidates` unions onto a monitor cycle's triggers), and the
    order ids to retire so a cancelled entry does not read as `QUANTITY_DRIFT`
    on the next cycle.
    """
    cycle = ctx.get("cycle") or {}
    if not cycle.get("entries_allowed"):
        return [], [], [], []

    now_stamp = cycle.get("now")
    if not now_stamp:
        return [], [], [], []
    try:
        now = _moment(str(now_stamp))
    except ValueError:
        return [], [], [], []

    after_min = float(ctx["config"].get("entry_resubmit_after_min", 30))
    fills = _fills_by_order(ctx)

    findings: list[Finding] = []
    cancels: list[dict] = []
    resubmit: list[dict] = []
    retired: list[str] = []

    for order in ctx.get("orders") or []:
        order_id = order.get("order_id")
        mine = ours.get(order_id)
        if mine is None or not _is_open_order(order):
            continue
        if str(order.get("side") or mine.get("side") or "").lower() != BUY:
            continue
        if mine.get("protective_stop") or mine.get("protective_target"):
            continue
        if fills.get(order_id):
            continue

        created = order.get("created_at")
        if not created:
            continue
        try:
            age = (now - _moment(str(created))).total_seconds() / 60.0
        except ValueError:
            continue
        if age < after_min:
            continue

        symbol = order.get("symbol") or mine.get("symbol")
        why = (
            f"{symbol}: an entry limit has been unfilled for {age:.0f} min, past "
            f"the {after_min:.0f}-min resubmit window; cancelled and re-queued for "
            "the next cycle"
        )
        findings.append(Finding(ENTRY_RESUBMIT, symbol, why, order_id))
        cancels.append({"order_id": order_id, "symbol": symbol, "reason": why})
        resubmit.append({"symbol": symbol, "play_id": mine.get("play_id")})
        retired.append(order_id)

    return findings, cancels, resubmit, retired


def _fill_findings(
    ctx: Mapping[str, Any], ours: Mapping[str, Mapping[str, Any]],
    excluded: frozenset[str],
) -> list[Finding]:
    """§12.2's `UNTRACKED_FILL`, which is the one class that is not an error.

    "The operator buying a stock in the app is a supported workflow." It is
    logged and not alerted, and it does not count toward the persistent
    divergence escalation — alerting on the operator's own deliberate action
    is exactly the noise that makes a system tiresome.

    The other side's fill stays on this list — `get_equity_orders` is
    account-wide, and a fill on the account that no part of the runlog records
    is never the safer direction. What changes for it is the *sentence*: since
    phase 6 an owned symbol is deliberately **not** adopted (`_attribution`
    skips it, `_reconciled_positions` never books it), so the standing
    "adopted and managed on identical terms" wording would describe the exact
    opposite of what happened, in the one artefact a person actually reads.
    """
    return [
        Finding(
            UNTRACKED_FILL,
            row.get("symbol"),
            f"a {row.get('side')} of {_quantity(row):g} {row.get('symbol')} "
            + (
                "filled on a symbol the other strategy owns; recorded here, "
                "managed there, and deliberately not adopted into this book"
                if row.get("symbol") in excluded
                else "filled with no originating plan; adopted and managed on "
                "identical terms (Invariant 7)"
            ),
            row.get("order_id"),
        )
        for row in ctx.get("fills") or []
        if row.get("order_id") not in ours
    ]


def _expected_book(ctx: Mapping[str, Any]) -> dict[str, float]:
    """Prior positions plus this cycle's fills — what the book *should* be.

    Untracked fills are counted in exactly like any other, which is what makes
    an operator trade absorb without also raising `POSITION_DELTA_MISMATCH`.
    Leaving them out would give every manual trade a second finding, and the
    second one would be an error — which is how "absorb silently" stops being
    silent.
    """
    expected = {
        p["symbol"]: _quantity(p) for p in ctx.get("prior_positions") or []
    }
    for row in ctx.get("fills") or []:
        symbol = row.get("symbol")
        signed = _quantity(row) if row.get("side") == BUY else -_quantity(row)
        expected[symbol] = expected.get(symbol, 0.0) + signed
    return expected


def _position_findings(
    ctx: Mapping[str, Any], excluded: frozenset[str]
) -> list[Finding]:
    """§12.2's `POSITION_DELTA_MISMATCH`. Alert and proceed; the broker wins.

    `excluded` is dropped from *both* sides of the comparison, and it has to
    be. `_expected_book` is built from `prior_positions` and `fills`, both of
    which are this side's own — so once `_reconciled_positions` stops booking
    the other side's symbols, the next cycle's `prior_positions` no longer
    carries them while the broker's aggregate `positions` list still does.
    Comparing the two unfiltered would report a fresh `POSITION_DELTA_MISMATCH`
    on every other-side symbol on **every single cycle**, and that class is in
    `DIVERGENCE_CLASSES` — two cycles running and
    `RECONCILIATION_DIVERGENCE_PERSISTENT` blocks entries until an operator
    clears `edge_control` by hand. That is the same shape of permanent,
    self-inflicted block an earlier stale-placement incident produced, and
    filtering one side only is how it would come back.
    """
    epsilon = float(ctx["config"]["reconcile_epsilon_quantity"])
    expected = _expected_book(ctx)
    actual = {p["symbol"]: _quantity(p) for p in ctx.get("positions") or []}

    findings = []
    for symbol in sorted({*expected, *actual} - excluded):
        want = expected.get(symbol, 0.0)
        have = actual.get(symbol, 0.0)
        if not _same(want, have, epsilon):
            findings.append(
                Finding(
                    POSITION_DELTA_MISMATCH,
                    symbol,
                    f"the broker holds {have:g} {symbol} where prior positions "
                    f"and this cycle's fills come to {want:g}; the broker's "
                    "figure is what goes forward",
                )
            )
    return findings


# A cent is the finest the broker's own average_buy_price and this module's
# rounded entry_price (`_basis`, six-decimal cost / quantity) can be trusted to
# agree at — matching to the fraction they were both derived from would miss an
# earlier incident's own pair (66.0400 vs 66.0399).
SYMBOL_SWAP_PRICE_EPS = 0.01


def _symbol_swap_findings(
    ctx: Mapping[str, Any], epsilon: float, excluded: frozenset[str]
) -> list[Finding]:
    """The fingerprint of an order placed under the wrong ticker.

    A name the prior book expected and the broker no longer holds, paired with
    a name the broker holds that the prior book never mentioned, at the same
    quantity and the same price to the cent, is not two independent positions
    that happen to have moved — it is one trade that landed on the wrong
    instrument. `_position_findings` already reports both halves as ordinary
    `POSITION_DELTA_MISMATCH` rows; this only adds the connection between them,
    which is the one fact a person (or a future cycle) needs to resolve it
    without re-deriving it from two unrelated-looking lines under time
    pressure, as happened live (one symbol expected, another held, same
    quantity and price).

    Matched by quantity and price alone, not by sector or name similarity —
    the incident this exists for was already a same-sector, similar-name
    mismatch, so anything narrower would have missed the very case that
    prompted it.

    `excluded` is dropped from both halves for the same reason
    `_position_findings` drops it: the other side's symbols are permanently
    "held by the broker, absent from this side's prior book", which is exactly
    the `unexpected` half of this fingerprint. Left in, every one of them would
    sit in the candidate pool forever, waiting to coincide in quantity and
    price with some core name that closed — a false swap report generated by
    the filtering itself rather than by anything that happened.
    """
    prior = {
        p["symbol"]: p
        for p in ctx.get("prior_positions") or []
        if _quantity(p) > 0 and p["symbol"] not in excluded
    }
    held_now = {
        p["symbol"] for p in ctx.get("positions") or []
        if p["symbol"] not in excluded
    }
    missing = {s: p for s, p in prior.items() if s not in held_now}

    actual = {
        p["symbol"]: p for p in ctx.get("positions") or []
        if p["symbol"] not in excluded
    }
    unexpected = {s: p for s, p in actual.items() if s not in prior}

    findings = []
    for missing_symbol, mp in sorted(missing.items()):
        expected_price = mp.get("entry_price")
        if expected_price is None:
            continue
        for actual_symbol, ap in sorted(unexpected.items()):
            actual_price = ap.get("average_buy_price")
            if actual_price is None:
                continue
            if not _same(_quantity(mp), _quantity(ap), epsilon):
                continue
            if not _same(
                float(expected_price), float(actual_price), SYMBOL_SWAP_PRICE_EPS
            ):
                continue
            findings.append(
                Finding(
                    SYMBOL_SWAP_SUSPECTED,
                    actual_symbol,
                    f"the broker holds {_quantity(ap):g} {actual_symbol} at "
                    f"{float(actual_price):.2f} where the prior book expected "
                    f"{_quantity(mp):g} {missing_symbol} at "
                    f"{float(expected_price):.2f} — same quantity and price, "
                    "different symbol; likely an order placed under the wrong "
                    "ticker rather than two independent divergences",
                )
            )
    return findings


@dataclass(frozen=True)
class _Owner:
    """Which play manages a symbol, and whether the system chose to open it."""

    play_id: str
    adopted: bool
    adopted_on: str | None = None


def _adopt(symbol: str, ctx: Mapping[str, Any]) -> _Owner:
    """Take in a position nobody here opened, on terms that can still sell it.

    §12.2's original answer — no play, therefore no time stop, no target and no signal exit —
    makes an inherited name unsellable except at a loss, which is the exact opposite of that
    mandate.

    A real play wins where its **own trigger fires today**, because that is
    evidence about the name. Everything else takes `ADOPTED`, which is a fifth
    entry in `plays.py` rather than one of §6's four on purpose: crediting a
    play's `edge_play_state` row for a position it never chose would flatter or
    damn it for someone else's decision, and `DIP` in particular would sell a
    healthy momentum holding inherited at RSI 60 on the very next cycle.

    ``adopted_on`` is today, and it is what stops the time stop firing the moment a play is
    bound. ``sessions_held`` comes off the broker's oldest tax lot, so an inherited name arrives
    with a clock that expired long before this system saw it — every inherited winner would leave
    on the first cycle that gave it a play. Stamping the adoption day makes §6.5's clock measure
    how long *deterministic-trader* has held the position, which is the only reading
    under which "the thesis has had its run" means anything (operator).
    """
    return _Owner(
        would_open(symbol, ctx) or ADOPTED,
        True,
        (ctx.get("cycle") or {}).get("trading_day"),
    )


def _other_side_symbols(ctx: Mapping[str, Any], owner: str) -> frozenset[str]:
    """Symbols this side must not book, count, concentrate against or adopt.

    Dip Recovery plan phase 6. `ctx["positions"]` is the broker's **aggregate**
    book and has no strategy concept — nothing upstream of this module filters
    it — so once the other side holds a symbol, this side still sees it and,
    left alone, folds it into `book` (`positions_held`, `ctx["positions"]` for
    guard's §4.4 concentration caps, and `_adopt`'s binding of an unrecognised
    name to one of this side's plays). `guard.lock` and `plays/exits.py`
    already refuse to *trade* such a symbol; this is the other half — refusing
    to *account* for it.

    Two sources, and the scoping of each matters.

    **The lock**, read exactly as `_symbol_lock_claims` reads it: a symbol is
    the other side's only when the lock names an owner that is neither absent
    nor us. Phase 1 shipped the unscoped version of this same read and it
    raised a false `cross-strategy conflict` on every cycle both sides held
    anything; `in (None, owner)` — spelled here as `not in (None, owner)`,
    the complement — is the correction, and any new lock read in this module
    has to use it.

    **`dip_recovery_positions`**, core-side only, and only for symbols the
    lock has not spoken about yet. This closes the adoption race: Dip Recovery
    writes its `ENTRY_PENDING` row and places the order in the same tick
    (`dip_recovery.engine`'s entry pass, `dip-monitor-prompt.md` Step 8),
    but the lock is not claimed until `engine._apply_fills` confirms the fill
    on the *following* tick. A core cycle landing in that window sees an
    unclaimed symbol, and `in (None, owner)` correctly keeps it — so core
    would adopt Dip Recovery's brand-new position and could propose an exit on
    it before the lock ever existed. The pending row is the earliest evidence
    core can have, and it is strictly earlier than the fill. The
    `lock.get(symbol) is None` guard is what keeps that safe in the other
    direction: a symbol already locked to **core** is never dropped from
    core's own book on the strength of a row in the other side's table.
    """
    lock = ctx.get("symbol_lock") or {}
    theirs = {s for s, held_by in lock.items() if held_by not in (None, owner)}

    if owner != "core":
        return frozenset(theirs)

    return frozenset(
        theirs
        | {
            symbol
            for symbol in (ctx.get("dip_recovery_positions") or {})
            if lock.get(symbol) is None
        }
    )


def _attribution(
    ctx: Mapping[str, Any],
    ours: Mapping[str, Mapping[str, Any]],
    excluded: frozenset[str],
) -> dict[str, _Owner]:
    """Which play owns each symbol, in the order the answer should be trusted.

    A position carries its originating play for its whole life (§6), so the
    prior book wins: a DIP whose RSI has since recovered is still a DIP
    position. That pass also carries ``adopted`` forward, and must — by the
    cycle after an adoption the name is in ``prior_positions`` like any other
    and nothing downstream could tell it had been inherited.

    Then a fill on a name the book has not seen takes the play from the
    placement that bought it. A fill with no placement is the operator trading
    in the app, and a position held at the broker that is in neither list is the
    same fact arriving a cycle later — or day one, where it is the whole book.
    **That third pass is the one this module was missing**: `_attribution` only
    ever walked the prior book and this cycle's fills, so ten positions the
    system woke up holding fell through to `MANUAL` by never being looked at.

    An unadopted `MANUAL` in the prior book is the one thing that pass does
    *not* carry forward, because doing so shadowed the third pass out of
    existence. The ten inherited names were written to `edge_runlog.book` as
    `MANUAL` by every cycle before adoption was built, so they arrive in
    `prior_positions` on every cycle after it and would never be looked at
    again — holding the one `play_id` that gets no target, no time stop and no
    top-up. Letting them fall through adopts them once; the cycle after that
    the book carries ``adopted: true`` and the ordinary carry-forward resumes.
    Only while the name is *still held* — a `MANUAL` that has gone to zero is a
    round trip that is over, and there is nothing left to adopt.

    Every pass skips `excluded` (see `_other_side_symbols`), so a symbol the
    other side owns gets no owner at all rather than a wrong one. The middle
    pass is the one that matters most: the other side's buy fill arrives in
    *this* side's `ctx["fills"]` — `get_equity_orders` is account-wide — with
    no placement of ours behind it, which is precisely the shape `_adopt`
    exists to absorb. The first pass skips it too so that a symbol carried in
    this side's prior book cannot credit one of this side's plays with the
    other side's realised P&L in `_play_state_updates`.
    """
    still_held = {
        p.get("symbol") for p in ctx.get("positions") or []
        if p.get("symbol") not in excluded
    }

    owners: dict[str, _Owner] = {}
    for p in ctx.get("prior_positions") or []:
        if p["symbol"] in excluded:
            continue
        play_id = p.get("play_id") or MANUAL
        adopted = bool(p.get("adopted"))
        if play_id == MANUAL and not adopted and p["symbol"] in still_held:
            continue
        owners[p["symbol"]] = _Owner(play_id, adopted, p.get("adopted_on"))

    for row in ctx.get("fills") or []:
        symbol = row.get("symbol")
        if symbol in owners or symbol in excluded:
            continue
        play_id = (ours.get(row.get("order_id")) or {}).get("play_id")
        if play_id:
            owners[symbol] = _Owner(play_id, False)
        elif symbol in still_held:
            owners[symbol] = _adopt(symbol, ctx)
        # A fill with no placement on a name that is no longer held is a round
        # trip the system never owned. Adopting it here would hand its realised
        # P&L to whichever play `would_open` happens to name, which is the
        # scorecard contamination `_play_state_updates` exists to refuse.

    for held in ctx.get("positions") or []:
        symbol = held.get("symbol")
        if symbol not in owners and symbol not in excluded:
            owners[symbol] = _adopt(symbol, ctx)

    # The core sleeve's ETF is the sleeve's while the sleeve is on, however
    # the shares arrived (`src/core_sleeve.py`): bought by the sleeve, bought
    # by hand, or carried in the book under adoption's terms from before the
    # switch. Adoption would give it a 15-session time stop and a stop check,
    # which are exactly the two rules the sleeve is defined by not having.
    sleeve = core_sleeve.symbol(ctx)
    if (core_sleeve.enabled(ctx) and sleeve in still_held
            and owners.get(sleeve, _Owner(CORE, False)).play_id != CORE):
        owners[sleeve] = _Owner(CORE, False)

    return owners


def _high_since_entry(
    ctx: Mapping[str, Any], symbol: str, entry: float | None, prior: Mapping[str, Any]
) -> float | None:
    """The peak this position has reached, ratcheted and never lowered.

    Three sources, taken at their maximum: the figure the last reconciled book
    carried, this cycle's own quote, and the entry price itself as a floor. The
    entry floor is what makes the trailing stop start out *behind* the fixed
    one rather than ahead of it — a position bought this morning and already
    down has a high equal to its entry, so `entry − trail × ATR` is below
    `entry − stop × ATR` and `protection.stop_price`'s `max` keeps the fixed
    stop, which is the correct answer for a thesis that has not worked yet.

    The intraday high is preferred over the last trade where the broker
    supplies one, because a stop is about where the position *has been*, and
    a cycle running at 15:40 that reads only `last` has already thrown away
    most of the session. Both are read verbatim; nothing here is averaged.

    Returns None only for a position with no basis and no quote at all — the
    same read `POSITION_BASIS_UNKNOWN` is already reporting, and
    `protection.high_since_entry` fails closed to the entry price on it.
    """
    quote = (ctx.get("quotes") or {}).get(symbol) or {}
    seen = [
        value
        for value in (entry, prior.get("high_since_entry"), quote.get("high"), quote.get("last"))
        if value is not None
    ]
    return max(float(value) for value in seen) if seen else None


def _reconciled_positions(
    ctx: Mapping[str, Any],
    attribution: Mapping[str, _Owner],
    excluded: frozenset[str],
    epsilon: float,
) -> tuple[list[Position], list[Finding]]:
    """The broker's book, enriched with the two numbers `plays.py` was blocked on.

    A symbol `_other_side_symbols` names is not in this book at all — not as a
    `Position`, and not as a `POSITION_BASIS_UNKNOWN` finding either. The
    other side keeps its own basis and its own clock (`dip_recovery_positions`
    carries both), so reporting that *this* side cannot evaluate §6.5's stops
    on a position it does not manage would be a finding about nothing, on
    every cycle.
    """
    calendar = ctx.get("calendar") or {}
    trading_day = date.fromisoformat(ctx["cycle"]["trading_day"])
    # The last reconciled book, which is where the running high-water price
    # lives between cycles. `prior_positions` is `prior_book.positions` — the
    # same list `cli._carried` ages forward on the cycles that do not reconcile,
    # so the ratchet survives an overnight and a postclose without a control key.
    prior = {row.get("symbol"): row for row in ctx.get("prior_positions") or []}

    book: list[Position] = []
    findings: list[Finding] = []

    for held in sorted(ctx.get("positions") or [], key=lambda p: p["symbol"]):
        symbol = held["symbol"]
        if symbol in excluded:
            continue
        lots = _lots(ctx, symbol)
        anchor = _anchored_entry(
            held, lots, prior.get(symbol) or {}, ctx, epsilon
        )
        entry = anchor.price
        since = _held_since(lots)
        last_fill = _last_fill_on(lots)
        last_price = _last_fill_price(lots)

        if anchor.disputed and anchor.detail is not None:
            findings.append(
                Finding(POSITION_ENTRY_DISPUTED, symbol, anchor.detail)
            )

        if entry is None or since is None:
            findings.append(
                Finding(
                    POSITION_BASIS_UNKNOWN,
                    symbol,
                    f"no tax lot for {symbol}, so it has no entry basis and no "
                    "holding period; §6.5's ATR stop, time stop and target "
                    "cannot be evaluated on it",
                )
            )

        owner = attribution.get(symbol) or _Owner(MANUAL, False)

        book.append(
            Position(
                symbol=symbol,
                play_id=owner.play_id,
                adopted=owner.adopted,
                adopted_on=owner.adopted_on,
                quantity=_quantity(held),
                entry_price=entry,
                sessions_held=(
                    None if since is None else sessions_between(since, trading_day, calendar)
                ),
                last_fill_on=None if last_fill is None else last_fill.isoformat(),
                last_fill_price=last_price,
                open_lot_count=_open_lot_count(lots),
                high_since_entry=_high_since_entry(
                    ctx, symbol, entry, prior.get(symbol) or {}
                ),
                shares_available_for_sells=_number(
                    held.get("shares_available_for_sells")
                ),
                shares_held_for_sells=_number(held.get("shares_held_for_sells")),
                entry_price_source=anchor.source,
                entry_anchored_on=anchor.anchored_on,
                entry_price_disputed=anchor.disputed,
            )
        )

    return book, findings


def _number(value: Any) -> float | None:
    """A broker figure, or None. Never 0.0 for a field that did not arrive.

    Every `float(x or 0.0)` elsewhere in this module is reading a figure the
    broker always sends; these are not.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def calendar_day(value: Any) -> Any:
    """The date part of whatever a broker calls a date, or the value unchanged.

    `get_pnl_trade_history` dates a trade with a `timestamp`
    (``2026-09-04T16:33:05Z``) and `get_equity_orders` dates a fill with a
    plain day, so the two sides of `_Realized`'s `(symbol, date)` match are
    written in different units. Truncating to the day is what makes them
    comparable, and the day is the right granularity anyway: the broker's
    narrowest `span` is a week, so nothing finer than a date can discriminate
    between two rows in the window.

    Public and defined here rather than in `cli.py` because both sides of that
    match have to agree — `broker_shapes.realized` normalises the broker's rows
    on the way in and `_Realized.gain` normalises the fill's own date on the
    way out.
    Two truncations that merely agreed today would drift, and the failure would
    be silent: every lookup missing, every fill quietly falling back to the
    tax-lot arithmetic this whole path exists to replace.
    """
    if value is None:
        return None
    text = str(value)
    return text[:10] if len(text) > 10 and text[4] == "-" and text[7] == "-" else text


class _Realized:
    """`ctx["realized"]` keyed for lookup by fill — see the spec's §4.

    By `order_id` first, because it is the only key that survives two sells of
    the same name on the same day. By `(symbol, date)` second, and only where
    exactly one row claims the pair: a collision is stored as ``None`` so the
    lookup reads as "no match" rather than picking an arbitrary row. An
    ambiguous attribution is worse than none — it lands in the scorecard and
    stays there for as long as the account exists.

    The fill's own date is whatever it carries (`trade_date`, then `date`) and
    otherwise this cycle's trading day, which is what a fill read off
    `get_equity_orders` during the session actually is. The broker's `span` is
    a week at its narrowest, so the window is never the discriminator here —
    matching is.
    """

    def __init__(self, ctx: Mapping[str, Any]):
        self._trading_day = ctx["cycle"]["trading_day"]
        by_order: dict[str, Mapping[str, Any] | None] = {}
        by_symbol_date: dict[tuple[Any, Any], Mapping[str, Any] | None] = {}

        for row in ctx.get("realized") or []:
            order_id = row.get("order_id")
            if order_id is not None:
                # A duplicated id is a broker paging artefact or a parser bug,
                # not something to resolve here. Dropping it costs the fallback.
                key = str(order_id)
                by_order[key] = None if key in by_order else row

            pair = (row.get("symbol"), calendar_day(row.get("date")))
            by_symbol_date[pair] = None if pair in by_symbol_date else row

        self._by_order = by_order
        self._by_symbol_date = by_symbol_date

    def match(self, fill: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """The one `realized` row this fill is, or None."""
        order_id = fill.get("order_id")
        row = None if order_id is None else self._by_order.get(str(order_id))
        if row is None:
            when = fill.get("trade_date") or fill.get("date") or self._trading_day
            row = self._by_symbol_date.get((fill.get("symbol"), calendar_day(when)))
        return row

    def gain(self, fill: Mapping[str, Any]) -> float | None:
        """This fill's realized gain as the broker reports it, or None.

        None means "the broker did not tell us about this fill", which is a
        different statement from "the broker says zero" — the caller falls back
        to the tax lot on the first and records a break-even on the second.
        """
        row = self.match(fill)
        if row is None:
            return None

        gain = _number(row.get("realized_gain"))
        if gain is not None:
            return gain

        # A row with no gain field but both sides of the subtraction is still
        # an answer, and it is the broker's basis rather than the open-lot
        # basis the tax-lot fallback cannot supply on a full close.
        proceeds = _number(row.get("proceeds"))
        basis = _number(row.get("cost_basis"))
        if proceeds is not None and basis is not None:
            return proceeds - basis
        return None


def _realised(
    row: Mapping[str, Any], realized: _Realized | None = None
) -> float | None:
    """One sell fill's realised result, broker first, tax lots second.

    One computation feeding two consumers that read it in opposite directions:
    §9's ledger wants the losses and §1's scorecard wants the signed total.
    Two expressions of the same arithmetic would eventually disagree about a
    half-cent, and the disagreement would surface as a wash-sale row with no
    matching P&L.

    **The loss used to be computed rather than read**, because the account had
    exactly one realised close in its history and it was a gain, so the sign
    convention on the broker's own `realized_gain` was never verifiable. That
    reasoning did not survive contact with a closed position: the basis comes
    from `get_equity_tax_lots`, which returns *open* lots, so a position sold
    to zero has nothing left to price against and every full close read as
    `None` — 15 ledger rows and 13 closed trades with no money attached to any
    of them. The broker's own per-trade read is the only source that survives
    the position closing, so it goes first and the lot arithmetic stays as the
    fallback for a partial sale.
    """
    broker = None if realized is None else realized.gain(row)
    if broker is not None:
        return broker

    basis = row.get("cost_basis")
    if basis is None:
        return None
    return _quantity(row) * float(row.get("price") or 0.0) - float(basis)


def wash_sale_blocks(row: Mapping[str, Any], config: Mapping[str, Any]) -> bool:
    """Does this ledger row still bar a repurchase?

    The one predicate behind both readers — `guard._wash_sale_denials` and
    `dip_recovery.watchlist._wash_sale_symbols`.

    A row with a `loss_amount` is a measured loss and always blocks. A row
    without one is a close this system could not price, and
    `wash_sale_block_unknown_basis` decides what to do about that: `true` keeps
    §9's fail-closed fallback (over-blocking one repurchase is cheap), `false`
    is the setting to reach for once the broker read is confirmed live and the
    null rows are known to be historical.
    """
    if row.get("loss_amount") is not None:
        return True
    return bool(config.get("wash_sale_block_unknown_basis", True))


SIZING_V2_ENABLED = "sizing_v2_enabled"
REENTRY_BLOCK_DAYS = "reentry_block_days"


def reentry_window_days(config: Mapping[str, Any]) -> int:
    """How many days a name closed at a loss may not be bought back.

    **One window, not two**. With `sizing_v2_enabled` off it is
    `wash_sale_days` alone, exactly as before. At the shipped values the
    wash-sale window already contains the re-entry one, so switching on
    changes *where* the block is applied — `sizing` declines to spend a slot
    on the name instead of sizing it and letting `guard` deny it — and not
    *whether* it is.
    """
    window = int(config["wash_sale_days"])
    if bool(config.get(SIZING_V2_ENABLED)):
        window = max(window, int(config.get(REENTRY_BLOCK_DAYS) or 0))
    return window


def reentry_block(
    symbol: Any,
    loss_closes: Sequence[Mapping[str, Any]] | None,
    today: date,
    config: Mapping[str, Any],
) -> tuple[int, int] | None:
    """``(days since the loss close, the window)`` if ``symbol`` is still
    barred from repurchase, else None.

    The one reading of `edge_loss_closes` for every buyer: `guard`'s
    `WASH_SALE_COOLDOWN` denial, `sizing`'s `REENTRY_BLOCKED` refusal and the
    Dip Recovery watchlist all call this, so they cannot disagree about the
    window, the unknown-basis rows (`wash_sale_blocks`) or how a day is
    counted. The most recent qualifying close decides.
    """
    window = reentry_window_days(config)
    youngest: int | None = None
    for row in loss_closes or []:
        if row.get("symbol") != symbol or row.get("close_date") is None:
            continue
        if not wash_sale_blocks(row, config):
            continue
        try:
            closed = date.fromisoformat(str(row["close_date"])[:10])
        except ValueError:
            continue
        age = (today - closed).days
        if age <= window and (youngest is None or age < youngest):
            youngest = age
    return None if youngest is None else (youngest, window)


def _loss_closes(ctx: Mapping[str, Any]) -> list[dict]:
    """§9, on any sell fill that realised a loss — partial or full.

    Settled. The IRS rule attaches to the shares sold rather than to the
    position reaching zero, so a partial loss sale is a wash-sale trigger in
    fact. §9's own fallback already establishes the direction to err in:
    over-blocking a repurchase costs one trade, under-blocking costs a
    disallowed loss and the bookkeeping the operator explicitly wants to avoid.

    A gain writes no row at all — that is the fix the broker read buys, and the
    whole reason the account currently has a 31-day repurchase ban on names it
    closed in profit.

    **Two passes, one ledger.** `realized` (`get_pnl_trade_history`, span
    "week") is one dedicated broker call that reports every closing trade for
    the week regardless of what `fills` carries, so it now drives a second pass
    here rather than being only a lookup the first pass can silently fail to
    reach. Each realized row is a real, self-contained closing trade — unlike
    the ambiguous-match case inside `_Realized` (which is about attributing *a
    fill* to *one of several* candidate rows), a realized row taken on its own
    needs no fill to attribute it to.
    """
    close_date = ctx["cycle"]["trading_day"]
    realized = _Realized(ctx)
    rows: dict[tuple, dict] = {}

    def _record(
        symbol: Any, loss_amount: float | None, basis_unknown: bool, day: str = close_date
    ) -> None:
        if symbol is None:
            return
        amount = None if basis_unknown else round(loss_amount, 2)
        key = (symbol, day, amount, basis_unknown)
        rows.setdefault(
            key,
            {"symbol": symbol, "close_date": day,
             "loss_amount": amount, "basis_unknown": basis_unknown},
        )

    for row in ctx.get("fills") or []:
        if row.get("side") != SELL:
            continue

        # Dated by the broker's own trade when the fill matches one, so a
        # close this pass and the `realized` pass below both see lands on one
        # key even when it is reconciled a day after it happened. A hand-built
        # fill carries no reliable date of its own.
        matched = realized.match(row)
        day = (None if matched is None else _trade_day(matched)) or close_date

        realised = _realised(row, realized)
        if realised is None and row.get("derived"):
            # Read off `orders`' executions, not hand-built by a session that
            # saw the trade: no broker row and no open lot is "not reported
            # yet", and the `realized` pass below owns the close when it is.
            continue
        if realised is None:
            # §9's stated fallback: an unknown result is treated as a loss.
            _record(row.get("symbol"), None, True, day)
            continue

        loss = -realised
        if loss > MONEY_EPS:
            _record(row.get("symbol"), loss, False, day)

    for row in ctx.get("realized") or []:
        gain = _num(row.get("realized_gain"))
        if gain is None:
            # Silent, not `basis_unknown`: the fills pass above already owns
            # that fallback for a fill it can actually see, and a realized row
            # with no gain field carries no `proceeds`/`cost_basis` either
            # (confirmed live) so there is nothing here to price a close from.
            continue

        loss = -gain
        if loss > MONEY_EPS:
            # The trade's own day, not this cycle's. `realized` re-offers a
            # close on every reconciling cycle for a week; stamped with the
            # cycle's day, each re-offer would miss the ledger's key and write a fresh
            # row, pushing the wash-sale window out.
            _record(row.get("symbol"), loss, False, _trade_day(row) or close_date)

    return list(rows.values())


def _trade_day(row: Mapping[str, Any]) -> str | None:
    """The session day a `realized` row's trade belongs to, as ISO text.

    From the instant when there is one — `cadence.trading_day` in ET, so a
    20:30 ET overnight-session sell is the next session's, the same day every
    other part of this system files it under. Otherwise the row's own `date`.
    """
    stamp = row.get("traded_at")
    if stamp and len(str(stamp)) > 10:
        try:
            moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            moment = None
        if moment is not None and moment.tzinfo is not None:
            return session_day(moment.astimezone(ET)).isoformat()
    day = row.get("date")
    return None if day is None else str(day)[:10]


def _realized_trades(
    ctx: Mapping[str, Any], attribution: Mapping[str, _Owner]
) -> list[dict]:
    """Every closing trade the broker reports, for `edge_realized_trades`.

    **Why this exists.** `edge_play_state.realized_pnl` read 0.0 for every
    play in production: `_play_state_updates` credited money only from sell
    rows in `ctx["fills"]`, which the session hand-builds and the runner
    never sends, so DIP's four closes (-$14.39) and ADOPTED's eleven (+$7.93)
    never reached the scorecard. `realized` is the read that carries every
    close — but it re-offers each one on every reconciling cycle for a week,
    and `edge_play_state_apply` *sets* the figure, so crediting from it here
    would count each trade several times. These rows go to
    `edge_realized_trades_append`, which keys each on `(symbol, traded_at,
    quantity)` and credits a play only for a row it actually inserted.

    ``play_id`` is whoever held the symbol in the prior book (a trade is
    reported the cycle after it happens, when the position is still there);
    None when nothing did — stored, credited to no one. ``near_flat`` is the
    §1 annotation `config.near_flat_max_*` defines: small in dollars **and**
    small against the basis, because on an account this size "under $1" alone
    would flag every modest winner. ``entry_disputed`` is the flag on the
    position the trade closed — the result exists, but was reached off an
    entry basis two feeds disagreed about.
    """
    config = ctx.get("config") or {}
    max_dollars = float(config.get("near_flat_max_dollars", NEAR_FLAT_MAX_DOLLARS))
    max_pct = float(config.get("near_flat_max_pct", NEAR_FLAT_MAX_PCT))
    prior = {p.get("symbol"): p for p in ctx.get("prior_positions") or []}

    out = []
    for row in ctx.get("realized") or []:
        side = row.get("side")
        gain = _number(row.get("realized_gain"))
        stamp = row.get("traded_at")
        quantity = _number(row.get("quantity"))
        if (side is not None and str(side).lower() != SELL) or gain is None:
            continue
        if not stamp or len(str(stamp)) <= 10 or quantity is None:
            # No instant, no key: a row the ledger cannot deduplicate is one
            # it would credit again on every cycle.
            continue

        symbol = row.get("symbol")
        price = _number(row.get("price"))
        basis = None if price is None else round(quantity * price - gain, 6)
        near_flat = (
            basis is not None
            and basis > 0
            and abs(gain) < max_dollars
            and abs(gain) / basis < max_pct
        )
        owner = attribution.get(symbol)
        held = prior.get(symbol) or {}
        out.append(
            {
                "symbol": symbol,
                "traded_at": str(stamp),
                "trading_day": _trade_day(row),
                "side": SELL,
                "quantity": quantity,
                "price": price,
                "realized_gain": round(gain, 2),
                "cost_basis": basis,
                "play_id": None if owner is None else owner.play_id,
                "near_flat": near_flat,
                "entry_disputed": bool(held.get("entry_price_disputed")),
            }
        )
    return out


def _play_state_updates(
    ctx: Mapping[str, Any], attribution: Mapping[str, _Owner]
) -> dict:
    """What each play did this cycle: §6.3's counter.

    **`realized_pnl` is no longer written here.** It was credited from sell rows
    in `ctx["fills"]` — hand-built by the session, never sent by the runner — so
    it read 0.0 for every play in production. It is now credited by
    `edge_realized_trades_append`, once per broker-reported closing trade
    (`_realized_trades`); two writers would double-count, and
    `edge_play_state_apply` keeps the stored figure when the key is absent. The
    two rules below are unchanged — ``realized_pnl`` just follows the second one
    from its new writer.

    ``closed_trades`` counts a position reaching zero. A trade is a round trip:
    counting tranches would let one position scaled out in four pieces read as
    four closed trades, and §6.3's 0.75x would lift on a quarter of the evidence
    it asks for.

    ``realized_pnl`` follows §9's rule instead, because realised money is
    realised whether or not the rest of the position went with it. §6.3 asks "is
    CATALYST proven yet" and a count answers it; §1 asks which play had a bad
    month, and ten closed trades is the same number whether they made $50 or
    lost it.

    Both are cumulative and both are written whenever either moves, so
    `edge_play_state` has one row shape rather than two. A MANUAL position
    credits neither: an adopted trade proves nothing about any play's edge,
    and attributing its result would flatter or damn a play for a decision it
    did not make.

    ``ADOPTED`` does get a row, and that is the point of it being a play id
    rather than a flag. The inherited sleeve is real money and §1 should be able
    to say how it did; giving it a name of its own is what lets it be measured
    without any of it landing in CATALYST's or DIP's edge.
    """
    epsilon = float(ctx["config"]["reconcile_epsilon_quantity"])
    actual = {p["symbol"]: _quantity(p) for p in ctx.get("positions") or []}
    state = ctx.get("play_state") or {}

    closed: dict[str, int] = {}
    for prior in ctx.get("prior_positions") or []:
        play_id = prior.get("play_id")
        if _quantity(prior) <= 0 or play_id in (None, MANUAL):
            continue
        if actual.get(prior["symbol"], 0.0) > epsilon:
            continue
        closed[play_id] = closed.get(play_id, 0) + 1

    def prior(play_id: str, key: str, default):
        return (state.get(play_id) or {}).get(key) or default

    return {
        play_id: {
            "closed_trades": int(prior(play_id, "closed_trades", 0))
            + closed.get(play_id, 0),
        }
        for play_id in sorted(closed)
    }


def _pending_events(ctx: Mapping[str, Any], book: Sequence[Position]) -> bool:
    """An `edge_events` row newer than the last full cycle, on a name held.

    Settled. Cadence's `has_work` already fires on ``positions_held > 0``, so
    this is belt and braces — it binds only if that test ever tightens. An
    event on a name nobody holds is deliberately not pending: acting on it
    would be an entry, and cadence's own entries-remaining disjunct already
    covers that case.

    A `last_full_cycle` of None reads as "everything is newer", which is the
    safe direction on day one: the system wakes rather than sleeping through
    the first 8-K it ever sees.
    """
    last = (ctx.get("control") or {}).get("last_full_cycle")
    boundary = None if last is None else _moment(last)
    held = {p.symbol for p in book}

    for symbol, rows in (ctx.get("events") or {}).items():
        if symbol not in held:
            continue
        for row in rows or []:
            observed = row.get("observed_at")
            if boundary is None or (observed and _moment(observed) > boundary):
                return True
    return False


def _high_water_mark(ctx: Mapping[str, Any]) -> float:
    """§10's peak, ratcheted. Settled: this module maintains it.

    §10 says the mark is maintained by the code that reads it, but `guard.py`
    reads it and emits no writes at all. This module already holds
    `get_portfolio` equity at the start of every reconciling cycle, so it is
    the only place the mark can be current in the same cycle guard denies
    against it — a mark written at the close would leave guard measuring a
    stale floor for the whole session that set the peak.

    It never moves down. A mark that tracked equity into a drawdown would walk
    §10's floor down with it and the halt could never fire.

    **It is kept in flow-adjusted space**. The
    ratchet is why the round trip was the worse case: a $100 deposit lifted
    equity, the mark ratcheted to the funded figure, and withdrawing the same
    $100 left the floor $33 above an account that had not moved. Subtracting
    net deposits on both sides of the comparison makes a flow invisible to §10
    without giving up the ratchet — which is what a settled design decision actually
    needed, and why closing this did not have to reverse it.
    """
    return _high_water_updates(ctx)["high_water_mark"]


def _high_water_updates(ctx: Mapping[str, Any]) -> dict:
    """The mark, and the anchor that lets it survive a corrected deposit record
    (`sizing.anchored_mark`).

    Three keys, written together every reconciling cycle:

    * ``high_water_mark`` — the peak in flow-adjusted space, ratcheted. The
      prior it ratchets from is the *anchored* reading, so a stored figure
      that a back-dated deposit has made too high is corrected here on the
      first cycle after the record is.
    * ``high_water_mark_equity`` / ``high_water_mark_as_of`` — the account's
      raw equity and the trading day the mark stands on. A new peak anchors on
      this cycle's equity and day. A mark that is merely held keeps the anchor
      it has; one that never had an anchor gets ``mark + net deposits`` as of
      today, which is the same statement in equity terms and is true by
      construction on the day it is written.

    The anchor keys are omitted only when there is nothing to anchor: no
    trading day, or an unreadable equity with no anchor to carry forward.
    """
    account = ctx.get("account") or {}
    adjusted = flow_adjusted_equity(account)
    stored = (ctx.get("control") or {}).get("high_water_mark")
    prior = anchored_mark(account, stored)
    carried = (
        {
            "high_water_mark_equity": float(account["high_water_mark_equity"]),
            "high_water_mark_as_of": account["high_water_mark_as_of"],
        }
        if account.get("high_water_mark_equity") is not None
        and account.get("high_water_mark_as_of") is not None
        and account.get("net_deposits_at_mark") is not None
        else {}
    )
    if adjusted is None:
        # An unreadable equity (missing/zero/non-numeric `equity_total`) must
        # not ratchet the mark — that is exactly the bad read an earlier
        # flatten came from. Hold the prior peak; with no prior mark there is
        # nothing to ratchet and 0.0 keeps §10's floor at 0 until a real read
        # arrives. `guard.halts.ACCOUNT_UNREADABLE` stops the cycle meanwhile.
        return {"high_water_mark": 0.0 if prior is None else float(prior), **carried}

    today = (ctx.get("cycle") or {}).get("trading_day")
    if prior is None or adjusted > float(prior):
        anchor = (
            {} if today is None
            else {"high_water_mark_equity": raw_equity(account), "high_water_mark_as_of": today}
        )
        return {"high_water_mark": adjusted, **anchor}

    if not carried and today is not None:
        carried = {
            "high_water_mark_equity": round(
                float(prior) + float(account.get("net_deposits") or 0.0), 6
            ),
            "high_water_mark_as_of": today,
        }
    return {"high_water_mark": float(prior), **carried}


def _expire_operator_holds(
    control: Mapping[str, Any], trading_day: str | None, hold_days: int
) -> tuple[dict, list[Finding]]:
    """`edge_control.operator_holds` (docs/routine/cycle-prompt.md
    §"Step 8 — place") used to say "No expires after N days" — this is the N
    days. Each entry is `{"<symbol>": {"reason": ..., "set_on": "<trading_day>"}}`;
    one older than `hold_days` calendar days is dropped and reported.

    Returns the *whole* map back (not just the survivors) so a caller that
    finds nothing expired can compare it against `control`'s own copy and skip
    writing the key at all — this file's usual "no write on no change" economy,
    the same reason `unreconciled`/`retired_order_ids` are handled the way
    they are above.
    """
    holds = control.get("operator_holds") or {}
    if not holds or not trading_day:
        return dict(holds), []

    today = date.fromisoformat(trading_day)
    remaining: dict = {}
    findings: list[Finding] = []
    for symbol, entry in holds.items():
        set_on = (entry or {}).get("set_on")
        try:
            age_days = (today - date.fromisoformat(set_on)).days if set_on else 0
        except ValueError:
            age_days = 0
        if set_on and age_days > hold_days:
            findings.append(
                Finding(
                    OPERATOR_HOLD_EXPIRED,
                    symbol,
                    f"operator hold set {set_on} auto-expired after {hold_days} days",
                )
            )
        else:
            remaining[symbol] = entry
    return remaining, findings


def _symbol_lock_claims(ctx: Mapping[str, Any], book: Sequence[Position], owner: str) -> list[str]:
    """Every symbol this cycle's reconciled book holds that is unclaimed or
    already this side's — never a symbol another side already owns.

    Claiming the whole book, not just a symbol that just filled, is what
    self-bootstraps a legacy position from before this side's lock table
    existed (unclaimed reads as `None`, which this scopes in) — the same way
    `edge_control`'s NULL-tolerant lock claim self-heals instead of requiring
    a one-time migration step nobody remembers to run.

    Scoping out the other side's symbols is not an optimisation, it is
    correctness once both sides hold positions: the broker's book is
    aggregate, so the moment Dip Recovery owns X, core's reconcile still sees
    X in *its own* `positions` list and would otherwise call
    `edge_symbol_lock_claim(X, 'core')` every single cycle.
    The SQL correctly refuses that claim (a mismatched owner is never
    overwritten), but a `false` return is a cross-strategy conflict this
    module surfaces, not a benign no-op — every cycle both sides hold
    anything would raise a fresh false-alarm conflict about a lock that is
    working exactly as designed. Never asking for a symbol already owned by
    someone else is what keeps a return of `false` a genuine surprise again.
    """
    symbol_lock = ctx.get("symbol_lock") or {}
    return sorted(
        {p.symbol for p in book if symbol_lock.get(p.symbol) in (None, owner)}
    )


def _symbol_lock_releases(ctx: Mapping[str, Any], book: Sequence[Position], owner: str) -> list[str]:
    """Every symbol this side's lock says it owns that this cycle's book no
    longer holds — the position closed, and the lock must not outlive it.

    Read straight off `ctx["symbol_lock"]` rather than off a "closed this
    cycle" event, for the same self-healing reason `_symbol_lock_claims`
    claims the whole book instead of watching for a fill: whatever this side
    is holding the lock on but no longer holds a position in should not stay
    locked, regardless of why the two fell out of sync.
    """
    held_now = {p.symbol for p in book}
    locked_to_us = {
        symbol
        for symbol, held_by in (ctx.get("symbol_lock") or {}).items()
        if held_by == owner
    }
    return sorted(locked_to_us - held_now)


def _protection_plan(
    ctx: Mapping[str, Any],
    book: Sequence[Position],
    ours: Mapping[str, Mapping[str, Any]],
) -> dict:
    """What should be resting behind the book against what already is.

    §12's own job, applied to the one class of order §12 could never see:
    a resting protective order is not in `edge_prior()`'s placements after the
    cycle that placed it has rolled off, so `_order_findings` cannot tell a
    stop that is doing its job from one that was cancelled a fortnight ago.
    This pass does not diff intent against truth — it diffs *what should be
    true* against truth, which is the only diff that can notice an absence.

    Three outcomes per desired order, and the third is the one with a cost:

    * ``place`` — nothing rests, so place it.
    * ``keep`` — something rests within `protection_replace_min_pct` of the
      right price and covering the whole position. Left alone, named so the
      runlog shows the position was checked rather than skipped.
    * ``replace`` — something rests at the wrong price or for the wrong
      quantity. Cancel then place, in that order, and never the reverse: two
      live stops on one position sell it twice.

    The band exists because the trailing stop moves. Without it, a stop that
    ratchets a cent a day would be cancelled and replaced every cycle — three
    broker round trips a day per position, each one a window in which the
    position has no stop at all, to move a price by less than a spread.

    A duplicate resting order (two stops on one symbol, which should not
    happen and has) is emitted as a ``replace`` with a null ``new_order``:
    cancel it, and place nothing, because the entry ahead of it in the list
    already carries the replacement.
    """
    resting: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for order in ctx.get("orders") or []:
        kind = protection.resting_kind(order)
        if kind is None or not _is_open_order(order):
            continue
        resting.setdefault((order.get("symbol"), kind), []).append(order)

    # A position whose entry price two feeds disagree about is not this pass's
    # to act on in either direction. Dropping it from `book` alone would be
    # worse than doing nothing: `desired` would stop wanting a stop behind it,
    # and the leftover sweep at the bottom of this function would then read the
    # stop that *is* resting as an order nothing wants any more and cancel it —
    # stripping the protection off the one position whose data is least
    # trustworthy, on no evidence at all. So the symbol leaves both halves of
    # the diff at once, and whatever rests behind it stays exactly where the
    # operator can find it.
    disputed = {p.symbol for p in book if p.entry_price_disputed}
    considered = [p for p in book if p.symbol not in disputed]

    plan = protection.desired({**ctx, "positions": [p.as_dict() for p in considered]})
    band = float(ctx["config"]["protection_replace_min_pct"])
    epsilon = float(ctx["config"]["reconcile_epsilon_quantity"])

    place: list[dict] = []
    replace: list[dict] = []
    keep: list[dict] = []

    # Every resting protective order on a symbol that is not this side's to
    # cancel, **across both kinds**. It used to be read per kind, which was the
    # right shape only while a stop and a target could rest side by side. They
    # cannot: the broker reserves 100% of a position's shares against the first
    # resting sell it accepts, so the operator's own GTC take-profit is not
    # merely cover for the target we no longer place — it is what would refuse
    # the stop we do.
    foreign: dict[str, list[Mapping[str, Any]]] = {}
    for (symbol, _kind), group in resting.items():
        for order in group:
            if not protection.is_ours_to_replace(order, ours):
                foreign.setdefault(symbol, []).append(order)

    for want in plan.orders:
        group = sorted(
            resting.pop((want.symbol, want.kind), []),
            key=lambda o: str(o.get("order_id") or ""),
        )
        matched = next(
            (
                order
                for order in group
                if protection.within_band(
                    want.price, protection.resting_price(order), band
                )
                and float(order.get("quantity") or 0.0) + epsilon >= want.quantity
            ),
            None,
        )

        # Anything resting on this symbol that is not this side's to cancel
        # (§13: "a phantom order is never cancelled — it may be the
        # operator's"). It still counts as cover: the position has a sell
        # resting on it, so placing ours alongside would put more shares on
        # offer than the position holds. Reported as `keep` naming their order,
        # which is the honest description — checked, left alone, and not ours.
        theirs = sorted(
            foreign.get(want.symbol) or [], key=lambda o: str(o.get("order_id") or "")
        )
        group = [o for o in group if protection.is_ours_to_replace(o, ours)]

        if theirs:
            keep.extend(
                {**want.as_dict(), "order_id": o.get("order_id"), "ours": False}
                for o in theirs
            )
            # Ours may still be cancelled — two of our own stops behind the
            # operator's one order is still one too many.
            replace.extend(
                {"cancel_order_id": o.get("order_id"), "new_order": None} for o in group
            )
            continue

        if matched is not None:
            keep.append(
                {**want.as_dict(), "order_id": matched.get("order_id"), "ours": True}
            )
        elif group:
            replace.append(
                {"cancel_order_id": group[0].get("order_id"), "new_order": want.as_order()}
            )
        else:
            place.append(want.as_order())

        for extra in group:
            if extra is not matched and (matched is not None or extra is not group[0]):
                replace.append(
                    {"cancel_order_id": extra.get("order_id"), "new_order": None}
                )

    # Whatever is left rests in a kind nothing wants any more, and that stopped
    # being merely untidy: only one leg rests now (`protection_rest_kind`), and
    # **the broker reserves the whole position against the first resting sell it
    # accepts**. A leftover resting target is therefore not a redundant order
    # sitting beside the stop — it is the reason the stop cannot be placed at
    # all. Ours is cancelled with nothing to replace it, because the order that
    # should rest is already in `place` or `replace` above. The operator's is
    # reported and left alone (§13), and `guard.PROTECTION_CONTENDS` is what
    # refuses our stop against it by name rather than letting the broker refuse
    # it in a rejection nobody reads.
    #
    # Scoped to symbols that are actually in the book: a resting order on a
    # name this side does not hold is `_order_findings`' subject, not this
    # pass's, and cancelling one here would bypass every check it makes.
    book_symbols = {position.symbol for position in considered}
    reported = {row.get("order_id") for row in keep} | {
        row.get("cancel_order_id") for row in replace
    }
    for (symbol, kind), group in sorted(
        resting.items(), key=lambda item: (str(item[0][0]), str(item[0][1]))
    ):
        if symbol not in book_symbols:
            continue
        for order in sorted(group, key=lambda o: str(o.get("order_id") or "")):
            if order.get("order_id") in reported:
                continue
            if protection.is_ours_to_replace(order, ours):
                replace.append(
                    {"cancel_order_id": order.get("order_id"), "new_order": None}
                )
            else:
                keep.append(
                    {
                        "symbol": symbol,
                        "play_id": None,
                        "kind": kind,
                        "quantity": _number(order.get("quantity")),
                        "order_type": order.get("type"),
                        "stop_price": None,
                        "limit_price": None,
                        "reason": (
                            f"{symbol}: a resting {kind} that is not this side's to "
                            "cancel holds the shares; nothing of ours is placed over it"
                        ),
                        "order_id": order.get("order_id"),
                        "ours": False,
                    }
                )

    return {
        "place": place,
        "replace": replace,
        "keep": keep,
        # Every position the book could not be given a stop for, said out loud.
        "notes": [
            *plan.notes,
            *(
                f"{symbol}: entry price disputed between the broker's average "
                "and its tax lots — no protective order is placed, replaced or "
                "cancelled behind it this cycle, and whatever rests stays put "
                "until the operator resolves the basis"
                for symbol in sorted(disputed)
            ),
        ],
        # `place`/`replace`/`keep` are a diff against what already rests, not
        # a book of current levels — a `keep` entry's shape even differs from
        # `place`'s. `desired` is `plan.orders` verbatim: what should be
        # resting for the whole book *this cycle*, regardless of what the
        # broker already has. `services/core/core/run.py` persists it to
        # `edge_protection` so the 30-min state-change gate has something to
        # compare a live quote against without recomputing ATR, signals and
        # the book from scratch in a separate serverless function.
        "desired": [o.as_dict() for o in plan.orders],
    }


def _plural(count: int, noun: str) -> str:
    """``reason`` is read by a person in the runlog, not parsed by anything."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def run(ctx: Mapping[str, Any]) -> ReconcileResult:
    """Broker truth in, the reconciled cycle out. Pure function of JSON."""
    ours = _ours(ctx)
    owner = ctx.get("strategy") or "core"
    # One ownership answer for the cycle, computed once and threaded through
    # every pass that reads the broker's aggregate `positions` list. Two
    # passes disagreeing about whether a symbol is this side's is how a
    # position lands in the book but not the divergence check, or the reverse.
    excluded = _other_side_symbols(ctx, owner)

    # One attribution for the cycle. The book and the play ledger disagreeing
    # about which play owns a symbol is the one way a position could be exited
    # by one play and paid for by another.
    attribution = _attribution(ctx, ours, excluded)

    resub_findings, resub_cancels, resubmit, resub_retired = _entry_resubmissions(ctx, ours)
    order_findings, cancels, cycles, newly_retired = _order_findings(
        ctx, ours, excluded, frozenset(c["order_id"] for c in resub_cancels)
    )
    cancels = cancels + resub_cancels

    epsilon = float(ctx["config"]["reconcile_epsilon_quantity"])
    book, basis_findings = _reconciled_positions(ctx, attribution, excluded, epsilon)

    findings = [
        *order_findings,
        *resub_findings,
        *_fill_findings(ctx, ours, excluded),
        *_position_findings(ctx, excluded),
        *_symbol_swap_findings(ctx, epsilon, excluded),
        *basis_findings,
    ]

    control = ctx.get("control") or {}

    hold_days = int(ctx["config"].get("hold_days", 10))
    trading_day = (ctx.get("cycle") or {}).get("trading_day")
    remaining_holds, hold_findings = _expire_operator_holds(control, trading_day, hold_days)
    findings.extend(hold_findings)

    diverged = any(f.code in DIVERGENCE_CLASSES for f in findings)
    streak = int(control.get("divergence_streak") or 0) + 1 if diverged else 0
    persistent = streak >= PERSISTENT_DIVERGENCE_CYCLES

    if persistent:
        findings.append(
            Finding(
                RECONCILIATION_DIVERGENCE_PERSISTENT,
                "",
                f"the broker and the book have disagreed for {_plural(streak, 'cycle')} "
                "running; entries are blocked until edge_control is cleared",
            )
        )

    # Two blocks with two lifetimes, so they are two fields. §12.2's phantom
    # blocks "1 cycle" and lifts by itself; its persistent divergence blocks
    # "until cleared in edge_control", which means by the operator. A clean
    # cycle is evidence that the fault did not recur, not authorisation to
    # resume — a fault that alternates would clear itself forever.
    phantom = next((f for f in findings if f.code == PHANTOM_ORDER), None)
    until_cleared = bool(control.get("entries_blocked_until_cleared", False)) or persistent

    if until_cleared:
        blocked_reason = (
            "reconciliation divergence persisted across "
            f"{_plural(max(streak, PERSISTENT_DIVERGENCE_CYCLES), 'cycle')}; "
            "clear edge_control to resume entries"
        )
    elif phantom is not None:
        blocked_reason = phantom.detail
    else:
        blocked_reason = None

    # Bounded by intersecting with *this* cycle's `ours`: retirement only ever
    # needs to cover the one placements batch `edge_prior()` is currently
    # handing back. The moment a new placement supersedes that batch, `ours`
    # is a different set of order ids and the intersection drops every old
    # one on its own — no separate cleanup pass, no unbounded growth.
    retired = (
        set((control.get("retired_order_ids")) or [])
        | set(newly_retired)
        | set(resub_retired)
    ) & ours.keys()

    control_updates = {
        "positions_held": len(book),
        "has_whole_share_position": any(p.quantity >= 1.0 for p in book),
        # Cleared by any pass that completed, whatever it found. The routine
        # writes these updates only on success, so a reconcile that itself
        # failed leaves the flag set and the next cycle stays awake — no extra
        # mechanism needed. Divergence is a different fact and has its own
        # field: carrying both here would force every later cycle out of no-op
        # indefinitely, which is the expensive path §13 exists to avoid.
        "unreconciled": False,
        "pending_events": _pending_events(ctx, book),
        **_high_water_updates(ctx),
        "entries_blocked": bool(phantom is not None or until_cleared),
        "entries_blocked_reason": blocked_reason,
        "entries_blocked_until_cleared": until_cleared,
        "divergence_streak": streak,
        "open_order_cycles": cycles,
        "retired_order_ids": sorted(retired),
        # Entry limits this cycle cancelled
        # unfilled, handed back for the next monitor cycle's `cli.candidates`
        # to re-price and re-propose. Written every reconciling cycle — an
        # empty list overwrites a prior cycle's list so a re-queue never
        # outlives the cycle that raised it. `cli.candidates` also date-checks
        # `trading_day` before it unions.
        "resubmit_candidates": {
            "trading_day": (ctx.get("cycle") or {}).get("trading_day"),
            "triggers": resubmit,
        },
    }

    # Only write `operator_holds` back on the cycle something actually expired
    # — writing it every cycle regardless would churn the key for no reason,
    # the same economy `unreconciled`/`retired_order_ids` already follow
    # above.
    if hold_findings:
        control_updates["operator_holds"] = remaining_holds

    findings.sort(key=lambda f: (f.code, f.symbol, f.order_id or ""))

    return ReconcileResult(
        findings=findings,
        positions=book,
        cancels=cancels,
        control_updates=control_updates,
        loss_closes=_loss_closes(ctx),
        play_state_updates=_play_state_updates(ctx, attribution),
        realized_trades=_realized_trades(ctx, attribution),
        fills_quality=_fills_quality(ctx, ours),
        symbol_lock_claims=_symbol_lock_claims(ctx, book, owner),
        symbol_lock_releases=_symbol_lock_releases(ctx, book, owner),
        protection_plan=_protection_plan(ctx, book, ours),
        reason=(
            f"{_plural(len(book), 'position')} reconciled, "
            f"{_plural(len(findings), 'finding')}, "
            f"{_plural(len(cancels), 'cancel')}"
        ),
    )
