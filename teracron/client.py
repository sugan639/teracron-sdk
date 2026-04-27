# -*- coding: utf-8 -*-
"""
TeracronClient -- background metrics agent for Python applications.

Primary API::

    import teracron
    teracron.up()          # reads TERACRON_API_KEY from env, starts collecting
    teracron.down()        # graceful shutdown (also runs via atexit)

Advanced / explicit::

    from teracron import TeracronClient
    client = TeracronClient(api_key="tcn_...")
    client.start()
    client.stop()

Lifecycle:
    1. ``up()`` / ``client.start()`` spawns a daemon thread that collects
       metrics at the configured interval and appends to a bounded ring buffer.
    2. Every ``max_buffer_size`` ticks (or when the buffer is full), the client
       flushes: encode, encrypt, send.
    3. ``down()`` / ``client.stop()`` signals the thread, performs a final
       flush, closes the transport.
    4. An ``atexit`` handler ensures graceful shutdown even if ``stop()``
       is never called explicitly.

Thread safety:
    - Buffer access is guarded by a ``threading.Lock`` (minimal contention --
      the lock is held only for list append/drain, never during I/O).
    - The daemon thread flag ensures the agent never prevents process exit.

Error policy:
    - **Never** crash the host process. All exceptions are caught internally.
    - Transport failures are silently discarded (metrics are best-effort).
    - Debug mode logs to stderr via ``_debug()`` -- never to stdout.
"""

from __future__ import annotations

import atexit
import collections
import json
import sys
import threading
import time
from typing import List, Optional

from .collector import Collector
from .config import resolve_config
from .crypto import encrypt_envelope
from .encoder import encode_batch
from .transport import Transport
from .types import FlushResult, MetricsSnapshot, ResolvedConfig, TraceFlushResult


class TeracronClient:
    """
    Teracron metrics agent — collects, encrypts, and ships memory telemetry.

    Simplest usage::

        import teracron
        teracron.up()   # done — reads TERACRON_API_KEY from env

    Explicit usage::

        from teracron import TeracronClient
        client = TeracronClient(api_key=os.environ["TERACRON_API_KEY"])
        client.start()
    """

    __slots__ = (
        "_config",
        "_collector",
        "_transport",
        "_buffer",
        "_lock",
        "_stop_event",
        "_thread",
        "_started",
        "_tick_count",
        "_last_flush_time",
        # Tracing
        "_trace_buffer",
        "_trace_lock",
        "_trace_overflow_warned",
        "_last_trace_flush_time",
        "_scrubber",
        # Structured events (Phase 4)
        "_event_buffer",
    )

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        project_slug: Optional[str] = None,
        public_key: Optional[str] = None,
        domain: Optional[str] = None,
        interval_s: Optional[float] = None,
        max_buffer_size: Optional[int] = None,
        timeout_s: Optional[float] = None,
        flush_deadline_s: Optional[float] = None,
        debug: Optional[bool] = None,
        target_pid: Optional[int] = None,
        tracing_enabled: Optional[bool] = None,
        trace_batch_size: Optional[int] = None,
        trace_flush_interval: Optional[float] = None,
        trace_sample_rate: Optional[float] = None,
        tracing_scrubber=None,
    ) -> None:
        self._config = resolve_config(
            api_key=api_key,
            project_slug=project_slug,
            public_key=public_key,
            domain=domain,
            interval_s=interval_s,
            max_buffer_size=max_buffer_size,
            timeout_s=timeout_s,
            flush_deadline_s=flush_deadline_s,
            debug=debug,
            target_pid=target_pid,
            tracing_enabled=tracing_enabled,
            trace_batch_size=trace_batch_size,
            trace_flush_interval=trace_flush_interval,
            trace_sample_rate=trace_sample_rate,
            tracing_scrubber=tracing_scrubber,
        )  # type: ResolvedConfig
        self._collector = None  # type: Optional[Collector]
        self._transport = None  # type: Optional[Transport]
        self._buffer = collections.deque(maxlen=self._config.max_buffer_size)  # type: ignore[assignment]
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None  # type: Optional[threading.Thread]
        self._started = False
        self._tick_count = 0
        self._last_flush_time = 0.0  # monotonic; set on first start

        # Tracing
        self._trace_buffer = collections.deque(
            maxlen=self._config.trace_batch_size,
        )  # type: ignore[assignment]
        self._trace_lock = threading.Lock()
        self._trace_overflow_warned = False
        self._last_trace_flush_time = 0.0
        self._scrubber = self._config.tracing_scrubber

        # Structured events (Phase 4) — initialise only when event emission is enabled.
        self._event_buffer = None
        if self._config.trace_emit_events:
            from .tracing.events import EventBuffer
            self._event_buffer = EventBuffer(capacity=500)

    # ── Public API ──

    def start(self) -> None:
        """
        Start the background metrics collection thread.

        Safe to call multiple times — subsequent calls are no-ops.
        """
        if self._started:
            return

        try:
            self._collector = Collector(pid=self._config.target_pid)
        except Exception as exc:
            self._debug("Failed to attach to process: %s" % exc)
            raise

        self._transport = Transport(
            domain=self._config.domain,
            slug=self._config.project_slug,
            timeout_s=self._config.timeout_s,
        )

        self._stop_event.clear()
        self._started = True
        self._last_flush_time = time.monotonic()
        self._last_trace_flush_time = time.monotonic()

        self._thread = threading.Thread(
            target=self._run_loop,
            name="teracron-agent",
            daemon=True,
        )
        self._thread.start()

        # Register atexit handler for graceful shutdown
        atexit.register(self.stop)

        self._debug(
            "Started — slug=%s interval=%ss pid=%s"
            % (
                self._config.project_slug,
                self._config.interval_s,
                self._config.target_pid or "self",
            )
        )

    def stop(self) -> None:
        """
        Stop the agent gracefully — performs a final flush.

        Safe to call multiple times or from atexit handlers.
        """
        if not self._started:
            return

        self._started = False
        self._stop_event.set()

        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5.0)
            self._thread = None

        # Final flushes — metrics then traces
        self._flush()
        self._flush_traces()

        if self._transport is not None:
            self._transport.close()
            self._transport = None

        self._collector = None
        self._debug("Stopped.")

    def flush(self) -> Optional[FlushResult]:
        """Manually trigger a flush. Returns None if nothing to send."""
        return self._flush()

    @property
    def is_running(self) -> bool:
        return self._started and self._thread is not None and self._thread.is_alive()

    @property
    def config(self) -> ResolvedConfig:
        """Read-only access to the resolved configuration."""
        return self._config

    # ── Internal ──

    def _run_loop(self) -> None:
        """Background collection loop — runs on the daemon thread."""
        interval = self._config.interval_s

        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:
                self._debug("Tick error: %s" % exc)

            # Interruptible sleep: check stop_event every 0.5s
            elapsed = 0.0
            while elapsed < interval and not self._stop_event.is_set():
                time.sleep(min(0.5, interval - elapsed))
                elapsed += 0.5

    def _tick(self) -> None:
        """Single collection tick: collect snapshot → buffer → maybe flush."""
        if self._collector is None:
            return

        # Check target process is still alive
        if not self._collector.is_alive:
            self._debug("Target process exited — stopping agent.")
            self._stop_event.set()
            return

        snapshot = self._collector.collect()

        now = time.monotonic()
        should_flush = False
        with self._lock:
            self._buffer.append(snapshot)  # deque(maxlen) auto-drops oldest
            self._tick_count += 1

            buffer_full = (
                self._tick_count >= self._config.max_buffer_size
                or len(self._buffer) >= self._config.max_buffer_size
            )
            deadline_exceeded = (
                len(self._buffer) > 0
                and (now - self._last_flush_time) >= self._config.flush_deadline_s
            )

            if buffer_full or deadline_exceeded:
                should_flush = True
                self._tick_count = 0

        if should_flush:
            self._flush()

        # ── Trace flush check ──
        self._maybe_flush_traces()

    def _flush(self) -> Optional[FlushResult]:
        """Drain buffer → encode → encrypt → send. Never raises."""
        with self._lock:
            if not self._buffer:
                return None
            batch = list(self._buffer)
            self._buffer.clear()
            self._last_flush_time = time.monotonic()

        if self._transport is None:
            return None

        try:
            raw = encode_batch(batch)
            envelope = encrypt_envelope(raw, self._config.public_key)
            result = self._transport.send(envelope)

            flush_result = FlushResult(
                sent=len(batch),
                status_code=result.status_code,
                success=result.success,
            )
            self._debug(
                "Flush: sent=%d status=%d ok=%s"
                % (flush_result.sent, flush_result.status_code, flush_result.success)
            )
            return flush_result
        except Exception as exc:
            self._debug("Flush failed: %s" % exc)
            return FlushResult(sent=0, status_code=0, success=False)

    def _debug(self, msg: str) -> None:
        """Emit debug message to stderr. No-op when debug is disabled."""
        if self._config.debug:
            sys.stderr.write("[teracron] %s\n" % msg)
            sys.stderr.flush()

    # ── Tracing ──

    def _push_trace_span(self, span_dict: dict) -> None:
        """
        Append a span dict to the trace buffer.

        Ring buffer (``collections.deque(maxlen=N)``) auto-drops the oldest
        entry when full.  A single warning is emitted on the first overflow.

        Called from the ``@trace`` decorator on the application thread.
        """
        with self._trace_lock:
            if (
                len(self._trace_buffer) >= self._config.trace_batch_size
                and not self._trace_overflow_warned
            ):
                sys.stderr.write(
                    "[teracron] Trace buffer full — dropping oldest spans.\n"
                )
                sys.stderr.flush()
                self._trace_overflow_warned = True
            self._trace_buffer.append(span_dict)

    def _maybe_flush_traces(self) -> None:
        """Check if a trace flush is needed (batch-size or deadline)."""
        now = time.monotonic()
        should_flush = False

        with self._trace_lock:
            buffer_len = len(self._trace_buffer)
            if buffer_len == 0:
                return
            batch_full = buffer_len >= self._config.trace_batch_size
            deadline_exceeded = (
                (now - self._last_trace_flush_time)
                >= self._config.trace_flush_interval
            )
            if batch_full or deadline_exceeded:
                should_flush = True

        if should_flush:
            self._flush_traces()

    def _flush_traces(self) -> Optional[TraceFlushResult]:
        """Drain trace buffer → JSON encode → encrypt → send. Never raises."""
        with self._trace_lock:
            if not self._trace_buffer:
                return None
            batch = list(self._trace_buffer)
            self._trace_buffer.clear()
            self._last_trace_flush_time = time.monotonic()

        if self._transport is None:
            return None

        try:
            payload = {
                "type": "trace",
                "project_slug": self._config.project_slug,
                "spans": batch,
            }
            raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            envelope = encrypt_envelope(raw, self._config.public_key)
            result = self._transport.send_traces(envelope)

            flush_result = TraceFlushResult(
                sent=len(batch),
                status_code=result.status_code,
                success=result.success,
            )
            self._debug(
                "Trace flush: sent=%d status=%d ok=%s"
                % (flush_result.sent, flush_result.status_code, flush_result.success)
            )
            return flush_result
        except Exception as exc:
            self._debug("Trace flush failed: %s" % exc)
            return TraceFlushResult(sent=0, status_code=0, success=False)


# ── Module-level singleton API ──
# ``teracron.up()`` / ``teracron.down()`` — zero-ceremony interface.

_singleton_lock = threading.Lock()
_singleton = None  # type: Optional[TeracronClient]


def up(
    *,
    api_key: Optional[str] = None,
    domain: Optional[str] = None,
    interval_s: Optional[float] = None,
    max_buffer_size: Optional[int] = None,
    timeout_s: Optional[float] = None,
    flush_deadline_s: Optional[float] = None,
    debug: Optional[bool] = None,
    target_pid: Optional[int] = None,
    tracing_enabled: Optional[bool] = None,
    trace_batch_size: Optional[int] = None,
    trace_flush_interval: Optional[float] = None,
    trace_sample_rate: Optional[float] = None,
    tracing_scrubber=None,
) -> TeracronClient:
    """
    Start Teracron telemetry in one call.

    Reads ``TERACRON_API_KEY`` from the environment (or accepts it
    explicitly).  Spawns a daemon thread, registers ``atexit`` shutdown,
    and returns the running client.

    Idempotent — calling ``up()`` again returns the same running instance.

    Usage::

        import teracron
        teracron.up()              # env-based (recommended)
        teracron.up(debug=True)    # with overrides

    Returns:
        The running ``TeracronClient`` singleton.
    """
    global _singleton

    with _singleton_lock:
        if _singleton is not None and _singleton.is_running:
            return _singleton

        client = TeracronClient(
            api_key=api_key,
            domain=domain,
            interval_s=interval_s,
            max_buffer_size=max_buffer_size,
            timeout_s=timeout_s,
            flush_deadline_s=flush_deadline_s,
            debug=debug,
            target_pid=target_pid,
            tracing_enabled=tracing_enabled,
            trace_batch_size=trace_batch_size,
            trace_flush_interval=trace_flush_interval,
            trace_sample_rate=trace_sample_rate,
            tracing_scrubber=tracing_scrubber,
        )
        client.start()
        _singleton = client
        return client


def down() -> None:
    """
    Stop the Teracron singleton agent.

    Performs a final flush and releases resources.  Safe to call even
    if ``up()`` was never called.
    """
    global _singleton

    with _singleton_lock:
        if _singleton is not None:
            _singleton.stop()
            _singleton = None
