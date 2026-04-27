# -*- coding: utf-8 -*-
"""
Failure simulation engine — replay a failed trace for local diagnosis.

Fetches a failed trace from the Teracron query API, extracts reproducible
context (workflow, operations, captured params, error chain), and generates:
    1. A structured failure context (JSON).
    2. A markdown diagnosis for AI agents.
    3. A standalone Python repro script.

SECURITY:
    - The simulator NEVER executes arbitrary code from the server.
    - It only reconstructs the call context from captured params and span metadata.
    - Generated repro scripts are inert text — the AI agent decides what to run.
    - No secrets, tokens, or PII are embedded in generated output.
"""

from __future__ import annotations

import json
import re
import textwrap
import time
from typing import Any, Dict, List, Optional

_MAX_ERROR_DISPLAY_LEN = 512

# Only allow safe Python identifier characters in generated code symbols.
_SAFE_IDENTIFIER_RE = re.compile(r"[^a-zA-Z0-9_]")
_MAX_IDENTIFIER_LEN = 128


def _sanitise_identifier(raw: str, fallback: str = "unknown") -> str:
    """
    Sanitise a string for safe use as a Python identifier in generated code.

    Replaces all non-alphanumeric/underscore chars with ``_``, truncates,
    and ensures it doesn't start with a digit.  Prevents code injection
    in generated repro scripts.
    """
    if not raw or not isinstance(raw, str):
        return fallback
    cleaned = _SAFE_IDENTIFIER_RE.sub("_", raw)[:_MAX_IDENTIFIER_LEN]
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"_{cleaned}"
    return cleaned or fallback


def _sanitise_for_comment(raw: str, max_len: int = 256) -> str:
    """Sanitise a string for safe embedding in a Python comment (strip newlines)."""
    if not raw:
        return ""
    return raw.replace("\n", " ").replace("\r", "")[:max_len]


def _error_ctx(error: str, hint: str = "") -> Dict[str, Any]:
    """Build a structured error context dict."""
    result: Dict[str, Any] = {"error": error}
    if hint:
        result["hint"] = hint
    return result


class FailureSimulator:
    """
    Replay a failed trace locally to reproduce the issue.

    Uses ``TeracronQueryClient`` to fetch trace data, then produces
    diagnosis artifacts without executing any code.

    Args:
        query_client: An authenticated ``TeracronQueryClient`` instance.
    """

    __slots__ = ("_client",)

    def __init__(self, query_client: Any) -> None:
        self._client = query_client

    def fetch_failure_context(self, trace_id: str) -> Dict[str, Any]:
        """
        Fetch the failed trace and extract reproducible context.

        Returns a dict with:
            - ``trace_id``, ``workflow``, ``failed_operation``
            - ``error_type``, ``error_message``
            - ``captured_params`` (if available)
            - ``span_chain`` (ordered list of operations)
            - ``spans`` (full span list)

        On error, returns ``{"error": "...", "hint": "..."}``.
        """
        if not trace_id or not isinstance(trace_id, str):
            return _error_ctx("trace_id is required.")

        result = self._client.get_trace(trace_id.strip())

        if result.get("error"):
            return result

        spans = result.get("spans", [])
        if not spans:
            return _error_ctx(
                "No spans found in this trace.",
                hint="The trace may have been sampled out or not yet ingested.",
            )

        # Find the failed span(s).
        failed_spans = [s for s in spans if s.get("status") == "failed"]

        if not failed_spans:
            return _error_ctx(
                "No failed spans in this trace — all spans succeeded.",
                hint="This trace does not contain a failure to simulate.",
            )

        # Use the deepest (last) failed span as the primary failure.
        primary_failure = failed_spans[-1]

        # Build ordered span chain (by started_at).
        sorted_spans = sorted(spans, key=lambda s: s.get("started_at", 0))
        span_chain = [s.get("operation", "?") for s in sorted_spans]

        # Extract root span workflow.
        root_span = sorted_spans[0] if sorted_spans else {}
        workflow = primary_failure.get("workflow", root_span.get("workflow", "unknown"))

        error_message = primary_failure.get("error_message", "")
        if error_message and len(error_message) > _MAX_ERROR_DISPLAY_LEN:
            error_message = error_message[:_MAX_ERROR_DISPLAY_LEN]

        return {
            "trace_id": trace_id.strip(),
            "workflow": workflow,
            "failed_operation": primary_failure.get("operation", "unknown"),
            "error_type": primary_failure.get("error_type"),
            "error_message": error_message,
            "captured_params": primary_failure.get("captured_params"),
            "span_chain": span_chain,
            "spans": sorted_spans,
            "failed_span_id": primary_failure.get("span_id"),
            "failure_count": len(failed_spans),
        }

    def generate_repro_script(self, ctx: Dict[str, Any]) -> str:
        """
        Generate a standalone Python script that simulates the failure scenario.

        The script is inert — it does NOT execute the actual function.
        It reconstructs the call chain and expected error for manual review.

        SECURITY: All identifiers from server data are sanitised before
        embedding in generated Python code to prevent code injection.

        Args:
            ctx: Failure context from ``fetch_failure_context()``.

        Returns:
            A Python script as a string.
        """
        if ctx.get("error"):
            return f"# Error: {_sanitise_for_comment(ctx['error'])}"

        workflow = _sanitise_for_comment(ctx.get("workflow", "unknown"))
        failed_op = ctx.get("failed_operation", "unknown")
        error_type = ctx.get("error_type", "Exception")
        error_message = ctx.get("error_message", "Unknown error")
        captured_params = ctx.get("captured_params", {})
        span_chain = ctx.get("span_chain", [])

        # Sanitise identifiers used in executable positions.
        safe_func_name = _sanitise_identifier(failed_op, "unknown_op")
        safe_error_type = _sanitise_identifier(error_type, "Exception")
        safe_failed_op_comment = _sanitise_for_comment(failed_op)
        safe_error_msg_comment = _sanitise_for_comment(error_message)
        safe_trace_id = _sanitise_for_comment(ctx.get("trace_id", "?"), max_len=64)

        # Build parameter assignments — keys are sanitised, values are JSON-encoded.
        param_lines = ""
        if captured_params and isinstance(captured_params, dict):
            for k, v in captured_params.items():
                safe_key = _sanitise_identifier(str(k), "param")
                param_lines += f"    {safe_key} = {json.dumps(v)}\n"

        # Build span chain as comments.
        chain_comments = "\n".join(
            f"#   {i + 1}. {_sanitise_for_comment(str(op))}" for i, op in enumerate(span_chain)
        )

        # Safe param names for the commented-out call.
        safe_param_names = ", ".join(
            f"{_sanitise_identifier(str(k))}={_sanitise_identifier(str(k))}"
            for k in (captured_params or {})
        )

        # Escape strings for safe embedding in Python string literals
        # (no unescaped quotes, no newlines, no backslash tricks).
        safe_failed_op_literal = safe_failed_op_comment.replace("\\", "\\\\").replace('"', '\\"')
        safe_error_type_literal = _sanitise_for_comment(error_type).replace("\\", "\\\\").replace('"', '\\"')
        safe_error_msg_literal = safe_error_msg_comment.replace("\\", "\\\\").replace('"', '\\"')

        script = textwrap.dedent(f"""\
            #!/usr/bin/env python3
            # ──────────────────────────────────────────────────
            # Teracron Failure Reproduction Script
            # Generated at: {time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            #
            # Trace ID: {safe_trace_id}
            # Workflow: {workflow}
            # Failed:   {safe_failed_op_comment}
            # Error:    {_sanitise_for_comment(error_type)}: {safe_error_msg_comment}
            # ──────────────────────────────────────────────────
            #
            # Execution chain:
            {chain_comments}
            #
            # This script reconstructs the failure context.
            # It does NOT execute the original function — modify as needed.
            # ──────────────────────────────────────────────────

            import sys


            def simulate_{safe_func_name}():
                \\"\\"\\"Simulate the failure scenario for: {safe_failed_op_literal}\\"\\"\\"

                # Captured parameters at time of failure:
            {param_lines if param_lines else "    # (no parameters were captured)"}

                # The original call raised:
                #   {_sanitise_for_comment(error_type)}: {safe_error_msg_comment}

                # TODO: Replace this with the actual function call to reproduce:
                # result = {safe_func_name}({safe_param_names})

                print("Simulating failure in: {safe_failed_op_literal}")
                print("Expected error: {safe_error_type_literal}: {safe_error_msg_literal}")

                # Uncomment to simulate the exception:
                # raise {safe_error_type}("{safe_error_msg_literal}")


            if __name__ == "__main__":
                try:
                    simulate_{safe_func_name}()
                    print("\\n[OK] Simulation completed without error.")
                except Exception as exc:
                    print(f"\\n[FAIL] {{type(exc).__name__}}: {{exc}}", file=sys.stderr)
                    sys.exit(1)
        """)

        return script

    def print_diagnosis(self, ctx: Dict[str, Any]) -> str:
        """
        Generate a markdown-formatted diagnosis summary.

        Designed for AI agent consumption — structured, parseable, actionable.

        Args:
            ctx: Failure context from ``fetch_failure_context()``.

        Returns:
            Markdown-formatted diagnosis string.
        """
        if ctx.get("error"):
            return f"## Error\n\n{ctx['error']}"

        workflow = ctx.get("workflow", "unknown")
        failed_op = ctx.get("failed_operation", "unknown")
        error_type = ctx.get("error_type", "Unknown")
        error_message = ctx.get("error_message", "No error message")
        captured_params = ctx.get("captured_params")
        span_chain = ctx.get("span_chain", [])
        failure_count = ctx.get("failure_count", 1)

        # Build span chain.
        chain_md = "\n".join(
            f"   {i + 1}. `{op}`" for i, op in enumerate(span_chain)
        )

        # Build params section.
        params_md = "_No parameters were captured._"
        if captured_params:
            params_md = "\n".join(
                f"   - `{k}` = `{json.dumps(v)}`" for k, v in captured_params.items()
            )

        diagnosis = textwrap.dedent(f"""\
            ## Teracron Failure Diagnosis

            | Field | Value |
            |---|---|
            | **Trace ID** | `{ctx.get("trace_id", "?")}` |
            | **Workflow** | `{workflow}` |
            | **Failed Operation** | `{failed_op}` |
            | **Error Type** | `{error_type}` |
            | **Failed Spans** | {failure_count} |

            ### Error Message

            ```
            {error_message}
            ```

            ### Execution Chain

            {chain_md}

            ### Captured Parameters

            {params_md}

            ### Suggested Investigation

            1. Check the `{failed_op}` function for `{error_type}` scenarios.
            2. Verify the input parameters match expected types/ranges.
            3. Look for external dependency failures (DB, API, network).
            4. If captured params exist, run `teracron-agent simulate {ctx.get("trace_id", "?")} --format=script` to generate a repro script.
            5. Check logs around timestamp for correlated errors.
        """)

        return diagnosis
