"""POST /api/cycle — the hosted core's only entry point.

Request body::

    {"cycle_id": "<uuid>", "token": "<CORE_TOKEN>", "phase": "resolve"}
    {"cycle_id": "<uuid>", "token": "<CORE_TOKEN>", "phase": "decide"}
    {"cycle_id": "<uuid>", "token": "<CORE_TOKEN>", "phase": "finalize"}

Response, on any phase, is the `edge_cycle_output` row this call produced —
`{"cycle_id", "status", ...}`. `status` is `resolved`, `decided`, `finalized`,
`noop` or `failed`; a `failed` body carries the exception's own sentence in
`detail`.

**POST only, and no GET twin.** The routine's own sandbox can only reach a
Vercel URL through the Vercel MCP server's `web_fetch_vercel_url` tool, which
takes a URL and nothing else. This endpoint is deliberately not reachable that
way: the caller is Supabase, via `pg_net` from `edge_cycle_run`, server-side.
That is not a limitation being worked around — it is the design. A session that
could call this function directly could call it with a `cycle_id` it did not
claim the lock for, and the one thing the §13.1 lock has to guarantee is that
exactly one cycle is deciding at a time.

**Auth.** A `token` in the body matched against `CORE_TOKEN` with
`hmac.compare_digest`, and an unset `CORE_TOKEN` is a 500 rather than an open
door. In the body rather than the query string because the only caller posts
JSON anyway, which keeps the token out of Vercel's request logs.

**This function never places an order and holds no broker credential.** It
reads Supabase, runs `src/`, writes Supabase. Robinhood stays on the session's
side of the line, which is where an earlier finding left it.
"""

from __future__ import annotations

import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

# The deployment root is the repository root (see the root `vercel.json`), so
# `src/`, `config/` and `data/` are all importable/readable exactly as they are
# in a checkout. This insert makes the same true when the file is imported
# directly by a test.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.core.core.run import run  # noqa: E402
from services.core.core.supabase import Supabase, UrllibTransport  # noqa: E402

MAX_BODY_BYTES = 64 * 1024


class handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            self._respond(400, {"error": "bad Content-Length"})
            return
        if length > MAX_BODY_BYTES:
            self._respond(413, {"error": "body too large"})
            return

        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            self._respond(400, {"error": str(exc)})
            return

        auth_error = self._check_auth(body.get("token"))
        if auth_error is not None:
            self._respond(auth_error[0], {"error": auth_error[1]})
            return

        cycle_id = body.get("cycle_id")
        phase = body.get("phase") or "decide"
        if not cycle_id or not isinstance(cycle_id, str):
            self._respond(400, {"error": "cycle_id is required"})
            return

        try:
            result = run(cycle_id, phase, Supabase.from_env(UrllibTransport()))
        except Exception as exc:  # noqa: BLE001 — see the module docstring
            # Reaching here means `run` could not even record its own failure
            # (no Supabase, bad phase). There is no plan in the response, so
            # the runner places nothing, which is the correct outcome.
            self._respond(500, {"cycle_id": cycle_id, "status": "failed",
                                "error": f"{type(exc).__name__}: {exc}"})
            return

        status = 200 if result.get("status") != "failed" else 500
        self._respond(status, result)

    def do_GET(self) -> None:
        # Deliberate: see the module docstring.
        self._respond(405, {"error": "POST only; this endpoint is called by pg_net"})

    @staticmethod
    def _check_auth(token) -> tuple[int, str] | None:
        expected = os.environ.get("CORE_TOKEN")
        if not expected:
            return 500, "CORE_TOKEN is not configured on this deployment"
        if not token or not hmac.compare_digest(str(token), expected):
            return 401, "invalid or missing token"
        return None

    def _respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # pragma: no cover
        # The default handler writes every request line to stderr, which on
        # Vercel means the cycle id lands in the function log on every call.
        # Nothing reads it and it is account-adjacent; drop it.
        return
