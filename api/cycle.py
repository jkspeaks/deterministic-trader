"""Vercel's filesystem entry point for the hosted core.

Vercel's Python runtime detects functions at `<root>/api/*.py`, and the core
has to deploy from the repository root so that `import src` resolves —
`config/` and `data/` ship with it for the same reason (`cli.load_config`
reads the checkout). The handler itself lives with the rest of its package in
`services/core/api/cycle.py`; this file exists so the builder finds it.

**Why this subclasses rather than re-exports.** The obvious one line — `from
services.core.api.cycle import handler` — deploys as a 404: the builder
decides which `api/*.py` files are functions by looking for a definition in
the file, and a re-exported name is not one. The whole build fails with "The
pattern "api/cycle.py" defined in `functions` doesn't match any Serverless
Functions inside the `api` directory", which names the glob and says nothing
about why the file was skipped.

So: a real `class handler`, and an empty one. Everything it does is inherited
from the tested class in `services/core/api/cycle.py`. Keep it that way —
anything added here is production code with no test covering it.
"""

from services.core.api.cycle import handler as _CoreHandler


class handler(_CoreHandler):  # noqa: N801 — Vercel requires this exact name
    pass
