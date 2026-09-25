#!/usr/bin/env python3
"""
LR STT proxy — routed backend, mp3 payloads, retry + audio-preservation on failure.

Same /transcribe contract (POST form 'audio' -> {"text": "..."}). Serves BOTH
voice memos (server.js) AND voice calls (Pipecat LRWhisperHTTPSTTService -> port 5555).

Backend switch: STT_BACKEND=voxtral|fish|mistral (default fish). Rollback = flip + restart.

mistral backend (added 2026-08-07): Mistral La Plateforme direct, Voxtral Mini
Transcribe V2 (voxtral-mini-2602, pinned dated string — never "-latest", we do
not ride silent model swaps). Multipart upload per Mistral docs. Optional
context biasing via STT_MISTRAL_BIAS (comma-separated terms, max 100, DEFAULT
OFF — field format for multiple terms is unverified against their API, so first
tests run without it; enable deliberately, watch the log, flip off if it 4xxs).
Diarization exists on this endpoint but is NOT wired yet — response shape
differs and gets its own verified patch later, never a blind flag.

Robustness (added 2026-06-14, after silent memo loss during a panic meltdown):
  * Compressed inputs (webm/ogg from memos) are transcoded to MP3, not WAV.
    WAV is raw and ~10x larger; long memos were exceeding OpenRouter's payload
    size and returning 502 Bad Gateway. MP3 keeps payloads small.
    -> This path matters for MEMOS (one long blob).
  * Call utterances arrive as WAV already (small, per-utterance, VAD-gated) and
    pass through UNCONVERTED, so live-call latency is not affected.
  * Transient failures (timeouts, connection errors, 5xx) are retried once.
  * On final failure the audio is SAVED to {LR_HOME}/data/voice_stt_failures/
    and a loud, structured error (HTTP 502) is returned. Words are never silently
    lost — the recording is always recoverable for re-transcription.
  * Each request logs its outbound payload size for visibility.

NO duration or size cap is imposed: the production log proved length is NOT the
limiter (140s clips transcribed fine). If any hard cap is ever added later it
MUST be surfaced to the user explicitly, never silent.

Env ({LR_HOME}/.env):
    STT_BACKEND          "voxtral" | "fish" | "mistral"   (default: fish)
    OPENROUTER_API_KEY   required for voxtral
    STT_VOXTRAL_MODEL    default mistralai/voxtral-mini-transcribe
    FISH_AUDIO_API_KEY   required for fish
    MISTRAL_API_KEY      required for mistral
    STT_MISTRAL_MODEL    default voxtral-mini-2602 (pinned; avoid -latest)
    STT_MISTRAL_BIAS     comma-separated bias terms, default "" = off (experimental)
    STT_SUSPECT_MIN_SECS default 8   — clips at least this long are checked for
    STT_SUSPECT_CPS      default 0.75 — ...yielding under this many chars/sec:
                         text still delivered, but audio is SAVED + SUSPECT logged.
                         Set either to 0 to disable. (Added 2026-08-09 after
                         silent word-eating on 200 responses; thresholds are
                         first guesses, tune against real traffic.)
    STT_HTTP_TIMEOUT     seconds, default 75
    STT_FALLBACK_BACKEND "" (off) | "fish" | "mistral" | "voxtral"  — SECOND EAR.
                         (Added 2026-08-31 after Voxtral returned empty text on
                         14s of stuttered/strained speech during a call.) When
                         the primary backend fails outright OR returns empty
                         text, the SAME audio is sent once to this backend
                         before giving up. Different model, different bias.
                         Bounded: primary (1 try + 1 transient retry) then
                         fallback (1 try + 1 transient retry). No loops.
                         Must differ from STT_BACKEND; its API key must be set.
    STT_FALLBACK_ON_SUSPECT default 0 — if 1, ALSO run the fallback when the
                         primary SUSPECT heuristic fires (long clip, little
                         text) and deliver the LONGER of the two results.
                         Adds one fallback call of latency on suspect turns
                         only. Off by default; flip deliberately.
    STT_SAVE_RECOVERED   default 1 — when the fallback succeeds, the audio the
                         primary erased is SAVED to {FAIL_DIR}/recovered_*
                         (this is the corpus of speech the primary model
                         cannot hear). Set 0 to disable.
    LR_HOME              default /opt/love-refactored
    PERCEPTION_TEE_URL   default "" = TEE DISABLED. Set to the sidecar's
                         /perceive endpoint (http://127.0.0.1:5060/perceive)
                         to mirror each utterance to the perception sidecar.
                         Fire-and-forget on a daemon thread: NEVER blocks,
                         delays, retries, or fails the transcription path.
                         Tee failures log loudly (PERCEPTION-TEE-FAIL) and
                         are otherwise ignored — fail-open by decision
                         (Tiara, 2026-08-12).
"""
import base64
import json
import os
import re
import sys
import threading
import time
import uuid

import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

LR_HOME = os.environ.get("LR_HOME", "/opt/love-refactored")
load_dotenv(os.path.join(LR_HOME, ".env"))

STT_BACKEND = os.environ.get("STT_BACKEND", "mistral").strip().lower()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
VOXTRAL_MODEL = os.environ.get("STT_VOXTRAL_MODEL", "mistralai/voxtral-mini-transcribe")
FISH_API_KEY = os.environ.get("FISH_AUDIO_API_KEY", "")
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "")
MISTRAL_STT_MODEL = os.environ.get("STT_MISTRAL_MODEL", "voxtral-mini-2602")
MISTRAL_STT_BIAS = [t.strip() for t in os.environ.get("STT_MISTRAL_BIAS", "").split(",") if t.strip()]
SUSPECT_MIN_SECS = float(os.environ.get("STT_SUSPECT_MIN_SECS", "8"))
SUSPECT_CPS = float(os.environ.get("STT_SUSPECT_CPS", "0.75"))
HTTP_TIMEOUT = float(os.environ.get("STT_HTTP_TIMEOUT", "75"))
FALLBACK_BACKEND = os.environ.get("STT_FALLBACK_BACKEND", "").strip().lower()
FALLBACK_ON_SUSPECT = os.environ.get("STT_FALLBACK_ON_SUSPECT", "0").strip() == "1"
SAVE_RECOVERED = os.environ.get("STT_SAVE_RECOVERED", "1").strip() == "1"

PERCEPTION_TEE_URL = os.environ.get("PERCEPTION_TEE_URL", "").strip()

OPENROUTER_STT_URL = "https://openrouter.ai/api/v1/audio/transcriptions"
MISTRAL_STT_URL = "https://api.mistral.ai/v1/audio/transcriptions"
FISH_ASR_URL = "https://api.fish.audio/v1/asr"
CORRECTIONS_PATH = os.path.join(LR_HOME, "data", "stt_corrections.json")
FAIL_DIR = os.path.join(LR_HOME, "data", "voice_stt_failures")

app = Flask(__name__)


# ---------------------------------------------------------------- corrections
def load_corrections():
    try:
        with open(CORRECTIONS_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict) and data:
            print(f"[STT] Loaded {len(data)} corrections from {CORRECTIONS_PATH}", flush=True)
            return {str(k): str(v) for k, v in data.items()}
    except FileNotFoundError:
        print(f"[STT] No corrections file at {CORRECTIONS_PATH} (hook idle)", flush=True)
    except Exception as e:
        print(f"[STT] WARNING: corrections file unreadable ({e}) — hook idle", flush=True)
    return {}


CORRECTIONS = load_corrections()


def apply_corrections(text):
    if not text or not CORRECTIONS:
        return text
    for wrong, right in CORRECTIONS.items():
        text = re.sub(re.escape(wrong), right, text, flags=re.IGNORECASE)
    return text


# ---------------------------------------------------------------- audio prep
def prepare_audio(audio_file):
    """Read upload; choose the smallest sane upload format.

    webm/ogg (memos)  -> transcode to mp3 (small). Falls back to wav loudly if the
                         mp3 encoder is missing.
    wav (call turns)  -> pass through unchanged (small already; no added latency).
    mp3               -> pass through.

    Returns (data_bytes, filename, mimetype, api_format).
    """
    raw = audio_file.read()
    mimetype = audio_file.mimetype or "application/octet-stream"
    filename = audio_file.filename or "audio"

    if mimetype in ("audio/webm", "audio/ogg", "video/webm"):
        try:
            import io
            from pydub import AudioSegment
            seg = AudioSegment.from_file(io.BytesIO(raw))
            try:
                buf = io.BytesIO()
                seg.export(buf, format="mp3", bitrate="64k")
                data = buf.getvalue()
                print(f"[STT] Transcoded {mimetype} -> mp3 ({len(raw)} -> {len(data)} bytes)", flush=True)
                return data, "audio.mp3", "audio/mpeg", "mp3"
            except Exception as mp3_err:
                print(f"[STT] WARNING: mp3 encode failed ({mp3_err}); falling back to wav. "
                      f"Install ffmpeg w/ libmp3lame on the VPS to shrink memo payloads.", flush=True)
                buf = io.BytesIO()
                seg.export(buf, format="wav")
                data = buf.getvalue()
                print(f"[STT] Converted {mimetype} -> wav ({len(data)} bytes)", flush=True)
                return data, "audio.wav", "audio/wav", "wav"
        except Exception as conv_err:
            print(f"[STT] Conversion failed ({conv_err}); sending raw", flush=True)
            return raw, filename, mimetype, "wav"

    if filename.lower().endswith(".mp3") or mimetype == "audio/mpeg":
        return raw, filename, mimetype, "mp3"

    # wav and anything else: pass through (live-call utterances land here)
    return raw, (filename if filename != "audio" else "audio.wav"), \
        (mimetype if mimetype != "application/octet-stream" else "audio/wav"), "wav"


# ---------------------------------------------------------------- backend outcomes
class EmptyTranscript(ValueError):
    """Backend answered 200 but with no words. Never passed downstream."""


class SuspectTranscript(Exception):
    """Backend returned implausibly little text for the clip length.
    Carries the text so the caller can still deliver it if no fallback."""
    def __init__(self, text, secs, saved):
        super().__init__(f"suspect: {secs}s audio -> only {len(text)} chars")
        self.text, self.secs, self.saved = text, secs, saved


def require_text(text, backend, detail=""):
    """Every backend goes through this: 200-with-empty is a silent eat,
    refuse to pass it on. (Previously only the mistral path checked.)"""
    if not (text or "").strip():
        raise EmptyTranscript(f"{backend} returned 200 but empty text{detail}")
    return text


# ---------------------------------------------------------------- backends
def transcribe_voxtral(data, filename, mimetype, fmt):
    b64 = base64.b64encode(data).decode()
    print(f"[STT voxtral] sending {fmt} payload ~{len(b64)/1_000_000:.1f}MB", flush=True)
    resp = requests.post(
        OPENROUTER_STT_URL,
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
        json={"input_audio": {"data": b64, "format": fmt}, "model": VOXTRAL_MODEL},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    out = resp.json()
    usage = out.get("usage") or {}
    if usage:
        print(f"[STT voxtral] {usage.get('seconds', '?')}s audio, cost {usage.get('cost', '?')}", flush=True)
    return require_text(out.get("text", ""), "voxtral")


def transcribe_fish(data, filename, mimetype, fmt):
    resp = requests.post(
        FISH_ASR_URL,
        headers={"Authorization": f"Bearer {FISH_API_KEY}"},
        files={"audio": (filename, data, mimetype)},
        data={"language": "en"},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    text = resp.json().get("text", "")
    print(f"[STT fish] got {len(text)} chars / {len(text.split())} words", flush=True)
    return require_text(text, "fish")


def transcribe_mistral(data, filename, mimetype, fmt):
    """Mistral La Plateforme /v1/audio/transcriptions — multipart, per their docs.

    Verified 2026-08-07 against docs.mistral.ai (offline transcription page) and
    Mistral's published curl example: -F model=... -F file=@... with Bearer auth.
    We deliberately do NOT send: language (parity with the whisper path; also a
    forced "en" lens could suppress reo words — revisit as a test lever),
    timestamp_granularities (unused), diarize (needs its own response handling).
    Bias terms, when enabled, are sent as repeated context_bias form fields —
    this repeated-field format is UNVERIFIED against their API; if it 4xxs,
    unset STT_MISTRAL_BIAS and restart. Failures are loud either way.
    """
    print(f"[STT mistral] sending {fmt} ~{len(data)/1_000_000:.1f}MB "
          f"model={MISTRAL_STT_MODEL} bias_terms={len(MISTRAL_STT_BIAS)}", flush=True)
    form = [("model", (None, MISTRAL_STT_MODEL))]
    for term in MISTRAL_STT_BIAS[:100]:
        form.append(("context_bias", (None, term)))
    form.append(("file", (filename, data, mimetype)))
    resp = requests.post(
        MISTRAL_STT_URL,
        headers={"Authorization": f"Bearer {MISTRAL_API_KEY}"},
        files=form,
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    out = resp.json()
    text = out.get("text", "")
    usage = out.get("usage") or {}
    secs = usage.get("prompt_audio_seconds")
    print(f"[STT mistral] got {len(text)} chars / {len(text.split())} words"
          + (f" from {secs}s audio" if secs else ""), flush=True)
    if usage:
        print(f"[STT mistral] usage: {usage}", flush=True)
    # 200 with empty text is a silent eat. Refuse to pass it downstream:
    # raising sends us to the fallback (if configured) and then the failure
    # path, which SAVES the audio and returns a loud 502. Words never vanish.
    require_text(text, "mistral", f" ({secs}s audio, keys: {list(out)})")
    if (SUSPECT_MIN_SECS > 0 and SUSPECT_CPS > 0 and secs
            and secs >= SUSPECT_MIN_SECS and len(text) < secs * SUSPECT_CPS):
        # Long clip, implausibly little text: keep the audio and flag it.
        saved = save_failed_audio(data, fmt)
        print(f"[STT mistral] SUSPECT: {secs}s audio -> only {len(text)} chars "
              f"(< {SUSPECT_CPS}/s). Audio kept at {saved}", flush=True)
        if FALLBACK_ON_SUSPECT and FALLBACK_BACKEND:
            # Caller runs the second ear and delivers the longer result.
            raise SuspectTranscript(text, secs, saved)
        print(f"[STT mistral] SUSPECT text delivered as-is (fallback-on-suspect off)", flush=True)
    return text


BACKENDS = {"voxtral": transcribe_voxtral, "fish": transcribe_fish, "mistral": transcribe_mistral}


# ---------------------------------------------------------------- failure handling
def is_transient(exc):
    """Retry on timeouts, connection drops, and 5xx (incl. 502). Not on 4xx."""
    if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return 500 <= exc.response.status_code < 600
    return False


def save_failed_audio(data, fmt):
    """Persist audio that failed transcription so words are never lost."""
    try:
        os.makedirs(FAIL_DIR, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(FAIL_DIR, f"stt_fail_{ts}_{uuid.uuid4().hex[:6]}.{fmt}")
        with open(path, "wb") as f:
            f.write(data)
        return path
    except Exception as e:
        print(f"[STT] WARNING: could not save failed audio: {e}", flush=True)
        return None


def save_recovered_audio(data, fmt):
    """Audio the primary backend erased but the fallback heard. Kept as the
    corpus of speech the primary cannot hear. Never blocks delivery."""
    if not SAVE_RECOVERED:
        return None
    try:
        os.makedirs(FAIL_DIR, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(FAIL_DIR, f"recovered_{ts}_{uuid.uuid4().hex[:6]}.{fmt}")
        with open(path, "wb") as f:
            f.write(data)
        return path
    except Exception as e:
        print(f"[STT] WARNING: could not save recovered audio: {e}", flush=True)
        return None


# ---------------------------------------------------------------- perception tee
def tee_to_perception(data, filename, mimetype):
    """Mirror the utterance to the perception sidecar. Fire-and-forget.

    Runs on a daemon thread; a short timeout bounds the thread's life. No
    retry (one shot only — perception is best-effort by decided policy).
    ANY failure here is logged loudly and swallowed: the transcription
    path must be completely unaware this exists.
    """
    if not PERCEPTION_TEE_URL:
        return

    def _post():
        try:
            requests.post(
                PERCEPTION_TEE_URL,
                files={"audio": (filename, data, mimetype)},
                data={"turn_id": uuid.uuid4().hex[:8]},
                timeout=5,
            )
        except Exception as e:
            print(f"[PERCEPTION-TEE-FAIL] {e!r}", flush=True)

    threading.Thread(target=_post, daemon=True).start()


# ---------------------------------------------------------------- one ear
def run_backend(name, data, filename, mimetype, fmt):
    """Run one backend: 1 try + 1 retry on transient errors only.
    Returns the text (str) on success, or the final exception object.
    EmptyTranscript/SuspectTranscript are NOT transient (same audio, same
    model, same answer) — they come straight back to the caller."""
    backend = BACKENDS[name]
    last_exc = None
    for attempt in (1, 2):
        try:
            return backend(data, filename, mimetype, fmt)
        except Exception as e:
            last_exc = e
            if attempt == 1 and is_transient(e):
                print(f"[STT {name}] transient error ({e}); retrying once...", flush=True)
                time.sleep(1.5)
                continue
            break
    return last_exc


# ---------------------------------------------------------------- routes
@app.route("/transcribe", methods=["POST"])
def transcribe():
    audio_file = request.files.get("audio")
    if not audio_file:
        return jsonify({"error": "No audio file"}), 400

    try:
        data, filename, mimetype, fmt = prepare_audio(audio_file)
    except Exception as e:
        print(f"[STT] prepare_audio error: {e}", flush=True)
        return jsonify({"error": f"audio preparation failed: {e}"}), 500

    tee_to_perception(data, filename, mimetype)  # fire-and-forget; see fn docstring

    def deliver(text, via):
        corrected = apply_corrections(text)
        if corrected != text:
            print(f"[STT] corrections applied: {text!r} -> {corrected!r}", flush=True)
        return jsonify({"text": corrected, "via": via})

    # ---- primary ear: 1 try + 1 transient retry. Bounded, no loop.
    primary_exc = run_backend(STT_BACKEND, data, filename, mimetype, fmt)
    if isinstance(primary_exc, str):
        return deliver(primary_exc, STT_BACKEND)

    # ---- second ear (if configured): same audio, different model. Once.
    if FALLBACK_BACKEND and FALLBACK_BACKEND != STT_BACKEND:
        why = (f"SUSPECT ({primary_exc.secs}s -> {len(primary_exc.text)} chars)"
               if isinstance(primary_exc, SuspectTranscript) else f"{primary_exc!r}")
        print(f"[STT FALLBACK] {STT_BACKEND} failed: {why} -> trying {FALLBACK_BACKEND}", flush=True)
        fb = run_backend(FALLBACK_BACKEND, data, filename, mimetype, fmt)
        if isinstance(fb, str):
            if isinstance(primary_exc, SuspectTranscript) and len(primary_exc.text) >= len(fb):
                print(f"[STT FALLBACK] {FALLBACK_BACKEND} heard {len(fb)} chars, "
                      f"primary had {len(primary_exc.text)}; keeping primary", flush=True)
                return deliver(primary_exc.text, STT_BACKEND)
            saved = save_recovered_audio(data, fmt)
            print(f"[STT FALLBACK] RECOVERED via {FALLBACK_BACKEND}: {len(fb)} chars / "
                  f"{len(fb.split())} words — {fb!r}" + (f" — audio kept at {saved}" if saved else ""),
                  flush=True)
            return deliver(fb, FALLBACK_BACKEND)
        print(f"[STT FALLBACK] {FALLBACK_BACKEND} also failed: {fb!r}", flush=True)
        last_exc = fb
    else:
        last_exc = primary_exc

    # Primary SUSPECT but fallback unavailable/failed: still deliver what we have.
    if isinstance(primary_exc, SuspectTranscript):
        print(f"[STT {STT_BACKEND}] SUSPECT text delivered (fallback did not improve)", flush=True)
        return deliver(primary_exc.text, STT_BACKEND)

    # Every ear failed: preserve the audio, surface loudly. Nothing vanishes.
    saved = save_failed_audio(data, fmt)
    print(f"[STT {STT_BACKEND}] error detail: {last_exc!r}", flush=True)
    print(f"[STT {STT_BACKEND}] FAILED (fallback={FALLBACK_BACKEND or 'off'}) — "
          f"audio saved to {saved} — {last_exc}", flush=True)
    return jsonify({
        "error": f"transcription failed: {last_exc}",
        "audio_saved": saved,
        "recoverable": bool(saved),
    }), 502


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "backend": STT_BACKEND,
        "model": {"voxtral": VOXTRAL_MODEL, "fish": "fish-asr",
                  "mistral": MISTRAL_STT_MODEL}.get(STT_BACKEND, "?"),
        "bias_terms": len(MISTRAL_STT_BIAS) if STT_BACKEND == "mistral" else None,
        "fallback_backend": FALLBACK_BACKEND or None,
        "fallback_on_suspect": FALLBACK_ON_SUSPECT,
        "save_recovered": SAVE_RECOVERED,
        "corrections_loaded": len(CORRECTIONS),
        "http_timeout": HTTP_TIMEOUT,
        "fail_dir": FAIL_DIR,
    })


if __name__ == "__main__":
    if STT_BACKEND not in BACKENDS:
        print(f"ERROR: STT_BACKEND must be one of {list(BACKENDS)} (got {STT_BACKEND!r})")
        sys.exit(1)
    if STT_BACKEND == "voxtral" and not OPENROUTER_API_KEY:
        print(f"ERROR: STT_BACKEND=voxtral but OPENROUTER_API_KEY not set in {LR_HOME}/.env")
        sys.exit(1)
    if STT_BACKEND == "fish" and not FISH_API_KEY:
        print(f"ERROR: STT_BACKEND=fish but FISH_AUDIO_API_KEY not set in {LR_HOME}/.env")
        sys.exit(1)
    if STT_BACKEND == "mistral" and not MISTRAL_API_KEY:
        print(f"ERROR: STT_BACKEND=mistral but MISTRAL_API_KEY not set in {LR_HOME}/.env")
        sys.exit(1)
    if FALLBACK_BACKEND:
        if FALLBACK_BACKEND not in BACKENDS:
            print(f"ERROR: STT_FALLBACK_BACKEND must be one of {list(BACKENDS)} (got {FALLBACK_BACKEND!r})")
            sys.exit(1)
        if FALLBACK_BACKEND == STT_BACKEND:
            print(f"ERROR: STT_FALLBACK_BACKEND must differ from STT_BACKEND (both {STT_BACKEND!r})")
            sys.exit(1)
        need = {"voxtral": OPENROUTER_API_KEY, "fish": FISH_API_KEY, "mistral": MISTRAL_API_KEY}[FALLBACK_BACKEND]
        if not need:
            print(f"ERROR: STT_FALLBACK_BACKEND={FALLBACK_BACKEND} but its API key is not set in {LR_HOME}/.env")
            sys.exit(1)
    STT_HOST = os.environ.get("STT_HOST", "127.0.0.1")
    STT_PORT = int(os.environ.get("STT_PORT", "5555"))
    print(f"[STT] Proxy on {STT_HOST}:{STT_PORT} — backend: {STT_BACKEND}, "
          f"fallback: {FALLBACK_BACKEND or 'OFF'}, fallback_on_suspect: {FALLBACK_ON_SUSPECT}, "
          f"save_recovered: {SAVE_RECOVERED}, timeout {HTTP_TIMEOUT}s", flush=True)
    app.run(host=STT_HOST, port=STT_PORT)
