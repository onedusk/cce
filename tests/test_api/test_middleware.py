"""Tests for API request logging middleware."""

from __future__ import annotations

import logging
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from cce.api.middleware import RequestLoggingMiddleware


@pytest.fixture
def app_with_middleware() -> FastAPI:
    """Minimal FastAPI app with request logging middleware."""
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/ok")
    async def ok():
        return {"status": "ok"}

    @app.get("/not-found-route")
    async def not_found():
        return JSONResponse(status_code=404, content={"error": "not found"})

    @app.get("/boom")
    async def boom():
        raise RuntimeError("unhandled")

    return app


class TestRequestLoggingMiddleware:
    def test_success_request_logged(self, app_with_middleware, caplog):
        client = TestClient(app_with_middleware)

        with caplog.at_level(logging.INFO, logger="cce.api.middleware"):
            client.get("/ok")

        assert len(caplog.records) >= 1
        log_msg = caplog.records[-1].message
        assert "GET" in log_msg
        assert "/ok" in log_msg
        assert "200" in log_msg
        assert "ms" in log_msg

    def test_error_status_logged(self, app_with_middleware, caplog):
        client = TestClient(app_with_middleware)

        with caplog.at_level(logging.INFO, logger="cce.api.middleware"):
            client.get("/not-found-route")

        log_msg = caplog.records[-1].message
        assert "404" in log_msg
        assert "/not-found-route" in log_msg

    def test_duration_is_positive(self, app_with_middleware, caplog):
        client = TestClient(app_with_middleware)

        with caplog.at_level(logging.INFO, logger="cce.api.middleware"):
            client.get("/ok")

        log_msg = caplog.records[-1].message
        # Extract the duration number before "ms"
        match = re.search(r"(\d+)ms", log_msg)
        assert match is not None
        duration = int(match.group(1))
        assert duration >= 0

    def test_exception_still_logged(self, app_with_middleware, caplog):
        client = TestClient(app_with_middleware, raise_server_exceptions=False)

        with caplog.at_level(logging.INFO, logger="cce.api.middleware"):
            response = client.get("/boom")

        assert response.status_code == 500
        log_msg = caplog.records[-1].message
        assert "GET" in log_msg
        assert "/boom" in log_msg
        assert "500" in log_msg
