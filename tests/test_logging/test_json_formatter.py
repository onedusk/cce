"""Tests for JsonFormatter + configure_logging (audit D2, T-04.03)."""

from __future__ import annotations

import json
import logging
from io import StringIO

import pytest

from cce.logging_config import JsonFormatter, configure_logging

pytestmark = pytest.mark.unit


@pytest.fixture
def stream_handler_root():
    """Attach a StringIO-backed handler to the root logger for this test only.

    Snapshots the root logger's handlers and their formatters before the test
    and restores them on teardown so `configure_logging`'s in-place formatter
    swap doesn't leak JsonFormatter onto shared state used by other tests.
    """
    root = logging.getLogger()
    prev_level = root.level
    prev_formatters = [(h, h.formatter) for h in root.handlers]

    buf = StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    try:
        yield buf, handler
    finally:
        root.removeHandler(handler)
        for h, fmt in prev_formatters:
            h.setFormatter(fmt)
        root.setLevel(prev_level)


def _emit(record_kwargs: dict | None = None, *, level: int = logging.INFO):
    """Emit one log line through the root logger."""
    logger = logging.getLogger("cce.test.json_formatter")
    logger.log(level, "hello %s", "world", extra=record_kwargs or {})


def test_json_formatter_shape():
    """Formatter output is valid JSON with the documented top-level keys."""
    rec = logging.LogRecord(
        name="cce.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hi %s",
        args=("there",),
        exc_info=None,
    )
    line = JsonFormatter().format(rec)
    payload = json.loads(line)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "cce.test"
    assert payload["message"] == "hi there"
    assert payload["ts"].endswith("+00:00")  # ISO UTC


def test_json_formatter_passthrough_extra_fields():
    rec = logging.LogRecord(
        name="cce.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="stage update",
        args=None,
        exc_info=None,
    )
    # Simulate `logger.info(..., extra={"stage": "write", "iteration": 2})`
    rec.stage = "write"
    rec.iteration = 2

    payload = json.loads(JsonFormatter().format(rec))
    assert payload["stage"] == "write"
    assert payload["iteration"] == 2


def test_json_formatter_excludes_reserved_stdlib_attrs():
    """Reserved LogRecord attributes (pathname, process, asctime, etc.) don't leak."""
    rec = logging.LogRecord(
        name="cce.test",
        level=logging.WARNING,
        pathname="/some/path.py",
        lineno=42,
        msg="boom",
        args=None,
        exc_info=None,
    )
    payload = json.loads(JsonFormatter().format(rec))
    for reserved in (
        "pathname",
        "process",
        "asctime",
        "lineno",
        "filename",
        "funcName",
    ):
        assert reserved not in payload, f"{reserved!r} leaked into JSON payload"


def test_json_formatter_exception_info():
    try:
        raise ValueError("kaboom")
    except ValueError:
        import sys

        exc_info = sys.exc_info()

    rec = logging.LogRecord(
        name="cce.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="failed",
        args=None,
        exc_info=exc_info,
    )
    payload = json.loads(JsonFormatter().format(rec))
    assert "exc" in payload
    assert "ValueError" in payload["exc"]
    assert "kaboom" in payload["exc"]


def test_configure_logging_default_formatter_is_plain(monkeypatch, stream_handler_root):
    """Without CCE_LOG_FORMAT, the handler keeps its (plain) formatter."""
    monkeypatch.delenv("CCE_LOG_FORMAT", raising=False)
    buf, handler = stream_handler_root
    # Handler starts with no explicit formatter (stdlib default).
    configure_logging()
    _emit()
    handler.flush()
    output = buf.getvalue()
    # Default formatter emits "hello world" somewhere — does NOT parse as JSON.
    with pytest.raises(json.JSONDecodeError):
        json.loads(output.strip().splitlines()[-1])


def test_configure_logging_json_when_env_set(monkeypatch, stream_handler_root):
    """CCE_LOG_FORMAT=json swaps the formatter on every root handler."""
    monkeypatch.setenv("CCE_LOG_FORMAT", "json")
    buf, handler = stream_handler_root
    configure_logging()
    _emit({"stage": "write"})
    handler.flush()
    line = buf.getvalue().strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["message"] == "hello world"
    assert payload["stage"] == "write"
    # request_id is always present after configure_logging() due to the factory
    assert "request_id" in payload
    assert payload["request_id"] == "-"  # no active request context


def test_configure_logging_idempotent(monkeypatch, stream_handler_root):
    """Calling configure_logging() twice is safe — no error, no duplicate tagging."""
    monkeypatch.setenv("CCE_LOG_FORMAT", "json")
    configure_logging()
    configure_logging()
    # If we got here without raising, idempotency holds. Sanity: the factory
    # still tags records once.
    buf, handler = stream_handler_root
    _emit()
    handler.flush()
    line = buf.getvalue().strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["request_id"] == "-"
