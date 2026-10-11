"""§6.0 — arbitration across everything the four plays proposed, plus
§4.2's top-ups, ranked into one list."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from src import regime as regimes
from src import universe as universes

from .core import (
    CATALYST,
    DIP,
    MACRO,
    NO_CONVICTION,
    OUTRANKED_ON_SYMBOL,
    PEAD,
    PLAYS,
    REGIME_DARK,
    SECTOR_INACTIVE,
    SKIP_REGIME,
    SURGE,
    Candidate,
    EntryResult,
    Skipped,
    _clean,
    _notional_multiplier,
    _operator_dark,
    play_off,
    _Proposal,
    _regime_multiplier,
    _regime_reason,
    _restatement_veto,
    _signals,
    regime_v2_enabled,
)
from .catalyst import _catalyst
from .dip import _dip, sector_etf_for
from .macro import _macro
from . import no_trade
from .pead import _pead
from .sector import _sector
from .surge import _surge
from .top_up import _top_ups

# The per-symbol plays (§6's four minus MACRO, which proposes off the sector
# map rather than a symbol and is called separately below, plus §17's SURGE).
# One tuple, used by both `would_open` and `entries` — previously duplicated as
# two literal tuples that could drift if a play were added to one and not the
# other.
#
# Each is named with its play so `_live_plays` can leave out the ones that are
# retired or shelved (`core.play_off`). `_pead` and `_surge` stay listed: the
# retirement is one rule in one place, not an absence someone has to notice.
_SYMBOL_PLAYS = ((PEAD, _pead), (DIP, _dip), (CATALYST, _catalyst), (SURGE, _surge))


def _live_plays(ctx: Mapping[str, Any]) -> tuple:
    """The per-symbol plays that may propose this cycle."""
    return tuple(play for play_id, play in _SYMBOL_PLAYS if play_off(ctx, play_id) is None)


def _map_plays(ctx: Mapping[str, Any]) -> list:
    """What MACRO and SECTOR propose off their maps. MACRO is asked only
    while it is not shelved; SECTOR has its own switch inside `_sector`."""
    macro = _macro(ctx) if play_off(ctx, MACRO) is None else []
    return [*macro, *_sector(ctx)]


def _crowding_penalty(symbol: str, ctx: Mapping[str, Any]) -> float:
    """§6.0's penalty for a candidate sharing a sector with an existing position.

    Linear in the count, so a second name in a sector is penalised twice. At
    n=3 two positions in one sector already brush §4.4's 50% sector cap, and
    this is the soft gradient in front of that hard limit — the ranking should
    have discouraged the trade before guard has to refuse it outright.

    Measured against the book as it stands, per §6.0's wording. Candidates
    ranked above this one are not positions yet.
    """
    sectors = ctx.get("sectors") or {}
    sector = sectors.get(symbol)
    if sector is None:
        # Sizing refuses a sectorless candidate outright and guard fails it
        # closed, so there is nothing here for a penalty to improve.
        return 0.0

    held = sum(
        1
        for position in ctx.get("positions") or []
        if sectors.get(position.get("symbol")) == sector
    )
    return _clean(held * float(ctx["config"]["crowding_penalty"]))


# The two ways §6.0's crowding term has ever been read, named so the shadow
# ledger can run the retired one against the shipped one without a second
# copy of `_arbitrate`.
#
# `tiebreak` is what ships and what `config/config.json` omits into: the
# penalty is a `_rank_key` term only and cannot move or remove a candidate.
# `score` is the older behavior — the penalty subtracted from the
# score itself — kept reachable because "crowding as a tiebreak" was a
# judgment call made without evidence, and §4.2 P1 exists to stop those.
CROWDING_TIEBREAK = "tiebreak"
CROWDING_SCORE = "score"


def _crowding_mode(ctx: Mapping[str, Any]) -> str:
    """Which reading is in force. Anything unrecognised reads as `tiebreak`,
    which is the shipped behavior — a typo in a config override must not
    silently reinstate a rule that was removed for erasing candidates."""
    mode = (ctx.get("config") or {}).get("crowding_mode")
    return CROWDING_SCORE if mode == CROWDING_SCORE else CROWDING_TIEBREAK


def _arbitrate(
    proposals: list[_Proposal], ctx: Mapping[str, Any]
) -> tuple[list[Candidate], list[Skipped]]:
    """§6.0, applied to everything the four plays proposed.

    ``score = conviction × regime_multiplier``, and the crowding penalty is no
    longer one of its terms. §4.4's sector cap is the real constraint and it
    is enforced twice already, in `sizing._Budget` and again in
    `guard._concentration_denials`; this is the soft gradient in front of it,
    and a gradient that can zero a score is a cap wearing a gradient's name.

    It survives as a **tiebreak weight**, applied in `_rank_key` after the
    score is settled: between two candidates the plays like equally, the one
    whose sector is not already carrying the bet goes first. `sizing._rank`
    sorts on the same term for the same reason — see that function.

    The fourth §6.0 term, ``whole_share_bonus``, is still sizing's: it depends
    on the notional a rung produces, which nothing here can know.
    """
    candidates: list[Candidate] = []
    skipped: list[Skipped] = []

    for proposal in proposals:
        # The operator's manual switch first, because its reason is the more
        # specific one — "I turned PEAD off on Tuesday" beats "the tape is
        # risk-off" for a reader chasing why a name was not bought.
        dark = _operator_dark(ctx, proposal.play_id)
        if dark is not None:
            skipped.append(
                Skipped(
                    proposal.symbol,
                    proposal.play_id,
                    REGIME_DARK,
                    f"{proposal.play_id} is disabled today: {dark}",
                )
            )
            continue

        regime = _regime_multiplier(ctx, proposal.play_id)
        if regime <= 0.0:
            skipped.append(
                Skipped(
                    proposal.symbol,
                    proposal.play_id,
                    REGIME_DARK,
                    f"{proposal.play_id} is dark on today's regime: "
                    f"{_regime_reason(ctx)}",
                )
            )
            continue

        # SURGE's whole purpose is to ignore sector concentration, so the soft
        # crowding gradient in front of §4.4's sector cap does not apply to it.
        crowding = (
            0.0
            if proposal.play_id == SURGE
            else _crowding_penalty(proposal.symbol, ctx)
        )
        score = _clean(proposal.conviction * regime)
        if _crowding_mode(ctx) == CROWDING_SCORE:
            # The retired reading, reachable only by config override. It can
            # take a score to zero, which is why the `score <= 0.0` branch
            # below now has two ways to fire — and why the runlog needs to
            # say which one did.
            score = _clean(score - crowding)

        if score <= 0.0:
            # A play that proposed at zero conviction. Nothing to do with the
            # regime or the sector — the play itself had no opinion, and the
            # code says so rather than blaming a penalty that no longer
            # subtracts. Under the retired `score` reading it *can* be the
            # penalty, and then the message says so: a shadow variant whose
            # candidates all vanish has to be readable as to why.
            erased = crowding > 0.0 and _clean(proposal.conviction * regime) > 0.0
            skipped.append(
                Skipped(
                    proposal.symbol,
                    proposal.play_id,
                    NO_CONVICTION,
                    f"{proposal.play_id} proposed {proposal.symbol} at "
                    f"{score:.2f} conviction; there is nothing to rank"
                    + (
                        f" (the {crowding:.2f} crowding penalty erased a "
                        f"{_clean(proposal.conviction * regime):.2f} score "
                        "under crowding_mode=score)"
                        if erased
                        else ""
                    ),
                )
            )
            continue

        play = PLAYS[proposal.play_id]
        candidates.append(
            Candidate(
                symbol=proposal.symbol,
                play_id=proposal.play_id,
                conviction=score,
                conviction_raw=proposal.conviction,
                regime_fit=regime,
                crowding_penalty=crowding,
                expected_holding_days=play.holding_days,
                notional_multiplier=_notional_multiplier(proposal.play_id, ctx),
                detail=proposal.detail,
                full_deployment=proposal.play_id == SURGE,
            )
        )

    return candidates, skipped


def _rank_key(cand: Candidate) -> tuple[float, float, int, str]:
    """§6.0's order, and deliberately the same key `sizing._rank` sorts on.

    Plays and sizing agreeing by construction is what keeps the runlog's stated
    order and the order capital is actually committed in from drifting apart.

    ``crowding_penalty`` joined the key, ahead of the holding period and behind
    the score — this is the whole of what "apply crowding after ranking" means
    in practice. A candidate in an untouched sector is preferred to one in a
    sector already held, but only among candidates the plays scored the same;
    it can no longer move a name past a better one, and it can no longer remove
    one.
    """
    return (
        -cand.conviction,
        cand.crowding_penalty,
        cand.expected_holding_days,
        cand.symbol,
    )


def _best_per_symbol(
    candidates: list[Candidate],
) -> tuple[list[Candidate], list[Skipped]]:
    """One play per symbol. Two plays liking a name is one position, not two.

    Sizing would treat a second candidate on a held symbol as a top-up and
    guard would deny it as a duplicate open order, so the arbitration belongs
    here where the reason can be recorded.
    """
    best: dict[str, Candidate] = {}
    for cand in candidates:
        held = best.get(cand.symbol)
        if held is None or _rank_key(cand) < _rank_key(held):
            best[cand.symbol] = cand

    kept = list(best.values())
    skipped = [
        Skipped(
            c.symbol,
            c.play_id,
            OUTRANKED_ON_SYMBOL,
            f"{best[c.symbol].play_id} scores {best[c.symbol].conviction:.2f} on "
            f"{c.symbol} against this play's {c.conviction:.2f}; one name is one "
            "position",
        )
        for c in candidates
        if best[c.symbol] is not c
    ]
    return kept, skipped


def would_open(symbol: str, ctx: Mapping[str, Any]) -> str | None:
    """Which play's own trigger fires on ``symbol`` today, or None.

    `reconcile.py` asks this when it adopts a position it did not open, so that
    an inherited name whose thesis a play actually recognises is managed on that
    play's terms instead of on `ADOPTED`'s generic ones. It answers *what this
    name is*, never *whether to buy it* — nothing here places anything.

    **Deliberately the raw triggers rather than `entries`.** §6.0's arbitration
    asks whether to commit capital now, and its two terms both give the wrong
    answer to this question: `regime_fit` would leave a name unbound because the
    agent switched a play off this morning, and `crowding_penalty` would punish
    a holding for sharing a sector with itself. A play the agent has gone dark
    on still describes the position in front of it. Reconcile also runs before
    the premarket cycle has written a regime on most days, so arbitrating here
    would bind almost everything to `ADOPTED` for reasons unrelated to the name.

    Only a full proposal counts. A play that triggered and then declined — a
    dip below its 200-day, a beat the tape ignored — has looked at the name and
    refused to endorse it, and that is not a thesis to hand a position to.
    """
    proposals: list[_Proposal] = []

    # A retired or shelved play does not claim an inherited name either: the
    # system should not manage a new position on the terms of a play it has
    # stopped running. Such a name is `ADOPTED`.
    for play in _live_plays(ctx):
        outcome = play(symbol, ctx)
        if isinstance(outcome, _Proposal):
            proposals.append(outcome)

    for outcome in _map_plays(ctx):
        # MACRO and SECTOR propose off a map rather than off a symbol, so each
        # is asked once and filtered — the ETF sleeve is exactly where an
        # inherited holding can legitimately be a rotation. SECTOR returns
        # nothing at all while `play_sector_enabled` is off, so an inherited
        # ETF binds to MACRO or ADOPTED exactly as it does today.
        if isinstance(outcome, _Proposal) and outcome.symbol == symbol:
            proposals.append(outcome)

    if not proposals:
        return None

    # §6.0's order, minus the terms that do not apply: strongest conviction,
    # then the shorter holding period, then the play — `_rank_key`'s tiebreak
    # with its crowding term dropped, since a holding cannot be penalised for
    # sharing a sector with itself (see this function's docstring).
    best = min(
        proposals,
        key=lambda p: (-p.conviction, PLAYS[p.play_id].holding_days, p.play_id),
    )
    return best.play_id


def entries(ctx: Mapping[str, Any]) -> EntryResult:
    """Every play's proposal for this cycle, arbitrated by §6.0 and ranked."""
    proposals: list[_Proposal] = []
    skipped: list[Skipped] = []

    for symbol in ctx.get("candidates") or []:
        for play in _live_plays(ctx):
            outcome = play(symbol, ctx)
            if outcome is None:
                continue

            # A restatement outranks whatever else the play had to say about
            # the name, including why it was otherwise refused: it is the fact
            # a reader chasing this symbol needs first.
            veto = _restatement_veto(symbol, ctx, outcome.play_id)
            if veto is not None:
                skipped.append(veto)
            elif isinstance(outcome, _Proposal):
                proposals.append(outcome)
            else:
                skipped.append(outcome)

    # The two plays that propose off a map rather than off `candidates`: MACRO
    # off `config/macro_map.json`, SECTOR (§4.3 C, off by default) off
    # `config/etfs.json`. Both are asked once per cycle rather than per symbol,
    # and both can name an ETF the other one named — `_best_per_symbol` settles
    # that below, the same way it settles two per-symbol plays liking a name.
    for outcome in _map_plays(ctx):
        if isinstance(outcome, _Proposal):
            proposals.append(outcome)
        else:
            skipped.append(outcome)

    candidates, arbitration_skips = _arbitrate(proposals, ctx)

    # The day's posture over what the plays proposed, before the top-ups join.
    # A top-up adds to a winner already held and is not a new position, so it
    # is ranked below exactly as it was. Inert unless `regime_v2_enabled`.
    candidates, posture_skips = _posture_gate(candidates, ctx)
    arbitration_skips = arbitration_skips + posture_skips

    # Top-ups join the same ranked list rather than running as a second pass, so
    # one cycle's capital is allocated by one comparison. A fresh signal at 0.70
    # should outrank adding to a name drifting 3% above its 50-day, and it does
    # — but only if the two are ranked against each other. `_best_per_symbol`
    # then settles the case where a play triggers on a name that also qualifies
    # for a top-up: that is one position, sized once.
    top_ups, top_up_skips = _top_ups(ctx)
    candidates, duplicate_skips = _best_per_symbol(candidates + top_ups)

    # After the per-symbol merge so it sees every buy this cycle could make:
    # a new name, a top-up, and a play re-triggering on a name already held
    # (`ctx["candidates"]` always carries the book). Discovery already kept
    # inactive-sector names out of the pool; this is what stops new money
    # reaching one the book still holds. Exits never pass here.
    candidates, rotation_skips = _rotation_suppressed(candidates, ctx)

    # §4.3 F, last and off by default. Last because F is a suppression of what
    # the plays decided, not an input to it: running it here means the runlog
    # still records the full ranked list the shipped rules produced *and* the
    # reason each name was suppressed, which is exactly what "count suppressed
    # trades and their hypothetical P&L" needs to be answerable.
    candidates, no_trade_skips = _no_trade_suppressed(candidates, ctx)

    return EntryResult(
        candidates=sorted(candidates, key=_rank_key),
        skipped=(
            skipped + arbitration_skips + top_up_skips + duplicate_skips
            + rotation_skips + no_trade_skips
        ),
    )


def _posture_gate(
    candidates: list[Candidate], ctx: Mapping[str, Any]
) -> tuple[list[Candidate], list[Skipped]]:
    """`regime.Regime.posture` applied to new entries (audit §5.2).

    * NO_NEW_POSITIONS / HOLD_CASH — every play's candidate is a `SKIP_REGIME`.
    * HIGH_CONVICTION_ONLY — DIP needs its sector's relative strength vs SPY
      in the upper half of the eleven (the sector being above its own 200-day
      is `dip._dip`'s leg, already applied); unmeasurable strength is
      `SKIP_RS_UNAVAILABLE`, not a pass. CATALYST, PEAD and MACRO pass.
    * REDUCED_SIZE and HIGH_CONVICTION_ONLY multiply the size.
    * SURGE runs only in TRADE_NORMALLY: it commits the whole settled balance
      with the caps and the notional multiplier bypassed, so "reduced size"
      cannot be expressed on it.

    S3: with `dip_regime_table_enabled` as well, a DIP candidate is judged by
    the day's row of `dip_regime_table` (`regime.dip_table_gate`) instead of
    the three DIP rules above: whether DIP may enter in this posture at all,
    its RSI ceiling, the relative-strength test, and its size multiplier,
    clamped to [0, 1]. As shipped each row is the rule above. Every other
    play is judged as before.

    Unchanged when the flag is off or there is no regime — an absent regime is
    "no opinion", the same reading `_regime_multiplier` gives it. Exits never
    pass here.
    """
    regime = ctx.get("regime") or {}
    posture = regime.get("posture")
    if not regime_v2_enabled(ctx) or posture is None:
        return candidates, []

    seen = regimes.posture_seen(regime)
    multiplier = regimes.entry_size_multiplier(regime)
    no_entry = regimes.no_entry_reason(regime)

    table = regimes.dip_table_enabled(ctx.get("config"))

    kept: list[Candidate] = []
    skipped: list[Skipped] = []
    for cand in candidates:
        if table and cand.play_id == DIP:
            scale, refusal = regimes.dip_table_gate(
                regime,
                sector_etf_for(cand.symbol, ctx),
                ctx.get("config"),
                rsi=_signals(ctx, cand.symbol).get("rsi"),
                symbol=cand.symbol,
            )
            if refusal is not None:
                skipped.append(Skipped(cand.symbol, cand.play_id, *refusal))
                continue
            if scale != 1.0:
                cand = replace(
                    cand, notional_multiplier=_clean(cand.notional_multiplier * scale)
                )
            kept.append(cand)
            continue

        if no_entry is not None:
            skipped.append(
                Skipped(cand.symbol, cand.play_id, SKIP_REGIME,
                        f"{no_entry}: no new position in {cand.symbol}")
            )
            continue

        if cand.play_id == SURGE and posture != regimes.TRADE_NORMALLY:
            skipped.append(
                Skipped(cand.symbol, cand.play_id, SKIP_REGIME,
                        f"{posture} ({seen}): SURGE commits the whole balance "
                        "and runs only when the posture is TRADE_NORMALLY")
            )
            continue

        if cand.play_id == DIP:
            weak = regimes.sector_strength_verdict(
                regime, sector_etf_for(cand.symbol, ctx)
            )
            if weak is not None:
                skipped.append(Skipped(cand.symbol, cand.play_id, *weak))
                continue

        if multiplier != 1.0:
            cand = replace(
                cand,
                notional_multiplier=_clean(cand.notional_multiplier * multiplier),
            )
        kept.append(cand)

    return kept, skipped


def _rotation_suppressed(
    candidates: list[Candidate], ctx: Mapping[str, Any]
) -> tuple[list[Candidate], list[Skipped]]:
    """The operator's sector rotation over the ranked buy list.

    Unchanged when the rotation is inert (`[]`, or every name unmatched), for
    an unknown sector, and for Broad Market — `universe.SectorRotation`'s own
    fail-open rules, the same ones discovery applied.
    """
    rotation = universes.sector_rotation(
        (ctx.get("control") or {}).get("active_sectors"), ctx.get("etfs") or {},
        ctx.get("rotation_codes"), ctx.get("sub_industries"),
    )
    if not rotation.filtering:
        return candidates, []
    sectors = ctx.get("sectors") or {}
    kept: list[Candidate] = []
    skipped: list[Skipped] = []
    for cand in candidates:
        sector = rotation.inactive(cand.symbol, sectors)
        if sector is None:
            kept.append(cand)
        else:
            skipped.append(
                Skipped(cand.symbol, cand.play_id, SECTOR_INACTIVE,
                        f"{sector} is outside the active sector rotation; "
                        f"no new money into {cand.symbol}")
            )
    return kept, skipped


def _no_trade_suppressed(
    candidates: list[Candidate], ctx: Mapping[str, Any]
) -> tuple[list[Candidate], list[Skipped]]:
    """§4.3 F applied to the ranked list — `([], every candidate skipped)` when
    an account-wide leg fired, otherwise the wide-spread names dropped.

    Returns the list unchanged when F is off, which is every cycle the live
    account runs today (`no_trade.enabled`).
    """
    blocking = no_trade.blockers(ctx)
    if blocking:
        # The first leg names the code (one skip carries one code), every leg
        # names the reason — two conditions firing at once is a fact about the
        # day that a truncated message would hide.
        code = blocking[0][0]
        reason = "; ".join(text for _, text in blocking)
        return [], [
            Skipped(cand.symbol, cand.play_id, code, reason) for cand in candidates
        ]

    if not no_trade.enabled(ctx):
        return candidates, []

    kept: list[Candidate] = []
    skipped: list[Skipped] = []
    for cand in candidates:
        reason = no_trade.spread_reason(cand.symbol, ctx)
        if reason is None:
            kept.append(cand)
        else:
            skipped.append(
                Skipped(cand.symbol, cand.play_id, no_trade.NO_TRADE_SPREAD, reason)
            )
    return kept, skipped
