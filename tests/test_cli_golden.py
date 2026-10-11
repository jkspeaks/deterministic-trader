"""Five cycles recorded whole, as the routine would run them (§15).

Each scenario records **both** commands that carry logic — `plan-fetch` and
`decide` — because they are two halves of one decision and the interesting
failures live between them. A fetch list that omitted tradability and a plan
that then denied every order as `NOT_TRADABLE` would each look correct alone.

* `quiet_full_cycle` — 09:40 Monday. One position held and quiet, one entry
  taken. The shape of an ordinary day.
* `noop_cycle` — §13's cheap path. The economics of the whole system: nothing
  fetched, nothing looked at, nothing placed.
* `overnight_exit` — 20:00, no reconciliation, the book carried forward and
  aged, one position through its stop and past §6.6's spread gate.
* `drawdown_flatten` — §10 firing. §6.5 exit 2 flattens while guard refuses
  every buy, which is the division `guard.py`'s docstring exists to protect.
* `blocked_placement` — §14's repeat. The order is gone from the plan and the
  broker's sentence survives verbatim.
* `day_one` — Appendix A.1's real book, on the first cycle the system ever
  runs. A.1 predicts "near-full liquidation"; this records what actually
  happens, and the property tests below name which of its claims hold.
"""

import json
from pathlib import Path

import pytest

from src import cli, plays
from tests.factories import cli_payload, planned

GOLDEN = Path(__file__).parent / "golden" / "cli"

# Appendix A.1, read live from account ••0000 and marked at an earlier close.
# Symbol: (quantity, average cost, last).
DAY_ONE = {
    "INTC": (0.275534, 105.25, 90.21),
    "CAT": (0.030951, 936.96, 814.66),
    "MU": (0.030851, 940.00, 822.59),
    "C": (0.209711, 138.29, 132.44),
    "TXN": (0.070371, 278.95, 275.55),
    "MS": (0.092555, 212.09, 210.56),
    "MRK": (0.153954, 129.00, 130.22),
    "ADI": (0.054269, 361.72, 367.51),
    "SMH": (0.037344, 525.65, 540.16),
    "AMD": (0.043408, 452.22, 476.24),
}

# A.1 records no ATR. 2% of price is an ordinary daily range for these names,
# applied uniformly so the result is not tuned toward the answer A.1 predicts —
# the point of this scenario is to find out whether that prediction holds.
DAY_ONE_ATR_PCT = 0.02

# A.1 records no 50-day either, and production always returns one: `signals` is
# `get_equity_technical_indicators`, which carries rsi, sma_20, sma_50, sma_200
# and atr for every name fetched. Omitting it made every top-up fail closed, so
# this scenario claimed the account opens nothing for a reason that had stopped
# being the reason. Uniformly 3% below last, on the same principle as the ATR:
# it puts all ten above their 50-day, which leaves the *profit* gate — the one
# that decides here — to discriminate on A.1's own recorded costs.
DAY_ONE_SMA_50_PCT = 0.97

SECTORS = {
    "INTC": "Information Technology", "CAT": "Industrials",
    "MU": "Information Technology", "C": "Financials",
    "TXN": "Information Technology", "MS": "Financials",
    "MRK": "Health Care", "ADI": "Information Technology",
    "SMH": "Information Technology", "AMD": "Information Technology",
}


def day_one() -> dict:
    """The account exactly as Appendix A.1 measured it, on its first cycle.

    Every position is adopted, and that means `play_id: ADOPTED` rather than `MANUAL` —
    bound by `reconcile._attribution` because none of the ten fires any play's trigger, and
    carrying a 10% target and a 15-session clock where §12.2 originally gave neither. The
    account's ATR stop is not play property and still applies to all ten, which is what
    "managed on identical terms" means. None of them is in the S&P snapshot either, so each
    is an `ADOPTED_NON_MEMBER` admitted on Invariant 7's authority.

    The orders this produces are unchanged by that binding, and deliberately so:
    the best of the ten is AMD at 5.3% above cost, so nothing reaches adoption's
    10% target, and `sessions_held` is None on day one so no clock can expire.
    What changed is that the book can now *sell up* at all.
    """
    payload = cli_payload(
        control={"positions_held": 10, "has_whole_share_position": False},
        account={
            "equity_total": 236.42, "cash": 14.52,
            "buying_power": 14.52, "settled_cash": 14.52,
        },
        triggers=[],
    )
    payload["positions"] = [
        {"symbol": s, "quantity": q, "market_value": round(q * last, 2)}
        for s, (q, _, last) in DAY_ONE.items()
    ]
    payload["reconcile"] = {
        "findings": [], "cancels": [],
        "positions": [
            # Adopted today, which is what makes day one day one: the clock
            # §6.5 counts starts here, not at whatever these lots cost.
            {"symbol": s, "play_id": plays.ADOPTED, "quantity": q,
             "entry_price": cost, "sessions_held": None, "adopted": True,
             "adopted_on": "2026-08-03"}
            for s, (q, cost, _) in DAY_ONE.items()
        ],
    }
    payload["quotes"] = {
        s: {"bid": round(last * 0.999, 2), "ask": round(last * 1.001, 2), "last": last}
        for s, (_, _, last) in DAY_ONE.items()
    }
    payload["signals"] = {
        s: {"atr": round(last * DAY_ONE_ATR_PCT, 4),
            "sma_50": round(last * DAY_ONE_SMA_50_PCT, 2)}
        for s, (_, _, last) in DAY_ONE.items()
    }
    # **The broker's shape, not the derived one.** It cost nothing while day
    # one proposed no entries and costs four top-ups now that it does.
    payload["earnings"] = {
        s: {"data": {"results": [
            {"symbol": s, "year": 2026, "quarter": 3,
             "eps": {"estimate": "1.000000", "actual": None},
             "report": {"date": "2026-11-16", "timing": "pm", "verified": True}},
        ]}}
        for s in DAY_ONE
    }
    payload["tradability"] = {
        s: {"tradeable": True, "fractional_tradability": "tradable",
            "all_day_tradability": "tradable"}
        for s in DAY_ONE
    }
    payload["sectors"] = SECTORS
    return payload


def carried_book() -> dict:
    """20:00 ET. §12.1 does not reconcile here, so the book is last cycle's."""
    return {
        "trading_day": "2026-08-03",
        "positions": [
            {"symbol": "F", "play_id": "DIP", "quantity": 1.0,
             "entry_price": 15.00, "sessions_held": 2, "adopted": False,
             "adopted_on": None}
        ],
    }


def overnight() -> dict:
    payload = cli_payload(
        now="2026-08-04T00:30:00Z",
        control={"has_whole_share_position": True, "positions_held": 1},
        # F has gapped through its 13.65 stop. The spread is ~0.3%, well inside
        # §6.6's 1% gate, so the exit places rather than deferring to the open.
        quotes={"F": {"bid": 13.43, "ask": 13.47, "last": 13.45}},
    )
    payload["reconcile"] = None
    payload["prior_book"] = carried_book()
    return payload


SCENARIOS = {
    "quiet_full_cycle": cli_payload(),
    "noop_cycle": cli_payload(
        now="2026-08-03T14:40:00Z",
        control={
            "last_full_cycle": "2026-08-03T13:40:00Z",
            "positions_held": 0,
            "new_positions": {"date": "2026-08-03", "count": 3},
        },
    ),
    "overnight_exit": overnight(),
    # Equity 180 against a 236.42 mark is a 24% drawdown, so §10's floor of
    # 189.14 is breached: plays flattens and guard refuses the entry.
    "drawdown_flatten": cli_payload(account={"equity_total": 180.00}),
    "blocked_placement": cli_payload(
        rejections=[
            {"order": {"symbol": "PFE", "side": "buy", "type": "limit",
                       "quantity": 1.0},
             "detail": "Limit order quantity cannot include fractional shares"}
        ] * 2
    ),
    "day_one": day_one(),
}


def recorded(payload: dict) -> dict:
    loaded = cli.load_config()
    return {
        "plan_fetch": cli.plan_fetch(payload, loaded),
        "decide": cli.decide(payload, loaded),
    }


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_the_cycle_matches_its_golden_file(name):
    expected = json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))

    assert recorded(SCENARIOS[name]) == expected


# --- the properties the golden files are there to protect ------------------


def test_the_ordinary_day_places_one_order_and_says_why():
    """Nine cycles in ten look like this: a book that is fine, and at most one
    thing to do about it."""
    result = recorded(SCENARIOS["quiet_full_cycle"])["decide"]

    assert planned(result) == ["PFE"]
    assert result["guard"]["halted"] is False


def test_the_noop_cycle_touches_nothing():
    """§13's economics, in one assertion. If this ever grows a Robinhood step
    the system's cost per day roughly triples."""
    recorded_noop = recorded(SCENARIOS["noop_cycle"])

    assert recorded_noop["plan_fetch"]["fetch"] == []
    assert recorded_noop["plan_fetch"]["estimate"]["calls"] == 0
    assert recorded_noop["decide"]["universe"] is None


def test_the_overnight_exit_places_a_limit_at_the_bid():
    """P1: fractional orders are refused outside regular hours and market
    orders are regular-hours-only, so an exit that has to survive the night
    names its price — and a sale transacts at the bid."""
    result = recorded(SCENARIOS["overnight_exit"])["decide"]
    order = result["plan"]["orders"][0]

    assert order["type"] == "limit"
    assert order["limit_price"] == 13.43


def test_the_drawdown_flattens_the_book_while_refusing_every_buy():
    """The division `guard.py`'s docstring exists for: the breach is an order
    denial and not a halt, precisely so the flatten it demands can be placed.
    A halt here would trap the account in the position it is halting over."""
    result = recorded(SCENARIOS["drawdown_flatten"])["decide"]
    denials = [d["code"] for row in result["guard"]["denied"] for d in row["denials"]]

    assert planned(result, "sell") == ["F"]
    assert planned(result, "buy") == []
    assert "DRAWDOWN_HALT" in denials


def test_the_flatten_is_attributed_to_the_drawdown_and_not_to_a_stop():
    """§6.5 is a priority list. A record that said "time stop" because that
    check ran first would misattribute every position closed in a bad week."""
    result = recorded(SCENARIOS["drawdown_flatten"])["decide"]

    assert result["exits"]["exits"][0]["code"] == plays.DRAWDOWN_FLATTEN


def test_the_blocked_order_is_gone_and_its_sentence_is_not():
    """§14: the exact broker text, never paraphrased and never bucketed."""
    result = recorded(SCENARIOS["blocked_placement"])["decide"]

    assert planned(result) == []
    assert "fractional shares" in result["blocked"][0]["detail"]


# --- day one, against what Appendix A.1 predicted --------------------------


def test_day_one_exits_the_three_names_appendix_a_predicted():
    """A.1: "MU, INTC and CAT sit past a 2xATR stop and exit immediately."
    Recorded here because it is the one falsifiable claim the design makes
    about its own first cycle.
    """
    result = recorded(SCENARIOS["day_one"])["decide"]

    assert {"MU", "INTC", "CAT"} <= set(planned(result, "sell"))
    assert result["manual_required"] == []


def test_day_one_no_longer_exits_c_once_the_stop_widened():
    """C sat 55c through a 132.99 (2xATR) stop — inside the noise of the ATR this
    scenario had to assume, since A.1 recorded none — which is exactly why
    this was pinned separately from the three A.1 named: "so that a later ATR
    revision moves *this* test and not the claim above it." An earlier
    widening to 3xATR is that revision: C's stop moves to ~130.34, a live
    132.44 clears it by $2.10, well outside the noise band, and C now holds
    with the rest of the book rather than exiting alongside A.1's three.
    """
    result = recorded(SCENARIOS["day_one"])["decide"]

    assert sorted(planned(result, "sell")) == ["CAT", "INTC", "MU"]


def test_day_one_holds_the_names_that_are_inside_their_stops():
    """"Near-full liquidation" is A.1's expectation, not a rule. The rule is
    the 2xATR stop, and a name inside it is held however day one felt."""
    result = recorded(SCENARIOS["day_one"])["decide"]

    assert {"ADI", "AMD", "SMH", "MRK"} & set(planned(result, "sell")) == set()


def test_day_one_manages_every_adopted_position_on_identical_terms():
    """Invariant 7. None of these ten is in the S&P snapshot and none was chosen
    by a play, and neither fact exempts any of them from §6.5."""
    result = recorded(SCENARIOS["day_one"])["decide"]
    adopted = [n["symbol"] for n in result["universe"]["notes"]
               if n["code"] == "ADOPTED_NON_MEMBER"]

    assert len(adopted) == 10
    assert all(o["play_id"] == plays.ADOPTED for o in result["plan"]["orders"])


def test_day_one_tops_up_nothing_because_the_slot_is_fractional():
    """Ten positions against a target of three leaves `open_slots` at zero, so no
    *new* name can be opened and none is. MRK is the only held name that would
    otherwise qualify for a top-up (above its 50-day and above A.1's recorded
    130.22 cost), but the $9.79 of deployable cash cannot buy one full share
    of it at 130.35 — so under whole_shares_only it is refused, not bought
    fractional, and no top-up fires this cycle. Nothing else is bought or
    sold either.
    """
    result = recorded(SCENARIOS["day_one"])["decide"]
    bought = planned(result, "buy")

    assert result["sizing"]["ladder"]["open_slots"] == 0
    assert bought == []
    assert [u["symbol"] for u in result["sizing"]["unsized"] if u["reason"] == "WHOLE_SHARE_ONLY"] == ["MRK"]


def test_day_one_exits_place_at_market_because_every_position_is_fractional():
    """§4.3 and P1: the fractional path is market-only and regular-hours-only.
    This is 09:40, which is the only session any of these ten can be sold in —
    and the reason §13's overnight cycles are a paid-for no-op until the
    account holds its first whole share."""
    result = recorded(SCENARIOS["day_one"])["decide"]

    assert all(o["type"] == "market" for o in result["plan"]["orders"])
