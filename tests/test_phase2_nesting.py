# -*- coding: utf-8 -*-
"""
Phase 2 — integration tests for nested spans, cross-process propagation,
and combined context manager + decorator workflows.
"""

import asyncio
from unittest import mock

import pytest

from teracron.tracing.context import (
    clear_trace,
    get_trace_header,
    get_trace_id,
    peek_parent_span_id,
    set_trace_header,
    start_trace,
)
from teracron.tracing.decorator import (
    async_trace_context,
    trace,
    trace_context,
)
from teracron.tracing.sampling import clear_sampling_decision


def _make_mock_client(tracing_enabled=True):
    client = mock.MagicMock()
    client.config.tracing_enabled = tracing_enabled
    client.config.trace_sample_rate = 1.0
    client._push_trace_span = mock.MagicMock()
    client._scrubber = None
    return client


class TestNestedWorkflow:
    """Real-world nested workflow scenarios."""

    def setup_method(self):
        clear_trace()
        clear_sampling_decision()

    def teardown_method(self):
        clear_trace()
        clear_sampling_decision()

    def test_full_payment_workflow(self):
        """Simulate a realistic nested payment workflow."""
        client = _make_mock_client()

        @trace("payment", capture=["order_id"])
        def validate_order(order_id):
            return True

        @trace("payment", capture=["order_id", "amount"])
        def charge_card(order_id, amount, card_number):
            return "txn_123"

        @trace("payment", capture=["order_id"])
        def send_receipt(order_id):
            return True

        @trace("payment")
        def process_payment(order_id, amount, card_number):
            validate_order(order_id)
            charge_card(order_id, amount, card_number)
            send_receipt(order_id)

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            process_payment("ORD-1", 99.99, "4111-1111-1111-1111")

        # 4 spans total
        assert client._push_trace_span.call_count == 4
        spans = [call[0][0] for call in client._push_trace_span.call_args_list]

        # All share the same trace_id
        trace_ids = {s["trace_id"] for s in spans}
        assert len(trace_ids) == 1

        # process_payment is root (pushed last — completes last)
        root = spans[3]
        assert root["parent_span_id"] is None
        assert root["captured_params"] is None  # no capture on process_payment

        # validate_order is child of process_payment
        validate = spans[0]
        assert validate["parent_span_id"] == root["span_id"]
        assert validate["captured_params"] == {"order_id": "ORD-1"}

        # charge_card is child of process_payment
        charge = spans[1]
        assert charge["parent_span_id"] == root["span_id"]
        assert charge["captured_params"] == {"order_id": "ORD-1", "amount": 99.99}
        # card_number must NOT appear
        assert "4111" not in str(charge)

        # send_receipt is child of process_payment
        receipt = spans[2]
        assert receipt["parent_span_id"] == root["span_id"]

    def test_mixed_context_manager_and_decorator(self):
        """Context manager wraps decorator calls — nesting preserved."""
        client = _make_mock_client()

        @trace("w")
        def step_a():
            pass

        @trace("w")
        def step_b():
            pass

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            with trace_context("w", operation="orchestrator") as span:
                span.set_metadata({"run_id": "R-001"})
                step_a()
                step_b()

        assert client._push_trace_span.call_count == 3
        span_a = client._push_trace_span.call_args_list[0][0][0]
        span_b = client._push_trace_span.call_args_list[1][0][0]
        orchestrator = client._push_trace_span.call_args_list[2][0][0]

        assert orchestrator["parent_span_id"] is None
        assert orchestrator["metadata"] == {"run_id": "R-001"}
        assert span_a["parent_span_id"] == orchestrator["span_id"]
        assert span_b["parent_span_id"] == orchestrator["span_id"]


class TestCrossProcessPropagation:
    """Simulate cross-service trace propagation via headers."""

    def setup_method(self):
        clear_trace()
        clear_sampling_decision()

    def teardown_method(self):
        clear_trace()
        clear_sampling_decision()

    def test_header_propagation_between_services(self):
        """
        Service A sends header → Service B restores context and continues.
        """
        client = _make_mock_client()

        # ── Service A ──
        @trace("checkout")
        def service_a_handler():
            return get_trace_header()

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            header = service_a_handler()

        assert header is not None
        # Header should be just trace_id (no parent at this point since
        # the span is still on the stack when get_trace_header is called)
        parts = header.split(":")
        assert len(parts) in (1, 2)

        clear_trace()

        # ── Service B ──
        set_trace_header(header)
        assert get_trace_id() == parts[0]

    def test_propagated_trace_creates_child_spans(self):
        """
        Spans created after set_trace_header should belong to the
        propagated trace.
        """
        client = _make_mock_client()
        trace_id = "a" * 32
        parent_id = "b" * 32

        set_trace_header(f"{trace_id}:{parent_id}")

        @trace("downstream")
        def handle_request():
            pass

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            handle_request()

        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["trace_id"] == trace_id
        assert span_dict["parent_span_id"] == parent_id


class TestAsyncNesting:
    """Phase 2: async nested workflows."""

    def setup_method(self):
        clear_trace()
        clear_sampling_decision()

    def teardown_method(self):
        clear_trace()
        clear_sampling_decision()

    def test_async_nested_workflow(self):
        client = _make_mock_client()

        @trace("w", capture=["item_id"])
        async def fetch_item(item_id):
            await asyncio.sleep(0)
            return {"id": item_id}

        @trace("w")
        async def process_order():
            await fetch_item("I-001")
            await fetch_item("I-002")

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            asyncio.get_event_loop().run_until_complete(process_order())

        assert client._push_trace_span.call_count == 3
        fetch1 = client._push_trace_span.call_args_list[0][0][0]
        fetch2 = client._push_trace_span.call_args_list[1][0][0]
        parent = client._push_trace_span.call_args_list[2][0][0]

        assert fetch1["parent_span_id"] == parent["span_id"]
        assert fetch2["parent_span_id"] == parent["span_id"]
        assert fetch1["captured_params"] == {"item_id": "I-001"}
        assert fetch2["captured_params"] == {"item_id": "I-002"}

    def test_async_context_manager_nesting(self):
        client = _make_mock_client()

        @trace("w")
        async def inner():
            pass

        async def run():
            async with async_trace_context("w", operation="outer") as span:
                span.set_metadata({"step": "validate"})
                await inner()

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            asyncio.get_event_loop().run_until_complete(run())

        assert client._push_trace_span.call_count == 2
        inner_span = client._push_trace_span.call_args_list[0][0][0]
        outer_span = client._push_trace_span.call_args_list[1][0][0]
        assert inner_span["parent_span_id"] == outer_span["span_id"]
        assert outer_span["metadata"] == {"step": "validate"}
