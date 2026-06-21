# -*- coding: utf-8 -*-
"""
Tests for ``teracron.cli._resolve_domain``.

This helper centralises domain precedence across every CLI read command
(`projects`, `logs`, `events`, `trace`, `workflows`, `simulate`, `curl-example`).

Precedence (highest → lowest):
    1. ``--domain`` flag
    2. ``TERACRON_DOMAIN`` env var
    3. Saved credentials' ``domain`` field
    4. Hard-coded default ``www.teracron.com``

Without these tests, a future refactor could silently regress to "always
default" — forcing users to repeat ``--domain localhost:3000`` on every call
after a local-mode login. The end-to-end payment-server pipeline relies on
precedence (3), so it MUST be covered.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from teracron import cli


# ── 1. --domain flag wins over everything ────────────────────────────────────


def test_cli_flag_wins_over_env_and_credentials(monkeypatch):
    """`--domain` is explicit user intent and must trump env + saved creds."""
    monkeypatch.setenv("TERACRON_DOMAIN", "env.example.com")
    fake_creds = SimpleNamespace(domain="saved.example.com")
    with mock.patch("teracron.auth.load_credentials", return_value=fake_creds):
        assert cli._resolve_domain("flag.example.com") == "flag.example.com"


# ── 2. env var wins over saved credentials + default ─────────────────────────


def test_env_var_wins_over_credentials(monkeypatch):
    monkeypatch.setenv("TERACRON_DOMAIN", "env.example.com")
    fake_creds = SimpleNamespace(domain="saved.example.com")
    with mock.patch("teracron.auth.load_credentials", return_value=fake_creds):
        assert cli._resolve_domain(None) == "env.example.com"


def test_env_var_wins_over_default(monkeypatch):
    monkeypatch.setenv("TERACRON_DOMAIN", "env.example.com")
    with mock.patch("teracron.auth.load_credentials", return_value=None):
        assert cli._resolve_domain(None) == "env.example.com"


def test_blank_env_var_is_ignored(monkeypatch):
    """An empty/whitespace env var must NOT count as "set"."""
    monkeypatch.setenv("TERACRON_DOMAIN", "   ")
    with mock.patch("teracron.auth.load_credentials", return_value=None):
        assert cli._resolve_domain(None) == cli._DEFAULT_DOMAIN


# ── 3. saved credentials win over hard-coded default ─────────────────────────


def test_credentials_used_when_no_flag_no_env(monkeypatch):
    """
    The end-to-end payment-server flow depends on this branch:
    `teracron-agent login` saves ``domain=localhost:3000`` and subsequent
    `projects` / `logs` calls must inherit it without repeating ``--domain``.
    """
    monkeypatch.delenv("TERACRON_DOMAIN", raising=False)
    fake_creds = SimpleNamespace(domain="localhost:3000")
    with mock.patch("teracron.auth.load_credentials", return_value=fake_creds):
        assert cli._resolve_domain(None) == "localhost:3000"


def test_credentials_without_domain_falls_through(monkeypatch):
    """A creds object missing a ``domain`` attr must NOT crash — fall through."""
    monkeypatch.delenv("TERACRON_DOMAIN", raising=False)
    fake_creds = SimpleNamespace()  # no .domain attribute
    with mock.patch("teracron.auth.load_credentials", return_value=fake_creds):
        assert cli._resolve_domain(None) == cli._DEFAULT_DOMAIN


def test_credentials_with_empty_domain_falls_through(monkeypatch):
    monkeypatch.delenv("TERACRON_DOMAIN", raising=False)
    fake_creds = SimpleNamespace(domain="")
    with mock.patch("teracron.auth.load_credentials", return_value=fake_creds):
        assert cli._resolve_domain(None) == cli._DEFAULT_DOMAIN


# ── 4. hard-coded default fallback ───────────────────────────────────────────


def test_default_used_when_nothing_else(monkeypatch):
    monkeypatch.delenv("TERACRON_DOMAIN", raising=False)
    with mock.patch("teracron.auth.load_credentials", return_value=None):
        assert cli._resolve_domain(None) == cli._DEFAULT_DOMAIN


# ── 5. defensive: load_credentials raising is non-fatal ──────────────────────


def test_load_credentials_exception_is_swallowed(monkeypatch):
    """
    The resolver must NEVER raise — a corrupt credentials file or a missing
    auth-module attribute should silently fall through to the default. This
    guarantees the CLI stays usable even when the local credential store is in
    a bad state.
    """
    monkeypatch.delenv("TERACRON_DOMAIN", raising=False)
    with mock.patch("teracron.auth.load_credentials",
                    side_effect=OSError("disk on fire")):
        assert cli._resolve_domain(None) == cli._DEFAULT_DOMAIN


def test_load_credentials_unexpected_exception_swallowed(monkeypatch):
    """Even a non-OSError must not bubble up — defensive `except Exception`."""
    monkeypatch.delenv("TERACRON_DOMAIN", raising=False)
    with mock.patch("teracron.auth.load_credentials",
                    side_effect=RuntimeError("auth module broke")):
        assert cli._resolve_domain(None) == cli._DEFAULT_DOMAIN


# ── 6. integration: resolver actually wires into a CLI command ───────────────


def test_resolver_used_by_projects_command(monkeypatch, capsys):
    """
    Smoke test: invoking `_cmd_projects` without ``--domain`` must use the
    saved credentials' domain. This is the public-facing behaviour the
    payment-server flow depends on; if this regresses, every read command
    will once again require ``--domain`` after `login`.
    """
    monkeypatch.delenv("TERACRON_DOMAIN", raising=False)

    # 1. Saved creds point at a local dashboard.
    fake_creds = SimpleNamespace(
        api_key="tcn_dummy",
        domain="localhost:3000",
        project_slug="fast-shark-747",
        created_at=0,
        expires_at=None,
    )

    # 2. Capture which domain the QueryClient is built with.
    captured = {}

    class _FakeClient:
        def __init__(self, *, api_key, domain, **kwargs):
            captured["domain"] = domain

        def list_projects(self):
            return {"projects": [{"slug": "fast-shark-747", "name": "test"}],
                    "count": 1}

    with mock.patch("teracron.auth.load_credentials", return_value=fake_creds), \
         mock.patch("teracron.auth.resolve_api_key", return_value="tcn_dummy"), \
         mock.patch("teracron.query.TeracronQueryClient", _FakeClient):
        args = SimpleNamespace(
            api_key=None,
            domain=None,           # ← the key assertion: no flag passed
            json_output=False,
            create_name=None,
        )
        cli._cmd_projects(args)

    assert captured["domain"] == "localhost:3000"
