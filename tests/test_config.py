"""Unit tests for teracron.config — validation and resolution."""

import os

import pytest
from unittest import mock

from teracron.apikey import encode_api_key
from teracron.config import resolve_config
from teracron.types import ResolvedConfig

_VALID_SLUG = "vivid-kudu-655"
_VALID_PEM = """-----BEGIN PUBLIC KEY-----
MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEA0+dummykeydata0
-----END PUBLIC KEY-----"""
_VALID_API_KEY = encode_api_key(_VALID_SLUG, _VALID_PEM)


class TestResolveConfigApiKey:
    """Tests for the recommended api_key flow."""

    def test_valid_api_key(self):
        cfg = resolve_config(api_key=_VALID_API_KEY)
        assert cfg.project_slug == _VALID_SLUG
        assert cfg.public_key == _VALID_PEM
        assert cfg.domain == "www.teracron.com"
        assert cfg.interval_s == 30.0
        assert cfg.max_buffer_size == 60
        assert cfg.timeout_s == 10.0
        assert cfg.debug is False
        assert cfg.target_pid is None

    def test_api_key_from_env(self):
        env = {"TERACRON_API_KEY": _VALID_API_KEY}
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = resolve_config()
            assert cfg.project_slug == _VALID_SLUG
            assert cfg.public_key == _VALID_PEM

    def test_invalid_api_key_raises(self):
        with pytest.raises(ValueError, match="Invalid API key"):
            resolve_config(api_key="not_a_valid_key")

    def test_explicit_slug_overrides_api_key(self):
        other_slug = "alpha-beta-123"
        other_pem = _VALID_PEM
        other_api_key = encode_api_key(other_slug, other_pem)
        cfg = resolve_config(api_key=other_api_key, project_slug=_VALID_SLUG)
        assert cfg.project_slug == _VALID_SLUG

    def test_explicit_public_key_overrides_api_key(self):
        custom_pem = (
            "-----BEGIN PUBLIC KEY-----\n"
            "CustomKeyData\n"
            "-----END PUBLIC KEY-----"
        )
        cfg = resolve_config(api_key=_VALID_API_KEY, public_key=custom_pem)
        assert cfg.public_key == custom_pem

    def test_no_credentials_raises_helpful_message(self):
        # Clear all env vars that could provide credentials
        env_clear = {
            "TERACRON_API_KEY": "",
            "TERACRON_PROJECT_SLUG": "",
            "TERACRON_PUBLIC_KEY": "",
        }
        with mock.patch.dict(os.environ, env_clear, clear=False):
            with pytest.raises(ValueError, match="api_key is required"):
                resolve_config()


class TestResolveConfigLegacy:
    """Tests for the legacy project_slug + public_key flow (backward compat)."""

    def test_valid_minimal_config(self):
        cfg = resolve_config(project_slug=_VALID_SLUG, public_key=_VALID_PEM)
        assert cfg.project_slug == _VALID_SLUG
        assert cfg.domain == "www.teracron.com"
        assert cfg.interval_s == 30.0
        assert cfg.max_buffer_size == 60
        assert cfg.timeout_s == 10.0
        assert cfg.debug is False
        assert cfg.target_pid is None

    def test_missing_slug_and_no_api_key_raises(self):
        with pytest.raises(ValueError, match="api_key is required"):
            resolve_config(public_key=_VALID_PEM)

    def test_invalid_slug_format_raises(self):
        with pytest.raises(ValueError, match="Invalid project_slug format"):
            resolve_config(project_slug="INVALID", public_key=_VALID_PEM)

    def test_missing_public_key_and_no_api_key_raises(self):
        with pytest.raises(ValueError, match="api_key is required"):
            resolve_config(project_slug=_VALID_SLUG)

    def test_invalid_public_key_raises(self):
        with pytest.raises(ValueError, match="PEM-encoded RSA public key"):
            resolve_config(project_slug=_VALID_SLUG, public_key="not-a-pem")

    def test_env_var_fallback_legacy(self):
        env = {
            "TERACRON_API_KEY": "",
            "TERACRON_PROJECT_SLUG": _VALID_SLUG,
            "TERACRON_PUBLIC_KEY": _VALID_PEM,
            "TERACRON_INTERVAL": "15",
            "TERACRON_DEBUG": "true",
            "TERACRON_TARGET_PID": "42",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = resolve_config()
            assert cfg.project_slug == _VALID_SLUG
            assert cfg.interval_s == 15.0
            assert cfg.debug is True
            assert cfg.target_pid == 42

    def test_explicit_kwargs_override_env(self):
        env = {
            "TERACRON_API_KEY": "",
            "TERACRON_PROJECT_SLUG": "wrong-slug-000",
            "TERACRON_PUBLIC_KEY": "wrong-key",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = resolve_config(project_slug=_VALID_SLUG, public_key=_VALID_PEM)
            assert cfg.project_slug == _VALID_SLUG


class TestResolveConfigBounds:
    """Tests for clamping and sanitisation logic."""

    def test_interval_clamped_min(self):
        cfg = resolve_config(api_key=_VALID_API_KEY, interval_s=1.0)
        assert cfg.interval_s == 5.0

    def test_interval_clamped_max(self):
        cfg = resolve_config(api_key=_VALID_API_KEY, interval_s=999.0)
        assert cfg.interval_s == 300.0

    def test_timeout_clamped_min(self):
        cfg = resolve_config(api_key=_VALID_API_KEY, timeout_s=0.5)
        assert cfg.timeout_s == 2.0

    def test_timeout_clamped_max(self):
        cfg = resolve_config(api_key=_VALID_API_KEY, timeout_s=100.0)
        assert cfg.timeout_s == 30.0

    def test_max_buffer_minimum_is_one(self):
        cfg = resolve_config(api_key=_VALID_API_KEY, max_buffer_size=-5)
        assert cfg.max_buffer_size == 1

    def test_debug_flag(self):
        cfg = resolve_config(api_key=_VALID_API_KEY, debug=True)
        assert cfg.debug is True

    def test_target_pid(self):
        cfg = resolve_config(api_key=_VALID_API_KEY, target_pid=12345)
        assert cfg.target_pid == 12345


class TestDomainValidation:
    """Tests for domain allowlisting — prevents telemetry redirection."""

    def test_default_domain_allowed(self):
        cfg = resolve_config(api_key=_VALID_API_KEY)
        assert cfg.domain == "www.teracron.com"

    def test_teracron_com_allowed(self):
        cfg = resolve_config(api_key=_VALID_API_KEY, domain="teracron.com")
        assert cfg.domain == "teracron.com"

    def test_subdomain_teracron_allowed(self):
        cfg = resolve_config(api_key=_VALID_API_KEY, domain="ingest.teracron.com")
        assert cfg.domain == "ingest.teracron.com"

    def test_teracron_with_port_allowed(self):
        cfg = resolve_config(api_key=_VALID_API_KEY, domain="ingest.teracron.com:8443")
        assert cfg.domain == "ingest.teracron.com:8443"

    def test_sanitised_teracron_url_allowed(self):
        cfg = resolve_config(
            api_key=_VALID_API_KEY,
            domain="https://api.teracron.com/path/",
        )
        assert cfg.domain == "api.teracron.com"

    def test_non_teracron_domain_rejected(self):
        with pytest.raises(ValueError, match="not allowed"):
            resolve_config(api_key=_VALID_API_KEY, domain="evil.com")

    def test_localhost_rejected(self):
        with pytest.raises(ValueError, match="not allowed"):
            resolve_config(api_key=_VALID_API_KEY, domain="localhost:8080")

    def test_ip_address_rejected(self):
        with pytest.raises(ValueError, match="not allowed"):
            resolve_config(api_key=_VALID_API_KEY, domain="169.254.169.254")

    def test_custom_domain_allowed_with_env_override(self):
        env = {"TERACRON_ALLOW_CUSTOM_DOMAIN": "1"}
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = resolve_config(api_key=_VALID_API_KEY, domain="custom-onprem.internal")
            assert cfg.domain == "custom-onprem.internal"

    def test_suffix_attack_rejected(self):
        """Ensure 'evil-teracron.com' doesn't match."""
        with pytest.raises(ValueError, match="not allowed"):
            resolve_config(api_key=_VALID_API_KEY, domain="evil-teracron.com")


class TestResolveConfigPriority:
    """Tests for credential resolution priority."""

    def test_api_key_kwarg_over_env_legacy(self):
        env = {
            "TERACRON_PROJECT_SLUG": "wrong-slug-000",
            "TERACRON_PUBLIC_KEY": _VALID_PEM,
        }
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = resolve_config(api_key=_VALID_API_KEY)
            # api_key kwarg should win over env legacy vars
            assert cfg.project_slug == _VALID_SLUG

    def test_api_key_env_over_legacy_env(self):
        env = {
            "TERACRON_API_KEY": _VALID_API_KEY,
            "TERACRON_PROJECT_SLUG": "wrong-slug-000",
            "TERACRON_PUBLIC_KEY": "wrong",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = resolve_config()
            assert cfg.project_slug == _VALID_SLUG
