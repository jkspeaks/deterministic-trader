"""Adding funds and taking them out, in and out of hours (operator requirement).

Requested: prove that an external cash flow changes nothing about the behaviour
of the code already written. This is a **cross-module scenario** test rather
than a set of unit tests, because every hazard here is an interaction — a flow
lands in one module's arithmetic and surfaces as a decision in another's.

It matters more than it looks. The v2 spec said *"no withdrawals; everything
compounds"*, so **a withdrawal is an unspecified path through every module**.
`config/inception.json` documents one as a negative `deposits` amount and
`report.net_deposits` sums it correctly, and until this file that was the only
place in the codebase that had considered one at all.

Each cycle below is run the way the routine runs it — reconcile, then decide,
then report, threading `edge_control` between them — because a flow that
changed nothing per module and something in the sequence would pass every
existing test in the suite.
"""

import pytest

from src import cli, guard, plays, sizing
from tests.factories import cli_payload, planned

CONFIG = cli.load_config()["config"]


def run_cycle(payload: dict, loaded: dict | None = None) -> dict:
    """One full invocation sequence, with `edge_control` threaded as §12 has it.

    reconcile clears `unreconciled` and maintains the high-water mark; decide
    reads the control reconcile just wrote and stamps its own; report reads
    both. Running the three against one frozen control would hide exactly the
    ordering bugs this file is looking for.
    """
    loaded = loaded or cli.load_config()

    reconciled = cli.reconcile(payload, loaded)
    payload = {
        **payload,
        "control": {**payload["control"], **reconciled["control_updates"]},
        "reconcile": reconciled,
    }

    decided = cli.decide(payload, loaded)
    payload = {
        **payload,
        "control": {**payload["control"], **decided["control_updates"]},
        "blocked": decided["blocked"],
    }

    reported = cli.report(payload, loaded)

    return {
        "reconcile": reconciled,
        "decide": decided,
        "report": reported,
        "control": {**payload["control"], **reported["control_updates"]},
    }


def funded(equity: float, **overrides) -> dict:
    """The baseline account at a different size, cash and equity in step."""
    payload = cli_payload(**overrides)
    payload["account"] = {
        **payload["account"],
        "equity_total": equity,
        "cash": round(equity - 136.42, 2),
        "buying_power": round(equity - 136.42, 2),
        "settled_cash": round(equity - 136.42, 2),
    }
    return payload


# --- hazard 1, and what closing it demands --------------
#
# `reconcile._high_water_mark` still ratchets and still never falls (settled
# call 24 stands), because a mark that tracked equity down would walk §10's
# floor down with it and the halt could never fire. What changed is the space
# it ratchets in: both the mark and the comparison now run on
# `sizing.flow_adjusted_equity`, equity minus cumulative net deposits. A flow
# moves both sides by the same amount and is therefore invisible to §10.
#
# The obligation that buys: **a flow the operator does not record still reads
# as a result.** Nothing in the system can see a transfer, so
# `config/inception.json` is how it is told. That is not a hedge, it is the
# whole mechanism, and the first test below pins the unrecorded case so nobody
# later mistakes it for a bug.


def withdrew(amount: float, date: str = "2026-08-03") -> dict:
    """The config the routine loads once a flow has been recorded.

    Negative for money out, exactly as `inception.json`'s `_deposits` note
    specifies, and dated by the trading day it landed in.
    """
    loaded = cli.load_config()
    return {
        **loaded,
        "inception": {
            **loaded["inception"],
            "deposits": [{"date": date, "amount": amount}],
        },
    }


def test_an_unrecorded_withdrawal_still_reads_as_a_loss():
    """The residual hazard, stated so it is never a surprise.

    §10's floor is `mark x 0.80`. The mark is 236.42, so the floor is 189.14,
    and an account showing 186.42 is beneath it. Nothing distinguishes that
    from a $50 loss **except the operator having recorded the transfer** — and
    a rule that guessed would make a real 21% loss invisible.
    """
    assert sizing.drawdown_breached({"equity_total": 186.42}, 236.42, CONFIG) is True


def test_withdrawing_unused_funds_does_not_halt_trading():
    """What the operator asked for: taking unused cash out changes nothing.

    It passes now because the flow is recorded and the mark is measured net of
    it.
    """
    result = run_cycle(funded(186.42), loaded=withdrew(-50.00))
    denials = [
        d["code"] for row in result["decide"]["guard"]["denied"] for d in row["denials"]
    ]

    assert guard.DRAWDOWN_HALT not in denials
    assert planned(result["decide"], "sell") == []


def test_depositing_and_then_withdrawing_the_same_money_changes_nothing():
    """The round trip the operator named, and the case the ratchet made worse.

    Deposit $100 and the mark used to ratchet to the funded equity; take the
    same $100 back out and the floor stood at 269.14 against an account exactly
    where it started. Adjusted, the mark never moves on the way in, so there is
    nothing to come back down from.
    """
    deposited = run_cycle(funded(336.42), loaded=withdrew(100.00))
    mark = deposited["control"]["high_water_mark"]

    assert mark == 236.42
    assert sizing.drawdown_breached({"equity_total": 236.42}, mark, CONFIG) is False


def test_a_real_loss_of_the_same_size_still_halts():
    """The half that matters more. Flow-adjusting the mark must not have bought
    its safety by making §10 unreachable — an unfunded 21% fall still halts."""
    result = run_cycle(funded(186.42))
    denials = [
        d["code"] for row in result["decide"]["guard"]["denied"] for d in row["denials"]
    ]

    assert guard.DRAWDOWN_HALT in denials


def test_the_halt_a_withdrawal_causes_flattens_the_whole_book():
    """Why hazard 1 is the sharp one rather than a curiosity. §6.5 exit 2 is
    not a warning — it sells everything, at market, on the next cycle."""
    result = run_cycle(funded(186.42))

    assert planned(result["decide"], "sell") == ["F"]
    assert result["decide"]["exits"]["exits"][0]["code"] == plays.DRAWDOWN_FLATTEN


def test_a_deposit_alone_is_safe_and_lifts_a_halt_by_itself():
    """The other direction, and the reason a settled design decision leaves
    `trading_enabled` alone: funding the account clears §10's floor on the very
    next cycle, with no flag for the operator to remember to reset."""
    halted = funded(180.00)
    halted["control"] = {**halted["control"], "high_water_mark": 236.42}

    before = run_cycle(halted)
    after = run_cycle(funded(300.00, control={"high_water_mark": 236.42}))

    assert planned(before["decide"], "sell") == ["F"]
    assert planned(after["decide"], "sell") == []


# --- hazard 2: Invariant 1, and a deposit that has not settled -------------


def test_a_fresh_deposit_does_not_become_spendable_buying_power():
    """§2.3: every buy is funded by settled cash, and a same-day transfer is
    not settled. Robinhood shows it in `buying_power` immediately, so the
    conservative §2.4 basis — `min(buying_power, settled_cash)` — is the only
    thing standing between the deposit and a Good Faith Violation."""
    thin = funded(236.42)
    thin["account"] = {**thin["account"], "cash": 14.52, "buying_power": 14.52,
                       "settled_cash": 14.52}
    deposited = {**thin}
    deposited["account"] = {**thin["account"], "equity_total": 336.42,
                            "cash": 114.52, "buying_power": 114.52,
                            "settled_cash": 14.52}

    result = run_cycle(deposited)

    assert result["decide"]["sizing"]["ladder"]["deployable"] <= 14.52


def test_an_unsettled_deposit_cannot_fund_an_order_it_does_not_cover():
    """The consequence, through guard rather than through the ladder. An order
    sized against $114 of buying power would be denied at placement anyway —
    Invariant 1 exists so it is never proposed."""
    deposited = funded(336.42)
    deposited["account"] = {**deposited["account"], "buying_power": 114.52,
                            "settled_cash": 14.52}

    result = run_cycle(deposited)
    approved = [o for o in result["decide"]["plan"]["orders"] if o["side"] == "buy"]

    assert all(o["quantity"] * (o["limit_price"] or 0) <= 14.52 for o in approved)


# --- hazard 3: the capital ladder moves under an unchanged book ------------


def test_a_deposit_moves_the_rung_without_touching_the_book():
    """§4.1 derives position count from equity every cycle (Invariant 5), so
    money arriving changes the shape of the system. That is the design working
    — but it must not, by itself, cause a trade."""
    before = run_cycle(funded(236.42))["decide"]
    after = run_cycle(funded(700.42))["decide"]

    assert before["sizing"]["ladder"]["n_target"] == 3
    assert after["sizing"]["ladder"]["n_target"] == 4
    assert planned(before, "sell") == planned(after, "sell") == []


def test_a_withdrawal_that_puts_a_position_over_its_cap_does_not_sell_it():
    """§4.4's caps are fractions of equity, so a withdrawal can push an
    existing position over a cap it was comfortably inside when it was opened.

    Guard denies *buys* against the caps and there is no §6.5 exit for
    "concentrated" — so the position is simply not added to. Selling to fit a
    cap would be the system repairing a position the operator's own transfer
    created, which is §12.2's rule inverted."""
    over = funded(206.42)
    over["positions"] = [{"symbol": "F", "quantity": 1.0, "market_value": 85.00}]

    result = run_cycle(over)

    assert 85.00 > 206.42 * sizing.position_cap_fraction(3)
    assert planned(result["decide"], "sell") == []


def test_a_withdrawal_never_widens_a_cap():
    """The direction that would be dangerous. Caps are fractions, so less
    equity is always a tighter dollar limit, never a looser one."""
    rich = run_cycle(funded(336.42))["decide"]["sizing"]["ladder"]
    poor = run_cycle(funded(206.42))["decide"]["sizing"]["ladder"]

    assert poor["position_cap"] < rich["position_cap"]
    assert poor["sector_cap"] < rich["sector_cap"]


# --- hazard 4: the flow must not be scored as return -----------------------


def postclose(equity: float, deposits: list[dict]) -> dict:
    """A Friday post-close, so the scorecard is computed and the flow shows up."""
    loaded = cli.load_config()
    loaded["inception"] = {**loaded["inception"], "deposits": deposits}
    payload = funded(
        equity,
        now="2026-08-07T20:30:00Z",
        control={"positions_held": 1},
    )
    return run_cycle(payload, loaded)["report"]


def test_a_deposit_is_not_scored_as_a_gain():
    """§1 asks what the *agent* did. A deposit arrives as equity and is
    arithmetically identical to a gain, which is why `edge_benchmark` has five
    columns rather than §8.4's four."""
    reported = postclose(347.72, [{"date": "2026-08-05", "amount": 100.00}])

    assert reported["benchmark"]["net_deposits"] == 100.00
    assert reported["scorecard"]["agent_return"] == 0.0


def test_a_withdrawal_is_not_scored_as_a_loss():
    """The direction v2 said would never happen, and the sign that had never
    been exercised end to end. Taking $50 out drops equity $50; the agent did
    not lose it."""
    reported = postclose(197.72, [{"date": "2026-08-05", "amount": -50.00}])

    assert reported["benchmark"]["net_deposits"] == -50.00
    assert reported["scorecard"]["agent_return"] == 0.0


def test_a_round_trip_leaves_the_score_exactly_where_it_started():
    reported = postclose(
        247.72,
        [{"date": "2026-08-05", "amount": 100.00},
         {"date": "2026-08-06", "amount": -100.00}],
    )

    assert reported["benchmark"]["net_deposits"] == 0.0
    assert reported["scorecard"]["agent_return"] == 0.0


def test_a_real_gain_is_still_scored_as_one_alongside_a_flow():
    """The test above would pass on a module that scored nothing at all.

    Money-weighted against the *deposit-adjusted* base (`inception_equity +
    net_deposits`), not the raw inception base — 357.72 against a base of
    247.72 + 100.00 deposited."""
    reported = postclose(357.72, [{"date": "2026-08-05", "amount": 100.00}])

    assert reported["scorecard"]["agent_return"] == round(357.72 / 347.72 - 1, 6)


# --- hazard 5: a flow that lands out of hours ------------------------------


def test_a_flow_is_dated_by_the_trading_day_it_lands_in():
    """`cadence.trading_day` shifts at 20:00 ET, so a transfer landing at 20:30
    on Thursday belongs to **Friday's** session. Dated that way, the deposit
    and the `edge_benchmark` row that first sees the money agree."""
    reported = postclose(347.72, [{"date": "2026-08-07", "amount": 100.00}])

    assert reported["benchmark"]["trading_day"] == "2026-08-07"
    assert reported["benchmark"]["net_deposits"] == 100.00
    assert reported["scorecard"]["agent_return"] == 0.0


def test_dating_an_after_hours_flow_to_the_wrong_session_misstates_one_day():
    """Why the convention above is a rule and not a preference.

    A transfer at 20:30 Thursday dated to Thursday is counted against a row
    that was written at 16:30, four hours before the money existed — so that
    day's return is understated by the whole amount, permanently, because §8.4
    says the series cannot be rebuilt afterwards. The row for the *following*
    day is correct either way, so the error is exactly one session wide.
    """
    misdated = postclose(247.72, [{"date": "2026-08-06", "amount": 100.00}])

    assert misdated["benchmark"]["net_deposits"] == 100.00
    assert misdated["scorecard"]["agent_return"] == round(247.72 / 347.72 - 1, 6)


def test_a_flow_dated_after_this_session_is_not_counted_yet():
    """`report.net_deposits` is dated rather than running, so a cycle cannot
    see money that has not landed and a replayed cycle cannot double one."""
    early = postclose(236.42, [{"date": "2026-08-10", "amount": 100.00}])

    assert early["benchmark"]["net_deposits"] == 0.0


# --- and the rest of the system, unmoved -----------------------------------


def test_a_flow_does_not_disturb_the_reconciliation_it_runs_alongside():
    """§12.2 compares intent against broker truth, and a cash movement is
    neither. The book must reconcile identically at any account size."""
    poor = run_cycle(funded(206.42))["reconcile"]
    rich = run_cycle(funded(336.42))["reconcile"]

    assert poor["findings"] == rich["findings"] == []
    assert poor["positions"] == rich["positions"]


def test_a_flow_is_not_an_alertable_event():
    """§8.3's right-hand column. The operator moving their own money is not
    news to the operator — the cycle's one folded status push still fires,
    but says nothing about the flow."""
    alerts = run_cycle(funded(336.42))["report"]["alerts"]

    assert len(alerts) == 1
    assert alerts[0]["priority"] == "min"
    assert "Nothing to report." in alerts[0]["body"]
