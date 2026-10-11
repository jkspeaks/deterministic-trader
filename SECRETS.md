# Secrets

No real secret is ever committed to this repo. Real values live in four
places, none of them tracked by git:

1. **GitHub Actions secrets** (repo Settings → Secrets and variables →
   Actions) — used by `.github/workflows/*.yml`. These workflows are
   read-only extraction jobs (`ci/fetch_events.py`, `ci/fetch_macro.py`,
   `ci/fetch_universe.py`) plus a `contents: write` push of `data/*.json`;
   they don't need broker or notification credentials.
2. **A local `.env`** at the repo root (see `.env.example`), `chmod 600` on
   a POSIX host. Read by the offline `ops/` scripts (`ops/supabase_client.py`,
   `ops/control.py`) to reach Supabase. Never set as environment variables in
   a process listing or a crontab line — the offline ops scripts read the
   file directly.
3. **Supabase Vault**, read by SQL directly: `edge_telegram_bot_token`,
   `edge_telegram_chat_id`, `edge_core_url`, `edge_core_token`.
4. **The claude.ai routine's own configuration** — the scheduled session
   that runs `docs/routine/cycle-prompt.md` holds the Robinhood MCP
   connection, Supabase keys, and notification credentials it needs
   directly; those never pass through this repo at all.

## Required secrets

| Key | Used by | Purpose |
|---|---|---|
| `EDGE_SUPABASE_URL` / `EDGE_SUPABASE_KEY` | offline `ops/` scripts; GitHub Actions (both, as repository secrets) | Reads cycle/lock state from tables with no RLS policy (service_role key) |
| `CORE_TOKEN` | hosted core (Vercel env var, `<your-core-vercel-project>` project) + Supabase Vault (`edge_core_token`) | Shared secret in the body of every `POST /api/cycle` and `POST /api/premarket`; the only caller is Supabase `pg_net`, server-side. An unset value makes every request a 500, never an open door |
| `SUPABASE_URL` | hosted core (Vercel env var) | The project URL, read by `services/core/core/supabase.py`. Same project the offline `EDGE_SUPABASE_URL` points at, under the unprefixed name the core uses |
| `SUPABASE_SERVICE_ROLE_KEY` | hosted core (Vercel env var) | The routine already arrives as `service_role`; this is the second place that key lives, so `services/core` can read and write the same tables from a Vercel function |
| `TWELVE_DATA_API_KEY` | pre-market data job (`services/core/api/premarket.py`, Vercel env var, `<your-core-vercel-project>` project) | Optional for a local-only fork. Twelve Data Basic REST key. Read only by `services/core/core/twelvedata.py`, sent as the `apikey` query parameter to `api.twelvedata.com`. Never in the repo, never in a runlog row. Unset ⇒ the job fails closed (`TwelveDataError`) and writes nothing |
| `ALPHAVANTAGE_API_KEY` | evidence back-fill (`services/core/api/evidence.py`, Vercel env var, `<your-core-vercel-project>` project) | Optional. Alpha Vantage free REST key (25 requests a day). Read only by `services/core/core/alphavantage.py`, sent as the `apikey` query parameter to `www.alphavantage.co` for `EARNINGS_ESTIMATES`. Never in the repo, never in a runlog row, never in an error line. Unset ⇒ the job fetches bars as before and asks for no analyst estimate |
| Robinhood account credentials | scheduled claude.ai routine | Order placement — never stored in this repo; held by the routine's own MCP connection |
| Supabase service_role key (routine-side) | scheduled claude.ai routine | Cycle state, locking, reconcile bookkeeping |

Full field-level documentation (including why each one is shaped the way it
is) lives as comments in `.env.example` — copy that file rather than
retyping the keys.

## If a secret leaks

Rotate it immediately at the source (Robinhood, Supabase, Telegram
BotFather, the GitHub PAT settings) — deleting it from a future commit does
not remove it from git history. Then report it privately per
[SECURITY.md](SECURITY.md) if the leak happened through this repo, so the
exposure window and root cause get recorded.
