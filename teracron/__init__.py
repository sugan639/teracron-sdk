# -*- coding: utf-8 -*-
"""
Teracron SDK for Python -- encrypted metrics & workflow tracing agent.

Quick start (one line)::

    import teracron
    teracron.up()

That's it. Reads ``TERACRON_API_KEY`` from your environment, spawns a
background daemon thread, and starts shipping encrypted metrics. Shutdown
is automatic via ``atexit``.

Workflow tracing::

    from teracron import trace

    @trace("payment")
    def create_order(cart):
        ...

    @trace("payment", capture=["order_id", "amount"])
    def charge_card(order_id, amount):
        ...

Context manager tracing::

    from teracron import trace_context

    with trace_context("payment", operation="validate") as span:
        span.set_metadata({"order_id": "ORD-123"})
        ...

Cross-process propagation::

    from teracron import get_trace_header, set_trace_header

    headers["X-Teracron-Trace"] = get_trace_header()
    set_trace_header(request.headers.get("X-Teracron-Trace"))

Sampling & PII scrubbing::

    def my_scrubber(data: dict) -> dict:
        data.pop("email", None)
        return data

    teracron.up(
        trace_sample_rate=0.5,
        tracing_scrubber=my_scrubber,
    )

Framework middleware::

    from teracron.tracing.middleware.fastapi import TeracronTracingMiddleware
    app.add_middleware(TeracronTracingMiddleware, workflow="api")

Explicit shutdown::

    teracron.down()

Standalone CLI agent::

    $ export TERACRON_API_KEY="tcn_..."
    $ teracron-agent
"""

__version__ = "0.6.0"

from .client import TeracronClient, up, down
from .apikey import encode_api_key, decode_api_key
from .auth import login, logout, whoami, resolve_api_key
from .query import TeracronQueryClient
from .simulate import FailureSimulator
from .tracing import (
    trace,
    trace_context,
    async_trace_context,
    SpanHandle,
    get_trace_header,
    set_trace_header,
)
from .types import (
    AuthToken,
    FlushResult,
    MetricsSnapshot,
    ResolvedConfig,
    SimulationResult,
    Span,
    TraceFlushResult,
    WorkflowEvent,
    WorkflowRun,
)

__all__ = [
    # Primary API -- one call to start
    "up",
    "down",
    # Tracing — decorator
    "trace",
    # Tracing — context managers
    "trace_context",
    "async_trace_context",
    "SpanHandle",
    # Tracing — cross-process propagation
    "get_trace_header",
    "set_trace_header",
    # Auth (Phase 4)
    "login",
    "logout",
    "whoami",
    "resolve_api_key",
    # Query (Phase 4)
    "TeracronQueryClient",
    # Simulation (Phase 4)
    "FailureSimulator",
    # Advanced / explicit
    "TeracronClient",
    # Types
    "AuthToken",
    "FlushResult",
    "MetricsSnapshot",
    "ResolvedConfig",
    "SimulationResult",
    "Span",
    "TraceFlushResult",
    "WorkflowEvent",
    "WorkflowRun",
    # Utilities
    "encode_api_key",
    "decode_api_key",
    "__version__",
]
