"""S2, factor 12: analyst estimates recorded forward on the decision snapshot.

No source keeps a consensus estimate as it stood on a past date, so the
estimate-revision factor cannot be backtested. The nightly
evidence job records it from now on, from Alpha Vantage: Twelve Data has the
figures and refuses them to this account's plan. What these tests pin is what
that job must not do: ask before the database can store an answer, write a
number the vendor did not send, treat a throttle as an answer or a refusal as
a throttle, spend the day's 25 requests carelessly, or send two updates for
one row.

The vendor payloads are recorded ones: `fixtures/av_earnings_estimates.json`
(Alpha Vantage, on the operator's free key) and `fixtures/td_eps_*.json`
(Twelve Data, through the operator's connector).
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest

from services.core.core import alphavantage, evidence
from services.core.core.alphavantage import AlphaVantage
from services.core.core.twelvedata import TwelveData
from services.core.tests.test_evidence import (
    DAY, WRITE, Bars, Supabase, after, row, run, sessions, through,
)
from src import forward

FIXTURES = Path(__file__).parent / "fixtures"
TREND = json.loads((FIXTURES / "td_eps_trend.json").read_text(encoding="utf-8"))
REVISIONS = json.loads((FIXTURES / "td_eps_revisions.json").read_text(encoding="utf-8"))
AV = json.loads((FIXTURES / "av_earnings_estimates.json").read_text(encoding="utf-8"))
MU = AV["ok_MU"]
THROTTLED = AV["second_request_inside_one_second"]
UNKNOWN = AV["unknown_symbol_ZZZZQ"]
SCHEMA = Path(__file__).resolve().parents[3] / "db" / "schema.sql"

NIGHT = after(DAY)                     # the job's first run after the decision
FETCHED = "2026-10-05"                 # the day the Alpha Vantage fixture was recorded


def body(payload) -> tuple[int, bytes]:
    return 200, json.dumps(payload).encode()


class Fetcher:
    """Answers by symbol (Alpha Vantage) or by endpoint and symbol (Twelve
    Data); records every URL."""

    def __init__(self, answers=None, default=None):
        self.answers = answers or {}
        self.default = default
        self.urls: list[str] = []

    def get(self, url: str) -> tuple[int, bytes]:
        self.urls.append(url)
        symbol = url.split("symbol=")[1].split("&")[0]
        if "alphavantage" in url:
            answer = self.answers.get(symbol, self.default)
            return answer if answer is not None else body(MU)
        endpoint = url.split("/")[3].split("?")[0]
        answer = self.answers.get((endpoint, symbol), self.answers.get(endpoint))
        if answer is not None:
            return answer
        return body(TREND if endpoint == "eps_trend" else REVISIONS)

    def asked(self) -> list[str]:
        return [u.split("symbol=")[1].split("&")[0] for u in self.urls]


def av(fetcher, slept=None, **kw) -> AlphaVantage:
    return AlphaVantage("KEY", fetcher, sleep=(slept if slept is not None else []).append,
                        clock=kw.pop("clock", lambda: 0.0), **kw)


def td(fetcher, slept=None) -> TwelveData:
    return TwelveData("KEY", fetcher, sleep=(slept if slept is not None else []).append,
                      min_interval=61.0)


def judged(symbol="MU", **kw) -> dict:
    """A pending row as `edge_decision_snapshot_pending` sends it."""
    verdict = kw.pop("verdict", "SKIPPED")
    return {**row(symbol, **kw), "verdict": verdict, "has_estimates": False}


SETTLED = dict(has_features=True, status={"1": "ok"})   # nothing due for bars
QUIET = after(sessions(2)[-1])                          # a night with no bars due


def backfill(pending, fetcher, today=QUIET, bars=None):
    supabase = Supabase(pending)
    result = evidence.run_backfill(supabase, bars, estimator=av(fetcher), now=f"{today}T06:05:00Z")
    return supabase, result


# --- the two registered readings, from Alpha Vantage's rows -----------------------


def test_the_readings_are_taken_on_the_fiscal_year_after_the_one_in_progress():
    """The recorded payload lists fiscal years ending 2027-08-31 and
    2028-08-31. The first is in progress; the registered period is the next."""
    got = forward.estimate_features_alphavantage(MU["estimates"], FETCHED)
    year = next(r for r in MU["estimates"] if r["date"] == "2028-08-31" and r["horizon"] == "fiscal year")

    assert got["fiscal_year_end"] == "2028-08-31"
    assert got["eps_revision_90_7"] == pytest.approx(
        float(year["eps_estimate_average_7_days_ago"]) / float(year["eps_estimate_average_90_days_ago"]) - 1.0,
        abs=1e-6)
    up = float(year["eps_estimate_revision_up_trailing_30_days"])
    down = float(year["eps_estimate_revision_down_trailing_30_days"])
    assert got["revision_breadth_1m"] == pytest.approx((up - down) / (up + down), abs=1e-6)


def test_only_fiscal_years_that_end_after_the_fetch_count_and_quarters_never_do():
    ahead = forward.fiscal_years_ahead(MU["estimates"], FETCHED)

    assert [r["date"] for r in ahead] == ["2027-08-31", "2028-08-31"]
    # A year later the first of those has ended: one year ahead is not enough.
    later = forward.estimate_features_alphavantage(MU["estimates"], "2027-10-05")
    assert later == {"eps_revision_90_7": None, "revision_breadth_1m": None, "fiscal_year_end": None}


def test_a_reading_the_vendor_did_not_support_is_absent_not_zero():
    def year(end, **fields):
        return {"date": end, "horizon": "fiscal year", **fields}

    rows = [year("2027-12-31"), year(
        "2028-12-31",
        eps_estimate_average_7_days_ago="-0.40", eps_estimate_average_90_days_ago="-0.80",
        eps_estimate_revision_up_trailing_30_days="2.0000",
        eps_estimate_revision_down_trailing_30_days=None)]

    got = forward.estimate_features_alphavantage(rows, FETCHED)

    # A loss shrinking is not a percentage to rank; a null count is unknown.
    assert got["eps_revision_90_7"] is None and got["revision_breadth_1m"] is None
    assert got["fiscal_year_end"] == "2028-12-31"
    assert forward.estimate_features_alphavantage(None, FETCHED)["fiscal_year_end"] is None


def test_the_twelve_data_readings_come_from_its_next_fiscal_year_row():
    got = forward.estimate_features(TREND["eps_trend"], REVISIONS["eps_revision"])

    assert got["eps_revision_90_7"] == pytest.approx(9.58154 / 9.68258 - 1.0, abs=1e-6)
    assert got["revision_breadth_1m"] == pytest.approx(1 / 9, abs=1e-6)
    quiet = [{"period": "next_year", "up_last_month": 0, "down_last_month": 0}]
    assert forward.estimate_features(None, quiet) == {
        "eps_revision_90_7": None, "revision_breadth_1m": None}


# --- the Alpha Vantage client -------------------------------------------------------


def test_one_request_a_name_and_the_rows_come_back_as_the_vendor_sent_them():
    fetcher = Fetcher()
    client = av(fetcher)

    rows, failed = client.estimates(["MU"])

    assert failed == {} and rows["MU"] == MU["estimates"]
    (url,) = fetcher.urls
    assert url.startswith("https://www.alphavantage.co/query?")
    assert "function=EARNINGS_ESTIMATES" in url and "symbol=MU" in url and "apikey=KEY" in url
    assert client.requests == 1


def test_requests_are_spaced_so_the_vendor_never_sees_two_in_a_second():
    slept: list[float] = []
    client = av(Fetcher(), slept)

    client.estimates(["AAA", "BBB", "CCC"])

    assert slept == [1.5, 1.5]             # between requests, not before the first


def test_the_recorded_throttle_message_stops_the_walk_and_is_not_an_answer():
    """What the vendor actually said to a second request inside a second. It
    is the same message when the day's 25 are gone."""
    fetcher = Fetcher({"BBB": body(THROTTLED)})

    rows, failed = av(fetcher).estimates(["AAA", "BBB", "CCC"])

    assert list(rows) == ["AAA"]
    assert failed == {"BBB": {"kind": alphavantage.LATER, "message": THROTTLED["Information"][:300]}}
    assert fetcher.asked() == ["AAA", "BBB"]           # CCC was never asked


def test_the_recorded_empty_answer_for_an_unknown_symbol_is_that_names_answer():
    assert UNKNOWN == {}
    fetcher = Fetcher({"ZZZZQ": body(UNKNOWN)})

    rows, failed = av(fetcher).estimates(["ZZZZQ", "MU"])

    assert failed["ZZZZQ"]["kind"] == alphavantage.SYMBOL
    assert list(rows) == ["MU"]            # the walk went on


@pytest.mark.parametrize("payload", [
    {"Information": "Thank you for using Alpha Vantage! This is a premium endpoint. "
                    "You may subscribe to any of the premium plans to instantly unlock it."},
    {"Error Message": "the parameter apikey is invalid or missing."},
])
def test_a_refusal_of_the_key_or_the_plan_is_learned_from_one_request(payload):
    fetcher = Fetcher(default=body(payload))

    rows, failed = av(fetcher).estimates(["AAA", "BBB"])

    assert rows == {} and failed["AAA"]["kind"] == alphavantage.REFUSED
    assert len(fetcher.urls) == 1


@pytest.mark.parametrize("answer", [(503, b""), (200, b"<html>not json"), (200, b"[]")])
def test_a_response_that_is_not_an_answer_is_later_and_stops_the_walk(answer):
    fetcher = Fetcher(default=answer)

    rows, failed = av(fetcher).estimates(["AAA", "BBB"])

    assert rows == {} and failed["AAA"]["kind"] == alphavantage.LATER
    assert len(fetcher.urls) == 1


def test_a_fetch_that_throws_is_later_and_its_message_does_not_carry_the_key():
    class Broken:
        def get(self, url):
            raise TimeoutError("timed out")

    client = AlphaVantage("SECRET", Broken(), sleep=lambda _s: None)

    rows, failed = client.estimates(["AAA"])

    assert failed["AAA"]["kind"] == alphavantage.LATER
    assert all("SECRET" not in e for e in client.errors)


def test_a_pass_asks_about_at_most_its_ceiling_of_names():
    fetcher = Fetcher()

    rows, _ = av(fetcher).estimates([f"S{i:02d}" for i in range(30)])

    assert len(fetcher.urls) == alphavantage.MAX_REQUESTS_PER_PASS == 20
    assert alphavantage.MAX_REQUESTS_PER_PASS < 25      # under the day's limit, with room
    assert len(rows) == 20


def test_a_slow_vendor_runs_out_of_its_time_budget_not_the_functions():
    ticks = iter(range(0, 10_000, 25))                   # every look at the clock is 25 s later
    fetcher = Fetcher()
    client = av(fetcher, clock=lambda: float(next(ticks)))

    client.estimates([f"S{i:02d}" for i in range(20)])

    assert 1 <= len(fetcher.urls) <= 3                   # 60 s of budget, not 20 requests


def test_no_key_on_the_deployment_is_an_error_at_construction_only(monkeypatch):
    monkeypatch.delenv(alphavantage.ENV_KEY, raising=False)
    with pytest.raises(alphavantage.AlphaVantageError):
        AlphaVantage.from_env(Fetcher())
    monkeypatch.setenv(alphavantage.ENV_KEY, "abc")
    assert AlphaVantage.from_env(Fetcher()).estimates([]) == ({}, {})


# --- which rows are asked about --------------------------------------------------


def test_a_judged_row_gets_its_estimate_on_the_same_update_as_its_bars():
    bars = Bars({"MU": through(1), "SPY": through(1), "XLK": through(1)})
    fetcher = Fetcher()

    supabase, result = backfill([judged()], fetcher, today=NIGHT, bars=bars)

    (update,) = supabase.written()                     # one update for the row, not two
    assert update["price_features"]["bars_used"] > 0
    estimate = update["estimates"]
    assert estimate["status"] == "ok" and estimate["source"] == "alphavantage"
    assert estimate["fetched_on"] == NIGHT
    # Only fiscal-year rows are stored, not the vendor's 42.
    assert {r["horizon"] for r in estimate["estimates"]} == {"fiscal year"}
    assert result["estimates_fetched"] == ["MU"] and result["estimate_requests"] == 1
    assert result["estimate_source"] == "alphavantage"
    assert result["credits"] == 3                      # Twelve Data: MU, SPY, XLK. No more.


def test_the_stored_readings_are_the_ones_the_recorded_payload_supports():
    supabase, _ = backfill([judged(day=FETCHED, has_features=True)], Fetcher(), today=FETCHED)

    (update,) = supabase.written()
    assert update["estimates"]["fiscal_year_end"] == "2028-08-31"
    assert update["estimates"]["eps_revision_90_7"] == \
        forward.estimate_features_alphavantage(MU["estimates"], FETCHED)["eps_revision_90_7"]
    assert [r["date"] for r in update["estimates"]["estimates"]] == ["2027-08-31", "2028-08-31"]


def test_a_database_without_the_migration_is_never_asked_for_an_estimate():
    """A pending row from an older database carries neither key. An absent
    `has_estimates` is "nowhere to put one", not "has none"."""
    fetcher = Fetcher()

    supabase, result = backfill([row(**SETTLED), {**row("NVDA", **SETTLED), "verdict": "SKIPPED"}], fetcher)

    assert fetcher.urls == [] and result["estimates_wanted"] == 0
    assert WRITE not in [name for name, _ in supabase.calls]


def test_a_name_discovery_dropped_and_a_row_that_has_one_are_not_asked_about():
    pending = [judged("AAA", verdict="DROPPED", **SETTLED),
               {**judged("BBB", **SETTLED), "has_estimates": True},
               judged("CCC", verdict="RANKED", **SETTLED),
               judged("DDD", verdict="UNTRIGGERED", **SETTLED)]
    fetcher = Fetcher()

    backfill(pending, fetcher)

    assert fetcher.asked() == ["CCC", "DDD"]


def test_a_decision_older_than_the_window_is_left_without_one():
    """Past five days "7 days before the fetch" is no longer before the
    decision, so the registered reading would not be the one recorded."""
    old = sessions(1, "2026-07-20")[0]
    pending = [judged("OLD", day=old, has_features=True,
                      status={"1": "ok", "3": "ok", "5": "ok", "8": "ok"}),
               judged("NEW", **SETTLED)]
    fetcher = Fetcher()

    backfill(pending, fetcher, today=after(DAY, 4))

    assert fetcher.asked() == ["NEW"]


def test_the_oldest_decision_is_asked_about_first_because_it_ages_out_first():
    newer = sessions(1)[0]
    pending = [judged(f"S{i:02d}", day=newer, **SETTLED) for i in range(25)] + [judged("OLD", **SETTLED)]
    fetcher = Fetcher()

    _, result = backfill(pending, fetcher)

    asked = fetcher.asked()
    assert asked[0] == "OLD" and len(asked) == 20
    assert result["estimates_wanted"] == 26 and len(result["estimates_fetched"]) == 20


def test_one_fetch_puts_the_estimate_on_every_row_of_the_symbol():
    pending = [judged(play_id="DIP", **SETTLED), judged(play_id="CATALYST", **SETTLED),
               judged(cycle_id="c-2", play_id="DIP", **SETTLED)]
    fetcher = Fetcher()

    supabase, _ = backfill(pending, fetcher)

    written = supabase.written()
    assert len(fetcher.urls) == 1 and len(written) == 3
    assert len({(u["cycle_id"], u["symbol"], u["play_id"]) for u in written}) == 3
    # Nothing else was due: each update carries the key and the estimate alone.
    assert all(set(u) == {"cycle_id", "symbol", "play_id", "estimates"} for u in written)


# --- the bars and the estimates do not depend on each other -------------------------


def test_a_night_full_of_bars_still_gets_its_estimates():
    """They are different vendors with different limits. The first design
    shared Twelve Data's minute between them; this one does not have to."""
    pending = [judged(f"S{i:02d}") for i in range(40)]
    bars = Bars({})
    fetcher = Fetcher()

    backfill(pending, fetcher, today=NIGHT, bars=bars)

    assert len(bars.asked) > 24 and len(fetcher.urls) == 20


def test_no_estimate_key_changes_nothing_about_the_bars_and_asks_for_nothing():
    supabase = Supabase([judged()])
    bars = Bars({"MU": through(1), "SPY": through(1), "XLK": through(1)})

    result = run(supabase, bars, NIGHT)                 # no estimator at all

    (update,) = supabase.written()
    assert "estimates" not in update and update["price_features"]["bars_used"] > 0
    assert result["estimate_source"] is None and result["estimate_requests"] == 0
    assert result["estimates_wanted"] == 1 and result["estimates_fetched"] == []


def test_no_bars_key_still_fetches_estimates():
    supabase, result = backfill([judged(**SETTLED)], Fetcher(), bars=None)

    assert result["status"] == "ok" and result["estimates_fetched"] == ["MU"]
    assert supabase.written()[0]["estimates"]["status"] == "ok"


# --- throttled is "later"; anything else is the answer ------------------------------


def test_the_days_limit_writes_what_was_fetched_and_leaves_the_rest_to_be_asked_again():
    pending = [judged(s, **SETTLED) for s in ("AAA", "BBB", "CCC")]
    fetcher = Fetcher({"BBB": body(THROTTLED)})

    supabase, result = backfill(pending, fetcher)

    assert [u["symbol"] for u in supabase.written()] == ["AAA"]
    assert result["estimates_fetched"] == ["AAA"] and result["estimates_unavailable"] == []
    assert result["status"] == "ok" and any("BBB" in e for e in result["errors"])


def test_a_name_the_vendor_has_nothing_for_is_written_as_unavailable_with_the_reason():
    fetcher = Fetcher({"ZZZZQ": body(UNKNOWN)})

    supabase, result = backfill([judged("ZZZZQ", **SETTLED), judged("MU", **SETTLED)], fetcher)

    by_symbol = {u["symbol"]: u["estimates"] for u in supabase.written()}
    assert by_symbol["ZZZZQ"]["status"] == "unavailable"
    assert by_symbol["ZZZZQ"]["kind"] == alphavantage.SYMBOL and by_symbol["ZZZZQ"]["reason"]
    assert "eps_revision_90_7" not in by_symbol["ZZZZQ"]        # nothing imputed
    assert by_symbol["MU"]["status"] == "ok"
    assert result["estimates_unavailable"] == ["ZZZZQ"]


def test_a_refused_key_is_the_answer_for_every_name_the_pass_asked_about():
    refusal = {"Error Message": "the parameter apikey is invalid or missing."}
    fetcher = Fetcher(default=body(refusal))
    pending = [judged(s, **SETTLED) for s in ("AAA", "BBB", "CCC")]

    supabase, result = backfill(pending, fetcher)

    assert len(fetcher.urls) == 1
    written = supabase.written()
    assert sorted(u["symbol"] for u in written) == ["AAA", "BBB", "CCC"]
    assert all(u["estimates"]["status"] == "unavailable"
               and u["estimates"]["kind"] == alphavantage.REFUSED for u in written)
    assert result["estimates_unavailable"] == ["AAA", "BBB", "CCC"]


def test_an_outage_writes_nothing_so_the_row_is_asked_again():
    supabase, result = backfill([judged(**SETTLED)], Fetcher(default=(503, b"")))

    assert WRITE not in [name for name, _ in supabase.calls]
    assert result["estimates_unavailable"] == [] and result["status"] == "ok"


# --- the Twelve Data estimate call (kept; refused on this account's plan) -----------

# The hosted core's own answer, status and body as they came.
PLAN_REFUSAL = (403, b'{"code":403,"message":"/eps_trend is available exclusively with ultra or '
                     b'enterprise plans. Consider upgrading your API Key now at '
                     b'https://twelvedata.com/pricing","status":"error"}')


def test_twelve_data_one_symbol_a_request_under_the_endpoint_asked():
    fetcher = Fetcher()
    client = td(fetcher)

    rows, failed = client.analyst(["AAPL"])

    assert failed == {}
    assert rows["AAPL"]["eps_trend"] == TREND["eps_trend"]
    assert rows["AAPL"]["eps_revisions"] == REVISIONS["eps_revision"]
    assert all("symbol=AAPL&" in u and "interval=" not in u for u in fetcher.urls)
    assert client.budget.used == 2


def test_twelve_data_requests_are_paced_by_credits_four_symbols_to_the_minute():
    slept: list[float] = []
    fetcher = Fetcher()

    td(fetcher, slept).analyst([f"S{i}" for i in range(9)])

    assert len(fetcher.urls) == 18 and slept == [61.0, 61.0]


def test_twelve_data_the_plan_refusal_is_learned_from_one_request():
    fetcher = Fetcher({"eps_trend": PLAN_REFUSAL})

    rows, failed = td(fetcher).analyst(["AAA", "BBB", "CCC"])

    assert rows == {} and list(failed) == ["AAA"]
    assert failed["AAA"]["code"] == 403 and "ultra or enterprise" in failed["AAA"]["message"]
    assert len(fetcher.urls) == 1


def test_twelve_data_a_rate_limit_stops_the_walk_and_an_uncovered_name_does_not():
    limited = body({"code": 429, "status": "error", "message": "out of credits"})
    missing = body({"code": 404, "status": "error", "message": "symbol not found"})

    rows, failed = td(Fetcher({("eps_revisions", "AAA"): limited})).analyst(["AAA", "BBB"])
    assert rows == {} and failed["AAA"]["code"] == 429

    rows, failed = td(Fetcher({("eps_trend", "AAA"): missing})).analyst(["AAA", "BBB"])
    assert failed["AAA"]["code"] == 404 and list(rows) == ["BBB"]


# --- the schema ---------------------------------------------------------------------


def body_of(sql: str, function: str) -> str:
    start = sql.index(f"create or replace function {function}")
    return sql[start : sql.index("$$;", start)]


EVIDENCE_FUNCTIONS = (
    "edge_decision_snapshot_pending",
    "edge_decision_forward_write",
    "edge_decision_snapshot_read",
)


def test_the_schema_defines_the_three_functions_the_job_and_the_evaluator_call():
    sql = SCHEMA.read_text(encoding="utf-8")

    table = sql[sql.index("create table if not exists edge_decision_snapshot"):]
    assert re.search(r"^\s+estimates jsonb,?$", table[: table.index(");")], re.M)
    for signature in (
        r"edge_decision_snapshot_pending\(p_limit integer default 4000\)",
        r"edge_decision_forward_write\(p_rows jsonb\)",
        r"edge_decision_snapshot_read\(p_from date, p_to date\)",
    ):
        assert re.search(rf"create or replace function {signature}", sql), signature
    assert len(re.findall(r"^begin;$", sql, re.M)) == 1
    assert len(re.findall(r"^commit;$", sql, re.M)) == 1


def test_the_pending_rows_carry_what_the_job_selects_on():
    pending = body_of(SCHEMA.read_text(encoding="utf-8"), "edge_decision_snapshot_pending")

    assert re.search(r"\bverdict\b", pending)
    assert "(estimates is not null) as has_estimates" in pending
    assert "(price_features is not null) as has_features" in pending


def test_an_estimate_is_written_once_and_an_estimate_only_row_moves_nothing_else():
    write = body_of(SCHEMA.read_text(encoding="utf-8"), "edge_decision_forward_write")

    # Stored value first: a later pass cannot replace "as of the decision".
    assert "estimates      = coalesce(s.estimates, nullif(r->'estimates', 'null'::jsonb))" in write
    # Every other column keeps what is stored when its key is absent.
    for column in ("fwd", "fwd_status"):
        assert re.search(rf"{column}\s*= coalesce\(nullif\(r->'{column}', 'null'::jsonb\), s\.{column}\)", write)
    assert "coalesce((r->>'complete')::boolean, s.complete)" in write


def test_the_cycles_own_write_cannot_erase_an_estimate():
    cycle_write = body_of(SCHEMA.read_text(encoding="utf-8"), "edge_decision_snapshot_write")
    assert "estimates" not in cycle_write


def test_the_evaluator_is_handed_the_estimates_and_no_public_role_can_call_any_of_it():
    sql = SCHEMA.read_text(encoding="utf-8")

    assert "'estimates', estimates" in body_of(sql, "edge_decision_snapshot_read")
    for name in EVIDENCE_FUNCTIONS:
        assert re.search(
            rf"revoke execute on function {name}\([^)]*\)\s+from anon, authenticated, public", sql), name
        assert re.search(rf"grant  execute on function {name}\([^)]*\)\s+to service_role", sql), name
