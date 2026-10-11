# Routines

The scheduled Claude routines that run this system: eight that run every
trading day, and one, the Manual Check, that has no schedule and runs only when
you start it.

Times are US Eastern. The crons are UTC and carry no timezone field, so they
shift by an hour at each daylight-saving change (both sets are listed below).
The names carry Central time (ET − 1h).

## Before you create them

1. Finish steps 1 to 6 of the README's "Install and run". The routines need the
   database, the hosted core and your own `config/inception.json` in place.
2. Connect the **Robinhood** (Agentic Trading MCP) and **Supabase** connectors
   to your Claude account, and attach both to every routine below.
3. Give each routine access to your fork of this repository, so it can make the
   shallow clone its prompt file asks for.

## The instruction each routine carries

Every routine has the same short instruction. Only the prompt file changes.
Paste this, replacing the prompt file with the one in the routine's row:

```
Repository: https://github.com/<owner>/deterministic-trader (branch main).
Trading account: Robinhood cash account <your-cash-account-number>. Touch no other account.

Make a shallow clone of the repository, read docs/routine/runner-prompt.md
from that checkout, and follow it exactly. Do nothing that file does not ask for.
```

`scripts/install.py --apply` fills in `<owner>` and the account number here.
The prompt files hold every rule; the instruction only says which file to read
and which account to trade.

## Every trading day

| ET | Name | Cron, UTC (summer, EDT) | Cron, UTC (winter, EST) | Model | Prompt file |
|---|---|---|---|---|---|
| 07:40 | Deterministic Trader: Universe Pick + Dip Recovery 6:40 CT | `40 11 * * 1-5` | `40 12 * * 1-5` | Sonnet | `docs/routine/universe-pick-prompt.md` |
| 09:40 | Deterministic Trader: Core 8:40 CT | `40 13 * * 1-5` | `40 14 * * 1-5` | Sonnet | `docs/routine/runner-prompt.md` |
| 11:47 | Deterministic Trader: Dip Monitor 10:47 CT | `47 15 * * 1-5` | `47 16 * * 1-5` | Sonnet | `docs/routine/dip-monitor-prompt.md` |
| 12:47 | Deterministic Trader: Dip Monitor 11:47 CT | `47 16 * * 1-5` | `47 17 * * 1-5` | Sonnet | `docs/routine/dip-monitor-prompt.md` |
| 15:07 | Deterministic Trader: Core 14:07 CT | `7 19 * * 1-5` | `7 20 * * 1-5` | Sonnet | `docs/routine/runner-prompt.md` |
| 18:00 | Deterministic Trader: Dip Monitor 17:00 CT | `0 22 * * 1-5` | `0 23 * * 1-5` | Sonnet | `docs/routine/dip-monitor-prompt.md` |
| 18:47 | Deterministic Trader: Postclose 17:47 CT | `47 22 * * 1-5` | `47 23 * * 1-5` | Haiku | `docs/routine/runner-prompt.md` |
| 22:47 | Deterministic Trader: Overnight 21:47 CT | `47 2 * * 2-6` | `47 3 * * 2-6` | Sonnet | `docs/routine/runner-prompt.md` |

Use the summer column while US daylight-saving time is in effect (the second
Sunday of March to the first Sunday of November) and the winter column
otherwise. Change every routine's cron by hand on the day the clocks change.
The server-side jobs gate on Eastern wall-clock time and need no change.

What each one does:

- **Universe Pick (07:40).** Scans for the day's deep-dip candidates and writes
  the Dip Recovery watchlist that the dip monitors read. It places no orders.
- **Core 09:40, the full cycle.** The first trading cycle of the day. It
  reconciles the broker's positions and orders with the book, places exits,
  then screens and places new entries, including the core sleeve's buys.
- **Dip Monitor (11:47, 12:47, 18:00).** The Dip Recovery sleeve's own cycle.
  It checks the watchlist names against the sleeve's entry tests, manages the
  sleeve's open positions and places its limit orders. It shares the cycle
  lock with the core routines, so the two never run at once. The 18:00 slot
  is after the regular close on purpose; it trades the extended session. These
  three only matter while the sleeve is switched on (`dip_on`).
- **Core 15:07, the monitor cycle.** Re-checks exits and working orders in
  the afternoon. It screens new entries only when something has changed since
  the full cycle, and under the daily cap.
- **Postclose (18:47).** Reconciles the day, cancels unfilled entry orders and
  writes the daily benchmark row. On Fridays it sends the weekly scorecard.
  It places no entries.
- **Overnight (22:47).** Exits only. It runs when a whole-share position
  exists, so a reaction to after-hours news can be acted on before the next
  morning.

The four routines on `runner-prompt.md` are the same prompt. The hosted core
decides what kind of cycle a firing is from the clock, not from the routine's
name, so you can move a time and the firing is classified by when it ran.

The smallest working setup is the four `runner-prompt.md` routines. Add
Universe Pick and the three Dip Monitors when you switch the Dip Recovery
sleeve on.

## Manual, never scheduled

| Name | Schedule | Model | Prompt file |
|---|---|---|---|
| Deterministic Trader: Manual Check | none; started by hand with "Run now" | Sonnet | `docs/routine/manual-check-prompt.md` |

You start it at any hour, weekends included. It does not apply the
`SESSION_CLOSED` rule on a Saturday or Sunday; a market holiday is report-only.
It reads **every** holding (core's, the core ETF sleeve and Dip Recovery's) and
for each one either sells the whole position (at or above the Dip target, T+1
clear, never below cost) or averages down once (the Dip Recovery engine's own
trigger, drop, minutes and fill-cap rules, sized at one fifth of the deployable
cash at most). Every order passes `guard.evaluate`.

It claims the shared run lock, so it cannot overlap a scheduled cycle, and it
writes no position row, symbol lock or counter, so it changes nothing a
scheduled routine reads. It spends Claude usage like any run. The logic is
`src/manual_check.py`, the assembler `src/manual_assemble.py` and the command
`src.cli manual-check-decide`.

## The recovery path

`docs/routine/cycle-prompt.md` is a self-contained cycle that runs `src.cli` in
the routine's own checkout instead of calling the hosted core. No routine above
uses it. If the hosted core is unavailable, point the four core routines at it
instead of `runner-prompt.md`.

## Not routines

Three scheduled jobs run on the server and never appear in the routine list:

- **Pre-market data job**, 04:30 to 07:15 ET: `pg_cron` job `edge-premarket`
  calling the hosted core. It caches the day's SMA, RSI, ATR and regime.
- **State-change gate**, every 30 minutes from 10:00 to 15:30 ET: `pg_cron`
  job `edge-watch`. It decides whether a monitor slot has anything to do.
- **Schedule producer**, 04:30 ET: `pg_cron` job `edge-schedule`. It
  publishes the cycle slots that `edge_should_run` checks each firing against.
