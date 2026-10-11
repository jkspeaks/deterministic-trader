"""Snapshot age, and what §14 says to do about it.

    | Universe snapshot > 14 days | Proceed on positions only; no new entries |

`guard.py` already denies every buy on a stale snapshot (`UNIVERSE_STALE`), so
narrowing the candidate set here is not a second denial — it is the other half
of the same sentence. §5.3's economics are the reason: a non-position candidate
exists only to become an entry, and entries are exactly what staleness forbids,
so carrying them costs a Robinhood call per symbol per cycle to reach a
conclusion guard has already reached.
"""

from src import universe
from tests.factories import notes, universe_context


def held(*symbols: str) -> list[dict]:
    return [{"symbol": s, "quantity": 1.0} for s in symbols]


# --- the arithmetic -------------------------------------------------------


def test_the_age_is_days_from_the_snapshot_to_the_trading_day():
    result = universe.resolve(universe_context())

    assert result.snapshot_age_days == 1


def test_a_snapshot_taken_this_morning_is_zero_days_old():
    ctx = universe_context(snapshot={"as_of": "2026-08-03"})

    assert universe.resolve(ctx).snapshot_age_days == 0


def test_a_weekly_fetch_that_ran_is_never_close_to_stale():
    """§5.1 fetches weekly against a 14-day limit — one missed run is
    survivable by design, two is not."""
    ctx = universe_context(snapshot={"as_of": "2026-07-27"})

    result = universe.resolve(ctx)

    assert result.snapshot_age_days == 7
    assert not result.stale


# --- the boundary, which has to be guard's ---------------------------------


def test_fourteen_days_is_not_yet_stale():
    ctx = universe_context(snapshot={"as_of": "2026-07-20"})

    result = universe.resolve(ctx)

    assert result.snapshot_age_days == 14
    assert not result.stale


def test_fifteen_days_is_stale():
    ctx = universe_context(snapshot={"as_of": "2026-07-19"})

    result = universe.resolve(ctx)

    assert result.snapshot_age_days == 15
    assert result.stale


def test_the_limit_is_read_from_config_not_hardcoded():
    ctx = universe_context(config={"universe_max_age_days": 3})

    assert universe.resolve(ctx).stale is False
    assert universe.resolve(
        universe_context(
            snapshot={"as_of": "2026-07-29"}, config={"universe_max_age_days": 3}
        )
    ).stale


# --- what staleness does to the candidate set ------------------------------


def test_a_stale_snapshot_narrows_the_candidate_set_to_positions():
    ctx = universe_context(
        snapshot={"as_of": "2026-07-01"},
        positions=held("PFE"),
        triggers=[{"symbol": "F", "play_id": "DIP"}],
        macro_targets=["XLE"],
    )

    result = universe.resolve(ctx)

    assert result.candidates == ["PFE"]


def test_a_stale_snapshot_still_admits_every_position():
    """The point of proceeding at all: exits must stay placeable."""
    ctx = universe_context(
        snapshot={"as_of": "2026-07-01"}, positions=held("PFE", "GME"), triggers=[]
    )

    assert universe.resolve(ctx).candidates == ["GME", "PFE"]


def test_a_stale_cycle_does_not_report_its_triggers_as_out_of_universe():
    """They were members. The snapshot's age is the reason they were dropped,
    and mislabelling that would send someone hunting a fence bug."""
    ctx = universe_context(
        snapshot={"as_of": "2026-07-01"}, triggers=[{"symbol": "F", "play_id": "DIP"}]
    )

    result = universe.resolve(ctx)

    assert notes(result, universe.TRIGGER_OUTSIDE_UNIVERSE) == []


def test_a_stale_cycle_says_so_in_its_reason():
    ctx = universe_context(snapshot={"as_of": "2026-07-01"})

    assert "33 days" in universe.resolve(ctx).reason


def test_a_fresh_cycle_reasons_about_the_set_it_built():
    result = universe.resolve(universe_context(positions=held("PFE")))

    assert "2 candidates" in result.reason


def test_the_reason_counts_one_of_anything_in_the_singular():
    """It goes into the runlog for a person to read, and "1 candidates from
    23 members, snapshot 1 days old" is three mistakes in one line."""
    result = universe.resolve(universe_context(snapshot={"as_of": "2026-08-02"}))

    assert "1 candidate from" in result.reason
    assert "snapshot 1 day old" in result.reason


def test_a_stale_cycle_holding_one_name_reads_in_the_singular():
    ctx = universe_context(snapshot={"as_of": "2026-07-01"}, positions=held("PFE"))

    assert "1 held position only" in universe.resolve(ctx).reason


def test_membership_survives_staleness():
    """The universe is old, not unknown. Narrowing is about what to spend
    calls on, not about forgetting who was in the index."""
    ctx = universe_context(snapshot={"as_of": "2026-07-01"})

    assert "F" in universe.resolve(ctx).members


# --- no snapshot at all ----------------------------------------------------


def test_a_snapshot_with_no_date_fails_closed():
    """Guard reads a missing age as stale, and the two must not disagree about
    whether an unknown universe is safe."""
    ctx = universe_context(snapshot={"as_of": None})

    result = universe.resolve(ctx)

    assert result.snapshot_age_days is None
    assert result.stale


def test_a_dateless_snapshot_narrows_to_positions_even_with_a_full_list():
    ctx = universe_context(
        snapshot={"as_of": None},
        positions=held("PFE"),
        triggers=[{"symbol": "F", "play_id": "DIP"}],
    )

    assert universe.resolve(ctx).candidates == ["PFE"]


def test_a_dateless_snapshot_says_so_in_its_reason():
    ctx = universe_context(snapshot={"as_of": None})

    assert "never" in universe.resolve(ctx).reason
