"""config/etfs.json — the one part of the universe nobody fetches (§5.2).

The sleeve is checked in because it changes on the order of once a decade, and
a refresh cadence for something that static would be ceremony. The trade is
that nothing upstream will ever notice a typo in it, so these tests are the
only thing standing between a mistyped ticker and a rotation target that can
never fill.
"""

import json
from pathlib import Path

ETFS = json.loads(
    (Path(__file__).resolve().parents[1] / "config" / "etfs.json").read_text(
        encoding="utf-8"
    )
)

SECTOR = ETFS["sector"]
BROAD = ETFS["broad"]


def test_all_eleven_sector_spdrs_are_present():
    """§6.4 rotates across sectors; a missing one is a tilt that can never be
    expressed, and §5.3's '≤ 11' bound is read straight off this list."""
    assert sorted(SECTOR) == [
        "XLB", "XLC", "XLE", "XLF", "XLI",
        "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
    ]


def test_every_sector_bucket_is_distinct():
    """Two ETFs sharing a bucket would halve the headroom §4.4 grants each."""
    assert len(set(SECTOR.values())) == len(SECTOR)


def test_the_sector_names_are_the_gics_names_the_holdings_file_publishes():
    """The reason XLE and XOM land in one cap without a translation table."""
    assert SECTOR["XLE"] == "Energy"
    assert SECTOR["XLK"] == "Information Technology"
    assert SECTOR["XLRE"] == "Real Estate"
    assert SECTOR["XLC"] == "Communication Services"


def test_the_broad_etfs_share_a_single_bucket():
    """SPY, QQQ, IWM and DIA are close enough to the same bet that giving each
    its own cap headroom would be a fiction."""
    assert set(BROAD.values()) == {"Broad Market"}


def test_no_broad_bucket_collides_with_a_sector_bucket():
    assert set(BROAD.values()).isdisjoint(set(SECTOR.values()))


def test_no_symbol_is_in_both_groups():
    """A symbol in both would be a rotation target and not, depending on which
    dict the merge read second."""
    assert set(SECTOR).isdisjoint(set(BROAD))


def test_every_entry_is_a_plausible_ticker_and_bucket():
    for symbol, bucket in {**SECTOR, **BROAD}.items():
        assert symbol.isupper() and symbol.isalpha() and 1 <= len(symbol) <= 5
        assert isinstance(bucket, str) and bucket.strip()
