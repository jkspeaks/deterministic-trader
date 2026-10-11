"""Fixtures shared by `tests/` and `services/core/tests/`."""

from __future__ import annotations

import pytest

from pathlib import Path

from src import cli
from src.plays import core

# The account numbers the fixtures and tests use are fixed dummies. The suite
# must not depend on the real number a fork writes into `config/inception.json`,
# so the shipped config's `account_number` is pinned to the dummy for every test
# that loads it. A test that passes its own config directory is left alone.
TEST_ACCOUNT_NUMBER = "<your-cash-account-number>"

_load_config = cli.load_config


def _load_config_with_test_account(config_dir=None):
    loaded = _load_config(config_dir)
    shipped = config_dir is None or Path(config_dir).resolve() == cli.CONFIG_DIR.resolve()
    if shipped and isinstance(loaded.get("inception"), dict):
        loaded["inception"] = {**loaded["inception"], "account_number": TEST_ACCOUNT_NUMBER}
    return loaded


cli.load_config = _load_config_with_test_account


@pytest.fixture
def all_plays_live(monkeypatch):
    """PEAD and SURGE are retired and CATALYST and MACRO are shelved
    (`plays.play_off`). A module that pins a play's own arithmetic, or that
    uses event triggers to exercise something else (discovery, the rotation,
    the regime, the pre-market plan), asks for this so it keeps testing what
    it was written to test. `ops/backtest_engine.py` still runs the same play
    functions, so their arithmetic is still worth pinning.
    `tests/test_retired_shelved_plays.py` pins the shipped behaviour.
    """
    monkeypatch.setattr(core, "RETIRED", {})
    monkeypatch.setattr(core, "SHELVED", {})
