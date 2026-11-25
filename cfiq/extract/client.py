"""Codeforces API client.

DMDW topic: data extraction (the E in ETL/ELT), from an operational source system.

Three responsibilities, each a small piece you can point at:
  1. RateLimiter      - at most one request every `min_interval` seconds.
  2. retry + backoff  - transient failures (HTTP 429/5xx, network errors,
                        FAILED responses such as "Call limit exceeded") are
                        retried with exponential backoff; permanent failures
                        (e.g. "handle not found") are raised immediately.
  3. raw cache        - every successful response is saved as JSON on disk,
                        keyed by endpoint + params, so reruns never hit the API.
"""

import hashlib
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

import requests

log = logging.getLogger(__name__)


class CodeforcesAPIError(Exception):
    """The API kept failing after all retries."""


class PermanentAPIError(CodeforcesAPIError):
    """The API answered FAILED with an error that retrying cannot fix."""


class RateLimiter:
    """Guarantees at least `min_interval` seconds between consecutive calls to wait()."""

    def __init__(self, min_interval: float, clock=time.monotonic, sleep=time.sleep):
        self.min_interval = min_interval
        self.clock = clock
        self.sleep = sleep
        self._last_call: float | None = None

    def wait(self) -> None:
        now = self.clock()
        if self._last_call is not None:
            remaining = self.min_interval - (now - self._last_call)
            if remaining > 0:
                self.sleep(remaining)
                now = self.clock()
        self._last_call = now


class CodeforcesClient:
    """Rate-limited, retrying, caching client for https://codeforces.com/api/."""

    def __init__(
        self,
        base_url: str,
        cache_dir: Path,
        min_interval: float = 2.0,
        max_retries: int = 5,
        backoff_base: float = 2.0,
        retry_http_codes: tuple[int, ...] = (429, 500, 502, 503, 504),
        permanent_error_patterns: tuple[str, ...] = ("not found",),
        timeout: float = 60,
        user_agent: str = "cfiq",
        session=None,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        self.base_url = base_url.rstrip("/") + "/"
        self.cache_dir = Path(cache_dir)
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.retry_http_codes = set(retry_http_codes)
        self.permanent_error_patterns = [p.lower() for p in permanent_error_patterns]
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        self.sleep = sleep
        self.rate_limiter = RateLimiter(min_interval, clock=clock, sleep=sleep)
        # Counters, read by the pipeline for etl_metadata.
        self.api_calls = 0
        self.cache_hits = 0

    @classmethod
    def from_config(cls, cfg: dict) -> "CodeforcesClient":
        api = cfg["api"]
        return cls(
            base_url=api["base_url"],
            cache_dir=cfg["paths"]["raw_cache"],
            min_interval=api["min_interval_seconds"],
            max_retries=api["max_retries"],
            backoff_base=api["backoff_base_seconds"],
            retry_http_codes=tuple(api["retry_http_codes"]),
            permanent_error_patterns=tuple(api["permanent_error_patterns"]),
            timeout=api["timeout_seconds"],
            user_agent=api["user_agent"],
        )

    # ---------- cache ----------

    def cache_path(self, endpoint: str, params: dict | None = None) -> Path:
        """data/raw/<endpoint>/<params>.json  (params hashed if they are long)."""
        query = urlencode(sorted((params or {}).items()))
        if not query:
            name = "_all"
        elif len(query) <= 80:
            name = query.replace("&", "__").replace("=", "-")
        else:
            name = "h_" + hashlib.sha1(query.encode()).hexdigest()[:16]
        return self.cache_dir / endpoint / f"{name}.json"

    def _read_cache(self, path: Path) -> dict | None:
        if not path.exists():
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def _write_cache(self, path: Path, endpoint: str, params: dict, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "endpoint": endpoint,
            "params": params,
            "fetched_at": datetime.now(UTC).isoformat(),
            "response": payload,
        }
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f)
        tmp.replace(path)  # atomic: a crash never leaves a half-written cache file

    # ---------- public API ----------

    def get(self, endpoint: str, params: dict | None = None, use_cache: bool = True):
        """Return the `result` field of an API call, from cache if available.

        use_cache=False always calls the API and stores nothing (live stream polling)."""
        params = params or {}
        if not use_cache:
            return self._fetch_with_retry(endpoint, params)["result"]
        path = self.cache_path(endpoint, params)
        cached = self._read_cache(path)
        if cached is not None:
            self.cache_hits += 1
            return cached["response"]["result"]

        payload = self._fetch_with_retry(endpoint, params)
        self._write_cache(path, endpoint, params, payload)
        return payload["result"]

    # ---------- HTTP + retry ----------

    def _is_permanent(self, comment: str) -> bool:
        comment = comment.lower()
        return any(p in comment for p in self.permanent_error_patterns)

    def _fetch_with_retry(self, endpoint: str, params: dict) -> dict:
        url = self.base_url + endpoint
        last_error = ""
        for attempt in range(self.max_retries + 1):
            self.rate_limiter.wait()
            self.api_calls += 1
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as e:
                last_error = f"network error: {e}"
            else:
                if resp.status_code in self.retry_http_codes:
                    last_error = f"HTTP {resp.status_code}"
                else:
                    try:
                        payload = resp.json()
                    except ValueError:
                        last_error = f"non-JSON response (HTTP {resp.status_code})"
                    else:
                        if payload.get("status") == "OK":
                            return payload
                        comment = payload.get("comment", "")
                        if self._is_permanent(comment):
                            raise PermanentAPIError(f"{endpoint} {params}: {comment}")
                        last_error = f"FAILED: {comment}"

            if attempt < self.max_retries:
                delay = self.backoff_base * (2**attempt)
                log.warning(
                    "%s failed (%s); retry %d/%d in %.0fs",
                    endpoint,
                    last_error,
                    attempt + 1,
                    self.max_retries,
                    delay,
                )
                self.sleep(delay)

        raise CodeforcesAPIError(
            f"{endpoint} {params}: gave up after {self.max_retries + 1} attempts ({last_error})"
        )
