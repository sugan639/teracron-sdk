# Teracron SDK for Python

Encrypted memory metrics agent for Python applications. Collects RSS, VMS, USS, and CPU usage — encrypts with RSA-4096 + AES-256-GCM — and ships to the Teracron ingest endpoint.

## Installation

```bash
pip install teracron-sdk
```

## Quick Start

**1.** Copy your API key from the Teracron dashboard → Settings → SDK Setup.

**2.** Add it to your `.env`:

```bash
TERACRON_API_KEY=tcn_dml2aWQta3VkdS02NTU6LS0t...
```

**3.** One line in your app:

```python
import teracron
teracron.up()
```

That's it. One env var, one line of code. Metrics flow in a background daemon thread — shutdown is automatic via `atexit`.

---

## Standalone Agent (sidecar)

Run alongside your web server without touching app code:

```bash
export TERACRON_API_KEY="tcn_..."
export TERACRON_TARGET_PID=$(pgrep -f "gunicorn")
teracron-agent
```

## Configuration

| Parameter | Env Variable | Default | Description |
|---|---|---|---|
| `api_key` | `TERACRON_API_KEY` | *required* | API key from dashboard (encodes slug + public key) |
| `domain` | `TERACRON_DOMAIN` | `www.teracron.com` | Ingest endpoint domain |
| `interval_s` | `TERACRON_INTERVAL` | `30` | Collection interval in seconds (5–300) |
| `max_buffer_size` | `TERACRON_MAX_BUFFER` | `60` | Max buffered snapshots before flush |
| `timeout_s` | `TERACRON_TIMEOUT` | `10` | HTTP request timeout in seconds (2–30) |
| `debug` | `TERACRON_DEBUG` | `false` | Enable debug logging to stderr |
| `target_pid` | `TERACRON_TARGET_PID` | `None` (self) | PID of target process to monitor |

Pass overrides to `up()` if needed:

```python
teracron.up(interval_s=10, debug=True)
```

## API Reference

### `teracron.up(**kwargs) → TeracronClient`

Start telemetry. Reads `TERACRON_API_KEY` from env. Idempotent — calling again returns the same instance.

### `teracron.down()`

Stop the agent. Performs a final flush. Safe to call even if `up()` was never called.

### Advanced: `TeracronClient`

For full control (multiple instances, custom lifecycle):

```python
from teracron import TeracronClient

client = TeracronClient(api_key=os.environ["TERACRON_API_KEY"])
client.start()
# ...
client.stop()
```

## Framework Examples

### Flask

```python
from flask import Flask
import teracron

app = Flask(__name__)
teracron.up()

@app.route("/")
def index():
    return "Hello, World!"
```

### FastAPI

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
import teracron

@asynccontextmanager
async def lifespan(app: FastAPI):
    teracron.up()
    yield
    teracron.down()

app = FastAPI(lifespan=lifespan)
```

### Django

```python
# settings.py or AppConfig.ready()
import teracron
teracron.up()
```

## Memory Mapping

Python doesn't have a V8 heap. The SDK maps Python memory concepts to the Teracron schema:

| Teracron Field | Python Source | Description |
|---|---|---|
| `heapTotal` | `psutil.Process.memory_info().vms` | Virtual memory size |
| `heapUsed` | `psutil.Process.memory_full_info().uss` | Unique set size (closest to heap used) |
| `rss` | `psutil.Process.memory_info().rss` | Resident set size |
| `external` | `0` | Not applicable in Python |
| `arrayBuffers` | `0` | Not applicable in Python |
| `cpuUsagePct` | `psutil.Process.cpu_percent()` | CPU usage (0–100%) |

## Security

- **RSA-4096 OAEP + AES-256-GCM** hybrid encryption — same as the Node.js SDK.
- The API key contains ONLY the **public key** — no secrets.
- Ephemeral AES key + IV are generated per flush — no key reuse.
- AES key material is zeroed immediately after encryption.
- All traffic over **HTTPS** (TLS 1.2+).
- The SDK **never** logs PII, keys, or metric payloads.

## Requirements

- Python >= 3.9
- `psutil` >= 5.9
- `cryptography` >= 42.0.4
- `requests` >= 2.32.0

## License

Apache 2.0
