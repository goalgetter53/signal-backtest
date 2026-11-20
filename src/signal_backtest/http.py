"""Polite, cached async HTTP fetching.

* one global concurrency cap plus a per-host cap and minimum delay between requests to a host
* robots.txt honoured for website fetches (APIs that are documented as public are exempt)
* on-disk cache keyed by URL with a TTL, so re-running a collector on the same day is idempotent
  and does not re-hit the network
* 429/5xx retried with exponential backoff; final status recorded instead of raised
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import urllib.robotparser
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx

USER_AGENT = "signal-backtest/0.1 (+https://github.com/goalgetter53/signal-backtest)"


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: int  # HTTP status; 0 = network error; -1 = disallowed by robots.txt
    body: bytes
    headers: dict[str, str]
    final_url: str
    from_cache: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> object:
        return json.loads(self.body)


@dataclass
class Fetcher:
    cache_dir: Path
    ttl_seconds: int = 20 * 3600
    max_concurrency: int = 24
    per_host: int = 2
    host_delay: float = 0.5
    timeout: float = 20.0
    max_bytes: int = 4_000_000
    retries: int = 2
    offline: bool = False
    # host -> (max concurrent, min delay seconds); public JSON APIs tolerate more than websites
    host_limits: dict[str, tuple[int, float]] = field(default_factory=dict)
    _client: httpx.AsyncClient | None = field(default=None, init=False, repr=False)
    _sem: asyncio.Semaphore | None = field(default=None, init=False, repr=False)
    _host_sems: dict[str, asyncio.Semaphore] = field(default_factory=dict, init=False, repr=False)
    _host_last: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _robots: dict[str, urllib.robotparser.RobotFileParser | None] = field(
        default_factory=dict, init=False, repr=False
    )

    async def __aenter__(self) -> Fetcher:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
            timeout=httpx.Timeout(self.timeout, pool=30.0),
            # Concurrency is bounded by our semaphores, not the pool. A generous pool and no
            # keep-alive mean a connection leaked by a cancelled request can never starve it.
            limits=httpx.Limits(max_connections=2000, max_keepalive_connections=0),
            follow_redirects=True,
        )
        self._sem = asyncio.Semaphore(self.max_concurrency)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()

    # ---------------------------------------------------------------- cache
    def _cache_path(self, url: str, extra: str = "") -> Path:
        h = hashlib.sha256((url + "|" + extra).encode()).hexdigest()
        return self.cache_dir / h[:2] / f"{h}.json"

    def _cache_get(self, url: str, extra: str = "") -> FetchResult | None:
        p = self._cache_path(url, extra)
        if not p.exists():
            return None
        meta = json.loads(p.read_text())
        if not self.offline and time.time() - float(meta["ts"]) > self.ttl_seconds:
            return None
        body_path = p.with_suffix(".bin")
        body = body_path.read_bytes() if body_path.exists() else b""
        return FetchResult(
            url=url,
            status=int(meta["status"]),
            body=body,
            headers=dict(meta["headers"]),
            final_url=str(meta["final_url"]),
            from_cache=True,
            error=str(meta.get("error", "")),
        )

    def _cache_put(self, res: FetchResult, extra: str = "") -> None:
        p = self._cache_path(res.url, extra)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.with_suffix(".bin").write_bytes(res.body)
        p.write_text(
            json.dumps(
                {
                    "ts": time.time(),
                    "status": res.status,
                    "headers": res.headers,
                    "final_url": res.final_url,
                    "error": res.error,
                }
            )
        )

    # --------------------------------------------------------------- robots
    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            res = await self.get(origin + "/robots.txt", check_robots=False)
            rp: urllib.robotparser.RobotFileParser | None = None
            if res.ok:
                rp = urllib.robotparser.RobotFileParser()
                rp.parse(res.text().splitlines())
            self._robots[origin] = rp
        rp = self._robots[origin]
        return True if rp is None else rp.can_fetch(USER_AGENT, url)

    # ------------------------------------------------------------------ get
    async def get(
        self,
        url: str,
        *,
        check_robots: bool = False,
        headers: dict[str, str] | None = None,
    ) -> FetchResult:
        extra = json.dumps(headers or {}, sort_keys=True) if headers else ""
        cached = self._cache_get(url, extra)
        if cached is not None:
            return cached
        if self.offline:
            return FetchResult(url, 0, b"", {}, url, error="offline-cache-miss")
        if check_robots and not await self.allowed(url):
            return FetchResult(url, -1, b"", {}, url, error="robots-disallow")
        assert self._client is not None and self._sem is not None, "use 'async with Fetcher'"
        host = urlsplit(url).netloc
        per_host, delay = self.host_limits.get(host, (self.per_host, self.host_delay))
        hsem = self._host_sems.setdefault(host, asyncio.Semaphore(per_host))
        res = FetchResult(url, 0, b"", {}, url, error="not-attempted")
        async with self._sem, hsem:
            for attempt in range(self.retries + 1):
                wait = delay - (time.monotonic() - self._host_last.get(host, 0.0))
                if wait > 0:
                    await asyncio.sleep(wait)
                self._host_last[host] = time.monotonic()
                try:
                    # hard deadline: slow-drip bodies would otherwise pin a pooled connection
                    r = await asyncio.wait_for(
                        self._client.get(url, headers=headers), timeout=self.timeout * 2
                    )
                    body = r.content[: self.max_bytes]
                    res = FetchResult(
                        url=url,
                        status=r.status_code,
                        body=body,
                        headers={k.lower(): v for k, v in r.headers.items()},
                        final_url=str(r.url),
                    )
                except (httpx.HTTPError, httpx.InvalidURL, OSError, TimeoutError, ValueError) as e:
                    res = FetchResult(url, 0, b"", {}, url, error=type(e).__name__)
                retryable = res.status in (0, 429, 500, 502, 503, 504)
                if retryable and attempt < self.retries:
                    await asyncio.sleep(2.0 * (2**attempt))
                    continue
                break
        if res.status != 0:  # network errors are not cached, so the next run retries them
            self._cache_put(res, extra)
        return res
