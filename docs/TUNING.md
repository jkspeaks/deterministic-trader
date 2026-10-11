# TUNING.md — the command centre

Every knob you can turn to fit this system to your own trading style, in one
place. Three surfaces, in order of how often you touch them:

| Surface | What it is | Change takes effect | How you edit it |
|---|---|---|---|
| **1. `config/config.json`** | Static tuning — sizing, stops, the daily cap, exit discipline | **next cycle after you commit + push** | hand-edit the file in the repo |
| **2. `edge_control` table** | Runtime switches — the kill switch, entry blocks, per-name holds | next cycle, **no deploy** | a GitHub Action, the CLI, or SQL |
| **3. `dip_recovery_config` table** | The Dip Recovery sleeve's own knobs | next cycle, **no deploy** | the Action, the CLI, or SQL |

Anything not in one of those three is **code-level** (see the last section) and
needs a source change plus a review — by design.

---

## 1. `config/config.json` — static tuning

Grouped exactly as the file is. Values shown are the shipped defaults.

### 1.1 Position sizing — how many positions, how big

| Knob | Default | What it does | Turn it… |
|---|---|---|---|
| `min_position_size` | `200.0` | `n_target = clamp(round(equity / this), 3, 25)` | **up** → fewer, larger positions (each can hold a whole share of a dearer name); **down** → more, smaller slots. |
| `max_new_positions_per_day` | `3` | Cap on *fresh* entries per day (top-ups don't count) | **up** for more turnover, **down** to slow entry pace. |
| `cash_reserve_pct` | `0.02` | Fraction of equity held back from every cycle | **up** for a bigger dry-cash buffer. |
| `cash_reserve_floor` | `1.00` | Dollar floor under that reserve at low equity | rarely touched. |

### 1.1a Volatility-aware sizing — **off by default**

With `risk_sizing_enabled` true a candidate's notional
becomes

```
notional = min(slot notional, risk_per_trade_pct × equity / stop_distance_pct)
```

`stop_distance_pct` is the position's **own** stop — the play's ATR multiple
where it declares one (`SECTOR`, 1.5×; see §4) and `atr_stop_multiple`
otherwise — divided by the price the order will actually cross at, which is the
ask marked up by `entry_limit_offset_atr × ATR` (§1.2). One resolver,
`plays.core.stop_distance`, so the stop a position is sized against, the stop
that rests at the broker and the stop `exits` sells through cannot drift apart.

The even split asks *how many names does this account hold*; the risk budget
asks *how much is it willing to lose if this one is wrong*. It can only ever
**cut** a slot, never fund one past the ladder, and a candidate whose ATR never
arrived falls back to the even split rather than being refused.

| Knob | Default | What it does | Turn it… |
|---|---|---|---|
| `risk_sizing_enabled` | `false` | The switch. False is the ladder alone, which is every cycle the live account has ever run | leave **false** until `edge_shadow_trades` says otherwise — see §5. |
| `risk_per_trade_pct` | `0.01` | Fraction of equity risked to the stop on one entry | **down** to cut volatile names harder; **up** toward the plain even split. |

### 1.1b Portfolio construction and posture sizing — **off by default**

The ladder sizes one position; nothing asked what the *set* of new positions
looks like. `sizing_v2_enabled` adds the rules below, for **new money only**.

| Knob | Default | What it does |
|---|---|---|
| `sizing_v2_enabled` | `false` | The switch. False: sizing is the ladder, exactly, and `decide` records what these rules would have bought as `shadow_sizing`. Shadow-ledger variant `I_sizing_v2`. |
| `one_per_sector_day` | `true` | At most one **new** name per sector per trading day, counting names opened today and buys still resting. The second is unsized `ONE_PER_SECTOR_DAY`. |
| `reentry_block_days` | `14` | With `wash_sale_days`, **one** window: `max` of the two, read through `reconcile.reentry_block` by guard, sizing and the Dip Recovery watchlist. At 31 and 14 the wash-sale window already contains it. What the switch changes is that sizing refuses the name (`REENTRY_BLOCKED`) and gives the slot to the next candidate, rather than sizing it for guard to deny. |
| `corr_holdings_budget_fraction` | `0.5` | A new name in the same GICS sub-industry or the same sector as something held gets this fraction of its slot. **A proxy** — no return correlation is computed. |
| `dead_play_alert_days` | `5` | Not behind the switch. A play that has put no new name into the ranked list for this many consecutive trading days is named once in the status alert. Plays that are switched off or darked are not tracked. |

Also under the switch:

* **Share limit.** A whole-share order is capped at the shares that fit under
  the position cap; a name whose single share does not fit is unsized
  `SHARE_LIMIT` (it used to read `WHOLE_SHARE_ONLY`, the same words as a slot
  that is merely too small).
* **Posture.** The slot is multiplied by the regime's `size_multiplier` —
  once: if `regime_v2_enabled` already applied it in the plays, sizing does
  not apply it again. Total new deployment is capped so invested (the book at
  market plus resting buys) stays at or under `target_invested_pct`.
  `NO_NEW_POSITIONS` / `HOLD_CASH` size nothing (`POSTURE_NO_ENTRY`), and that
  idle cash is the intended outcome.
* **Top-ups** are exempt from the sector-day rule, the correlation rule and
  the multiplier — a second tranche is not a new position. The deployment cap
  does bound them: it is a ceiling on all new money.
* **Full-send (SURGE)** stands aside unless the posture is `TRADE_NORMALLY`.

On a recorded four-name cycle these rules buy three names and leave one
out — $360 committed where the ladder committed $577.

**The deployment record is not behind the switch.** Every entry-capable cycle
writes one `edge_deployment_decision` row, bought or not: posture, breadth,
the candidate funnel (universe > discovered > pool > triggered > passed plays >
passed regime > ranked > sized > approved > placed), per-reason skip counts,
cash, invested against the target, and `DEPLOY | PARTIAL | NONE` with a reason.
`intended` is true when cash was held for `REGIME_BLOCK`, `NO_CANDIDATES` or
`NO_CANDIDATES_PASS`; any other reason for holding cash is worth a look. The
status alert carries one line of it:

```
Deployment: NO_NEW_POSITIONS, breadth 27% · funnel 518 > 20 > 20 > 0 > 0 > 0 > 0 · NONE (REGIME_BLOCK)
```

A failed write of the row is named in that same alert and downgrades a clean
cycle to "succeeded with warnings"; it never costs the cycle its plan.

### 1.2 Order execution

| Knob | Default | What it does |
|---|---|---|
| `whole_shares_only` | `true` | Refuse any buy that can't afford one full share at basis (reported `WHOLE_SHARE_ONLY`, never routed fractional). Keeps every position sellable 24/5. Set `false` to allow fractional buys again — but fractional lots can only be *sold* in regular hours. |
| `whole_share_bonus` | `0.05` | Ranking tiebreak nudging the WHOLE path ahead. Not a quality override. |
| `min_order_notional` | `1.00` | The broker's own minimum order size. |
| `entry_blackout_minutes` | `30` | No new entries in the last N minutes before the close. |
| `entry_limit_offset_atr` | `0.10` | A WHOLE entry with no play-supplied limit is priced at `ask + this × ATR(14)`, rounded to the cent, so the limit is *marketable* and actually fills. Sizing budgets against this marked-up price, so guard's costing still agrees. No ATR on hand → falls back to the plain ask. **up** to fill more aggressively (worse price), **down** toward a passive ask. |

See also §1.11 for `entry_resubmit_after_min`, which retries an entry this offset did not get filled.

### 1.3 The circuit breaker

| Knob | Default | What it does | Turn it… |
|---|---|---|---|
| `drawdown_halt_pct` | `0.20` | Once the account has fallen this fraction from its peak, guard denies **every** buy and the book is flattened. The peak and the equity compared with it both have deposits taken out, so a transfer moves neither; the fraction is of the whole account (peak plus net deposits), not of the gain alone, so `floor = mark − this × (mark + net_deposits)`. With no deposits that is `(1 − this) × mark`. Lifts by itself when equity recovers. | **down** for a tighter leash, **up** to ride out deeper drawdowns. |

### 1.4 Exit discipline

| Knob | Default | What it does |
|---|---|---|
| `atr_stop_multiple` | `3.0` (raised from `2.0`) | Primary stop: `entry_price − N × ATR(14)`. Raised at the owner's direction after a run of stop-outs read as the stop being too tight for the account's actual risk tolerance — a wider fixed floor gives a position more room before the system cuts it, at the cost of a larger loss when the thesis really is wrong. Applies to every play that doesn't declare its own multiple (`stop_atr_multiple` on `PlayProfile`) — `SECTOR`'s 1.5× is unchanged by this. |
| `atr_trail_multiple` | `3.5` (raised from `2.5`, alongside `atr_stop_multiple`) | Trailing stop: `high_since_entry − N × ATR(14)`, measured from the position's own peak rather than its entry. The resting stop sits at whichever of the two is **higher** (tighter), so a position that has never been up is unaffected by the trailing leg and one that has run gives back at most this much of the move. **Must stay above `atr_stop_multiple`** — since `high_since_entry ≥ entry_price` always, `trailing ≥ fixed` exactly when `atr_trail_multiple ≥ atr_stop_multiple`, so if trailing ever drops to or below the fixed multiple, the trailing leg wins on *every* position, flat or not, and the fixed stop stops mattering at all. The two were raised together (`2.0`→`3.0`, `2.5`→`3.5`) to preserve the original 0.5x gap: a wider fixed floor after a run of stop-outs read as too tight, without inverting which leg governs a flat position. Lower it to bank gains sooner; raise it to give a trend more room. |
| `protection_rest_kind` | Do not change.** | Which **one** order rests at the broker per whole-share position. On `"target"` it is the take-profit: a `limit` **sell priced above the buy price** (the play's own target — DIP +6%, adopted +10%, …) and **nothing else**. **A resting stop is never placed:** resting stops sold positions into intraday dips they would have recovered from, at a loss. The stop-loss is a per-cycle *check*: `plays/exits.py`'s `ATR_STOP` is evaluated every cycle, and only when a position is actually through it does the cycle sell — with an ordinary limit order, after cancelling the resting target (the broker reserves 100% of a position's shares against the first resting sell, so the cancel has to go first; `cli._protection_orders` emits it). `"stop"` and `"none"` still exist in code for history; **setting `"stop"` violates the owner's rule.** `tests/test_profit_first_policy.py` pins the shipped value. |
| `protection_replace_min_pct` | `0.005` | The no-churn band. A resting stop or target is only cancelled and replaced once the price it *should* rest at has moved more than this. Without it a trailing stop that ratchets a cent a day would be replaced on every cycle — three windows a day per position in which nothing is resting at all. |
| `fractional_exit_market_ok` | `true` | When an exit fires on a fractional lot **in regular hours**, place a market sell (the broker's own fractional path) instead of routing it to `manual_required`. Set `false` to keep the hand-placement fallback for every fractional exit. Outside regular hours, or with no bid, a fractional exit still goes to `manual_required` regardless. |
| `entry_price_divergence_pct` | `0.01` | How far the anchored entry price, the broker's `average_buy_price` and the tax-lot weighted basis may disagree before the position is **disputed**: no ATR stop, no time stop, no target, and no protective order placed, replaced or cancelled behind it. **One gap is not a dispute:** a tax-lot basis *above* the anchor by no more than the measured losses `edge_loss_closes` holds on the symbol within 30 calendar days of any open lot (+$0.05 rounding) is the IRS wash-sale adjustment — the broker adds the disallowed loss to the replacement lot's `tax_cost_basis` and says so nowhere in the payload. The anchor stands, recorded as `entry_price_source: wash_sale_adjusted`. Without it a wash-sale-adjusted position sits with no stop at all. Do not raise the tolerance to cover that case — it would also cover the bad reads the dispute exists for. |
| `max_adopted_hold_sessions` | `30` | Caps §6.5's adopted-position "a time stop never realises a loss" exemption. After this many sessions of the adopted clock the exemption ends and an underwater inherited name time-stops out, so a stranded slot is eventually freed. Raise it to hold inherited losers longer, lower it to rotate them sooner. |
| `atr_secondary_stop_multiple` | `3.5` | Kept so an old runlog row parses; nothing reads it. |
| `average_down_min_drop_pct` / `average_down_min_sessions_since_fill` / `average_down_max_open_lots` | `2.5` / `1` / `4` | Kept so an old runlog row parses; nothing reads them. |

### 1.5 The all-in switch — SURGE (`§6.4a`)

**Retired (§1.17).** SURGE proposes nothing whatever the switch says, and `surge on confirm` is refused with a message saying so (`retired` in `ops/verbs.json`). `surge off` still clears the flag. What follows describes the switch as it was.

Not a `config.json` knob — `full_send_enabled` lives in **`edge_control`** so it's
flippable at runtime (`surge_on`/`surge_off`) with no deploy. See §2.

### 1.6 Soft pre-trade gates

| Knob | Default | What it does |
|---|---|---|
| `crowding_penalty` | `0.10` | Rank penalty per existing position in a candidate's sector — the gradient before the hard sector cap. |
| `mc_stop_probability_max` | `0.35` | Deny a buy whose Monte Carlo path hits its stop more than this fraction of the time before target. Only bites when the routine wires `ctx["mc_check"]`. |
| `mc_drawdown_p95_max` | `0.15` | Deny a buy whose worst-5%-of-paths drawdown exceeds this. Same opt-in. |

### 1.7 Event windows

| Knob | Default | What it does |
|---|---|---|
| `earnings_flatten_days` | `2` | No entry within N sessions of a confirmed report; a held name is flattened as the window opens. |
| `wash_sale_days` | `31` | A name closed at a **loss** is not repurchased for N days. Account-level (shared with the Dip Recovery sleeve). |
| `wash_sale_block_unknown_basis` | `true` | Whether a close the system could not price (`loss_amount: null`, `basis_unknown: true`) also blocks. `true` is §9's fail-closed fallback; set `false` only once the broker's realized read is confirmed live and the null rows are known to be historical. |

**This is a repurchase policy knob, not a tax computation.** The IRS rule runs
30 days *before* **and** 30 days after a loss sale — 61 days — on substantially
identical securities. The code looks only forward, and only at an exact symbol
match, so a shorter window here does not make the account non-compliant and a
longer one does not make it compliant; Robinhood reports wash sales on the
1099-B either way. What the knob actually buys is avoided bookkeeping, at the
cost of every stopped name being untradeable for a month.

`reconcile` now reads the broker's own per-fill figure first; the fallback and
this knob are what remain for the fills it cannot match.

**Same-day close and re-entry are intentionally covered, not excluded.**
`guard._wash_sale_denials` (`src/guard/schedule.py`) measures `age = today -
close_date` and denies at `age <= wash_sale_days`, so a loss closed *today*
has `age == 0` and blocks a repurchase in the same session — there was never
a same-day carve-out in the predicate itself. An earlier audit found a
loss pass a same-cycle re-entry buy ~10 minutes later
with `guard.denied: []`. The cause was not the date math: it was that
`decide`'s `ctx["loss_closes"]` came only from the Step 6 `loss_closes` fetch
(`docs/routine/cycle-prompt.md`), which runs *before* Step 7's `reconcile` —
so a loss `reconcile` realizes this same cycle is written to
`edge_loss_closes` after the fetch already ran, and `decide` never saw it
until the *next* cycle's fetch caught up. Fixed the same day by having
`cli._loss_closes_for_guard` union the fetched rows with
`reconcile.loss_closes` (this cycle's own, freshly computed) before handing
the list to the guard — see `tests/test_cli_decide.py`'s
`test_a_loss_reconcile_writes_this_cycle_still_blocks_a_same_cycle_rebuy`.

### 1.8 The overnight session gate

| Knob | Default | What it does |
|---|---|---|
| `overnight_max_spread_pct` | `0.01` | An exit only places outside regular hours if the spread is within this; otherwise it waits for the open. |
| `catastrophe_multiple` | `3.0` | Lets an exit pay up to N × the spread to escape a position already far past its stop. |

### 1.9 Plumbing — rarely touched

| Knob | Default | What it does |
|---|---|---|
| `settled_cash_policy` | `"buying_power_is_settled"` | Trusts `buying_power` as spendable (measured true on this cash account). Set `"conservative"` if the account ever stops being a cash account. |
| `universe_max_age_days` | `14` | Past this many missed weekly universe fetches, the cycle trades held positions only and places no entries. |
| `reconcile_epsilon_quantity` | `1e-6` | Quantity tolerance so fractional rounding isn't read as divergence. |
| `reconcile_epsilon_notional` | `0.01` | Same, for notional. |
| `monitor_cadence_minutes` | `30` | How far apart the 10:00–15:30 ET monitor slots are in the schedule the 04:30 ET producer publishes (`services/core/core/schedule.py` → `edge_cycle_schedule`). At 30 the band is 12 slots; at 60 it is 6. **A cost knob, not a rule about what a monitor may do** — `edge_should_run`'s state-change gate already turns most slots into a free `run: false`, so raising this does not make the system trade less, it makes it look less often. Takes effect on the next 04:30 run, not the next cycle. |

### 1.9b Cycle budgets — how long a cycle may run

** Wall-clock minutes per cycle type. One number does two jobs: past it the
runner is told to stop fetching the optional data, and the watchdog raises
`Cycle over budget`.

| Knob | Default | What it does |
|---|---|---|
| `cycle_budget_min.full` | `40` | The 09:40 full cycle. |
| `cycle_budget_min.monitor` | `20` | Each monitor cycle. |
| `cycle_budget_min.dip-monitor` | `15` | The Dip Monitor. **Alert only** — that routine does not call `edge_cycle_await`, so nothing is cut. Its runs take 8-13 minutes; expect the alert occasionally, or raise this. |
| `cycle_budget_min.postclose` | `10` | The post-close cycle. |
| `cycle_budget_min.overnight` | `20` | The overnight cycle. |
| *(any other type)* | `30` | Not configurable per type; the SQL default. |

**What "over budget" does.** Every `edge_cycle_await` and `edge_cycle_beat`
the runner makes returns `elapsed_min` and `budget_left_min`. Below zero, the
runner skips Step 2a's `earnings` and `fundamentals`, posts what it holds and
goes to `decide`. **Quotes and tradability are never cut** (a held position
with no quote cannot have its exit priced), **nothing is aborted**, and exits
and protection are placed as usual. A candidate with no earnings row is not
entered. Raise a budget to let a slow morning finish fetching; lower it to cap
what a cycle can cost.

**It takes effect the next morning, not the next cycle.** Postgres cannot read
`config/`, so the 04:30 ET schedule job publishes this map to
`edge_control.cycle_budget_min`. For today only:
`select edge_control_apply('{"cycle_budget_min": {"full": 60}}'::jsonb);`
(the next 04:30 run overwrites it with the file's values). A type missing from
the published row falls back to the SQL default in `db/schema.sql`.

**What bounds the Robinhood calls** is not this — it is how many names a cycle
looks at: `dip_survivor_cap` and `resolve_max_symbols` (§1.11c), the Dip
Recovery pool's `max_scan_survivors` (§3), and the batch sizes in
`runner-prompt.md` Step 2a (quotes ≤20 a call, tradability and fundamentals
≤10, earnings one per stale symbol). At `resolve_max_symbols` 50 a full cycle
is at most 3 quote calls, 5 tradability calls and — on the first cycle of the
day, when every name's earnings are stale — up to 50 earnings calls. The
runner's own database statements are fixed at about 23 a cycle whatever the
symbol count (`max_batch_rows` / `max_batch_kb`, §2, police the batches).

### 1.10 The regime — what kind of tape the system will trade

** `src/regime.py` reads SPY and the eleven sector ETFs each entry cycle and
answers two questions: what kind of market this is, and which sectors are off
the board today. It replaces the `regime_fit` number a premarket session used
to write by hand — that producer died and nothing noticed for a week, because a
stale number and a considered one are the same JSON.

| Knob | Default | What it does | Turn it… |
|---|---|---|---|
| `regime_vol_max` | `0.35` | Second leg of the no-entry condition: SPY **below** its 200-day **and** 20-day annualized realized vol above this reads `risk_off`, which darks every play for the day. Below the 200-day in a quieter tape is just a correction and entries continue. | **up** to keep trading through volatile corrections, **down** to stand down sooner. |
| `sector_rsi_peak` | `70.0` | A sector ETF over this RSI(14) is `peak`. Its names are dropped from the candidate set *before* the per-symbol fetch. | **down** to avoid more stretched sectors, **up** to keep buying into strength. |
| `sector_breakdown_pct` | `0.05` | A sector ETF more than this fraction below its own 200-day is `breakdown`, and its names are dropped the same way. Inside the band it is `oversold_ok` and still tradeable — the ordinary pullback DIP exists for. | **down** for a stricter structural filter, **up** to keep buying dips in weakening sectors. |

Three states come out of it and nothing else does:

* **`risk_on`** — SPY above both its 50- and 200-day. Every play runs, SURGE
  included. **This is the only tape SURGE's all-in mode runs in**, and it is
  the only regime gate that play has ever had (§2 below).
* **`neutral`** — trading normally; SURGE is dark.
* **`risk_off`** — no entries. Exits and the drawdown flatten are untouched.

**No regime inputs means no regime**, and that reads as *full weight on every
play*, not as dark. The old rule was the opposite and it is what let a dead
producer trade a frozen 0.4 for a week; the plays' own gates, `guard` and
`sizing` are all still in front of every order.

`edge_play_state.regime_fit` survives as the operator's per-play kill switch
and nothing else: an explicit `0.0` darks that play (and takes a MACRO position
out, §6.4's regime flip). Any other value in that column is ignored.

### 1.10b Breadth, the five postures and DIP's sector rule — **off by default**

This group adds a breadth reading and a five-state **posture** beside
`market_state`. It is **computed on every cycle and enforced only when
`regime_v2_enabled` is true**. It gates **new entries only**: exits, the
resting take-profit, the per-cycle stop check and top-ups of working positions
never pass through it.

| Knob | Default | What it does |
|---|---|---|
| `regime_v2_enabled` | `false` | The switch. False: the plan is the old rule's, and `decide` records `shadow_posture`, `shadow_dip_would_block` and `shadow_entries` beside it. True: enforced, and `shadow_entries` records what the old rule would have entered. Shadow-ledger variant `G_regime_v2`. |
| `regime_breadth_min_sectors` | `8` | Fewer sector ETFs than this with readable data and breadth is `unavailable` — no breadth rule fires, and `regime.notes` says so. |
| `posture_no_new_breadth_200` | `0.30` | Breadth below this is `NO_NEW_POSITIONS`. At 0.25 that day reads `HIGH_CONVICTION_ONLY`. |
| `posture_high_conviction_breadth_200` | `0.45` | Breadth below this is `HIGH_CONVICTION_ONLY`. |
| `posture_high_conviction_breadth_50` | `0.40` | SPY under its 50-day **and** 50-day breadth below this is also `HIGH_CONVICTION_ONLY`. |
| `posture_normal_breadth_200` | `0.60` | Breadth below this is `REDUCED_SIZE`. |
| `posture_reduced_rv20` | `0.20` | SPY 20-day realized vol at or over this is `REDUCED_SIZE`. |
| `posture_high_conviction_rsp_lag_1m` | `0.02` | RSP's 1-month return more than this **below** SPY's (`rsp_vs_spy_1m < -0.02`) is `HIGH_CONVICTION_ONLY`. Exactly 2 points behind does not trip it. |
| `posture_no_new_drawdown_52w` / `posture_no_new_drawdown_rv20` | `0.08` / `0.25` | SPY more than the first figure off its 52-week high (`drawdown_52w < -0.08`; the input is signed, zero or negative) **and** realized vol over the second is `NO_NEW_POSITIONS`. Both legs are required. |
| `posture_size_multiplier` | `1.0 / 0.5 / 0.5 / 0 / 0` | Multiplies each new entry's size, per posture. |
| `posture_target_invested_pct` | `0.80 / 0.50 / 0.30 / 0 / 0` | Recorded on the regime for P3's deployment cap. **Caps nothing yet.** |

The posture, most restrictive first:

* **`HOLD_CASH`** — the `risk_off` tape of §1.10.
* **`NO_NEW_POSITIONS`** — SPY below its 200-day, or breadth under the floor.
  Every play's candidate is skipped `SKIP_REGIME`, naming the posture and the
  breadth.
* **`HIGH_CONVICTION_ONLY`** — DIP additionally needs its sector in the upper
  half of the eleven on 1M/3M return vs SPY (`SKIP_SECTOR_RS_WEAK`), and an
  unmeasurable ranking is `SKIP_RS_UNAVAILABLE`, not a pass. CATALYST, PEAD and
  MACRO pass. Half size.
* **`REDUCED_SIZE`** — everything passes at half size. Also set by a macro
  release day (a row in `data/macro.json` first seen today) and by SPY under
  its 50-day.
* **`TRADE_NORMALLY`** — the only posture SURGE runs in.

**DIP's sector rule** applies in every posture when the switch is on: a name
is bought only if its sector ETF is `trend_ok` (`last >= sma200`, not at a
peak). `oversold_ok` is still shown in the sector table and still not a
discovery drop for any play — it just stops being a buy permission for DIP
(`SKIP_SECTOR_BELOW_200D`).

**Where the inputs come from.** The pre-market job derives all of it from the
daily bars it already fetches: each sector's 50-day and 1M/3M return, SPY's,
SPY's drawdown from the highest close of the trailing year (needs 240+ closes;
it fetches 250), and RSP's 1-month return against SPY's. RSP is the one added
symbol — it rides the existing batch and costs one Twelve Data credit a day.
An `edge_regime` row written by an older version has none of these, so on it `breadth_50` is null,
relative strength is unavailable and neither optional rule fires.

**The Dip Recovery sleeve reads the same regime, through the same functions,
under the same switch** (`regime.dip_entry_verdict`; codes identical to core
DIP's). The Dip Monitor posts the day's `regime_inputs` and
`dip-recovery-decide` gates, on **new money only**:

| Posture | A new sleeve entry | An average-down |
|---|---|---|
| `HOLD_CASH`, `NO_NEW_POSITIONS` | `SKIP_REGIME`, not evaluated | `SKIP_REGIME`; the position is still managed and exits normally |
| `HIGH_CONVICTION_ONLY` | sector at or above its 200-day **and** in the upper half on 1M/3M strength; half size. No ranking → `SKIP_RS_UNAVAILABLE` | untouched |
| `REDUCED_SIZE` | sector at or above its 200-day **and** not in the weakest third (`SKIP_SECTOR_RS_WEAK`); half size. No ranking → that leg is not applied | untouched |
| `TRADE_NORMALLY` | sector at or above its 200-day; the sleeve's own filters as before | untouched |

The weakest-third rule in `REDUCED_SIZE` is the sleeve's only: core DIP in that
posture is sector trend plus half size. With the switch off the sleeve places
exactly what it placed before and records `shadow_posture`,
`shadow_would_skip` and `shadow_would_resize` on its runlog row. A tick that
posts no `regime_inputs` has no regime and is not gated.

### 1.10c `_10c_dip_regime_table` — what DIP may do in each posture, **off by default**

§1.10b answers "may DIP buy in
this posture, at what size, under which sector tests" in code. This group
restates those answers as one row per posture, so a change is a config edit.
**As shipped every row is exactly the §1.10b rule: switching it on changes
nothing until a row is edited.** No row had enough evidence to change.

It is read only when `regime_v2_enabled` **and** `dip_regime_table_enabled`
are both true, and only for **new core DIP entries**. Exits, the resting
take-profit, the per-cycle stop check, top-ups, every other play and the Dip
Recovery sleeve never read it.

| Knob | Default | What it does |
|---|---|---|
| `dip_regime_table_enabled` | `false` | The switch. Needs `regime_v2_enabled` as well; alone it does nothing. |
| `dip_regime_table.<POSTURE>.dip_allowed` | `true / true / true / false / false` | False refuses every new DIP in that posture (`SKIP_REGIME`). |
| `...dip_size_multiplier` | `1.0 / 0.5 / 0.5 / 0 / 0` | Multiplies the entry's size. Must be in 0..1: a row lowers a size and never raises one above the ladder. Zero is the same as not allowed. |
| `...require_sector_above_200d` | `true` everywhere | §1.10b's sector rule: the name's sector ETF must be `trend_ok` (at or above its own 200-day, not at a peak). False drops the rule for that posture only. |
| `...require_sector_rs_upper_half` | `true` in `HIGH_CONVICTION_ONLY`, else `false` | The sector must rank in the upper half on 1M/3M strength against SPY. No reading refuses (`SKIP_RS_UNAVAILABLE`). |
| `...max_rsi` | `null` | A number refuses a dip whose RSI is above it (`SKIP_DIP_TABLE_RSI`); a dip with no RSI reading is refused too. `null` is no ceiling. |

Defaults are listed in the order `TRADE_NORMALLY`, `REDUCED_SIZE`,
`HIGH_CONVICTION_ONLY`, `NO_NEW_POSITIONS`, `HOLD_CASH`.

What no row can do:

* **Open `HOLD_CASH`.** The risk-off tape darks every play before the table is
  read, and the loader refuses a row that sets `dip_allowed` there.
* **Raise a size.** The loader refuses a multiplier over 1.
* **Touch the sleeve or another play.** A CATALYST candidate in
  `NO_NEW_POSITIONS` is refused whatever the DIP row says.
* **Override `sizing_v2_enabled`'s deployment cap.** With that switch on,
  `posture_target_invested_pct` still bounds new money, so a row that opens
  `NO_NEW_POSITIONS` does nothing while that posture's target is 0.

A missing table, row or field falls back to the §1.10b value field by field.
A mistyped posture name, field name or value stops the config from loading.

**Shadow.** Whichever way the switches are set, `decide` records
`shadow_dip_table`: the day's row as read, whether it differs from the §1.10b
rule, and each DIP candidate the two would treat differently with its size
multiplier under each (`null` = not entered). With the shipped table
`would_change` is always empty.

### 1.11 Entry funnel

Mirrors the `_11_entry_funnel` group in `config.json`. `entry_limit_offset_atr`
is documented in §1.2 with the rest of order execution; the other two are here.

| Knob | Default | What it does |
|---|---|---|
| `dip_trend_band_pct` | `0.03` | Widens DIP's 200-day gate: a name qualifies at `price ≥ SMA200 × (1 − this)`. A strict `price > SMA200` rejected every ordinary 2–3% pullback as a "falling knife". It is a play-shape number in config **by exception**. **up** for more DIP candidates on a soft tape; **`0.0`** restores the old strict rule. |
| `entry_resubmit_after_min` | `30` | An entry limit still unfilled after this many minutes, on a cycle where entries are still allowed, is cancelled at `reconcile` and its symbol re-queued for the next monitor cycle's `candidates` (`control_updates.resubmit_candidates`). **down** to chase fills harder; **up** (or very large) to leave a slow fill alone until the post-close sweep. |

### 1.11c Scope caps — how many names one cycle pulls live data for

Mirrors the `_11c_scope_caps` group in `config.json`. **Why they exist:** the DIP scan returned 186 survivors, 128 survived the sector rotation, and the 09:40 ET cycle had the runner fetch quotes, tradability, fundamentals and earnings for **149 symbols** (21–22 is normal) — 3 h 03 m and ~11% of the weekly usage window for an empty plan. The core's own work took ten seconds; the cost is the runner fetching and posting one symbol's data at a time.

| Knob | Default | What it does | Turn it… |
|---|---|---|---|
| `dip_survivor_cap` | `30` | The most DIP scanner survivors one cycle carries into discovery. Applied **after** the sector rotation and **ranked most-oversold-first**: the pre-market job caches each screened name's daily RSI in `day_context`, and ascending RSI is the same RSI-extremity term `plays/dip.py` sizes conviction on (`(30 − RSI) / 15`). The play's other term, depth below the 20-day, needs a live price, which does not exist before the fetch this cap bounds. Ties, and names with no cached RSI (a scan hit outside the screened universe), keep the scanner's own order, after every name that has one. The overflow is named in the decide runlog's `candidates_dropped` with its RSI (`outside the N-name DIP survivor cap, RSI 24.0, ranked most oversold first`). **Not** the Dip Recovery sleeve's `max_scan_survivors` (§3): this list feeds the **core** DIP play (entries, the silent-entry streak, the mc prefilter), a different strategy with its own knob. | **down** for cheaper cycles (fewer oversold names weighed); **up** to scan wider on a deep sell-off. |
| `resolve_max_symbols` | `50` | The ceiling on everything `resolve` hands the runner. Held positions and sector ETFs are **never** trimmed; only discovery names, DIP-scan-only names first (tail of the scan), then event triggers (oldest filing first). One `Resolve scope trimmed` alert whenever anything is cut. `decide` applies the same trim so it never weighs a name the runner was not asked to fetch. | **up** if the alert fires daily on a genuinely wide day; **down** to cap cost harder. |

Both must be positive integers; `cli._validate_config` refuses anything else rather than letting a typo fall back to the default unnoticed.

**The most expensive fetch is narrowed inside that ceiling.** `get_equity_fundamentals` is fetched only for `resolve`'s `fundamentals_needed`, the names CATALYST's event check fires on, and not for every resolved symbol. This is not a knob. Its only switch is `play_dip_pullback_enabled` (§1.11a): with that on, DIP reads `average_volume` for any candidate, so every symbol is fetched again. See `docs/OPERATIONS.md`, "What the runner fetches, and who writes the cache".

### 1.11a `_11_play_dip` — the pullback reshape, **off by default**

**This is the one place a play's shape lives in config, and it is an explicit
exception to §17.** See §4 below for the exception itself and why it was
granted; this section is the knobs.

With `play_dip_pullback_enabled` true, `DIP` stops being §6.2 (RSI ≤ 30 plus
the 200-day band, conviction from depth below the 20-day) and becomes
a *shallow* give-back inside an intact trend, with
liquidity gates in front of it and a target stated in stop distances rather
than as a flat percentage. Six gates, each named in its own refusal so the
shadow ledger can say which one cost a candidate.

| Knob | Default | What it does | Turn it… |
|---|---|---|---|
| `play_dip_pullback_enabled` | `false` | The switch. False is §6.2, unchanged | leave **false** until `edge_shadow_trades` says otherwise (variant `E_dip_pullback`). |
| `dip_pullback_rsi_max` | `35.0` | The oversold ceiling — looser than §6.2's 30, the one direction the reshape widens | **down** toward §6.2's own trigger. |
| `dip_pullback_sma50_band_pct` | `0.05` | Price must sit inside `−this…0%` of the 50-day. **Both edges bind**: over the 50-day there is no pullback to buy, far under it the trend has already broken | **down** for shallower give-backs only; **up** to accept deeper ones (and toward the falling knives §6.2 buys). |
| `dip_pullback_min_adv_usd` | `20000000.0` | Average dollar volume floor (`average_volume × last`). A name with no `average_volume` on file is **refused**, not assumed liquid | **up** to trade only the most liquid names. |
| `dip_pullback_max_spread_pct` | `0.002` | Quoted spread ceiling, off the mid. Distinct from the no-trade gate's 0.3% account-wide gate and deliberately tighter — that gate is a condition on the day, this is a condition of the play | **down** to pay less away on entry, at the cost of candidates. |
| `dip_pullback_target_stop_multiple` | `1.5` | The target, as a multiple of the stop distance. At a 2×ATR stop this is a 3×ATR target | **up** for a wider target and fewer target exits. |

The 200-day band is **not** a new key: A's "above SMA200, band −3% allowed" is
`dip_trend_band_pct` (§1.11) at exactly its shipped value.

A missing ATR falls back to `DIP`'s flat 6% target rather than to no target at
all — an R-multiple needs an ATR, and cancelling a resting broker-side limit
over a data gap is the worse failure. The runlog's owner string says which was
used (`DIP 1.5R` against `DIP`).

### 1.11b `play_sector_enabled` — the sector rotation play, **off by default**

One key, and one key only. Unlike the pullback reshape above, this play keeps its shape in
code per §17 (`src/plays/sector.py`, listed in §4): the ETF's RSI ≤ 30 while it
holds its own 200-day and SPY's RSI is over 40; exit at RSI ≥ 55 or +4%, stop
1.5×ATR, time stop 10 sessions.

| Knob | Default | What it does | Turn it… |
|---|---|---|---|
| `play_sector_enabled` | `false` | The switch. False and the play proposes nothing at all — not even a skip | leave **false** until `edge_shadow_trades` says otherwise (variant `F_sector_play`). |

Two things to know before turning it on:

* **It is deliberately absent from `regime.PLAY_IDS`, so a risk-off tape does
  not dark it.** Its stated market condition is literally "Any". That is
  the most aggressive thing about the play and it is part of what the ledger is
  measuring.
* Turning it on also changes **discovery**: `src/candidates.py` adds the
  qualifying ETFs to the cycle's `macro_targets`, so their quotes and ATR are
  actually fetched. Up to eleven extra per-symbol fetches on a day when every
  sector is oversold at once, though in practice a sector at RSI ≤ 30 while
  still above its 200-day is a rare pair.

### 1.11d Relative volume, time-normalised — **off by default**

Two gates compared today's volume **so far** with a **full day's** average, so
neither could pass early in the session:

* **CATALYST** needs a 3% move on 1.5x volume. At the 09:40 cycle 1.5x of a
  full day is unreachable — HPE was +7.0% at 09:44 on 0.16x and was refused.
  CATALYST has never bought.
* **The Dip Recovery sleeve** needs `relvol_min` (1.2x) at its 11:47 and 12:47
  ET ticks. The sleeve has never opened a position.

Under each switch the multiple is scaled to a full session before the same
threshold is applied:

```
rvol_tnorm = (volume / average_volume) x (session_minutes / minutes_elapsed)
```

`session_minutes` is 390, or 210 on a half-day. `minutes_elapsed` runs from
09:30 ET to the cycle's own clock and is clamped to the session, so after the
close the reading is the raw one. The cycle's clock is when it started; the
volume is fetched a few minutes later, so the scaled figure runs slightly
generous, which is one reason for the minimum below.

| Knob | Default | What it does |
|---|---|---|
| `catalyst_rvol_time_normalised` | `false` | CATALYST's 1.5x is applied to `rvol_tnorm`. Shadow-ledger variant `H_catalyst_rvol_tnorm`. |
| `catalyst_min_minutes` | `30` | Before this minute CATALYST refuses with `SKIP_TOO_EARLY_FOR_RVOL` rather than extrapolate — a 14-minute print scaled 28x is noise. **At 30 the 09:40 cycle still cannot fire CATALYST; the monitor cycles can.** |
| `dip_recovery_relvol_time_normalised` | `false` | The sleeve's `relvol_min` is applied to `rvol_tnorm`. **Turning this on is what would let the sleeve trade for the first time.** |
| `dip_recovery_relvol_min_minutes` | `30` | The same floor, for the sleeve. |

Whichever way the switches are set, both figures are recorded: every CATALYST
verdict carries `factors` (`rvol_raw`, `rvol_tnorm`, `minutes_elapsed`, and
what each rule would have said), and every sleeve relvol rejection and
proposal carries the time-normalised reading beside the raw one. With no
clock in the context (`would_open`, `mc_prefilter`) the raw rule decides.

**Reason codes** (these apply regardless of the switches and change no
decision). Missing data is never reported as a failed test:

| Play | Code | Meaning |
|---|---|---|
| CATALYST | `SKIP_NO_CHANGE` | no last trade or previous close on the quote |
| CATALYST | `SKIP_NO_VOLUME` | volume or its average missing or zero |
| CATALYST | `SKIP_BELOW_MIN_MOVE` | a real move, under 3% |
| CATALYST | `SKIP_BELOW_RVOL` | raw multiple under 1.5x (switch off, or no clock) |
| CATALYST | `SKIP_BELOW_RVOL_TNORM` | time-normalised multiple under 1.5x |
| CATALYST / sleeve | `SKIP_TOO_EARLY_FOR_RVOL` | before the minimum minute |
| PEAD | `SKIP_NO_REACTION_ROW` | the reaction session has closed and no row exists — reported once per cycle in the status alert |
| PEAD | `SKIP_REACTION_PENDING` | the reaction session has not closed yet (a `pm` report on its first session) — expected, not reported |
| PEAD | `SKIP_NO_SURPRISE` | no EPS surprise figure |
| PEAD | `SKIP_NO_TREND_DATA` | no last price or no 50-day |

A PEAD name that has every input and fails a threshold keeps
`ENTRY_CONDITIONS_UNMET`, and its message names the clause that failed.

**Not changed: when catalysts are evaluated.** The alternative to a
30-minute floor is to stop asking the 09:40 cycle and evaluate CATALYST at
12:47 / 15:07 only. That gives a far better volume reading (a third to
three-quarters of the session) and costs the first hours of the move — on a
gap-and-go the entry is later and higher. The schedule is untouched here.

### 1.12 The 30-minute state-change gate

`services/core/api/watch.py`. Runs every 30
minutes in market hours on one Twelve Data quote batch for held symbols; sets
`edge_control.needs_session` instead of running the full core, and
`edge_should_run` skips a monitor slot entirely
when the key is null and there is nothing else to do.

| Knob | Default | What it does |
|---|---|---|
| `watch_band_pct` | `0.01` | A held whole-share position's live quote within this fraction of its `edge_protection` stop or target price sets `needs_session`, as does a whole-share position with no resting protection row at all. **down** for fewer, later wake-ups (cheaper, more reaction lag near a level); **up** to wake earlier. |

---

### 1.13 Near-flat exits — `_14_near_flat_exit` (annotation only)

| Knob | Default | What it does |
|---|---|---|
| `near_flat_max_dollars` | `1.00` | A closing trade is **near-flat** when its realized gain is under this in dollars **and** under `near_flat_max_pct` of its cost basis (`quantity × price − realized_gain`). Both, not either: with OR, every modest winner on an account this size would flag. |
| `near_flat_max_pct` | `0.001` | The basis half of the same test. |

Changes no decision — nothing is denied, sized or exited on it. Each close is
recorded once in `edge_realized_trades` by `edge_realized_trades_append`,
which is also, the only writer of `edge_play_state.realized_pnl` (it read 0.0 for
every play until then — the old writer credited from the session's hand-built
`fills`, which the runner never sends). The weekly scorecard shows `DIP: 4 closed,
-$14.39 (1 near-flat)` and, when a close that week came off a disputed entry
basis, a `Data-quality:` line naming it. Loosen either knob to flag more; set
`near_flat_max_dollars` to `0` to switch the annotation off.

### 1.15 `_15_core_sleeve` — idle cash in one broad ETF, **on**

Code: `src/core_sleeve.py`.

| Key | Shipped | What it does |
|---|---|---|
| `core_sleeve_enabled` | `true` | The switch, turned on by the operator. Set it to `false` and nothing more is bought; a sleeve already held keeps its resting limit sell and still has no stop. |
| `core_symbol` | `SPYM` | The ETF. The same index as SPY at about a ninth of the price, so whole shares fit this account. |
| `core_cap_pct` | `0.50` | The most of equity the sleeve may hold. Guard enforces it (`CORE_CAP_EXCEEDED`). |
| `core_buffer_slots` | `1` | Settled cash kept back for the plays, in position slots, on top of the cash reserve. |
| `core_target_pct` | `0.20` | The sleeve's one resting order: a limit sell this far above its buy price. Must be above zero. |

**What happens each entry-capable cycle with the switch on.** The plays are sized first, exactly as before. From the settled cash they leave, the reserve and the buffer are kept back. The rest buys whole shares of the ETF until the sleeve reaches its cap.

**What it never does.**

- **No stop of any kind.** No resting stop, no per-cycle ATR check, no time stop. A sleeve down 40% is held. Its only exits are its resting target, the account's drawdown halt and a non-reliance 8-K (which an index ETF does not file).
- **No funding sale.** It never sells to raise cash for a play. The cap is what leaves cash for them.
- **No play slot.** It is outside the slot count and the position and sector caps; its own cap replaces them.

**What stays the same for it.** Settled cash only. The 31-day loss re-entry window. The drawdown halt, on both sides: no buy while it is breached, and the sleeve is flattened with the rest of the book. Whole shares only.

**What switching it on changes for the plays.** They are sized from settled cash. Once the sleeve has taken its share, a play has the buffer and whatever a sale has freed. At the shipped values on a $1,400 account that is about one slot at a time. If DIP is meant to keep trading at its present pace beside the sleeve, raise `core_buffer_slots` or lower `core_cap_pct` first. The recommendation is to stop DIP entries; that switch is `dip_entries_enabled` (1.16).

**The form measured.** The nearest comparison arms were CS9 (the sleeve with no plays: +31.6% from 2024, -9.8% drawdown) and CS7 (no buffer, plays starved). The exact shipped form, a one-slot buffer with no funding sale and DIP still on, was not replayed.

**How to see what it would do before switching it on.** Every `decide` records a `core_sleeve` block: the sleeve's value, its cap, the cash after the plays, what is kept back, and `would_deploy`. With the switch off the ETF is not quoted, so there is a dollar figure and no share count.

**How the ETF gets its data.** While the switch is on, `cli.candidates` adds one trigger row for it, which is what puts it in the runner's fetch list and inside guard's candidate fence. The row is protected from the resolve ceiling. No play reads it.

**Checked before it was switched on.** The broker quotes `SPYM` two-sided (91.23 / 91.25) and answers its earnings lookup with an empty list, which the cycle reads as "no date" and the sleeve's buy does not need. `services/core/tests/test_core_sleeve_runner.py` runs that through the runner's resolve and decide on the shipped file. Not checked from here: the account's own tradability answer for `SPYM`, which needs the account number. The first cycle's `tradability` row and the `core_sleeve` block's `placed` field show it; a refusal reads as `NOT_TRADABLE` on the order and nothing is bought.

### 1.16 `_16_dip_entries` — the switch that stops DIP's new money, **on (DIP still buying) as shipped**

| Key | Shipped | What it does |
|---|---|---|
| `dip_entries_enabled` | `true` | `true` is today's behaviour. Only an explicit `false` stops DIP; an absent key reads as `true`. Anything that is not `true` or `false` stops the config from loading. |

**What `false` stops.**

- New DIP positions and top-ups of held DIP positions. Each is recorded as skipped, with this key named as the reason.
- The fetch work that only feeds DIP. `cli.candidates` carries no DIP row, so the runner fetches no quote, tradability, earnings or fundamentals for the scanner's survivors. The count set aside is recorded as `dip_entries_off`.
- The "DIP has produced no candidates" notice and DIP's place in the dead-play count, since the silence is asked for.

**What it does not touch.**

- A DIP position already held: its exits, its resting limit sell above its buy price and its per-cycle check are exactly as before.
- Which play `reconcile` binds an inherited name to.
- Every other play, and the core sleeve.
- The Dip Recovery sleeve. That is a separate strategy with its own switch, `dip_recovery_enabled`, in the database.

**What it does not save.** The session still runs the scan itself (one `run_scan` call) and posts the result, because the routine's fetch list is written in the database schedule and not read from this file. The core ignores what it posts.

**A row in `edge_play_state` cannot turn DIP back on over it.** The config key is read first.

### 1.17 `_17_shelved_plays` — PEAD and SURGE retired, CATALYST and MACRO shelved

Code: `plays.play_off` in `src/plays/core.py`.

| Play | State | How it is held off | To bring it back |
|---|---|---|---|
| PEAD | Retired | In code (`plays.RETIRED`). No key. | A code change, with a new test of the play behind it. |
| SURGE | Retired | In code (`plays.RETIRED`). No key. `full_send_enabled` no longer does anything and `surge on` is refused. | The same. |
| CATALYST | Shelved | `play_catalyst_enabled`, shipped `false`. | Set the key to `true`. |
| MACRO | Shelved | `play_macro_enabled`, shipped `false`. | Set the key to `true`. |

Only an explicit `true` brings a shelved play back. An absent key reads as off. Anything that is not `true` or `false` stops the config from loading. A row in `edge_play_state` cannot bring any of the four back.

**What off stops.**

- New positions and top-ups under the play. Nothing is proposed, so nothing is recorded as skipped either: the runlog no longer lists the names these plays looked at and declined.
- Discovery. `candidates.assemble` returns no PEAD or CATALYST trigger and no MACRO sector ETF, so the runner fetches no quote, tradability, earnings or fundamentals for them and the pre-market job does not pre-fetch them. A watchlist or resubmit row written earlier the same day is dropped and counted as `plays_off_dropped`.
- The pre-market earnings-reaction fetch, a number only PEAD read.
- The play's place in the dead-play count.
- Binding an inherited position to the play. A name the operator buys by hand is managed as `ADOPTED` or `DIP`.

**What it does not touch.**

- A position already held under one of the four. Its exits and its one resting limit sell above its buy price are unchanged; `plays.PLAYS` still declares all four plays' terms.
- DIP, the core sleeve and the Dip Recovery sleeve.
- The catalyst-confirmed target exit and the restatement veto. Both read the events file and apply to every play.

**A side effect on DIP.** Every discovered name was offered to every play, so a PEAD or CATALYST trigger was also looked at by DIP. DIP now sees the scanner's survivors and the held book only. The scanner covers the same universe on the same RSI rule, so a name DIP would buy should already be among its survivors.

**Not removed.** `src/plays/pead.py` and `src/plays/surge.py` stay on disk: `ops/backtest_engine.py` runs them to reproduce the verdict. The Events workflow still runs, because the exit and the veto above read its file. The SURGE paths in `sizing` and `guard` are unreachable and were left alone.

## 2. `edge_control` — runtime switches (no deploy)

Flip these from the **Manual Control Action** (`workflow_dispatch`), the
`ops/control.py` **CLI**, or **SQL**
(`select edge_control_apply('{"key": "value"}'::jsonb)`). `ops/control.py`
is the front door for all three; `status` prints the current values.

| Key | Verb | What it does |
|---|---|---|
| `trading_enabled` | `master on\|off` (`kill` = off) | The absolute kill switch. `FALSE` → nothing places on either strategy, every cycle logs `halted` and stops. The operator's field alone; nothing in `src/` ever writes it. |
| `full_send_enabled` | `surge on\|off` | **No effect: SURGE is retired (§1.17); `surge on` is refused, `surge off` works.** As it was: `§6.4a`'s SURGE all-in mode. When `TRUE`, a name in a clean stacked-MA uptrend (`price > 20d > 50d > 200d`, RSI 55–80, `price ≥ 2%` over the 50-day — gate in `plays/surge.py`) lets the cycle commit the **entire available settled cash** across up to 3 such names, split by conviction, **ignoring the slot / position / sector caps** (`§4.4`). Every other guard check still runs (drawdown halt, earnings and wash-sale windows, tradability, settled cash, the daily new-position cap). That is the first regime gate this play has ever had: it used to read an absent `edge_play_state` row as 1.0, so the riskiest play in the system was the one the dead premarket producer left running at full weight. Default `FALSE` — inert until flipped; once on, the uptrend gate and the regime decide. |
| `entries_blocked` + `entries_blocked_reason` | `block on [reason]` / `block off` | Block **new entries** while still managing and exiting what's held. |
| `entries_blocked_until_cleared` | (set by `block on`) | Sticky — stays on until `block off` (used after a divergence). |
| `operator_holds` | SQL only | A `{"<symbol>": {"reason": "...", "set_on": "<day>"}}` map you edit by hand. A listed symbol skips the drawdown-flatten exit for that name only — everything else still applies. |
| `high_water_mark` | SQL only | `§10`'s ratcheting peak, maintained by reconcile. You can reset it by hand (e.g. after a deliberate strategy change) so the drawdown floor re-bases. |
| `active_sectors` | `sectors set\|add\|remove\|clear <names>` (bare `sectors` shows it) | The sector rotation — which GICS sectors new money may go into. `[]` (the default) = every sector. See §2.1. |
| `enforce_batch_2a` | SQL only | The batch rule. Absent (the default) or anything but `reject` = **warn**: a single-row `edge_cycle_input_write` of `quotes`/`tradability`/`fundamentals`/`earnings`/`signals`, or a batch over the limits below, goes through and is counted into the cycle's status alert (`Runner fetch:` line). `reject` = that write raises. Flip it only after a clean week — a refused post aborts the cycle with nothing placed. |
| `cycle_budget_min` | *(published by the 04:30 schedule job from `config.cycle_budget_min`)* | Wall-clock budgets per cycle type, as `{type: minutes}`. Read by `edge_cycle_budget_min()`; a manual write lasts until the next 04:30 run. See §1.9b. |
| `max_batch_rows` / `max_batch_kb` | SQL only | The batch limits `enforce_batch_2a` checks. Defaults **200 rows / 200 KB** when the rows are absent. The prompt's draft said 60 rows, but a 50-symbol resolve (`resolve_max_symbols`) posts ~50 rows per kind, and the 19:10 monitor (21 symbols) posted 63 rows / 61 KB. **down** only together with `resolve_max_symbols`. |

### 2.1 `active_sectors` — the sector rotation

**What it does.** Limits *new money* to the GICS sectors you name. A name in
any other sector is never proposed, never fetched for, and never bought — and
a position you already hold in such a sector gets no top-up and no Dip
Recovery average-down. `[]` means every sector is active (the default, and
byte-identical to the system before this existed).

**Vocabulary (codes).** Short codes from `config/rotation_codes.json`
— full table in `docs/ROTATION_CODES.md`:

* **11 sector codes** — `EN` `FIN` `IT` `HC` `IND` `CD` `CS` `UTL` `MAT` `RE` `COM`;
* **126 GICS sub-industry codes** — every sub-industry in the S&P 500, e.g.
  `CHIP` Semiconductors, `SME` Semiconductor Materials & Equipment, `APPS`
  Application Software, `DC` Data Center REITs, `TOWR` Telecom Tower REITs,
  `PAY` Transaction & Payment Processing, `BIO` Biotechnology;
* **5 baskets** — `SEMI` (CHIP+SME), `SOFT` (APPS+SYSW), `BANK`, `INS`, `RESI`.

A stock passes when its **sector or its sub-industry** is active, so
`SEMI, SOFT, DC` trades only semiconductors, software and data-center REITs.
Full GICS names ("Health Care", "Semiconductors") and sleeve tickers (`XLE`)
are accepted too, case-insensitive; what is stored is the code. A **sector ETF**
(XLK…) passes only when its whole sector is active. **Broad Market**
(SPY/QQQ/IWM/DIA) is never filtered — the benchmark expression, not a bet.
Membership comes from the Wikipedia S&P 500 table the weekly Universe job
fetches (GICS quirk: FSLR sits under Semiconductors). A stock whose
sub-industry is not yet known (a brand-new constituent) is kept, not dropped;
`decide.json`'s `rotation.sub_industry_data` is false when the snapshot has no
sub-industries at all.

**Rotate.** From the Manual Control Action or SQL: `sectors set SEMI, SOFT, DC`,
`sectors add EN`, `sectors remove DC`, `sectors clear`
(see `docs/OPERATIONS.md` for what is refused and why). Or by SQL:

```sql
select edge_control_apply('{"active_sectors": ["SEMI", "SOFT", "DC"]}'::jsonb);
select edge_control_apply('{"active_sectors": []}'::jsonb);   -- all sectors again
```

**What it affects:** PEAD and CATALYST discovery (filtered *before* the
20-name cap), MACRO tilts and the sector rotation play's oversold sector-ETF entries (an XLF
target is a Financials bet), DIP scanner survivors, reconcile's resubmitted
entries, the Dip Recovery pool (filtered *before* `max_scan_survivors`) and its
entry gate, and top-ups / average-downs on held names. Every name dropped is
recorded with its reason in `candidates_dropped`; every `decide.json` carries
`rotation: {active_sectors, unmatched, filtering}`.

**What it never affects:** held positions' protection and exits (no forced
exit, ever — Invariant 7), and Broad Market ETFs.

**Narrowing vs widening mid-day.** A narrowing applies from the next cycle.
A widening does not bring back names the 09:40 `full` cycle (or Universe
Pick) already dropped — those return at the next `full` / Universe Pick.

**Typos fail open, loudly.** Entries that match no sector are listed in
`decide.json`'s `rotation.unmatched`. If *every* entry is unmatched the
rotation filters nothing (`filtering: false`) rather than freezing entries on
a typo. A symbol with no known sector (today only CBOE) is never filtered.
If the weekly universe fetch ever publishes a sector outside the eleven, the
Universe workflow's "Check the sector vocabulary" step goes red
(`ci/check_sectors.py`) — the rotation would otherwise silently not filter
that sector's names.

**Premarket credits and the prefetch.** The pre-market data job (04:30-07:15
ET, one ≤8-symbol Twelve Data step every 2 minutes) plans the day once and
freezes the plan (`services/core/core/premarket.py`, `_tick`):

1. **Core, always fetched:** SPY and the eleven sector ETFs, every held name,
   the Dip Recovery watchlist, PEAD-window names, and today's PEAD/CATALYST
   discovery candidates (the same `candidates.assemble` the 09:40 cycle runs,
   rotation and 20-name cap included).
2. **DIP screen, fills what the cap leaves:** every universe member the
   active rotation admits, heaviest S&P weight first — so a DIP scanner
   survivor found later in the day already has its RSI/ATR cached.

Each symbol costs **1 credit** (250 daily bars; the 200-day SMA is computed
from them, so there is no separate SMA call). The job stops at **560 credits
a day** (`DAILY_CREDIT_CAP`, 70% of the plan's 800): an all-sectors day uses
about 530; any narrower rotation far less. If the cap binds, the screen is
truncated from the lightest names and the day still runs. Screen symbols
Twelve Data cannot serve are counted in the job result, not alerted. The plan
is frozen at 04:30, so a mid-day rotation cannot reclaim credits already
planned for that day.

The other `edge_control` rows (`cycle_in_progress`, `positions_held`, the two
clocks, the dedupe stamps, …) are machine state — read them for status, don't
hand-edit them.

---

## 3. `dip_recovery_config` — the Dip Recovery sleeve

Key/value shaped like `edge_control`. Same three front doors
(`edge_dip_recovery_config_apply` SQL function, or the `dip_on`/`dip_off` verb for the enable flag).
Schema and seeds: `db/schema.sql`. Defaults:

### 3.1 Flags

| Key | Default | What it does |
|---|---|---|
| `core_strategies_enabled` | `TRUE` | Master enable for the four core plays' entries. |
| `dip_recovery_enabled` | `FALSE` | Master enable for the Dip Recovery sleeve's entries. |

### 3.2 `entry_thresholds`

| Key | Default | What it does |
|---|---|---|
| `rsi_max` | `35` | Oversold ceiling to qualify. |
| `relvol_min` | `1.2` | Minimum relative volume. |
| `dip_score_min` | `70` | The 0–100 deterministic dip score a candidate must clear. |
| `min_dip_pct_intraday` | `2.0` | Price must be ≥ this % below the intraday high. |
| `min_dip_pct_5day` | `4.0` | …and ≥ this % below the 5-day high. |
| `bollinger_period` / `bollinger_k` | `20` / `2.0` | Lower-band definition; price must sit at or under it. |
| `trend_band_pct` | `0.10` | ** Price may sit up to this fraction **below** the 200-day and still qualify — the relaxed structural filter. Lower it toward `0` to demand a strict uptrend. |
| `freefall_drop_pct` | `15.0` | ** A drop of ≥ this % from the close `freefall_window_sessions` bars back is a crash, not a dip — vetoed regardless of the band. |
| `freefall_window_sessions` | `10` | The lookback for that drop, in daily bars. |

### 3.3 `sizing_thresholds`

| Key | Default | What it does |
|---|---|---|
| `max_active_positions` | `5` | Concurrent Dip Recovery positions. |
| `max_new_entries_per_day` | `3` | Fresh Dip Recovery entries per day. |
| `max_fills_before_stop_averaging` | `4` | Add-down cap per position. |
| `average_down_trigger_pct` | `-3.0` | How far underwater before an add is considered. |
| `average_down_min_price_drop_pct` | `2.5` | Minimum drop off the last fill for the next add. |
| `average_down_min_minutes_since_fill` | `30` | Cooldown between adds. |

### 3.4 `exit_thresholds`

| Key | Default | What it does |
|---|---|---|
| `profit_target_pct` | `5.0` | The locked-in net profit a whole position exits at. |
| `cost_buffer_pct` | `0.25` | Execution-cost cushion added to the target price. |

### 3.5 `universe_thresholds` / `concentration`

`universe_thresholds`: `watchlist_size` (5), `min_dollar_volume` (20M),
`min_atr_pct` / `max_atr_pct` (1.5 / 8.0), `max_scan_survivors` (30).
`concentration`: `position_cap_pct` (0.24), `sector_cap_pct` (0.30).

`max_hold_trading_days` (47) and `scan_interval_minutes` (90) are top-level keys
in the same table.

---

## 4. Code-level thresholds — a source change **and** a review

`§17` makes a play's *shape* the play's own property: its trigger, its windows,
its conviction scale. Changing one is a strategy change, not a knob twist, so
it deliberately does not live in any config surface. They are listed here so
you know they exist and where to find them — not so you edit them lightly.

### The one §17 exception: `_11_play_dip`

**The pullback reshape's thresholds live in `config/config.json`, not in
`src/plays/dip.py`.** Every threshold of the pullback reshape —
`dip_pullback_rsi_max`, `dip_pullback_sma50_band_pct`,
`dip_pullback_min_adv_usd`, `dip_pullback_max_spread_pct`,
`dip_pullback_target_stop_multiple` — is a config key (§1.11a). That is a
deliberate departure from the rule this section opens with, and it is recorded
here rather than left to be discovered.

**The reason is the owner's UI goal.** The intent is a control surface that
tunes a strategy without a deploy, and the pullback reshape is the first strategy shaped for
it: six numeric gates, each independently meaningful, on a play that ships
switched off and is meant to be *searched* against the shadow ledger rather
than reasoned into position. §17's rule exists so a play's shape cannot be
retuned without a code review; a play whose entire purpose is to be retuned
against measured evidence is the case that rule was not written for.

**What the exception does not buy.** It is scoped to this one group. The sector
rotation play shipped in the same commit and keeps its numbers in `src/plays/sector.py`,
because it is a fixed hypothesis and a tunable version of it would be a
different hypothesis the shadow evidence would not be about. Nothing else in
the table below moves on the strength of this precedent, and a second exception
needs its own paragraph here.

Two safety properties hold regardless. Each key is read with a module constant
as its fallback, so the `would_open` / `mc_prefilter` path —
which carries no config at all — gates identically to `decide`. And the whole
group is inert while `play_dip_pullback_enabled` is false, which is what ships.

| Where | Controls |
|---|---|
| `src/plays/surge.py` | The SURGE uptrend gate: `SURGE_RSI_MIN` 55, `SURGE_RSI_MAX` 80, `SURGE_MOMENTUM_MIN` 0.02 (price ≥ 2% over the 50-day), `SURGE_CONVICTION_FULL_SCALE` 0.15, `SURGE_EARNINGS_CLEARANCE_SESSIONS` 3. The documented loosening lever if pick counts run low is dropping `SURGE_RSI_MIN` toward a stacked-MAs-only gate. |
| `src/plays/core.py` | `SURGE_MAX_NAMES` 3 (names one full-send cycle spreads across); `SURGE_HOLDING_DAYS` 7 / `SURGE_TARGET_PCT` 0.08; **`DIP_TARGET_PCT` 0.06 and `MACRO_TARGET_PCT` 0.05** (both plays declared *no* target until then, so neither could sell up except on its own signal, and a dip that recovered without reaching RSI 50 rode the round trip back down); the `PLAYS` table's per-play holding periods and targets; `ADOPTED_HOLDING_DAYS` 15 / `ADOPTED_TARGET_PCT` 0.10 (a backstop with nothing left to back up — every play declares a target; the adopted no-loss time-stop exemption is now capped by `max_adopted_hold_sessions` in `config.json`, §1.4); `CATALYST_PROVEN_TRADES` 20 / `CATALYST_UNPROVEN_MULTIPLIER` 0.75. `LOSS_HOLD_MAX_DAY` / `LOSS_HOLD_EXTENDED_MAX_DAY` are **deprecated** (the news-hold was removed) and read by nothing. |
| `src/plays/pead.py` | PEAD: window sessions `(1, 3)`, `MIN_REACTION_PCT` 0.02, surprise/reaction full-scale. |
| `src/plays/dip.py` | DIP: `RSI_TRIGGER` 30, `RSI_EXIT` 50, `DEPTH_FULL_SCALE` 0.10, `RSI_EXTREMITY_FULL_SCALE` 15, earnings clearance 3. `DIP_TREND_BAND_PCT` 0.03 is here **only as the fallback** for the `would_open`/`mc_prefilter` path, which carries no config — the live gate is the `dip_trend_band_pct` config knob (§1.11). |
| `src/plays/catalyst.py` | CATALYST: window 2, `MIN_REACTION_PCT` 0.03, `MIN_VOLUME_MULTIPLE` 1.5, `CLUSTER_HEADLINES` 3. |
| `src/plays/macro.py` | MACRO: window 2, `SURPRISE_FULL_SCALE_PCT` 0.25, the 200-day trend confirmation. |
| `src/plays/sector.py` | SECTOR (off by default): `SECTOR_RSI_MAX` 30, `SPY_RSI_MIN` 40, `SECTOR_RSI_FULL_SCALE` 15, `SECTOR_MIN_CONVICTION` 0.2 (what an ETF sitting exactly on the RSI 30 line is worth — without it the boundary case of the play's own trigger would be dropped as NO_CONVICTION). Its exit terms are in `core.py`'s `PLAYS` row: `SECTOR_HOLDING_DAYS` 10, `SECTOR_TARGET_PCT` 0.04, `SECTOR_STOP_ATR_MULTIPLE` 1.5, `SECTOR_RSI_EXIT` 55. Only the on/off switch is config (§1.11b). |
| `src/plays/dip.py` | **Deliberately empty** — the pullback reshape's thresholds are config by the exception above. What stays in code is the shape of the conviction scale (`DIP_PULLBACK_RSI_EXTREMITY_FULL_SCALE` 20) and the fact that there are six gates rather than five. |
| `src/sizing.py` | `MIN_POSITIONS` 3, `MIN_POSITION_WEIGHT` 0.04 (the 25-name ceiling), the concentration-cap formulas (`§4.4`). `RISK_PER_TRADE_PCT_DEFAULT` 0.01 is the fallback for the config key in §1.1a, not a separate number. |
| `src/report.py` | `§1`'s 24%/year performance bar. |
| `config/macro_map.json` | The macro-release → sector-tilt map (a config *file*, but it defines MACRO's behaviour, so revise it deliberately). |

After changing any of these: run `uv run pytest`, regenerate whatever golden
files move (`tests/golden/**`), and treat it as a reviewed strategy change.

---

## 5. Shadow-only overrides — the keys that ship in the off position

A shadow ledger is the **precondition for strategy
changes**, and `services/core/core/shadow.py` is it: a replay of the stored
cycles in `edge_cycle_input` against named rule variants, where a variant is a
config override handed to the same `plays`/`guard`/`sizing` modules the live
account runs — never a second copy of a play.

**Three keys are deliberately absent from `config/config.json`.** Each is read
with a default that is exactly today's behaviour, so the shipped rule *is* the
key being missing.

**Four more are present and set false**, because they switch on whole plays and
a group of numbers that ship with them — an absent switch would leave the
thresholds in the file with nothing saying they are inert.
`play_dip_pullback_enabled` (§1.11a), `play_sector_enabled` (§1.11b) and
`risk_sizing_enabled` (§1.1a) are the switches; `risk_per_trade_pct` is a
budget that only bites when its switch is on.

Setting any of these seven moves the live account onto a rule the ledger exists
to test first. Do not, until `edge_shadow_trades` says so.

| Key | Default (absent) | The override, and what it tests |
|---|---|---|
| `crowding_mode` | `"tiebreak"` — §6.0's crowding penalty is a `_rank_key` term only | `"score"` subtracts it from the score, the retired reading that could erase a candidate outright. Variant `B_crowding_score` |
| `force_exit_items` | `["4.02"]` — non-reliance only | `["4.02", "5.02"]` restores the forced exit on an officer/director 8-K, to measure what dropping it was worth. Variant `C_hard_event_502` |
| `no_trade_conditions` | `false` — the no-trade gate is off | `true` switches on `src/plays/no_trade.py`: SPY under its 200-day with vol over 1.5x its own median, a candidate spread over 0.3%, an unreadable `equity_total`, or any open reconcile divergence. Variant `D_no_trade` |
| `play_dip_pullback_enabled` + `risk_sizing_enabled` | both `false` — DIP is §6.2 and sizing is the even split | both `true` is the pullback reshape (§1.11a) sized on a 1%-equity risk budget (§1.1a) in full. **They ride together in one variant** because the reshape's target is stated in stop distances and its size is the same measurement — splitting them produces a column that is neither the shipped rule nor the reshape. Variant `E_dip_pullback` |
| `play_sector_enabled` | `false` — there is no such play | `true` adds the sector rotation play, `src/plays/sector.py` (§1.11b). **Additive rather than a contrast**: baseline has no such play, so every row under this variant is a trade the shipped rules did not take. Variant `F_sector_play` |
| `sizing_v2_enabled` | `false` — sizing is the ladder | `true` adds one new name per sector per day, half a slot beside a correlated holding, the posture's size multiplier and its target-invested cap (§1.1b). **A suppression and a resize.** Variant `I_sizing_v2` |
| `catalyst_rvol_time_normalised` | `false` — CATALYST's 1.5x is applied to volume so far against a full-day average | `true` applies it to volume scaled to a full session, refused before minute 30 (§1.11d). Additive in practice: baseline's CATALYST has never bought. Variant `H_catalyst_rvol_tnorm` |
| `regime_v2_enabled` | `false` — the posture is computed and recorded as a shadow field, not enforced | `true` enforces breadth, the five postures and DIP's sector rule on new entries (§1.10b). **A suppression**: its skipped trades are baseline's rows with no row here. Variant `G_regime_v2` |

`A_dip_band` needs no new key — it is `dip_trend_band_pct: 0.0` (§1.11), the
strict pre-band rule.

**The scorecard block is still capped at five lines** (`report.SHADOW_MAX_LINES`)
and there are now seven variants, so two of them collapse into the block's
`+N more variants (edge_shadow_trades)` line on any week where all seven close
a trade. That is the cap doing its job — the alert is read on a phone — and the
full table is in `edge_shadow_trades`.

**Three of the four named variants already shipped**, so each is registered in the
direction that still produces a contrast: the *old* rule against today's.
`shadow.VARIANTS` carries that reasoning per variant, and the weekly scorecard
carries one line per variant under a `shadow 7d` heading.

Run one by hand with `POST /api/shadow` (`{"token": "...", "days": 10,
"write": false}` for a dry run that writes nothing).
