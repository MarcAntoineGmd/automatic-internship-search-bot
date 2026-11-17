"""Async HTTP helper: timeouts, retries with exponential backoff, Retry-After, ETag caching."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Awaitable, Callable

import httpx

log = logging.getLogger(__name__)

RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}
# Every Workday tenant (RBC, TD, NVIDIA, …) sits behind the same rate limit, so all of them share one gate:
# (max requests in flight, min seconds between request starts). Without it, ~30 tenants at once get HTTP 429.
HOST_LIMITS: dict[str, tuple[int, float]] = {"myworkdayjobs.com": (2, 0.6), "myworkdaysite.com": (2, 0.6)}
# After a request to such a group still gets 429 once retries are used up, leave the group alone for a while
# (the rest of the scan skips it) instead of every tenant hammering it and prolonging the block.
RATE_LIMIT_COOLDOWN = 15 * 60.0


class _HostGate:
    def __init__(self, name: str, concurrency: int, interval: float) -> None:
        self.name = name
        self.blocked_until = 0.0
        self.sem = asyncio.Semaphore(concurrency)
        self.lock = asyncio.Lock()
        self.interval = interval
        self.next_start = 0.0


class SourceFetchError(Exception):
    """A source could not be fetched or parsed. Message is shown in /status."""


class HttpClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        max_backoff: float = 30.0,
        concurrency: int = 8,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.client = client
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.max_backoff = max_backoff
        self._sem = asyncio.Semaphore(concurrency)
        self._sleep = sleep
        self._etag_cache: dict[str, tuple[str, Any]] = {}
        self._gates: dict[str, _HostGate] = {}

    def _gate(self, url: str) -> _HostGate | None:
        host = httpx.URL(url).host or ""
        for suffix, (conc, interval) in HOST_LIMITS.items():
            if host == suffix or host.endswith("." + suffix):
                if suffix not in self._gates:
                    self._gates[suffix] = _HostGate(suffix, conc, interval)
                return self._gates[suffix]
        return None

    async def _send(self, gate: _HostGate | None, method: str, url: str, **kwargs) -> httpx.Response:
        if gate is None:
            async with self._sem:
                return await self.client.request(method, url, **kwargs)
        async with gate.sem:
            async with gate.lock:  # space out request starts for the whole host group
                wait = gate.next_start - time.monotonic()
                if wait > 0:
                    await self._sleep(wait)
                gate.next_start = max(time.monotonic(), gate.next_start) + gate.interval
            async with self._sem:
                return await self.client.request(method, url, **kwargs)

    def _delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(self.max_backoff, max(0.0, float(retry_after)))
            except ValueError:
                pass
        return min(self.max_backoff, self.backoff_base * (2 ** attempt) + random.uniform(0, 0.25))

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        last_exc: Exception | None = None
        gate = self._gate(url)
        for attempt in range(self.max_retries + 1):
            if gate is not None and gate.blocked_until > time.monotonic():
                mins = max(1, round((gate.blocked_until - time.monotonic()) / 60))
                raise SourceFetchError(f"HTTP 429 (rate limited) from {gate.name}; skipped, retrying in ~{mins} min")
            try:
                resp = await self._send(gate, method, url, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    break
                delay = self._delay(attempt, None)
                log.debug("%s %s failed (%s); retrying in %.1fs", method, url, type(exc).__name__, delay)
                await self._sleep(delay)
                continue
            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                retry_after = resp.headers.get("Retry-After")
                delay = self._delay(attempt, retry_after)
                if resp.status_code == 429 and not retry_after:  # rate limited: back off for real
                    delay = max(delay, min(self.max_backoff, 5.0 * (2 ** attempt)))
                if gate is not None:  # pause the whole host group, not just this request
                    gate.next_start = max(gate.next_start, time.monotonic() + delay)
                log.debug("%s %s -> %s; retrying in %.1fs", method, url, resp.status_code, delay)
                await self._sleep(delay)
                continue
            if resp.status_code == 429 and gate is not None:
                gate.blocked_until = time.monotonic() + RATE_LIMIT_COOLDOWN
                log.warning("%s is rate-limiting us (HTTP 429); pausing it for %d min", gate.name,
                            RATE_LIMIT_COOLDOWN // 60)
            return resp
        kind = type(last_exc).__name__ if last_exc else "error"
        raise SourceFetchError(f"{kind} after {self.max_retries + 1} attempts: {url}") from last_exc

    @staticmethod
    def _check(resp: httpx.Response, url: str) -> None:
        if resp.status_code >= 400:
            raise SourceFetchError(f"HTTP {resp.status_code} from {url}")

    async def get_json(self, url: str, **kwargs) -> Any:
        resp = await self.request("GET", url, **kwargs)
        self._check(resp, url)
        try:
            return resp.json()
        except ValueError as exc:
            raise SourceFetchError(f"Invalid JSON from {url}") from exc

    async def post_json(self, url: str, payload: Any, **kwargs) -> Any:
        resp = await self.request("POST", url, json=payload, **kwargs)
        self._check(resp, url)
        try:
            return resp.json()
        except ValueError as exc:
            raise SourceFetchError(f"Invalid JSON from {url}") from exc

    async def get_text(self, url: str, **kwargs) -> str:
        resp = await self.request("GET", url, **kwargs)
        self._check(resp, url)
        return resp.text

    async def get_cached(self, url: str, parse: Callable[[httpx.Response], Any]) -> tuple[Any, bool]:
        """Conditional GET using ETag. Returns (parsed, from_cache)."""
        headers = {}
        cached = self._etag_cache.get(url)
        if cached:
            headers["If-None-Match"] = cached[0]
        resp = await self.request("GET", url, headers=headers)
        if resp.status_code == 304 and cached:
            return cached[1], True
        self._check(resp, url)
        try:
            parsed = await asyncio.to_thread(parse, resp)  # big files (13 MB JSON) parse off the event loop
        except ValueError as exc:
            raise SourceFetchError(f"Could not parse response from {url}") from exc
        etag = resp.headers.get("ETag")
        if etag:
            self._etag_cache[url] = (etag, parsed)
        return parsed, False


def build_async_client(timeout: float = 20.0, user_agent: str | None = None,
                       transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    headers = {"Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9"}
    if user_agent:
        headers["User-Agent"] = user_agent
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
        headers=headers,
        follow_redirects=True,
        transport=transport,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
