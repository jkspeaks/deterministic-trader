"""`src/guard/mc.py`'s contract with the `ctx["mc_check"]` payload.

The fixture this reads (`tests/fixtures/mc_check_live_response.json`) is a
representative response captured from one Monte Carlo bootstrap call — not a
hand-typed guess at the payload's shape. This repo's dominant defect class is
fixtures describing a world production doesn't produce (tax_lots, tradability,
earnings all bit it this way); this test exists so a schema change in the
payload is caught here rather than by every candidate silently reading
`MC_STOP_PROBABILITY_EXCEEDED`/`MC_DRAWDOWN_P95_EXCEEDED` as `None` and never
denying anything.
"""

import json
from pathlib import Path

from src import guard
from tests.factories import codes, context, order

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "mc_check_live_response.json")
    .read_text(encoding="utf-8")
)


def test_the_live_response_has_every_field_guard_mc_reads():
    response = FIXTURE["response"]

    assert set(response) >= {
        "p_stop", "p_target", "drawdown_p5", "drawdown_p50", "drawdown_p95",
        "n_paths", "horizon_days",
    }
    assert isinstance(response["p_stop"], float)
    assert isinstance(response["drawdown_p95"], float)


def test_a_real_favorable_response_is_approved_through_guard():
    # F's real read (p_stop 0.0507, drawdown_p95 0.0704) sits well under both
    # config.json defaults (0.35, 0.15) — this candidate should clear guard.
    result = guard.evaluate(
        [order()], context(mc_check={"F": FIXTURE["response"]})
    )

    assert codes(result, "F") == []
    assert result.approved == [order()]


def test_the_captured_error_bodys_shape_is_what_the_routine_treats_as_absent():
    # Step 6b (cycle-prompt.md): a non-200 means the symbol is left out of
    # mc_check entirely, not mapped from the error body. Confirm the error
    # body has no "p_stop"/"drawdown_p95" a careless routine step could
    # accidentally forward instead of omitting the symbol.
    error = FIXTURE["error_response"]

    assert error["status"] != 200
    assert "p_stop" not in error["body"]
    assert "drawdown_p95" not in error["body"]
    assert "error" in error["body"]
