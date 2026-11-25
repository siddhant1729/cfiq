"""Tests for the API rate limiter, retry/backoff and raw cache."""

import pytest
import requests

from cfiq.extract.client import (
    CodeforcesAPIError,
    CodeforcesClient,
    PermanentAPIError,
    RateLimiter,
)


class FakeClock:
    """A clock that only moves when sleep() is called, so tests run instantly."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """Returns the queued responses in order; records each request time."""

    def __init__(self, responses, clock):
        self.responses = list(responses)
        self.clock = clock
        self.call_times = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.call_times.append(self.clock.time())
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


OK = FakeResponse(200, {"status": "OK", "result": [1, 2, 3]})


def make_client(tmp_path, responses, **kw):
    clock = FakeClock()
    session = FakeSession(responses, clock)
    client = CodeforcesClient(
        base_url="https://example.test/api/",
        cache_dir=tmp_path,
        min_interval=kw.pop("min_interval", 2.0),
        max_retries=kw.pop("max_retries", 3),
        backoff_base=1.0,
        session=session,
        clock=clock.time,
        sleep=clock.sleep,
        **kw,
    )
    return client, session, clock


# ---------- rate limiter ----------


def test_rate_limiter_spaces_calls_by_min_interval():
    clock = FakeClock()
    limiter = RateLimiter(2.0, clock=clock.time, sleep=clock.sleep)
    times = []
    for _ in range(4):
        limiter.wait()
        times.append(clock.time())
    assert times == [0.0, 2.0, 4.0, 6.0]


def test_rate_limiter_does_not_sleep_if_enough_time_passed():
    clock = FakeClock()
    limiter = RateLimiter(2.0, clock=clock.time, sleep=clock.sleep)
    limiter.wait()
    clock.now += 5.0  # caller did other work for 5 s
    limiter.wait()
    assert clock.sleeps == []


def test_client_requests_are_rate_limited(tmp_path):
    client, session, _ = make_client(tmp_path, [OK, OK, OK])
    for h in ("a", "b", "c"):
        client.get("user.rating", {"handle": h})
    gaps = [b - a for a, b in zip(session.call_times, session.call_times[1:], strict=False)]
    assert all(g >= 2.0 for g in gaps)


# ---------- retry + backoff ----------


def test_retries_on_429_then_succeeds(tmp_path):
    client, session, clock = make_client(tmp_path, [FakeResponse(429), FakeResponse(503), OK])
    assert client.get("contest.list") == [1, 2, 3]
    assert client.api_calls == 3
    # backoff delays 1, 2 (base * 2^attempt) were slept
    assert 1.0 in clock.sleeps and 2.0 in clock.sleeps


def test_retries_on_failed_status(tmp_path):
    failed = FakeResponse(400, {"status": "FAILED", "comment": "Call limit exceeded"})
    client, _, _ = make_client(tmp_path, [failed, OK])
    assert client.get("contest.list") == [1, 2, 3]
    assert client.api_calls == 2


def test_retries_on_network_error(tmp_path):
    client, _, _ = make_client(tmp_path, [requests.ConnectionError("boom"), OK])
    assert client.get("contest.list") == [1, 2, 3]


def test_permanent_error_is_not_retried(tmp_path):
    missing = FakeResponse(
        400, {"status": "FAILED", "comment": "handle: User with handle zzz not found"}
    )
    client, _, _ = make_client(tmp_path, [missing, OK])
    with pytest.raises(PermanentAPIError):
        client.get("user.rating", {"handle": "zzz"})
    assert client.api_calls == 1


def test_gives_up_after_max_retries(tmp_path):
    # min_interval=0 so the only sleeps recorded are the backoff delays
    client, _, clock = make_client(tmp_path, [FakeResponse(500)] * 4, max_retries=3, min_interval=0)
    with pytest.raises(CodeforcesAPIError, match="gave up after 4 attempts"):
        client.get("contest.list")
    assert client.api_calls == 4
    assert clock.sleeps == [1.0, 2.0, 4.0]  # base * 2^attempt


# ---------- cache ----------


def test_second_call_is_served_from_cache(tmp_path):
    client, _, _ = make_client(tmp_path, [OK])  # only ONE response queued
    first = client.get("user.status", {"handle": "tourist"})
    second = client.get("user.status", {"handle": "tourist"})
    assert first == second == [1, 2, 3]
    assert client.api_calls == 1 and client.cache_hits == 1


def test_cache_survives_new_client(tmp_path):
    client, _, _ = make_client(tmp_path, [OK])
    client.get("contest.list", {"gym": "false"})
    client2, session2, _ = make_client(tmp_path, [])  # would crash if it hit the "API"
    assert client2.get("contest.list", {"gym": "false"}) == [1, 2, 3]
    assert session2.call_times == []


def test_cache_key_depends_on_params(tmp_path):
    client, _, _ = make_client(tmp_path, [])
    a = client.cache_path("user.status", {"handle": "a"})
    b = client.cache_path("user.status", {"handle": "b"})
    assert a != b
    assert client.cache_path("x", {"p": 1, "q": 2}) == client.cache_path("x", {"q": 2, "p": 1})
    long_key = client.cache_path("user.info", {"handles": ";".join(["h"] * 100)})
    assert long_key.name.startswith("h_")
