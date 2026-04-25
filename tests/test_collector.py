"""Unit tests for teracron.collector — system metrics collection."""

import os
import time

import pytest

from teracron.collector import Collector
from teracron.types import MetricsSnapshot


class TestCollector:
    """Tests for the Collector class."""

    def test_collect_returns_snapshot(self):
        """Collecting from the current process should return a valid snapshot."""
        collector = Collector()
        snap = collector.collect()

        assert isinstance(snap, MetricsSnapshot)
        assert snap.rss > 0
        assert snap.heap_total > 0
        assert snap.timestamp > 0
        assert snap.external == 0
        assert snap.array_buffers == 0

    def test_timestamp_is_recent(self):
        """Timestamp should be within 2 seconds of now."""
        collector = Collector()
        now_ms = int(time.time() * 1000)
        snap = collector.collect()
        assert abs(snap.timestamp - now_ms) < 2000

    def test_heap_used_leq_heap_total(self):
        """heap_used (USS) should not exceed heap_total (VMS)."""
        collector = Collector()
        snap = collector.collect()
        assert snap.heap_used <= snap.heap_total

    def test_cpu_priming(self):
        """First collection should report cpu_usage=-1 (priming), second should be >=0."""
        collector = Collector()
        first = collector.collect()
        assert first.cpu_usage == -1.0

        time.sleep(0.1)  # Small delay for CPU delta
        second = collector.collect()
        assert second.cpu_usage >= 0.0

    def test_is_alive_for_current_process(self):
        """Current process should always be alive."""
        collector = Collector()
        assert collector.is_alive is True

    def test_invalid_pid_raises(self):
        """Non-existent PID should raise."""
        with pytest.raises(Exception):
            Collector(pid=999999999)

    def test_target_pid(self):
        """Collecting from own PID (explicit) should work identically."""
        pid = os.getpid()
        collector = Collector(pid=pid)
        snap = collector.collect()
        assert snap.rss > 0
