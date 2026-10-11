# `tests/fixtures/broker/` — the broker's own shapes, not this core's

Every file here is a **tool response as `get_*` returns it**, under the
broker's own field names and inside the broker's own envelope. Nothing here is
in Appendix D's vocabulary; turning one into the other is
`src/broker_shapes.py`'s whole job, and `tests/test_broker_contract.py` is the
test that feeds these through `normalize → reconcile → decide` end to end.

**Why the directory exists.** Five production failures in three days came from
fixtures that described a world production does not produce — `balances`,
`intent`, the quote field names, the MMM day-context shape and, worst,
`orders.id`, which blocked entries for five cycles and left three exits
unplaceable while the suite stayed green at 1947 passing. A fixture written in
the shape the code already reads cannot catch a rename; only a recorded
response can. **A fixture in this directory may never be edited to make a test
pass.**

## Provenance of each file

| File | Source |
|---|---|
| `get_equity_orders.json` | Three resting GTC stop orders: `state: confirmed`, `placed_agent: agentic`, a `stop_market` coming back as `type: "market"` with `trigger: "stop"`, and the order id under `id`. |
| `get_equity_positions.json` | The same three positions, carrying `shares_available_for_sells: 0` — the broker's record of the shares it has already reserved against those resting sells, which is why all 16 exit attempts in the window were refused "Not enough shares to sell." |
| `get_portfolio.json` | `get_portfolio`'s own shape: `total_value` (not `equity_total`) and `buying_power` nested under `buying_power`. `equity_value` is the field an earlier overnight session picked by mistake, producing the false urgent Drawdown-halt alert; it is kept here precisely so the wrong one is available to be picked. |
| `get_accounts.json` | Both accounts on the login — the `margin` one this system does not trade and the `cash` one it does — because `unsettled_funds` is per account and reading the wrong row is the mistake `cli.settled_cash` exists to prevent. |

## What is recorded and what is reconstructed

**Recorded:** symbols, quantities, sides, order types, `trigger`, `state`,
`time_in_force`, `placed_agent`, the three stop order ids and the field *names*
throughout.

A rejected order never rests, so those three rows described a book the broker
will not produce — it reserves 100% of a position's shares against the first
resting sell and refuses the second. They are removed. This is the fixture rule
working as intended, not an exception to it: the rule forbids editing a fixture
so a test passes, and requires editing one that disagrees with the recorded
response.

**Reconstructed:** cost bases, ATRs, and the exact prices of the orders other
than MGM's — the run logs carry the findings, not the
full response bodies. They are internally consistent — each stop is
`protection.stop_price`'s own answer for that basis and ATR (the fixed 2×ATR
floor for MGM and SPG, the 2.5×ATR trailing floor for INVH, which has traded
above its entry) — and **no assertion in
`test_broker_contract.py` depends on any of them**: the assertions are about
classification, ids and plan contents.

## `dip_monitor/`: the Dip Monitor's market reads

Recorded for BMY and JNJ, two names on that day's Dip Recovery watchlist, and fed
through `src/dip_assemble.py` by `tests/test_dip_assemble.py`. Each file is named
the way the routine names it in its `inputs/` directory.

| File | Source |
|---|---|
| `quotes.json` | `get_equity_quotes`, whole. |
| `tradability.json` | `get_equity_tradability`, whole. |
| `historicals.json` | `get_equity_historicals interval=day bounds=regular`, whole, four sessions. Bars carry `high_price` / `low_price` / `close_price`, not `high` / `low` / `close`. |
| `indicators.json` | Six `get_equity_technical_indicators output=latest` responses (RSI 14, SMA 20, SMA 200 for each name) in one array. The values are the ones an earlier ticks decided on. |
| `fundamentals.json` | `get_equity_fundamentals`. Every field is as returned except `description`, shortened to `(shortened)`; nothing reads it. |
| `fills.json` | The executions for the day, as `get_equity_orders` returned them. |
| `orders.confirmed.json` | `get_equity_orders state=confirmed`: the one resting SPG target. `created_at` and `last_transaction_at` were cut off in the run log this was taken from and are absent. |
| `orders.today.json` | `get_equity_orders created_at_gte=2026-10-05`: empty that day. |

The `guide` text on each response is shortened. `get_portfolio`, `get_accounts`
and `get_earnings_results` are not recorded here; the test builds them inline in
the live envelope.

## `manual_check/`: the real book, for the Manual Check

Recorded from the live agentic account (••0000) and fed through
`src/manual_assemble.py` by `tests/test_manual_check.py`. Unlike the Dip Monitor
set above, these are not a day's market reads but the shapes of the *book*.

| File | Source |
|---|---|
| `positions.json` | `get_equity_positions`: SPG, SPYM, BNY, USB, each with `shares_available_for_sells: 0` (a full-size resting take-profit reserves every share). No `market_value` field: the broker sends none, so the assembler values the book from the quote. |
| `taxlots.USB.json` | `open_date` is a **date, not a timestamp**, which is why the exact time of the newest buy comes from the order executions instead. Lot prices are `cost_per_share`. |
| `orders.confirmed.json` | `get_equity_orders state=confirmed`: the four resting GTC take-profit limit sells, `placed_agent: agentic`, each for the full position. |

The `guide` text on each response is shortened.
