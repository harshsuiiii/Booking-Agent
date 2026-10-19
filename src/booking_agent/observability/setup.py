"""
OpenTelemetry tracing setup.

Call configure_tracing() once at app startup. If no OTLP endpoint is provided
the provider still runs (spans are created) but no data is exported, which is
correct for local dev and unit tests.
"""

import logging

from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider

_log = logging.getLogger(__name__)


def configure_tracing(
    service_name: str = "booking-agent",
    otlp_endpoint: str | None = None,
) -> None:
    """
    Configure the global OTel TracerProvider.

    If *otlp_endpoint* is given (e.g. "http://collector:4317"), spans are
    exported via OTLP/gRPC using a BatchSpanProcessor.  The exporter is an
    optional dependency (opentelemetry-exporter-otlp-proto-grpc); a missing
    import is logged as a warning and the provider runs without export.
    """
    resource = Resource.create({SERVICE_NAME: service_name})
    provider = TracerProvider(resource=resource)

    if otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # type: ignore[import-not-found]
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
            provider.add_span_processor(BatchSpanProcessor(exporter))
            _log.info("OTel tracing: OTLP export enabled → %s", otlp_endpoint)
        except ImportError:
            _log.warning(
                "OTel OTLP exporter not installed — spans will not be exported. "
                "Install opentelemetry-exporter-otlp-proto-grpc to enable export."
            )
    else:
        _log.debug("OTel tracing: no OTLP endpoint configured — spans created but not exported")

    trace.set_tracer_provider(provider)


def get_tracer(name: str) -> trace.Tracer:
    """Convenience wrapper — always returns the configured tracer."""
    return trace.get_tracer(name)
