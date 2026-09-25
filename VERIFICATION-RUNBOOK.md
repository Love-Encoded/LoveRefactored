# Verification Runbook (Local + VPS)

Use this checklist after changes to confirm Love Refactored is healthy and parity-safe.

## 0) Preflight

From repo root:

```bash
pwd
```

Expected: path ends with `love-refactored`.

Syntax checks (preferred — covers routes and DB modules):

```bash
npm run check
```

Expected: no output (success).

## 1) Start Services

```bash
./stop.sh
./start.sh
```

Expected:

- UI starts and opens on app port (default `3000`)
- Whisper starts (`5555`)
- Tanevan starts (`5001`) or a clear skip warning appears if no companion exists yet

Startup order in `start.sh`: Whisper → UI → Tanevan (Tanevan only when companions exist).

## 2) Health Checks

### 2a) Python services (no auth)

These endpoints are always reachable without a login session:

```bash
curl -s -o /dev/null -w "tanevan_health_http=%{http_code}\n" http://127.0.0.1:5001/health
curl -s -o /dev/null -w "whisper_health_http=%{http_code}\n" http://127.0.0.1:5555/health
curl -s -o /dev/null -w "whisper_transcribe_http=%{http_code}\n" -X POST http://127.0.0.1:5555/transcribe
```

Expected:

- `tanevan_health_http=200`
- `whisper_health_http=200`
- `whisper_transcribe_http=400` (no file is fine; endpoint exists)

### 2b) Web app (auth-aware)

If `auth.js` is **not** present (localhost-only dev):

```bash
curl -s -o /dev/null -w "app_root_http=%{http_code}\n" http://127.0.0.1:3000/
```

Expected: `app_root_http=200`

If `auth.js` **is** present (typical secured local/VPS deploy):

```bash
curl -s -o /dev/null -w "app_root_http=%{http_code}\n" http://127.0.0.1:3000/
curl -sL -o /dev/null -w "app_login_http=%{http_code}\n" http://127.0.0.1:3000/login
curl -s -o /dev/null -w "api_companions_http=%{http_code}\n" http://127.0.0.1:3000/api/companions
```

Expected:

- `app_root_http=302` (redirect to login)
- `app_login_http=200`
- `api_companions_http=401`

For full app API verification when auth is enabled, use the browser while logged in (see section 2c).

Auth setup (internet-facing deploys):

```bash
cp auth.example.js auth.js
node scripts/auth-users.js add <username> --role admin
./start.sh
```

### 2c) Browser smoke test (logged in)

With the app open at `http://localhost:3000` after login:

- [ ] Chat sends and receives a reply
- [ ] Memory dashboard loads stats for a companion
- [ ] Settings → Memory → Test Memory succeeds
- [ ] Voice memo works (or fails gracefully with a skip reason, not a hard 500)

## 3) Config Resolution Checks

```bash
python3 -c 'import json; s=json.load(open("data/settings.json")); print("voiceMemo.provider=", (s.get("voiceMemo") or {}).get("provider")); print("memory.tanevUrl=", (s.get("memory") or {}).get("tanevUrl")); print("whisper.url=", (s.get("whisper") or {}).get("url"))'
```

Expected:

- Values are present and match intended deployment profile
- `memory.tanevUrl` → `http://127.0.0.1:5001` (or your VPS internal URL)
- `whisper.url` → `http://127.0.0.1:5555`

`memory.reflectionSchedule` is the global nightly reflections default (enabled, typically `03:00` local). Companions opt in with `reflectionsEnabled: true`. Dreams and psyche synthesis are not part of this product.

## 4) Voice Fallback Contract Checks

Run these in the **browser** (Settings or devtools) when `auth.js` is present. Unauthenticated `curl` to `/api/tts` returns `401`.

### 4a) Voice disabled path

Temporarily set `voiceMemo.provider` to `none` in Settings, then trigger a voice memo or POST from the browser console:

```javascript
fetch('/api/tts', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ text: 'test', companion: 'Aria' })
}).then(r => r.json()).then(console.log)
```

Expected response includes:

- `"skipped": true`
- `"reason": "voice_disabled"`

Restore your normal provider afterward.

### 4b) Unavailable voice provider path

Set `voiceMemo.provider` to a local provider (e.g. `fish`) while the local TTS server is **not** running on `:5050`, then repeat the fetch above.

Expected response includes:

- `"skipped": true`
- `"reason": "tts_unavailable"`
- `"warning": ...`

Chat must continue (no hard 500 requirement for voice failure).

If using a cloud provider (e.g. `elevenlabs`), skip 4b — local TTS on `:5050` is not required.

## 5) Spotify Route Ownership Check

```bash
npm run check
grep -n "app.get('/api/spotify/status'" server.js || echo "OK: no duplicate in server.js"
```

Expected:

- Syntax check passes
- No inline duplicate route definitions in `server.js`; Spotify routes are owned by `routes/spotify.js`.

## 6) VPS-Specific Verification

Run with VPS profile/env (example):

```bash
export LR_PROFILE=vps
export LR_HOME=/opt/love-refactored
export PORT=3000
export TANEVAN_URL=http://127.0.0.1:5001
export WHISPER_URL=http://127.0.0.1:5555
```

Then run sections 1–2a and the browser smoke test (2c).

Expected:

- Same Python service outcomes as local
- App login wall works; APIs require session
- No hardcoded local-home path failures in logs

## 7) Logs to Inspect on Failure

```bash
tail -n 80 ~/.love-refactored/ui.log
tail -n 80 ~/.love-refactored/tanevan.log
tail -n 80 ~/.love-refactored/whisper.log
```

Common interpretations:

- Memory unreachable warning at boot can be transient during startup sequencing
- Persistent memory failure means `TANEVAN_URL` / service startup mismatch
- `tts_unavailable` with text response is expected if optional voice service is down
- `401 Unauthorized` from curl is expected for `/api/*` when `auth.js` is present — not a service failure

## 8) Exit Criteria (Release Gate)

Release is good when:

- `npm run check` passes
- Tanevan + Whisper health checks pass (section 2a)
- Browser smoke test passes while logged in (section 2c)
- Voice fallback contract passes in browser when relevant (section 4)
- Local and VPS profiles both satisfy sections 1–2 and 6
