"""§6.2 — oversold inside an uptrend, in two shapes.

The shipped shape is §6.2's: RSI(14) <= 30, price holding within
`dip_trend_band_pct` of the 200-day, conviction from depth below the 20-day.

§4.3's whole premise is that every play in this system is an untested prior, so A
does not replace §6.2 on anybody's judgment: `play_dip_pullback_enabled` ships
false, the shadow ledger runs it as a variant against the shipped rule, and a
config flag flips it live once `edge_shadow_trades` has something to say. Until
then this file behaves exactly as it did — `_pullback` is unreachable and every DIP
golden is byte-identical.

**A is a tighter funnel, not a looser one.** §6.2 asks two questions (oversold,
above the 200-day); A asks six — RSI <= 35 but *shallow* against the 50-day
(-5%..0%, so a name that has already broken down does not qualify), above the
200-day band, a sector the regime is not refusing, ADV >= $20M and a spread
inside 0.2%. The extra four are all liquidity and structure: §6.2 was written
before the account had ever had a fill go wrong, and three of A's four new
gates exist because it had.

**Why A's thresholds are the one place play shape lives in config.** §17 makes
a play's shape the play's own property and `docs/TUNING.md` §4 lists the
constants that follow from that. A is an explicit exception, recorded there:
the owner's goal is a UI that tunes a strategy without a deploy, and A is the
first strategy shaped for it. The exception is scoped to this play's `_11_play_dip`
group and buys nothing anywhere else — `sector.py`, three sentences younger,
keeps its numbers in code.
"""

from __future__ import annotations

from typing import Any, Mapping

from src import regime as regimes

from .core import (
    DIP,
    ENTRY_CONDITIONS_UNMET,
    NO_CONVICTION,
    SKIP_SECTOR_BELOW_200D,
    Skipped,
    _earnings_veto,
    _price,
    _Proposal,
    _scaled,
    _signals,
    pullback_enabled,
    regime_v2_enabled,
)

# §6.2. RSI(14) <= 30 triggers; price must hold within `dip_trend_band_pct` of
# the 200-day; earnings must be more than three sessions out. Conviction is
# depth below the 20-day mean, full at 10% under.
DIP_RSI_TRIGGER = 30.0

# The 200-day gate's tolerance, config. A strict `price > SMA200` rejected
# every ordinary 2-3% pullback as a "falling knife" — 9 of 9 DIP survivors — so
# the funnel starved on a soft tape. Read off `ctx["config"]` where there is
# one; this default is the value the shipped file carries, so the
# `would_open`/`mc_prefilter` path (no config in its ctx) gates identically to
# `decide`. §4.3 A's "above SMA200, band -3% allowed" is this same number and
# this same gate, which is why A adds no key for it.
DIP_TREND_BAND_PCT = 0.03
# Read by `exits._signal_exit` (§6.5 exit 7) — the recovery threshold that
# closes a DIP position, not a trigger.
DIP_RSI_EXIT = 50.0
DIP_EARNINGS_CLEARANCE_SESSIONS = 3
DIP_DEPTH_FULL_SCALE = 0.10
# Depth alone let an RSI-18 name and an RSI-30 name score identically
# whenever their 20-day gaps happened to match, though RSI 18 is the stronger
# oversold read. 15 points below the trigger (RSI 15) is full extremity —
# half the distance from the trigger down to the indicator's own floor. This
# only ever raises conviction above what depth alone gives (`max`, not a
# blend): a name with genuine depth already prices correctly, and the
# no-depth-no-conviction refusal below stays a hard floor rather than
# something a low RSI alone can buy its way past.
DIP_RSI_EXTREMITY_FULL_SCALE = 15.0

# --- §4.3 A's knobs, and the defaults that are its stated shape -------------
#
# Every one of these is read off `ctx["config"]` with the play's own
# number as the fallback, so `would_open`/`mc_prefilter` — which carry no
# config at all — gate identically to `decide`, exactly as `DIP_TREND_BAND_PCT`
# already does for the shipped rule.
DIP_PULLBACK_RSI_MAX = "dip_pullback_rsi_max"
DIP_PULLBACK_RSI_MAX_DEFAULT = 35.0
DIP_PULLBACK_SMA50_BAND_PCT = "dip_pullback_sma50_band_pct"
DIP_PULLBACK_SMA50_BAND_PCT_DEFAULT = 0.05
DIP_PULLBACK_MIN_ADV_USD = "dip_pullback_min_adv_usd"
DIP_PULLBACK_MIN_ADV_USD_DEFAULT = 20_000_000.0
DIP_PULLBACK_MAX_SPREAD_PCT = "dip_pullback_max_spread_pct"
DIP_PULLBACK_MAX_SPREAD_PCT_DEFAULT = 0.002

# A's conviction scale. It cannot be §6.2's depth-below-the-20-day: A's whole
# entry condition is that the name is *shallow* (within 5% of the 50-day), so
# the depth term §6.2 sizes on is bounded near zero by construction and every
# candidate would score the same. Depth into A's own band is the analogue —
# full scale is the band itself, so a name at the -5% edge is a 1.0 and one
# sitting on the 50-day is a 0.0 — maxed with the same RSI extremity term
# §6.2 uses, rebased on A's own RSI ceiling.
DIP_PULLBACK_RSI_EXTREMITY_FULL_SCALE = 20.0


def _config(ctx: Mapping[str, Any], key: str, default: float) -> float:
    return float((ctx.get("config") or {}).get(key, default))


def _dip(symbol: str, ctx: Mapping[str, Any]) -> _Proposal | Skipped | None:
    """§6.2, or §4.3 A where `play_dip_pullback_enabled` says so.

    Under `regime_v2_enabled` either shape's proposal must also clear
    `_sector_below_trend`: the name's sector ETF at or above its own 200-day.
    """
    outcome = _pullback(symbol, ctx) if pullback_enabled(ctx) else _classic(symbol, ctx)
    if (
        isinstance(outcome, _Proposal)
        and regime_v2_enabled(ctx)
        # S3: the DIP decision table may switch this rule off for the day's
        # posture. True unless `dip_regime_table_enabled` and the row say so.
        and regimes.dip_requires_sector_trend(ctx.get("regime"), ctx.get("config"))
    ):
        reason = _sector_below_trend(symbol, ctx)
        if reason is not None:
            return Skipped(
                symbol,
                DIP,
                SKIP_SECTOR_BELOW_200D,
                f"{symbol} is a dip but {reason}; DIP buys only where the "
                "sector itself is at or above its 200-day",
            )
    return outcome


def sector_etf_for(symbol: str, ctx: Mapping[str, Any]) -> tuple[str, str] | None:
    """``(GICS sector, its ETF)`` for a candidate, or None when either lookup
    misses — a sector the snapshot does not carry, or one with no ETF."""
    return regimes.sector_and_etf(symbol, ctx.get("sectors"), ctx.get("etfs"))


def _sector_below_trend(symbol: str, ctx: Mapping[str, Any]) -> str | None:
    """Why this name's sector is not one DIP buys into.

    `regime.sector_trend_reason` is the rule, shared with the Dip Recovery
    sleeve: a sector must be `trend_ok` — `last >= sma200` and not at a peak.

    **Unknown is ok, on every lookup**, the same direction `_sector_state_reason`
    and `regime._sector_state` fail in: no regime, no sector for the name, no
    ETF for the sector, no verdict for the ETF — the gate is not applied.
    """
    return regimes.sector_trend_reason(ctx.get("regime"), sector_etf_for(symbol, ctx))


def _classic(symbol: str, ctx: Mapping[str, Any]) -> _Proposal | Skipped | None:
    """§6.2. Oversold inside an uptrend — the play for weeks with no catalyst.

    The 200-day filter is the whole play. Without it "oversold" selects for
    names that are falling because something is wrong, and RSI 27 on a
    structural decline is not a dip. `dip_trend_band_pct` softens the line from
    "above the 200-day" to "within a few percent of it", which is where an
    ordinary pullback lives; a name well below still refuses.
    """
    signal = _signals(ctx, symbol)
    rsi = signal.get("rsi")
    if rsi is None or float(rsi) > DIP_RSI_TRIGGER:
        return None

    price = _price(ctx, symbol)
    sma_200 = signal.get("sma_200")
    band = float((ctx.get("config") or {}).get("dip_trend_band_pct", DIP_TREND_BAND_PCT))
    if price is None or sma_200 is None or price < float(sma_200) * (1.0 - band):
        return Skipped(
            symbol,
            DIP,
            ENTRY_CONDITIONS_UNMET,
            f"{symbol} is oversold at RSI {float(rsi):.0f} but is more than "
            f"{band:.0%} below its 200-day; that is a falling knife, not a dip",
        )

    veto = _earnings_veto(symbol, ctx, DIP, DIP_EARNINGS_CLEARANCE_SESSIONS)
    if veto is not None:
        return veto

    sma_20 = signal.get("sma_20")
    depth = 0.0 if not sma_20 else (float(sma_20) - price) / float(sma_20)
    depth_conviction = _scaled(depth, DIP_DEPTH_FULL_SCALE)

    if depth_conviction <= 0.0:
        return Skipped(
            symbol,
            DIP,
            NO_CONVICTION,
            f"{symbol} is oversold but is not below its 20-day mean, so the "
            "dip has no depth to size against",
        )

    extremity = _scaled(DIP_RSI_TRIGGER - float(rsi), DIP_RSI_EXTREMITY_FULL_SCALE)
    conviction = max(depth_conviction, extremity)

    return _Proposal(
        symbol,
        DIP,
        conviction,
        f"RSI {float(rsi):.0f}, {depth:.1%} below the 20-day mean, holding "
        f"within {band:.0%} of the 200-day",
    )


def _sector_state_reason(symbol: str, ctx: Mapping[str, Any]) -> str | None:
    """A's "sector state ok" leg — the reason it is not, or None.

    `cli._drop_blocked_sectors` already takes these names out at discovery, so
    on the live path this leg is normally a restatement that costs nothing.
    It is here anyway because the shadow ledger replays `decide` against stored
    inputs where the drop may have been made under a *different* config, and
    because §4.3 A names the condition as part of the play.

    **Unknown is ok, on both lookups.** A candidate whose GICS sector the
    snapshot does not carry, or a sector whose ETF price never arrived, is
    kept — the same "can be too generous, cannot afford to be too strict" rule
    `_drop_blocked_sectors` states, and the same direction `regime._sector_state`
    already fails in.
    """
    sector = (ctx.get("sectors") or {}).get(symbol)
    if sector is None:
        return None

    etf = regimes.sector_etf((ctx.get("etfs") or {})).get(sector)
    if etf is None:
        return None

    state = ((ctx.get("regime") or {}).get("sector_states") or {}).get(etf)
    if state not in regimes.BLOCKED_SECTOR_STATES:
        return None
    return f"{sector} ({etf}) is {state} today"


def _adv_usd(symbol: str, ctx: Mapping[str, Any], price: float) -> float | None:
    """Average dollar volume, or None when the average share volume is absent.

    `average_volume` is `get_equity_fundamentals`' own field, the one
    `_catalyst` already divides today's volume by, so this adds no call. It is
    a share count; A's gate is stated in dollars, so it is priced at the same
    last `_price` gave the trend checks rather than at a second number.
    """
    average = ((ctx.get("fundamentals") or {}).get(symbol) or {}).get("average_volume")
    if average is None:
        return None
    try:
        shares = float(average)
    except (TypeError, ValueError):
        return None
    return shares * price


def _spread_pct(symbol: str, ctx: Mapping[str, Any]) -> float | None:
    quote = ((ctx.get("quotes") or {}).get(symbol)) or {}
    bid, ask = quote.get("bid"), quote.get("ask")
    if bid is None or ask is None:
        return None
    try:
        bid, ask = float(bid), float(ask)
    except (TypeError, ValueError):
        return None
    mid = (bid + ask) / 2.0
    if mid <= 0.0:
        return None
    return (ask - bid) / mid


def _pullback(symbol: str, ctx: Mapping[str, Any]) -> _Proposal | Skipped | None:
    """Oversold, but only a *shallow* oversold.

    The difference from §6.2 that matters is the 50-day band. §6.2 sizes
    conviction on how far a name has fallen below its 20-day, which rewards
    exactly the names A refuses: a stock 12% under its 50-day inside a 3% band
    of its 200-day is not pulling back, it is arriving at the last support it
    has. A buys the ordinary 2-4% give-back in a trend that is still intact and
    pays for the tighter funnel with liquidity gates, because the fills are
    where a mean-reversion edge this small is actually lost.

    Every refusal past the trigger is a `Skipped` rather than a silent None:
    the whole point of running this in the shadow ledger is being able to read
    *which* of six gates cost the candidate, and a play that declines in
    silence answers none of that.
    """
    signal = _signals(ctx, symbol)
    rsi = signal.get("rsi")
    rsi_max = _config(ctx, DIP_PULLBACK_RSI_MAX, DIP_PULLBACK_RSI_MAX_DEFAULT)
    if rsi is None or float(rsi) > rsi_max:
        return None

    price = _price(ctx, symbol)
    if price is None:
        return None

    def refuse(message: str) -> Skipped:
        return Skipped(symbol, DIP, ENTRY_CONDITIONS_UNMET, message)

    # 1. Above the 200-day, within the shipped band. Same gate as §6.2's, same
    #    key — A's "band -3% allowed" is `dip_trend_band_pct`'s shipped value.
    sma_200 = signal.get("sma_200")
    band = _config(ctx, "dip_trend_band_pct", DIP_TREND_BAND_PCT)
    if sma_200 is None or price < float(sma_200) * (1.0 - band):
        return refuse(
            f"{symbol} is oversold at RSI {float(rsi):.0f} but is more than "
            f"{band:.0%} below its 200-day; that is a falling knife, not a dip"
        )

    # 2. Within -5%..0% of the 50-day. Both edges: over the 50-day there is no
    #    pullback to buy, and far under it the trend A is buying into is the
    #    thing that has already broken.
    sma_50 = signal.get("sma_50")
    if sma_50 is None or float(sma_50) <= 0.0:
        return refuse(
            f"{symbol} has no 50-day to measure the pullback against; §4.3 A "
            "buys a give-back inside a trend and cannot see the trend"
        )
    gap = (price - float(sma_50)) / float(sma_50)
    sma50_band = _config(
        ctx, DIP_PULLBACK_SMA50_BAND_PCT, DIP_PULLBACK_SMA50_BAND_PCT_DEFAULT
    )
    if gap > 0.0 or gap < -sma50_band:
        return refuse(
            f"{symbol} is {gap:+.1%} against its 50-day, outside §4.3 A's "
            f"{-sma50_band:.0%}..0% pullback band"
        )

    # 3. The sector the regime will buy into.
    sector_reason = _sector_state_reason(symbol, ctx)
    if sector_reason is not None:
        return refuse(f"{symbol} is a clean pullback but {sector_reason}")

    # 4. Liquidity, in dollars.
    min_adv = _config(ctx, DIP_PULLBACK_MIN_ADV_USD, DIP_PULLBACK_MIN_ADV_USD_DEFAULT)
    adv = _adv_usd(symbol, ctx, price)
    if adv is None:
        return refuse(
            f"{symbol} has no average volume on file, so §4.3 A's "
            f"${min_adv:,.0f} liquidity floor cannot be measured"
        )
    if adv < min_adv:
        return refuse(
            f"{symbol} trades ${adv:,.0f} a day, under §4.3 A's "
            f"${min_adv:,.0f} floor"
        )

    # 5. The spread, which is where this edge is lost if it is lost. Distinct
    #    from §4.3 F's 0.3% account-wide gate (`plays/no_trade.py`) and tighter:
    #    F is a condition on the day, this is a condition of the play.
    max_spread = _config(
        ctx, DIP_PULLBACK_MAX_SPREAD_PCT, DIP_PULLBACK_MAX_SPREAD_PCT_DEFAULT
    )
    spread = _spread_pct(symbol, ctx)
    if spread is None:
        return refuse(
            f"{symbol} has no two-sided quote, so §4.3 A's {max_spread:.2%} "
            "spread ceiling cannot be measured"
        )
    if spread > max_spread:
        return refuse(
            f"{symbol}'s {spread:.2%} spread is over §4.3 A's "
            f"{max_spread:.2%} ceiling"
        )

    # 6. The earnings clearance §6.2 has always had. §4.3 A does not restate
    #    it, and it is not A's to drop: "the system never holds through a
    #    print" is an account rule, not a property of this trigger.
    veto = _earnings_veto(symbol, ctx, DIP, DIP_EARNINGS_CLEARANCE_SESSIONS)
    if veto is not None:
        return veto

    depth = _scaled(-gap, sma50_band)
    extremity = _scaled(rsi_max - float(rsi), DIP_PULLBACK_RSI_EXTREMITY_FULL_SCALE)
    conviction = max(depth, extremity)

    if conviction <= 0.0:
        return Skipped(
            symbol,
            DIP,
            NO_CONVICTION,
            f"{symbol} is sitting exactly on its 50-day at RSI "
            f"{float(rsi):.0f}; there is no pullback to size against",
        )

    return _Proposal(
        symbol,
        DIP,
        conviction,
        f"RSI {float(rsi):.0f}, {gap:+.1%} against the 50-day inside §4.3 A's "
        f"{-sma50_band:.0%} band, ${adv:,.0f} ADV on a {spread:.2%} spread",
    )
