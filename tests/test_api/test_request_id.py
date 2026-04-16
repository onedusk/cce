"""Tests for RequestIdMiddleware / RequestIdFilter / get_request_id (audit U1, T-04.02).

Covers only the middleware-side machinery. The error-body carries-request_id
assertion lives with T-04.01's reshape of APIEnvelope in test_error_envelope.py.
"""

from __future__ import annotations

import logging
import re

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request

from cce.api.middleware import (
    RequestIdFilter,
    RequestIdMiddleware,
    RequestLoggingMiddleware,
    get_request_id,
    install_request_id_log_factory,
)

pytestmark = pytest.mark.integration


_REQUEST_ID_PATTERN = re.compile(r"^req_[0-9a-f]{12}$")


def _make_probe_app() -> FastAPI:
    """Minimal FastAPI app exercising only the middleware under test."""
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(RequestIdMiddleware)

    @app.get("/probe")
    async def probe(request: Request) -> dict:
        return {
            "state_request_id": request.state.request_id,
            "contextvar_request_id": get_request_id(),
        }

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("intentional")

    return app


async def _get(
    app: FastAPI, path: str, *, headers: dict | None = None
) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(path, headers=headers or {})


async def test_middleware_sets_request_id_on_state():
    app = _make_probe_app()
    resp = await _get(app, "/probe")
    assert resp.status_code == 200
    body = resp.json()
    assert _REQUEST_ID_PATTERN.match(body["state_request_id"])
    # contextvar matches request.state — both read the same source
    assert body["contextvar_request_id"] == body["state_request_id"]


async def test_response_header_contains_request_id():
    app = _make_probe_app()
    resp = await _get(app, "/probe")
    header = resp.headers.get("x-request-id")
    assert header is not None
    assert _REQUEST_ID_PATTERN.match(header)


async def test_inbound_x_request_id_is_reused():
    app = _make_probe_app()
    resp = await _get(app, "/probe", headers={"X-Request-ID": "req_deadbeef1234"})
    assert resp.headers["X-Request-ID"] == "req_deadbeef1234"
    assert resp.json()["state_request_id"] == "req_deadbeef1234"


async def test_distinct_requests_get_distinct_ids():
    app = _make_probe_app()
    r1 = await _get(app, "/probe")
    r2 = await _get(app, "/probe")
    assert r1.headers["X-Request-ID"] != r2.headers["X-Request-ID"]


async def test_request_id_in_log_records(caplog):
    """LogRecordFactory tags records from any logger with the current request_id."""
    install_request_id_log_factory()
    app = _make_probe_app()

    with caplog.at_level(logging.INFO, logger="cce.api.middleware"):
        resp = await _get(app, "/probe")
    expected = resp.headers["X-Request-ID"]
    tagged = [r for r in caplog.records if getattr(r, "request_id", None) == expected]
    # RequestLoggingMiddleware emits at least one line per request.
    assert tagged, (
        f"No log records carried request_id={expected!r}; "
        f"seen ids = {[getattr(r, 'request_id', None) for r in caplog.records]}"
    )


def test_sentinel_when_outside_request():
    """Calling get_request_id() from a non-request context returns None."""
    assert get_request_id() is None


def test_filter_emits_sentinel_when_no_request_active():
    """RequestIdFilter stamps '-' when the contextvar is unset."""
    filt = RequestIdFilter()
    rec = logging.LogRecord(
        name="cce.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=None,
        exc_info=None,
    )
    assert filt.filter(rec) is True
    assert rec.request_id == "-"
