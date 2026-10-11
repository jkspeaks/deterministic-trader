"""§4.2 — idle cash offered to positions already working, never to a new slot."""

from __future__ import annotations

from typing import Any, Mapping

from .core import (
    CORE,
    MANUAL,
    PLAYS,
    REGIME_DARK,
    TOP_UP_UNMANAGED,
    UNCHOSEN,
    Candidate,
    Skipped,
    _clean,
    _earnings_veto,
    _notional_multiplier,
    _operator_dark,
    play_off,
    _price,
    _Proposal,
    _regime_multiplier,
    _regime_reason,
    _restatement_veto,
    _scaled,
    _signals,
)

# Topping up a name already held (operator ruling). §4.2's idle cash is deployed
# into positions that are working rather than into new slots, and never by
# selling something else — `open_slots` is 0 at `n_target` 6 against 10 held, so
# without this the account cannot commit a cent no matter what it sees.
#
# The enforcement for this was already built and had never been reachable:
# `sizing._Budget.has_slot_for` and `guard._concentration_denials` both exempt a
# symbol already held from the slot limit. Only the *proposal* was missing.
#
# Two gates, and the second is the one that matters. Above the 50-day is "this
# name is working". **Above its own entry price is "do not average down"** —
# adding to a loser is the failure mode this whole idea invites, and it is the
# opposite of the anti-churn rule that motivated it: that rule says do not sell a
# loser to chase a winner, not buy more of the loser. Conviction is the distance
# above the 50-day, full at 10%, matching the other magnitude scales here.
TOP_UP_TREND_FULL_SCALE = 0.10
# The same clearance `DIP` asks for. A top-up is an entry, and §6's rule that the
# system never holds through a print applies to the shares being added.
TOP_UP_EARNINGS_CLEARANCE_SESSIONS = 3


def _top_up(held: Mapping[str, Any], ctx: Mapping[str, Any]) -> _Proposal | None:
    """More of a position that is already working, or nothing.

    Silent when it declines, like a trigger and unlike a play that triggered and
    then refused: this runs over every held name on every entry cycle, and a
    note per name that did not qualify is the runlog nobody reads.
    """
    symbol = held.get("symbol")
    # The sleeve grows by its own rule (`src/core_sleeve.py`), against its own
    # cap. A top-up here would add to it through a play slot's caps instead.
    if held.get("play_id") == CORE:
        return None
    price = _price(ctx, symbol)
    entry = held.get("entry_price")
    sma_50 = _signals(ctx, symbol).get("sma_50")

    if price is None or entry is None or sma_50 is None:
        # Fails closed on every missing input. Adding capital on an incomplete
        # picture is the one direction that cannot be undone cheaply.
        return None

    if price <= float(entry):
        return None

    if price <= float(sma_50):
        return None

    trend = (price - float(sma_50)) / float(sma_50)
    conviction = _scaled(trend, TOP_UP_TREND_FULL_SCALE)
    if conviction <= 0.0:
        return None

    return _Proposal(
        symbol,
        held.get("play_id"),
        conviction,
        f"already held and working: {price:.2f} is {trend:.1%} above the 50-day "
        f"and above a {float(entry):.2f} entry",
    )


def _top_ups(ctx: Mapping[str, Any]) -> tuple[list[Candidate], list[Skipped]]:
    """§4.2's idle cash, offered to the book rather than to a new slot.

    These become `Candidate`s directly rather than going through `_arbitrate`,
    because two of §6.0's terms ask a question about a name the account does not
    own yet and give the wrong answer about one it does:

    * **`regime_fit` is not `ADOPTED`'s to fail.** An inherited position is
      bound to a play the agent does not control, seeded at 0.0 by
      `edge_play_state_apply` — every top-up on the inherited book would read as
      a dark regime, which is exactly the sleeve this ruling exists to deploy
      into. The four plays are still gated: adding to a `DIP` the agent has
      switched off today is a new commitment against its stated judgment.
    * **`crowding_penalty` would be charged for the position itself.** It counts
      names held in the candidate's sector, and a held name is in its own — so a
      top-up is penalised for existing. §4.4's sector cap is the real constraint
      on adding to a sector, and it is enforced twice already, in `sizing._Budget`
      and again in `guard._concentration_denials`.
    """
    candidates: list[Candidate] = []
    skipped: list[Skipped] = []

    for held in ctx.get("positions") or []:
        proposal = _top_up(held, ctx)
        if proposal is None:
            continue

        symbol = proposal.symbol
        play = PLAYS.get(proposal.play_id)
        if play is None:
            # A MANUAL position has no exit terms at all, so adding to it buys
            # shares the system has no rule for selling. `reconcile.py` binds a
            # play to everything it adopts now, so this is a legacy book row.
            skipped.append(
                Skipped(
                    symbol,
                    proposal.play_id or MANUAL,
                    TOP_UP_UNMANAGED,
                    f"{symbol} is held under {proposal.play_id or MANUAL}, which "
                    "has no exit terms; the system will not add to a position it "
                    "has no rule for leaving",
                )
            )
            continue

        veto = _restatement_veto(symbol, ctx, play.play_id) or _earnings_veto(
            symbol, ctx, play.play_id, TOP_UP_EARNINGS_CLEARANCE_SESSIONS
        )
        if veto is not None:
            skipped.append(veto)
            continue

        # The same gate `entries._arbitrate` applies, and deliberately not a
        # second reading of it: a top-up is an entry, and a play the tape or
        # the operator has darked must not be added to through a side door.
        # `UNCHOSEN` (MANUAL/ADOPTED) is exempt because neither is a play the
        # regime has an opinion about — they are terms for a position the
        # system inherited, not a thesis it chose.
        regime = 1.0
        if play.play_id not in UNCHOSEN:
            # A retired or shelved play first: it takes no new money, and a
            # top-up is new money.
            dark = play_off(ctx, play.play_id) or _operator_dark(ctx, play.play_id)
            if dark is not None:
                skipped.append(
                    Skipped(symbol, play.play_id, REGIME_DARK,
                            f"{play.play_id} is disabled today ({dark}), so "
                            f"{symbol} is held rather than added to")
                )
                continue

            regime = _regime_multiplier(ctx, play.play_id)
            if regime <= 0.0:
                skipped.append(
                    Skipped(symbol, play.play_id, REGIME_DARK,
                            f"{play.play_id} is dark on today's regime "
                            f"({_regime_reason(ctx)}), so {symbol} is held "
                            "rather than added to")
                )
                continue

        candidates.append(
            Candidate(
                symbol=symbol,
                play_id=play.play_id,
                conviction=_clean(proposal.conviction * regime),
                conviction_raw=proposal.conviction,
                regime_fit=regime,
                crowding_penalty=0.0,
                expected_holding_days=play.holding_days,
                notional_multiplier=_notional_multiplier(play.play_id, ctx),
                detail=proposal.detail,
            )
        )

    return candidates, skipped
