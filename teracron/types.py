# -*- coding: utf-8 -*-
"""
Teracron SDK — Type definitions.

Dataclasses for metrics snapshots, flush results, spans, and resolved
configuration.  All types are self-contained with zero external dependencies.

Compatible with Python 3.8+.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple


# ── Metrics ──


@dataclass(frozen=True)
class MetricsSnapshot:
    """A single system metrics snapshot aligned to the Convex schema."""

    timestamp: int  # Unix ms
    heap_total: int  # bytes — mapped from Python VMS
    heap_used: int  # bytes — mapped from Python USS or RSS
    rss: int  # bytes — resident set size
    external: int = 0  # bytes — N/A in Python, always 0
    array_buffers: int = 0  # bytes — N/A in Python, always 0
    cpu_usage: float = -1.0  # ratio 0.0–1.0; -1 if unavailable
    event_loop_lag_ms: float = -1.0  # ms; -1 if unavailable (N/A for Python)


@dataclass(frozen=True)
class FlushResult:
    """Result of a single flush operation."""

    sent: int  # metrics count sent in this flush
    status_code: int  # HTTP status, or 0 on transport failure
    success: bool  # True when server returned 202


# ── Tracing ──

# Allowed metadata value types — primitives only; no nested objects.
METADATA_ALLOWED_TYPES: Tuple[type, ...] = (str, int, float, bool)

# Hard ceiling for metadata dict to prevent accidental DoS.
METADATA_MAX_KEYS = 32
METADATA_MAX_KEY_LEN = 128
METADATA_MAX_VALUE_LEN = 1024  # applies to str values only

# Hard ceiling for captured parameter values.
CAPTURE_MAX_VALUE_LEN = 512


@dataclass(frozen=True)
class Span:
    """
    Immutable record of a single method execution within a traced workflow.

    Frozen to guarantee thread-safety once created.  Use
    ``dataclasses.replace()`` to produce updated copies (e.g. on
    finalisation).

    Fields mirror the ``POST /v1/traces`` payload schema.
    """

    trace_id: str  # 32-char hex — groups spans in one workflow execution
    span_id: str  # 32-char hex — unique per method call
    parent_span_id: Optional[str] = field(default=None)  # Phase 2 nesting
    workflow: str = ""  # logical process name (user-provided)
    operation: str = ""  # func.__qualname__ (auto-detected)
    status: str = "started"  # "started" | "succeeded" | "failed"
    started_at: int = 0  # Unix ms wall-clock
    duration_ms: float = 0.0  # monotonic delta (ms)
    error_type: Optional[str] = field(default=None)
    error_message: Optional[str] = field(default=None)
    metadata: Optional[Dict[str, object]] = field(default=None)
    captured_params: Optional[Dict[str, object]] = field(default=None)

    def to_dict(self) -> dict:
        """Return a JSON-serialisable dictionary matching the wire schema."""
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "workflow": self.workflow,
            "operation": self.operation,
            "status": self.status,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "metadata": self.metadata,
            "captured_params": self.captured_params,
        }


@dataclass(frozen=True)
class TraceFlushResult:
    """Result of a single trace flush operation."""

    sent: int  # span count sent in this flush
    status_code: int  # HTTP status, or 0 on transport failure
    success: bool  # True when server returned 202


# ── Workflow Events (Phase 4 — query & agent support) ──


@dataclass(frozen=True)
class WorkflowEvent:
    """A single workflow event returned by the query API."""

    trace_id: str
    workflow: str
    status: str  # "succeeded" | "failed" | "in_progress"
    started_at: int  # Unix ms
    duration_ms: float
    span_count: int
    error_summary: Optional[str] = field(default=None)

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "workflow": self.workflow,
            "status": self.status,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "span_count": self.span_count,
            "error_summary": self.error_summary,
        }


@dataclass(frozen=True)
class WorkflowRun:
    """Aggregated workflow run summary returned by the query API."""

    workflow: str
    total_runs: int
    failed_runs: int
    avg_duration_ms: float
    last_run_at: int  # Unix ms

    def to_dict(self) -> dict:
        return {
            "workflow": self.workflow,
            "total_runs": self.total_runs,
            "failed_runs": self.failed_runs,
            "avg_duration_ms": self.avg_duration_ms,
            "last_run_at": self.last_run_at,
        }


@dataclass(frozen=True)
class SimulationResult:
    """Result of a failure simulation/replay."""

    trace_id: str
    workflow: str
    failed_operation: str
    error_type: Optional[str] = field(default=None)
    error_message: Optional[str] = field(default=None)
    captured_params: Optional[Dict[str, object]] = field(default=None)
    span_chain: Tuple[str, ...] = field(default_factory=tuple)
    diagnosis: str = ""
    repro_script: str = ""

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "workflow": self.workflow,
            "failed_operation": self.failed_operation,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "captured_params": self.captured_params,
            "span_chain": list(self.span_chain),
            "diagnosis": self.diagnosis,
            "repro_script": self.repro_script,
        }


@dataclass(frozen=True)
class AuthToken:
    """Resolved authentication token for API access."""

    api_key: str
    project_slug: str
    domain: str
    source: str  # "cli_flag" | "env_var" | "credentials_file"


# ── Configuration ──


@dataclass(frozen=True)
class ResolvedConfig:
    """Fully validated and normalised configuration."""

    project_slug: str
    public_key: str
    domain: str
    interval_s: float
    max_buffer_size: int
    timeout_s: float
    flush_deadline_s: float = 60.0  # seconds — time-based flush ceiling
    debug: bool = False
    target_pid: Optional[int] = field(default=None)
    # Tracing
    tracing_enabled: bool = True
    trace_batch_size: int = 100  # max spans per flush (1–10_000)
    trace_flush_interval: float = 10.0  # seconds between trace flushes (1–300)
    trace_sample_rate: float = 1.0  # 0.0–1.0; decided per trace root
    tracing_scrubber: Optional[Callable[[dict], dict]] = field(default=None)  # PII scrubber
    trace_emit_events: bool = False  # Emit structured workflow events (Phase 4)
