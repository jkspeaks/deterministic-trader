"""The Alpha Vantage client the evidence job fetches analyst estimates through.

S2 factor 12: the analyst-estimate revision. Twelve Data's estimate endpoints
need a plan
the account does not have; Alpha Vantage's `EARNINGS_ESTIMATES` answers on the
free key and carries the same figures in one request a name: the consensus now
and 7, 30, 60 and 90 days ago, and the revisions up and down.

The third "the core's rules meet a socket" module, and it follows the other
two (`supabase.py`, `twelvedata.py`): standard library only, an injectable
fetcher so the tests never touch the network, and `src/` never imports it.

## The free key, and why the walk is shaped the way it is

**25 requests a day, and no more than one a second** — both confirmed; the
vendor's own words are in the test fixture. So:

* requests are spaced `MIN_SECONDS_BETWEEN_CALLS` apart;
* a pass asks about at most `max_requests` names and stops when its
  `budget_seconds` are spent, so a slow vendor cannot run the function into
  its timeout behind the bars;
* **the vendor's throttle message stops the walk.** It is the same message
  for "too fast" and for "the day's 25 are gone", and neither is an answer
  about the name: the names not reached are asked about another night.

Every answer is one of three kinds, and the caller treats them differently:

* `rows[symbol]` — the estimates;
* `failed[symbol]` with `kind` `SYMBOL` — the vendor has nothing for this
  name. That is the name's answer;
* `failed[symbol]` with `kind` `REFUSED` (the key itself is not accepted, or
  the endpoint has moved behind a paid plan) or `LATER` (throttled, or the
  request did not come back). Both stop the walk.

This module holds no broker credential and nothing that decides reads what it
fetches.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
from typing import Any, Callable, Mapping, Sequence

from .twelvedata import Fetcher

BASE_URL = "https://www.alphavantage.co/query"
FUNCTION = "EARNINGS_ESTIMATES"
ENV_KEY = "ALPHAVANTAGE_API_KEY"
SOURCE = "alphavantage"

MIN_SECONDS_BETWEEN_CALLS = 1.5
# One pass's ceiling. The day's is the vendor's 25; a night has six passes
# and nothing here counts across them, because nothing needs to: the 26th
# request is refused with the throttle message and the walk stops.
MAX_REQUESTS_PER_PASS = 20
BUDGET_SECONDS = 60.0

SYMBOL = "symbol"        # the vendor has nothing for this name
REFUSED = "refused"      # the key, or the plan, is not accepted
LATER = "later"          # throttled, or no usable response


class AlphaVantageError(RuntimeError):
    """A deployment with no key. Raised by `from_env` only."""


def classify(payload: Any) -> tuple[str, str] | None:
    """`(kind, message)` for a response that is not estimates, else None.

    The vendor answers 200 for everything and says what went wrong in prose,
    under `Information`, `Note` or `Error Message`. An empty object is what an
    unknown symbol gets.
    """
    if not isinstance(payload, Mapping):
        return LATER, "response was not a JSON object"
    if isinstance(payload.get("estimates"), list):
        return None
    if not payload:
        return SYMBOL, "the vendor returned nothing for this symbol"
    text = str(payload.get("Information") or payload.get("Note")
               or payload.get("Error Message") or "")[:300]
    lowered = text.lower()
    if "error message" in {str(k).lower() for k in payload}:
        # "Invalid API call": a symbol or a parameter the vendor will not take.
        return (REFUSED if "apikey" in lowered else SYMBOL), text
    if "premium endpoint" in lowered or "invalid" in lowered and "apikey" in lowered:
        return REFUSED, text
    if "rate limit" in lowered or "requests per day" in lowered or "per second" in lowered \
            or "sparingly" in lowered or "frequency" in lowered:
        return LATER, text
    return LATER, text or "no estimates in the response"


class AlphaVantage:
    def __init__(
        self,
        api_key: str,
        fetcher: Fetcher,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        min_interval: float = MIN_SECONDS_BETWEEN_CALLS,
        max_requests: int = MAX_REQUESTS_PER_PASS,
        budget_seconds: float = BUDGET_SECONDS,
    ) -> None:
        self._key = api_key
        self._fetcher = fetcher
        self._sleep = sleep
        self._clock = clock
        self._min_interval = min_interval
        self._max_requests = max_requests
        self._budget = budget_seconds
        self.requests = 0
        self.errors: list[str] = []

    @classmethod
    def from_env(cls, fetcher: Fetcher, **kw: Any) -> AlphaVantage:
        key = os.environ.get(ENV_KEY)
        if not key:
            raise AlphaVantageError(f"{ENV_KEY} is not set on this deployment")
        return cls(key, fetcher, **kw)

    def estimates(
        self, symbols: Sequence[str]
    ) -> tuple[dict[str, list[dict]], dict[str, dict]]:
        """`(rows, failed)`: `{symbol: [estimate row, ...]}` as the vendor
        sent them, and `{symbol: {"kind", "message"}}`. A symbol the walk
        never reached is in neither."""
        rows: dict[str, list[dict]] = {}
        failed: dict[str, dict] = {}
        started = self._clock()
        for symbol in dict.fromkeys(symbols):
            if self.requests >= self._max_requests or self._clock() - started > self._budget:
                break
            if self.requests > 0:
                self._sleep(self._min_interval)
            self.requests += 1
            payload = self._get(symbol)
            problem = classify(payload)
            if problem is None:
                rows[symbol] = [dict(r) for r in payload["estimates"] if isinstance(r, Mapping)]
                continue
            kind, message = problem
            failed[symbol] = {"kind": kind, "message": message}
            self.errors.append(f"{FUNCTION} {symbol}: {kind}: {message[:160]}")
            if kind != SYMBOL:
                break
        return rows, failed

    def _get(self, symbol: str) -> Any:
        query = urllib.parse.urlencode(
            {"function": FUNCTION, "symbol": symbol, "apikey": self._key})
        try:
            status, body = self._fetcher.get(f"{BASE_URL}?{query}")
        except Exception as exc:  # noqa: BLE001 — a fetch that threw is "later"
            return {"Note": f"{type(exc).__name__}: {exc}"}
        if status < 200 or status >= 300:
            return {"Note": f"HTTP {status}"}
        try:
            return json.loads(body or b"{}")
        except ValueError as exc:
            return {"Note": f"bad JSON: {exc}"}
