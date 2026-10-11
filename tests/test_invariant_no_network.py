"""Invariant 2 — the Python has no network access.

Per design v3 §15 this is the first test in the suite. It walks the import
graph of ``src/`` and fails on any networking module.

The scanner is tested against a fixture that genuinely violates the invariant,
because an invariant test that has never seen a violation proves nothing about
its own ability to catch one.
"""

from pathlib import Path

from tests.importscan import FORBIDDEN_MODULES, forbidden_imports

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
VIOLATIONS = Path(__file__).resolve().parent / "fixtures" / "invariant_violations"


def test_scanner_catches_a_plain_import():
    found = forbidden_imports([VIOLATIONS / "imports_socket.py"])

    assert [(f.module, f.lineno) for f in found] == [("socket", 3)]


def test_scanner_catches_a_from_import_of_a_submodule():
    found = forbidden_imports([VIOLATIONS / "imports_urllib_request.py"])

    assert [f.module for f in found] == ["urllib.request"]


def test_scanner_catches_an_import_nested_inside_a_function():
    found = forbidden_imports([VIOLATIONS / "imports_httpx_lazily.py"])

    assert [f.module for f in found] == ["httpx"]


def test_scanner_ignores_a_module_that_merely_shares_a_prefix():
    found = forbidden_imports([VIOLATIONS / "imports_socketserver_lookalike.py"])

    assert found == []


def test_every_forbidden_module_named_by_the_design_is_covered():
    assert FORBIDDEN_MODULES == frozenset(
        {"requests", "httpx", "yfinance", "urllib", "socket"}
    )


def test_src_has_no_network_imports():
    found = forbidden_imports(sorted(SRC.rglob("*.py")))

    assert found == [], "Invariant 2 breached: " + "; ".join(
        f"{f.path.relative_to(REPO_ROOT)}:{f.lineno} imports {f.module}" for f in found
    )
