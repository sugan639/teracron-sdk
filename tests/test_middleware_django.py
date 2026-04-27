# -*- coding: utf-8 -*-
"""
Tests for Django middleware — uses mock request/response objects without
requiring Django installation.
"""

from unittest import mock

import pytest

from teracron.tracing.context import clear_trace
from teracron.tracing.middleware.django import TeracronTracingMiddleware
from teracron.tracing.sampling import clear_sampling_decision


def _make_mock_client(tracing_enabled=True, sample_rate=1.0, scrubber=None):
    client = mock.MagicMock()
    client.config.tracing_enabled = tracing_enabled
    client.config.trace_sample_rate = sample_rate
    client._push_trace_span = mock.MagicMock()
    client._scrubber = scrubber
    return client


def _make_request(method="GET", path="/test", trace_header=None):
    """Create a mock Django HttpRequest."""
    request = mock.MagicMock()
    request.method = method
    request.path = path
    meta = {}
    if trace_header:
        meta["HTTP_X_TERACRON_TRACE"] = trace_header
    request.META = meta
    return request


def _make_response(status_code=200):
    """Create a mock Django HttpResponse."""
    response = mock.MagicMock()
    response.status_code = status_code
    response.__setitem__ = mock.MagicMock()
    return response


class TestDjangoMiddleware:
    """Tests for Django TeracronTracingMiddleware."""

    def setup_method(self):
        clear_trace()
        clear_sampling_decision()

    def teardown_method(self):
        clear_trace()
        clear_sampling_decision()

    def test_creates_span_on_success(self):
        client = _make_mock_client()
        response = _make_response(200)
        get_response = mock.MagicMock(return_value=response)
        request = _make_request("GET", "/users")

        with mock.patch("teracron.client._singleton", client):
            with mock.patch.dict("sys.modules", {"django": mock.MagicMock(), "django.conf": mock.MagicMock()}):
                mw = TeracronTracingMiddleware(get_response)
                mw.workflow = "api"
                result = mw(request)

        assert result == response
        client._push_trace_span.assert_called_once()
        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["workflow"] == "api"
        assert span_dict["operation"] == "GET /users"
        assert span_dict["status"] == "succeeded"
        assert span_dict["metadata"]["http.method"] == "GET"
        assert span_dict["metadata"]["http.path"] == "/users"
        assert span_dict["metadata"]["http.status_code"] == 200

    def test_records_500_as_failed(self):
        client = _make_mock_client()
        response = _make_response(500)
        get_response = mock.MagicMock(return_value=response)
        request = _make_request("POST", "/error")

        with mock.patch("teracron.client._singleton", client):
            with mock.patch.dict("sys.modules", {"django": mock.MagicMock(), "django.conf": mock.MagicMock()}):
                mw = TeracronTracingMiddleware(get_response)
                mw(request)

        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["status"] == "failed"
        assert span_dict["metadata"]["http.status_code"] == 500

    def test_records_error_on_exception(self):
        client = _make_mock_client()
        get_response = mock.MagicMock(side_effect=RuntimeError("view crash"))
        request = _make_request("GET", "/crash")

        with mock.patch("teracron.client._singleton", client):
            with mock.patch.dict("sys.modules", {"django": mock.MagicMock(), "django.conf": mock.MagicMock()}):
                mw = TeracronTracingMiddleware(get_response)
                with pytest.raises(RuntimeError, match="view crash"):
                    mw(request)

        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["status"] == "failed"
        assert span_dict["error_type"] == "RuntimeError"
        assert span_dict["error_message"] == "view crash"

    def test_extracts_trace_header(self):
        client = _make_mock_client()
        trace_id = "a" * 32
        parent_id = "b" * 32
        response = _make_response(200)
        get_response = mock.MagicMock(return_value=response)
        request = _make_request("GET", "/test", f"{trace_id}:{parent_id}")

        with mock.patch("teracron.client._singleton", client):
            with mock.patch.dict("sys.modules", {"django": mock.MagicMock(), "django.conf": mock.MagicMock()}):
                mw = TeracronTracingMiddleware(get_response)
                mw(request)

        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["trace_id"] == trace_id
        assert span_dict["parent_span_id"] == parent_id

    def test_sampling_rate_0_skips(self):
        client = _make_mock_client(sample_rate=0.0)
        response = _make_response(200)
        get_response = mock.MagicMock(return_value=response)
        request = _make_request("GET", "/test")

        with mock.patch("teracron.client._singleton", client):
            with mock.patch.dict("sys.modules", {"django": mock.MagicMock(), "django.conf": mock.MagicMock()}):
                mw = TeracronTracingMiddleware(get_response)
                result = mw(request)

        assert result == response
        client._push_trace_span.assert_not_called()

    def test_tracing_disabled_passthrough(self):
        client = _make_mock_client(tracing_enabled=False)
        response = _make_response(200)
        get_response = mock.MagicMock(return_value=response)
        request = _make_request("GET", "/test")

        with mock.patch("teracron.client._singleton", client):
            with mock.patch.dict("sys.modules", {"django": mock.MagicMock(), "django.conf": mock.MagicMock()}):
                mw = TeracronTracingMiddleware(get_response)
                result = mw(request)

        assert result == response
        client._push_trace_span.assert_not_called()

    def test_no_client_passthrough(self):
        response = _make_response(200)
        get_response = mock.MagicMock(return_value=response)
        request = _make_request("GET", "/test")

        with mock.patch("teracron.client._singleton", None):
            with mock.patch.dict("sys.modules", {"django": mock.MagicMock(), "django.conf": mock.MagicMock()}):
                mw = TeracronTracingMiddleware(get_response)
                result = mw(request)

        assert result == response

    def test_scrubber_applied_to_metadata(self):
        def scrubber(d):
            d.pop("http.path", None)
            return d

        client = _make_mock_client(scrubber=scrubber)
        response = _make_response(200)
        get_response = mock.MagicMock(return_value=response)
        request = _make_request("GET", "/secret")

        with mock.patch("teracron.client._singleton", client):
            with mock.patch.dict("sys.modules", {"django": mock.MagicMock(), "django.conf": mock.MagicMock()}):
                mw = TeracronTracingMiddleware(get_response)
                mw(request)

        span_dict = client._push_trace_span.call_args[0][0]
        assert "http.path" not in span_dict["metadata"]
