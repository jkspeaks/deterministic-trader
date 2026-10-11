"""Called by `ops/control.py` (the operator front door). No I/O here — the
caller reads `active_sectors` and writes the result through
`edge_control_apply` (R2: one write path, no new RPC).

The vocabulary is `ops/verbs.json`'s `lists[].codes` (sub-industry and basket
codes, stored canonically) plus `aliases`, which a test asserts equal to
`config/etfs.json`'s sleeve; the verb table carries its own copy so it needs no
filesystem read.

Refusals (operator ruling) — each would silently mean something
other than what was typed:
  * `set` with no names      -> would be a clear in disguise; use `clear`.
  * `add` onto `[]` ("all")  -> would NARROW eleven sectors to one; use `set`.
  * `remove` of the last one -> `[]` means ALL sectors, the opposite; use `clear`.
  * any unknown name         -> rejected with the nearest matches; a stored
                                 typo would make the rotation fail open.
"""

from __future__ import annotations

import difflib
from typing import Any, List, Mapping, Sequence

OPS = ("set", "add", "remove", "clear")

UNKNOWN = 1
SET_NEEDS_NAMES = 2
ADD_ONTO_ALL = 3
REMOVE_TO_EMPTY = 4
NOT_IN_SET = 5
NEEDS_NAMES = 6
ALREADY_ALL = 7


class SectorVerbError(ValueError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _norm(text: str) -> str:
    return " ".join(str(text).split()).lower()


def _codes(defs: Mapping[str, Any]) -> Mapping[str, Any]:
    return defs.get("codes") or {}


def _order(defs: Mapping[str, Any]) -> List[str]:
    """Every canonical code, in display order: sectors, baskets, sub-industries."""
    codes = _codes(defs)
    return [*(codes.get("sectors") or {}), *(codes.get("baskets") or {}),
            *(codes.get("sub_industries") or {})]


def _lookup(defs: Mapping[str, Any]) -> dict:
    """Every accepted spelling (lower-cased) -> its canonical code.

    A sector's code, its GICS name or its sleeve ticker (`EN`, "Energy",
    `XLE`); a sub-industry's code or name (`CHIP`, "Semiconductors"); a basket
    code (`SEMI`). The code is what gets stored.
    """
    codes = _codes(defs)
    table = {}
    sector_code = {}
    for code, name in (codes.get("sectors") or {}).items():
        table[_norm(code)] = code
        table[_norm(name)] = code
        sector_code[name] = code
    for ticker, name in (defs.get("aliases") or {}).items():
        if name in sector_code:
            table[_norm(ticker)] = sector_code[name]
    for code, name in (codes.get("sub_industries") or {}).items():
        table[_norm(code)] = code
        table[_norm(name)] = code
    for code in codes.get("baskets") or {}:
        table[_norm(code)] = code
    return table


def _ordered(names: Sequence[str], defs: Mapping[str, Any]) -> List[str]:
    """Canonical codes, de-duplicated, in the vocabulary's own order."""
    wanted = set(names)
    return [code for code in _order(defs) if code in wanted]


def label(code: str, defs: Mapping[str, Any]) -> str:
    """`CHIP` -> "Semiconductors"; a basket's own name; a sector's GICS name."""
    codes = _codes(defs)
    for group in ("sectors", "sub_industries"):
        if code in (codes.get(group) or {}):
            return str(codes[group][code])
    basket = (codes.get("baskets") or {}).get(code)
    return str(basket.get("name")) if isinstance(basket, Mapping) else code


def resolve(text: str | None, defs: Mapping[str, Any]) -> List[str]:
    """Comma-separated names/tickers -> canonical names. Raises on any unknown."""
    table = _lookup(defs)
    parts = [p for p in (_norm(x) for x in (text or "").split(",")) if p]
    unknown = [p for p in parts if p not in table]
    if unknown:
        codes = _codes(defs)
        hints = []
        for item in unknown:
            close = difflib.get_close_matches(item, sorted(table), n=2, cutoff=0.6)
            guess = []
            for key in close:
                code = table[key]
                text = f"{code} ({label(code, defs)})"
                if text not in guess:
                    guess.append(text)
            hints.append(f"'{item}'" + (f" (did you mean {' or '.join(guess)}?)" if guess else ""))
        raise SectorVerbError(
            UNKNOWN,
            "unknown sector(s): " + ", ".join(hints)
            + ". Sector codes: " + ", ".join(codes.get("sectors") or {})
            + "; baskets: " + ", ".join(codes.get("baskets") or {})
            + f"; plus {len(codes.get('sub_industries') or {})} sub-industry codes"
            + " (docs/ROTATION_CODES.md). Full names and sleeve tickers (XLE) work too.",
        )
    return _ordered([table[p] for p in parts], defs)


def current_set(value: Any, defs: Mapping[str, Any]) -> List[str]:
    """`edge_control.active_sectors` as stored, canonicalized; unknowns dropped."""
    table = _lookup(defs)
    items = [value] if isinstance(value, str) else (value if isinstance(value, list) else [])
    return _ordered([table[_norm(v)] for v in items if _norm(v) in table], defs)


def mutate(op: str, current: Any, text: str | None, defs: Mapping[str, Any]) -> List[str]:
    """The new `active_sectors` list, or a `SectorVerbError` saying why not."""
    have = current_set(current, defs)

    if op == "clear":
        return []

    names = resolve(text, defs)

    if op == "set":
        if not names:
            raise SectorVerbError(
                SET_NEEDS_NAMES,
                "'sectors set' needs at least one sector. To allow every sector: sectors clear",
            )
        return names

    if not names:
        raise SectorVerbError(NEEDS_NAMES, f"'sectors {op}' needs at least one sector")

    if op == "add":
        if not have:
            raise SectorVerbError(
                ADD_ONTO_ALL,
                "every sector is active now — adding one would narrow trading to just "
                f"{', '.join(names)}. If that is what you want: sectors set {', '.join(names)}",
            )
        return _ordered(have + names, defs)

    if op == "remove":
        if not have:
            raise SectorVerbError(ALREADY_ALL, "every sector is active; there is nothing to remove")
        missing = [n for n in names if n not in have]
        if missing:
            raise SectorVerbError(
                NOT_IN_SET, f"not in the active set: {', '.join(missing)} (active: {', '.join(have)})"
            )
        left = [n for n in have if n not in names]
        if not left:
            raise SectorVerbError(
                REMOVE_TO_EMPTY,
                "that would leave no sector, and an empty set means ALL sectors — "
                "to allow every sector: sectors clear",
            )
        return left

    raise SectorVerbError(0, f"unknown sectors op: {op!r} (use {'|'.join(OPS)})")


def describe(value: Any, defs: Mapping[str, Any]) -> str:
    """One status line: `all` for `[]`, else the canonical names."""
    have = current_set(value, defs)
    if have:
        return ", ".join(have)

    raw = [value] if isinstance(value, str) else (value if isinstance(value, list) else [])
    if raw:
        # The engine fails open on a list naming no sector (R4), so "all" is
        # the truth — but the operator should see why.
        return "all (stored value matches no sector: " + ", ".join(str(v) for v in raw) + ")"
    return "all"
