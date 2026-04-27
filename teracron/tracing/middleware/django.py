# -*- coding: utf-8 -*-
"""
Django WSGI middleware for automatic request tracing.

Creates a root span per HTTP request, extracts/injects the
``X-Teracron-Trace`` header, and records ``method``, ``path``, and
``status_code`` as span metadata.

Usage::

    # settings.py
    MIDDLEWARE = [
        "teracron.tracing.middleware.django.TeracronTracingMiddleware",
        # ... other middleware
    ]

    # Or configure the workflow name:
    TERACRON_WORKFLOW = "api"

The middleware follows Django's standard interface — ``__init__`` receives
``get_response``, ``__call__`` wraps the request/response cycle.

Does NOT import Django at module level — uses duck-typing on the
request/response objects.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from ..context import (
    clear_trace,
    get_trace_id,
    peek_parent_span_id,
    push_span,
    pop_span,
    set_trace_header,
    start_trace,
)
from ..sampling import get_sampling_decision, set_sampling_decision, should_sample
from ..span import create_span, finalise_span

_TRACE_HEADER_NAME = "X-Teracron-Trace"
_DJANGO_META_KEY = "HTTP_X_TERACRON_TRACE"  # Django converts headers to META keys


class TeracronTracingMiddleware:
    """
    Django middleware that auto-traces HTTP requests.

    Args:
        get_response: The next middleware/view in the chain (Django convention).
    """

    def __init__(self, get_response: Callable) -> None:
        self.get_response = get_response
        # Allow workflow name configuration via Django settings.
        self.workflow = "http"
        try:
            from django.conf import settings
            self.workflow = getattr(settings, "TERACRON_WORKFLOW", "http")
        except Exception:  # nosec B110
            pass

    def __call__(self, request: Any) -> Any:
        # Lazy import to avoid circular dependency.
        from ...client import _singleton

        client = _singleton
        if client is None or not client.config.tracing_enabled:
            return self.get_response(request)

        # Extract inbound trace header.
        raw_header = getattr(request, "META", {}).get(_DJANGO_META_KEY, "")
        if raw_header:
            set_trace_header(raw_header)

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

        method = getattr(request, "method", "UNKNOWN")
        path = getattr(request, "path", "/")
        operation = f"{method} {path}"

        parent_span_id = peek_parent_span_id()
        span = create_span(
            workflow=self.workflow,
            operation=operation,
            trace_id=trace_id,
            parent_span_id=parent_span_id,
        )

        push_span(span.span_id)
        t0 = time.monotonic()

        error_type: Optional[str] = None
        error_message: Optional[str] = None
        status_code = 500

        try:
            response = self.get_response(request)
            status_code = getattr(response, "status_code", 200)
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

        # Inject trace header into response.
        if hasattr(response, "__setitem__"):
            header_val = trace_id
            current_parent = peek_parent_span_id()
            if current_parent:
                header_val = f"{trace_id}:{current_parent}"
            response[_TRACE_HEADER_NAME] = header_val

        return response
