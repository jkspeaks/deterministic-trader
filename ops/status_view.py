"""ops/status_view.py — what `control` replies look like, pure.

Written for a phone: lines of 30 characters or fewer, one idea per group,
and nothing shown that was not asked for.

* `status` — one headline, then SWITCHES, ACCOUNT, and ATTENTION only when
  something needs it. Sectors are one count line; the list is `sectors`.
* `applied` — the reply to a write: one line saying what changed, then the
  switches. A sectors write shows the sectors instead.
* `sectors` — the active set, then how to change it.

No I/O. `tests/test_ops_status_view.py` pins the exact text.
"""

from __future__ import annotations

from typing import Any, List, Mapping, Optional, Sequence

WIDTH = 30
LABEL = 17
OK = "✓"
WARN = "⚠"

SWITCH_LABELS = {
    "master": "Trading (master)",
    "core": "Core strategies",
    "dip": "Dip Recovery",
}


def row(label: str, value: str) -> str:
    """`label` padded to one column, then `value`."""
    return label.ljust(LABEL) + " " + value


def enabled(value: Any) -> str:
    return "Enabled" if value else "Disabled"


def money(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def wrap(words: Sequence[str], width: int = WIDTH) -> List[str]:
    """Space-joined `words`, broken into lines no wider than `width`."""
    lines: List[str] = []
    line = ""
    for word in words:
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}" if line else word
    if line:
        lines.append(line)
    return lines


def headline(c: Mapping[str, Any]) -> str:
    trading = "Trading ON" if c.get("trading_enabled") else f"Trading OFF {WARN}"
    entries = f"entries BLOCKED {WARN}" if c.get("entries_blocked") else "entries open"
    return f"{trading} · {entries}"


def switch_lines(c: Mapping[str, Any], d: Mapping[str, Any]) -> List[str]:
    lines = [
        "SWITCHES",
        row(SWITCH_LABELS["master"], enabled(c.get("trading_enabled"))),
        row(SWITCH_LABELS["core"], enabled(d.get("core_strategies_enabled"))),
        row(SWITCH_LABELS["dip"], enabled(d.get("dip_recovery_enabled"))),
    ]
    if c.get("entries_blocked"):
        lines.append(row("Entry block", "ON"))
        streak = c.get("divergence_streak")
        why = f"divergence streak {streak}" if streak else (c.get("entries_blocked_reason") or "manual")
        lines += ["  " + line for line in wrap(f"why: {why}".split(), WIDTH - 2)]
        if c.get("entries_blocked_until_cleared"):
            lines.append("  stays until clear")
    else:
        lines.append(row("Entry block", "Off"))
    return lines


def attention_lines(c: Mapping[str, Any], lag: Optional[int]) -> List[str]:
    notes: List[str] = []
    if not c.get("trading_enabled"):
        notes.append("Trading is off: nothing is bought or sold")
    if c.get("full_send_enabled"):
        notes.append("SURGE flag is on. SURGE is retired, so it does nothing. Clear: surge off")
    if lag is not None and lag > 1:
        notes.append(f"Benchmark is {lag} days behind")
    lines: List[str] = []
    for note in notes:
        wrapped = wrap(note.split(), WIDTH - 2)
        lines.append("- " + wrapped[0])
        lines += ["  " + line for line in wrapped[1:]]
    return lines


def sector_count_line(active: Sequence[str]) -> str:
    if not active:
        return "Sectors: all"
    noun = "code" if len(active) == 1 else "codes"
    return f"Sectors: {len(active)} {noun} active"


def status(
    c: Mapping[str, Any], d: Mapping[str, Any], lag: Optional[int], active: Sequence[str]
) -> str:
    if not c.get("readable"):
        return "control: UNREADABLE"

    lines = [headline(c), ""]
    lines += switch_lines(c, d)

    lines += ["", "ACCOUNT"]
    lines.append(row("Positions held", str(c.get("positions_held") if c.get("positions_held") is not None else "n/a")))
    lines.append(row("High-water mark", money(c.get("high_water_mark"))))
    if c.get("high_water_mark_as_of"):
        lines.append("  set " + str(c.get("high_water_mark_as_of"))[:10])
    last_full = str(c.get("last_full_cycle") or "")[:10]
    lines.append(row("Last full cycle", last_full or "n/a"))
    lines.append(row("Cycle running", str(c.get("cycle_in_progress") or "none")))
    lines.append(row("Book reconciled", "No" if c.get("unreconciled") else "Yes"))

    attention = attention_lines(c, lag)
    if attention:
        lines += ["", "ATTENTION"] + attention

    lines += ["", sector_count_line(active), "List: sectors"]
    return "\n".join(lines)


def sectors(active: Sequence[str]) -> str:
    """The active set, then how to change it."""
    if active:
        noun = "code" if len(active) == 1 else "codes"
        lines = [f"ACTIVE SECTORS ({len(active)} {noun})", "New money goes only here."]
        lines += wrap(list(active))
    else:
        lines = ["ACTIVE SECTORS (all)", "No restriction on new money."]
    return "\n".join(lines)


def sectors_help(defs: Mapping[str, Any]) -> str:
    codes = defs.get("codes") or {}
    lines = [
        "CHANGE",
        "sectors set EN,FIN",
        "sectors add CHIP",
        "sectors remove CHIP",
        "sectors clear  (all)",
        "",
        "SECTOR CODES",
    ]
    lines += wrap(list((codes.get("sectors") or {}).keys()))
    lines += ["", "BASKETS"]
    lines += wrap(list((codes.get("baskets") or {}).keys()))
    lines += ["", f"+{len(codes.get('sub_industries') or {})} sub-industry codes:", "docs/ROTATION_CODES.md"]
    return "\n".join(lines)


def confirmation(verb: str, applied: Mapping[str, Any]) -> str:
    """One line saying what a write did."""
    for name, label in SWITCH_LABELS.items():
        if verb == f"{name}_on":
            return f"{OK} {label}: Enabled"
        if verb == f"{name}_off":
            return f"{OK} {label}: Disabled"
    if verb == "surge_off":
        return f"{OK} SURGE flag cleared"
    if verb == "block_on":
        return f"{OK} Entries blocked"
    if verb == "block_off":
        return f"{OK} Entry block removed"
    if verb == "clear":
        return f"{OK} Block cleared, streak reset"
    if verb.startswith("sectors_"):
        return f"{OK} Sectors updated"
    if verb == "holds":
        return f"{OK} Hold removed: {applied.get('operator_holds_removed')}"
    return f"{OK} Done: {verb}"


def applied(
    verb: str,
    result: Mapping[str, Any],
    c: Mapping[str, Any],
    d: Mapping[str, Any],
    active: Sequence[str],
) -> str:
    """The reply to a write: what changed, then only what the write is about."""
    lines = [confirmation(verb, result), ""]
    if verb.startswith("sectors_"):
        lines.append(sectors(active))
    else:
        lines += switch_lines(c, d)
    return "\n".join(lines)
