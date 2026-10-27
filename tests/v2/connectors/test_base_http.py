"""The polite HTTP client: pacing, pinned versions, retries, Retry-After, auth errors, pagination."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from autonomy.connectors import (
    AccessDenied,
    AuthRejected,
    ConnectorHttp,
    HttpPolicy,
    HttpResult,
    ManualClock,
    RateLimited,
    RateSpec,
    RemoteError,
    SourceUnavailable,
    TransientFailure,
    graphql_page,
    graphql_throttle_wait,
    graphql_throttled,
    idempotency_key,
    iterate_pages,
    jsonapi_next,
    link_header_next,
    parse_retry_after,
    redact,
)
from autonomy.connectors.http import bucket_for

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
BASE = "https://api.example/v1"


def client(handler, clock: ManualClock | None = None, **kwargs) -> tuple[ConnectorHttp, ManualClock]:
    clock = clock or ManualClock(T0)
    policy = kwargs.pop("policy", None) or HttpPolicy(
        base_url=BASE, version_headers=(("revision", "2026-07-15"),)
    )
    http = ConnectorHttp(policy, clock, transport=httpx.MockTransport(handler), **kwargs)
    return http, clock


def test_pins_the_version_and_sends_auth_on_every_request() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    http, _ = client(handler, auth=lambda: {"Authorization": "Bearer t1"})
    assert http.json("GET", "profiles", params={"page[size]": 100}) == {"ok": True}
    request = seen[0]
    assert request.headers["revision"] == "2026-07-15"
    assert request.headers["authorization"] == "Bearer t1"
    assert request.url.path == "/v1/profiles"
    assert request.headers["user-agent"].startswith("Autonomy-Connectors/")


def test_429_with_retry_after_seconds_is_waited_then_retried() -> None:
    answers = [httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(200, json={"n": 1})]
    http, clock = client(lambda r: answers.pop(0))
    assert http.json("GET", "events") == {"n": 1}
    assert clock.slept == [2.0]
    assert http.requests_made == 2


def test_429_with_a_long_retry_after_hands_the_job_back_instead_of_sleeping() -> None:
    http, clock = client(lambda r: httpx.Response(429, headers={"Retry-After": "120"}))
    with pytest.raises(RateLimited) as caught:
        http.send("GET", "events")
    assert caught.value.retry_at == T0 + timedelta(seconds=120)
    assert clock.slept == []


def test_retry_after_as_an_http_date() -> None:
    when = format_datetime(T0 + timedelta(seconds=5), usegmt=True)
    assert parse_retry_after(when, T0) == pytest.approx(5.0)
    assert parse_retry_after("1.5", T0) == 1.5
    assert parse_retry_after("soon", T0) is None
    assert parse_retry_after(None, T0) is None


def test_5xx_backs_off_with_jitter_then_gives_up_with_a_retry_time() -> None:
    http, clock = client(lambda r: httpx.Response(503))
    with pytest.raises(TransientFailure) as caught:
        http.send("GET", "orders")
    assert http.requests_made == 5
    assert len(clock.slept) == 4
    assert clock.slept == sorted(clock.slept)  # exponential
    assert all(1.0 <= s <= 60 * 1.25 for s in clock.slept)
    assert caught.value.retry_at is not None and caught.value.retry_at > clock.now()
    # the jitter is fixed by the request, so the same run repeats exactly
    again, clock2 = client(lambda r: httpx.Response(503))
    with pytest.raises(TransientFailure):
        again.send("GET", "orders")
    assert clock2.slept == clock.slept


def test_a_post_is_retried_after_a_timeout_only_with_an_idempotency_key() -> None:
    calls: list[httpx.Request] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(202, json={"accepted": True})

    http, _ = client(flaky)
    with pytest.raises(TransientFailure, match="no idempotency key"):
        http.send("POST", "events", json_body={"a": 1})
    assert len(calls) == 1

    calls.clear()
    http, _ = client(flaky)
    key = idempotency_key("decision_1", "klaviyo_event")
    result = http.send("POST", "events", json_body={"a": 1}, idempotency_key=key)
    assert result.status == 202
    assert [c.headers["idempotency-key"] for c in calls] == [key, key]
    assert key == idempotency_key("decision_1", "klaviyo_event")


def test_401_refreshes_once_then_reconnect_is_needed() -> None:
    tokens = ["old"]
    refreshed: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["authorization"] == "Bearer new":
            return httpx.Response(200, json={"ok": 1})
        return httpx.Response(401, json={"error": "expired"})

    def refresh() -> None:
        refreshed.append(1)
        tokens[0] = "new"

    http, _ = client(handler, auth=lambda: {"Authorization": f"Bearer {tokens[0]}"}, on_unauthorized=refresh)
    assert http.json("GET", "me") == {"ok": 1}
    assert refreshed == [1]

    http, _ = client(
        lambda r: httpx.Response(401),
        auth=lambda: {"Authorization": "Bearer x"},
        on_unauthorized=lambda: None,
    )
    with pytest.raises(AuthRejected):
        http.send("GET", "me")


def test_403_402_and_404_are_classified() -> None:
    http, _ = client(lambda r: httpx.Response(403))
    with pytest.raises(AccessDenied):
        http.send("GET", "orders")
    http, _ = client(lambda r: httpx.Response(402))
    with pytest.raises(SourceUnavailable) as caught:
        http.send("GET", "orders")
    assert caught.value.retry_at == T0 + timedelta(hours=1)
    http, _ = client(lambda r: httpx.Response(404))
    with pytest.raises(RemoteError) as missing:
        http.json("GET", "orders/9")
    assert missing.value.status == 404


def test_credentials_never_go_to_another_host() -> None:
    http, _ = client(lambda r: httpx.Response(200, json={}), auth=lambda: {"Authorization": "Bearer t"})
    http.json("GET", "https://api.example/v1/next?page=2")  # same host: fine
    with pytest.raises(RemoteError, match=r"another host|other than"):
        http.json("GET", "https://evil.example/steal")


def test_token_bucket_paces_to_the_documented_rate() -> None:
    http, clock = client(
        lambda r: httpx.Response(200, json={}),
        bucket=bucket_for(RateSpec(requests=2, per=timedelta(seconds=1), burst=2)),
    )
    for _ in range(4):
        http.send("GET", "x")
    assert sum(clock.slept) == pytest.approx(1.0, rel=0.01)  # 2 at once, then 2 more at 2/s


def test_ratelimit_headers_hold_off_the_next_request() -> None:
    answers = [
        httpx.Response(200, json={}, headers={"RateLimit-Remaining": "0", "RateLimit-Reset": "3"}),
        httpx.Response(200, json={}),
    ]
    http, clock = client(lambda r: answers.pop(0))
    http.send("GET", "a")
    assert clock.slept == []
    http.send("GET", "b")
    assert clock.slept == [3.0]


def test_graphql_cost_pacing_and_throttled_retry() -> None:
    throttled = {
        "errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
        "extensions": {
            "cost": {
                "requestedQueryCost": 100,
                "throttleStatus": {"maximumAvailable": 1000, "currentlyAvailable": 50, "restoreRate": 50},
            }
        },
    }
    ok = {
        "data": {"orders": {"nodes": [{"id": "1"}], "pageInfo": {"hasNextPage": True, "endCursor": "abc"}}},
        "extensions": {
            "cost": {
                "requestedQueryCost": 100,
                "throttleStatus": {"maximumAvailable": 1000, "currentlyAvailable": 900, "restoreRate": 50},
            }
        },
    }
    answers = [httpx.Response(200, json=throttled), httpx.Response(200, json=ok)]
    http, clock = client(
        lambda r: answers.pop(0), pacing=graphql_throttle_wait, retry_signal=graphql_throttled
    )
    data = http.graphql("query { orders(first: 1) { nodes { id } } }")
    assert clock.slept == [1.0]  # (100 - 50) / 50 restore per second
    nodes, cursor = graphql_page(data, ["orders"])
    assert [n["id"] for n in nodes] == ["1"] and cursor == "abc"


def test_graphql_errors_raise_with_codes_only() -> None:
    http, _ = client(
        lambda r: httpx.Response(
            200, json={"errors": [{"message": "secret stuff", "extensions": {"code": "ACCESS_DENIED"}}]}
        )
    )
    with pytest.raises(RemoteError) as caught:
        http.graphql("{ shop { id } }")
    assert "ACCESS_DENIED" in caught.value.message and "secret stuff" not in caught.value.message


def test_pagination_helpers_tolerate_drift() -> None:
    assert jsonapi_next({"links": {"next": "https://a.klaviyo.com/api/profiles?page[cursor]=x"}}).endswith(
        "x"
    )
    assert jsonapi_next({"links": {"next": None}}) is None
    assert jsonapi_next({"data": []}) is None
    header = (
        '<https://s.example/a?page_info=p1>; rel="previous", <https://s.example/a?page_info=p2>; rel="next"'
    )
    assert link_header_next(header) == "https://s.example/a?page_info=p2"
    assert link_header_next(None) is None
    nodes, cursor = graphql_page(
        {"orders": {"edges": [{"node": {"id": "2"}}], "pageInfo": {"hasNextPage": False}}}, ["orders"]
    )
    assert nodes == [{"id": "2"}] and cursor is None
    assert graphql_page({"orders": "renamed"}, ["orders"]) == ([], None)
    assert graphql_page(None, ["orders"]) == ([], None)


def test_iterate_pages_follows_cursors_and_stops_on_a_repeated_one() -> None:
    pages = {None: ([{"i": 1}], "a"), "a": ([{"i": 2}], "b"), "b": ([{"i": 3}], "b")}
    assert [r["i"] for r in iterate_pages(lambda c: pages[c])] == [1, 2, 3]


def test_download_lines_streams_jsonl_through_the_host_guard() -> None:
    guarded: list[str] = []
    body = b'{"id": 1}\n{"id": 2}\n'

    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers  # signed URLs get no token
        return httpx.Response(200, content=body)

    http, _ = client(handler, auth=lambda: {"Authorization": "Bearer t"}, host_guard=guarded.append)
    lines = list(http.download_lines("https://storage.example/bulk/abc.jsonl?sig=s3cr3t"))
    assert lines == [b'{"id": 1}', b'{"id": 2}']
    assert guarded == ["https://storage.example/bulk/abc.jsonl?sig=s3cr3t"]


def test_errors_are_secret_free() -> None:
    text = "POST https://shop.example/admin/oauth/access_token?code=abc123&hmac=deadbeef failed: Bearer shpat_1234567890abcdef access_token=xyz"
    clean = redact(text)
    for secret in ("abc123", "deadbeef", "shpat_1234567890abcdef", "xyz"):
        assert secret not in clean
    http, _ = client(lambda r: httpx.Response(500))
    with pytest.raises(TransientFailure) as caught:
        http.send(
            "GET", "orders", params={"api_key": "k_supersecret"}  # secret-scan: allow (fake test value)
        )
    assert "k_supersecret" not in str(caught.value)


def test_http_result_json_on_garbage() -> None:
    result = HttpResult(status=200, headers={}, body=b"<html>", method="GET", path="/x")
    with pytest.raises(RemoteError):
        result.json()


def test_base_url_must_be_https() -> None:
    with pytest.raises(ValueError):
        ConnectorHttp(HttpPolicy(base_url="http://api.example"), ManualClock(T0))


def test_download_lines_from_resumes_by_byte_offset_with_a_range_request() -> None:
    body = b'{"id": 1}\n{"id": 2}\r\n\n{"id": 3}'
    ranges: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ranges.append(request.headers.get("range"))
        start = int(request.headers["range"][6:-1]) if "range" in request.headers else 0
        return httpx.Response(206 if start else 200, content=body[start:])

    http, _ = client(handler)
    url = "https://storage.example/bulk.jsonl"
    first = []
    for line, offset in http.download_lines_from(url):
        first.append((line, offset))
        if len(first) == 1:
            break  # one bounded page
    assert first == [(b'{"id": 1}', 10)]
    rest = list(http.download_lines_from(url, start=10))
    assert rest == [(b'{"id": 2}', 21), (b'{"id": 3}', len(body))]
    assert ranges == [None, "bytes=10-"]
    # a server that ignores Range (200) has the first bytes skipped instead
    plain, _ = client(lambda r: httpx.Response(200, content=body))
    assert [line for line, _ in plain.download_lines_from(url, start=10)] == [b'{"id": 2}', b'{"id": 3}']
    done, _ = client(lambda r: httpx.Response(416))
    assert list(done.download_lines_from(url, start=len(body))) == []


def test_a_download_that_breaks_off_resumes_from_the_last_whole_line() -> None:
    body = b"a\nb\nc\n"
    calls: list[str | None] = []

    class Broken(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            yield b"a\nb"
            raise httpx.ReadError("reset")

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("range"))
        if len(calls) == 1:
            return httpx.Response(200, stream=Broken())
        return httpx.Response(206, content=body[2:])

    http, _ = client(handler)
    assert list(http.download_lines("https://storage.example/x.jsonl")) == [b"a", b"b", b"c"]
    assert calls == [None, "bytes=2-"]


def test_credentials_never_downgrade_to_http_or_another_port() -> None:
    http, _ = client(lambda r: httpx.Response(200, json={}), auth=lambda: {"Authorization": "Bearer t"})
    for url in ("http://api.example/v1/next", "https://api.example:8443/v1/next"):
        with pytest.raises(RemoteError):
            http.json("GET", url)
