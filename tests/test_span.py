# -*- coding: utf-8 -*-
"""Unit tests for Span data model and span factory functions."""

import time

import pytest

from teracron.types import Span
from teracron.tracing.span import create_span, finalise_span


class TestSpanDataclass:
    """Tests for the frozen Span dataclass."""

    def test_create_with_all_fields(self):
        span = Span(
            trace_id="a" * 32,
            span_id="b" * 32,
            parent_span_id="c" * 32,
            workflow="payment",
            operation="create_order",
            status="started",
            started_at=1700000000000,
        )
        assert span.trace_id == "a" * 32
        assert span.span_id == "b" * 32
        assert span.parent_span_id == "c" * 32
        assert span.workflow == "payment"
        assert span.operation == "create_order"
        assert span.status == "started"
        assert span.started_at == 1700000000000
        assert span.duration_ms == 0.0
        assert span.error_type is None
        assert span.error_message is None
        assert span.metadata is None
        assert span.captured_params is None

    def test_create_with_defaults(self):
        span = Span(trace_id="a" * 32, span_id="b" * 32)
        assert span.parent_span_id is None
        assert span.workflow == ""
        assert span.operation == ""
        assert span.status == "started"
        assert span.started_at == 0
        assert span.duration_ms == 0.0
        assert span.metadata is None
        assert span.captured_params is None

    def test_frozen_immutability(self):
        span = Span(
            trace_id="a" * 32,
            span_id="b" * 32,
            workflow="w",
            operation="op",
            status="started",
            started_at=0,
        )
        with pytest.raises(AttributeError):
            span.status = "succeeded"  # type: ignore[misc]

    def test_to_dict_output_shape(self):
        span = Span(
            trace_id="abc123",
            span_id="def456",
            workflow="onboarding",
            operation="send_email",
            status="succeeded",
            started_at=1700000000000,
            duration_ms=42.5,
        )
        d = span.to_dict()

        assert isinstance(d, dict)
        expected_keys = {
            "trace_id", "span_id", "parent_span_id", "workflow",
            "operation", "status", "started_at", "duration_ms",
            "error_type", "error_message", "metadata", "captured_params",
        }
        assert set(d.keys()) == expected_keys

    def test_to_dict_values(self):
        span = Span(
            trace_id="tid",
            span_id="sid",
            parent_span_id="pid",
            workflow="w",
            operation="op",
            status="failed",
            started_at=999,
            duration_ms=10.0,
            error_type="ValueError",
            error_message="bad input",
            metadata={"key": "val"},
            captured_params={"order_id": 42},
        )
        d = span.to_dict()
        assert d["trace_id"] == "tid"
        assert d["span_id"] == "sid"
        assert d["parent_span_id"] == "pid"
        assert d["workflow"] == "w"
        assert d["operation"] == "op"
        assert d["status"] == "failed"
        assert d["started_at"] == 999
        assert d["duration_ms"] == 10.0
        assert d["error_type"] == "ValueError"
        assert d["error_message"] == "bad input"
        assert d["metadata"] == {"key": "val"}
        assert d["captured_params"] == {"order_id": 42}

    def test_to_dict_nulls_when_no_metadata(self):
        span = Span(trace_id="t", span_id="s", workflow="w",
                     operation="op", status="started", started_at=0)
        d = span.to_dict()
        assert d["parent_span_id"] is None
        assert d["metadata"] is None
        assert d["captured_params"] is None

    def test_to_dict_field_types(self):
        span = Span(
            trace_id="t", span_id="s", workflow="w",
            operation="op", status="started", started_at=0,
        )
        d = span.to_dict()
        assert isinstance(d["trace_id"], str)
        assert isinstance(d["span_id"], str)
        assert isinstance(d["started_at"], int)
        assert isinstance(d["duration_ms"], float)


class TestCreateSpan:
    """Tests for the create_span factory."""

    def test_generates_unique_ids(self):
        s1 = create_span("w", "op")
        s2 = create_span("w", "op")
        assert s1.trace_id != s2.trace_id
        assert s1.span_id != s2.span_id

    def test_ids_are_32_hex_chars(self):
        span = create_span("w", "op")
        assert len(span.trace_id) == 32
        assert len(span.span_id) == 32
        int(span.trace_id, 16)  # Should not raise
        int(span.span_id, 16)

    def test_status_is_started(self):
        span = create_span("w", "op")
        assert span.status == "started"

    def test_started_at_is_recent(self):
        before = int(time.time() * 1000)
        span = create_span("w", "op")
        after = int(time.time() * 1000)
        assert before <= span.started_at <= after

    def test_uses_provided_trace_id(self):
        span = create_span("w", "op", trace_id="custom_trace_id")
        assert span.trace_id == "custom_trace_id"

    def test_workflow_and_operation_set(self):
        span = create_span("payment", "create_order")
        assert span.workflow == "payment"
        assert span.operation == "create_order"

    def test_parent_span_id_set(self):
        span = create_span("w", "op", parent_span_id="parent123")
        assert span.parent_span_id == "parent123"

    def test_parent_span_id_default_none(self):
        span = create_span("w", "op")
        assert span.parent_span_id is None


class TestFinaliseSpan:
    """Tests for the finalise_span function."""

    def test_succeeded(self):
        span = create_span("w", "op")
        finished = finalise_span(span, status="succeeded", duration_ms=12.4)
        assert finished.status == "succeeded"
        assert finished.duration_ms == 12.4
        assert finished.error_type is None
        assert finished.error_message is None

    def test_failed_with_error(self):
        span = create_span("w", "op")
        finished = finalise_span(
            span,
            status="failed",
            duration_ms=5.0,
            error_type="ValueError",
            error_message="invalid input",
        )
        assert finished.status == "failed"
        assert finished.duration_ms == 5.0
        assert finished.error_type == "ValueError"
        assert finished.error_message == "invalid input"

    def test_original_span_unchanged(self):
        span = create_span("w", "op")
        original_status = span.status
        finalise_span(span, status="succeeded", duration_ms=1.0)
        assert span.status == original_status  # Frozen — no mutation

    def test_preserves_ids_and_workflow(self):
        span = create_span("payment", "charge")
        finished = finalise_span(span, status="succeeded", duration_ms=1.0)
        assert finished.trace_id == span.trace_id
        assert finished.span_id == span.span_id
        assert finished.workflow == span.workflow
        assert finished.operation == span.operation
        assert finished.started_at == span.started_at

    def test_with_metadata(self):
        span = create_span("w", "op")
        finished = finalise_span(
            span, status="succeeded", duration_ms=1.0,
            metadata={"order_id": "ORD-123", "amount": 99.99},
        )
        assert finished.metadata == {"order_id": "ORD-123", "amount": 99.99}

    def test_with_captured_params(self):
        span = create_span("w", "op")
        finished = finalise_span(
            span, status="succeeded", duration_ms=1.0,
            captured_params={"user_id": 42, "action": "buy"},
        )
        assert finished.captured_params == {"user_id": 42, "action": "buy"}

    def test_metadata_sanitises_invalid_keys(self):
        span = create_span("w", "op")
        finished = finalise_span(
            span, status="succeeded", duration_ms=1.0,
            metadata={123: "bad_key", "good": "val"},  # type: ignore
        )
        assert finished.metadata == {"good": "val"}

    def test_metadata_sanitises_invalid_values(self):
        span = create_span("w", "op")
        finished = finalise_span(
            span, status="succeeded", duration_ms=1.0,
            metadata={"a": "ok", "b": [1, 2, 3]},  # list is invalid
        )
        assert finished.metadata == {"a": "ok"}

    def test_metadata_truncates_long_strings(self):
        span = create_span("w", "op")
        long_val = "x" * 2000
        finished = finalise_span(
            span, status="succeeded", duration_ms=1.0,
            metadata={"key": long_val},
        )
        assert finished.metadata is not None
        assert len(finished.metadata["key"]) == 1024

    def test_captured_params_converts_complex_to_repr(self):
        span = create_span("w", "op")
        finished = finalise_span(
            span, status="succeeded", duration_ms=1.0,
            captured_params={"items": [1, 2, 3]},
        )
        assert finished.captured_params is not None
        assert finished.captured_params["items"] == "[1, 2, 3]"

    def test_error_message_truncated(self):
        span = create_span("w", "op")
        long_msg = "e" * 2000
        finished = finalise_span(
            span, status="failed", duration_ms=1.0,
            error_type="RuntimeError",
            error_message=long_msg,
        )
        assert finished.error_message is not None
        assert len(finished.error_message) == 1024
