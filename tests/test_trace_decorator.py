# -*- coding: utf-8 -*-
"""
Unit tests for @trace decorator, trace_context, async_trace_context,
parameter capture, metadata, and nesting — Phase 2.
"""

import asyncio
from unittest import mock

import pytest

from teracron.tracing.decorator import (
    SpanHandle,
    async_trace_context,
    trace,
    trace_context,
)
from teracron.tracing.context import clear_trace, get_trace_id, start_trace
from teracron.tracing.sampling import clear_sampling_decision


# ── Helpers ──

def _make_mock_client(tracing_enabled=True):
    """Create a mock TeracronClient with the minimum interface for @trace."""
    client = mock.MagicMock()
    client.config.tracing_enabled = tracing_enabled
    client.config.trace_sample_rate = 1.0
    client._push_trace_span = mock.MagicMock()
    client._scrubber = None
    return client


# ── Tests: Sync decorator ──


class TestTraceDecoratorSync:
    """Tests for @trace on synchronous functions."""

    def setup_method(self):
        clear_trace()
        clear_sampling_decision()

    def teardown_method(self):
        clear_trace()
        clear_sampling_decision()

    def test_returns_function_result(self):
        client = _make_mock_client()

        @trace("payment")
        def add(a, b):
            return a + b

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            result = add(2, 3)

        assert result == 5

    def test_preserves_function_metadata(self):
        @trace("workflow")
        def my_function():
            """Docstring."""
            pass

        assert my_function.__name__ == "my_function"
        assert my_function.__doc__ == "Docstring."

    def test_span_pushed_on_success(self):
        client = _make_mock_client()

        @trace("payment")
        def noop():
            return "ok"

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            noop()

        client._push_trace_span.assert_called_once()
        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["workflow"] == "payment"
        assert span_dict["status"] == "succeeded"
        assert span_dict["duration_ms"] >= 0
        assert span_dict["error_type"] is None
        assert span_dict["error_message"] is None

    def test_span_operation_is_qualname(self):
        client = _make_mock_client()

        @trace("w")
        def my_func():
            pass

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            my_func()

        span_dict = client._push_trace_span.call_args[0][0]
        assert "my_func" in span_dict["operation"]

    def test_exception_is_reraised(self):
        client = _make_mock_client()

        @trace("payment")
        def fail():
            raise ValueError("bad input")

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            with pytest.raises(ValueError, match="bad input"):
                fail()

    def test_span_records_error_on_exception(self):
        client = _make_mock_client()

        @trace("payment")
        def fail():
            raise TypeError("wrong type")

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            with pytest.raises(TypeError):
                fail()

        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["status"] == "failed"
        assert span_dict["error_type"] == "TypeError"
        assert span_dict["error_message"] == "wrong type"
        assert span_dict["duration_ms"] >= 0

    def test_root_span_auto_creates_trace_id(self):
        client = _make_mock_client()

        @trace("w")
        def func():
            pass

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            func()

        span_dict = client._push_trace_span.call_args[0][0]
        assert isinstance(span_dict["trace_id"], str)
        assert len(span_dict["trace_id"]) == 32

    def test_trace_id_reuse_within_context(self):
        """Two @trace calls in the same trace context share trace_id."""
        client = _make_mock_client()

        @trace("w")
        def inner():
            pass

        @trace("w")
        def outer():
            inner()

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            outer()

        assert client._push_trace_span.call_count == 2
        spans = [call[0][0] for call in client._push_trace_span.call_args_list]
        assert spans[0]["trace_id"] == spans[1]["trace_id"]

    def test_clears_trace_after_root_span(self):
        client = _make_mock_client()

        @trace("w")
        def func():
            pass

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            func()

        assert get_trace_id() is None


class TestTraceDecoratorNesting:
    """Phase 2: nested spans with parent-child relationships."""

    def setup_method(self):
        clear_trace()
        clear_sampling_decision()

    def teardown_method(self):
        clear_trace()
        clear_sampling_decision()

    def test_nested_calls_set_parent_span_id(self):
        client = _make_mock_client()

        @trace("w")
        def inner():
            pass

        @trace("w")
        def outer():
            inner()

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            outer()

        assert client._push_trace_span.call_count == 2
        # inner completes first (pushed first), outer second
        inner_span = client._push_trace_span.call_args_list[0][0][0]
        outer_span = client._push_trace_span.call_args_list[1][0][0]

        # Outer is root — no parent
        assert outer_span["parent_span_id"] is None
        # Inner's parent should be outer's span_id
        assert inner_span["parent_span_id"] == outer_span["span_id"]

    def test_triple_nesting(self):
        client = _make_mock_client()

        @trace("w")
        def c():
            pass

        @trace("w")
        def b():
            c()

        @trace("w")
        def a():
            b()

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            a()

        assert client._push_trace_span.call_count == 3
        # Push order: c, b, a (innermost completes first)
        span_c = client._push_trace_span.call_args_list[0][0][0]
        span_b = client._push_trace_span.call_args_list[1][0][0]
        span_a = client._push_trace_span.call_args_list[2][0][0]

        assert span_a["parent_span_id"] is None
        assert span_b["parent_span_id"] == span_a["span_id"]
        assert span_c["parent_span_id"] == span_b["span_id"]

    def test_sibling_spans_have_same_parent(self):
        client = _make_mock_client()

        @trace("w")
        def child_a():
            pass

        @trace("w")
        def child_b():
            pass

        @trace("w")
        def parent():
            child_a()
            child_b()

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            parent()

        assert client._push_trace_span.call_count == 3
        span_child_a = client._push_trace_span.call_args_list[0][0][0]
        span_child_b = client._push_trace_span.call_args_list[1][0][0]
        span_parent = client._push_trace_span.call_args_list[2][0][0]

        assert span_child_a["parent_span_id"] == span_parent["span_id"]
        assert span_child_b["parent_span_id"] == span_parent["span_id"]

    def test_nesting_cleans_up_stack_on_exception(self):
        client = _make_mock_client()

        @trace("w")
        def failing_inner():
            raise RuntimeError("boom")

        @trace("w")
        def outer():
            try:
                failing_inner()
            except RuntimeError:
                pass

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            outer()

        # Both spans should be pushed despite the inner exception
        assert client._push_trace_span.call_count == 2
        # Trace context should be clean
        assert get_trace_id() is None


class TestTraceDecoratorCapture:
    """Phase 2: opt-in parameter capture."""

    def setup_method(self):
        clear_trace()
        clear_sampling_decision()

    def teardown_method(self):
        clear_trace()
        clear_sampling_decision()

    def test_no_capture_by_default(self):
        client = _make_mock_client()

        @trace("w")
        def func(user_id, password):
            return "ok"

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            func("user_123", "s3cret!")

        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["captured_params"] is None

    def test_captures_whitelisted_param(self):
        client = _make_mock_client()

        @trace("w", capture=["order_id"])
        def func(order_id, amount, card_number):
            return "ok"

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            func("ORD-123", 99.99, "4111-xxxx")

        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["captured_params"] is not None
        assert span_dict["captured_params"]["order_id"] == "ORD-123"
        # Non-whitelisted params MUST NOT appear
        assert "amount" not in span_dict["captured_params"]
        assert "card_number" not in span_dict["captured_params"]

    def test_captures_multiple_params(self):
        client = _make_mock_client()

        @trace("w", capture=["order_id", "amount"])
        def func(order_id, amount, secret):
            return "ok"

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            func("ORD-1", 50.0, "top-secret")

        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["captured_params"]["order_id"] == "ORD-1"
        assert span_dict["captured_params"]["amount"] == 50.0
        assert "secret" not in span_dict["captured_params"]

    def test_capture_kwarg_params(self):
        client = _make_mock_client()

        @trace("w", capture=["status"])
        def func(name, status="pending"):
            return "ok"

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            func("test", status="active")

        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["captured_params"]["status"] == "active"

    def test_capture_nonexistent_param_ignored(self):
        client = _make_mock_client()

        @trace("w", capture=["nonexistent"])
        def func(a, b):
            return "ok"

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            func(1, 2)

        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["captured_params"] is None

    def test_capture_complex_type_converted_to_repr(self):
        client = _make_mock_client()

        @trace("w", capture=["items"])
        def func(items):
            return "ok"

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            func([1, 2, 3])

        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["captured_params"]["items"] == "[1, 2, 3]"

    def test_capture_persists_on_failure(self):
        client = _make_mock_client()

        @trace("w", capture=["order_id"])
        def func(order_id):
            raise ValueError("bad")

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            with pytest.raises(ValueError):
                func("ORD-FAIL")

        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["status"] == "failed"
        assert span_dict["captured_params"]["order_id"] == "ORD-FAIL"

    def test_capture_with_default_values(self):
        client = _make_mock_client()

        @trace("w", capture=["retries"])
        def func(name, retries=3):
            return "ok"

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            func("test")  # retries uses default

        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["captured_params"]["retries"] == 3


class TestTraceDecoratorAsync:
    """Tests for @trace on async functions."""

    def setup_method(self):
        clear_trace()
        clear_sampling_decision()

    def teardown_method(self):
        clear_trace()
        clear_sampling_decision()

    def test_async_returns_result(self):
        client = _make_mock_client()

        @trace("w")
        async def compute():
            return 42

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            result = asyncio.get_event_loop().run_until_complete(compute())

        assert result == 42

    def test_async_span_pushed(self):
        client = _make_mock_client()

        @trace("async_wf")
        async def work():
            return "done"

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            asyncio.get_event_loop().run_until_complete(work())

        client._push_trace_span.assert_called_once()
        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["workflow"] == "async_wf"
        assert span_dict["status"] == "succeeded"

    def test_async_exception_reraised(self):
        client = _make_mock_client()

        @trace("w")
        async def fail():
            raise RuntimeError("async fail")

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            with pytest.raises(RuntimeError, match="async fail"):
                asyncio.get_event_loop().run_until_complete(fail())

    def test_async_span_records_error(self):
        client = _make_mock_client()

        @trace("w")
        async def fail():
            raise IOError("disk full")

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            with pytest.raises(IOError):
                asyncio.get_event_loop().run_until_complete(fail())

        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["status"] == "failed"
        assert span_dict["error_type"] == "OSError"  # IOError is alias
        assert "disk full" in span_dict["error_message"]

    def test_async_preserves_function_metadata(self):
        @trace("w")
        async def my_async_func():
            """Async docstring."""
            pass

        assert my_async_func.__name__ == "my_async_func"
        assert my_async_func.__doc__ == "Async docstring."

    def test_async_nesting(self):
        client = _make_mock_client()

        @trace("w")
        async def inner():
            return "inner"

        @trace("w")
        async def outer():
            return await inner()

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            result = asyncio.get_event_loop().run_until_complete(outer())

        assert result == "inner"
        assert client._push_trace_span.call_count == 2
        inner_span = client._push_trace_span.call_args_list[0][0][0]
        outer_span = client._push_trace_span.call_args_list[1][0][0]
        assert inner_span["parent_span_id"] == outer_span["span_id"]

    def test_async_capture(self):
        client = _make_mock_client()

        @trace("w", capture=["order_id"])
        async def func(order_id, secret):
            return "ok"

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            asyncio.get_event_loop().run_until_complete(func("ORD-1", "s3cr3t"))

        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["captured_params"]["order_id"] == "ORD-1"
        assert "secret" not in span_dict["captured_params"]


class TestTraceDecoratorErrorPaths:
    """Tests for error conditions and edge cases."""

    def setup_method(self):
        clear_trace()
        clear_sampling_decision()

    def teardown_method(self):
        clear_trace()
        clear_sampling_decision()

    def test_raises_runtime_error_without_client(self):
        @trace("w")
        def func():
            pass

        with mock.patch("teracron.tracing.decorator._get_client", return_value=None):
            with pytest.raises(RuntimeError, match="teracron.up"):
                func()

    def test_async_raises_runtime_error_without_client(self):
        @trace("w")
        async def func():
            pass

        with mock.patch("teracron.tracing.decorator._get_client", return_value=None):
            with pytest.raises(RuntimeError, match="teracron.up"):
                asyncio.get_event_loop().run_until_complete(func())

    def test_tracing_disabled_bypasses(self):
        client = _make_mock_client(tracing_enabled=False)

        @trace("w")
        def func():
            return "executed"

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            result = func()

        assert result == "executed"
        client._push_trace_span.assert_not_called()

    def test_async_tracing_disabled_bypasses(self):
        client = _make_mock_client(tracing_enabled=False)

        @trace("w")
        async def func():
            return "async executed"

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            result = asyncio.get_event_loop().run_until_complete(func())

        assert result == "async executed"
        client._push_trace_span.assert_not_called()

    def test_empty_workflow_name_raises_at_definition(self):
        with pytest.raises(ValueError, match="non-empty workflow name"):
            @trace("")
            def func():
                pass

    def test_non_string_workflow_raises_at_definition(self):
        with pytest.raises(ValueError, match="non-empty workflow name"):
            @trace(123)  # type: ignore[arg-type]
            def func():
                pass


class TestTraceContextManager:
    """Phase 2: trace_context sync context manager."""

    def setup_method(self):
        clear_trace()
        clear_sampling_decision()

    def teardown_method(self):
        clear_trace()
        clear_sampling_decision()

    def test_basic_context_manager(self):
        client = _make_mock_client()

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            with trace_context("payment", operation="validate") as span:
                span.set_metadata({"order_id": "ORD-123"})

        client._push_trace_span.assert_called_once()
        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["workflow"] == "payment"
        assert span_dict["operation"] == "validate"
        assert span_dict["status"] == "succeeded"
        assert span_dict["metadata"] == {"order_id": "ORD-123"}

    def test_context_manager_default_operation(self):
        client = _make_mock_client()

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            with trace_context("w") as span:
                pass

        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["operation"] == "<context_manager>"

    def test_context_manager_on_exception(self):
        client = _make_mock_client()

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            with pytest.raises(ValueError, match="oops"):
                with trace_context("w", operation="op") as span:
                    raise ValueError("oops")

        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["status"] == "failed"
        assert span_dict["error_type"] == "ValueError"
        assert span_dict["error_message"] == "oops"

    def test_context_manager_requires_client(self):
        with mock.patch("teracron.tracing.decorator._get_client", return_value=None):
            with pytest.raises(RuntimeError, match="teracron.up"):
                with trace_context("w") as span:
                    pass

    def test_context_manager_bypass_when_disabled(self):
        client = _make_mock_client(tracing_enabled=False)

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            with trace_context("w") as span:
                span.set_metadata({"key": "val"})

        client._push_trace_span.assert_not_called()

    def test_context_manager_nesting_with_decorator(self):
        client = _make_mock_client()

        @trace("w")
        def inner():
            pass

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            with trace_context("w", operation="outer") as span:
                inner()

        assert client._push_trace_span.call_count == 2
        inner_span = client._push_trace_span.call_args_list[0][0][0]
        outer_span = client._push_trace_span.call_args_list[1][0][0]
        assert inner_span["parent_span_id"] == outer_span["span_id"]

    def test_span_handle_metadata_merges(self):
        client = _make_mock_client()

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            with trace_context("w") as span:
                span.set_metadata({"a": 1})
                span.set_metadata({"b": 2})

        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["metadata"] == {"a": 1, "b": 2}


class TestAsyncTraceContextManager:
    """Phase 2: async_trace_context async context manager."""

    def setup_method(self):
        clear_trace()
        clear_sampling_decision()

    def teardown_method(self):
        clear_trace()
        clear_sampling_decision()

    def test_async_context_manager(self):
        client = _make_mock_client()

        async def run():
            async with async_trace_context("payment", operation="verify") as span:
                span.set_metadata({"txn": "T-001"})

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            asyncio.get_event_loop().run_until_complete(run())

        client._push_trace_span.assert_called_once()
        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["workflow"] == "payment"
        assert span_dict["operation"] == "verify"
        assert span_dict["metadata"] == {"txn": "T-001"}

    def test_async_context_manager_on_exception(self):
        client = _make_mock_client()

        async def run():
            async with async_trace_context("w") as span:
                raise RuntimeError("async boom")

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            with pytest.raises(RuntimeError, match="async boom"):
                asyncio.get_event_loop().run_until_complete(run())

        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["status"] == "failed"
        assert span_dict["error_type"] == "RuntimeError"

    def test_async_context_manager_requires_client(self):
        async def run():
            async with async_trace_context("w") as span:
                pass

        with mock.patch("teracron.tracing.decorator._get_client", return_value=None):
            with pytest.raises(RuntimeError, match="teracron.up"):
                asyncio.get_event_loop().run_until_complete(run())

    def test_async_context_manager_bypass_when_disabled(self):
        client = _make_mock_client(tracing_enabled=False)

        async def run():
            async with async_trace_context("w") as span:
                span.set_metadata({"key": "val"})

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            asyncio.get_event_loop().run_until_complete(run())

        client._push_trace_span.assert_not_called()


class TestSpanHandle:
    """Tests for the SpanHandle class."""

    def test_initial_metadata_empty(self):
        handle = SpanHandle()
        assert handle.metadata == {}

    def test_set_metadata(self):
        handle = SpanHandle()
        handle.set_metadata({"key": "val"})
        assert handle.metadata == {"key": "val"}

    def test_set_metadata_merges(self):
        handle = SpanHandle()
        handle.set_metadata({"a": 1})
        handle.set_metadata({"b": 2})
        assert handle.metadata == {"a": 1, "b": 2}

    def test_set_metadata_overwrites(self):
        handle = SpanHandle()
        handle.set_metadata({"a": 1})
        handle.set_metadata({"a": 99})
        assert handle.metadata == {"a": 99}

    def test_set_metadata_ignores_non_dict(self):
        handle = SpanHandle()
        handle.set_metadata("not a dict")  # type: ignore
        assert handle.metadata == {}
