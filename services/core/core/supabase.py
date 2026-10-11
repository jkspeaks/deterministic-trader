"""The service-role client. Every Supabase write the cycle prompt makes today
goes through one of these two methods.

**`rpc` and nothing else, for writes.** `db/schema.sql`'s opening
argument is that prompt-authored SQL should be as dumb as possible, and moving
the caller from a prompt to a Python function does not change the reasoning:
the shaping (seventeen text rows into a typed object, per-key value/payload
routing, the fill-quality slippage sign) is reviewed once in the migration and
versioned with the code that depends on it. This module therefore never
writes a table directly and never composes SQL — it calls the same named
functions the prompt calls, with the same arguments, in the same order.
`select` exists for the one thing there is no RPC for: reading back this
cycle's own `edge_cycle_input` and `edge_runlog` rows.

**Service role, and why that is not a downgrade.** The routine already arrives
as `service_role` (`db/schema.sql`: "the only writer is the routine,
which arrives as service_role and bypasses RLS by design"). This function is
the routine's core, running the same writes; it holds the same key, in a
Vercel environment variable rather than in an MCP connector's config. It is
the second place that key lives, which is the real cost of this migration and
is recorded as such in the design doc.

`transport` is injectable so the tests exercise `run.py`'s full write sequence
against a recorder rather than a network: the unit here is the order and
arguments of fourteen writes, and a fake that records them is the only thing
that can assert on that.

**The production transport is `urllib`, not `httpx`, and that is deliberate.**
The first deploy that reached Supabase died with `ModuleNotFoundError: No
module named 'httpx'`: Vercel's Python builder installs this project from
`pyproject.toml` (the built function carries `deterministic_trader-0.1.0.dist-info`
and `tzdata` in `_vendor`), so a root `requirements.txt` naming `httpx` was
simply never read. The fix is not to add `httpx` to `pyproject.toml`'s
`dependencies` — that file keeps `openpyxl` out of them for exactly this
reason, so the routine's own `uv sync` never installs what it must not import
— but to need no third-party HTTP client at all. `urllib.request` does this
job: two verbs, JSON in, JSON out.

`src/` still may not import `urllib` (Invariant 2, and
`tests/test_invariant_no_network.py` names it explicitly). This module is not
`src/`; it is the one place the core's rules meet a socket, which is what the
`services/core` boundary is for.
"""

from __future__ import annotations

import json
import os
from typing import Any, Mapping, Protocol


class Transport(Protocol):
    """One HTTP round trip. `httpx` satisfies this; so does the test fake."""

    def request(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> tuple[int, bytes]:
        ...


class SupabaseError(RuntimeError):
    """A non-2xx from PostgREST. Never swallowed: a write that did not land is
    the failure `run.py` turns into `status = 'failed'`, which is what makes
    the session place nothing."""


class Supabase:
    def __init__(self, url: str, service_key: str, transport: Transport) -> None:
        self._url = url.rstrip("/")
        self._key = service_key
        self._transport = transport

    @classmethod
    def from_env(cls, transport: Transport) -> Supabase:
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        # A fail-closed shape: a misconfigured deployment is an error, never a
        # silent no-write. Name the variable that is actually missing -- "both
        # must be set" sends whoever is debugging this endpoint looking at the
        # one that was already there.
        missing = [
            name
            for name, value in (
                ("SUPABASE_URL", url),
                ("SUPABASE_SERVICE_ROLE_KEY", key),
            )
            if not value
        ]
        if missing:
            raise SupabaseError(
                f"not set on this deployment: {', '.join(missing)}"
            )
        return cls(url, key, transport)

    # -- the two verbs -----------------------------------------------------

    def rpc(self, function: str, params: Mapping[str, Any] | None = None) -> Any:
        """`select <function>(...)`, by its PostgREST name. Positional order
        is irrelevant here — PostgREST binds by parameter name, which removes
        the one class of mistake a hand-written `select` in a prompt can make
        and a reviewer cannot see."""
        return self._call(
            "POST", f"/rest/v1/rpc/{function}", json.dumps(params or {}).encode("utf-8")
        )

    def select(self, table: str, query: str) -> list[dict]:
        """A read, and only of this cycle's own rows. `query` is a PostgREST
        query string built by the caller from literals and the cycle id."""
        rows = self._call("GET", f"/rest/v1/{table}?{query}", None)
        return list(rows or [])

    def stamp(
        self,
        cycle_id: str,
        step: str,
        event: str,
        detail: Mapping[str, Any] | None = None,
        source: str = "core",
    ) -> bool:
        """One step-log row (`edge_step_stamp`). Unlike `rpc`, this
        **never raises** — a stamp is telemetry, and a broken log must not stop
        a phase, change its result or fail a trade. Returns whether it landed;
        `steps.stamp` counts the failures and the finalize gate reports the gap.
        """
        try:
            self.rpc(
                "edge_step_stamp",
                {
                    "p_cycle_id": cycle_id,
                    "p_step": step,
                    "p_event": event,
                    "p_detail": dict(detail) if detail else None,
                    "p_source": source,
                },
            )
        except Exception:  # noqa: BLE001 — see the docstring
            return False
        return True

    # -- transport ---------------------------------------------------------

    def _call(self, method: str, path: str, body: bytes | None) -> Any:
        headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        status, raw = self._transport.request(method, self._url + path, headers, body)
        if status < 200 or status >= 300:
            raise SupabaseError(f"{method} {path} -> {status}: {raw[:400]!r}")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError as exc:  # a 2xx that is not JSON is still a failure
            raise SupabaseError(f"{method} {path} -> unparseable body: {exc}") from exc


class UrllibTransport:
    """The production transport: the standard library, and nothing else.

    Imported lazily so the test suite — which never touches the network — does
    not pull `urllib` into module scope at all.

    A non-2xx is an `HTTPError`, which is also a response object, so the status
    and body are read off it and handed back the same way a 200 is. `Supabase._call`
    is the one place that decides what a status means; a transport that raised
    on 404 would move that decision here.
    """

    def __init__(self, timeout: float = 20.0) -> None:
        self._timeout = timeout

    def request(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            url, data=body, headers=dict(headers), method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
