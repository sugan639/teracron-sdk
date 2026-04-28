# -*- coding: utf-8 -*-
"""
Structured workflow event emitter — discrete moments in a workflow lifecycle.

Events are distinct from spans:
    - **Spans** represent durations (start → end).
    - **Events** represent discrete moments (step_started, step_failed, retry, etc.)

Events are buffered alongside traces and shipped to ``POST /api/v1/events``.
Auto-emitted by the ``@trace`` decorator when ``trace_emit_events=True``.

Event types:
    - ``workflow_started``  — root span begins
    - ``workflow_completed`` — root span ends successfully
    - ``workflow_failed``   — root span ends with error
    - ``step_started``      — child span begins
    - ``step_completed``    — child span ends successfully
    - ``step_failed``       — child span ends with error
    - ``retry``             — a step is being retried

SECURITY:
    - Events carry trace_id and span_id for correlation, but NO parameter values.
    - Error messages are truncated to 512 chars.
    - No PII is emitted in events — only structural workflow data.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

# Maximum error message length in events.
_EVENT_ERROR_MAX_LEN = 512

# Allowed event types — strict enum for validation.
VALID_EVENT_TYPES = frozenset(
    {
        "workflow_started",
        "workflow_completed",
        "workflow_failed",
        "step_started",
        "step_completed",
        "step_failed",
        "retry",
    }
)

# Allowed severity levels.
VALID_SEVERITIES = frozenset({"info", "warning", "error", "critical"})


def build_event(
    *,
    workflow: str,
    event_type: str,
    trace_id: str = "",
    span_id: str = "",
    operation: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    severity: str = "info",
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Build a structured workflow event dict.

    Returns ``None`` if the event_type is invalid (defense in depth).

    Args:
        workflow:      Logical workflow name.
        event_type:    One of ``VALID_EVENT_TYPES``.
        trace_id:      Trace correlation ID.
        span_id:       Span correlation ID.
        operation:     Function/operation name.
        metadata:      Optional contextual metadata (no PII).
        severity:      ``info``, ``warning``, ``error``, ``critical``.
        error_type:    Exception class name (failure events only).
        error_message: Exception message (truncated to 512 chars).

    Returns:
        Structured event dict or ``None`` on invalid input.
    """
    if event_type not in VALID_EVENT_TYPES:
        return None

    safe_severity = severity if severity in VALID_SEVERITIES else "info"

    safe_error_message = error_message
    if safe_error_message and len(safe_error_message) > _EVENT_ERROR_MAX_LEN:
        safe_error_message = safe_error_message[:_EVENT_ERROR_MAX_LEN]

    # Sanitise metadata: only primitives, max 16 keys.
    safe_metadata: Optional[Dict[str, Any]] = None
    if metadata and isinstance(metadata, dict):
        safe_metadata = {}
        for k, v in metadata.items():
            if len(safe_metadata) >= 16:
                break
            if isinstance(k, str) and isinstance(v, (str, int, float, bool)):
                safe_metadata[k] = v
        if not safe_metadata:
            safe_metadata = None

    event: Dict[str, Any] = {
        "type": event_type,
        "workflow": workflow,
        "trace_id": trace_id,
        "span_id": span_id,
        "operation": operation,
        "severity": safe_severity,
        "timestamp": int(time.time() * 1000),
    }

    if safe_error_message:
        event["error_type"] = error_type or "Unknown"
        event["error_message"] = safe_error_message
    if safe_metadata:
        event["metadata"] = safe_metadata

    return event


def build_workflow_started_event(
    *,
    workflow: str,
    trace_id: str,
    span_id: str,
    operation: str,
) -> Optional[Dict[str, Any]]:
    """Convenience: build a ``workflow_started`` event."""
    return build_event(
        workflow=workflow,
        event_type="workflow_started",
        trace_id=trace_id,
        span_id=span_id,
        operation=operation,
        severity="info",
    )


def build_workflow_completed_event(
    *,
    workflow: str,
    trace_id: str,
    span_id: str,
    operation: str,
    duration_ms: float,
) -> Optional[Dict[str, Any]]:
    """Convenience: build a ``workflow_completed`` event."""
    return build_event(
        workflow=workflow,
        event_type="workflow_completed",
        trace_id=trace_id,
        span_id=span_id,
        operation=operation,
        severity="info",
        metadata={"duration_ms": round(duration_ms, 2)},
    )


def build_workflow_failed_event(
    *,
    workflow: str,
    trace_id: str,
    span_id: str,
    operation: str,
    error_type: str,
    error_message: str,
    duration_ms: float,
) -> Optional[Dict[str, Any]]:
    """Convenience: build a ``workflow_failed`` event."""
    return build_event(
        workflow=workflow,
        event_type="workflow_failed",
        trace_id=trace_id,
        span_id=span_id,
        operation=operation,
        severity="error",
        error_type=error_type,
        error_message=error_message,
        metadata={"duration_ms": round(duration_ms, 2)},
    )


def build_step_started_event(
    *,
    workflow: str,
    trace_id: str,
    span_id: str,
    operation: str,
) -> Optional[Dict[str, Any]]:
    """Convenience: build a ``step_started`` event."""
    return build_event(
        workflow=workflow,
        event_type="step_started",
        trace_id=trace_id,
        span_id=span_id,
        operation=operation,
        severity="info",
    )


def build_step_completed_event(
    *,
    workflow: str,
    trace_id: str,
    span_id: str,
    operation: str,
    duration_ms: float,
) -> Optional[Dict[str, Any]]:
    """Convenience: build a ``step_completed`` event."""
    return build_event(
        workflow=workflow,
        event_type="step_completed",
        trace_id=trace_id,
        span_id=span_id,
        operation=operation,
        severity="info",
        metadata={"duration_ms": round(duration_ms, 2)},
    )


def build_step_failed_event(
    *,
    workflow: str,
    trace_id: str,
    span_id: str,
    operation: str,
    error_type: str,
    error_message: str,
    duration_ms: float,
) -> Optional[Dict[str, Any]]:
    """Convenience: build a ``step_failed`` event."""
    return build_event(
        workflow=workflow,
        event_type="step_failed",
        trace_id=trace_id,
        span_id=span_id,
        operation=operation,
        severity="error",
        error_type=error_type,
        error_message=error_message,
        metadata={"duration_ms": round(duration_ms, 2)},
    )


class EventBuffer:
    """
    Thread-safe bounded buffer for workflow events.

    Ring buffer with drop-oldest policy — same design as the trace buffer.
    Events are JSON-serialised for transport.
    """

    __slots__ = ("_buffer", "_capacity", "_lock", "_dropped", "_warned")

    def __init__(self, capacity: int = 500) -> None:
        import collections
        import threading

        self._capacity = max(10, min(capacity, 10_000))
        self._buffer: collections.deque = collections.deque(maxlen=self._capacity)
        self._lock = threading.Lock()
        self._dropped = 0
        self._warned = False

    def push(self, event: Dict[str, Any]) -> None:
        """Append an event. Drops oldest if full."""
        if event is None:
            return
        with self._lock:
            if len(self._buffer) >= self._capacity:
                self._dropped += 1
                if not self._warned:
                    self._warned = True
                    import sys

                    sys.stderr.write(
                        "[teracron] Warning: event buffer full — dropping oldest events.\n"
                    )
            self._buffer.append(event)

    def drain(self, max_items: int = 100) -> List[Dict[str, Any]]:
        """Drain up to ``max_items`` events from the buffer."""
        with self._lock:
            count = min(len(self._buffer), max(1, max_items))
            batch: List[Dict[str, Any]] = []
            for _ in range(count):
                if self._buffer:
                    batch.append(self._buffer.popleft())
            return batch

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._buffer)

    @property
    def dropped_count(self) -> int:
        return self._dropped
