# Contributing

deterministic-trader is a personal trading system, developed for a single live
account. It's open for reading, forking, and issue reports; PRs are welcome
but reviewed against that context — a change that's a good idea for a
general-purpose framework may still not be a good idea for this specific
account's risk model.

## Before opening a PR

- Read the "Design decisions" section of `README.md`, `docs/TUNING.md` and
  `docs/DESIGN.md` first. A lot of things that look like oversights are
  deliberate and documented there.
- Keep `src/` free of network access. That boundary is enforced by a test
  (`tests/` — search for the no-network check); PRs that add an import
  capable of reaching the network to anything under `src/` will fail CI.
- Run `uv run pytest` locally before opening the PR.
- Small, focused PRs over large ones. This is a low-traffic repo maintained
  by one person alongside a live account — large diffs are hard to review
  carefully, and carefulness matters more here than usual.

## Developer Certificate of Origin

By submitting a PR, you certify that you wrote the change yourself (or have
the right to submit it under an open source license) and agree to license
your contribution under this project's [MIT License](LICENSE). This is the
same standard as the [DCO](https://developercertificate.org/) — no separate
sign-off line is required, but don't submit code you don't have the rights
to.

## Reporting bugs vs. security issues

Regular bugs: open a GitHub issue. Security vulnerabilities (credential
handling, guard bypasses, anything that could move money unexpectedly): see
[SECURITY.md](SECURITY.md) instead — don't file those as public issues.
