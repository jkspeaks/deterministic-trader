"""`sectors ...`.

The pure logic lives in `ops/sector_verbs.py`; `ops/control.py` is the operator
front door (GitHub Actions).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops import control
from ops import sector_verbs as sv

REPO = Path(__file__).resolve().parent.parent
DEFS = json.loads((REPO / "ops" / "verbs.json").read_text(encoding="utf-8"))["lists"][0]
ETFS = json.loads((REPO / "config" / "etfs.json").read_text(encoding="utf-8"))

# (op, current, text) -> expected list, or the refusal code. Everything is
# stored as its code — sectors (EN), baskets (SEMI) and sub-industries (CHIP) —
# in the code file's order: sectors, baskets, sub-industries. Full names, sleeve
# tickers and old stored names still resolve.
CASES = [
    ("set", [], "Energy", ["EN"]),
    ("set", [], "  health   CARE , xlf ", ["FIN", "HC"]),
    ("set", ["Energy"], "Information Technology, Energy", ["EN", "IT"]),
    ("set", [], "", sv.SET_NEEDS_NAMES),
    ("set", [], None, sv.SET_NEEDS_NAMES),
    ("set", [], "Finacials", sv.UNKNOWN),
    ("set", [], "Energy, Quantum", sv.UNKNOWN),
    ("set", [], "Broad Market", sv.UNKNOWN),
    ("set", [], "Energy, Semiconductors", ["EN", "CHIP"]),
    ("set", [], "semi, dc, chip", ["SEMI", "CHIP", "DC"]),
    ("set", [], "Data Center REITs", ["DC"]),
    ("add", ["SEMI"], "soft", ["SEMI", "SOFT"]),
    ("remove", ["SEMI", "DC"], "dc", ["SEMI"]),
    ("add", ["Energy"], "XLF", ["EN", "FIN"]),
    ("add", ["Energy"], "energy", ["EN"]),
    ("add", [], "Energy", sv.ADD_ONTO_ALL),
    ("add", ["Energy"], "", sv.NEEDS_NAMES),
    ("remove", ["Energy", "Financials"], "financials", ["EN"]),
    ("remove", ["Energy"], "Energy", sv.REMOVE_TO_EMPTY),
    ("remove", ["Energy"], "Utilities", sv.NOT_IN_SET),
    ("remove", [], "Energy", sv.ALREADY_ALL),
    ("clear", ["Energy"], None, []),
    ("clear", [], "ignored", []),
    # A stored value is canonicalized; junk in it is dropped, never kept.
    ("add", ["energy", "Finacials"], "XLK", ["EN", "IT"]),
]


def _python(op, current, text):
    try:
        return sv.mutate(op, current, text, DEFS)
    except sv.SectorVerbError as e:
        return e.code


def test_the_vocabulary_is_the_sleeve_verbatim():
    assert DEFS["sectors"] == list(ETFS["sector"].values())
    assert DEFS["aliases"] == ETFS["sector"]
    assert DEFS["key"] == "active_sectors"
    assert "Broad Market" not in DEFS["sectors"]


def test_the_codes_are_config_rotation_codes_verbatim():
    """The web host has no config/, so verbs.json carries a copy; this keeps
    the copy honest."""
    rc = json.loads((REPO / "config" / "rotation_codes.json").read_text(encoding="utf-8"))
    codes = DEFS["codes"]
    assert codes["sectors"] == rc["sectors"]
    assert codes["baskets"] == rc["baskets"]
    assert codes["sub_industries"] == {k: v["name"] for k, v in rc["sub_industries"].items()}
    assert set(codes["sectors"].values()) == set(ETFS["sector"].values())


@pytest.mark.parametrize("op,current,text,expected", CASES)
def test_python_mutate(op, current, text, expected):
    assert _python(op, current, text) == expected


def test_an_unknown_name_is_answered_with_the_nearest_match():
    with pytest.raises(sv.SectorVerbError) as err:
        sv.mutate("set", [], "Finacials", DEFS)
    assert "FIN (Financials)" in str(err.value)


def test_describe():
    assert sv.describe([], DEFS) == "all"
    assert sv.describe(None, DEFS) == "all"
    assert sv.describe(["financials", "Energy"], DEFS) == "EN, FIN"
    assert sv.describe(["SEMI", "dc"], DEFS) == "SEMI, DC"
    assert "matches no sector" in sv.describe(["Finacials"], DEFS)


# --- ops/control.py, the Actions front door ------------------------------------


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["sectors"], ("sectors", None)),
        (["sectors", "set", "Energy,", "Health", "Care"], ("sectors_set", "Energy, Health Care")),
        (["sectors_set", "Energy, Health Care"], ("sectors_set", "Energy, Health Care")),
        (["sectors", "clear"], ("sectors_clear", None)),
        (["sectors_add", "XLF", ""], ("sectors_add", "XLF")),
    ],
)
def test_parse_verb(argv, expected):
    assert control.parse_verb(argv) == expected


def test_a_bad_op_is_refused_by_the_parser():
    with pytest.raises(ValueError):
        control.parse_verb(["sectors", "replace", "Energy"])


class _FakeControl:
    def __init__(self, active):
        self.state = {"readable": True, "active_sectors": active}
        self.applied = []

    def get_control(self):
        return dict(self.state)

    def apply_control(self, updates):
        self.applied.append(updates)
        self.state.update(updates)


def test_apply_writes_the_canonical_list_through_edge_control_apply():
    fake = _FakeControl([])
    assert control._apply(fake, "sectors_set", "health care, XLE") == {
        "active_sectors": ["EN", "HC"]
    }
    assert fake.applied == [{"active_sectors": ["EN", "HC"]}]
    assert control._apply(fake, "sectors_clear", None) == {"active_sectors": []}


def test_a_refusal_writes_nothing():
    fake = _FakeControl(["Energy"])
    with pytest.raises(ValueError):
        control._apply(fake, "sectors_remove", "Energy")
    assert fake.applied == []


def test_the_key_is_whitelisted_and_the_help_names_the_verb():
    assert "active_sectors" in control.CONTROL_KEYS
    assert "sectors" in control.HELP_TEXT


# --- the control front door and the workflow ------------------------------------


def test_the_control_front_door_and_the_workflow_offer_the_same_sector_verbs():
    assert tuple(sv.OPS) == ("set", "add", "remove", "clear")
    workflow = (REPO / ".github" / "workflows" / "control.yml").read_text(encoding="utf-8")
    for verb in ("sectors", *(f"sectors_{op}" for op in sv.OPS)):
        assert f"          - {verb}\n" in workflow
        assert verb in control.VERBS
