#!/usr/bin/env python3
"""Shared OpenTelemetry setup — tracing + logging bridge for ai-briefing scripts."""

import logging
import os

from opentelemetry import trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.semconv.resource import ResourceAttributes

_tracer_provider: TracerProvider | None = None
_logger_provider: LoggerProvider | None = None


def setup(service_name: str) -> trace.Tracer:
    global _tracer_provider, _logger_provider

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    resource = Resource.create({ResourceAttributes.SERVICE_NAME: service_name})

    _tracer_provider = TracerProvider(resource=resource)
    _tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
    )
    trace.set_tracer_provider(_tracer_provider)

    _logger_provider = LoggerProvider(resource=resource)
    _logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{endpoint}/v1/logs"))
    )
    set_logger_provider(_logger_provider)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, LoggingHandler)
               for h in root.handlers):
        root.addHandler(logging.StreamHandler())
    root.addHandler(LoggingHandler(level=logging.DEBUG, logger_provider=_logger_provider))

    return trace.get_tracer(service_name)


def shutdown() -> None:
    if _tracer_provider:
        _tracer_provider.force_flush()
        _tracer_provider.shutdown()
    if _logger_provider:
        _logger_provider.force_flush()
        _logger_provider.shutdown()
