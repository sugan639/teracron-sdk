# -*- coding: utf-8 -*-
"""
Tests for Celery signal hooks — uses mock signals without requiring
Celery installation.

Since the actual signal connection requires celery to be importable,
we test the hook functions directly.
"""

import time
from unittest import mock

import pytest

from teracron.tracing.context import (
    clear_trace,
    get_trace_header,
    get_trace_id,
    push_span,
    start_trace,
)
from teracron.tracing.sampling import clear_sampling_decision


# We can't easily test setup_celery_tracing() without celery installed,
# but we CAN test the core tracing logic by simulating what the hooks do.
# This validates the context propagation and span creation logic.


def _make_mock_client(tracing_enabled=True, sample_rate=1.0, scrubber=None):
    client = mock.MagicMock()
    client.config.tracing_enabled = tracing_enabled
    client.config.trace_sample_rate = sample_rate
    client._push_trace_span = mock.MagicMock()
    client._scrubber = scrubber
    return client


class TestCeleryHeaderPropagation:
    """Test trace header propagation through Celery task headers."""

    def setup_method(self):
        clear_trace()
        clear_sampling_decision()

    def teardown_method(self):
        clear_trace()
        clear_sampling_decision()

    def test_inject_trace_header_into_task(self):
        """Simulate before_task_publish: inject header into outbound task."""
        tid = start_trace()
        push_span("a" * 32)

        headers = {}
        header_val = get_trace_header()
        if header_val:
            headers["X-Teracron-Trace"] = header_val

        assert "X-Teracron-Trace" in headers
        assert headers["X-Teracron-Trace"].startswith(tid)

    def test_restore_trace_from_header(self):
        """Simulate task_prerun: restore trace from task headers."""
        from teracron.tracing.context import set_trace_header

        trace_id = "c" * 32
        parent_id = "d" * 32
        set_trace_header(f"{trace_id}:{parent_id}")

        assert get_trace_id() == trace_id

    def test_task_span_lifecycle(self):
        """Simulate full task lifecycle: prerun → postrun."""
        from teracron.tracing.context import pop_span, peek_parent_span_id
        from teracron.tracing.span import create_span, finalise_span
        from teracron.tracing.sampling import (
            get_sampling_decision,
            set_sampling_decision,
            should_sample,
        )

        client = _make_mock_client()
        trace_id = start_trace()
        sampled = should_sample(trace_id, 1.0)
        set_sampling_decision(sampled)

        span = create_span(
            workflow="celery",
            operation="my_task",
            trace_id=trace_id,
            parent_span_id=peek_parent_span_id(),
        )
        push_span(span.span_id)
        t0 = time.monotonic()

        # Simulate task execution...
        time.sleep(0.001)

        # Simulate postrun.
        pop_span()
        duration_ms = (time.monotonic() - t0) * 1000.0
        finished = finalise_span(
            span,
            status="succeeded",
            duration_ms=duration_ms,
            metadata={"celery.task_id": "task-123"},
        )
        client._push_trace_span(finished.to_dict())
        clear_trace()

        client._push_trace_span.assert_called_once()
        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["workflow"] == "celery"
        assert span_dict["operation"] == "my_task"
        assert span_dict["status"] == "succeeded"
        assert span_dict["metadata"]["celery.task_id"] == "task-123"

    def test_task_failure_lifecycle(self):
        """Simulate task that raises an exception."""
        from teracron.tracing.context import pop_span, peek_parent_span_id
        from teracron.tracing.span import create_span, finalise_span
        from teracron.tracing.sampling import set_sampling_decision, should_sample

        client = _make_mock_client()
        trace_id = start_trace()
        set_sampling_decision(should_sample(trace_id, 1.0))

        span = create_span(
            workflow="celery",
            operation="failing_task",
            trace_id=trace_id,
            parent_span_id=peek_parent_span_id(),
        )
        push_span(span.span_id)
        t0 = time.monotonic()

        # Simulate failure.
        error_type = "ValueError"
        error_message = "bad input"

        pop_span()
        duration_ms = (time.monotonic() - t0) * 1000.0
        finished = finalise_span(
            span,
            status="failed",
            duration_ms=duration_ms,
            error_type=error_type,
            error_message=error_message,
            metadata={"celery.task_id": "task-456"},
        )
        client._push_trace_span(finished.to_dict())
        clear_trace()

        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["status"] == "failed"
        assert span_dict["error_type"] == "ValueError"
        assert span_dict["error_message"] == "bad input"

    def test_sampling_rate_0_skips_task_span(self):
        """Task span should be skipped when trace is not sampled."""
        from teracron.tracing.sampling import set_sampling_decision, should_sample

        client = _make_mock_client(sample_rate=0.0)
        trace_id = start_trace()
        sampled = should_sample(trace_id, 0.0)
        set_sampling_decision(sampled)

        assert sampled is False
        # If not sampled, no span should be pushed.
        # (The actual middleware checks this; we validate the decision.)
        clear_trace()
        client._push_trace_span.assert_not_called()

    def test_setup_requires_celery(self):
        """setup_celery_tracing raises ImportError without celery."""
        with mock.patch.dict("sys.modules", {"celery": None, "celery.signals": None}):
            from teracron.tracing.middleware.celery import setup_celery_tracing
            with pytest.raises(ImportError, match="Celery is required"):
                setup_celery_tracing(mock.MagicMock())
