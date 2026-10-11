"""§8.3's weekly scorecard — the one thing this system says when nothing is wrong.

Every other notification in §8.3 is a fault. The scorecard is the only row that
fires on a good week, which makes its timing load-bearing: a scorecard that
skipped a week would be indistinguishable from a scorecard that had nothing to
report, and the operator would learn nothing from silence ever again.
"""

from src import report
from tests.factories import report_context

FRIDAY = {"trading_day": "2026-08-07", "now": "2026-08-07T20:30:00Z"}
THURSDAY = {"trading_day": "2026-08-06", "now": "2026-08-06T20:30:00Z"}


def scorecard_body(ctx) -> str:
    """The scorecard push's body."""
    result = report.run(ctx)
    return next(a.body for a in result.alerts if a.title == report.WEEKLY_SCORECARD)


def test_the_week_ends_on_friday():
    assert report.run(report_context(cycle=FRIDAY)).scorecard is not None


def test_a_monday_post_close_says_nothing():
    """The baseline day. Four benchmark rows are written before one scorecard
    is, and §8.3 notifies operationally — a daily performance push is the
    "Daily P&L" column the table explicitly refuses."""
    assert report.run(report_context()).scorecard is None


def test_a_shortened_week_ends_on_its_last_open_session():
    """Good Friday, and the week still gets its scorecard. Keying on the
    weekday rather than the calendar would drop it silently on exactly the
    weeks that were unusual enough to be worth reading about."""
    ctx = report_context(
        cycle=THURSDAY, calendar={"closed": ["2026-08-07"]}
    )

    assert report.run(ctx).scorecard is not None


def test_the_thursday_before_an_open_friday_says_nothing():
    """The other half of the same rule. Without it every Thursday would ship a
    scorecard and Friday would ship a second one."""
    assert report.run(report_context(cycle=THURSDAY)).scorecard is None


def test_the_week_gets_one_scorecard_however_many_post_closes_it_has():
    """On a half-day §13's 13:40 through 16:30 firings are all `postclose`.
    The benchmark row absorbs that by upserting on the trading day; an alert
    cannot, because `edge_alerts` is an append-only queue with a serial id, so
    four firings would be four pushes."""
    ctx = report_context(
        cycle=FRIDAY, control={"last_scorecard_date": "2026-08-07"}
    )

    assert report.run(ctx).scorecard is None


def test_firing_records_the_week_it_fired_for():
    """The dedup above only works if the cycle that sent one says so."""
    result = report.run(report_context(cycle=FRIDAY))

    assert result.control_updates == {"last_scorecard_date": "2026-08-07"}


def test_a_week_that_was_missed_is_not_replayed():
    """A stamp from the previous week is not this week's, so the scorecard
    fires. Carrying a "weeks owed" counter instead would push two scorecards
    after an outage, and the older one would be measuring against prices
    nobody can still act on."""
    ctx = report_context(
        cycle=FRIDAY, control={"last_scorecard_date": "2026-07-31"}
    )

    assert report.run(ctx).scorecard is not None


# --- §1's three bars, and the one that is not a price ----------------------


def friday(**overrides) -> dict:
    return report_context(cycle={**FRIDAY, **overrides.pop("cycle", {})}, **overrides)


def test_a_flat_week_reads_flat_against_the_market():
    """Equity and SPY both exactly where inception left them."""
    card = report.run(friday()).scorecard

    assert card.agent_return == 0.0
    assert card.spy_return == 0.0


def test_the_agents_return_is_net_of_deposits():
    """§1 asks whether the *agent* beat the bar. $100 wired in raises equity by
    $100, and an agent credited with it would show +40% for doing nothing."""
    ctx = friday(account={"equity_total": 347.72})
    ctx["inception"] = {
        **ctx["inception"],
        "deposits": [{"date": "2026-08-05", "amount": 100.00}],
    }

    assert report.run(ctx).scorecard.agent_return == 0.0


def test_the_market_bar_is_spy_against_its_inception_close():
    ctx = friday(quotes={"SPY": {"last": 848.463}})

    assert report.run(ctx).scorecard.spy_return == 0.1


def test_the_frozen_bar_is_the_untouched_book_against_inception_equity():
    """On inception-day prices the frozen book must read exactly 0.00%. The
    baseline is now derived from the same closes the book is priced at, so the
    two agree by construction.
    """
    assert report.run(friday()).scorecard.frozen_return == 0.0


def test_the_operators_own_bar_is_pro_rated_and_compounded():
    """§1's third benchmark is 24%/year — the operator's 2025 result, and the
    one that actually matters. Three days into the experiment the bar is
    0.177%, not 24%, and not 24/52."""
    assert report.run(friday()).scorecard.target_return == 0.001768


def test_a_year_of_it_is_the_bar_itself():
    """The pro-rating has to land back on 24% or it is measuring something
    else. The scorecard only fires on a week's last session, so the first
    anniversary it can land on is 367 days out — 24.1%, a hair over, and over
    for the right reason."""
    ctx = friday(cycle={"trading_day": "2027-08-06"})

    assert report.run(ctx).scorecard.target_return == 0.241279


def test_an_unpriceable_frozen_book_leaves_the_other_bars_standing():
    """§1 has three benchmarks so that losing one is a gap, not an outage."""
    ctx = friday()
    ctx["quotes"] = {"SPY": {"last": 771.33}}

    card = report.run(ctx).scorecard

    assert card.frozen_return is None
    assert card.agent_return == 0.0
    assert card.target_return == 0.001768


def test_the_push_says_so_when_the_week_included_a_deposit():
    """The number that makes +4% mean two different things. An operator
    reading a scorecard has to be able to tell a good week from a funded one."""
    ctx = friday(account={"equity_total": 347.72})
    ctx["inception"] = {
        **ctx["inception"],
        "deposits": [{"date": "2026-08-05", "amount": 100.00}],
    }

    body = scorecard_body(ctx)

    assert "incl. $100.00 deposited" in body
    assert "+0.00%" in body


def test_the_push_does_not_mention_deposits_when_there_were_none():
    """Every week for the foreseeable future. A line reading "incl. $0.00
    deposited" trains the operator to skip the line that matters on the one
    week it is not zero."""
    assert "deposited" not in scorecard_body(friday())


# --- the part that names the play ------------------------------------------


def test_the_scorecard_attributes_the_week_to_named_plays():
    """§1: "a bad month is attributable to a named play rather than to the
    agent's mood." An account-level number alone cannot tell the operator
    which play to switch off."""
    lines = {line.play_id: line for line in report.run(friday()).scorecard.plays}

    assert lines["DIP"].closed_trades == 4
    assert lines["DIP"].realized_pnl == -1.25
    assert lines["DIP"].regime_fit == 1.0


def test_a_play_that_has_never_traded_still_appears():
    """A play that is live and has done nothing is a fact about the week. Only
    listing plays with closed trades would make a play that stopped firing
    look like a play that was switched off."""
    lines = {line.play_id: line for line in report.run(friday()).scorecard.plays}

    assert lines["MACRO"].closed_trades == 0
    assert lines["MACRO"].realized_pnl == 0.0


def test_a_darkened_play_is_reported_with_the_agents_reason():
    """§6.4's regime flip. `regime_fit` 0.0 is the agent switching a play off,
    and the reason it gave is the only record of why."""
    ctx = friday(play_state={"DIP": {"regime_fit": 0.0, "reason": "dips keep going"}})
    lines = {line.play_id: line for line in report.run(ctx).scorecard.plays}

    assert lines["DIP"].regime_fit == 0.0
    assert lines["DIP"].reason == "dips keep going"


# --- near-flat and data-quality annotations ------------------


def test_a_play_with_near_flat_closes_says_how_many():
    ctx = friday(play_state={"DIP": {"regime_fit": 1.0, "closed_trades": 4,
                                     "realized_pnl": -14.39, "near_flat_trades": 1}})

    assert "  DIP: 4 closed, -$14.39 (1 near-flat)" in scorecard_body(ctx)


def test_a_play_with_none_carries_no_suffix():
    """Named only when there is one — a "(0 near-flat)" on every line every
    week would teach the operator to skip the line."""
    assert "near-flat" not in scorecard_body(friday())


def test_a_close_off_a_disputed_entry_is_named_under_the_plays():
    """The money stays in DIP's total; the line says not to read DIP by it."""
    ctx = friday(realized_trades_week=[
        {"symbol": "MGM", "trading_day": "2026-09-17", "realized_gain": 0.08,
         "play_id": "DIP", "near_flat": True, "entry_disputed": True},
        {"symbol": "INVH", "trading_day": "2026-09-16", "realized_gain": -6.76,
         "play_id": "DIP", "near_flat": False, "entry_disputed": False},
    ])

    body = scorecard_body(ctx)

    assert ("  Data-quality: MGM 09-17 +$0.08, disputed entry basis, excluded "
            "from strategy conclusions") in body
    assert "INVH" not in body


def test_an_ordinary_week_has_no_data_quality_line():
    assert "Data-quality" not in scorecard_body(friday(realized_trades_week=[
        {"symbol": "INVH", "trading_day": "2026-09-16", "realized_gain": -6.76,
         "entry_disputed": False},
    ]))


# --- fill rate and slippage ------------


def test_a_week_with_no_fill_quality_data_reads_n_a():
    """`fill_quality_week` absent — the ordinary case until the telemetry
    migration is applied. No orders placed is not a 0% fill rate."""
    card = report.run(friday()).scorecard

    assert card.fill_rate is None
    assert card.median_slippage is None
    body = scorecard_body(friday())
    assert "fill    n/a" in body
    assert "slip    n/a" in body


def test_the_fill_rate_is_filled_orders_over_placed_orders():
    """Three of four placed orders never filled — an earlier case. A partial fill
    still counts as filled: the number the operator wants is how often an
    order did nothing at all."""
    ctx = friday(fill_quality_week=[
        {"symbol": "DAL", "side": "buy", "filled_qty": 0.0, "slippage": None},
        {"symbol": "HST", "side": "buy", "filled_qty": 0.0, "slippage": None},
        {"symbol": "KEY", "side": "buy", "filled_qty": 0.0, "slippage": None},
        {"symbol": "F", "side": "buy", "filled_qty": 1.0, "slippage": 0.002},
    ])
    card = report.run(ctx).scorecard

    assert card.fill_rate == 0.25
    assert "fill    25%" in scorecard_body(ctx)


def test_the_median_slippage_ignores_rows_that_never_filled():
    """A row with `slippage: null` (nothing filled, or no limit) is not a
    zero-slippage trade — it is not a trade."""
    ctx = friday(fill_quality_week=[
        {"symbol": "A", "side": "buy", "filled_qty": 1.0, "slippage": 0.01},
        {"symbol": "B", "side": "buy", "filled_qty": 1.0, "slippage": 0.03},
        {"symbol": "C", "side": "sell", "filled_qty": 1.0, "slippage": -0.01},
        {"symbol": "D", "side": "buy", "filled_qty": 0.0, "slippage": None},
    ])
    card = report.run(ctx).scorecard

    assert card.median_slippage == 0.01
    assert "slip    +1.00%" in scorecard_body(ctx)


def test_a_week_that_placed_orders_none_of_which_filled_reads_zero_not_n_a():
    ctx = friday(fill_quality_week=[
        {"symbol": "A", "side": "buy", "filled_qty": 0.0, "slippage": None},
        {"symbol": "B", "side": "buy", "filled_qty": 0.0, "slippage": None},
    ])
    card = report.run(ctx).scorecard

    assert card.fill_rate == 0.0
    assert card.median_slippage is None
    body = scorecard_body(ctx)
    assert "fill    0%" in body
    assert "slip    n/a" in body
