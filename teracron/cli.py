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
    teracron-agent projects           # List accessible project(s)
    teracron-agent events             # Query workflow events
    teracron-agent workflows          # List workflow runs
    teracron-agent trace <trace_id>   # Fetch a full trace span tree
    teracron-agent simulate <id>      # Replay a failed trace locally
    teracron-agent curl-example       # Print ready-to-use curl commands
    teracron-agent local-interface    # Run the local loopback interface (dev)

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


def _resolve_domain(cli_domain: Optional[str]) -> str:
    """
    Resolve the effective domain for read/query commands.

    Precedence (highest → lowest):
        1. ``--domain`` flag (``cli_domain``).
        2. ``TERACRON_DOMAIN`` environment variable.
        3. The ``domain`` field of the saved credentials file (set by
           ``teracron-agent login``).
        4. The hard-coded default ``www.teracron.com``.

    This means a developer who logged in against ``localhost:3000`` (local
    interface / local dashboard) no longer has to repeat ``--domain
    localhost:3000`` on every subsequent ``projects`` / ``logs`` / ``events`` /
    ``trace`` call — the domain travels with the login session, mirroring how
    the API key does.

    Centralising the precedence here keeps the seven CLI sub-commands consistent
    and future-proof: any new read command picks up the same behaviour for free.
    """
    if cli_domain:
        return cli_domain
    env_domain = os.environ.get("TERACRON_DOMAIN", "").strip()
    if env_domain:
        return env_domain
    # Lazy-import to avoid circular import + to keep the CLI cold-start path
    # cheap when no saved credentials are involved (login/logout/whoami).
    try:
        from .auth import load_credentials

        creds = load_credentials()
        if creds and getattr(creds, "domain", None):
            return creds.domain
    except Exception:  # nosec B110 — fall through to default on ANY error
        pass
    return _DEFAULT_DOMAIN


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

    # ── projects ──
    sub.add_parser(
        "projects",
        help="List the project(s) the current API key can access.",
    )

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

    # ── local-interface ──
    li_p = sub.add_parser(
        "local-interface",
        help="Run the local loopback interface that stands in for teracron.com (dev).",
    )
    li_p.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1).")
    li_p.add_argument("--port", type=int, default=3000, help="Bind port (default: 3000).")
    li_p.add_argument("--quiet", action="store_true", help="Suppress per-request logs.")
    li_p.add_argument(
        "--allow-non-loopback",
        dest="allow_non_loopback",
        action="store_true",
        help="Allow binding a non-loopback host (advanced; off by default).",
    )

    # ── local-teracron ──
    # The spool consumer: reads the local interface's spool and forwards the
    # still-encrypted envelopes to the Convex database over HTTPS. This is the
    # "local Teracron" hop in: SDK → interface → local teracron → Convex DB.
    lt_p = sub.add_parser(
        "local-teracron",
        help="Forward the local-interface spool to the Convex database (dev).",
    )
    lt_p.add_argument(
        "--convex-site",
        dest="convex_site",
        default=None,
        help="Convex site URL (https://<deployment>.convex.site). "
        "Defaults to CONVEX_SITE_URL / NEXT_PUBLIC_CONVEX_SITE_URL or is derived "
        "from CONVEX_URL / NEXT_PUBLIC_CONVEX_URL.",
    )
    lt_p.add_argument(
        "--once",
        action="store_true",
        help="Drain the spool once and exit (default: poll forever).",
    )
    lt_p.add_argument("--quiet", action="store_true", help="Suppress per-envelope logs.")

    # ── logs ──
    # Live-tail spans from the Convex database (the agent's "see live logs" view).
    logs_p = sub.add_parser(
        "logs",
        help="Tail recent trace spans from the database (use --follow for live).",
    )
    logs_p.add_argument(
        "--follow",
        "-f",
        action="store_true",
        help="Continuously poll for new spans (live tail). Ctrl+C to stop.",
    )
    logs_p.add_argument(
        "--limit", type=int, default=100, help="Max spans per poll (default: 100)."
    )
    logs_p.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Poll interval in seconds when --follow is set (default: 2.0).",
    )
    logs_p.add_argument(
        "--convex-site",
        dest="convex_site",
        default=None,
        help="Convex site URL to read from (https://<deployment>.convex.site). "
        "Defaults to CONVEX_SITE_URL / NEXT_PUBLIC_CONVEX_SITE_URL or derived "
        "from CONVEX_URL / NEXT_PUBLIC_CONVEX_URL.",
    )
    logs_p.add_argument(
        "--mode",
        choices=["development", "production", "all"],
        default=None,
        help="Filter spans by project mode. Default: the mode of your API key "
        "(a dev key tails dev spans, a prod key tails prod spans). Use 'all' "
        "to tail both modes.",
    )

    # ── account / project management (create from the terminal) ──
    sub.add_parser(
        "accounts",
        help="Show the logged-in account (account creation is via the dashboard).",
    )
    proj_p = sub.add_parser(
        "projects",
        help="List your projects, or create one with: projects --create NAME.",
    )
    proj_p.add_argument(
        "--create",
        dest="create_name",
        metavar="NAME",
        default=None,
        help="Create a new project with the given name and print its dev + "
        "prod API keys.",
    )

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
    domain = _resolve_domain(args.domain)

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


def _cmd_projects(args: argparse.Namespace) -> None:
    """
    List the project(s) the current API key can access.

    This is the AI agent's *discovery* step: given valid credentials, it learns
    which project (and slug) the key maps to — e.g. the payment server — before
    searching for a crash. A Teracron API key is project-scoped, so this returns
    the single project bound to the key.
    """
    # ── Create path (projects --create NAME) ──
    # Creating a project mints a brand-new project with BOTH dev and prod key
    # pairs server-side. Per the agreed scope (Q4) the default workflow reuses
    # the existing project; --create is the opt-in escape hatch for spinning up
    # a new one from the terminal.
    if getattr(args, "create_name", None):
        _create_project_from_cli(args)
        return

    from .auth import resolve_api_key
    from .query import TeracronQueryClient

    api_key = resolve_api_key(cli_key=args.api_key)
    if not api_key:
        _write_err("[teracron] ERROR: No API key found. Run: teracron-agent login\n")
        sys.exit(1)

    domain = _resolve_domain(args.domain)
    client = TeracronQueryClient(api_key=api_key, domain=domain)
    result = client.list_projects()

    if args.json_output:
        _write_out(json.dumps(result, indent=2, default=str) + "\n")
        return

    if result.get("error"):
        _write_err(f"[teracron] Error: {result['error']}\n")
        if result.get("hint"):
            _write_err(f"[teracron] Hint: {result['hint']}\n")
        return

    projects = result.get("projects") or []
    if not projects:
        _write_err("[teracron] No accessible projects found.\n")
        return

    # Surface the mode of the API key in use so it's obvious which dev/prod
    # bucket this key reads & writes.
    try:
        from .apikey import decode_api_key

        _slug, _pem, key_mode = decode_api_key(api_key)
    except Exception:
        key_mode = "production"

    _write_err(
        f"[teracron] {len(projects)} project(s)  "
        f"(API key mode: {key_mode}):\n\n"
    )
    for proj in projects:
        _write_err(
            f"  • {proj.get('name', '(unnamed)')}  "
            f"[slug={proj.get('slug', '?')}]\n"
        )
    _write_err(
        "\n[teracron] Tip: create a new project with: "
        "teracron-agent projects --create <NAME>\n"
    )


def _create_project_from_cli(args: argparse.Namespace) -> None:
    """
    Create a new project from the terminal by calling the Convex ``create``
    action, then print both the development and production API keys.

    Creating a project is a *user-scoped* operation (it belongs to your account,
    not to a single project key), so it needs your dashboard **session token**,
    not a project API key. Provide it via the ``TERACRON_SESSION_TOKEN`` env var
    (copy it from the dashboard after logging in). The Convex deployment URL is
    read from ``CONVEX_URL`` / ``NEXT_PUBLIC_CONVEX_URL``.

    This keeps account/project creation possible from the IDE terminal (per the
    product spec) while reusing the existing server-side ``create`` action — no
    new privileged endpoint is introduced.
    """
    import os as _os

    from .local_teracron import _convex_action  # lightweight Convex HTTP caller

    name = args.create_name.strip()
    if not (1 <= len(name) <= 64):
        _write_err("[teracron] ERROR: Project name must be 1–64 characters.\n")
        sys.exit(1)

    token = _os.environ.get("TERACRON_SESSION_TOKEN", "").strip()
    if not token:
        _write_err(
            "[teracron] ERROR: Creating a project needs your dashboard session "
            "token.\n"
            "[teracron]        Set TERACRON_SESSION_TOKEN (copy it from the "
            "Teracron dashboard after logging in), then retry.\n"
        )
        sys.exit(1)

    convex_url = (
        _os.environ.get("CONVEX_URL")
        or _os.environ.get("NEXT_PUBLIC_CONVEX_URL")
        or ""
    ).strip()
    if not convex_url:
        _write_err(
            "[teracron] ERROR: No Convex deployment URL configured.\n"
            "[teracron]        Set CONVEX_URL (or NEXT_PUBLIC_CONVEX_URL).\n"
        )
        sys.exit(1)

    _write_err(f"[teracron] Creating project '{name}'…\n")
    # 1. Create the project (returns its Convex document id).
    created = _convex_action(
        convex_url, "projects:create", {"token": token, "name": name}
    )
    if isinstance(created, dict) and created.get("error"):
        _write_err(f"[teracron] Create failed: {created['error']}\n")
        sys.exit(1)
    project_id = created if isinstance(created, str) else (
        created.get("value") if isinstance(created, dict) else None
    )
    if not project_id:
        _write_err("[teracron] Create failed: unexpected response.\n")
        sys.exit(1)

    # 2. Fetch the freshly-minted dev + prod API keys.
    keys = _convex_action(
        convex_url,
        "projects:getPublicKey",
        {"token": token, "projectId": project_id},
        kind="query",
    )
    if not isinstance(keys, dict) or keys.get("error"):
        _write_err(
            "[teracron] Project created, but failed to read its API keys. "
            "Open the dashboard to copy them.\n"
        )
        return

    if args.json_output:
        _write_out(json.dumps(keys, indent=2, default=str) + "\n")
        return

    _write_err(
        f"[teracron] ✓ Project created: {keys.get('slug')}\n\n"
        f"  Development API key (local/testing):\n    {keys.get('devApiKey')}\n\n"
        f"  Production API key (production system):\n    {keys.get('prodApiKey')}\n\n"
        "[teracron] Put ONE of these in TERACRON_API_KEY. The mode is baked "
        "into the key — dev keys log to development, prod keys to production.\n"
    )


def _cmd_accounts(args: argparse.Namespace) -> None:
    """
    Show the current account context.

    Account *creation* is a dashboard-only flow (it needs email/password sign-up
    UI). From the terminal we surface who you're authenticated as and point to
    project creation, which IS available here.
    """
    from .auth import mask_api_key, resolve_api_key, whoami

    creds = whoami()
    api_key = resolve_api_key(cli_key=args.api_key)

    if args.json_output:
        _write_out(
            json.dumps(
                {
                    "authenticated": bool(creds or api_key),
                    "project_slug": creds.project_slug if creds else None,
                    "api_key_masked": mask_api_key(api_key) if api_key else None,
                    "account_creation": "dashboard_only",
                },
                indent=2,
            )
            + "\n"
        )
        return

    if creds:
        _write_err(
            f"[teracron] Account context — project: {creds.project_slug}\n"
            f"[teracron]   API Key: {mask_api_key(creds.api_key)}\n"
        )
    elif api_key:
        _write_err(f"[teracron]   API Key: {mask_api_key(api_key)}\n")
    else:
        _write_err(
            "[teracron] Not authenticated. Run: teracron-agent login\n"
        )
    _write_err(
        "[teracron] New accounts are created on the dashboard (sign-up). "
        "New projects can be created here: teracron-agent projects --create <NAME>\n"
    )


def _cmd_local_interface(args: argparse.Namespace) -> None:
    """Run the local loopback interface (dev stand-in for teracron.com)."""
    from .local_interface import serve

    try:
        serve(
            host=args.host,
            port=args.port,
            quiet=args.quiet,
            allow_non_loopback=args.allow_non_loopback,
        )
    except ValueError as exc:
        _write_err(f"{exc}\n")
        sys.exit(1)


def _cmd_local_teracron(args: argparse.Namespace) -> None:
    """
    Run the local Teracron spool consumer.

    Reads the local interface's spool and forwards each still-encrypted envelope
    to the Convex database over HTTPS. Completes the local pipeline:
        SDK → local interface → local teracron (this) → Convex DB
    """
    from .local_teracron import LocalTeracron, derive_convex_site_url

    try:
        site = derive_convex_site_url(explicit=args.convex_site)
    except ValueError as exc:
        _write_err(f"{exc}\n")
        sys.exit(1)

    lt = LocalTeracron(convex_site=site, quiet=args.quiet)
    if args.once:
        counts = lt.run_once()
        lt.close()
        if args.json_output:
            _write_out(json.dumps(counts) + "\n")
        else:
            _write_err(
                f"[teracron] local-teracron one-shot: "
                f"forwarded={counts['forwarded']} "
                f"quarantined={counts['quarantined']} "
                f"retried={counts['retried']}\n"
            )
        return
    lt.serve()


def _render_span_line(span: dict) -> str:
    """Format one span DTO as a single readable live-log line."""
    status = span.get("status", "?")
    icon = {"succeeded": "✓", "failed": "✗", "started": "⋯"}.get(status, "?")
    op = span.get("operation", "?")
    wf = span.get("workflow", "?")
    dur = span.get("duration_ms", 0) or 0
    trace = (span.get("trace_id") or "")[:12]
    parent = "  └─" if span.get("parent_span_id") else "•"
    line = (
        f"  {parent} {icon} [{wf}] {op}  {dur:.1f}ms  "
        f"[{status}]  trace={trace}"
    )
    captured = span.get("captured_params")
    if captured:
        line += f"\n        params={json.dumps(captured, default=str)}"
    if span.get("error_message"):
        line += f"\n        error={span.get('error_type')}: {span.get('error_message')}"
    return line


def _cmd_logs(args: argparse.Namespace) -> None:
    """
    Tail recent trace spans from the Convex database (over HTTPS).

    Reads come straight from Convex's ``GET /v1/logs`` HTTP action on the
    ``*.convex.site`` host — no Next.js server required. Without ``--follow``
    this prints one snapshot of the latest spans. With ``--follow`` it polls
    forever, printing only spans newer than the last poll (uses the server
    ``cursor`` as an exclusive watermark), giving a live view of data landing in
    the database as the SDK emits it.
    """
    import time

    from .auth import resolve_api_key
    from .local_teracron import fetch_recent_spans, derive_convex_site_url

    api_key = resolve_api_key(cli_key=args.api_key)
    if not api_key:
        _write_err("[teracron] ERROR: No API key found. Run: teracron-agent login\n")
        sys.exit(1)

    try:
        site = derive_convex_site_url(explicit=args.convex_site)
    except ValueError as exc:
        _write_err(f"{exc}\n")
        sys.exit(1)

    # Resolve which mode to tail. Precedence:
    #   --mode all              → no filter (both modes)
    #   --mode development|prod → that mode
    #   (unset)                 → the mode encoded in the API key, so the tail
    #                             matches the key the user is holding.
    if args.mode == "all":
        mode = None
    elif args.mode in ("development", "production"):
        mode = args.mode
    else:
        try:
            from .apikey import decode_api_key

            _slug, _pem, mode = decode_api_key(api_key)
        except Exception:
            mode = None
    if mode:
        _write_err(f"[teracron] Tailing '{mode}' spans.\n")

    def _poll(since):
        result = fetch_recent_spans(
            convex_site=site, api_key=api_key, since=since,
            limit=args.limit, mode=mode,
        )
        if result.get("error"):
            return result, since
        return result, result.get("cursor", since)

    # First poll — snapshot of the most-recent spans.
    result, cursor = _poll(None)

    if args.json_output and not args.follow:
        _write_out(json.dumps(result, indent=2, default=str) + "\n")
        return

    if result.get("error"):
        _write_err(f"[teracron] Error: {result['error']}\n")
        if result.get("hint"):
            _write_err(f"[teracron] Hint: {result['hint']}\n")
        if not args.follow:
            return

    spans = result.get("spans") or []
    if not args.follow and not spans:
        _write_err("[teracron] No spans found yet.\n")
        return

    if not args.follow:
        _write_err(f"[teracron] {len(spans)} recent span(s):\n\n")
        for span in spans:
            _write_err(_render_span_line(span) + "\n")
        return

    # ── Live follow mode ──
    _write_err(
        f"[teracron] Live-tailing spans from {site}. Press Ctrl+C to stop.\n\n"
    )
    for span in spans:
        _write_err(_render_span_line(span) + "\n")

    try:
        while True:
            time.sleep(max(0.5, args.interval))
            result, cursor = _poll(cursor)
            if result.get("error"):
                # Transient (server not up yet, network) — keep trying quietly.
                continue
            for span in result.get("spans") or []:
                _write_err(_render_span_line(span) + "\n")
    except KeyboardInterrupt:
        _write_err("\n[teracron] Stopped live tail.\n")


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

    domain = _resolve_domain(args.domain)
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

    domain = _resolve_domain(args.domain)
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

    domain = _resolve_domain(args.domain)
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

    domain = _resolve_domain(args.domain)
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
    from .config import resolve_scheme, _sanitise_domain

    api_key = resolve_api_key(cli_key=args.api_key)
    domain = _sanitise_domain(_resolve_domain(args.domain))
    # Loopback-aware base URL: http:// for the local interface, https:// for prod.
    base = f"{resolve_scheme(domain)}://{domain}/api/v1"

    key_display = mask_api_key(api_key) if api_key else "<YOUR_API_KEY>"
    key_placeholder = "$TERACRON_API_KEY"

    examples = f"""# ─── Teracron API — curl examples for AI agents ───
#
# Replace {key_placeholder} with your actual API key.
# Or export it: export TERACRON_API_KEY="tcn_..."
#
# Current key: {key_display}
# Base URL: {base}

# 1. Discover the project this key maps to (find the slug, e.g. "payment")
curl -s -H "Authorization: Bearer {key_placeholder}" \\
  "{base}/projects"

# 2. List recent failed events (crash search)
curl -s -H "Authorization: Bearer {key_placeholder}" \\
  "{base}/events?status=workflow_failed&limit=10"

# 3. List events for a specific workflow
curl -s -H "Authorization: Bearer {key_placeholder}" \\
  "{base}/events?workflow=payment&limit=20"

# 4. Get a full trace span tree (the method-flow / thread flow)
curl -s -H "Authorization: Bearer {key_placeholder}" \\
  "{base}/traces/<TRACE_ID>"

# 5. List workflow summaries
curl -s -H "Authorization: Bearer {key_placeholder}" \\
  "{base}/workflows?limit=20"

# 6. Get a single span detail
curl -s -H "Authorization: Bearer {key_placeholder}" \\
  "{base}/spans/<SPAN_ID>"

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
        "accounts": _cmd_accounts,
        "projects": _cmd_projects,
        "events": _cmd_events,
        "workflows": _cmd_workflows,
        "trace": _cmd_trace,
        "simulate": _cmd_simulate,
        "curl-example": _cmd_curl_example,
        "local-interface": _cmd_local_interface,
        "local-teracron": _cmd_local_teracron,
        "logs": _cmd_logs,
    }

    handler = dispatch.get(command)
    if handler is None:
        parser.print_help()
        sys.exit(1)

    handler(args)


if __name__ == "__main__":
    main()
