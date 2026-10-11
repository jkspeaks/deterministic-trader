"""POST /api/watch — the 30-minute state-change gate's entry point.

Request body::

    {"token": "<CORE_TOKEN>"}

Response is the job's summary — `{"status": "ok", "trading_day", "needs_session",
"reasons"}` on a tick that flagged something, `{"status": "ok", "trading_day",
"needs_session": false, "checked"}` on a quiet one, or `{"status": "failed",
"detail"}`.

**POST only, no GET twin, same token as `/api/cycle` and `/api/premarket`.**
The third function in the hosted-core Vercel project, sharing that project's
`CORE_TOKEN` — one caller (Supabase `pg_net`, from the `edge-watch` pg_cron
job, every 30 min in market hours) and one secret. The reasoning for
POST-only and for an unset token being a 500 rather than an open door is
`api/cycle.py`'s, unchanged.

**This function holds no broker credential and places nothing.** It reads
Twelve Data and Supabase, writes one `edge_control` key, returns a summary.
"""

from __future__ import annotations

import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.core.core.supabase import Supabase, UrllibTransport  # noqa: E402
from services.core.core.twelvedata import TwelveData, UrllibFetcher  # noqa: E402
from services.core.core.watch import run_watch  # noqa: E402

MAX_BODY_BYTES = 8 * 1024


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

        try:
            result = run_watch(
                Supabase.from_env(UrllibTransport()),
                TwelveData.from_env(UrllibFetcher()),
            )
        except Exception as exc:  # noqa: BLE001 — could not even start
            self._respond(500, {"status": "failed",
                                "error": f"{type(exc).__name__}: {exc}"})
            return

        status = 200 if result.get("status") != "failed" else 500
        self._respond(status, result)

    def do_GET(self) -> None:
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
        return
