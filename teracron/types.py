# -*- coding: utf-8 -*-
"""
Teracron SDK — Type definitions.

Dataclasses for metrics snapshots, flush results, and resolved configuration.
All types are self-contained with zero external dependencies.

Compatible with Python 3.8+.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


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


@dataclass(frozen=True)
class ResolvedConfig:
    """Fully validated and normalised configuration."""

    project_slug: str
    public_key: str
    domain: str
    interval_s: float
    max_buffer_size: int
    timeout_s: float
    debug: bool
    target_pid: Optional[int] = field(default=None)
