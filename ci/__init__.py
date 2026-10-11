"""CI fetchers — the only Python in this repo that touches the network.

Invariant 2 says the deterministic core has no network access, and
`tests/test_invariant_no_network.py` enforces it by walking the import graph of
`src/`. That scan deliberately stops at `src/`, which is what makes this package
possible: these scripts do the fetching, write JSON to `data/`, and commit it.
The core then reads a checked-out file, which is not network access.

The dependency runs one way. `ci/` imports `src.cadence` for the session
calendar — one definition of what a trading day is, rather than two that drift.
Nothing under `src/` may ever import from here.
"""
