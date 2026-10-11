"""The five checked-in files, loaded from the routine's own git checkout.

`uv run python -m src.cli` cannot run unless the checkout is
already in the sandbox, so `config/` is there anyway — and an early probe
established that reading a checked-out file is not network access, which is what
keeps this inside Invariant 2.

The load-bearing test here is the first one. Every module reads
``ctx["config"]``, and until now the only thing that produced one was
`tests/factories.config()`. If the shipped file and the tested file drift, the
whole suite is pinning numbers the account never runs on.
"""

import json
from pathlib import Path

import pytest

from src import cli
from tests import factories

CONFIG = Path(__file__).resolve().parents[1] / "config"


def test_the_shipped_config_is_the_one_the_suite_tests_against():
    """The whole suite is written against `factories.config()`."""
    assert cli.load_config()["config"] == factories.config()
    assert not any(key.startswith("_") for key in factories.config())


def test_every_config_key_the_modules_read_is_shipped():
    """Named individually rather than inferred, because a `KeyError` at 09:40
    on an unattended cycle is the expensive way to discover a missing knob."""
    config = cli.load_config()["config"]

    assert set(config) >= {
        "min_position_size",
        "min_order_notional",
        "wash_sale_days",
        "wash_sale_block_unknown_basis",
        "earnings_flatten_days",
        "drawdown_halt_pct",
        "overnight_max_spread_pct",
        "catastrophe_multiple",
        "universe_max_age_days",
        "cash_reserve_floor",
        "cash_reserve_pct",
        "whole_share_bonus",
        "whole_shares_only",
        "max_new_positions_per_day",
        "entry_blackout_minutes",
        "crowding_penalty",
        "atr_stop_multiple",
        "average_down_min_drop_pct",
        "average_down_min_sessions_since_fill",
        "average_down_max_open_lots",
        "reconcile_epsilon_quantity",
        "reconcile_epsilon_notional",
        "dip_trend_band_pct",
        "entry_limit_offset_atr",
        "entry_resubmit_after_min",
    }


def test_the_loader_returns_all_six_files():
    """Four already existed and were read only by tests; `config.json` is new.
    cli is the first thing to read any of them at runtime."""
    loaded = cli.load_config()

    assert set(loaded) == {"config", "calendar", "etfs", "macro_map", "inception", "rotation_codes"}


def test_the_calendar_is_the_one_cadence_and_reconcile_share():
    """`cadence.is_open` and `reconcile.sessions_between` read the same table,
    and `test_cadence_calendar.py` fails when it stops covering the year. That
    test guards nothing unless the runtime calendar is this same file."""
    calendar = cli.load_config()["calendar"]

    assert "2026-12-25" in calendar["closed"]
    assert calendar["early_close"]["2026-11-27"] == "13:00"


def test_the_documentation_keys_are_stripped():
    """The four operator-maintained files carry their reasoning in-band under
    an underscore prefix. `_comment` is documentation, not configuration, and a
    module that iterated a config dict would otherwise see it as a knob."""
    loaded = cli.load_config()

    assert not any(key.startswith("_") for key in loaded["config"])
    assert not any(key.startswith("_") for key in loaded["etfs"])
    assert not any(key.startswith("_") for key in loaded["inception"])


def test_the_strip_is_top_level_only():
    """`macro_map.json` documents each release inline with `_why`, and §6.4
    puts that reasoning next to the mapping deliberately. `plays._macro` reads
    the map by direction and never enumerates it, so the note is free."""
    releases = cli.load_config()["macro_map"]["releases"]

    assert any("_why" in tilt for tilt in releases.values())


def test_the_loader_reads_from_a_named_directory():
    """Tests point it elsewhere; the routine gets the checkout's own `config/`.
    Without this the golden files would depend on the shipped inception book
    and could never record a second account."""
    loaded = cli.load_config(CONFIG)

    assert loaded["inception"]["inception_date"] == "2026-08-04"


def test_a_missing_config_file_is_not_silently_an_empty_one():
    """§14's first row: something that cannot read its own configuration must
    not act. An absent file returning `{}` would give `sizing.n_target` a
    `KeyError` three modules later, or worse, a default nobody chose."""
    with pytest.raises(FileNotFoundError):
        cli.load_config(CONFIG / "does-not-exist")


def test_the_shipped_config_is_valid_json_with_no_trailing_comma():
    """The operator maintains this file by hand, like the other four."""
    assert json.loads((CONFIG / "config.json").read_text(encoding="utf-8"))


def _write_config_dir(tmp_path, config_overrides):
    """A minimal five-file config dir, `config.json` patched with overrides.

    Reuses the shipped calendar/etfs/macro_map/inception verbatim — only
    `config.json` needs to vary for these tests, and `cli.load_config`
    requires all five files to exist.
    """
    for name, filename in cli.CONFIG_FILES.items():
        if name == "config":
            continue
        (tmp_path / filename).write_text(
            (CONFIG / filename).read_text(encoding="utf-8"), encoding="utf-8"
        )

    config = dict(factories.config())
    config.update(config_overrides)
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return tmp_path


def test_an_unrecognized_settled_cash_policy_raises(tmp_path):
    """`sizing.settled_basis` treats any unknown string as `conservative`
    (the strict fallback), so a typo here degrades safely at runtime but would
    otherwise run silently for weeks in the wrong mode. Catching it at load
    time turns that into an immediate, loud failure instead."""
    _write_config_dir(tmp_path, {"settled_cash_policy": "conservtive"})

    with pytest.raises(ValueError, match="settled_cash_policy"):
        cli.load_config(tmp_path)


def test_a_recognized_settled_cash_policy_loads_fine(tmp_path):
    _write_config_dir(tmp_path, {"settled_cash_policy": "conservative"})

    assert cli.load_config(tmp_path)["config"]["settled_cash_policy"] == "conservative"


@pytest.mark.parametrize(
    "key",
    [
        "drawdown_halt_pct",
        "cash_reserve_pct",
        "mc_stop_probability_max",
        "mc_drawdown_p95_max",
        "overnight_max_spread_pct",
    ],
)
def test_a_fraction_field_above_one_raises(tmp_path, key):
    """These are read via a plain `float()` and compared directly (guard.py,
    mc.py) — a `20` typed for a `0.20` field would deny or admit on a
    threshold nobody chose, with no crash to flag it."""
    _write_config_dir(tmp_path, {key: 20})

    with pytest.raises(ValueError, match=key):
        cli.load_config(tmp_path)


def test_a_fraction_field_negative_raises(tmp_path):
    _write_config_dir(tmp_path, {"drawdown_halt_pct": -0.1})

    with pytest.raises(ValueError, match="drawdown_halt_pct"):
        cli.load_config(tmp_path)


def test_a_fraction_field_at_the_boundary_is_fine(tmp_path):
    _write_config_dir(tmp_path, {"drawdown_halt_pct": 1.0, "cash_reserve_pct": 0.0})

    loaded = cli.load_config(tmp_path)["config"]
    assert loaded["drawdown_halt_pct"] == 1.0
    assert loaded["cash_reserve_pct"] == 0.0


# --- P1: the posture group -------------------------------------


def test_the_posture_group_ships_switched_off_with_the_audit_table():
    config = cli.load_config()["config"]

    assert config["regime_v2_enabled"] is False
    assert config["regime_breadth_min_sectors"] == 8
    assert config["posture_size_multiplier"]["REDUCED_SIZE"] == 0.5
    assert config["posture_size_multiplier"]["HIGH_CONVICTION_ONLY"] == 0.5
    assert config["posture_size_multiplier"]["NO_NEW_POSITIONS"] == 0.0
    assert set(config["posture_target_invested_pct"]) == set(
        config["posture_size_multiplier"]
    )


def test_the_shipped_posture_knobs_are_the_modules_own_defaults():
    """Every knob is read with a default so an old config computes the same
    posture. Two copies of a number are only safe while they agree."""
    from src import regime as regimes

    config = cli.load_config()["config"]

    for key, value in regimes.POSTURE_DEFAULTS.items():
        assert config[key] == value, key
    assert config["posture_size_multiplier"] == regimes.POSTURE_SIZE_MULTIPLIER_DEFAULT
    assert (
        config["posture_target_invested_pct"]
        == regimes.POSTURE_TARGET_INVESTED_PCT_DEFAULT
    )


@pytest.mark.parametrize(
    "override, match",
    [
        ({"regime_v2_enabled": "false"}, "regime_v2_enabled"),
        ({"posture_normal_breadth_200": 60}, "posture_normal_breadth_200"),
        ({"posture_no_new_breadth_200": 0.50}, "ordered"),
        ({"regime_breadth_min_sectors": 0}, "regime_breadth_min_sectors"),
        ({"posture_size_multiplier": {"REDUCED": 0.5}}, "REDUCED"),
        ({"posture_size_multiplier": {"REDUCED_SIZE": 5}}, "REDUCED_SIZE"),
        ({"posture_size_multiplier": {"NO_NEW_POSITIONS": 0.5}}, "NO_NEW_POSITIONS"),
        ({"posture_target_invested_pct": {"HOLD_CASH": 0.2}}, "HOLD_CASH"),
    ],
)
def test_a_mistyped_posture_knob_raises(tmp_path, override, match):
    _write_config_dir(tmp_path, override)

    with pytest.raises(ValueError, match=match):
        cli.load_config(tmp_path)


# --- S3: the DIP decision table ---------------------------------


def test_the_dip_table_ships_switched_off_and_equal_to_the_p1_rule():
    from src import regime as regimes

    config = cli.load_config()["config"]

    assert config["dip_regime_table_enabled"] is False
    table = config["dip_regime_table"]
    assert set(table) == set(regimes.POSTURES)
    for posture, row in table.items():
        p1 = regimes.DIP_REGIME_TABLE_P1[posture]
        assert {k: row[k] for k in p1} == p1, posture
        assert row["dip_size_multiplier"] == config["posture_size_multiplier"][posture]


@pytest.mark.parametrize(
    "override, match",
    [
        ({"dip_regime_table_enabled": "false"}, "dip_regime_table_enabled"),
        ({"dip_regime_table": []}, "dip_regime_table"),
        ({"dip_regime_table": {"REDUCED": {}}}, "REDUCED"),
        ({"dip_regime_table": {"REDUCED_SIZE": True}}, "REDUCED_SIZE"),
        ({"dip_regime_table": {"REDUCED_SIZE": {"dip_alowed": True}}}, "dip_alowed"),
        ({"dip_regime_table": {"REDUCED_SIZE": {"dip_allowed": "yes"}}}, "dip_allowed"),
        ({"dip_regime_table": {"REDUCED_SIZE": {"dip_size_multiplier": 1.5}}}, "never raise"),
        ({"dip_regime_table": {"REDUCED_SIZE": {"dip_size_multiplier": True}}}, "dip_size_multiplier"),
        ({"dip_regime_table": {"REDUCED_SIZE": {"max_rsi": 250}}}, "max_rsi"),
        ({"dip_regime_table": {"HOLD_CASH": {"dip_allowed": True}}}, "HOLD_CASH"),
    ],
)
def test_a_mistyped_dip_table_raises(tmp_path, override, match):
    _write_config_dir(tmp_path, override)

    with pytest.raises(ValueError, match=match):
        cli.load_config(tmp_path)


def test_a_partial_dip_table_loads(tmp_path):
    """One edited row is a valid table; the rest fall back to the P1 rule."""
    _write_config_dir(tmp_path, {"dip_regime_table": {
        "NO_NEW_POSITIONS": {"dip_allowed": True, "dip_size_multiplier": 0.5, "max_rsi": 25}}})

    assert cli.load_config(tmp_path)["config"]["dip_regime_table"]["NO_NEW_POSITIONS"]["max_rsi"] == 25


# --- P2: CATALYST's time-normalised volume ---------------------


@pytest.mark.parametrize(
    "override, match",
    [
        ({"catalyst_rvol_time_normalised": "false"}, "catalyst_rvol_time_normalised"),
        ({"catalyst_min_minutes": 0}, "catalyst_min_minutes"),
        ({"catalyst_min_minutes": 400}, "catalyst_min_minutes"),
        ({"catalyst_min_minutes": True}, "catalyst_min_minutes"),
    ],
)
def test_a_mistyped_catalyst_knob_raises(tmp_path, override, match):
    _write_config_dir(tmp_path, override)

    with pytest.raises(ValueError, match=match):
        cli.load_config(tmp_path)


def test_the_shipped_minimum_is_the_modules_own_default():
    from src.plays import catalyst

    assert cli.load_config()["config"]["catalyst_min_minutes"] == catalyst.MIN_MINUTES_DEFAULT


@pytest.mark.parametrize(
    "override, match",
    [
        ({"dip_recovery_relvol_time_normalised": 1}, "dip_recovery_relvol_time_normalised"),
        ({"dip_recovery_relvol_min_minutes": 0}, "dip_recovery_relvol_min_minutes"),
    ],
)
def test_a_mistyped_dip_recovery_relvol_knob_raises(tmp_path, override, match):
    _write_config_dir(tmp_path, override)

    with pytest.raises(ValueError, match=match):
        cli.load_config(tmp_path)


def test_both_time_normalised_switches_ship_off():
    config = cli.load_config()["config"]

    assert config["catalyst_rvol_time_normalised"] is False
    assert config["dip_recovery_relvol_time_normalised"] is False


# --- P3: portfolio construction and posture sizing -------------


def test_sizing_v2_ships_off_with_its_knobs():
    config = cli.load_config()["config"]

    assert config["sizing_v2_enabled"] is False
    assert config["one_per_sector_day"] is True
    assert config["reentry_block_days"] == 14
    assert config["corr_holdings_budget_fraction"] == 0.5
    assert config["dead_play_alert_days"] == 5


@pytest.mark.parametrize(
    "override, match",
    [
        ({"sizing_v2_enabled": "false"}, "sizing_v2_enabled"),
        ({"one_per_sector_day": 1}, "one_per_sector_day"),
        ({"corr_holdings_budget_fraction": 0}, "corr_holdings_budget_fraction"),
        ({"corr_holdings_budget_fraction": 50}, "corr_holdings_budget_fraction"),
        ({"reentry_block_days": 0}, "reentry_block_days"),
        ({"reentry_block_days": 14.5}, "reentry_block_days"),
        ({"dead_play_alert_days": -1}, "dead_play_alert_days"),
    ],
)
def test_a_mistyped_sizing_v2_knob_raises(tmp_path, override, match):
    _write_config_dir(tmp_path, override)

    with pytest.raises(ValueError, match=match):
        cli.load_config(tmp_path)


def test_the_shipped_sizing_v2_numbers_are_the_modules_own_defaults():
    from src import deployment, sizing

    config = cli.load_config()["config"]

    assert config["corr_holdings_budget_fraction"] == sizing.CORR_HOLDINGS_BUDGET_FRACTION_DEFAULT
    assert config["dead_play_alert_days"] == deployment.DEAD_PLAY_ALERT_DAYS_DEFAULT


# --- the cycle budgets -------------------------------------------


def test_the_cycle_budgets_ship_as_the_prompt_names_them():
    assert cli.load_config()["config"]["cycle_budget_min"] == {
        "full": 40, "monitor": 20, "dip-monitor": 15, "postclose": 10, "overnight": 20,
    }


@pytest.mark.parametrize(
    "override, match",
    [
        ({"cycle_budget_min": {"full": 0}}, "cycle_budget_min.full"),
        ({"cycle_budget_min": {"full": "40"}}, "cycle_budget_min.full"),
        ({"cycle_budget_min": {"monitor": 20.5}}, "cycle_budget_min.monitor"),
        ({"cycle_budget_min": {"full": 2400}}, "cycle_budget_min.full"),
        ({"cycle_budget_min": 40}, "cycle_budget_min"),
    ],
)
def test_a_mistyped_cycle_budget_raises(tmp_path, override, match):
    config = dict(factories.config())
    config.update(override)
    for name, filename in cli.CONFIG_FILES.items():
        text = (
            json.dumps(config) if name == "config"
            else (CONFIG / filename).read_text(encoding="utf-8")
        )
        (tmp_path / filename).write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match=match):
        cli.load_config(tmp_path)
