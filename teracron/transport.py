# -*- coding: utf-8 -*-
"""
Transport — sends encrypted envelopes to the Teracron ingest endpoint.

Design:
    - Uses ``requests.Session`` with HTTP keep-alive for persistent connections.
    - Non-blocking failure model: never raises on transport errors (returns result).
    - Connection timeout prevents hanging on unresponsive endpoints.
    - Follows redirects (307/308) automatically via requests library.
    - Fire-and-return — retry logic is handled at the client level.

ENDPOINT RESOLUTION:
    The target scheme is resolved per-domain via ``config.resolve_scheme``:
      - Production (``*.teracron.com``)  → ``https://`` (TLS, system CA bundle).
      - Local interface (loopback host)  → ``http://`` (the local interface,
        ``teracron.local_interface``, terminates here; there is no TLS in the
        local path). Payloads remain RSA+AES encrypted regardless of scheme,
        so dropping TLS for the loopback hop does not expose plaintext.

SECURITY: For non-loopback hosts only TLS is used (requests defaults to the
system CA bundle). The ``http://`` path is strictly loopback-scoped — see
``config._is_loopback_host``.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import __version__
from .config import resolve_scheme

_SDK_VERSION = __version__
_MAX_RETRIES_ON_CONNECT = 1  # Single retry on connection reset (keep-alive stale)


@dataclass(frozen=True)
class TransportResult:
    """Outcome of a single send attempt."""

    success: bool
    status_code: int


class Transport:
    """
    Persistent HTTPS transport bound to a single ingest endpoint.

    Maintains a ``requests.Session`` with keep-alive to minimise TLS
    handshake overhead across repeated flushes.
    """

    __slots__ = ("_session", "_url", "_slug", "_timeout", "_scheme", "_mode")

    def __init__(
        self,
        domain: str,
        slug: str,
        timeout_s: float,
        mode: str = "production",
    ) -> None:
        # Scheme is loopback-aware: http:// for the local interface, https://
        # for production. Mounting the retry adapter on the same scheme below
        # keeps keep-alive/retry behaviour identical across both paths.
        self._scheme = resolve_scheme(domain)
        self._url = f"{self._scheme}://{domain}/api/ingest"
        self._slug = slug
        self._timeout = timeout_s
        # Mode travels on every ingest request via the X-Project-Mode header so
        # the server can (a) pick the correct mode-scoped private key to decrypt
        # and (b) tag stored rows with the originating mode. The header is a
        # *hint*; the server still cross-checks it against the key actually used.
        self._mode = mode if mode in ("development", "production") else "production"

        self._session = requests.Session()

        # Retry adapter: single retry on connection errors (stale keep-alive)
        # urllib3 <1.26.0 uses method_whitelist; >=1.26.0 uses allowed_methods.
        retry_kwargs = dict(
            total=_MAX_RETRIES_ON_CONNECT,
            backoff_factor=0.1,
            status_forcelist=[502, 503, 504],
        )
        try:
            retry_kwargs["allowed_methods"] = ["POST"]
            retry = Retry(**retry_kwargs)
        except TypeError:
            retry_kwargs.pop("allowed_methods", None)
            retry_kwargs["method_whitelist"] = ["POST"]
            retry = Retry(**retry_kwargs)

        adapter = HTTPAdapter(
            max_retries=retry,
            pool_connections=1,
            pool_maxsize=1,
        )
        # Mount on the resolved scheme so loopback (http) and production (https)
        # both get the keep-alive pool + single-retry behaviour.
        self._session.mount(f"{self._scheme}://", adapter)

        self._session.headers.update({
            "Content-Type": "application/octet-stream",
            "X-Project-Slug": self._slug,
            "X-Project-Mode": self._mode,
            "User-Agent": f"teracron-sdk-python/{_SDK_VERSION} python/{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        })

    def send(self, envelope: bytes) -> TransportResult:
        """
        Send an encrypted metrics envelope. Never raises — returns TransportResult.
        """
        return self._post(self._url, envelope)

    def send_traces(self, envelope: bytes) -> TransportResult:
        """
        Send an encrypted trace envelope to ``POST /api/v1/traces``.

        Reuses the same ``requests.Session`` (shared keep-alive pool)
        but targets the dedicated traces endpoint.  Never raises.
        """
        traces_url = self._url.replace("/api/ingest", "/api/v1/traces")
        return self._post(traces_url, envelope)

    def _post(self, url: str, data: bytes) -> TransportResult:
        """Common POST logic — never raises."""
        try:
            resp = self._session.post(
                url,
                data=data,
                timeout=self._timeout,
                allow_redirects=False,
            )
            return TransportResult(
                success=resp.status_code == 202,
                status_code=resp.status_code,
            )
        except requests.RequestException:
            return TransportResult(success=False, status_code=0)
        except Exception:
            # Defensive: catch-all for unexpected errors (SSL, OS-level, etc.)
            return TransportResult(success=False, status_code=0)

    def send_events(self, payload: bytes) -> TransportResult:
        """
        Send structured workflow events to ``POST /api/v1/events``.

        Reuses the same ``requests.Session``.  Never raises.
        """
        events_url = self._url.replace("/api/ingest", "/api/v1/events")
        return self._post(events_url, payload)

    @property
    def query_base_url(self) -> str:
        """Base URL for query endpoints: ``{scheme}://{domain}/api/v1``."""
        return self._url.replace("/api/ingest", "/api/v1")

    def close(self) -> None:
        """Release the underlying connection pool."""
        try:
            self._session.close()
        except Exception:  # nosec B110
            pass
