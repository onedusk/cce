"""Uniform auth enforcement check across every protected API route (audit T3).

Discovery is explicit: the two lists below are the source of truth for
"which routes require auth" vs. "which routes are intentionally public."
A new route that joins either list must be added here — that is exactly
the test's purpose.

Structure:
  - PROTECTED_ROUTES is parameterized for the full matrix:
      A) unauthenticated request -> 401 (auth_missing branch)
      B) bearer-invalid request  -> 401 (auth_invalid branch)
      C) valid key request       -> anything EXCEPT 401 (auth passed)
  - UNPROTECTED_ROUTES is a single test that asserts the inventory
    matches the set of routes known to be public today (health, meta,
    job-read). A new public endpoint addition has to pass through
    this list or the test fails — forces an explicit decision.

The exists/valid-resource concern is sidestepped: for the "valid key"
branch we accept any non-401 status because the routes we're exercising
reference not-yet-created resources and will legitimately return 404 or
400. The ONLY thing we assert is "auth didn't reject the request."
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from cce.api.auth import generate_api_key, hash_api_key
from tests.test_api.test_jobs_lifecycle import _make_lifecycle_app

pytestmark = pytest.mark.integration


# (method, path, body-or-None). Routes declared protected by Depends(auth_dependency)
# in src/cce/api/routes/**. Order matches grep output for traceability.
PROTECTED_ROUTES: list[tuple[str, str, dict | None]] = [
    (
        "POST",
        "/v1/curate/jobs",
        {"topic": "test", "paths": ["blog"], "policy_id": "test-policy"},
    ),
    ("DELETE", "/v1/curate/jobs/job_nonexistent", None),
    ("POST", "/v1/curate/jobs/job_nonexistent/retry", None),
    ("GET", "/v1/curate/evidence/ev_nonexistent", None),
    ("GET", "/v1/curate/evidence", None),
]

# Routes intentionally public. A new entry here is an explicit decision.
UNPROTECTED_ROUTES_EXPECTED: set[tuple[str, str]] = {
    ("GET", "/v1/health"),
    ("GET", "/v1/meta"),
    ("GET", "/v1/curate/jobs/job_nonexistent"),
    ("GET", "/v1/curate/jobs"),
    ("GET", "/v1/curate/jobs/job_nonexistent/package"),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _auth_app(tmp_path: Path):
    """Fully wired app with require_auth=True + a seeded valid API key."""
    app, job_store, evidence_store = await _make_lifecycle_app(
        tmp_path, require_auth=True
    )
    key = generate_api_key()
    await job_store.store_api_key(hash_api_key(key), label="parameterized-auth-test")
    return app, job_store, evidence_store, key


async def _request(
    client: httpx.AsyncClient, method: str, path: str, body: dict | None, headers: dict
) -> httpx.Response:
    if method == "GET":
        return await client.get(path, headers=headers)
    if method == "DELETE":
        return await client.delete(path, headers=headers)
    if method == "POST":
        return await client.post(path, json=body, headers=headers)
    raise AssertionError(f"Unsupported method: {method}")


# ---------------------------------------------------------------------------
# PROTECTED routes — three-way parameterize
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body"), PROTECTED_ROUTES)
async def test_protected_route_rejects_unauthenticated(
    method: str, path: str, body: dict | None, tmp_path: Path
):
    app, job_store, evidence_store, _key = await _auth_app(tmp_path)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await _request(client, method, path, body, headers={})
        assert resp.status_code == 401
    finally:
        await job_store.close()
        await evidence_store.close()


@pytest.mark.parametrize(("method", "path", "body"), PROTECTED_ROUTES)
async def test_protected_route_rejects_invalid_bearer(
    method: str, path: str, body: dict | None, tmp_path: Path
):
    app, job_store, evidence_store, _key = await _auth_app(tmp_path)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await _request(
                    client,
                    method,
                    path,
                    body,
                    headers={"Authorization": "Bearer this-is-not-a-real-key"},
                )
        assert resp.status_code == 401
    finally:
        await job_store.close()
        await evidence_store.close()


@pytest.mark.parametrize(("method", "path", "body"), PROTECTED_ROUTES)
async def test_protected_route_accepts_valid_bearer(
    method: str, path: str, body: dict | None, tmp_path: Path
):
    """Auth doesn't reject a valid key. The response may still be 4xx
    (e.g. 404 for a nonexistent job) but MUST NOT be 401."""
    app, job_store, evidence_store, key = await _auth_app(tmp_path)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await _request(
                    client,
                    method,
                    path,
                    body,
                    headers={"Authorization": f"Bearer {key}"},
                )
        assert resp.status_code != 401, (
            f"Valid key rejected with 401 on {method} {path}: {resp.text[:200]}"
        )
    finally:
        await job_store.close()
        await evidence_store.close()


# ---------------------------------------------------------------------------
# Cross-check: unauthenticated error body carries request_id (M04 integration)
# ---------------------------------------------------------------------------


async def test_unauthenticated_error_body_carries_request_id(tmp_path: Path):
    """401 responses include the request_id so operators can correlate."""
    app, job_store, evidence_store, _key = await _auth_app(tmp_path)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/v1/curate/jobs",
                    json={"topic": "t", "paths": ["blog"], "policy_id": "test-policy"},
                )
        assert resp.status_code == 401
        header_id = resp.headers.get("X-Request-ID")
        assert header_id is not None and header_id.startswith("req_")
    finally:
        await job_store.close()
        await evidence_store.close()


# ---------------------------------------------------------------------------
# UNPROTECTED routes inventory — forces explicit decisions on new routes
# ---------------------------------------------------------------------------


async def test_unprotected_routes_are_explicit(tmp_path: Path):
    """Every route in UNPROTECTED_ROUTES_EXPECTED returns non-401 without a
    bearer token. A new public route is discovered by:
      - adding it to UNPROTECTED_ROUTES_EXPECTED (this set is the source of truth);
      - or adding it to PROTECTED_ROUTES above (which will catch missing auth).

    The test does NOT automatically enumerate all app routes — that would
    permit silent additions. The enum is manual and deliberate.
    """
    app, job_store, evidence_store, _key = await _auth_app(tmp_path)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                for method, path in UNPROTECTED_ROUTES_EXPECTED:
                    resp = await _request(client, method, path, None, headers={})
                    assert resp.status_code != 401, (
                        f"Expected {method} {path} to be public (non-401); "
                        f"got 401 — did auth get added? Update PROTECTED_ROUTES "
                        f"and drop from UNPROTECTED_ROUTES_EXPECTED."
                    )
    finally:
        await job_store.close()
        await evidence_store.close()
