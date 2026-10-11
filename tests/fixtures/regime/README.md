# `tests/fixtures/regime/` — `edge_regime` rows as the pre-market job wrote them

Each file is one `edge_regime.payload`, read from Supabase `deterministic-trader`
and saved verbatim. This is the exact block `services/core/core/run.py` copies into a runner
cycle's `regime_inputs`, so a test that feeds one through `regime.evaluate` / `cli.decide` /
`core.run` is judging what production judged on that day. Same rule as
`tests/fixtures/broker/`: **never edit one to make a test pass.**

| File | Day | What it shows |
|---|---|---|
| `edge_regime_2026-09-18.json` |  | SPY above both averages, 5 of 11 sector ETFs at or above their 200-day. |
| `edge_regime_2026-10-02.json` |  | SPY above both averages (`risk_on`), 3 of 11 sector ETFs at or above their 200-day. |

Both are older rows, so the sector blocks carry only
`last` / `sma200` / `rsi14` — no `sma50`, no `ret_1m` / `ret_3m`. That is the
point: `breadth_50` and sector relative strength must read as unavailable on
these rows rather than crash or block.
