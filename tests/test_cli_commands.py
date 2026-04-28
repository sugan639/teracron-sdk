# -*- coding: utf-8 -*-
"""
Tests for teracron.cli — subcommand routing, output formats, backward compat.

Covers:
    - Subcommand routing (run, login, logout, whoami, events, workflows, trace, simulate, curl-example)
    - --json output format
    - --help text
    - Backward compatibility (no subcommand = run)
    - Error handling (missing API key)
"""

from __future__ import annotations

import sys
from io import StringIO
from unittest import mock

import pytest

from teracron.cli import _build_parser, main


# ── Parser construction ──


class TestParser:
    def test_parser_creation(self) -> None:
        parser = _build_parser()
        assert parser is not None
        assert parser.prog == "teracron-agent"

    def test_version_flag(self) -> None:
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--version"])
        assert exc_info.value.code == 0

    def test_subcommands_exist(self) -> None:
        parser = _build_parser()
        # Each subcommand should parse without error.
        for cmd in ["run", "login", "logout", "whoami", "events", "workflows", "curl-example"]:
            args = parser.parse_args([cmd])
            assert args.command == cmd

    def test_trace_requires_id(self) -> None:
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["trace"])

    def test_trace_with_id(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["trace", "abc123"])
        assert args.command == "trace"
        assert args.trace_id == "abc123"

    def test_simulate_requires_id(self) -> None:
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["simulate"])

    def test_simulate_with_id_and_format(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["simulate", "abc123", "--format", "json"])
        assert args.command == "simulate"
        assert args.sim_trace_id == "abc123"
        assert args.sim_format == "json"

    def test_no_subcommand_defaults_to_none(self) -> None:
        parser = _build_parser()
        args = parser.parse_args([])
        assert args.command is None  # main() will map this to "run"

    def test_global_api_key_flag(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["--api-key", "tcn_test", "whoami"])
        assert args.api_key == "tcn_test"

    def test_global_json_flag(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["--json", "whoami"])
        assert args.json_output is True

    def test_global_domain_flag(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["--domain", "api.teracron.com", "whoami"])
        assert args.domain == "api.teracron.com"

    def test_events_filters(self) -> None:
        parser = _build_parser()
        args = parser.parse_args([
            "events",
            "--workflow", "payment",
            "--status", "failed",
            "--limit", "10",
            "--since", "2025-01-01T00:00:00Z",
        ])
        assert args.workflow == "payment"
        assert args.status == "failed"
        assert args.limit == 10
        assert args.since == "2025-01-01T00:00:00Z"

    def test_workflows_limit(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["workflows", "--limit", "5"])
        assert args.limit == 5

    def test_login_positional_key(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["login", "tcn_my_key_12345"])
        assert args.login_api_key == "tcn_my_key_12345"

    def test_login_no_key(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["login"])
        assert args.login_api_key is None


# ── Whoami command ──


class TestWhoamiCommand:
    def test_whoami_not_logged_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TERACRON_API_KEY", raising=False)
        stderr = StringIO()
        monkeypatch.setattr("sys.stderr", stderr)

        with mock.patch("teracron.auth.whoami", return_value=None):
            from teracron.cli import _cmd_whoami

            parser = _build_parser()
            args = parser.parse_args(["whoami"])
            _cmd_whoami(args)

        output = stderr.getvalue()
        assert "Not authenticated" in output

    def test_whoami_json_not_logged_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stdout = StringIO()
        monkeypatch.setattr("sys.stdout", stdout)
        monkeypatch.delenv("TERACRON_API_KEY", raising=False)

        with mock.patch("teracron.auth.whoami", return_value=None):
            from teracron.cli import _cmd_whoami

            parser = _build_parser()
            args = parser.parse_args(["--json", "whoami"])
            _cmd_whoami(args)

        import json
        output = json.loads(stdout.getvalue())
        assert output["authenticated"] is False


# ── Curl-example command ──


class TestCurlExampleCommand:
    def test_curl_example_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stdout = StringIO()
        monkeypatch.setattr("sys.stdout", stdout)
        monkeypatch.delenv("TERACRON_API_KEY", raising=False)

        with mock.patch("teracron.auth.resolve_api_key", return_value=None):
            with mock.patch("teracron.auth.mask_api_key", return_value="tcn_****"):
                from teracron.cli import _cmd_curl_example

                parser = _build_parser()
                args = parser.parse_args(["curl-example"])
                _cmd_curl_example(args)

        output = stdout.getvalue()
        assert "curl" in output
        assert "/api/v1/events" in output
        assert "/api/v1/traces" in output
        assert "Authorization: Bearer" in output


# ── Logout command ──


class TestLogoutCommand:
    def test_logout_with_no_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stderr = StringIO()
        monkeypatch.setattr("sys.stderr", stderr)

        with mock.patch("teracron.auth.logout", return_value=False):
            from teracron.cli import _cmd_logout

            parser = _build_parser()
            args = parser.parse_args(["logout"])
            _cmd_logout(args)

        output = stderr.getvalue()
        assert "No stored credentials" in output

    def test_logout_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stdout = StringIO()
        monkeypatch.setattr("sys.stdout", stdout)

        with mock.patch("teracron.auth.logout", return_value=True):
            from teracron.cli import _cmd_logout

            parser = _build_parser()
            args = parser.parse_args(["--json", "logout"])
            _cmd_logout(args)

        import json
        output = json.loads(stdout.getvalue())
        assert output["status"] == "logged_out"
        assert output["deleted"] is True


# ── Events command (error path) ──


class TestEventsCommandErrors:
    def test_events_no_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TERACRON_API_KEY", raising=False)
        stderr = StringIO()
        monkeypatch.setattr("sys.stderr", stderr)

        with mock.patch("teracron.auth.resolve_api_key", return_value=None):
            from teracron.cli import _cmd_events

            parser = _build_parser()
            args = parser.parse_args(["events"])

            with pytest.raises(SystemExit) as exc_info:
                _cmd_events(args)

            assert exc_info.value.code == 1

        output = stderr.getvalue()
        assert "No API key found" in output
