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

## Workflow Tracing

Track method execution across your application with the `@trace` decorator. Captures timing, success/failure, exceptions, and nested call trees — ships structured spans to Teracron.

### Basic Usage

```python
import teracron
from teracron import trace

teracron.up()

@trace("payment")
def create_order(cart):
    ...

@trace("payment")
async def charge_card(order_id, amount):
    ...

@trace("payment")
def send_receipt(order_id):
    ...
```

Each decorated function produces a **span** — when an exception occurs, the span records `error_type` and `error_message` automatically. **Exceptions are never swallowed** — they always re-raise.

### Nested Spans (Parent-Child)

Nested `@trace` calls automatically build a call tree with `parent_span_id`:

```python
@trace("payment")
def process_payment(cart):
    order = create_order(cart)          # child span
    charge_card(order.id, cart.total)   # child span
    send_receipt(order.id)              # child span
```

All spans in the same call chain share a `trace_id`. The Teracron dashboard renders this as a waterfall timeline.

### Parameter Capture (Opt-In)

**By default, NO function parameter values are sent to Teracron.** Only basic flow data (timing, status, errors) is traced. To capture specific parameter values, explicitly whitelist them:

```python
@trace("payment", capture=["order_id", "amount"])
def charge_card(order_id, amount, card_number):
    # order_id and amount are captured — card_number is NOT
    ...
```

This is the **PII safety boundary** — sensitive data like passwords, tokens, and card numbers are never sent unless you explicitly list them in `capture=[...]`.

### Context Manager

For tracing non-function code blocks, use the context manager:

```python
from teracron import trace_context, async_trace_context

with trace_context("payment", operation="validate") as span:
    span.set_metadata({"order_id": "ORD-123", "region": "us-east"})
    validate_order(order)

# Async version
async with async_trace_context("payment", operation="verify") as span:
    span.set_metadata({"txn_id": "T-001"})
    await verify_payment(txn)
```

### Cross-Process Propagation

Propagate trace context across HTTP boundaries (microservices, Celery, etc.):

```python
from teracron import get_trace_header, set_trace_header

# Service A — outbound request
headers["X-Teracron-Trace"] = get_trace_header()

# Service B — inbound request
set_trace_header(request.headers.get("X-Teracron-Trace"))
```

### Sampling

Control what percentage of traces are recorded. Decision is deterministic per `trace_id` — same trace always gets the same decision across services.

```python
teracron.up(trace_sample_rate=0.1)  # Record 10% of traces
```

When a trace is not sampled, functions still execute normally and exceptions still propagate. Only telemetry recording is skipped — zero overhead for non-sampled traces.

### PII Scrubber

Provide a callable to scrub sensitive data from metadata and captured params before they leave your application:

```python
def my_scrubber(data: dict) -> dict:
    data.pop("email", None)
    data.pop("ssn", None)
    data.pop("auth_token", None)
    return data

teracron.up(tracing_scrubber=my_scrubber)
```

The scrubber receives a **shallow copy** of the data dict — your original data is never mutated. If the scrubber raises an exception, the data is **dropped entirely** (never leaked).

### Framework Auto-Instrumentation

#### FastAPI / Starlette

```python
from fastapi import FastAPI
from teracron.tracing.middleware.fastapi import TeracronTracingMiddleware

app = FastAPI()
app.add_middleware(TeracronTracingMiddleware, workflow="api")
```

Auto-traces every HTTP request with `http.method`, `http.path`, and `http.status_code` metadata. Extracts/injects `X-Teracron-Trace` headers automatically.

#### Django

```python
# settings.py
MIDDLEWARE = [
    "teracron.tracing.middleware.django.TeracronTracingMiddleware",
    # ... other middleware
]
TERACRON_WORKFLOW = "api"  # optional, default: "http"
```

#### Celery

```python
from celery import Celery
from teracron.tracing.middleware.celery import setup_celery_tracing

app = Celery("tasks")
setup_celery_tracing(app, workflow="tasks")
```

Propagates trace context through task headers. Auto-creates a span per task execution with `celery.task_id` metadata.

### Tracing Configuration

| Parameter | Env Variable | Default | Description |
|---|---|---|---|
| `tracing_enabled` | `TERACRON_TRACING_ENABLED` | `true` | Master kill-switch for tracing |
| `trace_batch_size` | `TERACRON_TRACE_BATCH_SIZE` | `100` | Max spans buffered before flush (1–10,000) |
| `trace_flush_interval` | `TERACRON_TRACE_FLUSH_INTERVAL` | `10` | Seconds between trace flushes (1–300) |
| `trace_sample_rate` | `TERACRON_TRACE_SAMPLE_RATE` | `1.0` | Sampling rate (0.0–1.0). Deterministic per trace. |
| `tracing_scrubber` | — | `None` | Callable for PII scrubbing. Applied before buffering. |

```python
teracron.up(
    tracing_enabled=True,
    trace_batch_size=50,
    trace_flush_interval=5.0,
    trace_sample_rate=0.5,
    tracing_scrubber=my_scrubber,
)
```

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
| `interval_s` | `TERACRON_INTERVAL` | `10` | Collection interval in seconds (5–300) |
| `max_buffer_size` | `TERACRON_MAX_BUFFER` | `10` | Max buffered snapshots before flush |
| `flush_deadline_s` | `TERACRON_FLUSH_DEADLINE` | `60` | Max seconds before forcing a flush (10–600) |
| `timeout_s` | `TERACRON_TIMEOUT` | `10` | HTTP request timeout in seconds (2–30) |
| `debug` | `TERACRON_DEBUG` | `false` | Enable debug logging to stderr |
| `target_pid` | `TERACRON_TARGET_PID` | `None` (self) | PID of target process to monitor |

Pass overrides to `up()` if needed:

```python
teracron.up(interval_s=15, debug=True)
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
