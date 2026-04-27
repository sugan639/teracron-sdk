# -*- coding: utf-8 -*-
"""
Tests for teracron.simulate — failure replay engine.

Covers:
    - Failure context extraction from trace data
    - Repro script generation
    - Markdown diagnosis formatting
    - Error handling (missing trace, no failed spans, no spans)
    - Input validation
"""

from __future__ import annotations

from unittest import mock

import pytest

from teracron.simulate import FailureSimulator


# ── Helpers ──


def _make_trace_response(spans=None, error=None):
    """Build a mock trace response."""
    if error:
        return {"error": error}
    return {"spans": spans or []}


def _make_span(
    *,
    span_id="a" * 32,
    parent_span_id=None,
    workflow="payment",
    operation="process_payment",
    status="succeeded",
    started_at=1721500000000,
    duration_ms=100.0,
    error_type=None,
    error_message=None,
    captured_params=None,
):
    return {
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "workflow": workflow,
        "operation": operation,
        "status": status,
        "started_at": started_at,
        "duration_ms": duration_ms,
        "error_type": error_type,
        "error_message": error_message,
        "captured_params": captured_params,
    }


def _make_mock_client(trace_response):
    """Build a mock TeracronQueryClient."""
    client = mock.MagicMock()
    client.get_trace.return_value = trace_response
    return client


# ── fetch_failure_context ──


class TestFetchFailureContext:
    def test_successful_extraction(self) -> None:
        spans = [
            _make_span(
                span_id="1" * 32,
                operation="create_order",
                status="succeeded",
                started_at=1000,
            ),
            _make_span(
                span_id="2" * 32,
                parent_span_id="1" * 32,
                operation="charge_card",
                status="failed",
                started_at=2000,
                error_type="ValueError",
                error_message="Invalid card number",
                captured_params={"order_id": "ORD-123", "amount": 99.99},
            ),
        ]
        mock_client = _make_mock_client(_make_trace_response(spans=spans))
        simulator = FailureSimulator(query_client=mock_client)

        ctx = simulator.fetch_failure_context("abc123")

        assert ctx["workflow"] == "payment"
        assert ctx["failed_operation"] == "charge_card"
        assert ctx["error_type"] == "ValueError"
        assert ctx["error_message"] == "Invalid card number"
        assert ctx["captured_params"]["order_id"] == "ORD-123"
        assert len(ctx["span_chain"]) == 2
        assert ctx["failure_count"] == 1

    def test_multiple_failed_spans(self) -> None:
        spans = [
            _make_span(span_id="1" * 32, operation="step1", status="failed", started_at=1000,
                       error_type="TypeError", error_message="bad type"),
            _make_span(span_id="2" * 32, operation="step2", status="failed", started_at=2000,
                       error_type="ValueError", error_message="bad value"),
        ]
        mock_client = _make_mock_client(_make_trace_response(spans=spans))
        simulator = FailureSimulator(query_client=mock_client)

        ctx = simulator.fetch_failure_context("abc")

        # Should pick the deepest (last) failed span.
        assert ctx["failed_operation"] == "step2"
        assert ctx["failure_count"] == 2

    def test_no_failed_spans(self) -> None:
        spans = [
            _make_span(operation="step1", status="succeeded"),
        ]
        mock_client = _make_mock_client(_make_trace_response(spans=spans))
        simulator = FailureSimulator(query_client=mock_client)

        ctx = simulator.fetch_failure_context("abc")
        assert ctx.get("error")
        assert "all spans succeeded" in ctx["error"]

    def test_empty_spans(self) -> None:
        mock_client = _make_mock_client(_make_trace_response(spans=[]))
        simulator = FailureSimulator(query_client=mock_client)

        ctx = simulator.fetch_failure_context("abc")
        assert ctx.get("error")
        assert "No spans found" in ctx["error"]

    def test_api_error(self) -> None:
        mock_client = _make_mock_client(
            _make_trace_response(error="Connection failed")
        )
        simulator = FailureSimulator(query_client=mock_client)

        ctx = simulator.fetch_failure_context("abc")
        assert ctx.get("error") == "Connection failed"

    def test_empty_trace_id(self) -> None:
        mock_client = _make_mock_client({})
        simulator = FailureSimulator(query_client=mock_client)

        ctx = simulator.fetch_failure_context("")
        assert ctx.get("error")

    def test_none_trace_id(self) -> None:
        mock_client = _make_mock_client({})
        simulator = FailureSimulator(query_client=mock_client)

        ctx = simulator.fetch_failure_context(None)  # type: ignore[arg-type]
        assert ctx.get("error")

    def test_error_message_truncated(self) -> None:
        long_msg = "x" * 2000
        spans = [
            _make_span(
                operation="fail_op",
                status="failed",
                error_type="RuntimeError",
                error_message=long_msg,
            ),
        ]
        mock_client = _make_mock_client(_make_trace_response(spans=spans))
        simulator = FailureSimulator(query_client=mock_client)

        ctx = simulator.fetch_failure_context("abc")
        assert len(ctx["error_message"]) == 512


# ── generate_repro_script ──


class TestGenerateReproScript:
    def test_basic_script(self) -> None:
        ctx = {
            "trace_id": "abc123",
            "workflow": "payment",
            "failed_operation": "charge_card",
            "error_type": "ValueError",
            "error_message": "Invalid card number",
            "captured_params": {"order_id": "ORD-123", "amount": 99.99},
            "span_chain": ["create_order", "charge_card"],
        }
        mock_client = _make_mock_client({})
        simulator = FailureSimulator(query_client=mock_client)

        script = simulator.generate_repro_script(ctx)

        assert "#!/usr/bin/env python3" in script
        assert "charge_card" in script
        assert "ValueError" in script
        assert "ORD-123" in script
        assert "99.99" in script

    def test_no_captured_params(self) -> None:
        ctx = {
            "trace_id": "abc123",
            "workflow": "payment",
            "failed_operation": "process",
            "error_type": "RuntimeError",
            "error_message": "Failed",
            "captured_params": None,
            "span_chain": ["process"],
        }
        mock_client = _make_mock_client({})
        simulator = FailureSimulator(query_client=mock_client)

        script = simulator.generate_repro_script(ctx)
        assert "no parameters were captured" in script

    def test_error_context(self) -> None:
        ctx = {"error": "Something went wrong"}
        mock_client = _make_mock_client({})
        simulator = FailureSimulator(query_client=mock_client)

        script = simulator.generate_repro_script(ctx)
        assert "Error:" in script


# ── print_diagnosis ──


class TestPrintDiagnosis:
    def test_markdown_output(self) -> None:
        ctx = {
            "trace_id": "abc123",
            "workflow": "payment",
            "failed_operation": "charge_card",
            "error_type": "ValueError",
            "error_message": "Invalid card number",
            "captured_params": {"order_id": "ORD-123"},
            "span_chain": ["create_order", "charge_card"],
            "failure_count": 1,
        }
        mock_client = _make_mock_client({})
        simulator = FailureSimulator(query_client=mock_client)

        md = simulator.print_diagnosis(ctx)

        assert "## Teracron Failure Diagnosis" in md
        assert "payment" in md
        assert "charge_card" in md
        assert "ValueError" in md
        assert "ORD-123" in md
        assert "Suggested Investigation" in md

    def test_no_captured_params(self) -> None:
        ctx = {
            "trace_id": "abc123",
            "workflow": "payment",
            "failed_operation": "process",
            "error_type": "RuntimeError",
            "error_message": "Failed",
            "captured_params": None,
            "span_chain": [],
            "failure_count": 1,
        }
        mock_client = _make_mock_client({})
        simulator = FailureSimulator(query_client=mock_client)

        md = simulator.print_diagnosis(ctx)
        assert "No parameters were captured" in md

    def test_error_context(self) -> None:
        ctx = {"error": "Something went wrong"}
        mock_client = _make_mock_client({})
        simulator = FailureSimulator(query_client=mock_client)

        md = simulator.print_diagnosis(ctx)
        assert "Error" in md
