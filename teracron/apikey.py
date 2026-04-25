# -*- coding: utf-8 -*-
"""
API Key encoding/decoding.

The Teracron API key is a single opaque token that encodes both the
project slug and the PEM public key:

    tcn_<base64url(slug + ":" + publicKeyPEM)>

Users copy ONE value from the dashboard, set ONE env var, and the SDK
extracts both fields internally.  The ``tcn_`` prefix identifies the
token type and guards against accidental misuse (e.g. pasting a random
JWT or AWS key).

SECURITY:
    - The API key contains ONLY the public key — no secrets.
    - base64url encoding is used (URL-safe, no padding) for safe
      transport in env vars, CLI args, and config files.
    - The slug is validated on decode to catch corruption early.
"""

from __future__ import annotations

import base64
import re

_PREFIX = "tcn_"
_SLUG_PATTERN = re.compile(r"^[a-z]+-[a-z]+-\d{3}$")
_PEM_HEADER = "-----BEGIN PUBLIC KEY-----"


def encode_api_key(slug: str, public_key_pem: str) -> str:
    """
    Encode a project slug + PEM public key into a single API key string.

    Returns a string of the form ``tcn_<base64url payload>``.
    """
    if not slug or not _SLUG_PATTERN.match(slug):
        raise ValueError(f"Invalid slug: {slug!r}")
    if _PEM_HEADER not in public_key_pem:
        raise ValueError("public_key_pem must be a PEM-encoded RSA public key.")

    payload = f"{slug}:{public_key_pem}".encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    return f"{_PREFIX}{encoded}"


def decode_api_key(api_key: str) -> tuple:
    """
    Decode a Teracron API key into (slug, public_key_pem).

    Raises ``ValueError`` on malformed keys.

    Returns:
        Tuple of (slug: str, public_key_pem: str).
    """
    if not api_key or not isinstance(api_key, str):
        raise ValueError(
            "[Teracron] api_key is required and must be a non-empty string."
        )

    key = api_key.strip()

    if not key.startswith(_PREFIX):
        raise ValueError(
            "[Teracron] Invalid API key format. "
            "Expected a key starting with 'tcn_'. "
            "Copy the full API key from the Teracron dashboard."
        )

    encoded = key[len(_PREFIX):]

    # Re-add base64 padding
    padding = 4 - (len(encoded) % 4)
    if padding != 4:
        encoded += "=" * padding

    try:
        payload = base64.urlsafe_b64decode(encoded).decode("utf-8")
    except Exception:
        raise ValueError(
            "[Teracron] Corrupted API key — base64 decode failed. "
            "Copy the full API key from the Teracron dashboard."
        )

    # Split on first ":"
    colon_idx = payload.find(":")
    if colon_idx == -1:
        raise ValueError(
            "[Teracron] Malformed API key — missing separator. "
            "Copy the full API key from the Teracron dashboard."
        )

    slug = payload[:colon_idx]
    public_key_pem = payload[colon_idx + 1:]

    if not _SLUG_PATTERN.match(slug):
        raise ValueError(
            f"[Teracron] Corrupted API key — invalid slug component: {slug!r}."
        )

    if _PEM_HEADER not in public_key_pem:
        raise ValueError(
            "[Teracron] Corrupted API key — invalid public key component."
        )

    return (slug, public_key_pem)
