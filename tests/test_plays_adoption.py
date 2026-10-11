"""§12.2 as the operator reopened it.

The original rule was that an adopted position has no play, therefore no time
stop, no target and no signal exit — "inventing one would be the system deciding
how long the operator meant to hold something it never chose." That premise was
overturned: deterministic-trader now inherits every position in the account as its own,
whoever opened it, and manages it to make money rather than to record provenance.

Binding a play on adoption is what delivers that, but it does not deliver it on
its own, and these tests pin the two places it falls short:

* **Two of the four plays declare no target.** `DIP` and `MACRO` exit on a
  signal, not a number, so an adopted name bound to either could still only ever
  be sold at a loss. `ADOPTED_TARGET_PCT` is the floor that closes it.
* **The time stop does not read P&L.** Check 5 fires on a session count alone,
  so a name inherited underwater would be sold because time passed. That is
  "realise a loss to free capital", which the operator ruled out in the same
  breath: a loss waits for a protective exit or a genuinely broken thesis.
* **The clock it counts is not the position's.** ``sessions_held`` comes off the broker's oldest
  tax lot, so an inherited name arrives already past any play's limit. `_managed_sessions`
  counts from ``adopted_on`` instead, so the clock measures how long
  deterministic-trader has held the position (operator).

The protective exits are deliberately *not* gated on P&L. A 2xATR stop exists to
realise a loss; that is its whole function.

The baseline position is the shared `position()` fixture: DIP, entered at 15.00,
two sessions ago, ATR 0.45 so the stop sits at 14.10.
"""

from src import plays
from tests.factories import config, exited, plays_context, position

# Two sessions before an earlier trading day these tests run on, matching the
# baseline position's own entry: adopted recently, so no clock has run out and
# a test can move exactly one thing.
JUST_ADOPTED = "2026-07-30"

# Long before it, so the adopted clock is unambiguously expired. Note that this
# is the *only* thing that expires it — `sessions_held` no longer can, which is
# the change these tests are here to hold. Also past `max_adopted_hold_sessions`
# (30), so the adopted "a time stop never realises a loss" exemption has run
# out — see `expired_recent` for a clock past the play horizon but still inside
# that window.
LONG_AGO = "2026-01-05"

# 20 open sessions before an earlier trading day: past DIP's 8-session
# horizon (so the time stop is due) but short of the 30-session
# `max_adopted_hold_sessions` cap, so the adopted no-loss exemption is still
# in force. Added with the Phase 0.5 ruling that the exemption expires
# rather than lasting forever.
WITHIN_EXEMPTION = "2026-07-06"


def _at(price: float | None, adopted: bool, **overrides) -> dict:
    ctx = plays_context()
    ctx["positions"] = [position(adopted=adopted, **overrides)]
    ctx["quotes"]["F"]["last"] = price
    return ctx


def adopted(price: float | None = 14.88, **overrides) -> dict:
    """One inherited F position, bound to DIP at adoption.

    Adopted recently by default, so nothing fires on the clock and a test that
    wants one expired says so with ``expired()``.
    """
    overrides.setdefault("adopted_on", JUST_ADOPTED)
    return _at(price, True, **overrides)


def expired(price: float | None = 14.88, **overrides) -> dict:
    """The same inherited position, adopted long enough ago for §6.5's clock.

    ``sessions_held=200`` is set on every one of these deliberately: it is the
    inherited holding period, it is far past every play's limit, and it is *not*
    what fires the time stop. If a test in this block ever passes with
    ``adopted_on`` removed, the clock has silently gone back to reading it.
    """
    overrides.setdefault("sessions_held", 200)
    overrides.setdefault("adopted_on", LONG_AGO)
    return _at(price, True, **overrides)


def expired_recent(price: float | None = 14.88, **overrides) -> dict:
    """Adopted clock past the play horizon but inside `max_adopted_hold_sessions`
    — the window in which the no-loss exemption still holds an underwater name.
    """
    overrides.setdefault("sessions_held", 200)
    overrides.setdefault("adopted_on", WITHIN_EXEMPTION)
    return _at(price, True, **overrides)


def owned(price: float | None = 14.88, **overrides) -> dict:
    """The same position, entered by the system itself."""
    return _at(price, False, **overrides)


# --- the adopted target ---------------------------------------------------


def test_the_adoption_floor_only_reaches_a_play_that_declares_no_target():
    """Adoption's floor is a backstop, not a policy — and it has nothing left to
    back up.

    `MANUAL` is the only row that reaches `_reason_to_leave` with no target at
    all, and it has no play to resolve, so `PLAYS` no longer contains an entry
    the floor can apply to. It is kept because the floor is about the *shape*
    of the rule — a play that declares no target must not leave an inherited
    name unable to sell up — and a fifth play could declare none tomorrow.
    """
    assert all(
        play.target_pct is not None
        for play in plays.PLAYS.values()
    )


def test_an_adopted_dip_takes_its_gain_at_dips_own_target():
    """DIP declares 6%, so that is what an inherited DIP exits at — the adoption
    floor is looser and deliberately does not apply."""
    target = 15.00 * (1.0 + plays.DIP_TARGET_PCT)

    assert exited(plays.exits(adopted(target + 0.01)), "F").code == plays.TARGET


def test_the_dip_target_does_not_fire_below_it():
    target = 15.00 * (1.0 + plays.DIP_TARGET_PCT)

    assert exited(plays.exits(adopted(target - 0.01)), "F") is None


def test_an_owned_dip_position_takes_the_same_target():
    """The target is the play's, so ownership does not change it."""
    target = 15.00 * (1.0 + plays.DIP_TARGET_PCT)

    assert exited(plays.exits(owned(target + 0.01)), "F").code == plays.TARGET


def test_a_declared_target_wins_over_the_adoption_floor():
    """PEAD's 8% is the play's own number and adoption does not loosen it."""
    pead = plays.PLAYS[plays.PEAD].target_pct
    assert pead is not None and pead < plays.ADOPTED_TARGET_PCT

    ctx = adopted(15.00 * (1.0 + pead) + 0.01, play_id=plays.PEAD)

    assert exited(plays.exits(ctx), "F").code == plays.TARGET


# --- the time stop cannot realise a loss ----------------------------------


def test_an_adopted_position_underwater_does_not_time_stop():
    """A clock running out is not a broken thesis — while the exemption lasts."""
    assert exited(plays.exits(expired_recent(14.99)), "F") is None


def test_the_adopted_no_loss_exemption_expires_after_the_cap():
    """Past `max_adopted_hold_sessions` an inherited loser that has neither
    recovered nor tripped a protective exit is realised so the slot can be
    redeployed."""
    exit_ = exited(plays.exits(expired(14.99)), "F")

    assert exit_.code == plays.TIME_STOP
    assert "exemption ends at session 30" in exit_.reason


def test_the_cap_is_config():
    ctx = expired_recent(14.99)
    ctx["config"] = config(max_adopted_hold_sessions=10)

    assert exited(plays.exits(ctx), "F").code == plays.TIME_STOP


def test_an_adopted_position_in_profit_time_stops_normally():
    assert exited(plays.exits(expired(15.01)), "F").code == plays.TIME_STOP


def test_an_adopted_position_exactly_at_cost_time_stops():
    """At cost there is no loss to realise, so the ordinary rule applies."""
    assert exited(plays.exits(expired(15.00)), "F").code == plays.TIME_STOP


# --- the clock the time stop counts ---------------------------------------


def test_a_freshly_adopted_winner_is_not_time_stopped_on_an_inherited_clock():
    """The hazard binding a play introduced, and the reason `adopted_on` exists.

    This position has been held 200 sessions and is in profit, so every earlier
    condition for a time stop is met — but deterministic-trader adopted it *today*.
    Selling here would liquidate the inherited momentum book on the first cycle
    that gave it a play, for a few percent, into cash that `open_slots: 0`
    cannot redeploy.
    """
    ctx = expired(15.01, adopted_on="2026-08-03")

    assert exited(plays.exits(ctx), "F") is None


def test_the_adopted_clock_does_run_once_it_is_this_systems_to_run():
    """The other half: `ADOPTED` is a leash, not an exemption. Its fifteen
    sessions after adoption expire like any play's, and a winner rotates."""
    ctx = expired(15.01, adopted_on="2026-07-01", play_id=plays.ADOPTED)

    assert exited(plays.exits(ctx), "F").code == plays.TIME_STOP


def test_the_adopted_clock_has_not_run_at_fourteen_sessions():
    """The boundary, so "fifteen" is a number this suite would notice changing."""
    ctx = expired(15.01, adopted_on="2026-07-14", play_id=plays.ADOPTED)

    assert exited(plays.exits(ctx), "F") is None


def test_an_adopted_position_with_no_adoption_date_holds():
    """Fails closed. A book row written before reconcile stamped an adoption day
    has no clock to count, and a position that cannot be timed is held rather
    than sold on an assumed one."""
    ctx = expired(15.01, adopted_on=None)

    assert exited(plays.exits(ctx), "F") is None


def test_an_owned_position_underwater_still_time_stops():
    """§6.5 exit 5 is unchanged for anything the system chose to buy."""
    ctx = owned(14.99, sessions_held=99)

    assert exited(plays.exits(ctx), "F").code == plays.TIME_STOP


# --- protective exits are never gated -------------------------------------


def test_an_adopted_position_still_stops_out_at_a_loss():
    """The 3xATR stop exists to realise a loss; gating it would be the defect."""
    ctx = adopted(13.64, sessions_held=99)

    assert exited(plays.exits(ctx), "F").code == plays.ATR_STOP


def test_an_adopted_position_still_leaves_on_a_restatement_at_a_loss():
    ctx = adopted(14.99)
    ctx["events"]["F"] = [{"type": "8-K", "items": ["4.02"], "sessions_ago": 0}]

    assert exited(plays.exits(ctx), "F").code == plays.HARD_EVENT_EXIT


def test_an_adopted_position_still_leaves_on_the_earnings_deadline_at_a_loss():
    ctx = adopted(14.99)
    ctx["earnings"]["F"]["sessions_until_report"] = 1

    assert exited(plays.exits(ctx), "F").code == plays.EARNINGS_DEADLINE


def test_an_unpriced_adopted_position_is_not_time_stopped_on_a_guess():
    """No price means no P&L, and the loss gate fails closed — it holds.

    `expired_recent()` matters here: with a live clock this would pass whether
    the gate worked or not, and past the exemption cap it would time-stop."""
    assert exited(plays.exits(expired_recent(None)), "F") is None


def test_an_adopted_position_without_an_entry_price_is_not_time_stopped():
    ctx = expired_recent(14.99, entry_price=None)

    assert exited(plays.exits(ctx), "F") is None
