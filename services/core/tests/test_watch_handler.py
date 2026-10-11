"""`api/watch.py`'s HTTP surface, run for real against a local `HTTPServer`.

Same pattern as `test_premarket_handler.py`: the handler is the auth boundary
of a public URL, so the boundary is the unit. `run_watch` and the two clients
are stubbed — what happens after the token check has its own file
(`test_watch.py`).
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

from services.core.api import watch as endpoint  # noqa: E402

TOKEN = "test-core-token"


@pytest.fixture()
def server(monkeypatch):
    monkeypatch.setenv("CORE_TOKEN", TOKEN)
    monkeypatch.setattr(
        endpoint, "run_watch",
        lambda supabase, twelvedata: {"status": "ok", "trading_day": "2026-09-11",
                                       "needs_session": False, "checked": 3},
    )
    monkeypatch.setattr(endpoint.Supabase, "from_env", classmethod(lambda cls, t: None))
    monkeypatch.setattr(endpoint.TwelveData, "from_env", classmethod(lambda cls, f: None))

    srv = HTTPServer(("127.0.0.1", 0), endpoint.handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}/api/watch"
    finally:
        srv.shutdown()
        thread.join()


def _post(url, payload):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _get(url):
    try:
        with urllib.request.urlopen(url) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_a_valid_token_runs_the_job(server):
    status, body = _post(server, {"token": TOKEN})
    assert status == 200
    assert body["status"] == "ok" and body["needs_session"] is False


def test_a_missing_or_wrong_token_is_401(server):
    assert _post(server, {})[0] == 401
    assert _post(server, {"token": "nope"})[0] == 401


def test_get_is_405(server):
    assert _get(server)[0] == 405


def test_an_unset_core_token_is_500_not_an_open_door(server, monkeypatch):
    monkeypatch.delenv("CORE_TOKEN", raising=False)
    status, body = _post(server, {"token": TOKEN})
    assert status == 500
    assert "CORE_TOKEN" in body["error"]
