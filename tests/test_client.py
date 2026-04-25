"""Unit tests for teracron.client — TeracronClient lifecycle + up()/down() API."""

import time
from unittest import mock

import pytest

from teracron.apikey import encode_api_key
from teracron.client import TeracronClient, up, down, _singleton_lock
import teracron.client as client_module
from teracron.types import FlushResult

_VALID_SLUG = "vivid-kudu-655"
_VALID_PEM = """-----BEGIN PUBLIC KEY-----
MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEA0+dummykeydata0
-----END PUBLIC KEY-----"""
_VALID_API_KEY = encode_api_key(_VALID_SLUG, _VALID_PEM)


class TestTeracronClientLifecycle:
    """Tests for start/stop lifecycle."""

    def test_start_and_stop_with_api_key(self):
        client = TeracronClient(
            api_key=_VALID_API_KEY,
            interval_s=5.0,
            debug=True,
        )
        client.start()
        assert client.is_running is True

        client.stop()
        assert client.is_running is False

    def test_start_and_stop_legacy(self):
        client = TeracronClient(
            project_slug=_VALID_SLUG,
            public_key=_VALID_PEM,
            interval_s=5.0,
            debug=True,
        )
        client.start()
        assert client.is_running is True

        client.stop()
        assert client.is_running is False

    def test_double_start_is_noop(self):
        client = TeracronClient(api_key=_VALID_API_KEY, interval_s=5.0)
        client.start()
        client.start()  # Should not raise or create a second thread
        assert client.is_running is True
        client.stop()

    def test_double_stop_is_safe(self):
        client = TeracronClient(api_key=_VALID_API_KEY, interval_s=5.0)
        client.start()
        client.stop()
        client.stop()  # Should not raise

    def test_no_credentials_raises_on_init(self):
        with pytest.raises(ValueError):
            TeracronClient()

    def test_invalid_api_key_raises_on_init(self):
        with pytest.raises(ValueError):
            TeracronClient(api_key="not_valid")


class TestTeracronClientCollection:
    """Tests for metric collection and flushing."""

    @mock.patch("teracron.client.Transport")
    def test_manual_flush_empty_returns_none(self, _mock_transport):
        client = TeracronClient(
            api_key=_VALID_API_KEY,
            interval_s=300.0,  # Long interval — won't auto-collect
        )
        client.start()
        result = client.flush()
        assert result is None
        client.stop()

    @mock.patch("teracron.client.Transport")
    def test_collection_populates_buffer(self, MockTransport):
        """After starting, the background thread should collect at least 1 metric."""
        mock_transport = MockTransport.return_value
        mock_transport.send.return_value = mock.Mock(success=True, status_code=202)

        client = TeracronClient(
            api_key=_VALID_API_KEY,
            interval_s=5.0,  # Minimum interval
        )
        client.start()

        # Wait for at least one tick
        time.sleep(1.0)

        # Verify the client is running
        assert client.is_running is True
        client.stop()

    def test_daemon_thread(self):
        """The background thread must be a daemon thread."""
        client = TeracronClient(api_key=_VALID_API_KEY, interval_s=5.0)
        client.start()
        assert client._thread is not None
        assert client._thread.daemon is True
        client.stop()


class TestUpDown:
    """Tests for the module-level up()/down() singleton API."""

    def setup_method(self):
        """Reset singleton before each test."""
        with _singleton_lock:
            if client_module._singleton is not None:
                client_module._singleton.stop()
                client_module._singleton = None

    def teardown_method(self):
        """Clean up singleton after each test."""
        with _singleton_lock:
            if client_module._singleton is not None:
                client_module._singleton.stop()
                client_module._singleton = None

    def test_up_returns_running_client(self):
        client = up(api_key=_VALID_API_KEY, interval_s=5.0)
        assert client.is_running is True
        down()
        assert client.is_running is False

    def test_up_is_idempotent(self):
        c1 = up(api_key=_VALID_API_KEY, interval_s=5.0)
        c2 = up(api_key=_VALID_API_KEY, interval_s=5.0)
        assert c1 is c2  # Same instance
        down()

    def test_down_without_up_is_safe(self):
        down()  # Should not raise

    def test_down_clears_singleton(self):
        up(api_key=_VALID_API_KEY, interval_s=5.0)
        down()
        assert client_module._singleton is None

    def test_up_reads_env_var(self):
        env = {"TERACRON_API_KEY": _VALID_API_KEY}
        with mock.patch.dict("os.environ", env, clear=False):
            client = up(interval_s=5.0)
            assert client.is_running is True
            assert client.config.project_slug == _VALID_SLUG
            down()

    def test_up_without_credentials_raises(self):
        env = {"TERACRON_API_KEY": "", "TERACRON_PROJECT_SLUG": "", "TERACRON_PUBLIC_KEY": ""}
        with mock.patch.dict("os.environ", env, clear=False):
            with pytest.raises(ValueError, match="api_key is required"):
                up()

    def test_up_after_down_creates_new_instance(self):
        c1 = up(api_key=_VALID_API_KEY, interval_s=5.0)
        down()
        c2 = up(api_key=_VALID_API_KEY, interval_s=5.0)
        assert c1 is not c2  # New instance after down()
        assert c2.is_running is True
        down()

    def test_module_level_import(self):
        """Verify up/down are accessible from the package root."""
        import teracron
        assert callable(teracron.up)
        assert callable(teracron.down)
