"""§14's last row: "same rejection 2 cycles running ⇒ escalate PLACEMENT_BLOCKED".

No other module can own this. Runbook §7 located the boundary the broker
validates along — *it validates facts it can look up about this account, and
does not validate whether the order is legal* — so `guard.py` enforces order
legality and the broker enforces account state. What is left over reaches
placement and is refused there, and the refusal text exists only **after**
placement. `cli.py` is the only thing that sees the same order refused twice.

The key is the whole design. It is ``symbol + side + type + whole-vs-fractional +
the verbatim broker text``, and it is that wide because of one scenario: a
fractional market buy refused "cannot be traded in fractional shares" must **not**
block the whole-share limit buy the same name earns once equity has grown. A
`(symbol, side)` key would fence the account out of a name it can now legitimately
buy, which is a worse failure than the retry it prevents.

Nothing here is stored. The block is recomputed from the day's runlog every
cycle, so it clears when the shape changes or the day rolls over — one retry a
day after two identical failures is not a blind retry.
"""

from src import cli, guard
from tests.factories import NOTHING_TO_PROTECT, cli_payload, planned

TOO_SMALL = "Your order could not be placed: insufficient buying power"
NOT_FRACTIONAL = "This security cannot be traded in fractional shares"


def refused(detail: str = TOO_SMALL, times: int = 2, **order) -> list[dict]:
    """``times`` runlog rows: the order that was sent, and the broker's answer.

    The default is the whole-share PFE limit buy the baseline cycle produces,
    so a test names only the field it wants to differ.
    """
    row = {
        "order": {
            "symbol": "PFE", "side": "buy", "type": "limit", "quantity": 1.0, **order
        },
        "detail": detail,
    }
    return [row for _ in range(times)]


def decided(**overrides) -> dict:
    return cli.decide(cli_payload(**overrides), cli.load_config())


def blocked_symbols(result: dict) -> list[str]:
    return [row["order"]["symbol"] for row in result["blocked"]]


# --- the escalation --------------------------------------------------------


def test_an_order_refused_twice_is_not_sent_a_third_time():
    """§14: "no blind retry". The second attempt is the blind one; by the third
    the broker has said the same sentence twice and nothing about the order has
    changed."""
    result = decided(rejections=refused())

    assert planned(result) == []
    assert blocked_symbols(result) == ["PFE"]


def test_one_refusal_is_not_yet_a_pattern():
    """"2 cycles running" is two. A single rejection can be a quote that moved
    between the fetch and the placement, and refusing to retry that would make
    the system give up on its first small setback."""
    result = decided(rejections=refused(times=1))

    assert planned(result) == ["PFE"]
    assert result["blocked"] == []


def test_the_broker_sentence_survives_verbatim_onto_the_block():
    """§14: never paraphrased and never bucketed. "Limit order quantity cannot
    include fractional shares" is the sentence that diagnosed an entire
    invariant on this account."""
    result = decided(rejections=refused(detail=NOT_FRACTIONAL))

    assert result["blocked"][0]["detail"] == NOT_FRACTIONAL


def test_the_block_records_how_many_times_it_happened():
    """A repeat is a signal, not a second log line — so the count is the
    signal and it belongs in the record."""
    result = decided(rejections=refused(times=3))

    assert result["blocked"][0]["occurrences"] == 3


# --- the key: what counts as "the same rejection" --------------------------


def test_a_whole_share_order_is_not_blocked_by_a_fractional_refusal():
    """The scenario the key exists for. A fractional market buy refused twice
    says nothing about a whole-share limit buy — that is a different order in
    every respect that caused the refusal, and blocking it would fence the
    account out of a name it can now legitimately buy."""
    result = decided(
        rejections=refused(detail=NOT_FRACTIONAL, type="market", quantity=0.395)
    )

    assert planned(result) == ["PFE"]
    assert result["blocked"] == []


def test_the_same_shape_at_a_different_size_is_still_the_same_order():
    """Quantity drifts by pennies of notional every cycle as the ladder moves.
    A key that included it would never match twice and the escalation would be
    unreachable."""
    result = decided(rejections=refused(quantity=2.0))

    assert planned(result) == []


def test_two_different_refusals_are_two_problems_not_one_repeat():
    """Both are worth surfacing and neither is the pattern §14 describes. The
    order is still attempted, because nothing has been said twice."""
    result = decided(
        rejections=[
            *refused(detail=TOO_SMALL, times=1),
            *refused(detail=NOT_FRACTIONAL, times=1),
        ]
    )

    assert planned(result) == ["PFE"]


def test_a_refusal_on_the_other_side_does_not_block_this_one():
    """A buy the broker keeps refusing must never strand the exit on the same
    name. That is the direction §6.5 cannot tolerate being wrong in."""
    result = decided(rejections=refused(side="sell"))

    assert planned(result, "buy") == ["PFE"]


def test_a_refusal_on_another_symbol_is_not_this_symbols_problem():
    result = decided(rejections=refused(symbol="AAPL"))

    assert planned(result) == ["PFE"]


# --- where the block sits in the pipeline ---------------------------------

def test_a_blocked_order_never_reaches_guard():
    """It is a fact about the order's history, prior to legality. Guard stays
    the sole authority on whether an order *may* be placed, and a plan that
    showed the same order both approved and blocked would be a runlog nobody
    could read."""
    result = decided(rejections=refused())
    seen = [o["symbol"] for o in result["guard"]["approved"]]
    seen += [d["order"]["symbol"] for d in result["guard"]["denied"]]

    assert "PFE" not in seen


def test_a_blocked_buy_does_not_spend_the_days_allowance():
    result = decided(rejections=refused())

    assert result["control_updates"]["new_positions"]["count"] == 0


def test_a_blocked_cycle_that_places_nothing_stays_reconciled():
    """Nothing was sent, so §12.1 has nothing to reconcile next cycle.

    `NOTHING_TO_PROTECT` because §10.1's resting orders are orders too, and
    the baseline book earns two of them on every cycle.
    """
    result = decided(rejections=refused(), reconcile=NOTHING_TO_PROTECT)

    assert "unreconciled" not in result["control_updates"]


# --- an unplaced exit is worse than an unplaced entry ----------------------


def test_a_blocked_sell_is_marked_more_urgently_than_a_blocked_buy():
    """A missed entry costs an opportunity; an exit that will not place leaves
    a position unprotected, and §10.1 already says a FRACTIONAL position has no
    protection between cycles at all."""
    result = decided(
        quotes={"F": {"bid": 13.43, "ask": 13.47, "last": 13.45}},
        # Limit, not market: every exit places as a limit order, and
        # `_shape`'s match key includes order type.
        rejections=refused(symbol="F", side="sell", type="limit"),
    )
    row = next(r for r in result["blocked"] if r["order"]["symbol"] == "F")

    assert row["order"]["side"] == "sell"
    assert row["priority"] == cli.BLOCKED_EXIT
    assert decided(rejections=refused())["blocked"][0]["priority"] == cli.BLOCKED_ENTRY


# --- one definition of a fractional quantity ------------------------------


def test_the_fractional_test_is_the_one_guard_denies_on():
    """Two answers to "is this order fractional" would drift, and the day they
    did, a block key would treat as whole an order guard was refusing as
    fractional. Same rule as §4.4's cap fractions and `TERMINAL_ORDER_STATES`:
    defined once, imported."""
    assert cli._is_fractional is guard.is_fractional
