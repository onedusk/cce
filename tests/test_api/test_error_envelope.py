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


# ---------------------------------------------------------------------------
# Framework errors use the envelope too (CR-07)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_unknown_route_is_an_envelope(client: httpx.AsyncClient):
    resp = await client.get("/v1/curate/nope")
    assert resp.status_code == 404
    err = resp.json()["error"]
    assert err["code"] == "not_found"
    assert err["request_id"] == resp.headers["X-Request-ID"]


@pytest.mark.integration
async def test_wrong_method_is_an_envelope(client: httpx.AsyncClient):
    resp = await client.put("/v1/curate/jobs")
    assert resp.status_code == 405
    assert resp.json()["error"]["code"] == "method_not_allowed"
    assert "allow" in resp.headers


@pytest.mark.integration
async def test_query_validation_is_an_envelope(client: httpx.AsyncClient):
    resp = await client.get("/v1/curate/jobs?limit=0")
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "invalid_request"
    assert "query.limit" in err["message"]
    assert err["request_id"] == resp.headers["X-Request-ID"]


@pytest.mark.integration
async def test_body_validation_names_the_field_without_echoing_input(
    client: httpx.AsyncClient,
):
    """Pinned context can be confidential: a context item the request model
    rejects is named by location, and its content is not sent back."""
    from tests.conftest import make_evidence

    item = make_evidence(id="ctx_a", excerpt="Confidential pinned text.")
    payload = item.model_dump(mode="json")
    del payload["retrieved_at"]
    resp = await client.post(
        "/v1/curate/jobs",
        json={
            "topic": "test topic",
            "paths": ["blog"],
            "policy_id": "test-policy",
            "context": [payload],
        },
    )
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "invalid_request"
    assert "body.context.0.retrieved_at" in err["message"]
    assert "Confidential" not in resp.text


@pytest.mark.unit
def test_openapi_documents_job_statuses_and_error_responses(test_config):
    from cce.api.app import create_app

    schema = create_app(test_config).openapi()
    components = schema["components"]["schemas"]
    job_status = components["JobResponse"]["properties"]["status"]
    statuses = components[job_status["$ref"].rsplit("/", 1)[-1]]["enum"]
    assert "ready_for_approval" in statuses
    assert "review_required" in statuses

    def errors(method: str, path: str) -> set[str]:
        responses = schema["paths"][path][method]["responses"]
        return {status for status in responses if status[0] in "45"}

    # 401 and 422 are on every authed route; the rest are per route.
    common = {"401", "422"}
    assert errors("post", "/v1/curate/jobs") == common | {"400", "404", "413"}
    assert errors("get", "/v1/curate/jobs") == common | {"400"}
    assert errors("get", "/v1/curate/jobs/{job_id}") == common | {"404"}
    assert errors("delete", "/v1/curate/jobs/{job_id}") == common | {"404"}
    assert errors("post", "/v1/curate/jobs/{job_id}/retry") == common | {"404", "409"}
    assert errors("get", "/v1/curate/jobs/{job_id}/package") == common | {"404"}
    assert errors("get", "/v1/curate/evidence/{evidence_id}") == common | {"404"}
    assert errors("get", "/v1/curate/evidence") == common
    assert errors("get", "/v1/curate/health") == set()
    assert errors("get", "/v1/curate/meta") == set()
    # The framework's {"detail": [...]} 422 shape is no longer returned.
    assert "HTTPValidationError" not in components
