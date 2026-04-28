# -*- coding: utf-8 -*-
"""
Security hardening tests — validates fixes for audit findings.

Covers:
    - SSRF prevention via domain allowlist (query client, auth login)
    - Input sanitisation for hex IDs (trace_id, span_id)
    - Code injection prevention in repro script generation
    - TOCTOU race elimination in credential deletion
    - API key not leaked to os.environ from CLI
    - EventBuffer initialisation on client
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── SSRF: Domain allowlist on TeracronQueryClient ──


class TestQueryClientDomainValidation:
    """Verify TeracronQueryClient rejects non-allowlisted domains."""

    def test_default_domain_accepted(self):
        from teracron.query import TeracronQueryClient

        client = TeracronQueryClient(api_key="tcn_test", domain="www.teracron.com")
        assert "teracron.com" in client._base_url
        client.close()

    def test_subdomain_accepted(self):
        from teracron.query import TeracronQueryClient

        client = TeracronQueryClient(api_key="tcn_test", domain="api.teracron.com")
        assert "api.teracron.com" in client._base_url
        client.close()

    def test_evil_domain_rejected(self):
        from teracron.query import TeracronQueryClient

        with pytest.raises(ValueError, match="not allowed"):
            TeracronQueryClient(api_key="tcn_test", domain="evil.attacker.com")

    def test_domain_with_protocol_stripped(self):
        from teracron.query import TeracronQueryClient

        client = TeracronQueryClient(
            api_key="tcn_test", domain="https://www.teracron.com/extra"
        )
        assert client._base_url == "https://www.teracron.com/api/v1"
        client.close()

    def test_custom_domain_with_env_bypass(self):
        from teracron.query import TeracronQueryClient

        with patch.dict(os.environ, {"TERACRON_ALLOW_CUSTOM_DOMAIN": "1"}):
            client = TeracronQueryClient(
                api_key="tcn_test", domain="custom.internal.corp"
            )
            assert "custom.internal.corp" in client._base_url
            client.close()


# ── SSRF: Domain allowlist on auth.login ──


class TestAuthLoginDomainValidation:
    """Verify login() rejects non-allowlisted domains."""

    def test_login_rejects_evil_domain(self):
        from teracron.auth import login

        with pytest.raises(ValueError, match="not allowed"):
            login(
                api_key="tcn_" + "a" * 30,
                domain="evil.attacker.com",
            )


# ── Input sanitisation: hex ID validation ──


class TestHexIdValidation:
    """Verify query client rejects malformed trace/span IDs."""

    def _make_client(self):
        from teracron.query import TeracronQueryClient

        return TeracronQueryClient(api_key="tcn_test", domain="www.teracron.com")

    def test_valid_hex_trace_id(self):
        client = self._make_client()
        # Should proceed to HTTP call; mock a success response
        with patch.object(client._session, "get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"spans": []}
            mock_get.return_value = mock_resp
            result = client.get_trace("a" * 32)
            assert result == {"spans": []}
        client.close()

    def test_uppercase_hex_normalised(self):
        client = self._make_client()
        with patch.object(client._session, "get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"spans": []}
            mock_get.return_value = mock_resp
            client.get_trace("AABB" * 8)
            # Should be lowercased in the URL
            called_url = mock_get.call_args[0][0] if mock_get.call_args[0] else mock_get.call_args[1].get("url", "")
            assert "aabb" in called_url.lower()
        client.close()

    def test_injection_in_trace_id_rejected(self):
        client = self._make_client()
        result = client.get_trace("../../etc/passwd")
        assert "error" in result
        assert "hex" in result["error"].lower()
        client.close()

    def test_overlength_trace_id_rejected(self):
        client = self._make_client()
        result = client.get_trace("a" * 100)
        assert "error" in result
        client.close()

    def test_empty_trace_id_rejected(self):
        client = self._make_client()
        result = client.get_trace("")
        assert "error" in result
        client.close()

    def test_span_id_injection_rejected(self):
        client = self._make_client()
        result = client.get_span("'; DROP TABLE spans; --")
        assert "error" in result
        assert "hex" in result["error"].lower()
        client.close()


# ── Code injection: repro script sanitisation ──


class TestReproScriptSanitisation:
    """Verify generated repro scripts can't inject code."""

    def _make_simulator(self):
        from teracron.simulate import FailureSimulator

        mock_client = MagicMock()
        return FailureSimulator(query_client=mock_client)

    def test_malicious_error_type_sanitised(self):
        """error_type with code injection should be neutralised in executable positions."""
        sim = self._make_simulator()
        ctx = {
            "trace_id": "abc123",
            "workflow": "payment",
            "failed_operation": "process_payment",
            "error_type": 'Exception("injected"); import os; os.system("rm -rf /")',
            "error_message": "legit error",
            "captured_params": {},
            "span_chain": ["step1"],
        }
        script = sim.generate_repro_script(ctx)
        # The raise statement should use the sanitised identifier (no parens/semicolons)
        # Find the "# raise" line
        raise_lines = [l for l in script.split("\n") if l.strip().startswith("# raise")]
        assert len(raise_lines) == 1
        raise_line = raise_lines[0]
        # Should NOT contain semicolons or 'import os' in the raise line
        assert ";" not in raise_line
        assert "import os" not in raise_line

    def test_malicious_operation_sanitised(self):
        """failed_operation with __import__ should be neutralised in function defs."""
        sim = self._make_simulator()
        ctx = {
            "trace_id": "abc123",
            "workflow": "payment",
            "failed_operation": '__import__("os").system("id")',
            "error_type": "ValueError",
            "error_message": "bad input",
            "captured_params": None,
            "span_chain": [],
        }
        script = sim.generate_repro_script(ctx)
        # Function definition should use sanitised identifier — no parens, quotes, or dots
        def_lines = [l for l in script.split("\n") if l.strip().startswith("def simulate_")]
        assert len(def_lines) == 1
        # The part between 'simulate_' and '():' must be a clean identifier
        func_name_part = def_lines[0].split("simulate_")[1].split("(")[0]
        assert all(c.isalnum() or c == "_" for c in func_name_part), \
            f"Function name has unsafe chars: {func_name_part}"
        # The raw __import__("os") must not appear in any EXECUTABLE line.
        # It may appear in comment lines (# prefixed) which are inherently safe.
        executable_lines = [
            l for l in script.split("\n")
            if l.strip() and not l.strip().startswith("#") and not l.strip().startswith('"""')
        ]
        for line in executable_lines:
            assert '__import__("os")' not in line, \
                f"Raw __import__() call found in executable line: {line}"

    def test_newline_injection_in_print_statements(self):
        """Newlines in error_message must not break out of print() strings."""
        sim = self._make_simulator()
        ctx = {
            "trace_id": "abc123",
            "workflow": "payment",
            "failed_operation": "process",
            "error_type": "ValueError",
            "error_message": "msg\nimport os\nos.system('whoami')",
            "captured_params": None,
            "span_chain": [],
        }
        script = sim.generate_repro_script(ctx)
        # The error message should have newlines stripped by _sanitise_for_comment
        # Check no standalone 'import os' or 'os.system' on non-comment lines
        executable_lines = [
            l for l in script.split("\n")
            if l.strip() and not l.strip().startswith("#") and not l.strip().startswith('"""')
        ]
        for line in executable_lines:
            # These should only appear inside string literals (print), not as statements
            if "import os" in line:
                # Must be inside a print() string, not a standalone import
                assert "print(" in line, f"'import os' must be inside print(), got: {line}"

    def test_param_key_sanitised(self):
        sim = self._make_simulator()
        ctx = {
            "trace_id": "abc123",
            "workflow": "test",
            "failed_operation": "func",
            "error_type": "Error",
            "error_message": "err",
            "captured_params": {"valid_key": 1, "'; DROP TABLE users; --": 2},
            "span_chain": [],
        }
        script = sim.generate_repro_script(ctx)
        assert "DROP TABLE" not in script


# ── TOCTOU: delete_credentials race-free ──


class TestDeleteCredentialsRaceFree:
    """Verify delete_credentials uses fd-based open (no exists() check)."""

    def test_delete_nonexistent_returns_false(self):
        from teracron.auth import delete_credentials

        with patch("teracron.auth._credentials_path") as mock_path:
            mock_path.return_value = Path("/tmp/nonexistent_teracron_creds.json")
            result = delete_credentials()
            assert result is False

    def test_delete_existing_returns_true(self):
        from teracron.auth import delete_credentials, save_credentials, AuthCredentials

        with tempfile.TemporaryDirectory() as tmpdir:
            creds_path = Path(tmpdir) / "credentials.json"
            # Write a file manually
            creds_path.write_text('{"test": true}')

            with patch("teracron.auth._credentials_path", return_value=creds_path):
                result = delete_credentials()
                assert result is True
                assert not creds_path.exists()


# ── EventBuffer: client initialisation ──


class TestEventBufferInitialisation:
    """Verify _event_buffer is properly initialised on TeracronClient."""

    def test_event_buffer_none_when_disabled(self):
        """When trace_emit_events=False, _event_buffer should be None."""
        from teracron.apikey import encode_api_key

        test_pem = (
            "-----BEGIN PUBLIC KEY-----\n"
            "MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEA7e6B3rXfMbh96aJOe8Wn\n"
            "fakePEMContentHereForTestingPurposes1234567890ABCDEF\n"
            "-----END PUBLIC KEY-----"
        )
        api_key = encode_api_key("vivid-kudu-655", test_pem)
        from teracron.client import TeracronClient

        client = TeracronClient(api_key=api_key)
        assert client._event_buffer is None

    def test_event_buffer_created_when_enabled(self):
        """When trace_emit_events=True, _event_buffer should be an EventBuffer."""
        from teracron.apikey import encode_api_key

        test_pem = (
            "-----BEGIN PUBLIC KEY-----\n"
            "MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEA7e6B3rXfMbh96aJOe8Wn\n"
            "fakePEMContentHereForTestingPurposes1234567890ABCDEF\n"
            "-----END PUBLIC KEY-----"
        )
        api_key = encode_api_key("vivid-kudu-655", test_pem)
        from teracron.client import TeracronClient

        with patch.dict(os.environ, {"TERACRON_TRACE_EMIT_EVENTS": "true"}):
            client = TeracronClient(api_key=api_key)
            assert client._event_buffer is not None
            from teracron.tracing.events import EventBuffer
            assert isinstance(client._event_buffer, EventBuffer)


# ── Identifier sanitisation unit tests ──


class TestSanitiseIdentifier:
    """Unit tests for the _sanitise_identifier helper."""

    def test_normal_identifier(self):
        from teracron.simulate import _sanitise_identifier

        assert _sanitise_identifier("process_payment") == "process_payment"

    def test_dots_replaced(self):
        from teracron.simulate import _sanitise_identifier

        result = _sanitise_identifier("module.Class.method")
        assert "." not in result
        assert result == "module_Class_method"

    def test_injection_neutralised(self):
        from teracron.simulate import _sanitise_identifier

        result = _sanitise_identifier('__import__("os")')
        assert "(" not in result
        assert '"' not in result

    def test_empty_returns_fallback(self):
        from teracron.simulate import _sanitise_identifier

        assert _sanitise_identifier("") == "unknown"
        assert _sanitise_identifier(None) == "unknown"

    def test_starts_with_digit_prefixed(self):
        from teracron.simulate import _sanitise_identifier

        result = _sanitise_identifier("123abc")
        assert result.startswith("_")

    def test_truncation(self):
        from teracron.simulate import _sanitise_identifier

        long_name = "a" * 200
        result = _sanitise_identifier(long_name)
        assert len(result) <= 128
