# -*- coding: utf-8 -*-
"""Unit tests for trace buffer, ring-buffer overflow, and trace flush."""

import json
import time
from unittest import mock

import pytest

from teracron.apikey import encode_api_key
from teracron.client import TeracronClient

_VALID_SLUG = "vivid-kudu-655"
_VALID_PEM = """-----BEGIN PUBLIC KEY-----
MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEA0+dummykeydata0
-----END PUBLIC KEY-----"""
_VALID_API_KEY = encode_api_key(_VALID_SLUG, _VALID_PEM)


def _make_span_dict(index=0):
    """Create a minimal valid span dict for testing."""
    return {
        "trace_id": f"trace_{index:032d}",
        "span_id": f"span_{index:032d}",
        "parent_span_id": None,
        "workflow": "test",
        "operation": "op",
        "status": "succeeded",
        "started_at": 1700000000000 + index,
        "duration_ms": 1.0 + index,
        "error_type": None,
        "error_message": None,
        "metadata": None,
        "captured_params": None,
    }


class TestTraceBuffer:
    """Tests for the trace ring buffer in TeracronClient."""

    def test_push_span_adds_to_buffer(self):
        client = TeracronClient(
            api_key=_VALID_API_KEY,
            trace_batch_size=100,
        )
        client._push_trace_span(_make_span_dict(0))
        assert len(client._trace_buffer) == 1

    def test_ring_buffer_drops_oldest(self):
        batch_size = 5
        client = TeracronClient(
            api_key=_VALID_API_KEY,
            trace_batch_size=batch_size,
        )
        # Push more spans than the buffer can hold
        for i in range(batch_size + 3):
            client._push_trace_span(_make_span_dict(i))

        assert len(client._trace_buffer) == batch_size
        # Oldest should have been dropped; newest should be at the end
        last = client._trace_buffer[-1]
        assert last["started_at"] == 1700000000000 + (batch_size + 2)
        # First in buffer should be index 3 (0, 1, 2 dropped)
        first = client._trace_buffer[0]
        assert first["started_at"] == 1700000000000 + 3

    def test_overflow_warning_fires_once(self, capsys):
        batch_size = 3
        client = TeracronClient(
            api_key=_VALID_API_KEY,
            trace_batch_size=batch_size,
        )
        # Fill buffer
        for i in range(batch_size):
            client._push_trace_span(_make_span_dict(i))

        # Next push triggers overflow
        client._push_trace_span(_make_span_dict(batch_size))
        captured = capsys.readouterr()
        assert "Trace buffer full" in captured.err

        # Subsequent overflows should NOT produce additional warnings
        client._push_trace_span(_make_span_dict(batch_size + 1))
        captured2 = capsys.readouterr()
        assert "Trace buffer full" not in captured2.err

    def test_overflow_warned_flag(self):
        batch_size = 2
        client = TeracronClient(
            api_key=_VALID_API_KEY,
            trace_batch_size=batch_size,
        )
        assert client._trace_overflow_warned is False

        for i in range(batch_size):
            client._push_trace_span(_make_span_dict(i))

        client._push_trace_span(_make_span_dict(99))
        assert client._trace_overflow_warned is True


class TestTraceFlush:
    """Tests for _flush_traces and _maybe_flush_traces."""

    @mock.patch("teracron.client.Transport")
    @mock.patch("teracron.client.encrypt_envelope", return_value=b"encrypted")
    def test_flush_traces_payload_shape(self, mock_encrypt, MockTransport):
        mock_transport = MockTransport.return_value
        mock_transport.send_traces.return_value = mock.Mock(success=True, status_code=202)

        client = TeracronClient(
            api_key=_VALID_API_KEY,
            trace_batch_size=100,
        )
        client.start()

        span = _make_span_dict(0)
        client._push_trace_span(span)
        result = client._flush_traces()

        assert result is not None
        assert result.sent == 1
        assert result.success is True
        assert result.status_code == 202

        # Verify the encrypt call received valid JSON
        encrypt_call_args = mock_encrypt.call_args[0]
        raw_bytes = encrypt_call_args[0]
        payload = json.loads(raw_bytes)
        assert payload["type"] == "trace"
        assert payload["project_slug"] == _VALID_SLUG
        assert isinstance(payload["spans"], list)
        assert len(payload["spans"]) == 1
        assert payload["spans"][0]["workflow"] == "test"

        client.stop()

    @mock.patch("teracron.client.Transport")
    @mock.patch("teracron.client.encrypt_envelope", return_value=b"encrypted")
    def test_flush_traces_clears_buffer(self, _mock_encrypt, MockTransport):
        mock_transport = MockTransport.return_value
        mock_transport.send_traces.return_value = mock.Mock(success=True, status_code=202)

        client = TeracronClient(
            api_key=_VALID_API_KEY,
            trace_batch_size=100,
        )
        client.start()

        for i in range(5):
            client._push_trace_span(_make_span_dict(i))

        client._flush_traces()
        assert len(client._trace_buffer) == 0

        client.stop()

    def test_flush_empty_buffer_returns_none(self):
        client = TeracronClient(
            api_key=_VALID_API_KEY,
            trace_batch_size=100,
        )
        result = client._flush_traces()
        assert result is None

    @mock.patch("teracron.client.Transport")
    @mock.patch("teracron.client.encrypt_envelope", return_value=b"encrypted")
    def test_maybe_flush_traces_on_deadline(self, _mock_encrypt, MockTransport):
        mock_transport = MockTransport.return_value
        mock_transport.send_traces.return_value = mock.Mock(success=True, status_code=202)

        client = TeracronClient(
            api_key=_VALID_API_KEY,
            trace_batch_size=1000,  # Large — won't fill
            trace_flush_interval=1.0,  # Short interval
        )
        client.start()

        client._push_trace_span(_make_span_dict(0))
        # Force deadline exceeded
        client._last_trace_flush_time = time.monotonic() - 5.0
        client._maybe_flush_traces()

        # Buffer should be drained
        assert len(client._trace_buffer) == 0
        mock_transport.send_traces.assert_called_once()

        client.stop()

    @mock.patch("teracron.client.Transport")
    @mock.patch("teracron.client.encrypt_envelope", return_value=b"encrypted")
    def test_maybe_flush_traces_on_batch_full(self, _mock_encrypt, MockTransport):
        mock_transport = MockTransport.return_value
        mock_transport.send_traces.return_value = mock.Mock(success=True, status_code=202)

        batch_size = 3
        client = TeracronClient(
            api_key=_VALID_API_KEY,
            trace_batch_size=batch_size,
            trace_flush_interval=300.0,  # Long — won't trigger by time
        )
        client.start()

        for i in range(batch_size):
            client._push_trace_span(_make_span_dict(i))

        client._maybe_flush_traces()

        assert len(client._trace_buffer) == 0
        mock_transport.send_traces.assert_called_once()

        client.stop()

    @mock.patch("teracron.client.Transport")
    @mock.patch("teracron.client.encrypt_envelope", side_effect=Exception("crypto fail"))
    def test_flush_traces_never_raises(self, _mock_encrypt, MockTransport):
        """Flush must never crash the host process."""
        mock_transport = MockTransport.return_value

        client = TeracronClient(
            api_key=_VALID_API_KEY,
            trace_batch_size=100,
        )
        client.start()

        client._push_trace_span(_make_span_dict(0))
        result = client._flush_traces()

        assert result is not None
        assert result.success is False
        assert result.status_code == 0

        client.stop()

    @mock.patch("teracron.client.Transport")
    @mock.patch("teracron.client.encrypt_envelope", return_value=b"encrypted")
    def test_stop_performs_final_trace_flush(self, _mock_encrypt, MockTransport):
        mock_transport = MockTransport.return_value
        mock_transport.send.return_value = mock.Mock(success=True, status_code=202)
        mock_transport.send_traces.return_value = mock.Mock(success=True, status_code=202)

        client = TeracronClient(
            api_key=_VALID_API_KEY,
            trace_batch_size=1000,  # Large — won't auto-flush
            trace_flush_interval=300.0,
        )
        client.start()

        client._push_trace_span(_make_span_dict(0))
        client._push_trace_span(_make_span_dict(1))
        assert len(client._trace_buffer) == 2

        client.stop()

        # stop() should have flushed the trace buffer
        mock_transport.send_traces.assert_called_once()
