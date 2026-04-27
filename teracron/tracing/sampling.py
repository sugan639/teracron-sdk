# -*- coding: utf-8 -*-
"""
Deterministic hash-based trace sampling.

Sampling is **all-or-nothing per trace**: the decision is made once at
the trace root and all spans within that trace follow the same decision.

The algorithm hashes the ``trace_id`` to a deterministic uint64, then
compares against ``rate * MAX_UINT64``.  This ensures:

    - Same ``trace_id`` → same decision (idempotent across services).
    - Uniform distribution (within statistical bounds).
    - Zero entropy consumption (no RNG calls).
    - O(1) constant time, zero allocations.

Thread-safety: pure function, no shared mutable state.
"""

from __future__ import annotations

import hashlib
import struct
from contextvars import ContextVar
from typing import Optional

# Max unsigned 64-bit integer — comparison ceiling for rate threshold.
_MAX_UINT64 = (1 << 64) - 1

# ContextVar tracking whether the current trace is sampled.
# None = no decision yet (first span should decide).
# True = sampled (record spans).
# False = not sampled (skip spans).
_sampled_var: ContextVar[Optional[bool]] = ContextVar(
    "teracron_trace_sampled", default=None
)


def should_sample(trace_id: str, rate: float) -> bool:
    """
    Determine whether a trace should be sampled.

    Uses MD5 of ``trace_id`` truncated to 8 bytes → uint64.  MD5 is
    chosen for speed and uniformity — **not** for cryptographic security
    (this is a sampling decision, not a security boundary).

    Args:
        trace_id: 32-char hex trace identifier.
        rate:     Sampling rate in [0.0, 1.0].
                  0.0 = drop all, 1.0 = keep all.

    Returns:
        True if the trace should be recorded, False to skip.
    """
    # Fast-path: boundary values avoid hashing entirely.
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False

    # MD5 → first 8 bytes → uint64 (big-endian).
    # NOT cryptographic — used solely for uniform hash distribution in sampling.
    digest = hashlib.md5(trace_id.encode("ascii", errors="replace")).digest()  # nosec B303
    hash_val = struct.unpack(">Q", digest[:8])[0]

    threshold = int(rate * _MAX_UINT64)
    return hash_val <= threshold


def get_sampling_decision() -> Optional[bool]:
    """Return the current trace's sampling decision, or None if undecided."""
    return _sampled_var.get()


def set_sampling_decision(sampled: bool) -> None:
    """Record the sampling decision for the current trace."""
    _sampled_var.set(sampled)


def clear_sampling_decision() -> None:
    """Reset sampling state (called when trace context is cleared)."""
    _sampled_var.set(None)
