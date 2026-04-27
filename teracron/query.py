# -*- coding: utf-8 -*-
"""
Read-only query client for inspecting Teracron workflow events and traces.

Designed for AI agents and CLI tooling — provides structured access to
workflow events, traces, and span details via the Teracron REST API.

All requests use ``Authorization: Bearer <api_key>`` header.
Timeout: 10s. No retries (queries, not ingest).

IMPORTANT: The backend endpoints may not be deployed yet. The client
handles HTTP 404 gracefully, returning a structured error with a hint
that the endpoint is not yet available.

SECURITY:
    - API key is sent ONLY in the Authorization header (never in URL params).
    - No PII is logged from responses.
    - Responses are returned as-is — no caching of sensitive data.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests

from . import __version__
from .config import _validate_domain, _sanitise_domain

_SDK_VERSION = __version__
_QUERY_TIMEOUT_S = 10.0
_MAX_LIMIT = 1000

# Allowed hex chars for trace/span IDs — lowercase only, consistent with span generation.
_HEX_CHARS = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class QueryResult:
    """Typed outcome of a query request."""

    success: bool
    status_code: int
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    hint: Optional[str] = None


def _error_result(status_code: int, error: str, hint: str = "") -> Dict[str, Any]:
    """Build a structured error dict (consistent CLI output format)."""
    result: Dict[str, Any] = {"error": error, "status_code": status_code}
    if hint:
        result["hint"] = hint
    return result


class TeracronQueryClient:
    """
    Read-only client for querying Teracron workflow events and traces.

    Thread-safe: each instance holds its own ``requests.Session``.
    No retries — queries are latency-sensitive and idempotent.

    Args:
        api_key: The ``tcn_...`` API key (used in Authorization header).
        domain:  Teracron domain (default: ``www.teracron.com``).
        timeout_s: HTTP timeout in seconds (default: 10.0).
    """

    __slots__ = ("_base_url", "_session", "_timeout")

    def __init__(
        self,
        api_key: str,
        domain: str = "www.teracron.com",
        timeout_s: float = _QUERY_TIMEOUT_S,
    ) -> None:
        if not api_key or not isinstance(api_key, str):
            raise ValueError("[Teracron] api_key is required for TeracronQueryClient.")

        # Sanitise and validate domain to prevent SSRF — same rules as config.py.
        safe_domain = _sanitise_domain(domain)
        _validate_domain(safe_domain)

        self._base_url = f"https://{safe_domain}/v1"
        self._timeout = max(2.0, min(timeout_s, 30.0))

        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": (
                    f"teracron-sdk-python/{_SDK_VERSION} "
                    f"python/{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
                ),
            }
        )

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Common GET logic. Never raises — returns structured result dict.

        Handles:
            - 200: parse JSON body.
            - 401: auth error with hint.
            - 404: endpoint not deployed yet.
            - 429: rate limited with Retry-After hint.
            - Other: generic error.
        """
        url = f"{self._base_url}{path}"
        if params:
            # Filter out None values.
            clean_params = {k: v for k, v in params.items() if v is not None}
            if clean_params:
                url = f"{url}?{urlencode(clean_params)}"

        try:
            resp = self._session.get(url, timeout=self._timeout)
        except requests.ConnectionError:
            return _error_result(
                0,
                "Connection failed — cannot reach Teracron API.",
                hint=f"Check that {self._base_url} is reachable.",
            )
        except requests.Timeout:
            return _error_result(
                0,
                f"Request timed out after {self._timeout}s.",
                hint="Try again or increase timeout.",
            )
        except requests.RequestException as exc:
            return _error_result(0, f"Request failed: {type(exc).__name__}")

        if resp.status_code == 200:
            try:
                return resp.json()
            except (ValueError, KeyError):
                return _error_result(200, "Invalid JSON response from server.")

        if resp.status_code == 401:
            return _error_result(
                401,
                "Authentication failed — invalid or expired API key.",
                hint="Run: teracron-agent login",
            )

        if resp.status_code == 404:
            return _error_result(
                404,
                "Endpoint not found — this API is not yet deployed on the backend.",
                hint=(
                    "The SDK is ready for this feature, but the Teracron backend "
                    "has not yet been updated. Check https://docs.teracron.com for status."
                ),
            )

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After", "30")
            return _error_result(
                429,
                "Rate limited by Teracron API.",
                hint=f"Retry after {retry_after} seconds.",
            )

        return _error_result(
            resp.status_code,
            f"Unexpected response: HTTP {resp.status_code}",
        )

    def list_events(
        self,
        *,
        workflow: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        since: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Query recent workflow events.

        ``GET /v1/events?workflow=X&status=failed&limit=50&since=...``

        Args:
            workflow: Filter by workflow name.
            status:   Filter by status (``succeeded``, ``failed``, ``in_progress``).
            limit:    Max events to return (clamped to 1–1000).
            since:    ISO 8601 timestamp — events after this time.

        Returns:
            Dict with ``events`` list on success, or ``error`` + ``hint`` on failure.
        """
        safe_limit = max(1, min(limit, _MAX_LIMIT))
        return self._get(
            "/events",
            params={
                "workflow": workflow,
                "status": status,
                "limit": safe_limit,
                "since": since,
            },
        )

    def get_trace(self, trace_id: str) -> Dict[str, Any]:
        """
        Fetch a full trace span tree.

        ``GET /v1/traces/{trace_id}``

        Args:
            trace_id: The 32-char hex trace ID.

        Returns:
            Dict with ``spans`` list on success, or ``error`` + ``hint`` on failure.
        """
        if not trace_id or not isinstance(trace_id, str):
            return _error_result(0, "trace_id is required.")
        # Sanitize: strip whitespace, enforce lowercase hex only, max 32 chars.
        clean_id = trace_id.strip().lower()
        if not clean_id or len(clean_id) > 64 or not all(c in _HEX_CHARS for c in clean_id):
            return _error_result(0, "Invalid trace_id — must be a lowercase hex string (max 64 chars).")
        return self._get(f"/traces/{clean_id}")

    def list_workflows(self, *, limit: int = 20) -> Dict[str, Any]:
        """
        List workflow run summaries.

        ``GET /v1/workflows?limit=20``

        Args:
            limit: Max workflows to return (clamped to 1–1000).

        Returns:
            Dict with ``workflows`` list on success, or ``error`` + ``hint`` on failure.
        """
        safe_limit = max(1, min(limit, _MAX_LIMIT))
        return self._get("/workflows", params={"limit": safe_limit})

    def get_span(self, span_id: str) -> Dict[str, Any]:
        """
        Fetch a single span detail.

        ``GET /v1/spans/{span_id}``

        Args:
            span_id: The 32-char hex span ID.

        Returns:
            Dict with span fields on success, or ``error`` + ``hint`` on failure.
        """
        if not span_id or not isinstance(span_id, str):
            return _error_result(0, "span_id is required.")
        # Sanitize: strip whitespace, enforce lowercase hex only, max 32 chars.
        clean_id = span_id.strip().lower()
        if not clean_id or len(clean_id) > 64 or not all(c in _HEX_CHARS for c in clean_id):
            return _error_result(0, "Invalid span_id — must be a lowercase hex string (max 64 chars).")
        return self._get(f"/spans/{clean_id}")

    def close(self) -> None:
        """Release the underlying connection pool."""
        try:
            self._session.close()
        except Exception:  # nosec B110
            pass
