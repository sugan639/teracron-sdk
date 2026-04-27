# Teracron Workflow Tracing — Implementation Plan

> **Version:** 0.6  
> **Date:** 2025-01-20 (updated 2025-07-21)  
> **SDK Target:** teracron-sdk `0.6.0` (current release)
> **Backend Target:** teracron `TBD`
> **Status:** Phase 1 ✅ Phase 2 ✅ Phase 3 ✅ Phase 4 ✅ — SDK agent-ready

---

## Target

A `@trace("workflow_name")` decorator that captures method execution flow (timing, success/failure, exceptions) and ships structured span data to Teracron — giving users a clean process timeline instead of digging through logs.

**Not building:** a general-purpose APM, an OpenTelemetry replacement, or a log aggregator.

---

## Decisions (Locked)

| # | Decision | Answer |
|---|---|---|
| Q1 | Endpoint design | **Dedicated `POST /v1/traces`** — traces are structurally different from metrics/events. Separate endpoint, clean contract, independent rate limits. |
| Q2 | Auto-start or explicit init | **Explicit `teracron.up()`** — if user decorates without init, raise a clear error: *"call `teracron.up()` before using `@trace`"*. No silent swallowing. |
| Q3 | Buffer strategy | **Separate buffer** for traces — isolated from metrics. Different data shapes, different flush destinations, independent failure domains. |
| Q4 | Flush limits | **100 spans per batch, flush every 10 seconds** (whichever comes first). Both configurable via `teracron.up(trace_batch_size=100, trace_flush_interval=10)`. |
| Q5 | Overflow policy | **Drop oldest + warn once.** Ring buffer. SDK never blocks or slows user's application. One warning log when drops begin, then silent. |
| Q6 | Sampling strategy | **All-or-nothing per trace.** Decision made at trace root (first `@trace` call). All spans in a sampled trace are kept. Hash-based deterministic sampling on `trace_id`. |
| Q7 | PII scrubber | **User-provided callable.** Applied to `metadata` and `captured_params` dicts before buffering. Default: passthrough (`None`). Scrubber receives a shallow copy — can mutate or return replacement dict. Exception in scrubber → data dropped (never leaked). |
| Q8 | Middleware scope | **Auto-instrumentation only for request/response lifecycle.** Middleware creates a root span per request and propagates trace context. Business-logic tracing still requires explicit `@trace`. |

---

## User-Facing API (Complete — Phase 1+2+3)

```python
import teracron
from teracron.tracing import trace

teracron.up(api_key="tcn_...")

# Phase 1 — flat spans, auto-correlated within a thread/async context
@trace("payment")
def create_order(cart):
    ...

@trace("payment")
async def charge_card(order_id, amount):
    ...

# Phase 2 — nested spans (auto-detected via context)
@trace("payment")
def process_payment(cart):
    order = create_order(cart)          # child span
    charge_card(order.id, order.total)  # child span

# Phase 2 — context manager
from teracron.tracing import trace_context

with trace_context("payment", operation="validate") as span:
    span.set_metadata({"order_id": "ORD-123"})

# Phase 2 — opt-in parameter capture (PII safety boundary)
@trace("payment", capture=["order_id", "amount"])
def charge_card(order_id, amount, card_number):
    # order_id + amount captured; card_number is NEVER sent
    ...

# Phase 2 — cross-process propagation
from teracron.tracing import get_trace_header, set_trace_header
headers["X-Teracron-Trace"] = get_trace_header()
set_trace_header(request.headers.get("X-Teracron-Trace"))

# Phase 3 — sampling (1.0 = capture all, 0.1 = 10% of traces)
teracron.up(trace_sample_rate=0.5)

# Phase 3 — PII scrubber hook
def my_scrubber(data: dict) -> dict:
    data.pop("email", None)
    data.pop("ssn", None)
    return data

teracron.up(tracing_scrubber=my_scrubber)

# Phase 3 — FastAPI auto-instrumentation
from teracron.tracing.middleware.fastapi import TeracronTracingMiddleware
app.add_middleware(TeracronTracingMiddleware, workflow="api")

# Phase 3 — Django auto-instrumentation
# settings.py
MIDDLEWARE = [
    "teracron.tracing.middleware.django.TeracronTracingMiddleware",
    ...
]

# Phase 3 — Celery auto-instrumentation
from teracron.tracing.middleware.celery import setup_celery_tracing
setup_celery_tracing(app, workflow="tasks")
```

---

## Data Model

### Span (single method execution)

| Field | Type | Source | Description |
|---|---|---|---|
| `trace_id` | `str` | Auto-generated per root span | Groups all spans in one workflow execution |
| `span_id` | `str` | Auto-generated per span | Unique identifier for this method call |
| `parent_span_id` | `str \| null` | Auto from context | Enables call-tree reconstruction |
| `workflow` | `str` | User-provided in `@trace("name")` | Logical process name |
| `operation` | `str` | Auto from `func.__qualname__` | Method/function name |
| `status` | `enum` | Auto | `started` · `succeeded` · `failed` |
| `started_at` | `int` | Auto (Unix ms) | Wall-clock start time |
| `duration_ms` | `float` | Auto (`time.monotonic()` delta) | Execution wall-clock time |
| `error_type` | `str \| null` | Auto from exception | `ValueError`, `TimeoutError`, etc. |
| `error_message` | `str \| null` | Auto from exception | Exception message (max 1024 chars) |
| `metadata` | `dict \| null` | User-provided | Custom KV pairs (max 32 keys, primitives only) |
| `captured_params` | `dict \| null` | Opt-in via `capture=[...]` | Whitelisted parameter values (max 512 char per value) |

---

## Architecture

```
teracron/
├── __init__.py              # re-export: all public APIs  v0.6.0
├── client.py                # trace buffer + trace flush path + sampling + scrubber
├── config.py                # tracing config fields + sampling + scrubber + auth constants
├── types.py                 # Span, WorkflowEvent, WorkflowRun, SimulationResult, AuthToken
├── transport.py             # /v1/traces + /v1/events endpoint support
├── auth.py                  # CLI auth: login/logout/whoami, credential storage (Phase 4)
├── query.py                 # Read-only query client: events, traces, workflows (Phase 4)
├── simulate.py              # Failure simulation engine: replay, diagnosis, repro (Phase 4)
├── .agent-skill.md          # Root agent skill reference (Phase 4)
├── tracing/
│   ├── __init__.py          # public API exports
│   ├── .agent-skill.md      # Full agent skill reference (Phase 4)
│   ├── decorator.py         # @trace — sync + async, span lifecycle, event emission
│   ├── context.py           # contextvars: trace_id + span stack + propagation
│   ├── span.py              # Span builder, sanitisation
│   ├── sampling.py          # Deterministic hash-based sampling + ContextVar
│   ├── events.py            # Structured workflow event emitter + EventBuffer (Phase 4)
│   └── middleware/
│       ├── __init__.py
│       ├── fastapi.py       # FastAPI/Starlette ASGI middleware
│       ├── django.py        # Django WSGI middleware
│       └── celery.py        # Celery signal hooks (4 signals)
├── collector.py             # psutil memory/CPU metrics
├── crypto.py                # RSA-4096 + AES-256-GCM encryption
├── encoder.py               # zero-dependency protobuf encoder
├── apikey.py                # tcn_ API key encode/decode
└── cli.py                   # teracron-agent CLI (9 subcommands, Phase 4)
```

---

## Security Architecture (5-Layer Defence)

```
┌─────────────────────────────────────────────────────────────────────┐
│ Layer 1: Sampling     │ trace_sample_rate=0.5 → 50% of traces      │
│                       │ skip the entire pipeline. Zero allocation.  │
├───────────────────────┼─────────────────────────────────────────────┤
│ Layer 2: Capture      │ capture=["order_id"] → ONLY order_id        │
│   whitelist           │ extracted. card_number, cvv: NEVER.         │
│                       │ Default: NO params captured.                │
├───────────────────────┼─────────────────────────────────────────────┤
│ Layer 3: Scrubber     │ tracing_scrubber=my_fn → user callable      │
│                       │ mutates metadata/params before buffering.   │
│                       │ Exception → data DROPPED (never leaked).    │
├───────────────────────┼─────────────────────────────────────────────┤
│ Layer 4: Sanitise     │ _sanitise_captured_params() → type-check,   │
│                       │ truncate strings (512 chars), repr() objs.  │
│                       │ _sanitise_metadata() → max 32 keys,         │
│                       │ 128-char keys, 1024-char values.            │
├───────────────────────┼─────────────────────────────────────────────┤
│ Layer 5: Transport    │ RSA-4096 + AES-256-GCM encryption.          │
│                       │ TLS 1.2+ only. Domain allowlist.            │
└───────────────────────┴─────────────────────────────────────────────┘
```

---

## Phase Breakdown

### Phase 1 — MVP (SDK `0.3.0`) ✅ COMPLETED — 2025-07-20

**Delivered:**
- `@trace("workflow")` decorator (sync + async)
- Flat spans (no nesting, no parent-child)
- `Span` frozen dataclass with `trace_id`, `span_id`, `workflow`, `operation`, `status`, `started_at`, `duration_ms`, `error_type`, `error_message`
- Separate trace buffer with ring-buffer overflow (drop oldest + warn once)
- Flush to dedicated `POST /v1/traces` endpoint
- Config: `tracing_enabled`, `trace_batch_size` (1–10K), `trace_flush_interval` (1–300s)
- Env vars: `TERACRON_TRACING_ENABLED`, `TERACRON_TRACE_BATCH_SIZE`, `TERACRON_TRACE_FLUSH_INTERVAL`
- `teracron.tracing` subpackage: `context.py`, `span.py`, `decorator.py`
- 5 new test modules, all passing

**Test count after Phase 1:** 152 tests

### Phase 2 — Depth (SDK `0.4.0`) ✅ COMPLETED — 2025-07-20

**Delivered:**
- Nested spans / parent-child via `ContextVar`-based span stack (`push_span`, `pop_span`, `peek_parent_span_id`)
- `trace_context` sync + `async_trace_context` async context managers yielding `SpanHandle`
- `SpanHandle.set_metadata()` with type validation + size limits (32 keys, 128-char keys, 1024-char values)
- Opt-in parameter capture: `@trace("wf", capture=["param1"])` — only whitelisted params extracted via `inspect.signature` binding. Default: NO params captured.
- Sanitisation: `_sanitise_captured_params()` — type-check, truncate strings to 512 chars, `repr()` complex types
- Cross-process propagation: `get_trace_header()` / `set_trace_header()` with `X-Teracron-Trace` header (format: `<trace_id>:<parent_span_id>`, hex-validated)
- Error message truncation to 1024 chars
- 3 new test modules (76 new tests)

**Test count after Phase 2:** 258 tests

### Phase 3 — Ecosystem (SDK `0.5.0`) ✅ COMPLETED — 2025-07-21

**Delivered:**
- **Deterministic sampling** — `teracron/tracing/sampling.py`: `should_sample(trace_id, rate)` using MD5 hash → uint64 → threshold comparison. O(1), zero allocations, no RNG. Decision at trace root via `ContextVar`, inherited by all children (all-or-nothing per trace).
- **Sampling config** — `trace_sample_rate: float` (0.0–1.0, default 1.0). Env var: `TERACRON_TRACE_SAMPLE_RATE`. Clamped at resolution. Non-sampled spans never buffered (param extraction also skipped).
- **PII scrubber hook** — `tracing_scrubber: Callable[[dict], dict]`. Applied to `metadata` and `captured_params` via `_apply_scrubber()` in `_end_span` before buffering. Receives shallow copy. Exception → data dropped (PII never leaked). Validated as callable at config time.
- **FastAPI ASGI middleware** — `teracron.tracing.middleware.fastapi.TeracronTracingMiddleware`. Auto root span per request. Records `http.method`, `http.path`, `http.status_code` metadata. Extracts inbound `X-Teracron-Trace` header, injects in response. Respects sampling + scrubber.
- **Django WSGI middleware** — `teracron.tracing.middleware.django.TeracronTracingMiddleware`. Same semantics. Workflow configurable via `TERACRON_WORKFLOW` Django setting. Reads `HTTP_X_TERACRON_TRACE` from `request.META`.
- **Celery signal hooks** — `setup_celery_tracing(app, workflow="celery")`. 4 signals: `before_task_publish` (inject header), `task_prerun` (restore context + create span), `task_failure` (record error), `task_postrun` (finalise span). Records `celery.task_id`, `celery.state` metadata. Task-local span state keyed by `task_id`.
- **7 new test modules** (93 new tests): `test_sampling.py`, `test_scrubber.py`, `test_sampling_integration.py`, `test_middleware_fastapi.py`, `test_middleware_django.py`, `test_middleware_celery.py`, `test_phase3_config.py`

**Test count after Phase 3:** 351 tests (0 failures, 0 regressions)

---

## Task Breakdown — All Phases

### Phase 1 Tasks (S1–S15) ✅

| # | Task | Status |
|---|---|---|
| S1 | `Span` frozen dataclass + `to_dict()` | ✅ |
| S2 | `context.py` — `ContextVar` trace ID propagation | ✅ |
| S3 | `span.py` — `create_span()` factory | ✅ |
| S4 | `decorator.py` — `@trace` sync + async | ✅ |
| S5 | Trace buffer (ring buffer, `collections.deque`) | ✅ |
| S6 | Trace flush loop (batch size + interval) | ✅ |
| S7 | Config fields: `tracing_enabled`, `trace_batch_size`, `trace_flush_interval` | ✅ |
| S8 | Transport: `POST /v1/traces` | ✅ |
| S9 | Overflow policy: drop oldest + warn once | ✅ |
| S10 | Explicit init guard (`RuntimeError` if no `teracron.up()`) | ✅ |
| S11–S15 | Tests + docs + changelog | ✅ |

### Phase 2 Tasks (S16–S20) ✅

| # | Task | Status |
|---|---|---|
| S16 | Span stack (`push_span`, `pop_span`, `peek_parent_span_id`) | ✅ |
| S17 | `trace_context` + `async_trace_context` context managers | ✅ |
| S18 | Metadata support (`SpanHandle.set_metadata()`, sanitisation) | ✅ |
| S19 | Cross-process propagation (`get_trace_header`, `set_trace_header`) | ✅ |
| S20 | Opt-in parameter capture (`capture=[...]`) + PII safety boundary | ✅ |

### Phase 3 Tasks (S21–S35) ✅

| # | Task | Status | Detail |
|---|---|---|---|
| S21 | Sampling module | ✅ | `tracing/sampling.py` — MD5 hash → uint64, `ContextVar` decision propagation |
| S22 | Sampling config | ✅ | `trace_sample_rate` in `ResolvedConfig`, env var, clamped [0.0, 1.0] |
| S23 | Sampling integration | ✅ | `_begin_span` returns `sampled` flag, `_end_span` skips non-sampled |
| S24 | PII scrubber hook | ✅ | `_apply_scrubber()` — shallow copy, exception → drop data |
| S25 | PII scrubber config | ✅ | `tracing_scrubber` in `ResolvedConfig`, callable validation at config time |
| S26 | FastAPI middleware | ✅ | ASGI wrapper, header extract/inject, method/path/status metadata |
| S27 | Django middleware | ✅ | WSGI class, `TERACRON_WORKFLOW` setting, `HTTP_X_TERACRON_TRACE` |
| S28 | Celery signal hooks | ✅ | 4 signals, header propagation, task-local span state |
| S29 | Tests — sampling | ✅ | Deterministic, boundary values, distribution ±5% over 10K |
| S30 | Tests — scrubber | ✅ | Applied, exception caught, passthrough, mutation, new dict |
| S31 | Tests — FastAPI | ✅ | Auto-span, header extraction, status metadata, error recording |
| S32 | Tests — Django | ✅ | Same for Django |
| S33 | Tests — Celery | ✅ | Header propagation, task spans, error recording |
| S34 | README update | ✅ | Sampling, scrubber, middleware sections |
| S35 | CHANGELOG `0.5.0` | ✅ | Full Phase 3 changelog |

---

## Success Criteria — Final Verification

### Phase 1 ✅ Verified

- Flat spans, buffer, flush, config — all working.
- 152 tests passing.

### Phase 2 ✅ Verified

- Nested spans, context managers, metadata, parameter capture, cross-process propagation.
- PII boundary enforced (default: NO params captured).
- 258 tests passing (zero regression on Phase 1).

### Phase 3 ✅ Verified — 2025-07-21

| # | Criterion | Result |
|---|---|---|
| 1 | `trace_sample_rate=0.5` samples ~50% (±5% over 10K) | ✅ Verified in `test_sampling.py::test_distribution_roughly_uniform` |
| 2 | `tracing_scrubber` applied to metadata + captured_params; exceptions never crash | ✅ Verified in `test_scrubber.py` (15 tests) |
| 3 | FastAPI middleware auto-creates root span with method/path/status_code | ✅ Verified in `test_middleware_fastapi.py` (10 tests) |
| 4 | Django middleware does the same | ✅ Verified in `test_middleware_django.py` (8 tests) |
| 5 | Celery hooks propagate trace context + auto-create task spans | ✅ Verified in `test_middleware_celery.py` (6 tests) |
| 6 | All middleware extracts/injects `X-Teracron-Trace` headers | ✅ Verified in middleware tests |
| 7 | Zero regression on existing tests | ✅ 258 pre-existing tests still pass |
| 8 | All new Phase 3 tests pass | ✅ 93 new tests pass |

**Final test suite: 351 passed, 0 failed, 0 errors.**

---

## What Gets Hardened Later (Post-0.5.0)

**Performance & Memory:**
- `__slots__` on `Span` dataclass for memory optimization
- Decorator overhead benchmarking (target: < 10µs)
- `copy_context()` integration for `ThreadPoolExecutor` propagation
- Pre-allocated span pools for high-throughput services

**Features:**
- gRPC interceptor middleware
- ASGI lifespan event tracing
- Conditional span finalization (early abort)
- Span events (sub-span log entries)
- Trace-based alerting SDK hooks

**Quality:**
- Import-time side-effect audit
- Fuzz testing on header parsing + config resolution
- Integration tests against live Teracron backend
- Python 3.12+ `TaskGroup` trace propagation testing

---

### Phase 4 — Agent & Workflow Page (SDK `0.6.0`) ✅ COMPLETED — 2026-04-27

**Delivered:**
- **CLI authentication** — `teracron-agent login/logout/whoami` with credential storage at `~/.teracron/credentials.json` (mode 0600). API key resolution chain: CLI flag → env var → stored credentials. Secure wipe on logout (overwrite with zeros before unlink).
- **CLI subcommand architecture** — `argparse`-based router with 9 subcommands: `run`, `login`, `logout`, `whoami`, `events`, `workflows`, `trace`, `simulate`, `curl-example`. Backward-compatible: no subcommand = `run`.
- **Read-only query client** — `TeracronQueryClient` with `list_events()`, `get_trace()`, `list_workflows()`, `get_span()`. Bearer token auth. Graceful HTTP error handling (401, 404, 429). SDK ready for backend endpoints not yet deployed.
- **Failure simulation engine** — `FailureSimulator` with `fetch_failure_context()`, `generate_repro_script()`, `print_diagnosis()`. Never executes code — generates inert artifacts for AI agent consumption.
- **Structured workflow events** — `teracron/tracing/events.py` with `build_event()`, convenience builders, `EventBuffer`. Auto-emission from `@trace` when `trace_emit_events=True`. Event types: workflow_started/completed/failed, step_started/completed/failed, retry.
- **Agent skill files** — Complete AI agent reference at `teracron/tracing/.agent-skill.md` and `teracron/.agent-skill.md` with curl examples, JSON schemas, decision tree, error handling guide.
- **Transport GET support** — `send_events()` and `query_base_url` property on `Transport`.
- **New types** — `WorkflowEvent`, `WorkflowRun`, `SimulationResult`, `AuthToken` dataclasses.
- **`--json` output** — All CLI commands support machine-readable JSON output.
- **Config additions** — `trace_emit_events` flag (env: `TERACRON_TRACE_EMIT_EVENTS`).
- **5 new test modules** (128 new tests): `test_auth.py`, `test_query.py`, `test_simulate.py`, `test_cli_commands.py`, `test_events.py`

**Test count after Phase 4:** 479 tests (0 failures, 0 regressions)

### Phase 4 Tasks (S36–S50) ✅

| # | Task | Status | Detail |
|---|---|---|---|
| S36 | `auth.py` — credential storage + login/logout/whoami | ✅ | `~/.teracron/credentials.json`, mode 0600, secure wipe, key masking |
| S37 | CLI subcommand rewrite | ✅ | argparse router, 9 subcommands, backward compat (no subcmd = run) |
| S38 | Config — auth constants + `trace_emit_events` | ✅ | `API_BASE_PATH`, `CREDENTIALS_DIR`, `resolve_api_base_url()` |
| S39 | `query.py` — read-only query client | ✅ | `TeracronQueryClient`, Bearer auth, 404/401/429 handling |
| S40 | Transport GET support | ✅ | `send_events()`, `query_base_url` property |
| S41 | New types | ✅ | `WorkflowEvent`, `WorkflowRun`, `SimulationResult`, `AuthToken` |
| S42 | `tracing/events.py` — structured events | ✅ | `build_event()`, convenience builders, `EventBuffer` ring buffer |
| S43 | `@trace` auto-emit events | ✅ | `_emit_start_event()`, `_emit_end_event()` when `trace_emit_events=True` |
| S44 | `simulate.py` — failure replay | ✅ | `FailureSimulator`, context extraction, repro script, markdown diagnosis |
| S45 | Agent skill file — full rewrite | ✅ | curl examples, JSON schemas, decision tree, error handling |
| S46 | Root agent skill file | ✅ | Quick reference at `teracron/.agent-skill.md` |
| S47 | Tests — auth | ✅ | 27 tests: storage, permissions, login, logout, masking, priority chain |
| S48 | Tests — query | ✅ | 27 tests: construction, headers, responses, input validation |
| S49 | Tests — simulate | ✅ | 14 tests: context extraction, repro script, diagnosis, errors |
| S50 | Tests — CLI + events | ✅ | 60 tests: parser, subcommands, JSON output, event building, buffer |

### Phase 4 Success Criteria ✅ Verified — 2026-04-27

| # | Criterion | Result |
|---|---|---|
| 1 | `teracron-agent login` stores credentials at `~/.teracron/credentials.json` with 0600 | ✅ Verified in `test_auth.py` |
| 2 | `teracron-agent whoami` shows auth status (file + env var source) | ✅ Verified in `test_cli_commands.py` |
| 3 | `teracron-agent events --status=failed --json` returns structured JSON | ✅ Verified in `test_query.py` + `test_cli_commands.py` |
| 4 | `teracron-agent trace <id>` fetches full span tree | ✅ Verified in `test_query.py` |
| 5 | `teracron-agent simulate <id> --format=markdown` produces diagnosis | ✅ Verified in `test_simulate.py` |
| 6 | `teracron-agent simulate <id> --format=script` generates Python repro script | ✅ Verified in `test_simulate.py` |
| 7 | curl examples include Bearer auth header | ✅ Verified in `test_cli_commands.py::TestCurlExampleCommand` |
| 8 | API key never printed in full (masked in all output) | ✅ Verified in `test_auth.py::TestMaskApiKey` |
| 9 | Expired credentials return `None` | ✅ Verified in `test_auth.py::TestCredentialStorage::test_expired_credentials` |
| 10 | Backend 404 returns graceful error + hint | ✅ Verified in `test_query.py::TestErrorResponses::test_404_not_deployed` |
| 11 | Zero regression on existing 351 tests | ✅ All 351 pass |
| 12 | All 128 new Phase 4 tests pass | ✅ 128 passed |

**Final test suite: 479 passed, 0 failed, 0 errors.**

