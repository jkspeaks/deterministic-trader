"""Every 30 min: FRED releases → `data/macro.json` (§6.4, §7.2).

Two things measured here changed the shape of this fetcher, and both are
improvements on what §7.2 assumed.

**No API key.** §7.2 budgets a "free key" for FRED. `fredgraph.csv?id=<series>`
serves the same observations with no key at all, so there is no secret to
provision, rotate, or discover has expired at 09:40 on a Monday.

**Consensus is not free, and the design already provided for that.** §6.4's
first trigger form is a print "against consensus", and no free source publishes
economists' consensus — the ones that do are the $20/month tier §7.4 rejects at
this account size. §6.4's *second* trigger form is "a threshold move in a
tracked series", explicitly noted as arriving "in the same row shape as a
scheduled release". That is the form implemented here: the surprise is the
latest change measured against the series' own recent trend, so a CPI month
running hot relative to how it has been running produces a positive surprise
and CPI printing in line with trend produces none. **This is a substitution and
it is not the same number a consensus feed would give** — recorded here rather
than discovered later from a `MACRO` fill nobody can explain.

Output shape is `plays.py`'s `ctx["macro"]`:

    {"releases": [{"series": "CPI", "surprise_pct": 0.31, "sessions_ago": 0,
                   "value": 332.568, "observed": "2026-06-01",
                   "first_seen": "2026-07-15"}]}

`series` keys `config/macro_map.json`, which is what decides the sector tilt. A
series absent from that file produces no candidate at all — §6.4's intended way
to retire one — so the four below are the four that map.
"""

from __future__ import annotations

import csv
import io
import sys
from datetime import date
from statistics import fmean
from typing import Any

from ci import common

CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}&cosd={since}"

# The keys are `config/macro_map.json`'s release names; the values are FRED
# series ids. Adding a series here without adding it there produces a row no
# play reads, which is harmless; the reverse produces a mapping that never
# fires, which is not, so the two files are checked against each other in main.
SERIES = {
    "CPI": "CPIAUCSL",   # all-items CPI, seasonally adjusted, monthly
    "PCE": "PCEPI",      # PCE price index — the Fed's preferred gauge, monthly
    "NFP": "PAYEMS",     # total non-farm payrolls, monthly
    "FOMC": "DFEDTARU",  # fed funds target upper bound, daily; moves only at meetings
}

# How much history to pull. Enough to establish a trend baseline over a couple
# of years of monthly prints without dragging decades of CSV over the wire.
LOOKBACK_YEARS = 3

# Periods of the series' own change used as the stand-in for consensus. Twelve
# monthly prints is a year — long enough to be a trend, short enough to still
# be the current regime.
TREND_PERIODS = 12

# A baseline this small makes the relative surprise explode: a trend change of
# 0.001 against a print of 0.01 is a 900% "surprise" that means nothing. Below
# this the series is treated as trendless and no surprise is emitted.
MIN_BASELINE = 1e-4


def _observations(series_id: str) -> list[tuple[str, float]]:
    """`(date, value)` oldest-first, with FRED's `.` placeholders dropped."""
    since = date(common.now_et().year - LOOKBACK_YEARS, 1, 1).isoformat()
    blob = common.fetch(CSV_URL.format(series=series_id, since=since))

    rows: list[tuple[str, float]] = []
    for row in csv.DictReader(io.StringIO(blob.decode("utf-8-sig"))):
        values = list(row.values())
        if len(values) < 2 or values[0] is None:
            continue
        try:
            rows.append((str(values[0]).strip(), float(values[1])))
        except (TypeError, ValueError):
            continue  # FRED writes "." for a period with no observation
    return rows


def _surprise(values: list[float]) -> float | None:
    """How far the latest change ran from the series' own recent trend.

    Relative, not absolute, because `MACRO_SURPRISE_FULL_SCALE_PCT` is 0.25 —
    a scale on which an absolute CPI move of 0.4% would score 1.6% conviction
    and no release would ever rotate anything. The quantity that belongs on
    that scale is *how much bigger than usual* the move was.
    """
    if len(values) < TREND_PERIODS + 2:
        return None

    changes = [later - earlier for earlier, later in zip(values, values[1:])]
    latest = changes[-1]
    baseline = fmean(abs(change) for change in changes[-TREND_PERIODS - 1 : -1])

    if baseline < MIN_BASELINE:
        return None
    return (latest - baseline) / baseline if latest >= 0 else (latest + baseline) / baseline


def main() -> int:
    cal = common.calendar()
    today = common.current_trading_day()

    macro_map = common.read_json(common.CONFIG_DIR / "macro_map.json") or {}
    mapped = set((macro_map.get("releases") or {}).keys())
    unmapped = mapped - set(SERIES)
    if unmapped:
        # A tilt for a series nothing fetches is a play that can never fire, and
        # it looks identical to a play that simply had no signal.
        print(
            f"config/macro_map.json tilts {sorted(unmapped)}, which this "
            "fetcher does not publish; add them to SERIES or retire them",
            file=sys.stderr,
        )
        return 1

    previous = common.read_json(common.DATA_DIR / "macro.json")
    seen: dict[str, str] = {
        row["series"]: row["first_seen"]
        for row in ((previous or {}).get("releases") or [])
        if row.get("series") and row.get("first_seen")
    }
    observed_before: dict[str, str] = {
        row["series"]: row["observed"]
        for row in ((previous or {}).get("releases") or [])
        if row.get("series") and row.get("observed")
    }

    releases: list[dict[str, Any]] = []
    for name, series_id in sorted(SERIES.items()):
        try:
            rows = _observations(series_id)
        except Exception as error:  # noqa: BLE001 — §14: degrade, do not stop
            print(f"{name} ({series_id}) unavailable: {error}", file=sys.stderr)
            continue
        if not rows:
            continue

        observed, value = rows[-1]
        surprise = _surprise([value for _, value in rows])

        # **The release date, which FRED's CSV does not carry.** An observation
        # dated 2026-06-01 is published in mid-July, so aging the row from its
        # observation date would make every release two months old and MACRO
        # would never fire. The run that first sees a new observation is the
        # release, to within this job's cadence.
        #
        # On the very first run there is no previous file to compare against,
        # so `first_seen` falls back to the observation date — which is weeks
        # old for a monthly series and therefore lands outside
        # `MACRO_WINDOW_SESSIONS`. The pipeline starts inert rather than firing
        # four rotations on prints the market absorbed a month ago.
        if observed_before.get(name) == observed and name in seen:
            first_seen = seen[name]
        elif previous is None:
            first_seen = observed
        else:
            first_seen = today.isoformat()

        releases.append(
            {
                "series": name,
                "surprise_pct": None if surprise is None else round(surprise, 4),
                "sessions_ago": common.sessions_between(
                    date.fromisoformat(first_seen), today, cal
                ),
                "value": value,
                "observed": observed,
                "first_seen": first_seen,
            }
        )

    if not releases:
        print("no series fetched; leaving the previous file in place", file=sys.stderr)
        return 1

    document = {
        "as_of": today.isoformat(),
        "generated_at": common.now_et().isoformat(timespec="seconds"),
        "surprise_basis": "trailing-trend deviation, not consensus (see module docstring)",
        "releases": releases,
    }
    common.write_json(common.DATA_DIR / "macro.json", document)
    common.publish_data_file("macro", today, document)
    live = [row["series"] for row in releases if row["sessions_ago"] <= 2]
    print(
        f"macro.json: {len(releases)} series, "
        f"{'in window: ' + ', '.join(live) if live else 'none in window'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
