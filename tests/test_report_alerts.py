"""§8.3's table, both columns, in code rather than in prompt text.

`Drawdown halt`, `Account read implausible`, `Weekly scorecard` and
`Benchmark integrity` still push on their own, because each already has its
own dedupe or its own weekly cadence and folding a halt into a routine status
line is how a halt gets missed.

The right-hand column is still the harder half. A system that pushes every
fill is one the operator stops reading, and once it is unread the left-hand
column is unread with it. So "not notified" is a rule with tests, not an
omission.

Trade detail lives in the Robinhood app, which is better at it than any
notification would be.
"""

from src import report
from tests.factories import alerted, fill, report_context

MONITOR = {"cycle_type": "monitor", "now": "2026-08-06T15:40:00Z"}
FULL = {"cycle_type": "full", "now": "2026-08-25T13:40:00Z"}

# The baseline is Thursday's post-close (`report_context`'s own docstring).
POSTCLOSE_SUCCESS = "Post-close cycle succeeded"
MONITOR_SUCCESS = "Monitor cycle succeeded"
FULL_SUCCESS = "Full cycle succeeded"


def status_body(result):
    """The one folded status alert's body — every scenario below has exactly
    one non-scorecard, non-halt, non-integrity alert."""
    return result.alerts[0].body


# --- the folded status push: what makes it into the body -------------------


def test_a_failed_cycle_is_notified():
    """§14's first rows. A cycle that threw placed nothing, or placed
    something and could not record it, and both need a person."""
    ctx = report_context(
        failure={"step": "place_equity_order", "detail": "connection reset"}
    )

    result = report.run(ctx)

    assert result.alerts[0].title == "Post-close cycle failed"
    assert result.status == report.STATUS_FAILED
    assert "connection reset" in status_body(result)


def test_a_failed_cycle_is_notified_loudly():
    ctx = report_context(failure={"step": "reconcile", "detail": "timeout"})

    assert report.run(ctx).alerts[0].priority == report.PRIORITY_HIGH


def test_reconciliation_divergence_is_notified():
    ctx = report_context(
        reconcile={
            "findings": [
                {"code": "POSITION_DELTA_MISMATCH", "symbol": "F",
                 "detail": "the broker holds 1.6 F", "order_id": None}
            ]
        }
    )

    result = report.run(ctx)

    assert result.status == report.STATUS_SUCCESS_WITH_WARNINGS
    assert result.alerts[0].title == "Post-close cycle succeeded with warnings"
    assert "POSITION_DELTA_MISMATCH" in status_body(result)


def test_many_findings_are_one_notification():
    """Five findings on one cycle is one thing wrong, not five. A push per
    finding is how a bad afternoon empties the operator's attention before the
    important one arrives."""
    ctx = report_context(
        reconcile={
            "findings": [
                {"code": "MISSING_ORDER", "symbol": "F", "detail": "a",
                 "order_id": "a1b2"},
                {"code": "PHANTOM_ORDER", "symbol": "MRK", "detail": "b",
                 "order_id": "zz99"},
                {"code": "QUANTITY_DRIFT", "symbol": "PFE", "detail": "c",
                 "order_id": "c3d4"},
            ]
        }
    )

    result = report.run(ctx)

    assert len(result.alerts) == 1
    body = status_body(result)
    assert "MISSING_ORDER" in body and "PHANTOM_ORDER" in body


def test_persistent_divergence_is_blocked_not_a_warning():
    """One divergence is a warning; two consecutive ones block entries until
    the operator clears `edge_control`, which cannot happen if they do not
    know. "any PHANTOM_ORDER/PERSISTENT ⇒
    blocked"."""
    ctx = report_context(
        reconcile={
            "findings": [
                {"code": "RECONCILIATION_DIVERGENCE_PERSISTENT", "symbol": "",
                 "detail": "disagreed for 2 cycles", "order_id": None}
            ]
        }
    )

    result = report.run(ctx)

    assert result.status == report.STATUS_BLOCKED
    assert result.alerts[0].title == "Post-close cycle blocked"
    assert result.alerts[0].priority == report.PRIORITY_HIGH


def test_a_play_switched_off_is_notified():
    """§1: the agent's judgment is exercised in choosing which plays are live.
    That is the one decision it makes that is not mechanical, so it is the one
    decision worth telling a person about."""
    ctx = report_context(play_state={"DIP": {"regime_fit": 0.0,
                                             "reason": "dips keep going"}})

    result = report.run(ctx)

    assert "dips keep going" in status_body(result)
    assert result.status == report.STATUS_SUCCESS


def test_a_play_switched_back_on_is_notified():
    """Both directions. A play going dark and staying dark is a decision the
    operator should get to disagree with; so is it coming back."""
    ctx = report_context(
        play_state={"MACRO": {"regime_fit": 0.8, "reason": "CPI cooled"}},
        prior_play_state={"MACRO": {"regime_fit": 0.0}},
    )

    assert "MACRO enabled" in status_body(report.run(ctx))


def test_a_conviction_wobble_is_not_a_play_being_switched_off():
    """§6.4's regime flip is an explicit 0.0, and `plays.py` treats only 0.0
    that way. 1.0 drifting to 0.4 is the agent having an opinion, which is its
    job and not news."""
    ctx = report_context(play_state={"DIP": {"regime_fit": 0.4,
                                             "reason": "less keen"}})

    assert status_body(report.run(ctx)) == f"Nothing to report.\n\n{_footer(ctx)}"


def test_the_adopted_row_appearing_is_not_a_play_being_switched_off():
    """`ADOPTED` gets an `edge_play_state` row the first time an inherited
    position closes, and `edge_play_state_apply` inserts every new row at
    regime_fit 0.0. Against an absent prior that reads as newly-dark, so without
    the `UNCHOSEN` skip the operator's phone would announce that a play they
    have never heard of and cannot control had been disabled."""
    ctx = report_context(
        play_state={"ADOPTED": {"regime_fit": 0.0, "realized_pnl": 9.0,
                                "closed_trades": 1}},
    )

    assert "ADOPTED" not in status_body(report.run(ctx))


def test_two_plays_changing_is_one_notification():
    ctx = report_context(
        play_state={
            "DIP": {"regime_fit": 0.0, "reason": "dips keep going"},
            "PEAD": {"regime_fit": 0.0, "reason": "drift stopped paying"},
        }
    )

    result = report.run(ctx)

    assert len(result.alerts) == 1
    body = status_body(result)
    assert "DIP" in body and "PEAD" in body


def test_the_weekly_scorecard_is_notified():
    ctx = report_context(
        cycle={"trading_day": "2026-08-07", "now": "2026-08-07T20:30:00Z"}
    )

    assert report.WEEKLY_SCORECARD in alerted(report.run(ctx))


def test_the_scorecard_notification_carries_the_three_bars():
    """The push is the whole deliverable — §8.3 sends it precisely so the
    operator does not have to go and look."""
    ctx = report_context(
        cycle={"trading_day": "2026-08-07", "now": "2026-08-07T20:30:00Z"}
    )

    body = alerted(report.run(ctx), report.WEEKLY_SCORECARD)[0]

    assert "SPY" in body
    assert "frozen" in body.lower()
    assert "24%" in body


def test_the_scorecard_is_ordinary_priority():
    """It is the one §8.3 row that fires when nothing is wrong. Sending it at
    the same priority as a drawdown halt teaches the operator to ignore both."""
    ctx = report_context(
        cycle={"trading_day": "2026-08-07", "now": "2026-08-07T20:30:00Z"}
    )

    result = report.run(ctx)
    scorecard = next(a for a in result.alerts if a.title == report.WEEKLY_SCORECARD)

    assert scorecard.priority == report.PRIORITY_DEFAULT


# --- the four that are still not notified -----------------------------------


def _footer(ctx: dict) -> str:
    """The exact footer `_status_alert` appends, recomputed the same way the
    module does, so a test asserting the whole body does not have to hardcode
    a cycle id and time it does not control. A "Nothing to report" cycle is
    always `success`, so the title is always `<routine> cycle succeeded`."""
    routine = report._routine_label(ctx.get("cycle", {}).get("cycle_type"))
    title = f"{routine} cycle {report._STATUS_PHRASE[report.STATUS_SUCCESS]}"
    return report._footer_with_link(ctx, routine, title)


def test_a_fill_is_not_notified():
    """§8.3's right-hand column, first row. Trade detail lives in the
    Robinhood app, which is better at it than any notification would be."""
    ctx = report_context(fills=[fill(), fill(order_id="c3d4", symbol="PFE")])

    assert status_body(report.run(ctx)) == f"Nothing to report.\n\n{_footer(ctx)}"


def test_routine_entries_and_exits_are_not_notified():
    """The system trading is the system working."""
    ctx = report_context(
        intent={
            "placements": [
                {"order_id": "a1b2", "symbol": "F", "side": "buy",
                 "quantity": 1.0, "play_id": "DIP"}
            ]
        },
        exits=[{"symbol": "PFE", "reason": "target"}],
    )

    assert status_body(report.run(ctx)) == f"Nothing to report.\n\n{_footer(ctx)}"


def test_the_days_p_and_l_is_not_notified():
    """A day up $8 is not an event. The weekly scorecard is where performance
    is reported, at a cadence a person can actually act on."""
    ctx = report_context(account={"equity_total": 244.42})

    assert status_body(report.run(ctx)) == f"Nothing to report.\n\n{_footer(ctx)}"


def test_candidate_rankings_are_not_notified():
    ctx = report_context(
        candidates=[{"symbol": "F", "conviction": 0.7},
                    {"symbol": "PFE", "conviction": 0.6}]
    )

    assert status_body(report.run(ctx)) == f"Nothing to report.\n\n{_footer(ctx)}"


def test_the_operators_own_trade_is_not_notified():
    """§12.2: `UNTRACKED_FILL` "is logged, not alerted — alerting on the
    operator's own deliberate action is exactly the noise that makes a system
    tiresome." It is a reconciliation finding that is not a divergence."""
    ctx = report_context(
        reconcile={
            "findings": [
                {"code": "UNTRACKED_FILL", "symbol": "GME",
                 "detail": "adopted", "order_id": "zz99"}
            ]
        }
    )

    result = report.run(ctx)

    assert status_body(result) == f"Nothing to report.\n\n{_footer(ctx)}"
    assert result.status == report.STATUS_SUCCESS


def test_a_quiet_cycle_still_gets_exactly_one_min_priority_push():
    """Ten of the day's eleven invocations."""
    result = report.run(report_context())

    assert len(result.alerts) == 1
    assert result.alerts[0].title == POSTCLOSE_SUCCESS
    assert result.alerts[0].priority == report.PRIORITY_MIN


# --- the halt's own dedupe -------------------------------


def test_the_drawdown_halt_is_notified():
    """§10's stop, and the one alert that means the system has switched itself
    off. Equity 180 against a 247.72 mark is a 24% drawdown."""
    ctx = report_context(account={"equity_total": 180.00})

    result = report.run(ctx)

    assert report.DRAWDOWN_HALT in alerted(result)
    halt = next(a for a in result.alerts if a.title == report.DRAWDOWN_HALT)
    assert halt.priority == report.PRIORITY_URGENT


def test_the_drawdown_halt_is_notified_once_not_every_cycle():
    """The breach stays true for as long as it takes the operator to decide
    what to do, and alerting on the state rather than the transition would push
    it eleven times a day until then.

    The dedupe is `edge_control.last_drawdown_alert` and **not**
    `trading_enabled`. Nothing in the code sets the kill switch any more: the
    halt is already latched by the ratcheting high-water mark, and a deposit
    that lifts equity over the floor must clear it with nothing for the
    operator to remember."""
    ctx = report_context(
        account={"equity_total": 180.00},
        control={"last_drawdown_alert": "2026-08-06"},
    )

    assert report.DRAWDOWN_HALT not in alerted(report.run(ctx))


def test_a_drawdown_inside_the_limit_is_not_a_halt():
    """19% is not 20%. `sizing.drawdown_breached` draws the line, so guard and
    report cannot disagree about which side of it the account is on. 19% below
    the 247.72 mark is 200.65; the floor is 198.18."""
    ctx = report_context(account={"equity_total": 200.65})

    assert report.DRAWDOWN_HALT not in alerted(report.run(ctx))


def test_an_implausible_equity_is_notified_as_account_implausible_not_drawdown():
    """Equity_total read 669.53 while the account held more than that in open
    positions, and the system raised an urgent `Drawdown halt` with a
    hand-retyped `(£10)` body. `Account read implausible` must fire instead,
    and `Drawdown halt` must not fire at all in the same cycle."""
    ctx = report_context(
        control={"high_water_mark": 1000.00},  # would also breach the drawdown floor
        account={"equity_total": 236.42},
        positions=[{"symbol": "OTHER", "market_value": 300.00}],
    )

    result = report.run(ctx)

    assert report.ACCOUNT_IMPLAUSIBLE_ALERT in alerted(result)
    assert report.DRAWDOWN_HALT not in alerted(result)
    implausible = next(
        a for a in result.alerts if a.title == report.ACCOUNT_IMPLAUSIBLE_ALERT
    )
    assert implausible.priority == report.PRIORITY_URGENT


def test_an_implausible_account_read_halts_implausible_not_drawdown():
    """The live session raised an urgent `Drawdown halt` with a hand-retyped
    `(£10)` body; this must now halt with `Account read implausible` and never
    reach `Drawdown halt` at all."""
    ctx = report_context(
        control={"high_water_mark": 799.54},
        account={"equity_total": 669.53},
        positions=[
            {"symbol": "INVH", "market_value": 497.32},
            {"symbol": "MGM", "market_value": 238.02},
        ],
    )

    result = report.run(ctx)

    assert report.ACCOUNT_IMPLAUSIBLE_ALERT in alerted(result)
    assert report.DRAWDOWN_HALT not in alerted(result)


def test_the_implausible_alert_is_notified_once_not_every_cycle():
    ctx = report_context(
        account={"equity_total": 236.42},
        positions=[{"symbol": "OTHER", "market_value": 300.00}],
        control={"last_account_implausible_alert": "2026-08-06"},
    )

    assert report.ACCOUNT_IMPLAUSIBLE_ALERT not in alerted(report.run(ctx))


def test_recovering_from_implausible_clears_its_own_stamp():
    ctx = report_context(control={"last_account_implausible_alert": "2026-08-06"})

    assert (
        report.run(ctx).control_updates["last_account_implausible_alert"] is None
    )


def test_the_alerting_cycle_stamps_the_halt_so_the_next_one_stays_quiet():
    """`edge_alerts` is append-only with a serial id and cannot be upserted the
    way the benchmark row is, so the stamp is the only thing that can keep one
    halt to one push. Same mechanism `last_scorecard_date` already uses."""
    ctx = report_context(account={"equity_total": 180.00})

    assert report.run(ctx).control_updates["last_drawdown_alert"] == "2026-08-06"


def test_recovering_above_the_floor_clears_the_stamp():
    """The halt lifts by itself when equity clears `mark x 0.80` — that is the
    whole reason nothing writes `trading_enabled`. The stamp has to lift with
    it or the *next* drawdown would be silent."""
    ctx = report_context(control={"last_drawdown_alert": "2026-08-06"})

    assert report.run(ctx).control_updates["last_drawdown_alert"] is None


def test_a_second_drawdown_after_a_recovery_is_notified_again():
    """A funded account that falls back through the floor is a new event, and
    a system that only ever alerted once would go quiet exactly when it
    mattered a second time."""
    ctx = report_context(
        account={"equity_total": 180.00}, control={"last_drawdown_alert": None}
    )

    assert report.DRAWDOWN_HALT in alerted(report.run(ctx))


def test_an_account_with_no_high_water_mark_yet_is_not_in_drawdown():
    """Day one, before `reconcile.py` has ever written one. There is no peak to
    be 20% below, and reading the absent mark as zero would make every account
    permanently halted — or, worse, permanently safe."""
    ctx = report_context()
    ctx["control"] = {k: v for k, v in ctx["control"].items()
                      if k != "high_water_mark"}

    result = report.run(ctx)

    assert report.DRAWDOWN_HALT not in alerted(result)
    assert result.control_updates == {}


def test_a_quiet_cycle_does_not_write_a_stamp_it_has_no_reason_to():
    """`control_updates` is merged by the routine, and a key written on every
    cycle is a write on every cycle."""
    assert "last_drawdown_alert" not in report.run(report_context()).control_updates


def test_the_halt_no_longer_claims_the_kill_switch_is_being_thrown():
    """It said "TRADING_ENABLED is being set FALSE (§10)", and under settled
    call 29 nothing sets it. An alert that describes an action the system does
    not take is worse than no alert — the operator would wait for a state that
    never arrives."""
    body = alerted(report.run(report_context(account={"equity_total": 180.00})),
                   report.DRAWDOWN_HALT)[0]

    assert "TRADING_ENABLED" not in body
    assert "deposit" in body.lower()


def test_the_stamp_is_written_on_whatever_cycle_finds_the_breach():
    """§10's floor is crossed intraday, and guard denies against it from that
    moment. Waiting for the post-close to record it would leave the 10:40
    breach able to alert again at 11:40."""
    ctx = report_context(cycle=MONITOR, account={"equity_total": 180.00})
    result = report.run(ctx)

    assert result.benchmark is None
    assert result.control_updates["last_drawdown_alert"] == "2026-08-06"


def test_the_scorecard_stamp_and_the_halt_stamp_do_not_displace_each_other():
    """Both are `control_updates` on the same post-close, and the routine
    merges what it is given. A drawdown on a Friday must not cost the week its
    scorecard dedupe."""
    ctx = report_context(
        cycle={"trading_day": "2026-08-07", "now": "2026-08-07T20:30:00Z"},
        account={"equity_total": 180.00},
    )
    updates = report.run(ctx).control_updates

    assert updates["last_scorecard_date"] == "2026-08-07"
    assert updates["last_drawdown_alert"] == "2026-08-07"


# --- §14's repeated rejection ------------------------------------------------


def test_a_blocked_placement_is_notified():
    """§14 escalates on a repeated rejection, and an escalation nobody is told
    about is not one. `cli.py` decides the block; §8.3's queue is this
    module's, so the message is too."""
    ctx = report_context(
        blocked=[
            {"order": {"symbol": "PFE", "side": "buy"}, "code": "PLACEMENT_BLOCKED",
             "detail": "insufficient buying power", "occurrences": 2,
             "priority": report.PRIORITY_DEFAULT, "message": "refused twice"}
        ]
    )

    result = report.run(ctx)

    assert result.status == report.STATUS_PARTIAL_SUCCESS
    assert result.alerts[0].title == "Post-close cycle partial"
    assert "insufficient buying power" in status_body(result)


def test_the_blocked_alert_carries_the_broker_sentence_verbatim():
    ctx = report_context(
        blocked=[
            {"order": {"symbol": "PFE", "side": "buy"}, "code": "PLACEMENT_BLOCKED",
             "detail": "Limit order quantity cannot include fractional shares",
             "occurrences": 2, "priority": report.PRIORITY_DEFAULT,
             "message": "refused twice"}
        ]
    )

    assert "Limit order quantity cannot include fractional shares" in status_body(
        report.run(ctx)
    )


def test_a_blocked_exit_escalates_the_status_over_a_blocked_entry():
    """A missed entry costs an opportunity and reads as `partial_success`; an
    exit that will not place leaves a position unprotected (§10.1 gives a
    FRACTIONAL position none between cycles), and reads as `blocked`. `cli.py`
    decides which it is by setting `priority`."""
    entry = {"order": {"symbol": "PFE", "side": "buy"}, "detail": "no buying power",
             "occurrences": 2, "priority": report.PRIORITY_DEFAULT}
    exit_ = {"order": {"symbol": "F", "side": "sell"}, "detail": "not tradable",
             "occurrences": 2, "priority": report.PRIORITY_HIGH}

    assert report.run(report_context(blocked=[entry])).status == (
        report.STATUS_PARTIAL_SUCCESS
    )
    assert report.run(report_context(blocked=[exit_])).status == report.STATUS_BLOCKED


def test_two_blocked_orders_are_one_notification():
    """Same argument as §12.2's findings: a push per blocked order is how a bad
    afternoon spends the operator's attention before the important one lands."""
    ctx = report_context(
        blocked=[
            {"order": {"symbol": "PFE", "side": "buy"}, "detail": "a",
             "occurrences": 2, "priority": report.PRIORITY_DEFAULT},
            {"order": {"symbol": "F", "side": "sell"}, "detail": "b",
             "occurrences": 3, "priority": report.PRIORITY_HIGH},
        ]
    )
    result = report.run(ctx)

    assert len(result.alerts) == 1
    assert "PFE" in result.alerts[0].body and "F" in result.alerts[0].body


def test_nothing_blocked_says_nothing_extra():
    ctx = report_context(blocked=[])

    assert status_body(report.run(ctx)) == f"Nothing to report.\n\n{_footer(ctx)}"


# --- benchmark integrity -------------------------------


def test_an_unexplained_equity_jump_is_notified():
    """Two consecutive daily rows that disagree by more than the recorded flows
    explain, on a day with no fill, is exactly what this fires on."""
    ctx = report_context(
        account={"equity_total": 1000.00},
        prior_benchmark={"trading_day": "2026-08-05", "equity": 400.00},
    )

    result = report.run(ctx)

    assert report.BENCHMARK_INTEGRITY in alerted(result)
    alert = next(a for a in result.alerts if a.title == report.BENCHMARK_INTEGRITY)
    assert alert.priority == report.PRIORITY_HIGH
    assert "400.00" in alert.body and "1,000.00" in alert.body


def test_an_unrecorded_withdrawal_is_notified_too():
    """The move is one-directional only in the incident; the check is not. An
    unrecorded withdrawal drops equity with nothing to explain it and reads as a
    loss of the same size — the hazard `tests/test_external_flows.py` pins."""
    ctx = report_context(
        account={"equity_total": 600.00},
        prior_benchmark={"trading_day": "2026-08-05", "equity": 900.00},
    )

    assert report.BENCHMARK_INTEGRITY in alerted(report.run(ctx))


def test_a_move_the_recorded_deposits_explain_is_silent():
    """A $250 jump matched by a $250 deposit dated into the same interval nets
    to zero unexplained — the row is doing its job, not failing."""
    ctx = report_context(
        account={"equity_total": 497.72},
        prior_benchmark={"trading_day": "2026-08-05", "equity": 247.72},
        inception={"deposits": [{"date": "2026-08-06", "amount": 250.00}]},
    )

    assert report.BENCHMARK_INTEGRITY not in alerted(report.run(ctx))


def test_a_move_inside_the_threshold_is_silent():
    """A day's ordinary P&L is not an event. $17.72 against a 247.72 prior is
    inside max(3%, $25)."""
    ctx = report_context(
        account={"equity_total": 265.44},
        prior_benchmark={"trading_day": "2026-08-05", "equity": 247.72},
    )

    assert report.BENCHMARK_INTEGRITY not in alerted(report.run(ctx))


def test_the_three_percent_band_widens_the_threshold_above_25():
    """On a larger account 3% is the binding term: 3% of 2000 is $60, so a $40
    unexplained move stays silent where the flat $25 floor would have flagged
    it."""
    ctx = report_context(
        account={"equity_total": 2040.00},
        prior_benchmark={"trading_day": "2026-08-05", "equity": 2000.00},
    )

    assert report.BENCHMARK_INTEGRITY not in alerted(report.run(ctx))


def test_a_fill_that_day_suppresses_the_integrity_check():
    """A move on a day the account traded is explained by the trading, and
    §8.4's series cannot be rebuilt later to tell the two apart."""
    ctx = report_context(
        account={"equity_total": 1000.00},
        prior_benchmark={"trading_day": "2026-08-05", "equity": 400.00},
        fills=[fill()],
    )

    assert report.BENCHMARK_INTEGRITY not in alerted(report.run(ctx))


def test_no_prior_benchmark_row_is_silent():
    """The first post-close the account ever writes has nothing to compare
    against; one row is not a series."""
    ctx = report_context(account={"equity_total": 5000.00})

    assert report.BENCHMARK_INTEGRITY not in alerted(report.run(ctx))


def test_the_integrity_check_is_post_close_only():
    """It compares two §8.4 rows, and only the post-close writes one. A monitor
    cycle with the same numbers returns before the benchmark section."""
    ctx = report_context(
        cycle=MONITOR,
        account={"equity_total": 1000.00},
        prior_benchmark={"trading_day": "2026-08-05", "equity": 400.00},
    )

    result = report.run(ctx)

    assert result.benchmark is None
    assert report.BENCHMARK_INTEGRITY not in alerted(result)


# --- the 09:40 no-activity summary, folded into the status push --


def test_a_quiet_full_cycle_gets_a_three_line_summary():
    """The one cycle a day that actually screens, saying so when it finds
    nothing — so silence never has to be read as "did it even run?"."""
    ctx = report_context(
        cycle=FULL,
        entries={
            "candidates": [],
            "skipped": [
                {"symbol": "APP", "message": "oversold but not above its "
                 "200-day; a falling knife, not a dip"},
            ],
        },
        exits={"exits": []},
    )

    body = status_body(report.run(ctx))
    lines = body.split("\n\n")[0].splitlines()

    assert len(lines) == 3
    assert lines[0].startswith("Buy:") and "APP" in lines[0]
    assert lines[1].startswith("Sell:")
    assert lines[2].startswith("Entries:")


def test_more_than_three_skipped_candidates_are_capped():
    """"cap at 3 reasons + and N more" — the exact
    truncation bug the old push had (a reason cut mid-sentence), just moved to
    a new body."""
    ctx = report_context(
        cycle=FULL,
        entries={
            "candidates": [],
            "skipped": [
                {"symbol": s, "message": f"reason {s}"}
                for s in ["A", "B", "C", "D", "E"]
            ],
        },
        exits={"exits": []},
    )

    buy_line = status_body(report.run(ctx)).split("\n\n")[0].splitlines()[0]

    assert "A" in buy_line and "B" in buy_line and "C" in buy_line
    assert "D" not in buy_line and "E" not in buy_line
    assert "and 2 more" in buy_line


def test_a_full_cycle_with_no_candidates_at_all_says_so():
    ctx = report_context(cycle=FULL, entries={"candidates": [], "skipped": []},
                          exits={"exits": []})

    body = status_body(report.run(ctx))

    assert body.split("\n\n")[0].splitlines()[0] == "Buy: no candidates surfaced"


def test_a_full_cycle_that_opened_something_gets_no_summary():
    """A placed entry has its own story — the fill is the news."""
    ctx = report_context(
        cycle=FULL, entries={"candidates": [{"symbol": "F"}], "skipped": []},
        exits={"exits": []},
    )

    assert status_body(report.run(ctx)) == f"Nothing to report.\n\n{_footer(ctx)}"


def test_a_full_cycle_that_exited_something_gets_no_summary():
    ctx = report_context(
        cycle=FULL, entries={"candidates": [], "skipped": []},
        exits={"exits": [{"symbol": "PFE"}]},
    )

    assert status_body(report.run(ctx)) == f"Nothing to report.\n\n{_footer(ctx)}"


def test_a_triggered_exit_blocked_on_a_fractional_share_says_so_not_none_met():
    """§10.1's manual-required case: the exit condition *did* fire, `exits.py`
    just could not price a limit order against it. Reporting "0 met an exit
    condition" here would misdescribe the DRAWDOWN_FLATTEN that already ran."""
    ctx = report_context(
        cycle=FULL, entries={"candidates": [], "skipped": []}, exits={"exits": []},
        manual_required=[
            {"symbol": "AMD", "code": "DRAWDOWN_FLATTEN", "reason": "fractional"},
        ],
    )

    body = status_body(report.run(ctx))
    activity = next(block for block in body.split("\n\n") if block.startswith("Buy:"))
    sell_line = activity.splitlines()[1]

    assert "manual" in sell_line.lower()
    assert report.MANUAL_ACTION_REQUIRED in sell_line


def test_the_drawdown_halt_is_named_in_the_status_line():
    ctx = report_context(cycle=FULL, account={"equity_total": 180.00},
                          entries={"candidates": [], "skipped": []},
                          exits={"exits": []})

    body = status_body(report.run(ctx))
    status_line = body.split("\n\n")[0].splitlines()[2]

    assert "drawdown halt" in status_line.lower()


def test_a_reconciliation_block_is_named_in_the_status_line():
    ctx = report_context(
        cycle=FULL, entries={"candidates": [], "skipped": []}, exits={"exits": []},
        control={"entries_blocked": True,
                 "entries_blocked_reason": "reconciliation divergence persisted"},
    )

    body = status_body(report.run(ctx))
    status_line = body.split("\n\n")[0].splitlines()[2]

    assert "reconciliation divergence persisted" in status_line


def test_a_monitor_cycle_never_gets_the_three_line_summary():
    """Scoped to `full` alone — `monitor` replays the same watchlist and a
    copy of the same answer three more times a day is exactly the noise
    §8.3's silence rule exists to prevent."""
    ctx = report_context(cycle=MONITOR, entries={"candidates": [], "skipped": []},
                          exits={"exits": []})

    assert status_body(report.run(ctx)) == f"Nothing to report.\n\n{_footer(ctx)}"


def test_alerts_are_emitted_on_cycles_that_write_no_benchmark_row():
    """The benchmark row is the post-close's job; §8.3 is every cycle's. A
    divergence found at 10:40 that waited for 16:30 would be six hours of the
    operator not knowing entries were blocked."""
    ctx = report_context(
        cycle=MONITOR,
        reconcile={
            "findings": [
                {"code": "PHANTOM_ORDER", "symbol": "MRK", "detail": "b",
                 "order_id": "zz99"}
            ]
        },
    )

    result = report.run(ctx)

    assert result.benchmark is None
    assert len(result.alerts) == 1
    assert result.alerts[0].title == "Monitor cycle succeeded with warnings"


# --- the footer --------------------------------


def test_the_footer_carries_the_cycle_id_time_and_routine():
    ctx = report_context(cycle_id="cse_017fXtc2CSsW63CoXbdMtiwp")

    body = status_body(report.run(ctx))

    assert "cycle cse_017f · 16:30 ET · Post-close" in body


def test_a_missing_cycle_id_degrades_rather_than_crashing():
    ctx = report_context()

    assert "cycle unknown ·" in status_body(report.run(ctx))


def test_runlog_ids_are_appended_only_when_the_caller_supplies_them():
    """`report.py` runs before its own `report` runlog row is written, so it
    can only ever name ids the caller already has — never invent one."""
    with_ids = report_context(runlog_ids=[476, 477, 479])
    without_ids = report_context()

    assert "runlog 476,477,479" in status_body(report.run(with_ids))
    assert "runlog" not in status_body(report.run(without_ids))


def test_a_pre_market_cycle_is_labelled_pre_market_not_premarket():
    ctx = report_context(cycle={"cycle_type": "premarket", "now": "2026-08-06T11:00:00Z"})

    assert report.run(ctx).alerts[0].title == "Pre-market cycle succeeded"


def test_an_overnight_cycle_is_labelled_overnight():
    ctx = report_context(cycle={"cycle_type": "overnight", "now": "2026-08-06T23:00:00Z"})

    assert report.run(ctx).alerts[0].title == "Overnight cycle succeeded"


# --- CANONICAL_TITLES stays in lockstep with the SQL regex ------------------

import re

_SQL_TITLE_PATTERN = re.compile(
    r"^(Full|Pre-market|Monitor|Post-close|Overnight) cycle "
    r"(succeeded|succeeded with warnings|partial|blocked|failed|skipped)$"
)
_SQL_FIXED_TITLES = {
    "Drawdown halt", "Account read implausible", "Weekly scorecard",
    report.BENCHMARK_INTEGRITY, "Order refused", "Manual action required",
    report.CYCLE_FAILED, report.PREMARKET_DATA_DEGRADED,
    report.CYCLE_STALLED, report.CYCLE_OVER_BUDGET, report.STEP_LOG_GAP,
    report.RESOLVE_SCOPE_TRIMMED,
}


def test_every_canonical_title_matches_the_sql_side_validator():
    """`db/schema.sql`'s `edge_alerts_enqueue` validates with
    the same regex-plus-fixed-list shape asserted here. This cannot run the
    live SQL, but a title Python generates that the SQL would reject is
    exactly the outage this prompt exists to prevent, and this at least catches
    it before a migration does."""
    for title in report.CANONICAL_TITLES:
        assert _SQL_TITLE_PATTERN.match(title) or title in _SQL_FIXED_TITLES, title


def test_a_cycle_that_placed_orders_names_them_instead_of_nothing_to_report():
    """Four TIME_STOP sells placed, alert read "Nothing to report."."""
    ctx = report_context(placed_orders=[
        {"symbol": "INVH", "side": "sell", "quantity": 7.0,
         "limit_price": 26.49, "play_id": "DIP"},
    ])

    body = status_body(report.run(ctx))

    assert "Placed: sell 7.0 INVH @ 26.49 limit (DIP)" in body
    assert "Nothing to report." not in body


# --- P2: PEAD's missing reaction rows, one line per run --------


def _pead_skips(*symbols, code="SKIP_NO_REACTION_ROW"):
    return {
        "candidates": [],
        "skipped": [
            {"symbol": s, "play_id": "PEAD", "code": code, "message": f"{s}: no row"}
            for s in symbols
        ],
    }


def test_three_names_with_no_reaction_row_are_one_line_in_one_alert():
    result = report.run(report_context(entries=_pead_skips("MU", "FDS", "JBL")))

    bodies = [a.body for a in result.alerts if "PEAD: no reaction row" in a.body]
    assert len(bodies) == 1
    assert bodies[0].count("PEAD: no reaction row") == 1
    assert "3 name(s) inside the window (FDS, JBL, MU)" in bodies[0]


def test_a_pending_reaction_is_not_reported():
    result = report.run(
        report_context(entries=_pead_skips("MU", code="SKIP_REACTION_PENDING"))
    )

    assert not any("PEAD: no reaction row" in a.body for a in result.alerts)


def test_a_cycle_with_every_reaction_row_says_nothing_about_pead():
    result = report.run(report_context())

    assert not any("PEAD: no reaction row" in a.body for a in result.alerts)


def test_the_pead_line_does_not_change_the_status_word():
    quiet = report.run(report_context())
    noisy = report.run(report_context(entries=_pead_skips("MU")))

    assert noisy.status == quiet.status


def test_the_report_and_the_play_agree_on_the_code():
    from src import plays

    assert report.PEAD_NO_REACTION_ROW == plays.SKIP_NO_REACTION_ROW
