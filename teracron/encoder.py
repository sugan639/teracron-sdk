# -*- coding: utf-8 -*-
"""
Protobuf Encoder — zero-dependency, minimal-allocation binary serialiser.

Produces the exact same wire format as the Node.js SDK encoder so the
Convex server decoder (convex/lib/protobuf.ts) can decode it identically.

Wire format:

    message MetricsEntry {
        uint64 timestamp      = 1;
        uint64 heap_total     = 2;
        uint64 heap_used      = 3;
        uint64 rss            = 4;
        uint64 external       = 5;
        uint64 array_buffers  = 6;
        uint64 cpu_usage_pct  = 7;  // CPU × 10 000 (0–10 000 for 0%–100%)
        uint64 event_loop_lag = 8;  // microseconds
    }

    message MetricsBatch {
        repeated MetricsEntry entries = 1;
        string sdk_version            = 2;
        string runtime_version        = 3;   // Python version
    }

Uses arithmetic (not bitwise) varint encoding to handle values > 2³¹
correctly — mirrors the Node.js SDK strategy.
"""

from __future__ import annotations

import platform
from typing import List, Sequence

from . import __version__
from .types import MetricsSnapshot

_SDK_VERSION = __version__


# ── Protobuf primitives ──

def _encode_varint(value: int, target: bytearray) -> None:
    """Encode an unsigned varint into *target*. Zero emits a single 0x00."""
    v = max(0, int(value))
    if v == 0:
        target.append(0)
        return
    while v > 0x7F:
        target.append((v & 0x7F) | 0x80)
        v >>= 7  # Python ints are arbitrary-precision; >> is safe
    target.append(v)


def _encode_tag(field_number: int, wire_type: int, target: bytearray) -> None:
    _encode_varint((field_number << 3) | wire_type, target)


def _encode_uint64_field(field_number: int, value: int, target: bytearray) -> None:
    """Encode a uint64 field. Omits field entirely when value is 0 (proto3 default)."""
    if value == 0:
        return
    _encode_tag(field_number, 0, target)  # wire type 0 = varint
    _encode_varint(value, target)


def _encode_string_field(field_number: int, value: str, target: bytearray) -> None:
    """Encode a length-delimited string field. Omits when empty."""
    if not value:
        return
    encoded = value.encode("utf-8")
    _encode_tag(field_number, 2, target)  # wire type 2 = length-delimited
    _encode_varint(len(encoded), target)
    target.extend(encoded)


# ── Public API ──

def encode_batch(snapshots: Sequence[MetricsSnapshot]) -> bytes:
    """
    Encode a batch of MetricsSnapshot into protobuf binary.

    Returns raw bytes ready for encryption. The wire format is identical
    to the Node.js SDK's ``encodeBatch()`` output.
    """
    batch = bytearray()

    for snap in snapshots:
        entry = bytearray()

        _encode_uint64_field(1, snap.timestamp, entry)
        _encode_uint64_field(2, snap.heap_total, entry)
        _encode_uint64_field(3, snap.heap_used, entry)
        _encode_uint64_field(4, snap.rss, entry)
        _encode_uint64_field(5, snap.external, entry)
        _encode_uint64_field(6, snap.array_buffers, entry)

        # CPU: encode as basis points (0–10 000 → 0%–100%). Skip if unavailable.
        if snap.cpu_usage >= 0:
            _encode_uint64_field(7, min(round(snap.cpu_usage * 10_000), 10_000), entry)

        # Event-loop lag: encode as microseconds. Skip if unavailable.
        if snap.event_loop_lag_ms >= 0:
            _encode_uint64_field(8, round(snap.event_loop_lag_ms * 1_000), entry)

        # Wrap entry as length-delimited field 1 in the batch message
        _encode_tag(1, 2, batch)
        _encode_varint(len(entry), batch)
        batch.extend(entry)

    # sdk_version (field 2)
    _encode_string_field(2, _SDK_VERSION, batch)

    # runtime_version (field 3) — Python version instead of Node.js version
    _encode_string_field(3, platform.python_version(), batch)

    return bytes(batch)
