"""`surge on confirm` is refused now that SURGE is retired.

The core has ignored `full_send_enabled` since the retirement, but the verb
still set it and replied as if SURGE were armed. The refusal lives in
`ops/verbs.json` (`retired`), which `ops/control.py` reads. `surge off` still
works, so a flag left on can be cleared.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops import control
from src import plays

REPO = Path(__file__).resolve().parents[1]
SURGE = next(
    t for t in json.loads((REPO / "ops" / "verbs.json").read_text(encoding="utf-8"))["toggles"]
    if t["name"] == "surge"
)


class Recorder:
    def __init__(self):
        self.applied = []

    def apply_control(self, updates):
        self.applied.append(updates)


def test_the_verb_file_marks_surge_retired_while_the_play_is():
    assert plays.SURGE in plays.RETIRED
    assert "retired" in SURGE["retired"]


@pytest.mark.parametrize("arg", ["confirm", None, "CONFIRM"])
def test_surge_on_is_refused_and_writes_nothing(arg):
    client = Recorder()

    with pytest.raises(ValueError, match="retired"):
        control._apply(client, "surge_on", arg)

    assert client.applied == []


def test_surge_off_still_clears_the_flag():
    client = Recorder()

    assert control._apply(client, "surge_off", None) == {"full_send_enabled": "FALSE"}
    assert client.applied == [{"full_send_enabled": "FALSE"}]


def test_help_no_longer_offers_surge_on():
    line = next(l for l in control.HELP_TEXT.splitlines() if "surge" in l)

    assert "RETIRED" in line and "on confirm" not in line


def test_the_status_line_warns_surge_on_for_a_retired_play():
    py = (REPO / "ops" / "control.py").read_text(encoding="utf-8")
    assert 'not TOGGLES["surge"].get("retired")' in py
    assert "(SURGE retired, no effect)" in py


def test_the_manual_control_workflow_no_longer_offers_surge_on():
    workflow = (REPO / ".github" / "workflows" / "control.yml").read_text(encoding="utf-8")
    options = [line.strip() for line in workflow.splitlines() if line.strip().startswith("- ")]

    assert "- surge_on" not in options
    assert "- surge_off" in options
