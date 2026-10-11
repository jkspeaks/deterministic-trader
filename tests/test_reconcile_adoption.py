"""Binding a play to a position the system did not open (operator).

§12.2 said an adopted trade has no play. `plays.py` reads that as no time stop,
no target and no signal exit, which leaves exits 1–4 — every one of them a *risk*
exit. An inherited name could therefore be sold when it fell and never when it
rose. The operator reversed the premise: every position in the account is
deterministic-trader's to manage, whoever opened it.

Two facts come out of `reconcile.run` for such a name, and they are separate:

* ``adopted`` — the system did not choose to open this. Sticky for the
  position's whole life, because by the next cycle it is in ``prior_positions``
  and looks like anything else.
* ``play_id`` — how to manage it. A real play wins where its **own trigger fires
  today**; otherwise `ADOPTED`, whose whole reason for existing is that binding
  one of the four would be wrong twice over — see the two tests under "why it
  is not one of the four", which are the reasons in executable form.

The third pass in `_attribution` is what makes day one work at all: before this,
the function walked only the prior book and this cycle's fills, so ten positions
the system woke up holding were never looked at.
"""

from src import plays, reconcile
from tests.factories import fill, realized, reconcile_context, reconciled

# F is the baseline's own DIP position. GME is the intruder in every test here:
# held at the broker, in no prior book, and bought by no placement of ours.
INHERITED = [{"symbol": "F", "quantity": 1.0}, {"symbol": "GME", "quantity": 2.0}]

LOTS = {
    "F": [{"quantity": 1.0, "cost_basis": 15.00, "acquired_date": "2026-07-30"}],
    "GME": [{"quantity": 2.0, "cost_basis": 60.00, "acquired_date": "2026-08-03"}],
}


def run(**overrides):
    return reconcile.run(reconcile_context(**overrides))


def held(**overrides):
    """GME sitting in the broker's book with nothing to explain it.

    No fill and no placement, which is the day-one shape and the one the old
    `_attribution` could not see at all — not the untracked-fill shape, which it
    could.
    """
    return run(positions=INHERITED, tax_lots=LOTS, **overrides)


def oversold(rsi: float = 27.0) -> dict:
    """The market read that makes §6.2 fire on GME, and the ctx keys it needs.

    These five keys reach `reconcile.run` in production because the routine
    fetches once into `cycle.json` and runs both `reconcile` and `decide`
    against it. The baseline fixture carries none of them, so nothing triggers
    and every other test here lands on `ADOPTED` — this is the one that proves
    the other branch is reachable.
    """
    return {
        "signals": {"GME": {"rsi": rsi, "sma_20": 35.00, "sma_50": 32.00,
                            "sma_200": 25.00, "atr": 1.10}},
        "quotes": {"GME": {"last": 30.00, "bid": 29.95, "ask": 30.05}},
        "earnings": {"GME": {"status": "known", "sessions_until_report": 40,
                             "next_report_date": "2026-09-30"}},
    }


# --- the two facts ----------------------------------------------------------


def test_a_position_the_book_has_never_seen_is_adopted():
    """The day-one case. Nothing bought it, nothing remembers it, and it is
    still deterministic-trader's to manage."""
    assert reconciled(held(), "GME").adopted is True


def test_a_position_the_system_opened_is_not_adopted():
    """The flag has to distinguish, or it means nothing. F came off the prior
    book with its own play and is untouched by any of this."""
    assert reconciled(held(), "F").adopted is False
    assert reconciled(held(), "F").play_id == "DIP"


def test_an_inherited_name_no_play_claims_takes_the_adopted_play():
    """With no market read, no trigger can fire — which is also what a cycle
    that fetched nothing looks like. `ADOPTED` is the answer in both cases."""
    assert reconciled(held(), "GME").play_id == plays.ADOPTED


def test_a_play_whose_trigger_fires_today_wins_over_adoption():
    """§6.2 fires on GME at RSI 27 above its 200-day, so the name is managed as
    a DIP — including DIP's RSI >= 50 signal exit, which is the right rule for a
    position that genuinely is an oversold bounce."""
    position = reconciled(held(**oversold()), "GME")

    assert position.play_id == plays.DIP
    # Still adopted: a play recognising the name does not mean the system chose
    # to open it, and exits 5 and 6 both still need to know that.
    assert position.adopted is True


def test_a_play_that_triggered_and_declined_does_not_win():
    """DIP triggers at RSI 27 and then refuses the name for sitting under its
    200-day. A play that looked and declined has not endorsed a thesis, so the
    position takes `ADOPTED` rather than a play that would not have bought it."""
    ctx = oversold()
    ctx["signals"]["GME"]["sma_200"] = 40.00  # price 30.00 is below it

    assert reconciled(held(**ctx), "GME").play_id == plays.ADOPTED


def test_a_name_that_is_merely_calm_is_not_forced_onto_a_play():
    """RSI 55 is most of an inherited momentum book. Nothing triggers, so
    nothing is invented."""
    assert reconciled(held(**oversold(rsi=55.0)), "GME").play_id == plays.ADOPTED


# --- why it is not one of the four -----------------------------------------


def test_the_four_plays_are_not_credited_with_an_inherited_result():
    """The reason `ADOPTED` is a play id rather than a flag on a real play.

    `_play_state_updates` would otherwise put this $9 in DIP's or PEAD's
    `edge_play_state` row, and §1 reads those rows to ask which play had a bad
    month. A position nobody chose proves nothing about anybody's edge.
    """
    result = run(
        positions=[{"symbol": "F", "quantity": 1.0}],
        prior_positions=[
            {"symbol": "F", "play_id": "DIP", "quantity": 1.0},
            {"symbol": "GME", "play_id": plays.ADOPTED, "quantity": 2.0,
             "adopted": True},
        ],
        fills=[fill(order_id="zz98", symbol="GME", side="sell", quantity=2.0,
                    price=34.50, cost_basis=60.00)],
        # The money arrives on the broker's own per-trade row; the attribution
        # rule is the same one the counter uses.
        realized=[realized(order_id="zz98", symbol="GME", quantity=2.0,
                           price=34.50, realized_gain=9.0)],
    )

    assert set(result.play_state_updates) == {plays.ADOPTED}
    assert [(t["symbol"], t["play_id"], t["realized_gain"])
            for t in result.realized_trades] == [("GME", plays.ADOPTED, 9.0)]


def test_the_adopted_sleeve_is_measured_rather_than_discarded():
    """MANUAL credits nothing at all; `ADOPTED` gets its own row. The inherited
    book is real money and §1 should be able to say how it did — giving it a
    name is what lets it be measured without contaminating the four."""
    result = run(
        positions=[],
        prior_positions=[
            {"symbol": "GME", "play_id": plays.ADOPTED, "quantity": 2.0,
             "adopted": True},
        ],
        fills=[fill(order_id="zz98", symbol="GME", side="sell", quantity=2.0,
                    price=34.50, cost_basis=60.00)],
        tax_lots={},
    )

    assert result.play_state_updates[plays.ADOPTED]["closed_trades"] == 1


def test_adoption_never_proposes_an_entry():
    """`ADOPTED` is management terms, not a trigger. It is absent from
    `entries`'s play list, and nothing may ever buy on it."""
    assert plays.ADOPTED in plays.PLAYS
    assert plays.would_open("GME", reconcile_context(**oversold())) != plays.ADOPTED


# --- stickiness -------------------------------------------------------------


def test_adoption_survives_into_the_next_cycle():
    """By the cycle after an adoption the name is in `prior_positions` like any
    other, so a re-derived flag would read False and the position would silently
    regain the time stop the ruling took off it."""
    prior = reconciled(held(), "GME")

    second = run(
        positions=INHERITED,
        tax_lots=LOTS,
        prior_positions=[
            {"symbol": "F", "play_id": "DIP", "quantity": 1.0},
            {"symbol": "GME", "play_id": prior.play_id, "quantity": 2.0,
             "adopted": prior.adopted},
        ],
    )

    assert reconciled(second, "GME").adopted is True
    assert reconciled(second, "GME").play_id == plays.ADOPTED


def test_the_bound_play_is_not_re_derived_once_it_is_set():
    """§6: a position carries its play for its whole life. GME was adopted onto
    `ADOPTED` on a quiet day; RSI 27 arriving later does not re-bind it to DIP,
    any more than a recovered RSI un-DIPs the position F is in."""
    second = run(
        positions=INHERITED,
        tax_lots=LOTS,
        prior_positions=[
            {"symbol": "F", "play_id": "DIP", "quantity": 1.0},
            {"symbol": "GME", "play_id": plays.ADOPTED, "quantity": 2.0,
             "adopted": True},
        ],
        **oversold(),
    )

    assert reconciled(second, "GME").play_id == plays.ADOPTED


# --- the book written before adoption existed -------------------------------


def test_a_manual_holding_off_an_older_book_is_still_adopted():
    """The shape `edge_runlog.book` actually held, and the reason adoption once fired
    on nothing.

    Every cycle before adoption was built wrote the ten inherited names as
    `MANUAL`, so they arrive in `prior_positions` on every cycle after it. The
    prior-book pass claimed them, the day-one pass never looked, and the names
    kept the one play_id that gets no target, no time stop and no top-up —
    permanently, because the next book said `MANUAL` too.
    """
    second = run(
        positions=INHERITED,
        tax_lots=LOTS,
        prior_positions=[
            {"symbol": "F", "play_id": "DIP", "quantity": 1.0},
            {"symbol": "GME", "play_id": "MANUAL", "quantity": 2.0},
        ],
    )

    assert reconciled(second, "GME").adopted is True
    assert reconciled(second, "GME").play_id == plays.ADOPTED


def test_adopting_off_an_older_book_happens_once():
    """The self-heal must not re-stamp. `adopted_on` starts the §6.5 time-stop
    clock, so re-deriving it every cycle would hold the clock at zero and no
    adopted position would ever reach its time stop."""
    first = reconciled(
        run(positions=INHERITED, tax_lots=LOTS,
            prior_positions=[{"symbol": "GME", "play_id": "MANUAL",
                              "quantity": 2.0}]),
        "GME",
    )

    later = run(
        positions=INHERITED,
        tax_lots=LOTS,
        cycle={"trading_day": "2026-08-21"},
        prior_positions=[
            {"symbol": "GME", "play_id": first.play_id, "quantity": 2.0,
             "adopted": first.adopted, "adopted_on": first.adopted_on},
        ],
    )

    assert reconciled(later, "GME").adopted_on == first.adopted_on


def test_a_manual_name_that_is_gone_is_not_adopted_on_its_way_out():
    """A `MANUAL` in the prior book with nothing behind it at the broker is a
    round trip that is over. Adopting it would hand its realised P&L to whatever
    play `would_open` names, which is exactly the contamination
    `_play_state_updates` refuses."""
    result = run(
        positions=[],
        tax_lots={},
        prior_positions=[{"symbol": "GME", "play_id": "MANUAL",
                          "quantity": 2.0}],
    )

    assert result.play_state_updates == {}
