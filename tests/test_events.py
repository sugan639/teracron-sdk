# -*- coding: utf-8 -*-
"""
Tests for teracron.tracing.events — structured workflow event emitter.

Covers:
    - Event building with valid/invalid types
    - Severity validation
    - Error message truncation
    - Metadata sanitisation
    - Convenience builders (workflow_started, step_failed, etc.)
    - EventBuffer: push, drain, capacity, overflow
"""

from __future__ import annotations

import pytest

from teracron.tracing.events import (
    EventBuffer,
    VALID_EVENT_TYPES,
    VALID_SEVERITIES,
    build_event,
    build_step_completed_event,
    build_step_failed_event,
    build_step_started_event,
    build_workflow_completed_event,
    build_workflow_failed_event,
    build_workflow_started_event,
)


# ── build_event ──


class TestBuildEvent:
    def test_valid_event(self) -> None:
        evt = build_event(
            workflow="payment",
            event_type="step_started",
            trace_id="a" * 32,
            span_id="b" * 32,
            operation="charge_card",
        )
        assert evt is not None
        assert evt["type"] == "step_started"
        assert evt["workflow"] == "payment"
        assert evt["trace_id"] == "a" * 32
        assert evt["severity"] == "info"
        assert "timestamp" in evt

    def test_invalid_event_type(self) -> None:
        evt = build_event(
            workflow="payment",
            event_type="invalid_type",
        )
        assert evt is None

    def test_all_valid_types(self) -> None:
        for event_type in VALID_EVENT_TYPES:
            evt = build_event(workflow="test", event_type=event_type)
            assert evt is not None
            assert evt["type"] == event_type

    def test_invalid_severity_defaults_to_info(self) -> None:
        evt = build_event(
            workflow="payment",
            event_type="step_started",
            severity="PANIC",
        )
        assert evt is not None
        assert evt["severity"] == "info"

    def test_all_valid_severities(self) -> None:
        for severity in VALID_SEVERITIES:
            evt = build_event(
                workflow="test",
                event_type="step_started",
                severity=severity,
            )
            assert evt is not None
            assert evt["severity"] == severity

    def test_error_message_truncated(self) -> None:
        long_msg = "x" * 2000
        evt = build_event(
            workflow="payment",
            event_type="step_failed",
            error_type="ValueError",
            error_message=long_msg,
        )
        assert evt is not None
        assert len(evt["error_message"]) == 512

    def test_metadata_primitives_only(self) -> None:
        evt = build_event(
            workflow="payment",
            event_type="step_started",
            metadata={
                "count": 42,
                "name": "test",
                "active": True,
                "rate": 0.5,
                "bad_list": [1, 2, 3],  # Should be dropped
                "bad_dict": {"nested": True},  # Should be dropped
            },
        )
        assert evt is not None
        meta = evt["metadata"]
        assert "count" in meta
        assert "name" in meta
        assert "active" in meta
        assert "rate" in meta
        assert "bad_list" not in meta
        assert "bad_dict" not in meta

    def test_metadata_max_keys(self) -> None:
        big_metadata = {f"key_{i}": i for i in range(50)}
        evt = build_event(
            workflow="test",
            event_type="step_started",
            metadata=big_metadata,
        )
        assert evt is not None
        assert len(evt["metadata"]) <= 16

    def test_empty_metadata_becomes_none(self) -> None:
        evt = build_event(
            workflow="test",
            event_type="step_started",
            metadata={},
        )
        assert evt is not None
        assert "metadata" not in evt

    def test_non_dict_metadata_ignored(self) -> None:
        evt = build_event(
            workflow="test",
            event_type="step_started",
            metadata="not a dict",  # type: ignore[arg-type]
        )
        assert evt is not None
        assert "metadata" not in evt

    def test_error_fields_only_on_failure(self) -> None:
        evt = build_event(
            workflow="test",
            event_type="step_started",
        )
        assert evt is not None
        assert "error_type" not in evt
        assert "error_message" not in evt

    def test_error_fields_present_on_failure(self) -> None:
        evt = build_event(
            workflow="test",
            event_type="step_failed",
            error_type="ValueError",
            error_message="bad input",
        )
        assert evt is not None
        assert evt["error_type"] == "ValueError"
        assert evt["error_message"] == "bad input"


# ── Convenience builders ──


class TestConvenienceBuilders:
    def test_workflow_started(self) -> None:
        evt = build_workflow_started_event(
            workflow="payment",
            trace_id="a" * 32,
            span_id="b" * 32,
            operation="process_payment",
        )
        assert evt is not None
        assert evt["type"] == "workflow_started"
        assert evt["severity"] == "info"

    def test_workflow_completed(self) -> None:
        evt = build_workflow_completed_event(
            workflow="payment",
            trace_id="a" * 32,
            span_id="b" * 32,
            operation="process_payment",
            duration_ms=123.45,
        )
        assert evt is not None
        assert evt["type"] == "workflow_completed"
        assert evt["metadata"]["duration_ms"] == 123.45

    def test_workflow_failed(self) -> None:
        evt = build_workflow_failed_event(
            workflow="payment",
            trace_id="a" * 32,
            span_id="b" * 32,
            operation="process_payment",
            error_type="ValueError",
            error_message="Bad card",
            duration_ms=50.0,
        )
        assert evt is not None
        assert evt["type"] == "workflow_failed"
        assert evt["severity"] == "error"
        assert evt["error_type"] == "ValueError"

    def test_step_started(self) -> None:
        evt = build_step_started_event(
            workflow="payment",
            trace_id="a" * 32,
            span_id="c" * 32,
            operation="validate_card",
        )
        assert evt is not None
        assert evt["type"] == "step_started"

    def test_step_completed(self) -> None:
        evt = build_step_completed_event(
            workflow="payment",
            trace_id="a" * 32,
            span_id="c" * 32,
            operation="validate_card",
            duration_ms=10.5,
        )
        assert evt is not None
        assert evt["type"] == "step_completed"

    def test_step_failed(self) -> None:
        evt = build_step_failed_event(
            workflow="payment",
            trace_id="a" * 32,
            span_id="c" * 32,
            operation="validate_card",
            error_type="TypeError",
            error_message="bad type",
            duration_ms=5.0,
        )
        assert evt is not None
        assert evt["type"] == "step_failed"
        assert evt["severity"] == "error"


# ── EventBuffer ──


class TestEventBuffer:
    def test_push_and_drain(self) -> None:
        buf = EventBuffer(capacity=100)
        evt = {"type": "step_started", "workflow": "test"}
        buf.push(evt)
        assert buf.size == 1

        drained = buf.drain(max_items=10)
        assert len(drained) == 1
        assert drained[0] == evt
        assert buf.size == 0

    def test_drain_respects_max(self) -> None:
        buf = EventBuffer(capacity=100)
        for i in range(20):
            buf.push({"type": "step_started", "index": i})

        drained = buf.drain(max_items=5)
        assert len(drained) == 5
        assert buf.size == 15

    def test_overflow_drops_oldest(self) -> None:
        buf = EventBuffer(capacity=10)
        for i in range(20):
            buf.push({"index": i})

        # Buffer should only hold the last 10.
        assert buf.size == 10
        drained = buf.drain(max_items=100)
        # deque with maxlen drops oldest automatically.
        indices = [e["index"] for e in drained]
        assert indices == list(range(10, 20))

    def test_push_none_ignored(self) -> None:
        buf = EventBuffer(capacity=100)
        buf.push(None)
        assert buf.size == 0

    def test_capacity_clamped_min(self) -> None:
        buf = EventBuffer(capacity=1)
        assert buf._capacity == 10  # Minimum is 10

    def test_capacity_clamped_max(self) -> None:
        buf = EventBuffer(capacity=100_000)
        assert buf._capacity == 10_000  # Maximum is 10,000

    def test_drain_empty_buffer(self) -> None:
        buf = EventBuffer(capacity=100)
        drained = buf.drain(max_items=10)
        assert drained == []

    def test_dropped_count(self) -> None:
        buf = EventBuffer(capacity=10)
        for i in range(15):
            buf.push({"index": i})
        # deque with maxlen handles overflow internally but our counter tracks it.
        assert buf.dropped_count >= 5
