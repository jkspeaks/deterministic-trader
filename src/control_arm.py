"""control_arm.py — the always-on random control: which name stands in.

An earlier placebo backtest asked, offline, whether a name a play selects does
better than a random name from the same sector on the same day under the same
exits — and could not tell the two apart. This is the same question asked of
live operation, one signal at a time, so the answer accumulates without anyone
re-running a study.

`pick` chooses the stand-in. Everything about pricing it lives in
`services/core/core/shadow.py`, which treats it exactly as it treats the
signal: same fill rule, same exit walk.

**Seeded and stateless.** The draw is a hash of the seed, the trading day,
the signal's symbol and the draw number over the *sorted* eligible list. The
nightly replay re-derives every row inside its window; a draw that depended on
call order, on Python's hash randomisation or on a stored cursor would hand
back a different stand-in on Tuesday than on Monday and the ledger would
quietly resample itself.

**Not conditioned on the outcome.** Eligibility is sector membership and "did
not signal" — nothing about whether bars later turn out to exist. A stand-in
with no bars is written as such; redrawing until one prices would bias the
arm toward names that survived.
"""

from __future__ import annotations

import hashlib
from typing import Any, Collection, Mapping

# The arm's seed. Changing it re-draws every control inside the replay window,
# which is a different experiment: record it in the trial ledger.
SEED = 20261004

# A play's RSI trigger marks a name as "signalling" even when discovery never
# surfaced it — the scanner is capped, so an oversold name can be absent from
# the pool for no reason but the cap. Read off the cached daily RSI.
OVERSOLD_RSI = 30.0


def eligible(
    symbol: str,
    sectors: Mapping[str, Any],
    excluded: Collection[str],
    signals: Mapping[str, Any] | None = None,
    members: Collection[str] | None = None,
) -> list[str]:
    """Same-sector names that did not signal, sorted.

    ``sectors`` is `{symbol: GICS sector}` for the universe; ``excluded`` is
    what the cycle's discovery, plays and book touched
    (`snapshot.signalling`); ``signals`` is the cycle's `{symbol: {"rsi"}}`,
    used to leave out a name at or under `OVERSOLD_RSI` (unknown RSI is
    allowed: absent means the test was not applied, the direction every other
    gate here fails in). ``members`` restricts the draw to the index fence
    when given, so a sector ETF or a cross-check-only name is never drawn.
    """
    sector = sectors.get(symbol)
    if not sector:
        return []
    out = []
    for name, theirs in sectors.items():
        if theirs != sector or name == symbol or name in excluded:
            continue
        if members is not None and name not in members:
            continue
        rsi = ((signals or {}).get(name) or {}).get("rsi")
        try:
            if rsi is not None and float(rsi) <= OVERSOLD_RSI:
                continue
        except (TypeError, ValueError):
            pass
        out.append(name)
    return sorted(out)


def pick(
    symbol: str,
    trading_day: str,
    candidates: list[str],
    *,
    seed: int = SEED,
    draw: int = 0,
) -> str | None:
    """One name from ``candidates`` (already sorted), or None if it is empty."""
    if not candidates:
        return None
    digest = hashlib.sha256(f"{seed}|{trading_day}|{symbol}|{draw}".encode("utf-8")).digest()
    return candidates[int.from_bytes(digest[:8], "big") % len(candidates)]
