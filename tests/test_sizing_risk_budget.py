"""Volatility-aware sizing, off by default.

    notional = min(slot notional, risk_per_trade_pct x equity / stop_distance_pct)

The even split asks how many names the account holds; the risk budget asks how
much the account is willing to lose if this one is wrong. The tighter of the
two wins, so two names with the same slot but very different stops get the same
*dollar* exposure to being wrong rather than the same notional.

Three properties this file exists to pin:

* it is **off** in the shipped config, which is what keeps every sizing golden
  byte-identical;
* it can only ever **cut** a slot, never fund one past the ladder;
* the stop it divides by is the **position's own** — `plays.core.stop_distance`,
  the same resolver `exits` and `protection` read — so a `SECTOR` candidate is
  sized against the 1.5xATR stop it will actually be exited on.
"""

from __future__ import annotations

import pytest

from src import plays, sizing
from tests.factories import candidate, config, context, entry, reasons

# $10,000 at a $100 name: 25 slots and a $392 rung. Every price below is the
# ask marked up by `entry_limit_offset_atr` x ATR (§4.3), so a $20 ATR quotes a
# $102.00 limit and a 3xATR stop $60 wide — 58.8% of it. $100 of risk funds
# $170 there, against the rung's $392, which is the case the budget binds on.
EQUITY = 10_000.0
SLOT = 392.0


def risk_ctx(atr: float, enabled: bool = True, **overrides) -> dict:
    ctx = context(
        account={
            "equity_total": EQUITY,
            "cash": EQUITY,
            "buying_power": EQUITY,
            "settled_cash": EQUITY,
        },
        control={"high_water_mark": EQUITY},
        quotes={"F": {"bid": 99.90, "ask": 100.00, "last": 99.95}},
        signals={"F": {"atr": atr}},
        config=config(risk_sizing_enabled=enabled),
        **overrides,
    )
    return ctx


# --- off by default ---------------------------------------------------------


def test_the_shipped_config_leaves_it_off():
    assert config()["risk_sizing_enabled"] is False
    assert config()["risk_per_trade_pct"] == 0.01


def test_off_is_the_even_split_however_wide_the_stop():
    """The property every existing sizing golden depends on. A $20 ATR is a
    39% stop — the budget would cut this name to two shares — and with the
    switch off it takes the whole rung, exactly as it did before this code
    existed."""
    off = entry(sizing.size([candidate()], risk_ctx(20.0, enabled=False)), "F")

    assert off.quantity == 3  # floor($392 rung / $102.00)
    assert off.notional == pytest.approx(306.00)


# --- the budget binds -------------------------------------------------------


def test_a_wide_stop_sizes_to_the_risk_budget_not_the_slot():
    """The acceptance case. A $20 ATR is a $60 stop, 58.8% of the $102.00 limit,
    so $100 of risk funds $170 — one share where the $392 rung would have
    bought three."""
    result = sizing.size([candidate()], risk_ctx(20.0))

    sized = entry(result, "F")
    assert result.ladder.notional == pytest.approx(SLOT)
    assert sized.quantity == 1
    assert sized.notional == pytest.approx(102.00)

    # ...and the same candidate on the ladder alone buys three.
    assert entry(sizing.size([candidate()], risk_ctx(20.0, enabled=False)), "F").quantity == 3


def test_a_tight_stop_is_left_on_the_ladder_and_never_levered_up():
    """`min`, not a replacement. A $1 ATR gives a $2 stop, 2% wide, and a
    budget of $5,020 — five slots' worth. The rung still wins."""
    result = sizing.size([candidate()], risk_ctx(1.0))

    assert entry(result, "F").notional == pytest.approx(
        entry(sizing.size([candidate()], risk_ctx(1.0, enabled=False)), "F").notional
    )


def test_the_budget_scales_with_the_configured_risk():
    ctx = risk_ctx(20.0)
    ctx["config"] = config(risk_sizing_enabled=True, risk_per_trade_pct=0.005)

    # Half the risk, half the budget: $127.50 buys one share, not two.
    assert entry(sizing.size([candidate()], ctx), "F").quantity == 1


def test_a_budget_under_the_broker_floor_refuses_and_says_why():
    ctx = risk_ctx(20.0)
    ctx["config"] = config(risk_sizing_enabled=True, risk_per_trade_pct=0.00001)

    result = sizing.size([candidate()], ctx)

    assert entry(result, "F") is None
    assert reasons(result, "F") == [sizing.RISK_BUDGET]
    assert "58.8% away" in [u.message for u in result.unsized][0]


# --- what it refuses to guess -----------------------------------------------


def test_no_atr_falls_back_to_the_even_split():
    """A name whose signal fetch missed has no stop distance to divide by. The
    order still sizes on the ladder rather than being refused — the same
    direction `_marketable_limit` already fails in for the same missing field."""
    ctx = risk_ctx(20.0)
    ctx["signals"] = {}

    assert entry(sizing.size([candidate()], ctx), "F").quantity == 3


def test_no_equity_leaves_the_budget_out_of_it():
    """`guard.halts` already denies every order on an unreadable `equity_total`.
    A budget derived from a missing number would be an invention (Absolute 2),
    and it would be one that silently *shrinks* orders on a bad feed."""
    ctx = risk_ctx(20.0)
    ctx["account"] = {**ctx["account"], "equity_total": None}

    assert sizing.risk_budget({"symbol": "F", "play_id": "DIP"}, ctx, 102.00) is None


def test_a_zero_atr_is_not_a_zero_stop_distance():
    """Dividing by it would produce an unbounded budget, which reads as
    "no limit" exactly where the number is least trustworthy."""
    ctx = risk_ctx(0.0)

    assert sizing.risk_budget({"symbol": "F", "play_id": "DIP"}, ctx, 102.00) is None


# --- it uses the play's own stop --------------------------------------------


def test_the_budget_divides_by_the_plays_own_stop_multiple():
    """A `SECTOR` candidate stops at 1.5xATR, not the account's 3.0 — so the
    same ATR buys it twice as much as it buys a DIP. The alternative is a
    position sized against a stop it is not exited on, which is the whole
    reason `plays.core.stop_distance` is one resolver and not three readings of
    `config["atr_stop_multiple"]`."""
    ctx = risk_ctx(20.0)

    dip = sizing.risk_budget({"symbol": "F", "play_id": plays.DIP}, ctx, 102.00)
    sector = sizing.risk_budget({"symbol": "F", "play_id": plays.SECTOR}, ctx, 102.00)

    assert sector == pytest.approx(dip * (3.0 / 1.5))
    assert dip == pytest.approx(0.01 * EQUITY / (3.0 * 20.0 / 102.00))


def test_an_unknown_play_takes_the_account_stop():
    ctx = risk_ctx(20.0)

    assert sizing.risk_budget({"symbol": "F", "play_id": None}, ctx, 102.00) == (
        sizing.risk_budget({"symbol": "F", "play_id": plays.DIP}, ctx, 102.00)
    )
