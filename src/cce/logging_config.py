"""Logging configuration helpers (audit D2, ADR-004).

Plain-text stdlib logging is the default — fine for local dev and for
grep-style debugging. Set ``CCE_LOG_FORMAT=json`` in the environment and
every log line is emitted as a single-line JSON object suitable for
aggregators (Datadog / Loki / OpenSearch).

``configure_logging()`` is idempotent and safe to call from every entry
point (API lifespan, CLI pre-run callback, runner scripts).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as a single-line JSON object.

    Built-in fields: ``ts``, ``level``, ``logger``, ``message``, ``request_id``
    (populated by the request-id LogRecordFactory installed via
    ``cce.api.middleware.install_request_id_log_factory``).

    Pass-through: any non-reserved attribute added via ``logger.info(...,
    extra={...})`` is included verbatim, so per-call structured data reaches
    the aggregator without the caller having to know this formatter exists.
    """

    # Stdlib LogRecord attributes that are internal plumbing — not useful
    # to ship to an aggregator and liable to collide with user extras.
    _RESERVED = frozenset(
        {
            "args",
            "asctime",
            "created",
            "exc_info",
            "exc_text",
            "filename",
            "funcName",
            "levelname",
            "levelno",
            "lineno",
            "message",
            "module",
            "msecs",
            "msg",
            "name",
            "pathname",
            "process",
            "processName",
            "relativeCreated",
            "stack_info",
            "thread",
            "threadName",
            "taskName",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        # Stdlib Formatter sets record.message = record.getMessage() as a
        # side effect of format() — mirror that here so downstream code /
        # test helpers that read record.message after logging keep working.
        record.message = record.getMessage()
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.message,
        }
        if hasattr(record, "request_id"):
            payload["request_id"] = record.request_id
        # Pass-through `extra={}` fields
        for k, v in record.__dict__.items():
            if k in self._RESERVED or k.startswith("_") or k == "request_id":
                continue
            payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    """Install the request-id LogRecordFactory and optional JSON formatter.

    Idempotent — safe to call at every entry point. When
    ``CCE_LOG_FORMAT=json`` is set in the environment, every existing root
    handler has its formatter swapped to ``JsonFormatter``. When unset, the
    stdlib default formatter is left in place (including anything a runner
    script configured via ``logging.basicConfig``).

    The request-id factory is ALWAYS installed — harmless on non-API paths
    (records just carry ``request_id="-"``) and essential on API paths.
    """
    # Deferred import to avoid a logging_config <-> api.middleware cycle at
    # module-load time. Safe: this function is only ever called at runtime.
    from cce.api.middleware import install_request_id_log_factory

    install_request_id_log_factory()

    root = logging.getLogger()
    # If nothing has configured a handler yet, install a default so our
    # formatter swap below has something to attach to.
    if not root.handlers:
        logging.basicConfig(level=logging.INFO)

    if os.getenv("CCE_LOG_FORMAT", "").strip().lower() == "json":
        formatter = JsonFormatter()
        for handler in root.handlers:
            handler.setFormatter(formatter)
