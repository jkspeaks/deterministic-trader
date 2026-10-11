# The Dip Monitor routine — Dip Recovery's own cycle, on its own schedule

It runs the **Dip Recovery** strategy: buy controlled dip tranches in symbols
on its own watchlist, average down without ever crossing into a loss, and exit
a whole position only at a locked-in net profit. It writes to none of core's
tables — the only shared row is `strategy_symbol_lock`.

Every threshold, every clock, every cap is in `src/dip_recovery` and read from the
checkout — **this prompt hard-codes none of them**, the same discipline
`cycle-prompt.md` holds itself to.

**You place no core orders.** This routine touches only symbols Dip Recovery
owns or is watching. If you find yourself about to act on a symbol core holds,
stop — the symbol lock exists precisely to keep the two sides apart.

## Schedule: three live slots — 11:47, 12:47 and 18:00 ET

The slots are spaced around the Claude account's 5-hour usage window (it opens on the day's
*first* firing, the 07:40 Universe Pick, and resets in fixed 5-hour blocks from there — not a
rolling window): the two `Core` cycles (09:40, 15:07) each anchor their own window largely alone,
and this routine's checks fall inside the windows those anchor, alongside the cheap
`Postclose`/`Overnight` pair (18:47, 22:47).

**18:00 ET is after the 16:00 ET regular close.** That is deliberate, not a
mistake: `cadence.session_of()` only returns `"closed"` for a non-trading
*day* (weekend/holiday), never for a time-of-day band, so a weekday-evening
firing reads `extended_hours`, not closed, and `dip-recovery-decide`'s guard
pass already checks `all_day_tradability` per symbol — the same reason
`cadence.py`'s own docstring says "over-claiming extended hours costs
nothing." Most Robinhood-tradable equities trade through the extended
session, so this slot still finds real opportunities.

Each slot is placed in a gap between core's own cycles (09:40 `full`, 15:07
`monitor`) so the shared §13.1 run lock is usually free when this routine
claims it. If a core cycle overruns into a slot, Step 1's claim returns
`{"claimed": false}` and this routine logs `CYCLE_ALREADY_IN_PROGRESS` and
stops — the next slot picks it up, at a cost of a skipped tick, never a bad
order.

---

## Status: places real orders when the sleeve is on

`dip_recovery_config.dip_recovery_enabled` is the sleeve's switch, and it ships
**FALSE**. With it on, this routine places for real — run every step through
Step 8 either way; with it off, guard refuses this side's entries and the tick
only manages and exits what it already holds. There is no dry-run mode, and `trading_enabled` remains the hard
kill switch for both strategy sides.

Two things keep the two sides apart:

1. `reconcile._other_side_symbols` keeps a Dip-Recovery-owned symbol out of
   core's book, `positions_held`, concentration input, adoption and divergence
   checks, and `cycle-prompt.md` Step 6 reads `edge_symbol_lock_json()` and
   `edge_dip_recovery_positions_json()` on every core cycle so it can. Core
   also claims and releases its own half of the lock (Step 7 there), which
   is what makes `guard.lock` deny a Dip Recovery order on a core-held symbol.
2. `guard.evaluate` runs **inside** `dip-recovery-decide`, exactly as it
   runs inside `decide` — there is no separate guard step to perform.
   `dip.json.orders` is what guard approved; `dip.json.blocked` is what it
   refused, and why.

---

## You hold data; you do not process it

**Never write a script, and never retype a figure into one.** No `python3 -c`,
no heredoc, no `build_cycle.py`, no `jq`, over any data in any step. The Python
you run is exactly three checked-in commands, each given below: `src.cli
plan-fetch`, `src.dip_assemble` and `src.cli dip-recovery-decide`. (Step 0.5's
UUID one-liner computes nothing and reads no data.)

Every tool response goes into the `inputs/` directory **with the Write tool,
exactly as the tool returned it** (the `guide` text may be left off; nothing
else may). `src.dip_assemble` does every rename, trim, merge and filter. If you
find yourself renaming a field, dropping a bar, picking a row out of a list or
converting a date, you have taken over its job.

`inputs/` is a directory in the checkout. The Write tool takes an absolute
path: `pwd` once, and write to `<that>/inputs/<name>.json`.

## Aborting: one call

Every "stop" after Step 1 has claimed the lock, other than the normal end,
means exactly this:

```sql
select edge_cycle_release('<cycle_id>', '{"stage": "<the step you were in>", "detail": "<what failed, verbatim>"}'::jsonb);
```

It releases the lock, enqueues the `Cycle failed` alert and marks the session
failed, server-side. Do not also call `edge_alerts_enqueue`, and never
`PushNotification`. Then `edge_session_finish` and the `Run end` line. A tick
that ends normally releases with no second argument (Step 9).

**A tool or command that fails twice is an abort, and so is a permission
denial.** Do not look for another way to run the same thing.

---

## Session-end timestamp

The last line of your final response — however you stop — must be:

```bash
TZ=America/New_York date "+Run end: %H:%M %Z"
```

Immediately before it, once a cycle id has been minted (Step 0.5): `select
edge_session_finish('<cycle_id>', <turns>, <tool_calls>, <bytes_in>, '<one-line
note or null>');`, `null` for any count you lack. This is not optional on any
exit path — the noop stop, the kill-switch halt, a lock-claim refusal, an
abort, or the normal Step 9 release all close the row `edge_session_start`
opens below, the same rule `cycle-prompt.md` and `runner-prompt.md` hold
themselves to, so `finished_at - started_at` always exists for this routine
too. Skip the call only if you stopped before Step 0.5 ever minted a cycle id.

Diagnostic narration otherwise, not a rule.

---

## Step 0 — the checkout

```bash
git clone --depth 1 https://github.com/<owner>/deterministic-trader.git
cd deterministic-trader
uv sync
```

`uv sync` because this routine does run Python (`src.cli dip-recovery-decide`).
If the clone fails, stop — there is nothing safe to guess in its place.

## Step 0.5 — the holiday/weekend check (free, local, no MCP)

`cycle-prompt.md` classifies every firing through `cadence.py` before
touching Robinhood or Supabase, and this routine does the same. Without it, a
holiday firing would fetch quotes, indicators, fundamentals, historicals,
earnings and tradability for a whole watchlist before `dip-recovery-decide`
(Step 6) computed `cycle.session == "closed"` and refused every entry. The
refusal would be correct; paying for Step 3 to reach it is not. It is the
same free check `universe-pick-prompt.md` runs first.

Mint the cycle id here, not later — the runlog row below and Step 1's lock
claim must share it. `python3 -c "import uuid; print(uuid.uuid4())"` and
`date -u +%Y-%m-%dT%H:%M:%SZ` print the two values; write them with the Write
tool to `inputs/seed.json`:

```json
{"cycle_id": "<uuid>", "now": "<UTC now>"}
```

```bash
uv run python -m src.cli plan-fetch < inputs/seed.json > plan.json
```

Then open the session-stats row, the first Supabase call this tick:
`select edge_session_start('<cycle_id>', 'dip-monitor',
'<plan.json.cycle.trading_day>', '<model>');` — a noop tick writes one too,
same as `cycle-prompt.md`.

`plan.json.cycle.session` reads `"closed"` on a weekend or any date in
`config/market_holidays.json`, and nothing else — `control` defaults to `{}`
for this call and the closed-session check never looks at it, so this needs
no Supabase read first.

* **`session` is `"closed"`** → write the noop and stop:

  ```sql
  select edge_runlog_write('<cycle_id>', 'noop', '<plan.json.cycle.trading_day>', 'plan-fetch', '<plan.json>'::jsonb);
  ```

  No lock claim, no watchlist read, no per-symbol fetch. A held Dip Recovery
  position is still exactly as it was left — `guard.evaluate` never ran, so
  nothing about it changed — and the next scheduled tick on the next open
  session evaluates it exactly as this one would have.
* **Anything else** → discard `plan.json` (its `fetch` list is core's own
  plan, not this routine's) and continue to Step 1 with the same `cycle_id`.

## Step 1 — the kill switch and the run lock

```sql
select edge_control_json() as control;
```

* **`control.trading_enabled` is not `true`** → the kill switch is absolute and
  covers both strategy sides. Write one runlog row with `command = 'halted'`
  and stop.

```sql
select edge_cycle_claim('<cycle_id>', 'dip-monitor') as claim;
```

The `§13.1` run lock is **shared with the main cycle** — Dip Monitor and the
07:40/09:40/… cycles must never run concurrently, because both place orders
against the same account. Same three outcomes as `cycle-prompt.md` Step 4:

| `claim` | Do |
|---|---|
| `{"claimed": true, "stole_from": null}` | Continue. |
| `{"claimed": true, "stole_from": "<id>"}` | Continue, and enqueue an alert — a prior session stopped without releasing. |
| `{"claimed": false, ...}` | Log `CYCLE_ALREADY_IN_PROGRESS` to the runlog and **stop**. No wait, no retry. |

Release it on every exit path: Step 9 when the tick ends normally, the one
call in "Aborting" when it does not.

## Step 2 — the state read: one statement

```sql
with base as (
  select edge_control_json() as control,
         edge_dip_recovery_positions_json() as positions
), names as (
  select jsonb_array_elements_text(
           case when control->'dip_recovery_watchlist'->>'trading_day' = '<trading_day>'
                then coalesce(control->'dip_recovery_watchlist'->'symbols', '[]'::jsonb)
                else '[]'::jsonb end) as symbol
    from base
  union
  select jsonb_object_keys(positions) from base
)
select jsonb_build_object(
  'control',                control,
  'dip_recovery_config',    edge_dip_recovery_config_json(),
  'symbol_lock',            edge_symbol_lock_json(),
  'dip_recovery_positions', positions,
  'loss_closes',            edge_loss_closes_recent(),
  'regime_inputs',          edge_regime_recent('<trading_day>') -> 'regime_inputs',
  'day_context',            (select coalesce(jsonb_object_agg(e.key, jsonb_build_object('earnings', e.value->'earnings')), '{}'::jsonb)
                               from jsonb_each(edge_day_context_recent('<trading_day>')) e
                              where e.key in (select symbol from names) and e.value ? 'earnings'),
  'last_decide_now',        (select stdout->'cycle'->>'now' from edge_runlog
                              where command = 'dip-recovery-decide' order by created_at desc limit 1)
) as state
from base;
```

`<trading_day>` is `plan.json.cycle.trading_day`, in both places. Write the
returned `state` object to `inputs/state.json`, whole.

**Do not call `edge_day_context_recent` by itself.** It returns core's cache
for every symbol of the day and overflows the inline limit; the statement
above narrows it to this tick's symbols and to the one field this routine
reads.

What is in it, and why you compute nothing from it:

* `control` — re-read after the claim, so `cycle_in_progress` now names this
  cycle. That is correct; paste it as it came.
* `control.dip_recovery_watchlist` — Universe Pick (07:40 ET) writes it each
  morning as `{"trading_day", "symbols"}`. A list dated any other day is
  treated as **empty** by `src.dip_assemble`: the tick then only manages and
  exits what is already held.
* `loss_closes` — the §9 wash-sale ledger, account-level, not core's.
* `regime_inputs` — the pre-market job's SPY / sector block, the same one
  core's `decide` judges on. `dip-recovery-decide` gates **new entries and
  average-downs only** on it, never an exit. Null means no regime opinion,
  which is the intended degradation.
* `day_context` — the earnings core has already paid for today, for this
  tick's symbols only.
* `last_decide_now` — when the last tick decided. Step 5's fills are cut on it.

**`SYMBOLS`** = `state.control.dip_recovery_watchlist.symbols` (only when its
`trading_day` is today) plus every key of `state.dip_recovery_positions`. If
`SYMBOLS` is empty, make only Step 3's two account calls and Step 4's and
Step 5's order calls: there is no symbol to fetch anything for.

## Step 3 — the account and the market reads

Each response to its own file, as it came.

| Call | File |
|---|---|
| `get_portfolio` | `inputs/portfolio.json` |
| `get_accounts` | `inputs/accounts.json` |
| `get_equity_quotes [SYMBOLS]` (one call, ≤20) | `inputs/quotes.json` |
| `get_equity_tradability [SYMBOLS]` (≤10 per call) | `inputs/tradability.json` |
| `get_equity_technical_indicators`, three calls per symbol in `SYMBOLS`: `type=rsi`; `type=sma period=20`; `type=sma period=200`. All `interval=day output=latest`, `start_time` a year back | `inputs/indicators.json`, one JSON array holding every response |
| `get_equity_fundamentals [watchlist symbols]` (≤10 per call) | `inputs/fundamentals.json` |
| `get_equity_historicals [watchlist symbols] interval=day bounds=regular start_time=<today minus 35 calendar days>` | `inputs/historicals.json` |
| `get_earnings_results <symbol>`, **only for a symbol in `SYMBOLS` that is not a key of `state.day_context`** | `inputs/earnings.<SYMBOL>.json` |

A second call of the same kind (more than ten symbols) goes in a second file:
`inputs/tradability.2.json`. The name before the first dot is what
`src.dip_assemble` reads; anything it does not recognise it refuses.

Every Robinhood call that takes one carries `account_number` (the account your
trigger prompt names).

* **Fundamentals may be trimmed to four fields per row**, values copied
  exactly: `symbol`, `volume`, `average_volume`, `sector`. Nothing else in the
  response is read. This is the one permitted trim.
* **Earnings.** A symbol already in `state.day_context` needs no call and no
  file. For each miss, write the whole response to its file and then cache it
  for core: `select edge_day_context_write_symbol('<trading_day>', '<symbol>',
  '{"earnings": <the response>}'::jsonb);`. One call per symbol per *day*,
  shared with core.
* **The 35 days** are `dip_recovery_config.entry_thresholds.bollinger_period`
  (20 sessions) plus weekends and holidays. A short series costs that symbol
  its entry this tick (`src/dip_recovery/signal.py` fails the bar gates
  closed), never a bad buy.
* **The operator's sector rotation** (`control.active_sectors`) is applied
  inside `dip-recovery-decide`, not by you.

A tick that skips one of these does not trade cautiously, it does not trade at
all: guard denies on a missing input (`EARNINGS_DATE_UNAVAILABLE`,
`NOT_TRADABLE`, `SECTOR_UNKNOWN`).

## Step 4 — the orders

```
get_equity_orders      state=confirmed                    -> inputs/orders.confirmed.json
get_equity_orders      created_at_gte=<trading_day>       -> inputs/orders.today.json
```

**`state` takes exactly one value.** `queued,confirmed` is not a filter: it
returns `[]` even while `confirmed` orders rest, so guard could not see a
resting order. `confirmed` is what rests (a GTC target of any age);
the `created_at_gte` call catches what was placed today and is still `queued`,
`new`, `unconfirmed` or `partially_filled`. Write both responses whole.
`src.dip_assemble` drops the finished ones and keeps an order both calls
returned once.

## Step 5 — confirmed fills since the last Dip Monitor tick

```
get_equity_orders    state=filled    created_at_gte=<trading_day minus 21 days>
```

Write it to `inputs/fills.json`, whole. Bounded on purpose: unfiltered,
`state=filled` is every fill the account has ever had. 21 days outlasts any
resting order's life.

You select nothing from it. `src.dip_assemble` keeps the executions on this
side's symbols (locked to `dip_recovery`, or carrying a position row) that
came after `state.last_decide_now` and are not already in the position row.

## Step 6 — assemble, then decide

```bash
uv run python -m src.dip_assemble inputs > cycle.json
```

Exit 0: `cycle.json` is the document. The one line it prints on stderr names
the tick's symbols and, under `missing`, any symbol a fetch did not cover —
read it. A symbol under `missing` will be refused entry this tick; if that is
every symbol for one key, the fetch for that key did not land in its file, and
you may fix the file and run the command once more.

Exit non-zero: stdout is a refusal naming the file. Correct that file once and
re-run. A second refusal is an abort, `stage` `Step 6 (assemble)`, `detail`
the refusal's own sentence.

**Do not open `cycle.json` to edit it, and never add an `mc_check` key.** Dip
Recovery runs no Monte Carlo pass, and `guard.mc` skips itself only when the
key is *absent*: present and empty, it denies every buy
`MC_SIMULATION_UNAVAILABLE`.

```bash
uv run python -m src.cli dip-recovery-decide < cycle.json > dip.json
```

`now` is the wall clock, never a rounded slot. The command derives its own
cycle from it (Invariant 6) and never takes one on stdin. If it exits
non-zero, its stdout is still JSON (`_refusal`) — write that to the runlog and
stop.

`dip.json` carries:

| key | for |
|---|---|
| `orders` | the **guard-approved** buys and sells (Step 8) |
| `blocked` | every order guard refused, with its denial codes — runlog and alert |
| `position_updates` | `edge_dip_recovery_positions_apply` (Step 8) |
| `symbol_lock_claims` / `symbol_lock_releases` | `edge_symbol_lock_claim` / `_release` (Step 8) |
| `evaluations` | `edge_dip_recovery_evaluations_append` — already `cycle_id`-stamped |
| `control_updates` | `edge_control_apply` for `dip_recovery_new_positions` (Step 8) |
| `manual_review` | a `HOLD_NO_NEW_BUYS` transition — enqueue an alert, never a sell |
| `closed` | positions a sell fill closed this tick — enqueue an alert |
| `skipped` | why each watchlist symbol did not become a candidate (runlog only) |

## Step 7 — write the runlog, before you place anything

Invariant 3.

```sql
select edge_runlog_write('<cycle_id>', 'dip-monitor', '<trading_day>', 'dip-recovery-decide', '<dip.json>'::jsonb);
```

**The guard pass already happened.** The design doc's last entry condition is
"every existing portfolio, account, concentration, and guardrail check passes",
and `dip-recovery-decide` runs `guard.evaluate` itself — there is nothing to
run here. `dip.json.orders` is what guard approved, with
`ctx["strategy"] = "dip_recovery"` fixed by the command, so `guard.lock`,
`guard.strategy`, `guard.schedule` and `guard.concentration` all read this
side's own lock, flag, watchlist and caps.

`dip.json.blocked` is what it refused. **Enqueue an alert for it and do not
retry any of it this tick** — a denial is a decision, not a transient failure.
A `blocked` list where every entry carries the same code across every symbol
is the shape of a missing Step 3 fetch rather than a real risk verdict; check
that before assuming the market is simply uninteresting.

`blocked` being non-empty is never a reason to skip Step 8 for the orders that
*were* approved.

## Step 8 — place, then apply state

For each order in `dip.json.orders` — all of which guard has already
approved — in this order, sells before buys, exactly as `cycle-prompt.md`
argues:

```
place_equity_order   type=limit  (every Dip Recovery order is a limit order)
```

* **Fills immediately / rests** → record the order id in the runlog
  (`command = 'place'`).
* **Rejected** → verbatim broker text to the runlog and an alert. No retry
  this tick.

Then, once, whatever placed:

```sql
select edge_dip_recovery_positions_apply('<dip.position_updates>'::jsonb);
select edge_symbol_lock_claim('<symbol>', 'dip_recovery');   -- per symbol_lock_claims
select edge_symbol_lock_release('<symbol>', 'dip_recovery');  -- per symbol_lock_releases
select edge_dip_recovery_evaluations_append('<dip.evaluations>'::jsonb);
select edge_control_apply('<dip.control_updates>'::jsonb);
```

`edge_control_apply` replaces the `dip_recovery_new_positions` key wholesale —
`dip.control_updates` already carries the full new value (`{"date", "count"}`),
so there is nothing to merge by hand.

A claim that returns `false` means the other side already owns that symbol —
that is a cross-strategy conflict, not a no-op: alert on it, and do **not**
place or have placed anything on that symbol this tick.

## Step 9 — release the lock

```sql
select edge_cycle_release('<cycle_id>');
```

On the normal path. An abort releases with the failure argument instead (see
"Aborting"), so the operator is told.

---

## What this routine does not do

* It does not build the watchlist — that is the Universe Pick routine's job
  (`universe-pick-prompt.md`). Dip Monitor reads whatever list it is handed and
  proposes nothing when it is empty or stale.
* It does not touch a symbol core owns, ever. The `symbol_lock` read gates
  both entries and exits (`src/dip_recovery` and `guard.lock` both enforce it).
* It does not force-sell a position at a loss. A position 47 trading days past
  its last fill with no exit goes to `HOLD_NO_NEW_BUYS` and waits for a human
  — `manual_review` in `dip.json` — it is never flattened.
* It does not retry a rejected order or a failed read within the same tick.
  The next scheduled tick (12:47 or 18:00 ET) picks it up.
* It does not write or run a script of its own, and it does not edit
  `cycle.json`. `src.dip_assemble` builds that file from `inputs/`.
* It never runs `git commit` or `git push`, and never opens a pull request
  (the checkout is read-only input here).
  It never calls `PushNotification`; every alert goes through
  `edge_alerts_enqueue`.
