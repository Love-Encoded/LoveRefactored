# Failure Matrix (Local + VPS)

This matrix captures current breakpoints after defining the runtime contract.

## Snapshot

- Date: 2026-06-25
- Contract source: `RUNTIME-CONTRACT.md`
- Local stack state checked against currently running services
- Reflections are in (Tanevan `/reflect` + Node `memory.reflectionSchedule`). Dreams and psyche synthesis are not part of this product.

## Local Observed Results

| Area | Check | Result | Status |
|---|---|---|---|
| Web app (no session) | `GET /` on `:3000` | `302` → login | healthy (auth enabled) |
| Web app (no session) | `GET /api/*` on `:3000` | `401` | expected when `auth.js` present |
| Memory service | `GET /health` on `:5001` | `200` | healthy |
| Whisper health | `GET /health` on `:5555` | `200` | healthy |
| Whisper transcription | `POST /transcribe` on `:5555` (no file) | `400` | healthy endpoint contract |
| Optional local TTS | `GET /health` on `:5050` | `000` (unreachable) | unavailable (ok if using cloud TTS) |
| App TTS API (auth + provider none) | `POST /api/tts` without session | `401` | auth wall (test in browser when logged in) |

## Confirmed Failure/Behavior Cases

| ID | Scenario | Current Behavior | Severity | Profile Impact |
|---|---|---|---|---|
| FM-01 | Local TTS server not running (`:5050`) | App returns graceful `tts_unavailable` (no hard failure) | low | local + vps |
| FM-02 | Voice disabled (`provider=none`) | Explicit `voice_disabled` skip response | expected | local + vps |
| FM-03 | Auth enabled (`auth.js` present) | Unauthenticated `curl` to `/api/*` returns `401`; `GET /` redirects to `/login` | expected | local + vps |
| FM-04 | Cloud voice provider (e.g. ElevenLabs) | Works without local TTS on `:5050` | expected | local + vps |

## Resolved (no longer open)

| ID | Was | Resolution |
|---|---|---|
| ~~FM-02 (old)~~ | Whisper `/health` returned 404 | `voice/whisper_server.py` now exposes `GET /health` → `200` |
| ~~VR-04~~ | Whisper monitoring false negative | Same as above |
| ~~VR-05~~ | Duplicate Spotify routes in `server.js` | Routes owned by `routes/spotify.js` only |
| ~~Reflection~~ | Experimental reflection scheduler called missing Tanevan `/reflect` | Reflections shipped: Tanevan `/reflect` plus Node `memory.reflectionSchedule`. Dreams/psyche stay out of this product. |

## VPS Risk Cases (Static Analysis)

| ID | Scenario | Evidence | Likely Failure | Severity |
|---|---|---|---|---|
| VR-01 | Fresh install with zero companions | `tanevan/proxy.py` exits if no default companion; `start.sh` skips Tanevan when companion count is 0 | memory service absent until first companion exists + restart | medium |
| VR-02 | Settings path drift between services | ~~Mixed legacy fallbacks~~ | **Resolved** — `lib/lr_settings.py` + one-time migration in `start.sh` | — |
| VR-03 | Voice file path drift in optional TTS servers | ~~Non-`data/` path defaults~~ | **Resolved** — `lib/lr_settings.voice_messages_dir()` + `start.sh` export | — |
| VR-04 | Auth wall blocks unauthenticated monitoring | `auth.js` returns `401` for `/api/*` without session | automated curl health checks look broken while app is fine | low |

## Root-Cause Themes

1. Environment/profile assumptions are spread across scripts/services.
2. ~~Legacy fallback paths still coexist with `data/` canonical path~~ — resolved via `lib/lr_settings.py`.
3. Auth-enabled deploys require browser or session-cookie checks for app APIs.
4. Startup on empty state depends on at least one companion existing.

## Recommended Next Fix Order

1. `config-parity`: ~~enforce one config precedence path~~ **done** (`lib/lr_settings.py`, `start.sh` migration).
2. `startup-parity`: clearer first-run messaging when Tanevan is skipped (no companions yet).
3. ~~`service-path-parity`: align optional TTS file paths to `data/voice_messages`~~ **done**.
4. `docs-parity`: keep runbook/matrix aligned after auth + route extraction changes.

## Command Evidence (local, 2026-06-25)

Python services (no auth required):

```bash
curl -s -o /dev/null -w "tanevan_health_http=%{http_code}\n" http://127.0.0.1:5001/health
curl -s -o /dev/null -w "whisper_health_http=%{http_code}\n" http://127.0.0.1:5555/health
curl -s -o /dev/null -w "whisper_transcribe_http=%{http_code}\n" -X POST http://127.0.0.1:5555/transcribe
```

App (auth-aware):

```bash
curl -s -o /dev/null -w "app_root_http=%{http_code}\n" http://127.0.0.1:3000/
curl -sL -o /dev/null -w "app_login_http=%{http_code}\n" http://127.0.0.1:3000/login
curl -s -o /dev/null -w "api_companions_http=%{http_code}\n" http://127.0.0.1:3000/api/companions
```

Expected with `auth.js`: `app_root_http=302`, `app_login_http=200`, `api_companions_http=401`.
