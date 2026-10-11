"""Each play recorded firing and silent, plus the cycles that combine them (§15).

"Plays are golden-file tested independently. Each play gets a fixture where it
fires and one where it does not, so a change to one play cannot silently alter
another."

That independence is the design of this file, not a remark about it. Every
scenario starts from the same deliberately quiet day and turns on exactly one
play's trigger, so moving `DIP`'s RSI threshold can only ever change the two
`dip_*` files. A single shared "busy day" fixture would couple all four and the
guarantee §15 asks for would be untestable.

`busy_day` exists in addition, not instead: it is the only place §6.0's
arbitration across four live plays is recorded whole.
"""

import json
from pathlib import Path

import pytest

from src import plays
from tests.factories import (
    ETFS,
    config,
    plays_context,
    position,
    proposals,
    regime,
    regime_inputs,
)

GOLDEN = Path(__file__).parent / "golden" / "plays"


# These tests predate an earlier retirement and shelving of the event plays
# and use them as they then ran. See `all_plays_live` in conftest.py.
pytestmark = pytest.mark.usefixtures("all_plays_live")


def quiet_day(**overrides) -> dict:
    """A day on which no play has anything to say about F.

    RSI 55 is unremarkable, the moving averages are stacked below price, the
    last report is long past and the next is sixty sessions out, nothing has
    been filed and the macro tape is empty.
    """
    ctx = plays_context(**overrides)
    ctx["signals"]["F"] = {
        "rsi": 55.0,
        "sma_20": 14.00,
        "sma_50": 14.00,
        "sma_200": 13.50,
        "atr": 0.45,
    }
    ctx["quotes"]["F"]["change_pct"] = 0.005
    ctx["fundamentals"]["F"] = {"volume": 55_000_000, "average_volume": 55_000_000}
    return ctx


def dip_fires() -> dict:
    ctx = quiet_day()
    ctx["signals"]["F"].update({"rsi": 27.0, "sma_20": 16.00})
    return ctx


def pead_fires() -> dict:
    ctx = quiet_day()
    ctx["earnings"]["F"] = {
        "status": "known",
        "sessions_until_report": 60,
        "sessions_since_report": 2,
        "eps_surprise_pct": 7.0,
        "reaction_pct": 0.03,
    }
    return ctx


def catalyst_fires() -> dict:
    ctx = quiet_day()
    ctx["quotes"]["F"]["change_pct"] = 0.05
    ctx["fundamentals"]["F"]["volume"] = 90_000_000
    ctx["events"]["F"] = [{"type": "8-K", "items": ["2.02", "9.01"], "sessions_ago": 1}]
    return ctx


def macro_fires() -> dict:
    ctx = quiet_day()
    ctx["macro"]["releases"] = [
        {"series": "CPI", "surprise_pct": 0.20, "sessions_ago": 0}
    ]
    return ctx


def busy_day() -> dict:
    """All four live at once, on four different names.

    Four plays competing for three slots is §6.0's stated arithmetic at this
    account size, so this is the shape the arbitration was designed against
    rather than a stress case.
    """
    ctx = quiet_day(candidates=["F", "PFE", "BAC"])
    ctx["signals"]["F"].update({"rsi": 27.0, "sma_20": 16.00})
    ctx["signals"]["PFE"] = {"rsi": 55.0, "sma_50": 24.00, "sma_200": 22.00, "atr": 0.60}
    ctx["signals"]["BAC"] = {
        "rsi": 55.0,
        "sma_50": 60.00,
        "sma_200": 58.00,
        "atr": 1.20,
    }
    ctx["quotes"]["PFE"] = {"bid": 24.97, "ask": 25.03, "last": 25.00}
    ctx["quotes"]["BAC"] = {"bid": 61.91, "ask": 61.97, "last": 61.94, "change_pct": 0.06}
    ctx["fundamentals"]["BAC"] = {"volume": 40_000_000, "average_volume": 20_000_000}
    ctx["earnings"]["PFE"] = {
        "status": "known",
        "sessions_until_report": 60,
        "sessions_since_report": 1,
        "eps_surprise_pct": 5.0,
        "reaction_pct": 0.04,
    }
    ctx["earnings"]["BAC"] = {"status": "known", "sessions_until_report": 45}
    ctx["events"]["BAC"] = [{"type": "8-K", "items": ["8.01"], "sessions_ago": 0}]
    ctx["macro"]["releases"] = [
        {"series": "NFP", "surprise_pct": 0.30, "sessions_ago": 1}
    ]
    ctx["sectors"].update({"PFE": "Health Care", "BAC": "Financials"})
    return ctx


def crowded_and_dark() -> dict:
    """What §6.0 does with a dark play and a crowded sector.

    This scenario used to record three refusals: PEAD disabled by the agent,
    MACRO refused for having no `edge_play_state` row, and DIP's shallow dip
    scored down to zero by a 0.20 crowding penalty. Two of those three are
    gone, and the file is kept under the same name precisely so the diff
    shows it:

    * **PEAD is still dark** — an explicit `regime_fit: 0.0` is the operator's
      switch and it is the one thing that column still does.
    * **MACRO has no row and proposes anyway.** The missing-row refusal was
      the defect, not the safeguard (`plays.core._regime_multiplier`).
    * **F survives its crowding.** 1.5% below the 20-day mean is 0.15 of
      depth-conviction and RSI 27 is 0.20 of extremity-conviction, and `max`
      takes the higher — so it scores 0.20 against a 0.20 penalty that used to
      erase it exactly and now only sorts it.
    """
    ctx = busy_day()
    ctx["signals"]["F"]["sma_20"] = 15.10
    ctx["play_state"]["PEAD"] = {"regime_fit": 0.0, "reason": "drift has stopped paying"}
    del ctx["play_state"]["MACRO"]
    ctx["positions"] = [
        position(symbol="GM", play_id="DIP"),
        position(symbol="TSLA", play_id="DIP"),
    ]
    ctx["sectors"].update({"GM": "Consumer Discretionary", "TSLA": "Consumer Discretionary"})
    return ctx


def risk_off_tape() -> dict:
    """§4.3 F firing: SPY under its 200-day in a volatile tape.

    The busy day's four live triggers, and not one of them proposed. This is
    the state that has no equivalent in the old system at all — `regime_fit`
    could dark a play the agent had thought about, but nothing could stand the
    whole book down on the tape's own numbers.
    """
    ctx = busy_day()
    ctx["regime"] = regime(spy={"last": 580.0, "rv20": 0.42})
    return ctx


def peaked_sector() -> dict:
    """A tradeable tape in which one sector is not.

    Recorded because the sector filter runs in `cli.candidates`, upstream of
    this module — so `plays` itself is deliberately *unaffected* by a peaked
    sector, and this file is where that stays true. Only the SURGE gate and
    the risk-off dark reach here.
    """
    ctx = busy_day()
    ctx["regime"] = regime(sectors={"XLK": {"rsi14": 74.0}})
    return ctx


def dip_pullback_fires() -> dict:
    """§4.3 A, switched on, on the quiet day's own F.

    Paired with `dip_pullback_quiet` below so the reshape gets the same
    fires/silent treatment §15 asks for every play. Its numbers are the quiet
    day's plus RSI 33 — over §6.2's 30 trigger, under A's 35 ceiling — and a
    50-day just above the last, which is the shallow give-back A buys. That
    combination is the point of the file: the shipped rule proposes nothing
    here and A proposes a candidate, on identical inputs.
    """
    ctx = quiet_day()
    ctx["config"] = config(play_dip_pullback_enabled=True)
    ctx["etfs"] = ETFS
    ctx["regime_inputs"] = regime_inputs()
    ctx["signals"]["F"].update({"rsi": 33.0, "sma_50": 15.20})
    return ctx


def dip_pullback_quiet() -> dict:
    """A switched on, and F over its 50-day. Nothing to buy back."""
    ctx = dip_pullback_fires()
    ctx["signals"]["F"]["sma_50"] = 14.00
    return ctx


def sector_fires() -> dict:
    """§4.3 C, switched on, with XLE oversold and holding its 200-day.

    The quiet day's ETFs all read RSI 55 in `regime_inputs`, so exactly one
    number moves — which is this file's whole discipline.
    """
    ctx = quiet_day()
    ctx["config"] = config(play_sector_enabled=True)
    ctx["etfs"] = ETFS
    ctx["regime_inputs"] = regime_inputs()
    ctx["regime_inputs"]["sectors"]["XLE"] = {
        "last": 100.0, "sma200": 95.0, "rsi14": 24.0,
    }
    ctx["sectors"]["XLE"] = "Energy"
    return ctx


def sector_quiet() -> dict:
    """C switched on with nothing oversold: eleven ETFs, no rows at all."""
    ctx = sector_fires()
    ctx["regime_inputs"]["sectors"]["XLE"] = {
        "last": 100.0, "sma200": 95.0, "rsi14": 55.0,
    }
    return ctx


ENTRY_SCENARIOS = {
    "dip_fires": dip_fires,
    "dip_quiet": quiet_day,
    "pead_fires": pead_fires,
    "pead_quiet": quiet_day,
    "catalyst_fires": catalyst_fires,
    "catalyst_quiet": quiet_day,
    "macro_fires": macro_fires,
    "macro_quiet": quiet_day,
    "busy_day": busy_day,
    "crowded_and_dark": crowded_and_dark,
    "risk_off_tape": risk_off_tape,
    "peaked_sector": peaked_sector,
    # Both fixtures set their own switch and nothing else, so a regression that
    # turned either on by accident would show up as a *different* file changing
    # rather than as these two.
    "dip_pullback_fires": dip_pullback_fires,
    "dip_pullback_quiet": dip_pullback_quiet,
    "sector_fires": sector_fires,
    "sector_quiet": sector_quiet,
}


def leaving_book() -> dict:
    """Four positions, four different §6.5 reasons, one cycle.

    Every priority tier above the play's own terms is represented, which is
    what makes this the file that notices a reordering.
    """
    ctx = quiet_day(candidates=[])
    ctx["positions"] = [
        position(symbol="F", play_id="DIP", sessions_held=8),
        position(symbol="PFE", play_id="PEAD", entry_price=25.00, sessions_held=3),
        position(symbol="BAC", play_id="CATALYST", entry_price=70.00, sessions_held=2),
        position(symbol="XLE", play_id="MACRO", entry_price=90.00, sessions_held=4),
    ]
    ctx["signals"].update(
        {
            "PFE": {"rsi": 55.0, "atr": 0.60},
            "BAC": {"rsi": 55.0, "atr": 1.20},
            "XLE": {"rsi": 55.0, "atr": 1.50},
        }
    )
    ctx["quotes"].update(
        {
            "PFE": {"bid": 24.97, "ask": 25.03, "last": 25.00},
            "BAC": {"bid": 61.91, "ask": 61.97, "last": 61.94},
            "XLE": {"bid": 89.95, "ask": 90.05, "last": 90.00},
        }
    )
    ctx["earnings"].update(
        {
            "PFE": {"status": "known", "sessions_until_report": 1},
            "BAC": {"status": "known", "sessions_until_report": 30},
            "XLE": {"status": "unknown"},
        }
    )
    ctx["events"]["XLE"] = []
    ctx["play_state"]["MACRO"] = {"regime_fit": 0.0, "reason": "payrolls broke the trend"}
    return ctx


def overnight_book() -> dict:
    """The same book on a 20:00 cycle, where every exit must name its price."""
    ctx = leaving_book()
    ctx["cycle"] = {
        "cycle_type": "overnight",
        "session": "all_day_hours",
        "now": "2026-08-04T00:30:00Z",
        "entries_allowed": False,
    }
    return ctx


EXIT_SCENARIOS = {
    "leaving_book": leaving_book,
    "overnight_book": overnight_book,
}


@pytest.mark.parametrize("name", sorted(ENTRY_SCENARIOS))
def test_the_entry_cycle_matches_its_golden_file(name):
    expected = json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))

    assert plays.entries(ENTRY_SCENARIOS[name]()).as_dict() == expected


@pytest.mark.parametrize("name", sorted(EXIT_SCENARIOS))
def test_the_exit_cycle_matches_its_golden_file(name):
    expected = json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))

    assert plays.exits(EXIT_SCENARIOS[name]()).as_dict() == expected


# --- the properties the golden files are there to protect ------------------


@pytest.mark.parametrize("name", ["dip", "pead", "catalyst", "macro"])
def test_the_quiet_day_is_genuinely_quiet_for_every_play(name):
    """The paired fixture is only meaningful if it is silent for *all* four.
    A quiet day that happened to trigger DIP would make three of the eight
    files record DIP instead of the play they are named for."""
    result = plays.entries(quiet_day())

    assert result.candidates == []
    assert result.skipped == []


@pytest.mark.parametrize(
    "scenario,play_id",
    [
        ("dip_fires", plays.DIP),
        ("pead_fires", plays.PEAD),
        ("catalyst_fires", plays.CATALYST),
    ],
)
def test_each_single_play_fixture_fires_only_its_own_play(scenario, play_id):
    result = plays.entries(ENTRY_SCENARIOS[scenario]())

    assert [c.play_id for c in result.candidates] == [play_id]


def test_the_two_prompt_52_plays_are_silent_on_the_shipped_config():
    """The acceptance criterion, as an assertion rather than as a diff.

    Each new fixture differs from the quiet day by its own switch plus the one
    number its trigger reads. Take the switch away and both go silent — which
    is what "with both plays disabled every existing golden is unchanged" means
    in behaviour rather than in bytes.
    """
    for scenario in (dip_pullback_fires, sector_fires):
        ctx = scenario()
        ctx["config"] = config()
        result = plays.entries(ctx)

        assert result.candidates == [] and result.skipped == []


def test_the_sector_fixture_fires_only_sector():
    result = plays.entries(sector_fires())

    assert proposals(result, plays.SECTOR) == ["XLE"]
    assert {c.play_id for c in result.candidates} == {plays.SECTOR}


def test_the_macro_fixture_fires_only_macro():
    # MACRO is the one play whose candidates are ETFs rather than F.
    result = plays.entries(macro_fires())

    assert proposals(result, plays.MACRO) == ["XLE", "XLF"]
    assert {c.play_id for c in result.candidates} == {plays.MACRO}


def test_a_busy_day_puts_all_four_plays_on_the_board():
    result = plays.entries(busy_day())

    assert {c.play_id for c in result.candidates} == {
        plays.DIP,
        plays.PEAD,
        plays.CATALYST,
        plays.MACRO,
    }


def test_the_busy_day_ranks_by_the_six_point_zero_score():
    result = plays.entries(busy_day())

    assert [c.conviction for c in result.candidates] == sorted(
        (c.conviction for c in result.candidates), reverse=True
    )


def test_a_dark_play_is_the_only_thing_arbitration_turns_away_now():
    """Two of this fixture's three refusals were retired. What is left is the
    operator's own switch, which is the one that was never broken."""
    result = plays.entries(crowded_and_dark())

    assert sorted({s.code for s in result.skipped}) == [plays.REGIME_DARK]
    assert {s.play_id for s in result.skipped} == {plays.PEAD}


def test_the_crowded_name_survives_the_penalty_that_used_to_erase_it():
    """F scores 0.20 against a 0.20 crowding penalty — the exact case the old
    `conviction × regime − crowding` arithmetic zeroed out."""
    cand = next(c for c in plays.entries(crowded_and_dark()).candidates if c.symbol == "F")

    assert cand.conviction == 0.20
    assert cand.crowding_penalty == 0.20


def test_a_missing_play_state_row_no_longer_costs_a_play_its_entry():
    """MACRO has no row in this fixture and still proposes."""
    proposed = plays.entries(crowded_and_dark()).candidates

    assert plays.MACRO in {c.play_id for c in proposed}


def test_a_risk_off_tape_stands_the_whole_book_down():
    """§4.3 F. Four live triggers, nothing proposed, every skip naming the
    tape rather than a play."""
    result = plays.entries(risk_off_tape())

    assert result.candidates == []
    assert {s.code for s in result.skipped} == {plays.REGIME_DARK}
    assert all("realized vol" in s.message for s in result.skipped)


def test_a_peaked_sector_is_not_this_modules_problem():
    """The sector filter is `cli.candidates`' job, upstream and before the
    fetch. A peaked XLK reaching `plays` unchanged is the boundary holding."""
    assert plays.entries(peaked_sector()).as_dict() == plays.entries(busy_day()).as_dict()


def test_the_leaving_book_exits_four_positions_for_four_reasons():
    result = plays.exits(leaving_book())

    assert [(e.symbol, e.code) for e in result.exits] == [
        ("PFE", plays.EARNINGS_DEADLINE),
        ("BAC", plays.ATR_STOP),
        ("F", plays.TIME_STOP),
        ("XLE", plays.SIGNAL_EXIT),
    ]


def test_the_overnight_book_prices_every_exit_at_the_bid():
    result = plays.exits(overnight_book())

    assert all(e.order_type == "limit" for e in result.exits)
    assert [e.limit_price for e in result.exits] == [24.97, 61.91, 14.85, 89.95]


def test_the_overnight_book_leaves_for_the_same_reasons_as_the_day_book():
    """The session changes how an exit is placed and never whether it happens.
    §6.5's first line is that exits run on every cycle."""
    day = plays.exits(leaving_book()).exits
    night = plays.exits(overnight_book()).exits

    assert [(e.symbol, e.code) for e in day] == [(e.symbol, e.code) for e in night]
