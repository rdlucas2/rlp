from __future__ import annotations

import json
import logging

from opentelemetry.sdk.trace import TracerProvider

from rlp_platform import logs


def test_json_lines_with_extra_fields_and_trace_context(capsys):
    logs.configure("debug")
    log = logging.getLogger("test")
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("work"):
        log.info("hello %s", "world", extra={"user_count": 3})
    entry = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert entry["message"] == "hello world"
    assert entry["level"] == "info"
    assert entry["logger"] == "test"
    assert entry["user_count"] == 3
    assert len(entry["trace_id"]) == 32
    assert len(entry["span_id"]) == 16


def test_exceptions_are_logged(capsys):
    logs.configure("info")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logging.getLogger("test").exception("failed")
    entry = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "RuntimeError: boom" in entry["exception"]
    assert "trace_id" not in entry


def test_uvicorn_loggers_propagate_to_root():
    logging.getLogger("uvicorn.access").addHandler(logging.NullHandler())
    logs.configure("warning")
    assert logging.getLogger().level == logging.WARNING
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert logging.getLogger(name).handlers == []
        assert logging.getLogger(name).propagate
