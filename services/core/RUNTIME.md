# Why `vercel.json` names no runtime

The first deploy of this service failed at build with:

    Error: Function Runtimes must have a valid version, for example `now-php@1.0.0`.

`functions[].runtime` is for **community** runtimes and wants a versioned npm
package (`@vercel/python@4.3.0`). Vercel's built-in Python runtime is selected
by the file extension — `api/cycle.py` is a Python function because it ends in
`.py` — and naming `"runtime": "python3.11"` there is not "pin the version",
it is "use a community runtime called python3.11", which does not exist.

So `vercel.json` carries `maxDuration` and nothing else. The Python version
is the project's, set in Vercel's project settings, not in this file.

`maxDuration: 60` is the other half of the design doc's open [V]; whether this
account's plan accepts it is answered by the build log, not by this file.

## Where the Python version comes from

The Vercel **project settings**, not this repo. The build resolves to
`python3.12` (visible in the built function's `.vc-config.json`) and `src/`
asks only for `>=3.11`. A `.python-version` file appears at the repo root
after a local `vercel build`; it is gitignored, because committing it would
also repoint the local interpreter, which runs on the 3.11.8 global.

## What the first successful build settled

- `maxDuration: 60` — **accepted**. That closes the design doc's open [V] on
  the Hobby duration ceiling.
- Runtime `python3.12`, architecture `x86_64`.
- The bundle is 1,378 files, of which 1,314 are vendored `httpx` and `tzdata`.
  The 64 that matter: `src/` (38), `services/core/` (10), `config/` (5),
  `data/` (3), `api/cycle.py`. `excludeFiles` in `vercel.json` is what keeps
  the rest out — including `.env.local`, which holds the Vercel OIDC token and
  was found sitting in a local bundle before that was added.
