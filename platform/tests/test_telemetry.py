from __future__ import annotations

import pytest
from fastapi import FastAPI
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace.export import ConsoleSpanExporter

from rlp_platform import telemetry


def test_disabled_sdk_sets_up_nothing():
    assert telemetry.setup(FastAPI(), {"OTEL_SDK_DISABLED": "true"}) is None


def test_service_name_defaults_to_project_and_component():
    provider = telemetry.setup(FastAPI(), {"OTEL_TRACES_EXPORTER": "none"})
    assert provider.resource.attributes["service.name"] == "rlp-platform-platform"


def test_service_name_from_environment():
    provider = telemetry.setup(FastAPI(), {"OTEL_TRACES_EXPORTER": "none", "OTEL_SERVICE_NAME": "custom"})
    assert provider.resource.attributes["service.name"] == "custom"


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"OTEL_TRACES_EXPORTER": "none"}, type(None)),
        ({"OTEL_TRACES_EXPORTER": "console"}, ConsoleSpanExporter),
        ({}, type(None)),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"}, OTLPSpanExporter),
        ({"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://collector:4318/v1/traces"}, OTLPSpanExporter),
    ],
)
def test_exporter_from_env(env, expected):
    assert isinstance(telemetry.exporter_from_env(env), expected)


def test_unknown_exporter_is_an_error():
    with pytest.raises(ValueError, match="not supported"):
        telemetry.exporter_from_env({"OTEL_TRACES_EXPORTER": "zipkin"})


def test_exporting_provider_has_a_span_processor():
    provider = telemetry.setup(FastAPI(), {"OTEL_TRACES_EXPORTER": "console"})
    assert provider._active_span_processor._span_processors
    provider.shutdown()
