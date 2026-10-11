"""The `cycle.json` key list, in one place, so two consumers cannot drift.

`docs/routine/cycle-prompt.md`'s Appendix D is the specification of what a
cycle payload may contain, and until now it existed only as a Markdown table.
Two things read that table:

* the live session following the prompt, which assembles `cycle.json` by hand;
* `services/core`, which assembles the same document from `edge_cycle_input`
  rows without a prompt in the loop.

A key added to one and not the other is the exact defect class that already
cost this project two production incidents (the `skipped_count` runlog
truncation and the hand-retyped `tradability` map) — prose drifting from what
the code does, invisible until a live cycle hits it. So the list lives here,
`tests/test_cycle_prompt_contract.py` asserts Appendix D still names exactly
these keys, and `services/core`'s own contract test asserts the assembler
emits nothing outside them.

**This is a manifest, not a schema.** It says which keys are legal, never
which are required — every cycle type carries a different subset, and Appendix
D's own rows say so ("reconciling cycles only", "postclose only", "omitted
entirely otherwise"). Nothing here validates a value.

Two things deliberately absent:

* `fetch_meta`, `held_for_news` and `reconcile`-derived carry-forwards that a *later* step of
  the same cycle adds to the document.
* `config`, `calendar`, `etfs`, `macro_map`, `inception`, `rotation_codes` — Appendix D's own
  closing line: `cli.py` reads all six from the checkout itself, and a cycle
  that supplied them would be handing the core its own rules.
"""

from __future__ import annotations

# Order follows Appendix D's table top to bottom. The order is documentation
# only — every consumer treats this as a set.
CYCLE_JSON_KEYS: tuple[str, ...] = (
    "cycle_id",
    "now",
    "control",
    "account",
    "positions",
    "orders",
    "fills",
    "tax_lots",
    "realized",
    "quotes",
    "day_context",
    "signals",
    "fundamentals",
    "earnings",
    "tradability",
    "mc_check",
    "snapshot",
    "events",
    "macro",
    "triggers",
    "macro_targets",
    "dip_survivors",
    "play_state",
    "prior_play_state",
    "loss_closes",
    "earnings_reactions",
    "open_orders",
    "intent",
    "prior_book",
    "rejections",
    "prior_positions",
    "symbol_lock",
    "dip_recovery_positions",
    "reconcile",
    "failure",
    "sectors",
    "regime_inputs",
    "manual_required",
    "blocked",
    "entries",
    "exits",
    "fill_quality_week",
    "shadow_week",
    "realized_trades_week",
)

CYCLE_JSON_KEY_SET = frozenset(CYCLE_JSON_KEYS)
