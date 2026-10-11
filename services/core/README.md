# services/core — the hosted core

`src/` is the brain. This is the brain running somewhere other than inside a
Claude session.

Today a cycle is a session that clones the repo, runs `uv sync`, reads a
911-line prompt, calls `src.cli` four times and hand-pastes multi-KB JSON into
`edge_runlog_write`. With this service, the session posts raw tool output into
`edge_cycle_input`, asks Postgres to run the core, and places what comes back
— `docs/routine/runner-prompt.md`, 200 lines, no Python and no arithmetic.

Read that first.

## Layout

```
services/core/
  api/cycle.py        the cycle HTTP handler and the auth boundary
  api/premarket.py    the pre-market data job's handler (same auth, same token)
  api/watch.py        the 30-minute state-change gate's handler (same auth, same token)
  api/shadow.py       the shadow ledger's replay handler (same auth, same token)
  api/evidence.py     the evidence back-fill's handler (same auth, same token)
  core/assemble.py    edge_cycle_input rows -> cycle.json (Appendix D)
  core/run.py         the two cycle phases; Steps 5-10 of cycle-prompt.md
  core/premarket.py   the pre-market job: regime + day-context SMAs + PEAD
  core/watch.py       the state-change gate: one TD quote batch vs edge_protection
  core/shadow.py      the shadow ledger: cli.decide replayed per rule variant,
                      plus the control arm (signal vs seeded random sector peer)
  core/evidence.py    forward returns + price features onto edge_decision_snapshot
  core/supabase.py    the service-role PostgREST client
  core/twelvedata.py  the Twelve Data REST client (stdlib, batched, budgeted)
  requirements.txt    documentation only; the build installs from pyproject
  tests/              in the root suite (see pyproject testpaths)
```

**Four functions, one project.** `api/cycle.py` runs a cycle server-side;
`api/premarket.py` is the pre-market deterministic data job that fills
`edge_day_context` SMAs, `edge_regime`, and `edge_earnings_reactions`;
`api/watch.py` is the 30-minute state-change gate that sets
`edge_control.needs_session` so `edge_should_run` can skip a monitor slot
whose book has not moved; `api/shadow.py` is the shadow ledger's nightly
replay, which re-runs `cli.decide` over the stored `edge_cycle_input` rows
once per named rule variant and writes the hypothetical trades to
`edge_shadow_trades`. A fifth, `api/evidence.py` (it
also records analyst estimates on the rows a play judged, from Alpha
Vantage, after the bars), back-fills forward returns and price-history
features onto the `edge_decision_snapshot` rows `core/run.py` writes each
entry-capable cycle. All of them are POST-only, check the same `CORE_TOKEN`,
and are called only by Supabase `pg_net`.

**The pre-market job is a tick, not a run.** Twelve Data Basic throttles at
8 credits/minute, so one invocation executes at most one Twelve Data
request and persists its progress to `edge_premarket_state` for
the next invocation to pick up. `pg_cron` fires it every two minutes across
the pre-market window; a finished day makes every later tick a free no-op.

**The watch job is one quote batch, not a tick.** It has no cross-invocation
state: at most `len(held symbols) + 1` (SPY) Twelve Data credits per
invocation, well inside the per-minute limit, so unlike the pre-market job it
never needs `edge_premarket_state`'s persist-and-resume shape — every
30-minute firing either sets `needs_session` or leaves it alone and returns.

**The shadow job places nothing and is safe to call by hand.** It holds no
broker credential, reads rows that already exist, and writes one table
nothing but the weekly scorecard reads. `{"write": false}` runs the whole
replay and returns the trades without touching the ledger. It is the one
endpoint here with a 300s `maxDuration`: five variants over ten days of
stored cycles, plus paced Twelve Data bar requests for the symbols that
were entered.

## Deployment — this deploys from the repository root

`api/cycle.py` does `import src`, and `cli.load_config` reads `config/` and
`data/` out of the checkout. So the Vercel **Root Directory for this project
is the repository root**, not `services/core`, and the live configuration is
therefore:

* `/vercel.json` — `maxDuration: 60` on `api/cycle.py`, `api/premarket.py`
  and `api/watch.py`; 300 on `api/shadow.py` and `api/evidence.py`
* `/requirements.txt` — one line, `-r services/core/requirements.txt`
* `/api/cycle.py`, `/api/premarket.py`, `/api/watch.py`, `/api/shadow.py` —
  six-line shims each,
  because Vercel's Python builder detects functions at `<root>/api/*.py` and
  the handler itself lives in this package
* `/.vercelignore` — keeps `tests/`, `db/`, `docs/`, `ci/`, `dashboard/` out of
  the bundle

There is deliberately **no `vercel.json` in this directory**. One that is
never read is worse than none: the next person to change `maxDuration` would
change the wrong file and watch nothing happen.

### Environment variables (Vercel project settings)

| Name | What |
|---|---|
| `CORE_TOKEN` | Matched against the `token` in every request body. Unset ⇒ every request is a 500, never an open door |
| `SUPABASE_URL` | `https://<project>.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | The routine already arrives as `service_role`; this is the second place that key lives, which is the real cost of this migration |
| `TWELVE_DATA_API_KEY` | `api/premarket.py` only. Twelve Data Basic REST key, sent as the `apikey` query param. Unset ⇒ the pre-market job fails closed and writes nothing |
| `ALPHAVANTAGE_API_KEY` | `api/evidence.py` only. Alpha Vantage free key (25 requests a day), for analyst estimates on judged snapshot rows (S2). Unset ⇒ no estimate is requested; nothing else changes |

### Supabase Vault (read by `edge_cycle_dispatch` and `edge_premarket_dispatch`)

```sql
select vault.create_secret('https://<project>.vercel.app', 'edge_core_url');
select vault.create_secret('<CORE_TOKEN>',                 'edge_core_token');
```

## Running the tests

They are in the root suite:

```bash
uv run pytest                        # everything
uv run pytest services/core/tests    # just this service
```

The acceptance test is
`test_run.py::test_the_hosted_plan_is_the_plan_cli_decide_returns_for_the_same_payload`:
it drives the hosted core with `tests/factories.cli_payload()` — the same
payload every `cli.decide` test uses — and asserts the orders, protection and
cancels are identical. If that ever fails, the hosted core and the local core
have started answering the same question differently, and nothing else in this
directory matters until it passes again.

## Two things that will bite you

**`POST` only; the `GET` is a deliberate 405.** A session can only reach a
Vercel URL through `web_fetch_vercel_url`, which is GET-only. Giving this
endpoint a GET would make it reachable from a session — and a session could
then run a cycle it never claimed the §13.1 lock for. The only caller is
Supabase, server-side, via `pg_net`.

**`edge_cycle_dispatch` and `edge_cycle_await` are two SQL calls and must stay
two statements.** `net.http_post` enqueues rather than sends; the queue insert
obeys MVCC, so a function that enqueues and then polls in the same transaction
is waiting on a request it is itself blocking. The migration says this at the
definition and the design doc explains it in full.
