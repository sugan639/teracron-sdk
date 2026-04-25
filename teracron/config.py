# -*- coding: utf-8 -*-
"""
Configuration validation and resolution.

Fails fast on invalid config — no silent degradation.

Supports two authentication modes:
    1. ``api_key`` (recommended) — single token from dashboard.
    2. ``project_slug`` + ``public_key`` — explicit fields (backward-compat).

If ``api_key`` is provided, ``project_slug`` and ``public_key`` are
extracted from it automatically.  Explicit fields override the decoded
values if both are supplied (defense in depth — not recommended).
"""

from __future__ import annotations

import os
import re
from typing import Optional

from .apikey import decode_api_key
from .types import ResolvedConfig

_SLUG_PATTERN = re.compile(r"^[a-z]+-[a-z]+-\d{3}$")
_PEM_HEADER = "-----BEGIN PUBLIC KEY-----"

# Domain allowlist — prevents SSRF-style redirection of telemetry data.
# Only *.teracron.com is accepted unless the user sets TERACRON_ALLOW_CUSTOM_DOMAIN=1.
_ALLOWED_DOMAIN_SUFFIX = ".teracron.com"
_ALLOWED_DOMAINS_EXACT = frozenset({"teracron.com", "www.teracron.com"})

_MIN_INTERVAL_S = 5.0
_MAX_INTERVAL_S = 300.0
_DEFAULT_INTERVAL_S = 30.0

_DEFAULT_DOMAIN = "www.teracron.com"
_DEFAULT_MAX_BUFFER = 60
_MAX_BUFFER_SIZE = 10_000  # Safety cap: ~1 MB of snapshots max
_DEFAULT_TIMEOUT_S = 10.0
_MIN_TIMEOUT_S = 2.0
_MAX_TIMEOUT_S = 30.0


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp a numeric value to [lo, hi]. Returns lo for non-finite values."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return lo
    if v != v:  # NaN check without math import
        return lo
    return max(lo, min(hi, v))


def _sanitise_domain(raw: str) -> str:
    """Strip protocol, trailing slashes, and paths — keep host[:port] only."""
    cleaned = raw.strip()
    cleaned = re.sub(r"^https?://", "", cleaned)
    slash_idx = cleaned.find("/")
    if slash_idx != -1:
        cleaned = cleaned[:slash_idx]
    return cleaned if cleaned else _DEFAULT_DOMAIN


def _validate_domain(domain: str) -> str:
    """
    Validate that domain is an allowed Teracron endpoint.

    By default only ``*.teracron.com`` is accepted. Set
    ``TERACRON_ALLOW_CUSTOM_DOMAIN=1`` to bypass (e.g. for on-prem).

    Returns the validated domain or raises ValueError.
    """
    allow_custom = os.environ.get("TERACRON_ALLOW_CUSTOM_DOMAIN", "").lower()
    if allow_custom in ("1", "true", "yes"):
        return domain

    # Extract host without port for comparison
    host = domain.split(":")[0].lower()

    if host in _ALLOWED_DOMAINS_EXACT or host.endswith(_ALLOWED_DOMAIN_SUFFIX):
        return domain

    raise ValueError(
        f'[Teracron] Domain "{domain}" is not allowed. '
        "Only *.teracron.com endpoints are accepted. "
        "Set TERACRON_ALLOW_CUSTOM_DOMAIN=1 to use a custom domain."
    )


def resolve_config(
    *,
    api_key: Optional[str] = None,
    project_slug: Optional[str] = None,
    public_key: Optional[str] = None,
    domain: Optional[str] = None,
    interval_s: Optional[float] = None,
    max_buffer_size: Optional[int] = None,
    timeout_s: Optional[float] = None,
    debug: Optional[bool] = None,
    target_pid: Optional[int] = None,
) -> ResolvedConfig:
    """
    Validate and resolve configuration.

    Priority order for slug/key resolution:
        1. Explicit ``project_slug`` / ``public_key`` kwargs (highest)
        2. Decoded from ``api_key`` kwarg
        3. ``TERACRON_API_KEY`` env var
        4. ``TERACRON_PROJECT_SLUG`` / ``TERACRON_PUBLIC_KEY`` env vars (lowest)

    Environment variable fallbacks:
        TERACRON_API_KEY          — single API key token (recommended)
        TERACRON_PROJECT_SLUG     — legacy: project slug
        TERACRON_PUBLIC_KEY       — legacy: PEM public key
        TERACRON_DOMAIN
        TERACRON_INTERVAL
        TERACRON_TIMEOUT
        TERACRON_MAX_BUFFER
        TERACRON_DEBUG
        TERACRON_TARGET_PID
    """
    # ── Resolve slug + public_key ──
    slug = project_slug
    key = public_key

    # Decode from api_key kwarg or env var (fills in slug + key if not set)
    _api_key = api_key or os.environ.get("TERACRON_API_KEY", "")
    if _api_key:
        decoded_slug, decoded_key = decode_api_key(_api_key)
        if not slug:
            slug = decoded_slug
        if not key:
            key = decoded_key

    # Fallback to individual env vars
    if not slug:
        slug = os.environ.get("TERACRON_PROJECT_SLUG", "")
    if not key:
        key = os.environ.get("TERACRON_PUBLIC_KEY", "")

    # ── Validate slug ──
    if not slug or not isinstance(slug, str):
        raise ValueError(
            "[Teracron] api_key is required. "
            "Set the TERACRON_API_KEY environment variable or pass api_key= to TeracronClient."
        )
    if not _SLUG_PATTERN.match(slug):
        raise ValueError(
            f'[Teracron] Invalid project_slug format: "{slug}". '
            'Expected: adjective-animal-NNN (e.g. "vivid-kudu-655").'
        )

    # ── Validate public_key ──
    if not key or not isinstance(key, str):
        raise ValueError(
            "[Teracron] api_key is required. "
            "Set the TERACRON_API_KEY environment variable or pass api_key= to TeracronClient."
        )
    if _PEM_HEADER not in key:
        raise ValueError(
            "[Teracron] public_key must be a PEM-encoded RSA public key "
            "(contains '-----BEGIN PUBLIC KEY-----')."
        )

    # ── Optional with bounds ──
    _raw_interval = interval_s
    if _raw_interval is None:
        env_interval = os.environ.get("TERACRON_INTERVAL")
        if env_interval is not None:
            try:
                _raw_interval = float(env_interval)
            except ValueError:
                _raw_interval = None
    resolved_interval = _clamp(
        _raw_interval if _raw_interval is not None else _DEFAULT_INTERVAL_S,
        _MIN_INTERVAL_S,
        _MAX_INTERVAL_S,
    )

    _raw_timeout = timeout_s
    if _raw_timeout is None:
        env_timeout = os.environ.get("TERACRON_TIMEOUT")
        if env_timeout is not None:
            try:
                _raw_timeout = float(env_timeout)
            except ValueError:
                _raw_timeout = None
    resolved_timeout = _clamp(
        _raw_timeout if _raw_timeout is not None else _DEFAULT_TIMEOUT_S,
        _MIN_TIMEOUT_S,
        _MAX_TIMEOUT_S,
    )

    _raw_buffer = max_buffer_size
    if _raw_buffer is None:
        env_buf = os.environ.get("TERACRON_MAX_BUFFER")
        if env_buf is not None:
            try:
                _raw_buffer = int(env_buf)
            except ValueError:
                _raw_buffer = None
    resolved_buffer = max(1, min(int(_raw_buffer if _raw_buffer is not None else _DEFAULT_MAX_BUFFER), _MAX_BUFFER_SIZE))

    _raw_domain = domain or os.environ.get("TERACRON_DOMAIN")
    resolved_domain = _sanitise_domain(_raw_domain) if _raw_domain else _DEFAULT_DOMAIN
    resolved_domain = _validate_domain(resolved_domain)

    _raw_debug = debug
    if _raw_debug is None:
        env_debug = os.environ.get("TERACRON_DEBUG", "").lower()
        _raw_debug = env_debug in ("1", "true", "yes")

    _raw_pid = target_pid
    if _raw_pid is None:
        env_pid = os.environ.get("TERACRON_TARGET_PID")
        if env_pid is not None:
            try:
                _raw_pid = int(env_pid)
            except ValueError:
                _raw_pid = None

    return ResolvedConfig(
        project_slug=slug,
        public_key=key,
        domain=resolved_domain,
        interval_s=resolved_interval,
        max_buffer_size=resolved_buffer,
        timeout_s=resolved_timeout,
        debug=bool(_raw_debug),
        target_pid=_raw_pid,
    )
