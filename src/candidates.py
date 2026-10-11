"""§5.3's coarse candidate discovery — cycle-prompt.md Step 5, run without
Robinhood.

Step 5 asks the live agent to assemble `triggers`/`macro_targets` from
`data/events.json` + `data/macro.json` + `config/macro_map.json` by hand, every
cycle, before `universe.resolve` ever runs. All three inputs are local files
already in the checkout — nothing here needs a broker call — so it belongs
beside `universe.py`, not inside the agent's own reasoning.

**DIP is not discoverable here.** §6.2's real condition needs a live RSI(14)
scan (Appendix C's `run_scan`), which only the agent can produce. This module's
`triggers` list covers PEAD and CATALYST only; the caller merges the DIP
scanner's survivors in separately, exactly as Step 5 already describes.

This is discovery, not judgment — the same relationship `plays.py` has to
`universe.resolve`. `_pead`/`_catalyst` re-check their own conditions against
live signals/quotes/fundamentals once a discovered symbol's per-symbol data
actually arrives; a symbol surfaced here that later fails that check simply
produces a `Skipped`, not a wrong trade. This module can be too generous. It
cannot afford to be too strict — a symbol dropped here never gets the chance
`plays.py` would have given it.
"""

from __future__ import annotations

from typing import Any, Mapping

from src import universe as universes

PEAD = "PEAD"
CATALYST = "CATALYST"

# SEC 8-K item 2.02, "Results of Operations and Financial Condition" — EDGAR's
# own item taxonomy for an earnings announcement. `plays.core.ITEM_NON_RELIANCE`
# (4.02) is already load-bearing elsewhere in this codebase; this is the same
# kind of fact but has not been verified live against `data/events.json` the
# way that one has (P3-style). If PEAD candidates stop surfacing that a manual
# read of the file says should have, check this constant first.
ITEM_EARNINGS = "2.02"
ITEM_NON_RELIANCE = "4.02"

# Matches `plays.pead.PEAD_WINDOW_SESSIONS` / `plays.catalyst.CATALYST_WINDOW_SESSIONS`
# / `plays.macro.MACRO_WINDOW_SESSIONS`. Not imported from there: this module
# is discovery for symbols `plays.py` has not seen yet, and importing the play
# modules here would make `src/candidates.py` depend on per-symbol signal/quote
# shapes it never reads. Kept in sync by comment, the same way `TERMINAL_ORDER_STATES`
# and the cap fractions warn readers to keep two copies aligned — here there is
# only one authoritative copy, this one, since the play modules' own windows
# gate the live trigger, not discovery.
PEAD_WINDOW_SESSIONS = (1, 3)
CATALYST_WINDOW_SESSIONS = 2
MACRO_WINDOW_SESSIONS = 2

# The switch and the ETF oscillator ceiling,
# restated here for the same reason the windows above are: this module is
# discovery and does not import the play modules. `_sector_targets` explains
# why the coarse screen is allowed to be looser than `plays.sector`'s and never
# tighter. Keep in sync with `plays.sector.CONFIG_KEY` / `SECTOR_RSI_MAX`.
SECTOR_CONFIG_KEY = "play_sector_enabled"
SECTOR_RSI_MAX = 30.0

CANDIDATE_CAP = 20


def _items(event: Mapping[str, Any]) -> set[str]:
    return {str(code).strip() for code in event.get("items") or []}


def _sessions_ago(row: Mapping[str, Any], key: str) -> int | None:
    """The published figure, trusted directly.

    `plays._sessions_ago` re-derives this from the calendar and an absolute
    date, because a frozen CI pipeline must not keep a stale event triggering
    forever. This module runs *before* that per-symbol data exists and has no
    calendar wired in; a discovery pass reading one extra day past a genuinely
    stale pipeline costs nothing since `plays.py` re-derives and re-checks the
    real trigger downstream regardless.
    """
    value = row.get(key)
    return None if value is None else int(value)


def _pead_triggers(events: Mapping[str, Any]) -> list[dict]:
    first, last = PEAD_WINDOW_SESSIONS
    out: list[dict] = []
    for symbol, rows in (events or {}).items():
        for event in rows or []:
            if event.get("type") != "8-K" or ITEM_EARNINGS not in _items(event):
                continue
            ago = _sessions_ago(event, "sessions_ago")
            if ago is None or not first <= ago <= last:
                continue
            out.append({"symbol": symbol, "play_id": PEAD, "sessions_ago": ago})
    return out


def _catalyst_triggers(events: Mapping[str, Any]) -> list[dict]:
    out: list[dict] = []
    for symbol, rows in (events or {}).items():
        best_ago: int | None = None
        for event in rows or []:
            ago = _sessions_ago(event, "sessions_ago")
            if ago is None or ago > CATALYST_WINDOW_SESSIONS:
                continue
            if ITEM_NON_RELIANCE in _items(event):
                # §6.3's veto: never propose a name whose own financials it
                # has disclaimed, no matter what else the filing says.
                best_ago = None
                break
            is_8k = event.get("type") == "8-K"
            has_cluster = int(event.get("headline_count") or 0) >= 3
            if is_8k or has_cluster:
                if best_ago is None or ago < best_ago:
                    best_ago = ago
        if best_ago is not None:
            out.append({"symbol": symbol, "play_id": CATALYST, "sessions_ago": best_ago})
    return out


def _macro_targets(
    macro: Mapping[str, Any], macro_map: Mapping[str, Any], sector_symbols: set[str]
) -> list[str]:
    releases = (macro or {}).get("releases") or []
    mapping = (macro_map or {}).get("releases") or {}
    targets: set[str] = set()

    for release in releases:
        ago = _sessions_ago(release, "sessions_ago")
        if ago is None or ago > MACRO_WINDOW_SESSIONS:
            continue
        series = release.get("series")
        tilt = mapping.get(series)
        if tilt is None:
            continue
        surprise = release.get("surprise_pct")
        if surprise is None:
            continue
        direction = "above" if float(surprise) > 0 else "below"
        for symbol in tilt.get(direction) or []:
            if symbol in sector_symbols:
                targets.add(symbol)

    return sorted(targets)


def _sector_targets(
    payload: Mapping[str, Any], loaded: Mapping[str, Any], sector_symbols: set[str]
) -> list[str]:
    """§4.3 C's ETFs, so the fetch list actually reaches them. Empty when off.

    `plays/sector.py` proposes off `regime_inputs`, which costs nothing — but
    `sizing` prices a candidate off `quotes` and stops it off `signals[atr]`,
    and an ETF nothing has put in the pool has neither. Discovery is where a
    symbol earns a per-symbol fetch, so this is where the play's universe joins
    it, alongside `macro_targets` and under the same `_drop_blocked_sectors`
    filter (which is no constraint on this play: an ETF at RSI <= 30 and above
    its own 200-day is neither a PEAK nor a BREAKDOWN by construction).

    The trigger is restated here rather than imported from `plays.sector`
    because the two are asking different questions and must be allowed to
    disagree in one direction only: this is the *coarse* screen that decides
    what to pay for, the play is the fine one that decides what to buy. Too
    generous is a wasted quote; too strict is a play that can never fire. So
    only the ETF's own two legs are checked here and SPY's is left to the play.
    """
    if not bool((loaded.get("config") or {}).get(SECTOR_CONFIG_KEY)):
        return []

    blocks = ((payload.get("regime_inputs") or {}).get("sectors")) or {}
    out: list[str] = []
    for symbol in sorted(sector_symbols):
        block = blocks.get(symbol) or {}
        rsi, last, sma200 = block.get("rsi14"), block.get("last"), block.get("sma200")
        if rsi is None or last is None or sma200 is None:
            continue
        try:
            if float(rsi) <= SECTOR_RSI_MAX and float(last) > float(sma200):
                out.append(symbol)
        except (TypeError, ValueError):
            continue
    return out


def dropped(symbol: str, reason: str) -> dict:
    """One `candidates_dropped` row.

    It grew a second producer that day — the regime's sector filter in
    `cli.candidates` — and a list in which "over the 20-name cap" and "its
    sector is at a peak" are the same bare string is a runlog that cannot
    answer why a play stopped firing, which is the exact question this list
    exists for.
    """
    return {"symbol": symbol, "reason": reason}


def assemble(payload: Mapping[str, Any], loaded: Mapping[str, Any]) -> dict:
    """`{"triggers": [...], "macro_targets": [...], "candidates_dropped": [...]}`.

    `triggers` still needs the DIP scanner's survivors merged in by the
    caller — see the module docstring. Ranked by recency (most recent filing
    first, ties broken by symbol) and capped at `CANDIDATE_CAP`; cycle-prompt.md's
    "trigger strength" tiebreak needs a live price/volume reaction this module
    never sees, so recency is the whole ranking here.
    """
    events = payload.get("events") or {}
    macro = payload.get("macro") or {}
    macro_map = loaded.get("macro_map") or {}
    sector_symbols = set((loaded.get("etfs") or {}).get("sector") or {})

    # A retired or shelved play discovers nothing (`plays.core.play_off`): a
    # trigger is what the runner pays a per-symbol fetch for, and what the
    # pre-market job pre-fetches. Dropped before the cap, so a play that is
    # off does not use up the 20 slots. Imported here rather than at the top
    # for the reason given above for the windows.
    from src.plays.core import MACRO, play_off

    found = [
        row for row in _pead_triggers(events) + _catalyst_triggers(events)
        if play_off(loaded, row["play_id"]) is None
    ]
    found.sort(key=lambda row: (row["sessions_ago"], row["symbol"]))

    macro_wanted = (
        _macro_targets(macro, macro_map, sector_symbols)
        if play_off(loaded, MACRO) is None else []
    )
    macro_targets = sorted(
        {
            *macro_wanted,
            *_sector_targets(payload, loaded, sector_symbols),
        }
    )

    # The operator's sector rotation, applied **before** the cap so the 20
    # slots are spent on sectors the operator wants, and so a name the
    # rotation removed is never mislabelled "outside the cap". Sector ETFs map
    # through the sleeve (XLF is Financials); Broad Market never filters.
    rotation = universes.sector_rotation(
        (payload.get("control") or {}).get("active_sectors"),
        loaded.get("etfs") or {},
        loaded.get("rotation_codes"),
        universes.sub_industry_map(payload.get("snapshot") or {}),
    )
    rotated: list[dict] = []
    if rotation.filtering:
        sectors = universes.sector_map(
            {"snapshot": payload.get("snapshot") or {}, "etfs": loaded.get("etfs") or {}}
        )
        found, fell = universes.rotation_filter(found, sectors, rotation)
        macro_targets, fell_etfs = universes.rotation_filter(macro_targets, sectors, rotation)
        rotated = [dropped(symbol, reason) for symbol, reason in fell + fell_etfs]

    kept = found[:CANDIDATE_CAP]

    return {
        "triggers": [{"symbol": row["symbol"], "play_id": row["play_id"]} for row in kept],
        # §4.3 C's ETFs ride in `macro_targets` rather than in a key of their
        # own: the list means "sector ETFs this cycle needs prices for", which
        # `universes.resolve` and the runner's fetch list both already consume,
        # and a second key would have to be threaded through both to say the
        # same thing. Sorted and de-duplicated so two plays wanting XLE is one
        # fetch. Empty addition while `play_sector_enabled` is off, which is
        # what keeps every discovery golden byte-identical.
        "macro_targets": macro_targets,
        "candidates_dropped": rotated + [
            dropped(
                row["symbol"],
                f"outside the {CANDIDATE_CAP}-name discovery cap, ranked by "
                "event recency",
            )
            for row in found[CANDIDATE_CAP:]
        ],
    }
