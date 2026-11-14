from __future__ import annotations

import asyncio
import gzip

import httpx
import pytest

from info_desk.exceptions import InfoDeskError
from info_desk.http_client import SafeHttpClient
from info_desk.sources.base import AdapterError, bounded_get


@pytest.mark.asyncio
async def test_arxiv_request_interval_is_enforced_without_real_sleep() -> None:
    clock_value = [100.0]
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)
        clock_value[0] += delay

    async with SafeHttpClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request)),
        clock=lambda: clock_value[0],
        sleep=sleep,
    ) as client:
        for _ in range(2):
            async with client.stream("GET", "https://export.arxiv.org/api/query") as response:
                await response.aread()

    assert delays == [pytest.approx(3.0)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("token", "expected"),
    [(None, 6.1), ("github-secret", 2.1)],
)
async def test_github_search_quota_uses_auth_aware_interval(token, expected) -> None:
    clock_value = [1_000.0]
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)
        clock_value[0] += delay

    async with SafeHttpClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request)),
        github_token=token,
        clock=lambda: clock_value[0],
        sleep=sleep,
    ) as client:
        for _ in range(2):
            async with client.stream(
                "GET", "https://api.github.com/search/repositories?q=vla"
            ) as response:
                await response.aread()

    assert delays == [pytest.approx(expected)]


@pytest.mark.asyncio
async def test_bounded_get_retries_429_using_retry_after() -> None:
    clock_value = [100.0]
    delays: list[float] = []
    requests = 0

    async def sleep(delay: float) -> None:
        delays.append(delay)
        clock_value[0] += delay

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    async with SafeHttpClient(
        max_retries=1,
        transport=httpx.MockTransport(handler),
        clock=lambda: clock_value[0],
        sleep=sleep,
    ) as client:
        response = await bounded_get(client, "https://huggingface.co/api/daily_papers")

    assert response.status_code == 200
    assert requests == 2
    assert sum(delays) == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_rate_limited_github_403_exposes_wait_without_secret() -> None:
    request = httpx.Request("GET", "https://api.github.com/search/repositories")
    response = httpx.Response(
        403,
        headers={"Retry-After": "17", "X-RateLimit-Remaining": "10"},
        json={"message": "You have exceeded a secondary rate limit"},
        request=request,
    )
    from info_desk.sources.github import _raise_github_forbidden

    with pytest.raises(AdapterError) as raised:
        _raise_github_forbidden(response)

    assert raised.value.code == "RATE_LIMITED"
    assert raised.value.retry_after_seconds == 17
    assert "secret" not in str(raised.value).lower()


@pytest.mark.asyncio
async def test_get_uses_bounded_stream_and_rejects_unsafe_redirect() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "api.github.com":
            return httpx.Response(302, headers={"Location": "https://example.com/"}, request=request)
        return httpx.Response(200, request=request)

    async with SafeHttpClient(
        github_token="do-not-leak",
        transport=httpx.MockTransport(handler),
        sleep=lambda _delay: asyncio.sleep(0),
    ) as client:
        with pytest.raises(InfoDeskError, match="supported source hosts") as raised:
            await client.get("https://api.github.com/repos/a/b")

    assert len(requests) == 1
    assert "do-not-leak" not in str(raised.value)


@pytest.mark.asyncio
async def test_redirect_between_allowlisted_hosts_does_not_forward_caller_token() -> None:
    auth_headers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth_headers.append(request.headers.get("authorization"))
        if request.url.host == "api.github.com":
            return httpx.Response(
                302,
                headers={"Location": "https://huggingface.co/api/daily_papers"},
                request=request,
            )
        return httpx.Response(200, json=[], request=request)

    async with SafeHttpClient(
        github_token="github-secret",
        transport=httpx.MockTransport(handler),
        sleep=lambda _delay: asyncio.sleep(0),
    ) as client:
        response = await client.get(
            "https://api.github.com/search/repositories",
            headers={"Authorization": "Bearer explicit-secret"},
        )

    assert response.status_code == 200
    assert auth_headers == ["Bearer explicit-secret", None]


@pytest.mark.asyncio
async def test_response_body_limit_applies_to_streamed_body() -> None:
    async with SafeHttpClient(
        max_response_bytes=5,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"123456", request=request)
        ),
        sleep=lambda _delay: asyncio.sleep(0),
    ) as client:
        with pytest.raises(AdapterError, match="size limit"):
            await bounded_get(client, "https://huggingface.co/api/daily_papers")


@pytest.mark.asyncio
async def test_bounded_get_does_not_decode_compressed_stream_twice() -> None:
    body = gzip.compress(b'{"papers": []}')
    async with SafeHttpClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"Content-Encoding": "gzip", "Content-Length": str(len(body))},
                content=body,
                request=request,
            )
        )
    ) as client:
        response = await bounded_get(client, "https://huggingface.co/api/daily_papers")
    assert response.json() == {"papers": []}
    assert "content-encoding" not in response.headers


@pytest.mark.asyncio
async def test_rejects_invalid_port_before_network_request() -> None:
    async with SafeHttpClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request))
    ) as client:
        with pytest.raises(InfoDeskError, match="supported source hosts"):
            await client.get("https://arxiv.org:99999/api/query")
