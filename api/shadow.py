"""Vercel's filesystem entry point for the shadow ledger's replay job.

The fourth function in the hosted-core project. Everything `api/cycle.py`'s
docstring says about *why this is an empty subclass* rather than a re-export
applies here identically: Vercel's Python builder decides which `api/*.py`
files are functions by finding a class definition in the file, and a
re-exported name is not one — the whole build fails with "The pattern
"api/shadow.py" defined in `functions` doesn't match any Serverless
Functions". So: a real `class handler`, empty, inheriting the tested class in
`services/core/api/shadow.py`.
"""

from services.core.api.shadow import handler as _ShadowHandler


class handler(_ShadowHandler):  # noqa: N801 — Vercel requires this exact name
    pass
