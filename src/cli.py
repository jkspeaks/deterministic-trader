"""cli.py — plan-fetch, decide, reconcile, report.

Design v3 §8 and §11. The eighth and last source file, and the only one that is
not a pure function of its argument: it reads the six checked-in files out of
the routine's own git checkout, and it is where the other seven are composed
into a cycle.

**It composes; it does not decide.** Every rule this module appears to apply
belongs to a module underneath it — cadence says what kind of cycle this is,
universe says who is inside the fence, plays says what to buy and sell, sizing
says how much, guard says what may be placed. What is genuinely cli's is the
plumbing between them, and there are only four pieces of it:

* **The §5.3 pool.** `universe.resolve` takes triggers and returns the surviving
  symbols; `plays.entries` judges that pool and does not choose it.
* **The book.** `reconcile.py` emits symbol, play, quantity, basis and holding
  period; sizing and guard also need ``market_value``, and §6.6 needs
  ``loss_pct_below_stop``. Neither exists anywhere else.
* **The ordering.** Cancels, then sells, then buys — proceeds from a sale are
  not redeployable under T+1 anyway, and a sell queued behind a buy that failed
  is a position left open for no reason.
* **§14's repeat.** The broker's rejection text only exists *after* placement,
  so this is the only module that can see the same order refused twice.

Invariant 2 still holds. Reading a checked-out file is not network access — an
early probe established the routine sandbox reaches `github.com` and nothing
else, which is precisely why the config and the CI-extracted data arrive as
files rather than as fetches.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from src import broker_shapes
from src import core_sleeve
from src import cadence, candidates as candidate_discovery, dip_recovery, guard, plays
from src import deployment as deployments
from src import manual_check
from src import reconcile as reconciler, regime as regimes, report as reporter
from src.sizing import POLICY_BUYING_POWER_IS_SETTLED, POLICY_CONSERVATIVE
from src import sizing, universe as universes

# One definition of "is this order fractional". §14's repeat key turns on
# whole-versus-fractional and so does `guard._legality_denials`; two answers
# would drift, and the day they did a block key would treat as whole an order
# guard was refusing as fractional.
from src.guard import is_fractional as _is_fractional

EXIT_OK = 0
EXIT_BAD_INPUT = 1
EXIT_USAGE = 2

UNKNOWN_COMMAND = "UNKNOWN_COMMAND"
BAD_INPUT = "BAD_INPUT"

# Recorded on every output so the runlog says where the numbers came from.
CONFIG_SOURCE = "checkout"

# Where a fetch step reads from. `checkout` is an early probe's channel and
# costs no call at all — CI commits its extraction to the repo and the routine
# reads a file, which is why a 530-name universe and a 30-minute EDGAR sweep
# are affordable. Supabase is reserved for state that needs atomic writes:
# §13.1's lock, `edge_control`, and the append-only tables.
ROBINHOOD = "robinhood"
SUPABASE = "supabase"
CHECKOUT = "checkout"

# How wide a step is. §5.3's whole economic argument is the difference between
# the last two: `positions` is bounded by the book and known from
# `edge_control`, `candidates` is not known until `universe.resolve` has run.
ACCOUNT = "account"
POSITIONS = "positions"
CANDIDATES = "candidates"
LISTED = "listed"

SELL = "sell"

# §5.3's per-candidate budget is dominated by `get_equity_technical_indicators`
# returning its default full series — a year of daily bars per (symbol,
# indicator) call against a 200-day SMA that only ever needs its latest value.
# Pinned here so the fetch plan names the parameters and the routine has
# nothing left to choose (§8.1: one decider). Verified live against the broker:
# SMA-200 on MRK returns the identical value whether `start_time` reaches back
# a year or one day — the tool back-extends for warm-up on its own — so a
# short, fixed lookback is exact, not an approximation, and `output: "latest"`
# is the ~24x-smaller response for the same number.
INDICATOR_INTERVAL = "day"
INDICATOR_OUTPUT = "latest"
INDICATOR_LOOKBACK_DAYS = 5

# `get_pnl_trade_history`'s window. The tool takes preset spans only — week,
# month, 3month, ytd, all — so there is no "today" to ask for, and `week` is
# the narrowest. Widening it would not break anything (`reconcile` matches
# rows to this cycle's fills, it does not filter by the window), but every
# extra row is response bytes the session pays for on eleven cycles a day.
REALIZED_SPAN = "week"

# The orders fetch is two bounded calls (see `_fetch_plan`). Robinhood's
# resting GTC orders read back as `confirmed`; the window is calendar days so a
# long weekend or holiday never narrows it below the last session's orders.
#
# **21 days, because a GTC target outlives a week.** A take-profit rests until
# it fills or the position's time stop: DIP's 8 sessions is ~12 calendar days,
# and the bounded loss-hold adds up to 4 more. An order that *fills* after
# aging out of the window is no longer `confirmed` and no longer recent, so
# `reconcile._order_findings` would see a placement `edge_prior` still names
# and a broker that does not list it: one `MISSING_ORDER`, counted toward
# divergence. 21 days clears that with room; measured the account places ~4
# orders a week (~800 characters each), so the window costs ~10k characters
# against a ~60k inline ceiling.
OPEN_ORDER_STATE = "confirmed"
ORDERS_LOOKBACK_DAYS = 21


def _orders_since(trading_day: str) -> str:
    """`created_at_gte` for the recent-orders row: a date, which the tool reads
    as UTC midnight, so the window can only be wider than asked, never narrower."""
    return (date.fromisoformat(trading_day) - timedelta(days=ORDERS_LOOKBACK_DAYS)).isoformat()

# The freshness window for `edge_day_context`. Was two hours, sized to a
# cadence that no longer exists: since the schedule moved to 09:40/11:47/14:47
# ET, monitor cycles land at 11:47 and 14:47 ET, 2h07m and 3h after the prior
# fetch, so a 2h window meant nearly every symbol read back `stale` at every
# monitor cycle regardless of the cache — confirmed against production (then a
# second wave at 19:13 UTC re-fetching the other 27, 5h22m later, entirely
# outside any 2h window). There was never a correctness reason tied to that
# number: what this caches is `INDICATOR_INTERVAL = "day"` bars and daily
# earnings results, and — — the daily-stable half of `fundamentals`
# (`fundamentals_static`: average_volume, sector, market_cap), none of which
# changes intraday. `tradability` and today's `fundamentals` `volume`
# (`fundamentals_live`) do change intraday and are never part of this cache —
# `_fetch_plan` fetches them live. Twelve hours comfortably spans one trading
# day (07:40 premarket to 20:00 overnight, Appendix A) with margin, while
# `edge_day_context_recent(trading_day)` scoping the row to `trading_day`
# already prevents a cross-day leak regardless of this number.
DAY_CONTEXT_MAX_AGE = timedelta(hours=12)

# §14's "same rejection 2 cycles running". Two, because one rejection can be a
# quote that moved between the fetch and the placement — refusing to retry that
# would have the system give up on its first small setback.
PLACEMENT_BLOCKED = "PLACEMENT_BLOCKED"
REPEATED_REJECTIONS = 2

# Reusing `report.py`'s own values rather than restating them, so a block's
# urgency and an alert's urgency cannot come to mean different things.
BLOCKED_EXIT = reporter.PRIORITY_HIGH
BLOCKED_ENTRY = reporter.PRIORITY_DEFAULT

# DIP is the one play `candidates.py` never discovers (its docstring says
# so): the live cycle merges the scanner's RSI survivors into
# `payload["triggers"]` itself, and a harness step that silently skips that
# merge produces zero DIP triggers indistinguishably from a quiet market with
# no oversold names above their 200-day — this counter does not try to tell
# those apart, only to make repeated zeros visible instead of silent.
# `edge_play_state`'s `DIP` row is where it lives, the same row
# `reconcile._play_state_updates` writes `closed_trades` to — but a separate
# `edge_play_state_apply` call, later in the cycle (cycle-prompt.md, after
# `decide`'s runlog write), since `decide` runs after `reconcile` and this is
# the one fact only `decide` has by then.
DIP_SILENT_STREAK_KEY = "silent_entry_cycles"
DIP_SILENT_STREAK = "DIP_SILENT_STREAK"
# Roughly two trading days of entry-permitting cycles (three a day at 09:40/
# 11:40/13:40/15:20 — four, on the current schedule) before this is worth a
# human's attention: long enough that "the market was quiet" stops being the
# likely explanation, short enough that a stuck harness step is caught inside
# a couple of days rather than found by accident weeks later.
DIP_SILENT_STREAK_ALERT_AT = 8


def _dip_silent_streak(
    payload: Mapping[str, Any], triggers: Sequence[Mapping[str, Any]], entries_allowed: bool
) -> int:
    """The new `edge_play_state["DIP"][DIP_SILENT_STREAK_KEY]` value.

    Counts consecutive entry-allowed cycles with zero `DIP`-tagged rows in the
    merged ``triggers`` list. An exit-only cycle asked nothing of the scan, so
    it neither extends nor resets the streak — it simply is not evidence
    either way. Any `DIP` row at all resets it to zero, proposed-and-skipped
    included: the question this answers is "did discovery run", not "did DIP
    enter", and a name the live re-check later refused is still proof the scan
    executed.
    """
    prior = int(
        ((payload.get("play_state") or {}).get(plays.DIP) or {}).get(
            DIP_SILENT_STREAK_KEY
        )
        or 0
    )
    if not entries_allowed:
        return prior
    saw_dip = any(row.get("play_id") == plays.DIP for row in triggers)
    return 0 if saw_dip else prior + 1

# The checkout's own `config/`, resolved from this file rather than from the
# working directory: the routine runs `uv run python -m src.cli` and nothing
# promises where it runs it from.
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

# §11's four operator-maintained files, plus the knobs. Named here rather than
# globbed so a file that fails to arrive in the checkout raises instead of
# quietly not being read.
CONFIG_FILES = {
    "config": "config.json",
    "calendar": "market_holidays.json",
    "etfs": "etfs.json",
    "macro_map": "macro_map.json",
    "inception": "inception.json",
    # The short codes `edge_control.active_sectors` may name — the eleven GICS
    # sectors, every S&P 500 GICS sub-industry, a few baskets.
    "rotation_codes": "rotation_codes.json",
}


# The fractions in config.json that must lie in [0, 1] to mean what their name
# says. Not every numeric key — atr multiples, day counts and epsilons all have
# their own legitimate ranges the modules that read them are better placed to
# police — just the handful whose value silently produces a *different*,
# unnoticed policy the instant it drifts outside a rate/probability's own
# range, rather than crashing or getting caught by a downstream comparison.
_CONFIG_FRACTION_KEYS = (
    "drawdown_halt_pct",
    "cash_reserve_pct",
    "mc_stop_probability_max",
    "mc_drawdown_p95_max",
    "overnight_max_spread_pct",
    # `sector_breakdown_pct` is a fraction of the 200-day; `regime_vol_max` is
    # an annualized standard deviation, which is a rate and belongs here for
    # the same reason — 35 typed for 0.35 would silently make risk_off
    # unreachable and nothing downstream would compare its way into noticing.
    # `sector_rsi_peak` is deliberately absent: RSI is a 0–100 oscillator.
    "sector_breakdown_pct",
    "regime_vol_max",
    # `dip_trend_band_pct` is a fraction of the 200-day SMA (price >= SMA200 x
    # (1 - band)). `3` typed for `0.03` would push the threshold negative and
    # every oversold structural decliner would clear DIP's uptrend gate — the
    # exact silent policy flip this list exists for. `entry_limit_offset_atr`
    # is deliberately absent: it is an ATR multiple, not a rate.
    "dip_trend_band_pct",
    # `watch_band_pct` is the state-change gate's proximity band: a held
    # symbol within this fraction of its resting stop or target wakes a full
    # session. `1` typed for `0.01` would make the gate fire on any quote
    # within 100% of the level — i.e. always — which silently turns every
    # 30-minute tick into a full session and erases the cost savings the gate
    # exists for.
    "watch_band_pct",
    # The posture thresholds. Breadth is a fraction of the sector ETFs, the
    # vol figures are annualized standard deviations in `regime_vol_max`'s
    # units, and the other two are fractional returns. `60` typed for `0.60`
    # would make TRADE_NORMALLY unreachable.
    "posture_normal_breadth_200",
    "posture_high_conviction_breadth_200",
    "posture_high_conviction_breadth_50",
    "posture_no_new_breadth_200",
    "posture_reduced_rv20",
    "posture_high_conviction_rsp_lag_1m",
    "posture_no_new_drawdown_52w",
    "posture_no_new_drawdown_rv20",
)

_SETTLED_CASH_POLICIES = frozenset({POLICY_CONSERVATIVE, POLICY_BUYING_POWER_IS_SETTLED})


def _validate_config(config: Mapping[str, Any]) -> None:
    """Catch a typo before it becomes a silent policy change.

    `settled_basis` (sizing.py) falls back to the strict conservative policy on
    any unrecognized `settled_cash_policy` string, so a typo there is safe but
    silent — it would run for weeks in the stricter mode without complaint. A
    fraction field outside [0, 1] is not self-correcting at all: guard and mc.py
    read it via a plain `float()` and compare it directly, so e.g. `20` typed
    for `drawdown_halt_pct` instead of `0.20` denies (or admits) on a threshold
    nobody chose.
    """
    policy = config.get("settled_cash_policy")
    if policy is not None and policy not in _SETTLED_CASH_POLICIES:
        raise ValueError(
            f"config.json: settled_cash_policy={policy!r} is not one of "
            f"{sorted(_SETTLED_CASH_POLICIES)}"
        )

    # Same argument as the policy string above, one degree worse: only one
    # protective leg may rest (the broker reserves the whole position against
    # the first one), and `protection.rest_kind` falls back to `stop` on an
    # unrecognised value. A typo would therefore run silently in the default
    # posture — which is the safe posture, but not the one the operator typed.
    rest_kind = config.get("protection_rest_kind")
    if rest_kind is not None and rest_kind not in plays.protection.REST_KINDS:
        raise ValueError(
            f"config.json: protection_rest_kind={rest_kind!r} is not one of "
            f"{sorted(plays.protection.REST_KINDS)}"
        )

    # A zero or negative one would not fail loudly: `_positive_int_config`
    # would quietly fall back to the default, so a typo would run for weeks as
    # a policy nobody chose.
    for key in ("dip_survivor_cap", "resolve_max_symbols"):
        if key not in config:
            continue
        value = config[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"config.json: {key}={value!r} must be a positive integer")

    for key in _CONFIG_FRACTION_KEYS:
        if key not in config:
            continue
        value = config[key]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not (0.0 <= value <= 1.0):
            raise ValueError(f"config.json: {key}={value!r} must be a number in [0, 1]")

    _validate_regime_v2(config)
    _validate_dip_regime_table(config)
    _validate_core_sleeve(config)
    for key in plays.SHELVED.values():
        flag = config.get(key)
        if flag is not None and not isinstance(flag, bool):
            # Only an explicit true brings a shelved play back, so a mistyped
            # "true" would leave it off while the file reads as on.
            raise ValueError(f"config.json: {key}={flag!r} must be true or false")
    flag = config.get(plays.DIP_ENTRIES_ENABLED)
    if flag is not None and not isinstance(flag, bool):
        # Only an explicit false stops DIP, so a mistyped "false" or 0 would
        # leave it buying while the file reads as off.
        raise ValueError(
            f"config.json: {plays.DIP_ENTRIES_ENABLED}={flag!r} must be true or false"
        )

    # P2. The string "false" is truthy, and a minimum of 0 would let a
    # one-minute print be scaled 390x; past the session nothing could ever
    # qualify. A budget of 0 or "40" would make every cycle read as over
    # budget from its first await, cutting earnings on all of them.
    budgets = config.get("cycle_budget_min")
    if budgets is not None:
        if not isinstance(budgets, Mapping) or not budgets:
            raise ValueError(
                "config.json: cycle_budget_min must map each cycle type to minutes"
            )
        for cycle_type, minutes in budgets.items():
            if (
                not isinstance(minutes, int) or isinstance(minutes, bool)
                or not (1 <= minutes <= 240)
            ):
                raise ValueError(
                    f"config.json: cycle_budget_min.{cycle_type}={minutes!r} must be "
                    "a whole number of minutes from 1 to 240"
                )

    # P3. The same two failure shapes: a string that is truthy, and a number
    # in the wrong units.
    for switch in (sizing.SIZING_V2_ENABLED, sizing.ONE_PER_SECTOR_DAY):
        flag = config.get(switch)
        if flag is not None and not isinstance(flag, bool):
            raise ValueError(f"config.json: {switch}={flag!r} must be true or false")
    fraction = config.get(sizing.CORR_HOLDINGS_BUDGET_FRACTION)
    if fraction is not None and (
        not isinstance(fraction, (int, float)) or isinstance(fraction, bool)
        or not (0.0 < fraction <= 1.0)
    ):
        raise ValueError(
            f"config.json: {sizing.CORR_HOLDINGS_BUDGET_FRACTION}={fraction!r} must "
            "be a number in (0, 1]"
        )
    for key in (reconciler.REENTRY_BLOCK_DAYS, deployments.DEAD_PLAY_ALERT_DAYS):
        days = config.get(key)
        if days is not None and (
            not isinstance(days, int) or isinstance(days, bool) or days < 1
        ):
            raise ValueError(f"config.json: {key}={days!r} must be a positive integer")

    for switch, minutes_key in (
        (plays.catalyst.RVOL_TIME_NORMALISED, plays.catalyst.MIN_MINUTES),
        (dip_recovery.core.RELVOL_TIME_NORMALISED, dip_recovery.core.RELVOL_MIN_MINUTES),
    ):
        flag = config.get(switch)
        if flag is not None and not isinstance(flag, bool):
            raise ValueError(f"config.json: {switch}={flag!r} must be true or false")
        minimum = config.get(minutes_key)
        if minimum is not None and (
            not isinstance(minimum, (int, float)) or isinstance(minimum, bool)
            or not (1 <= minimum <= plays.SESSION_MINUTES)
        ):
            raise ValueError(
                f"config.json: {minutes_key}={minimum!r} must be a number of "
                f"minutes from 1 to {plays.SESSION_MINUTES:.0f}"
            )


def _validate_core_sleeve(config: Mapping[str, Any]) -> None:
    """The `_15_core_sleeve` group. A cap typed as 50 and not 0.5, or a target
    typed as a negative number, would otherwise size a real order. Loading
    refuses instead.

    **The target must be above zero.** It is the sleeve's one resting order, a
    limit SELL above its buy price. Zero or below would rest a sell at or
    under the buy, which the standing rule forbids for every position.
    """
    flag = config.get(core_sleeve.ENABLED)
    if flag is not None and not isinstance(flag, bool):
        raise ValueError(f"config.json: {core_sleeve.ENABLED}={flag!r} must be true or false")

    def number(key: str) -> float | None:
        value = config.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"config.json: {key}={value!r} must be a number")
        return float(value)

    cap = number(core_sleeve.CAP_PCT)
    if cap is not None and not 0.0 < cap <= 1.0:
        raise ValueError(f"config.json: {core_sleeve.CAP_PCT}={cap!r} must be in (0, 1]")
    buffer = number(core_sleeve.BUFFER_SLOTS)
    if buffer is not None and buffer < 0.0:
        raise ValueError(f"config.json: {core_sleeve.BUFFER_SLOTS}={buffer!r} must not be negative")
    target = number(core_sleeve.TARGET_PCT)
    if target is not None and target <= 0.0:
        raise ValueError(
            f"config.json: {core_sleeve.TARGET_PCT}={target!r} must be above zero; the "
            "sleeve's resting order is a limit sell above its buy price"
        )
    name = config.get(core_sleeve.SYMBOL)
    if name is not None and (not isinstance(name, str) or not name.strip()):
        raise ValueError(f"config.json: {core_sleeve.SYMBOL}={name!r} must be a ticker")
    if flag is True and not name:
        raise ValueError(
            f"config.json: {core_sleeve.ENABLED} is true but {core_sleeve.SYMBOL} is not set"
        )


def _validate_dip_regime_table(config: Mapping[str, Any]) -> None:
    """S3's `_10c_dip_regime_table` group, on the same argument as the posture
    group below: every field is read with a fallback to the P1 rule, so a
    typo would not crash, it would quietly run P1 where the operator thought
    a row had been edited. Loading refuses instead.
    """
    flag = config.get(regimes.DIP_REGIME_TABLE_ENABLED)
    if flag is not None and not isinstance(flag, bool):
        raise ValueError(
            f"config.json: {regimes.DIP_REGIME_TABLE_ENABLED}={flag!r} must be "
            "true or false"
        )

    key = regimes.DIP_REGIME_TABLE
    table = config.get(key)
    if table is None:
        return
    if not isinstance(table, Mapping):
        raise ValueError(f"config.json: {key} must map each posture to a row")

    booleans = (
        regimes.DIP_ALLOWED, regimes.REQUIRE_SECTOR_ABOVE_200D,
        regimes.REQUIRE_SECTOR_RS_UPPER_HALF,
    )
    known = {*booleans, regimes.DIP_SIZE_MULTIPLIER, regimes.MAX_RSI}
    for posture, row in table.items():
        if posture not in regimes.POSTURES:
            raise ValueError(
                f"config.json: {key} names {posture!r}, which is not one of "
                f"{list(regimes.POSTURES)}"
            )
        if not isinstance(row, Mapping):
            raise ValueError(f"config.json: {key}.{posture} must be an object")
        for field, value in row.items():
            where = f"config.json: {key}.{posture}.{field}={value!r}"
            if field not in known:
                raise ValueError(f"{where} is not one of {sorted(known)}")
            if field in booleans and not isinstance(value, bool):
                raise ValueError(f"{where} must be true or false")
            if field == regimes.DIP_SIZE_MULTIPLIER and (
                not isinstance(value, (int, float)) or isinstance(value, bool)
                or not (0.0 <= value <= 1.0)
            ):
                raise ValueError(
                    f"{where} must be a number in [0, 1]; the table can lower "
                    "a size and never raise one"
                )
            if field == regimes.MAX_RSI and value is not None and (
                not isinstance(value, (int, float)) or isinstance(value, bool)
                or not (0.0 < value <= 100.0)
            ):
                raise ValueError(f"{where} must be null or an RSI in (0, 100]")
        if posture == regimes.HOLD_CASH and row.get(regimes.DIP_ALLOWED) is True:
            raise ValueError(
                f"config.json: {key}.{posture}.{regimes.DIP_ALLOWED} must be "
                "false; the risk-off tape opens no position"
            )


def _validate_regime_v2(config: Mapping[str, Any]) -> None:
    """The posture group (`_10b_regime_v2`), on the same argument as above.

    Every key here is read with a default, so a typo would not crash — it
    would run a posture table nobody chose. Three checks beyond "a fraction":
    the switch is a real boolean (the string "false" is truthy), the per-
    posture maps name only real postures, and the three breadth floors are in
    order — `no_new` above `high_conviction` would make HIGH_CONVICTION_ONLY
    unreachable without anyone deciding that.
    """
    flag = config.get(plays.REGIME_V2_ENABLED)
    if flag is not None and not isinstance(flag, bool):
        raise ValueError(
            f"config.json: {plays.REGIME_V2_ENABLED}={flag!r} must be true or false"
        )

    minimum = config.get("regime_breadth_min_sectors")
    if minimum is not None and (
        not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 1
    ):
        raise ValueError(
            f"config.json: regime_breadth_min_sectors={minimum!r} must be a "
            "positive integer"
        )

    for key in (regimes.POSTURE_SIZE_MULTIPLIER, regimes.POSTURE_TARGET_INVESTED_PCT):
        table = config.get(key)
        if table is None:
            continue
        if not isinstance(table, Mapping):
            raise ValueError(f"config.json: {key} must map each posture to a number")
        for posture, value in table.items():
            if posture not in regimes.POSTURES:
                raise ValueError(
                    f"config.json: {key} names {posture!r}, which is not one of "
                    f"{list(regimes.POSTURES)}"
                )
            if (
                not isinstance(value, (int, float)) or isinstance(value, bool)
                or not (0.0 <= value <= 1.0)
            ):
                raise ValueError(
                    f"config.json: {key}.{posture}={value!r} must be a number in [0, 1]"
                )
        for posture in regimes.NO_ENTRY_POSTURES:
            if table.get(posture, 0.0) != 0.0:
                raise ValueError(
                    f"config.json: {key}.{posture} must be 0; that posture opens "
                    "no position"
                )

    floors = [
        config.get(key, regimes.POSTURE_DEFAULTS[key])
        for key in (
            "posture_no_new_breadth_200",
            "posture_high_conviction_breadth_200",
            "posture_normal_breadth_200",
        )
    ]
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in floors):
        if not (floors[0] <= floors[1] <= floors[2]):
            raise ValueError(
                "config.json: the breadth floors must be ordered "
                "posture_no_new_breadth_200 <= posture_high_conviction_breadth_200 "
                f"<= posture_normal_breadth_200, got {floors}"
            )


def _documented(mapping: Mapping[str, Any]) -> dict:
    """Drop the in-band documentation keys.

    All five files carry their reasoning next to the values under an underscore
    prefix, because the operator maintains them by hand and a separate document
    would rot. The prefix is the comment convention, so it is stripped at the
    top level and nowhere else — `macro_map.json` documents each release inline
    with `_why`, and §6.4 puts that reasoning beside the mapping deliberately.
    """
    return {key: value for key, value in mapping.items() if not key.startswith("_")}


def load_config(config_dir: Path | str | None = None) -> dict:
    """The six checked-in files, as one mapping.

    A missing file raises rather than defaulting to ``{}``. §14's first row is
    that something which cannot read its own configuration must not act, and an
    empty calendar would silently make every holiday a trading day.
    """
    directory = Path(config_dir) if config_dir is not None else CONFIG_DIR
    loaded = {
        name: _documented(
            json.loads((directory / filename).read_text(encoding="utf-8"))
        )
        for name, filename in CONFIG_FILES.items()
    }
    _validate_config(loaded["config"])
    return core_sleeve.loaded_with_sleeve(loaded)


def normalized(payload: Mapping[str, Any], loaded: Mapping[str, Any]) -> dict:
    """The payload in this core's vocabulary, whoever assembled it.

    The first line of every command that reads a broker response. Before this,
    each key was renamed wherever it happened to be read — `quotes` in
    `_quotes`, lots in `_tax_lots`, the account only on the hosted path — so a
    key nobody had yet been bitten by had no renamer at all, and `orders` was
    the one that had not. It cost five blocked cycles and three unplaceable
    exits. `src/broker_shapes.py` is the single
    pass; this wrapper is only the part of it that cannot be pure.

    **The account merge is that part.** `shape_account` needs the trading day
    and `config/inception.json`, so it takes `loaded` — but it only ever runs
    on an `account` that is still `get_portfolio`'s own response, which is the
    hosted path's shape and, the shape a live session should be posting rather
    than hand-merging. An already-merged account is left exactly as it arrived
    (this is the path that stops the merge being anyone's to get wrong).
    """
    payload = broker_shapes.normalize(payload, reconciler.calendar_day)
    if broker_shapes.account_is_raw(payload):
        shaped = shape_account(payload, loaded)
        payload["account"] = shaped["account"]
        payload["account_problems"] = shaped["problems"]
    return payload


def _cycle(payload: Mapping[str, Any], loaded: Mapping[str, Any]) -> cadence.Cycle:
    """This firing's cycle, derived rather than accepted (Invariant 6).

    No command takes a cycle on stdin. A cycle dict arriving from the routine
    is prompt text, and a prompt that could claim ``entries_allowed`` on a
    post-close would reduce Invariant 3's fence to an honour system. Cadence is
    a pure function of the clock, ``edge_control`` and a checked-in calendar,
    so deriving it four times costs nothing and can never disagree with itself.
    """
    return cadence.decide(
        {
            "now": payload["now"],
            "control": payload.get("control") or {},
            "calendar": loaded["calendar"],
            "config": loaded["config"],
        }
    )


def cycle_of(payload: Mapping[str, Any], loaded: Mapping[str, Any]) -> cadence.Cycle:
    """`_cycle`, for the one caller outside this module.

    `services/core/core/run.py._payload` has to know the trading day and
    whether entries are allowed *before* it can decide which Supabase reads to
    make — the reads that used to be fetch-plan rows a session executed
    . Deriving the cycle a second time there costs nothing (it is
    a pure function of the clock, `edge_control` and the calendar) and is the
    same derivation `plan_fetch` makes a moment later, so the two cannot
    disagree. Exported rather than duplicated for exactly that reason;
    Invariant 6 is that nobody *accepts* a cycle, not that nobody may ask.
    """
    return _cycle(payload, loaded)


def _envelope(payload: Mapping[str, Any], cycle: cadence.Cycle) -> dict:
    """What every command says regardless of what it did.

    ``cycle_id`` is echoed, never minted. §13.1's lock is claimed before any of
    this runs and the id is the value it was claimed with; a second one
    generated here would leave the lock and the runlog naming different cycles.
    """
    return {
        "cycle_id": payload.get("cycle_id"),
        "cycle": cycle.as_dict(),
        "config_source": CONFIG_SOURCE,
    }


def _refusal(command: str | None, code: str, detail: str) -> dict:
    """A refusal, still shaped like everything else.

    Invariant 3 has the routine write stdout to `runlog` before it places
    anything, so stdout is JSON even when the input was not. The detail is the
    parser's own sentence: §14's rule about broker text is that the message
    naming the byte offset is what identifies a truncated payload, and a bare
    code identifies nothing.
    """
    return {"command": command, "error": {"code": code, "detail": detail}}


def _step(step: str, source: str, tool: str, scope: str, **extra: Any) -> dict:
    """One line of the fetch list. Source, tool and scope are all named.

    §8.1: there is exactly one decider. A routine left to infer which tool
    serves a named step is a second one, and the three defects this design
    keeps citing all began as a capability reimplemented somewhere it already
    worked.
    """
    return {"step": step, "source": source, "tool": tool, "scope": scope, **extra}


def _indicator_start(now: str) -> str:
    """`start_time` for every indicator call this cycle makes.

    A fixed, short lookback rather than a per-indicator warm-up calculation:
    the tool computes over whatever range its own warm-up needs and only uses
    `start_time` to place the output window, so any date on or after the most
    recent session is exact. `INDICATOR_LOOKBACK_DAYS` just has to clear a
    weekend or a holiday, not a 200-day moving average.
    """
    moment = datetime.fromisoformat(now.replace("Z", "+00:00"))
    start = moment - timedelta(days=INDICATOR_LOOKBACK_DAYS)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ")


def _symbol_fresh(cycle: cadence.Cycle, entry: Mapping[str, Any] | None) -> bool:
    """Whether one symbol's own cached `{"signals", "earnings", "fetched_at"}`
    entry inside `edge_day_context.market_data` is still usable.

    Freshness moved from the whole day's row (the original `_day_context_fresh`)
    to each symbol individually, because the row-level gate was all-or-nothing in
    a way that turned out costly: a live fetch that died partway through a
    ~20-candidate loop (hitting a session/spend limit before reaching `decide()`)
    left the row's single `fetched_at` unset, so nothing was ever usable and
    every later cycle repeated the identical full fetch from zero. Per-symbol
    writes (`edge_day_context_write_symbol`)
    mean a partial fetch now leaves partial, genuinely usable coverage — this is
    what reads that coverage back.

    No `pending_events`/`trading_day` cross-check here (the old function's
    other two conditions): those were properties of the *whole cached snapshot*
    being from today and undisturbed by a same-day event. A per-symbol
    `fetched_at` inside today's `trading_day` row already implies both — the
    row itself is keyed by `trading_day`, and `day-context-plan` is only ever
    called against the current day's row (see that command below).

    **`signals.rsi` and `earnings` must both be present.** `fresh` promises both
    halves — a caller skips the live `signals` *and* `earnings` fetch for a fresh
    symbol. Reading that as `fresh` would let the runner skip the earnings fetch
    for every held name — blinding guard's T-2 window and the earnings-deadline
    exit. So such an entry is `stale` (earnings still fetched, then written back
    beside the cached RSI) and `sma_fresh` in `day_context_plan` (the moving
    averages need no re-fetch).
    """
    if not isinstance(entry, Mapping):
        return False
    if not _age_fresh(cycle, entry.get("fetched_at")):
        return False
    if entry.get("earnings") is None:
        return False
    signals = entry.get("signals")
    return isinstance(signals, Mapping) and signals.get("rsi") is not None


def _age_fresh(cycle: cadence.Cycle, fetched_at: str | None) -> bool:
    """A single `fetched_at` stamp against `DAY_CONTEXT_MAX_AGE`. An absent or
    unparseable stamp is stale, never fresh."""
    if not fetched_at:
        return False
    now = datetime.fromisoformat(cycle.now.replace("Z", "+00:00"))
    fetched = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    return now - fetched < DAY_CONTEXT_MAX_AGE


def _statics_fresh(cycle: cadence.Cycle, entry: Mapping[str, Any] | None) -> bool:
    """Whether one symbol's cached `fundamentals_static`
    (`{average_volume, sector, market_cap, fetched_at}`) is still usable.

    Judged on its own `fetched_at`, independently of the `signals`/`earnings`
    stamp: `average_volume`, `sector` and `market_cap` are daily-stable, so a
    symbol can be `stale` for `signals` (which do move intraday) while its
    statics are still good — and a monitor cycle that only needs a fresh RSI
    should not also re-pull the full `get_equity_fundamentals` payload for a
    figure that has not changed since the morning.
    """
    if not isinstance(entry, Mapping):
        return False
    statics = entry.get("fundamentals_static")
    if not isinstance(statics, Mapping):
        return False
    return _age_fresh(cycle, statics.get("fetched_at"))


def _day_context_fresh_symbols(
    cycle: cadence.Cycle, day_context: Mapping[str, Any], symbols: Sequence[str]
) -> tuple[list[str], list[str]]:
    """Split a resolved candidate/position symbol list into `(fresh, stale)`
    against `day_context` (the raw `market_data` blob `edge_day_context_recent`
    returns). `stale` covers both "never cached" and "cached but too old" —
    the caller does not need to tell those apart, both mean "fetch it live."

    Order is preserved and each input symbol appears in exactly one list, so
    a caller can zip this back against the candidate order it already has.
    """
    fresh, stale = [], []
    for symbol in symbols:
        (fresh if _symbol_fresh(cycle, day_context.get(symbol)) else stale).append(symbol)
    return fresh, stale


def _day_context_statics_split(
    cycle: cadence.Cycle, day_context: Mapping[str, Any], symbols: Sequence[str]
) -> tuple[list[str], list[str]]:
    """The `fundamentals_static` equivalent of `_day_context_fresh_symbols`,
    judged on its own stamp so a symbol can be `stale` for `signals` yet
    `statics_fresh` — a monitor cycle then pulls a fresh RSI for it without
    re-pulling `get_equity_fundamentals` for `average_volume`/`sector`/
    `market_cap`, none of which move intraday.
    """
    fresh, stale = [], []
    for symbol in symbols:
        (fresh if _statics_fresh(cycle, day_context.get(symbol)) else stale).append(symbol)
    return fresh, stale


_SMA_KEYS = ("sma_20", "sma_50", "sma_200")


def _sma_fresh(cycle: cadence.Cycle, entry: Mapping[str, Any] | None) -> bool:
    """Whether one symbol's cached `signals` already carry all three moving
    averages, fresh.

    The pre-market data job (`services/core/core/premarket.py`) writes
    `signals.sma_20/50/200` + `bars_meta` — and, daily `rsi`/`atr` — for
    every held name, Dip Recovery watchlist name and PEAD name before the
    open, from Twelve Data bars. It writes no earnings, so such a symbol
    still lands in `stale` (`_symbol_fresh` needs earnings too) while
    `sma_fresh` says its moving averages need no re-fetch.

    Judged on the same `fetched_at` stamp and `DAY_CONTEXT_MAX_AGE` window as
    `_symbol_fresh`; the pre-market write at ~06:30 ET is inside that window
    for every session slot except the 18:47 post-close (which has no decide).
    """
    if not isinstance(entry, Mapping):
        return False
    if not _age_fresh(cycle, entry.get("fetched_at")):
        return False
    signals = entry.get("signals")
    if not isinstance(signals, Mapping):
        return False
    return all(signals.get(key) is not None for key in _SMA_KEYS)


def _day_context_sma_split(
    cycle: cadence.Cycle, day_context: Mapping[str, Any], symbols: Sequence[str]
) -> tuple[list[str], list[str]]:
    fresh, stale = [], []
    for symbol in symbols:
        (fresh if _sma_fresh(cycle, day_context.get(symbol)) else stale).append(symbol)
    return fresh, stale


def day_context_plan(payload: Mapping[str, Any], loaded: Mapping[str, Any]) -> dict:
    """Step 5's fresh/stale split, run once the candidate set is resolved.

    Cannot live inside `plan_fetch`/`_fetch_plan`: those run *before*
    `universe.resolve`, so the candidate set does not exist yet (`_calls`'s
    own docstring already names this exact ordering constraint for why a
    CANDIDATES-scope step's call count is unknowable at that point). This
    command is the one place that constraint is lifted, because by the time
    the routine calls it (Step 5, after `candidates.json` merges DIP's scanner
    survivors into `triggers`) the symbol list is finally known.

    `payload["symbols"]` is the resolved candidate (or position) list the
    routine is about to need `signals`/`earnings` for. `payload["day_context"]`
    is whatever `edge_day_context_recent` already returned this cycle — this
    command does not call Supabase itself, matching every other cli.py command
    (it shapes data the routine already fetched; it does not fetch on its own).

    Returns three independent splits: `fresh`/`stale` for `signals`/`earnings`
    (which move intraday), `statics_fresh`/`statics_stale` for
    `fundamentals_static` (`average_volume`/`sector`/`market_cap`, which do
    not), and `sma_fresh` for symbols whose cached `signals` already carry
    `sma_20/50/200` from the pre-market data job. A `sma_fresh` symbol is
    still in `stale` — its RSI and ATR are not pre-computed and must be
    fetched — but a caller may use `sma_fresh` to skip re-fetching the moving
    averages. A symbol routinely lands in `stale`, `statics_fresh` and
    `sma_fresh` at once on a monitor cycle: a fresh RSI is needed, the
    morning's average volume and moving averages are not.
    """
    cycle = _cycle(payload, loaded)
    symbols = payload.get("symbols") or []
    day_context = payload.get("day_context") or {}
    fresh, stale = _day_context_fresh_symbols(cycle, day_context, symbols)
    statics_fresh, statics_stale = _day_context_statics_split(cycle, day_context, symbols)
    sma_fresh, _ = _day_context_sma_split(cycle, day_context, symbols)
    return {
        **_envelope(payload, cycle),
        "fresh": fresh,
        "stale": stale,
        "statics_fresh": statics_fresh,
        "statics_stale": statics_stale,
        "sma_fresh": sma_fresh,
    }


def day_context_write_symbol(payload: Mapping[str, Any], loaded: Mapping[str, Any]) -> dict:
    """Shapes one just-fetched candidate's data for
    `edge_day_context_write_symbol(p_trading_day, p_symbol, p_symbol_data)` — this command does not call
    Supabase itself; the routine does, with this output as `p_symbol_data`,
    immediately after each stale candidate's live fetch completes (not
    batched to the end of Step 6's loop), so a session that dies partway
    through has already persisted every candidate it got to.

    `payload` carries `symbol` plus this cycle's already-accumulated
    `signals`/`earnings`/`fundamentals` — the same multi-symbol, keyed-by-symbol
    dicts `decide()` itself reads (`payload.get("signals")`), not a one-off
    shape invented for this command. The routine does not need to build a
    special payload just for this call: it passes the same `cycle.json` it has
    been building all cycle, naming which symbol to shape right now. Missing
    `earnings` for that symbol defaults to `{}` rather than being required,
    since a POSITIONS-scope or exit-only cycle's per-symbol fetch may carry
    only `signals`.

    `fundamentals_static` (`average_volume`/`sector`/`market_cap`, with its own
    `fetched_at`) is added **only when** this cycle actually fetched
    `get_equity_fundamentals` for the symbol — a monitor cycle that skipped the
    call because the statics were already `statics_fresh` writes `signals`/
    `earnings` alone, and `edge_day_context_write_symbol` shallow-
    merges that write so the cached statics survive untouched. An empty statics
    block is never written: that would read back as "fresh but null".
    """
    cycle = _cycle(payload, loaded)
    symbol = payload.get("symbol")
    symbol_data = {
        "signals": (payload.get("signals") or {}).get(symbol) or {},
        "earnings": (payload.get("earnings") or {}).get(symbol) or {},
        "fetched_at": cycle.now,
    }

    fundamentals = (payload.get("fundamentals") or {}).get(symbol)
    if fundamentals:
        symbol_data["fundamentals_static"] = {
            "average_volume": fundamentals.get("average_volume"),
            "sector": fundamentals.get("sector"),
            "market_cap": fundamentals.get("market_cap"),
            "fetched_at": cycle.now,
        }

    return {
        **_envelope(payload, cycle),
        "trading_day": cycle.trading_day,
        "symbol": symbol,
        "symbol_data": symbol_data,
    }


def _fetch_plan(
    cycle: cadence.Cycle, loaded: Mapping[str, Any], control: Mapping[str, Any]
) -> list[dict]:
    """Exactly what this cycle type needs, and nothing it cannot use.

    `control` is accepted but no longer read here — day-context freshness
    moved per-symbol (`_day_context_fresh_symbols`, `day_context_plan`) and no
    longer depends on `edge_control.last_data_fresh_at`, the row-level stamp
    this function used to check. Kept as a parameter rather than removed to
    avoid rippling the change through every caller and test that already
    passes three arguments; `plan_fetch` still reads it for `positions_held`.

    §13's economics live here. A no-op returns an empty list — `edge_control`
    has already been read by the time this runs, so there is genuinely nothing
    left to do, and one Robinhood step here would turn seven cheap invocations
    into seven MCP sessions.

    Scope is the other half. §5.3: universe size is free and candidate-set size
    costs a call per symbol per cycle, so the per-symbol steps widen to
    ``candidates`` only on a cycle that may actually buy. On a cycle that may
    only sell they stay at ``positions``, and `plays.py` degrades exactly the
    way its own docstring promises — a name whose data never arrived does not
    trigger.
    """
    if cycle.cycle_type == cadence.NOOP:
        return []

    # Every live cycle. `runlog` carries last cycle's placements for §12.2 and its rejections for
    # §14's repeat; the two account reads are what `sizing.settled_basis` needs to establish
    # Invariant 1's basis at all. **`edge_prior` and `edge_play_state_json`, not `edge_runlog` and
    # `edge_play_state`.** The latter two are table names, and this list has always named them.
    # Nothing noticed while the fetch plan was only ever read by a session that knew the real RPCs —
    # and then 21 rows of it were copied into `edge_cycle_schedule` by hand, handed to a runner told
    # to "run every row with the tool it names", and an earlier post-close cycle called two
    # functions that do not exist. The names are the RPCs now; `src/broker_tools.py` is the fence
    # that stops the next one being published at all.
    steps = [
        _step("runlog", SUPABASE, "edge_prior", ACCOUNT),
        _step("play_state", SUPABASE, "edge_play_state_json", ACCOUNT),
        _step("account", ROBINHOOD, "get_portfolio", ACCOUNT),
        _step("balances", ROBINHOOD, "get_accounts", ACCOUNT),
        _step("positions", ROBINHOOD, "get_equity_positions", ACCOUNT),
    ]

    if cycle.reconcile_required:
        # §12.2's diff needs the broker's own order list and the executions on
        # it; §12.1 runs it on full, monitor and post-close and nowhere else.
        # Tax lots are the sole source of `entry_price` and `sessions_held` and
        # of §9's realised basis.
        #
        # **Two bounded rows, never the bare call.** An unfiltered
        # `get_equity_orders` is the account's whole history, one full page
        #  that the harness spills to a file the
        # runner may not read back, which aborts the cycle in Step 2. Reconcile needs exactly
        # two things from the list, and neither is "everything":
        #   * what is *open*, of any age — a GTC target placed last week still
        #     rests, and a date window alone hides it (`MISSING_ORDER`, a
        #     duplicate target the broker rejects for lack of shares);
        #   * what happened *recently* — terminal orders, for fill quality and
        #     for the placements `edge_prior` still names.
        # `state` takes one value, so "open" is the resting state; anything
        # working that is newer than the window arrives through the second row.
        # Both rows post as kind `orders`; `assemble` concatenates and dedupes.
        steps.append(
            _step("orders", ROBINHOOD, "get_equity_orders", ACCOUNT, state=OPEN_ORDER_STATE)
        )
        steps.append(
            _step(
                "orders",
                ROBINHOOD,
                "get_equity_orders",
                ACCOUNT,
                created_at_gte=_orders_since(cycle.trading_day),
            )
        )
        steps.append(_step("tax_lots", ROBINHOOD, "get_equity_tax_lots", POSITIONS))
        # ...and §9's realised result, which tax lots cannot supply. They are
        # *open* lots: a position sold to zero has none, so every full close
        # arrived with a null basis and the ledger banned repurchase of names
        # closed at a profit. `span` is the narrowest window the tool offers
        # — it has preset spans only, no date range — and `reconcile` matches
        # rows to this cycle's fills rather than trusting the window, so a
        # wider one costs nothing but bytes.
        steps.append(
            _step(
                "realized",
                ROBINHOOD,
                "get_pnl_trade_history",
                ACCOUNT,
                span=REALIZED_SPAN,
            )
        )
        # §9's ledger. A buy's wash-sale check reads it, and so — — does
        # `reconcile._anchored_entry`: the broker rolls a wash-sale disallowed
        # loss into the replacement lot's `tax_cost_basis`, and without the
        # ledger in hand that legitimate gap reads as a disputed entry and
        # strips the position's stop management. Fetched on entry cycles
        # only until then, which let every monitor and post-close cycle
        # re-dispute it.
        steps.append(_step("loss_closes", SUPABASE, "edge_loss_closes_recent", ACCOUNT))
    elif cycle.exits_allowed:
        # **An exit needs the resting orders it must cancel first.** The
        # overnight cycle does not reconcile, so it fetched no `orders` at all:
        # `payload["open_orders"]` was empty, `_protection_orders` planned no
        # cancel, and guard denied RTX's TIME_STOP as PROTECTION_CONTENDS (cycle
        # 00c11c9e) — the replay that said otherwise had hand-supplied the
        # orders the runner never posted. Only the resting row: terminal orders
        # feed reconcile's fills, which this cycle skips.
        steps.append(
            _step("orders", ROBINHOOD, "get_equity_orders", ACCOUNT, state=OPEN_ORDER_STATE)
        )

    if cycle.cycle_type == cadence.POSTCLOSE:
        # §8.4's row and nothing else. The frozen book is named symbol by
        # symbol rather than left to the prompt: §1's control is a promise
        # about one specific inception book, and a routine that priced eleven
        # of twelve names would produce a cheaper control on exactly the days
        # the data is worst.
        steps.append(
            _step(
                "quotes",
                ROBINHOOD,
                "get_equity_quotes",
                LISTED,
                symbols=sorted({*(loaded["inception"].get("inventory") or {}),
                                reporter.SPY}),
            )
        )
        return steps

    if cycle.cycle_type == cadence.PREMARKET:
        # §13: "set the day's regime and live plays; place nothing." Quotes and
        # the macro file are the regime read; tradability and earnings gate an
        # order, and this cycle has none to gate.
        steps.append(_step("quotes", ROBINHOOD, "get_equity_quotes", POSITIONS))
        steps.append(_step("universe", CHECKOUT, "data/universe.json", ACCOUNT))
        steps.append(_step("macro", CHECKOUT, "data/macro.json", ACCOUNT))
        steps.append(_step("events", CHECKOUT, "data/events.json", ACCOUNT))
        return steps

    scope = CANDIDATES if cycle.entries_allowed else POSITIONS

    # `day_context` is always read now — cheap (one Supabase call) regardless
    # of how much of it turns out usable — and `indicators`/`earnings` are
    # always listed too, rather than a row-level `cached` flag choosing
    # between them the way this used to work. The candidate set is not known
    # yet here (`_calls`'s own docstring on why CANDIDATES-scope cost is
    # unknowable at this point), so this list cannot say how many of
    # `indicators`/`earnings`'s calls survive; the routine narrows both to
    # just `day-context-plan`'s `stale` symbols once Step 5 resolves the
    # candidate set — see that command's docstring for why the split has to
    # happen there and not here. A symbol whose cache entry is fresh costs
    # nothing beyond the one `day_context` read already paid for.
    steps.append(_step("quotes", ROBINHOOD, "get_equity_quotes", scope))
    steps.append(
        _step("day_context", SUPABASE, "edge_day_context_recent", ACCOUNT,
              trading_day=cycle.trading_day)
    )
    steps.append(
        _step(
            "indicators",
            ROBINHOOD,
            "get_equity_technical_indicators",
            scope,
            interval=INDICATOR_INTERVAL,
            output=INDICATOR_OUTPUT,
            start_time=_indicator_start(cycle.now),
        )
    )
    steps.append(_step("tradability", ROBINHOOD, "get_equity_tradability", scope))
    # Per symbol, not the market-wide calendar. `get_earnings_calendar` is a
    # <=31-day window over every name that reports in it, so it cannot answer
    # "when does F next report" for a name three months out — which is most
    # of the book, most of the time. This step was scoped per-symbol against
    # a market-wide tool from the start.
    steps.append(_step("earnings", ROBINHOOD, "get_earnings_results", scope))
    steps.append(_step("events", CHECKOUT, "data/events.json", ACCOUNT))

    if cycle.entries_allowed:
        # Both exist to admit a buy. §5.1's snapshot is the fence and §6.4's
        # releases are the only MACRO trigger — neither can change an exit.
        # §9's ledger used to sit here too; it moved below.
        steps.extend(
            [
                # §6.0's regime, deterministic. A fixed twelve symbols — SPY
                # plus §5.2's eleven sector ETFs — so this row costs the same
                # on every cycle regardless of how wide the candidate set turns
                # out to be, and is `listed` rather than `candidates` for
                # exactly that reason. Entry cycles only: `src/regime.py` gates
                # entries and nothing else, and an exit-only cycle has none to
                # gate.
                #
                # **Executed in Step 5, not Step 6** — the same pull-forward
                # `day_context` already gets, and for a sharper reason: its
                # answer removes whole sectors from the candidate set before
                # the per-symbol fetch loop pays for them.
                _step(
                    "regime",
                    ROBINHOOD,
                    "get_equity_technical_indicators",
                    LISTED,
                    symbols=sorted({*(loaded["etfs"].get("sector") or {}), reporter.SPY}),
                    interval=INDICATOR_INTERVAL,
                    output=INDICATOR_OUTPUT,
                    start_time=_indicator_start(cycle.now),
                ),
                _step("universe", CHECKOUT, "data/universe.json", ACCOUNT),
                _step("macro", CHECKOUT, "data/macro.json", ACCOUNT),
                # `fundamentals_live` — today's `volume` only, and only for
                # §6.3's CATALYST volume confirmation (its real volume/change
                # read). An 08:45 snapshot would have CATALYST comparing this
                # cycle's price action against this *morning's* volume — a
                # different gate, not a cheaper version of the same one — so
                # this stays a live call. The prompt narrows it to the
                # CATALYST candidates that actually reached plays (after
                # `candidates` and the regime sector filter); DIP is skipped
                # because `plays/dip.py` reads no relative-volume figure.
                # `average_volume`/`sector`/`market_cap` are daily- stable and
                # come from `day_context`'s `fundamentals_static` instead
                # (`_statics_fresh`, `day_context_plan`).
                _step("fundamentals_live", ROBINHOOD, "get_equity_fundamentals", CANDIDATES),
                # §6.1's `reaction_pct` — computed by the separate, lean reaction routine, not
                # here. This is a cheap read of what it has already found, same shape and same
                # reason as `loss_closes`. PEAD never proposes an exit either, so this belongs
                # beside `fundamentals`, not beside `indicators`.
                _step(
                    "earnings_reactions", SUPABASE, "edge_earnings_reactions_recent",
                    ACCOUNT,
                ),
            ]
        )

    if cycle.entries_allowed and not cycle.reconcile_required:
        # §9's ledger for a buy's wash-sale check, on the (currently
        # hypothetical) entry cycle that does not reconcile — every one that
        # does already fetched it above.
        steps.append(_step("loss_closes", SUPABASE, "edge_loss_closes_recent", ACCOUNT))

    return steps


def _calls(step: Mapping[str, Any], positions_held: int) -> int | None:
    """What this step costs, or ``None`` when it genuinely cannot be known.

    A candidate-scoped step is unknowable here: the candidate set does not
    exist until `universe.resolve` has run, and that cannot run until
    `edge_events` has been read — which is one of the things this list is
    telling the routine to read. An invented figure would be a number nobody
    could act on, and §7.4's arithmetic deserves better than a guess.
    """
    if step["source"] == CHECKOUT:
        return 0
    if step["scope"] == POSITIONS:
        return positions_held
    if step["scope"] == LISTED:
        return len(step["symbols"])
    if step["scope"] == CANDIDATES:
        return None
    return 1


def plan_fetch(payload: Mapping[str, Any], loaded: Mapping[str, Any]) -> dict:
    """What kind of cycle this is, and exactly what to read for it (§13)."""
    payload = normalized(payload, loaded)
    cycle = _cycle(payload, loaded)
    control = payload.get("control") or {}
    held = int(control.get("positions_held") or 0)

    fetch = [{**step, "calls": _calls(step, held)} for step in _fetch_plan(cycle, loaded, control)]

    return {
        **_envelope(payload, cycle),
        "fetch": fetch,
        "estimate": {
            "calls": sum(step["calls"] for step in fetch if step["calls"] is not None),
            "per_candidate_steps": sum(
                1 for step in fetch if step["scope"] == CANDIDATES
            ),
            "positions_held": held,
        },
        "reason": cycle.reason,
    }


def _triggers(payload: Mapping[str, Any]) -> tuple[list[dict], list[str]]:
    """Split what the plays triggered on into universe's two doors.

    §6.4 expresses macro through the sector sleeve and nothing else, and
    `universe.resolve` checks a macro target against the eleven-name rotation
    set rather than against index membership. Sending a MACRO row through
    ``triggers`` would ask the S&P holdings file a question about XLE that it
    cannot answer, and the name would be refused as a non-member.
    """
    rows = payload.get("triggers") or []
    macro = [r["symbol"] for r in rows if r.get("play_id") == plays.MACRO]
    return [r for r in rows if r.get("play_id") != plays.MACRO], macro


def _carried(prior: Mapping[str, Any], cycle: cadence.Cycle, calendar) -> list[dict]:
    """Last cycle's reconciled book, aged forward to this trading day.

    §12.1 runs reconciliation on full, monitor and post-close cycles only, so
    the 04:30 and 20:00 exits have no fresh book and would otherwise have
    nothing to exit against. Carrying it unchanged is not an option either: a
    book that never ages holds every position one session past §6.5's limit,
    every night.

    The count is `reconcile.sessions_between`, reused rather than
    reimplemented. Two holding-period rules would fire a time stop a day early
    in one module and a day late in the other, and a settled design decision
    put this arithmetic in one place precisely so that cannot happen.
    """
    aged = reconciler.sessions_between(
        date.fromisoformat(prior["trading_day"]),
        date.fromisoformat(cycle.trading_day),
        calendar,
    )
    return [
        {
            **position,
            "sessions_held": (
                None
                if position.get("sessions_held") is None
                else int(position["sessions_held"]) + aged
            ),
        }
        for position in prior.get("positions") or []
    ]


# --- earnings ---------------------------------------------------------------
#
# `guard._earnings_denials` and `plays._earnings_veto` read `status`, `sessions_until_report`,
# `sessions_since_report`, `eps_surprise_pct` and `reaction_pct`. Every entry was vetoed
# EARNINGS_DATE_UNAVAILABLE on every cycle. The routine reported it itself.
#
# Deriving here rather than in the prompt is absolute 2 — the routine copies the
# broker's answer verbatim and computes nothing. Deriving here rather than in
# `plays.py` or `guard.py` is the same reason `_carried` lives here: it is a
# question about the calendar, and `cadence.sessions_between` is the one place
# allowed to answer it, so a holiday week cannot mean two different things to
# two modules.
#
# `reaction_pct` is no longer always None. It is the report-day price move, which this cycle's
# own fetch steps still cannot carry — it is a lookback to a specific past date, not "latest" —
# so it arrives pre-computed from `edge_earnings_reactions`, written by a separate, lean
# routine that is the only thing with a reason to run `get_equity_historicals` at all. Absent a
# matching row (the reaction routine has not run yet for this report, or PEAD's window has
# already closed), it stays None and `_pead` proposes nothing — the same fail-closed behaviour
# as before, just no longer permanent.

_EARNINGS_UNKNOWN: dict[str, Any] = {
    "status": "unknown",
    "next_report_date": None,
    "verified": None,
    "sessions_until_report": None,
    "last_report_date": None,
    "sessions_since_report": None,
    "eps_surprise_pct": None,
    "reaction_pct": None,
    # Copied verbatim, never inferred — the reaction routine needs it to know whether a report
    # is digested the same session (`am`) or the next one (`pm`), and nothing here is positioned
    # to guess a market's reaction timing from a code.
    "timing": None,
}


def _quarters(raw: Any) -> list[Mapping[str, Any]]:
    """The quarter rows, out of whatever envelope the routine copied.

    `get_earnings_results` returns ``{"data": {"results": [...]}}``. A prompt
    that pasted one level in or one level out is not a reason to lose the book,
    so all three depths are accepted; anything else yields no quarters and the
    symbol reads ``unknown``, which vetoes. §14: a field that cannot be read is
    not a field that may be guessed.
    """
    if isinstance(raw, Mapping):
        raw = raw.get("data", raw)
    if isinstance(raw, Mapping):
        raw = raw.get("results")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    return [q for q in raw if isinstance(q, Mapping)]


def _reported(quarter: Mapping[str, Any]) -> bool:
    """Whether the quarter has been delivered, judged on ``eps.actual``.

    The broker's own guide says this is the discriminator, and the date cannot
    substitute for it: an unverified forward date drifts, and a company that
    reports late would otherwise read as already-reported on the day its
    estimate was still the only number available.
    """
    return (quarter.get("eps") or {}).get("actual") not in (None, "")


def _report_date(quarter: Mapping[str, Any]) -> date | None:
    value = (quarter.get("report") or {}).get("date")
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _eps_surprise_pct(quarter: Mapping[str, Any]) -> float | None:
    """Percent beat or miss. `_pead` scales conviction off this.

    A zero estimate has no percentage — dividing would emit an infinity that
    reads as an enormous beat, which is exactly the number that would size a
    position.
    """
    eps = quarter.get("eps") or {}
    try:
        estimate = float(eps["estimate"])
        actual = float(eps["actual"])
    except (KeyError, TypeError, ValueError):
        return None
    if estimate == 0:
        return None
    return (actual - estimate) / abs(estimate) * 100.0


def _earnings_view(
    symbol: str,
    raw: Any,
    trading_day: date,
    calendar: Mapping[str, Any],
    reactions: Mapping[tuple[str, str], float] | None = None,
) -> dict[str, Any]:
    """One symbol's quarters, reduced to what guard and plays actually read.

    **An unverified forward date still counts as known.** Companies confirm a
    quarter's date weeks ahead, so requiring `verified` would leave most names
    unknown most of the time and veto every entry — the very defect this
    repairs. The tentative date is carried through and `verified` alongside it,
    and the conservatism lands where it belongs: guard vetoes anything inside
    `earnings_flatten_days` regardless, and by T-2 the date is confirmed.

    A report date that has passed with no actual reads as zero sessions away,
    not a negative number — `sessions_between` counts forward only. An overdue
    print is therefore treated as imminent, which is the safe reading.
    """
    quarters = _quarters(raw)
    dated = [(day, q) for q in quarters if (day := _report_date(q)) is not None]

    # Keyed on the date alone: two quarters sharing one date would otherwise
    # fall through to comparing the dicts, which raises.
    upcoming = sorted(
        (p for p in dated if not _reported(p[1])), key=lambda pair: pair[0]
    )
    delivered = sorted(
        (p for p in dated if _reported(p[1])), key=lambda pair: pair[0]
    )

    entry = dict(_EARNINGS_UNKNOWN)

    if upcoming:
        day, quarter = upcoming[0]
        entry.update(
            {
                "status": "known",
                "next_report_date": day.isoformat(),
                "verified": bool((quarter.get("report") or {}).get("verified")),
                "sessions_until_report": cadence.sessions_between(
                    trading_day, day, calendar
                ),
            }
        )

    if delivered:
        day, quarter = delivered[-1]
        entry.update(
            {
                "last_report_date": day.isoformat(),
                "sessions_since_report": cadence.sessions_between(
                    day, trading_day, calendar
                ),
                "eps_surprise_pct": _eps_surprise_pct(quarter),
                # §6.1's last missing input. `reactions` arrives pre-computed
                # from `edge_earnings_reactions` — a lookback price move,
                # never something this module (or the routine) derives —
                # keyed on the same `(symbol, report_date)` the reaction
                # routine wrote it under. Absent until that routine has run
                # for this report, which `_pead` already treats as "no drift
                # confirmed yet", not as a defect to work around.
                "reaction_pct": (reactions or {}).get((symbol, day.isoformat())),
                "timing": (quarter.get("report") or {}).get("timing"),
            }
        )

    return entry


def _change_pct(quote: Mapping[str, Any]) -> float | None:
    """§6.3's confirming move. Today's price against the broker's own close.

    `get_equity_quotes`'s own guide names `adjusted_previous_close` as the
    daily-change denominator — used here rather than `previous_close` so
    `_catalyst`'s number is the one the broker itself would call "today's
    change", not a second, slightly different, home-grown one.
    """
    last = quote.get("last")
    prior = quote.get("adjusted_previous_close")
    if last is None or prior is None or float(prior) == 0:
        return None
    return (float(last) - float(prior)) / float(prior)


def _quotes(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Every fetched quote, plus the one derived field `_catalyst` needs.

    Derived here rather than left to the routine for the same reason
    `_earnings_view` is: `_change_pct` is arithmetic, and Absolute 2 reserves
    arithmetic for this module, never the prompt.
    """
    return {
        symbol: {**quote, "change_pct": _change_pct(quote)}
        for symbol, quote in broker_shapes.quotes(payload.get("quotes")).items()
    }


def _reactions(payload: Mapping[str, Any]) -> dict[tuple[str, str], float]:
    """`edge_earnings_reactions_recent()`'s rows, keyed for `_earnings_view`'s
    lookup. A row with no usable `reaction_pct` is dropped rather than kept as
    `None` — a missing key and an explicit null read identically to
    `_earnings_view`, so there is nothing to gain from keeping it and a
    malformed row silently disappearing here is safer than one raising."""
    out: dict[tuple[str, str], float] = {}
    for row in payload.get("earnings_reactions") or []:
        symbol, report_date, reaction = (
            row.get("symbol"), row.get("report_date"), row.get("reaction_pct")
        )
        if symbol is not None and report_date is not None and reaction is not None:
            out[(symbol, report_date)] = float(reaction)
    return out


def _earnings(
    payload: Mapping[str, Any], loaded: Mapping[str, Any], cycle: cadence.Cycle
) -> dict[str, dict[str, Any]]:
    """Every fetched symbol's earnings, derived against this cycle's day."""
    trading_day = date.fromisoformat(cycle.trading_day)
    reactions = _reactions(payload)
    return {
        symbol: _earnings_view(symbol, raw, trading_day, loaded["calendar"], reactions)
        for symbol, raw in (payload.get("earnings") or {}).items()
    }


def _account(
    payload: Mapping[str, Any], loaded: Mapping[str, Any], cycle: cadence.Cycle
) -> dict:
    """The broker's account, plus the one figure §10 cannot be measured without.

    `sizing.flow_adjusted_equity` subtracts cumulative net deposits from equity
    so a transfer is invisible to the drawdown halt. Four modules read that
    basis and not one of them can see `config/inception.json`, where the
    deposits are recorded — so cli joins them, exactly as it joins
    ``market_value`` onto the book for the same reason.

    `report.net_deposits` is the same function that fills the `edge_benchmark`
    column, deliberately: the number the halt is measured against and the number
    the scorecard reports are the same number, or one of them is wrong.

    Absent or empty, this contributes 0.0 and every figure is the raw one. An
    account that has never seen a flow is unaffected.

    **And the high-water mark's anchor** (`sizing.anchored_mark`). The mark is
    only as right as the deposit record was when it was set, so it is carried as
    raw equity on a day and re-derived here against the record as it reads now:
    ``net_deposits_at_mark`` is this same function asked about the anchor's day.
    The anchor is `edge_control`'s once `reconcile` has written one. Until then
    `config/inception.json` may carry a one-time ``high_water_mark_anchor`` —
    the correction that shipped with the three August deposits — which is
    ignored on any cycle dated before it and the moment the control row has its
    own.
    """
    inception = loaded["inception"]
    account = {
        **(payload.get("account") or {}),
        "net_deposits": reporter.net_deposits(inception, cycle.trading_day),
    }
    control = payload.get("control") or {}
    equity_then = control.get("high_water_mark_equity")
    as_of = control.get("high_water_mark_as_of")
    if equity_then is None or as_of is None:
        seed = inception.get("high_water_mark_anchor") or {}
        equity_then, as_of = seed.get("equity"), seed.get("as_of")
    if equity_then is not None and as_of is not None and str(as_of) <= cycle.trading_day:
        account["high_water_mark_equity"] = float(equity_then)
        account["high_water_mark_as_of"] = str(as_of)
        account["net_deposits_at_mark"] = reporter.net_deposits(inception, str(as_of))
    return account


def _market_value(
    position: Mapping[str, Any], broker: Mapping[str, Any], quote: Mapping[str, Any]
) -> float:
    """What §4.4's caps weigh this position at.

    `reconcile.Position` has no such field and neither cap can be evaluated
    without one. The broker's own figure comes first — §12.2's rule is that the
    broker wins every decision — then the quote, then what the position cost.

    Every fallback overstates rather than understates on purpose. A position
    read as worthless would silently widen both the position and the sector cap
    around it, which is a denial layer quietly failing open.
    """
    value = broker.get("market_value")
    if value is not None:
        return float(value)

    quantity = float(position.get("quantity") or 0.0)
    last = quote.get("last")
    if last is not None:
        return round(quantity * float(last), 6)

    entry = position.get("entry_price")
    return 0.0 if entry is None else round(quantity * float(entry), 6)


def _dip_book(positions: Mapping[str, Any], payload: Mapping[str, Any]) -> list[dict]:
    """Dip Recovery's own holdings, in the shape `guard._PlanState` reads.

    Guard measures §4.4's slot, position and sector caps off `ctx["positions"]`
    and expects `symbol` plus `market_value`. Dip Recovery keeps its book in
    `dip_recovery_positions` instead — one row per symbol, carrying
    `total_quantity` and `average_cost` — so this is the join, the same kind
    `_book` does for core.

    `ENTRY_PENDING` rows are included at zero value on purpose: they hold a
    slot (`engine._ACTIVE_STATES`) because an order resting at the broker has
    already claimed the position it will become, and leaving them out would let
    guard's slot count disagree with the ledger's. They carry no market value
    because no shares exist yet.

    Valued at the live quote where there is one and at cost otherwise, the same
    order of preference and the same overstate-rather-than-understate direction
    `_market_value` documents: a position read as worthless silently widens
    every cap around it.
    """
    quotes = broker_shapes.quotes(payload.get("quotes"))
    book = []

    for symbol, row in sorted(positions.items()):
        quantity = float(row.get("total_quantity") or 0.0)
        last = (quotes.get(symbol) or {}).get("last")
        price = float(last) if last is not None else float(row.get("weighted_avg_cost") or 0.0)
        book.append(
            {"symbol": symbol, "quantity": quantity,
             "market_value": round(quantity * price, 6)}
        )

    return book


def _loss_below_stop(
    position: Mapping[str, Any], signal: Mapping[str, Any], quote: Mapping[str, Any],
    config: Mapping[str, Any],
) -> float:
    """How far under its 2xATR stop this position is trading (§6.6).

    Nothing else computes it, and `guard._overnight_spread_denials` defaults it
    to 0.0 — so without this the catastrophe override can never fire and §6.6's
    gate is flat, forbidding the one exit it explicitly says is correct:
    "paying a 2% spread to escape a position 15% past its stop".

    Absent basis or absent ATR reads as zero rather than as a catastrophe. The
    override buys a deliberately worse fill, and it must not be reachable on
    data that never arrived.
    """
    entry, atr, last = position.get("entry_price"), signal.get("atr"), quote.get("last")
    if entry is None or atr is None or last is None:
        return 0.0

    stop = float(entry) - float(config["atr_stop_multiple"]) * float(atr)
    if stop <= 0 or float(last) >= stop:
        return 0.0
    return round((stop - float(last)) / stop, 6)


def _loss_closes_for_guard(payload: Mapping[str, Any]) -> list[dict]:
    """§9's ledger, as the wash-sale check must see it.

    Step 6's `loss_closes` fetch runs *before* Step 7's `reconcile` (routine
    order, `docs/routine/cycle-prompt.md`), so a loss this cycle's own
    `reconcile` just realized is written to `edge_loss_closes` after the fetch
    already ran and is invisible to `payload["loss_closes"]`. That gap let a
    same-cycle stop-out and re-entry both clear `guard._wash_sale_denials`
    `reconcile.loss_closes` — this cycle's own rows,
    computed fresh every `decide` call — closes it without waiting a cycle for
    the next fetch to catch up.
    """
    fetched = payload.get("loss_closes") or []
    this_cycle = (payload.get("reconcile") or {}).get("loss_closes") or []
    return [*fetched, *this_cycle]


def _book(payload: Mapping[str, Any], loaded, cycle: cadence.Cycle) -> list[dict]:
    """The book this cycle decides against, from whichever source is current."""
    reconciled = payload.get("reconcile") or {}
    if reconciled.get("positions") is not None:
        positions = reconciled["positions"]
    else:
        positions = _carried(
            payload.get("prior_book") or {"trading_day": cycle.trading_day},
            cycle,
            loaded["calendar"],
        )

    brokers = {p["symbol"]: p for p in payload.get("positions") or []}
    quotes = payload.get("quotes") or {}
    signals = payload.get("signals") or {}

    return [
        {
            **position,
            "market_value": _market_value(
                position,
                brokers.get(position["symbol"]) or {},
                quotes.get(position["symbol"]) or {},
            ),
            "loss_pct_below_stop": _loss_below_stop(
                position,
                signals.get(position["symbol"]) or {},
                quotes.get(position["symbol"]) or {},
                loaded["config"],
            ),
        }
        for position in positions
    ]


def _shape(order: Mapping[str, Any]) -> tuple:
    """What makes two placement attempts *the same order*.

    Symbol, side, order type and whole-versus-fractional — and deliberately
    **not** quantity, which drifts by pennies of notional every cycle as the
    ladder moves. A key that included it would never match twice and §14's
    escalation would be unreachable.

    Whole-versus-fractional is the term the whole design turns on. A fractional
    market buy the broker refused says nothing about the whole-share limit buy
    the same name earns once equity has grown: that is a different order in
    every respect that caused the refusal, and a `(symbol, side)` key would
    fence the account out of a name it can now legitimately buy.
    """
    quantity = order.get("quantity")
    return (
        order.get("symbol"),
        order.get("side"),
        order.get("type"),
        None if quantity is None else _is_fractional(order),
    )


def _repeats(payload: Mapping[str, Any]) -> dict[tuple, dict[str, int]]:
    """The day's rejections, counted by order shape and then by what was said.

    Grouped in two levels because the order being planned has no broker text of
    its own — it has not been sent yet. The question is whether *some* sentence
    has been said twice about this shape, and which one.
    """
    grouped: dict[tuple, dict[str, int]] = {}
    for row in payload.get("rejections") or []:
        counts = grouped.setdefault(_shape(row.get("order") or {}), {})
        detail = row.get("detail")
        counts[detail] = counts.get(detail, 0) + 1
    return grouped


def _blocked(
    orders: Sequence[Mapping[str, Any]], payload: Mapping[str, Any]
) -> tuple[list[dict], list[dict]]:
    """§14's "no blind retry", applied before guard rather than after.

    A repeated rejection is a fact about the order's *history*, which comes
    before any question of legality — and a plan showing the same order both
    approved and blocked would be a runlog nobody could read. Guard stays the
    sole authority on whether an order may be placed.

    Nothing here is stored. The count is recomputed from the day's runlog every
    cycle, so it clears the moment the shape changes or the day rolls over:
    one retry a day after two identical failures is not a blind retry, and a
    halt lifting or a tradability flag refreshing is exactly the kind of thing
    that makes tomorrow's attempt worth making.
    """
    repeats = _repeats(payload)
    kept: list[dict] = []
    blocked: list[dict] = []

    for order in orders:
        counts = repeats.get(_shape(order)) or {}
        detail, times = max(counts.items(), key=lambda row: row[1], default=(None, 0))

        if times < REPEATED_REJECTIONS:
            kept.append(dict(order))
            continue

        sell = order.get("side") == SELL
        blocked.append(
            {
                "order": dict(order),
                "code": PLACEMENT_BLOCKED,
                "detail": detail,
                "occurrences": times,
                # An exit that will not place leaves a position unprotected,
                # and §10.1 already gives a FRACTIONAL position no protection
                # between cycles. A missed entry costs an opportunity.
                "priority": BLOCKED_EXIT if sell else BLOCKED_ENTRY,
                "message": (
                    f"the broker refused this {order.get('symbol')} "
                    f"{order.get('side')} {times} times with the same answer; "
                    "§14 escalates rather than sending it again"
                ),
            }
        )

    return kept, blocked


def _buy(entry: sizing.SizedEntry) -> dict:
    """A sized entry in `guard.evaluate`'s order shape.

    ``side`` is supplied here rather than carried on `SizedEntry` for the same
    reason `plays.Exit.as_order` supplies it: sizing only ever sizes a purchase,
    and a field that is always one value belongs at the boundary that needs it.
    """
    return {
        "symbol": entry.symbol,
        "side": "buy",
        "type": entry.order_type,
        "quantity": entry.quantity,
        "limit_price": entry.limit_price,
        "stop_price": None,
        "session": entry.session,
        "play_id": entry.play_id,
        # §17's SURGE: tells `guard.concentration` to skip §4.4's caps for this
        # order. False for every ordinary entry.
        "full_deployment": entry.full_deployment,
    }


def _new_positions(
    book: Sequence[Mapping[str, Any]],
    approved: Sequence[Mapping[str, Any]],
    held: set[str],
    control,
    trading_day: str,
) -> dict:
    """§10's new-positions-a-day cap, advanced by the names that actually opened.

    Counting *approvals* let a 09:40 entry that never filled hold a slot for the
    rest of the day (and the cap counted all four). The count is now the names
    the reconciled book shows opened *today* — a position whose oldest lot is
    this session and that the system was not merely handed (`adopted`) — kept in
    ``filled`` so a name stays counted even if it is scratched the same
    afternoon, and self-resetting by ``date`` like every other daily counter.

    This cycle's own approvals are unioned in as well: they have not reconciled
    yet, so without them two cycles could each approve up to the cap before the
    first fill landed. They are *not* persisted into ``filled`` — that is the
    whole point, so a stale unfilled approval stops counting the moment its
    cycle passes. A top-up on a held name counts for nothing either way: §4.2
    counts names, and a second tranche opens no position.
    """
    taken = control.get("new_positions") or {}
    same_day = taken.get("date") == trading_day
    filled = set(taken.get("filled") or []) if same_day else set()
    filled |= {
        position["symbol"]
        for position in book
        if position.get("sessions_held") == 0 and not position.get("adopted")
    }
    approved_new = {
        order["symbol"]
        for order in approved
        if order.get("side") == "buy" and order["symbol"] not in held
    }
    return {
        "date": trading_day,
        "count": len(filled | approved_new),
        "filled": sorted(filled),
    }


def _day_context_write(payload: Mapping[str, Any], cycle: cadence.Cycle) -> dict:
    """Informational summary of this cycle's day-context cache coverage —
    reporting only; nothing downstream gates on it anymore.

    Persistence used to be a single wholesale write here, gated on
    `fetch_meta` showing a fully-completed live CANDIDATES-scope fetch. That
    design meant a cycle that died partway through Step 6 (hit a session or
    spend limit before ever calling `decide`) wrote nothing, no matter how
    much of the candidate set it actually finished — and in production the
    live fetch kept dying before `decide()` ever ran, so the cache was never
    once written. Persistence is now incremental: the routine calls
    `day_context_write_symbol` once per stale candidate as its live fetch
    completes, during Step 6 itself, not batched to the end and not routed
    through this function. This exists only to surface, in the runlog, how
    much of this cycle's candidate set was already warm.

    Reads `payload["fetch_meta"]`, which the routine sets from
    `day_context_plan`'s own output (`{"fresh": [...], "stale": [...]}`)
    before calling `decide`. Missing or malformed `fetch_meta` — a NOOP
    cycle, or one that never reached `day_context_plan` — reports zero for
    both rather than guessing.
    """
    meta = payload.get("fetch_meta") or {}
    fresh = meta.get("fresh")
    stale = meta.get("stale")
    return {
        "trading_day": cycle.trading_day,
        "fresh_count": len(fresh) if isinstance(fresh, list) else 0,
        "stale_count": len(stale) if isinstance(stale, list) else 0,
    }


def _macro_event_day(payload: Mapping[str, Any], trading_day: str) -> bool | None:
    """Whether a tracked macro series printed today, or None if unknowable.

    `data/macro.json`'s rows carry `first_seen`, the day CI first saw the
    release, which is the release day for FOMC/CPI/NFP/PCE. No `macro` block
    at all is "not known" rather than "no": `regime._posture` then leaves the
    macro-day rule unapplied and notes it.
    """
    macro = payload.get("macro")
    if not isinstance(macro, Mapping):
        return None
    return any(
        str(row.get("first_seen")) == str(trading_day)
        for row in macro.get("releases") or []
        if isinstance(row, Mapping)
    )


_DIP_TABLE_FIELDS = (
    regimes.DIP_ALLOWED, regimes.DIP_SIZE_MULTIPLIER, regimes.REQUIRE_SECTOR_ABOVE_200D,
    regimes.REQUIRE_SECTOR_RS_UPPER_HALF, regimes.MAX_RSI,
)


def _dip_table_shadow(ctx: Mapping[str, Any]) -> dict | None:
    """S3: what `dip_regime_table` says about this cycle's DIP entries.

    The day's row as the table reads it, whether it differs from the P1 rule,
    and, where it does, each DIP candidate the two rules treat differently:
    its size multiplier under each, null where that rule does not enter it.
    Both sides are computed with `regime_v2_enabled` on, whatever the live
    switches are, so the record compares the two rules and nothing else.

    None with no posture to read a row for. With the shipped table every row
    is the P1 rule, so `would_change` is empty and no second pass is run.
    """
    regime = ctx.get("regime")
    config = ctx["config"]
    row = regimes.dip_table_row(regime, config)
    if row is None:
        return None
    p1 = regimes.dip_table_row(regime, {})
    differs = any(row[key] != p1[key] for key in _DIP_TABLE_FIELDS)
    record = {
        "enabled": regimes.dip_table_enabled(config),
        "posture": row["posture"],
        "row": {key: row[key] for key in _DIP_TABLE_FIELDS},
        "notes": list(row["notes"]),
        "differs_from_p1": differs,
        "would_change": [],
    }
    if not differs:
        return record

    def dips(table: bool) -> dict[str, float]:
        result = plays.entries({**ctx, "config": {
            **config, plays.REGIME_V2_ENABLED: True, regimes.DIP_REGIME_TABLE_ENABLED: table,
        }})
        return {c.symbol: c.notional_multiplier for c in result.candidates
                if c.play_id == plays.DIP}

    without, with_table = dips(False), dips(True)
    record["would_change"] = [
        {"symbol": symbol, "p1": without.get(symbol), "table": with_table.get(symbol)}
        for symbol in sorted(set(without) | set(with_table))
        if without.get(symbol) != with_table.get(symbol)
    ]
    return record


def _regime_v2_shadow(
    regime: regimes.Regime | None,
    ctx: Mapping[str, Any],
    entering: plays.EntryResult,
    cycle: cadence.Cycle | None,
) -> dict:
    """`decide`'s record of the rule that is *not* in force this cycle.

    Flag off (the shipped default): `shadow_posture` is the posture the tape
    reads to, `shadow_dip_would_block` the DIP candidates the old rule ranked
    that the new one skips, and `shadow_entries` every play's such candidate
    plus the ones it would have resized. Flag on: the posture is enforced (it
    is on `regime.posture`), so `shadow_posture` is null and `shadow_entries`
    names what the old rule would have entered instead.

    Every key is always present. No regime, a no-op, or a cycle that cannot
    buy carries nulls and empty lists — present and empty, never absent.
    """
    enabled = plays.regime_v2_enabled(ctx)
    empty = {
        "regime_v2_enabled": enabled,
        "shadow_posture": None,
        "shadow_dip_would_block": [],
        "shadow_entries": None,
        # S3. Null unless an entry-capable cycle had a posture to read a row
        # of `dip_regime_table` for.
        "shadow_dip_table": None,
    }
    if regime is None:
        return empty
    if cycle is None or not cycle.entries_allowed:
        return {**empty, "shadow_posture": None if enabled else regime.posture}
    empty = {**empty, "shadow_dip_table": _dip_table_shadow(ctx)}

    other = plays.entries(
        {**ctx, "config": {**ctx["config"], plays.REGIME_V2_ENABLED: not enabled}}
    )
    v2, legacy = (entering, other) if enabled else (other, entering)
    v2_by_symbol = {c.symbol: c for c in v2.candidates}

    if enabled:
        return {
            **empty,
            "shadow_entries": {
                "rule": "legacy",
                "would_enter": [
                    c.symbol for c in legacy.candidates if c.symbol not in v2_by_symbol
                ],
                "would_size_full": [
                    c.symbol for c in legacy.candidates
                    if c.symbol in v2_by_symbol
                    and v2_by_symbol[c.symbol].notional_multiplier != c.notional_multiplier
                ],
            },
        }

    # Only skips the new rule adds: the same (symbol, play, code) under the old
    # rule is not a difference.
    already = {(s.symbol, s.play_id, s.code) for s in legacy.skipped}
    live = {c.symbol for c in legacy.candidates}
    would_skip = [
        s.as_dict() for s in v2.skipped
        if (s.symbol, s.play_id, s.code) not in already and s.symbol in live
    ]
    return {
        **empty,
        "shadow_posture": regime.posture,
        "shadow_dip_would_block": [
            row for row in would_skip if row["play_id"] == plays.DIP
        ],
        "shadow_entries": {
            "rule": "regime_v2",
            "would_skip": would_skip,
            "would_resize": [
                {
                    "symbol": c.symbol,
                    "play_id": c.play_id,
                    "notional_multiplier": v2_by_symbol[c.symbol].notional_multiplier,
                }
                for c in legacy.candidates
                if c.symbol in v2_by_symbol
                and v2_by_symbol[c.symbol].notional_multiplier != c.notional_multiplier
            ],
        },
    }


def _sizing_shadow(
    entering: plays.EntryResult,
    sized: sizing.SizingResult,
    ctx: Mapping[str, Any],
    cycle: cadence.Cycle,
) -> dict | None:
    """What the sizing rule that is *not* in force would have done (P3).

    `sizing.size` is pure, so the same ranked list is sized a second time with
    `sizing_v2_enabled` flipped. ``rule`` names the rule the shadow describes:
    ``sizing_v2`` while the switch is off, ``legacy`` once it is on. ``None``
    on a cycle that cannot buy — there is nothing to size either way.
    """
    if not cycle.entries_allowed:
        return None
    enabled = sizing.v2_enabled(ctx)
    other = sizing.size(
        [c.as_dict() for c in entering.candidates],
        {**ctx, "config": {**ctx["config"], sizing.SIZING_V2_ENABLED: not enabled}},
    )
    live = {e.symbol: e for e in sized.entries}
    return {
        "rule": "legacy" if enabled else "sizing_v2",
        "entries": [
            {"symbol": e.symbol, "quantity": e.quantity, "notional": round(e.notional, 2)}
            for e in other.entries
        ],
        "unsized": [{"symbol": u.symbol, "reason": u.reason} for u in other.unsized],
        "notional": round(sum(e.notional for e in other.entries), 2),
        # Where the two rules differ on a name, so the row can be read without
        # diffing two lists.
        "differs": sorted(
            {e.symbol for e in other.entries if e.symbol not in live}
            | {s for s in live if s not in {e.symbol for e in other.entries}}
            | {
                e.symbol for e in other.entries
                if e.symbol in live and live[e.symbol].quantity != e.quantity
            }
        ),
    }


def _play_activity(
    payload: Mapping[str, Any],
    ctx: Mapping[str, Any],
    entering: plays.EntryResult,
    held: set[str],
    cycle: cadence.Cycle,
) -> tuple[dict, list[dict]]:
    """Each play's dead-day streak, advanced by this cycle (P3).

    A play counts as having fired when it put a name the book does not hold
    into the ranked list. Plays that are switched off — a retired or shelved
    play (`plays.play_off`), SURGE without
    `full_send_enabled`, SECTOR without `play_sector_enabled`, anything the
    operator has darked with `regime_fit = 0.0` — are not tracked, so silence
    from a play nobody asked to run is not reported as a fault.
    """
    control = payload.get("control") or {}
    config = ctx["config"]
    tracked = [plays.DIP, plays.PEAD, plays.CATALYST, plays.MACRO]
    if bool(control.get("full_send_enabled", False)):
        tracked.append(plays.SURGE)
    if bool(config.get("play_sector_enabled")):
        tracked.append(plays.SECTOR)
    tracked = [
        p for p in tracked
        if plays.play_off(ctx, p) is None and plays._operator_dark(ctx, p) is None
    ]

    return deployments.play_activity(
        control.get(deployments.PLAY_ACTIVITY_KEY),
        plays=tracked,
        alive={c.play_id for c in entering.candidates if c.symbol not in held},
        trading_day=cycle.trading_day,
        threshold=int(
            config.get(
                deployments.DEAD_PLAY_ALERT_DAYS, deployments.DEAD_PLAY_ALERT_DAYS_DEFAULT
            )
        ),
    )


def decide(payload: Mapping[str, Any], loaded: Mapping[str, Any]) -> dict:
    """The order plan, or an empty plan with a reason (§8)."""
    loaded = core_sleeve.loaded_with_sleeve(loaded)
    payload = normalized(payload, loaded)
    cycle = _cycle(payload, loaded)
    envelope = _envelope(payload, cycle)
    day_context_write = _day_context_write(payload, cycle)

    if cycle.cycle_type == cadence.NOOP:
        # §13's cheap path. The routine fetched nothing, so there is nothing
        # for universe or any play to read — and resolving an empty snapshot
        # would write a confident-looking runlog row about a cycle that never
        # looked at anything.
        return {
            **envelope,
            "universe": None,
            "sectors": {},
            # Same reason `universe` is None here: a no-op fetched nothing, so
            # there is no tape to have judged.
            "regime": None,
            **_regime_v2_shadow(None, {}, plays.EntryResult(), None),
            # P3. A no-op cannot buy, so it has no deployment decision to
            # record and advances no play's dead-day streak.
            "sizing_v2_enabled": bool(loaded["config"].get(sizing.SIZING_V2_ENABLED)),
            "shadow_sizing": None,
            "core_sleeve": None,
            "deployment": None,
            "dead_plays": [],
            "book": [],
            "earnings": {},
            "exits": plays.ExitResult().as_dict(),
            "entries": plays.EntryResult().as_dict(),
            "sizing": None,
            "guard": None,
            "blocked": [],
            "held_for_news": [],
            "manual_required": [],
            "plan": {"cancels": [], "orders": [], "protection": []},
            "control_updates": dict(cycle.clock_updates),
            # §13's cheap path fetched nothing, so the streak neither advances
            # nor resets — the same "not evidence either way" rule as an
            # exit-only cycle, see `_dip_silent_streak`.
            "play_state_updates": {},
            "day_context_write": day_context_write,
            "rotation": _rotation_note(payload, loaded),
            "reason": cycle.reason,
        }

    triggers, macro_targets = _triggers(payload)
    book = _book(payload, loaded, cycle)

    # §6.0's regime, from prices rather than from an opinion someone wrote
    # into `edge_play_state` (see `src/regime.py`). `None` when the routine
    # sent no `regime_inputs` — which is every payload that predates this
    # command growing the key — and `plays` reads that as a multiplier of 1.0
    # on every play, so an old cycle behaves exactly as it did.
    regime = regimes.evaluate(
        payload.get("regime_inputs"),
        loaded["config"],
        macro_event_day=_macro_event_day(payload, cycle.trading_day),
    )

    resolved = universes.resolve(
        {
            "trading_day": cycle.trading_day,
            "snapshot": payload.get("snapshot") or {},
            "etfs": loaded["etfs"],
            "positions": book,
            "triggers": triggers,
            "macro_targets": macro_targets,
            "config": loaded["config"],
        }
    )

    # Repeated-zero visibility for DIP's harness-only discovery step (see
    # `_dip_silent_streak`'s docstring). A stale/never-fetched universe
    # already proceeds on positions only (§14) with no triggers of any kind,
    # which is not evidence the DIP scan itself is missing — so this is
    # skipped on exactly the cycles `resolved.stale` already excludes from
    # paying for discovery at all. With DIP entries switched off
    # `cli.candidates` carries no DIP row by design, so the count neither
    # advances nor resets: silence that was asked for is not a stuck scan.
    dip_streak = _dip_silent_streak(
        payload,
        triggers,
        cycle.entries_allowed
        and not resolved.stale
        and plays.dip_entries_enabled(loaded["config"]),
    )
    if dip_streak >= DIP_SILENT_STREAK_ALERT_AT:
        resolved = replace(
            resolved,
            notes=[
                *resolved.notes,
                universes.Note(
                    DIP_SILENT_STREAK,
                    plays.DIP,
                    f"DIP has produced no candidates for {dip_streak} consecutive "
                    "entry-allowed cycles; confirm the scan step (Appendix C) is "
                    "still running before assuming the tape is simply quiet",
                ),
            ],
        )

    # The issuer holdings file wins every source disagreement, so
    # `get_equity_fundamentals` fills gaps and never overrules. Letting the
    # broker's answer win would make §4.4's sector cap depend on which of two
    # sources the routine happened to read last.
    sectors = {**(payload.get("sectors") or {}), **resolved.sectors}

    # One context for all three modules, so they cannot disagree about the
    # world they are judging. Each reads the keys it knows and ignores the rest.
    ctx: dict[str, Any] = {
        "cycle": cycle.as_dict(),
        "calendar": loaded["calendar"],
        "config": loaded["config"],
        "macro_map": loaded["macro_map"],
        # §5.2's ETF file. Two readers, both added: `plays/sector.py`
        # enumerates the eleven it proposes over, and `plays/dip.py`'s §4.3 A
        # leg maps a candidate's GICS sector to the ETF whose regime state
        # speaks for it. Passing the loaded file rather than a derived map
        # keeps a settled design decision's single authoritative copy.
        "etfs": loaded["etfs"],
        # The operator's rotation codes and each member's GICS
        # sub-industry, read by `plays.entries` and `guard.rotation`.
        "rotation_codes": loaded.get("rotation_codes") or {},
        "sub_industries": universes.sub_industry_map(payload.get("snapshot") or {}),
        "control": payload.get("control") or {},
        "account": _account(payload, loaded, cycle),
        # The previous `edge_benchmark` row, the same optional key
        # `cli.report` already reads (see its own docstring) — only
        # `sizing.account_implausible` consumes it here, and only for its 50%
        # half; absent, that half is silently skipped.
        "prior_benchmark": payload.get("prior_benchmark") or {},
        "positions": book,
        "candidates": resolved.candidates,
        "universe": {
            "candidates": resolved.candidates,
            "snapshot_age_days": resolved.snapshot_age_days,
        },
        "sectors": sectors,
        "quotes": _quotes(payload),
        "signals": payload.get("signals") or {},
        "fundamentals": payload.get("fundamentals") or {},
        "earnings": _earnings(payload, loaded, cycle),
        "events": payload.get("events") or {},
        "macro": payload.get("macro") or {},
        "play_state": payload.get("play_state") or {},
        # `plays.entries`/`plays._top_ups` read `play_multipliers` off this and
        # nothing else does. Absent (None) is "no regime opinion", never dark —
        # `plays.core._regime_multiplier`'s docstring carries why that direction
        # is the safe one here.
        "regime": None if regime is None else regime.as_dict(),
        # The raw block `regime.evaluate` was given, alongside its verdict.
        # `regime.as_dict()` is five summary fields and deliberately carries
        # none of the numbers — but §4.3 F's vol leg (`plays.no_trade`, off by
        # default) measures SPY's realized vol against its own trailing median
        # and needs the closes to do it. Passing the block rather than a
        # second fetch keeps one source for the tape.
        "regime_inputs": payload.get("regime_inputs") or {},
        "open_orders": payload.get("open_orders") or [],
        "loss_closes": _loss_closes_for_guard(payload),
        "tradability": payload.get("tradability") or {},
        # Dip Recovery plan phase 1/2. `edge_symbol_lock_json()`, verbatim —
        # `{symbol: owner}`, absent entries meaning unclaimed rather than
        # "core's by default" (that reading belongs to `guard.lock`, not to
        # this assembly). `strategy` names which side this invocation is
        # deciding for and defaults to `"core"`, so every routine cycle that
        # predates Dip Recovery is unaffected: it never sends either key, and
        # an empty lock map plus a `"core"` default denies nothing that was
        # not already going to be denied.
        "symbol_lock": payload.get("symbol_lock") or {},
        "strategy": payload.get("strategy") or "core",
        # Dip Recovery plan phase 3. `edge_dip_recovery_config_json()`,
        # verbatim. Absent from every payload that predates this phase, so an
        # empty dict is what every existing cycle sends — `guard.strategy`
        # reads that as "core enabled, dip_recovery disabled", which is
        # exactly today's behavior.
        "dip_recovery_config": payload.get("dip_recovery_config") or {},
    }

    # §6.5 is evaluated on every cycle including overnight ones; entries are
    # gated by cadence. A cycle that cannot buy is not asked to propose —
    # the ranked list would be a runlog row about trades that were never
    # going to be placed.
    leaving = plays.exits(ctx)
    # The sleeve's ETF is a candidate only so its data is fetched and guard's
    # fence admits it. No play may propose it: a DIP on the index ETF would be
    # a second position in the sleeve's shares under a play's exits.
    if core_sleeve.enabled(ctx):
        ctx["candidates"] = [
            s for s in resolved.candidates if not core_sleeve.is_sleeve_symbol(s, ctx)
        ]
    entering = plays.entries(ctx) if cycle.entries_allowed else plays.EntryResult()

    # P1. The other rule's verdict on the same context, recorded and never
    # acted on: with `regime_v2_enabled` off this is what the posture and DIP's
    # sector rule would have skipped; with it on, what the old rule would have
    # entered. `plays.entries` is pure, so asking twice costs nothing and
    # cannot disagree with the answer the plan is built from.
    shadow = _regime_v2_shadow(regime, ctx, entering, cycle)

    # Sizing runs unconditionally, because guard denies against the same rung
    # sizing sized at and §4.4's caps scale with it. An empty candidate list
    # still returns the ladder, and the runlog records it either way.
    sized = sizing.size([c.as_dict() for c in entering.candidates], ctx)
    ctx["sizing"] = {"n_target": sized.ladder.n_target}
    # P3: the other sizing rule's answer on the same ranked list, recorded and
    # never acted on — what `sizing_v2` would have bought while it is off, and
    # what the old ladder would have bought once it is on.
    shadow_sizing = _sizing_shadow(entering, sized, ctx, cycle)

    # §10.1's resting orders, last in the list so guard evaluates them against
    # a plan state that already carries this cycle's exits and entries — and so
    # the routine places them after the entries they protect, which is the only
    # order that works: the shares have to exist before a stop can rest on them.
    #
    # They go through `_blocked` with everything else: §14's "no blind retry"
    # is about the order's history, and a `stop_market` the broker has refused
    # twice today would otherwise be re-sent on every cycle for the rest of the
    # day. Blocked here it is alerted instead, at the priority `_blocked` gives
    # a sell — which is exactly what an unprotected position deserves.
    cancels = [dict(c) for c in (payload.get("reconcile") or {}).get("cancels") or []]
    protection, protection_cancels, requires_cancel = _protection_orders(
        payload, leaving
    )
    cancels.extend(protection_cancels)

    # The dependency, carried on the order itself. Step 8a performs cancels
    # first, but "first" is not the same promise as "acknowledged": a cancel
    # the broker has accepted and not yet applied still holds the shares, and
    # placing into that window is exactly the *"Not enough shares to sell."*
    # the cancel exists to prevent. The runner waits for `state == cancelled`
    # on every id named here before it places the order.
    selling = []
    for exit_ in leaving.exits:
        order = exit_.as_order()
        depends = requires_cancel.get(order.get("symbol"))
        if depends:
            order["requires_cancel_of"] = list(depends)
        selling.append(order)

    # The core sleeve, after the plays: it takes what they left, less the
    # reserve and the buffer, up to its cap (`src/core_sleeve.py`). Computed
    # on every cycle and recorded under `core_sleeve`; an order only while the
    # switch is on. Not sized while the book is being flattened or an exit of
    # the sleeve itself is going out.
    sleeve = core_sleeve.plan(ctx, committed=sum(e.notional for e in sized.entries))
    sleeve_leaving = any(
        e.symbol == sleeve["symbol"] or e.code == plays.DRAWDOWN_FLATTEN for e in leaving.exits
    )
    sleeve_buys = []
    if core_sleeve.enabled(ctx) and sleeve["action"] == core_sleeve.BUY and not sleeve_leaving:
        sleeve_buys = [core_sleeve.order(sleeve, ctx)]

    orders = selling + [_buy(e) for e in sized.entries] + sleeve_buys
    orders, blocked = _blocked(orders + protection, payload)
    orders, protection = _partition_protection(orders)
    ctx["cancelling_order_ids"] = [c["order_id"] for c in cancels if c.get("order_id")]
    # Symbol-level, for `guard`'s PROTECTION_CONTENDS: the ids say which
    # resting orders go away, this says which *symbols* get their reserved
    # shares back before the plan's own sells are placed.
    ctx["cancelling_symbols"] = sorted(
        {c["symbol"] for c in cancels if c.get("symbol") is not None}
    )

    verdict = guard.evaluate(orders + protection, ctx)
    approved, resting = _partition_protection(verdict.approved)
    held = {position["symbol"] for position in book}

    # **A resting target is cancelled only for a sell that is going to be
    # placed.** The cancels above were planned for every exiting symbol, and
    # guard may still refuse the sell (`EXTENDED_SESSION_NOT_TRADABLE` on an
    # overnight cycle, an earlier `_blocked`).
    sells_placed = {o.get("symbol") for o in approved if o.get("side") == "sell"}
    kept_back = {
        order_id
        for symbol, ids in requires_cancel.items()
        if symbol not in sells_placed
        for order_id in ids
    }
    if kept_back:
        cancels = [c for c in cancels if c.get("order_id") not in kept_back]

    # P3: one record of what this cycle did with the cash and why, built from
    # the plan above and changing nothing in it. Only an entry-capable cycle
    # has a deployment decision to make.
    deployment = None
    dead_plays: list[dict] = []
    play_activity = None
    if cycle.entries_allowed:
        deployment = deployments.record(
            cycle_id=payload.get("cycle_id"),
            cycle=ctx["cycle"],
            regime=ctx["regime"],
            counts=deployments.funnel(
                members=resolved.members,
                pool=resolved.candidates,
                dropped=payload.get("candidates_dropped"),
                entries=entering.as_dict(),
                sized=[e.as_dict() for e in sized.entries],
                approved=approved,
            ),
            entries=entering.as_dict(),
            sizing=sized.as_dict(),
            guard=verdict.as_dict(),
            account=ctx["account"],
            positions=book,
            posture_enforced=plays.regime_v2_enabled(ctx) or sizing.v2_enabled(ctx),
            blocked=blocked,
            shadow={
                "posture": shadow["shadow_posture"],
                "dip_would_block": len(shadow["shadow_dip_would_block"]),
                "sizing": shadow_sizing,
            },
        )
        if not resolved.stale:
            play_activity, dead_plays = _play_activity(payload, ctx, entering, held, cycle)

    updates = dict(cycle.clock_updates)
    # §6.5 items 4/5's news-hold day-count and secondary-stop state
    # (`loss_holds`) lives in `edge_control`, merged the same way every other
    # module's control_updates is — `exits.py` cannot write it directly, since
    # only `cli.py`/the routine ever touch the database.
    updates.update(leaving.control_updates)
    updates["new_positions"] = _new_positions(
        book, approved, held, ctx["control"], cycle.trading_day
    )
    if play_activity is not None:
        updates[deployments.PLAY_ACTIVITY_KEY] = play_activity
    if verdict.approved or cancels:
        # §12.1: an unreconciled prior cycle that placed orders is itself an
        # event and promotes the next cycle out of no-op. `reconcile.py` clears
        # this; only this module can set it, because only this module knows an
        # order is about to exist. Set unconditionally it would hold every
        # later cycle out of §13's cheap path forever.
        updates["unreconciled"] = True

    return {
        **envelope,
        "universe": resolved.as_dict(),
        "sectors": sectors,
        # The day's tape, recorded whether or not it changed anything. §6.0's
        # multiplier used to be reconstructable only by reading `edge_play_state`
        # as it stood at the moment the cycle ran, which is why nobody could see
        # that it had been frozen — this key makes the input to every conviction
        # score part of the same runlog row as the score.
        "regime": None if regime is None else regime.as_dict(),
        **shadow,
        "sizing_v2_enabled": sizing.v2_enabled(ctx),
        "shadow_sizing": shadow_sizing,
        # What the sleeve would do with this cycle's idle cash, whether or not
        # it is switched on; `placed` says whether guard let its buy through.
        "core_sleeve": {
            "enabled": core_sleeve.enabled(ctx),
            **sleeve,
            "ordered": bool(sleeve_buys),
            "placed": any(core_sleeve.is_sleeve_order(o, ctx) for o in approved),
        },
        "deployment": deployment,
        "dead_plays": dead_plays,
        "book": book,
        # Exposed so the reaction routine can find candidates inside §6.1's window from
        # `edge_runlog` without re-deriving earnings itself — this is `ctx["earnings"]` verbatim,
        # the same dict `plays.py` already judged against.
        "earnings": ctx["earnings"],
        "exits": leaving.as_dict(),
        "entries": entering.as_dict(),
        "sizing": sized.as_dict(),
        "guard": verdict.as_dict(),
        "blocked": blocked,
        # Flattened alongside `blocked` (not nested under `exits`) because
        # `report.py` reads `ctx["held_for_news"]`/`ctx["manual_required"]`
        # directly, the same way it already reads `ctx["blocked"]` — the
        # routine merges these into `cycle.json` before calling `report`,
        # exactly as it already does for `blocked`.
        "held_for_news": leaving.as_dict()["held_for_news"],
        "manual_required": leaving.as_dict()["manual_required"],
        # Cancels first, then sells, then buys, then the resting orders. Guard
        # preserves the order it was given, so the sells are already ahead of
        # the buys — and a sale queued behind a buy that failed is a position
        # left open for no reason. `protection` is a separate key rather than
        # more of `orders` because the two are placed differently: these carry
        # `time_in_force: gtc`, they are expected *not* to fill, and every one
        # of them must be written into `placements` with its own flag so
        # neither §13's postclose sweep nor `reconcile`'s stale-order rule
        # cancels the protection it just placed.
        "plan": {
            "cancels": cancels,
            "orders": approved,
            "protection": resting,
        },
        "control_updates": updates,
        # Written via its own `edge_play_state_apply` call, after reconcile's —
        # see `_dip_silent_streak`.
        "play_state_updates": {plays.DIP: {DIP_SILENT_STREAK_KEY: dip_streak}},
        "day_context_write": day_context_write,
        "rotation": _rotation_note(payload, loaded),
        "reason": cycle.reason,
    }


def _rotation_note(payload: Mapping[str, Any], loaded: Mapping[str, Any]) -> dict:
    """`{"active_sectors", "unmatched", "filtering"}`. `unmatched` non-empty is
    the operator's cue: a name that matched no GICS sector filtered nothing,
    and a rotation of *only* such names filtered nothing at all (`filtering`
    false) rather than freezing entries on a typo. Read off `control` here, not
    carried from `candidates`, so it is the same on the live-session and
    runner paths.
    """
    return universes.sector_rotation(
        (payload.get("control") or {}).get("active_sectors"),
        loaded["etfs"],
        loaded.get("rotation_codes"),
        universes.sub_industry_map(payload.get("snapshot") or {}),
    ).as_dict()


def _protection_orders(
    payload: Mapping[str, Any], leaving: plays.ExitResult
) -> tuple[list[dict], list[dict], dict[str, list[str]]]:
    """`reconcile.protection_plan` turned into orders to evaluate, the cancels
    that have to precede them, and which cancel each sell depends on.

    Two rules, and the second one is the reason this is a function rather than
    a list comprehension:

    * A ``replace`` is a cancel and a place, in that order. The cancel joins
      `plan.cancels`, which the routine performs first (Step 8a), so the new
      order is never resting alongside the one it supersedes.
    * **A symbol that is being sold this cycle gets no protection and loses
      what it has.** A resting stop under a position an exit is about to close
      is a working sell order on shares that will not exist in an hour, and it
      would sell the *next* position in that name if one were opened before
      anyone noticed. This is the only case where the plan cancels a
      protective order without replacing it, and it is the case `reconcile`'s
      `protective_stop` skip was always at risk of getting wrong.

    **Read from the broker's open orders, not from `keep`**. This used to emit
    that cancel only for a `keep` row that carried an ``order_id``, and every
    `keep` row carried ``order_id: null`` — the broker returns the id as `id`
    and nothing renamed it, so `reconcile._ours` matched nothing (see
    `src/broker_shapes.py`). No cancel was emitted, the resting stop kept its
    reservation on every share, and the `TIME_STOP` was refused *"Not
    enough shares to sell."* on the last session of its limit. A symbol being
    sold now loses **every** resting agentic sell on it, found by walking
    `open_orders` — the same list `guard` and `exits` judge against — so a null
    id upstream can no longer turn a cancel into a no-op.

    The third return value maps each exiting symbol to the ids that must be
    `cancelled` before its sell may be placed; `decide` stamps it onto the
    order as ``requires_cancel_of`` so the runner cannot place the sell on a
    cancel it has not seen acknowledged (Step 8a).

    A cycle whose payload carries no `reconcile` block — an overnight or a
    postclose, which do not reconcile — produces no protection orders, which is
    correct twice over: `stop_market` is regular-hours-only, and those cycles
    have no fresh book to compute a plan against. The exit-side cancels below
    do not depend on that block at all, because they depend on the broker's
    open orders and this cycle's exits.
    """
    plan = (payload.get("reconcile") or {}).get("protection_plan") or {}
    exiting = {e.symbol for e in leaving.exits}

    orders: list[dict] = []
    cancels: list[dict] = []

    for order in plan.get("place") or []:
        if order.get("symbol") not in exiting:
            orders.append(dict(order))

    for row in plan.get("replace") or []:
        order = row.get("new_order")
        order_id = row.get("cancel_order_id")
        if order_id is not None:
            cancels.append(
                {
                    "order_id": order_id,
                    "symbol": (order or {}).get("symbol"),
                    "reason": "superseded by this cycle's protective order",
                }
            )
        if order is not None and order.get("symbol") not in exiting:
            replacement = dict(order)
            if order_id is not None:
                # The same dependency an exit carries, for the same reason: the
                # order being superseded holds the position's shares until it
                # actually reads `cancelled`, and a replacement stop refused
                # into that window leaves the position with nothing resting.
                replacement["requires_cancel_of"] = [order_id]
            orders.append(replacement)

    already = {c.get("order_id") for c in cancels}
    requires: dict[str, list[str]] = {}
    # `reconcile._ours`, rebuilt here rather than threaded through the payload:
    # both halves of `is_ours_to_replace` matter, and an order placed by *this*
    # cycle's predecessor is ours even on a broker row with no `placed_agent`.
    ours = {
        p["order_id"]: p
        for p in (_intent(payload).get("placements") or [])
        if p.get("order_id")
    }

    for order in payload.get("open_orders") or []:
        symbol = order.get("symbol")
        if symbol not in exiting or not reconciler._is_open_order(order):
            continue
        # §13's oldest rule still holds at the top of this loop: an order of
        # unknown provenance is never cancelled, at any hour, because it may be
        # the operator's. The exit is still placed and the broker may still
        # refuse it — `guard`'s PROTECTION_CONTENDS says so first, by name,
        # rather than leaving a rejection to be read out of a runlog.
        kind = plays.protection.resting_kind(order)
        if kind is None or not plays.protection.is_ours_to_replace(order, ours):
            continue

        order_id = order.get("order_id")
        if order_id is None or order_id in already:
            continue
        already.add(order_id)
        cancels.append(
            {
                "order_id": order_id,
                "symbol": symbol,
                "reason": (
                    f"{symbol} is being sold this cycle; the resting {kind} on it "
                    "holds the shares the sell needs and must not outlive the "
                    "position"
                ),
            }
        )
        requires.setdefault(symbol, []).append(order_id)

    return orders, cancels, requires


def _partition_protection(approved: Sequence[Mapping[str, Any]]) -> tuple[list[dict], list[dict]]:
    """Split guard's one approved list back into `plan.orders` and
    `plan.protection`.

    Guard evaluates a *plan*, not an order — two buys that each fit the
    settled balance alone can breach Invariant 1 together, and a resting sell
    on a symbol is state the orders after it have to see. So the protection
    orders go through the same call as everything else and are separated
    afterwards, rather than being given a second `guard.evaluate` that would
    judge them against a book the first call had already changed.
    """
    orders: list[dict] = []
    resting: list[dict] = []
    for order in approved:
        target = resting if plays.protection.order_kind(order) else orders
        target.append(dict(order))
    return orders, resting


def _intent(payload: Mapping[str, Any]) -> dict:
    """Last cycle's placements, whichever of `edge_prior`'s two shapes arrived.

    `reconcile._placements` reads `ctx["intent"]["placements"]`, but the
    function that fills it — `edge_prior` — returns ``{"placements",
    "prior_book", "rejections"}`` and has **no `intent` key at all.** Appendix
    D and the runner prompt both say only "call 5" against the three names,
    which reads as three fields to copy out and is not: a live session posted
    the whole result and `["placements"]` found what it wanted, the runner
    posted the `placements` array itself, and `.get` on a list raised
    `AttributeError` — the same vocabulary gap as `balances`, on a different
    key, found in the shadow ledger's own refusal line.

    Both readings are unambiguous, so both are accepted here rather than at the
    caller: a list can only be the placements array, and a Mapping is the
    envelope that carries it. Anything else is neither, and returns empty —
    which `_placements` already treats as "the prior cycle placed nothing".
    """
    intent = payload.get("intent")
    if isinstance(intent, Mapping):
        return dict(intent)
    if isinstance(intent, list):
        return {"placements": intent}
    return {}


def reconcile(payload: Mapping[str, Any], loaded: Mapping[str, Any]) -> dict:
    """§12's diff, run against broker truth.

    A thin command on purpose. §12.1 puts reconciliation in the routine because
    GitHub Actions cannot see Robinhood, and everything it does is already in
    `reconcile.py` — this only assembles the context and hands the result back
    for the routine to write.

    The last five keys are the market read, and they are here because adopting a
    position now means choosing how to manage it: `reconcile._adopt` asks
    `plays.would_open` which play's trigger the inherited name fires today, and
    that question cannot be answered from orders and tax lots. **They cost
    nothing extra.** The routine fetches once into `cycle.json` and runs both
    `reconcile` and `decide` against that same file (`cycle-prompt.md` §"Order
    of operations"), so every one of them is already in this payload — `decide`
    was simply the only command reading them. A cycle that fetched none of them
    still reconciles: each falls back to empty, no play triggers, and the name
    takes `ADOPTED`, which is the same answer for a different reason.
    """
    payload = normalized(payload, loaded)
    cycle = _cycle(payload, loaded)
    result = reconciler.run(
        {
            "cycle": cycle.as_dict(),
            "calendar": loaded["calendar"],
            "config": loaded["config"],
            "control": payload.get("control") or {},
            "account": _account(payload, loaded, cycle),
            "intent": _intent(payload),
            "orders": payload.get("orders") or [],
            "fills": payload.get("fills") or [],
            "positions": payload.get("positions") or [],
            "prior_positions": payload.get("prior_positions") or [],
            "tax_lots": payload.get("tax_lots") or {},
            # §9's ledger, for `_anchored_entry`'s wash-sale reading of a tax
            # lot that sits above the fill. Only the fetched rows: this cycle's
            # own closes are what `reconcile` is about to compute.
            "loss_closes": payload.get("loss_closes") or [],
            # The broker's own per-fill realized figure. Tax lots are open
            # lots, so a position sold to zero has no basis left to price
            # against and §9's ledger recorded 15 straight closes it could not
            # read. This is the read that survives the close.
            "realized": payload.get("realized") or [],
            "events": payload.get("events") or {},
            "play_state": payload.get("play_state") or {},
            "quotes": payload.get("quotes") or {},
            "signals": payload.get("signals") or {},
            "earnings": _earnings(payload, loaded, cycle),
            "macro": payload.get("macro") or {},
            "macro_map": loaded["macro_map"],
            # Dip Recovery plan phase 2. Read so `reconcile.run` can compute
            # which of this side's locks to release (a symbol it no longer
            # holds); the claims it emits are this cycle's `book`, not this
            # map — see `reconcile._symbol_lock_claims`.
            "symbol_lock": payload.get("symbol_lock") or {},
            "strategy": payload.get("strategy") or "core",
            # Dip Recovery plan phase 6. `edge_dip_recovery_positions_json()`,
            # verbatim — read here only so core's reconcile can close the
            # adoption race described in `reconcile._other_side_symbols`: the
            # other side's `ENTRY_PENDING` row exists a full tick before its
            # lock claim does. Absent (every payload that predates this, and
            # every cycle before Dip Recovery holds anything) it is an empty
            # map and nothing is excluded that the lock did not already
            # exclude.
            "dip_recovery_positions": payload.get("dip_recovery_positions") or {},
        }
    )
    return {**_envelope(payload, cycle), **result.as_dict()}


def report(payload: Mapping[str, Any], loaded: Mapping[str, Any]) -> dict:
    """§8.3's queue and §8.4's daily row.

    The `edge_alerts.cycle_id` column is still stamped here, not in
    `report.py` — that module doesn't decide anything from which invocation
    this is. `cycle_id` is passed into its `ctx` regardless, because
    `report._status_alert`'s footer prints the id for a human, the same
    reason the footer prints an ET time it also doesn't decide from.
    Labelling output and deciding from state are different things; only the
    second is the property this module's docstring means to hold.
    """
    payload = normalized(payload, loaded)
    cycle = _cycle(payload, loaded)
    result = reporter.run(
        {
            "cycle": cycle.as_dict(),
            "cycle_id": payload.get("cycle_id"),
            # Optional and silently omitted from the footer when absent —
            # `report.py` cannot know its own row's id before writing it, so
            # this is never the complete set, only whatever the caller chose
            # to carry forward.
            "runlog_ids": payload.get("runlog_ids") or [],
            # The hosted core's `run._finalize_phase` derives this once,
            # server-side, and hands it in verbatim so the alert's status word
            # cannot disagree with `edge_session_stats.status` -- see
            # `report._status_alert`'s use of it. Absent for any caller that
            # does not compute one (a bare `cli.report()` call, or a test
            # fixture); `_cycle_status` remains the fallback for exactly that
            # case, unchanged.
            "session_status": payload.get("session_status"),
            # The hosted runner's fetch warnings, counted by
            # `services/core` from the step log. Absent on the live path.
            "runner_fetch": payload.get("runner_fetch") or {},
            # `services/core`'s finalize phase, from the decide runlog row:
            # sells guard refused (no rejection row carries them) and the
            # stale `data/` sources behind a "warnings" status. Absent on the
            # live path.
            "denied_exits": payload.get("denied_exits") or [],
            "placed_orders": payload.get("placed_orders") or [],
            "stale_data_sources": payload.get("stale_data_sources") or [],
            # P3: the deployment record `run._finalize_phase` completed, the
            # error if writing it failed, and any play `decide` found dead
            # this cycle — each one line in the status alert. Absent on the
            # live path and on a cycle that cannot buy.
            "deployment": payload.get("deployment"),
            "deployment_write_error": payload.get("deployment_write_error"),
            # S0: the error if the decision snapshot could not be written.
            "snapshot_write_error": payload.get("snapshot_write_error"),
            "dead_plays": payload.get("dead_plays") or [],
            "calendar": loaded["calendar"],
            "config": loaded["config"],
            "inception": loaded["inception"],
            # The previous `edge_benchmark` row (`{trading_day, equity, ...}`),
            # read back by the routine so `report._integrity_alert` can compare
            # two consecutive daily marks. Absent on the first post-close the
            # account ever runs — the check stays silent until there is a series.
            "prior_benchmark": payload.get("prior_benchmark") or {},
            "control": payload.get("control") or {},
            "account": _account(payload, loaded, cycle),
            # `report.account_implausible` weighs equity against the
            # market value of what the broker's own `get_equity_positions`
            # says is open — the same raw, unwrapped-only shape `decide`
            # already reads it in (Appendix D's `positions`).
            "positions": payload.get("positions") or [],
            "quotes": payload.get("quotes") or {},
            "reconcile": payload.get("reconcile") or {},
            "play_state": payload.get("play_state") or {},
            "prior_play_state": payload.get("prior_play_state") or {},
            "failure": payload.get("failure"),
            "held_for_news": payload.get("held_for_news") or [],
            "manual_required": payload.get("manual_required") or [],
            # `decide.blocked`, flattened the same way `held_for_news` and
            # `manual_required` already are. Previously omitted here despite
            # `report._blocked_fragment` reading it and `test_report_alerts.py`
            # exercising it — `PLACEMENT_BLOCKED` could never actually fire.
            "blocked": payload.get("blocked") or [],
            # `decide.entries`/`decide.exits`, verbatim — the 09:40 no-activity
            # summary reads their `skipped`/`exits` lists the same way the
            # scorecard reads `play_state`: the numbers `decide` already
            # computed, not a second reading of them.
            "entries": payload.get("entries") or {},
            "exits": payload.get("exits") or {},
            # `edge_fill_quality_recent(7)`, verbatim — one row per terminal
            # order the core placed over the trailing week. The scorecard turns
            # it into two lines (fill rate, median slippage); every other cycle
            # ignores it.
            "fill_quality_week": payload.get("fill_quality_week") or [],
            # `edge_shadow_recent(7)`, verbatim — the shadow ledger's closed
            # trades for the week. The scorecard turns
            # it into one line per rule variant; every other cycle ignores it,
            # and a deployment that has never run the replay sends nothing and
            # gets no shadow block rather than a row of zeroes.
            "shadow_week": payload.get("shadow_week") or [],
            # `edge_realized_trades_recent(7)`, verbatim — the week's
            # broker-reported closes. The scorecard names any reached off a
            # disputed entry basis; every other cycle ignores it.
            "realized_trades_week": payload.get("realized_trades_week") or [],
        }
    ).as_dict()

    result["alerts"] = [
        {**alert, "cycle_id": payload.get("cycle_id")} for alert in result["alerts"]
    ]
    return {**_envelope(payload, cycle), **result}


WATCHLIST_KEY = "today_watchlist"


def _drop_blocked_sectors(
    result: Mapping[str, Any], payload: Mapping[str, Any], loaded: Mapping[str, Any]
) -> dict:
    """Take out the names whose sector today's regime will not buy into.

    The operator's "ignore the sector at a peak", applied where it is worth
    money: **before** Step 6's per-symbol loop pays six broker calls for a name
    `plays` would go on to propose and `guard` would go on to allow. Dropping
    it in `decide` instead would be equally correct and entirely free of
    benefit — the fetch would already have happened.

    A discovery-stage filter, so it is deliberately the *coarse* one. It knows
    a candidate's sector from the issuer holdings file (via
    `universe.sector_map`) and nothing else about it; a name whose sector the
    snapshot does not carry is kept, on this module's own "can be too generous,
    cannot afford to be too strict" rule. `plays` still judges everything that
    survives.

    ETF targets are filtered on their own ticker rather than through the sector
    map, since a sector ETF *is* the sector — buying XLK while XLK is the thing
    at a peak is the case this rule was asked for.

    Inert with no `regime_inputs`: `blocked_sectors` is empty for an absent
    regime, so nothing is dropped and the result is byte-identical to what
    `candidate_discovery.assemble` returned.
    """
    regime = regimes.evaluate(payload.get("regime_inputs"), loaded["config"])
    if regime is None:
        return dict(result)

    blocked = regimes.blocked_sectors(regime.as_dict(), loaded["etfs"])
    etf_states = regime.sector_states
    sectors = universes.sector_map(
        {"snapshot": payload.get("snapshot") or {}, "etfs": loaded["etfs"]}
    )

    triggers: list[dict] = []
    macro_targets: list[str] = []
    fell: list[dict] = []

    for trigger in result.get("triggers") or []:
        state = blocked.get(sectors.get(trigger["symbol"]))
        if state is None:
            triggers.append(trigger)
        else:
            fell.append(
                candidate_discovery.dropped(
                    trigger["symbol"],
                    f"{sectors[trigger['symbol']]} is {state} today; "
                    f"{trigger['play_id']} does not buy into it",
                )
            )

    for symbol in result.get("macro_targets") or []:
        state = etf_states.get(symbol)
        if state not in regimes.BLOCKED_SECTOR_STATES:
            macro_targets.append(symbol)
        else:
            fell.append(
                candidate_discovery.dropped(
                    symbol,
                    f"{symbol} is {state} today; the ETF sleeve does not "
                    "rotate into it",
                )
            )

    return {
        **result,
        "triggers": triggers,
        "macro_targets": macro_targets,
        "candidates_dropped": [*(result.get("candidates_dropped") or []), *fell],
    }


def candidates(payload: Mapping[str, Any], loaded: Mapping[str, Any]) -> dict:
    """Step 5's `triggers`/`macro_targets`.

    `full` (09:40, once a day) is the only cycle that actually discovers —
    it runs `src/candidates.py` fresh against `data/events.json`/
    `data/macro.json` exactly as before, and its result is what the day
    trades on. It also persists that result to `control_updates[WATCHLIST_KEY]`
    so `monitor` cycles never have to re-derive it.

    `monitor` (11:40/13:40/15:20) does not call `candidate_discovery.assemble`
    at all — re-scanning the event/macro files three more times a day was
    the direct cause of a 40+-symbol candidate set on every monitor firing,
    since PEAD/CATALYST/MACRO discovery has no memory of what a *prior*
    cycle already looked at and found nothing new in. Instead it reads back
    the watchlist `full` wrote earlier the same trading day. A watchlist
    from a different day (full hasn't run yet, or its write never landed)
    is not trusted — that degrades to an empty trigger/macro set, i.e.
    positions-only, per Absolute 7 rather than guessing at today's catalysts.

    The cost of this: an 8-K filed after 09:40 will not trigger a same-day
    PEAD/CATALYST entry — only the next day's `full` cycle sees it. DIP is
    unaffected by any of this; its scan survivors (`cycle.json`'s
    `dip_survivors`) are merged in here regardless of cycle type.

    Every list here — discovery, the watchlist, resubmits, macro targets and
    DIP survivors — passes the operator's `active_sectors` rotation before it
    leaves this function. Held positions are not in any of these lists and are
    never filtered (Invariant 7).
    """
    cycle = _cycle(payload, loaded)
    control = payload.get("control") or {}

    if cycle.cycle_type == cadence.MONITOR:
        watchlist = control.get(WATCHLIST_KEY) or {}
        if watchlist.get("trading_day") == cycle.trading_day:
            triggers = list(watchlist.get("triggers") or [])
            macro_targets = list(watchlist.get("macro_targets") or [])
        else:
            triggers, macro_targets = [], []

        # An entry order `reconcile` cancelled unfilled last cycle re-enters
        # here so this cycle's `decide` re-prices and re-proposes it. Date-checked
        # like the watchlist, and
        # de-duplicated against it by `(symbol, play_id)` so a name that is
        # already a live trigger is not proposed twice.
        resubmit = control.get("resubmit_candidates") or {}
        if resubmit.get("trading_day") == cycle.trading_day:
            seen = {(t.get("symbol"), t.get("play_id")) for t in triggers}
            for row in resubmit.get("triggers") or []:
                key = (row.get("symbol"), row.get("play_id"))
                if key[0] and key not in seen:
                    seen.add(key)
                    triggers.append({"symbol": row.get("symbol"), "play_id": row.get("play_id")})

        # The watchlist was rotation-filtered when `full` wrote it, but
        # against *that* moment's rotation. Re-filtering here is what makes a
        # mid-day narrowing apply to MACRO/PEAD/CATALYST and to the resubmits
        # `reconcile` queued. A mid-day widening cannot restore what `full`
        # already dropped — that waits for the next `full` (operator ruling).
        triggers, fell = _rotate(triggers, payload, loaded)
        macro_targets, fell_etfs = _rotate(macro_targets, payload, loaded)
        result = {"triggers": triggers, "macro_targets": macro_targets,
                  "candidates_dropped": fell + fell_etfs}
        result = _drop_off_plays(result, loaded)
        return {**_envelope(payload, cycle),
                **_with_core_trigger(_merge_dip_survivors(result, payload, loaded), loaded)}

    result = _drop_blocked_sectors(candidate_discovery.assemble(payload, loaded), payload, loaded)
    envelope = {**_envelope(payload, cycle),
                **_with_core_trigger(_merge_dip_survivors(result, payload, loaded), loaded)}

    if cycle.cycle_type == cadence.FULL:
        # The watchlist is `result`'s, from *before* the DIP merge: survivors
        # are a per-cycle scan, re-run by every entries-allowed cycle, and
        # were never part of what `monitor` reads back.
        envelope["control_updates"] = {
            WATCHLIST_KEY: {
                "trading_day": cycle.trading_day,
                "triggers": result["triggers"],
                "macro_targets": result["macro_targets"],
            }
        }

    return envelope


def _rotate(
    rows: Sequence[Any], payload: Mapping[str, Any], loaded: Mapping[str, Any]
) -> tuple[list[Any], list[dict]]:
    """The sector map is `universes.sector_map` over the same `snapshot` + sleeve
    every other discovery-stage filter reads, and is only built when the
    rotation actually filters — `[]` costs nothing.
    """
    rotation = universes.sector_rotation(
        (payload.get("control") or {}).get("active_sectors"),
        loaded["etfs"],
        loaded.get("rotation_codes"),
        universes.sub_industry_map(payload.get("snapshot") or {}),
    )
    if not rotation.filtering:
        return list(rows), []
    sectors = universes.sector_map(
        {"snapshot": payload.get("snapshot") or {}, "etfs": loaded["etfs"]}
    )
    kept, fell = universes.rotation_filter(rows, sectors, rotation)
    return kept, [candidate_discovery.dropped(symbol, reason) for symbol, reason in fell]


DEFAULT_DIP_SURVIVOR_CAP = 30
DEFAULT_RESOLVE_MAX_SYMBOLS = 50


def _positive_int_config(loaded: Mapping[str, Any], key: str, default: int) -> int:
    value = (loaded.get("config") or {}).get(key, default)
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else default


def dip_survivor_cap(loaded: Mapping[str, Any]) -> int:
    """`config.json`'s `dip_survivor_cap`: the most DIP scanner survivors one cycle
    carries into discovery. Falls back to the default (50) on an absent or
    non-positive value; `_validate_config` refuses a bad one first."""
    return _positive_int_config(loaded, "dip_survivor_cap", DEFAULT_DIP_SURVIVOR_CAP)


def resolve_max_symbols(loaded: Mapping[str, Any]) -> int:
    """`config.json`'s `resolve_max_symbols`: the most symbols the runner is asked
    to fetch live data for in one cycle. Enforced by the hosted core
    (`services/core/core/run.py`), which trims discovery names only."""
    return _positive_int_config(loaded, "resolve_max_symbols", DEFAULT_RESOLVE_MAX_SYMBOLS)


def _cached_rsi(day_context: Mapping[str, Any], symbol: str) -> float | None:
    """The pre-market job's daily RSI(14) for `symbol`, or None."""
    entry = day_context.get(symbol) if isinstance(day_context, Mapping) else None
    signals = entry.get("signals") if isinstance(entry, Mapping) else None
    value = signals.get("rsi") if isinstance(signals, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _with_core_trigger(result: Mapping[str, Any], loaded: Mapping[str, Any]) -> dict:
    """The core sleeve's ETF as a trigger row, while the sleeve is on.

    A trigger is what puts a symbol in the runner's fetch list
    (`services/core/core/run._symbols`) and in `universe.resolve`'s candidate
    set, so this one row is how the ETF gets a quote, a tradability read and a
    place inside guard's fence on every cycle. It is added after discovery's
    cap and the sector rotation: the sleeve is not a discovery and Broad
    Market is outside the rotation. No play reads the row; `decide` keeps the
    symbol away from every play.
    """
    out = dict(result)
    config = loaded.get("config") or {}
    if not core_sleeve.enabled(config):
        return out
    name = core_sleeve.symbol(config)
    triggers = list(out.get("triggers") or [])
    if not any(isinstance(t, Mapping) and t.get("symbol") == name
               and t.get("play_id") == core_sleeve.CORE for t in triggers):
        triggers.append({"symbol": name, "play_id": core_sleeve.CORE})
    out["triggers"] = triggers
    return out


def _drop_off_plays(result: Mapping[str, Any], loaded: Mapping[str, Any]) -> dict:
    """A watchlist or resubmit row for a play that is retired or shelved.

    `candidate_discovery.assemble` already leaves these out. This is for the
    rows a cycle carries in from earlier the same day, which were written
    before the play went off. How many were set aside is recorded only when
    there were any.
    """
    rows = list(result.get("triggers") or [])
    kept = [
        t for t in rows
        if not (isinstance(t, Mapping) and plays.play_off(loaded, t.get("play_id")))
    ]
    if len(kept) == len(rows):
        return dict(result)
    return {**result, "triggers": kept, "plays_off_dropped": len(rows) - len(kept)}


def _merge_dip_survivors(
    result: Mapping[str, Any], payload: Mapping[str, Any], loaded: Mapping[str, Any]
) -> dict:
    """Step 5's DIP merge: Appendix C's scanner survivors into `triggers`.

    Moved here from `services/core/core/run._union_triggers` so the
    live-session path, the runner path and the shadow ledger all merge — and
    rotation-filter — in the one function, rather than a session merging by
    hand after this command had already returned. Deduplicated on `(symbol,
    play_id)` against discovery's own triggers, which win. Never capped (Step
    5: only PEAD/CATALYST discovery is). A bare ticker is read as a DIP row,
    since the scanner produces nothing else.

    With `dip_entries_enabled` false no DIP row leaves this function: not the
    scanner's survivors, and not a DIP row the watchlist or a resubmit carried
    in. A trigger is what the runner fetches a quote, tradability, earnings
    and fundamentals for, so this is where the switch saves the work. How many
    were set aside is recorded as `dip_entries_off`. A held DIP position is
    not a trigger and is fetched as before.
    """
    if not plays.dip_entries_enabled(loaded.get("config")):
        rows = list(result.get("triggers") or [])
        kept_rows = [
            t for t in rows
            if not (isinstance(t, Mapping) and t.get("play_id") == plays.DIP)
        ]
        survivors_seen = sum(
            1 for row in payload.get("dip_survivors") or []
            if isinstance(row, str) or (isinstance(row, Mapping) and row.get("symbol"))
        )
        return {
            **result,
            "triggers": kept_rows,
            "dip_entries_off": {
                "reason": plays.DIP_ENTRIES_OFF_REASON,
                "survivors_ignored": survivors_seen,
                "triggers_dropped": len(rows) - len(kept_rows),
            },
        }

    triggers = list(result.get("triggers") or [])
    seen = {(t.get("symbol"), t.get("play_id")) for t in triggers}
    survivors: list[dict] = []
    for row in payload.get("dip_survivors") or []:
        if isinstance(row, str):
            row = {"symbol": row, "play_id": plays.DIP}
        if not isinstance(row, Mapping):
            continue
        key = (row.get("symbol"), row.get("play_id") or plays.DIP)
        if not key[0] or key in seen:
            continue
        seen.add(key)
        survivors.append({"symbol": key[0], "play_id": key[1]})

    kept, fell = _rotate(survivors, payload, loaded)

    # The scanner's list was the one discovery list with no ceiling:
    # `CANDIDATE_CAP` bounds PEAD/CATALYST events *before* this merge, and
    # nothing bounded the scan. 186 survivors -> 128 after rotation -> a
    # 149-symbol `resolve` and a 3 h 03 m cycle (21-22 symbols was normal). Docs
    # history: cycle-prompt.md said survivors were "never capped" (the design
    # expected ~5 a day) and the Dip Recovery sleeve's own pre-fetch truncation
    # is scan order; ranking exists only for events (recency) and for the
    # sleeve's *post*-fetch pick. Its own key, not the sleeve's
    # `max_scan_survivors`: this list feeds the *core* DIP play (entries, the
    # silent-entry streak, the mc prefilter), a different strategy.
    cap = dip_survivor_cap(loaded)
    overflow: list[dict] = []
    if len(kept) > cap:
        # Ranked, not truncated blind: the most oversold names are the ones the
        # DIP play would score highest, and the pre-market job has already
        # cached every screened name's daily RSI in `day_context` — free at this
        # point, no broker call. RSI extremity, `(30 - rsi) / 15`, is the *same*
        # term `plays/dip.py` sizes conviction on, so ascending RSI is that
        # ranking (its other term, depth below the 20-day, needs a live price
        # that does not exist before the fetch this cap exists to bound). A
        # name with no cached RSI (a scan hit outside the screened universe)
        # ranks after every name that has one, and ties and the unknowns keep
        # the scanner's own order — `sorted` is stable.
        day_context = payload.get("day_context") or {}
        rsi = {row["symbol"]: _cached_rsi(day_context, row["symbol"]) for row in kept}
        ranked = sorted(kept, key=lambda row: (rsi[row["symbol"]] is None, rsi[row["symbol"]] or 0.0))
        overflow = [
            candidate_discovery.dropped(
                row["symbol"],
                f"outside the {cap}-name DIP survivor cap, "
                + (
                    f"RSI {rsi[row['symbol']]:.1f}, ranked most oversold first"
                    if rsi[row["symbol"]] is not None
                    else "no cached RSI, kept after every name that has one, in scan order"
                ),
            )
            for row in ranked[cap:]
        ]
        kept = ranked[:cap]

    return {
        **result,
        "triggers": triggers + kept,
        "candidates_dropped": [*(result.get("candidates_dropped") or []), *fell, *overflow],
    }


def mc_prefilter(payload: Mapping[str, Any], loaded: Mapping[str, Any]) -> dict:
    """Step 6b's cheap gate, ahead of the Monte Carlo pre-trade check.

    `plays.would_open` is a pure, no-network function of data Step 6 has
    already fetched (`signals`, `quotes`, `earnings`, `events`, `macro`) — the
    same raw-trigger check `reconcile._adopt` already asks when binding an
    inherited position to a play. A candidate it refuses (RSI 27 but below the
    200-day, a PEAD window that already closed, an earnings-veto'd CATALYST)
    is about to fail `decide()`'s own play gate in Step 7 regardless of what a
    simulation says, so spending a `get_equity_historicals` fetch and a Vercel
    call on it buys nothing. A 24-candidate DIP scanner pool ran Monte Carlo
    on every name and every one of them then failed the identical 200-day
    check inside `decide` — 48 calls for zero candidates that were ever going
    to enter.

    The pool mirrors cycle-prompt.md §5.3 minus positions — `triggers`' and
    `macro_targets`' symbols only. Held positions never reach Step 6b at all
    (its own text already excludes them: `mc_check`'s denial is buy-only), so
    this does not re-derive that exclusion, it enforces the same one.

    This narrows Step 6b's loop; it never widens `decide`'s own judgment.
    `checked` is not a promise a candidate will enter — regime, crowding and
    sizing still run after MC in Step 7 — only that its own play's raw trigger
    is live today, the one fact this data can settle before spending the two
    calls.
    """
    cycle = _cycle(payload, loaded)
    held = {p.get("symbol") for p in payload.get("positions") or []}
    pool = sorted(
        {row.get("symbol") for row in payload.get("triggers") or [] if row.get("symbol")}
        | {s for s in payload.get("macro_targets") or [] if s}
    )
    pool = [symbol for symbol in pool if symbol not in held]

    ctx = {
        "cycle": cycle.as_dict(),
        "calendar": loaded["calendar"],
        "quotes": _quotes(payload),
        "signals": payload.get("signals") or {},
        "earnings": _earnings(payload, loaded, cycle),
        "events": payload.get("events") or {},
        "macro": payload.get("macro") or {},
        "macro_map": loaded["macro_map"],
    }

    checked: list[str] = []
    skipped: list[str] = []
    for symbol in pool:
        (checked if plays.would_open(symbol, ctx) is not None else skipped).append(symbol)

    return {**_envelope(payload, cycle), "checked": checked, "skipped_gate_failed": skipped}


def merge_fetch(payload: Mapping[str, Any], loaded: Mapping[str, Any]) -> dict:
    """Unions N background-agent fetch fragments into one symbol-keyed map.

    `tradability`, `signals` and `earnings` are all filled by partitioning the
    day's symbol set across parallel background-agent groups (`docs/routine/
    cycle-prompt.md` §6), and each group's own tool response is a `{symbol:
    {...}}` fragment scoped to just that group's symbols. This does the union
    mechanically so nothing is ever retyped.

    A symbol appearing in more than one fragment means the groups were not
    actually disjoint — a partitioning bug, not a merge decision. Picking one
    fragment's value over another would be a guess (Absolute 2 forbids it), so
    that symbol is dropped from `merged` and named in `conflicts` instead, the
    same "ambiguous is unusable" call `settled_cash` makes for a duplicated
    account entry below. A symbol missing from `merged` denies downstream —
    `guard/legality.py` treats an absent tradability entry as `NOT_TRADABLE` —
    rather than trading on a guessed value.
    """
    fragments = payload.get("fragments") or []
    merged: dict[str, Any] = {}
    conflicts: set[str] = set()
    for fragment in fragments:
        for symbol, value in (fragment or {}).items():
            if symbol in conflicts:
                continue
            if symbol in merged:
                conflicts.add(symbol)
                del merged[symbol]
            else:
                merged[symbol] = value

    cycle = _cycle(payload, loaded)
    return {
        **_envelope(payload, cycle),
        "merged": merged,
        "conflicts": sorted(conflicts),
    }


def _pending_deposits(
    portfolio: Mapping[str, Any], override: Any
) -> tuple[float, str]:
    """What to subtract for money the broker has credited but not received.

    **`get_portfolio` does expose this, confirmed.** The docstring below used
    to say no source field was verified — an early probe looked before an
    unsettled balance existed to reveal one, and every reading since has been
    of an account with nothing pending. An earlier runner payload carries
    `pending_deposits: "0"` at the top level of `get_portfolio`, so the field
    is real; what is still unverified is what it reads on a day a deposit is
    actually in flight. An explicit override therefore still wins, and the
    source is recorded either way so a runlog row says which number was used
    and where it came from.
    """
    if override is not None:
        return float(override), "override"
    reported = portfolio.get("pending_deposits")
    if reported is None:
        return 0.0, "absent"
    try:
        return float(reported), "get_portfolio"
    except (TypeError, ValueError):
        # A field that is present and unparseable is a broker change, not a
        # zero. Fail toward subtracting nothing, and say so.
        return 0.0, "unparseable"


def _settled_cash_value(
    portfolio: Mapping[str, Any],
    accounts: Sequence[Mapping[str, Any]],
    account_number: Any,
    pending_deposits: Any,
) -> tuple[float | None, str | None]:
    """``(value, problem)`` — the subtraction, or None and why not.

    One definition, because two callers need it: the `settled-cash` command a
    live session runs by hand, and `shape-account`, which the hosted core runs
    for the runner. Two implementations of "which account's unsettled funds"
    is the way the two paths would eventually disagree about a number that
    decides whether a buy is funded.
    """
    cash = portfolio.get("cash")
    if not account_number:
        return None, (
            "no account_number: get_accounts returns every account on the "
            "login and the unsettled figure is per account, so there is no "
            "safe default to pick"
        )

    matches = [
        a for a in accounts or [] if a.get("account_number") == str(account_number)
    ]
    if len(matches) != 1:
        return None, (
            f"{len(matches)} of {len(accounts or [])} get_accounts entries "
            f"match account_number {account_number!r}; exactly one is required"
        )
    unsettled = matches[0].get("unsettled_funds")

    if cash is None or unsettled is None:
        missing = "cash" if cash is None else "unsettled_funds"
        return None, f"{missing} is absent; settled cash is not a number to guess"

    amount, _source = _pending_deposits(portfolio, pending_deposits)
    return float(cash) - float(unsettled) - amount, None


def settled_cash(payload: Mapping[str, Any], loaded: Mapping[str, Any]) -> dict:
    """Step 6's one permitted subtraction: ``cash - unsettled_funds -
    pending_deposits``.

    Takes `get_portfolio` and `get_accounts` pasted through unchanged, plus
    `account_number` to pick the right entry out of `get_accounts`' list —
    never defaulted, the same guard `get_equity_orders` already puts on that
    parameter, because a wrong account's unsettled figure is wrong in a
    direction nothing downstream can detect. An earlier payload settles that
    this is not theoretical: `get_accounts` returned **two** accounts on this
    login, a `margin` one with `agentic_allowed: false` and the `cash` one the
    system trades, and picking the first would have read the wrong account's
    unsettled funds forever.

    Returns ``{"settled_cash": null}`` — never a guess — whenever `cash` or
    the matched `unsettled_funds` is missing or the match is not exactly one.
    Absolute 2 forbids inventing a number and `sizing.settled_basis` already
    treats a null `settled_cash` as safe (falls back to `buying_power` alone),
    so null is the correct answer here, not a failure to produce one.
    """
    value, problem = _settled_cash_value(
        payload.get("portfolio") or {},
        payload.get("accounts") or [],
        payload.get("account_number"),
        payload.get("pending_deposits"),
    )
    cycle = _cycle(payload, loaded)
    return {**_envelope(payload, cycle), "settled_cash": value, "problem": problem}


PLACEMENTS_INVALID = "PLACEMENTS_INVALID"

_PLACEMENT_SIDES = ("buy", "sell")


def _is_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _placement_row_problems(row: Any) -> list[str]:
    if not isinstance(row, Mapping):
        return ["not an object"]
    problems: list[str] = []

    order_id = row.get("order_id")
    if not isinstance(order_id, str) or not order_id.strip():
        problems.append("order_id missing or not a non-empty string")

    symbol = row.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        problems.append("symbol missing or not a non-empty string")

    side = row.get("side")
    if not isinstance(side, str) or side.strip().lower() not in _PLACEMENT_SIDES:
        problems.append(f"side missing or not one of {_PLACEMENT_SIDES}")

    if not _is_number(row.get("quantity")) or float(row["quantity"]) <= 0:
        problems.append("quantity missing or not a number > 0")

    limit_price = row.get("limit_price")
    if limit_price is not None and not _is_number(limit_price):
        problems.append("limit_price present but not a number or null")

    for flag in ("protective_stop", "protective_target"):
        value = row.get(flag)
        if value is not None and not isinstance(value, bool):
            problems.append(f"{flag} present but not a boolean")

    for opt in ("play_id", "session"):
        value = row.get(opt)
        if value is not None and not isinstance(value, str):
            problems.append(f"{opt} present but not a string or null")

    return problems


def _shape_placement_row(row: Mapping[str, Any]) -> dict:
    limit_price = row.get("limit_price")
    return {
        "order_id": row["order_id"].strip(),
        "symbol": row["symbol"].strip().upper(),
        "side": row["side"].strip().lower(),
        "quantity": float(row["quantity"]),
        "play_id": row.get("play_id"),
        "protective_stop": bool(row.get("protective_stop", False)),
        "protective_target": bool(row.get("protective_target", False)),
        "limit_price": None if limit_price is None else float(limit_price),
        "session": row.get("session"),
    }


def _rejection_row_problems(row: Any) -> list[str]:
    if not isinstance(row, Mapping):
        return ["not an object"]
    problems: list[str] = []

    symbol = row.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        problems.append("symbol missing or not a non-empty string")

    side = row.get("side")
    if not isinstance(side, str) or side.strip().lower() not in _PLACEMENT_SIDES:
        problems.append(f"side missing or not one of {_PLACEMENT_SIDES}")

    order_type = row.get("type")
    if not isinstance(order_type, str) or not order_type.strip():
        problems.append("type missing or not a non-empty string")

    if not _is_number(row.get("quantity")) or float(row["quantity"]) <= 0:
        problems.append("quantity missing or not a number > 0")

    detail = row.get("detail")
    if not isinstance(detail, str) or not detail.strip():
        problems.append("detail (verbatim broker text) missing or not a non-empty string")

    limit_price = row.get("limit_price")
    if limit_price is not None and not _is_number(limit_price):
        problems.append("limit_price present but not a number or null")

    session = row.get("session")
    if session is not None and not isinstance(session, str):
        problems.append("session present but not a string or null")

    return problems


def _shape_rejection_row(row: Mapping[str, Any]) -> dict:
    limit_price = row.get("limit_price")
    return {
        "order": {
            "symbol": row["symbol"].strip().upper(),
            "side": row["side"].strip().lower(),
            "type": row["type"].strip().lower(),
            "quantity": float(row["quantity"]),
            "limit_price": None if limit_price is None else float(limit_price),
            "session": row.get("session"),
        },
        # Absolute 5: the broker's sentence, exactly as it was said.
        "detail": row["detail"],
    }


SOLD_SIDE = "sell"


def _sold_this_session(orders: Any, trading_day: str) -> bool:
    """§2.4's same-session-sale flag, from `get_equity_orders` alone.

    The live session is told to work this out by hand ("true if any order
    filled today has side sell"); the runner is hands with no rules and cannot
    be asked to. So it is derived here, from the executions the broker already
    attaches to each order — which is where the session was reading it from
    anyway.

    **Executions, not `state`.** A `filled` order carries the day it was
    *placed* in `created_at`, and a GTC sell placed three weeks ago that fills
    today would read as an old order on that field. Each execution carries its
    own timestamp, and that is the only one that answers "did a sale settle
    into this session".

    Conservative on unreadable input: an execution with no parseable timestamp
    counts as today's. Under `POLICY_CONSERVATIVE` this flag denies buys, so
    the safe direction is to raise it, and under the shipped
    `buying_power_is_settled` policy it is not read at all.
    """
    for order in orders or []:
        if not isinstance(order, Mapping) or order.get("side") != SOLD_SIDE:
            continue
        for execution in order.get("executions") or []:
            if not isinstance(execution, Mapping):
                continue
            stamp = execution.get("timestamp") or execution.get("settlement_date")
            if stamp is None:
                return True
            try:
                if cadence.trading_day(
                    datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                ).isoformat() == trading_day:
                    return True
            except ValueError:
                return True
    return False


# Appendix D's `account`, and where each field is copied from. Named rather
# than inlined so `shape_account` reads as the map it is: three copies and
# nothing else, because Absolute 2 reserves everything but copying for the one
# subtraction `_settled_cash_value` makes.
_ACCOUNT_FIELDS = (
    ("equity_total", ("total_value",)),
    ("cash", ("cash",)),
    ("buying_power", ("buying_power", "buying_power")),
)


def shape_account(payload: Mapping[str, Any], loaded: Mapping[str, Any]) -> dict:
    """Appendix D's `account`, from the two raw broker responses.

    **Why this exists.** `_fetch_plan` names two account steps — `account`
    (`get_portfolio`) and `balances` (`get_accounts`) — and Appendix D has one
    `account` key that both feed. A live session merges them by hand under
    Step 6's four rules; the runner (`docs/routine/runner-prompt.md`) posts
    each tool response verbatim under its step's own name and holds no rules
    at all, so on the hosted path nobody was doing the merge. An early
    runner cycle died on it: `assemble` refused the `balances` row as not an
    Appendix D key, and had it not, `guard.halts` would have refused the cycle
    anyway on an `account` that carried `total_value` and no `equity_total`.

    **Three copies and one subtraction.** `equity_total`, `cash` and
    `buying_power` are copied character-for-character out of `get_portfolio` —
    the broker returns them as strings and they stay strings, because
    `guard`/`sizing` already `float()` what they read and Absolute 2 does not
    make an exception for a cast that looks harmless. `settled_cash` is
    `_settled_cash_value`, the same arithmetic the `settled-cash` command
    performs for a live session. `sold_this_session` is derived from the
    orders' own executions.

    **A missing field is omitted, never nulled or defaulted.** An absent
    `equity_total` is what `guard.halts` reads as ACCOUNT_UNREADABLE, and that
    is the correct outcome for a portfolio response the broker changed the
    shape of — `problems` says which field went missing so the runlog names it
    instead of leaving an operator to diff two JSON blobs.
    """
    cycle = _cycle(payload, loaded)
    portfolio = payload.get("portfolio") or {}
    accounts = payload.get("accounts") or []
    # The payload's own value wins so a session recovering by hand can pass
    # one; `config/inception.json` is where it lives the rest of the time,
    # beside the rest of this account's identity.
    account_number = payload.get("account_number") or (loaded.get("inception") or {}).get(
        "account_number"
    )

    account: dict[str, Any] = {}
    problems: list[str] = []

    for field, path in _ACCOUNT_FIELDS:
        value: Any = portfolio
        for key in path:
            value = value.get(key) if isinstance(value, Mapping) else None
        if value is None:
            problems.append(
                f"{field}: get_portfolio has no {'.'.join(path)}; the key is "
                "left out rather than guessed"
            )
            continue
        account[field] = value

    settled, problem = _settled_cash_value(
        portfolio, accounts, account_number, payload.get("pending_deposits")
    )
    if problem is not None:
        problems.append(f"settled_cash: {problem}")
    # Null, not omitted: `sizing.settled_basis` reads an explicit null as "fall
    # back to buying_power alone", which is the documented safe degradation.
    account["settled_cash"] = settled
    # Normalised here rather than through `_normalized`, which calls this: the
    # executions this reads hang off `get_equity_orders` rows, and a runner
    # that posted the response inside its own envelope would otherwise hand
    # `_sold_this_session` a list with no orders in it and get a confident
    # `false` for a day the account did sell.
    account["sold_this_session"] = _sold_this_session(
        broker_shapes.orders(payload.get("orders")), cycle.trading_day
    )

    return {
        **_envelope(payload, cycle),
        "account": account,
        "account_number": account_number,
        "problems": problems,
    }


def shape_placements(payload: Mapping[str, Any], loaded: Mapping[str, Any]) -> dict:
    """Step 8's placement/rejection/alert bundle, shaped by the core instead of
    the session's own hand.

    Reads a raw `placements_raw.json` — `{"placements": [...], "rejections":
    [...], "alerts": [...]}` — and returns the same three lists in the exact
    shape `edge_runlog_write`'s `placements`/`rejections` arguments and
    `edge_alerts_enqueue` expect: a `placements` row is
    `{order_id, symbol, side, quantity, play_id, protective_stop,
    protective_target, limit_price, session}` with the flags defaulted and the
    numbers coerced; a `rejections` row is `{order: {...}, detail}` with the
    broker's sentence carried through untouched (Absolute 5). `alerts` with a
    canonical title and a body pass straight through — rewording one would be
    the same violation; every other row moves to `runner_notes` unchanged.

    Any structurally invalid row fails the whole call with a `PLACEMENTS_INVALID`
    refusal naming every bad row — a malformed `placements` list is exactly the
    §12.2 diff input that must stop the cycle, never be written with a silent
    drop. The `placements_raw.json` on stdin carries the usual
    `now`/`control`/`cycle_id` envelope alongside the three raw lists, so the
    cycle is derived here like every other command (Invariant 6).
    """
    cycle = _cycle(payload, loaded)
    raw_placements = payload.get("placements") or []
    raw_rejections = payload.get("rejections") or []

    invalid: list[dict] = []
    for index, row in enumerate(raw_placements):
        problems = _placement_row_problems(row)
        if problems:
            invalid.append({"list": "placements", "index": index, "problems": problems})
    for index, row in enumerate(raw_rejections):
        problems = _rejection_row_problems(row)
        if problems:
            invalid.append({"list": "rejections", "index": index, "problems": problems})

    if invalid:
        return _refusal(
            "shape-placements",
            PLACEMENTS_INVALID,
            f"{len(invalid)} row(s) failed validation: {invalid}",
        )

    # An alert reaches `edge_alerts_enqueue` only if it carries a canonical
    # title and a body. Anything else — an earlier `{code, detail}` row, which
    # enqueued as a null-title insert and killed finalize after the orders
    # were already placed — is kept as a note for the report runlog row
    # instead of being reworded into an alert or dropped.
    alerts: list[dict] = []
    runner_notes: list[dict] = []
    for alert in payload.get("alerts") or []:
        if (
            isinstance(alert, Mapping)
            and alert.get("title") in reporter.CANONICAL_TITLES
            and str(alert.get("body") or "").strip()
        ):
            alerts.append(alert)
        else:
            runner_notes.append(alert)

    return {
        **_envelope(payload, cycle),
        "command": "shape-placements",
        "placements": [_shape_placement_row(row) for row in raw_placements],
        "rejections": [_shape_rejection_row(row) for row in raw_rejections],
        "alerts": alerts,
        "runner_notes": runner_notes,
    }


def dip_recovery_decide(payload: Mapping[str, Any], loaded: Mapping[str, Any]) -> dict:
    """One Dip Monitor tick: fills, then management, then new entries.

    A thin command, the same shape as `reconcile` and `report` — it assembles a
    context and hands `src.dip_recovery.evaluate`'s result straight back for the
    Dip Monitor routine to write. Every rule lives in `src/dip_recovery`, not
    here: the seven entry conditions and the 0-100 score (`signal.py`), the
    no-loss averaging machine, the T+1 and 47-trading-day clocks, and the
    5-position / 3-per-day caps (`engine.py`). This module only composes.

    `strategy` is fixed to `dip_recovery.STRATEGY` because this command decides
    for exactly one side, and that string is what `guard.lock`,
    `guard.strategy` and `guard.schedule` key their Dip-Recovery branches on
    when the routine puts a proposed order through `guard.evaluate` before it
    places anything — the design doc's "every existing portfolio, account,
    concentration, and guardrail check passes" is still guard's to enforce, not
    this package's to re-implement.

    The market reads (`quotes`/`signals`/`fundamentals`) and the four
    Dip-Recovery keys arrive already fetched in `cycle.json`, exactly as
    `reconcile`/`decide` take theirs — `dip_recovery_bars` is one
    `get_equity_historicals` call per watchlist symbol (`interval=day`, enough
    history for `bollinger_period`), normalized to `{high, low, close}` oldest
    first. A tick that fetched none of them still runs: each falls back to
    empty and `evaluate` proposes nothing.

    The routine turns this output into four writes, every one stamped with the
    cycle's own id: `edge_dip_recovery_positions_apply(position_updates)`,
    `edge_symbol_lock_claim`/`_release` over `symbol_lock_claims`/`releases`,
    `edge_dip_recovery_evaluations_append(evaluations)`, and the generic
    `edge_control_apply` for the `dip_recovery_new_positions` counter in
    `control_updates`.
    """
    cycle = _cycle(payload, loaded)

    # One context, the keys `src/dip_recovery` and `guard` read between them.
    # `_account` joins `net_deposits` the same way it does for `decide`, since
    # the Dip Recovery ledger sizes against the same flow-adjusted basis §10
    # measures the drawdown halt against.
    #
    # Phase 7 added the guard half. `dip_recovery.evaluate` now calls
    # `guard.evaluate` itself, exactly as `decide` always has, so every key
    # guard reads has to be here — and four of guard's categories fail
    # **closed** on an absent key (`earnings`, `tradability`, `sectors`, and
    # concentration via `positions`). Wiring guard in without them would have
    # denied every Dip Recovery buy four times over, which is the shape of an
    # earlier incident where two gates failed closed under a green suite.
    dip_positions = payload.get("dip_recovery_positions") or {}

    # The day's regime, the same one `decide` judges core's entries on. The Dip
    # Monitor reads the pre-market job's `edge_regime` row and posts its
    # `regime_inputs`; a tick that posts none gets no regime, which
    # `dip_recovery.evaluate` reads as "no opinion" and does not gate on.
    regime = regimes.evaluate(
        payload.get("regime_inputs"),
        loaded["config"],
        macro_event_day=_macro_event_day(payload, cycle.trading_day),
    )

    ctx: dict[str, Any] = {
        "cycle": cycle.as_dict(),
        "calendar": loaded["calendar"],
        "config": loaded["config"],
        "control": payload.get("control") or {},
        # The sleeve's eleven GICS names, which the operator's sector
        # rotation is read against (`engine.evaluate`); 11a adds the
        # rotation codes and each member's GICS sub-industry.
        "etfs": loaded["etfs"],
        "rotation_codes": loaded.get("rotation_codes") or {},
        "sub_industries": universes.sub_industry_map(payload.get("snapshot") or {}),
        "account": _account(payload, loaded, cycle),
        "quotes": _quotes(payload),
        "signals": payload.get("signals") or {},
        "fundamentals": payload.get("fundamentals") or {},
        "symbol_lock": payload.get("symbol_lock") or {},
        "strategy": dip_recovery.STRATEGY,
        "regime": None if regime is None else regime.as_dict(),
        "dip_recovery_config": payload.get("dip_recovery_config") or {},
        "dip_recovery_positions": dip_positions,
        "dip_recovery_watchlist": payload.get("dip_recovery_watchlist") or [],
        "dip_recovery_fills": payload.get("dip_recovery_fills") or [],
        "dip_recovery_bars": payload.get("dip_recovery_bars") or {},
        # `guard._PlanState` builds its slot count and its position/sector
        # exposure from `positions`. On this path that must be **this side's**
        # book, not core's: core's holdings are none of this strategy's
        # concentration, and `guard.lock` already forbids it touching them.
        "positions": _dip_book(dip_positions, payload),
        # Issuer holdings file over the broker's answer, a settled design
        # decision — the same precedence `decide` applies, via the same
        # `universe.sector_map`.
        "sectors": {
            **(payload.get("sectors") or {}),
            **universes.sector_map({"snapshot": payload.get("snapshot") or {}, "etfs": loaded["etfs"]}),
        },
        # `_earnings` derives `sessions_until_report` from `get_earnings_results`;
        # `guard._earnings_denials` reads that field and denies outright when it
        # is missing ("the T-2 flatten is a promise that cannot be kept blind").
        # Kept for this side deliberately: averaging down into a print is
        # exactly how a no-loss strategy gets stuck below cost.
        "earnings": _earnings(payload, loaded, cycle),
        "tradability": payload.get("tradability") or {},
        "loss_closes": payload.get("loss_closes") or [],
        "open_orders": payload.get("open_orders") or [],
        # `mc_check` is deliberately **absent**, not empty. `guard.mc` opens
        # with `if "mc_check" not in ctx: return []`, but a present-and-empty
        # map denies every buy `MC_SIMULATION_UNAVAILABLE` — the one key in
        # this file where the house `payload.get(x) or {}` idiom would invert
        # the meaning. Dip Recovery runs no Monte Carlo pass; its entry gate is
        # `signal.entry_signal`'s seven conditions.
    }

    result = dip_recovery.evaluate(ctx).as_dict()

    # `edge_dip_recovery_evaluations_append` reads `cycle_id` off each row, and
    # the engine has no cycle to stamp — joined here the same way `report`
    # stamps `cycle_id` onto its alerts rather than asking `report.py` for it.
    cycle_id = payload.get("cycle_id")
    result["evaluations"] = [{**row, "cycle_id": cycle_id} for row in result["evaluations"]]

    # The regime it judged on and whether it was enforced, on the same runlog
    # row as the orders — `shadow_posture` mirrors `decide`'s: the posture
    # while the gate is only being recorded, null once it is enforced.
    enabled = dip_recovery.regime_v2_enabled(ctx)
    result["regime"] = ctx["regime"]
    result["regime_v2_enabled"] = enabled
    result["shadow_posture"] = None if enabled or regime is None else regime.posture

    return {**_envelope(payload, cycle), **result}


def manual_check_decide(payload: Mapping[str, Any], loaded: Mapping[str, Any]) -> dict:
    """One on-demand Manual Check: every real holding, judged by Dip's rules.

    A thin command in the shape of `dip_recovery_decide`, and for the same
    reason: every rule lives elsewhere (`src/manual_check.py` composes the Dip
    Recovery engine's own helpers, and `guard.evaluate` rules on the plan).
    What this builds is the context.

    Four deliberate differences from `dip_recovery_decide`'s:

    * `symbol_lock` is empty. The operator asked for every holding, core's
      included, and the routine holds the §13.1 run lock for the whole run, so
      no scheduled cycle can be placing against the same shares. Nothing here
      writes a lock, a Dip row or a counter either (`manual_check` returns none
      of them), so who owns a symbol is exactly what it was before the run.
    * `positions` is the **whole broker book**, not the Dip side's, so guard's
      slot, position and sector caps see the exposure the account really has.
    * An all-day-hours session (20:00 to 07:00 ET) is judged as an overnight
      cycle, which switches on guard's overnight spread gate. The cadence
      classes the manual firing as a no-op there when no whole-share position
      is flagged, and a no-op carries no spread gate.
    * **A Saturday or Sunday is not "closed" here**. The cadence says `closed`
      on a weekend and guard turns that into a `SESSION_CLOSED` halt of every
      order; for this command alone the session is read as all-day hours, so
      guard's other rules, the overnight spread gate included, judge the
      orders. Guard and cadence themselves are untouched, so no scheduled
      routine changes. A **market holiday** stays `closed`: nothing has been
      observed about it, so the run reports and places nothing.
    """
    cycle = _cycle(payload, loaded)
    cycle_dict = cycle.as_dict()
    weekend = (
        cycle.session == cadence.CLOSED
        and date.fromisoformat(cycle.trading_day).weekday() >= cadence.SATURDAY
    )
    if weekend:
        cycle_dict["session"] = cadence.ALL_DAY_HOURS
    if cycle_dict["session"] == cadence.ALL_DAY_HOURS:
        cycle_dict["cycle_type"] = cadence.OVERNIGHT

    regime = regimes.evaluate(
        payload.get("regime_inputs"),
        loaded["config"],
        macro_event_day=_macro_event_day(payload, cycle.trading_day),
    )

    ctx: dict[str, Any] = {
        "cycle": cycle_dict,
        "calendar": loaded["calendar"],
        "config": loaded["config"],
        "control": payload.get("control") or {},
        "etfs": loaded["etfs"],
        "rotation_codes": loaded.get("rotation_codes") or {},
        "sub_industries": universes.sub_industry_map(payload.get("snapshot") or {}),
        "account": _account(payload, loaded, cycle),
        "quotes": _quotes(payload),
        "signals": {},
        "fundamentals": {},
        "symbol_lock": {},
        "strategy": dip_recovery.STRATEGY,
        "regime": None if regime is None else regime.as_dict(),
        "dip_recovery_config": payload.get("dip_recovery_config") or {},
        "dip_recovery_positions": payload.get("manual_rows") or {},
        "dip_recovery_watchlist": [],
        "dip_recovery_fills": [],
        "dip_recovery_bars": {},
        "positions": payload.get("positions") or [],
        "sectors": {
            **(payload.get("sectors") or {}),
            **universes.sector_map({"snapshot": payload.get("snapshot") or {}, "etfs": loaded["etfs"]}),
        },
        "earnings": _earnings(payload, loaded, cycle),
        "tradability": payload.get("tradability") or {},
        "loss_closes": payload.get("loss_closes") or [],
        "open_orders": payload.get("open_orders") or [],
        # `mc_check` absent on purpose, as in `dip_recovery_decide`.
    }

    result = manual_check.evaluate(ctx)
    result["regime"] = ctx["regime"]
    result["weekend_run"] = weekend
    return {**_envelope(payload, cycle), "command": manual_check.COMMAND, **result}


def _pick_universe(payload: Mapping[str, Any], loaded: Mapping[str, Any], cycle) -> dict:
    """Core's membership fence, resolved for the pick's own use.

    `universes.resolve` and not a hand-rolled member set, for the reason
    `sector_map` was factored out in phase 7: two derivations of "who is in the
    universe" would let the watchlist admit a name guard's own candidate check
    would then refuse. Only `members` and the staleness verdict are read here —
    `candidates` is core's §5.3 pool, which this side deliberately does not use
    (that is what the watchlist replaces).
    """
    return universes.resolve(
        {
            "trading_day": cycle.trading_day,
            "snapshot": payload.get("snapshot") or {},
            "etfs": loaded["etfs"],
            "positions": payload.get("positions") or [],
            "config": loaded["config"],
        }
    ).as_dict()


def dip_recovery_pool(payload: Mapping[str, Any], loaded: Mapping[str, Any]) -> dict:
    """Universe Pick step 1: the scan's tickers down to a fetch list.

    Runs before a single per-symbol broker call, on four things that cost
    nothing — the scan's tickers, `data/universe.json` from the checkout, the
    symbol lock and this side's own position rows — plus `edge_loss_closes`,
    one row set already read for the cycle. `fetch` is what step 2's per-symbol
    reads should cover; `skipped` is every name that did not make it and why,
    including the ones past `max_scan_survivors`.
    """
    cycle = _cycle(payload, loaded)

    ctx: dict[str, Any] = {
        "cycle": cycle.as_dict(),
        "config": loaded["config"],
        "dip_recovery_config": payload.get("dip_recovery_config") or {},
        # `pool` applies the operator's sector rotation before the fetch
        # ceiling, reading `active_sectors` off `control` and the eleven
        # GICS names off the sleeve.
        "control": payload.get("control") or {},
        "etfs": loaded["etfs"],
        "rotation_codes": loaded.get("rotation_codes") or {},
        "sub_industries": universes.sub_industry_map(payload.get("snapshot") or {}),
        "universe": _pick_universe(payload, loaded, cycle),
        "scan": payload.get("scan") or [],
        "symbol_lock": payload.get("symbol_lock") or {},
        "dip_recovery_positions": payload.get("dip_recovery_positions") or {},
        "positions": payload.get("positions") or [],
        "loss_closes": payload.get("loss_closes") or [],
    }

    return {**_envelope(payload, cycle), **dip_recovery.pool(ctx).as_dict()}


def dip_recovery_pick(payload: Mapping[str, Any], loaded: Mapping[str, Any]) -> dict:
    """Universe Pick step 2: rank the fetched pool into tomorrow's watchlist.

    `earnings` is derived here exactly as it is for `decide` and
    `dip-recovery-decide` — `_earnings_view` turns `get_equity_results`'
    quarter rows into the `sessions_until_report` the event-risk exclusion
    reads, and a payload that arrived pre-derived would be asserting a contract
    nothing in `src/` implements.

    `control_updates` is the one write this routine makes:
    `edge_control_apply` over `dip_recovery_watchlist`, shaped
    `{trading_day, symbols}` — the same key `dip-recovery-decide` later reads
    as a plain symbol list.
    """
    cycle = _cycle(payload, loaded)

    ctx: dict[str, Any] = {
        "cycle": cycle.as_dict(),
        "config": loaded["config"],
        "dip_recovery_config": payload.get("dip_recovery_config") or {},
        "fetch": payload.get("fetch") or [],
        "quotes": _quotes(payload),
        "signals": payload.get("signals") or {},
        "fundamentals": payload.get("fundamentals") or {},
        "earnings": _earnings(payload, loaded, cycle),
    }

    return {**_envelope(payload, cycle), **dip_recovery.pick(ctx).as_dict()}


COMMANDS = {
    "plan-fetch": plan_fetch,
    "decide": decide,
    "reconcile": reconcile,
    "report": report,
    "candidates": candidates,
    "mc-prefilter": mc_prefilter,
    "merge-fetch": merge_fetch,
    "settled-cash": settled_cash,
    "shape-account": shape_account,
    "shape-placements": shape_placements,
    "day-context-plan": day_context_plan,
    "day-context-write-symbol": day_context_write_symbol,
    "dip-recovery-decide": dip_recovery_decide,
    "dip-recovery-pool": dip_recovery_pool,
    "dip-recovery-pick": dip_recovery_pick,
    "manual-check-decide": manual_check_decide,
}


def main(
    argv: Sequence[str],
    stdin,
    stdout,
    config_dir: Path | str | None = None,
) -> int:
    """One invocation: `uv run python -m src.cli <command> < cycle.json`."""
    command = argv[0] if argv else None

    if command not in COMMANDS:
        json.dump(
            _refusal(
                command,
                UNKNOWN_COMMAND,
                f"{command!r} is not one of {', '.join(sorted(COMMANDS))}",
            ),
            stdout,
        )
        return EXIT_USAGE

    try:
        payload = json.load(stdin)
    except ValueError as exc:
        json.dump(_refusal(command, BAD_INPUT, str(exc)), stdout)
        return EXIT_BAD_INPUT

    json.dump(COMMANDS[command](payload, load_config(config_dir)), stdout)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main(sys.argv[1:], sys.stdin, sys.stdout))
