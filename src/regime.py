"""regime.py — the day's index and sector regime, computed rather than opined.

§6.0's `regime_fit` used to be a number a premarket session wrote into
`edge_play_state` by hand, once a day, from its own reading of the tape. That
producer died when the Dip Recovery retime moved the premarket trigger to
06:47 ET, ahead of `cadence.PREMARKET_OPEN` (07:00) — `_slot_type` classified
the firing as `overnight`, Step 11 never ran again, and every entry for the
following week was arbitrated against a frozen 0.4/0.5 that nobody had looked
at. Nothing detected it, because a stale number and a considered one are the
same JSON.

The replacement is this module: the same job, done from prices, so that a dead
producer shows up as an *absent* input rather than as a confident stale one.
Two questions, and no others:

* **What kind of tape is this?** SPY against its own 50- and 200-day, plus
  20-day realized volatility. `risk_off` is the §4.3 F condition — below the
  200-day *and* volatile — and it stops entries outright. `risk_on` is the
  stacked case, and it is the only tape SURGE's all-in mode is allowed to run
  in.
* **Which sectors are untradeable today?** A sector ETF far enough through its
  own 200-day is a `breakdown` and one stretched past `sector_rsi_peak` is a
  `peak`. Both mean the same thing to a candidate: do not spend a fetch on a
  name in that sector this cycle (the operator's "ignore the sector at a
  peak").

A third question was added: **how much of the market is with the index?** The
two above approved entries on 16 of 16 stored days, including one where SPY
read `risk_on` with 3 of 11 sectors above their own 200-day. `breadth`
measures that, `posture` turns it into one of five states with a size
multiplier, and `DIP_BLOCKED_SECTOR_STATES` stops `oversold_ok` being a buy
permission for DIP. All three are computed on every cycle and enforced on
*entries only*, by `plays.entries`, under `regime_v2_enabled`; nothing here
can reach an exit.

**Pure, and given no network** (Invariant 2). It reads one `regime_inputs`
block that the routine assembles from the same batched SMA/quote calls Step 6
already makes, and `config/config.json`'s `_10_regime` group. There is no
judgment left in it for a session to make.

**Absent input is not a dark regime.** `evaluate` returns `None` when there is
no readable SPY block, and `plays.entries` reads `None` as a multiplier of 1.0
on every play. That is the opposite of `regime_fit`'s old fail-closed posture,
and deliberately so: the old rule made one unwritten row stop all entries, and
what that actually bought was a week of trading against a stale 0.4 that
nobody could tell apart from a fresh one. A missing regime now degrades to "no
regime opinion", which is what it is; the plays' own gates, guard and sizing
are all still in front of every order.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

# The market states, coarsest first. Three because there are exactly three
# decisions downstream: stop entering (`risk_off`), enter normally
# (`neutral`), and enter normally *plus* let SURGE commit the book
# (`risk_on`).
RISK_ON = "risk_on"
NEUTRAL = "neutral"
RISK_OFF = "risk_off"

# What one sector ETF's own chart says. `trend_ok` and `oversold_ok` are both
# tradeable — the second is a sector under its 200-day but not far enough
# under to be structural, which is the ordinary pullback DIP exists for.
TREND_OK = "trend_ok"
OVERSOLD_OK = "oversold_ok"
PEAK = "peak"
BREAKDOWN = "breakdown"

# The two states that take a candidate out of the fetch list. Named as a set
# rather than checked twice, so the drop filter in `cli.candidates` and any
# later reader cannot come to disagree about which states block.
BLOCKED_SECTOR_STATES = frozenset({PEAK, BREAKDOWN})

# P1 of an earlier audit fixes. `oversold_ok` stays a state — the sector table
# and `sectors` still show it — but it stops being a buy permission for DIP: a
# dip is bought only where the sector itself is at or above its own 200-day.
# Read by `plays/dip.py` and nowhere else. Deliberately NOT folded into
# `BLOCKED_SECTOR_STATES`: that set is the discovery drop for every play, and
# CATALYST/PEAD are event plays a sector's 200-day must not take off the
# board.
DIP_BLOCKED_SECTOR_STATES = BLOCKED_SECTOR_STATES | {OVERSOLD_OK}

# The five postures (audit §5.2), coarsest last. They sit beside
# `market_state`/`allow_entries`/`play_multipliers`, which every existing
# consumer still reads; `plays.entries` enforces a posture only under
# `regime_v2_enabled`, and `cli.decide` records it as a shadow field otherwise.
TRADE_NORMALLY = "TRADE_NORMALLY"
REDUCED_SIZE = "REDUCED_SIZE"
HIGH_CONVICTION_ONLY = "HIGH_CONVICTION_ONLY"
NO_NEW_POSITIONS = "NO_NEW_POSITIONS"
HOLD_CASH = "HOLD_CASH"

POSTURES = (
    TRADE_NORMALLY, REDUCED_SIZE, HIGH_CONVICTION_ONLY, NO_NEW_POSITIONS, HOLD_CASH,
)

# The two postures in which no play may open a position.
NO_ENTRY_POSTURES = frozenset({NO_NEW_POSITIONS, HOLD_CASH})

# The posture thresholds, as `config/config.json`'s `_10b_regime_v2` group
# ships them. Each is read with `.get(key, default)` so a config that predates
# the group (a shadow-ledger replay, an old golden) computes the same posture
# the shipped file would.
POSTURE_DEFAULTS: dict[str, float] = {
    "regime_breadth_min_sectors": 8,
    "posture_normal_breadth_200": 0.60,
    "posture_high_conviction_breadth_200": 0.45,
    "posture_high_conviction_breadth_50": 0.40,
    "posture_no_new_breadth_200": 0.30,
    "posture_reduced_rv20": 0.20,
    "posture_high_conviction_rsp_lag_1m": 0.02,
    "posture_no_new_drawdown_52w": 0.08,
    "posture_no_new_drawdown_rv20": 0.25,
}
POSTURE_SIZE_MULTIPLIER = "posture_size_multiplier"
POSTURE_TARGET_INVESTED_PCT = "posture_target_invested_pct"
POSTURE_SIZE_MULTIPLIER_DEFAULT: dict[str, float] = {
    TRADE_NORMALLY: 1.0, REDUCED_SIZE: 0.5, HIGH_CONVICTION_ONLY: 0.5,
    NO_NEW_POSITIONS: 0.0, HOLD_CASH: 0.0,
}
POSTURE_TARGET_INVESTED_PCT_DEFAULT: dict[str, float] = {
    TRADE_NORMALLY: 0.80, REDUCED_SIZE: 0.50, HIGH_CONVICTION_ONLY: 0.30,
    NO_NEW_POSITIONS: 0.0, HOLD_CASH: 0.0,
}

# Sessions in "one month" and "three months" of daily bars, for sector
# relative strength.
RET_1M_SESSIONS = 21
RET_3M_SESSIONS = 63

# The 52-week high is the highest close over a trading year. The pre-market
# job fetches 250 daily bars, so the window is "up to 252, at least 240":
# short of that the figure would be a drawdown from a more recent, lower high
# and would understate the fall.
DRAWDOWN_SESSIONS = 252
DRAWDOWN_MIN_SESSIONS = 240

# The equal-weight S&P 500 ETF.
RSP = "RSP"

# Why a new entry was not taken on the day's regime. Defined here, beside the
# rules that emit them, and re-exported by `plays.core` and
# `dip_recovery.core` so core DIP and the Dip Recovery sleeve say the same
# thing in the same words. All are about new money only; no exit carries one.
# * SKIP_REGIME — the posture opens no position (NO_NEW_POSITIONS, HOLD_CASH),
#   or does not run this play (SURGE outside TRADE_NORMALLY).
# * SKIP_SECTOR_BELOW_200D — the name's sector ETF is under its own 200-day
#   (`DIP_BLOCKED_SECTOR_STATES`).
# * SKIP_SECTOR_RS_WEAK — the sector's 1M/3M strength vs SPY is outside the
#   upper half (HIGH_CONVICTION_ONLY), or in the weakest third (REDUCED_SIZE,
#   Dip Recovery only).
# * SKIP_RS_UNAVAILABLE — HIGH_CONVICTION_ONLY needs that ranking and it could
#   not be measured. Missing data has its own code so it never reads as a
#   failed test.
SKIP_REGIME = "SKIP_REGIME"
SKIP_SECTOR_BELOW_200D = "SKIP_SECTOR_BELOW_200D"
SKIP_SECTOR_RS_WEAK = "SKIP_SECTOR_RS_WEAK"
SKIP_RS_UNAVAILABLE = "SKIP_RS_UNAVAILABLE"

# Notes a `Regime` carries when an input a gate wanted was not there. Absent
# input is "gate not applied, reason recorded" — never blocked, never a crash.
BREADTH_UNAVAILABLE = "breadth_unavailable"
BREADTH_50_UNAVAILABLE = "breadth_50_unavailable"
RS_UNAVAILABLE = "rs_unavailable"
RSP_VS_SPY_UNAVAILABLE = "rsp_vs_spy_unavailable"
DRAWDOWN_52W_UNAVAILABLE = "drawdown_52w_unavailable"
MACRO_DAY_UNKNOWN = "macro_event_day_unknown"

# The five plays that can be proposed by `plays.entries`. Restated here rather
# than imported from `plays.core` for the same reason `candidates.py` restates
# its item codes: `plays.entries` imports *this* module, and importing back
# into `plays.core` would close the loop. There is one authoritative copy of
# each id (`plays/core.py`); these are labels on the output dict, and
# `tests/test_regime.py` pins them against it.
DIP = "DIP"
PEAD = "PEAD"
CATALYST = "CATALYST"
MACRO = "MACRO"
SURGE = "SURGE"

PLAY_IDS = (DIP, PEAD, CATALYST, MACRO, SURGE)

# Annualization for a daily realized vol, and the window it is measured over.
# 252 is the conventional session count; the exact figure does not matter to
# a threshold that is itself a tuning knob, but a *consistent* one does —
# `regime_vol_max` is stated in these units and nothing else may rescale them.
TRADING_DAYS_PER_YEAR = 252
RV_WINDOW = 20


@dataclass(frozen=True)
class Regime:
    """One trading day's tape, as five numbers the rest of the system can use.

    ``play_multipliers`` is what replaces `regime_fit` in §6.0's score. It is a
    per-play weight in [0, 1]; 0.0 means the tape has darked that play today
    and `plays.entries` records a `REGIME_DARK` skip naming this object's
    ``reason``.
    """

    market_state: str
    allow_entries: bool
    sector_states: dict[str, str]
    play_multipliers: dict[str, float]
    reason: str
    # P1. The posture and what it was judged from. Always computed;
    # enforced by `plays.entries` only under `regime_v2_enabled`.
    posture: str = TRADE_NORMALLY
    posture_reason: str = ""
    size_multiplier: float = 1.0
    target_invested_pct: float = 1.0
    breadth: dict = field(default_factory=dict)
    # `{ETF: {rs_1m, rs_3m, score, rank, upper_half}}`, or `{}` when the
    # returns it needs were not supplied.
    sector_rs: dict = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "market_state": self.market_state,
            "allow_entries": self.allow_entries,
            "sector_states": dict(self.sector_states),
            "play_multipliers": dict(self.play_multipliers),
            "reason": self.reason,
            "posture": self.posture,
            "posture_reason": self.posture_reason,
            "size_multiplier": self.size_multiplier,
            "target_invested_pct": self.target_invested_pct,
            "breadth": dict(self.breadth),
            "sector_rs": {k: dict(v) for k, v in self.sector_rs.items()},
            "notes": list(self.notes),
        }


def _number(value: Any) -> float | None:
    """A float, or None for anything that is not one.

    Every field here arrives from a vendor response the routine pasted
    through. A string that will not parse is missing data, not a crash: the
    caller's own absent-field path is already the right answer for it.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else number


def realized_vol(closes: Sequence[Any]) -> float | None:
    """Annualized standard deviation of the last ``RV_WINDOW`` daily returns.

    Computed here rather than in the session because the cycle prompt's second
    absolute is that a routine doing arithmetic has taken over someone else's
    job — and a standard deviation is exactly the kind of arithmetic that goes
    wrong silently. The routine pastes `get_equity_historicals`' closes
    through; this turns them into the one number `regime_vol_max` compares
    against.

    Sample standard deviation (n−1), so a short series is not flattered by its
    own length. ``None`` when there are not enough usable closes to measure
    twenty returns — a half-window vol is a different statistic wearing the
    same name.
    """
    values = [n for n in (_number(c) for c in closes or []) if n is not None and n > 0]
    if len(values) < RV_WINDOW + 1:
        return None

    window = values[-(RV_WINDOW + 1):]
    returns = [math.log(b / a) for a, b in zip(window, window[1:])]

    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(variance * TRADING_DAYS_PER_YEAR)


def _spy_rv(block: Mapping[str, Any]) -> float | None:
    """§4.3 F's vol leg: the published figure, else derived from closes.

    An explicit ``rv20`` wins because a caller that has one has already decided
    what window it measured. ``closes`` is the ordinary path — see
    `realized_vol` on why the session does not compute this itself.
    """
    published = _number(block.get("rv20"))
    if published is not None:
        return published
    return realized_vol(block.get("closes") or [])


def _sector_state(block: Mapping[str, Any], config: Mapping[str, Any]) -> str | None:
    """One ETF's verdict, or None when its own data is unreadable.

    **Absent is not blocked.** A sector whose quote or 200-day never arrived is
    left out of ``sector_states`` entirely, and the drop filter only drops what
    it has an explicit verdict for. Failing that closed would let one missing
    ETF price silently remove a ninth of the universe from the day — the same
    shape of defect as the frozen `regime_fit` this module exists to replace.

    Breakdown is tested before peak: a sector far enough through its 200-day is
    structurally broken whatever its oscillator says, and the two cannot both
    be acted on.
    """
    last = _number(block.get("last"))
    sma200 = _number(block.get("sma200"))
    if last is None or sma200 is None or sma200 <= 0:
        return None

    breakdown_pct = float(config["sector_breakdown_pct"])
    if last < sma200 * (1.0 - breakdown_pct):
        return BREAKDOWN

    rsi14 = _number(block.get("rsi14"))
    if rsi14 is not None and rsi14 > float(config["sector_rsi_peak"]):
        return PEAK

    return TREND_OK if last >= sma200 else OVERSOLD_OK


def _market_state(
    block: Mapping[str, Any], config: Mapping[str, Any]
) -> tuple[str, str] | None:
    """SPY's verdict and the sentence explaining it, or None if unreadable.

    ``risk_off`` needs both legs of §4.3 F — under the 200-day *and* volatile.
    Under the 200-day in a quiet tape is an ordinary correction and reads
    ``neutral``: that is the tape DIP is for, and suppressing it would starve
    the funnel exactly the way the frozen multiplier already did.
    """
    last = _number(block.get("last"))
    sma200 = _number(block.get("sma200"))
    if last is None or sma200 is None:
        return None

    sma50 = _number(block.get("sma50"))
    rv20 = _spy_rv(block)
    vol_max = float(config["regime_vol_max"])

    below_trend = last < sma200

    if below_trend and rv20 is not None and rv20 > vol_max:
        return RISK_OFF, (
            f"SPY {last:.2f} is below its 200-day {sma200:.2f} and 20-day "
            f"realized vol {rv20:.2f} is over the {vol_max:.2f} ceiling; no "
            "entries today"
        )

    if not below_trend and sma50 is not None and last > sma50:
        return RISK_ON, (
            f"SPY {last:.2f} is above both its 50-day {sma50:.2f} and its "
            f"200-day {sma200:.2f}"
        )

    if below_trend:
        measured = "unmeasurable" if rv20 is None else f"{rv20:.2f}"
        return NEUTRAL, (
            f"SPY {last:.2f} is below its 200-day {sma200:.2f} but 20-day "
            f"realized vol is {measured} against a {vol_max:.2f} ceiling; a "
            "correction, not a risk-off tape"
        )

    return NEUTRAL, (
        f"SPY {last:.2f} is above its 200-day {sma200:.2f} and not above its "
        "50-day; trend intact, momentum is not"
    )


def _play_multipliers(market_state: str) -> dict[str, float]:
    """§6.0's weight per play, which is where this module actually bites.

    Three rules and nothing else, because every finer distinction the old
    `regime_fit` invited was a number nobody could defend:

    * ``risk_off`` darks every play. This is ``allow_entries`` expressed in the
      one place §6.0 already reads.
    * ``SURGE`` needs ``risk_on``. It commits the entire settled balance across
      up to three names with §4.4's caps bypassed, and it was the *only* play
      the dead producer left unaffected — an absent `edge_play_state` row reads
      as 1.0 for SURGE by design (`plays/core.py`), so the riskiest play in the
      system has been running ungated.
    * Everything else runs at full weight when entries are allowed at all. The
      sector question is answered per candidate, in `cli.candidates`, and a
      blanket per-play damping would double-count it.
    """
    if market_state == RISK_OFF:
        return {play_id: 0.0 for play_id in PLAY_IDS}

    multipliers = {play_id: 1.0 for play_id in PLAY_IDS}
    multipliers[SURGE] = 1.0 if market_state == RISK_ON else 0.0
    return multipliers


def _knob(config: Mapping[str, Any], key: str) -> float:
    """One posture threshold: the config's, else the shipped default."""
    value = _number((config or {}).get(key))
    return float(POSTURE_DEFAULTS[key]) if value is None else value


def _posture_map(
    config: Mapping[str, Any], key: str, default: Mapping[str, float], posture: str
) -> float:
    """A per-posture number, falling back per posture rather than per map —
    a config that names four of the five still gets the fifth's default."""
    value = _number(((config or {}).get(key) or {}).get(posture))
    return float(default[posture]) if value is None else value


def period_return(closes: Sequence[Any], sessions: int) -> float | None:
    """The fractional change over the last ``sessions`` daily closes.

    ``None`` unless the series reaches back the whole way: a "three-month"
    return measured over six weeks is a different number wearing its name.
    """
    values = [n for n in (_number(c) for c in closes or []) if n is not None and n > 0]
    if sessions <= 0 or len(values) < sessions + 1:
        return None
    return values[-1] / values[-(sessions + 1)] - 1.0


def drawdown_from_high(closes: Sequence[Any]) -> float | None:
    """The last close against the highest close of the trailing trading year.

    A signed fraction, zero or negative: ``-0.08`` is 8% off the high.
    ``None`` with fewer than ``DRAWDOWN_MIN_SESSIONS`` usable closes.
    """
    values = [n for n in (_number(c) for c in closes or []) if n is not None and n > 0]
    if len(values) < DRAWDOWN_MIN_SESSIONS:
        return None
    high = max(values[-DRAWDOWN_SESSIONS:])
    return values[-1] / high - 1.0


def _drawdown_52w(spy: Mapping[str, Any]) -> float | None:
    """SPY's drawdown: the published figure, else derived from its closes.

    A positive value is not a drawdown — it is a block written in some other
    convention — and reads as missing rather than as "no drawdown".
    """
    published = _number(spy.get("drawdown_52w"))
    if published is not None:
        return published if published <= 0.0 else None
    return drawdown_from_high(spy.get("closes") or [])


def _rsp_vs_spy_1m(inputs: Mapping[str, Any]) -> float | None:
    """RSP's 1-month return minus SPY's; positive is equal-weight leading.

    The published difference wins. Otherwise it is derived from the two
    blocks' own 1-month returns, and is missing if either is.
    """
    published = _number(inputs.get("rsp_vs_spy_1m"))
    if published is not None:
        return published
    rsp = _block_return(inputs.get("rsp") or {}, "ret_1m", RET_1M_SESSIONS)
    spy = _block_return(inputs.get("spy") or {}, "ret_1m", RET_1M_SESSIONS)
    return None if rsp is None or spy is None else rsp - spy


def _block_return(block: Mapping[str, Any], key: str, sessions: int) -> float | None:
    """A published return, else one derived from the block's own closes."""
    published = _number(block.get(key))
    if published is not None:
        return published
    return period_return(block.get("closes") or [], sessions)


def breadth(
    sector_blocks: Mapping[str, Any] | None, config: Mapping[str, Any] | None = None
) -> dict:
    """How much of the market is above its own trend.

    ``breadth_200`` / ``breadth_50`` are the fraction of sector ETFs at or
    above their own 200-day / 50-day. The denominator is the ETFs whose own
    data is readable, never eleven — and with fewer than
    ``regime_breadth_min_sectors`` of them readable the figure is ``None`` and
    ``breadth_unavailable`` is set. A thin sample is not gated on: three ETFs
    of which two are above trend is not "67% breadth".
    """
    minimum = int(_knob(config or {}, "regime_breadth_min_sectors"))

    def leg(average: str) -> tuple[float | None, int]:
        readable = above = 0
        for block in (sector_blocks or {}).values():
            last = _number((block or {}).get("last"))
            mean = _number((block or {}).get(average))
            if last is None or mean is None or mean <= 0:
                continue
            readable += 1
            above += last >= mean
        if readable < minimum:
            return None, readable
        return above / readable, readable

    breadth_200, n_200 = leg("sma200")
    breadth_50, n_50 = leg("sma50")
    if breadth_200 is None:
        # Without the 200-day leg there is no breadth reading at all; a 50-day
        # figure on its own would be gated on as if it were one.
        breadth_50 = None

    return {
        "breadth_200": breadth_200,
        "breadth_50": breadth_50,
        "n_available": n_200,
        "n_available_50": n_50,
        "breadth_unavailable": breadth_200 is None,
    }


def sector_relative_strength(
    inputs: Mapping[str, Any], config: Mapping[str, Any] | None = None
) -> dict[str, dict]:
    """Each sector ETF's 1M and 3M return against SPY's, ranked.

    ``score`` is the mean of the two excess returns, ``upper_half`` is the
    top ``n // 2`` by it (five of eleven) and ``weakest`` the bottom ``n // 3``
    (three of eleven). ``{}`` when SPY's own returns are
    missing or fewer than ``regime_breadth_min_sectors`` sectors carry both —
    a ranking of four sectors is not "the upper half of the eleven".
    """
    spy = inputs.get("spy") or {}
    spy_1m = _block_return(spy, "ret_1m", RET_1M_SESSIONS)
    spy_3m = _block_return(spy, "ret_3m", RET_3M_SESSIONS)
    if spy_1m is None or spy_3m is None:
        return {}

    rows: dict[str, dict] = {}
    for symbol, block in (inputs.get("sectors") or {}).items():
        ret_1m = _block_return(block or {}, "ret_1m", RET_1M_SESSIONS)
        ret_3m = _block_return(block or {}, "ret_3m", RET_3M_SESSIONS)
        if ret_1m is None or ret_3m is None:
            continue
        rs_1m, rs_3m = ret_1m - spy_1m, ret_3m - spy_3m
        rows[symbol] = {"rs_1m": rs_1m, "rs_3m": rs_3m, "score": (rs_1m + rs_3m) / 2.0}

    if len(rows) < int(_knob(config or {}, "regime_breadth_min_sectors")):
        return {}

    ranked = sorted(rows, key=lambda symbol: (-rows[symbol]["score"], symbol))
    cutoff = len(ranked) // 2
    for index, symbol in enumerate(ranked):
        rows[symbol]["rank"] = index + 1
        rows[symbol]["upper_half"] = index < cutoff
        rows[symbol]["weakest"] = index >= len(ranked) - len(ranked) // 3
    return {symbol: rows[symbol] for symbol in sorted(rows)}


def _posture(
    inputs: Mapping[str, Any],
    config: Mapping[str, Any],
    market_state: str,
    breadths: Mapping[str, Any],
    macro_event_day: bool | None,
) -> tuple[str, str, list[str]]:
    """The posture, the sentence explaining it, and what could not be read.

    Every input beyond SPY's own 200-day is optional: a rule whose input is
    absent does not fire, and the note says which.
    """
    spy = inputs.get("spy") or {}
    last = _number(spy.get("last"))
    sma200 = _number(spy.get("sma200"))
    sma50 = _number(spy.get("sma50"))
    rv20 = _spy_rv(spy)
    b200 = breadths.get("breadth_200")
    b50 = breadths.get("breadth_50")
    rsp_vs_spy = _rsp_vs_spy_1m(inputs)
    drawdown = _drawdown_52w(spy)

    notes: list[str] = []
    if b200 is None:
        notes.append(BREADTH_UNAVAILABLE)
    elif b50 is None:
        notes.append(BREADTH_50_UNAVAILABLE)
    if rsp_vs_spy is None:
        notes.append(RSP_VS_SPY_UNAVAILABLE)
    if drawdown is None:
        notes.append(DRAWDOWN_52W_UNAVAILABLE)
    if macro_event_day is None:
        notes.append(MACRO_DAY_UNKNOWN)

    if b200 is None:
        seen = (
            f"breadth unavailable ({breadths.get('n_available', 0)} sector ETFs "
            "readable)"
        )
    else:
        seen = (
            f"breadth {b200:.0%} of {breadths.get('n_available')} sectors above "
            "their 200-day"
        )

    if market_state == RISK_OFF:
        return HOLD_CASH, f"risk_off tape; {seen}", notes

    no_new = _knob(config, "posture_no_new_breadth_200")
    if last is not None and sma200 is not None and last < sma200:
        return NO_NEW_POSITIONS, f"SPY is below its 200-day; {seen}", notes
    if b200 is not None and b200 < no_new:
        return NO_NEW_POSITIONS, f"{seen}, under the {no_new:.0%} floor", notes
    if (
        drawdown is not None and rv20 is not None
        and drawdown < -_knob(config, "posture_no_new_drawdown_52w")
        and rv20 > _knob(config, "posture_no_new_drawdown_rv20")
    ):
        return NO_NEW_POSITIONS, (
            f"SPY is {-drawdown:.0%} off its 52-week high with realized vol "
            f"{rv20:.2f}; {seen}"
        ), notes

    high = _knob(config, "posture_high_conviction_breadth_200")
    if b200 is not None and b200 < high:
        return HIGH_CONVICTION_ONLY, f"{seen}, under {high:.0%}", notes
    high_50 = _knob(config, "posture_high_conviction_breadth_50")
    if (
        b50 is not None and last is not None and sma50 is not None
        and last < sma50 and b50 < high_50
    ):
        return HIGH_CONVICTION_ONLY, (
            f"SPY is below its 50-day with {b50:.0%} of sectors above theirs; {seen}"
        ), notes
    lag = _knob(config, "posture_high_conviction_rsp_lag_1m")
    if rsp_vs_spy is not None and rsp_vs_spy < -lag:
        return HIGH_CONVICTION_ONLY, (
            f"equal-weight is lagging SPY by {-rsp_vs_spy:.1%} over a month; {seen}"
        ), notes

    normal = _knob(config, "posture_normal_breadth_200")
    if b200 is not None and b200 < normal:
        return REDUCED_SIZE, f"{seen}, under {normal:.0%}", notes
    reduced_rv = _knob(config, "posture_reduced_rv20")
    if rv20 is not None and rv20 >= reduced_rv:
        return REDUCED_SIZE, (
            f"20-day realized vol {rv20:.2f} is at or over {reduced_rv:.2f}; {seen}"
        ), notes
    if macro_event_day:
        return REDUCED_SIZE, f"macro-event day; {seen}", notes
    if sma50 is not None and last is not None and last < sma50:
        return REDUCED_SIZE, f"SPY is below its 50-day; {seen}", notes

    return TRADE_NORMALLY, seen, notes


def evaluate(
    inputs: Mapping[str, Any] | None,
    config: Mapping[str, Any],
    *,
    macro_event_day: bool | None = None,
) -> Regime | None:
    """The day's regime, or ``None`` when there is nothing to judge it from.

    ``None`` is returned for an absent block and for one whose SPY reading is
    unusable, and the two are the same answer on purpose: a regime derived from
    half its inputs is the stale-number problem again in a new costume. Every
    caller reads ``None`` as "no opinion", never as "dark".

    ``macro_event_day`` is the one posture input that does not come from
    prices: `cli.decide` reads it off the day's macro releases. ``None`` (the
    pre-market job, which has no release file) means "not known", and the
    macro-day rule does not fire.
    """
    if not inputs:
        return None

    # `edge_regime_recent()` returns the whole stored payload, with the block
    # one level down. A caller that pasted it through unopened still gets a
    # regime rather than a silent "no opinion".
    if "spy" not in inputs and isinstance(inputs.get("regime_inputs"), Mapping):
        inputs = inputs["regime_inputs"]

    verdict = _market_state(inputs.get("spy") or {}, config)
    if verdict is None:
        return None

    market_state, reason = verdict

    sector_states: dict[str, str] = {}
    for symbol, block in sorted((inputs.get("sectors") or {}).items()):
        state = _sector_state(block or {}, config)
        if state is not None:
            sector_states[symbol] = state

    blocked = sorted(
        symbol for symbol, state in sector_states.items()
        if state in BLOCKED_SECTOR_STATES
    )
    if blocked:
        reason = f"{reason}; {', '.join(blocked)} not tradeable today"

    breadths = breadth(inputs.get("sectors"), config)
    sector_rs = sector_relative_strength(inputs, config)
    posture, posture_reason, notes = _posture(
        inputs, config, market_state, breadths, macro_event_day
    )
    if not sector_rs:
        notes.append(RS_UNAVAILABLE)

    return Regime(
        market_state=market_state,
        allow_entries=market_state != RISK_OFF,
        sector_states=sector_states,
        play_multipliers=_play_multipliers(market_state),
        reason=reason,
        posture=posture,
        posture_reason=f"{posture}: {posture_reason}",
        size_multiplier=_posture_map(
            config, POSTURE_SIZE_MULTIPLIER, POSTURE_SIZE_MULTIPLIER_DEFAULT, posture
        ),
        target_invested_pct=_posture_map(
            config, POSTURE_TARGET_INVESTED_PCT, POSTURE_TARGET_INVESTED_PCT_DEFAULT,
            posture,
        ),
        breadth=breadths,
        sector_rs=sector_rs,
        notes=tuple(notes),
    )


def sector_etf(etfs: Mapping[str, Any]) -> dict[str, str]:
    """``{sector name: ETF}`` — `config/etfs.json`'s sector map, inverted.

    §5.2's file is keyed the other way (`{"XLK": "Information Technology"}`)
    because that is the concentration bucket a symbol counts against. The
    regime question runs the other direction: a candidate carries a GICS sector
    name from the issuer holdings file and needs the ETF whose chart speaks for
    it. Inverting here rather than adding a second mapping to the file keeps
    one authoritative copy (applied to a map rather than to a value).
    """
    return {
        sector: symbol
        for symbol, sector in (etfs.get("sector") or {}).items()
        if sector
    }


def blocked_sectors(
    regime: Mapping[str, Any] | None, etfs: Mapping[str, Any]
) -> dict[str, str]:
    """``{sector name: state}`` for every sector this cycle will not buy into.

    Keyed by GICS name so a caller holding `universe.sector_map`'s output can
    look a candidate up directly. Empty for an absent regime, which is what
    makes this safe to call unconditionally.
    """
    states = (regime or {}).get("sector_states") or {}
    names = {symbol: sector for sector, symbol in sector_etf(etfs).items()}

    return {
        names[symbol]: state
        for symbol, state in states.items()
        if state in BLOCKED_SECTOR_STATES and symbol in names
    }



# ---------------------------------------------------------------------------
# The entry gate — one reading of a regime dict, for every consumer
# ---------------------------------------------------------------------------
#
# `plays.entries` (core DIP) and `dip_recovery.engine` (the Dip Recovery
# sleeve) both buy oversold names, and both read the day's regime through the
# functions below, so "the sector is below its 200-day" cannot come to mean
# two things. Each takes the regime as the dict `Regime.as_dict()` produced
# and returns ``None`` for "nothing to say" — an absent regime, an unknown
# sector, a sector with no ETF row. Absent is never blocked.


def sector_and_etf(
    symbol: str, sectors: Mapping[str, Any] | None, etfs: Mapping[str, Any] | None
) -> tuple[str, str] | None:
    """``(GICS sector, its ETF)`` for a symbol, or None when either lookup
    misses — a sector the snapshot does not carry, or one with no ETF."""
    sector = (sectors or {}).get(symbol)
    if sector is None:
        return None
    etf = sector_etf(etfs or {}).get(sector)
    return None if etf is None else (sector, etf)


def posture_seen(regime: Mapping[str, Any] | None) -> str:
    """"breadth 27% of sectors above their 200-day", for a skip message."""
    value = ((regime or {}).get("breadth") or {}).get("breadth_200")
    if value is None:
        return "breadth unavailable"
    return f"breadth {value:.0%} of sectors above their 200-day"


def no_entry_reason(regime: Mapping[str, Any] | None) -> str | None:
    """Why the posture opens no position at all today, or None if it does."""
    posture = (regime or {}).get("posture")
    if posture not in NO_ENTRY_POSTURES:
        return None
    return f"{posture} ({posture_seen(regime)})"


def sector_trend_reason(
    regime: Mapping[str, Any] | None, found: tuple[str, str] | None
) -> str | None:
    """Why this sector is not one a dip is bought in, or None.

    `oversold_ok` used to be a buy permission: any sector under its own
    200-day, as long as it was within `sector_breakdown_pct` of it. An
    earlier cohort was bought into exactly that. A sector
    must be `trend_ok` — `last >= sma200` and not at a peak.
    """
    if found is None:
        return None
    sector, etf = found
    state = ((regime or {}).get("sector_states") or {}).get(etf)
    if state not in DIP_BLOCKED_SECTOR_STATES:
        return None
    if state == OVERSOLD_OK:
        return f"{sector} ({etf}) is below its own 200-day"
    return f"{sector} ({etf}) is {state} today"


def sector_strength_verdict(
    regime: Mapping[str, Any] | None,
    found: tuple[str, str] | None,
    *,
    weakest_in_reduced: bool = False,
) -> tuple[str, str] | None:
    """``(code, message)`` for a dip refused on sector relative strength.

    HIGH_CONVICTION_ONLY needs the sector in the upper half of the ranking,
    and an unmeasurable ranking is `SKIP_RS_UNAVAILABLE`, not a pass.
    With ``weakest_in_reduced`` (the Dip Recovery sleeve), REDUCED_SIZE also
    refuses the weakest third — but only where there is a ranking to read: a
    missing one does not block there.
    """
    posture = (regime or {}).get("posture")
    sector_rs = (regime or {}).get("sector_rs") or {}
    row = sector_rs.get(found[1]) if found else None
    seen = f"{posture} ({posture_seen(regime)})"

    if posture == HIGH_CONVICTION_ONLY:
        if row is None:
            return SKIP_RS_UNAVAILABLE, (
                f"{seen}: a dip needs its sector in the upper half on relative "
                "strength, and there is no relative-strength reading for this "
                "name's sector"
            )
        if not row.get("upper_half"):
            return SKIP_SECTOR_RS_WEAK, (
                f"{seen}: {found[0]} ({found[1]}) ranks {row.get('rank')} of "
                f"{len(sector_rs)} on 1M/3M strength vs SPY, outside the upper half"
            )

    if weakest_in_reduced and posture == REDUCED_SIZE and row and row.get("weakest"):
        return SKIP_SECTOR_RS_WEAK, (
            f"{seen}: {found[0]} ({found[1]}) ranks {row.get('rank')} of "
            f"{len(sector_rs)} on 1M/3M strength vs SPY, in the weakest third"
        )

    return None


def dip_entry_verdict(
    regime: Mapping[str, Any] | None,
    found: tuple[str, str] | None,
    *,
    weakest_in_reduced: bool = False,
) -> tuple[str, str] | None:
    """The whole gate for one oversold-name entry, most general rule first:
    the posture, then the sector's trend, then its relative strength."""
    if not regime:
        return None
    reason = no_entry_reason(regime)
    if reason is not None:
        return SKIP_REGIME, f"{reason}: no new position"
    reason = sector_trend_reason(regime, found)
    if reason is not None:
        return SKIP_SECTOR_BELOW_200D, (
            f"{reason}; a dip is bought only where the sector itself is at or "
            "above its 200-day"
        )
    return sector_strength_verdict(regime, found, weakest_in_reduced=weakest_in_reduced)


def entry_size_multiplier(regime: Mapping[str, Any] | None) -> float:
    """The posture's size multiplier for a new entry; 1.0 with no regime."""
    value = _number((regime or {}).get("size_multiplier"))
    return 1.0 if value is None else value


# ---------------------------------------------------------------------------
# S3: the DIP decision table (off by default)
# ---------------------------------------------------------------------------
#
# P1 answers "may DIP buy in
# this posture, at what size, under which sector tests" with rules written
# into the code above. This restates those answers as one row per posture in
# `config/config.json`, so that whichever way the evidence eventually points
# the answer is a config edit and not a deploy.
#
# It acts only when `regime_v2_enabled` AND `dip_regime_table_enabled` are
# both true, and only on NEW core DIP entries. Exits, the resting
# take-profit, the per-cycle stop check, top-ups, every other play and the
# Dip Recovery sleeve never read it.
#
# **As shipped, every row is the P1 rule**, so turning the switch on changes
# nothing until a row is edited (`tests/test_dip_regime_table.py` pins the
# equivalence). Three limits no row can lift:
#
# * `dip_size_multiplier` is clamped to [0, 1]. The table lowers a size; it
#   never raises one above the ladder.
# * HOLD_CASH opens nothing. The risk-off tape darks every play before this
#   is read (`_play_multipliers`), and the row is clamped to match.
# * A missing table, row or field falls back, field by field, to the P1
#   value, and an unreadable value is treated as missing and named in
#   ``notes``.
DIP_REGIME_TABLE_ENABLED = "dip_regime_table_enabled"
DIP_REGIME_TABLE = "dip_regime_table"

DIP_ALLOWED = "dip_allowed"
DIP_SIZE_MULTIPLIER = "dip_size_multiplier"
REQUIRE_SECTOR_ABOVE_200D = "require_sector_above_200d"
REQUIRE_SECTOR_RS_UPPER_HALF = "require_sector_rs_upper_half"
MAX_RSI = "max_rsi"

# The P1 rule, as rows. `dip_size_multiplier` is absent on purpose: its P1
# value is whatever `posture_size_multiplier` gave the day's regime.
DIP_REGIME_TABLE_P1: dict[str, dict[str, Any]] = {
    TRADE_NORMALLY: {DIP_ALLOWED: True, REQUIRE_SECTOR_ABOVE_200D: True,
                     REQUIRE_SECTOR_RS_UPPER_HALF: False, MAX_RSI: None},
    REDUCED_SIZE: {DIP_ALLOWED: True, REQUIRE_SECTOR_ABOVE_200D: True,
                   REQUIRE_SECTOR_RS_UPPER_HALF: False, MAX_RSI: None},
    HIGH_CONVICTION_ONLY: {DIP_ALLOWED: True, REQUIRE_SECTOR_ABOVE_200D: True,
                           REQUIRE_SECTOR_RS_UPPER_HALF: True, MAX_RSI: None},
    NO_NEW_POSITIONS: {DIP_ALLOWED: False, REQUIRE_SECTOR_ABOVE_200D: True,
                       REQUIRE_SECTOR_RS_UPPER_HALF: False, MAX_RSI: None},
    HOLD_CASH: {DIP_ALLOWED: False, REQUIRE_SECTOR_ABOVE_200D: True,
                REQUIRE_SECTOR_RS_UPPER_HALF: False, MAX_RSI: None},
}

# Why a table row refused a dip on its RSI ceiling. The other refusals reuse
# P1's codes, because they are P1's rules read from a different place.
SKIP_DIP_TABLE_RSI = "SKIP_DIP_TABLE_RSI"


def dip_table_enabled(config: Mapping[str, Any] | None) -> bool:
    """Whether DIP's posture handling is read from the table. Needs both
    switches: without `regime_v2_enabled` no posture is enforced on anything,
    and a table that enforced one for DIP alone would be a second regime."""
    config = config or {}
    return bool(config.get("regime_v2_enabled")) and bool(config.get(DIP_REGIME_TABLE_ENABLED))


def dip_table_row(
    regime: Mapping[str, Any] | None, config: Mapping[str, Any] | None
) -> dict | None:
    """The day's row with every fallback applied, or None with no posture.

    ``{dip_allowed, dip_size_multiplier, require_sector_above_200d,
    require_sector_rs_upper_half, max_rsi, posture, notes}``. A posture the
    table does not name, and a field a row does not carry, read as P1.
    """
    posture = (regime or {}).get("posture")
    if posture not in DIP_REGIME_TABLE_P1:
        return None

    base = DIP_REGIME_TABLE_P1[posture]
    table = (config or {}).get(DIP_REGIME_TABLE)
    given = table.get(posture) if isinstance(table, Mapping) else None
    given = given if isinstance(given, Mapping) else {}
    notes: list[str] = []

    def flag(key: str) -> bool:
        value = given.get(key)
        if isinstance(value, bool):
            return value
        if value is not None:
            notes.append(f"{key}_unreadable")
        return bool(base[key])

    multiplier = _number(given.get(DIP_SIZE_MULTIPLIER))
    if multiplier is None:
        if given.get(DIP_SIZE_MULTIPLIER) is not None:
            notes.append(f"{DIP_SIZE_MULTIPLIER}_unreadable")
        multiplier = entry_size_multiplier(regime)
    multiplier = max(0.0, min(1.0, multiplier))

    max_rsi = _number(given.get(MAX_RSI))
    if max_rsi is None and given.get(MAX_RSI) is not None:
        notes.append(f"{MAX_RSI}_unreadable")

    allowed = flag(DIP_ALLOWED)
    if posture == HOLD_CASH and allowed:
        allowed = False
        notes.append("hold_cash_cannot_be_opened")

    return {
        DIP_ALLOWED: allowed,
        DIP_SIZE_MULTIPLIER: multiplier,
        REQUIRE_SECTOR_ABOVE_200D: flag(REQUIRE_SECTOR_ABOVE_200D),
        REQUIRE_SECTOR_RS_UPPER_HALF: flag(REQUIRE_SECTOR_RS_UPPER_HALF),
        MAX_RSI: max_rsi,
        "posture": posture,
        "notes": notes,
    }


def dip_requires_sector_trend(
    regime: Mapping[str, Any] | None, config: Mapping[str, Any] | None
) -> bool:
    """Whether DIP's "sector at or above its 200-day" rule applies today.
    True (P1) unless the table is on and the day's row switches it off."""
    if not dip_table_enabled(config):
        return True
    row = dip_table_row(regime, config)
    return True if row is None else bool(row[REQUIRE_SECTOR_ABOVE_200D])


def dip_table_gate(
    regime: Mapping[str, Any] | None,
    found: tuple[str, str] | None,
    config: Mapping[str, Any] | None,
    *,
    rsi: Any = None,
    symbol: str | None = None,
) -> tuple[float, tuple[str, str] | None]:
    """The table over one DIP candidate that has already cleared the sector
    trend rule: ``(size multiplier, None)`` to take it, or
    ``(0.0, (code, message))`` to refuse it.

    In order: the posture's `dip_allowed` (and a multiplier of zero, which is
    the same answer); the row's RSI ceiling; the sector's relative strength.
    An RSI ceiling with no RSI to read refuses, the direction
    `SKIP_RS_UNAVAILABLE` already takes: a row that admits only the most
    oversold names has not been satisfied by a name nobody measured.
    """
    row = dip_table_row(regime, config)
    if row is None:
        return entry_size_multiplier(regime), None
    posture = row["posture"]
    seen = f"{posture} ({posture_seen(regime)})"

    if not row[DIP_ALLOWED] or row[DIP_SIZE_MULTIPLIER] <= 0.0:
        where = "" if symbol is None else f" in {symbol}"
        return 0.0, (SKIP_REGIME, f"{seen}: no new position{where}")

    ceiling = row[MAX_RSI]
    if ceiling is not None:
        value = _number(rsi)
        if value is None:
            return 0.0, (SKIP_DIP_TABLE_RSI, (
                f"{seen}: a dip needs RSI at or under {ceiling:.0f} here and "
                "this name has no RSI reading"
            ))
        if value > ceiling:
            return 0.0, (SKIP_DIP_TABLE_RSI, (
                f"{seen}: RSI {value:.0f} is over the {ceiling:.0f} a dip "
                "needs in this posture"
            ))

    if row[REQUIRE_SECTOR_RS_UPPER_HALF]:
        sector_rs = (regime or {}).get("sector_rs") or {}
        rs = sector_rs.get(found[1]) if found else None
        if rs is None:
            return 0.0, (SKIP_RS_UNAVAILABLE, (
                f"{seen}: a dip needs its sector in the upper half on relative "
                "strength, and there is no relative-strength reading for this "
                "name's sector"
            ))
        if not rs.get("upper_half"):
            return 0.0, (SKIP_SECTOR_RS_WEAK, (
                f"{seen}: {found[0]} ({found[1]}) ranks {rs.get('rank')} of "
                f"{len(sector_rs)} on 1M/3M strength vs SPY, outside the upper half"
            ))

    return row[DIP_SIZE_MULTIPLIER], None
