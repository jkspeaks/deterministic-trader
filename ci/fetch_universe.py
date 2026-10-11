"""Weekly: index constituents → `data/universe.json` (§5.1).

Two independent sources, and the asymmetry between them is the whole design.
The SSGA holdings file **is** membership — it is the issuer's own statement of
what SPY holds, and `universe._merge_constituents` lets nothing else add a name,
because anything that can widen the universe is a way around Invariant 3's
fence. Wikipedia is a cross-check that may only annotate.

**Measured, and it changes what the cross-check is for.** SSGA publishes a
`Sector` column and every row in it is `-`. So the issuer file carries no sector
at all, and every sector in the system comes from Wikipedia via
`_merge_constituents`'s documented fallback. That makes the cross-check
load-bearing for something the design treated as a nice-to-have, and it is why
this fetcher refuses to publish when *either* source fails rather than only when
both do — see `main`.

Output shape is `universe.resolve`'s `ctx["snapshot"]`:

    {"as_of": "2026-07-30",
     "holdings":   [{"symbol": "AAPL", "sector": null, "weight": 7.65}, ...],
     "cross_check":[{"symbol": "AAPL", "sector": "Information Technology",
                     "cik": "0000320193"}, ...]}

`cik` is not read by any module. It rides along because `fetch_events.py` needs
a CIK→ticker map for exactly these names, and taking it from the file that
already defines them costs nothing and cannot drift from the membership it is
keyed to.
"""

from __future__ import annotations

import io
import re
import sys
from datetime import date, datetime
from typing import Any

from ci import common

# The `/us/en/intermediary/` path 301s to a shorter one. Following redirects is
# fine; hardcoding the target is not, since SSGA has moved it before.
SPY_HOLDINGS = (
    "https://www.ssga.com/us/en/intermediary/library-content/products/"
    "fund-data/etfs/us/holdings-daily-us-en-spy.xlsx"
)

# The wikitext of section 1, not the rendered HTML: a table markup change breaks
# loudly here, where the parse is twenty lines, instead of subtly inside an HTML
# scraper that finds three of five columns and reports success.
WIKI_CONSTITUENTS = (
    "https://en.wikipedia.org/w/api.php?action=parse"
    "&page=List_of_S%26P_500_companies&prop=wikitext&section=1&format=json"
)

# `{{NyseSymbol|MMM}}`, `{{NasdaqSymbol|AAPL}}`, and the plain `MMM` some rows
# still use, matched against a cell's content with its `|`/`||` markup already
# stripped. Anchored on both ends so a symbol mentioned inside a company name
# cannot match.
WIKI_SYMBOL = re.compile(r"^(?:\{\{\w*Symbol\|([^}|]+)\}\}|([A-Z][A-Z.\-]{0,6}))$")

# SSGA pads the sheet with a disclaimer block after the holdings. A row whose
# ticker is not a plausible symbol ends the table.
SYMBOL = re.compile(r"^[A-Z][A-Z.\-]{0,6}$")

# The floor below which a "successful" parse is not one. SPY holds ~500 names;
# anything under this means the layout moved and we are about to publish a
# universe that would silently shrink the fence.
MIN_CONSTITUENTS = 400


def _ssga_holdings(blob: bytes) -> tuple[str, list[dict[str, Any]]]:
    """The issuer file: an `As of` date and one row per constituent."""
    import openpyxl  # CI-only dependency; see pyproject's `ci` group.

    workbook = openpyxl.load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    sheet = workbook[workbook.sheetnames[0]]

    as_of: str | None = None
    header: dict[str, int] | None = None
    holdings: list[dict[str, Any]] = []

    for row in sheet.iter_rows(values_only=True):
        cells = ["" if cell is None else str(cell).strip() for cell in row]
        if not any(cells):
            continue

        if header is None:
            # "Holdings:" / "As of 30-Jul-2026" — the issuer's own as-of, which
            # is what §5.1's 14-day staleness limit should age against. Stamping
            # the fetch time instead would make a file SSGA stopped updating
            # look permanently fresh.
            if cells[0].startswith("Holdings") and len(cells) > 1:
                match = re.search(r"(\d{1,2}-[A-Za-z]{3}-\d{4})", cells[1])
                if match:
                    as_of = datetime.strptime(match.group(1), "%d-%b-%Y").date().isoformat()
            if "Ticker" in cells and "Name" in cells:
                header = {name: index for index, name in enumerate(cells) if name}
            continue

        def cell(name: str) -> str:
            index = header.get(name)
            return cells[index] if index is not None and index < len(cells) else ""

        symbol = cell("Ticker").upper()
        if not SYMBOL.match(symbol):
            continue

        sector = cell("Sector")
        try:
            weight = float(cell("Weight"))
        except ValueError:
            weight = None

        holdings.append(
            {
                # `-` is what SSGA actually publishes in this column for every
                # row. Recording it as a sector would put the string "-" into
                # `sizing.py`'s concentration buckets; `None` takes the
                # cross-check's answer through the documented fallback instead.
                "symbol": symbol,
                "sector": sector if sector and sector != "-" else None,
                "weight": weight,
            }
        )

    if as_of is None:
        raise RuntimeError("SSGA holdings file carried no 'As of' date")
    return as_of, holdings


def _wikipedia_cross_check(blob: bytes) -> list[dict[str, Any]]:
    """The annotating source: symbol, GICS sector, GICS sub-industry, and CIK.

    The sector strings are GICS names as published, which is what
    `config/etfs.json` already buckets the sector SPDRs by — so XLE and XOM land
    in one Energy cap without a translation table anywhere.
    """
    import json

    wikitext = json.loads(blob)["parse"]["wikitext"]["*"]

    rows: list[dict[str, Any]] = []
    # Cells accumulate between `|-` row separators. Wikipedia has published
    # this table both as one line per row (`|MMM || [[3M]] || ...`) and, as of
    # 2026-08, one line per cell (`|| {{NyseSymbol|MMM}}` / `|| [[3M]]` / ...).
    # Splitting every `|`-led line on `||` handles both: a single-cell line
    # splits to one element, a multi-cell line splits to several.
    cells: list[str] = []

    def flush() -> None:
        if not cells:
            return
        match = WIKI_SYMBOL.match(cells[0])
        if not match:
            return
        symbol = (match.group(1) or match.group(2)).strip().upper()
        # Column order after Symbol: Security, GICS Sector, GICS Sub-Industry,
        # Headquarters Location, Date added, CIK, Founded.
        fields = cells[1:]
        if len(fields) < 6:
            return
        cik = re.sub(r"\D", "", fields[5])
        rows.append(
            {
                "symbol": symbol,
                "sector": fields[1] or None,
                # The GICS sub-industry ("Semiconductors", "Data Center
                # REITs"), plain text in the table. What the operator's finer
                # rotation codes (config/rotation_codes.json) select on.
                "sub_industry": fields[2] or None,
                # Zero-padded to ten digits, which is the form EDGAR's own
                # identifiers take. Stored as text: a leading zero that becomes
                # an integer somewhere downstream stops matching.
                "cik": cik.zfill(10) if cik else None,
            }
        )

    for line in wikitext.splitlines():
        line = line.rstrip()
        if line.startswith("|-") or line.startswith("|}"):
            flush()
            cells = []
        elif line.startswith("!"):
            continue  # header row, not data
        elif line.startswith("|"):
            # Editors occasionally trail a cell with an HTML comment (e.g. the
            # Berkshire Hathaway row warning against "correcting" BRK.B to
            # BRK-B); strip it so it doesn't break the exact-match below.
            stripped = re.sub(r"<!--.*?-->", "", line.lstrip("|"))
            cells.extend(field.strip() for field in stripped.split("||"))
    flush()

    return rows


def main() -> int:
    """Publish, or leave the previous snapshot alone and say why.

    **Neither source is optional, and that is a deliberate tightening of §5.1.**
    §5.1 says "if both fail, the previous snapshot is reused". Publishing on one
    is worse than it sounds in both directions: without SSGA the file has no
    membership and the fence admits nothing, and without Wikipedia it has no
    sectors and `sizing.py` refuses every name it is handed. Either way the
    result is a cycle that trades nothing while reporting a fresh universe. A
    seven-day-old complete snapshot is strictly better, and §5.1's own 14-day
    staleness limit is the backstop for the case where the outage persists.
    """
    cross_check: list[dict[str, Any]] = []
    try:
        cross_check = _wikipedia_cross_check(common.fetch(WIKI_CONSTITUENTS))
    except Exception as error:  # noqa: BLE001 — every failure has one response
        print(f"cross-check unavailable: {error}", file=sys.stderr)

    as_of: str | None = None
    holdings: list[dict[str, Any]] = []
    try:
        as_of, holdings = _ssga_holdings(common.fetch(SPY_HOLDINGS))
    except Exception as error:  # noqa: BLE001
        print(f"issuer holdings unavailable: {error}", file=sys.stderr)

    if len(holdings) < MIN_CONSTITUENTS or len(cross_check) < MIN_CONSTITUENTS:
        print(
            f"refusing to publish: {len(holdings)} holdings and "
            f"{len(cross_check)} cross-check rows, both need "
            f"{MIN_CONSTITUENTS}+. The previous snapshot stands and ages "
            "toward §5.1's 14-day limit.",
            file=sys.stderr,
        )
        return 1

    document = {
        "as_of": as_of,
        "generated_at": common.now_et().isoformat(timespec="seconds"),
        "holdings": sorted(holdings, key=lambda row: row["symbol"]),
        "cross_check": sorted(cross_check, key=lambda row: row["symbol"]),
    }
    common.write_json(common.DATA_DIR / "universe.json", document)
    common.publish_data_file("snapshot", date.fromisoformat(as_of), document)
    print(
        f"universe.json: {len(holdings)} holdings, {len(cross_check)} "
        f"cross-check rows, as_of {as_of}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
