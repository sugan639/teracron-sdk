"""Unit tests for teracron.transport — HTTPS transport layer."""

from unittest import mock

import pytest
import requests

from teracron.transport import Transport, TransportResult


class TestTransport:
    """Tests for the Transport class."""

    def test_init_creates_session(self):
        transport = Transport(domain="example.com", slug="test-slug-001", timeout_s=5.0)
        assert transport._session is not None
        assert transport._url == "https://example.com/api/ingest"
        transport.close()

    def test_headers_are_set(self):
        transport = Transport(domain="example.com", slug="test-slug-001", timeout_s=5.0)
        headers = transport._session.headers
        assert headers["Content-Type"] == "application/octet-stream"
        assert headers["X-Project-Slug"] == "test-slug-001"
        assert "teracron-sdk-python" in headers["User-Agent"]
        transport.close()

    @mock.patch("teracron.transport.requests.Session.post")
    def test_successful_send(self, mock_post):
        mock_resp = mock.Mock()
        mock_resp.status_code = 202
        mock_post.return_value = mock_resp

        transport = Transport(domain="example.com", slug="test-slug-001", timeout_s=5.0)
        result = transport.send(b"encrypted-data")

        assert result.success is True
        assert result.status_code == 202
        mock_post.assert_called_once()
        transport.close()

    @mock.patch("teracron.transport.requests.Session.post")
    def test_server_error(self, mock_post):
        mock_resp = mock.Mock()
        mock_resp.status_code = 500
        mock_post.return_value = mock_resp

        transport = Transport(domain="example.com", slug="test-slug-001", timeout_s=5.0)
        result = transport.send(b"data")

        assert result.success is False
        assert result.status_code == 500
        transport.close()

    @mock.patch("teracron.transport.requests.Session.post")
    def test_connection_error_returns_zero(self, mock_post):
        mock_post.side_effect = requests.ConnectionError("refused")

        transport = Transport(domain="example.com", slug="test-slug-001", timeout_s=5.0)
        result = transport.send(b"data")

        assert result.success is False
        assert result.status_code == 0
        transport.close()

    @mock.patch("teracron.transport.requests.Session.post")
    def test_timeout_returns_zero(self, mock_post):
        mock_post.side_effect = requests.Timeout("timed out")

        transport = Transport(domain="example.com", slug="test-slug-001", timeout_s=5.0)
        result = transport.send(b"data")

        assert result.success is False
        assert result.status_code == 0
        transport.close()

    def test_close_is_idempotent(self):
        transport = Transport(domain="example.com", slug="test-slug-001", timeout_s=5.0)
        transport.close()
        transport.close()  # Should not raise
