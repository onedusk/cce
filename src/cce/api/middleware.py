"""Request logging + request-ID middleware for the CCE API."""

from __future__ import annotations

import contextvars
import logging
import time
import uuid

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from cce.api.schemas import error_envelope

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 1_048_576  # 1 MiB — finding 5.1


def install_body_size_limit(app: FastAPI, max_bytes: int = MAX_BODY_BYTES) -> None:
    """Reject oversized requests before parsing (413). Content-Length
    check only — chunked bodies are bounded downstream by uvicorn's
    h11 max-incomplete-size; documented limitation, acceptable pre-1.0."""

    @app.middleware("http")
    async def _limit_body(request: Request, call_next):  # type: ignore[no-untyped-def]
        declared = request.headers.get("content-length")
        if declared is not None and int(declared) > max_bytes:
            return JSONResponse(
                status_code=413,
                content=error_envelope(
                    code="payload_too_large",
                    message=f"Request body exceeds {max_bytes} bytes",
                ).model_dump(mode="json"),
            )
        return await call_next(request)


# --- Request-ID correlation (audit U1, ADR-003) ---------------------------
# Every inbound request carries a short, log-friendly correlation ID. The ID
# lives on a contextvars.ContextVar so any code in the request's async
# subtree (handlers, pipeline, LLM retry, etc.) can read it without being
# passed a parameter. Log records pick it up via RequestIdFilter.

_request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "cce_request_id", default=None
)


def get_request_id() -> str | None:
    """Return the current request's correlation ID, or None outside a request."""
    return _request_id_var.get()


def _new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:12]}"


class RequestIdFilter(logging.Filter):
    """Inject the current request_id onto a LogRecord.

    Useful as a unit of logic and as a filter attached to a specific handler.
    Prefer ``install_request_id_log_factory()`` for process-wide injection —
    that mechanism runs on every record regardless of which logger produced
    it; ``logging.Logger.addFilter`` is local to one logger and does NOT
    apply to records from its children.

    Emits a `-` sentinel when no request is active so downstream formatters
    can always read ``record.request_id`` safely.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        record.request_id = _request_id_var.get() or "-"
        return True


_LOG_FACTORY_INSTALLED = False


def install_request_id_log_factory() -> None:
    """Tag every LogRecord with ``request_id`` via logging.setLogRecordFactory.

    Idempotent — safe to call during repeated app construction in tests.
    Unlike ``Logger.addFilter`` on the root logger, a LogRecordFactory runs
    when *any* logger creates a record, so child-logger emissions are also
    tagged.
    """
    global _LOG_FACTORY_INSTALLED
    if _LOG_FACTORY_INSTALLED:
        return
    original = logging.getLogRecordFactory()

    def _factory(*args, **kwargs):  # type: ignore[no-untyped-def]
        record = original(*args, **kwargs)
        record.request_id = _request_id_var.get() or "-"
        return record

    logging.setLogRecordFactory(_factory)
    _LOG_FACTORY_INSTALLED = True


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Assign a correlation ID to every inbound request.

    The ID is:
      - taken from an inbound ``X-Request-ID`` header if one is present
        (allows a caller / upstream proxy to supply its own), otherwise
        generated locally as ``req_<12 hex chars>``;
      - stashed on ``request.state.request_id`` for handler access;
      - set into the contextvar so the pipeline + log filter see it;
      - echoed back on the response as ``X-Request-ID`` so an operator
        reading a curl response can copy it into a bug report.

    Register this middleware AFTER ``RequestLoggingMiddleware`` so it
    becomes the outermost wrapper — the contextvar must be set before
    the logging middleware's dispatch() emits its first log line.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = request.headers.get("x-request-id") or _new_request_id()
        token = _request_id_var.set(request_id)
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        finally:
            _request_id_var.reset(token)
        response.headers["X-Request-ID"] = request_id
        return response


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log method, path, status code, and duration for every request."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        start = time.monotonic()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception:
            raise
        finally:
            duration_ms = (time.monotonic() - start) * 1000
            logger.info(
                "%s %s %d %.0fms",
                request.method,
                request.url.path,
                status_code,
                duration_ms,
            )
