# -*- coding: utf-8 -*-
"""
Tests for teracron.local_teracron — the spool consumer ("local Teracron").

Covers:
    - Convex site-URL derivation (explicit > env vars > derived from .cloud)
    - One-shot spool drain: success → archived to processed/
    - Permanent reject (4xx) → quarantined; transient (5xx/429/0) → left for retry
    - Kind → Convex path routing + X-Project-Slug header forwarding
    - fetch_recent_spans() read-back helper (status-code handling)

No real network: ``requests`` is monkeypatched so the tests are hermetic and
fast. The spool layout written here mirrors exactly what local_interface.py
produces (``.bin`` payload + ``.json`` sidecar).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from teracron import local_teracron as lt


# ── Helpers ─────────────────────────────────────────────────────────────────


def _write_spool_item(spool: Path, *, kind: str, slug: str, payload: bytes) -> str:
    """Write a `.bin` + `.json` pair exactly like the local interface does."""
    base = f"100000000000000000-{kind}-{slug}"
    (spool / f"{base}.bin").write_bytes(payload)
    (spool / f"{base}.json").write_text(
        json.dumps(
            {
                "kind": kind,
                "slug": slug,
                "payload_file": f"{base}.bin",
                "byte_length": len(payload),
                "received_at": 0,
            }
        )
    )
    return base


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeSession:
    """Records POSTs and returns a scripted status code per call."""

    def __init__(self, status_code: int = 202) -> None:
        self.status_code = status_code
        self.calls = []
        self.headers = {}

    def post(self, url, data=None, headers=None, timeout=None):  # noqa: D401
        self.calls.append({"url": url, "data": data, "headers": headers})
        return _FakeResponse(self.status_code)

    def close(self):
        pass


@pytest.fixture()
def spool(tmp_path) -> Path:
    d = tmp_path / "local-spool"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _make_consumer(spool: Path, *, status_code: int = 202) -> lt.LocalTeracron:
    """Build a consumer whose HTTP session is a fake returning ``status_code``."""
    c = lt.LocalTeracron(
        convex_site="https://demo.convex.site", spool=spool, quiet=True
    )
    c._session = _FakeSession(status_code)  # type: ignore[assignment]
    return c


# ── Site URL derivation ─────────────────────────────────────────────────────


def test_site_url_explicit_wins(monkeypatch):
    monkeypatch.setenv("CONVEX_SITE_URL", "https://env.convex.site")
    assert (
        lt.derive_convex_site_url(explicit="https://flag.convex.site/")
        == "https://flag.convex.site"
    )


def test_site_url_from_env(monkeypatch):
    monkeypatch.delenv("CONVEX_URL", raising=False)
    monkeypatch.setenv("CONVEX_SITE_URL", "https://env.convex.site")
    assert lt.derive_convex_site_url() == "https://env.convex.site"


def test_site_url_derived_from_cloud(monkeypatch):
    monkeypatch.delenv("CONVEX_SITE_URL", raising=False)
    monkeypatch.delenv("NEXT_PUBLIC_CONVEX_SITE_URL", raising=False)
    monkeypatch.setenv("NEXT_PUBLIC_CONVEX_URL", "https://abc-xyz-1.convex.cloud")
    assert lt.derive_convex_site_url() == "https://abc-xyz-1.convex.site"


def test_site_url_missing_raises(monkeypatch):
    for var in (
        "CONVEX_SITE_URL",
        "NEXT_PUBLIC_CONVEX_SITE_URL",
        "CONVEX_URL",
        "NEXT_PUBLIC_CONVEX_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ValueError):
        lt.derive_convex_site_url()


# ── One-shot drain semantics ────────────────────────────────────────────────


def test_forward_success_archives_to_processed(spool):
    _write_spool_item(spool, kind="traces", slug="fast-shark-747", payload=b"ENC")
    c = _make_consumer(spool, status_code=202)

    counts = c.run_once()

    assert counts == {"forwarded": 1, "quarantined": 0, "retried": 0}
    # Active spool no longer has the pair; processed/ now holds it.
    assert list(spool.glob("*.json")) == []
    assert len(list((spool / "processed").glob("*.bin"))) == 1
    # The encrypted bytes + slug header were forwarded to the traces endpoint.
    call = c._session.calls[0]  # type: ignore[attr-defined]
    assert call["url"].endswith("/v1/traces")
    assert call["headers"]["X-Project-Slug"] == "fast-shark-747"
    assert call["data"] == b"ENC"


def test_permanent_reject_quarantines(spool):
    _write_spool_item(spool, kind="traces", slug="fast-shark-747", payload=b"BAD")
    c = _make_consumer(spool, status_code=401)  # decryption failed → permanent

    counts = c.run_once()

    assert counts == {"forwarded": 0, "quarantined": 1, "retried": 0}
    assert len(list((spool / "quarantine").glob("*.bin"))) == 1
    assert list(spool.glob("*.json")) == []


def test_transient_failure_left_for_retry(spool):
    _write_spool_item(spool, kind="metrics", slug="fast-shark-747", payload=b"X")
    c = _make_consumer(spool, status_code=503)  # transient → retry next pass

    counts = c.run_once()

    assert counts == {"forwarded": 0, "quarantined": 0, "retried": 1}
    # Still in the active spool for a later pass.
    assert len(list(spool.glob("*.json"))) == 1
    assert list((spool / "quarantine").glob("*.bin")) == []


def test_kind_routing_paths(spool):
    _write_spool_item(spool, kind="metrics", slug="fast-shark-747", payload=b"m")
    _write_spool_item(spool, kind="events", slug="fast-shark-747", payload=b"e")
    c = _make_consumer(spool, status_code=202)

    c.run_once()

    urls = sorted(call["url"] for call in c._session.calls)  # type: ignore[attr-defined]
    assert urls == [
        "https://demo.convex.site/ingest",
        "https://demo.convex.site/v1/events",
    ]


def test_incomplete_sidecar_skipped(spool):
    # Sidecar present but its payload .bin missing → skipped (not an error).
    base = "1-traces-fast-shark-747"
    (spool / f"{base}.json").write_text(
        json.dumps(
            {"kind": "traces", "slug": "fast-shark-747", "payload_file": f"{base}.bin"}
        )
    )
    c = _make_consumer(spool, status_code=202)

    counts = c.run_once()

    assert counts == {"forwarded": 0, "quarantined": 0, "retried": 0}
    assert c._session.calls == []  # type: ignore[attr-defined]


# ── Read-back helper ────────────────────────────────────────────────────────


def test_fetch_recent_spans_ok(monkeypatch):
    captured = {}

    def _fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return _FakeJson(200, {"spans": [{"operation": "main"}], "cursor": 42})

    monkeypatch.setattr(lt.requests, "get", _fake_get)

    out = lt.fetch_recent_spans(
        convex_site="https://demo.convex.site/", api_key="tcn_abc", since=10, limit=5
    )

    assert out["cursor"] == 42
    assert out["spans"][0]["operation"] == "main"
    assert captured["url"] == "https://demo.convex.site/v1/logs"
    assert captured["params"]["since"] == 10
    assert captured["headers"]["Authorization"] == "Bearer tcn_abc"


def test_fetch_recent_spans_404(monkeypatch):
    monkeypatch.setattr(
        lt.requests, "get", lambda *a, **k: _FakeJson(404, {})
    )
    out = lt.fetch_recent_spans(convex_site="https://demo.convex.site", api_key="tcn_x")
    assert "error" in out


class _FakeJson:
    def __init__(self, status_code: int, body: dict) -> None:
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body
