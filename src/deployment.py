"""Deployment.py — what one cycle did with the cash, and why.

"Every full cycle writes one `deployment_decision` ... Cash held for
REGIME_BLOCK or NO_CANDIDATES_PASS is a *decision*; anything else is a fault."

Three things, all arithmetic over what `decide` already computed:

* `funnel` — how many names survived each stage, from the universe down to an
  approved order, each symbol counted once and each stage no larger than the
  one before it.
* `record` — the row: posture, breadth, the funnel, the per-reason counts,
  cash, invested against the posture's target, and one of
  ``DEPLOY | PARTIAL | NONE`` with the reason.
* `play_activity` — a per-play count of consecutive trading days on which a
  play produced nothing but skips, so a play that has quietly stopped firing
  is noticed.

**Pure, and given no network** (Invariant 2). Nothing here decides a trade:
`decide` builds its plan first and this describes it.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence

DEPLOY = "DEPLOY"
PARTIAL = "PARTIAL"
NONE = "NONE"
DECISIONS = (DEPLOY, PARTIAL, NONE)

# Why cash was held, or why less went out than the plays ranked.
REGIME_BLOCK = "REGIME_BLOCK"                # the posture, a dark play, a sector/RS gate
NO_CANDIDATES = "NO_CANDIDATES"              # nothing triggered any play
NO_CANDIDATES_PASS = "NO_CANDIDATES_PASS"    # names triggered; every play declined
ROTATION_BLOCK = "ROTATION_BLOCK"            # operator's sector rotation / no-trade gate
SLOT_CAP = "SLOT_CAP"
SECTOR_CAP = "SECTOR_CAP"
CORRELATION_CAP = "CORRELATION_CAP"
SHARE_LIMIT = "SHARE_LIMIT"
CASH = "CASH"
REENTRY_BLOCK = "REENTRY_BLOCK"
DATA_STALE = "DATA_STALE"
GUARD_DENIED = "GUARD_DENIED"
EXEC_FAIL = "EXEC_FAIL"
DEPLOYED = "DEPLOYED"

# Holding cash for one of these is the system working as designed. Anything
# else is worth a look.
INTENDED_REASONS = frozenset({REGIME_BLOCK, NO_CANDIDATES, NO_CANDIDATES_PASS, DEPLOYED})

# Skip codes that mean "the play fired and the tape, not the name, said no".
_REGIME_CODES = frozenset(
    {
        "REGIME_DARK", "SKIP_REGIME", "SKIP_SECTOR_BELOW_200D",
        "SKIP_SECTOR_RS_WEAK", "SKIP_RS_UNAVAILABLE", "SKIP_DIP_TABLE_RSI",
    }
)
# ...and the ones applied to the ranked list afterwards.
_ARBITRATION_CODES = frozenset(
    {"SECTOR_INACTIVE", "OUTRANKED_ON_SYMBOL", "NO_TRADE_SPREAD", "NO_TRADE_VOL",
     "NO_TRADE_EQUITY", "NO_TRADE_DIVERGENCE"}
)

_UNSIZED_REASON = {
    "NO_OPEN_SLOTS": SLOT_CAP,
    "NO_CAP_HEADROOM": SECTOR_CAP,
    "ONE_PER_SECTOR_DAY": SECTOR_CAP,
    "WHOLE_SHARE_ONLY": SHARE_LIMIT,
    "SHARE_LIMIT": SHARE_LIMIT,
    "DEPLOYABLE_EXHAUSTED": CASH,
    "BELOW_MIN_NOTIONAL": CASH,
    "RISK_BUDGET": CASH,
    "TARGET_INVESTED_REACHED": REGIME_BLOCK,
    "POSTURE_NO_ENTRY": REGIME_BLOCK,
    "FULL_SEND_ACTIVE": CASH,
    "REENTRY_BLOCKED": REENTRY_BLOCK,
    "SETTLED_CASH_UNKNOWN": DATA_STALE,
    "NO_PRICE": DATA_STALE,
    "SECTOR_UNKNOWN": DATA_STALE,
}

# The stages, in order. `funnel_line` prints the ones a reader scans.
STAGES = (
    "universe", "discovered", "pool", "triggered", "passed_plays",
    "passed_regime", "ranked", "sized", "approved",
)
_LINE_STAGES = ("universe", "pool", "triggered", "ranked", "sized", "approved")


def _symbols(rows: Sequence[Any] | None, key: str = "symbol") -> set[str]:
    out: set[str] = set()
    for row in rows or []:
        value = row.get(key) if isinstance(row, Mapping) else row
        if value:
            out.add(str(value))
    return out


def funnel(
    *,
    members: Sequence[str] | None,
    pool: Sequence[str] | None,
    dropped: Sequence[Mapping[str, Any]] | None,
    entries: Mapping[str, Any] | None,
    sized: Sequence[Mapping[str, Any]] | None,
    approved: Sequence[Mapping[str, Any]] | None,
) -> dict:
    """Counts of distinct symbols at each stage, and where the rest stopped.

    Each stage is a subset of the one before it, by construction: a symbol is
    counted at a stage only if it was counted at the previous one.

    * ``universe`` — the index fence, plus anything discovery or the book
      reached outside it.
    * ``discovered`` — what discovery surfaced before its drops (the sector
      filter, the caps, the rotation) plus the book.
    * ``pool`` — what was actually fetched and handed to the plays.
    * ``triggered`` — names on which at least one play returned a verdict.
    * ``passed_plays`` — of those, names a play proposed (its own conditions
      held).
    * ``passed_regime`` — of those, names the regime, the posture and the
      sector gates let through.
    * ``ranked`` — names in the ranked candidate list `sizing` received.
    * ``sized`` / ``approved`` — names with a sized buy / a buy `guard` passed.

    ``stopped`` says where the triggered names that were not ranked ended, so
    ``ranked + sum(stopped.values()) == triggered``.
    """
    pool_set = _symbols(pool)
    discovered = pool_set | _symbols(dropped)
    universe = set(members or []) | discovered

    entries = entries or {}
    ranked = _symbols(entries.get("candidates")) & pool_set
    skipped: dict[str, set[str]] = {}
    for row in entries.get("skipped") or []:
        if isinstance(row, Mapping) and row.get("symbol") in pool_set:
            skipped.setdefault(str(row["symbol"]), set()).add(str(row.get("code")))

    stopped = {"plays": 0, "regime": 0, "arbitration": 0}
    for symbol, codes in skipped.items():
        if symbol in ranked:
            continue
        if codes & _REGIME_CODES:
            stopped["regime"] += 1
        elif codes & _ARBITRATION_CODES:
            stopped["arbitration"] += 1
        else:
            stopped["plays"] += 1

    triggered = len(ranked) + sum(stopped.values())
    sized_set = _symbols(sized) & ranked
    approved_set = {
        str(o.get("symbol")) for o in approved or []
        if isinstance(o, Mapping) and o.get("side") == "buy"
    } & sized_set

    return {
        "universe": len(universe),
        "discovered": len(discovered),
        "pool": len(pool_set),
        "triggered": triggered,
        "passed_plays": triggered - stopped["plays"],
        "passed_regime": triggered - stopped["plays"] - stopped["regime"],
        "ranked": len(ranked),
        "sized": len(sized_set),
        "approved": len(approved_set),
        "stopped": stopped,
    }


def funnel_line(counts: Mapping[str, Any]) -> str:
    """``518 > 20 > 12 > 3 > 1 > 1`` — universe, pool, triggered, ranked,
    sized, approved (and placed, once the cycle knows it)."""
    parts = [str(counts.get(stage, 0)) for stage in _LINE_STAGES]
    if counts.get("placed") is not None:
        parts.append(str(counts["placed"]))
    return " > ".join(parts)


def _counts(rows: Sequence[Any] | None, key: str) -> dict[str, int]:
    tally: Counter[str] = Counter()
    for row in rows or []:
        if isinstance(row, Mapping) and row.get(key):
            tally[str(row[key])] += 1
    return dict(sorted(tally.items()))


def _denied_buy_codes(guard: Mapping[str, Any] | None) -> dict[str, int]:
    tally: Counter[str] = Counter()
    for row in (guard or {}).get("denied") or []:
        if (row.get("order") or {}).get("side") != "buy":
            continue
        for denial in row.get("denials") or []:
            tally[str(denial.get("code"))] += 1
    for halt in (guard or {}).get("halts") or []:
        tally[str(halt.get("code"))] += 1
    return dict(sorted(tally.items()))


def _top(counts: Mapping[str, int]) -> str | None:
    """The most frequent key, alphabetical on a tie so the row is stable."""
    if not counts:
        return None
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _verdict(
    counts: Mapping[str, Any],
    unsized: Mapping[str, int],
    denied: Mapping[str, int],
    posture: str | None,
    posture_enforced: bool,
    blocked: int = 0,
) -> tuple[str, str, str]:
    """``(decision, reason, detail)`` from the funnel."""
    approved, ranked = counts["approved"], counts["ranked"]

    if approved > 0:
        if approved >= ranked:
            return DEPLOY, DEPLOYED, f"{approved} of {ranked} ranked name(s) approved"
        if counts["sized"] > approved:
            reason, why = GUARD_DENIED, _top(denied)
        else:
            why = _top(unsized)
            reason = _UNSIZED_REASON.get(why or "", CASH)
        return PARTIAL, reason, (
            f"{approved} of {ranked} ranked name(s) approved; the rest stopped "
            f"at {why or 'sizing'}"
        )

    if posture_enforced and posture in ("NO_NEW_POSITIONS", "HOLD_CASH"):
        return NONE, REGIME_BLOCK, f"the posture is {posture}"
    if counts["triggered"] == 0:
        return NONE, NO_CANDIDATES, (
            f"no play triggered on any of the {counts['pool']} name(s) looked at"
        )
    if ranked == 0:
        stopped = counts["stopped"]
        if stopped["regime"] > 0:
            return NONE, REGIME_BLOCK, (
                f"{stopped['regime']} name(s) a play proposed were stopped by "
                "the regime or a sector gate"
            )
        if stopped["arbitration"] > 0 and stopped["plays"] == 0:
            return NONE, ROTATION_BLOCK, (
                f"{stopped['arbitration']} proposed name(s) were outside the "
                "active rotation or suppressed by the no-trade gate"
            )
        return NONE, NO_CANDIDATES_PASS, (
            f"{counts['triggered']} name(s) triggered and every play declined"
        )
    if counts["sized"] == 0:
        why = _top(unsized)
        return NONE, _UNSIZED_REASON.get(why or "", CASH), (
            f"{ranked} ranked name(s), none sized ({why or 'no reason recorded'})"
        )
    why = _top(denied)
    if why is None and blocked > 0:
        # `decide` withholds an order the broker has refused twice running
        # (§14); nothing was denied, the order was simply not sent again.
        return NONE, EXEC_FAIL, (
            f"{counts['sized']} sized name(s), {blocked} withheld after repeated "
            "broker rejections"
        )
    return NONE, GUARD_DENIED, (
        f"{counts['sized']} sized name(s), none approved ({why or 'no denial recorded'})"
    )


def record(
    *,
    cycle_id: Any,
    cycle: Mapping[str, Any],
    regime: Mapping[str, Any] | None,
    counts: Mapping[str, Any],
    entries: Mapping[str, Any] | None,
    sizing: Mapping[str, Any] | None,
    guard: Mapping[str, Any] | None,
    account: Mapping[str, Any] | None,
    positions: Sequence[Mapping[str, Any]] | None,
    posture_enforced: bool,
    shadow: Mapping[str, Any] | None = None,
    blocked: Sequence[Mapping[str, Any]] | None = None,
) -> dict:
    """The `edge_deployment_decision` row for one entry-capable cycle.

    Written whether or not anything was bought: a cycle that held cash because
    the posture said to is recorded ``NONE / REGIME_BLOCK`` with
    ``intended: true``, which is the intended outcome and not a failure.
    """
    regime = regime or {}
    breadth = regime.get("breadth") or {}
    account = account or {}
    unsized = _counts((sizing or {}).get("unsized"), "reason")
    denied = _denied_buy_codes(guard)
    posture = regime.get("posture")

    withheld = sum(
        1 for row in blocked or []
        if isinstance(row, Mapping) and (row.get("order") or {}).get("side") == "buy"
    )
    decision, reason, detail = _verdict(
        counts, unsized, denied, posture, posture_enforced, withheld
    )

    equity = account.get("equity_total")
    try:
        equity = float(equity) if equity is not None else None
    except (TypeError, ValueError):
        equity = None
    invested = sum(float(p.get("market_value") or 0.0) for p in positions or [])
    cash = account.get("cash")
    if cash is None:
        cash = account.get("buying_power")

    return {
        "cycle_id": cycle_id,
        "trading_day": cycle.get("trading_day"),
        "cycle_type": cycle.get("cycle_type"),
        "posture": posture,
        "posture_enforced": bool(posture_enforced),
        "breadth_200": breadth.get("breadth_200"),
        "breadth_50": breadth.get("breadth_50"),
        "funnel": dict(counts),
        "skip_counts": {
            "plays": _counts((entries or {}).get("skipped"), "code"),
            "sizing": unsized,
            "guard": denied,
        },
        "cash": cash,
        "equity": equity,
        "target_invested_pct": regime.get("target_invested_pct"),
        "invested_pct": (
            None if not equity or equity <= 0 else round(invested / equity, 6)
        ),
        "decision": decision,
        "reason": reason,
        "detail": detail,
        "intended": reason in INTENDED_REASONS,
        "placed": None,
        "shadow": dict(shadow or {}),
    }


def with_placements(
    row: Mapping[str, Any],
    placements: Sequence[Mapping[str, Any]] | None,
    rejections: Sequence[Mapping[str, Any]] | None,
) -> dict:
    """The decide-time row, completed with what the broker actually took.

    ``placed`` is buys placed. Approved buys the broker refused turn the
    decision into ``PARTIAL`` or ``NONE`` with ``EXEC_FAIL`` — a plan that did
    not execute is not a deployment, and it is never "intended".
    """
    out = dict(row)
    counts = dict(out.get("funnel") or {})
    placed = sum(
        1 for p in placements or []
        if isinstance(p, Mapping) and p.get("side") == "buy"
        and not p.get("protective_stop") and not p.get("protective_target")
    )
    rejected = sum(
        1 for r in rejections or []
        if isinstance(r, Mapping) and (r.get("order") or r).get("side") == "buy"
    )
    counts["placed"] = placed
    out["funnel"] = counts
    out["placed"] = placed

    approved = int(counts.get("approved") or 0)
    if approved > 0 and placed < approved:
        out["decision"] = NONE if placed == 0 else PARTIAL
        out["reason"] = EXEC_FAIL
        out["detail"] = (
            f"{approved} buy(s) approved, {placed} placed"
            + (f", {rejected} refused by the broker" if rejected else "")
        )
        out["intended"] = False
    return out


def summary_line(row: Mapping[str, Any]) -> str:
    """The one line the status alert carries."""
    posture = row.get("posture") or "no regime"
    breadth = row.get("breadth_200")
    seen = "breadth n/a" if breadth is None else f"breadth {float(breadth):.0%}"
    return (
        f"Deployment: {posture}, {seen} · funnel "
        f"{funnel_line(row.get('funnel') or {})} · {row.get('decision')} "
        f"({row.get('reason')})"
    )


# ---------------------------------------------------------------------------
# Dead plays
# ---------------------------------------------------------------------------

PLAY_ACTIVITY_KEY = "play_activity"
DEAD_PLAY_ALERT_DAYS = "dead_play_alert_days"
DEAD_PLAY_ALERT_DAYS_DEFAULT = 5


def play_activity(
    prior: Mapping[str, Any] | None,
    *,
    plays: Sequence[str],
    alive: set[str],
    trading_day: str,
    threshold: int,
) -> tuple[dict, list[dict]]:
    """Advance each play's dead-day streak; return ``(state, newly dead)``.

    A play is *alive* on a cycle when it put at least one new name into the
    ranked list — a verdict other than a skip. The streak counts **trading
    days**, not cycles: the first entry-capable cycle of a day on which the
    play is not alive adds one, later cycles that day add nothing, and any
    cycle on which it is alive resets it to zero (including later the same
    day). ``newly dead`` names a play exactly once, on the cycle its streak
    reaches ``threshold``; it is not repeated while the streak runs on.

    Plays absent from ``plays`` (switched off, or darked by the operator) are
    dropped from the state, so a play that is turned back on starts at zero.
    """
    prior = prior or {}
    state: dict[str, dict] = {}
    dead: list[dict] = []
    for play in plays:
        row = prior.get(play) if isinstance(prior.get(play), Mapping) else {}
        streak = int(row.get("streak") or 0)
        streak_day = row.get("streak_day")
        alive_day = row.get("alive_day")

        if play in alive:
            streak, alive_day = 0, trading_day
        elif alive_day != trading_day and streak_day != trading_day:
            streak, streak_day = streak + 1, trading_day
            if streak == threshold:
                dead.append({"play_id": play, "days": streak})

        state[play] = {"streak": streak, "streak_day": streak_day, "alive_day": alive_day}
    return state, dead
