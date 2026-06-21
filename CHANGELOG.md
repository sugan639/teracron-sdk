# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.1] - 2026-06-21

### Added
- **Development / Production mode split** — every Teracron project now has two modes (`development`, `production`) and the mode is encoded directly in the API key (`tcn_<base64url(short-mode:slug:publicKeyPEM)>`). The SDK decodes the mode in `teracron/apikey.py` (`decode_api_key`) and `teracron/config.py` (`resolve_config` → `ResolvedConfig.mode`), and the transport attaches it as the **`X-Project-Mode`** HTTP header on every metrics / traces / events request. Switching modes = swapping the env var; no code changes required. **Backward compatible**: legacy keys without a mode prefix decode to `production` (the safe default). Design reference: `DEV_PROD_MODE.md`.
- **Local interface (`teracron/local_interface.py`)** — a dependency-free (stdlib `http.server`) loopback stand-in for `teracron.com`, intended for local development. Accepts the same three routes as production (`/api/ingest`, `/api/v1/traces`, `/api/v1/events`) with the same validation rules and size caps, and spools the still-encrypted envelopes to `~/.teracron/local-spool/` for the local Teracron to consume. Binds to loopback only by default; refuses non-loopback hosts unless explicitly forced (`allow_non_loopback=True`). Exposed as `teracron.run_local_interface(...)` and `teracron-agent local-interface` CLI subcommand. Design reference: `LOCAL_INTERFACE.md`.
- **Local Teracron spool consumer (`teracron/local_teracron.py`)** — watches `~/.teracron/local-spool/` for sidecars written by the local interface, then forwards the encrypted envelopes (unchanged) over HTTPS to Convex's `.convex.site` ingest endpoints with the original `X-Project-Slug` and `X-Project-Mode` headers. **Never decrypts** — the private key stays server-side in Convex. Processed envelopes are archived to `processed/`; permanently-rejected envelopes are quarantined. Exposed as `teracron-agent local-teracron` CLI subcommand.
- **Loopback scheme resolution** in `teracron/config.py` — when the configured `teracron_domain` resolves to `127.0.0.1` / `localhost`, the SDK automatically switches to `http://` (plaintext, loopback only) instead of `https://`. Production domains always use `https://`. This lets local-mode integration tests run with no TLS setup while keeping production behaviour unchanged.
- **CLI domain resolution** — `teracron-agent` and the auth flow now honour `TERACRON_DOMAIN` / `--domain` for both REST and Convex endpoints, with explicit precedence (CLI arg > env > default). New test module: `tests/test_cli_domain_resolution.py`.
- **New tests**: `test_local_interface.py`, `test_local_teracron.py`, `test_cli_domain_resolution.py`. Existing modules expanded for the dev/prod mode split: `test_apikey.py`, `test_config.py`, `test_phase2_capture.py`, `test_phase2_nesting.py`, `test_query.py`, `test_scrubber.py`, `test_span.py`, `test_trace_decorator.py`.

### Changed
- `teracron/apikey.py` — `encode_api_key()` / `decode_api_key()` now accept and round-trip a `mode` argument; legacy two-segment keys still decode (mode defaults to `production`).
- `teracron/config.py` — `ResolvedConfig` gained a `mode` field; resolution prefers the mode embedded in the API key, with config-level override available for legacy keys.
- `teracron/transport.py` — sends `X-Project-Mode` on every request alongside `X-Project-Slug`. Adds explicit loopback handling for the local-mode `http://` path.
- `teracron/query.py` — query client now scopes results by `mode`, so dashboard reads never mix dev and prod telemetry.
- `teracron/cli.py` — significantly expanded with subcommands for the local interface, local Teracron consumer, and domain-aware auth flow.
- `teracron/__init__.py` — re-exports `run_local_interface` for ergonomic access.

### Fixed
- Tracing decorator (`teracron/tracing/decorator.py`) and span helpers (`teracron/tracing/span.py`) — minor correctness fixes for nested-span parent resolution under the new mode-aware buffer key.

### Security
- **Mode never travels in source code or in `tcn_…` plaintext.** It is carried as a single short token inside the base64url payload of the API key, alongside the existing slug + public key. No new key material is introduced and no new decryption surface is added.
- **Local interface refuses non-loopback binds by default.** A plaintext ingest endpoint exposed to the network would be unsafe, so binding to `0.0.0.0` requires an explicit `allow_non_loopback=True` opt-in.
- **Local Teracron never decrypts.** The encrypted envelope is replayed byte-for-byte to Convex over HTTPS; the private key stays on the server. Local mode therefore has the same crypto trust boundary as production.
- **Repository URL corrected** in `pyproject.toml` (was pointing at a non-existent GitHub org/repo).

### Documentation
- New: `DEV_PROD_MODE.md` — end-to-end design of the dev/prod split, including the API-key format, header flow, Convex ingest routing, and dashboard filtering.
- New: `LOCAL_INTERFACE.md` — design + implementation reference for the local interface, spool contract, and the loopback scheme resolution.
- Updated `README.md` and the two `.agent-skill.md` files (`teracron/.agent-skill.md`, `teracron/tracing/.agent-skill.md`) so AI agents implementing Teracron support pick up the dev/prod mode automatically.

## [0.6.1] - 2026-04-27

### Fixed
- **Bandit B324 false positive** — added `usedforsecurity=False` to `hashlib.md5()` in `teracron/tracing/sampling.py` (Python ≥3.9) and dual `# nosec B303 B324` suppression for cross-version Bandit compatibility.
- Fixed nosec annotations across `query.py`, `transport.py`, `decorator.py`, and `middleware/django.py` for CI compatibility with Bandit ≥1.8.

## [0.5.0] - 2025-07-21

### Added
- **Deterministic trace sampling** — `trace_sample_rate` config field (0.0–1.0, default 1.0). Decision made once at the trace root using MD5 hash of `trace_id` → uint64 comparison. All spans in a sampled trace are kept (all-or-nothing). Env var: `TERACRON_TRACE_SAMPLE_RATE`.
- **PII scrubber hook** — `tracing_scrubber: Callable[[dict], dict]` config option. User-provided function applied to `metadata` and `captured_params` before buffering. Receives a shallow copy. Default: `None` (passthrough). Scrubber exceptions are caught — never crash the app; data is dropped on failure (PII safety).
- **FastAPI / Starlette ASGI middleware** — `teracron.tracing.middleware.fastapi.TeracronTracingMiddleware`. Auto-traces HTTP requests with `method`, `path`, `status_code` metadata. Extracts/injects `X-Teracron-Trace` header. Respects sampling and scrubber config.
- **Django WSGI middleware** — `teracron.tracing.middleware.django.TeracronTracingMiddleware`. Same semantics as FastAPI middleware. Configurable workflow name via `TERACRON_WORKFLOW` Django setting.
- **Celery signal hooks** — `teracron.tracing.middleware.celery.setup_celery_tracing(app, workflow="tasks")`. Auto-spans per task with `celery.task_id` metadata. Propagates trace context through task headers (`X-Teracron-Trace`). Hooks: `before_task_publish`, `task_prerun`, `task_failure`, `task_postrun`.
- **`teracron.tracing.sampling` module** — `should_sample(trace_id, rate)` pure function for deterministic hash-based sampling. `ContextVar`-based sampling decision propagation across nested spans.
- **7 new test modules:** `test_sampling.py` (20 tests), `test_scrubber.py` (16 tests), `test_sampling_integration.py` (12 tests), `test_middleware_fastapi.py` (10 tests), `test_middleware_django.py` (8 tests), `test_middleware_celery.py` (6 tests), `test_phase3_config.py` (14 tests).

### Changed
- `_begin_span` now accepts `sample_rate` and returns `(span, is_root, t0, sampled)` tuple. Sampling decision is made at root span and inherited by all children.
- `_end_span` now accepts `sampled` kwarg — non-sampled spans are never buffered (zero allocation waste).
- `clear_trace()` now also clears the sampling decision `ContextVar`.
- Captured params are not extracted when trace is not sampled (avoids `inspect.signature` overhead on skipped traces).
- PII scrubber is applied in `_end_span` before `finalise_span` — defence in depth before data ever touches the buffer.

### Security
- **Scrubber exception safety**: If user's scrubber function raises, the data is **dropped** (not passed through) — PII can never leak due to a buggy scrubber.
- **Scrubber receives shallow copy**: Original data passed to `_end_span` is never mutated by the scrubber.
- **Sampling is transparent**: Even when a trace is not sampled, the function still executes normally and exceptions still propagate. Only telemetry recording is skipped.
- **Middleware never crashes the app**: All middleware (FastAPI, Django, Celery) catches internal errors and degrades gracefully — never affects request/response lifecycle.

## [0.4.0] - 2025-07-20

### Added
- **Nested spans (parent-child)** — `@trace` calls within other `@trace` calls automatically set `parent_span_id`, enabling call-tree reconstruction and waterfall timeline views.
- **Span stack** — `ContextVar`-based span stack in `teracron/tracing/context.py` tracks the current parent span. `push_span()` / `pop_span()` / `peek_parent_span_id()` manage nesting.
- **`trace_context` sync context manager** — `with trace_context("workflow", operation="name") as span:` for tracing non-function blocks. Yields a `SpanHandle` with `.set_metadata()`.
- **`async_trace_context` async context manager** — same API as `trace_context` but for async code: `async with async_trace_context(...)`.
- **Metadata support** — `SpanHandle.set_metadata({"key": "value"})` attaches user-defined key-value pairs to spans. Keys must be strings; values must be `str | int | float | bool`. Invalid entries are silently dropped. Hard limits: 32 keys max, 128-char key length, 1024-char value length.
- **Opt-in parameter capture** — `@trace("workflow", capture=["param1", "param2"])` — only explicitly whitelisted function parameter values are extracted and sent to Teracron. **By default, NO parameter values are transmitted.** This is the PII safety boundary.
  - Non-primitive captured values are converted to `repr()` and truncated to 512 chars.
  - Capture uses `inspect.signature` binding, supporting positional, keyword, and default-value arguments.
- **Cross-process trace propagation** — `get_trace_header()` / `set_trace_header()` for propagating trace context via `X-Teracron-Trace` HTTP header. Wire format: `<trace_id>:<parent_span_id>`. Invalid headers are silently ignored (zero-trust).
- **Error message truncation** — `error_message` is now truncated to 1024 characters to prevent oversized payloads.
- **New exports:** `trace_context`, `async_trace_context`, `SpanHandle`, `get_trace_header`, `set_trace_header` from `teracron` and `teracron.tracing`.
- **3 new test modules:** `test_phase2_capture.py` (PII boundary, sanitisation), `test_phase2_nesting.py` (nested workflows, cross-process propagation).
- **Updated test modules:** `test_span.py`, `test_trace_context.py`, `test_trace_decorator.py`, `test_trace_buffer.py` updated for Phase 2 schema changes.

### Changed
- `Span` dataclass now includes `parent_span_id`, `metadata`, and `captured_params` fields.
- `Span.to_dict()` output now includes all Phase 2 fields (previously hardcoded `null`).
- `create_span()` accepts optional `parent_span_id` argument.
- `finalise_span()` accepts optional `metadata` and `captured_params` arguments with full sanitisation.
- `start_trace()` now resets the span stack (prevents stale nesting state).
- `clear_trace()` now clears both trace ID and span stack.
- Agent skill file (`.agent-skill.md`) updated with Phase 2 documentation and examples.

### Security
- **PII boundary enforcement**: By default, `@trace` captures NO function parameter values. Only parameters explicitly listed in `capture=[...]` are extracted. This prevents accidental PII exfiltration.
- **Metadata validation**: All metadata keys/values are type-checked and size-limited before being attached to spans. Non-primitive types are rejected.
- **Input validation on trace headers**: `set_trace_header()` validates hex format and length of trace/span IDs. Invalid headers are silently ignored — never crash on bad input.

## [0.3.0] - 2025-07-20

### Added
- **`@trace("workflow")` decorator** — captures method execution timing, success/failure, and exceptions as structured spans. Supports both sync and async functions.
- **Flat span tracing** — `Span` frozen dataclass with `trace_id`, `span_id`, `workflow`, `operation`, `status`, `started_at`, `duration_ms`, `error_type`, `error_message`.
- **Dedicated trace buffer** — separate ring buffer (`collections.deque(maxlen=N)`) isolated from the metrics buffer. Drop-oldest overflow with one-time warning.
- **Trace transport** — `POST /v1/traces` endpoint support via `Transport.send_traces()`. Reuses the same `requests.Session` keep-alive pool.
- **Trace flush loop** — flushes when buffer hits `trace_batch_size` or `trace_flush_interval` seconds elapse (whichever first). Final flush on `stop()`.
- **3 new config fields:**
  - `tracing_enabled` (bool, default `True`) — master kill-switch for tracing.
  - `trace_batch_size` (int, default `100`, range 1–10,000) — max spans per flush.
  - `trace_flush_interval` (float, default `10.0s`, range 1–300s) — time-based flush ceiling.
- **Environment variable support:** `TERACRON_TRACING_ENABLED`, `TERACRON_TRACE_BATCH_SIZE`, `TERACRON_TRACE_FLUSH_INTERVAL`.
- **`teracron.tracing` subpackage** — `context.py` (ContextVar trace propagation), `span.py` (span factory), `decorator.py` (`@trace`).
- **New types:** `Span`, `TraceFlushResult` in `teracron.types`.
- **Public API:** `from teracron import trace` or `from teracron.tracing import trace`.
- **5 new test modules:** `test_span.py`, `test_trace_context.py`, `test_trace_decorator.py`, `test_trace_buffer.py`, `test_trace_config.py`.
- **Agent skill file** (`.agent-skill.md`) documenting annotation usage with examples.

## [0.2.0] - 2025-07-07

### Changed
- **BREAKING (defaults):** `interval_s` default lowered from 30s → 10s.
- **BREAKING (defaults):** `max_buffer_size` default lowered from 60 → 10.
- First flush now fires within ~60s (was ~30 min) with default settings.

### Added
- `flush_deadline_s` parameter (default: 60s, range 10–600s) — time-based flush ceiling that forces a flush even when the buffer isn't full, preventing data from sitting in-memory indefinitely.
- `TERACRON_FLUSH_DEADLINE` environment variable for configuring `flush_deadline_s`.
- 6 new tests covering `flush_deadline_s` config resolution and time-based flush behaviour.

### Fixed
- SDK appeared unresponsive with default settings because first flush required 30 × 60 = 1,800s (30 min) of buffering before any data was sent.

## [0.1.0] - 2025-04-25

### Added
- Initial release of `teracron-sdk`.
- `teracron.up()` / `teracron.down()` — zero-ceremony singleton API.
- `TeracronClient` — explicit lifecycle for advanced use cases.
- `teracron-agent` CLI for sidecar monitoring.
- Hybrid RSA-4096 OAEP + AES-256-GCM encryption (wire-compatible with Node.js SDK).
- Zero-dependency protobuf encoder (wire-compatible with Node.js SDK).
- API key format (`tcn_...`) — encodes slug + public key in a single token.
- Domain allowlisting — restricts to `*.teracron.com` by default.
- Background daemon thread — never blocks the host process.
- `atexit` handler for graceful shutdown.
- Framework examples: Flask, FastAPI, Django.
- 87 tests, 100% bandit-clean.
