"""`control` replies, as a phone shows them (operator request).

`ops/status_view.py` renders the status, the reply to a write, and the sectors
view. The tests pin what the operator asked for: plain labels instead of key
names, groups, no sector list unless asked, and a write that answers with one
line plus the switches.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops import control, sector_verbs, status_view

REPO = Path(__file__).resolve().parents[1]
DEFS = json.loads((REPO / "ops" / "verbs.json").read_text(encoding="utf-8"))
SECTORS = DEFS["lists"][0]

CONTROL = {
    "readable": True,
    "trading_enabled": True,
    "entries_blocked": False,
    "entries_blocked_reason": None,
    "entries_blocked_until_cleared": False,
    "divergence_streak": 0,
    "full_send_enabled": False,
    "positions_held": 1,
    "high_water_mark": 1264.01,
    "high_water_mark_as_of": "2026-08-17",
    "last_full_cycle": "2026-10-06T13:42:17Z",
    "cycle_in_progress": "none",
    "unreconciled": False,
    "active_sectors": ["CHIP", "BIO", "FOOD"],
}
DIP = {"core_strategies_enabled": True, "dip_recovery_enabled": True}
MANY = ["OFS", "AMGT", "CFIN", "DBNK", "EXCH", "IBRK", "HOLD", "APPS", "CEQ", "EEI", "EMS",
        "SME", "CHIP", "SYSW", "HW", "BIO", "HCIT", "LSTS", "PHRM", "AERO", "FARM"]
BLOCKED = {
    **CONTROL,
    "entries_blocked": True,
    "entries_blocked_reason": "waiting on the Fed minutes before adding anything new",
    "entries_blocked_until_cleared": True,
}

# (name, function, arguments) — every case both twins must agree on.
CASES = [
    ("status", "status", [CONTROL, DIP, None, ["CHIP", "BIO", "FOOD"]]),
    ("status_all_sectors", "status", [CONTROL, DIP, None, []]),
    ("status_one_sector", "status", [CONTROL, DIP, 0, ["CHIP"]]),
    ("status_blocked", "status", [BLOCKED, DIP, None, MANY]),
    ("status_streak", "status", [{**BLOCKED, "divergence_streak": 3}, DIP, None, []]),
    ("status_off", "status", [{**CONTROL, "trading_enabled": False}, {**DIP, "dip_recovery_enabled": False}, None, []]),
    ("status_attention", "status", [{**CONTROL, "full_send_enabled": True, "unreconciled": True}, DIP, 4, MANY]),
    ("status_sparse", "status", [{"readable": True}, {}, None, []]),
    ("status_unreadable", "status", [{"readable": False}, {}, None, []]),
    ("sectors", "sectors", [["CHIP", "BIO", "FOOD"]]),
    ("sectors_many", "sectors", [MANY]),
    ("sectors_all", "sectors", [[]]),
    ("sectors_help", "sectors_help", [SECTORS]),
    ("applied_dip_off", "applied", ["dip_off", {"dip_recovery_enabled": "FALSE"}, CONTROL, {**DIP, "dip_recovery_enabled": False}, MANY]),
    ("applied_master_on", "applied", ["master_on", {"trading_enabled": "TRUE"}, CONTROL, DIP, MANY]),
    ("applied_core_off", "applied", ["core_off", {}, CONTROL, {**DIP, "core_strategies_enabled": False}, []]),
    ("applied_surge_off", "applied", ["surge_off", {"full_send_enabled": "FALSE"}, CONTROL, DIP, MANY]),
    ("applied_block_on", "applied", ["block_on", {}, BLOCKED, DIP, MANY]),
    ("applied_block_off", "applied", ["block_off", {}, CONTROL, DIP, MANY]),
    ("applied_clear", "applied", ["clear", {}, CONTROL, DIP, MANY]),
    ("applied_sectors", "applied", ["sectors_set", {"active_sectors": MANY}, CONTROL, DIP, MANY]),
    ("applied_sectors_clear", "applied", ["sectors_clear", {"active_sectors": []}, CONTROL, DIP, []]),
    ("applied_holds", "applied", ["holds", {"operator_holds_removed": "MU"}, CONTROL, DIP, MANY]),
    ("applied_unknown", "applied", ["something_new", {}, CONTROL, DIP, []]),
]


def render(function: str, args: list) -> str:
    return getattr(status_view, function)(*args)


def lines_of(name: str) -> list[str]:
    function, args = next((f, a) for n, f, a in CASES if n == name)
    return render(function, args).split("\n")


# --- what the operator asked for ----------------------------------------------


def test_status_reads_as_groups_of_plain_labels():
    assert lines_of("status") == [
        "Trading ON · entries open",
        "",
        "SWITCHES",
        "Trading (master)  Enabled",
        "Core strategies   Enabled",
        "Dip Recovery      Enabled",
        "Entry block       Off",
        "",
        "ACCOUNT",
        "Positions held    1",
        "High-water mark   1,264.01",
        "  set 2026-08-17",
        "Last full cycle   2026-10-06",
        "Cycle running     none",
        "Book reconciled   Yes",
        "",
        "Sectors: 3 codes active",
        "List: sectors",
    ]


@pytest.mark.parametrize("name", [n for n, f, _ in CASES if f in ("status", "applied")])
def test_no_reply_shows_a_stored_key_name_or_a_raw_boolean(name):
    text = "\n".join(lines_of(name))

    for raw in ("_enabled", "entries_blocked", "high_water_mark", "positions_held",
                "cycle_in_progress", "True", "False", "TRUE", "FALSE", "None", "null", "{"):
        assert raw not in text, raw


def test_status_never_lists_the_sectors_however_many_are_active():
    text = "\n".join(lines_of("status_attention"))

    assert "Sectors: 21 codes active" in text and "sectors" in text
    assert "AMGT" not in text


def test_attention_appears_only_when_something_needs_it():
    assert "ATTENTION" not in lines_of("status")

    lines = lines_of("status_attention")
    assert "ATTENTION" in lines
    assert "- Benchmark is 4 days behind" in lines
    assert any("SURGE flag is on" in line for line in lines)
    assert "Book reconciled   No" in lines


def test_a_block_says_why_and_how_it_ends():
    lines = lines_of("status_blocked")

    assert lines[0] == "Trading ON · entries BLOCKED ⚠"
    at = lines.index("Entry block       ON")
    assert lines[at + 1].startswith("  why: waiting on the Fed")
    assert "  stays until clear" in lines
    assert "  why: divergence streak 3" in lines_of("status_streak")


def test_a_write_answers_with_one_line_and_the_switches_only():
    assert lines_of("applied_dip_off") == [
        "✓ Dip Recovery: Disabled",
        "",
        "SWITCHES",
        "Trading (master)  Enabled",
        "Core strategies   Enabled",
        "Dip Recovery      Disabled",
        "Entry block       Off",
    ]
    assert lines_of("applied_surge_off")[0] == "✓ SURGE flag cleared"
    assert lines_of("applied_clear")[0] == "✓ Block cleared, streak reset"
    assert lines_of("applied_holds")[0] == "✓ Hold removed: MU"


@pytest.mark.parametrize("name", [n for n, f, a in CASES if f == "applied" and not a[0].startswith("sectors_")])
def test_a_switch_write_shows_no_sectors_and_no_account(name):
    text = "\n".join(lines_of(name))

    assert "ACCOUNT" not in text and "Sectors" not in text and "AMGT" not in text


def test_a_sectors_write_answers_with_the_sectors_and_not_the_switches():
    lines = lines_of("applied_sectors")

    assert lines[:3] == ["✓ Sectors updated", "", "ACTIVE SECTORS (21 codes)"]
    assert "SWITCHES" not in lines
    assert lines_of("applied_sectors_clear")[2] == "ACTIVE SECTORS (all)"


def test_the_sectors_view_lists_every_active_code():
    lines = lines_of("sectors_many")

    assert " ".join(lines[2:]).split() == MANY


@pytest.mark.parametrize("name", [n for n, _, _ in CASES])
def test_every_line_fits_a_phone(name):
    for line in lines_of(name):
        assert len(line) <= status_view.WIDTH, line


def test_help_is_grouped_and_fits_a_phone():
    lines = control.HELP_TEXT.split("\n")

    assert [l for l in lines if l.isupper()] == ["VIEW", "SWITCHES", "SECTORS", "OTHER"]
    assert max(len(l) for l in lines) <= 34
    for verb in ("status", "sectors", "last", "step", "holds", "master", "core", "dip",
                 "block", "clear", "kill", "help"):
        assert any(l.startswith(f"{verb}") for l in lines), verb


# --- the implementation --------------------------------------------------------


def test_the_cli_uses_the_view_for_status_writes_and_sectors():
    class Client:
        def get_control(self):
            return CONTROL

        def get_dip_config(self):
            return DIP

        def get_benchmark_latest(self):
            return None

    status = control._read_status(Client())
    assert "Trading \\(master\\)  Enabled" in status and "trading_enabled" not in status

    sectors = control._read_sectors(CONTROL)
    assert "ACTIVE SECTORS \\(3 codes\\)" in sectors and "CHANGE" in sectors
    assert sector_verbs.current_set(CONTROL["active_sectors"], SECTORS) == ["CHIP", "BIO", "FOOD"]
