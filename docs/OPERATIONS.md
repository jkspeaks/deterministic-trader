# Operations

A fork-facing guide: what shows up on your phone, what to do about it, and how
to stand the system up from a bare Supabase project. This is the doc every
alert body's footer links to (`docs/OPERATIONS.md#<anchor>`) — the anchor is a
GitHub-style slug of the alert's own title (`src/report.py::_doc_slug`), so
every `## <title>` heading below resolves from the push that named it.

This document covers what a fork operator needs to run the system day to day.
Every knob is in [`docs/TUNING.md`](TUNING.md).

---

## Current state

The shipped values are in `config/config.json`; the runtime switches are
whatever `status` says.

**What may buy.**

| Strategy | State | Held off or switched by |
|---|---|---|
| Core sleeve (`SPYM`, up to 50% of equity, no stop of any kind) | On | `core_sleeve_enabled` in `config/config.json`; the `core_off` verb pauses its buys |
| DIP | On | `dip_entries_enabled` in `config/config.json`; the `core_off` verb pauses its entries |
| Dip Recovery sleeve | On in the live account; a new database seeds it off | the `dip_on`/`dip_off` verbs |
| CATALYST, MACRO | Shelved | `play_catalyst_enabled`, `play_macro_enabled` (both `false`) |
| PEAD, SURGE | Retired in code | A code change. The `surge_on` verb is refused |
| Sector play, DIP pullback reshape, regime v2, sizing v2 | Off; each records what it would have done | Their keys in `config/config.json` |

**Protection.** Every whole-share position rests one broker order: a limit
sell above its buy price. The stop is checked once per cycle and is never a
resting order. See [The stop/target rule](#the-stoptarget-rule).

**What runs, and where.** Times are Eastern, trading days.

| When | What | Runs on |
|---|---|---|
| 04:30 | Publishes ten trading days of cycle slots and the cycle budgets | `pg_cron` `edge-schedule` → hosted core |
| 04:30-07:15, every 2 min | Pre-market data job | `pg_cron` `edge-premarket` → hosted core |
| 07:40 | Universe Pick (the Dip Recovery watchlist) | Claude routine, `universe-pick-prompt.md` |
| 09:40 | Full cycle | Claude routine, `runner-prompt.md` |
| 10:00-15:30, every 30 min | State-change gate | `pg_cron` `edge-watch` → hosted core |
| 11:47, 12:47, 18:00 | Dip monitor | Claude routine, `dip-monitor-prompt.md` |
| 15:07 | Monitor cycle | Claude routine, `runner-prompt.md` |
| 18:47 | Post-close cycle | Claude routine, `runner-prompt.md` |
| 22:47 | Overnight cycle | Claude routine, `runner-prompt.md` |
| Every minute | Alert relay (`edge-alert-send`, `edge-alert-reap`) and `edge-step-watchdog` | `pg_cron` |
| Nightly | Shadow replay (`edge-shadow`), evidence back-fill (`edge-evidence`), three reap jobs | `pg_cron` |
| Half-hourly, weekly, monthly | Events, Universe, Leaders shadow record | GitHub Actions |

There is no pre-market *cycle* routine any more: the pre-market data job
replaced it. The `Pre-market cycle …` titles below stay canonical and keep
their headings, but nothing scheduled sends them. `cycle-prompt.md` is on no
enabled routine; it is the recovery path if the hosted core is down.

The routine crons are UTC and carry no timezone field, so they shift by an
hour at each daylight-saving change. Keep them in step with Eastern wall-clock
time when the change happens.

---

## Alert titles

Every push this system ever sends uses one of 42 canonical titles
(`src/report.CANONICAL_TITLES` plus `Order refused`/`Manual action required`,
enforced by `db/schema.sql`'s `edge_alerts_enqueue` — a
non-canonical title is rejected before it reaches the queue). 30 are
`<Routine> cycle <status>` combinations — one end-of-cycle push per cycle,
covered below by status phrase. 12 are fixed, one-off titles, covered after.

### Generated: `<Routine> cycle <status>`

Five routine labels (`Full`, `Pre-market`, `Monitor`, `Post-close`,
`Overnight`; no routine is scheduled under `Pre-market`, see [Current
state](#current-state)) each combine with six status words (`succeeded`,
`succeeded with warnings`, `partial`, `blocked`, `failed`, `skipped` the last
one, `skipped`, is a NOOP cycle's word, sometimes-disagreeing one). The
meaning and action depend only on the status word, not which routine sent it —
grouped here by routine so every exact title still gets its own heading (each
resolves from a real push).

## Full cycle succeeded

*Screens entries, decides exits, places both sides.*

**Meaning:** Nothing to report — every step ran, nothing was rejected, and either the routine didn't screen entries this cycle (Monitor/Post-close/Overnight) or it did and found nothing to open or close.

**Action:** None. Priority `min` — this exists so a missing push (a dead relay, a cycle that never ran) is distinguishable from a quiet market, not so you read it.

## Full cycle succeeded with warnings

*Screens entries, decides exits, places both sides.*

**Meaning:** The cycle finished and placed/held what it should have, but the body carries at least one reconciliation finding, an expired `operator_holds` entry, or a play the agent switched on/off — worth a glance, nothing broke.

**Action:** Skim the body. If it's a play-state change or an `OPERATOR_HOLD_EXPIRED` line, no action is needed. If it's a reconciliation finding, see [How to read a runlog row](#how-to-read-a-runlog-row).

## Full cycle partial

*Screens entries, decides exits, places both sides.*

**Meaning:** At least one order was refused by the broker, or an exit computed but couldn't place (a fractional quantity, a missing bid) and fell back to `Manual action required`.

**Action:** Read the refused-order line(s) in the body — they name the symbol, side and the broker's own rejection text. If it's the same rejection repeating, see [The stop/target rule](#the-stoptarget-rule) — a second resting order on a name that already has one is refused every time by design.

## Full cycle blocked

*Screens entries, decides exits, places both sides.*

**Meaning:** Entries are blocked for this cycle or until cleared — either a broker order nobody's plan proposed (`PHANTOM_ORDER`), or the broker and the book have disagreed for enough cycles running that it's `until cleared`.

**Action:** One phantom order clears itself next cycle if it turns out to be ours. A persistent block does not — see [How to clear a block](#how-to-clear-a-block).

## Full cycle failed

*Screens entries, decides exits, places both sides.*

**Meaning:** A step in the cycle raised — the failure's stage and the broker/tool error text are in the body verbatim, never paraphrased.

**Action:** Read the `<step> failed: <detail>` line. Most failures are transient (a rate limit, a stale schedule row) and the next scheduled trigger retries on its own; if the same step fails twice running, it's worth opening the runlog row for that `cycle_id`.

## Full cycle skipped

*Screens entries, decides exits, places both sides.*

**Meaning:** The clock moved between `edge_should_run` saying this slot should fire and the core re-deriving the cycle a moment later — by the time it did, it was a NOOP (no trading session, or a holiday). Nothing was fetched, nothing was decided.

**Action:** None. Priority `min` — same reason `succeeded` is: this exists so a missing push is distinguishable from a routine that correctly found nothing to do.

## Pre-market cycle succeeded

*Data-only — computes the day's regime and dip-recovery watchlist, places nothing.*

**Meaning:** Nothing to report — every step ran, nothing was rejected, and either the routine didn't screen entries this cycle (Monitor/Post-close/Overnight) or it did and found nothing to open or close.

**Action:** None. Priority `min` — this exists so a missing push (a dead relay, a cycle that never ran) is distinguishable from a quiet market, not so you read it.

## Pre-market cycle succeeded with warnings

*Data-only — computes the day's regime and dip-recovery watchlist, places nothing.*

**Meaning:** The cycle finished and placed/held what it should have, but the body carries at least one reconciliation finding, an expired `operator_holds` entry, or a play the agent switched on/off — worth a glance, nothing broke.

**Action:** Skim the body. If it's a play-state change or an `OPERATOR_HOLD_EXPIRED` line, no action is needed. If it's a reconciliation finding, see [How to read a runlog row](#how-to-read-a-runlog-row).

## Pre-market cycle partial

*Data-only — computes the day's regime and dip-recovery watchlist, places nothing.*

**Meaning:** At least one order was refused by the broker, or an exit computed but couldn't place (a fractional quantity, a missing bid) and fell back to `Manual action required`.

**Action:** Read the refused-order line(s) in the body — they name the symbol, side and the broker's own rejection text. If it's the same rejection repeating, see [The stop/target rule](#the-stoptarget-rule) — a second resting order on a name that already has one is refused every time by design.

## Pre-market cycle blocked

*Data-only — computes the day's regime and dip-recovery watchlist, places nothing.*

**Meaning:** Entries are blocked for this cycle or until cleared — either a broker order nobody's plan proposed (`PHANTOM_ORDER`), or the broker and the book have disagreed for enough cycles running that it's `until cleared`.

**Action:** One phantom order clears itself next cycle if it turns out to be ours. A persistent block does not — see [How to clear a block](#how-to-clear-a-block).

## Pre-market cycle failed

*Data-only — computes the day's regime and dip-recovery watchlist, places nothing.*

**Meaning:** A step in the cycle raised — the failure's stage and the broker/tool error text are in the body verbatim, never paraphrased.

**Action:** Read the `<step> failed: <detail>` line. Most failures are transient (a rate limit, a stale schedule row) and the next scheduled trigger retries on its own; if the same step fails twice running, it's worth opening the runlog row for that `cycle_id`.

## Pre-market cycle skipped

*Data-only — computes the day's regime and dip-recovery watchlist, places nothing.*

**Meaning:** The clock moved between `edge_should_run` saying this slot should fire and the core re-deriving the cycle a moment later — by the time it did, it was a NOOP (no trading session, or a holiday). Nothing was fetched, nothing was decided.

**Action:** None. Priority `min` — same reason `succeeded` is: this exists so a missing push is distinguishable from a routine that correctly found nothing to do.

## Monitor cycle succeeded

*Re-checks exits and working orders between full cycles; screens entries only on a state change.*

**Meaning:** Nothing to report — every step ran, nothing was rejected, and either the routine didn't screen entries this cycle (Monitor/Post-close/Overnight) or it did and found nothing to open or close.

**Action:** None. Priority `min` — this exists so a missing push (a dead relay, a cycle that never ran) is distinguishable from a quiet market, not so you read it.

## Monitor cycle succeeded with warnings

*Re-checks exits and working orders between full cycles; screens entries only on a state change.*

**Meaning:** The cycle finished and placed/held what it should have, but the body carries at least one reconciliation finding, an expired `operator_holds` entry, or a play the agent switched on/off — worth a glance, nothing broke.

**Action:** Skim the body. If it's a play-state change or an `OPERATOR_HOLD_EXPIRED` line, no action is needed. If it's a reconciliation finding, see [How to read a runlog row](#how-to-read-a-runlog-row).

## Monitor cycle partial

*Re-checks exits and working orders between full cycles; screens entries only on a state change.*

**Meaning:** At least one order was refused by the broker, or an exit computed but couldn't place (a fractional quantity, a missing bid) and fell back to `Manual action required`.

**Action:** Read the refused-order line(s) in the body — they name the symbol, side and the broker's own rejection text. If it's the same rejection repeating, see [The stop/target rule](#the-stoptarget-rule) — a second resting order on a name that already has one is refused every time by design.

## Monitor cycle blocked

*Re-checks exits and working orders between full cycles; screens entries only on a state change.*

**Meaning:** Entries are blocked for this cycle or until cleared — either a broker order nobody's plan proposed (`PHANTOM_ORDER`), or the broker and the book have disagreed for enough cycles running that it's `until cleared`.

**Action:** One phantom order clears itself next cycle if it turns out to be ours. A persistent block does not — see [How to clear a block](#how-to-clear-a-block).

## Monitor cycle failed

*Re-checks exits and working orders between full cycles; screens entries only on a state change.*

**Meaning:** A step in the cycle raised — the failure's stage and the broker/tool error text are in the body verbatim, never paraphrased.

**Action:** Read the `<step> failed: <detail>` line. Most failures are transient (a rate limit, a stale schedule row) and the next scheduled trigger retries on its own; if the same step fails twice running, it's worth opening the runlog row for that `cycle_id`.

## Monitor cycle skipped

*Re-checks exits and working orders between full cycles; screens entries only on a state change.*

**Meaning:** The clock moved between `edge_should_run` saying this slot should fire and the core re-deriving the cycle a moment later — by the time it did, it was a NOOP (no trading session, or a holiday). Nothing was fetched, nothing was decided.

**Action:** None. Priority `min` — same reason `succeeded` is: this exists so a missing push is distinguishable from a routine that correctly found nothing to do.

## Post-close cycle succeeded

*Writes the day's benchmark row; the routine that fires the weekly scorecard on Fridays.*

**Meaning:** Nothing to report — every step ran, nothing was rejected, and either the routine didn't screen entries this cycle (Monitor/Post-close/Overnight) or it did and found nothing to open or close.

**Action:** None. Priority `min` — this exists so a missing push (a dead relay, a cycle that never ran) is distinguishable from a quiet market, not so you read it.

## Post-close cycle succeeded with warnings

*Writes the day's benchmark row; the routine that fires the weekly scorecard on Fridays.*

**Meaning:** The cycle finished and placed/held what it should have, but the body carries at least one reconciliation finding, an expired `operator_holds` entry, or a play the agent switched on/off — worth a glance, nothing broke.

**Action:** Skim the body. If it's a play-state change or an `OPERATOR_HOLD_EXPIRED` line, no action is needed. If it's a reconciliation finding, see [How to read a runlog row](#how-to-read-a-runlog-row).

## Post-close cycle partial

*Writes the day's benchmark row; the routine that fires the weekly scorecard on Fridays.*

**Meaning:** At least one order was refused by the broker, or an exit computed but couldn't place (a fractional quantity, a missing bid) and fell back to `Manual action required`.

**Action:** Read the refused-order line(s) in the body — they name the symbol, side and the broker's own rejection text. If it's the same rejection repeating, see [The stop/target rule](#the-stoptarget-rule) — a second resting order on a name that already has one is refused every time by design.

## Post-close cycle blocked

*Writes the day's benchmark row; the routine that fires the weekly scorecard on Fridays.*

**Meaning:** Entries are blocked for this cycle or until cleared — either a broker order nobody's plan proposed (`PHANTOM_ORDER`), or the broker and the book have disagreed for enough cycles running that it's `until cleared`.

**Action:** One phantom order clears itself next cycle if it turns out to be ours. A persistent block does not — see [How to clear a block](#how-to-clear-a-block).

## Post-close cycle failed

*Writes the day's benchmark row; the routine that fires the weekly scorecard on Fridays.*

**Meaning:** A step in the cycle raised — the failure's stage and the broker/tool error text are in the body verbatim, never paraphrased.

**Action:** Read the `<step> failed: <detail>` line. Most failures are transient (a rate limit, a stale schedule row) and the next scheduled trigger retries on its own; if the same step fails twice running, it's worth opening the runlog row for that `cycle_id`.

## Post-close cycle skipped

*Writes the day's benchmark row; the routine that fires the weekly scorecard on Fridays.*

**Meaning:** The clock moved between `edge_should_run` saying this slot should fire and the core re-deriving the cycle a moment later — by the time it did, it was a NOOP (no trading session, or a holiday). Nothing was fetched, nothing was decided.

**Action:** None. Priority `min` — same reason `succeeded` is: this exists so a missing push is distinguishable from a routine that correctly found nothing to do.

## Overnight cycle succeeded

*Exit-only, and only when a whole-share position exists to exit.*

**Meaning:** Nothing to report — every step ran, nothing was rejected, and either the routine didn't screen entries this cycle (Monitor/Post-close/Overnight) or it did and found nothing to open or close.

**Action:** None. Priority `min` — this exists so a missing push (a dead relay, a cycle that never ran) is distinguishable from a quiet market, not so you read it.

## Overnight cycle succeeded with warnings

*Exit-only, and only when a whole-share position exists to exit.*

**Meaning:** The cycle finished and placed/held what it should have, but the body carries at least one reconciliation finding, an expired `operator_holds` entry, or a play the agent switched on/off — worth a glance, nothing broke.

**Action:** Skim the body. If it's a play-state change or an `OPERATOR_HOLD_EXPIRED` line, no action is needed. If it's a reconciliation finding, see [How to read a runlog row](#how-to-read-a-runlog-row).

## Overnight cycle partial

*Exit-only, and only when a whole-share position exists to exit.*

**Meaning:** At least one order was refused by the broker, or an exit computed but couldn't place (a fractional quantity, a missing bid) and fell back to `Manual action required`.

**Action:** Read the refused-order line(s) in the body — they name the symbol, side and the broker's own rejection text. If it's the same rejection repeating, see [The stop/target rule](#the-stoptarget-rule) — a second resting order on a name that already has one is refused every time by design.

## Overnight cycle blocked

*Exit-only, and only when a whole-share position exists to exit.*

**Meaning:** Entries are blocked for this cycle or until cleared — either a broker order nobody's plan proposed (`PHANTOM_ORDER`), or the broker and the book have disagreed for enough cycles running that it's `until cleared`.

**Action:** One phantom order clears itself next cycle if it turns out to be ours. A persistent block does not — see [How to clear a block](#how-to-clear-a-block).

## Overnight cycle failed

*Exit-only, and only when a whole-share position exists to exit.*

**Meaning:** A step in the cycle raised — the failure's stage and the broker/tool error text are in the body verbatim, never paraphrased.

**Action:** Read the `<step> failed: <detail>` line. Most failures are transient (a rate limit, a stale schedule row) and the next scheduled trigger retries on its own; if the same step fails twice running, it's worth opening the runlog row for that `cycle_id`.

## Overnight cycle skipped

*Exit-only, and only when a whole-share position exists to exit.*

**Meaning:** The clock moved between `edge_should_run` saying this slot should fire and the core re-deriving the cycle a moment later — by the time it did, it was a NOOP (no trading session, or a holiday). Nothing was fetched, nothing was decided.

**Action:** None. Priority `min` — same reason `succeeded` is: this exists so a missing push is distinguishable from a routine that correctly found nothing to do.

### Fixed titles

## Drawdown halt

**Meaning:** The account has fallen 20% from its peak (§10). Both sides are
measured with deposits taken out, so funding or withdrawing moves neither; the
20% is of the whole account (the peak plus everything deposited), not of the
gain alone. Buys are denied account-wide; exits still place, including the
flatten this halt itself may trigger.

Recording a deposit late therefore corrects the peak in the same cycle and
cannot trip this alert by itself.

**Action:** None required to lift it — the halt clears itself the moment
equity clears the floor again. A deposit does not lift it: deposits are taken
out of both sides. There is no flag to reset. If it fired on a plausible equity read, treat it as a real drawdown;
if the equity number itself looks wrong, expect `Account read implausible`
instead (they never fire in the same cycle).

## Account read implausible

**Meaning:** The equity figure read from the broker doesn't cover the value
of open positions, or has collapsed against yesterday's benchmark row. This
outranks `Drawdown halt` — an unreliable read must not flatten the book.

**Action:** Buys are denied for the cycle; nothing is sold on this alone.
Check the broker account directly. If the read really is bad (an outage, a
stale cache), it should self-correct next cycle; if it recurs, something
upstream of `account.equity_total` is wrong.

## Weekly scorecard

**Meaning:** Friday's post-close summary — the one push that fires when
nothing is wrong. Money-weighted return net of deposits on the
deposit-adjusted base, plus realized P&L in dollars, against SPY and the
frozen (inception) book, per-play closed-trade counts, and (if any replayed
this week) shadow-variant comparison lines.

**Action:** None. Read it; it's the only thing this system says unprompted
when everything is fine.

## Benchmark integrity — unexplained equity move

**Meaning:** Equity moved between two daily benchmark rows by more than
recorded net deposits explain, on a day with no fill — `config/inception.json`
is likely missing a transfer, or the equity read on one of the two days was
wrong. §10's drawdown floor and the scorecard are both derived from this
series, so an unexplained gap here is worth resolving promptly.

**Action:** Reconcile the transfer history in `config/inception.json` against
your actual bank/brokerage transfers for the two days named in the body.

## Order refused

**Meaning:** One of the two titles a session/runner may hand-compose directly
at placement time, rather than through `report.py` — an order the broker
rejected, named and quoted verbatim, that didn't fit the routine end-of-cycle
push (e.g. it fires from inside placement before the cycle's `report.run()`
step).

**Action:** Read the broker's rejection text in the body. The most common
cause is [the stop/target rule](#the-stoptarget-rule) — a second resting
order on a name whose shares are already reserved by the first.

## Manual action required

**Meaning:** The other hand-composed title: an exit `exits.py` computed but
could not place at all — a fractional quantity, a missing bid — so the
system is asking you to place the limit order yourself.

**Action:** Place the named limit order manually at your broker. A
`FRACTIONAL` position has no protection between cycles until you do.

## Cycle failed

**Meaning:** The bootstrap/mid-abort fallback title — used when a cycle fails
before it even has a routine label to build `"<Routine> cycle failed"` from
(a checkout failure at Step 0), or on any mid-cycle abort that never reaches
the report step. Functionally the same as a generated `... cycle failed`,
just from a point in the cycle where the routine name isn't known yet.

**Action:** Same as any `... cycle failed` — see the failed-status rows above.

## Pre-market data job degraded

**Meaning:** Not a cycle push — the pre-market data job (`services/core/core/premarket.py`, 04:30-07:15 ET daily) had at least one Twelve Data fetch error on a **core** symbol this run (`priority: default`: the regime set, a held name, the Dip Recovery watchlist, a discovery candidate), or never finished a single step at all (`priority: high`). Errors on the DIP screen's breadth symbols are counted in the job result (`screen_errors`) and never alert. A core symbol may be missing its SMA/RSI/ATR cache for today.

**Action:** Read the body — it lists the Twelve Data error(s) verbatim, or the exception if the whole job failed. A `default`-priority partial degradation usually self-heals the next trading day (Twelve Data's own rate limit clearing); a `high`-priority full failure is worth checking `edge_premarket_state` for the current trading day.

## Cycle stalled

**Meaning:** Not a cycle push — `edge_step_watchdog()` (pg_cron, every minute) found a *running* cycle (the lock is held, the session is not finished) that has written no row for longer than its latest step's ceiling (`edge_step_kind.ceiling_s`; 15-20 min for most steps, 5 min inside the core's own phases). The body names the cycle, its type, the step it was last seen in and the ET time of the last row. `priority: high`, once per (cycle, step). Most often the runner is wedged inside a Robinhood fetch, where no table is written — this is the only thing that can see that.

**Action:** Run `select * from edge_cycle_timeline('<cycle_id>')` — `gap_before_s` shows the quiet stretch and which step preceded it. If the runner really is stuck, the 20-minute lock timeout lets the next trigger steal the lock and log the steal; nothing else needs doing.

## Cycle over budget

**Meaning:** Not a cycle push — the same watchdog: a cycle is still running past its wall-clock budget. The budgets are **full 40 min, monitor 20, dip-monitor 15, post-close 10, overnight 20, anything else 30** — `config.cycle_budget_min`, published to `edge_control` by the 04:30 ET schedule job (TUNING.md §1.9b). `priority: default`, once per cycle. A healthy full cycle takes about 10 minutes (monitor 5, post-close 4); an earlier cycle took 3 h 03 m because the DIP scan handed `resolve` 149 symbols instead of ~21.

**What the runner does about it, by itself:** the same number is returned to the runner on every `edge_cycle_await` and `edge_cycle_beat` as `budget_left_min`. Once it is negative the runner stops fetching `earnings` and `fundamentals`, posts what it has and goes straight to `decide`. Quotes and tradability are never cut, nothing is aborted, and exits and protection are placed as usual. A candidate whose earnings never arrived is simply not entered. So this alert on a full or monitor cycle means **entries may have been thinner than usual this cycle** — not that anything was left unprotected. **The Dip Monitor does not call `edge_cycle_await`**, so for it this alert is informational only; its runs take 8-13 minutes against a 15-minute budget, so expect it now and then. A Dip Monitor tick that stops part way raises `Cycle failed` through `edge_cycle_release` (before that an aborted tick left only a runlog row with `cycle_type = 'aborted'`, the build is now `python -m src.dip_assemble`).

**Action:** Check `edge_cycle_progress` for the current step and `resolved.symbols` on the `S2a.resolve` row of `edge_cycle_timeline` — a big symbol count is the usual cause. The runner's own beats (`S2.fetch`, `S2a.fetch.quotes` / `.tradability` / `.earnings` / `.fundamentals`, `S4.place`) show which Robinhood phase the time went to; `S2a.fetch.earnings` is one call per stale symbol and is the usual long one. To change a budget, edit `cycle_budget_min` in `config/config.json` (effective after the next 04:30 ET schedule run) or, for today only, `select edge_control_apply('{"cycle_budget_min": {"full": 60}}'::jsonb);`.

**The lock is no longer the runner's to renew.** Every row the runner posts and every beat renews `cycle_in_progress` server-side, so a long fetch can no longer outrun the 20-minute lock timeout the way it did several times before 12d. A lock that *does* go stale now means the runner has genuinely stopped — look at `Cycle stalled`.

## Step log gap

**Meaning:** A cycle finished, but the step log has no row for a step that should have left one (the body lists them). The finalize gate downgrades a clean cycle to *succeeded with warnings* and raises this once. The cycle's own actions are unaffected — this is about the *log*, not the trades. A missing step means a database trigger, a core stamp write or a runner step did not leave its mark; a trigger dropped by a later migration is the likeliest cause, and `tests/test_step_log_contract.py` exists to catch that in CI.

**Action:** `select * from edge_cycle_timeline('<cycle_id>')` to see what did land; then `select tgname, tgrelid::regclass from pg_trigger where tgname like 'edge_step_%'` — there should be eight rows (input, output insert+update, day_context, runlog, lock, stats insert+update).

## Resolve scope trimmed

**Meaning:** Not a cycle push — the hosted core's `resolve` phase found that the runner would have been asked to fetch live data for more symbols than `resolve_max_symbols` (`config/config.json`, default 50) and trimmed the excess (`priority: default`, once per cycle). Only discovery names are trimmed — DIP-scan-only names first, from the tail of the scan order, then event-driven names, oldest filing first. Held positions and sector ETFs are never trimmed. The body gives the before/after counts and how many of each kind went; the names and reasons are in that cycle's decide runlog row under `candidates_dropped`. A second variant says held positions and sector ETFs alone exceed the ceiling and nothing was trimmed.

**Why it exists:** the DIP scan returned 186 survivors and the 09:40 ET cycle fetched 149 symbols and ran 3 h 03 m. `dip_survivor_cap` (default 30) is the first line of defence and bounds the scan itself; this ceiling is the second.

**Action:** Usually none. If it fires every day, either the day's candidate set is genuinely wide (raise `resolve_max_symbols`) or the scan is (lower `dip_survivor_cap`). Nothing about held positions or their protection is affected.

## Which path owns DIP entry candidates

There are two separate ways a "dip" name can be bought, with **different candidate lists and different knobs** — the scope caps apply to only one of them.

| | Core DIP play (`src/plays/dip.py`) | Dip Recovery sleeve (`src/dip_recovery/`) |
|---|---|---|
| Candidate source | The DIP scanner's survivors (`cycle-prompt.md` Appendix C: RSI ≤ 30, above the 200-day EMA, volume, market cap), posted by the 09:40 ET full cycle as `dip_survivors` and merged into that cycle's `triggers` by `cli._merge_dip_survivors` | The day's watchlist written by **Universe Pick** (07:40 ET, three names, e.g. `BNY/JPM/STT`) into `dip_recovery_watchlist`, evaluated by the 11:47 / 12:47 / 18:00 dip-monitor triggers |
| Scan survivors used? | **Yes** — these are its entries (also the silent-entry streak and the mc prefilter) | **No** — it never reads `dip_survivors` |
| Bounded by | `dip_survivor_cap` (30, after the sector rotation, most-oversold-first), then `resolve_max_symbols` (50) | `dip_recovery_config.universe_thresholds.max_scan_survivors` (30) inside `watchlist.pool`, before its per-symbol fetch |
| Knob lives in | `config/config.json` (§1.11c of `docs/TUNING.md`) | `dip_recovery_config` in Supabase (§3 of `docs/TUNING.md`) |

They are deliberately not one knob. Changing `dip_survivor_cap` never changes what the sleeve evaluates, and changing `max_scan_survivors` never changes what the 09:40 cycle fetches. The core DIP play ranks survivors **most-oversold-first** before applying `dip_survivor_cap`, using the daily RSI the pre-market job caches in `day_context` (the same RSI-extremity term the play sizes on); ties and names with no cached RSI keep the scanner's order, which looked like market-cap order (BAC, MS, HD, RTX, PEP …) — an observation, not a guarantee the scanner makes. The sleeve ranks too, but **after** its fetch: on average daily dollar volume and 20-day ATR%; before its fetch it truncates in scan order.

## Where is the routine right now

Two queries, no cycle needs to be triggered:

```sql
-- the step every recent cycle is in / ended in, and how long it has been there
select cycle_id, cycle_type, current_step, seconds_in_step, last_beat_at,
       elapsed_min, rows_written, status, lock_held
  from edge_cycle_progress;

-- where one cycle's time went; gap_before_s is idle time before each step
select * from edge_cycle_timeline('<cycle_id>');
```

`step` on Telegram returns the running cycle's step and the ET time it entered it. The log is written by triggers on the tables the runner and the core write to and by the core's phase wrappers (`services/core/core/steps.py`), **not** by the runner: the runner prompt does not have to log anything and cannot forget to. Two things no table can see are covered by the watchdog above: Robinhood fetches (no row is written while they run) and `edge_cycle_dispatch` (an HTTP post — the core stamps the phase when the request arrives).

## What the runner fetches, and who writes the cache

- **`resolve` names three lists and the runner fetches only those.** `quotes`/`tradability` for every name in `symbols`; `earnings` for `stale`; `get_equity_fundamentals` for `fundamentals_needed` only. `fundamentals_needed` is the set of names that CATALYST's own event check fires on (`plays.catalyst.triggered`), held names included. With `play_dip_pullback_enabled` on, it is every symbol. On an earlier inputs that is 18 names instead of 149. CATALYST is shelved (`play_catalyst_enabled` is `false`), so as shipped the list is empty and no fundamentals are fetched.
- **Fundamentals rows carry five fields** (`symbol`, `volume`, `average_volume`, `sector`, `market_cap`). This is runner-prompt.md's one named exception to "paste verbatim".
- **The core writes `day_context`, not the runner** (`run._write_day_context`, in `decide`, after the decide runlog row). The write goes through `edge_day_context_write_symbol`. `earnings` and the top-level `fetched_at` are written only for `stale` names whose earnings were posted. `fundamentals_static` is written only for `fundamentals_needed` names, with exactly `average_volume`/`sector`/`market_cap` plus its own `fetched_at`. The core never writes `signals`. A failed write is a `warn` on `S2a.day_context` and never costs the plan.
- **Warnings are warn-only and counted into the cycle's status alert** as a `Runner fetch:` line. There are three kinds:
  - a fundamentals row outside `fundamentals_needed` (the core stamps `outside_need`);
  - a single-row `edge_cycle_input_write` of a market-data kind;
  - a batch over 200 rows or 200 KB.
  To make the batch rule refuse instead of warn, after a clean week: `insert into edge_control (key, value) values ('enforce_batch_2a', 'reject') on conflict (key) do update set value = excluded.value;`. To change the limits, add `max_batch_rows` / `max_batch_kb` rows the same way.

Check the cache after a live cycle. This is read-only, and every count should be 0:

```sql
select count(*) filter (where e.value ? 'fundamentals_static'
                          and not (e.value -> 'fundamentals_static') ? 'fetched_at')      as statics_without_stamp,
       count(*) filter (where e.value ? 'fundamentals_static'
                          and (select count(*) from jsonb_object_keys(e.value -> 'fundamentals_static')) > 4)
                                                                                        as statics_with_extra_keys,
       count(*) filter (where e.value ? 'signals' and e.value -> 'signals' = '{}'::jsonb) as emptied_signals
  from edge_day_context d, jsonb_each(d.market_data) e
 where d.trading_day = current_date;
```

---

## The `control` verbs

`ops/verbs.json` is the single source `ops/control.py` reads — it is the front
door for both the CLI and the `control.yml` GitHub Action. A plain on/off verb
(`master`, `core`, `dip`, `surge`) is added there once and the front door picks
it up.

| Verb | Effect |
|---|---|
| `status` | Read-only snapshot (`ops/status_view.py`). A headline (`Trading ON · entries open`), then **SWITCHES** (Trading (master), Core strategies, Dip Recovery, Entry block, each `Enabled`/`Disabled` or `On`/`Off`, with the reason under a block), then **ACCOUNT** (positions held, high-water mark, last full cycle, cycle running, book reconciled), then **ATTENTION** only when something needs it (trading off, a stale benchmark, the SURGE flag left on). Sectors are one count line; the list is `sectors`. No stored key names and no `True`/`False`. |
| `master on\|off` | `trading_enabled` — the absolute kill switch. |
| `core on\|off` | `core_strategies_enabled` — pause/resume entries by the core plays. It gates every buy the core cycle makes: as shipped that is DIP and the core sleeve (CATALYST and MACRO are shelved, PEAD and SURGE retired). Exits are unaffected. |
| `dip on\|off` | `dip_recovery_enabled` — pause/resume the Dip Recovery sleeve. |
| `block on [reason]` | Sets `entries_blocked` (+ reason), held until `block off`. |
| `block off` | Clears `entries_blocked` (+ reason, + until_cleared) only — see [How to clear a block](#how-to-clear-a-block) for why this is sometimes not enough. |
| `surge on confirm` \| `surge off` | `surge off` still clears the flag.** As it was: `full_send_enabled` — the all-in SURGE switch. Turning it **on** requires the literal third token `confirm` — `surge on` alone is rejected. Turning it off needs no confirmation. |
| `clear` | Resets `entries_blocked`/`entries_blocked_reason`/`entries_blocked_until_cleared` **and** `divergence_streak` to 0. |
| `last` | The last cycle's status and its status alert's footer (cycle id, ET time, routine, runlog ids, doc link). |
| `step` | Where the routine is right now: the newest cycle, the step it is in, since when (ET) and how long it has run, plus the time of its last row and the rows posted. Read from `edge_session_stats`'s step columns; available from the `ops/control.py` CLI. |
| `holds` | Lists every `operator_holds` entry (symbol, reason, set-on date, age). `holds expire <symbol>` removes one early. |
| any write (`master`, `core`, `dip`, `block`, `clear`, `surge off`, `holds expire`) | Replies with one line saying what changed (`✓ Dip Recovery: Disabled`) and the SWITCHES group, nothing else. A `sectors` write replies with `✓ Sectors updated` and the active sectors instead. |
| `sectors` \| `sectors set <codes>` \| `add` \| `remove` \| `clear` | `active_sectors` — what new money may go into (see `docs/TUNING.md` §2.1). **Codes**, comma-separated: sector codes (`EN`, `IT`, `RE`…), baskets (`SEMI`, `SOFT`, `BANK`, `INS`, `RESI`) or any of the 126 sub-industry codes in `docs/ROTATION_CODES.md` (`CHIP`, `DC`…). Full names and sleeve tickers (`XLE`) work too, case-insensitive; the code is what is stored. Bare `sectors` shows the set; `clear` means every sector. **Refused on purpose:** an unknown name (answered with the nearest match), `set` with nothing (use `clear`), `add` while every sector is active (it would narrow to one — use `set`), and `remove` of the last sector (empty means ALL — use `clear`). Held positions are never sold for it; they just get no new money. `status` shows the set. The bare `sectors` reply is the active codes, wrapped to the screen, then how to change them and the sector and basket codes. |
| `kill` | Alias for `master off`. Does **not** cancel the resting limit sells at the broker — they remain working. |
| `help` | This verb list. |

---

## How to read a runlog row

`edge_runlog` (one row per `cli` invocation, written before placement, always)
has:

- **`cycle_id`** — the id every alert footer and `edge_session_stats` row
  shares for the same cycle; use it to pull every row for one cycle's story.
- **`command`** — which module ran: `plan-fetch`, `decide`, `reconcile`,
  `report`, or `place` (the routine's own placement record, the one row no
  `cli` command emits).
- **`stdout`** — that module's `as_dict()` output, verbatim.
- **`placements`** — what was actually sent to the broker: `order_id`,
  `symbol`, `side`, `quantity`, `play_id`.
- **`rejections`** — the order and the broker's rejection text, verbatim,
  never paraphrased or bucketed into a code — the sentence is the diagnosis.
- **`book`** — the reconciled position book that cycle produced (null on a
  cycle that didn't reconcile).

## How to clear a block

Two verbs, two different scopes:

- **`block off`** clears `entries_blocked`, `entries_blocked_reason`
  and `entries_blocked_until_cleared` only.
- **`clear`** does all of that **and** resets `divergence_streak` to 0.

Use `clear`, not `block off`, after a `Full cycle blocked` /
`... cycle blocked` push whose body mentions a divergence streak —
`block off` alone leaves the streak standing, and the very next cycle that
still sees the same broker/book disagreement re-blocks immediately. A plain
`PHANTOM_ORDER` block (one cycle, no streak) clears itself either way.

## The stop/target rule

Only one resting order per position is ever live at the broker. Robinhood
reserves 100% of a position's shares against the first resting sell it
accepts, so a second one is refused "Not enough shares to sell" every time.

The stop is not an order. `plays/exits.py` checks the ATR stop once per cycle,
and only when a position is actually through it does the cycle cancel the
resting target, wait for the cancel, and place an ordinary limit sell
(`guard`'s `PROTECTION_CONTENDS` denial catches anything that tries to skip
that wait).

What this means in practice:

- A position can fall through its stop level between cycles and nothing sells
  it until the next cycle looks. That gap risk is the price of not being sold
  into a dip the position recovers from.
- The core sleeve has no stop check at all. Its only resting order is its
  target.
- A fractional position cannot carry a resting limit order. It has no resting
  target and is exited by the cycle (a market sell in regular hours, otherwise
  `Manual action required`).
- Do not place a stop order by hand on a position the system manages. It
  takes the shares the target needs, and the next cycle reports the
  disagreement.

## Where each secret lives

The full table is in [`SECRETS.md`](../SECRETS.md). In short:

- **A local `.env`** at the repo root (see `.env.example`), chmod 600, read by
  the offline `ops/` scripts: `EDGE_SUPABASE_URL` / `EDGE_SUPABASE_KEY`
  (service_role, bypasses RLS by design).
- **Supabase Vault**, read by SQL directly:
  `edge_telegram_bot_token`, `edge_telegram_chat_id` (the alert relay), and
  `edge_core_url`, `edge_core_token` (the dispatch functions that call the
  hosted core).
- **Vercel project env** (hosted core): `CORE_TOKEN`, `SUPABASE_URL`,
  `SUPABASE_SERVICE_ROLE_KEY`, `TWELVE_DATA_API_KEY`, and optionally
  `ALPHAVANTAGE_API_KEY`.
- **GitHub Actions secrets**: `EDGE_SUPABASE_URL`, `EDGE_SUPABASE_KEY`.
- **The Claude account**: the Robinhood and Supabase connectors. No broker
  credential is stored anywhere in this repo or on any server.

---

## First-run checklist

1. **Create a Supabase project.** Note its URL and service-role key.
2. **Run `db/schema.sql` once**, in the Supabase SQL Editor. It is the whole
   database in one file: one `begin; … commit;` block that enables `pg_cron` +
   `pg_net` and creates every table, function and seed row, followed by the
   `pg_cron` jobs.
3. **Set the Vault secrets** (`select vault.create_secret(...)`):
   `edge_telegram_bot_token`, `edge_telegram_chat_id`, and,
   once step 5 has given you a URL, `edge_core_url` and `edge_core_token`.
   The relay skips a channel whose secret is absent.
4. **Verify the relay end to end:**
   `insert into edge_alerts (title, body, priority) values ('Full cycle succeeded', 'relay test', 'default');`
   Expect it on Telegram within a minute.
5. **Deploy the hosted core to Vercel**, with the repository root as the
   project's Root Directory, and set `CORE_TOKEN`, `SUPABASE_URL`,
   `SUPABASE_SERVICE_ROLE_KEY` and `TWELVE_DATA_API_KEY`
   (`services/core/README.md`). `ALPHAVANTAGE_API_KEY` is optional.
6. **Add the GitHub Actions secrets** `EDGE_SUPABASE_URL` and
   `EDGE_SUPABASE_KEY`, then run the `Universe` and `Events` workflows once by
   hand so `data/` is populated.
7. **Replace `config/inception.json`** with your own starting book and
   deposit history, review `config/config.json`, commit and push.
8. **Create the Claude routines** in the [Current state](#current-state)
   table: Universe Pick 07:40, full 09:40, monitor 15:07, post-close 18:47,
   overnight 22:47, and the dip monitors at 11:47, 12:47 and 18:00 if you want
   the Dip Recovery sleeve. Crons are UTC. Each routine needs the Robinhood
   and Supabase connectors, and its instruction is to read its prompt file
   from a checkout of your fork (`docs/routine/runner-prompt.md`,
   `universe-pick-prompt.md` or `dip-monitor-prompt.md`) and follow it.
9. **Flip `trading_enabled` to `TRUE`** (the `master_on` verb, once every step
   above is verified) — the deliberate, final, operator-only go-live action.
   It seeds `FALSE`.
