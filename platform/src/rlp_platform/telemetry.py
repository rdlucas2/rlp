"""OpenTelemetry tracing, configured only by the standard OTEL_* environment variables.

- OTEL_SDK_DISABLED=true turns tracing off.
- OTEL_SERVICE_NAME and OTEL_RESOURCE_ATTRIBUTES name the service (the SDK reads them).
- OTEL_TRACES_EXPORTER picks the exporter: otlp (the default), console or none.
- OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_EXPORTER_OTLP_TRACES_ENDPOINT and the other
  OTEL_EXPORTER_OTLP_* variables configure the OTLP/HTTP exporter (it reads them).
  With otlp selected but no endpoint set, spans are created but not exported, so
  a laptop without a collector stays quiet.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SpanExporter

DEFAULT_SERVICE_NAME = "rlp-platform-platform"


def exporter_from_env(env: Mapping[str, str]) -> SpanExporter | None:
    choice = env.get("OTEL_TRACES_EXPORTER", "otlp").strip().lower()
    if choice == "none":
        return None
    if choice == "console":
        return ConsoleSpanExporter()
    if choice != "otlp":
        raise ValueError(f"OTEL_TRACES_EXPORTER={choice!r} is not supported; use otlp, console or none")
    if not (env.get("OTEL_EXPORTER_OTLP_ENDPOINT") or env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")):
        return None
    return OTLPSpanExporter()


def setup(app: FastAPI, env: Mapping[str, str] | None = None) -> TracerProvider | None:
    """Instrument the app. Returns the provider, or None when the SDK is disabled."""
    env = os.environ if env is None else env
    if env.get("OTEL_SDK_DISABLED", "false").strip().lower() == "true":
        return None
    resource = Resource.create({"service.name": env.get("OTEL_SERVICE_NAME") or DEFAULT_SERVICE_NAME})
    provider = TracerProvider(resource=resource)
    exporter = exporter_from_env(env)
    if exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider, excluded_urls="healthz,readyz")
    return provider
