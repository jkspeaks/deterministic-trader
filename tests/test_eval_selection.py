"""`ops/eval_selection.py` — the one evaluation path for selection evidence.

The offline studies and the live control arm are scored by these functions,
so a wrong one is wrong everywhere at once. Each test pins a number that can
be worked out by hand, or a property a careless rewrite would lose: ties
ranked by their average, same-day events resampled together, missing values
dropped rather than filled, the deflation growing with the trial count.
"""

from __future__ import annotations

import csv
import math
import random

import pytest

from ops import eval_selection as es


# --- ranks and correlations --------------------------------------------------


def test_ties_share_the_average_of_the_ranks_they_span():
    assert es.ranks([10.0, 30.0, 20.0]) == [1.0, 3.0, 2.0]
    assert es.ranks([5.0, 5.0, 1.0, 9.0]) == [2.5, 2.5, 1.0, 4.0]
    assert es.ranks([7.0, 7.0, 7.0]) == [2.0, 2.0, 2.0]


def test_spearman_is_one_for_any_monotone_relation_and_minus_one_reversed():
    x = [1.0, 2.0, 3.0, 4.0, 5.0]

    assert es.spearman(x, [v ** 3 for v in x]) == pytest.approx(1.0)
    assert es.spearman(x, [-v for v in x]) == pytest.approx(-1.0)
    # The textbook case: d = (0, 1, -1, 0, 0)... rho = 1 - 6*sum(d^2)/(n(n^2-1)).
    assert es.spearman(x, [1.0, 3.0, 2.0, 4.0, 5.0]) == pytest.approx(1 - 6 * 2 / 120)


def test_a_constant_side_or_too_few_pairs_has_no_correlation():
    assert es.spearman([1.0, 2.0, 3.0], [4.0, 4.0, 4.0]) is None
    assert es.spearman([1.0, 2.0], [2.0, 1.0]) is None


# --- one date's cross-section ------------------------------------------------


def test_sector_demeaning_removes_the_sector_and_keeps_the_stock():
    returns = [0.10, 0.12, -0.05, -0.03]
    sectors = ["Energy", "Energy", "Utilities", "Utilities"]

    assert es.demean_by(returns, sectors) == pytest.approx([-0.01, 0.01, -0.01, 0.01])
    # Alone in its sector on the date: no within-sector information.
    assert es.demean_by([0.2, 0.1, 0.3], ["A", "B", "B"]) == pytest.approx([0.0, -0.1, 0.1])


def test_a_factor_that_only_picks_sectors_has_no_sector_neutral_ic():
    """Energy returned 10%, Utilities -5%. A factor that is high for every
    Energy name ranks raw returns perfectly and ranks nothing within a sector."""
    n = 20
    sectors = ["Energy"] * n + ["Utilities"] * n
    rng = random.Random(1)
    noise = [rng.gauss(0.0, 0.01) for _ in range(2 * n)]
    forward = [(0.10 if s == "Energy" else -0.05) + e for s, e in zip(sectors, noise)]
    factor = [(1.0 if s == "Energy" else 0.0) + rng.random() * 0.5 for s in sectors]

    raw = es.cross_section(factor, forward)
    neutral = es.cross_section(factor, forward, sectors)

    assert raw["ic"] > 0.7
    assert abs(neutral["ic"]) < 0.35


def test_missing_values_are_dropped_as_pairs_never_filled():
    factor = [1.0, 2.0, None, 4.0, float("nan"), 6.0] * 5
    forward = [0.01, 0.02, 0.03, None, 0.05, 0.06] * 5

    stat = es.cross_section(factor, forward, min_names=5)

    assert stat["n"] == 15                       # three usable pairs per block of six
    assert stat["ic"] == pytest.approx(1.0)


def test_a_thin_cross_section_is_no_statistic_at_all():
    assert es.cross_section([1.0, 2.0, 3.0], [0.1, 0.2, 0.3], min_names=20) is None


def test_the_quantile_spread_is_top_minus_bottom():
    factor = [float(i) for i in range(10)]
    forward = [0.01 * i for i in range(10)]

    stat = es.cross_section(factor, forward, min_names=5, quantiles=5)

    # Top two (0.08, 0.09) minus bottom two (0.00, 0.01).
    assert stat["spread"] == pytest.approx(0.08)


def test_the_top_bucket_is_reported_with_the_callers_own_positions():
    # A missing pair in front shifts every later position by one; the top
    # bucket must still point into the sequences the caller passed in.
    factor = [None, *[float(i) for i in range(10)]]
    forward = [0.5, *[0.01 * i for i in range(10)]]

    stat = es.cross_section(factor, forward, min_names=5, quantiles=5)

    assert stat["top"] == pytest.approx(0.085)
    assert sorted(stat["top_rows"]) == [9, 10]


# --- S2: the sector-neutral score and the composite ---------------------------


def test_a_z_score_compares_a_name_with_its_own_sector_only():
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    sectors = ["Energy"] * 5 + ["Utilities"] * 5

    z = es.sector_z(values, sectors)

    # The two sectors sit a hundred apart and score identically.
    assert z[:5] == pytest.approx(z[5:])
    assert z[2] == pytest.approx(0.0)
    assert z[4] == pytest.approx(2.0 / math.sqrt(2.5))


def test_an_outlier_is_winsorized_at_three():
    values = [0.0] * 30 + [1000.0]

    z = es.sector_z(values, ["Energy"] * 31)

    assert z[-1] == 3.0
    assert all(v < 0.0 for v in z[:-1])


def test_a_thin_or_flat_sector_and_a_missing_value_score_nothing():
    values = [1.0, 2.0, 3.0, 4.0, 7.0, 7.0, 7.0, 7.0, 7.0, 1.0, 2.0, None, 4.0, 5.0, 6.0]
    sectors = ["Thin"] * 4 + ["Flat"] * 5 + ["Fine"] * 6

    z = es.sector_z(values, sectors)

    assert z[:9] == [None] * 9
    assert z[11] is None
    assert all(v is not None for i, v in enumerate(z[9:]) if i != 2)
    # A name with no sector cannot be neutralised against one.
    assert es.sector_z([1.0, 2.0, 3.0, 4.0, 5.0], [None] * 5) == [None] * 5


def test_the_composite_is_the_equal_weight_average_of_scaled_ranks():
    a = [1.0, 2.0, 3.0, 4.0]
    b = [40.0, 30.0, 20.0, 10.0]

    # Opposite orders cancel exactly; the same order twice is the order.
    assert es.rank_average([a, b]) == pytest.approx([0.5, 0.5, 0.5, 0.5])
    assert es.rank_average([a, a]) == pytest.approx([0.0, 1 / 3, 2 / 3, 1.0])


def test_a_name_is_scored_on_the_factors_it_has_if_it_has_half_of_them():
    a = [1.0, 2.0, 3.0, None]
    b = [1.0, None, 3.0, None]
    c = [1.0, None, 3.0, None]

    got = es.rank_average([a, b, c])

    assert got[0] == pytest.approx(0.0)
    assert got[2] == pytest.approx(1.0)
    assert got[1] is None          # one of three is under half
    assert got[3] is None
    assert es.rank_average([a, b])[1] == pytest.approx(0.5)     # one of two is half


# --- a series of ICs ---------------------------------------------------------


def test_the_ic_summary_is_mean_sd_icir_and_t():
    ics = [0.02, 0.04, 0.06]

    summary = es.ic_summary(ics)

    assert summary["n"] == 3 and summary["mean"] == pytest.approx(0.04)
    assert summary["sd"] == pytest.approx(0.02)
    assert summary["icir"] == pytest.approx(2.0)
    assert summary["t"] == pytest.approx(0.04 / (0.02 / math.sqrt(3)))
    assert summary["hit"] == 1.0
    assert es.ic_summary([])["t"] is None


def test_the_bar_needs_both_the_t_and_four_of_five_years():
    strong = {"t": 3.4, "mean": 0.03}
    steady = {"2021": 0.02, "2022": 0.03, "2023": -0.01, "2024": 0.04, "2025": 0.02}
    patchy = {"2021": 0.09, "2022": -0.01, "2023": -0.01, "2024": 0.08, "2025": 0.02}

    assert es.passes_bar(strong, steady) is True
    assert es.passes_bar(strong, patchy) is False              # 3 of 5
    assert es.passes_bar({"t": 2.9, "mean": 0.03}, steady) is False
    assert es.passes_bar(strong, {"2025": 0.02, "2026": 0.03}) is False   # too few years to say
    # A negative factor passes on negative years, not positive ones.
    assert es.passes_bar({"t": -3.4, "mean": -0.03}, {y: -v for y, v in steady.items()}) is True


def test_top_quantile_turnover_is_the_share_replaced():
    names = [f"S{i}" for i in range(10)]
    same = {name: float(i) for i, name in enumerate(names)}
    flipped = {name: float(-i) for i, name in enumerate(names)}
    # Top two are S8, S9; then S9 is replaced by S0.
    one_swapped = {**same, "S9": -1.0, "S0": 99.0}

    assert es.top_quantile_turnover({"d1": same, "d2": same}) == 0.0
    assert es.top_quantile_turnover({"d1": same, "d2": flipped}) == 1.0
    assert es.top_quantile_turnover({"d1": same, "d2": one_swapped}) == 0.5
    assert es.top_quantile_turnover({"d1": same}) is None


# --- the paired, date-clustered bootstrap ------------------------------------


def test_the_bootstrap_is_reproducible_and_brackets_the_mean():
    rng = random.Random(3)
    diffs = [rng.gauss(0.004, 0.03) for _ in range(400)]
    days = [f"d{i // 4}" for i in range(400)]

    first = es.clustered_bootstrap(diffs, days, boot=2000)
    again = es.clustered_bootstrap(diffs, days, boot=2000)

    assert first == again
    assert first["n"] == 400 and first["clusters"] == 100
    assert first["mean"] == pytest.approx(sum(diffs) / 400)
    assert first["lo"] < first["mean"] < first["hi"]


def test_same_day_events_are_resampled_together():
    """Forty dates, ten events each, and all the variation is between dates.
    Treated as 400 independent draws the interval would be a third as wide."""
    rng = random.Random(5)
    days, diffs = [], []
    for d in range(40):
        shock = rng.gauss(0.0, 0.03)
        for _ in range(10):
            days.append(f"d{d}")
            diffs.append(shock + rng.gauss(0.0, 0.001))

    clustered = es.clustered_bootstrap(diffs, days, boot=2000)
    naive = es.clustered_bootstrap(diffs, list(range(400)), boot=2000)

    assert (clustered["hi"] - clustered["lo"]) > 2.5 * (naive["hi"] - naive["lo"])


def test_unusable_differences_are_skipped_and_too_few_is_no_result():
    diffs = [0.01] * 25 + [None, float("nan"), "x"]
    days = [f"d{i}" for i in range(28)]

    assert es.clustered_bootstrap(diffs, days, boot=200)["n"] == 25
    assert es.clustered_bootstrap(diffs[:10], days[:10], boot=200) is None


def test_a_clear_edge_has_no_resample_at_or_below_zero():
    stat = es.clustered_bootstrap([0.02] * 30, [f"d{i}" for i in range(30)], boot=500)

    assert stat["p_le_0"] == 0.0 and stat["lo"] == pytest.approx(0.02)


# --- pairing the control arm -------------------------------------------------


def shadow(variant, symbol, day, pnl, pair="", reason="TIME_STOP"):
    return {"variant": variant, "symbol": symbol, "entry_day": day, "pair_symbol": pair,
            "pnl_pct": pnl, "reason": reason, "play_id": "DIP"}


def test_a_signal_is_joined_to_its_own_stand_in_by_pair_symbol_and_day():
    rows = [
        shadow("R_signal_norsi", "MU", "2026-10-05", 0.04),
        shadow("R_random_sector_norsi", "ON", "2026-10-05", 0.01, pair="MU"),
        shadow("R_signal_norsi", "HPE", "2026-10-05", -0.02),
        shadow("R_random_sector_norsi", "ON", "2026-10-05", 0.01, pair="HPE"),   # same stand-in
        shadow("R_signal_norsi", "MU", "2026-10-06", 0.00),
    ]

    pairs = es.control_pairs(rows)

    assert [(p["symbol"], p["control"], p["entry_day"]) for p in pairs] == [
        ("MU", "ON", "2026-10-05"), ("HPE", "ON", "2026-10-05")]
    assert pairs[0]["diff"] == pytest.approx(0.03)
    assert pairs[1]["diff"] == pytest.approx(-0.03)


def test_an_open_or_unpriced_leg_leaves_the_pair_out():
    rows = [
        shadow("R_signal_norsi", "MU", "2026-10-05", None, reason="OPEN"),
        shadow("R_random_sector_norsi", "ON", "2026-10-05", 0.01, pair="MU"),
        shadow("R_signal_norsi", "HPE", "2026-10-05", 0.02),
        shadow("R_random_sector_norsi", "JBL", "2026-10-05", None, pair="HPE", reason="NO_BARS"),
    ]

    assert es.control_pairs(rows) == []


def test_the_rsi_and_no_rsi_arms_are_never_mixed():
    rows = [
        shadow("R_signal", "MU", "2026-10-05", 0.02, reason="SIGNAL"),
        shadow("R_random_sector_norsi", "ON", "2026-10-05", 0.01, pair="MU"),
    ]

    assert es.control_pairs(rows) == []
    assert es.control_pairs(rows, "R_signal", "R_random_sector") == []


def test_spy_pairs_through_the_same_join():
    rows = [
        shadow("R_signal_norsi", "MU", "2026-10-05", 0.04),
        shadow("B_hold_spy", "SPY", "2026-10-05", 0.015, pair="MU"),
    ]

    (pair,) = es.control_pairs(rows, "R_signal_norsi", "B_hold_spy")

    assert pair["diff"] == pytest.approx(0.025)


def test_a_name_that_keeps_signalling_is_one_trade_not_five():
    events = [{"symbol": "MU", "entry_day": d} for d in
              ("2026-10-05", "2026-10-06", "2026-10-07", "2026-10-20")]
    events.append({"symbol": "ON", "entry_day": "2026-10-06"})

    kept = es.deoverlap(events, hold_days=12)

    assert [(e["symbol"], e["entry_day"]) for e in kept] == [
        ("MU", "2026-10-05"), ("ON", "2026-10-06"), ("MU", "2026-10-20")]


# --- the trial ledger and deflation ------------------------------------------


def test_the_ledger_counts_what_is_appended(tmp_path):
    ledger = tmp_path / "ledger" / "trial-ledger.csv"

    assert es.ledger_count(ledger) == 0
    es.ledger_append({"date": "2026-10-05", "phase": "S1", "variant": "a", "n": 10,
                      "metric": "m", "result": "+0.1%"}, ledger)
    es.ledger_append({"date": "2026-10-05", "phase": "S2", "variant": "b, with a comma"}, ledger)

    assert es.ledger_count(ledger) == 2 and es.ledger_count(ledger, "S1") == 1
    with ledger.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert list(rows[0]) == list(es.LEDGER_FIELDS)
    assert rows[1]["variant"] == "b, with a comma"


def test_bonferroni_scales_with_the_trials_and_caps_at_one():
    assert es.bonferroni(0.01, 1) == 0.01
    assert es.bonferroni(0.01, 20) == pytest.approx(0.2)
    assert es.bonferroni(0.01, 500) == 1.0
    assert es.bonferroni(None, 20) is None


def test_the_t_a_result_needs_rises_with_every_look_at_the_data():
    assert es.bonferroni_t(1) == pytest.approx(1.96, abs=0.005)
    assert es.bonferroni_t(112) == pytest.approx(3.51, abs=0.01)
    assert es.bonferroni_t(1) < es.bonferroni_t(20) < es.bonferroni_t(200)


def test_p_from_t_is_the_two_sided_normal_tail():
    assert es.p_two_sided(1.96) == pytest.approx(0.05, abs=0.001)
    assert es.p_two_sided(-1.96) == pytest.approx(0.05, abs=0.001)
    assert es.p_two_sided(None) is None


def test_the_deflated_sharpe_falls_as_the_trials_behind_it_grow():
    args = dict(sharpe=0.12, n_obs=400, sharpe_sd=0.05)

    alone = es.deflated_sharpe(trials=1, **args)
    few = es.deflated_sharpe(trials=10, **args)
    many = es.deflated_sharpe(trials=200, **args)

    assert alone > few > many
    # One trial: no selection to correct for, just "is the Sharpe above zero".
    assert alone == pytest.approx(es._NORMAL.cdf(0.12 * math.sqrt(399) / math.sqrt(1 + 0.5 * 0.12 ** 2)))
    assert es.expected_max_sharpe(1, 0.05) == 0.0
    assert es.expected_max_sharpe(100, 0.05) > es.expected_max_sharpe(10, 0.05) > 0


# --- power -------------------------------------------------------------------


def test_the_sample_size_reproduces_the_programs_section_1_5_figure():
    """Per-trade sd 4.65%, a 0.30% edge, 5% two-sided, 80% power: about 1,900."""
    assert es.required_n(0.0465, 0.0030) == 1886
    # Twice the edge needs a quarter of the pairs.
    assert es.required_n(0.0465, 0.0060) == pytest.approx(1886 / 4, abs=2)
    assert es.sessions_needed(1886, 1.15) == pytest.approx(1640, abs=1)
    assert es.sessions_needed(1886, 0.0) is None


# --- the live panel ----------------------------------------------------------


def snapshot_row(symbol, day, sector, mom, ret, cycle="c1", verdict="SKIPPED"):
    return {"symbol": symbol, "trading_day": day, "cycle_id": cycle, "sector": sector,
            "verdict": verdict, "price_features": {"mom_12_1": mom}, "fwd": {"no_8": ret, "no_21": ret}}


def panel_rows(sign=1.0, days=6):
    rng = random.Random(11)
    rows = []
    for d in range(days):
        day = f"2026-10-{5 + d:02d}"
        for i in range(12):
            sector = "Energy" if i % 2 else "Utilities"
            mom = rng.random()
            rows.append(snapshot_row(f"S{i}", day, sector, mom, sign * 0.05 * mom + rng.gauss(0, 0.002)))
    return rows


def test_the_live_panel_finds_a_factor_that_ranks_returns_within_sector():
    panel = es.factor_panel(panel_rows())

    assert panel["mom_12_1"]["n"] == 6 and panel["mom_12_1"]["names"] == 72
    assert panel["mom_12_1"]["mean"] > 0.8 and panel["mom_12_1"]["spread"] > 0
    assert panel["mom_12_1"]["coverage"] == 1.0
    # A factor nothing recorded is absent — zero coverage, no IC — not weak.
    assert panel["conviction"]["n"] == 0 and panel["conviction"]["coverage"] == 0.0
    # Six days is not five calendar years: nothing can pass the bar yet.
    assert panel["mom_12_1"]["passes_bar"] is False


def test_a_monitor_cycle_does_not_double_the_days_cross_section():
    rows = panel_rows(days=2)
    again = [{**r, "cycle_id": "c2"} for r in rows]

    assert es.factor_panel(rows + again)["mom_12_1"]["names"] == es.factor_panel(rows)["mom_12_1"]["names"]
    assert len(es.first_per_day(rows + again)) == len(rows)


def test_the_live_panel_scores_the_recorded_estimate_revision_and_counts_its_coverage():
    """S2, factor 12. The evidence job puts `estimates` on a row; a row the
    vendor would not answer carries a status and no reading, and is counted
    as uncovered rather than scored as zero."""
    rows = panel_rows()
    for i, row in enumerate(rows):
        if i % 4 == 0:
            row["estimates"] = {"status": "unavailable", "code": 403, "reason": "plan"}
        elif i % 4 == 1:
            row["estimates"] = None
        else:
            row["estimates"] = {"status": "ok", "eps_revision_90_7": row["price_features"]["mom_12_1"],
                                "revision_breadth_1m": None}

    panel = es.factor_panel(rows, min_names=4)

    assert panel["eps_revision"]["coverage"] == pytest.approx(0.5)
    assert panel["eps_revision"]["mean"] > 0.5
    assert panel["revision_breadth"]["coverage"] == 0.0 and panel["revision_breadth"]["n"] == 0


def test_evaluate_reports_the_control_arm_with_its_deflation():
    rng = random.Random(2)
    shadow_rows = []
    for i in range(40):
        day = f"2026-{10 + i // 28:02d}-{1 + i % 28:02d}"
        symbol = f"S{i}"
        shadow_rows.append(shadow("R_signal_norsi", symbol, day, 0.03 + rng.gauss(0, 0.01)))
        shadow_rows.append(shadow("R_random_sector_norsi", f"C{i}", day, 0.00 + rng.gauss(0, 0.01), pair=symbol))
        shadow_rows.append(shadow("B_hold_spy", "SPY", day, 0.005, pair=symbol))

    result = es.evaluate(panel_rows(), shadow_rows, trials=112, boot=500)

    arm = result["control_arm"]["no_rsi_exit (primary)"]
    assert arm["pairs_closed"] == arm["pairs_independent"] == 40
    assert arm["paired"]["mean"] == pytest.approx(0.03, abs=0.01) and arm["paired"]["lo"] > 0
    assert arm["paired"]["p_bonferroni"] == 0.0
    # No RSI-exit rows were supplied: that arm says so instead of borrowing.
    assert result["control_arm"]["with_rsi_exit"]["paired"] is None
    assert result["vs_spy"]["paired"]["mean"] == pytest.approx(0.025, abs=0.01)
    assert result["trials"] == 112 and result["t_needed_bonferroni"] == pytest.approx(3.51, abs=0.01)
    text = es.render(result)
    assert "112" in text and "no_rsi_exit (primary)" in text and "too few to test" in text


def test_evaluate_on_an_empty_ledger_says_too_few_rather_than_failing():
    result = es.evaluate([], [], trials=112, boot=200)

    assert result["snapshot_rows"] == 0
    assert result["control_arm"]["no_rsi_exit (primary)"]["paired"] is None
    assert "too few to test" in es.render(result)


def test_the_power_command_prints_the_table(capsys):
    assert es.main(["power", "--sd", "0.0465", "--rate", "1.15", "--effects", "0.003"]) == 0

    out = capsys.readouterr().out
    assert "| 0.30% | 1,886 | 1,640 | 6.5 |" in out


# --- S3: a difference between two groups of events ----------------------------


def test_the_group_difference_is_the_two_means_and_is_seeded():
    rng = random.Random(11)
    values, flags, days = [], [], []
    for i in range(600):
        inside = i % 3 == 0
        values.append((0.02 if inside else 0.0) + rng.gauss(0.0, 0.01))
        flags.append(inside)
        days.append(f"d{i // 5}")

    first = es.clustered_bootstrap_difference(values, flags, days, boot=1000)
    again = es.clustered_bootstrap_difference(values, flags, days, boot=1000)

    assert first == again
    assert (first["n_in"], first["n_out"]) == (200, 400)
    inside = [v for v, f in zip(values, flags) if f]
    outside = [v for v, f in zip(values, flags) if not f]
    assert first["diff"] == pytest.approx(sum(inside) / 200 - sum(outside) / 400)
    assert first["lo"] > 0.0 and first["p_le_0"] == 0.0
    assert first["t"] > 3.0


def test_a_shared_daily_shock_cancels_when_both_groups_trade_that_day():
    """All the variation is between dates and both groups see every date, so
    the difference is known far more tightly than either mean."""
    rng = random.Random(3)
    values, flags, days = [], [], []
    for d in range(60):
        shock = rng.gauss(0.0, 0.03)
        for k in range(6):
            values.append(shock + rng.gauss(0.0, 0.001))
            flags.append(k < 3)
            days.append(f"d{d}")

    diff = es.clustered_bootstrap_difference(values, flags, days, boot=1000)
    one = es.clustered_bootstrap([v for v, f in zip(values, flags) if f],
                                 [d for d, f in zip(days, flags) if f], boot=1000)

    assert (diff["hi"] - diff["lo"]) < 0.2 * (one["hi"] - one["lo"])


def test_a_group_difference_needs_enough_events_on_both_sides():
    values = [0.01] * 40
    days = [f"d{i}" for i in range(40)]

    assert es.clustered_bootstrap_difference(values, [i < 5 for i in range(40)], days, boot=200) is None
    assert es.clustered_bootstrap_difference(values, [i < 20 for i in range(40)], days, boot=200)["diff"] == 0.0
