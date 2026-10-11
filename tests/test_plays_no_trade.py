"""§4.3 F — the no-trade conditions, and the fact that they are off.

Strategy F is proposed with an explicit instruction attached:
"Shadow: count suppressed trades and their hypothetical P&L". So the first and
most important thing these tests pin is that **the shipped config does not
switch it on** — `config/config.json` omits `no_trade_conditions`, every
existing cycle reads that as false, and `plays.entries` returns exactly what it
returned before this module existed. The shadow ledger's `D_no_trade` variant
is the only thing that turns it on, by config override.

The rest pin the four legs, and in particular the two ways each differs from
the gate that already covers it:

* the vol leg is **relative** (1.5x the trailing median) where `regime.py`'s is
  **absolute** (`regime_vol_max`), so the two disagree in both directions;
* the divergence leg fires on the **first** open divergence where
  `reconcile.py` waits for the second.
"""

from src import plays
from src.plays import no_trade
from tests.factories import config, plays_context

# `plays_context` is a 09:40 full cycle where DIP fires on F at 0.70 conviction
# and nothing else does. One candidate is what makes a suppression legible: the
# entry list goes from one name to none, and the skip says why.
ON = {"no_trade_conditions": True}


def ctx(**overrides):
    return plays_context(**overrides)


def entered(context):
    return [c.symbol for c in plays.entries(context).candidates]


def skips(context, code):
    return [s for s in plays.entries(context).skipped if s.code == code]


# ---------------------------------------------------------------------------
# Off by default — the property the live account depends on
# ---------------------------------------------------------------------------


def test_the_shipped_config_does_not_switch_the_no_trade_conditions_on():
    assert "no_trade_conditions" not in config()
    assert not no_trade.enabled({"config": config()})


def test_with_the_gate_off_nothing_is_suppressed_even_when_every_leg_would_fire():
    context = ctx(
        account={"equity_total": 0.0},
        control={"high_water_mark": 236.42, "divergence_streak": 3},
        quotes={"F": {"bid": 10.00, "ask": 12.00, "last": 14.88}},
    )

    assert entered(context) == ["F"]
    assert no_trade.blockers(context) == []


# ---------------------------------------------------------------------------
# The spread leg — the one condition nothing else covers
# ---------------------------------------------------------------------------


def test_a_candidate_wider_than_the_ceiling_is_suppressed_by_name():
    # 0.4% against a 0.3% ceiling.
    context = ctx(
        config=config(**ON),
        quotes={"F": {"bid": 14.85, "ask": 14.91, "last": 14.88}},
    )

    assert entered(context) == []
    suppressed = skips(context, no_trade.NO_TRADE_SPREAD)
    assert [s.symbol for s in suppressed] == ["F"]
    assert "0.30%" in suppressed[0].message


def test_a_candidate_inside_the_ceiling_is_untouched():
    context = ctx(
        config=config(**ON),
        quotes={"F": {"bid": 14.87, "ask": 14.89, "last": 14.88}},
    )

    assert entered(context) == ["F"]


def test_an_unquotable_candidate_is_not_counted_as_a_spread_suppression():
    # Sizing cannot price a rung with no ask and guard fails it closed, so a
    # suppression here would relabel an existing refusal and inflate what §4.3
    # F appears to have cost.
    context = ctx(config=config(**ON), quotes={"F": {"last": 14.88}})

    assert no_trade.spread_reason("F", context) is None


# ---------------------------------------------------------------------------
# The vol leg — relative, where regime.py's is absolute
# ---------------------------------------------------------------------------


def rising_closes(n: int, step: float, tail: int = 0, tail_step: float = 0.0):
    """A price path: `n` bars moving `step` per day, then `tail` moving
    `tail_step`. Alternating sign so the series has a measurable vol at all."""
    closes = [100.0]
    for i in range(n):
        closes.append(closes[-1] * (1 + (step if i % 2 else -step)))
    for i in range(tail):
        closes.append(closes[-1] * (1 + (tail_step if i % 2 else -tail_step)))
    return closes


def test_the_vol_leg_needs_both_legs_and_a_trend_alone_is_not_enough():
    context = ctx(
        config=config(**ON),
        regime_inputs={
            "spy": {
                "last": 590.0,
                "sma200": 600.0,
                "closes": rising_closes(60, 0.002),
            }
        },
    )

    assert no_trade.blockers(context) == []
    assert entered(context) == ["F"]


def test_vol_over_the_trailing_median_with_spy_below_its_200_day_stops_everything():
    context = ctx(
        config=config(**ON),
        regime_inputs={
            "spy": {
                "last": 590.0,
                "sma200": 600.0,
                # Forty quiet bars, then twenty violent ones: the last 20-day
                # window is far over the median of the windows before it.
                "closes": rising_closes(40, 0.001, tail=20, tail_step=0.05),
            }
        },
    )

    codes = [code for code, _ in no_trade.blockers(context)]
    assert codes == [no_trade.NO_TRADE_VOL]
    assert entered(context) == []
    assert [s.symbol for s in skips(context, no_trade.NO_TRADE_VOL)] == ["F"]


def test_the_same_vol_above_spys_200_day_suppresses_nothing():
    context = ctx(
        config=config(**ON),
        regime_inputs={
            "spy": {
                "last": 610.0,
                "sma200": 600.0,
                "closes": rising_closes(40, 0.001, tail=20, tail_step=0.05),
            }
        },
    )

    assert no_trade.blockers(context) == []


def test_too_few_closes_abstains_rather_than_comparing_against_two_windows():
    context = ctx(
        config=config(**ON),
        regime_inputs={
            "spy": {"last": 590.0, "sma200": 600.0, "closes": rising_closes(25, 0.05)}
        },
    )

    assert no_trade.blockers(context) == []


# ---------------------------------------------------------------------------
# The divergence leg — the first one, not the second
# ---------------------------------------------------------------------------


def test_one_open_divergence_is_enough_where_reconcile_waits_for_two():
    from src.reconcile import PERSISTENT_DIVERGENCE_CYCLES

    assert PERSISTENT_DIVERGENCE_CYCLES == 2  # the rule F is stricter than

    context = ctx(
        config=config(**ON),
        control={"high_water_mark": 236.42, "divergence_streak": 1},
    )

    codes = [code for code, _ in no_trade.blockers(context)]
    assert codes == [no_trade.NO_TRADE_DIVERGENCE]
    assert entered(context) == []


def test_a_clean_reconcile_blocks_nothing():
    context = ctx(
        config=config(**ON),
        control={"high_water_mark": 236.42, "divergence_streak": 0},
    )

    assert no_trade.blockers(context) == []


# ---------------------------------------------------------------------------
# The equity leg — restated, never the only thing stopping an order
# ---------------------------------------------------------------------------


def test_an_unreadable_equity_total_suppresses_at_proposal_time():
    context = ctx(config=config(**ON), account={"equity_total": None})

    codes = [code for code, _ in no_trade.blockers(context)]
    assert codes == [no_trade.NO_TRADE_EQUITY]
    assert entered(context) == []


def test_two_legs_firing_at_once_both_appear_in_the_reason():
    context = ctx(
        config=config(**ON),
        account={"equity_total": 0.0},
        control={"high_water_mark": 236.42, "divergence_streak": 2},
    )

    suppressed = skips(context, no_trade.NO_TRADE_EQUITY)
    assert suppressed
    assert "equity_total" in suppressed[0].message
    assert "divergence" in suppressed[0].message
