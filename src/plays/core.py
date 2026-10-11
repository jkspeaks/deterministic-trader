"""plays.core — shared types, constants and cross-play primitives.

Everything here is used by at least two of the four plays, by both `entries`
and `exits`, or by the arbitration layer — which is what earns it a place in
the shared module rather than in one play's own file. A play-specific
threshold (``PEAD_MIN_REACTION_PCT``, ``DIP_RSI_TRIGGER``, ...) lives with its
play instead; see `pead.py`, `dip.py`, `catalyst.py`, `macro.py`, `top_up.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping

from src import cadence
from src import core_sleeve
from src import regime as regimes
from src.cadence import sessions_between

PEAD = "PEAD"
DIP = "DIP"
CATALYST = "CATALYST"
MACRO = "MACRO"
# "Full send": when `full_send_enabled` is true in `edge_control` (a runtime
# switch, `surge on|off`) and a name is in a clean, quickly-monetisable uptrend,
# the cycle is allowed to commit the *entire* available cash across up to
# `SURGE_MAX_NAMES` such names, ignoring §4.4's slot, position and sector caps
# entirely. The trigger, conviction scale and exit terms live in `surge.py`; the
# cap bypass is a `full_deployment` flag `sizing.py` and `guard.concentration`
# both honour. Governed by that switch, not by a daily `edge_play_state` regime
# row — an absent row reads as on for SURGE, though an explicit 0.0 still darks
# it.
SURGE = "SURGE"
# "Sector-relative oversold rotation": buy the sector ETF itself when it is
# oversold *and* still above its own 200-day, while the broad tape has not
# rolled over. It shares MACRO's universe (`config/etfs.json`'s eleven) and
# nothing else — MACRO rotates on a macro *release* and holds fifteen sessions,
# this one rotates on the ETF's own oscillator and holds ten. The trigger, the
# exit terms and the 1.5xATR stop live in `sector.py` and in the `PLAYS` row
# below; the only thing `config/config.json` says about it is whether it is
# switched on at all, and it ships off. See `plays/sector.py`.
SECTOR = "SECTOR"
# §12.2. An adopted trade with no originating play at all. `reconcile.py` now
# binds a play at adoption instead (operator ruling), so this is the residual
# case: a fill the book has never seen and no play would claim.
MANUAL = "MANUAL"

# §17's fifth play — "a new entry in plays.py", which is exactly what this is.
# It has no trigger and `entries` never proposes it: it is a set of *management*
# terms for a position the system inherited rather than chose (operator ruling).
#
# It exists because binding one of the four to an inherited name is wrong twice
# over. `_play_state_updates` would credit that play's `edge_play_state` row for
# a decision it did not make, which is the exact contamination its own docstring
# refuses; and `DIP` carries a signal exit at RSI >= 50, so a healthy momentum
# holding inherited at RSI 60 would be sold on the next cycle — "realise a loss
# to free capital", the churn ruled out on the same day.
#
# A real play still wins where its *own trigger fires today*, because that is
# evidence about the name rather than a default. `ADOPTED` is what is left.
ADOPTED = "ADOPTED"

# The core sleeve (`src/core_sleeve.py`): one broad ETF holding idle cash. A
# play id so its position resolves exit terms like any other, and absent from
# `entries`'s play list so no play can propose it. Its terms are the reverse
# of a trade's: no time stop, no per-cycle stop check, one resting target.
CORE = core_sleeve.CORE

# Every position in the account is deterministic-trader's own, whoever opened it, and
# is managed to make money rather than to record who bought it. Binding a play at adoption is
# what delivers that — but two of the four plays declare no `target_pct`, so a name bound to
# `DIP` or `MACRO` would still have no way to *sell up*, only the stop and the signal. This is
# the floor that closes it, and it applies only where the bound play declares none: a play's own
# target is its property and adoption never loosens it.
#
# 10% rather than PEAD's 8% because the operator's other ruling removed the
# clock that would otherwise push an adopted name out — with the time stop
# unable to realise a loss, a tighter floor would just take small gains off
# positions that have nothing forcing them to move.
ADOPTED_TARGET_PCT = 0.10

# `ADOPTED`'s leash, and the longest of the five deliberately. The inherited
# book is a momentum sleeve (operator) — good names with no thesis of ours to
# expire. The time stop cannot realise a loss on an adopted position anyway, so
# a shorter clock would only take small gains off winners that have nothing
# forcing them to move.
ADOPTED_HOLDING_DAYS = 15

# EDGAR item codes. 4.02 is non-reliance — the company has said its own
# published financials cannot be trusted. 5.02 is an officer departure.
ITEM_NON_RELIANCE = "4.02"
ITEM_OFFICER_DEPARTURE = "5.02"

# §6.3 sizes the play at 0.75x "until it has 20 closed trades". The count lives
# in `edge_play_state` and `reconcile.py` writes it. Lives here rather than in
# `catalyst.py` because only `_notional_multiplier` below reads it — `_catalyst`
# itself never does — and `_notional_multiplier` is arbitration-shared, not
# CATALYST-specific.
CATALYST_PROVEN_TRADES = 20
CATALYST_UNPROVEN_MULTIPLIER = 0.75

# Why a candidate a play looked at did not become one. A play whose *trigger*
# never fired records nothing at all: a note per non-triggering name would be
# hundreds of rows a cycle saying nothing happened, which is how a runlog stops
# being read.
ENTRY_CONDITIONS_UNMET = "ENTRY_CONDITIONS_UNMET"
EARNINGS_VETO = "EARNINGS_VETO"
NO_CONVICTION = "NO_CONVICTION"
REGIME_DARK = "REGIME_DARK"
# `REGIME_UNSET` and `CROWDED_OUT` were both retired with the deterministic
# regime (see `_regime_multiplier` and `entries._arbitrate`). Nothing emits
# them any more: an absent regime is no longer a refusal, and the crowding
# penalty no longer subtracts from a score it could erase. Old
# `edge_runlog` rows still carry the two strings and are read as history.
OUTRANKED_ON_SYMBOL = "OUTRANKED_ON_SYMBOL"
TOP_UP_UNMANAGED = "TOP_UP_UNMANAGED"
# The operator's `edge_control.active_sectors` excludes the name's sector, so
# no new money goes into it — a new entry, a top-up, or a play re-triggering
# on a name already held. Exits and protection are untouched.
SECTOR_INACTIVE = "SECTOR_INACTIVE"
HARD_VETO_RESTATEMENT = "HARD_VETO_RESTATEMENT"
# P1 of an earlier audit fixes, emitted only under `regime_v2_enabled`
# (and in `cli.decide`'s shadow fields when it is off). Defined in
# `src/regime.py`, beside the rules that emit them, so the Dip Recovery
# sleeve's copies cannot drift from these.
SKIP_REGIME = regimes.SKIP_REGIME
SKIP_SECTOR_BELOW_200D = regimes.SKIP_SECTOR_BELOW_200D
SKIP_SECTOR_RS_WEAK = regimes.SKIP_SECTOR_RS_WEAK
SKIP_RS_UNAVAILABLE = regimes.SKIP_RS_UNAVAILABLE
# S3: a row of `dip_regime_table` refused a dip on its RSI ceiling. Emitted
# only with `dip_regime_table_enabled` and a row that sets `max_rsi`.
SKIP_DIP_TABLE_RSI = regimes.SKIP_DIP_TABLE_RSI

# P2 of an earlier audit fixes (W5): **missing data is not a failed test.**
# CATALYST used to print "no change on 0.0x average volume" for a name whose
# quote or volume never arrived (126 of 157 rejections), and PEAD folded an
# absent reaction row into "does not clear §6.1". Each case below is its own
# code, so a tally of skips separates "the tape said no" from "we could not see
# the tape". None of them changes which names enter.
#
# CATALYST (`plays/catalyst.py`):
SKIP_NO_CHANGE = "SKIP_NO_CHANGE"                    # no quote change to read
SKIP_NO_VOLUME = "SKIP_NO_VOLUME"                    # volume/average missing or zero
SKIP_BELOW_MIN_MOVE = "SKIP_BELOW_MIN_MOVE"          # a real move, under 3%
SKIP_BELOW_RVOL = "SKIP_BELOW_RVOL"                  # raw volume multiple under 1.5x
SKIP_BELOW_RVOL_TNORM = "SKIP_BELOW_RVOL_TNORM"      # time-normalised, under 1.5x
SKIP_TOO_EARLY_FOR_RVOL = "SKIP_TOO_EARLY_FOR_RVOL"  # too few minutes to extrapolate
# PEAD (`plays/pead.py`):
SKIP_NO_REACTION_ROW = "SKIP_NO_REACTION_ROW"        # row should exist and does not
SKIP_REACTION_PENDING = "SKIP_REACTION_PENDING"      # reaction session not closed yet
SKIP_NO_SURPRISE = "SKIP_NO_SURPRISE"                # no EPS surprise figure
SKIP_NO_TREND_DATA = "SKIP_NO_TREND_DATA"            # no price or no 50-day

REGIME_V2_ENABLED = "regime_v2_enabled"


def regime_v2_enabled(ctx: Mapping[str, Any]) -> bool:
    """Whether the posture and DIP's sector rule are enforced on entries.

    Truthiness, not presence, like `pullback_enabled`: every payload and
    golden that predates the key reads as off, which is the shipped rule.
    """
    return bool((ctx.get("config") or {}).get(REGIME_V2_ENABLED))

# §6.5, in priority order. A position leaves once, and the record names the
# reason that mattered most. `DRAWDOWN_FLATTEN` is deliberately not guard's
# `DRAWDOWN_HALT`: one emits a sell and the other refuses a buy, and a shared
# code in the runlog would make the two indistinguishable after the fact.
HARD_EVENT_EXIT = "HARD_EVENT_EXIT"
DRAWDOWN_FLATTEN = "DRAWDOWN_FLATTEN"
EARNINGS_DEADLINE = "EARNINGS_DEADLINE"
ATR_STOP = "ATR_STOP"
TIME_STOP = "TIME_STOP"
TARGET = "TARGET"
# Exit 6, refined. §6.5 item 6: a TARGET exit where a qualifying
# CATALYST-window event also confirms it — "exit now on confirmed momentum
# instead of waiting for a possible pullback" rather than a healthy position
# being trimmed. Reuses exit 6's priority and full-sell path; no new slot.
TARGET_CATALYST = "TARGET_CATALYST"
SIGNAL_EXIT = "SIGNAL_EXIT"

# The loss-sell news-hold these two constants governed — a loss-making
# ATR_STOP/TIME_STOP delayed on a live session's read of whether recent news
# explained a bounce — was removed with `exits._news_hold_gate`. It never opened
# once in production (0 `held_for_news` rows) and was a prompt-resident judgment
# on untrusted news text with no schema. The names are kept only so an old
# `edge_runlog` row that still carries `loss_holds` / `held_for_news` parses;
# nothing reads them now.
LOSS_HOLD_MAX_DAY = 4
LOSS_HOLD_EXTENDED_MAX_DAY = 7

# §6.5 exit 1. A restatement on a name already held.
#
# 5.02 (`ITEM_OFFICER_DEPARTURE`) was dropped. CI reads 8-K item codes out of
# EDGAR's `getcurrent` Atom summary, which carries the code but not the
# officer's role — so a routine director election looked identical to a
# CEO/CFO departure and forced a noise exit. `ci/fetch_events.py` cannot
# distinguish the two without fetching each document, which §7.2 rules out on
# cost, so 5.02 is surfaced as a note in `exits.py` for the operator to
# eyeball rather than an automatic sale. The constant is still exported;
# nothing forces on it now.
FORCE_EXIT_ITEMS = frozenset({ITEM_NON_RELIANCE})


def force_exit_items(ctx: Mapping[str, Any]) -> frozenset[str]:
    """Which 8-K item codes force an exit this cycle.

    `FORCE_EXIT_ITEMS` unless `config.force_exit_items` names a list, which
    `config/config.json` does not — so the live account reads exactly the
    frozenset above, and the only caller that overrides it is the shadow
    ledger's `C_hard_event_502` variant, which puts 5.02 back to measure what
    dropping it was worth. An earlier ruling above is the default; it is not
    the only rule the evidence is allowed to support.

    A non-list, or a list of anything but strings, reads as the default. An
    override that silently emptied this set would turn off the one exit §6.5
    describes as not waiting for the price.
    """
    override = (ctx.get("config") or {}).get("force_exit_items")
    if not isinstance(override, (list, tuple)) or not override:
        return FORCE_EXIT_ITEMS
    codes = frozenset(str(code) for code in override if isinstance(code, str))
    return codes or FORCE_EXIT_ITEMS

REGULAR_HOURS = "regular_hours"

# Where a conviction stops meaning anything, and the same precision
# `sizing.py` floors share quantities to.
CONVICTION_PRECISION = 6

# The precision `sizing.py` floors fractional quantities to. A quantity within
# this of its own round() is a whole share; Robinhood does not accept a limit
# order on anything else (the incident `report._failure_fragment`'s docstring
# names verbatim: "Limit order quantity cannot include fractional shares").
#
# Shared rather than private to `exits.py`: `protection.py` asks the same
# question of the same positions — §10.1 gives a resting stop to a `WHOLE`
# position and none at all to a `FRACTIONAL` one — and two answers to "is this
# a whole share" would eventually disagree, leaving a position `exits` treats
# as sellable and `protection` refuses to cover.
_WHOLE_SHARE_EPSILON = 1e-6


def _is_whole_share(quantity: float) -> bool:
    return abs(quantity - round(quantity)) <= _WHOLE_SHARE_EPSILON


@dataclass(frozen=True)
class Play:
    """A play's exit terms, which §6.5 evaluates and Invariant 3 freezes.

    ``holding_days`` is also §6.0's tiebreak input: ties break toward the
    shorter period, because faster capital turnover matters more than expected
    edge when only three slots exist.
    """

    play_id: str
    holding_days: int
    target_pct: float | None
    # Added for §4.3 C, which states a 1.5xATR stop where the shipped global is
    # 2.0 — and a stop multiple is exactly the kind of thing §17 calls a play's
    # own property. `stop_multiple` below is the one resolver; `exits`,
    # `protection` and `sizing` all read it rather than
    # `config["atr_stop_multiple"]` directly, so the stop a position is exited on,
    # the stop that rests at the broker and the stop the risk budget sizes against
    # cannot disagree.
    stop_atr_multiple: float | None = None
    # False removes §6.5 item 4, the per-cycle ATR stop check, for this play.
    # Only `CORE` sets it: S4 measured the sleeve with no stop check, and a
    # broad ETF sold on a dip is what the owner's standing rule rules out.
    # This is a check withheld, never an order added.
    stop_check: bool = True
    # False removes §6.5 item 5, the time stop. `holding_days` then only feeds
    # §6.0's tiebreak, which a play that never enters `entries` never reaches.
    time_stop: bool = True


# §17's SURGE knobs. The exit profile is deliberately ordinary — a 7-day clock
# (the shortest, "quick to monetise") and an 8% target — so a full-send position
# is managed on the same §6.5 terms as any other, including the global 2xATR
# stop. The concentration bypass is the whole of what makes SURGE different, and
# it lives in `sizing`/`guard`, not in a looser stop.
SURGE_HOLDING_DAYS = 7
SURGE_TARGET_PCT = 0.08
# How many names one full-send cycle may spread the entire cash across. The
# operator's "1 or 2 or 3": a hard ceiling, not a target.
SURGE_MAX_NAMES = 3

# `DIP` and `MACRO` declared `target_pct: None`, so the only way either could
# sell *up* was its own signal exit — RSI back through 50, or the operator
# flipping the macro regime — and a `DIP` that recovered without reaching RSI
# 50 rode the round trip back down. A resting broker-side limit needs a price
# to rest at (`plays/protection.py`), and "no target" is not one.
#
# Deliberately tighter than `PEAD`'s 8% and `CATALYST`'s 10%. Both of those are
# event plays paid for by a re-rating; `DIP` is a mean reversion, which is a
# smaller move by construction, and `MACRO` is an index-tracking sector ETF,
# where 5% is a large one. They are module constants and not config keys for
# §17's reason: a play's shape is the play's own property.
DIP_TARGET_PCT = 0.06
MACRO_TARGET_PCT = 0.05

# §4.3 C's exit terms, verbatim from the play's row: "Exit at RSI >= 55
# or +4%; stop 1.5xATR; time 10". Module constants and not config keys for
# §17's reason — the numbers *are* the hypothesis, and a tunable version of it
# would be a different hypothesis the shadow evidence would not be about. The
# only config key this play has is the switch that turns it on.
SECTOR_HOLDING_DAYS = 10
SECTOR_TARGET_PCT = 0.04
SECTOR_STOP_ATR_MULTIPLE = 1.5
SECTOR_RSI_EXIT = 55.0

PLAYS: dict[str, Play] = {
    CATALYST: Play(CATALYST, 7, 0.10),
    DIP: Play(DIP, 8, DIP_TARGET_PCT),
    PEAD: Play(PEAD, 10, 0.08),
    MACRO: Play(MACRO, 15, MACRO_TARGET_PCT),
    SURGE: Play(SURGE, SURGE_HOLDING_DAYS, SURGE_TARGET_PCT),
    # The only play that declares its own stop. Off by default
    # (`play_sector_enabled`), but present in `PLAYS` unconditionally: a
    # position opened while the switch was on must still resolve its exit
    # terms after someone turns it off, and a play id that vanishes from this
    # table would strand that position on `_reason_to_leave`'s `play is None`
    # branch with no time stop and no target at all.
    SECTOR: Play(SECTOR, SECTOR_HOLDING_DAYS, SECTOR_TARGET_PCT, SECTOR_STOP_ATR_MULTIPLE),
    # Resolvable by `_reason_to_leave` so an inherited name gets §6.5's exits 5
    # and 6, and absent from `entries`'s play list so it can never buy. It
    # declares its own target, so adoption's floor never applies to it.
    ADOPTED: Play(ADOPTED, ADOPTED_HOLDING_DAYS, ADOPTED_TARGET_PCT),
    # Present whatever `core_sleeve_enabled` says, for SECTOR's reason above:
    # a sleeve bought while the switch was on must keep its terms after it is
    # turned off. The target declared here is the default; `target_pct` below
    # reads `core_target_pct` from config and falls back to this.
    CORE: Play(CORE, core_sleeve.HOLDING_DAYS, core_sleeve.TARGET_PCT_DEFAULT,
               stop_check=False, time_stop=False),
}

# The two play ids that are not one of §6's four, and therefore not the agent's
# to enable or disable. `entries` proposes neither, and `report._play_fragment`
# skips both so a row appearing is never reported as a decision. `CORE` joined:
# the sleeve is switched by config, never by a regime row, and it too would
# otherwise be reported "disabled" the first time its `edge_play_state` row
# appeared.
UNCHOSEN = frozenset({MANUAL, ADOPTED, CORE})


@dataclass(frozen=True)
class Candidate:
    """A play's proposal, arbitrated by §6.0 and ready for `sizing.size`.

    ``conviction`` is what sizing ranks on: three of §6.0's four terms, with
    only ``whole_share_bonus`` left for sizing to add because only the ladder
    knows what a rung is worth. The unarbitrated number survives beside it so
    attribution can tell a weak signal from a dark regime.
    """

    symbol: str
    play_id: str
    conviction: float
    conviction_raw: float
    regime_fit: float
    crowding_penalty: float
    expected_holding_days: int
    notional_multiplier: float
    detail: str
    # SURGE only. Tells `sizing.size` to fund this candidate from the whole
    # available cash and `guard.concentration` to skip §4.4's caps for its
    # order. Default false, so every other play is untouched.
    full_deployment: bool = False

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "play_id": self.play_id,
            "conviction": self.conviction,
            "conviction_raw": self.conviction_raw,
            "regime_fit": self.regime_fit,
            "crowding_penalty": self.crowding_penalty,
            "expected_holding_days": self.expected_holding_days,
            "notional_multiplier": self.notional_multiplier,
            "detail": self.detail,
            "full_deployment": self.full_deployment,
        }


@dataclass(frozen=True)
class Skipped:
    """A play that triggered and then declined. Not a denial — guard owns those."""

    symbol: str
    play_id: str
    code: str
    message: str
    # The numbers the verdict was read from, where a play records them (P2:
    # CATALYST's `rvol_raw` / `rvol_tnorm`). Absent from the dict when a play
    # records none, so every skip that predates the field serialises as before.
    factors: Mapping[str, Any] | None = None

    def as_dict(self) -> dict:
        out = {
            "symbol": self.symbol,
            "play_id": self.play_id,
            "code": self.code,
            "message": self.message,
        }
        if self.factors is not None:
            out["factors"] = dict(self.factors)
        return out


@dataclass
class EntryResult:
    candidates: list[Candidate] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "candidates": [c.as_dict() for c in self.candidates],
            "skipped": [s.as_dict() for s in self.skipped],
        }


@dataclass(frozen=True)
class Exit:
    """A position leaving, and the §6.5 rule that took it out.

    Order-shaped because plays is the module that knows the exit is happening
    and everything the order needs is already in front of it. It is still only
    an intent: guard decides whether it may be placed, and will refuse a
    fractional quantity outside regular hours on its own reading of P1.
    """

    symbol: str
    play_id: str
    priority: int
    code: str
    reason: str
    quantity: float
    order_type: str
    session: str
    limit_price: float | None

    def as_dict(self) -> dict:
        """The record: what left, and which §6.5 rule took it out."""
        return {
            "symbol": self.symbol,
            "play_id": self.play_id,
            "priority": self.priority,
            "code": self.code,
            "reason": self.reason,
            "quantity": self.quantity,
            "order_type": self.order_type,
            "session": self.session,
            "limit_price": self.limit_price,
        }

    def as_order(self) -> dict:
        """Exactly `guard.evaluate`'s order shape, so nothing translates.

        ``side`` is supplied rather than carried: every §6.5 outcome is a sale,
        and a field that is only ever one value belongs at the boundary that
        needs it rather than in the record. ``stop_price`` is always None —
        §10.1 gives the resting stop to `sizing.py` at entry, and an exit
        already past its stop is a sale, not another stop.
        """
        return {
            "symbol": self.symbol,
            "side": "sell",
            "type": self.order_type,
            "quantity": self.quantity,
            "limit_price": self.limit_price,
            "stop_price": None,
            "session": self.session,
            "play_id": self.play_id,
        }


@dataclass(frozen=True)
class HeldForNews(object):
    """The loss-sell news-hold was removed; `exits.py` never constructs this
    any more and `ExitResult.held_for_news` is a permanently-empty list.
    Kept so an old `edge_runlog` row that carries one still deserialises.
    """

    symbol: str
    day: int
    secondary_stop: float
    reason: str

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "day": self.day,
            "secondary_stop": self.secondary_stop,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class AverageDownEligible:
    """The average-down offer was removed — it contradicted `plays/top_up.py`'s
    "never average down" and the decision lived in prompt text. `exits.py`
    never constructs this any more and `ExitResult.average_down_eligible` is a
    permanently-empty list. Kept so an old `edge_runlog` row that carries one
    still deserialises.
    """

    symbol: str
    entry_price: float | None
    price: float
    drop_since_last_fill_pct: float
    detail: str

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "entry_price": self.entry_price,
            "price": self.price,
            "drop_since_last_fill_pct": self.drop_since_last_fill_pct,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ManualRequired(object):
    """An exit `exits.py` computed but cannot place as a limit order itself.

    Two causes, both terminal for this cycle: a fractional quantity (Robinhood
    rejects a limit order on one — the same broker restriction "Limit order
    quantity cannot include fractional shares" that named Invariant 4's own
    incident), or a missing bid to price one against. Either way this is the
    operator's own "notify and I will place the limit myself" policy, not a
    silent failure — no market fallback.
    """

    symbol: str
    code: str
    reason: str

    def as_dict(self) -> dict:
        return {"symbol": self.symbol, "code": self.code, "reason": self.reason}


@dataclass(frozen=True)
class CoveredByResting:
    """A §6.5 exit that fired and was not placed, because a resting broker order
    already sells the position at that price.

    Not a skip and not a refusal: the rule fired, the sale is happening, and
    the order carrying it out was placed on an earlier cycle. Recorded because
    the alternative is a `TARGET` that vanishes from the runlog on the exact
    cycle it was reached — the position leaves and nothing in the record says
    which rule took it out. See `exits._covering_order`.
    """

    symbol: str
    code: str
    order_id: str | None
    reason: str

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "code": self.code,
            "order_id": self.order_id,
            "reason": self.reason,
        }


@dataclass
class ExitResult:
    exits: list[Exit] = field(default_factory=list)
    covered_by_resting: list[CoveredByResting] = field(default_factory=list)
    held_for_news: list[HeldForNews] = field(default_factory=list)
    manual_required: list[ManualRequired] = field(default_factory=list)
    average_down_eligible: list[AverageDownEligible] = field(default_factory=list)
    control_updates: dict = field(default_factory=dict)
    # Free-text observations that are not an exit and not a hold — today the one
    # case is a DRAWDOWN_FLATTEN that could not be evaluated because
    # `equity_total` was unreadable (guard halts the cycle with
    # ACCOUNT_UNREADABLE instead). Recorded so the runlog shows *why* §10 did
    # not fire rather than leaving it silently absent.
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "exits": [e.as_dict() for e in self.exits],
            "covered_by_resting": [c.as_dict() for c in self.covered_by_resting],
            "held_for_news": [h.as_dict() for h in self.held_for_news],
            "manual_required": [m.as_dict() for m in self.manual_required],
            "average_down_eligible": [a.as_dict() for a in self.average_down_eligible],
            "control_updates": dict(self.control_updates),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class _Proposal:
    """What a play returns before §6.0 has had its say."""

    symbol: str
    play_id: str
    conviction: float
    detail: str


# --- the stop, and the target measured off it -------------------------------
#
# These two functions are the single reading.


def stop_multiple(play_id: Any, ctx: Mapping[str, Any]) -> float:
    """How many ATRs below entry this play's stop sits.

    The play's own number where it declares one (`SECTOR`, 1.5x), otherwise
    `config.atr_stop_multiple` — which is every other play, and therefore
    every position the live account holds today.
    """
    play = PLAYS.get(play_id)
    if play is not None and play.stop_atr_multiple is not None:
        return float(play.stop_atr_multiple)
    return float(ctx["config"]["atr_stop_multiple"])


def stop_distance(play_id: Any, atr: Any, ctx: Mapping[str, Any]) -> float | None:
    """``stop_multiple x ATR`` in price terms, or None on an unusable ATR.

    The distance rather than the price, because the two callers want different
    things from it: `protection` subtracts it from an entry, `sizing` divides
    an equity risk budget by it. Neither should be re-deriving the multiple.
    """
    if atr is None:
        return None
    try:
        value = float(atr)
    except (TypeError, ValueError):
        return None
    if value <= 0.0:
        return None
    return stop_multiple(play_id, ctx) * value


# §4.3 A's target: "1.5x stop distance", which is an R-multiple rather than the
# fixed percentage every play has declared until now. It is DIP's target only
# while the pullback reshape is switched on — see `plays/dip.py`.
DIP_PULLBACK_ENABLED = "play_dip_pullback_enabled"
DIP_PULLBACK_TARGET_STOP_MULTIPLE = "dip_pullback_target_stop_multiple"
DIP_PULLBACK_TARGET_STOP_MULTIPLE_DEFAULT = 1.5


# "stop DIP entries with a switch, not a
# deletion". True as shipped, and an absent key reads as true: every payload,
# golden and config-less context that predates the key keeps buying exactly as
# it did. Read by `_operator_dark` (new money) and `cli.candidates` (the
# discovery and fetch work that exists only to feed DIP).
DIP_ENTRIES_ENABLED = "dip_entries_enabled"
DIP_ENTRIES_OFF_REASON = (
    f"{DIP_ENTRIES_ENABLED} is false in config/config.json"
)


def dip_entries_enabled(source: Mapping[str, Any] | None) -> bool:
    """Whether DIP may put new money to work. ``source`` is a context carrying
    ``config``, or the config itself.

    Only an explicit ``false`` stops it. It stops new positions and top-ups of
    DIP positions; it does not touch a held DIP position's exits, its resting
    take-profit, or which play `reconcile` binds an inherited name to.
    """
    source = source or {}
    config = source.get("config") if isinstance(source.get("config"), Mapping) else source
    return config.get(DIP_ENTRIES_ENABLED) is not False


# Acted (operator).
#
# RETIRED plays are off in code. There is no switch: bringing one back is a
# code change with a new test of the play behind it. SHELVED plays are off
# behind a config key each, because their verdict was "not shown", not "shown
# to be nothing". An absent key reads as off, so a config that loses the key
# cannot resume trading a shelved play.
#
# Off means no new money and none of the work that only feeds the play: no
# proposal, no top-up, no discovery trigger, no dead-play count, and for PEAD
# no pre-market reaction fetch. It does not touch a position already held
# under one of these plays. `PLAYS` keeps all four, so such a position keeps
# its exit terms and its resting limit sell above its buy price.
RETIRED_REASON = "retired 2026-10-06"
RETIRED = {PEAD: RETIRED_REASON, SURGE: RETIRED_REASON}
PLAY_CATALYST_ENABLED = "play_catalyst_enabled"
PLAY_MACRO_ENABLED = "play_macro_enabled"
SHELVED = {CATALYST: PLAY_CATALYST_ENABLED, MACRO: PLAY_MACRO_ENABLED}


def play_off(source: Mapping[str, Any] | None, play_id: str | None) -> str | None:
    """Why ``play_id`` takes no new money, or None if it may. ``source`` is a
    context carrying ``config``, or the config itself."""
    if play_id in RETIRED:
        return f"{play_id} is {RETIRED[play_id]}"

    key = SHELVED.get(play_id)
    if key is None:
        return None

    source = source or {}
    config = source.get("config") if isinstance(source.get("config"), Mapping) else source
    if config.get(key) is True:
        return None
    return f"{play_id} is shelved: {key} is not true in config/config.json"


def pullback_enabled(ctx: Mapping[str, Any]) -> bool:
    """Whether DIP runs as §4.3 A's pullback-in-uptrend rather than as §6.2.
    """
    return bool((ctx.get("config") or {}).get(DIP_PULLBACK_ENABLED))


def target_pct(held: Mapping[str, Any], ctx: Mapping[str, Any]) -> tuple[float, str] | None:
    """``(fraction above entry, who owns it)`` for a held position's target.

    The three-way resolution `exits` and `protection` have always shared — the
    play's own target, adoption's floor where the play declares none and the
    position was inherited, nothing at all otherwise — plus §4.3 A's fourth
    case: a `DIP` position taken under the pullback reshape targets
    ``1.5 x stop distance`` rather than a flat 6%.

    **A missing ATR falls back to the play's declared percentage rather than
    to no target at all.** An R-multiple needs an ATR and a position whose
    signal fetch missed has none; dropping the target on that would cancel a
    resting broker-side limit over a data gap, which is a worse failure than
    resting at the play's ordinary 6%. The owner string says which was used,
    so a runlog reader can tell the two apart.
    """
    play = PLAYS.get(held.get("play_id"))
    entry = held.get("entry_price")
    if play is None or entry is None:
        return None

    if play.play_id == DIP and pullback_enabled(ctx):
        distance = stop_distance(
            DIP, _signals(ctx, held.get("symbol")).get("atr"), ctx
        )
        if distance is not None and float(entry) > 0.0:
            multiple = float(
                (ctx.get("config") or {}).get(
                    DIP_PULLBACK_TARGET_STOP_MULTIPLE,
                    DIP_PULLBACK_TARGET_STOP_MULTIPLE_DEFAULT,
                )
            )
            return _clean(multiple * distance / float(entry)), f"{DIP} {multiple:g}R"

    if play.play_id == CORE:
        return core_sleeve.target_pct(ctx), CORE
    if play.target_pct is not None:
        return float(play.target_pct), play.play_id
    if held.get("adopted"):
        return ADOPTED_TARGET_PCT, "adoption"
    return None


def _signals(ctx: Mapping[str, Any], symbol: str) -> Mapping[str, Any]:
    return (ctx.get("signals") or {}).get(symbol) or {}


def _price(ctx: Mapping[str, Any], symbol: str) -> float | None:
    """Last trade, which is what a signal comparison wants.

    Guard costs *orders* at the far side of the spread; these are comparisons
    against moving averages, and costing them at the ask would drift every
    threshold by half a spread.
    """
    last = ((ctx.get("quotes") or {}).get(symbol) or {}).get("last")
    return None if last is None else float(last)


def _clean(value: float) -> float:
    """Round past any precision a 0–1 conviction could carry meaning at.

    §6.0 breaks *exact* ties toward the shorter holding period, and binary
    floating point turns most of those ties into 0.6999999999999995 against
    0.7 — a tiebreak that can never fire is indistinguishable from one nobody
    wrote. Six decimals also matches the precision `sizing.py` rounds share
    quantities to, so the two modules describe the same numbers the same way.
    """
    return round(value, CONVICTION_PRECISION)


def _scaled(value: float, full_scale: float) -> float:
    """A raw magnitude as a 0–1 conviction, capped (§6.1's "capped at 1.0").

    The cap is not decoration. Past the full scale the signal has stopped
    telling you more — a 40% gap below the 20-day mean is not four times the
    opportunity of a 10% one, it is a different kind of event.
    """
    return _clean(max(0.0, min(1.0, value / full_scale)))


SESSION_MINUTES = cadence.SESSION_MINUTES


def session_minutes(ctx: Mapping[str, Any]) -> float:
    """How long today's regular session is — 390, or 210 on a published
    half-day. Falls back to 390 where the context carries no calendar."""
    trading_day = (ctx.get("cycle") or {}).get("trading_day")
    if trading_day is None:
        return SESSION_MINUTES
    try:
        day = date.fromisoformat(str(trading_day))
        return cadence.session_minutes(day, ctx.get("calendar") or {})
    except (TypeError, ValueError):
        return SESSION_MINUTES


def minutes_since_open(ctx: Mapping[str, Any]) -> float | None:
    """Minutes from 09:30 ET on the cycle's trading day to the cycle's `now`.

    The one place this arithmetic lives (P2). Negative before the open and on
    an overnight cycle, whose trading day is tomorrow; unbounded after the
    close — the caller clamps, because what a late or early reading should
    mean is the caller's rule. ``None`` when the context has no clock, which
    is `would_open` / `mc_prefilter` and nothing on the live path.

    `now` is when the cycle started; the volume it is compared with was
    fetched a few minutes later, so an intraday reading is slightly short and
    a time-normalised volume slightly generous. `catalyst_min_minutes` keeps
    that error small relative to the elapsed time.
    """
    cycle = ctx.get("cycle") or {}
    return cadence.minutes_since_open(cycle.get("now"), cycle.get("trading_day"))


def _earnings_veto(
    symbol: str, ctx: Mapping[str, Any], play_id: str, clearance: int
) -> Skipped | None:
    """No entry with a print inside ``clearance`` sessions.

    Deliberately wider than guard's T−2: a play that stops proposing before the
    denial layer has to spends no cycle on an order that was never placeable.
    An unconfirmed date fails the same way it does in guard — a deadline
    promise cannot be kept blind.
    """
    entry = (ctx.get("earnings") or {}).get(symbol) or {}
    sessions = entry.get("sessions_until_report")

    if entry.get("status") != "known" or sessions is None:
        return Skipped(
            symbol,
            play_id,
            EARNINGS_VETO,
            f"no confirmed earnings date for {symbol}; {play_id} needs a clear "
            f"{clearance} sessions and cannot count them blind",
        )

    if int(sessions) <= clearance:
        return Skipped(
            symbol,
            play_id,
            EARNINGS_VETO,
            f"{symbol} reports in {sessions} session(s); {play_id} needs "
            f"{clearance} clear and the system never holds through a print",
        )

    return None


def _events(ctx: Mapping[str, Any], symbol: str) -> list[Mapping[str, Any]]:
    return list((ctx.get("events") or {}).get(symbol) or [])


def _items(event: Mapping[str, Any]) -> set[str]:
    return {str(code).strip() for code in event.get("items") or []}


def _sessions_ago(row: Mapping[str, Any], ctx: Mapping[str, Any], key: str) -> int | None:
    """How many sessions ago this event landed, counted rather than believed.

    CI publishes `sessions_ago` and re-derives it on every run, which is
    correct exactly as long as CI runs. If it stops, the file freezes with its
    last counts and a week-old 8-K keeps reading as one session old — so
    `CATALYST` would go on firing on stale news, which is the precise opposite
    of §14's "EDGAR/FRED pipeline down ⇒ CATALYST and MACRO no-op".

    Both files also carry the absolute date the row is about (`filed_day` for
    an 8-K, `first_seen` for a release), and an absolute date cannot go stale.
    Counting from it makes the published figure advisory and makes a dead
    pipeline degrade the way §14 says it should: the events age out on their
    own, the play stops triggering, and nothing had to notice CI was down.

    The published value is the fallback, for a row that predates the absolute
    field or a context with no calendar. That is the only case where a frozen
    count can still be believed, and it is one this module cannot improve on.
    """
    absolute = row.get(key)
    trading_day = (ctx.get("cycle") or {}).get("trading_day")
    if absolute is not None and trading_day is not None:
        return sessions_between(
            date.fromisoformat(str(absolute)),
            date.fromisoformat(str(trading_day)),
            ctx.get("calendar") or {},
        )

    published = row.get("sessions_ago")
    return None if published is None else int(published)


def _restatement_veto(
    symbol: str, ctx: Mapping[str, Any], play_id: str
) -> Skipped | None:
    """§6.3's hard veto, read at the name rather than inside one play.

    §6.3 files it under CATALYST and §6.5 makes the matching *exit* global. An
    entry on any other play would open a position that exit 1 flattens on the
    next cycle, so the narrower reading proposes trades the system has already
    decided to reverse.

    Lives here rather than in `catalyst.py` because it is never called from
    `_catalyst` itself — only from `entries()` (for every play's proposal) and
    from `_top_ups()`. It is a global veto read at the name, not part of the
    CATALYST trigger.
    """
    for event in _events(ctx, symbol):
        if ITEM_NON_RELIANCE in _items(event):
            return Skipped(
                symbol,
                play_id,
                HARD_VETO_RESTATEMENT,
                f"{symbol} filed an 8-K item {ITEM_NON_RELIANCE}: the company "
                "has said its own financials cannot be relied on, and no price "
                "reaction makes that a trade",
            )
    return None


def _regime_multiplier(ctx: Mapping[str, Any], play_id: str) -> float:
    """§6.0's weight, from `src/regime.py` rather than from a written opinion.

    Replaced the old `_regime_fit`. That function read
    `edge_play_state.regime_fit` — a number the premarket session wrote by
    hand — and returned `None` for a play with no row, which `entries` treated
    as a refusal. The producer had been dead and nothing noticed, because the
    rows it left behind still read as opinions. `src/regime.py`'s docstring
    carries the rest.

    **A non-zero `regime_fit` is no longer read at all.** The manual switch
    survives as the explicit-0.0 case only (`_operator_dark` below), because
    that is the half of it an operator actually uses and the half that cannot
    go stale in a direction that keeps trading.

    1.0 when there is no regime, which is every cycle whose payload predates
    `regime_inputs` — so the change is inert until the routine supplies one.
    """
    multipliers = (ctx.get("regime") or {}).get("play_multipliers") or {}
    value = multipliers.get(play_id)
    return 1.0 if value is None else float(value)


def _regime_reason(ctx: Mapping[str, Any]) -> str:
    """The sentence `regime.evaluate` wrote, for a skip message to quote."""
    return (ctx.get("regime") or {}).get("reason") or "no regime recorded"


def _operator_dark(ctx: Mapping[str, Any], play_id: str) -> str | None:
    """The operator's own kill switch for one play, or None if it is not set.

    An explicit `regime_fit = 0.0` in `edge_play_state` still darks a play, and
    this is now the only thing that column does. Its reason is returned rather
    than the fact, because §6.0's rule has always been that a play which went
    dark without one is indistinguishable from a play that went dark by
    accident.

    An absent row is **not** dark. That reading is what turned a dead premarket
    session into a week of halved convictions; a switch nobody has touched is
    off, not on.

    `dip_entries_enabled: false` is the same switch for DIP, held in
    `config/config.json` so it is a reviewed change with a date in git. It is
    read first: it is the standing decision, and a row in `edge_play_state`
    cannot turn DIP back on over it.
    """
    if play_id == DIP and not dip_entries_enabled(ctx):
        return DIP_ENTRIES_OFF_REASON

    state = (ctx.get("play_state") or {}).get(play_id)
    if state is None:
        return None

    value = state.get("regime_fit")
    if value is None or float(value) != 0.0:
        return None

    return state.get("reason") or "no reason recorded"


def _notional_multiplier(play_id: str, ctx: Mapping[str, Any]) -> float:
    """§6.3's 0.75x while CATALYST is unproven. Every other play sizes full.

    An absent count reads as zero and therefore as unproven. It is the safe
    direction: `reconcile.py` owes this figure, and until it writes one a
    missing field is not twenty closed trades.
    """
    if play_id != CATALYST:
        return 1.0

    state = (ctx.get("play_state") or {}).get(play_id) or {}
    closed = int(state.get("closed_trades") or 0)
    return 1.0 if closed >= CATALYST_PROVEN_TRADES else CATALYST_UNPROVEN_MULTIPLIER
