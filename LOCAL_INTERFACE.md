# Local Interface — Direct Mode Architecture

> Design + implementation reference for the **local interface** that lets the
> Teracron Python SDK communicate with a locally-running Teracron **directly**,
> with no inter-server HTTPS. Read this before changing `local_interface.py`,
> the loopback scheme logic in `config.py`, or the spool contract.

---

## Why this exists

In production the SDK ships encrypted envelopes to `teracron.com` over HTTPS.
For local development we want **no inter-server TLS** between an instrumented
app (e.g. the payment server) and the local Teracron. Instead of pointing the
SDK at a half-configured local HTTPS server, the SDK posts to a small **local
interface** — a loopback stand-in for `teracron.com` — over plain HTTP. The
local Teracron then consumes whatever that interface receives.

```
  payment.py ──http──▶ local interface (loopback:3000) ──spool──▶ local Teracron
    (SDK)                teracron.local_interface             ~/.teracron/local-spool/
```

The SDK code path is **identical** in local and production — only the target
host/scheme changes, and that is resolved automatically (see "Scheme
resolution"). In production you simply set the domain to `www.teracron.com`
and the same calls go over HTTPS.

---

## Components

### 1. `teracron/local_interface.py` — the interface

A dependency-free (stdlib `http.server`) receiver that:

- Accepts the same three routes as production:
  - `POST /api/ingest`    → metrics (64 KB cap)
  - `POST /api/v1/traces` → trace spans (128 KB cap)
  - `POST /api/v1/events` → workflow events (64 KB cap)
- Performs the **same validation** as the Next.js routes: `X-Project-Slug`
  format, `Content-Type: application/octet-stream`, and the per-route size cap.
  Mirroring the limits keeps local status codes identical to production.
- **Spools** each accepted (still-encrypted) envelope to disk for the local
  Teracron to consume. It never decrypts, decodes, or interprets payloads —
  that is the local Teracron's job. Keeping responsibilities split keeps the
  security surface tiny.
- Binds to **loopback only** by default and refuses non-loopback hosts unless
  explicitly forced (`allow_non_loopback=True`) — a plaintext ingest endpoint
  on a public interface would be unsafe.

Run it:

```bash
teracron-agent local-interface            # 127.0.0.1:3000
teracron-agent local-interface --port 4000
python -m teracron.local_interface --port 3000
```

### 2. Scheme resolution — `teracron/config.py`

`resolve_scheme(domain)` returns `http` for loopback hosts and `https`
otherwise. `_is_loopback_host(domain)` matches **exactly** against
`localhost`, `127.0.0.1`, `::1` (port-stripped) — never a range match — so the
relaxation can never be tricked into redirecting production telemetry to an
arbitrary internal address.

`_validate_domain` allows loopback hosts **without** the
`TERACRON_ALLOW_CUSTOM_DOMAIN` escape hatch. Every non-loopback host still
requires `*.teracron.com` (or the explicit escape hatch). `transport.py` and
`query.py` both build their URLs through `resolve_scheme`, so the two paths stay
consistent.

> Security: dropping TLS applies **only** to the loopback hop. Payloads remain
> RSA-4096 + AES-256-GCM encrypted regardless of scheme, so no plaintext is ever
> exposed — and the spooled bytes are encrypted at rest too.

---

## The spool contract (hand-off boundary — keep stable)

Each accepted request becomes **two files** in `~/.teracron/local-spool/`:

| File | Contents |
|---|---|
| `<epoch_ns>-<kind>-<slug>.bin`  | The raw encrypted envelope, unchanged. |
| `<epoch_ns>-<kind>-<slug>.json` | Metadata sidecar. |

Sidecar JSON:

```json
{
  "kind": "traces",                       // metrics | traces | events
  "slug": "vivid-kudu-655",
  "payload_file": "1737500000000000000-traces-vivid-kudu-655.bin",
  "byte_length": 4096,
  "received_at": 1737500000000             // Unix ms
}
```

**Write ordering guarantee:** the `.bin` is written (temp + atomic replace, with
`fsync`) **before** the `.json` sidecar. A consumer that watches for `*.json`
therefore only ever sees a sidecar once its payload is fully on disk — no torn
reads. Files are created `0600` under a `0700` directory (owner-only), because
they hold encrypted customer data.

### Consuming the spool (local Teracron side) — IMPLEMENTED

The spool consumer is `teracron/local_teracron.py` (the **local Teracron**). It
completes the pipeline that the interface only half-implements:

```
  payment.py ─http─▶ local interface ─spool─▶ local teracron ─https─▶ Convex DB
    (SDK)            local_interface.py        local_teracron.py       (database)
```

What it does each poll:

1. Watches `~/.teracron/local-spool/` for new `*.json` sidecars.
2. For each sidecar, reads its `payload_file` and routes by `kind` to the
   matching **Convex HTTP ingest endpoint** over HTTPS — sending the
   **still-encrypted bytes unchanged** with the `X-Project-Slug` header, exactly
   the arguments the production Next.js routes pass:

   | `kind`   | Convex route (`*.convex.site`) | Convex action                       |
   |----------|--------------------------------|-------------------------------------|
   | metrics  | `POST /ingest`                 | `internal.ingest.processIngest`     |
   | traces   | `POST /v1/traces`              | `internal.traces.processTraceIngest`|
   | events   | `POST /v1/events`              | `internal.events.processEventIngest`|

3. Archives the pair to `local-spool/processed/` on success (HTTP 202),
   quarantines it to `local-spool/quarantine/` on a permanent reject (400/401/
   413/415 — it will never decrypt), or **leaves it in place** for the next pass
   on a transient failure (0/404/429/5xx).

**Why replay the encrypted envelope (instead of decrypting locally):** Convex is
the single holder of the project's RSA private key, so the local Teracron never
decrypts. This keeps *all* crypto server-side and reuses the **identical** ingest
+ validation code as production — only the delivery mechanism changes (spool poll
vs. HTTP body). Every byte that reaches the database does so over HTTPS.

Run it:

```bash
teracron-agent local-teracron                       # poll forever
teracron-agent local-teracron --once                # drain once and exit
teracron-agent local-teracron --convex-site https://<deployment>.convex.site
# Site URL also resolves from CONVEX_SITE_URL / NEXT_PUBLIC_CONVEX_SITE_URL,
# or is derived from CONVEX_URL / NEXT_PUBLIC_CONVEX_URL (.cloud → .site).
```

### Reading live logs back (the agent's view)

Once spans are in the database, the terminal / AI agent tails them live straight
from Convex over HTTPS — **no Next.js server required**:

```bash
teracron-agent logs                 # snapshot of recent spans
teracron-agent logs --follow        # live tail (polls GET /v1/logs)
teracron-agent logs --follow --json # machine-readable
```

`logs` reads `GET {convex-site}/v1/logs` (a Convex HTTP action added in
`convex/http.ts`), which authenticates the Bearer `tcn_` key (PEM-matched
against the stored project key) and returns recent spans newest-first with an
incremental `cursor`. The follower passes the previous `cursor` back as `since`,
so each poll returns only spans newer than the last printed — no duplicates,
O(limit) per poll. The query is backed by `traces.getRecentSpansBySlug` using
the `by_project_startedAt` index, so it stays cheap at any span rate.

### End-to-end demo

`payment-server/run_live_teracron.sh` orchestrates the whole loop in one shot:
starts the interface (loopback) + the local teracron (→ Convex), runs the
instrumented `payment.py`, shows what the interface accepted and what the
consumer forwarded to the DB, then reads the spans **back out of the database**
with `teracron-agent logs`. (Contrast with `run_pipeline_demo.sh`, which only
decrypts a spooled file locally and never reaches the database.)

> Note: the interface defaults to port 3000, which the Teracron Next.js
> dashboard also uses. When both run together, start the interface on another
> loopback port (e.g. `--port 3100`) and point the SDK at `localhost:3100`
> (`run_live_teracron.sh` does this automatically via `IFACE_PORT`).

---

## Scale / readability notes

- A single dev process generates trivial volume, so a flat spool directory is
  fine. If this ever needs more throughput, shard by slug or date — but that is
  explicitly **out of scope** for a local dev interface; do not add complexity
  speculatively.
- The interface is intentionally stdlib-only so it never drags the SDK's
  runtime deps (`requests`, `cryptography`) into the receive path and is trivial
  to run anywhere Python exists.

---

## Agent terminal workflow (why direct mode matters)

The whole point: an IDE AI agent can diagnose a reported crash from the terminal
without any HTTPS plumbing. See `teracron/.agent-skill.md` for the full
playbook. In short:

```bash
teracron-agent --domain localhost:3000 projects --json
teracron-agent --domain localhost:3000 events --status=workflow_failed --json
teracron-agent --domain localhost:3000 trace <TRACE_ID> --json
```
