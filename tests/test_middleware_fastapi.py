# -*- coding: utf-8 -*-
"""
Tests for FastAPI/ASGI middleware — tested via raw ASGI protocol
without requiring FastAPI/Starlette installation.
"""

import asyncio
from unittest import mock

import pytest

from teracron.tracing.context import clear_trace, start_trace, get_trace_id
from teracron.tracing.middleware.fastapi import TeracronTracingMiddleware
from teracron.tracing.sampling import clear_sampling_decision


def _make_mock_client(tracing_enabled=True, sample_rate=1.0, scrubber=None):
    client = mock.MagicMock()
    client.config.tracing_enabled = tracing_enabled
    client.config.trace_sample_rate = sample_rate
    client._push_trace_span = mock.MagicMock()
    client._scrubber = scrubber
    return client


def _make_http_scope(method="GET", path="/test", headers=None):
    """Create a minimal ASGI HTTP scope."""
    raw_headers = []
    if headers:
        for k, v in headers.items():
            raw_headers.append((k.lower().encode("latin-1"), v.encode("latin-1")))
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": raw_headers,
    }


async def _dummy_app(scope, receive, send):
    """Minimal ASGI app that returns 200."""
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [],
    })
    await send({
        "type": "http.response.body",
        "body": b"OK",
    })


async def _error_app(scope, receive, send):
    """ASGI app that raises an exception."""
    raise RuntimeError("app error")


async def _500_app(scope, receive, send):
    """ASGI app that returns 500."""
    await send({
        "type": "http.response.start",
        "status": 500,
        "headers": [],
    })
    await send({
        "type": "http.response.body",
        "body": b"Internal Server Error",
    })


class TestFastAPIMiddleware:
    """Tests for TeracronTracingMiddleware (ASGI)."""

    def setup_method(self):
        clear_trace()
        clear_sampling_decision()

    def teardown_method(self):
        clear_trace()
        clear_sampling_decision()

    def test_creates_span_on_success(self):
        client = _make_mock_client()
        middleware = TeracronTracingMiddleware(_dummy_app, workflow="api")
        scope = _make_http_scope("GET", "/users")

        async def receive():
            return {"type": "http.request", "body": b""}

        sent_messages = []
        async def send(msg):
            sent_messages.append(msg)

        with mock.patch("teracron.client._singleton", client):
            asyncio.get_event_loop().run_until_complete(
                middleware(scope, receive, send)
            )

        client._push_trace_span.assert_called_once()
        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["workflow"] == "api"
        assert span_dict["operation"] == "GET /users"
        assert span_dict["status"] == "succeeded"
        assert span_dict["metadata"]["http.method"] == "GET"
        assert span_dict["metadata"]["http.path"] == "/users"
        assert span_dict["metadata"]["http.status_code"] == 200

    def test_records_error_on_exception(self):
        client = _make_mock_client()
        middleware = TeracronTracingMiddleware(_error_app, workflow="api")
        scope = _make_http_scope("POST", "/crash")

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(msg):
            pass

        with mock.patch("teracron.client._singleton", client):
            with pytest.raises(RuntimeError, match="app error"):
                asyncio.get_event_loop().run_until_complete(
                    middleware(scope, receive, send)
                )

        client._push_trace_span.assert_called_once()
        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["status"] == "failed"
        assert span_dict["error_type"] == "RuntimeError"
        assert span_dict["error_message"] == "app error"

    def test_records_500_as_failed(self):
        client = _make_mock_client()
        middleware = TeracronTracingMiddleware(_500_app, workflow="api")
        scope = _make_http_scope("GET", "/fail")

        async def receive():
            return {"type": "http.request", "body": b""}

        sent_messages = []
        async def send(msg):
            sent_messages.append(msg)

        with mock.patch("teracron.client._singleton", client):
            asyncio.get_event_loop().run_until_complete(
                middleware(scope, receive, send)
            )

        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["status"] == "failed"
        assert span_dict["metadata"]["http.status_code"] == 500

    def test_extracts_trace_header(self):
        client = _make_mock_client()
        middleware = TeracronTracingMiddleware(_dummy_app)
        trace_id = "a" * 32
        parent_id = "b" * 32
        scope = _make_http_scope(
            "GET", "/test",
            headers={"x-teracron-trace": f"{trace_id}:{parent_id}"},
        )

        async def receive():
            return {"type": "http.request", "body": b""}

        sent_messages = []
        async def send(msg):
            sent_messages.append(msg)

        with mock.patch("teracron.client._singleton", client):
            asyncio.get_event_loop().run_until_complete(
                middleware(scope, receive, send)
            )

        span_dict = client._push_trace_span.call_args[0][0]
        assert span_dict["trace_id"] == trace_id
        assert span_dict["parent_span_id"] == parent_id

    def test_injects_trace_header_in_response(self):
        client = _make_mock_client()
        middleware = TeracronTracingMiddleware(_dummy_app)
        scope = _make_http_scope("GET", "/test")

        async def receive():
            return {"type": "http.request", "body": b""}

        sent_messages = []
        async def send(msg):
            sent_messages.append(msg)

        with mock.patch("teracron.client._singleton", client):
            asyncio.get_event_loop().run_until_complete(
                middleware(scope, receive, send)
            )

        # Check response headers contain x-teracron-trace.
        start_msg = sent_messages[0]
        header_keys = [h[0] for h in start_msg.get("headers", [])]
        assert b"x-teracron-trace" in header_keys

    def test_sampling_rate_0_skips_span(self):
        client = _make_mock_client(sample_rate=0.0)
        middleware = TeracronTracingMiddleware(_dummy_app)
        scope = _make_http_scope("GET", "/test")

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(msg):
            pass

        with mock.patch("teracron.client._singleton", client):
            asyncio.get_event_loop().run_until_complete(
                middleware(scope, receive, send)
            )

        client._push_trace_span.assert_not_called()

    def test_tracing_disabled_passthrough(self):
        client = _make_mock_client(tracing_enabled=False)
        middleware = TeracronTracingMiddleware(_dummy_app)
        scope = _make_http_scope("GET", "/test")

        async def receive():
            return {"type": "http.request", "body": b""}

        sent_messages = []
        async def send(msg):
            sent_messages.append(msg)

        with mock.patch("teracron.client._singleton", client):
            asyncio.get_event_loop().run_until_complete(
                middleware(scope, receive, send)
            )

        client._push_trace_span.assert_not_called()
        # App should still work.
        assert len(sent_messages) == 2

    def test_no_client_passthrough(self):
        middleware = TeracronTracingMiddleware(_dummy_app)
        scope = _make_http_scope("GET", "/test")

        async def receive():
            return {"type": "http.request", "body": b""}

        sent_messages = []
        async def send(msg):
            sent_messages.append(msg)

        with mock.patch("teracron.client._singleton", None):
            asyncio.get_event_loop().run_until_complete(
                middleware(scope, receive, send)
            )

        assert len(sent_messages) == 2

    def test_websocket_passthrough(self):
        """Non-HTTP scopes should be passed through."""
        client = _make_mock_client()
        middleware = TeracronTracingMiddleware(_dummy_app)
        scope = {"type": "websocket", "path": "/ws"}

        called = []

        async def mock_app(scope, receive, send):
            called.append(True)

        middleware.app = mock_app

        async def receive():
            return {}

        async def send(msg):
            pass

        with mock.patch("teracron.client._singleton", client):
            asyncio.get_event_loop().run_until_complete(
                middleware(scope, receive, send)
            )

        assert called == [True]
        client._push_trace_span.assert_not_called()

    def test_scrubber_applied_to_metadata(self):
        def scrubber(d):
            d.pop("http.path", None)
            return d

        client = _make_mock_client(scrubber=scrubber)
        middleware = TeracronTracingMiddleware(_dummy_app)
        scope = _make_http_scope("GET", "/secret-path")

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(msg):
            pass

        with mock.patch("teracron.client._singleton", client):
            asyncio.get_event_loop().run_until_complete(
                middleware(scope, receive, send)
            )

        span_dict = client._push_trace_span.call_args[0][0]
        assert "http.path" not in span_dict["metadata"]
