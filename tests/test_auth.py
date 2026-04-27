# -*- coding: utf-8 -*-
"""
Tests for teracron.auth — credential storage, login, logout, whoami.

Covers:
    - Credential file creation with restrictive permissions
    - Login validation (valid key, invalid key, decode failure)
    - Logout (secure wipe)
    - Whoami (loaded vs. absent)
    - Expired credentials handling
    - API key masking (no full key exposure)
    - resolve_api_key priority chain
    - Edge cases: corrupt file, missing fields, empty file
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from unittest import mock

import pytest

from teracron.auth import (
    AuthCredentials,
    _credentials_path,
    delete_credentials,
    load_credentials,
    login,
    logout,
    mask_api_key,
    resolve_api_key,
    save_credentials,
    validate_key_format,
    whoami,
)


# ── Helpers ──

# A valid API key for testing (matches the tcn_ format that decode_api_key expects).
# We need a real decodable key, so we build one from encode_api_key.
def _make_test_key() -> str:
    from teracron.apikey import encode_api_key

    return encode_api_key(
        "vivid-kudu-655",
        "-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA0\n-----END PUBLIC KEY-----",
    )


@pytest.fixture
def temp_credentials_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect credentials storage to a temp directory."""
    creds_dir = tmp_path / ".teracron"
    creds_dir.mkdir()
    creds_file = creds_dir / "credentials.json"

    monkeypatch.setattr(
        "teracron.auth._credentials_path",
        lambda: creds_file,
    )
    return creds_file


# ── validate_key_format ──


class TestValidateKeyFormat:
    def test_valid_key(self) -> None:
        key = _make_test_key()
        assert validate_key_format(key) is True

    def test_empty_string(self) -> None:
        assert validate_key_format("") is False

    def test_none(self) -> None:
        assert validate_key_format(None) is False  # type: ignore[arg-type]

    def test_wrong_prefix(self) -> None:
        assert validate_key_format("aws_1234567890abcdef1234567890") is False

    def test_too_short(self) -> None:
        assert validate_key_format("tcn_abc") is False

    def test_valid_minimum_length(self) -> None:
        # tcn_ + 20 chars = 24 chars minimum
        assert validate_key_format("tcn_" + "a" * 20) is True

    def test_non_string(self) -> None:
        assert validate_key_format(12345) is False  # type: ignore[arg-type]


# ── mask_api_key ──


class TestMaskApiKey:
    def test_normal_key(self) -> None:
        key = "tcn_abcdefghijklmnopqrstuvwxyz"
        masked = mask_api_key(key)
        assert masked.startswith("tcn_")
        assert "..." in masked
        # Must NOT contain the full key
        assert masked != key

    def test_short_key(self) -> None:
        masked = mask_api_key("tcn_short")
        assert masked == "tcn_****"

    def test_empty_key(self) -> None:
        assert mask_api_key("") == "tcn_****"

    def test_none_key(self) -> None:
        assert mask_api_key(None) == "tcn_****"  # type: ignore[arg-type]

    def test_never_exposes_middle(self) -> None:
        key = _make_test_key()
        masked = mask_api_key(key)
        # The middle portion should not appear
        middle = key[8:-4]
        assert middle not in masked


# ── save_credentials / load_credentials ──


class TestCredentialStorage:
    def test_save_and_load(self, temp_credentials_dir: Path) -> None:
        creds = AuthCredentials(
            api_key="tcn_test_key_1234567890",
            project_slug="vivid-kudu-655",
            domain="www.teracron.com",
            created_at=int(time.time()),
        )
        path = save_credentials(creds)
        assert path.exists()

        loaded = load_credentials()
        assert loaded is not None
        assert loaded.api_key == creds.api_key
        assert loaded.project_slug == creds.project_slug
        assert loaded.domain == creds.domain

    def test_file_permissions(self, temp_credentials_dir: Path) -> None:
        creds = AuthCredentials(
            api_key="tcn_test_key_1234567890",
            project_slug="vivid-kudu-655",
            domain="www.teracron.com",
            created_at=int(time.time()),
        )
        path = save_credentials(creds)
        mode = stat.S_IMODE(os.stat(str(path)).st_mode)
        # Should be 0600 (owner read+write only).
        assert mode == (stat.S_IRUSR | stat.S_IWUSR)

    def test_load_nonexistent(self, temp_credentials_dir: Path) -> None:
        assert load_credentials() is None

    def test_load_corrupt_json(self, temp_credentials_dir: Path) -> None:
        temp_credentials_dir.write_text("NOT JSON {{{", encoding="utf-8")
        assert load_credentials() is None

    def test_load_missing_fields(self, temp_credentials_dir: Path) -> None:
        temp_credentials_dir.write_text('{"api_key": "tcn_x"}', encoding="utf-8")
        assert load_credentials() is None

    def test_load_empty_file(self, temp_credentials_dir: Path) -> None:
        temp_credentials_dir.write_text("", encoding="utf-8")
        assert load_credentials() is None

    def test_expired_credentials(self, temp_credentials_dir: Path) -> None:
        creds = AuthCredentials(
            api_key="tcn_test_key_1234567890",
            project_slug="vivid-kudu-655",
            domain="www.teracron.com",
            created_at=int(time.time()) - 3600,
            expires_at=int(time.time()) - 1,  # Expired 1 second ago
        )
        save_credentials(creds)
        assert load_credentials() is None

    def test_non_expired_credentials(self, temp_credentials_dir: Path) -> None:
        creds = AuthCredentials(
            api_key="tcn_test_key_1234567890",
            project_slug="vivid-kudu-655",
            domain="www.teracron.com",
            created_at=int(time.time()),
            expires_at=int(time.time()) + 3600,  # Expires in 1 hour
        )
        save_credentials(creds)
        loaded = load_credentials()
        assert loaded is not None
        assert loaded.api_key == creds.api_key


# ── delete_credentials ──


class TestDeleteCredentials:
    def test_delete_existing(self, temp_credentials_dir: Path) -> None:
        creds = AuthCredentials(
            api_key="tcn_test_key_1234567890",
            project_slug="vivid-kudu-655",
            domain="www.teracron.com",
            created_at=int(time.time()),
        )
        save_credentials(creds)
        assert temp_credentials_dir.exists()

        deleted = delete_credentials()
        assert deleted is True
        assert not temp_credentials_dir.exists()

    def test_delete_nonexistent(self, temp_credentials_dir: Path) -> None:
        assert delete_credentials() is False


# ── login ──


class TestLogin:
    def test_valid_login(self, temp_credentials_dir: Path) -> None:
        key = _make_test_key()
        creds = login(key)
        assert creds.project_slug == "vivid-kudu-655"
        assert creds.domain == "www.teracron.com"

        # Verify persisted
        loaded = load_credentials()
        assert loaded is not None
        assert loaded.api_key == key

    def test_custom_domain(self, temp_credentials_dir: Path) -> None:
        key = _make_test_key()
        creds = login(key, domain="api.teracron.com")
        assert creds.domain == "api.teracron.com"

    def test_invalid_key_format(self, temp_credentials_dir: Path) -> None:
        with pytest.raises(ValueError, match="Invalid API key format"):
            login("bad_key")

    def test_empty_key(self, temp_credentials_dir: Path) -> None:
        with pytest.raises(ValueError, match="Invalid API key format"):
            login("")

    def test_whitespace_stripped(self, temp_credentials_dir: Path) -> None:
        key = _make_test_key()
        creds = login(f"  {key}  ")
        assert creds.project_slug == "vivid-kudu-655"


# ── whoami ──


class TestWhoami:
    def test_logged_in(self, temp_credentials_dir: Path) -> None:
        key = _make_test_key()
        login(key)
        result = whoami()
        assert result is not None
        assert result.project_slug == "vivid-kudu-655"

    def test_not_logged_in(self, temp_credentials_dir: Path) -> None:
        assert whoami() is None


# ── logout ──


class TestLogout:
    def test_logout_clears_credentials(self, temp_credentials_dir: Path) -> None:
        key = _make_test_key()
        login(key)
        assert whoami() is not None
        logout()
        assert whoami() is None

    def test_logout_when_not_logged_in(self, temp_credentials_dir: Path) -> None:
        assert logout() is False


# ── resolve_api_key ──


class TestResolveApiKey:
    def test_cli_key_highest_priority(self, temp_credentials_dir: Path) -> None:
        key = _make_test_key()
        login(key)  # Store a credential

        cli_key = "tcn_" + "x" * 20  # Different key via CLI
        result = resolve_api_key(cli_key=cli_key)
        assert result == cli_key

    def test_env_var_over_file(
        self, temp_credentials_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        key = _make_test_key()
        login(key)  # Store a credential

        env_key = "tcn_" + "e" * 20
        monkeypatch.setenv("TERACRON_API_KEY", env_key)
        result = resolve_api_key()
        assert result == env_key

    def test_file_fallback(self, temp_credentials_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        key = _make_test_key()
        login(key)

        monkeypatch.delenv("TERACRON_API_KEY", raising=False)
        result = resolve_api_key()
        assert result == key

    def test_no_key_available(
        self, temp_credentials_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TERACRON_API_KEY", raising=False)
        result = resolve_api_key()
        assert result is None

    def test_invalid_cli_key_falls_through(
        self, temp_credentials_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        key = _make_test_key()
        login(key)
        monkeypatch.delenv("TERACRON_API_KEY", raising=False)
        result = resolve_api_key(cli_key="bad")
        assert result == key  # Falls through to stored credential
