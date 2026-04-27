# -*- coding: utf-8 -*-
"""
Phase 2 — comprehensive tests for opt-in parameter capture, metadata
sanitisation, PII boundary enforcement, and edge cases.
"""

import asyncio
from unittest import mock

import pytest

from teracron.tracing.decorator import trace, trace_context, _extract_captured_params
from teracron.tracing.context import clear_trace
from teracron.tracing.sampling import clear_sampling_decision
from teracron.tracing.span import (
    _sanitise_captured_params,
    _sanitise_metadata,
)
from teracron.types import (
    CAPTURE_MAX_VALUE_LEN,
    METADATA_ALLOWED_TYPES,
    METADATA_MAX_KEY_LEN,
    METADATA_MAX_KEYS,
    METADATA_MAX_VALUE_LEN,
)


# ── Helpers ──

def _make_mock_client(tracing_enabled=True):
    client = mock.MagicMock()
    client.config.tracing_enabled = tracing_enabled
    client.config.trace_sample_rate = 1.0
    client._push_trace_span = mock.MagicMock()
    client._scrubber = None
    return client


# ── _extract_captured_params unit tests ──


class TestExtractCapturedParams:
    """Direct tests for the parameter extraction function."""

    def test_empty_capture_returns_none(self):
        def func(a, b):
            pass
        result = _extract_captured_params(func, [], (1, 2), {})
        assert result is None

    def test_extracts_positional_args(self):
        def func(order_id, amount, secret):
            pass
        result = _extract_captured_params(func, ["order_id", "amount"], ("ORD-1", 50, "s3cr3t"), {})
        assert result == {"order_id": "ORD-1", "amount": 50}
        assert "secret" not in result

    def test_extracts_kwargs(self):
        def func(name, status="pending"):
            pass
        result = _extract_captured_params(func, ["status"], ("test",), {"status": "active"})
        assert result == {"status": "active"}

    def test_extracts_defaults_when_not_passed(self):
        def func(name, retries=3):
            pass
        result = _extract_captured_params(func, ["retries"], ("test",), {})
        assert result == {"retries": 3}

    def test_nonexistent_param_returns_none(self):
        def func(a, b):
            pass
        result = _extract_captured_params(func, ["nonexistent"], (1, 2), {})
        assert result is None

    def test_partial_match(self):
        def func(a, b, c):
            pass
        result = _extract_captured_params(func, ["a", "nonexistent"], (1, 2, 3), {})
        assert result == {"a": 1}

    def test_does_not_capture_unlisted_params(self):
        def func(user_id, password, email, ssn):
            pass
        result = _extract_captured_params(func, ["user_id"], ("u1", "p@ss", "e@mail", "123-45"), {})
        assert result == {"user_id": "u1"}
        assert "password" not in result
        assert "email" not in result
        assert "ssn" not in result


# ── Metadata sanitisation ──


class TestSanitiseMetadata:
    """Tests for metadata validation and sanitisation."""

    def test_valid_metadata_passes_through(self):
        data = {"key": "value", "count": 42, "rate": 3.14, "active": True}
        result = _sanitise_metadata(data)
        assert result == data

    def test_non_dict_returns_none(self):
        assert _sanitise_metadata("not a dict") is None  # type: ignore
        assert _sanitise_metadata(42) is None  # type: ignore
        assert _sanitise_metadata(None) is None  # type: ignore

    def test_non_string_keys_dropped(self):
        result = _sanitise_metadata({123: "bad", "good": "val"})  # type: ignore
        assert result == {"good": "val"}

    def test_empty_key_dropped(self):
        result = _sanitise_metadata({"": "empty_key", "ok": "val"})
        assert result == {"ok": "val"}

    def test_long_key_dropped(self):
        long_key = "k" * (METADATA_MAX_KEY_LEN + 1)
        result = _sanitise_metadata({long_key: "val", "ok": "val"})
        assert result == {"ok": "val"}

    def test_invalid_value_types_dropped(self):
        result = _sanitise_metadata({
            "list": [1, 2, 3],
            "dict": {"nested": True},
            "set": {1, 2},
            "none": None,
            "good": "ok",
        })
        assert result == {"good": "ok"}

    def test_long_string_value_truncated(self):
        long_val = "x" * (METADATA_MAX_VALUE_LEN + 100)
        result = _sanitise_metadata({"key": long_val})
        assert result is not None
        assert len(result["key"]) == METADATA_MAX_VALUE_LEN

    def test_max_keys_enforced(self):
        data = {f"key_{i}": i for i in range(METADATA_MAX_KEYS + 10)}
        result = _sanitise_metadata(data)
        assert result is not None
        assert len(result) == METADATA_MAX_KEYS

    def test_empty_after_filtering_returns_none(self):
        result = _sanitise_metadata({123: [1, 2]})  # type: ignore
        assert result is None

    def test_bool_values_accepted(self):
        result = _sanitise_metadata({"flag": True, "disabled": False})
        assert result == {"flag": True, "disabled": False}

    def test_int_and_float_values_accepted(self):
        result = _sanitise_metadata({"count": 42, "rate": 3.14})
        assert result == {"count": 42, "rate": 3.14}


# ── Captured params sanitisation ──


class TestSanitiseCapturedParams:
    """Tests for captured parameter sanitisation."""

    def test_primitive_values_pass_through(self):
        data = {"a": "str", "b": 42, "c": 3.14, "d": True}
        result = _sanitise_captured_params(data)
        assert result == data

    def test_complex_value_converted_to_repr(self):
        result = _sanitise_captured_params({"items": [1, 2, 3]})
        assert result is not None
        assert result["items"] == "[1, 2, 3]"

    def test_dict_value_converted_to_repr(self):
        result = _sanitise_captured_params({"config": {"key": "val"}})
        assert result is not None
        assert isinstance(result["config"], str)
        assert "key" in result["config"]

    def test_long_string_truncated(self):
        long_val = "y" * (CAPTURE_MAX_VALUE_LEN + 100)
        result = _sanitise_captured_params({"data": long_val})
        assert result is not None
        assert len(result["data"]) == CAPTURE_MAX_VALUE_LEN

    def test_long_repr_truncated(self):
        huge_list = list(range(1000))
        result = _sanitise_captured_params({"big": huge_list})
        assert result is not None
        assert len(result["big"]) == CAPTURE_MAX_VALUE_LEN

    def test_non_dict_returns_none(self):
        assert _sanitise_captured_params("bad") is None  # type: ignore

    def test_empty_after_filtering_returns_none(self):
        assert _sanitise_captured_params({}) is None


# ── Integration: PII boundary enforcement ──


class TestPIIBoundary:
    """
    End-to-end tests verifying that parameter values are NEVER sent
    unless explicitly whitelisted via the ``capture`` argument.
    """

    def setup_method(self):
        clear_trace()
        clear_sampling_decision()

    def teardown_method(self):
        clear_trace()
        clear_sampling_decision()

    def test_sensitive_params_never_leaked_without_capture(self):
        """Even if a function has PII params, they must not appear in spans."""
        client = _make_mock_client()

        @trace("auth")
        def login(username, password, mfa_token):
            return "session_123"

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            login("admin", "s3cr3t!", "123456")

        span_dict = client._push_trace_span.call_args[0][0]
        # No captured_params at all
        assert span_dict["captured_params"] is None
        # Verify the actual param values don't appear anywhere in the span
        span_str = str(span_dict)
        assert "s3cr3t" not in span_str
        assert "123456" not in span_str

    def test_only_whitelisted_params_captured(self):
        """Capture=["username"] must NOT leak password or mfa_token."""
        client = _make_mock_client()

        @trace("auth", capture=["username"])
        def login(username, password, mfa_token):
            return "session_123"

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            login("admin", "s3cr3t!", "123456")

        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["captured_params"] == {"username": "admin"}
        assert "s3cr3t" not in str(span_dict)
        assert "123456" not in str(span_dict)

    def test_async_pii_boundary(self):
        """Same PII boundary enforcement for async functions."""
        client = _make_mock_client()

        @trace("auth", capture=["user_id"])
        async def fetch_profile(user_id, auth_token):
            return {"name": "Alice"}

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            asyncio.get_event_loop().run_until_complete(
                fetch_profile("U-001", "Bearer abc123def")
            )

        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["captured_params"] == {"user_id": "U-001"}
        assert "Bearer" not in str(span_dict)
        assert "abc123def" not in str(span_dict)

    def test_tracing_disabled_leaks_nothing(self):
        """When tracing is disabled, absolutely nothing is captured."""
        client = _make_mock_client(tracing_enabled=False)

        @trace("w", capture=["secret"])
        def func(secret):
            return "ok"

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            result = func("top_secret_value")

        assert result == "ok"
        client._push_trace_span.assert_not_called()

    def test_context_manager_metadata_no_pii_by_default(self):
        """Context manager does not auto-capture anything."""
        client = _make_mock_client()

        with mock.patch("teracron.tracing.decorator._get_client", return_value=client):
            with trace_context("w") as span:
                # User explicitly sets only what they want
                pass

        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["metadata"] is None
        assert span_dict["captured_params"] is None
