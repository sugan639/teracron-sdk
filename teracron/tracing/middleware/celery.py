# -*- coding: utf-8 -*-
"""
Celery signal hooks for automatic task tracing.

Propagates trace context through task headers and auto-creates a span
per task execution.

Usage::

    from celery import Celery
    from teracron.tracing.middleware.celery import setup_celery_tracing

    app = Celery("tasks")
    setup_celery_tracing(app, workflow="tasks")

Hooks:
    - ``before_task_publish``: Injects ``X-Teracron-Trace`` into task headers.
    - ``task_prerun``: Restores trace context + creates task span.
    - ``task_postrun``: Finalises span.
    - ``task_failure``: Records error info on the span.

Thread-safety: uses ``contextvars`` — Celery prefork workers get isolated
contexts per task. Celery's ``solo`` pool (single-thread) also works
because each task runs sequentially.

Does NOT import Celery at module level — uses signal-based duck-typing.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from ..context import (
    clear_trace,
    get_trace_header,
    get_trace_id,
    peek_parent_span_id,
    pop_span,
    push_span,
    set_trace_header,
    start_trace,
)
from ..sampling import get_sampling_decision, set_sampling_decision, should_sample
from ..span import create_span, finalise_span

_TRACE_HEADER_KEY = "X-Teracron-Trace"

# Task-local storage for active span state (keyed by task_id to handle
# concurrent tasks in eventlet/gevent pools).
_active_spans: Dict[str, dict] = {}


def setup_celery_tracing(
    app: Any,
    *,
    workflow: str = "celery",
) -> None:
    """
    Connect Celery signals for automatic task tracing.

    Args:
        app:      Celery application instance.
        workflow:  Workflow name for task spans (default: ``"celery"``).
    """

    def _on_before_publish(
        sender: Any = None,
        headers: Optional[dict] = None,
        **kwargs: Any,
    ) -> None:
        """Inject trace header into outbound task headers."""
        if headers is None:
            return
        header_val = get_trace_header()
        if header_val:
            headers[_TRACE_HEADER_KEY] = header_val

    def _on_task_prerun(
        sender: Any = None,
        task_id: Optional[str] = None,
        task: Any = None,
        **kwargs: Any,
    ) -> None:
        """Restore trace context and start a span for the task."""
        from ...client import _singleton

        client = _singleton
        if client is None or not client.config.tracing_enabled:
            return
        if task_id is None:
            return

        # Restore trace context from task headers if available.
        request = getattr(task, "request", None)
        if request is not None:
            raw_header = None
            # Celery stores custom headers in request.get() or request.headers
            if hasattr(request, "get"):
                raw_header = request.get(_TRACE_HEADER_KEY)
            if raw_header is None and hasattr(request, "headers"):
                headers = getattr(request, "headers", None)
                if isinstance(headers, dict):
                    raw_header = headers.get(_TRACE_HEADER_KEY)
            if raw_header:
                set_trace_header(raw_header)

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

        task_name = getattr(task, "name", None) or str(sender)
        parent_span_id = peek_parent_span_id()

        span = create_span(
            workflow=workflow,
            operation=task_name,
            trace_id=trace_id,
            parent_span_id=parent_span_id,
        )
        push_span(span.span_id)

        _active_spans[task_id] = {
            "span": span,
            "is_root": is_root,
            "t0": time.monotonic(),
            "sampled": sampled,
            "error_type": None,
            "error_message": None,
        }

    def _on_task_failure(
        sender: Any = None,
        task_id: Optional[str] = None,
        exception: Optional[Exception] = None,
        **kwargs: Any,
    ) -> None:
        """Record error info on the active span."""
        if task_id is None or task_id not in _active_spans:
            return
        state = _active_spans[task_id]
        if exception is not None:
            state["error_type"] = type(exception).__name__
            state["error_message"] = str(exception)[:1024]

    def _on_task_postrun(
        sender: Any = None,
        task_id: Optional[str] = None,
        retval: Any = None,
        state: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """Finalise the task span."""
        from ...client import _singleton

        client = _singleton
        if client is None or task_id is None or task_id not in _active_spans:
            return

        span_state = _active_spans.pop(task_id)
        pop_span()

        sampled = span_state["sampled"]
        if not sampled:
            if span_state["is_root"]:
                clear_trace()
            return

        duration_ms = (time.monotonic() - span_state["t0"]) * 1000.0
        error_type = span_state["error_type"]
        error_message = span_state["error_message"]
        status = "failed" if error_type else "succeeded"

        metadata = {"celery.task_id": task_id}
        if state:
            metadata["celery.state"] = str(state)

        from ..decorator import _apply_scrubber
        scrubber = getattr(client, "_scrubber", None)
        metadata = _apply_scrubber(scrubber, metadata)

        finished = finalise_span(
            span_state["span"],
            status=status,
            duration_ms=duration_ms,
            error_type=error_type,
            error_message=error_message,
            metadata=metadata,
        )
        client._push_trace_span(finished.to_dict())

        if span_state["is_root"]:
            clear_trace()

    # Connect signals — use weak=False to prevent GC of lambdas.
    try:
        from celery.signals import (
            before_task_publish,
            task_failure,
            task_postrun,
            task_prerun,
        )
        before_task_publish.connect(_on_before_publish, weak=False)
        task_prerun.connect(_on_task_prerun, weak=False)
        task_failure.connect(_on_task_failure, weak=False)
        task_postrun.connect(_on_task_postrun, weak=False)
    except ImportError:
        raise ImportError(
            "[Teracron] Celery is required for setup_celery_tracing(). "
            "Install it with: pip install celery"
        )
