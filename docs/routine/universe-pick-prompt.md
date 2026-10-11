# The Universe Pick routine — Dip Recovery's morning watchlist

It runs **once each trading morning** and does exactly one thing: choose the
up-to-five symbols Dip Recovery is allowed to consider for a new entry today,
and write them to `edge_control.dip_recovery_watchlist`.

**It places no orders. It touches no position. It writes one key.** Everything
downstream of that key — the seven-condition entry gate, sizing, the guard
pass — belongs to `dip-monitor-prompt.md`, and a mistake here can only ever
cost this side a candidate, never produce a trade. That asymmetry is the whole
reason this routine is allowed to be simple.

Every threshold is in
`src/dip_recovery/watchlist.py` and `dip_recovery_config.universe_thresholds` —
**this prompt hard-codes none of them**, with the single, deliberate exception
of the scanner filters in Appendix A, which are explained there.

---

## Schedule: 07:40 ET, behind the pre-market job

Not arbitrary. `data/universe.json` (the membership fence, refreshed by a
periodic data snapshot commit — not by any same-morning cycle),
`edge_loss_closes` (the wash-sale ledger, kept current by whichever cycle last
reconciled) and the per-symbol `edge_day_context` cache are all things this
routine reads. What runs ahead of this slot is the pg_cron
`edge-premarket` job (`edge_premarket_dispatch`,
`services/core/core/premarket.py`), firing every two minutes across a
**04:30-07:15 ET** window to write each candidate's `day_context` — SMAs plus
daily RSI(14)/ATR(14) — and prefetch today's DIP screen. By 07:40 that window has been running for over three
hours, so `edge_day_context` and most of the day's earnings dates are already
paid for. This slot also sits two hours ahead of the 09:40 ET full cycle,
which then enters against a watchlist this routine has already written.

**This routine does not take the §13.1 run lock, deliberately.** The lock
exists because two sessions must never place orders against the same account at
once, and this one places none. Claiming it would instead mean that a core
cycle running a minute long makes the pick return `CYCLE_ALREADY_IN_PROGRESS`
and silently do nothing — a lock protecting against a hazard that does not
exist, at the cost of the watchlist. The one thing a concurrent core cycle can
do to this routine is let it read a symbol lock a moment before core claims a
symbol; the result is one watchlist name that `guard.lock` refuses hours
later, which is exactly what that gate is for.

---

## Session-end timestamp

The last line of your final response — however you stop — must be:

```bash
TZ=America/New_York date "+Run end: %H:%M %Z"
```

Immediately before it, once a cycle id has been minted (Step 0.5): `select
edge_session_finish('<cycle_id>', <turns>, <tool_calls>, <bytes_in>, '<one-line
note or null>');`, `null` for any count you lack. Not optional on any exit
path — the noop stop, the kill-switch halt, and the normal Step 7/8 finish all
close the row `edge_session_start` opens below, the same rule `cycle-prompt.md`
and `runner-prompt.md` hold themselves to. Skip the call only if you stopped
before Step 0.5 ever minted a cycle id.

Diagnostic narration otherwise, not a rule.

---

## Step 0 — the checkout

```bash
git clone --depth 1 https://github.com/<owner>/deterministic-trader.git
cd deterministic-trader
uv sync
```

If the clone fails, stop — there is nothing safe to guess in its place.

## Step 0.5 — the holiday/weekend check (free, local, no MCP)

`cycle-prompt.md` classifies every firing through `cadence.py` before
touching Robinhood or Supabase, and this routine does the same. Without it, a
holiday firing would run the full scan → fetch → pick pipeline and write a
five-symbol watchlist for a day the exchange is shut.

Mint the cycle id here, not later — the runlog row below and Step 4's fetch
list must share it:

```json
{"cycle_id": "<uuid>", "now": "<UTC now, from `date -u +%Y-%m-%dT%H:%M:%SZ`>"}
```

```bash
uv run python -m src.cli plan-fetch < cycle.json > plan.json
```

Then open the session-stats row, the first Supabase call this tick:
`select edge_session_start('<cycle_id>', 'universe-pick',
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

  No scan, no per-symbol fetch, no watchlist write. Step 6 would have written
  an empty watchlist anyway on a day with no scan hits — this just avoids
  paying for the scan and Step 5's reads to get there.
* **Anything else** → discard `plan.json` (its `fetch` list is core's own
  plan, not this routine's) and continue to Step 1 with the same `cycle_id`.

## Step 1 — the kill switch

```sql
select edge_control_json() as control;
```

* **`control.trading_enabled` is not `true`** → the kill switch is absolute and
  covers both strategy sides. Write one runlog row with `command = 'halted'`
  and stop. Today's watchlist is then never written, and Dip Monitor reads
  yesterday's as stale — so it manages and exits what it holds and proposes no
  entries, which is what the switch means.

`dip_recovery_config.dip_recovery_enabled` being off is **not** a reason to
stop here. The pick places nothing, and writing the watchlist anyway means
flipping that flag back on takes effect the same day instead of the next.

## Step 2 — the free reads

```sql
select edge_dip_recovery_config_json()    as dip_recovery_config;
select edge_symbol_lock_json()            as symbol_lock;
select edge_dip_recovery_positions_json() as dip_recovery_positions;
select edge_loss_closes_recent()          as loss_closes;
select edge_day_context_recent('<trading_day>') as day_context;
```

```
get_equity_positions   ->  positions
```

`positions` is **core's book**, and it is read alongside `symbol_lock` rather
than instead of it: `reconcile.py` claims a symbol on the cycle *after* core's
fill confirms, so between those two moments the book is the only place that
position exists. Both, unioned, are what "the other side owns this" means.

`loss_closes` is the §9 wash-sale ledger and it is **account-level, not
core's** — the IRS rule attaches to the account and the symbol, so a name core
closed at a loss last week is one this side may not repurchase either.

## Step 3 — the scan

Run the Dip Recovery oversold scan (Appendix A — create it once, then only run
it):

```
run_scan   scan_id <edge-dip-recovery-oversold's id>   ->  scan (tickers only)
```

Keep the tickers **in the order the scan returned them**, deduplicated by
nothing — `dip-recovery-pool` deduplicates, and their order is load-bearing
exactly once (the fetch ceiling truncates from the end). Do not sort, filter or
re-rank them here. This is discovery, not judgment.

If the scan returns nothing, continue anyway: Step 6 still writes today's
watchlist as an empty list, which is a different and more useful fact than not
writing it at all.

## Step 4 — the fetch list

```json
{
  "cycle_id": "<uuid>",
  "now": "<UTC now, from `date -u +%Y-%m-%dT%H:%M:%SZ`>",
  "control": { ... },
  "dip_recovery_config": { ... },
  "snapshot": { ...the checkout's `data/universe.json`... },
  "scan": [ ...tickers... ],
  "symbol_lock": { ... },
  "dip_recovery_positions": { ... },
  "positions": [ ...get_equity_positions... ],
  "loss_closes": [ ... ]
}
```

```bash
uv run python -m src.cli dip-recovery-pool < pool.json > fetch.json
```

Every filter this command applies costs nothing — membership against the
checked-in snapshot, the lock, this side's own rows, the loss ledger, and the
operator's sector rotation (`control.active_sectors`, `SECTOR_INACTIVE`) —
which is the entire reason it runs *before* the per-symbol reads rather than
after. The rotation is applied **before** the fetch ceiling, so a narrow
rotation returns a shorter pool rather than a full one spent partly on names
it would drop anyway. Pass `control` through exactly as Step 1 read it; do
not filter the scan by sector yourself.
`fetch.json` carries:

| key | for |
|---|---|
| `fetch` | the symbols Step 5 fetches, and nothing else |
| `skipped` | every scan hit that did not make it, with its reason (runlog) |
| `reason` | one line for the runlog and the operator |

A `skipped` list where every row reads `NOT_IN_UNIVERSE` means the universe
snapshot is stale, not that the scan misfired — check `reason`, which says so
in words.

**Fetch exactly what `fetch` lists.** It is already capped at
`universe_thresholds.max_scan_survivors`; extending it by hand on a morning
that looks interesting is how one pick turns into a few hundred sequential
broker calls.

## Step 5 — the per-symbol reads

Per symbol in `fetch`:

```
get_equity_quotes                 [<symbol>]                     -> quotes[symbol]  (last)
get_equity_technical_indicators   [<symbol>] interval=day output=latest
                                                                 -> signals[symbol] (atr)
get_equity_fundamentals           [<symbol>]                     -> fundamentals[symbol] (average_volume)
get_earnings_results              [<symbol>]                     -> earnings[symbol]
```

* **`atr` is the reason the indicator call is here at all.** The pick ranks on
  20-day ATR as a percentage of price; `src/dip_recovery/watchlist.py` fails a
  symbol **closed** (`METRICS_UNAVAILABLE`) when it is missing, never ranking
  it as if it had no volatility.
* **`earnings` — check `day_context` first.** For any symbol already in Step
  2's `day_context` blob for today, copy its cached earnings straight across
  and make no call. Fetch only the misses, and write each one back with
  `select edge_day_context_write_symbol('<trading_day>', '<symbol>', '<symbol_data>'::jsonb);`
  immediately after its own fetch. One call per symbol per *day*, shared with
  core and with Dip Monitor.
* **Paste each earnings response whole.** `cli._earnings` derives
  `sessions_until_report` from the broker's quarter rows; a response
  pre-reduced by hand would silently drop every symbol `EVENT_RISK` here
  rather than fail loudly.

## Step 6 — the pick

```json
{
  "cycle_id": "<uuid>",
  "now": "<UTC now>",
  "control": { ... },
  "dip_recovery_config": { ... },
  "fetch": [ ...from Step 4... ],
  "quotes": { ... }, "signals": { ... }, "fundamentals": { ... },
  "earnings": { ... }
}
```

```bash
uv run python -m src.cli dip-recovery-pick < pick.json > watchlist.json
```

`watchlist.json` carries:

| key | for |
|---|---|
| `symbols` | today's watchlist, at most `universe_thresholds.watchlist_size` |
| `ranked` | every survivor with its two factors and its score (runlog) |
| `skipped` | every rejection and its reason, `RANK_NOT_TOP` included |
| `control_updates` | the single `edge_control_apply` payload for Step 7 |
| `reason` | one line for the runlog |

`now` is the wall clock, never a rounded slot. The command derives its own
cycle from it (Invariant 6). If it exits non-zero, its stdout is still JSON
(`_refusal`) — write that to the runlog and stop **without** writing the
watchlist.

## Step 7 — write

Runlog first (Invariant 3):

```sql
select edge_runlog_write('<cycle_id>', 'universe-pick', '<trading_day>', 'dip-recovery-pick', '<watchlist.json>'::jsonb);
select edge_control_apply('<watchlist.control_updates>'::jsonb);
```

`edge_control_apply` is a per-key upsert, so this writes
`dip_recovery_watchlist` and touches nothing else in `edge_control` — core's
own keys, including its `today_watchlist`, are untouched by construction.

**Write `control_updates` verbatim.** It already carries
`{"trading_day", "symbols"}`, and the `trading_day` is what Dip Monitor's
freshness check reads. A hand-assembled value with yesterday's date is a
watchlist Dip Monitor will correctly ignore all day.

There is no lock to release — Step 1 never claimed one.

## Step 8 — the alert, when there is one

Enqueue one alert if either is true:

* `symbols` is empty on a day the scan returned hits. Not an error — it means
  every hit failed a real filter — but a week of empty watchlists means the
  thresholds want looking at, and nobody will notice from the runlog alone.
* any `skipped` row reads `SCAN_POOL_TRUNCATED`. The fetch ceiling bound this
  morning, so the pick chose from a partial pool.

Otherwise the runlog row is the whole record.

---

## What this routine does not do

* It does not decide anything about entries. `symbols` is a list of names the
  entry gate is *allowed to consider*; `signal.entry_signal` still re-checks
  all seven conditions on every Dip Monitor tick, and `guard.evaluate` still
  rules on the order.
* It does not touch a symbol core owns, and it does not write core's keys.
* It does not carry yesterday's watchlist forward. Every run writes today's,
  empty or not, and a day the routine never ran is a day Dip Monitor reads the
  key as stale and proposes no entries — the safe default, unchanged.
* It does not retry a failed read. The next morning's run picks it up; a day
  without a watchlist costs entries, never a position.

---

## Appendix A — the Dip Recovery oversold scan

Created once, then only run. Separate from core's `edge-dip-oversold`
(`cycle-prompt.md` Appendix C) because the two sides gate on different RSI
numbers and share nothing but the idea:

```
create_scan  name "edge-dip-recovery-oversold"
  FILTER_TYPE_RSI            <= 40    length 14   interval 1d
  FILTER_TYPE_EMA            (close above)  length 200  interval 1d
  FILTER_TYPE_AVERAGE_VOLUME >= 1000000       interval 1d
  FILTER_TYPE_MARKET_CAP     >= 2000000000
```

**The 40 is deliberately looser than `entry_thresholds.rsi_max` (35), and that
is the only reason a number appears in this prompt at all.** A scan filter has
to be a literal, and a literal that matched the configured threshold would
quietly become the real threshold — change `rsi_max` to 38 and the scan would
still be handing over only RSI≤35 names, with nothing anywhere saying so. A
loose scan cannot do that: the config stays the authority, `signal.entry_signal`
re-checks the real gate per tick, and the cost of the slack is a few extra
symbols in Step 5. This is the same reasoning `cycle-prompt.md` Appendix C
gives for its EMA200 proxy — a discovery step may be too generous and may not be too strict.

**One-time setup after creating it: strip the filter-driven columns.**

```
update_scan_config  scan_id <edge-dip-recovery-oversold's id>  columns []
```

Each filter auto-adds its own result column and nothing downstream reads
them. `dip-recovery-pick` re-derives every number it ranks on from
Step 5's own calls. Run once, immediately after `create_scan`, and never again
unless the scan is recreated.
