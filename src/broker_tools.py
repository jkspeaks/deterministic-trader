"""The MCP tool names a routine prompt is allowed to call, by source.

**Why this file exists.** `edge_cycle_schedule.fetch_plan` was hand-seeded
with two rows naming `edge_runlog` and `edge_play_state` as "tools". Neither
is a tool; neither is even an RPC name (the reads are `edge_prior` and
`edge_play_state_json`). The runner prompt says "run every row of
`should.fetch` with the `tool` it names", so it dutifully called two functions
that do not exist, failed twice, and ended the cycle silently.

A producer that generates the plan from `cli.plan_fetch` cannot make that
particular mistake, because `_fetch_plan` names real tools. This file is the
second fence: `services/core/core/schedule.py` refuses to store a fetch row
whose `tool` is not in here, so a typo, a renamed broker tool or a hand-edited
row is caught at publish time — at 04:30 in a server job that can alarm —
rather than at 18:47 in a session that cannot.

**It is an allowlist, not a schema.** Nothing here knows what a tool returns
or what arguments it takes; the fetch row carries those and `cli._step` is
what shapes them. The only question this module answers is "is this a name a
prompt may say out loud", and the only correct way to extend it is to add a
name the MCP connector actually exposes.

**Placement tools are listed and are deliberately not fetchable.** A fetch
plan is a read list. `place_equity_order` appearing in one would be a schedule
that places orders, which is not a thing any slot may be, so `FETCH_TOOLS` and
`PLACEMENT_TOOLS` are separate sets and `is_fetch_tool` only consults the
first.

**Bare names, not the `mcp__…__` prefixes.** `cli._step` writes `tool:
"get_equity_quotes"` and the prompts say `get_equity_quotes`; the connector
prefix is a property of one client's tool namespace, not of the broker API,
and putting it here would make this list wrong for every other client.
"""

from __future__ import annotations

ROBINHOOD = "robinhood"
TWELVEDATA = "twelvedata"

# Read-only Robinhood tools. Every one of these is named by `cli._fetch_plan`
# today except `get_equity_historicals`, which belongs to the separate
# earnings-reaction routine and is listed so that routine's plan validates too.
ROBINHOOD_FETCH = frozenset(
    {
        "get_accounts",
        "get_portfolio",
        "get_equity_positions",
        "get_equity_orders",
        "get_equity_tax_lots",
        "get_pnl_trade_history",
        "get_equity_quotes",
        "get_equity_historicals",
        "get_equity_technical_indicators",
        "get_equity_tradability",
        "get_equity_fundamentals",
        "get_earnings_results",
        "get_earnings_calendar",
        # Appendix C's DIP scanner. A read that returns tickers, which the
        # core then judges — the runner prompt is explicit that the survivors
        # are "candidates for the core to judge, not names you have chosen".
        "run_scan",
        "get_scanner_filter_specs",
    }
)

# The Twelve Data MCP surface the SMA exception uses (SMA only, 20/50/200;
# `rsi`/`atr` never move). No fetch row names one of these
# today — `_fetch_plan` emits Robinhood for every indicator and the Twelve
# Data substitution is made inside the prompt's Step 6 — so this set exists so
# that the substitution can be *published* rather than improvised the day it
# moves into the plan.
TWELVEDATA_FETCH = frozenset(
    {
        "get_technical_indicator",
        "get_time_series",
        "get_price",
        "get_quote",
        "get_market_state",
        "get_api_usage",
    }
)

FETCH_TOOLS = {
    ROBINHOOD: ROBINHOOD_FETCH,
    TWELVEDATA: TWELVEDATA_FETCH,
}

# Named, and never returned by `is_fetch_tool`. See the module docstring.
PLACEMENT_TOOLS = frozenset(
    {
        "review_equity_order",
        "place_equity_order",
        "cancel_equity_order",
    }
)

# The two sources whose rows a runner executes with a tool. `supabase` and
# `checkout` rows are read by the core itself (`run._payload` and
# `run.CHECKOUT_DATA`) and are stripped from a published plan — the runner has
# no git clone and, no Supabase reads of its own either.
FETCH_SOURCES = frozenset(FETCH_TOOLS)


def is_fetch_tool(source: str, tool: str) -> bool:
    """Whether ``tool`` is a read this project's prompts may run on ``source``.

    False for an unknown source, so a row that arrives with `source:
    "supabase"` is refused by name rather than by having slipped through a
    membership test against a set that does not exist.
    """
    return tool in FETCH_TOOLS.get(source, frozenset())
