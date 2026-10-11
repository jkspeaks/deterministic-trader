"""`step`.

`ops/control.py`'s `format_step` renders the in-progress step for the operator
control surface (GitHub Actions).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ops import control

REPO = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 9, 29, 14, 0, 0, tzinfo=timezone.utc)  # 10:00:00 ET (EDT)


def session(**overrides):
    row = {
        "cycle_id": "9e372369-035c-40b1-b8bf-b487ca4d5cd9",
        "cycle_type": "full",
        "status": None,
        "started_at": "2026-09-29T13:41:28.678526+00:00",
        "finished_at": None,
        "current_step": "S2a.post.quotes",
        "current_step_at": "2026-09-29T13:56:00+00:00",
        "last_beat_at": "2026-09-29T13:58:30+00:00",
        "rows_written": 152,
    }
    row.update(overrides)
    return row


def test_a_running_cycle_names_its_step_and_when_it_entered_it():
    assert control.format_step(session(), NOW) == [
        "full — running",
        "in S2a.post.quotes since 09:56:00 ET (4m 00s ago)",
        "started 09:41 ET · running 18m 31s",
        "last row 09:58:30 ET (1m 30s ago)",
        "152 rows posted",
    ]


def test_a_finished_cycle_reports_where_it_ended_and_how_long_it_ran():
    lines = control.format_step(
        session(
            status="success_with_warnings",
            finished_at="2026-09-29T16:44:06+00:00",
            current_step="S6.session_finish",
            current_step_at="2026-09-29T16:44:06+00:00",
        ),
        NOW,
    )
    assert lines[0] == "full — success_with_warnings"
    assert lines[1] == "ended in S6.session_finish at 12:44:06 ET"
    assert lines[2].startswith("started 09:41 ET · ran 3h 02m")
    assert not any(l.startswith("last row") for l in lines)


def test_a_cycle_with_no_step_yet_says_so():
    lines = control.format_step(session(current_step=None, current_step_at=None, last_beat_at=None, rows_written=0), NOW)
    assert lines[:2] == ["full — running", "no step recorded yet"]


def test_no_session_at_all():
    assert control.format_step(None, NOW) == ["no session recorded yet"]


def test_a_garbled_timestamp_degrades_instead_of_raising():
    lines = control.format_step(session(current_step_at="not a time", started_at="nope", last_beat_at=""), NOW)
    assert lines[0] == "full — running" and "no step recorded yet" in lines


def test_step_is_a_known_verb_with_help_text():
    assert control.parse_verb(["step"]) == ("step", None)
    assert "step" in control.VERBS
    assert "step" in control.HELP_TEXT

    defs = json.loads((REPO / "ops" / "verbs.json").read_text(encoding="utf-8"))
    single = {s["name"]: s for s in defs["single"]}
    assert single["step"]["readonly"] is True
