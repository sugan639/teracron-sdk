# -*- coding: utf-8 -*-
"""
Local interface — the loopback stand-in for ``teracron.com`` in development.

WHY THIS EXISTS
---------------
In production the Python SDK posts encrypted envelopes to ``teracron.com``
(``/api/ingest``, ``/api/v1/traces``, ``/api/v1/events``). For local
development we don't want any inter-server HTTPS between the instrumented app
(e.g. the payment server) and the local Teracron. Instead the SDK posts to a
*local interface* — this module — over plain HTTP on a loopback address.

The interface is a thin, dependency-free receiver:

    payment.py  ──http──▶  local_interface (this file)  ──spool──▶  local Teracron
      (SDK)                  loopback:3000                   ~/.teracron/local-spool/

It performs the *same* request validation as the production Next.js routes
(slug header, content-type, size caps), then **spools the raw encrypted
envelope to disk**. The local Teracron consumes the spool directory. Because
the envelope stays RSA+AES encrypted, dropping TLS on the loopback hop does not
expose plaintext, and the encrypted bytes are also encrypted at rest.

DESIGN NOTES (for future devs)
------------------------------
* **Stdlib only** (``http.server``). No third-party deps — this must be trivial
  to run anywhere a dev has Python, and must never pull the SDK's runtime deps
  (``requests`` etc.) into the receive path.
* **One file, one job.** It accepts envelopes and spools them. It does NOT
  decrypt, decode, or interpret payloads — that's the local Teracron's job.
  Keeping responsibilities split keeps the security surface tiny.
* **Endpoint parity.** The accepted routes and validation mirror the real
  routes so the SDK code path is identical in local and production. If a
  production route's limits change, mirror them in :data:`_ROUTES`.
* **Spool contract.** Each accepted request is written as one file:
  ``<epoch_ns>-<kind>-<slug>.bin`` plus a sidecar ``.json`` with metadata
  (kind, slug, byte length, received-at). The local Teracron reads ``*.json``
  to learn how to decrypt/route the matching ``.bin``. This contract is the
  hand-off boundary — keep it stable.
* **Scale/readability.** A single dev process generates trivial volume, so a
  flat spool dir is fine. If this ever needs to scale, shard by slug or date —
  but that is explicitly out of scope for a local dev interface.

RUN IT
------
    teracron-agent local-interface            # listens on 127.0.0.1:3000
    teracron-agent local-interface --port 4000
    python -m teracron.local_interface --port 3000

This is a development tool only — it binds to loopback by default and refuses
to bind to non-loopback addresses unless explicitly forced.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional, Tuple

# ── Spool location ──────────────────────────────────────────────────────────
# Mirrors the SDK's credentials dir (``~/.teracron``) so all local Teracron
# state lives under one predictable, user-scoped root.
_SPOOL_DIR_NAME = "local-spool"
_TERACRON_DIR = ".teracron"

# ── Validation constants (mirror the production Next.js routes) ─────────────
_SLUG_RE = re.compile(r"^[a-z]+-[a-z]+-\d{3}$")
_CONTENT_TYPE = "application/octet-stream"

# Valid project modes. The SDK sends the mode on every request via the
# X-Project-Mode header; mode-less (legacy) requests default to "production".
_VALID_MODES = frozenset({"development", "production"})
_DEFAULT_MODE = "production"


def _slug_ok(slug: str) -> bool:
    """Validate a project slug exactly as the production routes do."""
    return bool(slug) and bool(_SLUG_RE.match(slug))


def _normalise_mode(raw: str) -> str:
    """Map the X-Project-Mode header to a canonical mode, defaulting safely."""
    m = (raw or "").strip().lower()
    if m in _VALID_MODES:
        return m
    if m == "dev":
        return "development"
    if m == "prod":
        return "production"
    return _DEFAULT_MODE

# Restrictive perms for spooled (encrypted) data — owner only.
_DIR_MODE = stat.S_IRWXU  # 0700
_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR  # 0600


@dataclass(frozen=True)
class _Route:
    """One accepted ingest route and its payload ceiling.

    ``kind`` is the logical payload type written into the spool sidecar so the
    local Teracron knows which decoder/mutation to run. ``max_bytes`` mirrors
    the corresponding production route's size cap.
    """

    kind: str
    max_bytes: int


# Route table — keep in lockstep with the production Next.js routes.
#   /api/ingest      → metrics      (64KB, see src/app/api/ingest/route.ts)
#   /api/v1/traces   → trace spans  (128KB, see .../v1/traces/route.ts)
#   /api/v1/events   → workflow evs (64KB, see .../v1/events/route.ts)
_ROUTES: Dict[str, _Route] = {
    "/api/ingest": _Route(kind="metrics", max_bytes=65_536),
    "/api/v1/traces": _Route(kind="traces", max_bytes=131_072),
    "/api/v1/events": _Route(kind="events", max_bytes=65_536),
}

_LOOPBACK_BIND = frozenset({"127.0.0.1", "localhost", "::1"})


def _spool_dir() -> Path:
    """Resolve and create the spool directory (``~/.teracron/local-spool``)."""
    root = Path.home() / _TERACRON_DIR / _SPOOL_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(root), _DIR_MODE)
    except OSError:
        pass  # Best-effort on platforms without chmod.
    return root


def _spool_envelope(
    spool: Path, *, kind: str, slug: str, mode: str, payload: bytes
) -> str:
    """
    Write one accepted envelope to the spool and return the base filename.

    Layout (the hand-off contract with the local Teracron):
        <epoch_ns>-<kind>-<slug>.bin    → raw encrypted bytes (unchanged)
        <epoch_ns>-<kind>-<slug>.json   → metadata sidecar (includes ``mode``)

    The ``.bin`` is written first, then the sidecar — a consumer that watches
    for ``*.json`` therefore only ever sees a sidecar once its payload is fully
    on disk (no torn reads).
    """
    stamp = time.time_ns()
    base = f"{stamp}-{kind}-{slug}"
    bin_path = spool / f"{base}.bin"
    json_path = spool / f"{base}.json"

    # Payload first (atomic-ish: temp + replace) so the sidecar never points at
    # a partial file.
    tmp_bin = bin_path.with_suffix(".bin.tmp")
    fd = os.open(str(tmp_bin), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp_bin), str(bin_path))

    meta = {
        "kind": kind,
        "slug": slug,
        "mode": mode,  # development | production — replayed downstream
        "payload_file": bin_path.name,
        "byte_length": len(payload),
        "received_at": int(time.time() * 1000),  # Unix ms
    }
    tmp_json = json_path.with_suffix(".json.tmp")
    fd = os.open(str(tmp_json), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
    try:
        os.write(fd, json.dumps(meta, sort_keys=True).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp_json), str(json_path))

    return base


class _IngestHandler(BaseHTTPRequestHandler):
    """
    Minimal request handler mirroring the production ingest contract.

    Each instance is created per-request by ``ThreadingHTTPServer``; the spool
    directory is shared via a class attribute set in :func:`serve`.
    """

    # Set by serve(); avoids per-request Path resolution.
    spool: Path = None  # type: ignore[assignment]
    quiet: bool = False

    # Use HTTP/1.1 keep-alive to match the SDK's persistent session.
    protocol_version = "HTTP/1.1"
    server_version = "teracron-local-interface"

    # ── Response helpers ──
    def _json(self, status: int, body: Dict[str, object]) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def do_OPTIONS(self) -> None:  # noqa: N802 (http.server naming)
        """CORS preflight — mirror the production routes."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, X-Project-Slug, X-Project-Mode, Authorization",
        )
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 (http.server naming)
        """
        Accept an encrypted envelope, validate it, and spool it.

        Validation order matches the production routes so the SDK sees the same
        status codes locally as it would in production.
        """
        route = _ROUTES.get(self.path.split("?", 1)[0])
        if route is None:
            self._error(404, "Unknown ingest path.")
            return

        # 1. Slug header (format-validated, identical to production).
        slug = self.headers.get("X-Project-Slug", "")
        if not _slug_ok(slug):
            self._error(400, "Missing or invalid X-Project-Slug header.")
            return

        # 1b. Mode header (optional). Absent/invalid ⇒ "production" so legacy
        #     SDKs that don't send the header keep their prior behaviour.
        mode = _normalise_mode(self.headers.get("X-Project-Mode", ""))

        # 2. Content-Type.
        ctype = self.headers.get("Content-Type", "")
        if _CONTENT_TYPE not in ctype:
            self._error(415, "Content-Type must be application/octet-stream.")
            return

        # 3. Body length (fail fast on missing/oversize before reading).
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._error(400, "Invalid Content-Length.")
            return
        if length <= 0:
            self._error(400, "Empty payload.")
            return
        if length > route.max_bytes:
            self._error(413, f"Payload exceeds {route.max_bytes} byte limit.")
            return

        try:
            payload = self.rfile.read(length)
        except Exception:
            self._error(400, "Failed to read request body.")
            return
        if len(payload) != length:
            self._error(400, "Truncated request body.")
            return

        # 4. Spool to disk for the local Teracron to consume. Never raise back
        #    to the SDK — degrade to a 500 the SDK treats as a transient miss.
        try:
            base = _spool_envelope(
                self.spool, kind=route.kind, slug=slug, mode=mode, payload=payload
            )
        except OSError as exc:
            if not self.quiet:
                sys.stderr.write(f"[local-interface] spool error: {exc}\n")
            self._error(500, "Failed to persist payload.")
            return

        if not self.quiet:
            sys.stderr.write(
                f"[local-interface] accepted {route.kind} "
                f"slug={slug} mode={mode} bytes={len(payload)} → {base}.bin\n"
            )
            # Flush immediately so live monitoring (tail -f, piped logs) sees
            # each receipt in real time — stderr is block-buffered when
            # redirected to a file/pipe.
            sys.stderr.flush()

        # 202 Accepted — exactly what the SDK expects from production.
        self._json(202, {"status": "accepted", "kind": route.kind})

    def log_message(self, *_args: object) -> None:
        """Silence the default per-request stderr access log (we log our own)."""
        return


def serve(host: str = "127.0.0.1", port: int = 3000, *, quiet: bool = False,
          allow_non_loopback: bool = False) -> None:
    """
    Start the local interface and block until interrupted.

    Args:
        host: Bind address. Must be loopback unless ``allow_non_loopback`` is
            set — binding a plaintext ingest endpoint to a public interface
            would be a security risk, so we refuse by default.
        port: TCP port (default 3000 — the same port ``payment.py`` targets).
        quiet: Suppress per-request stderr logging.
        allow_non_loopback: Escape hatch for advanced setups (e.g. container
            networking). Off by default.
    """
    if host not in _LOOPBACK_BIND and not allow_non_loopback:
        raise ValueError(
            f"[local-interface] Refusing to bind non-loopback host '{host}'. "
            "This interface terminates plaintext HTTP and is dev-only. "
            "Pass allow_non_loopback=True only if you understand the risk."
        )

    spool = _spool_dir()
    _IngestHandler.spool = spool
    _IngestHandler.quiet = quiet

    httpd = ThreadingHTTPServer((host, port), _IngestHandler)
    sys.stderr.write(
        "[local-interface] Teracron local interface listening on "
        f"http://{host}:{port}\n"
        f"[local-interface] Spooling encrypted envelopes to: {spool}\n"
        "[local-interface] Accepted routes: "
        f"{', '.join(sorted(_ROUTES))}\n"
        "[local-interface] Press Ctrl+C to stop.\n"
    )
    sys.stderr.flush()  # Surface the banner immediately under piped/redirected stderr.
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\n[local-interface] Shutting down.\n")
    finally:
        httpd.server_close()


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="teracron-local-interface",
        description="Local loopback interface that stands in for teracron.com.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=3000, help="Bind port (default: 3000).")
    parser.add_argument("--quiet", action="store_true", help="Suppress per-request logs.")
    parser.add_argument(
        "--allow-non-loopback",
        action="store_true",
        help="Allow binding a non-loopback host (advanced; off by default).",
    )
    return parser


def main(argv: Optional[list] = None) -> None:
    """Console entry point for ``python -m teracron.local_interface``."""
    args = _build_arg_parser().parse_args(argv)
    try:
        serve(
            host=args.host,
            port=args.port,
            quiet=args.quiet,
            allow_non_loopback=args.allow_non_loopback,
        )
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
