"""Shared plumbing for the three fetchers — sessions, HTTP, and atomic writes.

Three things every fetcher needs and none of them should own privately:

* **What "sessions ago" means.** `plays.py` gates `CATALYST` and `MACRO` on a
  `sessions_ago` integer and computes it from nothing — it arrives in the
  payload already counted. CI is what counts it, so the count has to agree with
  `cadence.trading_day`'s 20:00 ET shift or an 8-K filed at 21:00 Monday is
  measured against the wrong day.
* **A User-Agent that identifies us.** SEC blocks anonymous traffic outright
  (403 plus a ~10 minute IP block, measured in Phase 0). It is a courtesy
  everywhere else and a hard requirement there, so it is not optional anywhere.
* **Writes that cannot half-land.** These files are read by a live trading
  cycle. A fetcher killed by the job timeout mid-`write()` would leave truncated
  JSON in the checkout, and `json.loads` failing inside the routine is a cycle
  that does not run.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from src import cadence

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
CONFIG_DIR = REPO_ROOT / "config"

# SEC's stated policy is a UA carrying a contact address. The operator's address
# is the honest one to send: if a fetcher misbehaves, SEC should be able to
# reach the person who can stop it, not a mailbox nobody reads.
CONTACT = os.environ.get("EDGAR_CONTACT", "you@example.com")
USER_AGENT = f"deterministic-trader/1.0 ({CONTACT})"

# SEC allows 10 req/s. Half of that costs nothing here — the fetchers make tens
# of requests, not thousands — and leaves headroom for the shared runner IP.
SEC_MIN_INTERVAL = 0.2

_last_request_at = 0.0


def calendar() -> dict:
    """The operator-maintained NYSE calendar, read from the checkout.

    The same file `cli.py` loads, for the same reason: a session count that used
    a different holiday table than the cycle reading it would be wrong in
    exactly the weeks that matter most.
    """
    return json.loads((CONFIG_DIR / "market_holidays.json").read_text(encoding="utf-8"))


def now_et() -> datetime:
    return datetime.now(timezone.utc).astimezone(cadence.ET)


def current_trading_day() -> date:
    """The session this run's output will be read against."""
    return cadence.trading_day(now_et())


def sessions_between(earlier: date, later: date, cal: Mapping[str, Any]) -> int:
    """Trading sessions from ``earlier`` to ``later``, counting neither twice.

    Same day is 0, the previous session is 1. Non-trading days contribute
    nothing, so a Friday event read on Monday is 1 session old rather than 3
    days old — which is the unit `plays.py`'s two-session windows are written
    in. A future date (a filing stamped after this run's trading day, which
    EDGAR does emit around the 20:00 boundary) clamps to 0 rather than going
    negative, because "not yet" and "just now" gate identically and a negative
    would silently pass every `> window` test.
    """
    if earlier >= later:
        return 0

    sessions = 0
    day = earlier
    while day < later:
        day += timedelta(days=1)
        if cadence.is_open(day, cal):
            sessions += 1
    return sessions


def fetch(url: str, *, accept: str | None = None, retries: int = 3) -> bytes:
    """One GET, rate-limited and retried, or an exception.

    Retries cover the transient half of the failure space — a 503 from SEC
    under load, a dropped connection on a shared runner. They deliberately do
    **not** cover 4xx: a 403 means the UA or the rate limit is wrong, and
    retrying it three times is how a soft block becomes a hard one.
    """
    global _last_request_at

    headers = {"User-Agent": USER_AGENT}
    if accept is not None:
        headers["Accept"] = accept

    last: Exception | None = None
    for attempt in range(retries):
        gap = SEC_MIN_INTERVAL - (time.monotonic() - _last_request_at)
        if gap > 0:
            time.sleep(gap)

        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code < 500:
                raise
            last = error
        except (urllib.error.URLError, TimeoutError) as error:
            last = error
        finally:
            _last_request_at = time.monotonic()

        time.sleep(2**attempt)

    raise RuntimeError(f"{url} failed after {retries} attempts: {last}")


def fetch_json(url: str) -> Any:
    return json.loads(fetch(url, accept="application/json"))


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write via a temporary file and one rename.

    `os.replace` is atomic on both POSIX and Windows, so a reader either sees
    the whole previous file or the whole new one. Nothing in between exists to
    be read, which is the only guarantee worth having when the reader is a
    trading cycle.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def read_json(path: Path) -> dict | None:
    """The previous run's output, or ``None`` if there is not one yet.

    A corrupt file reads as absent rather than raising. The fetchers use the
    previous file as a baseline, and refusing to run because last week's output
    is unparseable would turn one bad write into a permanently dead pipeline.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Supabase — the second publish channel
# ---------------------------------------------------------------------------
#
# The git commit each fetcher's caller makes is still the channel of record.
# This is a mirror, not a replacement — `run._payload` reads it first and
# falls back to the bundled file, but a Supabase outage here must never fail
# the fetch: the commit already landed, or is about to, regardless.
#
# **Same door `ops/control.py` and the routine's write path already use.**
# `EDGE_SUPABASE_URL`/`EDGE_SUPABASE_KEY` are the same two Actions secrets
# `control.yml` passes today; `ci`'s dependency group deliberately carries no
# `httpx` (the comment at its declaration in `pyproject.toml` says why), so
# this reaches Supabase the same way `services/core/core/supabase.py` does —
# `urllib`, two verbs, JSON in, JSON out — rather than adding a dependency.
def _rpc(function: str, payload: Mapping[str, Any]) -> Any:
    url = os.environ.get("EDGE_SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("EDGE_SUPABASE_KEY", "")
    if not url or not key:
        raise RuntimeError("EDGE_SUPABASE_URL/EDGE_SUPABASE_KEY not set")

    request = urllib.request.Request(
        f"{url}/rest/v1/rpc/{function}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        body = response.read()
    return json.loads(body) if body else None


def publish_data_file(name: str, as_of: date, payload: Mapping[str, Any]) -> None:
    """Mirror a just-written `data/*.json` file into `edge_data_files` under this same key — `snapshot`/`events`/
    `macro`, matching `services/core/core/run.py.CHECKOUT_DATA`.

    Best-effort and never fatal: a fetcher that could not reach Supabase has
    still done its job the moment the caller commits the file, and failing
    the whole CI run over the mirror would turn a degradation (the runner
    falls back to whatever was last bundled) into an outage of the thing that
    actually matters, the checked-in file.
    """
    try:
        _rpc(
            "edge_data_file_upsert",
            {"p_name": name, "p_as_of": as_of.isoformat(), "p_payload": dict(payload)},
        )
    except Exception as error:  # noqa: BLE001 — mirror only, never fatal
        print(f"edge_data_file_upsert({name}) failed, continuing: {error}", file=sys.stderr)
