"""Weekly, after `fetch_universe`: every published sector is one the rotation can name.

The operator's sector rotation (`edge_control.active_sectors`,
`universe.SectorRotation`) speaks in the eleven GICS names on the §5.2 sleeve
(`config/etfs.json`). Every sector in `data/universe.json` comes from the Wikipedia
cross-check (see `fetch_universe`'s docstring), a live external source. If it ever
renames one — "Information Technology" to "Technology", say — the names in that sector
stop matching the rotation, and the rotation fails *open* for them: they are never
filtered, silently.

That fail-open is correct for a trading cycle and useless as a signal, so this
is the loud half. It runs as its own workflow step **after** the snapshot is
committed, deliberately: refusing to publish over a vocabulary drift would let
the snapshot age toward §5.1's 14-day limit and switch entries off, which is a
far bigger failure than a rotation that is briefly too generous. The run goes
red; the data still lands.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent


def unknown_sectors(snapshot: Mapping[str, Any], etfs: Mapping[str, Any]) -> dict[str, list[str]]:
    """`{sector: [symbols...]}` for every snapshot sector outside the sleeve's eleven."""
    vocabulary = set((etfs.get("sector") or {}).values())
    unknown: dict[str, list[str]] = {}
    for key in ("holdings", "cross_check"):
        for row in snapshot.get(key) or []:
            sector = row.get("sector")
            if sector and sector not in vocabulary:
                unknown.setdefault(sector, []).append(row.get("symbol"))
    return {sector: sorted(set(symbols)) for sector, symbols in sorted(unknown.items())}


def sub_industry_drift(snapshot: Mapping[str, Any], codes: Mapping[str, Any]) -> dict[str, list[str]]:
    """`{"uncoded": [...], "vanished": [...], "missing": [...]}`.

    * uncoded  — a sub-industry the snapshot carries that no code names (S&P
      added or renamed one): its stocks can only be reached by sector code.
    * vanished — a coded sub-industry no member carries any more: that code
      now filters to nothing (it fails open, silently — hence this check).
    * missing  — the snapshot carries no sub-industries at all: the fetcher
      lost the column, and every sub-industry code is inert.
    """
    coded = {entry["name"] for entry in (codes.get("sub_industries") or {}).values()}
    members = {row.get("symbol") for row in snapshot.get("holdings") or []}
    present = {
        row.get("sub_industry")
        for row in snapshot.get("cross_check") or []
        if row.get("symbol") in members and row.get("sub_industry")
    }
    if not present:
        return {"uncoded": [], "vanished": [], "missing": ["no sub_industry in the snapshot"]}
    return {
        "uncoded": sorted(present - coded),
        "vanished": sorted(coded - present),
        "missing": [],
    }


def main() -> int:
    snapshot = json.loads((REPO_ROOT / "data" / "universe.json").read_text(encoding="utf-8"))
    etfs = json.loads((REPO_ROOT / "config" / "etfs.json").read_text(encoding="utf-8"))
    unknown = unknown_sectors(snapshot, etfs)
    codes = json.loads((REPO_ROOT / "config" / "rotation_codes.json").read_text(encoding="utf-8"))
    drift = sub_industry_drift(snapshot, codes)
    for kind, names in drift.items():
        for name in names:
            print(
                f"sub-industry drift ({kind}): {name!r} — reconcile config/rotation_codes.json "
                "(and ops/verbs.json + docs/ROTATION_CODES.md) with the snapshot.",
                file=sys.stderr,
            )
    if not unknown and not any(drift.values()):
        print("vocabulary: every snapshot sector and sub-industry has a rotation code")
        return 0
    for sector, symbols in unknown.items():
        print(
            f"sector vocabulary drift: {sector!r} is not in config/etfs.json's sleeve "
            f"({len(symbols)} names, e.g. {', '.join(symbols[:5])}). The sector "
            "rotation will not filter these names until the sleeve or the "
            "fetcher is reconciled.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
