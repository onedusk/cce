"""Tests for ErrorBody / reshaped APIEnvelope / error_envelope helper (audit U1, T-04.01)."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from cce.api.middleware import RequestIdMiddleware, install_request_id_log_factory
from cce.api.schemas import APIEnvelope, ErrorBody, envelope, error_envelope

pytestmark_unit = pytest.mark.unit


# ---------------------------------------------------------------------------
# Unit-level: ErrorBody + APIEnvelope shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_error_body_serialization_minimal():
    assert ErrorBody(code="x").model_dump() == {
        "code": "x",
        "message": None,
        "request_id": None,
    }


@pytest.mark.unit
def test_error_body_serialization_full():
    body = ErrorBody(code="not_found", message="missing", request_id="req_abc123")
    assert body.model_dump() == {
        "code": "not_found",
        "message": "missing",
        "request_id": "req_abc123",
    }


@pytest.mark.unit
def test_envelope_error_accepts_error_body():
    env = APIEnvelope(error=ErrorBody(code="x"))
    # Roundtrip through JSON must keep the nested shape.
    dumped = env.model_dump()
    assert dumped["error"] == {"code": "x", "message": None, "request_id": None}


@pytest.mark.unit
def test_envelope_error_rejects_string():
    """Confirms the shape change actually happened — legacy string form no longer valid."""
    with pytest.raises(ValidationError):
        APIEnvelope(error="legacy string form")


@pytest.mark.unit
def test_error_envelope_helper_populates_fields():
    env = error_envelope(
        code="not_found", message="Thing missing", request_id="req_xyz"
    )
    assert env.error is not None
    assert env.error.code == "not_found"
    assert env.error.message == "Thing missing"
    assert env.error.request_id == "req_xyz"
    assert "timestamp" in env.meta


@pytest.mark.unit
def test_envelope_passes_through_error_body():
    """The underlying envelope() helper still accepts an ErrorBody for callers
    that construct one explicitly (e.g. when re-wrapping an existing body)."""
    env = envelope(error=ErrorBody(code="x", message="y"))
    assert env.error is not None
    assert env.error.code == "x"
    assert env.error.message == "y"


# ---------------------------------------------------------------------------
# Integration: request_id flows from middleware into the error body
# ---------------------------------------------------------------------------


def _make_probe_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)
    install_request_id_log_factory()

    @app.exception_handler(HTTPException)
    async def _http_exc(request, exc: HTTPException):  # type: ignore[no-untyped-def]
        from fastapi.responses import JSONResponse

        from cce.api.middleware import get_request_id

        return JSONResponse(
            status_code=exc.status_code,
            content=error_envelope(
                code="test_error",
                message=str(exc.detail),
                request_id=get_request_id(),
            ).model_dump(mode="json"),
        )

    @app.get("/boom")
    async def boom() -> None:
        raise HTTPException(status_code=418, detail="I'm a teapot")

    return app


@pytest.mark.integration
async def test_error_body_carries_request_id():
    """The in-flight request_id from the middleware reaches the error body."""
    app = _make_probe_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/boom")

    assert resp.status_code == 418
    body = resp.json()
    assert body["error"]["code"] == "test_error"
    assert body["error"]["request_id"] == resp.headers["X-Request-ID"]
    assert body["error"]["request_id"].startswith("req_")
