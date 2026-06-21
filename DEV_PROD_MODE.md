# Development / Production Mode Split

This document describes the **dev/prod mode** design that lets a single
Teracron project keep its local-testing telemetry completely separate from its
production telemetry. It is written for developers extending the system.

---

## 1. The product behaviour

Every Teracron project has **two modes**:

| Mode          | Used for                                  | API key the user pastes |
| ------------- | ----------------------------------------- | ----------------------- |
| `development` | the app running locally / in testing      | the **dev** API key     |
| `production`  | the real production deployment            | the **prod** API key    |

The user never selects a mode in code. **The mode is baked into the API key.**
If they put the *development* key in `TERACRON_API_KEY`, every span/metric/event
lands in the project's *development* logs; if they put the *production* key,
it lands in *production* logs. Switching modes = swapping the env var. Nothing
else changes.

---

## 2. How the mode travels (end to end)

```
 ┌─────────────┐   X-Project-Mode: development     ┌──────────────┐
 │  Your app   │ ───────────────────────────────▶ │ local        │
 │  + SDK      │   (dev API key in env)            │ interface    │
 └─────────────┘                                   └──────┬───────┘
        │ mode decoded from the API key                   │ mode in spool sidecar
        ▼                                                  ▼
   ResolvedConfig.mode                              local Teracron (consumer)
                                                          │ X-Project-Mode header
                                                          ▼
                                                   ┌──────────────┐
                                                   │   Convex     │  picks the
                                                   │   ingest     │  mode-matching
                                                   │              │  PRIVATE KEY,
                                                   └──────┬───────┘  tags rows mode=…
                                                          ▼
                                                   spans/events/metrics
                                                   (each row tagged `mode`)
```

1. **API key** carries the mode (see §3).
2. **SDK** (`config.resolve_config`) decodes the mode into `ResolvedConfig.mode`
   and the transport sends it as the **`X-Project-Mode`** HTTP header on every
   request (metrics, traces, events).
3. **Local interface** records the mode in each spool sidecar.
4. **Local Teracron** (spool consumer) replays the `X-Project-Mode` header to
   Convex.
5. **Convex ingest** selects the **mode-matching private key** to decrypt and
   tags every stored row with `mode`.
6. **Reads** (dashboard panel + `logs` CLI) filter by `mode`.

> The Node SDK (`teracron-node`) is wired identically: `config.ts` decodes the
> mode and `transport.ts` sends `X-Project-Mode`.

---

## 3. API key format (mode-encoded)

```
tcn_<base64url( <short-mode> : <slug> : <publicKeyPEM> )>
         e.g.   dev          : fast-shark-747 : -----BEGIN PUBLIC KEY-----…
```

* `short-mode` is `dev` or `prod`.
* **Backward compatible**: a *legacy* key with only `slug:PEM` (no mode prefix)
  decodes to `mode = production`. Production is the safe default, so every key
  minted before the split keeps working unchanged.
* The leading `dev`/`prod` token can never be mistaken for a slug (slugs are
  `word-word-NNN`), so the two formats are unambiguous.

Source of truth: `teracron/apikey.py` (`encode_api_key` / `decode_api_key`).
The dashboard (`convex/projects.ts buildApiKey`) and the Node SDK
(`teracron-node/src/config.ts`) implement the **same** format.

`decode_api_key` returns a **3-tuple** `(slug, public_key_pem, mode)`.

---

## 4. Two key pairs per project

Each project stores **two independent RSA-4096 key pairs**:

| Schema field (`projects`) | Meaning                              |
| ------------------------- | ------------------------------------ |
| `publicKey` / `privateKey`       | **production** pair (unchanged names → no migration) |
| `devPublicKey` / `devPrivateKey` | **development** pair (new, optional)  |

Why separate keys (not just a label)? Because a dev API key encrypts with the
dev public key, so it **cannot be decrypted with the production private key**
and vice-versa. The mode separation is therefore enforced *cryptographically*,
not merely by a flag — a dev key can never write production data even if the
mode header were forged.

* New projects (`projects.create`) generate **both** pairs up front.
* Legacy projects gain a dev pair lazily via `projects.ensureModeKeys`
  (idempotent) — no data migration required.

---

## 5. Storage & indexing

`spans`, `workflowEvents`, and `memoryMetrics` each gain an optional
`mode` field (`"development" | "production"`). Absent ⇒ treated as
`production`, so pre-split rows remain valid.

`spans` also gains the index **`by_project_mode_startedAt`**
(`projectId, mode, startedAt`). Mode-scoped reads use this index so a dev tail
never scans production spans (and vice-versa) — keeping reads O(limit) even at
millions of spans per project.

---

## 6. Reads

* **Dashboard** — `traces.getRecentSpansForProject` (session-authed) powers the
  "Live Logs" panel, with a dev/prod toggle (`LiveLogsPanel.tsx`). Convex
  `useQuery` is reactive, so the panel live-updates as spans land.
* **Project-header mode badge** — `ProjectModeBadge.tsx` renders a
  Convex/Vercel-style pill in the **top bar, immediately right of the project
  switcher** (logo · separator · project pill · separator · mode pill), reading
  `Development (Cloud) • <slug>`, colour-coded: warm green = dev, warm bronze =
  prod, with a dropdown switcher. Placing the project selector and the
  environment selector side-by-side mirrors Convex/Vercel, so users pick *which
  project* and *which environment* from the same header cluster.
  It is the at-a-glance indicator of which environment the dashboard is showing.
  The selected mode is **lifted to the project page** (`viewMode` state) and is
  the single source of truth — it drives both the badge and the mode-scoped
  panels (e.g. `LiveLogsPanel` is rendered controlled so its toggle and the
  header badge always agree).
* **Project switcher** — `ProjectSwitcher.tsx` renders a Convex/Vercel-style
  project pill in the top bar (logo · separator · `<project-name> ⌄`). Clicking
  it opens a dropdown listing every project the user owns (reactive
  `projects.list`, `privateKey` stripped server-side), with a client-side filter
  and a "View all projects" footer. Selecting another project navigates to its
  detail page (`/dashboard/project/<id>`); the current project is highlighted +
  checkmarked. This lets users hop between projects without returning to the
  dashboard grid.
* **CLI** — `teracron-agent logs` tails `GET /v1/logs` on Convex. The mode
  defaults to the mode of the API key in use (`--mode all` to see both, or
  `--mode development|production` to override). The logs HTTP action accepts a
  token whose PEM matches **either** the prod or dev public key.

---

## 7. Creating projects / accounts from the terminal

* `teracron-agent projects` — list the project bound to your key (shows the
  key's mode).
* `teracron-agent projects --create <NAME>` — create a new project and print
  **both** its dev and prod API keys. Needs your dashboard **session token**
  in `TERACRON_SESSION_TOKEN` and the Convex URL in `CONVEX_URL` /
  `NEXT_PUBLIC_CONVEX_URL`; it calls the existing `projects.create` action over
  the Convex HTTP API — no new privileged endpoint.
* `teracron-agent accounts` — shows the current account context. **Account
  sign-up itself remains a dashboard-only flow** (it needs the sign-up UI).

---

## 8. Backward-compatibility summary

| Concern                         | Guarantee                                        |
| ------------------------------- | ------------------------------------------------ |
| Existing (legacy) API keys      | decode as `production`, keep working             |
| Existing project documents      | unchanged; dev keys backfilled lazily            |
| Existing span/event/metric rows | `mode` absent ⇒ read as `production`             |
| Existing ingest mutations       | `mode` arg is optional, defaults to `production` |
| Node SDK with an old key        | still works (mode defaults to production)        |

Nothing in the prior single-mode world breaks.

---

## 9. Dashboard UI layout changes

### 9a. Project cards on the dashboard grid (`dashboard/page.tsx`)

Each project card now follows the Convex-style layout:

| Section | Content |
| --- | --- |
| **Left** | Project name + slug (or "Needs Setup" badge) |
| **Center** | Clickable **Production** / **Development** mode buttons + "Created X ago" |
| **Right** | 3-dot context menu (`⋮`) |

**Mode buttons** — The "Production" and "Development" labels are now clickable
buttons. Each navigates to `/dashboard/project/{id}?mode=production` or
`?mode=development` respectively, so the project detail page opens pre-set to
the chosen environment. Clicking the card background itself defaults to
development mode.

**3-dot menu** (`ProjectCardMenu`) — contains a single action:
- **Delete project** — destructive action, triggers a browser `confirm()`
  dialog before calling `projects.remove`.

All other menu items (View Deployments, Settings, Lost Access) have been
removed for simplicity — users navigate directly via the mode buttons instead.

### 9c. URL-driven mode initialisation (`project/[id]/page.tsx`)

The project detail page reads the `?mode=` search parameter on load via
`useSearchParams()`. If the value is `"production"` or `"development"`, it
initialises `viewMode` to that value; otherwise it falls back to `"development"`.
This means clicking a mode button on the project card seamlessly opens the
dashboard in the correct environment context.

### 9b. Fixed top bar & sidebar on the project detail page

The project detail page (`dashboard/project/[id]/page.tsx`) now uses a **fixed
viewport layout** (`h-screen flex-col overflow-hidden`):

* **Top bar** (`<header>`) — `shrink-0`, never scrolls. Contains logo,
  project switcher, dev/prod mode badge, username, and log-out button.
* **Sidebar** (`<ProjectSidebar>`) — fills the remaining height beside the
  content. Already handles its own internal scroll if nav items overflow.
* **Content section** (`<section>`) — the **only** scrollable region. Uses
  `overflow-y-auto` so scrolling only moves the main panel, leaving the top
  bar and sidebar pinned in place.

This prevents the visual issue where scrolling moved the entire page including
the top bar and sidebar.
