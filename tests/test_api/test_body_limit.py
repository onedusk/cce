"""Tests for the request body-size limit middleware (T-01.03, finding 5.1).

A declared Content-Length over the limit is rejected up front; otherwise
the body bytes are counted as they arrive, so a chunked body (no
Content-Length) or one longer than its Content-Length is rejected too
(SEC-02: uvicorn/h11 do not bound a request body).
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from cce.api.auth import auth_dependency
from cce.api.middleware import MAX_BODY_BYTES

pytestmark = pytest.mark.integration


async def test_oversized_request_rejected_before_route(
    app: FastAPI, client: httpx.AsyncClient
):
    """Content-Length > 1 MiB returns the 413 envelope without ever
    reaching the router — pinned via a counting auth-dependency override
    (the router-level dependency runs before any handler)."""
    calls: list[int] = []

    async def _counting_auth() -> None:
        calls.append(1)

    app.dependency_overrides[auth_dependency] = _counting_auth

    body = b"x" * (MAX_BODY_BYTES + 1)
    resp = await client.post(
        "/v1/curate/jobs",
        content=body,
        headers={"content-type": "application/json"},
    )

    assert resp.status_code == 413
    body = resp.json()
    assert body["error"]["code"] == "payload_too_large"
    assert body["error"]["request_id"] is not None
    assert calls == []  # short-circuited before routing/auth


async def test_declared_oversized_content_length_rejected(
    client: httpx.AsyncClient,
):
    """The check reads the declared header — a small body with an inflated
    Content-Length is rejected without buffering anything."""
    resp = await client.post(
        "/v1/curate/jobs",
        content=b"{}",
        headers={
            "content-type": "application/json",
            "content-length": str(MAX_BODY_BYTES + 1),
        },
    )
    assert resp.status_code == 413
    body = resp.json()
    assert body["error"]["code"] == "payload_too_large"
    assert body["error"]["request_id"] is not None


async def test_normal_size_request_unaffected(app: FastAPI, client: httpx.AsyncClient):
    """A normal-size request passes through the middleware to the route."""
    calls: list[int] = []

    async def _counting_auth() -> None:
        calls.append(1)

    app.dependency_overrides[auth_dependency] = _counting_auth

    resp = await client.post(
        "/v1/curate/jobs",
        json={"topic": "test topic", "paths": ["blog"], "policy_id": "test-policy"},
    )
    assert resp.status_code == 202
    assert calls == [1]  # route reached exactly once


async def _chunks(total: int, size: int = 64 * 1024, prefix: bytes = b""):
    """Yield ``prefix`` padded with spaces to ``total`` bytes, in chunks:
    httpx sends an async iterable with no Content-Length."""
    data = prefix + b" " * (total - len(prefix))
    for start in range(0, total, size):
        yield data[start : start + size]


async def test_streamed_body_without_content_length_rejected(
    app: FastAPI, client: httpx.AsyncClient
):
    """SEC-02: a chunked body over the limit used to reach the router (and be
    parsed before auth); it now gets the 413 envelope."""
    calls: list[int] = []

    async def _counting_auth() -> None:
        calls.append(1)

    app.dependency_overrides[auth_dependency] = _counting_auth

    request = client.build_request(
        "POST",
        "/v1/curate/jobs",
        content=_chunks(4 * MAX_BODY_BYTES),
        headers={"content-type": "application/json"},
    )
    assert "content-length" not in request.headers
    resp = await client.send(request)

    assert resp.status_code == 413
    body = resp.json()
    assert body["error"]["code"] == "payload_too_large"
    assert body["error"]["request_id"] is not None
    assert calls == []


async def test_body_longer_than_its_content_length_rejected(
    app: FastAPI, client: httpx.AsyncClient
):
    """SEC-02: a small declared Content-Length doesn't let a large body through."""
    resp = await client.post(
        "/v1/curate/jobs",
        content=b"x" * (MAX_BODY_BYTES + 1),
        headers={"content-type": "application/json", "content-length": "10"},
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"


async def test_streamed_body_at_exactly_the_limit_passes(
    app: FastAPI, client: httpx.AsyncClient
):
    """The limit is inclusive: max_bytes streamed bytes still reach the route."""
    calls: list[int] = []

    async def _counting_auth() -> None:
        calls.append(1)

    app.dependency_overrides[auth_dependency] = _counting_auth

    job = b'{"topic": "test topic", "paths": ["blog"], "policy_id": "test-policy"}'
    resp = await client.post(
        "/v1/curate/jobs",
        content=_chunks(MAX_BODY_BYTES, prefix=job),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 202, resp.text
    assert calls == [1]


async def test_non_http_scopes_pass_through():
    """Lifespan (and websocket) scopes go straight to the wrapped app."""
    from cce.api.middleware import BodySizeLimitMiddleware

    seen: list[str] = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    async def receive():
        raise AssertionError("the middleware must not read a lifespan scope")

    async def send(message):
        pass

    await BodySizeLimitMiddleware(inner)({"type": "lifespan"}, receive, send)
    assert seen == ["lifespan"]
