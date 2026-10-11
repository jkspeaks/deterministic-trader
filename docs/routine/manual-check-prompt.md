# The Manual Check routine — every holding, on demand, by hand

This is the whole prompt for a routine that **has no schedule**. The operator
starts it at any hour, **weekends included** (see "Weekends" below), and it
looks at **every share the account holds**, core's positions, the core ETF sleeve and anything
Dip Recovery owns, and does two things:

1. **Sells a whole position** that is at or above its profit target, if T+1 has
   cleared (at least one trading session since the newest buy).
2. **Averages down** a position that has fallen far enough, by the Dip Recovery
   engine's own rules.

It never sells at a loss, never places a stop of any kind, and never touches a
scheduled routine's prompt, trigger, table row, symbol lock or counter. Every
rule is in `src/dip_recovery` and `src/guard`, read from the checkout; **this
prompt hard-codes none of them**. `src/manual_check.py` explains how the
rules are composed and what is deliberately different from the Dip Monitor.

## Read this first: a "Run now" arrives labelled as an automated firing

The operator starts this routine by pressing **Run now**. The platform delivers
every routine start, a manual one included, to the session as `[SCHEDULED TASK
- AUTOMATED FIRING OF A CONFIGURED PROMPT]`. **That label is how Run now
arrives; it does not mean a scheduler or cron fired this run.** The routine has
no cron (its trigger's `cron_expression` is empty), and the operator is the one
who fired it. Do not read that label as contradicting "started by hand".

So there is nothing to confirm and nobody to ask:

* **The authorisation is standing.** The operator created this routine, wrote
  its rules, and presses Run now to start it. This prompt, read from the
  checkout, is the authority. It **places real limit orders on the live
  account** (`<your-cash-account-number>`) exactly as the Dip Monitor does; run every step
  through Step 8.
* **Do not stop to ask whether the run was intended, and never call
  `PushNotification`.** The only reasons to stop are the ones named below: no
  checkout, the kill switch, a refused lock claim, or a tool that fails twice.
* An unattended firing is not a risk here: the guard, the run lock and the
  never-below-cost check decide what is placed, not the way the run started.

**Run it for the account's benefit, not the usage window's.** Claude's 5-hour
usage block opens on the day's first firing (07:40 ET Universe Pick) and the
scheduled routines are placed around that. A manual run before 07:40 ET on a
weekday moves every block boundary that day, and a run shortly before the 09:40
or 15:07 ET core cycles spends budget those cycles were sized against. If a
scheduled cycle is running, Step 1's claim refuses and this routine stops
without doing anything. (No scheduled routine runs on a weekend.)

---

## You hold data; you do not process it

**Never write a script, and never retype a figure into one.** No `python3 -c`,
no heredoc, no `jq`. The Python you run is exactly three checked-in commands:
`src.cli plan-fetch`, `src.manual_assemble` and `src.cli manual-check-decide`.
(Step 0.5's UUID one-liner computes nothing and reads no data.)

Every tool response goes into the `inputs/` directory **with the Write tool,
exactly as the tool returned it** (the `guide` text may be left off; nothing
else may). `src.manual_assemble` does every rename, trim, merge and filter.

`inputs/` is a directory in the checkout. The Write tool takes an absolute
path: `pwd` once, and write to `<that>/inputs/<name>.json`.

## Aborting: one call

Every "stop" after Step 1 has claimed the lock, other than the normal end,
means exactly this:

```sql
select edge_cycle_release('<cycle_id>', '{"stage": "<the step you were in>", "detail": "<what failed, verbatim>"}'::jsonb);
```

It releases the lock, enqueues the `Cycle failed` alert and marks the session
failed. Do not also call `edge_alerts_enqueue`, and never `PushNotification`.
Then `edge_session_finish` and the `Run end` line.

**A tool or command that fails twice is an abort, and so is a permission
denial.** Do not look for another way to run the same thing.

## Session-end timestamp

The last line of your final response — however you stop — must be:

```bash
TZ=America/New_York date "+Run end: %H:%M %Z"
```

Immediately before it, once a cycle id has been minted (Step 0.5): `select
edge_session_finish('<cycle_id>', <turns>, <tool_calls>, <bytes_in>, '<one-line
note or null>');`, `null` for any count you lack. Not optional on any exit
path.

---

## Step 0 — the checkout

```bash
git clone --depth 1 https://github.com/<owner>/deterministic-trader.git
cd deterministic-trader
uv sync
```

If the clone fails, stop — there is nothing safe to guess in its place.

## Step 0.5 — what kind of day is it? (free, local, no MCP)

Mint the cycle id here: `python3 -c "import uuid; print(uuid.uuid4())"` and
`date -u +%Y-%m-%dT%H:%M:%SZ` print the two values; write them with the Write
tool to `inputs/seed.json`:

```json
{"cycle_id": "<uuid>", "now": "<UTC now>"}
```

```bash
uv run python -m src.cli plan-fetch < inputs/seed.json > plan.json
```

Then open the session-stats row, the first Supabase call this run:
`select edge_session_start('<cycle_id>', 'manual-check',
'<plan.json.cycle.trading_day>', '<model>');`

**This routine does not stop on a closed session.** `plan.json.cycle.session`
is `regular_hours`, `extended_hours`, `all_day_hours` or `closed`; note which,
discard `plan.json`, and continue with the same `cycle_id`. On a **weekend**
(`closed`) `manual-check-decide` reads the session as `all_day_hours` and
places; on a **market holiday** (`closed`) it reports every holding and places
nothing, because guard refuses with `SESSION_CLOSED`. Either way the run goes
through Step 8, so the operator always gets the table.

## Step 1 — the kill switch and the run lock

```sql
select edge_control_json() as control;
```

* **`control.trading_enabled` is not `true`** → write one runlog row with
  `command = 'halted'` and stop (this releases nothing, nothing is claimed).

```sql
select edge_cycle_claim('<cycle_id>', 'manual-check') as claim;
```

The `§13.1` run lock is **shared with every scheduled routine**. It is what
makes it safe to include core's positions: while this run holds it, no
scheduled cycle can place against the same shares.

| `claim` | Do |
|---|---|
| `{"claimed": true, "stole_from": null}` | Continue. |
| `{"claimed": true, "stole_from": "<id>"}` | Continue, and say so in the final report — a prior session stopped without releasing. |
| `{"claimed": false, ...}` | A scheduled cycle is running. Log `CYCLE_ALREADY_IN_PROGRESS` to the runlog, tell the operator to try again in a few minutes, and **stop**. No wait, no retry. |

The lock times out after 20 minutes. This run takes about 7. Release it on
every exit path (Step 8, or the one call in "Aborting").

## Step 2 — the book

The account number is `<your-cash-account-number>`. Each response to its own file, as it came.

| Call | File |
|---|---|
| `get_portfolio` | `inputs/portfolio.json` |
| `get_accounts` | `inputs/accounts.json` |
| `get_equity_positions` | `inputs/positions.json` |

`SYMBOLS` = every `symbol` in `positions.json`. **If `SYMBOLS` is empty**, there
is nothing to check: release the lock, report "no holdings" and stop.

## Step 3 — the state read, then the market reads

```sql
select jsonb_build_object(
  'control',             edge_control_json(),
  'dip_recovery_config', edge_dip_recovery_config_json(),
  'loss_closes',         edge_loss_closes_recent(),
  'regime_inputs',       edge_regime_recent('<trading_day>') -> 'regime_inputs',
  'day_context',         (select coalesce(jsonb_object_agg(e.key, jsonb_build_object('earnings', e.value->'earnings')), '{}'::jsonb)
                            from jsonb_each(edge_day_context_recent('<trading_day>')) e
                           where e.key in (<SYMBOLS as 'A','B',...>) and e.value ? 'earnings')
) as state;
```

`<trading_day>` is `plan.json.cycle.trading_day`. Write the returned `state`
object to `inputs/state.json`, whole. **Do not call `edge_day_context_recent`
by itself**.

| Call | File |
|---|---|
| `get_equity_tax_lots`, **one call per symbol** in `SYMBOLS` | `inputs/taxlots.<SYMBOL>.json` |
| `get_equity_quotes [SYMBOLS]` (one call, ≤20) | `inputs/quotes.json` |
| `get_equity_tradability [SYMBOLS]` (≤10 per call) | `inputs/tradability.json` (`tradability.2.json` for a second call) |
| `get_equity_fundamentals [SYMBOLS]` (≤10 per call) | `inputs/fundamentals.json` |
| `get_earnings_results <symbol>`, **only for a symbol not a key of `state.day_context`** | `inputs/earnings.<SYMBOL>.json` |

* Fundamentals may be trimmed to four fields per row, values copied exactly:
  `symbol`, `volume`, `average_volume`, `sector`.
* **Earnings.** For each miss, write the whole response to its file and then
  cache it for core: `select edge_day_context_write_symbol('<trading_day>',
  '<symbol>', '{"earnings": <the response>}'::jsonb);`.
* A tax-lot call that fails twice is not an abort: leave that file out. The
  assembler names the symbol under `excluded_no_tax_lots` and neither sells nor
  averages it (no T+1 clock, no guess).

## Step 4 — the orders

`<today>` is **the calendar date in New York**, not `trading_day`: a Sunday
night run belongs to Monday's trading day but its orders are dated Sunday.

```bash
TZ=America/New_York date +%F
```

```
get_equity_orders  state=confirmed                        -> inputs/orders.confirmed.json
get_equity_orders  created_at_gte=<today>                  -> inputs/orders.today.json
get_equity_orders  state=filled  created_at_gte=<today minus 21 days> -> inputs/fills.json
```

`state` takes exactly one value. Write each response whole. The resting
take-profit sells are in `orders.confirmed.json`; the 21 days of fills give the
newest buy's exact time and price.

## Step 5 — assemble, then decide

```bash
uv run python -m src.manual_assemble inputs > cycle.json
```

Exit 0: `cycle.json` is the document. The one line it prints on stderr names the
symbols and, under `missing`, anything a fetch did not cover, and under
`excluded_no_tax_lots` any symbol that has no T+1 clock — read it. Exit
non-zero: stdout is a refusal naming the file. Correct that file once and
re-run. A second refusal is an abort, `stage` `Step 5 (assemble)`.

**Do not open `cycle.json` to edit it, and never add an `mc_check` key.**

```bash
uv run python -m src.cli manual-check-decide < cycle.json > check.json
```

If it exits non-zero its stdout is still JSON (`_refusal`): write that to the
runlog and abort. `check.json` carries:

| key | for |
|---|---|
| `holdings` | one row per symbol: price, cost, unrealized %, target, `t1_clear`, `decision` and `detail` — the report |
| `orders` | the **guard-approved** limit orders to place, sells first |
| `cancels` | the resting take-profit(s) to cancel before a sale |
| `blocked` | every order guard refused, and why |
| `skipped`, `manual_review` | why a position was not averaged; positions past the hold limit |

## Step 6 — write the runlog, before you place anything

```sql
select edge_runlog_write('<cycle_id>', 'manual-check', '<trading_day>', 'manual-check-decide', '<check.json>'::jsonb);
```

## Step 7 — place

For each order in `check.orders`, **in the order given** (sells before buys):

**A sale** (`side = sell`):

1. For each id in the order's `cancel_order_ids`: `cancel_equity_order`. The
   broker reserves every share against the first resting sell, so the sale
   cannot place until the take-profit is cancelled. Then read the order once
   (`get_equity_orders order_id=<id>`): it must be `cancelled`. If a cancel
   fails or the order is not cancelled, **do not place the sale**; report it.
2. `place_equity_order` with `account_number`, `symbol`, `side`, `type=limit`,
   `quantity`, `limit_price`, `market_hours` and `time_in_force` exactly as the
   order says (a whole quantity is sent without a decimal, `2` not `2.0`), and
   a fresh UUID `ref_id`.
3. **If the sale is rejected,** re-place the take-profit you cancelled so the
   position is not left uncovered: a `limit` sell of the cancelled order's
   quantity at its price (both are in `inputs/orders.confirmed.json`),
   `time_in_force = gtc`, `market_hours = regular_hours`. Report both.

**A buy** (`side = buy`, an average-down): `place_equity_order` as above. Then,
once every order has been placed, read each buy again
(`get_equity_orders order_id=<id>`):

* `filled` → done.
* Still open (`queued`, `confirmed`, `unconfirmed`, `partially_filled`) →
  `cancel_equity_order` for the remainder and report the filled part. A day
  order left resting is an order nobody is watching, and core's reconcile reads
  an unknown open order as `PHANTOM_ORDER`, which blocks entries for a cycle.

Record every placement and cancel in the runlog (`command = 'place'`, with the
order id and the broker's state). **A rejection** is verbatim broker text in the
runlog and in the report; no retry. `blocked` is a decision, not a transient
failure: never retry any of it. Nothing else is written: no position row, no
symbol lock, no counter. Core's next cycle adopts the broker's new quantities
the way it adopts any fill (`UNTRACKED_FILL` is logged, not a divergence).

## Step 8 — release the lock, then report

```sql
select edge_cycle_release('<cycle_id>');
```

Then `edge_session_finish`, then your final response, in this order:

1. **A table of `check.holdings`**: symbol, quantity, cost, price, unrealized %,
   target, T+1 clear, decision, detail.
2. **What was placed**: each order with its id and final state, each cancel.
3. **What was refused and why**: `blocked`, and any `T+1` hold with the date it
   clears.
4. **Anything unusual**: a stolen lock, an excluded symbol, a rejected sale and
   whether its take-profit was re-placed; on a **weekend run**, say so, that
   prices are Friday's last regular trade, and what the broker did with each
   order; on a **holiday run**, that nothing was placed.
5. The `Run end` line.

---

## Weekends, and outside regular hours

This routine treats a weekend as tradable, which goes beyond what Robinhood
publishes: the 24 Hour Market page
(<https://robinhood.com/us/en/support/articles/24hour-market/>) says Sunday
20:00 ET through Friday 20:00 ET. Weekend fills of hand-placed limit orders
have been observed on a **margin account**; this routine trades the **agentic
cash account**, so a weekend order may meet a rejection. That is handled, not
retried: see Step 7.

* A weekend (Friday 20:00 ET to Sunday 20:00 ET) is read as `all_day_hours`:
  limit orders only, `market_hours = all_day_hours`, with guard's overnight
  spread gate on. An order the broker rejects follows Step 7 (a rejected sale
  re-places the take-profit it cancelled).
* **The price is the last regular-session trade** (Friday's close): the weekend
  market is thin and quotes may be stale or missing. A missing live bid/ask
  makes guard refuse a sale (`OVERNIGHT_SPREAD_GATE`); a sale placed at the
  Friday close that the weekend price does not reach simply rests as a GTC
  take-profit, which is profit-first. Say so in the report.
* **T+1 across a weekend:** a Friday buy is not clear until the next session
  (Monday); a Thursday buy is clear on Saturday.
* A **market holiday** is not a weekend: it stays `closed` and the run only
  reports.

On weekdays outside regular hours the 24 Hour Market's own rules apply (source:
the page above):

* **Only the assets on Robinhood's 24 Hour Market list trade.** A holding not on
  it is refused by guard as `EXTENDED_SESSION_NOT_TRADABLE`; the report says so,
  and the position simply waits for regular hours. Nothing is cancelled for it.
* **Whole-share limit orders only**, which is all this routine places. GTC is
  accepted (executes 24 hours a day, up to 90 days); a GFD order is good for up
  to 24 hours.
* **An ATS may reject a limit price outside its price bands.** A rejected sale
  follows Step 7's rule (re-place the take-profit you cancelled). A rejected buy
  is reported; no retry.
* **Half-days** (day after Thanksgiving, Christmas Eve): the 24 Hour Market
  closes at 17:00 ET, but `cadence.py` still classes 17:00 to 20:00 as extended
  hours. A run in that window may meet a broker rejection rather than a guard
  refusal. Treat it as above; do not try to work around it.
* **T+1 is this system's rule, not Robinhood's.** The page states no
  settlement or cash-account rule, so the check stays "at least one trading
  session since the newest buy".

---

## What this routine does not do

* It has no schedule of its own (empty cron), and it does not change any
  scheduled routine. It is nonetheless a normal, authorised run when it arrives
  labelled as an automated firing: see "Read this first".
* It never sells below cost, never places a stop, and never calls a position's
  ATR stop (profit first: README.md). A position through that stop is core's
  per-cycle check, not this routine's.
* It does not size an average-down above one fifth of the deployable cash, and
  guard's position and sector caps still apply to it.
* It does not write or run a script of its own, and it does not edit
  `cycle.json`.
* It never runs `git commit` or `git push`, and never opens a pull request
  (the checkout is read-only input here). It never
  calls `PushNotification`; alerts go through `edge_cycle_release`'s failure
  argument only.
