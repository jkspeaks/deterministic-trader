# deterministic-trader — design

The design the code was built from. Section numbers (`§4`, `§13`) and appendix
letters cited in code comments, tests and migrations refer to this document.
Where this document and the code disagree, the code and its tests are right.

---

## 0. Why v3 exists

v2 was a careful design that answered a different question than the one being
asked. It optimised for a backtestable momentum strategy on a fixed universe with
a paid data overlay, executed once a day in regular hours. The actual goal is an
event-adaptive swing system on free data that stays awake outside regular hours
and scales from $236 to $10,000 without a rewrite.

v3 is a rebuild, not a patch. Where v2 was right, it is carried forward and said
so. Where it was wrong, the correction cites the measurement that forced it.

| # | v2 said | v3 says | Forced by |
|---|---|---|---|
| 1 | *(silent — never mentions it)* | **Good Faith Violations are the account-blocking risk**, and one invariant removes them | §2 — this is a cash account; 3 GFVs ⇒ 90-day restriction |
| 2 | Fractional ⇒ market ⇒ regular hours, universally (Invariant 6) | **Per-symbol.** Robinhood publishes `extended_hours_fractional_tradability`, `true` for a large liquid subset | §A.2 — measured on 20 symbols |
| 3 | Fixed book: 9 positions, ~$23, $232 | **Position count and size are computed from equity every cycle** | §1 — capital grows as the system earns it |
| 4 | 12-1 momentum selects, MR times; QuantConnect validates | **Four named plays; the agent picks which are live** | Strategy must shift with news and regime |
| 5 | TipRanks overlay, 10 calls/month, quota-gated | **No paid data.** EDGAR, FRED, index CSVs, RSS, Robinhood MCP | Free/OSS requirement; §7.4 cost-recovery arithmetic |
| 6 | Universe = scanner cap/liquidity band, index membership impossible | **Index membership is free and daily** from published constituent files | §5 — v2's an early probe gave up too early |
| 7 | Inherited positions are "opening inventory" | **Every position is managed identically.** No legacy tier | Manual trades must absorb with no intervention |
| 8 | Trade only in regular hours | **Overnight exits at ~20:00 and ~04:30 ET**, spread-gated | §A.3 — overnight spreads are bimodal, not uniformly bad |
| 9 | Wash-sale set rebuilt from broker every cycle | **Only loss-closes are tracked**, rows self-expire at 31 days | Explicit optimisation requirement |
| 10 | Six source files | **Eight.** Reconciliation and reporting are not sleeves | §11 |

### The rule v2 got right, carried forward unchanged

> **Before building a capability, find where it already works and reuse that
> path.** Where v3 adds something new, it says why the existing path could not
> be used.

Three defects in this account's history — fractional limit orders, the
duplicate-decider incident, the notification outage — shared one cause: a
capability that already worked somewhere was reimplemented elsewhere and got it
wrong. That lesson survives the rewrite.

---

## 1. Purpose and success criteria

A fully unattended equities agent that runs with no laptop, uses Robinhood MCP as
its only price and execution source, and trades a dynamic, news-aware subset of
the S&P 500 plus sector ETFs.

It exists to answer one question — *can an agentic system beat a competent human
doing this by hand* — measured against three benchmarks:

1. **SPY buy-and-hold** from inception (market beta).
2. **Frozen inventory** — the book as it stood at inception, held unchanged
   (the do-nothing control).
3. **24% / year** — the operator's own realised return in 2025, which is the bar
   that actually matters.

Capital is the risk control. Autonomy is the experiment.

### What "agentic" means here, precisely

Per cycle there is **no human between decision and execution**. The agent reads
state, decides, places real orders, and reports afterward. Human control is the
kill switch before the fact and cancel-in-app after — both are controls, not
steps in the loop.

The decision layer is **not** free-form LLM judgment, and **not** a frozen rule.
It is a **playbook**: a small set of named plays with mechanical entry and exit
rules, where the agent's judgment is exercised in choosing *which plays are live
today*, not in inventing trades. A losing play can be switched off without
switching off the system, and a bad month is attributable to a named play rather
than to the agent's mood.

---

## 2. Regulatory guardrails — the section v2 did not have

This is first because it is the only part where a mistake costs more than money.

### 2.1 Account facts

| | |
|---|---|
| Trading account | `••0000`, nickname **Agentic**, `agentic_allowed: true` |
| Type | **cash** |
| Other account | `••1111`, margin, `agentic_allowed: **false**` — unreachable by this agent |
| Option level | 2 (**unused** — options are out of scope, §17) |

### 2.2 Pattern Day Trader — does not apply

PDT is a FINRA margin-account rule. On a **cash** account there is no day-trade
counter and no $25,000 threshold. The system may open and close a position the
same day without accumulating a PDT strike.

**This must not be relaxed into "day trading is free."** It is not; §2.3 is what
binds instead.

### 2.3 Good Faith Violations — the real constraint, and how it is removed

A GFV occurs when a security bought with **unsettled** funds is sold before those
funds settle. Three GFVs in a rolling 12 months restricts the account to
settled-cash-only for 90 days.

The naive version of the requested design — *"check every 40 minutes and sell if
it slumps"* — is a GFV generator. Buy at 10:00 using proceeds from yesterday's
sale, exit at 14:00 on a drop, violation.

> ### Invariant 1 — every buy is funded by settled cash
>
> `guard.py` denies any buy whose notional exceeds **settled** buying power.
> Unsettled proceeds are never deployed.

A GFV requires the *purchase* to have used unsettled funds. If no purchase ever
does, **no sell can ever create one** — at any hour, at any frequency, forever.
The reactive exit loop becomes legal by construction rather than by careful
bookkeeping.

Cost: proceeds from a sale are not redeployable until T+1. At $236 that is real
drag; at $10,000 it is negligible. Accepted deliberately — it buys away an entire
class of account-level risk for one denial rule.

**Free-riding** (buying without sufficient funds and selling to cover) is
eliminated by the same invariant.

### 2.4 What "settled" means, and the open question

v2 concluded `buying_power` is authoritative and no settled/unsettled split
exists in the API. That observation was made with **no unsettled proceeds
outstanding** — `cash == buying_power == $14.52` — where the claim is trivially
true and therefore untested.

**Probe P3 (§16) must close this before the first sell-then-buy sequence.** Until
it does, `guard.py` applies a conservative fallback:

> Deployable = `min(buying_power, cash_at_last_reconciled_settled_state)`, and any
> buy in the same session as a sell is denied outright.

This over-restricts. That is the correct direction to err, and it self-relaxes
the moment P3 returns.

### 2.5 Wash sales

A wash sale is an **IRS** matter, not a Robinhood restriction — it cannot block
the account. It is nonetheless treated as a hard constraint by operator
instruction, because the bookkeeping cost of a disallowed loss is not worth the
trade it enables.

Mechanism in §9. The design principle, per explicit requirement: **only names
closed at a loss are ever monitored.** Winners and flat closes create no state.

### 2.6 Other blocking risks, checked and dismissed

| Risk | Applies? | Why |
|---|---|---|
| PDT | No | Cash account |
| Good Faith Violation | **Yes** | Removed by Invariant 1 |
| Free riding | **Yes** | Removed by Invariant 1 |
| Cash liquidation violation | No | Margin-account concept |
| Position/concentration limits | No | Broker-side limits are far above this account's size |
| Options assignment risk | No | Options out of scope |
| Short selling | No | Long-only (§17) |

---

## 3. Invariants

**Invariant 1 — every buy is funded by settled cash.** §2.3.

**Invariant 2 — the Python has no network access.** No `requests`, `httpx`,
`yfinance`, `urllib`, or `socket` anywhere under `src/`. Enforced by a test that
walks the import graph. The agent is the only thing that touches the outside
world; the Python only transforms JSON. This is what makes every module testable
from a fixture with no mocking.

**Invariant 3 — the agent may only place what the plan emits.** `guard.py`
rejects any symbol not in the current candidate set. "The agent invented a trade"
is a code-level impossibility, not a prompt instruction. The plan's stdout is
written to `runlog` **before** anything is placed, so a mid-execution crash still
leaves a record of intent.

**Invariant 4 — Robinhood is the sole source of prices, indicators and
execution, with one named exception (SMA, §7.1a).** No other provider may
supply a price, a bar, an RSI or an ATR that reaches a play. Providers
disagree on adjusted closes and split handling; a silent source switch would
invalidate every golden-file test at once — confirmed, not just a theoretical
risk: Twelve Data's RSI disagreed with Robinhood's by 8-17 points and its ATR
by 15-25% on the same symbols, easily enough to flip `ATR_STOP` or a boundary
gate, so both stayed on Robinhood. SMA(20/50/200) is the sole exception, made
explicit here rather than left as a live judgment call, because it held
within 0.3% across every date offset tested — it's arithmetic on closes, not
a smoothing formula, so it doesn't carry the same vendor-disagreement risk
RSI/ATR do. News and macro providers (§7) supply *events and dates*, never
prices.

**Invariant 5 — capability is computed, never configured.** Position count,
position size, order type, session eligibility and stop mechanism are all derived
from current equity and current per-symbol broker flags at each cycle. There is
no "small account mode" to switch. §4.

**Invariant 6 — cadence lives in Python, not in cron.** Routines fire on a
schedule; `cadence.py` decides what kind of cycle this is. Every "what runs now"
decision is golden-file testable and cannot drift in prompt text.

**Invariant 7 — every position is managed identically.** There is no legacy
tier, no inherited-inventory exemption, and no symbol the rules do not apply to.
A position the operator opened by hand is indistinguishable from one the agent
opened, and is managed on the same terms from the next cycle onward.

---

## 4. Capital-adaptive sizing — the ladder

The system must work at $236 and at $10,000 with no code change. It does this by
deriving structure from equity each cycle.

### 4.1 Position count

```
n_target = clamp(round_half_up(equity_total / min_position_size), 3, 25)
```

with `min_position_size = 200` (config). At $236–$500 that is the **3-position**
floor; at $1,000, 5; at $5,000 and above, the 25 ceiling.

*History: the clamp ceiling was raised 12 → 25, and `min_position_size` 80 →
200. The $80 rung split the deployable cash so many ways that under
`whole_shares_only` any name much above ~$75 could not afford a whole share and
was refused; $200 buys a whole share of nearly every liquid large-cap, which is
what makes a quick round-trip possible.*

**Half-up, not banker's.** Python's built-in `round` rounds half to even, so
`round(8.5)` is 8 and `round(9.5)` is 10 — the slot count would depend on the
parity of a number nobody chose. `sizing.py` rounds half away from zero.

**Why fewer positions at low capital is the safe choice, not the risky one.**
Ten positions of $23 is the *most* fragile configuration available: every one is
fractional, therefore market-order-only, therefore regular-hours-only, therefore
un-stoppable at the broker. Three positions of $79 can hold whole shares, which
unlocks limit orders, extended-hours eligibility, and resting stops that protect
the book while the agent is asleep. **Concentration buys capability.**

### 4.2 Position notional

```
deployable   = max(0, settled_buying_power − cash_reserve)
cash_reserve = max(1.00, 0.02 × equity_total)
open_slots   = max(0, n_target − names currently held)
notional     = deployable / max(1, open_slots)
```

`settled_buying_power` is the §2.4 basis — `min(buying_power, settled_cash)`
under the conservative policy — and when it cannot be established, **nothing is
sized**. Invariant 1 fails closed: zero would be a claim about the balance, and
an absent figure is not a balance.

A position exiting this cycle **keeps its slot**. Its proceeds are unsettled, so
guard would deny a same-cycle re-entry anyway; freeing the slot would only
produce an order that cannot be placed.

**Guard does not subtract the reserve.** The reserve is sizing policy and
settled cash is legality. A guard that enforced the reserve would deny orders
that are perfectly legal, which is guard repairing the plan rather than judging
it.

### 4.3 Execution path selection — per candidate, per cycle

```
if price ≤ notional and whole_shares ≥ 1:
      path = WHOLE      qty = floor(notional / price)
      order = LIMIT, sessions = regular + extended (+ overnight if §6.3 passes)
      resting stop = eligible
else:
      path = FRACTIONAL qty = floor(notional / price, 6 dp)
      order = MARKET, sessions = regular only
      resting stop = NOT eligible — stop is agent-evaluated
```

P1 came back **no**: fractional orders are rejected client-side for every
non-regular session regardless of `extended_hours_fractional_tradability`, so
there is no third row and the flag is never read. Fractional quantities floor
to six decimals; rounding up would spend more than the cycle budgeted.

**Which price sizing uses.** The limit price is an *input* to sizing, never an
invention of it. Sizing sizes against the price the order will actually
transact at — the candidate's `limit_price` when a play carries one, the ask
when it does not. Guard costs limits at `limit_price`, so the two agree by
construction; the runbook's F case was $0.48 short precisely because sizing and
guard used different numbers.

Two consequences:

* A **staged** order — one that will sit unfilled across a session boundary —
  must carry a limit, or it is returned unsized. An order queued for the open
  takes whatever the open hands it, and that is the one case where the limit is
  the entire protection.
* A staged order that sizes `FRACTIONAL` is refused outright, because the
  fractional path is market-only and a market order cannot carry that limit.
  At $236 this means pre-market and overnight cycles stage no entries — the
  same conclusion the probe findings reached for exits.

For exits the rule is already enforced: guard denies a staged sell that is not
a limit order (`ORDER_TYPE_OUTSIDE_REGULAR_HOURS`).

Candidates on the `WHOLE` path receive a **ranking preference** (config
`whole_share_bonus`), because the path itself is worth something: it carries
better risk control. It is a tiebreaker, never an override of play quality.

`sizing.py` applies it and **returns the candidate list ordered**, because it is
the only module that knows the path. The bonus is scored against the path the
*unclamped* notional implies, since ranking must be settled before §4.4's walk
can consume headroom in rank order; a candidate the walk later shrinks into a
fractional order keeps the rank it earned. Exact ties break on symbol, so a
golden file never depends on dict ordering.

As equity grows, more of the universe crosses into `WHOLE` automatically. **The
system walks up the capability ladder as it is funded; there is no threshold to
maintain and no migration to run.**

A slot that cannot afford one full share of a candidate at basis is refused
(`WHOLE_SHARE_ONLY`) rather than routed to the `FRACTIONAL` path — no
market-only buy carries a resting stop or reaches extended hours (§10.1), so
the operator chose to stop opening or topping up positions that way. Scoped to
**buys only**: fractional lots already held stay sellable and exit normally
through `plays/exits.py`'s own path, untouched by this ruling. `sizing.py`'s
near-miss promotion (a slot's even split already reaching ≥ 0.5 of a share,
see above) runs first and can still rescue a candidate into `WHOLE`; only a
candidate still `FRACTIONAL` after that is refused. `guard/legality.py` denies
the same shape of order (`WHOLE_SHARE_ONLY`, side `buy` only) as
defense-in-depth against a hand-placed or replayed order that bypassed sizing.
The knob is a plain top-level key in `config.json` (`whole_shares_only`,
alongside `whole_share_bonus`) — an operator who wants fractional buys back
sets it `false`.

### 4.4 Concentration limits

| Limit | Value |
|---|---|
| Max single position | 40% of equity at n=3, scaling to 15% at n≥8 (`max(0.15, 1.2/n)`) |
| Max per sector | 50% at n=3, scaling to 30% at n≥5 |
| Min order notional | $1.00 (broker floor) |
| Max new positions per day | 3 (config; 2 by design) |

The concentration caps must scale with `n`. A flat 15% cap is unsatisfiable at
three positions and would deadlock the system at low capital — a failure mode
worth naming, because a guard that can never pass is indistinguishable from a
broken system.

**Sizing shrinks to the caps; guard still denies against them.** The notional is
`min(even split, position-cap headroom, sector-cap headroom, remaining cash)`,
and a candidate that cannot reach the $1 broker floor is returned *unsized*
rather than as an order guard is certain to refuse. The walk is sequential for
the same reason guard's is: two entries sized independently against one sector's
headroom breach it together.

The two cap fractions and the §2.4 settled basis are defined **once, in
`sizing.py`**, and imported by `guard.py`. Two definitions that merely agree
today would eventually drift, and a sizer that disagreed with its guard would
emit orders denied on every cycle — an account that silently stops trading.

---

## 5. Universe

**S&P 500 + sector and index ETFs. Roughly 530 names.**

### 5.1 Membership is free and published daily

v2 concluded index membership was unavailable via Robinhood (`get_indexes`
returns no constituents, no scanner filter exists) and fell back to a cap and
liquidity band. That conclusion was right about Robinhood and wrong about the
world: constituent lists are published free, daily, by the issuers.

| Source | Gives | Cost |
|---|---|---|
| SSGA SPY holdings file | S&P 500 constituents + weights | free, no key |
| Invesco QQQ holdings file | Nasdaq-100 (cross-check) | free, no key |
| Wikipedia S&P 500 list | constituents + sector + change history | free, no key |

Fetched **weekly** by GitHub Actions into `edge_universe`. Two independent
sources; a symbol appearing in one and not the other is logged, not silently
resolved. If both fail, the previous snapshot is reused and flagged stale after
14 days.

### 5.2 ETF sleeve

Required by the macro rotation play (§6.4). Sector SPDRs (XLE, XLF, XLK, XLV,
XLI, XLY, XLP, XLU, XLB, XLRE, XLC) plus broad index ETFs. Maintained as a static
list in `config/etfs.json` — it changes on the order of once a decade, so a
refresh cadence would be ceremony.

### 5.3 Daily candidate set

The universe is ~530 names. The **candidate set** is what any cycle actually
reasons about, and it is small by construction:

```
candidates = current_positions
           ∪ names with a live play trigger today   (typically 5–30)
           ∪ macro rotation targets if a play is live (≤ 11)
```

Only the candidate set incurs per-symbol Robinhood calls. **Universe size costs
nothing; candidate set size costs calls.** This is what makes a 530-name universe
affordable where v2's 150-name shortlist needed a monthly batch job.

---

## 6. The playbook

> **Plays are mechanical. The agent chooses which are live.**

Each play declares: a trigger, entry conditions, sizing, exit rules, and the data
it needs. The agent may enable or disable a play for the day based on regime, and
must record why. It may **not** invent an entry outside a play, and may not
modify a play's exit rules — Invariant 3.

Every position carries its originating `play_id` for its whole life, so
attribution survives even when the agent's reasoning does not.

### 6.0 Play arbitration

Four plays compete for `n_target` slots. At $236 that is **three slots for four
plays** — most plays will be idle most days, and attribution will accumulate
slowly until capital grows. This is stated plainly because it is a real cost of
starting small, not a defect to fix.

Ranking across plays uses a single normalised score per candidate:

```
score = play_conviction (0–1, play-specific)
      × regime_fit       (0–1, agent-set per play per day, logged with reason)
      + whole_share_bonus
      − crowding_penalty (same sector as an existing position)
```

Ties break toward the shorter expected holding period, because faster capital
turnover matters more than expected edge when only three slots exist.

### 6.1 Play `PEAD` — post-earnings drift

The best-evidenced anomaly on this horizon, and entirely free to serve.

| | |
|---|---|
| **Trigger** | Company reported within the last 1–3 sessions |
| **Entry** | EPS surprise > 0 **and** report-day reaction ≥ +2% **and** price > 50-day SMA |
| **Conviction** | scaled by surprise magnitude, capped at 1.0 |
| **Exit** | 10 trading days · 2×ATR(14) stop · +8% target · next earnings date − 2 |
| **Data** | RH `get_earnings_calendar`, `get_earnings_results`, `technical_indicators(sma, atr)` |

### 6.2 Play `DIP` — oversold inside an uptrend

Covers quiet weeks when nothing has a catalyst. Carried from v2's mean-reversion
sleeve, which was its most defensible component.

| | |
|---|---|
| **Trigger** | RSI(14) ≤ 30 |
| **Entry** | price > 200-day SMA **and** no earnings within 3 sessions |
| **Conviction** | scaled by depth below the 20-day mean |
| **Exit** | RSI ≥ 50 · 8 trading-day time stop · 2×ATR(14) stop |
| **Data** | RH `technical_indicators(rsi, sma, atr)` — all server-side, ~250 bytes each |

**No raw bars are required.** Robinhood computes these server-side, which is what
makes a 530-name universe tractable. v2 needed ~25 bars per name for a z-score;
`rsi` is the same signal for 1/20th the payload.

### 6.3 Play `CATALYST` — event-driven

Highest variance, most dependent on the news pipeline. Sized at **0.75× normal
notional** until it has 20 closed trades.

| | |
|---|---|
| **Trigger** | 8-K filed within 2 sessions, or an analyst/news cluster |
| **Entry** | price reaction ≥ +3% on ≥ 1.5× average volume, confirming direction |
| **Hard veto** | 8-K item **4.02** (non-reliance / restatement) — never enter |
| **Force exit** | item **4.02** or **5.02** (officer departure) on a held name |
| **Exit** | 7 trading days · 2×ATR stop · +10% target |
| **Data** | SEC EDGAR submissions JSON (`items` field is pre-structured — no document parsing), RH quotes and volume |

**Direction comes from price, not from text.** An 8-K item code says *something
material happened*, never whether it was good. Requiring a confirming price
reaction avoids sentiment classification entirely — a deliberate refusal to build
the least reliable component.

This also sidesteps the trap v2 recorded from a Form 4 probe: routine director
grants are tagged `side: buy`, so a naive insider-buying detector manufactures
bullish signal from scheduled compensation. Raw Form 4 remains **out of scope**
(§17); if it ever returns, it must filter on EDGAR `transactionCode == P`, never
on a broker's `side` field.

### 6.4 Play `MACRO` — regime rotation

Expressed through sector ETFs, which have tighter overnight spreads than single
names and therefore survive §6.6 more often.

| | |
|---|---|
| **Trigger** | Scheduled macro release (CPI, PCE, FOMC, NFP) or a threshold move in a tracked series |
| **Entry** | Sector tilt from a declared surprise→sector mapping in `config/macro_map.json` |
| **Conviction** | scaled by surprise magnitude vs. consensus |
| **Exit** | 15 trading days · 2×ATR stop · regime flip |
| **Data** | FRED API (free key), Treasury FiscalData, RH quotes |

**Stated honestly: this is the least evidenced of the four.** The
surprise→sector mapping is a heuristic, not a fitted model, and it lives in a
config file precisely so it can be revised or the play retired without touching
code. It is included because macro is what the operator asked to trade on, and
because a wrong mapping costs a mediocre sector ETF trade rather than a blowup.
It should be the first play killed if attribution is unkind.

### 6.4a Play `SURGE` — full send (operator)

The sixth play, and the one that deliberately breaks §4.4. When
`full_send_enabled` is true in **`edge_control`** (a Telegram-flippable runtime
switch, `/edge surge on|off` — it is an account-wide risk switch like
`trading_enabled`, not a git-checkout config value) and a name is in a clean,
quickly-monetisable uptrend, the cycle may commit the **entire available
settled cash** across up to `SURGE_MAX_NAMES` (3) such names, split by
conviction, ignoring the slot, position and sector caps outright. Every other
guard check still runs — the drawdown halt, the earnings and wash-sale windows,
tradability, the settled-cash basis, and the daily new-position cap.

| | |
|---|---|
| **Trigger** | `edge_control.full_send_enabled` true **and** `price > sma_20 > sma_50 > sma_200`, `55 ≤ RSI(14) ≤ 80`, `price ≥ sma_50 × 1.02` (thresholds in `plays/surge.py`) |
| **Entry** | whole balance across ≤3 names, conviction-weighted, whole shares only |
| **Conviction** | scaled by how far price sits above the 50-day, full at +15% |
| **Exit** | 7 trading days · 2×ATR stop · 8% target — the ordinary §6.5 terms; the concentration bypass is the *only* thing that makes SURGE different |
| **Data** | RH `get_equity_technical_indicators` + quotes — no new call |

Governed by the switch, not by a daily `edge_play_state` regime row: an absent
row reads as on, an explicit `0.0` still darks it. The documented loosening
lever, if pick counts run too low, is dropping the RSI floor toward stacked-MAs
alone. This is a real risk decision — a wrong pick is now the whole account,
not one slot — and it is code-level per §17.

### 6.5 Exits, in priority order

Exits are evaluated on **every** cycle, including overnight cycles. Entries are
not.

1. **Hard event exit** — 8-K 4.02/5.02 on a held name
2. **Drawdown halt** — account below 80% of high-water mark (§10)
3. **Earnings deadline** — flatten at T−2 before a confirmed report
4. **2×ATR(14) stop** — subject to §6.5.1's news-hold when it fires at a loss
   on a `monitor`/`full` cycle
5. **Play time stop** — same news-hold, on the same condition, when it closes
   at a loss
6. **Play target** — refined by §6.5.2 into `TARGET_CATALYST` when a
   qualifying event confirms it
7. **Play-specific signal exit** (e.g. `DIP` RSI ≥ 50)

The system never holds through an earnings print. At these position sizes no
sizing rule protects against a 25% overnight gap, and it keeps every play's job
to *timing and deadline*, never direction prediction across a binary event.
Exits 1–3 are never subject to the news-hold below — they are urgent and
time-boxed by construction (a filed restatement, a drawdown breach, a earnings
date), and delaying any of them to ask whether the news is "actually good"
defeats the reason each one exists.

#### 6.5.1 The news-hold on exits 4 and 5

Two same-day stop-outs (C and INTC, both `ADOPTED`, both cut on ordinary
volatility rather than a broken thesis) prompted this operator ruling: a
loss-making ATR stop or time stop, on a `monitor`/`full` cycle only, does not
place immediately. The acting session checks recent news for the symbol first
— a genuinely positive catalyst (an earnings beat, a deal or partnership) with
the price still down is a profit-booking pattern with a fair chance of a
48–72h bounce, and that case is worth a bounded delay rather than a reflexive
sale. `docs/routine/cycle-prompt.md`'s Step 8 has the exact procedure.

This is a **delay, never a veto** — Invariant 3 still holds. The rule fires or
it doesn't; the only discretion is *when* within a hard 4-trading-day window:

* **Day 0** (the stop first fires, no hold open): the session's own news
  check decides whether to open a hold instead of selling. `exits.py` cannot
  make this call itself — it has no news access — so it always emits the exit
  exactly as §6.5's priority list says, and the routine intercepts it before
  placing.
* **Days 1–3** (a hold is open): `exits.py` owns this deterministically. Every
  cycle, `entry_price − atr_secondary_stop_multiple × ATR` (config, default
  3.5×, wider than the 2× primary stop on purpose) is checked first — a
  breach force-closes the hold immediately, any day, no further news check.
  Short of that, the hold continues and the day count advances.
* **Day 4**: secondary stop breached closes it, same as any other day. Short
  of that, `exits.py` checks price against `opened_price` (the price at the
  moment the hold opened, written alongside `secondary_stop` when it was —
  not `entry_price`, which the stop math already owns) — an uptick earns
  **one** 3-day extension, to day 7, deterministically, no second news check.
  No uptick closes the hold on schedule, exactly as before this extension
  existed.
* **Day 7** (only reachable after an extension): the hold closes regardless
  of price — this window is the hard cap, never renewed a second time. An
  indefinitely renewable hold is a live discretion to never sell, which
  Invariant 3 forbids as surely as an indefinitely renewable veto would.

State (`loss_holds`, keyed by symbol) lives in `edge_control`, written through
the same `control_updates` merge every other module already uses — not in the
per-position book, since `exits.py`'s day-count/secondary-stop logic runs
before that cycle's book would exist to carry it. Each entry carries
`opened_trading_day`, `day`, `secondary_stop`, `opened_price`, `reason`, and
(once the extension has fired) `extended: true`.

#### 6.5.2 `TARGET_CATALYST` — exit 6, refined

Full-exit only — no partial-exit machinery exists anywhere in this package
(`exits.py`, `guard.py`, `reconcile.py` all assume a whole-position sale), and
building it was explicitly deferred, not bundled into this fix. Reuses exit
6's priority and full-sell path; no new slot, no change to
`guard.py`/`reconcile.py`.

#### 6.5.3 Limit-only exits

Every exit places as a limit order at the bid — never market, in any session.
Two cases cannot place at all and fall through to `manual_required` instead of
a market fallback: a fractional quantity (Robinhood rejects a limit order on
one outright — the same restriction behind *"Limit order quantity cannot
include fractional shares"*, §8.3/history), and a missing bid to price one
against. Both alert Telegram and wait for the operator to place the limit
manually — the same shape as an earlier VMRK precedent, now the rule rather
than an improvisation.

#### 6.5.4 The same-day-fill guard and the average-down offer

After a same-day manual buy-then-sell on MU realised an avoidable loss (the
operator's own account, not the system), two changes:

**The same-day-fill guard.** A position that picked up a fill on today's
trading day cannot leave via exits 4-7 (`ATR_STOP`, `TIME_STOP`, `TARGET`,
`SIGNAL_EXIT`) — all four are judgment calls about a position's trajectory,
and a same-day round trip has not had one yet. Exits 1-3 stay mandatory, the
same carve-out shape §6.5's operator-hold override already uses: a forced
filing, the drawdown flatten and the earnings deadline are "the system never
waits on X" commitments, not a trajectory judgment this guard has any
business overriding. Driven by `reconcile.Position.last_fill_on` — the
newest open tax lot's acquisition date, the mirror image of the existing
`sessions_held`'s oldest-lot reading.

**The average-down offer, `ATR_STOP` only.** The operator's read of the MU
trade: a stop firing on a name with nothing wrong with it is a
profit-booking opportunity for a future recovery, not evidence to sell into
a loss. `exits.py` still computes and still defaults to the sell — it has no
news or trend access (Invariant 4) to make that call itself — but a
mechanical floor (`_average_down_gate`) decides whether the `ATR_STOP` is
even *offered* to the routine's own judgment as a candidate to average down
instead: at least `average_down_min_drop_pct` below the newest lot's own
price (not the position's whole cost basis), at least
`average_down_min_sessions_since_fill` trading days since that fill (tax
lots carry no fill time, so this counts sessions rather than minutes), and
fewer than `average_down_max_open_lots` lots already on the position.
Clearing the floor only populates `decide.exits.average_down_eligible` —
nothing places or persists on its own. Step 8a0a (`docs/routine
/cycle-prompt.md`) makes the actual call, sizes the add like any other buy
through `sizing`/`guard.evaluate`, and pulls the `ATR_STOP` sell from the
plan if it takes it. No new cost-basis state to keep in sync: the position's
new `entry_price`/`last_fill_on` are exactly what the next `reconcile` pass
derives on its own from the broker's tax lots, the same way an ordinary
scale-in already worked before this existed.

### 6.6 The overnight spread gate

`all_day_tradability` is `tradable` for **every** symbol measured — including MRK
at a 14.8% overnight spread. It is necessary and nowhere near sufficient.

```
spread_pct = (ask − bid) / mid

allow_overnight_exit =
      spread_pct ≤ overnight_max_spread_pct           # default 1.0%
   or loss_pct_below_stop ≥ catastrophe_multiple × spread_pct   # default 3×
```

The catastrophe override matters: paying a 2% spread to escape a position 15%
past its stop is correct, and a flat gate would forbid it.

Against measured 20:00 ET quotes (§A.3), the 1.0% gate admits MU, AAPL, NVDA,
AMD, SMH, PFE, HOOD, INTC and TSLA, and correctly excludes MRK (14.8%), TXN
(8.5%), ADI (6.2%) and CAT (5.5%).

**Spread is measured live at the moment of the exit decision, never cached.**
Overnight liquidity varies by hour and by news.

### 6.7 The buy-side peak check

§6.5.1's mirror on the way in, from the same operator conversation: before
placing any buy `sizing.py`/`guard.py` approved, the acting session checks
whether the entry looks like it is chasing a spike with nothing recent enough
to justify today's price, using the same news tools (TipRanks/Alpha Vantage,
never Robinhood) as 8a0's sell-side check. A clean read places the order
exactly as computed; a peak-chase read skips it for the cycle and alerts —
`sizing.py` cannot make this call itself, the same reason `exits.py` cannot
open a news-hold on its own (Invariant 4: no news access in pure Python).

Unlike the sell side, this runs on **every** cycle with an entry proposed,
not only `monitor`/`full` — there is no multi-day hold to manage, only a
same-cycle skip, so the reasoning that kept 8a0 to market hours (a live news
lookup and a manual hand-off don't work off-hours) doesn't apply here.

Fractional entries are **not** routed to `manual_required` the way fractional
exits are (§6.5.3) — the operator's ruling here was explicit: a fractional
buy that clears the peak check auto-places at market exactly as §4.3's
market-only fractional path already did before this existed. This check is
the only gate added on the way in.

---

## 7. Data — all free

### 7.1 Robinhood MCP — prices, execution, and more than expected

The single source for everything in Invariant 4's scope, plus several things a
paid provider would otherwise supply:

`run_scan` · `get_equity_quotes` · `get_equity_historicals` ·
`get_equity_technical_indicators` (rsi, sma, atr, roc — server-side, ~250 bytes)
· `get_earnings_calendar` · `get_earnings_results` · `get_equity_fundamentals`
(sector) · `get_equity_tradability` (per-session and fractional flags) ·
`get_equity_tax_lots` · `get_pnl_trade_history` · `get_equity_orders` ·
`get_portfolio` · `get_equity_positions` · `place_equity_order` ·
`cancel_equity_order`

No practical call limit on a paid Robinhood membership.

### 7.1a Twelve Data MCP — SMA only

A narrow, explicit exception to Invariant 4's single-source rule. SMA
matched within 0.3% regardless of which day's bar either vendor's "latest"
happened to land on, so it's the only indicator sourced from Twelve Data.

`sma` at 20/50/200 — batch call, comma-separated symbols confirmed live: a
3-symbol request returns all three, keyed by symbol, in one call. The real
per-call symbol cap is not published and was not fully bounded during the
live check (a 30-symbol probe hit the free tier's rate limit while testing)
— the routine chunks at ≤10 symbols per call, the same chunk size already
used for `get_equity_fundamentals`/`get_equity_tradability`, rather than
guessing a larger number.

Free tier: 800 calls/day, 8/min. At 3 calls per cycle (one per SMA period,
each call covering every `stale` symbol in ≤10-wide chunks) and ~11
cycles/day, nowhere near either limit under normal `stale` list sizes.

Connected the same way as the TipRanks/Alpha Vantage MCP connectors already
in use (claude.ai Settings → Connectors). No tool name or parameter shape is
pinned here — none has been verified against a live Twelve Data MCP session
yet, and Absolute 2 (`docs/routine/cycle-prompt.md`) already forbids
guessing a provider's field names, the same rule that applies to Robinhood's
own tools. Until a session has this connector, `docs/routine/cycle-prompt.md`
Step 6 falls back to the original all-Robinhood `sma` calls — a documented
degradation, not a live judgment call.

### 7.2 Free external sources, fetched by CI

| Source | Serves | Auth | Limit |
|---|---|---|---|
| **SEC EDGAR** `data.sec.gov` | 8-K item codes, filing dates | User-Agent only | 10 req/s |
| **FRED** | CPI, PCE, fed funds, yield curve, unemployment | free key | generous |
| **Treasury FiscalData** | rates, auctions | none | none stated |
| **SSGA / Invesco holdings** | index constituents | none | none |
| **Wikipedia** | S&P 500 list + sector | none | courtesy |
| **RSS** (Fed, company IR, wire feeds) | headlines | none | none |

**EDGAR needs no document parsing.** The submissions JSON carries an `items`
field (`"4.02,9.01"`), so item codes are read directly out of the index. This is
why `CATALYST` is affordable.

### 7.3 Where interpretation happens

CI does **mechanical extraction only** — item codes, surprise percentages, release
values, headline text. It runs no model and makes no judgment.

The agent does **interpretation** — which plays fit today's regime, what a
cluster of headlines means. This keeps judgment in the one component that has
judgment, costs nothing extra, and means the pipeline never needs an API key for
a model.

### 7.4 Why there is no paid tier

A $20/month tool over six months is $120 — **51% of a $236 account**. Recovering
it in 15 trading days requires +51% in three weeks, which is not a plan but a
gamble.

The same $120 against $10,000 is 1.2%, which is entirely reasonable. **Revisit
above ~$5,000; not before.** And note that a 2-week holding period needs nothing
real-time, so the usual justification for paid market data does not apply here at
any account size.

§7.1a's Twelve Data SMA exception does not reopen this stance — it's a
free-tier connector (800 calls/day) scoped to one indicator, not a paid
subscription, and was adopted for cross-vendor accuracy reasons (Invariant
4), not for real-time data this pipeline has never needed.

---

## 8. Architecture

```
claude.ai scheduled routines          ← the ONLY component that can hold a
  │  ~11 invocations/trading day         Robinhood MCP session unattended
  │
  ├─ reads   Supabase (MCP)  : universe, events, macro, loss ledger, lock, control
  ├─ reads   Robinhood (MCP) : positions, orders, quotes, indicators, tax lots
  │
  ├─ runs    uv run python -m src.cli decide < cycle.json     ← deterministic core
  │            cadence · universe · plays · sizing · guard · reconcile
  │          → an order plan, or an empty plan with a reason
  │
  ├─ places  exactly what the plan says, via Robinhood MCP
  │
  └─ writes  runlog · edge_alerts → Supabase
                            │
                            └─► GitHub Actions relay ─► Telegram

GitHub Actions  (free tier)
  ├─ weekly   : index constituents → edge_universe
  ├─ 30 min   : EDGAR 8-K items, FRED releases, RSS → edge_events
  └─ 15 min   : drain edge_alerts → Telegram

the PHP host PHP cron  (already paid for)
  └─ hourly   : did the routine and CI both run? → Telegram if not
```

### 8.1 Why routines execute and CI does not

MCP is session-only. GitHub Actions **cannot reach Robinhood MCP**, so a Python
brain on CI would need a second-hand price source — reintroducing the exact
failure class Invariant 4 exists to prevent. Collapsing brain and hands into the
routine eliminates the category.

There is exactly **one decider**. CI collects data and delivers notifications; it
makes no decisions and places no orders.

### 8.2 Why the PHP host is a watchdog and nothing else

Its value is being a **third failure domain**. If the routine dies, CI cannot
necessarily tell. If CI dies, the routine's alerts stop being delivered and the
routine cannot report it. A PHP cron on unrelated infrastructure can notice
either, and it fails independently of both.

It is deliberately **not** load-bearing: it fetches nothing the system depends on
and places no orders. If the PHP host goes down, trading continues unaffected and the
only loss is the outer watchdog. Shared hosting has unpredictable cron
granularity and sometimes-firewalled outbound HTTP; making it load-bearing would
be discovering that the hard way.

### 8.3 Notifications

The routine sandbox **cannot reach `api.telegram.org` or `api.telegram.org`** — verified
across three runs and four transports, all returning 403 at the CONNECT tunnel.
This is the environment, not a misconfiguration.

So the routine writes to `edge_alerts` over Supabase MCP (a different transport,
proven working) and GitHub Actions delivers. Per-channel delivery stamps, atomic
row claim, and terminal state after 5 failed attempts — because "delivered if
either channel succeeded" makes a permanently dead Telegram invisible forever.

**What gets notified** (operational only, by explicit requirement):

| Notified | Not notified |
|---|---|
| Cycle failed or did not run | Individual fills |
| Reconciliation divergence | Routine entries and exits |
| Drawdown halt triggered | Daily P&L |
| Play enabled/disabled by the agent | Candidate rankings |
| Relay stalled / channel dead | |
| Weekly scorecard | |

Trade detail lives in the Robinhood app, which is better at it than any
notification would be.

### 8.4 Storage — deliberately minimal

There is **no analytics database**. Robinhood's own dashboard is the record of
what was traded. Supabase holds only what the agent cannot obtain from the broker:

| Table | Why it cannot come from Robinhood |
|---|---|
| `edge_universe` | Index membership is not a broker concept |
| `edge_events` | EDGAR/FRED/RSS extraction |
| `edge_loss_closes` | 31-day repurchase ledger (§9) |
| `edge_control` | Kill switch, cycle lock, high-water mark |
| `edge_runlog` | Append-only log of every cycle: the plan before placement, the broker answers after. |
| `edge_alerts` | Notification queue |
| `edge_benchmark` | One row/day; SPY-relative series cannot be reconstructed later |
| `edge_play_state` | Which plays are live, and why |

`edge_benchmark` is the one concession to measurement, justified by §1's third
benchmark. One row per day, four columns.

---

## 9. Wash-sale ledger — only losses are tracked

Per explicit requirement, and it is also the efficient design.

**On every detected close**, compute the realised result from **tax-lot basis
versus proceeds** — both available from Robinhood. If it is a loss, insert
`{symbol, close_date, loss_amount}` into `edge_loss_closes`. Gains and flat
closes create no row.

`guard.py` denies any buy whose symbol appears in `edge_loss_closes` with
`close_date` within 31 days. Rows are deleted once past 31 days, so the table
holds a handful of rows at steady state and the check is a single indexed lookup.

**The loss is computed, not read.** v2 planned to trust Robinhood's
`realized_gain` field, but this account has exactly one realised close in its
entire history and it was a *gain*, so the sign convention for losses was never
verifiable. Computing `proceeds − tax_cost_basis` from `get_equity_tax_lots` and
the fill removes the dependency on an unverified convention entirely.

**Fallback:** if tax-lot data is unavailable for a close, treat it as a loss and
write the row. Over-blocking a repurchase costs one trade; under-blocking costs a
disallowed loss and the bookkeeping the operator explicitly wants to avoid.

---

## 10. Risk controls

| Control | Value | Where enforced |
|---|---|---|
| Position stop | **2 × ATR(14)** from entry | `guard.py` + resting order when `WHOLE` |
| Account drawdown halt | **20% below high-water mark** | `guard.py`, with its own test |
| Max single position | `max(0.15, 1.2/n)` of equity | `guard.py` |
| Max per sector | `max(0.30, 1.5/n)` of equity | `guard.py` |
| Earnings flatten | T−2, mandatory | `guard.py` |
| Max new positions/day | 3 | `cadence.py` |
| Settled-cash-only buys | absolute | `guard.py` (Invariant 1) |
| Wash-sale block | 31 days | `guard.py` (§9) |

**The drawdown halt lives in code, not in a prompt.** Its predecessor existed
only in cloud-prompt text, so disabling the routines silently deleted the only
equity stop in the system and nobody noticed for days. Here it is a `guard.py`
denial with an explicit test, and `edge_control.high_water_mark` is maintained by
the same code that reads it.

### 10.1 Stop mechanism depends on the path — say so plainly

| Path | Stop | Protection between cycles |
|---|---|---|
| `WHOLE` | resting stop / trailing stop at the broker (pending probe P4) | **continuous** |
| `FRACTIONAL` | agent-evaluated at each cycle | **none** — a gap through the stop is not caught |

Robinhood does not accept stop orders on fractional quantities. So at $236 the
2×ATR stop means *"exit at the first cycle where price is below the stop,"* which
overnight gaps can jump. This is the sharpest practical argument for consolidating
into fewer, larger positions, and it resolves itself as capital grows.

---

## 11. Modules

**Eight source files.** Exceeding it is the signal to retire a play, not to add a
module.

| Module | Owns |
|---|---|
| `cadence.py` | Cycle type, what is due, daily caps, the two clocks |
| `universe.py` | Constituent snapshot → candidate set |
| `plays.py` | The four plays: triggers, entries, exits, conviction |
| `sizing.py` | The capital ladder (§4): count, notional, path selection |
| `guard.py` | Every denial. The layer the agent cannot argue past |
| `reconcile.py` | Intent vs. broker truth (§12) |
| `report.py` | Benchmarks, weekly scorecard |
| `cli.py` | `plan-fetch`, `decide`, `reconcile`, `report` |

Reconciliation and reporting are **not sleeves** — they are audit and
measurement. They do not compete with plays for budget, which is why the file
count rose from six rather than a play being cut.

Every module is a pure function from JSON to JSON (Invariant 2).

---

## 12. Reconciliation

Manual trades must absorb with **no operator intervention, ever** — an explicit
requirement, and Invariant 7 in practice.

### 12.1 Placement

GitHub Actions **cannot see Robinhood**, so it can only verify that
reconciliation *happened*, never its numbers. Broker-truth reconciliation
therefore lives in the routine.

It runs at the start of every full, monitor, and post-close cycle — never in a
no-op cycle, which must stay cheap. An unreconciled prior cycle that placed
orders is itself an event, promoting the next cycle out of no-op.

### 12.2 The diff

Previous `runlog` intent + placement attempts, against live `get_equity_orders`,
fills, and positions.

| Class | Meaning | Response |
|---|---|---|
| `MISSING_ORDER` | plan said buy, placement logged, no broker order | alert, proceed |
| `PHANTOM_ORDER` | broker order no plan proposed | **block entries 1 cycle**, alert |
| `UNTRACKED_FILL` | fill with no originating plan — **an operator trade** | **absorb silently**, adopt under Invariant 7 |
| `QUANTITY_DRIFT` | fill quantity ≠ intent beyond `reconcile_epsilon` | alert, proceed |
| `POSITION_DELTA_MISMATCH` | positions ≠ prior + fills | alert, proceed |
| `STALE_OPEN_ORDER` | placed, still open after 2 cycles | cancel, alert |

**`UNTRACKED_FILL` is not an error.** The operator buying a stock in the app is a
supported workflow. The position is adopted, assigned `play_id: MANUAL`, and
managed on identical terms from that cycle onward: same stops, same limits, same
exits. It is logged, not alerted — alerting on the operator's own deliberate
action is exactly the noise that makes a system tiresome.

An adopted position with no known entry basis takes its stop from
`get_equity_tax_lots` cost basis, which the broker provides.

`reconcile_epsilon`: quantity 1e-6, notional $0.01. Without it, fractional
rounding produces permanent false positives.

**Everything is reported, never silently repaired. The broker wins every
decision.** Persistent divergence across two consecutive cycles escalates to
`RECONCILIATION_DIVERGENCE_PERSISTENT` and blocks entries until cleared in
`edge_control`.

---

## 13. Cycle types and cadence

`cadence.py` decides the type. **The prompt does not.**

| # | Time (ET) | Type | Entries | Does |
|---|---|---|---|---|
| 1 | 04:30 | `overnight` | no | Exits only, spread-gated (§6.6) |
| 2 | 08:45 | `premarket` | no | Overnight/international read; set the day's regime and live plays; **place nothing** |
| 3 | 09:40 | `full` | **yes** | Reconcile → exits → entries |
| 4–9 | 10:40–15:40, hourly | `monitor` | yes, capped | Stops, exits, deadline checks; entries only within the daily cap and never in the last 30 min |
| 13 | 16:30 | `postclose` | no | Reconcile, cancel unfilled, write scorecard data |
| 14 | 20:00 | `overnight` | no | Exits only — catches post-close earnings reactions |

~11 invocations per trading day. Weekends: none (equities do not trade; the
24-hour session is Sunday 20:00 ET through Friday 20:00 ET).

**The no-op path must be genuinely cheap.** Read lock, pending events and
unreconciled state in a single Supabase query, then exit **before touching
Robinhood**. A prompt that runs `get_portfolio` "just to check" first turns 14
invocations into 11 full MCP sessions and the economics collapse.

**Two clocks, never one.** `last_full_cycle` and `last_overnight_cycle` are
separate fields. A shared "last cycle" stamp would let an overnight exit suppress
the day's full cycle.

### 13.1 The lock

Multiple schedules mean concurrent cycles are possible, and concurrent deciders
on one account is a failure that has already occurred once here.

```sql
update edge_control
   set value = :cycle_id, cycle_type = :cycle_type, updated_at = now()
 where key = 'cycle_in_progress'
   and (value = 'none' or updated_at < now() - interval :cycle_timeout)
returning value;
```

**One atomic statement.** Read-then-write races: two sessions both read `none`,
both claim. A racy lock is worse than no lock because it stops you worrying.

Zero rows returned ⇒ someone holds it; log `CYCLE_ALREADY_IN_PROGRESS` and exit.
The **timeout is the primary release**, not a fallback — an LLM session can stop
without releasing. The routine renews the lock at each major step, and releases
on completion as well.

Flat columns, not JSON: the routine authors this SQL by hand, and prompt-authored
SQL should be as dumb as possible.

---

## 14. Error handling

| Failure | Behaviour | Rationale |
|---|---|---|
| `edge_control` unreadable | **Stop.** Place nothing | Something that cannot hear "stop" must not act |
| `TRADING_ENABLED` ≠ TRUE | **Stop** | Kill switch is absolute |
| Drawdown > 20% | **Halt**, set `TRADING_ENABLED=FALSE`, alert | §10 |
| Settled cash unknown | **Deny all buys**, allow exits | Invariant 1 fails closed |
| Universe snapshot > 14 days | Proceed on positions only; no new entries | Stale membership ⇒ unknown universe |
| EDGAR/FRED pipeline down | Proceed; `CATALYST` and `MACRO` no-op | Two plays remain; degradation, not failure |
| Earnings date unavailable | **Block entries** for that name, allow exits | T−2 is a promise that cannot be kept blind |
| Overnight spread above gate | Do not exit; defer to the open, log | §6.6 |
| Fractional order outside regular hours | Do not attempt; defer, log | Structurally impossible (pending P1) |
| Order rejected by broker | Log **exact broker text**, alert, **no blind retry** | Blind retry is how a position doubles |
| Review passes, place fails | Treat as rejection | A passing review is not proof — observed |
| Same rejection 2 cycles running | Escalate `PLACEMENT_BLOCKED` | A repeat is a signal, not a second log line |
| Buying power short of remaining names | **Not a failure.** Fund best-first, report | Partial deployment is correct under T+1 |
| `UNTRACKED_FILL` | **Absorb silently**, adopt | The operator is allowed to trade (§12.2) |
| Divergence persists 2 cycles | Block entries until cleared | |
| Lock claim returns zero rows | Log, exit | Concurrent deciders already happened once |
| Lock held past timeout | Next claim steals it atomically, logs | The timeout is the real release |
| Routine never runs | No trades; positions persist | Fails safe |
| Supabase write fails post-fill | Alert loudly; reconcile next cycle from broker | Broker is the system of record |
| `edge_alerts` undrained > 6h | the PHP host watchdog alerts | Otherwise the relay dies silently |
| Both CI and routine silent > 3h | the PHP host watchdog alerts | The third failure domain earning its place |

**The exact broker rejection string is written verbatim to `runlog`**, never
paraphrased and never bucketed. "Limit order quantity cannot include fractional
shares" is the sentence that diagnosed an entire invariant; `ORDER_REJECTED`
would have diagnosed nothing.

---

## 15. Testing

Because the Python has no network (Invariant 2), every module is a pure JSON→JSON
function: fixture in, expected out, no mocks.

- **Invariant test first.** Walk the import graph of `src/`; fail on `requests`,
  `httpx`, `yfinance`, `urllib`, `socket`.
- **`guard.py` gets the heaviest coverage.** One explicit denial test each:
  unsettled-cash buy, symbol not in candidate set, position above the scaled cap,
  sector above the scaled cap, second order while one is open, buy inside the
  earnings window, wash-sale cooldown, order below $1, `n+1`-th position,
  **drawdown breach**.
- **The scaled concentration caps get a satisfiability test** at n=3, 6 and 12 —
  a cap that can never pass is indistinguishable from a broken system.
- **`sizing.py` is golden-file tested across the capital ladder**: $236, $500,
  $1,000, $5,000, $10,000 × a fixed candidate set, asserting position count,
  notional, and `WHOLE`/`FRACTIONAL` path selection at each.
- **Plays are golden-file tested independently.** Each play gets a fixture where
  it fires and one where it does not, so a change to one play cannot silently
  alter another.
- **Placement-rejection handling has its own tests** — the broker is the one
  actor no fixture constrains: review-passed-then-place-failed classifies as a
  rejection and is never retried; raw broker text survives verbatim into
  `runlog`; the same rejection twice escalates.
- **`reconcile.py` golden files** include an `UNTRACKED_FILL` case asserting
  silent adoption with `play_id: MANUAL` and a stop derived from tax-lot basis.
- **`cadence.py` cycle-type contract is golden-file tested**: first run of day →
  `full`; overnight event after the full cycle → `overnight` with
  `entries_allowed: false`; nothing pending and full already run → no-op;
  an overnight cycle must **not** advance `last_full_cycle`.

**No backtest is required to ship.** The playbook is four independently
attributable plays measured live; a backtest of `PEAD` or `DIP` would be
informative and is welcome later, but gating the build on QuantConnect — which
was never attached — would trade a working system for a validation exercise
neither play strictly needs.

---

## 16. Phase 0 — probes before code

Each is cheap, and each changes the design. None may be assumed.

| # | Probe | Changes what |
|---|---|---|
| **P1** | Does a fractional order **place** in extended hours for a symbol flagged `extended_hours_fractional_tradability: true` (NVDA, INTC, F, PFE…)? The flag and the MCP guide text contradict each other. | **Highest value.** If yes, extended-hours reactivity works at $236 rather than at $5,000, and §4.3's path table gains a third row |
| **P2** | Does `WebSearch` work inside a scheduled routine? `WebFetch` and `curl` are blocked, but WebSearch is a different, server-side path | If yes, §7's news pipeline shrinks dramatically and needs no keys |
| **P3** | Does `buying_power` exclude unsettled proceeds? Sell a fragment, read it the same afternoon | Invariant 1 depends on it. Until closed, §2.4's conservative fallback binds |
| **P4** | Does Robinhood accept **stop** and **trailing-stop** orders on whole shares in this account? | The only continuous protection between cycles (§10.1) |
| **P5** | Is true 24/7 available, or only 24/5? `twenty_four_seven_tradability` is documented but was returned for none of 20 symbols | Whether weekend coverage exists at all |
| **P6** | Can the routine sandbox reach `data.sec.gov` and `api.stlouisfed.org`? | If yes, some fetching skips the CI hop |

**P1 and P2 must run before the core is written.** P3 before the first
sell-then-buy sequence. P4–P6 before go-live.

---

## 17. Out of scope for v1

Options, shorting, crypto, futures, any second broker, raw Form 4 parsing,
sentiment classification from headline text, and any paid data tier.

A fifth play is anticipated. Plays are pluggable: each declares its own trigger,
exits and data needs, so adding one is a new entry in `plays.py` within the file
budget, not a rewrite.

---

## Appendix A — measured facts, 2026-08-01

Recorded so nobody re-derives them. All read live from account `••0000`.

### A.2 Tradability flags

`fractional_tradability: tradable` and `all_day_tradability: tradable` for all 20
symbols checked. The discriminating field is:

| `extended_hours_fractional_tradability: true` | `false` |
|---|---|
| NVDA, TSLA, AMD, INTC, PLTR, F, SOFI, PFE, T | MU, CAT, AAPL, BAC, HOOD, C, SMH, ADI, TXN, MS, MRK |

`twenty_four_seven_tradability` was **not returned for any symbol** (probe P5).

The MCP guide text asserts "fractional and dollar-based orders place only in
regular_hours regardless of the flags," directly contradicting the per-symbol
field. **Unresolved — probe P1.**

### A.3 Overnight spreads, ~20:00 ET 2026-07-31

The evidence for §6.6. Bimodal, and uncorrelated with `all_day_tradability`.

| Symbol | Bid | Ask | Spread |
|---|---|---|---|
| MU | 812.39 | 812.50 | **0.014%** |
| AAPL | 307.30 | 307.35 | 0.016% |
| NVDA | 198.90 | 198.95 | 0.025% |
| PFE | 25.00 | 25.04 | 0.160% |
| HOOD | 86.00 | 86.25 | 0.290% |
| AMD | 470.00 | 472.15 | 0.456% |
| SMH | 537.50 | 539.96 | 0.457% |
| INTC | 89.20 | 89.91 | 0.793% |
| TSLA | 306.80 | 309.30 | 0.812% |
| BAC | 61.44 | 62.07 | 1.020% |
| PLTR | 120.05 | 122.77 | 2.240% |
| SOFI | 15.84 | 16.25 | 2.555% |
| T | 22.97 | 23.57 | 2.579% |
| MS | 205.54 | 211.27 | 2.749% |
| F | 14.42 | 14.87 | 3.073% |
| C | 129.00 | 134.04 | 3.832% |
| CAT | 791.14 | 835.66 | 5.473% |
| ADI | 345.25 | 367.40 | 6.216% |
| TXN | 263.09 | 286.33 | 8.460% |
| **MRK** | 125.00 | 145.00 | **14.815%** |

Overnight cycles only sell, so the relevant cost is one-way: exiting at the bid
gives up roughly half the spread against mid. On a $23 MRK position that is
**~$1.70 surrendered to avoid a $2.30 loss** — the trade barely pays even when
the exit decision is right. Three of the ten current holdings are unusable
overnight.

### A.4 Price context

Prices are far above what v1 and v2 assumed, which is what makes whole shares
expensive at this capital. SPY 746.81. Cheapest current holding: INTC at 90.21 —
**38% of the account in one share.** Whole-share candidates under a $79 notional
from the sampled set: F (14.68), SOFI (16.31), T (23.27), PFE (25.03), BAC
(61.97).

Real gaps are occurring: AAPL −7.3% and MU −6.0% in the last session.

### A.5 Environment constraints, carried forward

- Routine sandbox HTTP egress to `api.telegram.org` and `api.telegram.org` returns **403
  at the CONNECT tunnel** — 12 probes, 3 runs, 4 transports. Not a
  misconfiguration; the environment. Drives §8.3.
- Scheduled routines **do** place real unattended orders — 7 fills recorded, all
  fractional market orders with no session attached.
- `uvx`/`npx` MCP servers are stdio transports spawning a local process, and are
  therefore structurally unreachable from a cloud routine. Any routine-side
  provider must be a remote HTTP connector.
- `review_equity_order` can pass while `place_equity_order` fails. A passing
  review is not proof.
- `get_pnl_trade_history.side` is an empty string — unusable. Spans are presets
  only (`week|month|3month|ytd|all`), and `month` is shorter than the 31-day wash
  window, so always request `3month` and filter in Python.
- A lot acquired today returns `is_selectable: false` and cannot be chosen for a
  specified-lot sell until it settles.
