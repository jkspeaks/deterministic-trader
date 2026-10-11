"""Five cycles recorded whole (§15).

* `quiet_postclose` — Monday's close. A benchmark row and silence. The shape
  of four days in five.
* `weekly_scorecard` — Friday's close: the one message this system sends when
  everything is working.
* `bad_afternoon` — a monitor cycle where the placement threw, reconciliation
  diverged and the agent switched a play off. No benchmark row, three alerts,
  and the three of them must not collapse into one.
* `drawdown_halt` — §10 firing. The alert that means the system has stopped
  itself.
* `unpriceable_frozen_book` — one inception holding without a quote. The row is
  written, two bars are measured, and the third says why it is missing.
"""

import json
from pathlib import Path

import pytest

from src import report
from tests.factories import INCEPTION_QUOTES, report_context

GOLDEN = Path(__file__).parent / "golden" / "report"


def without_quote(symbol: str) -> dict:
    """A context genuinely missing one quote.

    By assignment, because `factories._merge` deep-merges: passing a quotes map
    with `symbol` left out would merge onto the baseline and leave it exactly
    where it was, and the scenario would silently record the ordinary case.
    """
    ctx = report_context()
    ctx["quotes"] = {s: q for s, q in INCEPTION_QUOTES.items() if s != symbol}
    return ctx


SCENARIOS = {
    "quiet_postclose": report_context(),
    "weekly_scorecard": report_context(
        cycle={"trading_day": "2026-08-07", "now": "2026-08-07T20:30:00Z"},
        account={"equity_total": 260.01},
        quotes={"SPY": {"last": 784.36}},
        play_state={
            "DIP": {"regime_fit": 1.0, "reason": "range-bound tape",
                    "closed_trades": 5, "realized_pnl": 6.40},
            "PEAD": {"regime_fit": 0.0, "reason": "drift stopped paying",
                     "closed_trades": 2, "realized_pnl": -4.10},
        },
        # PEAD was switched off earlier in the week and alerted then. The
        # scorecard still reports it as disabled; the alert does not repeat.
        prior_play_state={"PEAD": {"regime_fit": 0.0}},
    ),
    "bad_afternoon": report_context(
        cycle={"cycle_type": "monitor", "now": "2026-08-06T15:40:00Z"},
        failure={
            "step": "place_equity_order",
            "detail": "Limit order quantity cannot include fractional shares",
        },
        reconcile={
            "findings": [
                {"code": "PHANTOM_ORDER", "symbol": "MRK",
                 "detail": "the broker has an open buy of 1 MRK (zz99) that no "
                           "plan proposed; entries are blocked for one cycle",
                 "order_id": "zz99"},
                {"code": "UNTRACKED_FILL", "symbol": "GME",
                 "detail": "adopted and managed on identical terms",
                 "order_id": "yy88"},
            ]
        },
        play_state={"DIP": {"regime_fit": 0.0, "reason": "dips keep going"}},
    ),
    # A 24% drawdown on a day the whole market is down 7.6% and the frozen
    # book with it. Marking only SPY down would record an account that fell
    # alone while ten equities it used to hold sat still — a picture that
    # cannot happen, and the frozen bar is the one that says how much of the
    # fall was the agent's doing.
    "drawdown_halt": report_context(
        account={"equity_total": 180.00},
        quotes={
            "SPY": {"last": 690.00},
            **{
                symbol: {"last": round(quote["last"] * 0.90, 2)}
                for symbol, quote in INCEPTION_QUOTES.items()
                if symbol != "SPY"
            },
        },
    ),
    # CAT went unquoted — a halt, a delisting, or a fetch that came back
    # short. §14's shape: the row is still written, the two bars that can be
    # measured are measured, and the third says why it is not there.
    "unpriceable_frozen_book": without_quote("CAT"),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_the_cycle_matches_its_golden_file(name):
    expected = json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))

    assert report.run(SCENARIOS[name]).as_dict() == expected


# --- the properties the golden files are there to protect ------------------


def test_the_quiet_close_writes_a_row_and_pushes_once_at_min_priority():
    """Four closes in five look like this. Still one push, not zero — a
    `min`-priority "nothing happened" is how a missing push (a dead relay, a
    cycle that never ran) stays distinguishable from a quiet market."""
    result = report.run(SCENARIOS["quiet_postclose"])

    assert result.benchmark is not None
    assert len(result.alerts) == 1
    assert result.alerts[0].priority == report.PRIORITY_MIN
    assert result.scorecard is None


def test_the_weekly_push_carries_every_bar_and_every_play():
    """The whole deliverable in one message: what the account did, what the
    three §1 benchmarks did, and which play is responsible for which part."""
    result = report.run(SCENARIOS["weekly_scorecard"])
    body = next(a.body for a in result.alerts if a.title == report.WEEKLY_SCORECARD)

    assert "+1.69%" in body           # SPY
    assert "money-weighted" in body
    assert "DIP: 5 closed, $6.40" in body
    assert "PEAD: 2 closed, -$4.10 (disabled)" in body


def test_a_play_that_was_already_off_is_reported_without_re_alerting():
    """PEAD was switched off earlier in the week and alerted then. §8.3
    notifies the *change*; the scorecard reports the state. A system that
    re-alerted every cycle a play stayed dark would push it eleven times a
    day for as long as the agent's judgment held — the folded status push's
    play fragment is empty here, so it reads as `Post-close cycle succeeded`."""
    result = report.run(SCENARIOS["weekly_scorecard"])

    titles = [a.title for a in result.alerts]

    assert report.WEEKLY_SCORECARD in titles
    assert "ADOPTED" not in " ".join(titles)  # no separate play-change push
    scorecard = next(a for a in result.alerts if a.title == report.WEEKLY_SCORECARD)
    assert "(disabled)" in scorecard.body


def test_three_different_things_going_wrong_fold_into_one_status_push():
    """The failure, the divergence and the play change that used to push three
    overlapping high-priority alerts now fold into the cycle's one status
    push, titled with the worst thing that happened — `failed`, since the
    placement itself threw."""
    result = report.run(SCENARIOS["bad_afternoon"])

    assert [a.title for a in result.alerts] == ["Monitor cycle failed"]
    body = result.alerts[0].body
    assert "place_equity_order failed" in body
    assert "PHANTOM_ORDER" in body
    assert "DIP disabled" in body


def test_the_broker_sentence_survives_verbatim_into_the_alert():
    """§14: the exact rejection string, "never paraphrased and never
    bucketed". "Limit order quantity cannot include fractional shares" is the
    sentence that diagnosed an entire invariant on this account."""
    body = report.run(SCENARIOS["bad_afternoon"]).alerts[0].body

    assert "Limit order quantity cannot include fractional shares" in body


def test_the_operators_own_fill_is_not_in_the_folded_status_push():
    """Both findings arrived in the same reconcile result; only one of them is
    a fault. §12.2 logs the other."""
    body = report.run(SCENARIOS["bad_afternoon"]).alerts[0].body

    assert "MRK" in body
    assert "GME" not in body


def test_a_monitor_cycle_alerts_without_writing_a_row():
    """§13 gives the benchmark row to the post-close. Waiting until 16:30 to
    report a 15:40 failure would be fifty minutes of an operator not knowing
    the cycle had thrown."""
    result = report.run(SCENARIOS["bad_afternoon"])

    assert result.benchmark is None
    assert len(result.alerts) == 1


def test_the_halt_is_the_only_urgent_thing_this_module_can_say():
    """§10's stop is the one alert that means the system has switched itself
    off, and it is the only one at this priority — which is what makes the
    priority mean anything. The folded status push still fires alongside it,
    at whatever priority the cycle's own outcome earns (`min`, here — nothing
    else went wrong)."""
    result = report.run(SCENARIOS["drawdown_halt"])

    assert report.PRIORITY_URGENT in [a.priority for a in result.alerts]
    urgent = [a for a in result.alerts if a.priority == report.PRIORITY_URGENT]
    assert [a.title for a in urgent] == [report.DRAWDOWN_HALT]


def test_an_unpriceable_book_still_writes_the_row_it_can():
    """Degradation, not failure (§14). Equity and SPY are true today and
    unobtainable tomorrow, so the row is written with a hole in one column
    and a note saying which name made the hole — rather than a frozen book
    quietly $25 lighter than the one that existed. The cycle still gets its
    one `min`-priority status push; a missing quote is not this module's
    business to escalate."""
    result = report.run(SCENARIOS["unpriceable_frozen_book"])

    assert result.benchmark.frozen_value is None
    assert result.benchmark.spy_close == 771.33
    assert [n.symbol for n in result.notes] == ["CAT"]
    assert len(result.alerts) == 1
    assert result.alerts[0].priority == report.PRIORITY_MIN


def test_the_halt_cycle_still_writes_its_benchmark_row():
    """The day the account fell 24% is the single most important row in the
    series, and §8.4 says it cannot be reconstructed afterwards."""
    result = report.run(SCENARIOS["drawdown_halt"])

    assert result.benchmark.equity == 180.00
