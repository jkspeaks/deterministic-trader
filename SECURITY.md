# Security Policy

## Scope

deterministic-trader is a personal, single-account trading system.
The deterministic core (`src/`) has no network access — that's enforced by a
test, not a convention — so most of the attack surface is in:

- the CI extraction jobs (`ci/`, `.github/workflows/`)
- the scheduled routine that reads `docs/routine/cycle-prompt.md` and calls
  the broker/notification APIs
- the hosted core (`services/core/`), a small set of Vercel functions the
  routine and Supabase `pg_net` call into

## Reporting a vulnerability

If you find a security issue — a way to leak credentials, bypass a guard
check, or make CI or the routine do something it shouldn't — please report it
privately rather than opening a public issue:

- Use GitHub's [private vulnerability reporting](https://github.com/<owner>/deterministic-trader/security/advisories/new)
  for this repo, or
- Email you@example.com with a description and, if possible, reproduction
  steps.

Please don't open a public issue or PR that discloses the vulnerability
before it's been addressed.

## What to expect

This is a single-maintainer hobby project, not a funded security program.
There's no bug bounty. I'll acknowledge reports within a few days and fix
confirmed issues as soon as I reasonably can, prioritizing anything that
could move money or leak the credentials listed in `.env.example`.

## Secrets

Real credentials are never committed — see `.gitignore` and `.env.example`.
If you believe a secret has leaked into this repo's history, report it
privately (above) rather than filing a public issue, since a public report of
a live credential is itself a disclosure.
