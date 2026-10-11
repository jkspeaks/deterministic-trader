"""The Twelve Data client the pre-market job fetches through.

`services/core` already has one "the core's rules meet a socket" module
(`supabase.py`); this is the second, and it follows the same three rules:

* **Standard library only.** `urllib.request`, no `httpx`. The Vercel build
  installs this project from `pyproject.toml` and an `httpx` named only in a
  requirements file was never installed (`supabase.py`'s docstring has the
  full story). One GET verb, JSON out.
* **`transport` is injectable** so the tests drive the whole batching /
  rate-limit / credit-budget sequence against recorded JSON rather than the
  network — the same test pattern used elsewhere in this service.
* **`src/` never imports this.** The pure regime and PEAD arithmetic lives in
  `src/regime.py` and `src/pead_reaction.py`; this module only *fetches* the
  numbers those two consume.

## The rate limit, and why the job is paced the way it is

Twelve Data Basic throttles at **8 API credits per minute**, 800/day, one credit per symbol in a
batched request — confirmed by the vendor's own 429 body ("15 API credits were used, with the
current limit being 8"). That is *credits*, not requests: a single batch of `MAX_SYMBOLS_PER_CALL`
(8) symbols already spends the whole minute's budget.

**In production this class is only ever asked for one batch per instance.**
`services/core/core/premarket.py` is a small state machine that persists
across many separate Vercel invocations (`edge_premarket_state`)
and executes exactly one Twelve Data request per invocation — each
invocation constructs a fresh `TwelveData`, so `self._calls` is always 0 at
the start and the sleep below never fires. The real pacing is the two
minutes between pg_cron ticks, not anything inside this module.

The batching/pacing/budget machinery below still exists and is still tested,
because this class has to be correct for *any* caller, including a test or a
future one that hands it more than eight symbols at once:

* batching at most `MAX_SYMBOLS_PER_CALL` (8) symbols per request,
* sleeping `MIN_SECONDS_BETWEEN_CALLS` (8 s) between requests **within one
  call to `time_series`/`sma`** — inert in production, exercised in tests,
* refusing to start a request that would push a single call past
  `credit_cap` (300 per call; a pre-market tick sends at most 8) — belt-and-braces; the authoritative cap is
  `premarket.DAILY_CREDIT_CAP`, checked once against the whole day's plan
  before any request is sent.

A per-symbol vendor error (`{"status": "error", ...}` inside a batch, or a
non-2xx for a whole request) is recorded on `.errors` and that symbol is left
out of the result — a partial pre-market context is still worth writing.
Exceeding `credit_cap` is the one hard stop: it raises `CreditCapExceeded`,
because a run that would blow the daily budget must not also blow tomorrow's.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
from typing import Any, Callable, Mapping, Protocol, Sequence

BASE_URL = "https://api.twelvedata.com"
INTERVAL = "1day"

MAX_SYMBOLS_PER_CALL = 8
MIN_SECONDS_BETWEEN_CALLS = 8.0
DEFAULT_CREDIT_CAP = 300

# `time_series` is fetched at this depth: enough to place any PEAD report date
# (1-3 sessions back) and to derive a 20- and 50-day SMA and a 20-day realized
# vol locally. The 200-day SMA cannot come from 60 bars and is the one figure
# fetched from the `/sma` endpoint instead (`premarket.SMA_PERIODS_FROM_TD`).
BAR_COUNT = 60

# The analyst-estimate endpoints and the key each one's rows arrive under
# (`/eps_revisions` answers with `eps_revision`). One symbol a request.
ANALYST_ROWS = {"eps_trend": "eps_trend", "eps_revisions": "eps_revision"}
# Vendor codes that mean "ask again later", as opposed to "this is the answer".
TRANSIENT_CODES = frozenset({None, 429})
# Vendor codes that refuse the key, not the symbol: the plan does not include
# the endpoint, or the key is not accepted. The next symbol would get the same
# answer, so there is no point asking it. The hosted core's key gets 403
# "/eps_trend is available exclusively with ultra or enterprise plans".
PLAN_CODES = frozenset({401, 403})


class Fetcher(Protocol):
    """One HTTP GET. The production impl is `UrllibFetcher`; the tests pass a
    recorder that maps a URL to a canned `(status, body)`."""

    def get(self, url: str) -> tuple[int, bytes]:
        ...


class TwelveDataError(RuntimeError):
    """A request that did not come back usable. Carried on `.errors`, not
    raised — except where noted."""


class CreditCapExceeded(RuntimeError):
    """The run tried to spend past `credit_cap`. Always raised: the daily
    budget is shared with tomorrow."""


class _Budget:
    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.used = 0

    def charge(self, credits: int) -> None:
        if self.used + credits > self.cap:
            raise CreditCapExceeded(
                f"run would spend {self.used + credits} credits, cap is {self.cap}"
            )
        self.used += credits


def _batches(symbols: Sequence[str], size: int) -> list[list[str]]:
    ordered = list(dict.fromkeys(symbols))  # de-dup, order-preserving
    return [ordered[i : i + size] for i in range(0, len(ordered), size)]


def _rows(block: Mapping[str, Any]) -> list[dict]:
    """A Twelve Data `values` array, oldest-first, or `[]` for an error block."""
    if not isinstance(block, Mapping):
        return []
    if str(block.get("status")) == "error":
        return []
    values = block.get("values")
    if not isinstance(values, list):
        return []
    # `order=ASC` is requested, but a vendor that ignores it must not flip the
    # series silently — sort here too.
    rows = [v for v in values if isinstance(v, Mapping) and v.get("datetime")]
    rows.sort(key=lambda v: str(v["datetime"]))
    return rows


class TwelveData:
    def __init__(
        self,
        api_key: str,
        fetcher: Fetcher,
        *,
        credit_cap: int = DEFAULT_CREDIT_CAP,
        sleep: Callable[[float], None] = time.sleep,
        min_interval: float = MIN_SECONDS_BETWEEN_CALLS,
    ) -> None:
        self._key = api_key
        self._fetcher = fetcher
        self._sleep = sleep
        self._min_interval = min_interval
        self.budget = _Budget(credit_cap)
        self.errors: list[str] = []
        self._calls = 0

    @classmethod
    def from_env(cls, fetcher: Fetcher, **kw: Any) -> TwelveData:
        key = os.environ.get("TWELVE_DATA_API_KEY")
        if not key:
            # Same fail-closed shape as `Supabase.from_env`: a misconfigured
            # deployment is an error, never a silent empty fetch.
            raise TwelveDataError("TWELVE_DATA_API_KEY is not set on this deployment")
        return cls(key, fetcher, **kw)

    # -- public surface --------------------------------------------------

    def time_series(
        self, symbols: Sequence[str], outputsize: int = BAR_COUNT
    ) -> dict[str, list[dict]]:
        """`{symbol: [bar, ...]}`, each bar `{"datetime", "open", ..., "close"}`
        oldest-first. A symbol whose request or block errored is absent."""
        out: dict[str, list[dict]] = {}
        for batch in _batches(symbols, MAX_SYMBOLS_PER_CALL):
            block = self._request(
                "time_series",
                batch,
                {"outputsize": str(outputsize), "order": "ASC"},
            )
            for symbol in batch:
                rows = _rows(self._for(block, symbol, batch))
                if rows:
                    out[symbol] = rows
        return out

    def price(self, symbols: Sequence[str]) -> dict[str, float]:
        """`{symbol: last price}`, one credit per symbol via `/price` — the
        cheapest quote endpoint, and all the state-change gate
        (`services/core/core/watch.py`) needs: it compares a level, not a
        full quote. A symbol whose block errored or carried no parseable
        `price` is absent."""
        out: dict[str, float] = {}
        for batch in _batches(symbols, MAX_SYMBOLS_PER_CALL):
            block = self._request("price", batch, {})
            for symbol in batch:
                candidate = self._for(block, symbol, batch)
                value = candidate.get("price")
                try:
                    if value is not None:
                        out[symbol] = float(value)
                except (TypeError, ValueError):
                    self.errors.append(f"price {symbol}: unparseable {value!r}")
        return out

    def sma(self, symbols: Sequence[str], period: int) -> dict[str, float]:
        """`{symbol: sma}` — the single most recent value of the `time_period`
        SMA. A symbol whose block errored or carried no value is absent."""
        out: dict[str, float] = {}
        for batch in _batches(symbols, MAX_SYMBOLS_PER_CALL):
            block = self._request(
                "sma",
                batch,
                {"time_period": str(period), "outputsize": "1"},
            )
            for symbol in batch:
                rows = _rows(self._for(block, symbol, batch))
                value = rows[-1].get("sma") if rows else None
                try:
                    if value is not None:
                        out[symbol] = float(value)
                except (TypeError, ValueError):
                    self.errors.append(f"sma {period} {symbol}: unparseable {value!r}")
        return out

    def analyst(
        self, symbols: Sequence[str], endpoints: Sequence[str] = ("eps_trend", "eps_revisions")
    ) -> tuple[dict[str, dict[str, list[dict]]], dict[str, dict]]:
        """`(rows, failed)` for the analyst-estimate endpoints (S2, factor 12).

        ``rows`` is `{symbol: {endpoint: [row, ...]}}` for every symbol all of
        whose endpoints answered; ``failed`` is `{symbol: {"endpoint", "code",
        "message"}}` for the rest. These endpoints take one symbol a request,
        so the pacing is by credits rather than by call: requests go out back
        to back until the next symbol would pass `MAX_SYMBOLS_PER_CALL`
        credits, then the client waits its interval.

        **A rate limit or a transport failure stops the walk.** ``code`` is
        then 429 or None, the symbols after it are simply not in either map,
        and the caller tries them another night. **So does a refusal of the
        key** (`PLAN_CODES`): one request is enough to learn that the plan
        lacks the endpoint. Any other vendor error (a symbol it does not
        cover) is that symbol's answer, and the walk goes on.
        """
        rows: dict[str, dict[str, list[dict]]] = {}
        failed: dict[str, dict] = {}
        spent = MAX_SYMBOLS_PER_CALL          # so the first symbol opens a group
        for symbol in dict.fromkeys(symbols):
            fresh = spent + len(endpoints) > MAX_SYMBOLS_PER_CALL
            if fresh:
                spent = 0
            got: dict[str, list[dict]] = {}
            for n, endpoint in enumerate(endpoints):
                status, block = self._get(endpoint, [symbol], {}, pace=fresh and n == 0,
                                          interval=False)
                spent += 1
                values = block.get(ANALYST_ROWS.get(endpoint, endpoint)) if isinstance(block, Mapping) else None
                if status is not None and 200 <= status < 300 and isinstance(values, list):
                    got[endpoint] = [dict(v) for v in values if isinstance(v, Mapping)]
                    continue
                code = block.get("code") if isinstance(block, Mapping) else None
                if code is None and status is not None and not 200 <= status < 300:
                    code = status
                message = block.get("message") if isinstance(block, Mapping) else None
                failed[symbol] = {"endpoint": endpoint, "code": code,
                                  "message": str(message or "no usable response")[:200]}
                if status is not None and 200 <= status < 300:
                    # `_get` has already recorded a transport or HTTP failure.
                    self.errors.append(f"{endpoint} {symbol}: {code} {failed[symbol]['message']}")
                break
            else:
                rows[symbol] = got
                continue
            code = failed[symbol]["code"]
            if code in TRANSIENT_CODES or code in PLAN_CODES or (code or 0) >= 500:
                break
        return rows, failed

    # -- transport -----------------------------------------------------

    def _request(
        self, endpoint: str, symbols: Sequence[str], params: Mapping[str, str]
    ) -> Mapping[str, Any]:
        """One paced, budgeted GET. Returns the decoded JSON (a flat block for
        a one-symbol batch, a `{symbol: block}` map otherwise), or `{}` on a
        transport / decode failure — recorded on `.errors`."""
        status, payload = self._get(endpoint, symbols, params)
        return payload if status is not None and 200 <= status < 300 else {}

    def _get(
        self, endpoint: str, symbols: Sequence[str], params: Mapping[str, str],
        *, pace: bool = True, interval: bool = True,
    ) -> tuple[int | None, Mapping[str, Any]]:
        """`(HTTP status, decoded JSON)`; the status is None when the fetch
        itself threw. A non-2xx keeps whatever JSON the vendor sent with it,
        so a caller that needs the vendor's own error code can read it.
        ``pace`` false skips the wait before the request: the caller has
        already counted this request into the current minute's credits."""
        self.budget.charge(len(symbols))
        if self._calls > 0 and pace:
            self._sleep(self._min_interval)
        self._calls += 1

        query = urllib.parse.urlencode(
            {
                "symbol": ",".join(symbols),
                **({"interval": INTERVAL} if interval else {}),
                "apikey": self._key,
                "dp": "6",
                **params,
            },
            safe=",",  # keep the batched symbol list readable in request logs
        )
        url = f"{BASE_URL}/{endpoint}?{query}"
        try:
            status, body = self._fetcher.get(url)
        except Exception as exc:  # noqa: BLE001 — a fetch that threw is an error row
            self.errors.append(f"{endpoint} {symbols}: {type(exc).__name__}: {exc}")
            return None, {}
        if status < 200 or status >= 300:
            self.errors.append(f"{endpoint} {symbols}: HTTP {status}: {body[:200]!r}")
            try:
                payload = json.loads(body or b"{}")
            except ValueError:
                payload = {}
            return status, payload if isinstance(payload, Mapping) else {}
        try:
            return status, json.loads(body or b"{}")
        except ValueError as exc:
            self.errors.append(f"{endpoint} {symbols}: bad JSON: {exc}")
            return status, {}

    def _for(
        self, block: Mapping[str, Any], symbol: str, batch: Sequence[str]
    ) -> Mapping[str, Any]:
        """Pull one symbol's sub-block out of a response. Twelve Data returns a
        flat object for a single-symbol request and a `{symbol: {...}}` map for
        a multi-symbol one; this normalises both."""
        if len(batch) == 1:
            candidate = block
        else:
            candidate = block.get(symbol) if isinstance(block, Mapping) else None
        if not isinstance(candidate, Mapping):
            return {}
        if str(candidate.get("status")) == "error":
            self.errors.append(
                f"{symbol}: {candidate.get('code')} {candidate.get('message')}"
            )
            return {}
        return candidate


class UrllibFetcher:
    """The production `Fetcher`: standard library, imported lazily so the test
    suite never pulls `urllib` into module scope."""

    def __init__(self, timeout: float = 15.0) -> None:
        self._timeout = timeout

    def get(self, url: str) -> tuple[int, bytes]:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
