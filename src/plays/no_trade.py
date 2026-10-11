"""§4.3 F — the no-trade conditions, off by default.

Strategy F is not a play. It is a set of conditions under
which *no* play may enter, proposed there with an explicit instruction to
measure it before believing it: "Shadow: count suppressed trades and their
hypothetical P&L". So it ships here as a rule the shipped config does not turn
on — `no_trade_conditions` is absent from `config/config.json`, this module
reads that as false, and `plays.entries` behaves exactly as it did — and the
shadow ledger's `D_no_trade` variant turns it on by config override to produce
the evidence §4.3 asks for. Nothing else may turn it on until that evidence
exists.

**Three of F's four conditions are already enforced, and differently.** That
is the whole reason this module is worth having rather than a config flag on
something existing:

* *SPY below its 200-day and volatile.* `src/regime.py` already darks every
  play on exactly this pair (`_market_state`'s `risk_off`), but its vol leg is
  an **absolute** ceiling — `regime_vol_max`, 0.35 annualized. F states a
  **relative** one: 20-day realized vol above 1.5x its own 60-day median. The
  two disagree in both directions. A market whose vol has doubled off a very
  quiet base is F's no-trade and regime's risk-on; a permanently volatile tape
  sitting at its own median is regime's risk-off and F's fine. Only the
  relative leg is implemented here; the absolute one stays regime's.
* *`equity_total` missing.* `guard.halts` already denies every order on an
  absent, zero or non-numeric `equity_total`, and has since a real incident.
  Restated here so a suppression is recorded at proposal time and shows up in
  the shadow ledger as a suppressed *trade* rather than as a guard denial the
  ledger never sees. It can never be the only thing that stops an order.
* *Any open reconcile divergence.* `reconcile.py` blocks entries only once a
  divergence has **persisted across two cycles** (`PERSISTENT_DIVERGENCE_CYCLES`);
  F blocks on the first. Read off `edge_control.divergence_streak`, which the
  last reconcile wrote — not recomputed here, because reconcile is the one
  module entitled to say the book and the broker disagree.

The fourth, the spread gate, has no equivalent anywhere on the entry path:
`guard.legality._overnight_spread_denials` measures a spread, but only outside
regular hours and only for exits.

**Account-wide versus per-candidate.** The first three suppress the whole
cycle; the spread gate suppresses one name. `plays.entries` applies both after
arbitration, so every suppressed candidate is recorded as a `Skipped` with the
condition that took it — which is what makes "count the suppressed trades"
answerable from `edge_runlog` and from `edge_shadow_trades` alike.
"""

from __future__ import annotations

from statistics import median
from typing import Any, Mapping, Sequence

from src.regime import RV_WINDOW, realized_vol

# §4.3 F's own numbers, as module constants rather than config keys. These
# play-shape thresholds stay out of `config/config.json`, and these
# are the shape of the condition, not a knob: F is a specific hypothesis with
# specific numbers, and a tunable version of it would be a different
# hypothesis that the shadow evidence would not be about.
VOL_MEDIAN_MULTIPLE = 1.5
MAX_SPREAD_PCT = 0.003

# How many closes it takes to have a 60-day median of a 20-day vol at all.
# Below this the vol leg abstains rather than compares against a median of two
# windows — a "median" over a handful of overlapping windows is not the
# statistic F names, and abstaining leaves regime's absolute ceiling as the
# only vol gate, which is the pre-F behavior and therefore the safe direction.
VOL_MEDIAN_WINDOW = 60
MIN_CLOSES = RV_WINDOW * 2

# The switch. Absent from `config/config.json` on purpose — see the docstring.
CONFIG_KEY = "no_trade_conditions"

# Skip codes, kept distinct so the runlog says which leg of F fired.
NO_TRADE_VOL = "NO_TRADE_VOL"
NO_TRADE_EQUITY = "NO_TRADE_EQUITY"
NO_TRADE_DIVERGENCE = "NO_TRADE_DIVERGENCE"
NO_TRADE_SPREAD = "NO_TRADE_SPREAD"


def enabled(ctx: Mapping[str, Any]) -> bool:
    """Whether §4.3 F is switched on for this cycle.

    Truthiness, not presence: every payload that predates this module omits the
    key, and `config.get` reading `None` as off is the behavior that keeps the
    live account exactly where it was.
    """
    return bool((ctx.get("config") or {}).get(CONFIG_KEY))


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rv_series(closes: Sequence[Any]) -> list[float]:
    """Every trailing ``RV_WINDOW`` realized vol the closes support, oldest
    first, over at most the last ``VOL_MEDIAN_WINDOW`` windows.

    `realized_vol` is `regime.py`'s, not a second implementation — F's leg has
    to be measured in the same units `regime_vol_max` is stated in or the two
    gates would disagree for reasons of arithmetic rather than of policy.
    """
    if len(closes) < MIN_CLOSES:
        return []

    series: list[float] = []
    # `realized_vol` takes the last RV_WINDOW + 1 usable closes of whatever it
    # is given, so a prefix slice is a trailing window — and letting it do the
    # unusable-value filtering keeps one definition of "usable close" rather
    # than a second one here that could drift from it.
    for end in range(RV_WINDOW + 1, len(closes) + 1):
        value = realized_vol(closes[:end])
        if value is not None:
            series.append(value)
    return series[-VOL_MEDIAN_WINDOW:]


def _vol_reason(ctx: Mapping[str, Any]) -> str | None:
    """F's first leg: SPY under its 200-day *and* vol above 1.5x its median.

    Both halves, the way `regime._market_state` reads both halves — a market
    below its 200-day in ordinary conditions is where DIP is supposed to work,
    and suppressing entries on the trend alone would be a different rule.
    """
    spy = ((ctx.get("regime_inputs") or {}).get("spy")) or {}
    last, sma200 = _number(spy.get("last")), _number(spy.get("sma200"))
    if last is None or sma200 is None or last >= sma200:
        return None

    closes = spy.get("closes") or []
    series = _rv_series(closes)
    if not series:
        # Named, not silent: the leg abstained, and a reader looking at a
        # risk-off tape with no NO_TRADE_VOL skip should be able to tell
        # "measured and fine" from "could not measure".
        return None

    current, base = series[-1], median(series)
    if base <= 0.0 or current <= VOL_MEDIAN_MULTIPLE * base:
        return None
    return (
        f"SPY {last:.2f} is under its {sma200:.2f} 200-day and 20-day realized "
        f"vol {current:.2f} is over {VOL_MEDIAN_MULTIPLE:g}x its "
        f"{len(series)}-window median of {base:.2f} (§4.3 F)"
    )


def _equity_reason(ctx: Mapping[str, Any]) -> str | None:
    equity = _number((ctx.get("account") or {}).get("equity_total"))
    if equity is not None and equity > 0.0:
        return None
    return (
        "account equity_total is missing, zero or non-numeric; §4.3 F places "
        "nothing against an account it cannot measure"
    )


def _divergence_reason(ctx: Mapping[str, Any]) -> str | None:
    streak = _number((ctx.get("control") or {}).get("divergence_streak")) or 0.0
    if streak < 1.0:
        return None
    return (
        f"the last reconcile left a divergence open ({int(streak)} cycle(s) "
        "running); §4.3 F blocks entries on the first, not the second"
    )


def blockers(ctx: Mapping[str, Any]) -> list[tuple[str, str]]:
    """``[(code, reason)]`` — the account-wide legs of F that fired.

    Empty when F is off, which is every cycle the live account runs today.
    """
    if not enabled(ctx):
        return []

    found = [
        (NO_TRADE_VOL, _vol_reason(ctx)),
        (NO_TRADE_EQUITY, _equity_reason(ctx)),
        (NO_TRADE_DIVERGENCE, _divergence_reason(ctx)),
    ]
    return [(code, reason) for code, reason in found if reason is not None]


def spread_reason(symbol: str, ctx: Mapping[str, Any]) -> str | None:
    """F's per-candidate leg, or None when the name is fine or unmeasurable.

    **Unmeasurable is not blocked.** A candidate with no two-sided quote is
    already refused downstream — `sizing` cannot price a rung without an ask
    and `guard.pricing` costs every order at the far side of the spread — so
    blocking here would only relabel an existing refusal as a §4.3 F
    suppression and inflate the shadow ledger's count of what F cost.
    """
    quote = ((ctx.get("quotes") or {}).get(symbol)) or {}
    bid, ask = _number(quote.get("bid")), _number(quote.get("ask"))
    if bid is None or ask is None:
        return None

    mid = (bid + ask) / 2.0
    if mid <= 0.0:
        return None

    spread = (ask - bid) / mid
    if spread <= MAX_SPREAD_PCT:
        return None
    return (
        f"{symbol}'s {spread:.2%} spread is over §4.3 F's "
        f"{MAX_SPREAD_PCT:.2%} ceiling"
    )
