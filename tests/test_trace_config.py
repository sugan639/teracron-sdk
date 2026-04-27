# -*- coding: utf-8 -*-
"""Unit tests for tracing-specific config fields in resolve_config."""

import os
from unittest import mock

import pytest

from teracron.apikey import encode_api_key
from teracron.config import resolve_config

_VALID_SLUG = "vivid-kudu-655"
_VALID_PEM = """-----BEGIN PUBLIC KEY-----
MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEA0+dummykeydata0
-----END PUBLIC KEY-----"""
_VALID_API_KEY = encode_api_key(_VALID_SLUG, _VALID_PEM)


class TestTracingConfigDefaults:
    """Tests for default tracing config values."""

    def test_tracing_enabled_default_true(self):
        cfg = resolve_config(api_key=_VALID_API_KEY)
        assert cfg.tracing_enabled is True

    def test_trace_batch_size_default(self):
        cfg = resolve_config(api_key=_VALID_API_KEY)
        assert cfg.trace_batch_size == 100

    def test_trace_flush_interval_default(self):
        cfg = resolve_config(api_key=_VALID_API_KEY)
        assert cfg.trace_flush_interval == 10.0


class TestTracingConfigKwargs:
    """Tests for custom tracing config values via kwargs."""

    def test_tracing_enabled_false(self):
        cfg = resolve_config(api_key=_VALID_API_KEY, tracing_enabled=False)
        assert cfg.tracing_enabled is False

    def test_trace_batch_size_custom(self):
        cfg = resolve_config(api_key=_VALID_API_KEY, trace_batch_size=50)
        assert cfg.trace_batch_size == 50

    def test_trace_flush_interval_custom(self):
        cfg = resolve_config(api_key=_VALID_API_KEY, trace_flush_interval=5.0)
        assert cfg.trace_flush_interval == 5.0


class TestTracingConfigClamping:
    """Tests for bound clamping on tracing config."""

    def test_trace_batch_size_clamped_min(self):
        cfg = resolve_config(api_key=_VALID_API_KEY, trace_batch_size=-10)
        assert cfg.trace_batch_size == 1

    def test_trace_batch_size_clamped_max(self):
        cfg = resolve_config(api_key=_VALID_API_KEY, trace_batch_size=99999)
        assert cfg.trace_batch_size == 10_000

    def test_trace_flush_interval_clamped_min(self):
        cfg = resolve_config(api_key=_VALID_API_KEY, trace_flush_interval=0.01)
        assert cfg.trace_flush_interval == 1.0

    def test_trace_flush_interval_clamped_max(self):
        cfg = resolve_config(api_key=_VALID_API_KEY, trace_flush_interval=999.0)
        assert cfg.trace_flush_interval == 300.0


class TestTracingConfigEnvVars:
    """Tests for environment variable fallbacks."""

    def test_tracing_enabled_env_true(self):
        env = {"TERACRON_API_KEY": _VALID_API_KEY, "TERACRON_TRACING_ENABLED": "1"}
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = resolve_config()
            assert cfg.tracing_enabled is True

    def test_tracing_enabled_env_false_zero(self):
        env = {"TERACRON_API_KEY": _VALID_API_KEY, "TERACRON_TRACING_ENABLED": "0"}
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = resolve_config()
            assert cfg.tracing_enabled is False

    def test_tracing_enabled_env_false_string(self):
        env = {"TERACRON_API_KEY": _VALID_API_KEY, "TERACRON_TRACING_ENABLED": "false"}
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = resolve_config()
            assert cfg.tracing_enabled is False

    def test_tracing_enabled_env_no(self):
        env = {"TERACRON_API_KEY": _VALID_API_KEY, "TERACRON_TRACING_ENABLED": "no"}
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = resolve_config()
            assert cfg.tracing_enabled is False

    def test_tracing_enabled_env_yes(self):
        env = {"TERACRON_API_KEY": _VALID_API_KEY, "TERACRON_TRACING_ENABLED": "yes"}
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = resolve_config()
            assert cfg.tracing_enabled is True

    def test_trace_batch_size_from_env(self):
        env = {"TERACRON_API_KEY": _VALID_API_KEY, "TERACRON_TRACE_BATCH_SIZE": "200"}
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = resolve_config()
            assert cfg.trace_batch_size == 200

    def test_trace_batch_size_env_invalid_fallback(self):
        env = {"TERACRON_API_KEY": _VALID_API_KEY, "TERACRON_TRACE_BATCH_SIZE": "not_a_number"}
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = resolve_config()
            assert cfg.trace_batch_size == 100  # default

    def test_trace_flush_interval_from_env(self):
        env = {"TERACRON_API_KEY": _VALID_API_KEY, "TERACRON_TRACE_FLUSH_INTERVAL": "30.0"}
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = resolve_config()
            assert cfg.trace_flush_interval == 30.0

    def test_trace_flush_interval_env_invalid_fallback(self):
        env = {"TERACRON_API_KEY": _VALID_API_KEY, "TERACRON_TRACE_FLUSH_INTERVAL": "abc"}
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = resolve_config()
            assert cfg.trace_flush_interval == 10.0  # default

    def test_kwargs_override_env(self):
        env = {
            "TERACRON_API_KEY": _VALID_API_KEY,
            "TERACRON_TRACING_ENABLED": "0",
            "TERACRON_TRACE_BATCH_SIZE": "50",
            "TERACRON_TRACE_FLUSH_INTERVAL": "5.0",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = resolve_config(
                tracing_enabled=True,
                trace_batch_size=200,
                trace_flush_interval=20.0,
            )
            assert cfg.tracing_enabled is True
            assert cfg.trace_batch_size == 200
            assert cfg.trace_flush_interval == 20.0
