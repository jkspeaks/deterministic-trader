"""Every 30 min: EDGAR 8-K item codes → `data/events.json` (§7.2, §6.3).

§7.2's claim is that CATALYST is affordable because item codes can be read
straight out of an index without parsing a single document. That is true, and
the index is cheaper than the design assumed: EDGAR's `getcurrent` Atom feed
carries the item codes **in the entry summary**, so one request returns a page
of filings already annotated —

    <title>8-K - Marpai, Inc.

rather than the ~530 per-company submissions documents §7.2 implies. Tens of
kilobytes per run instead of ~160 MB.

**The feed is a delta, so this file is an accumulator.** `getcurrent` shows only
the most recent filings across all filers — a day or so of traffic, not a
history. Each run merges what the feed shows into what the file already holds,
dedupes on accession number, re-derives `sessions_ago` for every row from the
absolute filing day, and drops what has aged out. The 30-minute cadence is what
makes that lossless; a daily job would miss filings entirely.

Output shape is `plays.py`'s `ctx["events"]`:

    {"AAPL": [{"type": "8-K", "items": ["1.01", "9.01"], "sessions_ago": 1,
               "headline_count": 0, "filed_day": "2026-07-31",
               "accession": "0001213900-26-084129"}]}
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ElementTree
from datetime import date, datetime
from typing import Any

from ci import common
from src import cadence

ATOM = "{http://www.w3.org/2005/Atom}"

FEED = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K"
    "&owner=include&output=atom&count=100&start={start}"
)

# `getcurrent` pages 100 at a time. Six pages covers a normal day's 8-K traffic
# with room to spare; the loop also stops early once a page adds nothing new,
# so this is a ceiling on a bad day rather than a cost paid every run.
MAX_PAGES = 6

# **Wider than CATALYST's window, deliberately.** `plays._catalyst` ignores
# anything past `CATALYST_WINDOW_SESSIONS` (2), but `_restatement_veto` and
# §6.5's forced-exit check read the same rows with **no window at all** — a 4.02
# blocks entries and forces an exit however old it is. (`exits.py` now surfaces
# it as a note; it still rides along in this file, unused by any hard rule.)
# Retaining only two sessions would silently give the 4.02 rule a two-day memory
# that no line in `plays.py` asks for. Ten is a judgement call, recorded as one:
# long enough that a Friday restatement still vetoes the following Thursday,
# short enough that the file stays a few kilobytes.
RETENTION_SESSIONS = 10

CIK_IN_TITLE = re.compile(r"\((\d{4,10})\)")
ITEM_CODE = re.compile(r"Item\s+(\d+\.\d+)\s*:")
TAG = re.compile(r"<[^>]+>")


def _members() -> dict[str, str]:
    """CIK → symbol, for the names inside the fence and no others.

    Built from `data/universe.json`'s cross-check rows, which `fetch_universe`
    populates with the CIK from the same table that named the constituent. The
    feed carries every filer on EDGAR — thousands a day — and this map is what
    turns that into the ~500 the system may act on.
    """
    snapshot = common.read_json(common.DATA_DIR / "universe.json")
    if not snapshot:
        raise RuntimeError(
            "data/universe.json is absent or unreadable; the weekly universe "
            "job has to land before events can be attributed to a symbol"
        )

    mapping: dict[str, str] = {}
    for row in snapshot.get("cross_check") or []:
        cik, symbol = row.get("cik"), row.get("symbol")
        if cik and symbol:
            mapping[str(cik).lstrip("0")] = symbol
    return mapping


def _entry(element: ElementTree.Element) -> dict[str, Any] | None:
    """One Atom entry, or ``None`` if it is not a usable 8-K row."""
    title = (element.findtext(f"{ATOM}title") or "").strip()
    summary = (element.findtext(f"{ATOM}summary") or "").strip()
    updated = (element.findtext(f"{ATOM}updated") or "").strip()

    cik = CIK_IN_TITLE.search(title)
    if not cik or not updated:
        return None

    accession = re.search(r"AccNo:\s*</b>\s*([\d-]+)", summary) or re.search(
        r"AccNo:\s*([\d-]+)", TAG.sub(" ", summary)
    )

    try:
        filed_at = datetime.fromisoformat(updated).astimezone(cadence.ET)
    except ValueError:
        return None

    return {
        "cik": cik.group(1).lstrip("0"),
        # The filing's own session, via the same 20:00 ET shift the cycle uses.
        # An 8-K filed at 21:00 Monday belongs to Tuesday, and counting it from
        # the calendar date would make it a session older than it is on the one
        # morning the reaction is still tradeable.
        "filed_day": cadence.trading_day(filed_at).isoformat(),
        "accession": accession.group(1) if accession else None,
        "items": sorted(set(ITEM_CODE.findall(TAG.sub(" ", summary)))),
    }


def _harvest(members: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Walk the feed until it stops offering filings we have not seen."""
    found: dict[str, dict[str, Any]] = {}

    for page in range(MAX_PAGES):
        blob = common.fetch(FEED.format(start=page * 100), accept="application/atom+xml")
        try:
            root = ElementTree.fromstring(blob)
        except ElementTree.ParseError as error:
            print(f"page {page} did not parse as Atom: {error}", file=sys.stderr)
            break

        entries = root.findall(f"{ATOM}entry")
        if not entries:
            break

        before = len(found)
        for element in entries:
            row = _entry(element)
            if row is None or row["cik"] not in members:
                continue
            key = row["accession"] or f"{row['cik']}:{row['filed_day']}"
            found[key] = {**row, "symbol": members[row["cik"]]}

        if len(found) == before:
            # A whole page of filings, none of them ours and none new. The feed
            # is ordered newest-first, so further pages are older still.
            break

    return found


def main() -> int:
    cal = common.calendar()
    today = common.current_trading_day()

    try:
        members = _members()
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1

    previous = common.read_json(common.DATA_DIR / "events.json") or {}

    # Everything already on file, re-keyed so a filing seen on ten consecutive
    # runs stays one row. `filed_day` is what survives; `sessions_ago` is
    # derived below and never trusted from the previous file, because the whole
    # point of re-deriving it is that yesterday's count is wrong today.
    merged: dict[str, dict[str, Any]] = {}
    for symbol, rows in (previous.get("events") or {}).items():
        for row in rows:
            if not row.get("filed_day"):
                continue
            key = row.get("accession") or f"{symbol}:{row['filed_day']}"
            merged[key] = {**row, "symbol": symbol}

    try:
        merged.update(_harvest(members))
    except Exception as error:  # noqa: BLE001
        # §14: the pipeline being down means CATALYST and MACRO no-op, not that
        # the cycle stops. Re-publishing the aged file is the honest middle —
        # existing rows keep aging out on schedule and nothing new is claimed.
        print(f"EDGAR feed unavailable, re-aging the existing file: {error}", file=sys.stderr)

    events: dict[str, list[dict[str, Any]]] = {}
    for row in merged.values():
        filed = date.fromisoformat(row["filed_day"])
        ago = common.sessions_between(filed, today, cal)
        if ago > RETENTION_SESSIONS:
            continue

        events.setdefault(row["symbol"], []).append(
            {
                "type": "8-K",
                "items": list(row.get("items") or []),
                "sessions_ago": ago,
                # §6.3's second trigger form — three headlines clustered on one
                # name — has no source yet. There is no free per-ticker news
                # feed that does not need a key, and inventing a number here
                # would fire CATALYST on a count nothing measured. Zero is the
                # honest value: `plays._catalyst` requires >= 3, so the clause
                # is inert rather than wrong.
                "headline_count": 0,
                "filed_day": row["filed_day"],
                "accession": row.get("accession"),
            }
        )

    for rows in events.values():
        rows.sort(key=lambda row: (row["sessions_ago"], row["accession"] or ""))

    document = {
        "as_of": today.isoformat(),
        "generated_at": common.now_et().isoformat(timespec="seconds"),
        "retention_sessions": RETENTION_SESSIONS,
        "events": events,
    }
    common.write_json(common.DATA_DIR / "events.json", document)
    common.publish_data_file("events", today, document)
    print(
        f"events.json: {sum(len(rows) for rows in events.values())} filings "
        f"across {len(events)} symbols, as_of {today}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
