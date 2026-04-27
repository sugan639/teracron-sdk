# -*- coding: utf-8 -*-
"""
Unit tests for the PII scrubber hook.

Tests that:
- Scrubber is applied to metadata and captured_params.
- Scrubber exceptions are caught (never crash user's app).
- Scrubber=None is a passthrough.
- Scrubber receives a shallow copy (caller's data unmodified).
- Scrubber that returns non-dict results in data being dropped.
- End-to-end integration with @trace decorator.
"""

import asyncio
from unittest import mock

import pytest

from teracron.tracing.context import clear_trace
from teracron.tracing.decorator import _apply_scrubber, trace, trace_context


# ── _apply_scrubber unit tests ──


class TestApplyScrubber:
    """Direct tests for the _apply_scrubber function."""

    def test_none_scrubber_passthrough(self):
        data = {"key": "value"}
        result = _apply_scrubber(None, data)
        assert result == {"key": "value"}

    def test_none_data_passthrough(self):
        scrubber = lambda d: d  # noqa: E731
        result = _apply_scrubber(scrubber, None)
        assert result is None

    def test_scrubber_removes_fields(self):
        def scrubber(d):
            d.pop("email", None)
            d.pop("ssn", None)
            return d

        data = {"user_id": "U-1", "email": "a@b.com", "ssn": "123-45-6789"}
        result = _apply_scrubber(scrubber, data)
        assert result == {"user_id": "U-1"}

    def test_scrubber_returns_new_dict(self):
        def scrubber(d):
            return {"scrubbed": True}

        data = {"sensitive": "data"}
        result = _apply_scrubber(scrubber, data)
        assert result == {"scrubbed": True}

    def test_scrubber_receives_copy(self):
        """Scrubber should receive a shallow copy — original unmodified."""
        original = {"key": "value", "secret": "data"}
        received = {}

        def scrubber(d):
            received.update(d)
            d.pop("secret", None)
            return d

        _apply_scrubber(scrubber, original)
        # Original should be unmodified.
        assert "secret" in original
        # Scrubber received the original data.
        assert received == {"key": "value", "secret": "data"}

    def test_scrubber_exception_drops_data(self):
        """If scrubber raises, data is dropped (PII safety)."""
        def scrubber(d):
            raise RuntimeError("scrubber bug")

        data = {"sensitive": "data"}
        result = _apply_scrubber(scrubber, data)
        assert result is None

    def test_scrubber_returns_non_dict_drops_data(self):
        def scrubber(d):
            return "not a dict"

        data = {"key": "value"}
        result = _apply_scrubber(scrubber, data)
        assert result is None

    def test_scrubber_returns_empty_dict_returns_none(self):
        def scrubber(d):
            return {}

        data = {"key": "value"}
        result = _apply_scrubber(scrubber, data)
        assert result is None

    def test_scrubber_returns_none_drops_data(self):
        def scrubber(d):
            return None  # type: ignore

        data = {"key": "value"}
        result = _apply_scrubber(scrubber, data)
        assert result is None


# ── Integration with @trace decorator ──


def _make_mock_client(tracing_enabled=True, scrubber=None, sample_rate=1.0):
    client = mock.MagicMock()
    client.config.tracing_enabled = tracing_enabled
    client.config.trace_sample_rate = sample_rate
    client._push_trace_span = mock.MagicMock()
    client._scrubber = scrubber
    return client


class TestScrubberWithDecorator:
    """Integration: scrubber applied via @trace decorator."""

    def setup_method(self):
        clear_trace()

    def teardown_method(self):
        clear_trace()

    def test_scrubber_applied_to_captured_params(self):
        def scrubber(d):
            d.pop("email", None)
            return d

        client = _make_mock_client(scrubber=scrubber)

        @trace("w", capture=["user_id", "email"])
        def func(user_id, email):
            return "ok"

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            func("U-1", "a@b.com")

        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["captured_params"] == {"user_id": "U-1"}
        # email must not appear
        assert "email" not in str(span_dict["captured_params"])

    def test_scrubber_applied_to_context_manager_metadata(self):
        def scrubber(d):
            d.pop("secret", None)
            return d

        client = _make_mock_client(scrubber=scrubber)

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            with trace_context("w") as span:
                span.set_metadata({"order_id": "ORD-1", "secret": "s3cr3t"})

        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["metadata"] == {"order_id": "ORD-1"}
        assert "secret" not in str(span_dict["metadata"])

    def test_scrubber_exception_drops_params_safely(self):
        def bad_scrubber(d):
            raise ValueError("scrubber bug")

        client = _make_mock_client(scrubber=bad_scrubber)

        @trace("w", capture=["user_id"])
        def func(user_id):
            return "ok"

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            func("U-1")

        span_dict = client._push_trace_span.call_args[0][0]
        # Params dropped due to scrubber error — PII safety.
        assert span_dict["captured_params"] is None

    def test_no_scrubber_passthrough(self):
        client = _make_mock_client(scrubber=None)

        @trace("w", capture=["order_id"])
        def func(order_id):
            return "ok"

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            func("ORD-1")

        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["captured_params"] == {"order_id": "ORD-1"}

    def test_scrubber_applied_on_failure_path(self):
        def scrubber(d):
            d.pop("token", None)
            return d

        client = _make_mock_client(scrubber=scrubber)

        @trace("w", capture=["token", "action"])
        def func(token, action):
            raise ValueError("fail")

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            with pytest.raises(ValueError):
                func("s3cr3t", "buy")

        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["status"] == "failed"
        assert span_dict["captured_params"] == {"action": "buy"}
        assert "s3cr3t" not in str(span_dict)

    def test_async_scrubber_applied(self):
        def scrubber(d):
            d.pop("password", None)
            return d

        client = _make_mock_client(scrubber=scrubber)

        @trace("w", capture=["username", "password"])
        async def func(username, password):
            return "ok"

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            asyncio.get_event_loop().run_until_complete(func("admin", "s3cr3t"))

        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["captured_params"] == {"username": "admin"}
        assert "s3cr3t" not in str(span_dict)
