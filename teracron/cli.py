# -*- coding: utf-8 -*-
"""
CLI entry point for the Teracron standalone agent.

Installed as ``teracron-agent`` via the package's ``[project.scripts]``.

Usage::

    # Set your API key and go
    export TERACRON_API_KEY="tcn_..."
    teracron-agent

    # Monitor a specific process by PID
    export TERACRON_API_KEY="tcn_..."
    export TERACRON_TARGET_PID=$(pgrep -f "gunicorn")
    teracron-agent

    # With options
    TERACRON_API_KEY="tcn_..." TERACRON_DEBUG=true teracron-agent

Environment variables:
    TERACRON_API_KEY        — API key from the Teracron dashboard (required)
    TERACRON_INTERVAL       — collection interval in seconds (default: 30)
    TERACRON_TIMEOUT        — HTTP timeout in seconds (default: 10)
    TERACRON_MAX_BUFFER     — max buffered snapshots (default: 60)
    TERACRON_DOMAIN         — ingest domain (default: www.teracron.com)
    TERACRON_TARGET_PID     — PID of the target process to monitor
    TERACRON_DEBUG          — "true" or "1" to enable debug logging
"""

from __future__ import annotations

import os
import signal
import sys
import threading


_BANNER = r"""
  ╔════════════════════════════════════════╗
  ║       Teracron Agent  v{version:<14s} ║
  ║       Python Memory Metrics Agent      ║
  ╚════════════════════════════════════════╝
"""


def main() -> None:
    """Entry point for ``teracron-agent`` CLI command."""

    from . import __version__
    from .client import up, down

    sys.stderr.write(_BANNER.format(version=__version__))
    sys.stderr.flush()

    # ── Validate API key early ──
    api_key = os.environ.get("TERACRON_API_KEY", "").strip()

    if not api_key:
        sys.stderr.write(
            "[teracron] ERROR: TERACRON_API_KEY environment variable is required.\n"
            "[teracron]        Copy it from the Teracron dashboard → Settings → SDK Setup.\n"
        )
        sys.exit(1)

    # ── Start agent via teracron.up() ──
    try:
        client = up()
    except ValueError as exc:
        sys.stderr.write("[teracron] Configuration error: %s\n" % exc)
        sys.exit(1)
    except Exception as exc:
        sys.stderr.write("[teracron] Failed to start: %s\n" % exc)
        sys.exit(1)

    target_pid = os.environ.get("TERACRON_TARGET_PID", "self")
    resolved_slug = client.config.project_slug
    sys.stderr.write(
        "[teracron] Monitoring PID=%s  slug=%s\n"
        "[teracron] Press Ctrl+C to stop.\n" % (target_pid, resolved_slug)
    )
    sys.stderr.flush()

    # ── Register signal handlers for graceful shutdown ──
    shutdown_event = threading.Event()

    def _handle_signal(signum, _frame):
        # type: (int, object) -> None
        sig_name = signal.Signals(signum).name
        sys.stderr.write("\n[teracron] Received %s — shutting down...\n" % sig_name)
        sys.stderr.flush()
        shutdown_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # ── Block until signal ──
    shutdown_event.wait()

    # ── Graceful shutdown ──
    down()
    sys.stderr.write("[teracron] Agent stopped. Goodbye.\n")
    sys.stderr.flush()


if __name__ == "__main__":
    main()
