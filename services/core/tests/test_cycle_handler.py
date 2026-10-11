"""`api/cycle.py`'s HTTP surface, run for real against a local `HTTPServer`.

The handler is the auth boundary of a public URL that can move money by
returning a plan, so the thing under test is the boundary itself, not a
function called behind it. `run` is stubbed out — what happens after the token
is checked has its own file.
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.core.api import cycle as endpoint  # noqa: E402

TOKEN = "test-core-token"


@pytest.fixture()
def server(monkeypatch):
    monkeypatch.setenv("CORE_TOKEN", TOKEN)
    monkeypatch.setattr(
        endpoint,
        "run",
        lambda cycle_id, phase, supabase: {
            "cycle_id": cycle_id,
            "status": "decided",
            "phase": phase,
            "plan": [],
        },
    )
    monkeypatch.setattr(endpoint.Supabase, "from_env", classmethod(lambda cls, t: None))

    srv = HTTPServer(("127.0.0.1", 0), endpoint.handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}/api/cycle"
    finally:
        srv.shutdown()
        thread.join()


def post(url, payload):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def get(url):
    try:
        with urllib.request.urlopen(url) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_a_good_token_runs_the_cycle(server):
    status, body = post(server, {"cycle_id": "c-1", "token": TOKEN, "phase": "decide"})

    assert status == 200
    assert body["cycle_id"] == "c-1"
    assert body["status"] == "decided"


def test_phase_defaults_to_decide(server):
    _, body = post(server, {"cycle_id": "c-1", "token": TOKEN})

    assert body["phase"] == "decide"


def test_a_wrong_token_gets_nothing(server):
    status, body = post(server, {"cycle_id": "c-1", "token": "guess", "phase": "decide"})

    assert status == 401
    assert "plan" not in body


def test_a_missing_token_gets_nothing(server):
    status, _ = post(server, {"cycle_id": "c-1"})

    assert status == 401


def test_an_unconfigured_deployment_fails_closed_rather_than_open(monkeypatch, server):
    monkeypatch.delenv("CORE_TOKEN", raising=False)

    status, body = post(server, {"cycle_id": "c-1", "token": TOKEN})

    assert status == 500
    assert "CORE_TOKEN" in body["error"]


def test_a_missing_cycle_id_is_refused_after_auth(server):
    status, body = post(server, {"token": TOKEN})

    assert status == 400
    assert "cycle_id" in body["error"]


def test_a_malformed_body_is_a_400_not_a_crash(server):
    request = urllib.request.Request(
        server, data=b"{not json", headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code

    assert status == 400


def test_there_is_no_get_twin(server):
    # Deliberate. The only caller is pg_net, server-side; a GET would make the
    # endpoint reachable from a session through `web_fetch_vercel_url`, and a
    # session could then run a cycle it never claimed the lock for.
    status, _ = get(server + "?cycle_id=c-1&token=" + TOKEN)

    assert status == 405


def test_a_failed_run_is_a_500_so_pg_net_records_it_as_one(server, monkeypatch):
    monkeypatch.setattr(
        endpoint,
        "run",
        lambda cycle_id, phase, supabase: {
            "cycle_id": cycle_id,
            "status": "failed",
            "detail": "boom",
        },
    )

    status, body = post(server, {"cycle_id": "c-1", "token": TOKEN})

    assert status == 500
    assert body["status"] == "failed"


def test_a_missing_env_var_is_named_rather_than_both_being_listed(monkeypatch):
    from services.core.core.supabase import Supabase, SupabaseError

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)

    with pytest.raises(SupabaseError) as exc:
        Supabase.from_env(object())

    assert "SUPABASE_SERVICE_ROLE_KEY" in str(exc.value)
    assert "SUPABASE_URL" not in str(exc.value), (
        "naming the variable that IS set sends the reader to the wrong place"
    )
