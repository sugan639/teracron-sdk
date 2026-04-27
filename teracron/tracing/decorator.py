# -*- coding: utf-8 -*-
"""
``@trace("workflow")`` decorator and ``trace_context`` context manager.

Phase 2 API:

    @trace("payment")
    def create_order(cart):
        ...

    @trace("payment", capture=["order_id", "amount"])
    def charge_card(order_id, amount):
        ...

    with trace_context("payment", operation="validate") as span:
        span.set_metadata({"order_id": "ORD-123"})
        ...

Parameter capture is **opt-in only**.  By default, NO function arguments
are transmitted — basic method flow (timing, status, errors) is always
traced, but parameter values require explicit whitelisting via the
``capture`` argument.  This is the PII safety boundary.

Thread-safety: uses ``contextvars`` for trace propagation, which is
inherently thread-safe and asyncio-safe.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import time
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Dict,
    Iterator,
    Optional,
    Sequence,
    TypeVar,
)

from .context import (
    clear_trace,
    get_trace_id,
    peek_parent_span_id,
    pop_span,
    push_span,
    start_trace,
)
from .events import (
    build_step_completed_event,
    build_step_failed_event,
    build_step_started_event,
    build_workflow_completed_event,
    build_workflow_failed_event,
    build_workflow_started_event,
)
from .sampling import (
    get_sampling_decision,
    set_sampling_decision,
    should_sample,
)
from .span import create_span, finalise_span

F = TypeVar("F", bound=Callable[..., Any])


def _get_client():
    """
    Lazy import to avoid circular dependency.

    Returns the singleton ``TeracronClient`` or ``None``.
    """
    from ..client import _singleton
    return _singleton


# ── Parameter capture ──


def _extract_captured_params(
    func: Callable[..., Any],
    capture: Sequence[str],
    args: tuple,
    kwargs: dict,
) -> Optional[Dict[str, object]]:
    """
    Extract whitelisted parameter values from function call arguments.

    Uses ``inspect.signature`` to bind positional+keyword args, then
    picks only the names listed in ``capture``.  Returns ``None`` if
    no matching params found (avoids empty dicts on the wire).

    This is the critical PII boundary: only explicitly named parameters
    are extracted.  Everything else is discarded.
    """
    if not capture:
        return None

    try:
        sig = inspect.signature(func)
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
    except (TypeError, ValueError):
        # Defensive: if binding fails (e.g. C extensions), skip capture.
        return None

    result: Dict[str, object] = {}
    capture_set = frozenset(capture)  # O(1) lookup
    for name, value in bound.arguments.items():
        if name in capture_set:
            result[name] = value

    return result if result else None


# ── Core span lifecycle (shared by decorator and context manager) ──


def _begin_span(
    workflow: str,
    operation: str,
    sample_rate: float = 1.0,
) -> tuple:
    """
    Begin a span: resolve trace context, check sampling, create Span, push.

    Sampling decision is made at the trace root and inherited by all
    child spans (all-or-nothing per trace).

    Returns (span, is_root, t0, sampled) tuple.
    ``sampled=False`` means this trace was not selected — skip buffering.
    """
    is_root = get_trace_id() is None
    trace_id = get_trace_id() if not is_root else start_trace()

    # Sampling: decide at root, inherit for children.
    sampled = get_sampling_decision()
    if sampled is None:
        # First span in this trace — make the decision.
        sampled = should_sample(trace_id, sample_rate)
        set_sampling_decision(sampled)

    parent_span_id = peek_parent_span_id()

    span = create_span(
        workflow=workflow,
        operation=operation,
        trace_id=trace_id,
        parent_span_id=parent_span_id,
    )

    push_span(span.span_id)
    t0 = time.monotonic()

    # Emit structured event if enabled.
    client = _get_client()
    if client is not None and getattr(client.config, "trace_emit_events", False) and sampled:
        _emit_start_event(client, span, is_root)

    return span, is_root, t0, sampled


def _emit_start_event(client: Any, span: Any, is_root: bool) -> None:
    """Emit a step_started or workflow_started event (best-effort, never raises)."""
    try:
        event_buffer = getattr(client, "_event_buffer", None)
        if event_buffer is None:
            return
        if is_root:
            evt = build_workflow_started_event(
                workflow=span.workflow,
                trace_id=span.trace_id,
                span_id=span.span_id,
                operation=span.operation,
            )
        else:
            evt = build_step_started_event(
                workflow=span.workflow,
                trace_id=span.trace_id,
                span_id=span.span_id,
                operation=span.operation,
            )
        if evt:
            event_buffer.push(evt)
    except Exception:  # nosec B110
        pass


def _apply_scrubber(
    scrubber: Optional[Callable[..., Any]],
    data: Optional[Dict[str, object]],
) -> Optional[Dict[str, object]]:
    """
    Apply user-provided PII scrubber to a metadata/params dict.

    Defence in depth: scrubber exceptions are caught and logged —
    never crash the user's application over telemetry scrubbing.
    Receives a shallow copy to prevent mutation of caller's data.
    """
    if scrubber is None or data is None:
        return data
    try:
        scrubbed = scrubber(dict(data))  # shallow copy
        if isinstance(scrubbed, dict):
            return scrubbed if scrubbed else None
        # Scrubber returned non-dict — discard data as safety measure.
        return None
    except Exception:
        # Scrubber failed — drop the data rather than risk leaking PII.
        return None


def _end_span(
    client: Any,
    span: Any,
    is_root: bool,
    t0: float,
    *,
    sampled: bool = True,
    status: str,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
    metadata: Optional[Dict[str, object]] = None,
    captured_params: Optional[Dict[str, object]] = None,
) -> None:
    """Finalise a span, pop from stack, push to client buffer, clear root."""
    pop_span()

    if not sampled:
        # Trace was not sampled — skip buffering entirely.
        if is_root:
            clear_trace()
        return

    # Apply PII scrubber before finalisation.
    scrubber = getattr(client, "_scrubber", None)
    safe_metadata = _apply_scrubber(scrubber, metadata)
    safe_params = _apply_scrubber(scrubber, captured_params)

    duration_ms = (time.monotonic() - t0) * 1000.0
    finished = finalise_span(
        span,
        status=status,
        duration_ms=duration_ms,
        error_type=error_type,
        error_message=error_message,
        metadata=safe_metadata,
        captured_params=safe_params,
    )
    client._push_trace_span(finished.to_dict())

    # Emit structured end event if enabled.
    if getattr(client.config, "trace_emit_events", False):
        _emit_end_event(
            client, span, is_root,
            status=status,
            duration_ms=duration_ms,
            error_type=error_type,
            error_message=error_message,
        )

    if is_root:
        clear_trace()


def _emit_end_event(
    client: Any,
    span: Any,
    is_root: bool,
    *,
    status: str,
    duration_ms: float,
    error_type: Optional[str],
    error_message: Optional[str],
) -> None:
    """Emit a step_completed/step_failed or workflow_completed/workflow_failed event."""
    try:
        event_buffer = getattr(client, "_event_buffer", None)
        if event_buffer is None:
            return

        if status == "failed":
            builder = build_workflow_failed_event if is_root else build_step_failed_event
            evt = builder(
                workflow=span.workflow,
                trace_id=span.trace_id,
                span_id=span.span_id,
                operation=span.operation,
                error_type=error_type or "Unknown",
                error_message=error_message or "",
                duration_ms=duration_ms,
            )
        else:
            builder = build_workflow_completed_event if is_root else build_step_completed_event
            evt = builder(
                workflow=span.workflow,
                trace_id=span.trace_id,
                span_id=span.span_id,
                operation=span.operation,
                duration_ms=duration_ms,
            )
        if evt:
            event_buffer.push(evt)
    except Exception:  # nosec B110
        pass


# ── @trace decorator ──


def trace(
    workflow: str,
    *,
    capture: Optional[Sequence[str]] = None,
) -> Callable[[F], F]:
    """
    Decorator that traces a function as part of a named workflow.

    Args:
        workflow: Logical workflow name (e.g. ``"payment"``, ``"onboarding"``).
        capture:  Optional list of parameter names whose values should be
                  sent to Teracron.  **Only whitelisted names are captured.**
                  By default ``None`` — no parameter values are transmitted.
                  This is the PII safety boundary.

    Usage::

        @trace("payment")
        def create_order(cart):
            ...

        @trace("payment", capture=["order_id", "amount"])
        def charge_card(order_id, amount):
            ...

    Raises:
        RuntimeError: If ``teracron.up()`` has not been called before the
            decorated function is invoked.
    """
    if not isinstance(workflow, str) or not workflow:
        raise ValueError(
            "[Teracron] @trace() requires a non-empty workflow name string."
        )

    # Freeze capture list at decoration time — immutable after this point.
    _capture: tuple = tuple(capture) if capture else ()

    def decorator(func: F) -> F:
        if asyncio.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                client = _get_client()
                if client is None:
                    raise RuntimeError(
                        "[Teracron] Call teracron.up() before using @trace."
                    )

                if not client.config.tracing_enabled:
                    return await func(*args, **kwargs)

                span, is_root, t0, sampled = _begin_span(
                    workflow, func.__qualname__,
                    sample_rate=client.config.trace_sample_rate,
                )

                # Extract captured params only if sampled (avoid unnecessary work).
                params = (
                    _extract_captured_params(func, _capture, args, kwargs)
                    if sampled else None
                )

                try:
                    result = await func(*args, **kwargs)
                except Exception as exc:
                    _end_span(
                        client, span, is_root, t0,
                        sampled=sampled,
                        status="failed",
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                        captured_params=params,
                    )
                    raise
                else:
                    _end_span(
                        client, span, is_root, t0,
                        sampled=sampled,
                        status="succeeded",
                        captured_params=params,
                    )
                    return result

            return async_wrapper  # type: ignore[return-value]
        else:
            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                client = _get_client()
                if client is None:
                    raise RuntimeError(
                        "[Teracron] Call teracron.up() before using @trace."
                    )

                if not client.config.tracing_enabled:
                    return func(*args, **kwargs)

                span, is_root, t0, sampled = _begin_span(
                    workflow, func.__qualname__,
                    sample_rate=client.config.trace_sample_rate,
                )

                # Extract captured params only if sampled (avoid unnecessary work).
                params = (
                    _extract_captured_params(func, _capture, args, kwargs)
                    if sampled else None
                )

                try:
                    result = func(*args, **kwargs)
                except Exception as exc:
                    _end_span(
                        client, span, is_root, t0,
                        sampled=sampled,
                        status="failed",
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                        captured_params=params,
                    )
                    raise
                else:
                    _end_span(
                        client, span, is_root, t0,
                        sampled=sampled,
                        status="succeeded",
                        captured_params=params,
                    )
                    return result

            return sync_wrapper  # type: ignore[return-value]

    return decorator


# ── SpanHandle (for trace_context context manager) ──


class SpanHandle:
    """
    Mutable handle exposed inside ``trace_context``.

    Allows the user to attach metadata and captured params to the
    span while the context manager is active.

    Thread-safety: SpanHandle is created per-invocation (stack-local);
    no shared mutable state.
    """

    __slots__ = ("_metadata",)

    def __init__(self) -> None:
        self._metadata: Dict[str, object] = {}

    def set_metadata(self, data: Dict[str, object]) -> None:
        """
        Merge key-value pairs into the span's metadata.

        Keys must be strings.  Values must be ``str | int | float | bool``.
        Invalid entries are silently dropped during span finalisation.

        Args:
            data: Dictionary of metadata to attach.
        """
        if isinstance(data, dict):
            self._metadata.update(data)

    @property
    def metadata(self) -> Dict[str, object]:
        return self._metadata


# ── trace_context context manager ──


@contextlib.contextmanager
def trace_context(
    workflow: str,
    *,
    operation: Optional[str] = None,
) -> Iterator[SpanHandle]:
    """
    Sync context manager for tracing a block of code.

    Args:
        workflow:  Logical workflow name.
        operation: Operation name.  Defaults to ``"<context_manager>"``.

    Usage::

        with trace_context("payment", operation="validate") as span:
            span.set_metadata({"order_id": "ORD-123"})
            validate_order(order)

    Raises:
        RuntimeError: If ``teracron.up()`` has not been called.
    """
    client = _get_client()
    if client is None:
        raise RuntimeError(
            "[Teracron] Call teracron.up() before using trace_context."
        )

    if not client.config.tracing_enabled:
        yield SpanHandle()
        return

    op = operation or "<context_manager>"
    span, is_root, t0, sampled = _begin_span(
        workflow, op,
        sample_rate=client.config.trace_sample_rate,
    )
    handle = SpanHandle()

    try:
        yield handle
    except Exception as exc:
        _end_span(
            client, span, is_root, t0,
            sampled=sampled,
            status="failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
            metadata=handle.metadata if handle.metadata else None,
        )
        raise
    else:
        _end_span(
            client, span, is_root, t0,
            sampled=sampled,
            status="succeeded",
            metadata=handle.metadata if handle.metadata else None,
        )


@contextlib.asynccontextmanager
async def async_trace_context(
    workflow: str,
    *,
    operation: Optional[str] = None,
) -> AsyncIterator[SpanHandle]:
    """
    Async context manager for tracing a block of async code.

    Args:
        workflow:  Logical workflow name.
        operation: Operation name.  Defaults to ``"<context_manager>"``.

    Usage::

        async with async_trace_context("payment", operation="validate") as span:
            span.set_metadata({"order_id": "ORD-123"})
            await validate_order(order)

    Raises:
        RuntimeError: If ``teracron.up()`` has not been called.
    """
    client = _get_client()
    if client is None:
        raise RuntimeError(
            "[Teracron] Call teracron.up() before using async_trace_context."
        )

    if not client.config.tracing_enabled:
        yield SpanHandle()
        return

    op = operation or "<context_manager>"
    span, is_root, t0, sampled = _begin_span(
        workflow, op,
        sample_rate=client.config.trace_sample_rate,
    )
    handle = SpanHandle()

    try:
        yield handle
    except Exception as exc:
        _end_span(
            client, span, is_root, t0,
            sampled=sampled,
            status="failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
            metadata=handle.metadata if handle.metadata else None,
        )
        raise
    else:
        _end_span(
            client, span, is_root, t0,
            sampled=sampled,
            status="succeeded",
            metadata=handle.metadata if handle.metadata else None,
        )
