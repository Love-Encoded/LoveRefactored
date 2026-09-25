# Runtime Contract (Local + VPS)

This document defines how Love Refactored resolves runtime configuration across local and VPS deployments.

Last aligned: 2026-09-07 (see also `VERIFICATION-RUNBOOK.md` and `FAILURE-MATRIX.md`).

## Goals

- One codebase for local and VPS.
- No hard dependency on optional voice services.
- Predictable config resolution order across Node + Python services.
- Graceful degradation: TTS and optional services must never block core chat.

## Canonical Resolution Order

For any runtime value, resolve in this order:

1. Environment variable
2. `data/settings.json`
3. Profile default (`local` or `vps`)

If a key is optional and unresolved, features should degrade gracefully (never block core chat).

Legacy: repo-root `settings.json` is **not read at runtime**. On first boot, `start.sh` and `lib/lr_settings.py` copy it once into `data/settings.json` if the canonical file is missing.

## Runtime Profile

Use `LR_PROFILE`:

- `local`: laptop/dev defaults (`nodemon` for the UI server)
- `vps`: server defaults (`node server.js` directly)

If unset, treat as `local` unless deploy scripts explicitly set `vps`.

## Canonical Paths

| Path | Purpose |
|---|---|
| `LR_HOME` | Repo/install root (local: repo root; VPS: e.g. `/opt/love-refactored`) |
| `${LR_HOME}/data/` | All mutable runtime data (settings, companions, chat DB, uploads) |
| `${LR_HOME}/data/settings.json` | Primary settings file |
| `${LR_HOME}/data/love.db` | SQLite chat storage (messages, state, audit log) |
| `${LR_HOME}/data/auth/` | Auth users, sessions (`auth.js` deploys only) |
| `${LR_HOME}/data/voice_messages/` | Generated voice memo audio served by the app |
| `~/.love-refactored/` | Process logs and PID file (`ui.log`, `tanevan.log`, `whisper.log`, `pids.txt`) |
| `~/tanevan-data/` | Per-companion Tanevan memory DBs and pipeline config (override via `TANEVAN_DATA_DIR`) |

## Core Service Contract

### Web App (`server.js`)

**Ports and URLs**

- `PORT` (env) → default `3000`
- Tanevan URL: `TANEVAN_URL` (env) → `settings.memory.tanevUrl` → default `http://127.0.0.1:5001`
- Whisper URL: `settings.whisper.url` → default `http://127.0.0.1:5555`

**Voice**

- Provider resolution: companion `voiceMemoProvider` → `settings.voiceMemo.provider` → default `none`
- Cloud providers (e.g. `elevenlabs`) do not require a local TTS server on `:5050`

Voice behavior contract:

- `provider=none`: `{ audioUrl: null, skipped: true, reason: "voice_disabled" }`
- Provider enabled but unreachable: retry once, then `{ audioUrl: null, skipped: true, reason: "tts_unavailable", warning }`
- TTS failure must not block the text response.

**UI**

- Primary shell: `GET /` (also `/index.html`, `/v3`, `/v3/`, `/v3/index.html`) serves `public/v3/index.html` with `Cache-Control: no-store`.
- If `public/v3/index.html` is missing, `/` returns 500. There is no backup shell.

**Memory pipeline (Tanevan)**

Three-pass memory plus reflections: **Summarizer → Extractor → Updater**, then scheduled **temporal reflections** (daily → annual). Node injects reflections into chat only when the companion card has `reflectionsEnabled: true`. Global default schedule is `memory.reflectionSchedule` (enabled, `03:00` local). Dreams and psyche synthesis are not part of this product; schedule APIs reject those jobs.

Existing installs upgrading the Tanevan store: stop Tanevan, run `python3 tanevan/backfill_entities.py`, then `python3 tanevan/reembed.py`, then start Tanevan again.

**Route ownership**

Extracted route modules under `routes/` (companions, groups, history-chat, spotify, persona, lorebooks, calendar, parlors, logs-settings, telegram, v3-shell). Do not re-inline duplicate handlers in `server.js`.

**Auth (optional, recommended for internet-facing deploys)**

- Without `auth.js`: no login wall (localhost dev only).
- With `auth.js` (copy from `auth.example.js`):
  - Accounts: `data/auth/users.json` (managed by `scripts/auth-users.js`)
  - Sessions: `data/auth/sessions.db` + `data/auth/session-secret`
  - Unauthenticated `/api/*` → `401`; `GET /` → redirect to `/login`
  - `POST /telegram` and `POST /telegram/:companion` are registered **before** `setupAuth`, so Telegram webhooks do not need a session cookie.
  - Server self-calls (companion selfies, voice photo jobs, Telegram → `/chat`, etc.) use loopback `x-internal-auth` header with a boot-generated secret shared in-process — no config required.

### Startup (`start.sh`)

**Environment**

- Sets `LR_PROFILE`, `LR_HOME`
- Reads Anthropic key from `data/settings.json` (migrates legacy repo-root `settings.json` once if canonical file is missing)
- Exports `ANTHROPIC_API_KEY`, `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`
- Activates `$LR_HOME/venv` when present
- Requires: `python3`, `node`, `npx`, `lsof`, `curl`

**Startup sequence**

1. Clear ports `${PORT}`, `${TANEVAN_PROXY_PORT:-5001}`, `${WHISPER_PORT:-5555}`
2. **Whisper** (`voice/whisper_server.py`) → `:5555`
3. **UI server** (`server.js` or `nodemon`) → `:PORT` (default `3000`)
4. Wait up to 20s for UI to respond on `/`
5. **Tanevan** (`tanevan/proxy.py`) → `:5001` **only if** at least one companion exists in `data/companions/*.json` or `TANEVAN_COMPANION_NAME` is set

If step 5 is skipped, the app still runs; memory features require creating a companion and re-running `./start.sh`.

**Logs**

- `~/.love-refactored/ui.log`
- `~/.love-refactored/whisper.log`
- `~/.love-refactored/tanevan.log`

### Tanevan (`tanevan/proxy.py`)

**Supported env keys**

- `TANEVAN_PROXY_PORT` (default `5001`)
- `TANEVAN_PROXY_HOST` (default `127.0.0.1`)
- `TANEVAN_LM_STUDIO_URL` (default `http://localhost:1234/v1/chat/completions`)
- `TANEVAN_SUMMARIZE_EVERY` (default `100`)
- `TANEVAN_COMPANION_NAME` (override default companion)
- `TANEVAN_USER_NAME` (default `the user`)
- `TANEVAN_DATA_DIR` (default `~/tanevan-data`)
- `LR_HOME` (Love Refactored install root for settings resolution)
- `ANTHROPIC_API_KEY` (env or settings fallback)

**Pipeline config**

- Stored at `${TANEVAN_DATA_DIR}/pipeline_config.json` (editable from Settings UI)
- Steps: `summarizer`, `extractor`, `updater`, `reflect` — each can target local, OpenAI, Anthropic, OpenRouter, or hybrid

**HTTP contract**

- `GET /health` — JSON status + memory stats
- `POST /transcribe` is **not** on Tanevan (Whisper owns transcription)
- Memory CRUD, flush, import, config, reflections: see `routes/companions.js` proxies
- Tanevan also exposes `POST /reflect`, `GET /reflections`, `GET /living-narrative`, `GET /reflection-injection`

**Anthropic key loading order**

1. `ANTHROPIC_API_KEY` env
2. `data/settings.json` (via `LR_HOME` / `lib/lr_settings.py`)

### Whisper (`voice/whisper_server.py`)

- Bind: `127.0.0.1:5555` (not configurable in script today)
- `GET /health` → `{ status: "ok", service: "whisper" }`
- `POST /transcribe` → requires `audio` file; `400` without file is valid
- No API key required

### Optional Local TTS Services

Not started by `start.sh`. Required only when `settings.voiceMemo.provider` points at a local backend.

- **Fish** (`voice/fish_tts_server.py`) — port `5050`, needs `FISH_AUDIO_API_KEY`
- **Turbo / NeuTTS** (`voice/server_turbo.py`, `voice/server_neutts.py`) — port `5050`, RVC via Applio

All local TTS servers write MP3s to **`${LR_HOME}/data/voice_messages/`** via `lib/lr_settings.ensure_voice_messages_dir()`. The Node app serves those files at `/api/voice-message/:filename`.

Cloud providers (ElevenLabs, etc.) use vendor APIs directly from `server.js` and also save into `data/voice_messages/` — no local TTS process needed.

## Required vs Optional for Deployments

### Required (both local + VPS)

- Node app on configured `PORT`
- Valid Anthropic API key (chat + memory pipeline)
- At least one companion in `data/companions/` for Tanevan to start
- Tanevan reachable at resolved URL (after companions exist)
- Whisper reachable at resolved URL

### Optional

- `auth.js` (required for public/internet exposure)
- Any TTS provider (`none` is valid)
- Local `:5050` voice servers
- ElevenLabs / Fish / Replicate credentials unless those features are enabled

## Storage Ownership

| Data | Location | Notes |
|---|---|---|
| Active chat history | `data/love.db` (`chat_messages`) | Source of truth via `db/chat.js` |
| Chat audit trail | `data/love.db` (`chat_log`) + JSONL shadow in `data/chat_logs/` | Viewer/export; not authoritative for live history |
| Legacy JSON history | `data/chat_history/*.json` | Not written after SQLite migration; run `npm run migrate-chat` once |
| Companion cards | `data/companions/*.json` | |
| Settings | `data/settings.json` | |
| Tanevan memories | `~/tanevan-data/<companion>/memories.db` | Per-companion SQLite |

## Local Defaults

- `LR_PROFILE=local`
- `PORT=3000`
- `TANEVAN_URL=http://127.0.0.1:5001`
- `settings.whisper.url=http://127.0.0.1:5555`
- `settings.voiceMemo.provider=none` (often overridden per deploy)

## VPS Defaults

- `LR_PROFILE=vps`
- `LR_HOME=/opt/love-refactored`
- `PORT` set by service manager / reverse proxy
- `TANEVAN_URL` explicit via env (recommended)
- `settings.whisper.url` explicit (or `127.0.0.1:5555` if co-located)
- `auth.js` + admin account required before exposing to the internet
- `AUTH_TRUST_PROXY=1` when behind nginx/Caddy/Tailscale Serve

## Smoke Checks (Contract Validation)

Run `npm run check` first. Then:

**Python services (no auth)**

- `GET http://127.0.0.1:5001/health` → `200` JSON
- `GET http://127.0.0.1:5555/health` → `200` JSON
- `POST http://127.0.0.1:5555/transcribe` (no file) → `400`

**Web app**

- Without `auth.js`: `GET /` → `200` HTML
- With `auth.js`: `GET /` → `302` to `/login`; `GET /login` → `200`; `/api/*` without session → `401`
- Logged-in browser: chat round-trip, memory stats load, Test Memory succeeds

**Voice (browser, logged in if auth enabled)**

- `provider=none` → `voice_disabled` skip response
- Unavailable local provider → `tts_unavailable` skip response (chat unaffected)

Full step-by-step: `VERIFICATION-RUNBOOK.md`.
