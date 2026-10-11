"""The tax-lot join — the broker's field names becoming the ones `reconcile.py`
reads.

`get_equity_tax_lots` returns `tax_cost_basis` and `open_date` per lot, but `reconcile._basis`
and `_held_since` read `cost_basis` and `acquired_date`. `broker_shapes.tax_lots` (when the
four scattered renamers became one pass) is the permanent fix; these tests exist so the fixture
can never assert the derived shape as if it were the broker's again.

The rule they encode: the fixture supplies what the broker supplies, and the
derivation is what is under test.
"""

from src import broker_shapes, cli
from tests.factories import cli_payload


def reconciled(**overrides) -> dict:
    return cli.reconcile(cli_payload(**overrides), cli.load_config())


def test_broker_field_names_become_the_shape_reconcile_reads():
    result = broker_shapes.tax_lots(
        {"F": [{"quantity": 1.0, "tax_cost_basis": 15.00, "open_date": "2026-07-30"}]}
    )

    assert result["F"][0]["cost_basis"] == 15.00
    assert result["F"][0]["acquired_date"] == "2026-07-30"
    assert result["F"][0]["quantity"] == 1.0


def test_a_lot_missing_the_broker_fields_gains_neither_key():
    """Omitted, not nulled — `broker_shapes`' own rule for every key it
    touches, and identical downstream: `_basis` and `_held_since` both read
    these with `.get`, so an absent field and an explicit `None` are the same
    fact and only one of them is a value this module invented."""
    result = broker_shapes.tax_lots({"F": [{"quantity": 1.0}]})

    assert result["F"][0].get("cost_basis") is None
    assert result["F"][0].get("acquired_date") is None
    assert result["F"][0] == {"quantity": 1.0}


def test_no_tax_lots_key_renames_to_empty():
    assert broker_shapes.tax_lots({}) == {}
    assert broker_shapes.tax_lots(None) == {}


def test_the_baseline_cycle_reconciles_a_real_entry_price_from_the_raw_shape():
    """`cli_payload`'s F lot arrives as the broker sends it (`tax_cost_basis`,
    `open_date`)."""
    result = reconciled()

    position = next(p for p in result["positions"] if p["symbol"] == "F")
    assert position["entry_price"] == 15.00
    assert position["sessions_held"] is not None
