# -*- coding: utf-8 -*-
"""
Tests for teracron.local_interface — the loopback stand-in for teracron.com.

Covers:
    - Route table parity with production limits
    - Slug / content-type / size validation (mirrors the Next.js routes)
    - Spool write contract (.bin payload + .json sidecar)
    - Loopback bind guard (refuses non-loopback host by default)

The HTTP handler is exercised end-to-end against a real ThreadingHTTPServer on
an ephemeral loopback port so the validation path is covered exactly as the SDK
would hit it — but without any third-party HTTP client (stdlib urllib only).
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from teracron import local_interface as li


# ── Spool fixture ──


@pytest.fixture()
def spool(tmp_path, monkeypatch) -> Path:
    """Point the interface's spool dir at a temp directory."""
    spool_dir = tmp_path / "local-spool"

    def _fake_spool_dir() -> Path:
        spool_dir.mkdir(parents=True, exist_ok=True)
        return spool_dir

    monkeypatch.setattr(li, "_spool_dir", _fake_spool_dir)
    return spool_dir


# ── Live server fixture ──


@pytest.fixture()
def server(spool):
    """Start the interface on an ephemeral loopback port; yield its base URL."""
    li._IngestHandler.spool = li._spool_dir()
    li._IngestHandler.quiet = True
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), li._IngestHandler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _post(url: str, data: bytes, headers: dict):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    return urllib.request.urlopen(req, timeout=5)


# ── Route table ──


class TestRouteTable:
    def test_routes_match_production_limits(self) -> None:
        assert li._ROUTES["/api/ingest"].max_bytes == 65_536
        assert li._ROUTES["/api/v1/traces"].max_bytes == 131_072
        assert li._ROUTES["/api/v1/events"].max_bytes == 65_536

    def test_route_kinds(self) -> None:
        assert li._ROUTES["/api/ingest"].kind == "metrics"
        assert li._ROUTES["/api/v1/traces"].kind == "traces"
        assert li._ROUTES["/api/v1/events"].kind == "events"


# ── Validation + spool ──


class TestIngestEndToEnd:
    _SLUG = "vivid-kudu-655"
    _OK_HEADERS = {
        "Content-Type": "application/octet-stream",
        "X-Project-Slug": _SLUG,
    }

    def test_accepts_and_spools(self, server, spool) -> None:
        payload = b"encrypted-envelope-bytes"
        resp = _post(f"{server}/api/v1/traces", payload, self._OK_HEADERS)
        assert resp.status == 202
        body = json.loads(resp.read())
        assert body["status"] == "accepted"
        assert body["kind"] == "traces"

        # Spool contract: one .bin + one .json sidecar.
        bins = list(spool.glob("*.bin"))
        sidecars = list(spool.glob("*.json"))
        assert len(bins) == 1 and len(sidecars) == 1
        assert bins[0].read_bytes() == payload

        meta = json.loads(sidecars[0].read_text())
        assert meta["kind"] == "traces"
        assert meta["slug"] == self._SLUG
        assert meta["byte_length"] == len(payload)
        assert meta["payload_file"] == bins[0].name

    def test_rejects_bad_slug(self, server) -> None:
        headers = dict(self._OK_HEADERS, **{"X-Project-Slug": "BAD"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(f"{server}/api/v1/traces", b"x", headers)
        assert exc.value.code == 400

    def test_rejects_wrong_content_type(self, server) -> None:
        headers = dict(self._OK_HEADERS, **{"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(f"{server}/api/v1/traces", b"x", headers)
        assert exc.value.code == 415

    def test_rejects_oversize(self, server) -> None:
        big = b"x" * (li._ROUTES["/api/v1/events"].max_bytes + 1)
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(f"{server}/api/v1/events", big, self._OK_HEADERS)
        assert exc.value.code == 413

    def test_unknown_path_404(self, server) -> None:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(f"{server}/api/v1/nope", b"x", self._OK_HEADERS)
        assert exc.value.code == 404


# ── Bind guard ──


class TestBindGuard:
    def test_non_loopback_refused(self) -> None:
        with pytest.raises(ValueError, match="non-loopback"):
            li.serve(host="0.0.0.0", port=0)
