"""Import-graph scanner backing the Invariant 2 test.

Parses rather than imports, so scanning a file never executes it. Matching is
on dotted-path roots, not substrings: ``socketserver`` is not ``socket``.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

FORBIDDEN_MODULES = frozenset({"requests", "httpx", "yfinance", "urllib", "socket"})


@dataclass(frozen=True)
class Finding:
    path: Path
    lineno: int
    module: str


def _root(dotted: str) -> str:
    return dotted.split(".", 1)[0]


def _scan_file(path: Path) -> list[Finding]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    findings: list[Finding] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _root(alias.name) in FORBIDDEN_MODULES:
                    findings.append(Finding(path, node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            # A relative import (level > 0) can only reach inside this package,
            # so it can never be one of the forbidden third-party modules.
            if node.level or node.module is None:
                continue
            if _root(node.module) in FORBIDDEN_MODULES:
                for alias in node.names:
                    findings.append(
                        Finding(path, node.lineno, f"{node.module}.{alias.name}")
                    )

    return findings


def forbidden_imports(paths: Iterable[Path]) -> list[Finding]:
    """Return every forbidden import reachable in ``paths``, in file order."""
    findings: list[Finding] = []
    for path in paths:
        findings.extend(_scan_file(path))
    return findings
