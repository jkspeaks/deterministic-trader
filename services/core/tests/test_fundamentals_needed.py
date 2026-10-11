"""Fundamentals only where a consumer reads them, and the core writes
`day_context`.

The 09:40 full cycle posted `get_equity_fundamentals` for all 149 resolved
symbols (259 KB) when only the names CATALYST's event check fires on read it.
`resolve` now names them (`fundamentals_needed`); the runner fetches only those.

Held under test:

* `fundamentals_needed` is `plays.catalyst.triggered` over the resolved set —
  held names included (a held name with a fresh 8-K can re-trigger CATALYST),
  DIP-scan names excluded unless the pullback reshape is on;
* the decide **plan is identical** whether the runner posted fundamentals for
  every symbol or for `fundamentals_needed` only — the premise of the whole
  narrowing, asserted on a cycle where CATALYST actually places;
* the core writes `day_context` through `edge_day_context_write_symbol` with the
  exact shape the runner prompt used to compose: no `signals`; `earnings` and
  the top-level `fetched_at` only for `stale` names whose earnings were posted;
  `fundamentals_static` with exactly three keys and its own `fetched_at`, only
  for needed names — never a raw payload;
* a fundamentals row outside the list is a `warn`, counted into the status
  alert, never an error.
"""

from __future__ import annotations

import copy

import pytest

from services.core.core import run as core
from services.core.core import steps
from services.core.tests.conftest import FakeSupabase, rows_from
from src import cli, plays, report
from src.plays import catalyst
from tests.factories import cli_payload

NOW = "2026-08-03T13:40:00Z"
TRADING_DAY = "2026-08-03"


# These tests predate an earlier retirement and shelving of the event plays
# and use them as they then ran. See `all_plays_live` in conftest.py.
pytestmark = pytest.mark.usefixtures("all_plays_live")


def catalyst_payload() -> dict:
    """`cli_payload` plus XOM: an 8-K filed Friday (one session back), up 5% on
    3x average volume, cheap enough for one whole share — CATALYST places it.
    Every symbol gets a fundamentals row, the way the runner posted them before
    this prompt, and the rows for names CATALYST never reads are deliberately
    'confirming' (10x volume) so a narrowing that changed anything would show."""
    p = cli_payload()
    p["events"] = {
        "XOM": [{"type": "8-K", "items": ["8.01"], "sessions_ago": 1, "filed_day": "2026-07-31"}],
    }
    p["quotes"]["XOM"] = {"bid": 20.99, "ask": 21.01, "last": 21.00, "adjusted_previous_close": 20.00}
    p["signals"]["XOM"] = {"rsi": 58.0, "sma_20": 20.10, "sma_50": 19.80, "sma_200": 18.00, "atr": 0.40}
    p["tradability"]["XOM"] = dict(p["tradability"]["PFE"])
    xom_earnings = copy.deepcopy(p["earnings"]["PFE"])
    for quarter in xom_earnings["data"]["results"]:
        quarter["symbol"] = "XOM"
    p["earnings"]["XOM"] = xom_earnings
    p["fundamentals"] = {
        "XOM": _fund("XOM", volume="30000000.000000", average="10000000.000000"),
        "F": _fund("F", volume="500000000.000000", average="50000000.000000"),
        "PFE": _fund("PFE", volume="400000000.000000", average="40000000.000000"),
    }
    return p


def _fund(symbol: str, *, volume: str, average: str) -> dict:
    # The trimmed row the runner posts under runner-prompt.md's named exception.
    return {
        "symbol": symbol,
        "volume": volume,
        "average_volume": average,
        "sector": "Energy",
        "market_cap": "400000000000.000000",
    }


def run_phase(phase: str, p: dict, supabase: FakeSupabase | None = None):
    supabase = supabase or FakeSupabase(tables={"edge_cycle_input": rows_from(p, p["cycle_id"])})
    return core.run(p["cycle_id"], phase, supabase), supabase


def loaded(**config):
    base = cli.load_config()
    return {**base, "config": {**base["config"], **config}}


# -- the derivation -------------------------------------------------------------


def test_triggered_is_catalysts_own_event_leg():
    ctx = {"events": catalyst_payload()["events"], "cycle": {"trading_day": TRADING_DAY},
           "calendar": cli.load_config()["calendar"]}

    assert catalyst.triggered("XOM", ctx)
    assert not catalyst.triggered("PFE", ctx)
    # Same answer the play gives: no trigger, no proposal and no skip at all.
    assert catalyst._catalyst("PFE", ctx) is None


def test_needed_is_the_catalyst_names_only():
    p = catalyst_payload()

    needed = core._fundamentals_needed(p, loaded(), TRADING_DAY, ["F", "PFE", "XOM"])

    assert needed == ["XOM"]


def test_a_held_name_with_a_fresh_event_is_needed():
    """Every candidate is offered to every play, held ones included — a held
    name with an 8-K can re-trigger CATALYST, and that reads fundamentals."""
    p = catalyst_payload()
    p["events"]["F"] = [{"type": "8-K", "items": ["8.01"], "sessions_ago": 0, "filed_day": TRADING_DAY}]

    assert core._fundamentals_needed(p, loaded(), TRADING_DAY, ["F", "PFE", "XOM"]) == ["F", "XOM"]


def test_an_old_or_thin_event_is_not_needed():
    p = catalyst_payload()
    p["events"]["PFE"] = [
        {"type": "8-K", "items": ["8.01"], "sessions_ago": 5, "filed_day": "2026-07-27"},
        {"type": "news", "headline_count": 2, "sessions_ago": 0, "filed_day": TRADING_DAY},
    ]

    assert core._fundamentals_needed(p, loaded(), TRADING_DAY, ["F", "PFE", "XOM"]) == ["XOM"]


def test_the_pullback_reshape_needs_every_symbol():
    """`plays/dip.py` `_adv_usd` reads `average_volume` for any candidate while
    `play_dip_pullback_enabled` is on — unnarrowed, as before this prompt."""
    p = catalyst_payload()

    needed = core._fundamentals_needed(p, loaded(play_dip_pullback_enabled=True), TRADING_DAY, ["F", "PFE", "XOM"])

    assert needed == ["F", "PFE", "XOM"]


def test_resolve_returns_fundamentals_needed_and_stamps_the_count():
    result, supabase = run_phase(core.RESOLVE, catalyst_payload())

    assert result["status"] == core.STATUS_RESOLVED
    assert result["fundamentals_needed"] == ["XOM"]
    assert set(result["fundamentals_needed"]) <= set(result["symbols"])
    stored = supabase.first("edge_cycle_output_write")["p_fields"]
    assert stored["fundamentals_needed"] == ["XOM"]
    end = [d for _, s, e, d, _ in supabase.stamps if s == steps.S2A_RESOLVE and e == steps.END]
    assert end and end[0]["fundamentals_needed"] == 1


# -- the plan does not move -----------------------------------------------------


def _plan(result: dict) -> dict:
    return {key: result[key] for key in ("plan", "protection", "cancels")}


def test_the_plan_is_identical_with_fundamentals_for_needed_names_only():
    everything = catalyst_payload()
    resolved, _ = run_phase(core.RESOLVE, everything)
    narrowed = catalyst_payload()
    narrowed["fundamentals"] = {
        s: row for s, row in narrowed["fundamentals"].items() if s in resolved["fundamentals_needed"]
    }

    full, _ = run_phase(core.DECIDE, everything)
    thin, _ = run_phase(core.DECIDE, narrowed)

    assert full["status"] == thin["status"] == core.STATUS_DECIDED
    # Not vacuous: CATALYST really places XOM off the fundamentals it kept.
    assert ("XOM", plays.CATALYST) in [(o["symbol"], o.get("play_id")) for o in full["plan"] if o["side"] == "buy"]
    assert _plan(full) == _plan(thin)


def test_without_its_fundamentals_a_catalyst_name_is_no_entry_never_an_error():
    """Fail-closed: a name the runner never fetched fundamentals for is a
    multiple of 0.0 — skipped, not a crash, and exits are untouched."""
    p = catalyst_payload()
    p["fundamentals"] = {}

    result, _ = run_phase(core.DECIDE, p)

    assert result["status"] == core.STATUS_DECIDED
    assert "XOM" not in [o["symbol"] for o in result["plan"]]


# -- the core writes day_context ------------------------------------------------


def day_context_writes(supabase: FakeSupabase) -> dict[str, dict]:
    return {a["p_symbol"]: a["p_symbol_data"] for a in supabase.args("edge_day_context_write_symbol")}


def test_decide_writes_day_context_in_the_runner_prompts_old_shape():
    p = catalyst_payload()

    _, supabase = run_phase(core.DECIDE, p)
    writes = day_context_writes(supabase)

    # No cache at all in this double, so every symbol is `stale`: each posted
    # earnings block is written with the cycle's own `now`...
    for symbol in ("F", "PFE", "XOM"):
        assert writes[symbol]["earnings"] == p["earnings"][symbol]
        assert writes[symbol]["fetched_at"] == NOW
        assert "signals" not in writes[symbol]
    # ...and `fundamentals_static` only for the needed name, three keys exactly.
    assert writes["XOM"]["fundamentals_static"] == {
        "average_volume": "10000000.000000",
        "sector": "Energy",
        "market_cap": "400000000000.000000",
        "fetched_at": NOW,
    }
    assert "fundamentals_static" not in writes["F"] and "fundamentals_static" not in writes["PFE"]
    assert all(a["p_trading_day"] == TRADING_DAY for a in supabase.args("edge_day_context_write_symbol"))


def test_a_raw_fundamentals_payload_is_never_cached_whole():
    """The runner-composed write-back stored the whole ~40-field response. The
    core keeps three keys whatever the runner posts."""
    p = catalyst_payload()
    p["fundamentals"]["XOM"].update({"description": "x" * 2000, "ceo": "someone", "pe_ratio": "12.0"})

    _, supabase = run_phase(core.DECIDE, p)

    statics = day_context_writes(supabase)["XOM"]["fundamentals_static"]
    assert set(statics) == {"average_volume", "sector", "market_cap", "fetched_at"}


def test_writes_follow_the_freshness_rules():
    posted = {
        "earnings": {"A": {"data": {}}, "B": {"data": {}}},
        "fundamentals": {"A": {"average_volume": "1", "sector": "S", "market_cap": "2"},
                         "C": {"average_volume": None, "sector": None, "market_cap": None}},
    }

    writes = core._day_context_writes(posted, stale=["A"], needed=["A", "C"], now=NOW)

    # B's earnings were posted but B is not stale: no stamp that would mark it fresh.
    # C's statics are all null: an empty block would read back as fresh.
    assert writes == {
        "A": {
            "earnings": {"data": {}},
            "fetched_at": NOW,
            "fundamentals_static": {"average_volume": "1", "sector": "S", "market_cap": "2", "fetched_at": NOW},
        }
    }


def test_a_cached_value_is_never_written_back_as_if_fetched():
    """`_fill_from_day_context` puts cached earnings into the payload for fresh
    names; only what the runner *posted* may be written."""
    p = catalyst_payload()
    posted = core._posted(p)
    del p["earnings"]["F"]

    assert "F" not in core._posted(p)["earnings"] and "F" in posted["earnings"]


def test_a_warm_cache_still_stamps_the_day_context_step():
    """Nothing to write must not read as a missing step to the finalize gate."""
    p = catalyst_payload()
    p["earnings"], p["fundamentals"] = {}, {}

    _, supabase = run_phase(core.DECIDE, p)

    assert supabase.args("edge_day_context_write_symbol") == []
    assert (steps.S2A_DAY_CONTEXT, steps.START) in supabase.stamped()
    assert (steps.S2A_DAY_CONTEXT, steps.END) in supabase.stamped()


def test_a_failing_cache_write_never_costs_the_plan():
    class Refuses(FakeSupabase):
        def rpc(self, function, params=None):
            if function == "edge_day_context_write_symbol":
                raise RuntimeError("boom")
            return super().rpc(function, params)

    p = catalyst_payload()
    clean, _ = run_phase(core.DECIDE, catalyst_payload())
    result, supabase = run_phase(
        core.DECIDE, p, Refuses(tables={"edge_cycle_input": rows_from(p, p["cycle_id"])})
    )

    assert result["status"] == core.STATUS_DECIDED
    assert _plan(result) == _plan(clean)
    warns = [d for _, s, e, d, _ in supabase.stamps if s == steps.S2A_DAY_CONTEXT and e == steps.WARN]
    assert warns and set(warns[0]["write_failed"]) == {"F", "PFE", "XOM"}


def test_the_day_context_write_comes_after_the_decide_runlog_row():
    """Invariant 3: nothing precedes the decide row; the cache is not a
    trading write and waits until the plan is recorded."""
    _, supabase = run_phase(core.DECIDE, catalyst_payload())
    names = supabase.names()

    decide_row = next(i for i, (n, a) in enumerate(supabase.calls)
                      if n == "edge_runlog_write" and a["p_command"] == "decide")
    assert names.index("edge_day_context_write_symbol") > decide_row


def test_an_exit_only_cycle_writes_no_day_context():
    p = catalyst_payload()
    p["now"] = "2026-08-04T01:00:00Z"  # 21:00 ET, overnight: exits only

    result, supabase = run_phase(core.DECIDE, p)

    assert result["status"] in (core.STATUS_DECIDED, core.STATUS_NOOP)
    assert supabase.args("edge_day_context_write_symbol") == []


# -- the backstop: warn, count, never refuse ------------------------------------


def test_fundamentals_outside_the_list_are_a_warning_not_an_error():
    p = catalyst_payload()  # F and PFE fundamentals are outside `needed`

    result, supabase = run_phase(core.DECIDE, p)

    assert result["status"] == core.STATUS_DECIDED
    warns = [d for _, s, e, d, _ in supabase.stamps if s == steps.S2A_POST_FUNDAMENTALS and e == steps.WARN]
    assert warns == [{"outside_need": True, "count": 2, "needed": 1, "symbols": ["F", "PFE"]}]
    row = next(r for r in supabase.args("edge_runlog_write") if r["p_command"] == "decide")
    assert row["p_stdout"]["runner_fetch"] == {
        "fundamentals_needed": 1,
        "fundamentals_posted": 3,
        "fundamentals_outside_need": ["F", "PFE"],
    }


def test_no_warning_when_the_runner_kept_to_the_list():
    p = catalyst_payload()
    p["fundamentals"] = {"XOM": p["fundamentals"]["XOM"]}

    _, supabase = run_phase(core.DECIDE, p)

    assert not [s for s in supabase.stamps if s[1] == steps.S2A_POST_FUNDAMENTALS and s[2] == steps.WARN]


def test_fetch_warnings_counts_off_the_step_log():
    log = [
        {"step": steps.S2A_POST_FUNDAMENTALS, "event": "warn", "detail": {"outside_need": True, "count": 7}},
        {"step": steps.S2A_POST_QUOTES, "event": "warn", "detail": {"per_row_write": True}},
        {"step": steps.S2A_POST_QUOTES, "event": "warn", "detail": {"per_row_write": True}},
        {"step": steps.S2A_POST_EARNINGS, "event": "warn", "detail": {"oversize_batch": True}},
        {"step": steps.S2A_POST_QUOTES, "event": "beat", "detail": {"per_row_write": True}},
    ]

    counts = steps.fetch_warnings(FakeSupabase(tables={"edge_cycle_step": log}), "c")

    assert counts == {"outside_need": 7, "per_row_writes": 2, "oversize_batches": 1}
    assert steps.fetch_warnings(FakeSupabase(tables={"edge_cycle_step": []}), "c") == {}


def test_the_status_alert_carries_the_counts():
    ctx = {"cycle": {"cycle_type": "monitor"}, "runner_fetch": {"outside_need": 2, "per_row_writes": 3}}

    fragment = report._runner_fetch_fragment(ctx)

    assert fragment == (
        "Runner fetch: 2 fundamentals row(s) outside resolve's fundamentals_needed; "
        "3 single-row Step 2a write(s) instead of one batch."
    )
    assert report._runner_fetch_fragment({"runner_fetch": {}}) is None


@pytest.mark.parametrize("phase", [core.RESOLVE, core.DECIDE])
def test_an_older_runner_that_posted_everything_still_decides(phase):
    """Backward compatible: the prompt change and the core change can land in
    either order."""
    result, _ = run_phase(phase, catalyst_payload())

    assert result["status"] in (core.STATUS_RESOLVED, core.STATUS_DECIDED)
