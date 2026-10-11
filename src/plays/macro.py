"""§6.4 — regime rotation through the sector sleeve."""

from __future__ import annotations

from typing import Any, Mapping

from .core import (
    ENTRY_CONDITIONS_UNMET,
    MACRO,
    NO_CONVICTION,
    Skipped,
    _price,
    _Proposal,
    _scaled,
    _sessions_ago,
    _signals,
)

# §6.4. A release stays tradeable for two sessions. ``surprise_pct`` is the
# print against consensus as a signed fraction, which is what makes one scale
# work across series measured in percentage points and series measured in jobs:
# CI extracts both mechanically (§7.3) and neither needs a per-series constant.
MACRO_WINDOW_SESSIONS = 2
MACRO_SURPRISE_FULL_SCALE_PCT = 0.25
# PEAD requires price > the 50-day (pead.py), CATALYST a confirmed move
# (catalyst.py), DIP price > the 200-day (dip.py) — MACRO alone traded on the
# surprise sign with no check that the tape agrees. Rotating into a sector ETF
# already below its own 200-day on a single print is a structural bet dressed
# as a tactical one. The ETF is always a member of the candidate pool by the
# time this runs (it is either a macro target this cycle or a held position),
# so its signal is already in `ctx` — this adds no call, the same way DIP's
# 200-day check adds none.
MACRO_TREND_SMA = "sma_200"


def _macro(ctx: Mapping[str, Any]) -> list[_Proposal | Skipped]:
    """§6.4. Regime rotation, expressed through the sector sleeve.

    The one play that does not scan symbols: a release tilts sectors, so the
    candidates come out of `config/macro_map.json` rather than out of §5.3's
    pool. That is also why it needs no per-symbol Robinhood call to propose,
    and why §6.4 chose ETFs — they clear §6.6's overnight spread gate far more
    often than single names.

    §6.4's second trigger form, "a threshold move in a tracked series", arrives
    in the same row shape as a scheduled release and needs no second code path.
    """
    releases = (ctx.get("macro") or {}).get("releases") or []
    mapping = (ctx.get("macro_map") or {}).get("releases") or {}

    best: dict[str, _Proposal] = {}
    skipped: list[Skipped] = []
    trend_failed: set[str] = set()

    for release in releases:
        ago = _sessions_ago(release, ctx, "first_seen")
        if ago is None or int(ago) > MACRO_WINDOW_SESSIONS:
            continue

        series = release.get("series")
        tilt = mapping.get(series)
        if tilt is None:
            # Retiring a series is deleting it from the file, per §6.4.
            continue

        surprise = float(release.get("surprise_pct") or 0.0)
        conviction = _scaled(abs(surprise), MACRO_SURPRISE_FULL_SCALE_PCT)

        if conviction <= 0.0:
            skipped.append(
                Skipped(
                    series,
                    MACRO,
                    NO_CONVICTION,
                    f"{series} printed on consensus; there is no surprise to "
                    "rotate on",
                )
            )
            continue

        direction = "above" if surprise > 0 else "below"
        detail = (
            f"{series} printed {surprise:+.1%} against consensus; §6.4's map "
            f"tilts {direction}"
        )

        for symbol in tilt.get(direction) or []:
            if symbol in trend_failed:
                continue

            price = _price(ctx, symbol)
            sma_200 = _signals(ctx, symbol).get(MACRO_TREND_SMA)
            if price is None or sma_200 is None or price <= float(sma_200):
                trend_failed.add(symbol)
                best.pop(symbol, None)
                skipped.append(
                    Skipped(
                        symbol,
                        MACRO,
                        ENTRY_CONDITIONS_UNMET,
                        f"{series} tilts to {symbol} but it is not holding its "
                        "200-day; a rotation against the sector's own trend is "
                        "not §6.4's play",
                    )
                )
                continue

            held = best.get(symbol)
            # Two releases can agree on a sector — CPI and PCE both favour
            # energy when hot. That is one position sized on the better signal,
            # not one play outranking itself in the record.
            if held is None or conviction > held.conviction:
                best[symbol] = _Proposal(symbol, MACRO, conviction, detail)

    return [*best.values(), *skipped]
