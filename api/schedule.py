"""Vercel's filesystem entry point for the daily schedule producer.

The fourth function in the hosted-core project. Everything `api/cycle.py`'s
docstring says about *why this is an empty subclass* rather than a re-export
applies here identically: Vercel's Python builder decides which `api/*.py`
files are functions by finding a class definition in the file, and a
re-exported name is not one — the whole build fails with "The pattern
"api/schedule.py" defined in `functions` doesn't match any Serverless
Functions". So: a real `class handler`, empty, inheriting the tested class in
`services/core/api/schedule.py`.
"""

from services.core.api.schedule import handler as _ScheduleHandler


class handler(_ScheduleHandler):  # noqa: N801 — Vercel requires this exact name
    pass
