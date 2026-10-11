"""Vercel's filesystem entry point for the evidence back-fill (S0).

The sixth function in the hosted-core project. An empty subclass rather than
a re-export, for the reason `api/cycle.py`'s docstring gives: Vercel's Python
builder finds functions by a class definition in the file.
"""

from services.core.api.evidence import handler as _EvidenceHandler


class handler(_EvidenceHandler):  # noqa: N801 — Vercel requires this exact name
    pass
