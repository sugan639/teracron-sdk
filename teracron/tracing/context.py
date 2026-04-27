# -*- coding: utf-8 -*-
"""
Trace context propagation via ``contextvars``.

Provides thread-safe and asyncio-safe trace ID propagation using
Python's ``contextvars.ContextVar``.  Each thread inherits its own
copy; each ``asyncio.Task`` gets an isolated snapshot automatically.

Phase 2: adds span stack for parent-child nesting and cross-process
propagation via ``X-Teracron-Trace`` header.

Header wire format:  ``<trace_id>:<parent_span_id>``
Both values are 32-char hex strings.  The separator is ``:``.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import List, Optional, Tuple

# ── Context variables ──

# Active trace ID — None means no trace in progress.
_trace_id_var: ContextVar[Optional[str]] = ContextVar(
    "teracron_trace_id", default=None
)

# Span stack — list of span_id strings from outermost (root) to innermost.
# Each @trace call pushes its span_id; on exit it pops.
# The top of the stack is the current parent for the next nested span.
_span_stack_var: ContextVar[List[str]] = ContextVar(
    "teracron_span_stack", default=[]
)

# ── Trace lifecycle ──

_TRACE_HEADER_NAME = "X-Teracron-Trace"


def start_trace() -> str:
    """
    Generate a new trace ID and set it in the current context.

    Also resets the span stack for this new trace.

    Returns:
        The 32-char hex trace ID (``uuid4().hex``).
    """
    trace_id = uuid.uuid4().hex
    _trace_id_var.set(trace_id)
    _span_stack_var.set([])
    return trace_id


def get_trace_id() -> Optional[str]:
    """Return the active trace ID, or ``None`` if no trace is in progress."""
    return _trace_id_var.get()


def clear_trace() -> None:
    """Reset the trace context to ``None`` (no active trace)."""
    _trace_id_var.set(None)
    _span_stack_var.set([])
    # Clear sampling decision when trace ends.
    from .sampling import clear_sampling_decision
    clear_sampling_decision()


# ── Span stack (Phase 2 nesting) ──


def push_span(span_id: str) -> None:
    """Push a span_id onto the stack (called on span entry)."""
    # ContextVar.get() returns the default [] for a fresh context.
    # We must copy-on-write to avoid mutating the default list.
    stack = _span_stack_var.get()
    _span_stack_var.set(stack + [span_id])


def pop_span() -> Optional[str]:
    """Pop the top span_id off the stack (called on span exit). Returns it."""
    stack = _span_stack_var.get()
    if not stack:
        return None
    popped = stack[-1]
    _span_stack_var.set(stack[:-1])
    return popped


def peek_parent_span_id() -> Optional[str]:
    """Return the current parent span_id (top of stack), or ``None``."""
    stack = _span_stack_var.get()
    return stack[-1] if stack else None


# ── Cross-process propagation (Phase 2) ──


def get_trace_header() -> Optional[str]:
    """
    Build the ``X-Teracron-Trace`` header value for outbound requests.

    Format: ``<trace_id>:<current_span_id>``

    Returns ``None`` if no trace is active — caller should skip the header.
    """
    trace_id = _trace_id_var.get()
    if trace_id is None:
        return None
    parent = peek_parent_span_id()
    if parent is None:
        return trace_id
    return f"{trace_id}:{parent}"


def set_trace_header(header_value: Optional[str]) -> None:
    """
    Restore trace context from an inbound ``X-Teracron-Trace`` header.

    Parses ``<trace_id>`` or ``<trace_id>:<parent_span_id>`` and sets
    the context accordingly.  Invalid/empty values are silently ignored
    (zero-trust — never crash on bad input).

    Args:
        header_value: Raw header string, or ``None``.
    """
    if not header_value or not isinstance(header_value, str):
        return

    header_value = header_value.strip()
    if not header_value:
        return

    parts = header_value.split(":", 1)
    trace_id = parts[0].strip()

    # Validate trace_id: must be hex, 32 chars.
    if not trace_id or len(trace_id) != 32:
        return
    try:
        int(trace_id, 16)
    except ValueError:
        return

    _trace_id_var.set(trace_id)

    if len(parts) == 2:
        parent_span_id = parts[1].strip()
        # Validate parent_span_id: must be hex, 32 chars.
        if parent_span_id and len(parent_span_id) == 32:
            try:
                int(parent_span_id, 16)
            except ValueError:
                _span_stack_var.set([])
                return
            _span_stack_var.set([parent_span_id])
        else:
            _span_stack_var.set([])
    else:
        _span_stack_var.set([])
