"""The two-source constituent merge and the ETF sleeve (§5.1, §5.2).

§5.1 buys two independent sources and then declines to let them argue: the
issuer's own holdings file defines membership, and the cross-check can only
produce a logged discrepancy. That is what "logged, not silently resolved"
buys — the disagreement survives into the record without either source being
able to widen or narrow the fence Invariant 3 rests on.
"""

from src import universe
from tests.factories import notes, universe_context


def test_the_issuer_file_defines_membership():
    result = universe.resolve(universe_context())

    assert "F" in result.members


def test_a_name_only_the_issuer_file_lists_is_still_a_member():
    """The cross-check cannot veto. A lagging wiki is the expected case."""
    ctx = universe_context(
        snapshot={"cross_check": [{"symbol": "AAPL", "sector": "Information Technology"}]}
    )

    result = universe.resolve(ctx)

    assert "F" in result.members


def test_a_name_only_the_cross_check_lists_is_not_a_member():
    """And cannot add. Otherwise the fence is editable by anyone with a wiki
    account, which is the whole reason membership has an issuer."""
    ctx = universe_context(
        snapshot={
            "cross_check": [
                {"symbol": "AAPL", "sector": "Information Technology"},
                {"symbol": "F", "sector": "Consumer Discretionary"},
                {"symbol": "PFE", "sector": "Health Care"},
                {"symbol": "XOM", "sector": "Energy"},
                {"symbol": "ENRN", "sector": "Energy"},
            ]
        }
    )

    result = universe.resolve(ctx)

    assert "ENRN" not in result.members


def test_a_name_missing_from_the_cross_check_is_logged():
    ctx = universe_context(
        snapshot={"cross_check": [{"symbol": "AAPL", "sector": "Information Technology"}]}
    )

    result = universe.resolve(ctx)

    assert sorted(notes(result, universe.CROSS_CHECK_MISSING)) == ["F", "PFE", "XOM"]


def test_a_name_only_the_cross_check_lists_is_logged():
    ctx = universe_context(
        snapshot={
            "cross_check": [
                {"symbol": "AAPL", "sector": "Information Technology"},
                {"symbol": "F", "sector": "Consumer Discretionary"},
                {"symbol": "PFE", "sector": "Health Care"},
                {"symbol": "XOM", "sector": "Energy"},
                {"symbol": "ENRN", "sector": "Energy"},
            ]
        }
    )

    result = universe.resolve(ctx)

    assert notes(result, universe.NOT_IN_ISSUER_FILE) == ["ENRN"]


def test_two_sources_that_agree_produce_no_notes():
    """The baseline has to be quiet, or a real discrepancy is lost in noise."""
    assert universe.resolve(universe_context()).notes == []


# --- sector, which the merge gets for free --------------------------------


def test_the_issuer_file_wins_a_sector_disagreement():
    ctx = universe_context(
        snapshot={"cross_check": [{"symbol": "F", "sector": "Industrials"}]}
    )

    result = universe.resolve(ctx)

    assert result.sectors["F"] == "Consumer Discretionary"


def test_a_sector_disagreement_is_logged():
    ctx = universe_context(
        snapshot={"cross_check": [{"symbol": "F", "sector": "Industrials"}]}
    )

    result = universe.resolve(ctx)

    assert "F" in notes(result, universe.SECTOR_DISAGREEMENT)


def test_a_constituent_with_no_sector_anywhere_gets_none():
    """Guard fails a sectorless name closed and sizing refuses it, so the gap
    is reported rather than papered over with a placeholder sector."""
    ctx = universe_context(
        snapshot={
            "holdings": [{"symbol": "F"}],
            "cross_check": [{"symbol": "F"}],
        }
    )

    result = universe.resolve(ctx)

    assert "F" in result.members
    assert result.sectors.get("F") is None


def test_the_cross_check_supplies_a_sector_the_issuer_file_omits():
    ctx = universe_context(
        snapshot={"holdings": [{"symbol": "F"}]},
    )

    result = universe.resolve(ctx)

    assert result.sectors["F"] == "Consumer Discretionary"


# --- the ETF sleeve --------------------------------------------------------


def test_the_sleeve_joins_the_membership():
    result = universe.resolve(universe_context())

    assert "XLE" in result.members
    assert "SPY" in result.members


def test_a_sector_etf_shares_its_cap_bucket_with_its_constituents():
    """XLE and XOM are one Energy bet, and §4.4's cap should see them as one."""
    result = universe.resolve(universe_context())

    assert result.sectors["XLE"] == result.sectors["XOM"] == "Energy"


def test_the_broad_etfs_share_one_bucket():
    result = universe.resolve(universe_context())

    buckets = {result.sectors[s] for s in ("SPY", "QQQ", "IWM", "DIA")}
    assert buckets == {"Broad Market"}


def test_the_rotation_set_is_the_sector_sleeve_and_stops_at_eleven():
    """§5.3 bounds macro targets at 11, and that bound is this list."""
    result = universe.resolve(universe_context())

    assert len(result.rotation) == 11
    assert "XLE" in result.rotation
    assert "SPY" not in result.rotation


def test_the_sleeve_is_never_reported_as_a_constituent_discrepancy():
    """ETFs are in neither the holdings file nor the wiki list, so a naive
    merge would report all fifteen as missing from both."""
    result = universe.resolve(universe_context())

    assert "XLE" not in notes(result)
    assert "SPY" not in notes(result)


def test_membership_is_sorted():
    """Golden files and Robinhood call order both depend on it."""
    result = universe.resolve(universe_context())

    assert result.members == sorted(result.members)
