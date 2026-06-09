"""Tests for the request body-size limit middleware (T-01.03, finding 5.1).

The middleware checks the Content-Length header only — chunked bodies
(no Content-Length) bypass the check and are bounded downstream by
uvicorn/h11's max-incomplete-size. Accepted limitation (Stage 2 note).
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
    assert resp.json()["error"]["code"] == "payload_too_large"
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
    assert resp.json()["error"]["code"] == "payload_too_large"


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
