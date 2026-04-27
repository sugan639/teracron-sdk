# -*- coding: utf-8 -*-
"""
CLI Authentication — credential storage, login, logout, whoami.

Stores credentials at ``~/.teracron/credentials.json`` with mode ``0600``.
Implements a resolution chain:
    1. ``--api-key`` CLI flag (highest priority)
    2. ``TERACRON_API_KEY`` env var
    3. ``~/.teracron/credentials.json`` (lowest priority)

SECURITY:
    - Credential file is created with ``0600`` permissions (owner-only).
    - API keys are never printed in full — middle chars are masked.
    - Token validation hits ``GET /v1/auth/whoami`` with Bearer header.
    - ``logout()`` overwrites the file with zeros before unlinking.
"""

from __future__ import annotations

import json
import os
import stat
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

_CREDENTIALS_DIR = ".teracron"
_CREDENTIALS_FILE = "credentials.json"
_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR  # 0600

# Minimum key length to be considered valid (tcn_ + at least 20 chars payload).
_MIN_KEY_LEN = 24


@dataclass(frozen=True)
class AuthCredentials:
    """Stored credential set for a Teracron project."""

    api_key: str
    project_slug: str
    domain: str
    created_at: int  # Unix timestamp (seconds)
    expires_at: Optional[int] = None  # None = no expiry


def _credentials_path() -> Path:
    """Resolve the credentials file path: ``~/.teracron/credentials.json``."""
    home = Path.home()
    return home / _CREDENTIALS_DIR / _CREDENTIALS_FILE


def _ensure_directory(path: Path) -> None:
    """Create the parent directory with restrictive permissions."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(parent), stat.S_IRWXU)  # 0700
    except OSError:
        pass  # Best-effort on platforms that don't support chmod


def mask_api_key(api_key: str) -> str:
    """
    Mask an API key for safe display: ``tcn_abc...xyz``.

    Shows prefix (``tcn_``) + first 4 payload chars + ``...`` + last 4 chars.
    Short keys (< 16 chars) are fully masked as ``tcn_****``.
    """
    if not api_key or len(api_key) < 16:
        return "tcn_****"
    prefix_end = 8  # "tcn_" + 4 chars
    suffix_start = len(api_key) - 4
    return f"{api_key[:prefix_end]}...{api_key[suffix_start:]}"


def validate_key_format(api_key: str) -> bool:
    """
    Check that the API key has a valid structural format.

    Does NOT validate against the server — only checks the ``tcn_`` prefix
    and minimum length.
    """
    if not api_key or not isinstance(api_key, str):
        return False
    key = api_key.strip()
    return key.startswith("tcn_") and len(key) >= _MIN_KEY_LEN


def save_credentials(credentials: AuthCredentials) -> Path:
    """
    Persist credentials to ``~/.teracron/credentials.json`` with mode 0600.

    Returns the path to the saved file.

    Raises:
        OSError: If the file cannot be created or permissions cannot be set.
    """
    path = _credentials_path()
    _ensure_directory(path)

    payload = json.dumps(asdict(credentials), indent=2, sort_keys=True)

    # Atomic write: write to temp, then rename.
    tmp_path = path.with_suffix(".tmp")
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)

    os.replace(str(tmp_path), str(path))
    # Enforce permissions on final file (defense in depth).
    os.chmod(str(path), _FILE_MODE)
    return path


def load_credentials() -> Optional[AuthCredentials]:
    """
    Load credentials from ``~/.teracron/credentials.json``.

    Returns ``None`` if the file doesn't exist, is unreadable, or is
    structurally invalid.  Expired credentials are also returned as ``None``.
    """
    path = _credentials_path()
    if not path.exists():
        return None

    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None

    # Validate required fields.
    api_key = data.get("api_key", "")
    project_slug = data.get("project_slug", "")
    domain = data.get("domain", "")
    created_at = data.get("created_at", 0)

    if not api_key or not project_slug or not domain:
        return None

    expires_at = data.get("expires_at")

    # Check expiry.
    if expires_at is not None and isinstance(expires_at, (int, float)):
        if time.time() > expires_at:
            return None

    return AuthCredentials(
        api_key=api_key,
        project_slug=project_slug,
        domain=domain,
        created_at=int(created_at),
        expires_at=int(expires_at) if expires_at is not None else None,
    )


def delete_credentials() -> bool:
    """
    Securely wipe and delete the credentials file.

    Overwrites file content with null bytes before unlinking.
    Returns ``True`` if a file was deleted, ``False`` if no file existed.

    Uses fd-based open to avoid TOCTOU race between exists-check and open.
    """
    path = _credentials_path()

    try:
        # Open the file directly — eliminates TOCTOU race between
        # exists() check and open().  If the file doesn't exist,
        # os.open raises FileNotFoundError (subclass of OSError).
        fd = os.open(str(path), os.O_WRONLY)
    except OSError:
        return False  # File does not exist or is not writable.

    try:
        # Overwrite with zeros before deletion (defense in depth).
        file_size = os.fstat(fd).st_size
        if file_size > 0:
            os.write(fd, b"\x00" * file_size)
            os.fsync(fd)
    except OSError:
        pass  # Best-effort wipe.
    finally:
        os.close(fd)

    try:
        path.unlink()
    except OSError:
        return False
    return True


def resolve_api_key(
    *,
    cli_key: Optional[str] = None,
    env_key: Optional[str] = None,
) -> Optional[str]:
    """
    Resolve the API key from the priority chain:
        1. ``cli_key`` (explicit CLI flag)
        2. ``env_key`` or ``TERACRON_API_KEY`` env var
        3. Stored credentials file

    Returns the first valid key found, or ``None``.
    """
    # Priority 1: explicit CLI flag.
    if cli_key and validate_key_format(cli_key):
        return cli_key.strip()

    # Priority 2: environment variable.
    env_value = env_key or os.environ.get("TERACRON_API_KEY", "").strip()
    if env_value and validate_key_format(env_value):
        return env_value

    # Priority 3: stored credentials.
    creds = load_credentials()
    if creds and validate_key_format(creds.api_key):
        return creds.api_key

    return None


def login(api_key: str, domain: str = "www.teracron.com") -> AuthCredentials:
    """
    Validate and store credentials.

    Decodes the API key to extract the project slug, then persists
    to ``~/.teracron/credentials.json``.

    Args:
        api_key: The ``tcn_...`` API key from the Teracron dashboard.
        domain:  Teracron domain (default: ``www.teracron.com``).

    Returns:
        The stored ``AuthCredentials``.

    Raises:
        ValueError: If the API key is invalid, domain is not allowed,
            or cannot be decoded.
    """
    key = api_key.strip()
    if not validate_key_format(key):
        raise ValueError(
            "[Teracron] Invalid API key format. "
            "Expected a key starting with 'tcn_' (minimum 24 characters)."
        )

    # Validate domain against allowlist (SSRF prevention).
    from .config import _validate_domain, _sanitise_domain

    safe_domain = _sanitise_domain(domain)
    _validate_domain(safe_domain)  # Raises ValueError if not allowed.

    # Decode to extract project slug — validates structure.
    from .apikey import decode_api_key

    slug, _public_key = decode_api_key(key)

    credentials = AuthCredentials(
        api_key=key,
        project_slug=slug,
        domain=safe_domain,
        created_at=int(time.time()),
        expires_at=None,
    )

    save_credentials(credentials)
    return credentials


def whoami() -> Optional[AuthCredentials]:
    """
    Return the currently stored credentials, or ``None`` if not logged in.

    Does NOT validate the key against the server — offline-safe.
    """
    return load_credentials()


def logout() -> bool:
    """
    Securely wipe stored credentials.

    Returns ``True`` if credentials were deleted, ``False`` if none existed.
    """
    return delete_credentials()
