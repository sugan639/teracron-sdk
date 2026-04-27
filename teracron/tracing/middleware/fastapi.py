# -*- coding: utf-8 -*-
"""
FastAPI / Starlette ASGI middleware for automatic request tracing.

Creates a root span per HTTP request, extracts/injects the
``X-Teracron-Trace`` header, and records ``method``, ``path``, and
``status_code`` as span metadata.

Usage::

    from fastapi import FastAPI
    from teracron.tracing.middleware.fastapi import TeracronTracingMiddleware

    app = FastAPI()
    app.add_middleware(TeracronTracingMiddleware, workflow="api")

The middleware is a thin ASGI wrapper — it does NOT import FastAPI or
Starlette at module level.  It only requires the standard ASGI interface
(``scope``, ``receive``, ``send``).

Thread-safety: each request gets its own ``contextvars`` snapshot via
the ASGI server (uvicorn/hypercorn), so traces are isolated.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from ..context import (
    clear_trace,
    get_trace_id,
    peek_parent_span_id,
    set_trace_header,
    start_trace,
)
from ..sampling import get_sampling_decision, set_sampling_decision, should_sample
from ..span import create_span, finalise_span

_TRACE_HEADER = "x-teracron-trace"


class TeracronTracingMiddleware:
    """
    ASGI middleware that auto-traces HTTP requests.

    Args:
        app:      The ASGI application.
        workflow:  Workflow name for spans (default: ``"http"``).
    """

    def __init__(
        self,
        app: Any,
        workflow: str = "http",
    ) -> None:
        self.app = app
        self.workflow = workflow

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Lazy import to avoid circular dependency at module level.
        from ...client import _singleton

        client = _singleton
        if client is None or not client.config.tracing_enabled:
            await self.app(scope, receive, send)
            return

        # Extract inbound trace header from request headers.
        headers = dict(scope.get("headers", []))
        raw_header = headers.get(_TRACE_HEADER.encode("latin-1"), b"")
        if raw_header:
            set_trace_header(raw_header.decode("latin-1", errors="replace"))

        # Start trace if none propagated.
        is_root = get_trace_id() is None
        if is_root:
            trace_id = start_trace()
        else:
            trace_id = get_trace_id()

        # Sampling decision.
        sampled = get_sampling_decision()
        if sampled is None:
            sampled = should_sample(trace_id, client.config.trace_sample_rate)
            set_sampling_decision(sampled)

        method = scope.get("method", "UNKNOWN")
        path = scope.get("path", "/")
        operation = f"{method} {path}"

        parent_span_id = peek_parent_span_id()
        span = create_span(
            workflow=self.workflow,
            operation=operation,
            trace_id=trace_id,
            parent_span_id=parent_span_id,
        )

        from ..context import push_span, pop_span
        push_span(span.span_id)

        t0 = time.monotonic()
        status_code = 500  # default if send() never called
        error_type: Optional[str] = None
        error_message: Optional[str] = None

        async def send_wrapper(message: dict) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = message.get("status", 500)
                # Inject trace header into response.
                resp_headers = list(message.get("headers", []))
                trace_header_val = trace_id
                current_span = peek_parent_span_id()
                if current_span:
                    trace_header_val = f"{trace_id}:{current_span}"
                resp_headers.append(
                    (_TRACE_HEADER.encode("latin-1"), trace_header_val.encode("latin-1"))
                )
                message = dict(message, headers=resp_headers)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            error_type = type(exc).__name__
            error_message = str(exc)[:1024]
            raise
        finally:
            pop_span()

            if sampled:
                metadata = {
                    "http.method": method,
                    "http.path": path,
                    "http.status_code": status_code,
                }
                if error_type:
                    metadata["http.error_type"] = error_type

                from ..decorator import _apply_scrubber
                scrubber = getattr(client, "_scrubber", None)
                metadata = _apply_scrubber(scrubber, metadata)

                duration_ms = (time.monotonic() - t0) * 1000.0
                status = "failed" if (status_code >= 500 or error_type) else "succeeded"
                finished = finalise_span(
                    span,
                    status=status,
                    duration_ms=duration_ms,
                    error_type=error_type,
                    error_message=error_message,
                    metadata=metadata,
                )
                client._push_trace_span(finished.to_dict())

            if is_root:
                clear_trace()
