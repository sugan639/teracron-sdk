# -*- coding: utf-8 -*-
"""
CLI entry point for the Teracron standalone agent.

Installed as ``teracron-agent`` via the package's ``[project.scripts]``.

Subcommands::

    teracron-agent                    # Default: run the metrics agent
    teracron-agent run                # Explicit: run the metrics agent
    teracron-agent login              # Store API key credentials
    teracron-agent logout             # Wipe stored credentials
    teracron-agent whoami             # Show current auth status
    teracron-agent events             # Query workflow events
    teracron-agent workflows          # List workflow runs
    teracron-agent trace <trace_id>   # Fetch a full trace span tree
    teracron-agent simulate <id>      # Replay a failed trace locally
    teracron-agent curl-example       # Print ready-to-use curl commands

Environment variables:
    TERACRON_API_KEY        — API key from the Teracron dashboard
    TERACRON_INTERVAL       — collection interval in seconds (default: 10)
    TERACRON_TIMEOUT        — HTTP timeout in seconds (default: 10)
    TERACRON_MAX_BUFFER     — max buffered snapshots before flush (default: 10)
    TERACRON_FLUSH_DEADLINE — max seconds before forcing a flush (default: 60)
    TERACRON_DOMAIN         — ingest domain (default: www.teracron.com)
    TERACRON_TARGET_PID     — PID of the target process to monitor
    TERACRON_DEBUG          — "true" or "1" to enable debug logging
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from typing import List, Optional


_BANNER = r"""
  ╔════════════════════════════════════════╗
  ║       Teracron Agent  v{version:<14s} ║
  ║       Python Memory Metrics Agent      ║
  ╚════════════════════════════════════════╝
"""

_DEFAULT_DOMAIN = "www.teracron.com"


def _write_err(msg: str) -> None:
    """Write to stderr without raising."""
    sys.stderr.write(msg)
    sys.stderr.flush()


def _write_out(msg: str) -> None:
    """Write to stdout without raising."""
    sys.stdout.write(msg)
    sys.stdout.flush()


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse CLI with all subcommands."""
    from . import __version__

    parser = argparse.ArgumentParser(
        prog="teracron-agent",
        description="Teracron SDK agent — metrics collection, tracing, and workflow diagnostics.",
    )
    parser.add_argument(
        "--version", action="version", version=f"teracron-agent {__version__}"
    )
    parser.add_argument(
        "--api-key",
        dest="api_key",
        default=None,
        help="API key (overrides env var and stored credentials).",
    )
    parser.add_argument(
        "--domain",
        default=None,
        help=f"Teracron domain (default: {_DEFAULT_DOMAIN}).",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=False,
        help="Output in JSON format (machine-readable).",
    )

    sub = parser.add_subparsers(dest="command")

    # ── run (default) ──
    sub.add_parser("run", help="Run the background metrics agent (default).")

    # ── login ──
    login_p = sub.add_parser("login", help="Authenticate and store API key.")
    login_p.add_argument(
        "login_api_key",
        nargs="?",
        default=None,
        help="API key to store (or use --api-key flag).",
    )

    # ── logout ──
    sub.add_parser("logout", help="Wipe stored credentials.")

    # ── whoami ──
    sub.add_parser("whoami", help="Show current authentication status.")

    # ── events ──
    events_p = sub.add_parser("events", help="Query recent workflow events.")
    events_p.add_argument("--workflow", default=None, help="Filter by workflow name.")
    events_p.add_argument(
        "--status",
        default=None,
        choices=["succeeded", "failed", "in_progress"],
        help="Filter by event status.",
    )
    events_p.add_argument(
        "--limit", type=int, default=50, help="Max events to return (default: 50)."
    )
    events_p.add_argument(
        "--since", default=None, help="ISO 8601 timestamp — events after this time."
    )

    # ── workflows ──
    wf_p = sub.add_parser("workflows", help="List workflow run summaries.")
    wf_p.add_argument(
        "--limit", type=int, default=20, help="Max workflows to return (default: 20)."
    )

    # ── trace ──
    trace_p = sub.add_parser("trace", help="Fetch a full trace span tree.")
    trace_p.add_argument("trace_id", help="The trace ID to inspect.")

    # ── simulate ──
    sim_p = sub.add_parser(
        "simulate", help="Replay a failed trace for local diagnosis."
    )
    sim_p.add_argument("sim_trace_id", help="The trace ID of the failed run.")
    sim_p.add_argument(
        "--format",
        dest="sim_format",
        choices=["json", "markdown", "script"],
        default="markdown",
        help="Output format (default: markdown).",
    )

    # ── curl-example ──
    sub.add_parser("curl-example", help="Print curl command examples for AI agents.")

    return parser


# ── Subcommand handlers ──


def _cmd_run(args: argparse.Namespace) -> None:
    """Run the background metrics agent (original behavior)."""
    from . import __version__
    from .client import up, down

    _write_err(_BANNER.format(version=__version__))

    api_key = args.api_key or os.environ.get("TERACRON_API_KEY", "").strip()
    if not api_key:
        # Fallback to stored credentials.
        from .auth import resolve_api_key

        api_key = resolve_api_key()

    if not api_key:
        _write_err(
            "[teracron] ERROR: No API key found.\n"
            "[teracron]        Set TERACRON_API_KEY, use --api-key, or run: teracron-agent login\n"
        )
        sys.exit(1)

    # Pass API key directly to the client — avoid polluting os.environ
    # which leaks secrets to child processes.

    try:
        client = up(api_key=api_key)
    except ValueError as exc:
        _write_err(f"[teracron] Configuration error: {exc}\n")
        sys.exit(1)
    except Exception as exc:
        _write_err(f"[teracron] Failed to start: {exc}\n")
        sys.exit(1)

    target_pid = os.environ.get("TERACRON_TARGET_PID", "self")
    resolved_slug = client.config.project_slug
    _write_err(
        f"[teracron] Monitoring PID={target_pid}  slug={resolved_slug}\n"
        "[teracron] Press Ctrl+C to stop.\n"
    )

    shutdown_event = threading.Event()

    def _handle_signal(signum: int, _frame: object) -> None:
        sig_name = signal.Signals(signum).name
        _write_err(f"\n[teracron] Received {sig_name} — shutting down...\n")
        shutdown_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    shutdown_event.wait()
    down()
    _write_err("[teracron] Agent stopped. Goodbye.\n")


def _cmd_login(args: argparse.Namespace) -> None:
    """Authenticate and store API key."""
    from .auth import login, mask_api_key, validate_key_format

    key = args.login_api_key or args.api_key or ""
    domain = args.domain or _DEFAULT_DOMAIN

    # If no key provided via flags, prompt interactively.
    if not key:
        env_key = os.environ.get("TERACRON_API_KEY", "").strip()
        if env_key:
            key = env_key
        else:
            try:
                _write_err("[teracron] Enter your API key (from the Teracron dashboard):\n")
                key = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                _write_err("\n[teracron] Login cancelled.\n")
                sys.exit(1)

    if not validate_key_format(key):
        _write_err(
            "[teracron] ERROR: Invalid API key format.\n"
            "[teracron]        Expected: tcn_<base64 payload> (minimum 24 characters).\n"
        )
        sys.exit(1)

    try:
        creds = login(key, domain=domain)
    except ValueError as exc:
        _write_err(f"[teracron] Login failed: {exc}\n")
        sys.exit(1)

    masked = mask_api_key(creds.api_key)

    if args.json_output:
        _write_out(
            json.dumps(
                {
                    "status": "authenticated",
                    "project_slug": creds.project_slug,
                    "domain": creds.domain,
                    "api_key_masked": masked,
                },
                indent=2,
            )
            + "\n"
        )
    else:
        _write_err(
            f"[teracron] ✓ Authenticated as project: {creds.project_slug}\n"
            f"[teracron]   Domain:  {creds.domain}\n"
            f"[teracron]   API Key: {masked}\n"
            f"[teracron]   Credentials saved to ~/.teracron/credentials.json\n"
        )


def _cmd_logout(args: argparse.Namespace) -> None:
    """Wipe stored credentials."""
    from .auth import logout

    deleted = logout()

    if args.json_output:
        _write_out(json.dumps({"status": "logged_out", "deleted": deleted}) + "\n")
    elif deleted:
        _write_err("[teracron] ✓ Credentials wiped.\n")
    else:
        _write_err("[teracron] No stored credentials found.\n")


def _cmd_whoami(args: argparse.Namespace) -> None:
    """Show current authentication status."""
    from .auth import mask_api_key, resolve_api_key, whoami

    creds = whoami()
    env_key = os.environ.get("TERACRON_API_KEY", "").strip()

    if args.json_output:
        if creds:
            _write_out(
                json.dumps(
                    {
                        "authenticated": True,
                        "source": "credentials_file",
                        "project_slug": creds.project_slug,
                        "domain": creds.domain,
                        "api_key_masked": mask_api_key(creds.api_key),
                    },
                    indent=2,
                )
                + "\n"
            )
        elif env_key:
            _write_out(
                json.dumps(
                    {
                        "authenticated": True,
                        "source": "environment_variable",
                        "api_key_masked": mask_api_key(env_key),
                    },
                    indent=2,
                )
                + "\n"
            )
        else:
            _write_out(json.dumps({"authenticated": False}) + "\n")
    else:
        if creds:
            _write_err(
                f"[teracron] Logged in as: {creds.project_slug}\n"
                f"[teracron] Domain:       {creds.domain}\n"
                f"[teracron] API Key:      {mask_api_key(creds.api_key)}\n"
                f"[teracron] Source:        ~/.teracron/credentials.json\n"
            )
        elif env_key:
            _write_err(
                f"[teracron] API Key:      {mask_api_key(env_key)}\n"
                "[teracron] Source:        TERACRON_API_KEY env var\n"
            )
        else:
            _write_err(
                "[teracron] Not authenticated.\n"
                "[teracron] Run: teracron-agent login\n"
            )


def _cmd_events(args: argparse.Namespace) -> None:
    """Query recent workflow events."""
    from .auth import resolve_api_key
    from .query import TeracronQueryClient

    api_key = resolve_api_key(cli_key=args.api_key)
    if not api_key:
        _write_err(
            "[teracron] ERROR: No API key found. Run: teracron-agent login\n"
        )
        sys.exit(1)

    domain = args.domain or _DEFAULT_DOMAIN
    client = TeracronQueryClient(api_key=api_key, domain=domain)

    result = client.list_events(
        workflow=args.workflow,
        status=args.status,
        limit=args.limit,
        since=args.since,
    )

    if args.json_output:
        _write_out(json.dumps(result, indent=2, default=str) + "\n")
    else:
        if result.get("error"):
            _write_err(f"[teracron] Error: {result['error']}\n")
            if result.get("hint"):
                _write_err(f"[teracron] Hint: {result['hint']}\n")
        elif not result.get("events"):
            _write_err("[teracron] No events found.\n")
        else:
            _write_err(f"[teracron] {len(result['events'])} event(s):\n\n")
            for evt in result["events"]:
                status_icon = {"succeeded": "✓", "failed": "✗", "in_progress": "⋯"}.get(
                    evt.get("status", ""), "?"
                )
                _write_err(
                    f"  {status_icon} [{evt.get('workflow', '?')}] "
                    f"{evt.get('operation', '')}  "
                    f"{evt.get('duration_ms', 0):.1f}ms  "
                    f"trace={evt.get('trace_id', '?')[:12]}...\n"
                )
                if evt.get("error_summary"):
                    _write_err(f"    └─ {evt['error_summary']}\n")


def _cmd_workflows(args: argparse.Namespace) -> None:
    """List workflow run summaries."""
    from .auth import resolve_api_key
    from .query import TeracronQueryClient

    api_key = resolve_api_key(cli_key=args.api_key)
    if not api_key:
        _write_err(
            "[teracron] ERROR: No API key found. Run: teracron-agent login\n"
        )
        sys.exit(1)

    domain = args.domain or _DEFAULT_DOMAIN
    client = TeracronQueryClient(api_key=api_key, domain=domain)
    result = client.list_workflows(limit=args.limit)

    if args.json_output:
        _write_out(json.dumps(result, indent=2, default=str) + "\n")
    else:
        if result.get("error"):
            _write_err(f"[teracron] Error: {result['error']}\n")
            if result.get("hint"):
                _write_err(f"[teracron] Hint: {result['hint']}\n")
        elif not result.get("workflows"):
            _write_err("[teracron] No workflows found.\n")
        else:
            _write_err(f"[teracron] {len(result['workflows'])} workflow(s):\n\n")
            for wf in result["workflows"]:
                _write_err(
                    f"  {wf.get('workflow', '?'):<24s} "
                    f"total={wf.get('total_runs', 0):>5d}  "
                    f"failed={wf.get('failed_runs', 0):>5d}  "
                    f"avg={wf.get('avg_duration_ms', 0):.1f}ms\n"
                )


def _cmd_trace(args: argparse.Namespace) -> None:
    """Fetch a full trace span tree."""
    from .auth import resolve_api_key
    from .query import TeracronQueryClient

    api_key = resolve_api_key(cli_key=args.api_key)
    if not api_key:
        _write_err(
            "[teracron] ERROR: No API key found. Run: teracron-agent login\n"
        )
        sys.exit(1)

    domain = args.domain or _DEFAULT_DOMAIN
    client = TeracronQueryClient(api_key=api_key, domain=domain)
    result = client.get_trace(args.trace_id)

    if args.json_output:
        _write_out(json.dumps(result, indent=2, default=str) + "\n")
    else:
        if result.get("error"):
            _write_err(f"[teracron] Error: {result['error']}\n")
            if result.get("hint"):
                _write_err(f"[teracron] Hint: {result['hint']}\n")
        else:
            _write_err(f"[teracron] Trace: {args.trace_id}\n\n")
            spans = result.get("spans", [])
            if not spans:
                _write_err("  (no spans)\n")
            for span in spans:
                indent = "  "
                if span.get("parent_span_id"):
                    indent = "    "
                status_icon = {"succeeded": "✓", "failed": "✗", "started": "⋯"}.get(
                    span.get("status", ""), "?"
                )
                _write_err(
                    f"{indent}{status_icon} {span.get('operation', '?')}"
                    f"  {span.get('duration_ms', 0):.1f}ms"
                    f"  [{span.get('status', '?')}]\n"
                )
                if span.get("error_message"):
                    _write_err(f"{indent}  └─ {span['error_type']}: {span['error_message']}\n")
                if span.get("captured_params"):
                    _write_err(f"{indent}  └─ params: {span['captured_params']}\n")


def _cmd_simulate(args: argparse.Namespace) -> None:
    """Replay a failed trace for local diagnosis."""
    from .auth import resolve_api_key
    from .simulate import FailureSimulator
    from .query import TeracronQueryClient

    api_key = resolve_api_key(cli_key=args.api_key)
    if not api_key:
        _write_err(
            "[teracron] ERROR: No API key found. Run: teracron-agent login\n"
        )
        sys.exit(1)

    domain = args.domain or _DEFAULT_DOMAIN
    query_client = TeracronQueryClient(api_key=api_key, domain=domain)
    simulator = FailureSimulator(query_client=query_client)

    trace_id = args.sim_trace_id
    ctx = simulator.fetch_failure_context(trace_id)

    if ctx.get("error"):
        if args.json_output:
            _write_out(json.dumps(ctx, indent=2, default=str) + "\n")
        else:
            _write_err(f"[teracron] Error: {ctx['error']}\n")
            if ctx.get("hint"):
                _write_err(f"[teracron] Hint: {ctx['hint']}\n")
        sys.exit(1)

    fmt = args.sim_format

    if fmt == "json":
        _write_out(json.dumps(ctx, indent=2, default=str) + "\n")
    elif fmt == "script":
        script = simulator.generate_repro_script(ctx)
        _write_out(script + "\n")
    else:
        diagnosis = simulator.print_diagnosis(ctx)
        _write_out(diagnosis + "\n")


def _cmd_curl_example(args: argparse.Namespace) -> None:
    """Print curl command examples for AI agents."""
    from .auth import mask_api_key, resolve_api_key

    api_key = resolve_api_key(cli_key=args.api_key)
    domain = args.domain or _DEFAULT_DOMAIN

    key_display = mask_api_key(api_key) if api_key else "<YOUR_API_KEY>"
    key_placeholder = "$TERACRON_API_KEY"

    examples = f"""# ─── Teracron API — curl examples for AI agents ───
#
# Replace {key_placeholder} with your actual API key.
# Or export it: export TERACRON_API_KEY="tcn_..."
#
# Current key: {key_display}
# Domain: {domain}

# 1. List recent failed events
curl -s -H "Authorization: Bearer {key_placeholder}" \\
  "https://{domain}/api/v1/events?status=failed&limit=10"

# 2. List events for a specific workflow
curl -s -H "Authorization: Bearer {key_placeholder}" \\
  "https://{domain}/api/v1/events?workflow=payment&limit=20"

# 3. Get a full trace span tree
curl -s -H "Authorization: Bearer {key_placeholder}" \\
  "https://{domain}/api/v1/traces/<TRACE_ID>"

# 4. List workflow summaries
curl -s -H "Authorization: Bearer {key_placeholder}" \\
  "https://{domain}/api/v1/workflows?limit=20"

# 5. Get a single span detail
curl -s -H "Authorization: Bearer {key_placeholder}" \\
  "https://{domain}/api/v1/spans/<SPAN_ID>"

# 6. Auth check (whoami)
curl -s -H "Authorization: Bearer {key_placeholder}" \\
  "https://{domain}/v1/auth/whoami"

# ─── Tip: pipe JSON output through jq for readability ───
# curl ... | jq '.'
"""

    _write_out(examples)


# ── Main entry point ──


def main() -> None:
    """
    Entry point for ``teracron-agent`` CLI command.

    Backward-compatible: no subcommand = ``run`` (original behavior).
    """
    parser = _build_parser()
    args = parser.parse_args()

    command = args.command

    # Default to "run" when no subcommand is provided (backward compat).
    if command is None:
        command = "run"

    dispatch = {
        "run": _cmd_run,
        "login": _cmd_login,
        "logout": _cmd_logout,
        "whoami": _cmd_whoami,
        "events": _cmd_events,
        "workflows": _cmd_workflows,
        "trace": _cmd_trace,
        "simulate": _cmd_simulate,
        "curl-example": _cmd_curl_example,
    }

    handler = dispatch.get(command)
    if handler is None:
        parser.print_help()
        sys.exit(1)

    handler(args)


if __name__ == "__main__":
    main()
