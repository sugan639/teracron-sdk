# -*- coding: utf-8 -*-
"""
Tests for teracron.query — read-only query client.

Covers:
    - Client construction and validation
    - URL building and parameter encoding
    - Authorization header injection
    - Response parsing (200, 401, 404, 429, connection errors, timeouts)
    - Input validation (trace_id, span_id format)
    - Limit clamping
"""

from __future__ import annotations

from unittest import mock

import pytest
import requests

from teracron.query import TeracronQueryClient, _error_result


# ── Helpers ──

_TEST_API_KEY = "tcn_test_query_key_1234567890"


def _mock_response(status_code: int, json_data=None, headers=None):
    """Build a mock requests.Response."""
    resp = mock.MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.headers = headers or {}
    return resp


# ── Construction ──


class TestClientConstruction:
    def test_valid_construction(self) -> None:
        client = TeracronQueryClient(api_key=_TEST_API_KEY)
        assert client._base_url == "https://www.teracron.com/api/v1"

    def test_custom_domain(self) -> None:
        client = TeracronQueryClient(
            api_key=_TEST_API_KEY, domain="api.teracron.com"
        )
        assert client._base_url == "https://api.teracron.com/api/v1"

    def test_empty_key_raises(self) -> None:
        with pytest.raises(ValueError, match="api_key is required"):
            TeracronQueryClient(api_key="")

    def test_none_key_raises(self) -> None:
        with pytest.raises(ValueError, match="api_key is required"):
            TeracronQueryClient(api_key=None)  # type: ignore[arg-type]

    def test_timeout_clamped(self) -> None:
        client = TeracronQueryClient(api_key=_TEST_API_KEY, timeout_s=0.5)
        assert client._timeout == 2.0  # Clamped to minimum

    def test_timeout_clamped_upper(self) -> None:
        client = TeracronQueryClient(api_key=_TEST_API_KEY, timeout_s=100.0)
        assert client._timeout == 30.0  # Clamped to maximum


# ── Authorization header ──


class TestAuthHeader:
    def test_bearer_token_set(self) -> None:
        client = TeracronQueryClient(api_key=_TEST_API_KEY)
        assert client._session.headers["Authorization"] == f"Bearer {_TEST_API_KEY}"

    def test_accept_json(self) -> None:
        client = TeracronQueryClient(api_key=_TEST_API_KEY)
        assert client._session.headers["Accept"] == "application/json"


# ── list_events ──


class TestListEvents:
    def test_success(self) -> None:
        client = TeracronQueryClient(api_key=_TEST_API_KEY)
        mock_resp = _mock_response(200, {"events": [{"trace_id": "abc", "status": "failed"}]})

        with mock.patch.object(client._session, "get", return_value=mock_resp):
            result = client.list_events(status="failed", limit=10)

        assert "events" in result
        assert result["events"][0]["status"] == "failed"

    def test_with_workflow_filter(self) -> None:
        client = TeracronQueryClient(api_key=_TEST_API_KEY)
        mock_resp = _mock_response(200, {"events": []})

        with mock.patch.object(client._session, "get", return_value=mock_resp) as mock_get:
            client.list_events(workflow="payment", limit=5)
            call_url = mock_get.call_args[0][0]
            assert "workflow=payment" in call_url
            assert "limit=5" in call_url

    def test_limit_clamped_to_max(self) -> None:
        client = TeracronQueryClient(api_key=_TEST_API_KEY)
        mock_resp = _mock_response(200, {"events": []})

        with mock.patch.object(client._session, "get", return_value=mock_resp) as mock_get:
            client.list_events(limit=5000)
            call_url = mock_get.call_args[0][0]
            assert "limit=1000" in call_url

    def test_limit_clamped_to_min(self) -> None:
        client = TeracronQueryClient(api_key=_TEST_API_KEY)
        mock_resp = _mock_response(200, {"events": []})

        with mock.patch.object(client._session, "get", return_value=mock_resp) as mock_get:
            client.list_events(limit=-5)
            call_url = mock_get.call_args[0][0]
            assert "limit=1" in call_url

    def test_none_params_excluded(self) -> None:
        client = TeracronQueryClient(api_key=_TEST_API_KEY)
        mock_resp = _mock_response(200, {"events": []})

        with mock.patch.object(client._session, "get", return_value=mock_resp) as mock_get:
            client.list_events()
            call_url = mock_get.call_args[0][0]
            assert "workflow=" not in call_url
            assert "status=" not in call_url
            assert "since=" not in call_url


# ── get_trace ──


class TestGetTrace:
    def test_success(self) -> None:
        client = TeracronQueryClient(api_key=_TEST_API_KEY)
        trace_id = "a" * 32
        mock_resp = _mock_response(200, {"spans": [{"span_id": "b" * 32}]})

        with mock.patch.object(client._session, "get", return_value=mock_resp):
            result = client.get_trace(trace_id)

        assert "spans" in result

    def test_invalid_trace_id(self) -> None:
        client = TeracronQueryClient(api_key=_TEST_API_KEY)
        result = client.get_trace("INVALID_HEX!")
        assert result.get("error")
        assert "hex" in result["error"]

    def test_empty_trace_id(self) -> None:
        client = TeracronQueryClient(api_key=_TEST_API_KEY)
        result = client.get_trace("")
        assert result.get("error")

    def test_none_trace_id(self) -> None:
        client = TeracronQueryClient(api_key=_TEST_API_KEY)
        result = client.get_trace(None)  # type: ignore[arg-type]
        assert result.get("error")


# ── list_workflows ──


class TestListWorkflows:
    def test_success(self) -> None:
        client = TeracronQueryClient(api_key=_TEST_API_KEY)
        mock_resp = _mock_response(200, {"workflows": [{"workflow": "payment"}]})

        with mock.patch.object(client._session, "get", return_value=mock_resp):
            result = client.list_workflows(limit=10)

        assert "workflows" in result


# ── get_span ──


class TestGetSpan:
    def test_success(self) -> None:
        client = TeracronQueryClient(api_key=_TEST_API_KEY)
        span_id = "c" * 32
        mock_resp = _mock_response(200, {"span_id": span_id, "status": "failed"})

        with mock.patch.object(client._session, "get", return_value=mock_resp):
            result = client.get_span(span_id)

        assert result["span_id"] == span_id

    def test_invalid_span_id(self) -> None:
        client = TeracronQueryClient(api_key=_TEST_API_KEY)
        result = client.get_span("NOT_HEX")
        assert result.get("error")


# ── HTTP error responses ──


class TestErrorResponses:
    def test_401_unauthorized(self) -> None:
        client = TeracronQueryClient(api_key=_TEST_API_KEY)
        mock_resp = _mock_response(401)

        with mock.patch.object(client._session, "get", return_value=mock_resp):
            result = client.list_events()

        assert result.get("error")
        assert "Authentication" in result["error"]
        assert result.get("hint")

    def test_404_not_deployed(self) -> None:
        client = TeracronQueryClient(api_key=_TEST_API_KEY)
        mock_resp = _mock_response(404)

        with mock.patch.object(client._session, "get", return_value=mock_resp):
            result = client.list_events()

        assert result.get("error")
        assert "not yet deployed" in result["error"]
        assert result.get("hint")

    def test_429_rate_limited(self) -> None:
        client = TeracronQueryClient(api_key=_TEST_API_KEY)
        mock_resp = _mock_response(429, headers={"Retry-After": "60"})

        with mock.patch.object(client._session, "get", return_value=mock_resp):
            result = client.list_events()

        assert result.get("error")
        assert "Rate limited" in result["error"]
        assert "60" in result.get("hint", "")

    def test_500_server_error(self) -> None:
        client = TeracronQueryClient(api_key=_TEST_API_KEY)
        mock_resp = _mock_response(500)

        with mock.patch.object(client._session, "get", return_value=mock_resp):
            result = client.list_events()

        assert result.get("error")
        assert "500" in result["error"]

    def test_connection_error(self) -> None:
        client = TeracronQueryClient(api_key=_TEST_API_KEY)

        with mock.patch.object(
            client._session, "get", side_effect=requests.ConnectionError("refused")
        ):
            result = client.list_events()

        assert result.get("error")
        assert "Connection failed" in result["error"]

    def test_timeout_error(self) -> None:
        client = TeracronQueryClient(api_key=_TEST_API_KEY)

        with mock.patch.object(
            client._session, "get", side_effect=requests.Timeout("timed out")
        ):
            result = client.list_events()

        assert result.get("error")
        assert "timed out" in result["error"]

    def test_generic_request_exception(self) -> None:
        client = TeracronQueryClient(api_key=_TEST_API_KEY)

        with mock.patch.object(
            client._session,
            "get",
            side_effect=requests.RequestException("something bad"),
        ):
            result = client.list_events()

        assert result.get("error")


# ── _error_result helper ──


class TestErrorResult:
    def test_with_hint(self) -> None:
        result = _error_result(404, "Not found", "Check docs")
        assert result["error"] == "Not found"
        assert result["hint"] == "Check docs"
        assert result["status_code"] == 404

    def test_without_hint(self) -> None:
        result = _error_result(500, "Server error")
        assert "hint" not in result


# ── close ──


class TestClose:
    def test_close(self) -> None:
        client = TeracronQueryClient(api_key=_TEST_API_KEY)
        client.close()
        # Should not raise


class TestInvalidJsonResponse:
    def test_invalid_json_body(self) -> None:
        client = TeracronQueryClient(api_key=_TEST_API_KEY)
        mock_resp = _mock_response(200)
        mock_resp.json.side_effect = ValueError("No JSON")

        with mock.patch.object(client._session, "get", return_value=mock_resp):
            result = client.list_events()

        assert result.get("error")
        assert "Invalid JSON" in result["error"]
