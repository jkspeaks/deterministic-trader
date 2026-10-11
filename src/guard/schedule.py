"""Candidate-set membership, the earnings T-2 flatten window, the wash-sale
cooldown, and entry-permission gating (universe staleness, the operator's own
``entries_blocked``)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping

from src.reconcile import reentry_block

from .core import Denial

NOT_IN_CANDIDATE_SET = "NOT_IN_CANDIDATE_SET"
EARNINGS_WINDOW = "EARNINGS_WINDOW"
EARNINGS_DATE_UNAVAILABLE = "EARNINGS_DATE_UNAVAILABLE"
WASH_SALE_COOLDOWN = "WASH_SALE_COOLDOWN"
UNIVERSE_STALE = "UNIVERSE_STALE"
ENTRIES_BLOCKED = "ENTRIES_BLOCKED"
ENTRIES_NOT_ALLOWED_THIS_CYCLE = "ENTRIES_NOT_ALLOWED_THIS_CYCLE"


def _held_symbols(ctx: Mapping[str, Any]) -> set[str]:
    return {p.get("symbol") for p in (ctx.get("positions") or [])}


def _candidate_denials(
    order: Mapping[str, Any], ctx: Mapping[str, Any]
) -> list[Denial]:
    """§5.3's fence — an order may only touch a symbol its own side's plan
    named today.

    Strategy-scoped since Dip Recovery phase 4.5, the same way
    `guard.strategy._strategy_enabled_denials` (phase 3) and
    `guard.lock._symbol_lock_denials` (phase 1) already branch on
    ``ctx["strategy"]``. Core's candidate set is `ctx["universe"]["candidates"]`
    and its held book is `ctx["positions"]`; Dip Recovery's are its own
    watchlist and its own `dip_recovery_positions` rows, and the two must
    never be read interchangeably — a Dip Recovery buy checked against core's
    universe would be refused for every symbol core's own scan never picked,
    and a Dip Recovery sell checked against core's book would be refused for
    every position Dip Recovery itself opened.
    """
    symbol = order.get("symbol")
    strategy = ctx.get("strategy") or "core"

    if strategy == "dip_recovery":
        # §5.3's "the candidate set includes current positions" holds for this
        # side too, and it has to be spelled out here because core gets it for
        # free: `universe._candidate_set` already folds core's book into
        # `candidates`, while Dip Recovery's watchlist is a *new-entry* list
        # the Universe Pick routine rebuilds each morning. Averaging down is
        # this strategy's core mechanic and it is a **buy**, so without the
        # union the first held symbol to drop off a morning's watchlist could
        # never be averaged into again — denied `NOT_IN_CANDIDATE_SET` while
        # the position it belongs to sat there waiting for exactly that.
        # The sell-side `held` union below stays as its own line: it also
        # admits a symbol core's book still shows, which is the exit-of-last-
        # resort the same §5.3 note describes.
        candidates = set(ctx.get("dip_recovery_watchlist") or []) | set(
            (ctx.get("dip_recovery_positions") or {}).keys()
        )
        held = set((ctx.get("dip_recovery_positions") or {}).keys()) | _held_symbols(ctx)
    else:
        candidates = set((ctx.get("universe") or {}).get("candidates") or [])
        held = _held_symbols(ctx)

    if symbol in candidates:
        return []

    # §5.3 defines the candidate set as including current positions, so a held
    # name is a candidate by construction. Admitting its exit anyway keeps an
    # upstream bug in universe.py from trapping a position with no way out.
    if order.get("side") == "sell" and symbol in held:
        return []

    return [
        Denial(
            NOT_IN_CANDIDATE_SET,
            f"{symbol} is not in today's candidate set; Invariant 3 allows only "
            "what the plan emits",
        )
    ]


def _earnings_denials(order: Mapping[str, Any], ctx: Mapping[str, Any]) -> list[Denial]:
    symbol = order.get("symbol")
    entry = (ctx.get("earnings") or {}).get(symbol)
    sessions = (entry or {}).get("sessions_until_report")

    if not entry or entry.get("status") != "known" or sessions is None:
        return [
            Denial(
                EARNINGS_DATE_UNAVAILABLE,
                f"no confirmed earnings date for {symbol}; the T-2 flatten is a "
                "promise that cannot be kept blind",
            )
        ]

    window = int(ctx["config"]["earnings_flatten_days"])
    if int(sessions) <= window:
        return [
            Denial(
                EARNINGS_WINDOW,
                f"{symbol} reports in {sessions} session(s); entries stop at "
                f"T-{window} and the system never holds through a print",
            )
        ]

    return []


def _as_date(value: str) -> date:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def _wash_sale_denials(
    order: Mapping[str, Any], ctx: Mapping[str, Any]
) -> list[Denial]:
    symbol = order.get("symbol")
    today = _as_date(ctx["cycle"]["now"])

    # A row whose basis this system could not read is a *close*, not a
    # measured loss. `wash_sale_block_unknown_basis` is what that behaviour
    # is now called, and `reconcile.reentry_block` is the one place it and
    # the window are read (P3: `sizing` asks the same function before it
    # spends a slot).
    blocked = reentry_block(symbol, ctx.get("loss_closes"), today, ctx["config"])
    if blocked is None:
        return []
    age, window = blocked
    return [
        Denial(
            WASH_SALE_COOLDOWN,
            f"{symbol} was closed at a loss {age} day(s) ago; "
            f"repurchase is blocked for {window} days",
        )
    ]


def _entry_permission_denials(ctx: Mapping[str, Any]) -> list[Denial]:
    """The gates every buy passes regardless of which symbol it is.

    Strategy-scoped since Dip Recovery phase 4.5, the same way
    `_candidate_denials` (above), `guard.strategy._strategy_enabled_denials`
    and `guard.lock._symbol_lock_denials` already branch on `ctx["strategy"]`.
    The operator's `entries_blocked` kill applies to both sides — it is set by
    core's reconcile when the account itself stops making sense, and "we do not
    currently understand this account" is not a fact one strategy may opt out
    of.

    **The cadence gate is core's alone**, corrected in phase 7. Phase 4.5 kept
    it for both sides, but `cycle.entries_allowed` comes from `cadence`'s
    cycle-type table — premarket, full, monitor, postclose, overnight — which
    is core's seven-slot schedule and describes nothing about when Dip
    Recovery may act. Dip Monitor runs on its own ~90-minute interval and its
    entire purpose is to enter on those ticks; judged against core's table
    every one of them is a `monitor` cycle, so every Dip Recovery entry was
    denied `ENTRIES_NOT_ALLOWED_THIS_CYCLE` — the same class of mistake as
    checking its buys against core's universe, one function and one phase
    later. What still bounds *when* this side may enter is session-shaped
    rather than cadence-shaped, and is already enforced elsewhere:
    `halts._account_halts` refuses a closed session outright, `legality`
    refuses the order types and fractional quantities a non-regular session
    will not take, and `strategy._strategy_enabled_denials` refuses everything
    while the flag is off.

    `UNIVERSE_STALE` is core's alone for the reason phase 4.5 gave: it is a
    fact about core's weekly membership snapshot
    (`ctx["universe"]["snapshot_age_days"]`), a set Dip Recovery never reads —
    its own freshness gate is the Dip Monitor routine's check that
    `dip_recovery_watchlist.trading_day` is today.
    """
    denials: list[Denial] = []
    strategy = ctx.get("strategy") or "core"

    if strategy == "core" and not (ctx.get("cycle") or {}).get("entries_allowed", False):
        denials.append(
            Denial(
                ENTRIES_NOT_ALLOWED_THIS_CYCLE,
                f"a {(ctx.get('cycle') or {}).get('cycle_type')} cycle places "
                "no entries",
            )
        )

    control = ctx.get("control") or {}
    if control.get("entries_blocked", False):
        denials.append(
            Denial(
                ENTRIES_BLOCKED,
                "entries are blocked: "
                f"{control.get('entries_blocked_reason') or 'no reason recorded'}",
            )
        )

    if strategy == "core":
        age = (ctx.get("universe") or {}).get("snapshot_age_days")
        max_age = int(ctx["config"]["universe_max_age_days"])
        if age is None or int(age) > max_age:
            denials.append(
                Denial(
                    UNIVERSE_STALE,
                    f"universe snapshot is {age} days old against a {max_age}-day "
                    "limit; stale membership means an unknown universe",
                )
            )

    return denials
