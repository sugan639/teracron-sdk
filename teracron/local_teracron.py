# -*- coding: utf-8 -*-
"""
Local Teracron — the spool consumer that forwards to the Convex database.

WHERE THIS SITS IN THE PIPELINE
-------------------------------
    payment.py ──http──▶ local interface ──spool──▶ LOCAL TERACRON ──https──▶ Convex DB
       (SDK)              (local_interface.py)      (THIS MODULE)            (the database)

The *local interface* (``local_interface.py``) only receives encrypted
envelopes and spools them to ``~/.teracron/local-spool/`` — it never decrypts,
decodes, or forwards anything. That missing forward hop is exactly what this
module provides: it is the "local Teracron" the interface's docstring and
``LOCAL_INTERFACE.md`` always referred to as the spool *consumer*.

WHAT IT DOES
------------
1. Watches ``~/.teracron/local-spool/`` for new ``*.json`` sidecars (the
   hand-off contract written by the interface).
2. For each sidecar, reads the matching encrypted ``.bin`` envelope and routes
   it by ``kind`` (``metrics`` / ``traces`` / ``events``) to the corresponding
   Convex HTTP ingest endpoint, sending the **still-encrypted bytes unchanged**
   with the ``X-Project-Slug`` header — exactly the arguments the production
   Next.js routes pass. Convex decrypts (it holds the project's private key) and
   stores the spans/metrics/events in its database.
3. Archives processed files (moves them to ``local-spool/processed/``) so they
   are never re-sent, and quarantines permanently-rejected envelopes.

WHY REPLAY THE ENCRYPTED ENVELOPE (and not decrypt here)
--------------------------------------------------------
Per the agreed design (Q1 = path, Q2 = "1.a"), the local Teracron does **not**
decrypt. It replays the opaque envelope to Convex, which is the single component
that holds the private key. Benefits:
  * Zero new decryption surface — all crypto stays server-side in Convex.
  * The local path reuses the *identical* ingest + validation code as
    production; only the delivery mechanism changes (spool poll vs HTTP body).
  * Convex is reached over HTTPS (the database provider is always remote), so
    every byte that reaches the DB does so over TLS — matching Q2.

TRANSPORT TARGET
----------------
Convex exposes HTTP ingest actions on its ``*.convex.site`` host:
    POST {site}/ingest      → metrics   (internal.ingest.processIngest)
    POST {site}/v1/traces   → traces    (internal.traces.processTraceIngest)
    POST {site}/v1/events   → events    (internal.events.processEventIngest)
The site URL is derived from the deployment's ``.convex.cloud`` URL (``.cloud``
→ ``.site``) or supplied explicitly via ``--convex-site`` / ``CONVEX_SITE_URL``.

DELIVERY SEMANTICS (at-least-once, idempotent in practice)
----------------------------------------------------------
* A sidecar is only acted on once its ``.bin`` is fully on disk (the interface
  writes ``.bin`` then ``.json``), so there are no torn reads.
* On a permanent rejection (4xx that is not rate-limit) the envelope is moved to
  ``quarantine/`` — it will never decrypt, so retrying is pointless.
* On a transient failure (network error, 5xx, 429) the files are left in place
  and retried on the next poll with capped backoff. Convex span inserts are
  keyed by ``(traceId, spanId)`` semantics in the mutation, so an occasional
  duplicate delivery does not corrupt the trace view.

SCALE / READABILITY
-------------------
A single dev process produces trivial volume, so a flat poll loop with a small
``requests.Session`` is intentionally simple and easy to read. If this ever
needed real throughput it would shard the spool and parallelise — but that is
deliberately out of scope for a local dev component (don't add speculative
complexity).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

import requests

from . import __version__
from .config import resolve_scheme, _sanitise_domain

# ── Spool layout (must match local_interface.py) ────────────────────────────
_TERACRON_DIR = ".teracron"
_SPOOL_DIR_NAME = "local-spool"
_PROCESSED_DIR = "processed"   # archive of successfully forwarded envelopes
_QUARANTINE_DIR = "quarantine"  # permanently-rejected envelopes (won't decrypt)

# ── Convex HTTP ingest routes, keyed by the sidecar ``kind`` ────────────────
# Paths mirror convex/http.ts exactly. The local interface tags each spooled
# envelope with one of these kinds; we map kind → Convex path here.
_KIND_TO_PATH: Dict[str, str] = {
    "metrics": "/ingest",
    "traces": "/v1/traces",
    "events": "/v1/events",
}

# Poll cadence + backoff. Small + simple — this is a dev component.
_POLL_INTERVAL_S = 1.0
_MAX_BACKOFF_S = 15.0
_HTTP_TIMEOUT_S = 15.0

# HTTP statuses that mean "this envelope will never succeed" → quarantine.
# 401 = decryption failed, 400 = malformed/oversize, 413 = too large, 415 =
# wrong content-type. (404 = project-not-found is treated as transient: the
# project may simply not be created yet.)
_PERMANENT_REJECT = frozenset({400, 401, 413, 415})


def _spool_root() -> Path:
    """Resolve (and create) the spool directory shared with the interface."""
    root = Path.home() / _TERACRON_DIR / _SPOOL_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def derive_convex_site_url(*, explicit: Optional[str] = None) -> str:
    """
    Resolve the Convex HTTP-actions base URL (the ``*.convex.site`` origin).

    Resolution order:
        1. ``explicit`` argument (``--convex-site`` flag).
        2. ``CONVEX_SITE_URL`` env var.
        3. ``NEXT_PUBLIC_CONVEX_SITE_URL`` env var (the dashboard's own var).
        4. Derive from ``CONVEX_URL`` / ``NEXT_PUBLIC_CONVEX_URL`` by swapping
           the ``.convex.cloud`` suffix for ``.convex.site`` (Convex's fixed
           convention: the deployment's HTTP actions live on the ``.site`` host).

    Returns a URL with no trailing slash. Raises ``ValueError`` if nothing is
    configured.
    """
    candidate = (
        explicit
        or os.environ.get("CONVEX_SITE_URL")
        or os.environ.get("NEXT_PUBLIC_CONVEX_SITE_URL")
    )
    if candidate:
        return candidate.rstrip("/")

    cloud = os.environ.get("CONVEX_URL") or os.environ.get("NEXT_PUBLIC_CONVEX_URL")
    if cloud:
        cloud = cloud.rstrip("/")
        if ".convex.cloud" in cloud:
            return cloud.replace(".convex.cloud", ".convex.site")
        # Already a site URL or self-hosted — use as-is.
        return cloud

    raise ValueError(
        "[local-teracron] No Convex site URL configured. "
        "Set CONVEX_SITE_URL (or NEXT_PUBLIC_CONVEX_SITE_URL), or pass "
        "--convex-site https://<deployment>.convex.site"
    )


def fetch_recent_spans(
    *,
    convex_site: str,
    api_key: str,
    since: Optional[int] = None,
    limit: int = 100,
    mode: Optional[str] = None,
    timeout_s: float = _HTTP_TIMEOUT_S,
) -> Dict[str, object]:
    """
    Read the most-recent spans for a project from the Convex database over HTTPS.

    This is the read side of the live-log tail (``teracron-agent logs``). It
    targets the Convex ``GET /v1/logs`` HTTP action directly on the
    ``*.convex.site`` host — so the terminal / AI agent can watch data land with
    **no Next.js server required**, matching the "reads go through Convex over
    HTTPS" contract.

    Auth is the same ``Bearer tcn_`` key used everywhere else; Convex verifies
    the token's PEM against the stored project key before returning anything.

    Returns the parsed JSON dict (``{spans, cursor, span_count}``) on success, or
    ``{"error": ..., "hint": ...}`` on failure. Never raises.
    """
    url = f"{convex_site.rstrip('/')}/v1/logs"
    params: Dict[str, object] = {"limit": max(1, min(limit, 500))}
    if since is not None:
        params["since"] = since
    # Restrict the tail to a single mode (development|production). Omitted ⇒ the
    # server returns spans from all modes.
    if mode in ("development", "production"):
        params["mode"] = mode

    try:
        resp = requests.get(
            url,
            params=params,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
            timeout=timeout_s,
        )
    except requests.ConnectionError:
        return {"error": "Cannot reach Convex.", "hint": f"Check {url} is reachable."}
    except requests.Timeout:
        return {"error": f"Request timed out after {timeout_s}s."}
    except requests.RequestException as exc:
        return {"error": f"Request failed: {type(exc).__name__}"}

    if resp.status_code == 200:
        try:
            return resp.json()
        except ValueError:
            return {"error": "Invalid JSON response from Convex."}
    if resp.status_code == 401:
        return {"error": "Authentication failed — invalid API key.",
                "hint": "Run: teracron-agent login"}
    if resp.status_code == 404:
        return {"error": "Project not found.",
                "hint": "Create the project in the Teracron dashboard first."}
    return {"error": f"Unexpected response: HTTP {resp.status_code}"}


_VALID_MODES = frozenset({"development", "production"})


def _convex_action(
    convex_url: str,
    path: str,
    args: Dict[str, object],
    *,
    kind: str = "action",
    timeout_s: float = _HTTP_TIMEOUT_S,
) -> object:
    """
    Call a Convex function over its public HTTP API on the ``.cloud`` host.

    Convex exposes ``POST {deployment}.convex.cloud/api/<kind>`` where ``kind``
    is ``query`` / ``mutation`` / ``action`` and the body is
    ``{"path": "module:function", "args": {...}, "format": "json"}``. This is the
    same endpoint the JS ``ConvexHttpClient`` uses; calling it directly lets the
    CLI create projects (and read their keys) from the terminal with nothing but
    a session token — no Node/JS dependency.

    Returns the function's ``value`` on success, or ``{"error": ...}`` on
    failure. Never raises.
    """
    base = convex_url.rstrip("/")
    url = f"{base}/api/{kind}"
    try:
        resp = requests.post(
            url,
            json={"path": path, "args": args, "format": "json"},
            headers={"Content-Type": "application/json"},
            timeout=timeout_s,
        )
    except requests.RequestException as exc:
        return {"error": f"Convex request failed: {type(exc).__name__}"}

    try:
        body = resp.json()
    except ValueError:
        return {"error": f"Invalid Convex response (HTTP {resp.status_code})."}

    # Convex returns {"status": "success", "value": ...} or
    # {"status": "error", "errorMessage": ...}.
    if isinstance(body, dict):
        if body.get("status") == "success":
            return body.get("value")
        if body.get("status") == "error":
            return {"error": body.get("errorMessage", "Convex error.")}
    return {"error": f"Unexpected Convex response (HTTP {resp.status_code})."}


@dataclass(frozen=True)
class _SpoolItem:
    """One spooled envelope = a sidecar + its payload, parsed and ready to send."""

    sidecar: Path
    payload: Path
    kind: str
    slug: str
    mode: str  # development | production — replayed to Convex as a header


def _read_spool_item(sidecar: Path) -> Optional[_SpoolItem]:
    """
    Parse a ``*.json`` sidecar into a :class:`_SpoolItem`.

    Returns ``None`` (and leaves the files for a later pass) if the sidecar is
    not yet complete or its payload is missing — both are normal transient races
    with the interface still writing, not errors.
    """
    try:
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None

    kind = meta.get("kind")
    slug = meta.get("slug")
    payload_name = meta.get("payload_file")
    if kind not in _KIND_TO_PATH or not slug or not payload_name:
        return None

    # Mode is optional in the sidecar for forward-compat with envelopes spooled
    # by an older interface — those default to "production".
    mode = meta.get("mode", "production")
    if mode not in _VALID_MODES:
        mode = "production"

    payload = sidecar.parent / payload_name
    if not payload.exists():
        # Payload not on disk yet (or already archived) — skip this pass.
        return None

    return _SpoolItem(
        sidecar=sidecar, payload=payload, kind=kind, slug=slug, mode=mode
    )


class LocalTeracron:
    """
    The spool consumer. Polls the spool and forwards envelopes to Convex.

    One ``requests.Session`` is reused across sends for keep-alive. The class is
    deliberately small: ``run_once`` does a single drain pass (used by tests and
    one-shot mode), ``serve`` loops ``run_once`` forever with backoff.
    """

    __slots__ = ("_spool", "_site", "_session", "_quiet", "_processed", "_quarantine")

    def __init__(self, *, convex_site: str, spool: Optional[Path] = None,
                 quiet: bool = False) -> None:
        self._spool = spool or _spool_root()
        self._site = convex_site.rstrip("/")
        self._quiet = quiet

        # Archive sub-dirs — created lazily but resolved up front.
        self._processed = self._spool / _PROCESSED_DIR
        self._quarantine = self._spool / _QUARANTINE_DIR
        self._processed.mkdir(parents=True, exist_ok=True)
        self._quarantine.mkdir(parents=True, exist_ok=True)

        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/octet-stream",
            "User-Agent": (
                f"teracron-local-teracron/{__version__} "
                f"python/{sys.version_info.major}.{sys.version_info.minor}"
            ),
        })

    # ── Logging ──
    def _log(self, msg: str) -> None:
        if not self._quiet:
            sys.stderr.write(f"[local-teracron] {msg}\n")
            sys.stderr.flush()

    # ── Forwarding ──
    def _forward(self, item: _SpoolItem) -> Tuple[bool, int]:
        """
        POST one encrypted envelope to the matching Convex ingest endpoint.

        Returns ``(success, status_code)``. Never raises — a network error is
        reported as ``(False, 0)`` so the caller can retry.
        """
        url = f"{self._site}{_KIND_TO_PATH[item.kind]}"
        try:
            payload = item.payload.read_bytes()
        except OSError:
            return (False, 0)

        try:
            resp = self._session.post(
                url,
                data=payload,
                headers={"X-Project-Slug": item.slug, "X-Project-Mode": item.mode},
                timeout=_HTTP_TIMEOUT_S,
            )
        except requests.RequestException:
            return (False, 0)
        # 202 Accepted is the success contract for all three ingest routes.
        return (resp.status_code == 202, resp.status_code)

    def _archive(self, item: _SpoolItem, dest_dir: Path) -> None:
        """Move a sidecar+payload pair out of the active spool into ``dest_dir``."""
        for f in (item.payload, item.sidecar):
            try:
                shutil.move(str(f), str(dest_dir / f.name))
            except OSError:
                pass  # Best-effort; a leftover file is retried next pass.

    def run_once(self) -> Dict[str, int]:
        """
        Drain the spool one time. Returns counts ``{forwarded, quarantined,
        retried}`` for observability/tests.

        Sidecars are processed in filename order — filenames are ``epoch_ns``
        prefixed, so this is chronological (envelopes land in the DB in send
        order).
        """
        forwarded = quarantined = retried = 0

        sidecars = sorted(
            p for p in self._spool.glob("*.json") if p.is_file()
        )
        for sidecar in sidecars:
            item = _read_spool_item(sidecar)
            if item is None:
                continue  # Incomplete/owned-by-interface — try again next pass.

            ok, status = self._forward(item)
            if ok:
                self._archive(item, self._processed)
                forwarded += 1
                self._log(
                    f"forwarded {item.kind} slug={item.slug} "
                    f"mode={item.mode} → Convex DB (202)"
                )
            elif status in _PERMANENT_REJECT:
                self._archive(item, self._quarantine)
                quarantined += 1
                self._log(
                    f"quarantined {item.kind} slug={item.slug} "
                    f"(HTTP {status} — will never succeed)"
                )
            else:
                # Transient (0/404/429/5xx) — leave in place, retry next pass.
                retried += 1
                self._log(
                    f"retry later {item.kind} slug={item.slug} (HTTP {status})"
                )

        return {"forwarded": forwarded, "quarantined": quarantined, "retried": retried}

    def serve(self) -> None:
        """
        Poll the spool forever, forwarding envelopes to Convex. Blocks until
        interrupted (Ctrl+C). Backs off when a pass makes no forward progress so
        an unreachable Convex doesn't spin the CPU.
        """
        self._log(f"Local Teracron started — forwarding spool → {self._site}")
        self._log(f"Spool: {self._spool}")
        self._log("Press Ctrl+C to stop.")

        backoff = _POLL_INTERVAL_S
        try:
            while True:
                counts = self.run_once()
                progressed = counts["forwarded"] > 0
                if progressed:
                    backoff = _POLL_INTERVAL_S  # reset on success
                    time.sleep(_POLL_INTERVAL_S)
                else:
                    # No forwards this pass — idle or transient failure. Back off
                    # gently so a down Convex / empty spool is cheap.
                    time.sleep(backoff)
                    backoff = min(backoff * 1.5, _MAX_BACKOFF_S)
        except KeyboardInterrupt:
            self._log("Shutting down.")
        finally:
            self.close()

    def close(self) -> None:
        """Release the connection pool."""
        try:
            self._session.close()
        except Exception:  # nosec B110
            pass


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="teracron-local-teracron",
        description=(
            "Local Teracron — consumes the local-interface spool and forwards "
            "encrypted envelopes to the Convex database over HTTPS."
        ),
    )
    parser.add_argument(
        "--convex-site",
        default=None,
        help=(
            "Convex HTTP-actions base URL (https://<deployment>.convex.site). "
            "Defaults to CONVEX_SITE_URL / NEXT_PUBLIC_CONVEX_SITE_URL, or is "
            "derived from CONVEX_URL / NEXT_PUBLIC_CONVEX_URL."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Drain the spool once and exit (default: poll forever).",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="Suppress per-envelope logging."
    )
    return parser


def main(argv: Optional[list] = None) -> None:
    """Console entry point for ``python -m teracron.local_teracron``."""
    args = _build_arg_parser().parse_args(argv)
    try:
        site = derive_convex_site_url(explicit=args.convex_site)
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        sys.exit(1)

    lt = LocalTeracron(convex_site=site, quiet=args.quiet)
    if args.once:
        counts = lt.run_once()
        lt.close()
        sys.stderr.write(
            f"[local-teracron] one-shot: forwarded={counts['forwarded']} "
            f"quarantined={counts['quarantined']} retried={counts['retried']}\n"
        )
        return
    lt.serve()


if __name__ == "__main__":
    main()
