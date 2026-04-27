# -*- coding: utf-8 -*-
"""
Tests for Phase 3 config fields: trace_sample_rate and tracing_scrubber.
"""

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


class TestSampleRateConfig:
    """Tests for trace_sample_rate configuration."""

    def test_default_is_1(self):
        cfg = resolve_config(api_key=_VALID_API_KEY)
        assert cfg.trace_sample_rate == 1.0

    def test_custom_value(self):
        cfg = resolve_config(api_key=_VALID_API_KEY, trace_sample_rate=0.5)
        assert cfg.trace_sample_rate == 0.5

    def test_clamped_above_1(self):
        cfg = resolve_config(api_key=_VALID_API_KEY, trace_sample_rate=1.5)
        assert cfg.trace_sample_rate == 1.0

    def test_clamped_below_0(self):
        cfg = resolve_config(api_key=_VALID_API_KEY, trace_sample_rate=-0.5)
        assert cfg.trace_sample_rate == 0.0

    def test_zero(self):
        cfg = resolve_config(api_key=_VALID_API_KEY, trace_sample_rate=0.0)
        assert cfg.trace_sample_rate == 0.0

    def test_env_var(self):
        env = {"TERACRON_API_KEY": _VALID_API_KEY, "TERACRON_TRACE_SAMPLE_RATE": "0.25"}
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = resolve_config()
            assert cfg.trace_sample_rate == 0.25

    def test_env_var_invalid_fallback(self):
        env = {"TERACRON_API_KEY": _VALID_API_KEY, "TERACRON_TRACE_SAMPLE_RATE": "abc"}
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = resolve_config()
            assert cfg.trace_sample_rate == 1.0  # default

    def test_kwarg_overrides_env(self):
        env = {"TERACRON_API_KEY": _VALID_API_KEY, "TERACRON_TRACE_SAMPLE_RATE": "0.1"}
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = resolve_config(trace_sample_rate=0.75)
            assert cfg.trace_sample_rate == 0.75


class TestScrubberConfig:
    """Tests for tracing_scrubber configuration."""

    def test_default_is_none(self):
        cfg = resolve_config(api_key=_VALID_API_KEY)
        assert cfg.tracing_scrubber is None

    def test_callable_accepted(self):
        def my_scrubber(d):
            return d

        cfg = resolve_config(api_key=_VALID_API_KEY, tracing_scrubber=my_scrubber)
        assert cfg.tracing_scrubber is my_scrubber

    def test_lambda_accepted(self):
        scrubber = lambda d: d  # noqa: E731
        cfg = resolve_config(api_key=_VALID_API_KEY, tracing_scrubber=scrubber)
        assert cfg.tracing_scrubber is scrubber

    def test_non_callable_raises(self):
        with pytest.raises(ValueError, match="tracing_scrubber must be a callable"):
            resolve_config(api_key=_VALID_API_KEY, tracing_scrubber="not callable")

    def test_none_accepted(self):
        cfg = resolve_config(api_key=_VALID_API_KEY, tracing_scrubber=None)
        assert cfg.tracing_scrubber is None

    def test_class_with_call_accepted(self):
        class Scrubber:
            def __call__(self, d):
                return d

        scrubber = Scrubber()
        cfg = resolve_config(api_key=_VALID_API_KEY, tracing_scrubber=scrubber)
        assert cfg.tracing_scrubber is scrubber
