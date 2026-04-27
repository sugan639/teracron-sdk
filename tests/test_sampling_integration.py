# -*- coding: utf-8 -*-
"""
Integration tests for sampling with @trace decorator and context managers.
"""

import asyncio
from unittest import mock

import pytest

from teracron.tracing.context import clear_trace
from teracron.tracing.decorator import trace, trace_context, async_trace_context
from teracron.tracing.sampling import clear_sampling_decision


def _make_mock_client(tracing_enabled=True, sample_rate=1.0, scrubber=None):
    client = mock.MagicMock()
    client.config.tracing_enabled = tracing_enabled
    client.config.trace_sample_rate = sample_rate
    client._push_trace_span = mock.MagicMock()
    client._scrubber = scrubber
    return client


class TestSamplingWithDecorator:
    """Tests for sampling integration with @trace."""

    def setup_method(self):
        clear_trace()
        clear_sampling_decision()

    def teardown_method(self):
        clear_trace()
        clear_sampling_decision()

    def test_rate_1_always_records(self):
        client = _make_mock_client(sample_rate=1.0)

        @trace("w")
        def func():
            return "ok"

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            func()

        client._push_trace_span.assert_called_once()

    def test_rate_0_never_records(self):
        client = _make_mock_client(sample_rate=0.0)

        @trace("w")
        def func():
            return "ok"

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            func()

        client._push_trace_span.assert_not_called()

    def test_rate_0_still_executes_function(self):
        """Sampling skips recording, but function MUST still execute."""
        client = _make_mock_client(sample_rate=0.0)
        executed = []

        @trace("w")
        def func():
            executed.append(True)
            return "result"

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            result = func()

        assert result == "result"
        assert executed == [True]
        client._push_trace_span.assert_not_called()

    def test_rate_0_still_reraises_exceptions(self):
        """Exceptions re-raised even when trace is not sampled."""
        client = _make_mock_client(sample_rate=0.0)

        @trace("w")
        def func():
            raise ValueError("fail")

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            with pytest.raises(ValueError, match="fail"):
                func()

        client._push_trace_span.assert_not_called()

    def test_nested_spans_share_sampling_decision(self):
        """All spans in a trace share the root's sampling decision."""
        client = _make_mock_client(sample_rate=0.0)

        @trace("w")
        def inner():
            return "inner"

        @trace("w")
        def outer():
            return inner()

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            outer()

        # Both spans should be skipped (root decided not to sample).
        client._push_trace_span.assert_not_called()

    def test_nested_rate_1_all_recorded(self):
        client = _make_mock_client(sample_rate=1.0)

        @trace("w")
        def inner():
            return "inner"

        @trace("w")
        def outer():
            return inner()

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            outer()

        assert client._push_trace_span.call_count == 2

    def test_context_clears_between_traces(self):
        """Sampling decision should reset between independent traces."""
        client = _make_mock_client(sample_rate=1.0)

        @trace("w")
        def func():
            return "ok"

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            func()
            func()

        assert client._push_trace_span.call_count == 2

    def test_async_rate_0_skips_recording(self):
        client = _make_mock_client(sample_rate=0.0)

        @trace("w")
        async def func():
            return "async_result"

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            result = asyncio.get_event_loop().run_until_complete(func())

        assert result == "async_result"
        client._push_trace_span.assert_not_called()


class TestSamplingWithContextManager:
    """Tests for sampling with trace_context."""

    def setup_method(self):
        clear_trace()
        clear_sampling_decision()

    def teardown_method(self):
        clear_trace()
        clear_sampling_decision()

    def test_context_manager_rate_0_skips(self):
        client = _make_mock_client(sample_rate=0.0)

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            with trace_context("w") as span:
                span.set_metadata({"key": "val"})

        client._push_trace_span.assert_not_called()

    def test_context_manager_rate_1_records(self):
        client = _make_mock_client(sample_rate=1.0)

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            with trace_context("w", operation="op") as span:
                span.set_metadata({"key": "val"})

        client._push_trace_span.assert_called_once()

    def test_async_context_manager_rate_0_skips(self):
        client = _make_mock_client(sample_rate=0.0)

        async def run():
            async with async_trace_context("w") as span:
                span.set_metadata({"key": "val"})

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            asyncio.get_event_loop().run_until_complete(run())

        client._push_trace_span.assert_not_called()
