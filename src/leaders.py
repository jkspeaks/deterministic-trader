"""Leaders monthly — the simple picker the S5 study tested.

Once a month, from index members whose close is above their own 200-session
average, rank by residual 12-1 momentum and take the top names. No model is
asked anything and no vendor is paid: the only input is daily closes.

This module is the rule and nothing else. It places nothing, sizes nothing and
is not read by the entry path: `ops/leaders_shadow.py` runs it once a month and
records what it would have picked beside a random same-sector control, so the
rule has a live record before anyone decides whether to trade it. Whether to
trade it is the operator's decision (S6).

If it is ever traded, the standing rule applies to it as to every position:
one resting limit SELL above the buy price (`TARGET_PCT`, the value S5
replayed) and no resting stop of any kind. There is no stop constant here
because there is no stop.

Every series is a list of closes on one shared calendar, oldest first, with
None where the name did not trade. The last element is the as-of close; a
decision for a session reads the close before it.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, Mapping, Sequence

LOOKBACK = 252          # sessions back the momentum window starts
SKIP = 21               # the most recent month is left out (the "-1" of 12-1)
MIN_OBSERVATIONS = 200  # of the 231 daily returns in the window
TREND_SESSIONS = 200
TOP_N = 7               # the account's slot count at its present size
TARGET_PCT = 0.25       # the resting limit sell S5 replayed, above the buy price

ABSENT_NOT_MEMBER = "not_a_member"
ABSENT_NO_SECTOR = "no_sector_series"
ABSENT_NO_TREND = "no_200_session_average"
ABSENT_BELOW_TREND = "below_200_session_average"
ABSENT_NO_SCORE = "no_momentum_value"


def _ok(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _returns(closes: Sequence[float | None]) -> list[float | None]:
    out: list[float | None] = [None]
    for prior, last in zip(closes, closes[1:]):
        out.append(last / prior - 1.0 if _ok(prior) and _ok(last) and prior > 0.0 else None)
    return out


def _solve3(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting on a 3x3 system."""
    m = [row[:] + [rhs] for row, rhs in zip(a, b)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-18:
            return None
        m[col], m[pivot] = m[pivot], m[col]
        for row in range(col + 1, 3):
            factor = m[row][col] / m[col][col]
            for k in range(col, 4):
                m[row][k] -= factor * m[col][k]
    out = [0.0, 0.0, 0.0]
    for row in (2, 1, 0):
        out[row] = (m[row][3] - sum(m[row][k] * out[k] for k in range(row + 1, 3))) / m[row][row]
    return out


def residual_momentum(closes: Sequence[float | None], market: Sequence[float | None],
                      sector: Sequence[float | None]) -> float | None:
    """The name's 12-1 return with the market and its sector taken out.

    The daily returns ending on closes ``i - 251 .. i - 21`` (``i`` the last
    row) are regressed on the market's and the sector's; the score is the sum
    of what is left, intercept included, over its standard deviation times
    root n. None with fewer than `MIN_OBSERVATIONS` usable days.
    """
    i = len(closes) - 1
    lo, hi = i - (LOOKBACK - 1), i - SKIP + 1
    if lo < 1 or len(market) != len(closes) or len(sector) != len(closes):
        return None
    y_all, m_all, s_all = _returns(closes), _returns(market), _returns(sector)
    rows = [(y_all[k], m_all[k], s_all[k]) for k in range(lo, hi)
            if _ok(y_all[k]) and _ok(m_all[k]) and _ok(s_all[k])]
    n = len(rows)
    if n < MIN_OBSERVATIONS:
        return None
    sums = [[0.0] * 3 for _ in range(3)]
    rhs = [0.0] * 3
    for y, m, s in rows:
        x = (1.0, m, s)
        for a in range(3):
            rhs[a] += x[a] * y
            for b in range(3):
                sums[a][b] += x[a] * x[b]
    beta = _solve3(sums, rhs)
    if beta is None:
        return None
    eps = [y - beta[0] - beta[1] * m - beta[2] * s for y, m, s in rows]
    mean = sum(eps) / n
    noise = math.sqrt(sum((e - mean) ** 2 for e in eps) / (n - 1))
    # A name the two series explain exactly leaves only rounding error behind;
    # dividing by it would rank garbage first.
    if noise <= 1e-12:
        return None
    return (sum(eps) + n * beta[0]) / (noise * math.sqrt(n))


def above_trend(closes: Sequence[float | None]) -> bool | None:
    """Whether the last close is above the mean of the last 200. None when any
    of those 200 is missing: an average over fewer is not the 200-session one."""
    window = closes[-TREND_SESSIONS:]
    if len(window) < TREND_SESSIONS or not all(_ok(c) for c in window):
        return None
    return window[-1] > sum(window) / TREND_SESSIONS


def rank(
    closes: Mapping[str, Sequence[float | None]],
    market: Sequence[float | None],
    sector_closes: Mapping[str, Sequence[float | None]],
    sector_of: Mapping[str, str],
    *,
    members: Sequence[str] | None = None,
    top_n: int = TOP_N,
) -> dict:
    """This month's leaders.

    ``closes`` is every candidate's series; ``sector_closes`` is one series a
    sector (its ETF); ``sector_of`` names each symbol's sector. ``members``
    narrows the candidates to the index as it stands; None takes them all.

    Returns ``{"picks": [...], "eligible": [...], "absent": {reason: count}}``.
    Both lists are highest score first, each row ``{symbol, sector, score}``.
    A name left out is counted under the first reason that applies; nothing
    is imputed for it.
    """
    wanted = set(members) if members is not None else None
    absent: dict[str, int] = {}
    scored = []

    def drop(reason: str) -> None:
        absent[reason] = absent.get(reason, 0) + 1

    for symbol in sorted(closes):
        if wanted is not None and symbol not in wanted:
            drop(ABSENT_NOT_MEMBER)
            continue
        sector = sector_of.get(symbol)
        if sector is None or sector not in sector_closes:
            drop(ABSENT_NO_SECTOR)
            continue
        trend = above_trend(closes[symbol])
        if trend is None:
            drop(ABSENT_NO_TREND)
            continue
        if not trend:
            drop(ABSENT_BELOW_TREND)
            continue
        score = residual_momentum(closes[symbol], market, sector_closes[sector])
        if score is None:
            drop(ABSENT_NO_SCORE)
            continue
        scored.append({"symbol": symbol, "sector": sector, "score": score})
    scored.sort(key=lambda row: (-row["score"], row["symbol"]))
    return {"picks": scored[:max(0, int(top_n))], "eligible": scored, "absent": dict(sorted(absent.items()))}


def _draw(pool: Sequence[str], *parts: Any) -> str | None:
    if not pool:
        return None
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).digest()
    return sorted(pool)[int.from_bytes(digest[:8], "big") % len(pool)]


def control(ranked: Mapping[str, Any], period: str, seed: int = 20261005) -> list[dict]:
    """One seeded random stand-in per pick: an eligible name of the same
    sector that is neither a pick nor already chosen. The same ``period`` and
    ``seed`` always give the same names. A pick with no such name gets none.
    """
    picked = {row["symbol"] for row in ranked["picks"]}
    by_symbol = {row["symbol"]: row for row in ranked["eligible"]}
    chosen: list[dict] = []
    for row in ranked["picks"]:
        taken = {c["symbol"] for c in chosen}
        pool = [s for s, other in by_symbol.items()
                if other["sector"] == row["sector"] and s not in picked and s not in taken]
        name = _draw(pool, seed, period, row["symbol"])
        if name is not None:
            chosen.append({"symbol": name, "sector": row["sector"], "for": row["symbol"]})
    return chosen
