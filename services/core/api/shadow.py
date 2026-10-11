"""POST /api/shadow — the shadow ledger's replay job.

Request body::

    {"token": "<CORE_TOKEN>", "days": 10, "write": true}

`days` (optional, default `shadow.DEFAULT_DAYS`) is how many trading days back
to replay; `write` (optional, default true) false runs the whole replay and
returns the trades without touching `edge_shadow_trades`, which is what a dry
run is.

Response is the job's summary: per-day status (`replayed`, or "no stored
inputs" for a day `edge_cycle_input`'s seven-day reap has already cleared),
per-variant entry and closed counts, and the number of rows written.

**POST only, same token as `/api/cycle`, `/api/premarket` and `/api/watch`.**
The fourth function in the hosted-core Vercel project; the reasoning for
POST-only and for an unset token being a 500 rather than an open door is
`api/cycle.py`'s, unchanged.

**This function holds no broker credential and places nothing.** It reads
Supabase and Twelve Data, writes one append/upsert table nothing else in the
system reads except the weekly scorecard, and returns a summary. It is also
the only endpoint here that is safe to call by hand at any hour: the replay is
a pure function of rows that already exist.
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

from services.core.core.shadow import DEFAULT_DAYS, run_shadow  # noqa: E402
from services.core.core.supabase import Supabase, UrllibTransport  # noqa: E402
from services.core.core.twelvedata import TwelveData, UrllibFetcher  # noqa: E402

MAX_BODY_BYTES = 8 * 1024

# A replay is bounded by `edge_cycle_input`'s seven-day reap either way; this
# only stops a typo'd `days` from walking the calendar for a year.
MAX_DAYS = 30


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
            days = int(body.get("days") or DEFAULT_DAYS)
        except (TypeError, ValueError):
            self._respond(400, {"error": "days must be an integer"})
            return
        if not 1 <= days <= MAX_DAYS:
            self._respond(400, {"error": f"days must be between 1 and {MAX_DAYS}"})
            return

        try:
            # No key is a degradation, not a failure: the replay still produces
            # every entry and reports each exit `NO_BARS`, which is a more
            # useful answer than a 500 for an operator checking what the
            # variants would have bought.
            try:
                # A minute between requests: Twelve Data Basic allows 8
                # credits a minute and a request of eight symbols spends all of
                # them. At the client's default 8 s every request after the
                # first came back 429 and its symbols were written `NO_BARS`.
                twelvedata = TwelveData.from_env(UrllibFetcher(), min_interval=61.0)
            except Exception:  # noqa: BLE001
                twelvedata = None
            result = run_shadow(
                Supabase.from_env(UrllibTransport()),
                twelvedata,
                days=days,
                write=body.get("write", True) is not False,
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
