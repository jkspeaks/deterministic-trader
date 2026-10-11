"""`edge_prior` has no `intent` field, and two callers read that differently.

Live, in the shadow ledger's own refusal line: every variant
reported ``AttributeError: 'list' object has no attribute 'get'`` against the
one stored cycle. `reconcile._placements` reads ``ctx["intent"]["placements"]``;
`edge_prior` returns ``{placements, prior_book, rejections}``. A live
session posted the envelope and it worked; the runner posted the placements
array and `decide` refused the cycle outright.

The same defect class as `balances` a day earlier — prose naming a vocabulary
the database function does not use — so it gets the same treatment: the rename
lives in `cli.py`, and both shapes are pinned here so neither reading can go
quietly wrong again.
"""

from __future__ import annotations

from src.cli import _intent

PLACEMENTS = [
    {"symbol": "SPG", "side": "sell", "quantity": 1, "order_id": "1b70ca01"},
]


def test_the_envelope_a_live_session_posts_passes_through():
    # `edge_prior`'s whole result. `prior_book`/`rejections` ride along
    # untouched -- reconcile reads them from their own cycle.json keys, and
    # dropping them here would be this file inventing a schema.
    prior = {"placements": PLACEMENTS, "prior_book": {"positions": []}, "rejections": []}

    assert _intent({"intent": prior}) == prior


def test_the_bare_array_the_runner_posts_is_read_as_placements():
    assert _intent({"intent": PLACEMENTS}) == {"placements": PLACEMENTS}


def test_an_absent_key_is_no_placements_not_a_crash():
    # A monitor cycle that never reconciles posts no `intent` at all, and the
    # prior cycle placing nothing is a real answer.
    assert _intent({}) == {}
    assert _intent({"intent": None}) == {}


def test_neither_shape_is_read_as_neither_shape():
    # Absolute 2 and §14: a scalar under this key is a payload nobody has seen.
    # Empty is the fail-closed reading -- `_placements` treats it as "nothing
    # was placed", which raises MISSING_ORDER rather than approving a diff
    # against invented placements.
    assert _intent({"intent": "placements"}) == {}
    assert _intent({"intent": 7}) == {}


def test_the_result_is_a_copy_not_the_payload_row():
    # `reconcile` writes into the context it is handed; the stored
    # `edge_cycle_input` payload is evidence and must not be mutated behind it.
    prior = {"placements": PLACEMENTS}
    out = _intent({"intent": prior})
    out["placements"] = []

    assert prior["placements"] == PLACEMENTS
