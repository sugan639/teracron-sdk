# -*- coding: utf-8 -*-
"""
Teracron SDK for Python -- encrypted memory metrics agent.

Quick start (one line)::

    import teracron
    teracron.up()

That's it. Reads ``TERACRON_API_KEY`` from your environment, spawns a
background daemon thread, and starts shipping encrypted metrics. Shutdown
is automatic via ``atexit``.

Explicit shutdown::

    teracron.down()

Standalone CLI agent::

    $ export TERACRON_API_KEY="tcn_..."
    $ teracron-agent
"""

__version__ = "0.1.0"

from .client import TeracronClient, up, down
from .apikey import encode_api_key, decode_api_key
from .types import FlushResult, MetricsSnapshot, ResolvedConfig

__all__ = [
    # Primary API -- one call to start
    "up",
    "down",
    # Advanced / explicit
    "TeracronClient",
    # Types
    "FlushResult",
    "MetricsSnapshot",
    "ResolvedConfig",
    # Utilities
    "encode_api_key",
    "decode_api_key",
    "__version__",
]
