# Teracron Workflow Tracing — Implementation Plan

> **Version:** 0.7  
> **Date:** 2025-01-20 (updated 2025-07-22)  
> **SDK:** `teracron-sdk 0.6.x` — ✅ Published to PyPI  
> **Backend:** `teracron` (Convex + Next.js) — 🔲 Phase 5–8 pending  
> **Status:** Phase 1 ✅ Phase 2 ✅ Phase 3 ✅ Phase 4 ✅ Phase 5 🔲 Phase 6 🔲 Phase 7 🔲 Phase 8 🔲

---

## Summary

The SDK (`teracron-sdk 0.6.x`) is published and ships traces + events. The backend (`teracron`) has **no support** for receiving, storing, or querying that data yet. Phases 5–8 implement the backend data layer, ingest endpoints, query API, and dashboard UI.

---

## SDK Wire Format Reference

The SDK sends two types of encrypted payloads. Both use `RSA-4096 + AES-256-GCM` envelope encryption, identical to the existing metrics ingest.

### Trace Payload — `POST /v1/traces`

```
Header:  X-Project-Slug: <slug>
         Content-Type: application/octet-stream
Body:    RSA-OAEP encrypted envelope → decrypts to JSON:
```

```json
{
  "type": "trace",
  "project_slug": "vivid-kudu-655",
  "spans": [
    {
      "trace_id": "32-char hex",
      "span_id": "32-char hex",
      "parent_span_id": "32-char hex | null",
      "workflow": "payment",
      "operation": "PaymentService.charge_card",
      "status": "succeeded | failed | started",
      "started_at": 1721500000000,
      "duration_ms": 142.5,
      "error_type": "ValueError | null",
      "error_message": "max 1024 chars | null",
      "metadata": { "max 32 keys, primitives only" },
      "captured_params": { "max 32 keys, 512-char values" }
    }
  ]
}
```

### Event Payload — `POST /v1/events`

```json
{
  "type": "event",
  "project_slug": "vivid-kudu-655",
  "events": [
    {
      "type": "workflow_started | workflow_completed | workflow_failed | step_started | step_completed | step_failed | retry",
      "workflow": "payment",
      "trace_id": "32-char hex",
      "span_id": "32-char hex",
      "operation": "PaymentService.charge_card",
      "severity": "info | warning | error | critical",
      "timestamp": 1721500000000,
      "error_type": "ValueError | null (failure events only)",
      "error_message": "max 512 chars | null",
      "metadata": { "max 16 keys, primitives only" }
    }
  ]
}
```

### Query Endpoints (SDK `TeracronQueryClient` expects)

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/v1/events?workflow=&status=&limit=&since=` | Bearer `tcn_...` | List workflow events |
| `GET` | `/v1/traces/{trace_id}` | Bearer `tcn_...` | Full span tree for a trace |
| `GET` | `/v1/workflows?limit=` | Bearer `tcn_...` | Aggregated workflow run summaries |
| `GET` | `/v1/spans/{span_id}` | Bearer `tcn_...` | Single span detail |

---

## Decisions (Locked)

| # | Decision | Answer |
|---|---|---|
| Q1 | Trace endpoint | **Dedicated `POST /v1/traces`** — separate from metrics, independent rate limits |
| Q2 | Event endpoint | **Dedicated `POST /v1/events`** — separate from traces |
| Q3 | Auth for ingest | **Encryption gate** — same as metrics. RSA envelope proves possession of project public key |
| Q4 | Auth for queries | **Bearer `tcn_...` API key** — decode key → extract slug → verify project exists |
| Q5 | Rate limits | **Separate buckets**: traces 30 req/min, events 30 req/min (independent from metrics 60 req/min) |
| Q6 | Payload limits | **128KB for traces** (up to 100 spans), **64KB for events** |
| Q7 | Convex table strategy | **`spans` + `workflowEvents`** — no denormalized workflow runs table (aggregate on read for now) |
| Q8 | API route strategy | **Next.js API routes** as canonical endpoints (same as `/api/ingest`), Convex HTTP routes as fallback |

---

## Completed Phases (SDK)

### Phase 1 — MVP (SDK `0.3.0`) ✅
### Phase 2 — Depth (SDK `0.4.0`) ✅
### Phase 3 — Ecosystem (SDK `0.5.0`) ✅
### Phase 4 — Agent & Workflow Page (SDK `0.6.0`) ✅

**SDK test suite: 479 passed, 0 failed, 0 errors.**

---

## Phase 5 — Backend Schema & Data Layer 🔲

> **Target:** `convex/schema.ts`, `convex/traces.ts`, `convex/events.ts`, `convex/workflows.ts`  
> **Depends on:** Nothing (first backend phase)

### Tasks

| # | Task | Status | Detail |
|---|---|---|---|
| B1 | `spans` table definition in `schema.ts` | 🔲 | Fields: `projectId`, `traceId`, `spanId`, `parentSpanId`, `workflow`, `operation`, `status`, `startedAt`, `durationMs`, `errorType`, `errorMessage`, `metadata`, `capturedParams`, `receivedAt`. Indexes: `by_project_traceId`, `by_traceId`, `by_spanId`, `by_project_workflow_startedAt`, `by_project_startedAt` |
| B2 | `workflowEvents` table definition in `schema.ts` | 🔲 | Fields: `projectId`, `traceId`, `spanId`, `workflow`, `eventType`, `operation`, `severity`, `timestamp`, `errorType`, `errorMessage`, `metadata`, `receivedAt`. Indexes: `by_project_timestamp`, `by_project_workflow`, `by_traceId` |
| B3 | `traceIngestRateLimits` table definition in `schema.ts` | 🔲 | Separate rate limit table from metrics. Fields: `slug`, `windowStart`, `count`. Index: `by_slug_window` |
| B4 | `convex/traces.ts` — `processTraceIngest` mutation | 🔲 | Rate limit (30/min per slug) → lookup project → decrypt envelope → parse JSON → validate each span (`trace_id` hex ≤64 chars, `span_id` hex ≤64 chars, `workflow` non-empty, `status` in allowed set, `startedAt` within bounds, `durationMs` ≥0, `error_message` ≤1024 chars, `metadata` ≤32 keys) → batch insert into `spans`. Max 100 spans per batch |
| B5 | `convex/traces.ts` — `getTraceSpans` query | 🔲 | Authenticated. Fetch all spans for a `traceId` belonging to project, ordered by `startedAt` |
| B6 | `convex/traces.ts` — `getSpanById` query | 🔲 | Authenticated. Single span lookup by `spanId` |
| B7 | `convex/traces.ts` — `listRecentTraces` query | 🔲 | Authenticated. Distinct recent traces for a project (last N unique `traceId`s by most recent `startedAt`). Returns trace summary: `traceId`, `workflow`, `status`, `spanCount`, `durationMs`, `startedAt` |
| B8 | `convex/events.ts` — `processEventIngest` mutation | 🔲 | Rate limit → decrypt → parse JSON → validate (`event_type` in allowed set, `severity` valid, timestamps within bounds) → insert into `workflowEvents` |
| B9 | `convex/events.ts` — `listEvents` query | 🔲 | Authenticated. Filter by `workflow`, `eventType`, `since` timestamp. Paginated with `limit` (max 1000) |
| B10 | `convex/workflows.ts` — `listWorkflows` query | 🔲 | Authenticated. Aggregate spans by `workflow` for the project — distinct `traceId` count, failed count, avg duration, last run time |

### Validation Rules (B4, B8)

```
Span validation:
  - trace_id:        hex string, 1–64 chars
  - span_id:         hex string, 1–64 chars
  - workflow:        non-empty string, ≤128 chars
  - operation:       non-empty string, ≤256 chars
  - status:          "started" | "succeeded" | "failed"
  - started_at:      Unix ms, not >30s future, not >10min past
  - duration_ms:     ≥0, ≤86_400_000 (24h ceiling)
  - error_message:   ≤1024 chars
  - metadata:        ≤32 keys, string keys ≤128 chars, primitive values ≤1024 chars
  - captured_params: ≤32 keys, string keys ≤128 chars, primitive values ≤512 chars

Event validation:
  - event_type:      one of VALID_EVENT_TYPES (7 values)
  - severity:        "info" | "warning" | "error" | "critical"
  - workflow:        non-empty string, ≤128 chars
  - operation:       ≤256 chars
  - error_message:   ≤512 chars
  - metadata:        ≤16 keys, primitives only
  - timestamp:       Unix ms, not >30s future, not >10min past
```

---

## Phase 6 — Ingest API Routes 🔲

> **Target:** Next.js API routes + Convex HTTP fallback  
> **Depends on:** Phase 5

### Tasks

| # | Task | Status | Detail |
|---|---|---|---|
| B11 | `src/app/api/v1/traces/route.ts` — `POST /v1/traces` | 🔲 | Validate `X-Project-Slug` header → validate `Content-Type: application/octet-stream` → enforce 128KB max payload → forward to `processTraceIngest` Convex mutation → return `202 { accepted, dropped }`. Error mapping: `RATE_LIMITED` → 429, `PROJECT_NOT_FOUND` → 404, `DECRYPTION_FAILED` → 401, etc. |
| B12 | `src/app/api/v1/events/route.ts` — `POST /v1/events` | 🔲 | Same pattern as B11 but for events. 64KB max payload. Forward to `processEventIngest` mutation |
| B13 | `convex/http.ts` — Add `/v1/traces` POST route | 🔲 | Fallback ingest via Convex HTTP router (same pattern as existing `/ingest`). 128KB limit. CORS headers |
| B14 | `convex/http.ts` — Add `/v1/events` POST route | 🔲 | Fallback ingest via Convex HTTP router. 64KB limit. CORS headers |
| B15 | `convex/http.ts` — CORS preflight for new routes | 🔲 | OPTIONS handlers for `/v1/traces` and `/v1/events` |

### Endpoint Contract

```
POST /v1/traces
  Request:   X-Project-Slug: <slug>, Content-Type: application/octet-stream, body: encrypted envelope
  Success:   202 { "status": "accepted", "accepted": 5, "dropped": 0 }
  Errors:    400 (bad request), 401 (decryption failed), 404 (project not found),
             413 (payload too large), 415 (wrong content type), 429 (rate limited)

POST /v1/events
  Request:   X-Project-Slug: <slug>, Content-Type: application/octet-stream, body: encrypted envelope
  Success:   202 { "status": "accepted", "accepted": 3, "dropped": 0 }
  Errors:    (same as above)
```

---

## Phase 7 — Query API Routes 🔲

> **Target:** Next.js API routes for SDK `TeracronQueryClient` + CLI  
> **Depends on:** Phase 5

### Tasks

| # | Task | Status | Detail |
|---|---|---|---|
| B16 | `convex/lib/apiKeyAuth.ts` — API key authentication helper | 🔲 | Decode `tcn_...` Bearer token → extract `slug` → lookup project by slug → verify exists → return `projectId`. Reuse `decodeApiKey()` from SDK's apikey format. Reject invalid/expired keys |
| B17 | `src/app/api/v1/traces/[traceId]/route.ts` — `GET /v1/traces/:id` | 🔲 | Authenticate via Bearer token → validate `traceId` (hex, ≤64 chars) → call `getTraceSpans` → return `{ trace_id, spans: [...] }` |
| B18 | `src/app/api/v1/spans/[spanId]/route.ts` — `GET /v1/spans/:id` | 🔲 | Authenticate → validate `spanId` → call `getSpanById` → return span object |
| B19 | `src/app/api/v1/events/route.ts` — `GET /v1/events` (extend B12) | 🔲 | Authenticate → parse query params (`workflow`, `status`, `limit`, `since`) → call `listEvents` → return `{ events: [...] }` |
| B20 | `src/app/api/v1/workflows/route.ts` — `GET /v1/workflows` | 🔲 | Authenticate → parse `limit` param → call `listWorkflows` → return `{ workflows: [...] }` |

### Authentication Flow

```
Authorization: Bearer tcn_<base64_payload>
  ↓
Decode API key → extract project_slug
  ↓
Lookup project by slug → 404 if not found
  ↓
Return projectId → proceed with query
```

### Response Contracts

```
GET /v1/traces/{trace_id}
  200 { "trace_id": "abc...", "workflow": "payment", "span_count": 5, "spans": [...] }
  401 { "error": "Authentication failed" }
  404 { "error": "Trace not found" }

GET /v1/spans/{span_id}
  200 { "span_id": "abc...", "trace_id": "...", "workflow": "...", ... }

GET /v1/events?workflow=payment&status=failed&limit=50&since=2025-07-22T00:00:00Z
  200 { "events": [...], "count": 12 }

GET /v1/workflows?limit=20
  200 { "workflows": [{ "workflow": "payment", "total_runs": 150, "failed_runs": 3, "avg_duration_ms": 245.2, "last_run_at": ... }] }
```

---

## Phase 8 — Dashboard UI 🔲

> **Target:** Traces panel in project dashboard  
> **Depends on:** Phase 5 (Convex queries)

### Tasks

| # | Task | Status | Detail |
|---|---|---|---|
| B21 | Update `ProjectSidebar.tsx` — add "Traces" nav item | 🔲 | Extend `ProjectSection` type to `"metrics" \| "settings" \| "traces"`. Add nav entry |
| B22 | Update `project/[id]/page.tsx` — route to `TracesPanel` | 🔲 | Extend section type. Subscribe to `api.traces.listRecentTraces`. Render `<TracesPanel>` when `activeSection === "traces"` |
| B23 | `src/components/TracesPanel.tsx` — trace list view | 🔲 | Table: Workflow, Status (badge), Duration, Span Count, Timestamp. Filters: workflow dropdown, status, time range. Click row → expand `TraceTimeline`. Empty state: "No traces received yet" |
| B24 | `src/components/TraceTimeline.tsx` — span waterfall | 🔲 | Horizontal waterfall chart. Bar width ∝ `duration_ms`. Indented child spans. Hover tooltip: operation, duration, metadata. Click → `TraceDetail` |
| B25 | `src/components/TraceDetail.tsx` — span detail panel | 🔲 | Full span info: operation, workflow, status, duration, error (if failed), metadata KV pairs, captured params, parent span link |
| B26 | Update `globals.css` — trace status colours | 🔲 | Warm muted palette: succeeded `#6b8a5e`, failed `#c4624a`, in-progress `#b89b5e`. No bright/neon colours |

### UI Colour Palette (Warm Dark Mode)

```
Status badges:
  succeeded:    bg #6b8a5e/15, text #6b8a5e   (muted warm green)
  failed:       bg #c4624a/15, text #c4624a   (muted burnt red)
  in_progress:  bg #b89b5e/15, text #b89b5e   (warm gold)

Waterfall bars:
  succeeded:    #8a7e6b  (warm taupe)
  failed:       #c4624a  (burnt orange)
  root span:    #a08b6e  (warm bronze)

Backgrounds:
  panel:        existing dash-surface
  row hover:    existing dash-bg
  selected:     accent/10
```

---

## Implementation Order

```
Phase 5 (Schema + Data)  ──┐
                            ├──→  Phase 6 (Ingest Routes)  ──→  End-to-end SDK → Backend
                            ├──→  Phase 7 (Query Routes)   ──→  CLI + SDK queries work
Phase 5 ────────────────────┴──→  Phase 8 (Dashboard UI)   ──→  Visual tracing
```

Phase 6 and Phase 7 are independent of each other (both depend only on Phase 5).  
Phase 8 depends only on Phase 5 (uses Convex reactive queries directly, not REST routes).

---

## Files Changed / Created (Backend)

### Modified
| File | Change |
|---|---|
| `convex/schema.ts` | Add `spans`, `workflowEvents`, `traceIngestRateLimits` tables |
| `convex/http.ts` | Add `/v1/traces` POST, `/v1/events` POST, CORS preflight |
| `src/components/ProjectSidebar.tsx` | Add "Traces" nav item, extend section type |
| `src/app/dashboard/project/[id]/page.tsx` | Add traces section routing + `TracesPanel` render |
| `src/app/globals.css` | Add trace status colour CSS variables |

### Created
| File | Purpose |
|---|---|
| `convex/traces.ts` | Trace ingest mutation + query functions |
| `convex/events.ts` | Event ingest mutation + query functions |
| `convex/workflows.ts` | Aggregated workflow queries |
| `convex/lib/apiKeyAuth.ts` | Bearer token authentication for query endpoints |
| `src/app/api/v1/traces/route.ts` | `POST /v1/traces` ingest |
| `src/app/api/v1/traces/[traceId]/route.ts` | `GET /v1/traces/:id` query |
| `src/app/api/v1/events/route.ts` | `POST /v1/events` ingest + `GET /v1/events` query |
| `src/app/api/v1/workflows/route.ts` | `GET /v1/workflows` query |
| `src/app/api/v1/spans/[spanId]/route.ts` | `GET /v1/spans/:id` query |
| `src/components/TracesPanel.tsx` | Trace list view |
| `src/components/TraceTimeline.tsx` | Span waterfall visualisation |
| `src/components/TraceDetail.tsx` | Span detail panel |

