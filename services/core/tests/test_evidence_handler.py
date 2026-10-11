"""`api/evidence.py`'s HTTP surface, run for real against a local `HTTPServer`.

Same pattern as `test_watch_handler.py`: the handler is the auth boundary of a
public URL, so the boundary is the unit. `run_backfill` and the two clients
are stubbed — what happens after the token check is `test_evidence.py`'s.
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

from services.core.api import evidence as endpoint  # noqa: E402

TOKEN = "test-core-token"


@pytest.fixture()
def server(monkeypatch):
    monkeypatch.setenv("CORE_TOKEN", TOKEN)
    seen = {}

    def fake(supabase, twelvedata, estimator=None, write=True):
        seen["write"] = write
        seen["twelvedata"] = twelvedata
        seen["estimator"] = estimator
        return {"status": "ok", "pending": 4, "due": 2, "written": 2}

    monkeypatch.setattr(endpoint, "run_backfill", fake)
    monkeypatch.setattr(endpoint.Supabase, "from_env", classmethod(lambda cls, t: None))
    monkeypatch.setattr(
        endpoint.TwelveData, "from_env",
        classmethod(lambda cls, f, **kw: ("client", kw)),
    )
    monkeypatch.setattr(
        endpoint.AlphaVantage, "from_env",
        classmethod(lambda cls, f, **kw: "estimator"),
    )
    monkeypatch.setattr(endpoint.handler, "seen", seen, raising=False)

    srv = HTTPServer(("127.0.0.1", 0), endpoint.handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}/api/evidence"
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
    assert body["status"] == "ok" and body["written"] == 2
    assert endpoint.handler.seen["write"] is True


def test_the_client_is_paced_at_one_request_a_minute(server):
    """Twelve Data Basic allows 8 credits a minute and a request of eight
    symbols spends all of them; the client's 8-second default would 429 every
    request of a pass after the first."""
    _post(server, {"token": TOKEN})

    _, kwargs = endpoint.handler.seen["twelvedata"]
    assert kwargs == {"min_interval": 61.0}
    assert endpoint.SECONDS_BETWEEN_REQUESTS >= 60.0


def test_write_false_is_a_dry_run(server):
    _post(server, {"token": TOKEN, "write": False})
    assert endpoint.handler.seen["write"] is False


def test_no_market_data_key_still_runs(server, monkeypatch):
    def missing(cls, fetcher, **kw):
        raise RuntimeError("TWELVE_DATA_API_KEY is not set on this deployment")

    monkeypatch.setattr(endpoint.TwelveData, "from_env", classmethod(missing))
    status, body = _post(server, {"token": TOKEN})

    assert status == 200 and body["status"] == "ok"
    assert endpoint.handler.seen["twelvedata"] is None


def test_the_estimate_client_is_handed_to_the_job_when_its_key_is_set(server):
    _post(server, {"token": TOKEN})

    assert endpoint.handler.seen["estimator"] == "estimator"


def test_no_estimate_key_still_runs_and_the_bars_are_unaffected(server, monkeypatch):
    def missing(cls, fetcher, **kw):
        raise RuntimeError("ALPHAVANTAGE_API_KEY is not set on this deployment")

    monkeypatch.setattr(endpoint.AlphaVantage, "from_env", classmethod(missing))
    status, body = _post(server, {"token": TOKEN})

    assert status == 200 and body["status"] == "ok"
    assert endpoint.handler.seen["estimator"] is None
    assert endpoint.handler.seen["twelvedata"] is not None


def test_a_failed_pass_is_a_500(server, monkeypatch):
    monkeypatch.setattr(endpoint, "run_backfill",
                        lambda supabase, twelvedata, write=True: {"status": "failed", "detail": "x"})
    assert _post(server, {"token": TOKEN})[0] == 500


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


def test_vercel_knows_the_function_and_gives_it_the_passes_five_minutes():
    import json as _json

    config = _json.loads((REPO_ROOT / "vercel.json").read_text(encoding="utf-8"))
    assert config["functions"]["api/evidence.py"]["maxDuration"] == 300
    assert (REPO_ROOT / "api" / "evidence.py").read_text(encoding="utf-8").count("class handler(") == 1
