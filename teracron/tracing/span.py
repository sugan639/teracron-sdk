# -*- coding: utf-8 -*-
"""
Span factory — creates and finalises immutable ``Span`` instances.

All span creation routes through this module to ensure consistent
ID generation, field population, and safety validation.

Phase 2: supports parent_span_id (nesting), metadata, and captured_params.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import replace
from typing import Dict, Optional, Sequence, Tuple

from ..types import (
    CAPTURE_MAX_VALUE_LEN,
    METADATA_ALLOWED_TYPES,
    METADATA_MAX_KEY_LEN,
    METADATA_MAX_KEYS,
    METADATA_MAX_VALUE_LEN,
    Span,
)


# Reserved metadata keys auto-populated by the SDK so the dashboard's live-log
# search can filter by thread without the user having to add anything. These
# are *never* overwritten if the user explicitly set the same key — user
# values always win.
_RESERVED_THREAD_NAME_KEY = "thread_name"
_RESERVED_THREAD_ID_KEY = "thread_id"


def _capture_thread_context() -> Dict[str, object]:
    """
    Return a tiny metadata dict describing the current thread.

    Why this lives here: ``finalise_span`` is the single chokepoint that
    every code path (decorator, ``trace_context``, FastAPI/Django/Celery
    middleware, future call sites) routes through. Injecting once here
    guarantees uniform coverage without touching N call sites.

    Failure mode: ``threading`` is a stdlib module and these calls cannot
    realistically raise — but if they ever did, we swallow the error so
    telemetry never breaks the user's application.
    """
    try:
        current = threading.current_thread()
        # ``ident`` may be ``None`` if the thread hasn't started yet; the
        # finalising thread is always running, so this is just defensive.
        thread_id = current.ident
        thread_name = current.name
        ctx: Dict[str, object] = {}
        if isinstance(thread_name, str) and thread_name:
            ctx[_RESERVED_THREAD_NAME_KEY] = thread_name
        if isinstance(thread_id, int):
            ctx[_RESERVED_THREAD_ID_KEY] = thread_id
        return ctx
    except Exception:  # pragma: no cover — defensive
        return {}


def create_span(
    workflow: str,
    operation: str,
    trace_id: Optional[str] = None,
    parent_span_id: Optional[str] = None,
) -> Span:
    """
    Create a new span in *started* state.

    Args:
        workflow:       Logical process name (user-provided via ``@trace``).
        operation:      Fully-qualified function name (``func.__qualname__``).
        trace_id:       Existing trace ID to correlate with.  If ``None``,
                        a new trace ID is generated (root span).
        parent_span_id: Span ID of the parent span, or ``None`` for root.

    Returns:
        A frozen ``Span`` with ``status="started"`` and wall-clock
        ``started_at`` set to the current time in Unix milliseconds.
    """
    return Span(
        trace_id=trace_id or uuid.uuid4().hex,
        span_id=uuid.uuid4().hex,
        parent_span_id=parent_span_id,
        workflow=workflow,
        operation=operation,
        status="started",
        started_at=int(time.time() * 1000),
    )


def finalise_span(
    span: Span,
    *,
    status: str,
    duration_ms: float,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
    metadata: Optional[Dict[str, object]] = None,
    captured_params: Optional[Dict[str, object]] = None,
) -> Span:
    """
    Produce a finalised copy of *span* with outcome fields populated.

    Uses ``dataclasses.replace()`` because ``Span`` is frozen — the
    original instance is never mutated.

    Metadata and captured_params are validated and sanitised before
    being attached.  Invalid entries are silently dropped (defence in
    depth — never crash the user's application over telemetry data).

    Args:
        span:            The in-progress span to finalise.
        status:          ``"succeeded"`` or ``"failed"``.
        duration_ms:     Wall-clock execution time in milliseconds.
        error_type:      Exception class name (only on failure).
        error_message:   Exception message string (only on failure).
        metadata:        User-provided key-value pairs (from ``set_metadata``).
        captured_params: Whitelisted function parameter captures.

    Returns:
        A new frozen ``Span`` with updated fields.
    """
    safe_metadata = _sanitise_metadata(metadata) if metadata else None
    safe_params = _sanitise_captured_params(captured_params) if captured_params else None

    # Auto-inject thread context so the dashboard's live-log search can
    # filter by thread name / id without requiring user instrumentation.
    # User-supplied keys ALWAYS win — we only fill in gaps. This keeps the
    # change strictly additive and backward-compatible.
    thread_ctx = _capture_thread_context()
    if thread_ctx:
        merged: Dict[str, object] = dict(thread_ctx)
        if safe_metadata:
            merged.update(safe_metadata)  # user values overwrite reserved
        safe_metadata = merged

    # Truncate error_message to prevent oversized payloads.
    safe_error_message = error_message
    if safe_error_message and len(safe_error_message) > METADATA_MAX_VALUE_LEN:
        safe_error_message = safe_error_message[:METADATA_MAX_VALUE_LEN]

    return replace(
        span,
        status=status,
        duration_ms=duration_ms,
        error_type=error_type,
        error_message=safe_error_message,
        metadata=safe_metadata,
        captured_params=safe_params,
    )


def _sanitise_metadata(raw: Dict[str, object]) -> Optional[Dict[str, object]]:
    """
    Validate and sanitise user metadata. Returns None if empty after filtering.

    Rules:
    - Keys must be strings, max ``METADATA_MAX_KEY_LEN`` chars.
    - Values must be ``str | int | float | bool``.
    - String values are truncated to ``METADATA_MAX_VALUE_LEN``.
    - Max ``METADATA_MAX_KEYS`` entries (oldest-wins — dict insertion order).
    - Invalid entries are silently dropped.
    """
    if not isinstance(raw, dict):
        return None

    safe: Dict[str, object] = {}
    for key, value in raw.items():
        if len(safe) >= METADATA_MAX_KEYS:
            break
        if not isinstance(key, str) or not key or len(key) > METADATA_MAX_KEY_LEN:
            continue
        if not isinstance(value, METADATA_ALLOWED_TYPES):
            continue
        # Truncate long strings
        if isinstance(value, str) and len(value) > METADATA_MAX_VALUE_LEN:
            value = value[:METADATA_MAX_VALUE_LEN]
        safe[key] = value

    return safe if safe else None


def _sanitise_captured_params(raw: Dict[str, object]) -> Optional[Dict[str, object]]:
    """
    Validate and sanitise captured function parameters.

    Same rules as metadata, but with ``CAPTURE_MAX_VALUE_LEN`` for
    string truncation.  This is the critical PII boundary — only
    explicitly whitelisted params ever reach this function.
    """
    if not isinstance(raw, dict):
        return None

    safe: Dict[str, object] = {}
    for key, value in raw.items():
        if len(safe) >= METADATA_MAX_KEYS:
            break
        if not isinstance(key, str) or not key or len(key) > METADATA_MAX_KEY_LEN:
            continue
        if not isinstance(value, METADATA_ALLOWED_TYPES):
            # Non-primitive values: convert to str repr and truncate.
            value = repr(value)
            if len(value) > CAPTURE_MAX_VALUE_LEN:
                value = value[:CAPTURE_MAX_VALUE_LEN]
        elif isinstance(value, str) and len(value) > CAPTURE_MAX_VALUE_LEN:
            value = value[:CAPTURE_MAX_VALUE_LEN]
        safe[key] = value

    return safe if safe else None
