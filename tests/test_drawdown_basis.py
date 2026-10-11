"""§10's drawdown halt on an account funded after inception.

Two defects, found together and fixed together, because fixing either alone
would have liquidated the account.

1. **Three August deposits were never recorded.** $500 of the operator's own
   money was scored as gain: the scorecard read +$1,002 on an account that was
   down $98, and `edge_control.high_water_mark` had ratcheted to 750.00 on
   it. Recording the deposits with that mark left alone puts flow-adjusted
   equity at 150 under a 380 floor, and §6.5 exit 2 sells every position.
2. **The halt measured 20% of the gain, not of the account.** Even against the
   true mark (250.00) a $50 fall read as 20%: the mark is what is left after
   $1,100 of deposits are taken out, a base of about $248 on a $1,350 account.

The fix is two rules in `src/sizing.py`. `anchored_mark` carries the mark as
raw equity on a day and re-derives it from the deposit record as it reads now,
so a corrected record corrects the mark in the same cycle. `drawdown_floor`
measures the fall against ``mark + net deposits``. Both collapse to the old
arithmetic on an account with no deposits and no anchor, which is every other
test in the suite.

The figures below are the live account's.
"""

import json
from pathlib import Path

import pytest

from src import cli, guard, plays, reconcile, report, sizing
from tests.factories import cli_payload, config

SHIPPED_INCEPTION = json.loads(
    (Path(__file__).resolve().parent.parent / "config" / "inception.json").read_text(encoding="utf-8")
)

EQUITY = 1250.00
DEPOSITS = 1100.00             # every transfer, the three late ones included
DEPOSITS_AT_PEAK = 700.00
PEAK_EQUITY = 950.00  # An earlier cycle
TRUE_MARK = 250.00
STALE_MARK = 750.00            # what edge_control held, $500 too high

ANCHORED = {
    "equity_total": EQUITY,
    "net_deposits": DEPOSITS,
    "high_water_mark_equity": PEAK_EQUITY,
    "high_water_mark_as_of": "2026-08-17",
    "net_deposits_at_mark": DEPOSITS_AT_PEAK,
}


# --- the mark is anchored to equity on a day ---------------------------------


def test_the_mark_is_equity_at_the_anchor_less_what_was_deposited_by_then():
    assert sizing.anchored_mark(ANCHORED, STALE_MARK) == pytest.approx(TRUE_MARK)


def test_a_deposit_recorded_late_lowers_the_mark_by_exactly_its_amount():
    """The property the anchor exists for."""
    before = {**ANCHORED, "net_deposits_at_mark": 200.00}

    assert sizing.anchored_mark(before, None) - sizing.anchored_mark(ANCHORED, None) == pytest.approx(500.00)


@pytest.mark.parametrize("missing", ["high_water_mark_equity", "net_deposits_at_mark"])
def test_without_a_whole_anchor_the_stored_mark_stands(missing):
    account = {k: v for k, v in ANCHORED.items() if k != missing}

    assert sizing.anchored_mark(account, 236.42) == 236.42
    assert sizing.anchored_mark({}, None) is None
    assert sizing.anchored_mark(None, 236.42) == 236.42


# --- the floor is 20% of the account ------------------------------------------


def test_the_fall_is_measured_against_the_mark_plus_net_deposits():
    floor = sizing.drawdown_floor(TRUE_MARK, config(), {"net_deposits": DEPOSITS})

    # 250.00 - 20% of (250.00 + 1100.00)
    assert floor == pytest.approx(TRUE_MARK - 0.20 * (TRUE_MARK + DEPOSITS))


def test_an_account_with_no_deposits_keeps_the_old_floor_exactly():
    assert sizing.drawdown_floor(295.525, config()) == pytest.approx(236.42)
    assert sizing.drawdown_floor(295.525, config(), {}) == pytest.approx(236.42)
    assert sizing.drawdown_floor(295.525, config(), {"net_deposits": 0.0}) == pytest.approx(236.42)


def test_the_live_account_is_three_and_a_half_percent_down_not_twenty():
    assert not sizing.drawdown_breached(ANCHORED, STALE_MARK, config())
    # The same account measured the old way, against even the *true* mark:
    assert EQUITY - DEPOSITS < TRUE_MARK * 0.80 + 0.01


def test_a_real_twenty_percent_loss_of_the_account_still_halts():
    """The halt is rescaled, not removed. 20% of 1350.00 is 270.00, so the
    account breaches at 1350.00 - 270.00 = 1080.00 of equity."""
    at_the_floor = {**ANCHORED, "equity_total": 1080.00}
    through_it = {**ANCHORED, "equity_total": 1079.98}

    assert not sizing.drawdown_breached(at_the_floor, STALE_MARK, config())
    assert sizing.drawdown_breached(through_it, STALE_MARK, config())


def test_a_withdrawal_shrinks_the_base_the_fall_is_measured_against():
    floor = sizing.drawdown_floor(236.42, config(), {"net_deposits": -50.00})

    assert floor == pytest.approx(236.42 - 0.20 * 186.42)


def test_a_base_withdrawn_to_nothing_does_not_put_the_floor_above_the_mark():
    assert sizing.drawdown_floor(10.0, config(), {"net_deposits": -100.0}) == pytest.approx(10.0)


def test_recording_the_deposits_without_the_anchor_is_the_liquidation():
    """What this fix prevents, pinned: the corrected deposit record against
    the stale stored mark reads as a breach."""
    unanchored = {"equity_total": EQUITY, "net_deposits": DEPOSITS}

    assert sizing.drawdown_breached(unanchored, STALE_MARK, config())


# --- reconcile writes the mark and its anchor together --------------------------


def updates(account, mark, trading_day="2026-10-05"):
    return reconcile._high_water_updates(
        {"account": account, "control": {"high_water_mark": mark}, "cycle": {"trading_day": trading_day}}
    )


def test_a_stored_mark_the_record_has_made_too_high_is_corrected_in_one_cycle():
    assert updates(ANCHORED, STALE_MARK) == {
        "high_water_mark": pytest.approx(TRUE_MARK),
        "high_water_mark_equity": PEAK_EQUITY,
        "high_water_mark_as_of": "2026-08-17",
    }


def test_a_new_peak_anchors_on_this_cycles_equity_and_day():
    account = {**ANCHORED, "equity_total": 1500.00}

    assert updates(account, STALE_MARK) == {
        "high_water_mark": pytest.approx(1500.00 - DEPOSITS),
        "high_water_mark_equity": 1500.00,
        "high_water_mark_as_of": "2026-10-05",
    }


def test_a_mark_with_no_anchor_is_given_todays_reading_of_it():
    """`mark + net deposits` as of today is the same mark in equity terms, and
    true by construction on the day it is written."""
    account = {"equity_total": 300.00, "net_deposits": 100.00}

    assert updates(account, 236.42) == {
        "high_water_mark": 236.42,
        "high_water_mark_equity": pytest.approx(336.42),
        "high_water_mark_as_of": "2026-10-05",
    }


def test_the_first_reading_seeds_the_mark_and_its_anchor():
    assert updates({"equity_total": 236.42}, None, "2026-08-03") == {
        "high_water_mark": 236.42,
        "high_water_mark_equity": 236.42,
        "high_water_mark_as_of": "2026-08-03",
    }


def test_an_unreadable_equity_neither_ratchets_nor_drops_the_anchor():
    account = {**ANCHORED, "equity_total": None}

    assert updates(account, STALE_MARK) == {
        "high_water_mark": pytest.approx(TRUE_MARK),
        "high_water_mark_equity": PEAK_EQUITY,
        "high_water_mark_as_of": "2026-08-17",
    }
    assert updates({"equity_total": 0}, None) == {"high_water_mark": 0.0}


def test_the_mark_still_never_moves_down_with_equity():
    falling = {**ANCHORED, "equity_total": 1200.00}

    assert updates(falling, STALE_MARK)["high_water_mark"] == pytest.approx(TRUE_MARK)
    assert reconcile._high_water_mark(
        {"account": falling, "control": {"high_water_mark": STALE_MARK}, "cycle": {"trading_day": "2026-10-05"}}
    ) == pytest.approx(TRUE_MARK)


# --- the shipped record -----------------------------------------------------------


@pytest.mark.parametrize(
    "day, total",
    [
        ("2026-08-04", 0.00),
        ("2026-08-07", 200.00),
        ("2026-08-10", 450.00),
        ("2026-08-13", 600.00),
        ("2026-08-17", 700.00),
        ("2026-08-25", 950.00),
        ("2026-10-02", 1100.00),
    ],
)
def test_the_shipped_deposit_record_by_day(day, total):
    assert report.net_deposits(SHIPPED_INCEPTION, day) == pytest.approx(total)


def test_the_shipped_anchor_is_the_true_peak():
    anchor = SHIPPED_INCEPTION["high_water_mark_anchor"]

    assert anchor == {"as_of": "2026-08-17", "equity": PEAK_EQUITY}
    assert anchor["equity"] - report.net_deposits(SHIPPED_INCEPTION, anchor["as_of"]) == pytest.approx(TRUE_MARK)


def test_the_account_is_down_ninety_eight_dollars_not_up_a_thousand():
    base = SHIPPED_INCEPTION["equity"] + report.net_deposits(SHIPPED_INCEPTION, "2026-10-02")

    assert EQUITY - base == pytest.approx(-97.72)


# --- cli joins the anchor -----------------------------------------------------------

LOADED = cli.load_config()


def joined(now, control=None, inception=None):
    payload = cli_payload(now=now, control=control or {})
    loaded = LOADED if inception is None else {**LOADED, "inception": inception}
    return cli._account(payload, loaded, cli._cycle(payload, loaded))


def test_the_shipped_anchor_is_joined_while_control_has_none():
    account = joined("2026-10-05T13:40:00Z")

    assert account["net_deposits"] == pytest.approx(DEPOSITS)
    assert account["high_water_mark_equity"] == PEAK_EQUITY
    assert account["high_water_mark_as_of"] == "2026-08-17"
    assert account["net_deposits_at_mark"] == pytest.approx(DEPOSITS_AT_PEAK)


def test_an_anchor_dated_after_the_cycle_is_not_applied():
    account = joined("2026-08-03T13:40:00Z")

    assert "high_water_mark_equity" not in account
    assert "net_deposits_at_mark" not in account


def test_the_control_rows_anchor_outranks_the_one_in_the_file():
    account = joined(
        "2026-10-05T13:40:00Z",
        control={"high_water_mark_equity": 1500.00, "high_water_mark_as_of": "2026-09-30"},
    )

    assert account["high_water_mark_equity"] == 1500.00
    assert account["high_water_mark_as_of"] == "2026-09-30"
    assert account["net_deposits_at_mark"] == pytest.approx(DEPOSITS)


def test_half_an_anchor_in_control_falls_back_to_the_file():
    account = joined("2026-10-05T13:40:00Z", control={"high_water_mark_equity": 1500.00})

    assert account["high_water_mark_equity"] == PEAK_EQUITY


def test_no_anchor_anywhere_joins_only_net_deposits():
    bare = {k: v for k, v in SHIPPED_INCEPTION.items() if k != "high_water_mark_anchor"}

    account = joined("2026-10-05T13:40:00Z", inception=bare)

    assert account["net_deposits"] == pytest.approx(DEPOSITS)
    assert "high_water_mark_equity" not in account


# --- the whole cycle: the day the corrected record lands -------------------------------
#
# The live account's figures on the fixture's own clock, so the calendar, universe and
# earnings fixtures still apply: 700.00 deposited by the anchor day, 400.00 after it, and
# a control row still holding the stale mark.

CORRECTED = {
    **SHIPPED_INCEPTION,
    "deposits": [
        {"date": "2026-07-01", "amount": 200.00},
        {"date": "2026-07-10", "amount": 500.00},      # the three, recorded late
        {"date": "2026-07-25", "amount": 400.00},
    ],
    "high_water_mark_anchor": {"as_of": "2026-07-20", "equity": PEAK_EQUITY},
}
WITHOUT_THE_ANCHOR = {k: v for k, v in CORRECTED.items() if k != "high_water_mark_anchor"}


def live_payload(**control):
    payload = cli_payload()
    payload["control"] = {**payload["control"], "high_water_mark": STALE_MARK, **control}
    payload["account"] = {
        **payload["account"],
        "equity_total": EQUITY,
        "cash": 900.00,
        "buying_power": 900.00,
        "settled_cash": 900.00,
    }
    return payload


def cycle(payload, inception):
    loaded = {**LOADED, "inception": inception}
    reconciled = cli.reconcile(payload, loaded)
    payload = {
        **payload,
        "control": {**payload["control"], **reconciled["control_updates"]},
        "reconcile": reconciled,
    }
    return reconciled, cli.decide(payload, loaded), payload


def exit_codes(decided):
    return [e["code"] for e in decided["exits"]["exits"]]


def denial_codes(decided):
    return [d["code"] for row in decided["guard"]["denied"] for d in row["denials"]]


def test_the_first_cycle_after_the_correction_flattens_nothing_and_fixes_the_mark():
    reconciled, decided, _ = cycle(live_payload(), CORRECTED)

    assert plays.DRAWDOWN_FLATTEN not in exit_codes(decided)
    assert "DRAWDOWN_HALT" not in denial_codes(decided)
    updates_ = reconciled["control_updates"]
    assert updates_["high_water_mark"] == pytest.approx(TRUE_MARK)
    assert updates_["high_water_mark_equity"] == PEAK_EQUITY
    assert updates_["high_water_mark_as_of"] == "2026-07-20"


def test_the_same_deposits_without_the_anchor_would_have_sold_the_book():
    """The counterfactual, so the test above is known to bite."""
    _, decided, _ = cycle(live_payload(), WITHOUT_THE_ANCHOR)

    assert plays.DRAWDOWN_FLATTEN in exit_codes(decided)


def test_once_control_holds_the_anchor_the_file_is_no_longer_needed():
    _, _, after_first = cycle(live_payload(), CORRECTED)

    reconciled, decided, _ = cycle(after_first, WITHOUT_THE_ANCHOR)

    assert plays.DRAWDOWN_FLATTEN not in exit_codes(decided)
    assert reconciled["control_updates"]["high_water_mark"] == pytest.approx(TRUE_MARK)
    assert reconciled["control_updates"]["high_water_mark_as_of"] == "2026-07-20"


def test_the_drawdown_alert_stays_quiet_and_would_quote_the_true_mark():
    payload = live_payload()
    loaded = {**LOADED, "inception": CORRECTED}
    account = cli._account(payload, loaded, cli._cycle(payload, loaded))
    ctx = {"control": payload["control"], "account": account, "config": LOADED["config"],
           "cycle": {"trading_day": "2026-08-03"}, "positions": [], "prior_benchmark": {}}

    assert report._drawdown_alert(ctx) == ([], {})

    ctx["account"] = {**account, "equity_total": 1000.00}
    alerts, stamp = report._drawdown_alert(ctx)
    assert [a.title for a in alerts] == [report.DRAWDOWN_HALT]
    assert "$250.00" in alerts[0].body and "750.00" not in alerts[0].body
    assert stamp == {"last_drawdown_alert": "2026-08-03"}


# --- the two messages quote the floor that was judged ----------------------------


def test_guards_denial_and_the_flatten_quote_the_same_rescaled_floor():
    """Both halves of §10 print a floor. A message quoting the old one would
    tell the operator the account had breached a line it is $400 above."""
    breached = {**ANCHORED, "equity_total": 1000.00}
    ctx = {"account": breached, "control": {"high_water_mark": STALE_MARK}, "config": config()}
    expected = TRUE_MARK - 0.20 * (TRUE_MARK + DEPOSITS)

    is_breached, adjusted, floor = guard.halts._drawdown_breached(ctx)

    assert is_breached and adjusted == pytest.approx(1000.00 - DEPOSITS)
    assert floor == pytest.approx(expected)

    payload = live_payload()
    payload["account"]["equity_total"] = 1000.00
    _, decided, _ = cycle(payload, CORRECTED)
    flatten = [e for e in decided["exits"]["exits"] if e["code"] == plays.DRAWDOWN_FLATTEN]
    assert flatten and f"{expected:.2f} drawdown floor" in flatten[0]["reason"]
