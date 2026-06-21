# -*- coding: utf-8 -*-
"""
API Key encoding/decoding.

The Teracron API key is a single opaque token that encodes the project
**mode**, slug, and PEM public key:

    tcn_<base64url(mode + ":" + slug + ":" + publicKeyPEM)>

where ``mode`` is the short form ``dev`` (development) or ``prod``
(production). Each project has *two* independent key pairs — one per
mode — so the mode carried by the key decides which bucket telemetry
lands in (and which private key the server decrypts with):

    - a ``dev`` key  → logs go to the project's **development** view
    - a ``prod`` key → logs go to the project's **production** view

Users copy ONE value from the dashboard, set ONE env var, and the SDK
extracts mode + slug + key internally.  The ``tcn_`` prefix identifies
the token type and guards against accidental misuse (e.g. pasting a
random JWT or AWS key).

BACKWARD COMPATIBILITY:
    Legacy keys minted before the mode split encode only ``slug:PEM``
    (two parts).  Such keys decode to ``mode="production"`` so existing
    deployments keep working unchanged — production is the safe default.

SECURITY:
    - The API key contains ONLY the public key — no secrets.
    - base64url encoding is used (URL-safe, no padding) for safe
      transport in env vars, CLI args, and config files.
    - mode and slug are validated on decode to catch corruption early.
"""

from __future__ import annotations

import base64
import re

_PREFIX = "tcn_"
_SLUG_PATTERN = re.compile(r"^[a-z]+-[a-z]+-\d{3}$")
_PEM_HEADER = "-----BEGIN PUBLIC KEY-----"

# Canonical (long) mode names used throughout the SDK / dashboard / schema.
MODE_DEVELOPMENT = "development"
MODE_PRODUCTION = "production"
_VALID_MODES = frozenset({MODE_DEVELOPMENT, MODE_PRODUCTION})

# Short ↔ long mode mapping. The *short* form is what travels inside the
# encoded API-key payload (keeps the token compact); the *long* form is the
# canonical value surfaced to callers, stored in the schema, and shown in UI.
_MODE_TO_SHORT = {MODE_DEVELOPMENT: "dev", MODE_PRODUCTION: "prod"}
_SHORT_TO_MODE = {"dev": MODE_DEVELOPMENT, "prod": MODE_PRODUCTION}


def normalize_mode(mode: str) -> str:
    """
    Normalize any accepted spelling of a mode to its canonical long form.

    Accepts ``development``/``dev`` and ``production``/``prod`` (case
    insensitive). Raises ``ValueError`` for anything else.
    """
    if not mode or not isinstance(mode, str):
        raise ValueError("mode is required (development|production).")
    m = mode.strip().lower()
    if m in _VALID_MODES:
        return m
    if m in _SHORT_TO_MODE:
        return _SHORT_TO_MODE[m]
    raise ValueError(
        f"Invalid mode: {mode!r}. Expected 'development' or 'production'."
    )


def encode_api_key(
    slug: str,
    public_key_pem: str,
    mode: str = MODE_PRODUCTION,
) -> str:
    """
    Encode mode + project slug + PEM public key into a single API key string.

    Args:
        slug: project slug (``adjective-animal-NNN``).
        public_key_pem: the **mode-matching** PEM public key.
        mode: ``development``/``dev`` or ``production``/``prod``
              (default ``production`` — matches legacy behaviour).

    Returns a string of the form ``tcn_<base64url payload>`` where the
    payload is ``<short-mode>:<slug>:<PEM>``.
    """
    if not slug or not _SLUG_PATTERN.match(slug):
        raise ValueError(f"Invalid slug: {slug!r}")
    if _PEM_HEADER not in public_key_pem:
        raise ValueError("public_key_pem must be a PEM-encoded RSA public key.")

    canonical = normalize_mode(mode)
    short = _MODE_TO_SHORT[canonical]

    payload = f"{short}:{slug}:{public_key_pem}".encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    return f"{_PREFIX}{encoded}"


def decode_api_key(api_key: str) -> tuple:
    """
    Decode a Teracron API key into ``(slug, public_key_pem, mode)``.

    Supports both formats transparently:
        - new:    ``<short-mode>:<slug>:<PEM>``  → mode as encoded
        - legacy: ``<slug>:<PEM>``               → mode = ``production``

    Raises ``ValueError`` on malformed keys.

    Returns:
        Tuple of (slug: str, public_key_pem: str, mode: str).
        ``mode`` is always a canonical long form (``development`` /
        ``production``).
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

    # The payload is a colon-delimited tuple. We split into at most 3 parts so
    # that the PEM body (which itself contains no ':') is never fragmented:
    #   - 3 parts → new format: <short-mode>:<slug>:<PEM>
    #   - 2 parts → legacy format: <slug>:<PEM>  (mode defaults to production)
    # The leading token of a new key is always 'dev'/'prod' and can never be a
    # valid slug (slugs require two hyphens + digits), so the two formats are
    # unambiguous to tell apart.
    parts = payload.split(":", 2)

    if len(parts) == 3 and parts[0].lower() in _SHORT_TO_MODE:
        mode = _SHORT_TO_MODE[parts[0].lower()]
        slug = parts[1]
        public_key_pem = parts[2]
    else:
        # Legacy (or mode-less) key: first segment is the slug.
        colon_idx = payload.find(":")
        if colon_idx == -1:
            raise ValueError(
                "[Teracron] Malformed API key — missing separator. "
                "Copy the full API key from the Teracron dashboard."
            )
        mode = MODE_PRODUCTION
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

    return (slug, public_key_pem, mode)
