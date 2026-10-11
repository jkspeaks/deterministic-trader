"""POST /api/evidence — the evidence back-fill (S0).

Request body::

    {"token": "<CORE_TOKEN>", "write": true}

`write` (optional, default true) false runs the whole pass and returns what it
would have written without touching `edge_decision_snapshot`.

Response is the pass's summary: how many snapshot rows are pending, how many
were due tonight, which symbols were fetched, which had no bars, how many rows
were updated and how many Twelve Data credits it cost.

**POST only, same token as every other function in the hosted-core project**;
the reasoning is `api/cycle.py`'s, unchanged.

**This function holds no broker credential, places nothing, and nothing that
decides reads what it writes.** It fills forward returns and price-history
features onto rows `run.py` already wrote (`services/core/core/evidence.py`).
Safe to call by hand at any hour: a pass with nothing due makes no request.

**No market-data key is a degradation, not a failure**, as in `/api/shadow`:
the pass still closes rows that are past saving and reports what is owed.
The same holds for the analyst-estimate key (`ALPHAVANTAGE_API_KEY`, S2):
without it the pass fetches bars as before and asks for no estimate.
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

from services.core.core.alphavantage import AlphaVantage  # noqa: E402
from services.core.core.evidence import run_backfill  # noqa: E402
from services.core.core.supabase import Supabase, UrllibTransport  # noqa: E402
from services.core.core.twelvedata import TwelveData, UrllibFetcher  # noqa: E402

MAX_BODY_BYTES = 8 * 1024

# Twelve Data Basic: 8 credits a minute, one per symbol, so a request of eight
# spends the whole minute. `twelvedata.MIN_SECONDS_BETWEEN_CALLS` (8 s) is
# right for the pre-market job, which sends one request per invocation; this
# job sends up to four, and has to wait the minute itself.
SECONDS_BETWEEN_REQUESTS = 61.0


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
            try:
                twelvedata = TwelveData.from_env(
                    UrllibFetcher(), min_interval=SECONDS_BETWEEN_REQUESTS
                )
            except Exception:  # noqa: BLE001 — see the module docstring
                twelvedata = None
            try:
                estimator = AlphaVantage.from_env(UrllibFetcher())
            except Exception:  # noqa: BLE001 — no key: no estimates, nothing else changes
                estimator = None
            result = run_backfill(
                Supabase.from_env(UrllibTransport()),
                twelvedata,
                estimator=estimator,
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
