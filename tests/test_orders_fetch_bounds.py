"""Nothing asks the broker for an account's whole order history.

The runner's bare `get_equity_orders` was 66k characters, the harness spilled
it to a file the runner may not read back, and the cycle aborted in Step 2.
The fix is in `cli._fetch_plan` and `assemble`; this file pins the parts that
live in prose, because a prompt edit is how it comes back.
"""

import re
from pathlib import Path

from src import cli

ROUTINE = Path(__file__).resolve().parents[1] / "docs" / "routine"


def prompt(name: str) -> str:
    return (ROUTINE / name).read_text(encoding="utf-8")


def test_the_lookback_outlasts_a_resting_take_profit():
    """A GTC target rests until it fills or the time stop (~12 calendar days for
    DIP, plus the bounded loss-hold). One that fills after leaving the window
    reads as a prior placement the broker does not list: `MISSING_ORDER`."""
    assert cli.ORDERS_LOOKBACK_DAYS >= 21


def test_no_routine_prompt_passes_a_multi_value_state():
    """`get_equity_orders.state` takes one value. `queued,confirmed` matched
    nothing, so the dip monitor's `open_orders` was silently empty."""
    offenders = {
        path.name: re.findall(r"state=\w+,\w+", path.read_text(encoding="utf-8"))
        for path in ROUTINE.glob("*.md")
    }

    assert {name: hits for name, hits in offenders.items() if hits} == {}


def test_the_dip_monitor_bounds_its_filled_read_and_reads_resting_orders():
    text = prompt("dip-monitor-prompt.md")

    assert re.search(r"state=filled\s+created_at_gte=", text)
    assert "state=confirmed" in text


def test_the_runner_prompt_names_where_account_number_comes_from():
    """Every account-scoped Robinhood tool requires it and no fetch row carries
    it. The runner takes it from its trigger prompt; the prompt has to say so,
    or the next reader assumes the rows are incomplete."""
    text = prompt("runner-prompt.md")

    assert "`account_number` is the one parameter no row carries" in text
    assert "Never read it off\n`get_accounts`" in text.replace("\r\n", "\n")


def test_the_runner_prompt_tells_it_to_post_both_orders_rows():
    text = prompt("runner-prompt.md").replace("\r\n", "\n")

    assert "The `orders` step is two rows, on purpose" in text
    assert "never widen a row" in text
