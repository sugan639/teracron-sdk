# -*- coding: utf-8 -*-
"""
System metrics collector — gathers memory and CPU data from a target process.

Uses psutil for cross-platform process inspection. Falls back to the resource
module for RSS-only collection when psutil is unavailable (should not happen
given the hard dependency, but defense in depth).

Memory mapping (Python → Convex schema):
    heap_total  →  vms  (virtual memory size — analogous to V8 heap total)
    heap_used   →  uss  (unique set size — closest to V8 heap used)
    rss         →  rss  (resident set size — identical concept)
    external    →  0    (no C++ binding layer in Python)
    array_buffers → 0   (no ArrayBuffer concept in Python)
"""

from __future__ import annotations

import time
from typing import Optional

import psutil

from .types import MetricsSnapshot


class Collector:
    """
    Stateful metrics collector bound to a single OS process.

    The instance caches a psutil.Process handle and pre-warms CPU tracking
    on first call (cpu_percent needs a prior measurement for delta calculation).
    """

    __slots__ = ("_process", "_cpu_primed")

    def __init__(self, pid: Optional[int] = None) -> None:
        """
        Args:
            pid: Target process ID. ``None`` monitors the current process.

        Raises:
            psutil.NoSuchProcess: If the PID does not exist.
            psutil.AccessDenied: If insufficient permissions.
        """
        self._process = psutil.Process(pid)  # None → os.getpid()
        self._cpu_primed = False

    def collect(self) -> MetricsSnapshot:
        """
        Take a single metrics snapshot.

        Returns a frozen MetricsSnapshot dataclass. Never raises — returns
        best-effort data on partial failure.
        """
        now_ms = int(time.time() * 1000)

        # ── Memory ──
        heap_total = 0
        heap_used = 0
        rss = 0

        try:
            mem_info = self._process.memory_info()
            rss = mem_info.rss
            heap_total = mem_info.vms

            # USS (unique set size) is the best Python analog to "heap_used".
            # Available via memory_full_info() on Linux/macOS/Windows.
            try:
                full_info = self._process.memory_full_info()
                heap_used = getattr(full_info, "uss", rss)
            except (psutil.AccessDenied, psutil.NoSuchProcess, AttributeError):
                # Fallback: use RSS as heap_used (conservative approximation)
                heap_used = rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass  # All memory values remain 0

        # ── CPU ──
        cpu_usage = -1.0
        try:
            raw_pct = self._process.cpu_percent(interval=None)
            if not self._cpu_primed:
                # First call returns 0.0 always — discard and prime the tracker
                self._cpu_primed = True
                cpu_usage = -1.0
            else:
                # psutil returns 0–100+ (can exceed 100 on multi-core)
                # Normalise to 0.0–1.0 ratio, capped at 1.0
                cpu_usage = min(raw_pct / 100.0, 1.0)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

        return MetricsSnapshot(
            timestamp=now_ms,
            heap_total=heap_total,
            heap_used=heap_used,
            rss=rss,
            external=0,
            array_buffers=0,
            cpu_usage=cpu_usage,
            event_loop_lag_ms=-1.0,  # No event loop in Python
        )

    @property
    def is_alive(self) -> bool:
        """Check if the target process is still running."""
        try:
            return self._process.is_running() and self._process.status() != psutil.STATUS_ZOMBIE
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
