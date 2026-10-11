"""`edge_cycle_input` rows -> `cycle.json`, exactly as Appendix D describes it.

This module is the hosted replacement for the part of the cycle prompt that
says "copy the returned object into `cycle.json` as `control`, unchanged" —
forty-one times, in prose. It does the copying, and its whole contract is that
it copies: no rounding, no renaming, no filtering, no defaulting a missing key
to a plausible value. Absolute 2 ("never invent, round, average or recompute a
number") is a rule about the hands, and moving the hands into Python does not
retire it; `test_cycle_json_contract.py` is what holds it.

**The row shape.** The runner posts one `edge_cycle_input` row per fetch
result:

    (cycle_id, kind, symbol, payload)

* `kind` names an Appendix D key, or one of `EXTRA_KINDS` (the post-placement
  rows, which are cycle state but not cycle *input* — they exist only in the
  finalize phase and never enter `cycle.json`).
* `symbol` null means `payload` is the value for that key.
* `symbol` set means `payload` is one entry of a symbol-keyed map, which is
  how a batched `get_equity_quotes` of twenty names arrives as twenty rows and
  how a per-symbol `get_earnings_results` arrives as one.

**Merging is by kind, not by guesswork.** Two rows for the same key merge only
in the one way their type permits: lists concatenate (a second
`get_equity_orders` page extends the first), objects update (a second
fundamentals batch adds symbols), scalars replace. There is no case where the
merge has to decide between two values for the same path — if it ever does,
the later row wins and that is a fetch that ran twice, not a conflict this
module is entitled to resolve. `cli.merge_fetch` handles the genuinely
ambiguous version of this (two fragments claiming the same symbol) and refuses
it; nothing here reaches that situation because rows are appended by one
session in one order.

**An unknown kind is an error.** Not ignored, not passed through. A kind that
is neither in Appendix D nor in `EXTRA_KINDS` means the runner prompt and this
function have drifted, which is precisely the failure this whole module exists
to make impossible — so it fails the cycle rather than silently assembling a
document with a key the core will never read.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from src import broker_shapes
from src.cycle_keys import CYCLE_JSON_KEY_SET

# Rows that are cycle state but not `cycle.json` keys.
#
# `placements`/`placement_rejections`/`placement_alerts` are posted *after*
# placing and are addressed to the finalize phase (Step 8c's runlog bundle) —
# `cycle.json` is what the core decides *from*, and a placement is a fact
# about what the decision produced.
#
# **`placement_rejections`, not `rejections`.** `rejections` is already an
# Appendix D key with a different meaning: `edge_prior`'s list of *today's
# prior* rejections, which `cli._repeats` reads to find §14's same-order-
# refused-twice case. Two different lists under one name is how a cycle ends
# up treating this minute's refusal as a repeat of itself; the contract test
# in `test_cycle_json_contract.py` is what caught it.
#
# It is now an Appendix D key: `cli.candidates` merges it into `triggers`
# itself — and applies the operator's sector rotation to it — so the live
# session, the runner and the shadow ledger all merge in one place instead of
# `run._union_triggers` doing it for the runner alone.
#
# `balances` is `get_equity_orders`' opposite number: a real fetch step
# (`cli._fetch_plan` names it, `get_accounts` answers it) that is deliberately
# NOT an Appendix D key, because Appendix D has one `account` key that both
# account-scope tools feed. The live session merges them by hand under Step
# 6's rules; the runner posts each response verbatim under its own step name
# and holds no rules, so the merge is `cli.shape_account`'s and runs in
# `run._payload`. An early runner cycle died exactly here, on this row
# being refused — which was the correct refusal for the wrong reason: the row
# is legitimate, and what was missing was somewhere for it to go.
EXTRA_KINDS = frozenset(
    {
        "placements",
        "placement_rejections",
        "placement_alerts",
        "balances",
    }
)

# Keys `run.py` derives server-side rather than accepting from the runner.
# Listed here so a runner that posts one gets a loud refusal instead of having
# its value silently overwritten three lines later.
DERIVED_KINDS = frozenset({"triggers", "macro_targets", "reconcile"})


class AssemblyError(ValueError):
    """The rows do not describe a cycle this core will run."""


def assemble(rows: Iterable[Mapping[str, Any]]) -> tuple[dict, dict]:
    """Returns `(cycle_json, extras)`.

    `rows` must already be ordered by insertion (`edge_cycle_input.id`); the
    merge is order-dependent by construction and an unordered read would make
    a two-page fetch nondeterministic.
    """
    cycle: dict[str, Any] = {}
    extras: dict[str, Any] = {}

    for row in rows:
        kind = row.get("kind")
        if not isinstance(kind, str) or not kind:
            raise AssemblyError(f"edge_cycle_input row with no kind: {row!r}")

        if kind in EXTRA_KINDS:
            target = extras
        elif kind in DERIVED_KINDS:
            raise AssemblyError(
                f"{kind!r} is derived by the core, not posted by the runner — "
                "the runner prompt and services/core have drifted"
            )
        elif kind in CYCLE_JSON_KEY_SET:
            target = cycle
        else:
            raise AssemblyError(
                f"{kind!r} is not an Appendix D key and not one of "
                f"{sorted(EXTRA_KINDS)} — see src/cycle_keys.py"
            )

        symbol = row.get("symbol")
        payload = row.get("payload")

        if symbol:
            existing = target.get(kind)
            if existing is None:
                existing = {}
                target[kind] = existing
            if not isinstance(existing, dict):
                raise AssemblyError(
                    f"{kind!r} arrived both as a whole value and as a "
                    f"per-symbol entry ({symbol!r})"
                )
            existing[symbol] = payload
            continue

        target[kind] = _merge(target.get(kind, _ABSENT), payload, kind)

    return cycle, extras


class _Absent:
    __slots__ = ()


_ABSENT = _Absent()


def _merge(existing: Any, incoming: Any, kind: str) -> Any:
    if isinstance(existing, _Absent):
        return incoming
    if kind == "orders":
        # The fetch plan asks for orders twice (open, and recent — see
        # `cli._fetch_plan`), and the runner posts each response in the tool's
        # own `{"data": {"orders": [...]}}` envelope. Dict-merging two of those
        # would let the second replace the first's `data` wholesale, so the open
        # GTC targets would vanish whenever the recent row followed. Unwrap
        # both, concatenate in posting order, and keep the first copy of an
        # order that both windows returned. Rows are copied, not edited.
        return broker_shapes.order_rows([existing, incoming])
    if isinstance(existing, list) and isinstance(incoming, list):
        return existing + incoming
    if isinstance(existing, dict) and isinstance(incoming, dict):
        merged = dict(existing)
        merged.update(incoming)
        return merged
    # Mixed types, or a repeated scalar. The later row wins — see the module
    # docstring: this is a fetch that ran twice, not a conflict.
    return incoming
