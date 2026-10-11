#!/usr/bin/env python3
"""Ops/control.py — single source of truth for the operator control surface (ops/control
plan). One front door calls this:

    - .github/workflows/control.yml, a workflow_dispatch for the verbs, run
      from the GitHub UI/CLI.

Deterministic, no LLM. Six keys are writable, split across two tables that
`edge_control_apply` and `edge_dip_recovery_config_apply`
own — this module decides *which* table a verb writes to; the DB functions
still decide, per key, whether a value lands in `value` or `payload`.

Every write verb here applies immediately and replies with the resulting state;
safety is the allowlist plus explicit on/off verbs (no bare toggle) plus the
per-verb cooldown below, not a two-step dialog. `surge on` is the one exception
: an all-in switch gets a literal `confirm` third token, still one
round trip, not a second message.

Only genuinely bespoke verbs (`block`, `clear`, `last`, `holds`, `status`) keep
hand-written logic here; their name/help/whitelist membership still comes from the
shared file.

Usage:
    uv run --group control python -m ops.control status
    uv run --group control python -m ops.control master on
    uv run --group control python -m ops.control master_on        # same verb,
                                                                   # underscore
                                                                   # form (GH
                                                                   # Actions'
                                                                   # workflow_dispatch
                                                                   # choice list)
    uv run --group control python -m ops.control block on "waiting on Fed minutes"
    uv run --group control python -m ops.control kill
    uv run --group control python -m ops.control surge on confirm
    uv run --group control python -m ops.control holds expire AMD
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from ops import sector_verbs, status_view
from ops.supabase_client import SupabaseClient, load_env

VERBS_JSON = Path(__file__).parent / "verbs.json"


def _load_verbs() -> Dict[str, Any]:
    return json.loads(VERBS_JSON.read_text(encoding="utf-8"))


VERB_DEFS = _load_verbs()
TOGGLES = {t["name"]: t for t in VERB_DEFS["toggles"]}
BLOCK = VERB_DEFS["block"]
SINGLES = {s["name"]: s for s in VERB_DEFS["single"]}
# List-valued control keys (today only `sectors` -> `active_sectors`).
LISTS = {entry["name"]: entry for entry in VERB_DEFS.get("lists") or []}
SECTORS = LISTS["sectors"]

# --- writable keys, and which table owns them -------------------------------
# The whitelist §5 (Security Posture) asks for: an unknown verb, or a verb
# that resolves to a key outside this set, is rejected before any RPC runs.

CONTROL_KEYS = {
    "trading_enabled",
    "entries_blocked",
    "entries_blocked_reason",
    "entries_blocked_until_cleared",
    # §6.4a / §17's SURGE all-in switch. Moved here from `config.json` so it
    # is flippable without a deploy — an account-wide risk switch, same class
    # as `trading_enabled`.
    "full_send_enabled",
    "divergence_streak",
} | {t["key"] for t in TOGGLES.values() if t["table"] == "control"} | {
    entry["key"] for entry in LISTS.values() if entry["table"] == "control"
}
DIP_CONFIG_KEYS = {t["key"] for t in TOGGLES.values() if t["table"] == "dip_config"}
WRITABLE_KEYS = CONTROL_KEYS | DIP_CONFIG_KEYS

VERBS = (
    "status", "help", "kill", "clear", "last", "step", "holds",
    *(f"{name}_on" for name in TOGGLES),
    *(f"{name}_off" for name in TOGGLES),
    "block_on", "block_off",
    "sectors", *(f"sectors_{op}" for op in sector_verbs.OPS),
)


def _help_text() -> str:
    # One phone-width line per entry, grouped. The per-verb help strings
    # below are the fallback for a verbs.json without it.
    if VERB_DEFS.get("help_text"):
        return "\n".join(VERB_DEFS["help_text"])
    lines = [f"{SINGLES['status']['help']}"]
    for name in ("master", "core", "dip"):
        lines.append(f"{TOGGLES[name]['help']}")
    lines.append(f"{BLOCK['help']}")
    lines.append(f"{TOGGLES['surge']['help']}")
    lines.append(f"{SECTORS['help']}")
    lines.append(f"{SINGLES['clear']['help']}")
    lines.append(f"{SINGLES['last']['help']}")
    lines.append(f"{SINGLES['step']['help']}")
    lines.append(f"{SINGLES['holds']['help']}")
    lines.append(f"{SINGLES['kill']['help']}")
    lines.append(f"{SINGLES['help']['help']}")
    return "\n".join(lines)


HELP_TEXT = _help_text()


# --- MarkdownV2 -------------------------------------------------------------

def md2_escape(s: Any) -> str:
    """Escape a value for MarkdownV2. Non-strings are stringified first."""
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!\\])", r"\\\1", str(s))


# --- cooldown / rate-limit ---------------------------------------------------
# Short on purpose (10s default): this isn't "don't repeat an alert", it's
# "don't double-apply the same write if an update is redelivered" — a real risk
# when a state commit fails after the write already landed.

DEFAULT_COOLDOWN_S = 10


def _state_path() -> Path:
    return Path(os.getenv("EDGE_CONTROL_STATE", ".control-state.json"))


def _state_load(path: Path) -> Dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        state = json.loads(raw)
        return state if isinstance(state, dict) else {"last_write": {}}
    except (OSError, ValueError):
        return {"last_write": {}}


def _state_save(path: Path, state: Dict[str, Any]) -> None:
    try:
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as e:
        sys.stderr.write(f"[control] could not persist {path}: {e}\n")


def _due(state: Dict[str, Any], key: str, cooldown_s: int) -> bool:
    last = state.setdefault("last_write", {}).get(key, 0)
    if time.time() - last < cooldown_s:
        return False
    state["last_write"][key] = time.time()
    return True


# --- verb parsing -------------------------------------------------------------
# Accepts both the two-word form ("master", "on") and the
# underscore-joined form GitHub Actions' workflow_dispatch choice list uses
# ("master_on"), so one parser serves both front doors.

def parse_verb(argv: List[str]) -> Tuple[str, Optional[str]]:
    tokens = [t for t in argv if t != ""]  # Actions passes "" for an unset `until`
    if not tokens:
        return "help", None

    first = tokens[0].lower()
    if first in ("status", "help", "kill", "clear", "last", "step"):
        rest = tokens[1:]
        return first, (rest[0] if rest else None)

    if first == "holds":
        rest = tokens[1:]
        return first, (" ".join(rest) if rest else None)

    if first == "sectors":
        # `sectors` alone reads the set; `sectors <op> <comma-separated names>`
        # writes it. Names keep their spaces ("Health Care") — the op splits on
        # commas, never on whitespace (ops/sector_verbs.py).
        if len(tokens) < 2:
            return "sectors", None
        op = tokens[1].lower()
        if op not in sector_verbs.OPS:
            raise ValueError(f"'sectors' needs {'|'.join(sector_verbs.OPS)} (or nothing, to show the set)")
        rest = tokens[2:]
        return f"sectors_{op}", (" ".join(rest) if rest else None)

    if first in TOGGLES:
        if len(tokens) < 2 or tokens[1].lower() not in ("on", "off"):
            raise ValueError(f"'{first}' needs on|off")
        canonical = f"{first}_{tokens[1].lower()}"
        rest = tokens[2:]
        return canonical, (" ".join(rest) if rest else None)

    if first == "block":
        if len(tokens) < 2 or tokens[1].lower() not in ("on", "off"):
            raise ValueError("'block' needs on|off")
        canonical = f"block_{tokens[1].lower()}"
        rest = tokens[2:]
        return canonical, (" ".join(rest) if rest else None)

    if first in VERBS:
        rest = tokens[1:]
        return first, (" ".join(rest) if rest else None)

    raise ValueError(f"unknown verb: {first!r}")


# --- reads --------------------------------------------------------------------

def _benchmark_lag_days(control: SupabaseClient, trading_day: Optional[str]) -> Optional[int]:
    """How many calendar days behind the latest `edge_benchmark` row is. `None` if
    either date is unreadable — the status summary treats that as "don't warn",
    not "warn"."""
    if not trading_day:
        return None
    latest = control.get_benchmark_latest() or {}
    latest_day = latest.get("trading_day")
    if not latest_day:
        return None
    try:
        return (date.fromisoformat(trading_day) - date.fromisoformat(latest_day)).days
    except ValueError:
        return None


def _summary_line(c: Dict[str, Any], d: Dict[str, Any], control: SupabaseClient) -> str:
    """The one-line status summary. Every field comes from
    `get_control()`/`get_dip_config()`'s existing keys plus
    `get_benchmark_latest()` — no new RPC beyond that one.

    Two `⚠` triggers, deliberately simplified (design call):
    - SURGE: warns whenever `full_send_enabled` is TRUE, not only when the
      tape is actually risk-on. A true `risk_on` read means fetching
      `edge_play_state`/`src.regime` from this CLI, disproportionate to a
      status line — "armed" here means "one flag away from firing", which is
      the more conservative (more often true) reading, not a wrong one.
    - benchmark lag: warns once the latest `edge_benchmark` row is more than
      one day behind the control row's own `last_full_cycle` trading day.
    """
    trading = (c.get("last_full_cycle") or "")[:10] or None
    lag = _benchmark_lag_days(control, trading)

    parts = ["Trading " + ("ON" if c.get("trading_enabled") else "OFF")]

    if c.get("entries_blocked"):
        streak = c.get("divergence_streak")
        detail = f"divergence streak {streak}" if streak else (c.get("entries_blocked_reason") or "manual")
        parts.append(f"entries BLOCKED ({detail})")
    else:
        parts.append("entries open")

    positions = c.get("positions_held")
    if positions is not None:
        parts.append(f"{positions} position{'s' if positions != 1 else ''}")

    if c.get("full_send_enabled") and not TOGGLES["surge"].get("retired"):
        parts.append("SURGE ON ⚠")

    if sector_verbs.current_set(c.get("active_sectors"), SECTORS):
        parts.append("sectors: " + sector_verbs.describe(c.get("active_sectors"), SECTORS))

    parts.append("Dip sleeve " + ("ON" if d.get("dip_recovery_enabled") else "OFF"))

    if trading:
        parts.append(f"last full {trading}")

    if lag is not None and lag > 1:
        parts.append(f"benchmark {lag} days behind ⚠")

    return " · ".join(parts)


def _read_status(control: SupabaseClient) -> str:
    c = control.get_control() or {}
    d = control.get_dip_config() or {}
    if not c.get("readable", False):
        return "```\ncontrol: UNREADABLE\n```"

    trading = (c.get("last_full_cycle") or "")[:10] or None
    text = status_view.status(
        c, d, _benchmark_lag_days(control, trading),
        sector_verbs.current_set(c.get(SECTORS["key"]), SECTORS),
    )
    return "```\n" + "\n".join(md2_escape(l) for l in text.split("\n")) + "\n```"


def _read_status_raw(control: SupabaseClient) -> str:
    """The older reply: every key by its stored name. Kept for a
    reader who wants the raw values; nothing calls it."""
    c = control.get_control() or {}
    d = control.get_dip_config() or {}
    lines = [_summary_line(c, d, control), ""]
    lines += [
        f"trading_enabled: {c.get('trading_enabled')}",
        f"core_strategies_enabled: {d.get('core_strategies_enabled')}",
        f"dip_recovery_enabled: {d.get('dip_recovery_enabled')}",
        f"full_send_enabled: {c.get('full_send_enabled')}"
        + (" (SURGE retired, no effect)" if TOGGLES["surge"].get("retired") else ""),
        f"active_sectors: {sector_verbs.describe(c.get('active_sectors'), SECTORS)}",
        f"entries_blocked: {c.get('entries_blocked')}",
    ]
    if c.get("entries_blocked"):
        lines.append(f"  reason: {c.get('entries_blocked_reason')}")
        lines.append(f"  until_cleared: {c.get('entries_blocked_until_cleared')}")
        lines.append(f"  divergence_streak: {c.get('divergence_streak')}")
    lines += [
        f"high_water_mark: {c.get('high_water_mark')}",
        f"  anchored on: equity {c.get('high_water_mark_equity')} "
        f"as of {c.get('high_water_mark_as_of')}",
        f"positions_held: {c.get('positions_held')}",
        f"unreconciled: {c.get('unreconciled')}",
        f"cycle_in_progress: {c.get('cycle_in_progress')}",
    ]
    return "```\n" + "\n".join(md2_escape(l) for l in lines) + "\n```"


def _read_sectors(c: Dict[str, Any]) -> str:
    """The read-only `sectors`: the active set and the vocabulary."""
    # Codes, not names — the sub-industry list is too long for a chat reply
    # and lives in docs/ROTATION_CODES.md.
    active = sector_verbs.current_set(c.get(SECTORS["key"]), SECTORS)
    lines = (status_view.sectors(active) + "\n\n" + status_view.sectors_help(SECTORS)).split("\n")
    return "```\n" + "\n".join(md2_escape(l) for l in lines) + "\n```"


def _read_last(control: SupabaseClient) -> str:
    """Two plain PostgREST table reads, not a new SQL function —
    `edge_session_stats` and `edge_alerts` both grant `select` to
    anon/authenticated the same way every other read here
    already relies on RLS rather than a `security definer` wrapper."""
    session = control.get_last_session()
    if not session:
        return "```\nno session recorded yet\n```"

    cycle_id = session.get("cycle_id")
    lines = [
        f"{session.get('cycle_type')} — {session.get('status')}",
    ]
    alert = control.get_last_alert_for_cycle(cycle_id) if cycle_id else None
    if alert:
        lines.append(alert.get("title", ""))
        body = alert.get("body") or ""
        footer = body.splitlines()[-1] if body else ""
        lines.append(footer)
    return "```\n" + "\n".join(md2_escape(l) for l in lines) + "\n```"


_ET = ZoneInfo("America/New_York")


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _dur(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


def format_step(session: Optional[Dict[str, Any]], now: datetime) -> List[str]:
    """Pure, so `tests/test_ops_step_verb.py` can pin it."""
    if not session:
        return ["no session recorded yet"]

    started = _parse_ts(session.get("started_at"))
    finished = _parse_ts(session.get("finished_at"))
    step_at = _parse_ts(session.get("current_step_at"))
    beat = _parse_ts(session.get("last_beat_at"))
    step = session.get("current_step")
    running = finished is None

    label = "running" if running else (session.get("status") or "finished")
    lines = [f"{session.get('cycle_type') or 'cycle'} — {label}"]

    if step and step_at:
        at = step_at.astimezone(_ET).strftime("%H:%M:%S")
        if running:
            lines.append(f"in {step} since {at} ET ({_dur((now - step_at).total_seconds())} ago)")
        else:
            lines.append(f"ended in {step} at {at} ET")
    else:
        lines.append("no step recorded yet")

    if started:
        span = ((finished or now) - started).total_seconds()
        lines.append(
            f"started {started.astimezone(_ET).strftime('%H:%M')} ET · "
            f"{'running' if running else 'ran'} {_dur(span)}"
        )
    if running and beat:
        lines.append(
            f"last row {beat.astimezone(_ET).strftime('%H:%M:%S')} ET "
            f"({_dur((now - beat).total_seconds())} ago)"
        )
    if session.get("rows_written"):
        lines.append(f"{session['rows_written']} rows posted")
    return lines


def _read_step(control: SupabaseClient) -> str:
    lines = format_step(control.get_current_session(), datetime.now(timezone.utc))
    return "```\n" + "\n".join(md2_escape(l) for l in lines) + "\n```"


def _read_holds(control: SupabaseClient, today: Optional[str]) -> str:
    c = control.get_control() or {}
    holds = c.get("operator_holds") or {}
    if not holds:
        return "```\nno operator_holds set\n```"

    lines = []
    for symbol, entry in sorted(holds.items()):
        reason, set_on = (entry or {}).get("reason"), (entry or {}).get("set_on")
        age = ""
        if today and set_on:
            try:
                age = f", {(date.fromisoformat(today) - date.fromisoformat(set_on)).days}d old"
            except ValueError:
                age = ""
        lines.append(f"{symbol}: {reason} (set {set_on}{age})")
    return "```\n" + "\n".join(md2_escape(l) for l in lines) + "\n```"


# --- writes ---------------------------------------------------------------

def _apply(control: SupabaseClient, verb: str, arg: Optional[str]) -> Dict[str, Any]:
    if verb in ("master_on", "master_off", "kill"):
        on = verb == "master_on"
        updates = {"trading_enabled": "TRUE" if on else "FALSE"}
        assert set(updates) <= CONTROL_KEYS
        control.apply_control(updates)
        return updates

    if verb in ("core_on", "core_off"):
        on = verb == "core_on"
        updates = {"core_strategies_enabled": "TRUE" if on else "FALSE"}
        assert set(updates) <= DIP_CONFIG_KEYS
        control.apply_dip_config(updates)
        return updates

    if verb in ("dip_on", "dip_off"):
        on = verb == "dip_on"
        updates = {"dip_recovery_enabled": "TRUE" if on else "FALSE"}
        assert set(updates) <= DIP_CONFIG_KEYS
        control.apply_dip_config(updates)
        return updates

    if verb in ("surge_on", "surge_off"):
        on = verb == "surge_on"
        # A retired play's switch can be cleared but not set: the core ignores
        # it either way, and a reply of "on" would say otherwise.
        if on and TOGGLES["surge"].get("retired"):
            raise ValueError(TOGGLES["surge"]["retired"])
        if on and (arg or "").strip().lower() != TOGGLES["surge"]["confirm_on_required"]:
            raise ValueError(
                "surge on requires confirmation: surge on confirm "
                "(an all-in switch)"
            )
        updates = {"full_send_enabled": "TRUE" if on else "FALSE"}
        assert set(updates) <= CONTROL_KEYS
        control.apply_control(updates)
        return updates

    if verb == "block_on":
        updates = {
            "entries_blocked": "TRUE",
            "entries_blocked_reason": arg or "manual block (ops/control)",
            "entries_blocked_until_cleared": "TRUE",
        }
        assert set(updates) <= CONTROL_KEYS
        control.apply_control(updates)
        return updates

    if verb == "block_off":
        updates = {
            "entries_blocked": "FALSE",
            "entries_blocked_reason": None,
            "entries_blocked_until_cleared": "FALSE",
        }
        assert set(updates) <= CONTROL_KEYS
        control.apply_control(updates)
        return updates

    if verb == "clear":
        updates = {
            "entries_blocked": "FALSE",
            "entries_blocked_reason": None,
            "entries_blocked_until_cleared": "FALSE",
            "divergence_streak": "0",
        }
        assert set(updates) <= CONTROL_KEYS
        control.apply_control(updates)
        return updates

    if verb.startswith("sectors_"):
        op = verb[len("sectors_"):]
        current = (control.get_control() or {}).get(SECTORS["key"])
        updates = {SECTORS["key"]: sector_verbs.mutate(op, current, arg, SECTORS)}
        assert set(updates) <= CONTROL_KEYS
        control.apply_control(updates)
        return updates

    if verb == "holds":
        tokens = (arg or "").split()
        if not tokens:
            raise ValueError("not a write verb: 'holds' (read-only; try 'holds expire <symbol>')")
        if len(tokens) != 2 or tokens[0].lower() != "expire":
            raise ValueError("usage: holds expire <symbol>")
        symbol = tokens[1].upper()
        c = control.get_control() or {}
        holds = dict(c.get("operator_holds") or {})
        if symbol not in holds:
            raise ValueError(f"no operator_hold set for {symbol}")
        del holds[symbol]
        updates = {"operator_holds": holds}
        control.apply_control(updates)
        return {"operator_holds_removed": symbol}

    raise ValueError(f"not a write verb: {verb!r}")


# --- entry point --------------------------------------------------------------

def run(argv: List[str]) -> int:
    load_env()
    control = SupabaseClient()

    try:
        verb, arg = parse_verb(argv)
    except ValueError as e:
        sys.stderr.write(f"{e}\n\n{HELP_TEXT}\n")
        return 2

    if verb == "help":
        print(HELP_TEXT)
        return 0

    if not control.is_configured:
        sys.stderr.write("EDGE_SUPABASE_URL / EDGE_SUPABASE_KEY not set.\n")
        return 2

    if verb == "status":
        print(_read_status(control))
        return 0

    if verb == "last":
        print(_read_last(control))
        return 0

    if verb == "step":
        print(_read_step(control))
        return 0

    if verb == "sectors":
        c = control.get_control() or {}
        print(_read_sectors(c))
        return 0

    if verb == "holds" and not (arg or "").split():
        c = control.get_control() or {}
        today = ((c.get("last_full_cycle") or "")[:10]) or None
        print(_read_holds(control, today))
        return 0

    # kill is an alias resolved inside _apply, but the cooldown key and the
    # cooldown-only path below both need the resolved name.
    canonical = "master_off" if verb == "kill" else verb

    cooldown_s = int(os.getenv("EDGE_CONTROL_COOLDOWN_S", DEFAULT_COOLDOWN_S))
    state_path = _state_path()
    state = _state_load(state_path)
    cooldown_key = f"{canonical}:{arg or ''}"
    if not _due(state, cooldown_key, cooldown_s):
        print(f"cooldown active for {canonical}, not re-applied (retry in a few seconds)")
        return 0

    try:
        applied = _apply(control, canonical, arg)
    except ValueError as e:
        sys.stderr.write(f"{e}\n")
        return 2

    _state_save(state_path, state)

    # One line saying what changed, then the switches (or the sectors,
    # for a sectors write). Not the whole status.
    c = control.get_control() or {}
    d = control.get_dip_config() or {}
    text = status_view.applied(
        canonical, applied, c, d, sector_verbs.current_set(c.get(SECTORS["key"]), SECTORS)
    )
    print("```\n" + "\n".join(md2_escape(l) for l in text.split("\n")) + "\n```")
    return 0


def main() -> None:
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
