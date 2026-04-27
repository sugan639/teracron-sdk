# -*- coding: utf-8 -*-
"""
Teracron Workflow Tracing — public API.

Phase 3 exports: decorator, context managers, cross-process propagation,
and sampling utilities.

Usage::

    from teracron.tracing import trace, trace_context, async_trace_context
    from teracron.tracing import get_trace_header, set_trace_header

    @trace("payment")
    def create_order(cart):
        ...

    @trace("payment", capture=["order_id", "amount"])
    def charge_card(order_id, amount):
        ...

    with trace_context("payment", operation="validate") as span:
        span.set_metadata({"order_id": "ORD-123"})
        ...

    # Cross-process propagation
    headers["X-Teracron-Trace"] = get_trace_header()
    set_trace_header(request.headers.get("X-Teracron-Trace"))

Framework middleware (import separately)::

    from teracron.tracing.middleware.fastapi import TeracronTracingMiddleware
    from teracron.tracing.middleware.django import TeracronTracingMiddleware
    from teracron.tracing.middleware.celery import setup_celery_tracing
"""

from .context import get_trace_header, set_trace_header
from .decorator import SpanHandle, async_trace_context, trace, trace_context
from .events import EventBuffer, build_event
from .sampling import should_sample

__all__ = [
    "trace",
    "trace_context",
    "async_trace_context",
    "SpanHandle",
    "get_trace_header",
    "set_trace_header",
    "should_sample",
    "EventBuffer",
    "build_event",
]
