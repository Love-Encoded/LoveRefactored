"""Pipecat voice call server for Love Refactored.

Bridges browser WebRTC audio to LR's /v1/chat/completions wrapper and Fish Audio TTS,
reusing the existing whisper_server.py for STT (no duplicate Whisper model on the GPU).

Architecture:
    Browser <-WebRTC->  Pipecat (this server)
                          |
                          +-> Silero VAD (end-of-utterance detection)
                          +-> LRWhisperHTTPSTTService -> POST /transcribe -> whisper_server.py
                          +-> OpenAILLMService -> LR /v1/chat/completions (X-Companion header)
                          +-> FishSDKTTSService -> Fish Audio SDK (per-utterance HTTP)
                          |
                          v
                       Browser audio out

Run:
    source ~/pipecat-venv/bin/activate
    python ~/love-refactored/voice/pipecat/voice_server.py

Env (read from $LR_HOME/.env unless overridden):
    LR_HOME             -- repo root (auto-detected if unset)
    FISH_AUDIO_API_KEY  -- required
    (FISH_VOICE_ID / COMPANION fallbacks REMOVED 2026-09-12: companion and
    fishVoiceId are strictly required in the browser offer and fail loudly
    with 400 when absent. No default voice exists in this system.)
    LR_BASE_URL         -- LR wrapper base URL (default: http://localhost:3000/v1)
    WHISPER_URL         -- whisper_server URL (default: http://127.0.0.1:5555)
    PIPECAT_HOST        -- bind address (default: 127.0.0.1)
    PIPECAT_PORT        -- HTTP port for the WebRTC signaling + test page (default: 7860)
"""

import asyncio
import re
import json
import io
import os
import wave
import urllib.parse
from dataclasses import dataclass
from typing import AsyncGenerator, Optional

import uuid
from datetime import datetime
from pathlib import Path

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.responses import StreamingResponse

from fish_audio_sdk import Session as FishSession, TTSRequest
from fish_audio_sdk.schemas import Prosody

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from loguru import logger
from pipecat.frames.frames import CancelFrame, EndFrame, ErrorFrame, Frame, InterimTranscriptionFrame, TranscriptionFrame, TTSSpeakFrame, UserStartedSpeakingFrame, UserStoppedSpeakingFrame, BotStartedSpeakingFrame, BotStoppedSpeakingFrame, VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame, InputAudioRawFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask, PipelineParams
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.services.settings import TTSSettings
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.services.tts_service import TTSService
from pipecat.utils.tracing.service_decorators import traced_tts
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.stt_service import STTService, SegmentedSTTService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    ConnectionMode,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.utils.time import time_now_iso8601


# === Config ===

def _default_lr_home():
    # voice/pipecat/voice_server.py -> repo root is three levels up
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LR_HOME = os.path.abspath(os.path.expanduser(os.environ.get("LR_HOME", _default_lr_home())))
_env_path = os.path.join(LR_HOME, ".env")
if os.path.exists(_env_path):
    load_dotenv(_env_path)
else:
    load_dotenv()

LR_BASE_URL = os.getenv("LR_BASE_URL", "http://localhost:3000/v1")
# LR's non-OpenAI endpoints (e.g. /api/voice-call/save-transcript) live one level
# up from the /v1 wrapper. Derive once so the save hook doesn't have to.
LR_API_BASE = LR_BASE_URL.rstrip("/")
if LR_API_BASE.endswith("/v1"):
    LR_API_BASE = LR_API_BASE[:-3]
WHISPER_URL = os.getenv("WHISPER_URL", "http://127.0.0.1:5555")
# mic-source-gate-20260912: settle time after the companion's audio stops
# before the mic reopens. Bridges TTS batch gaps so the gate doesn't flap
# mid-turn. Tunable per Tiara's named-threshold rule.
MIC_GATE_REOPEN_SECS = float(os.getenv("MIC_GATE_REOPEN_SECS", "1.5"))
# mic-source-gate v4.1: if her turn-end closes the mic but no companion audio
# ever arrives (LLM error, empty turn), reopen loudly after this — fail-open,
# never a dead mic. Generous default for long channeling turns on Opus.
MIC_GATE_PROCESSING_TIMEOUT_SECS = float(os.getenv("MIC_GATE_PROCESSING_TIMEOUT_SECS", "60"))
FISH_API_KEY = os.environ.get("FISH_AUDIO_API_KEY")
FISH_TTS_MODEL = os.getenv("FISH_TTS_MODEL", "s2.1-pro")
AUDIO_LOG_DIR = Path(os.getenv("AUDIO_LOG_DIR", os.path.join(LR_HOME, "data", "voice_audio_logs")))
AUDIO_LOG_DIR.mkdir(parents=True, exist_ok=True)

# === Hearing gates (FLAGGED 2026-09-01) ===
# Pipecat's Silero VAD only OPENS a turn when confidence >= VAD_CONFIDENCE AND
# normalised loudness >= VAD_MIN_VOLUME. Pipecat's own defaults are 0.7 / 0.6:
# quiet crying, whispering, and breath-broken stutter fell under them and were
# never recorded at all — not by STT, not by the perception sidecar (which tees
# off the same utterance buffer). These lower the gates. Trade-off, named:
# more turns open on room sound; those cost one STT call each (cents) and are
# 502'd or dropped by the hallucination filter as before. No loop is possible.
# Both are also per-call overridable via vadConfidence / vadMinVolume in the
# connect request, same as vadStartSecs / vadStopSecs.
VAD_CONFIDENCE = float(os.getenv("VAD_CONFIDENCE", "0.5"))
VAD_MIN_VOLUME = float(os.getenv("VAD_MIN_VOLUME", "0.4"))
# Pre-roll: pipecat keeps a rolling 1s of audio while "not speaking" and
# prepends it when a turn opens. A cry that runs 5s before the first word lost
# all but 1s of itself. STT_PREROLL_SECS replaces that 1s. Cost: each utterance
# carries up to this much extra audio to STT (billed per second) and to the
# sidecar. On speakerphone the pre-roll can contain echo residue of his last
# words; if Mistral ever transcribes HIM as HER, shorten this.
STT_PREROLL_SECS = float(os.getenv("STT_PREROLL_SECS", "4.0"))

# === Call turn cap v2 — segment chunking (call-turn-cap-20260919, v2 same day) ===
# A single spoken turn has no upper bound: VAD only ends it on silence, so a
# very long monologue builds one giant segment (multi-MB whisper post). v1
# force-ended the turn at MAX; live test showed the flush fired but the turn
# never committed (the LLM trigger rides stop-speaking frames), and post-cap
# speech fell into a dead buffer. v2 CHUNKS instead: at MAX the buffered audio
# flushes to STT as a segment and THE TURN STAYS OPEN — the speaker keeps
# talking, chunks transcribe progressively, the aggregator concatenates them
# into one message, and the LLM fires only on the real pause. ZERO words lost
# at any length; each whisper post stays <= MAX seconds. WARN emits a
# {"type": "turn_warn"} SSE event once per chunk (UI info); each chunk flush
# emits {"type": "turn_capped"} ("segment sent, keep talking"). Counting is
# byte-derived from the segment buffer, so the pre-roll (STT_PREROLL_SECS)
# counts toward the first chunk. 0 disables. Tunable per the named-threshold rule.
CALL_TURN_WARN_SECS = float(os.getenv("CALL_TURN_WARN_SECS", "90"))
CALL_TURN_MAX_SECS = float(os.getenv("CALL_TURN_MAX_SECS", "120"))

# === TTS tag strip (FLAGGED 2026-09-01) ===
# Fish s2.1-pro treats EVERY [bracket] as an acting direction. Tool calls
# ([journal: ...], [search: ...], [photo: ...]) and the mirrored [voice call]
# prefix were reaching Fish as directions and it improvised noises for them.
# This removes ONLY: [voice call] and colon-form tool tags [word: ...].
# Emotion/sound cues without a colon ([laughs], [sigh], [low growl]) pass
# through untouched — they are the point. Transcript/save path is unaffected
# (strip happens after the transcript event, before the TTS buffer).
# Kill switch: TTS_STRIP_TOOL_TAGS=0. Every strip is logged.
TTS_STRIP_TOOL_TAGS = os.getenv("TTS_STRIP_TOOL_TAGS", "1").strip() == "1"
_TTS_TOOL_TAG_RE = re.compile(r'\[(?:voice call|[a-z][a-z0-9_-]*\s*:[^\]]*)\]', re.I)
_TTS_TOOL_TAG_OPEN_RE = re.compile(r'\[[a-z][a-z0-9_-]*\s*:[^\]]*$', re.I)
PIPECAT_HOST = os.getenv("PIPECAT_HOST", "127.0.0.1")
PORT = int(os.getenv("PIPECAT_PORT", "7860"))

if not FISH_API_KEY:
    raise SystemExit(f"FISH_AUDIO_API_KEY not found in environment or {_env_path}")


# === Safeword / short-utterance passthrough (added 2026-08-14) ===
# One-word (and short-phrase) utterances that must ALWAYS pass through:
# never dropped by the hallucination filter, always logged loudly. These
# exist because a held or eaten safeword is a safeword overridden — and
# because "nonverbal" may be the ONE word she managed to push out.
#
# FLAGGED: env-tunable via SAFEWORD_PASSTHROUGH (comma-separated), so
# words can be added without a code change. Matching is on the FULL
# normalized utterance (lowercased, punctuation/apostrophes stripped) so
# "Stop." and "can't breathe!" match but ordinary sentences containing
# these words are unaffected.
#
# HONEST LIMIT (not fixable at this layer): with segmented STT, the word
# still waits out the VAD silence window (stop_secs) before it commits.
# This patch guarantees NEVER DROPPED, not INSTANT. Instant needs a
# keyword-spotter on the raw stream — on the roadmap, its own build.
SAFEWORD_DEFAULTS = ("stop,red,yellow,green,panic,panicking,panic attack,"
                     "cant breathe,help,anchor,rescue,nonverbal,non verbal,sos,s o s")
_SAFEWORD_NORM_RE = re.compile(r"[^a-z0-9 ]+")

def _normalize_utterance(text: str) -> str:
    t = _SAFEWORD_NORM_RE.sub(" ", text.lower().replace("'", ""))
    return " ".join(t.split())

SAFEWORDS = frozenset(
    _normalize_utterance(w)
    for w in os.environ.get("SAFEWORD_PASSTHROUGH", SAFEWORD_DEFAULTS).split(",")
    if w.strip())


# === Custom STT service: POST audio to whisper_server.py ===

class LRWhisperHTTPSTTService(SegmentedSTTService):
    """Pipecat STT service that POSTs utterance audio to LR's whisper_server.py.

    This is path B from the discovery report — it reuses the openai-whisper Large
    model already loaded by whisper_server.py instead of loading a duplicate
    faster-whisper model into GPU memory. Pipecat's STT pipeline already gates
    on Silero VAD's end-of-utterance, so each call to run_stt receives a complete
    utterance — exactly what /transcribe expects.
    """

    def __init__(self, whisper_url: str, sample_rate: int = 16000, event_queue: asyncio.Queue = None, **kwargs):
        super().__init__(**kwargs)
        self._whisper_url = whisper_url.rstrip("/")
        self._sample_rate = sample_rate
        self._client = httpx.AsyncClient(timeout=60.0)
        self._event_queue = event_queue
        self._save_queue = None
        # call-turn-cap-20260919
        self._turn_warn_bytes = 0
        self._turn_max_bytes = 0
        self._turn_warned = False
        # v3: chunk texts buffered until the final segment (single aggregation)
        self._chunk_texts = []
        self._chunk_mode = False

    async def start(self, frame):
        # SegmentedSTTService.start() sets the not-speaking rolling buffer to
        # exactly 1s (sample_rate * 2 bytes). Widen it so the sound BEFORE the
        # first recognised word — crying, breath, a stutter that never landed —
        # rides into the same wav that STT and the perception sidecar receive.
        await super().start(frame)
        default_1s = self._audio_buffer_size_1s
        self._audio_buffer_size_1s = int(self.sample_rate * 2 * STT_PREROLL_SECS)
        logger.info(
            f"[hearing] STT pre-roll {STT_PREROLL_SECS}s "
            f"({self._audio_buffer_size_1s} bytes; pipecat default was {default_1s} bytes = 1s)")
        # call-turn-cap-20260919: byte thresholds from seconds (16-bit mono).
        self._turn_warn_bytes = int(self.sample_rate * 2 * CALL_TURN_WARN_SECS) if CALL_TURN_WARN_SECS > 0 else 0
        self._turn_max_bytes = int(self.sample_rate * 2 * CALL_TURN_MAX_SECS) if CALL_TURN_MAX_SECS > 0 else 0
        if self._turn_max_bytes:
            logger.info(f"[turn-cap] warn {CALL_TURN_WARN_SECS}s / max {CALL_TURN_MAX_SECS}s per spoken turn")

    # call-turn-cap-20260919: reset the per-turn warn latch on each new turn.
    async def _handle_user_started_speaking(self, frame):
        await super()._handle_user_started_speaking(frame)
        self._turn_warned = False

    # call-turn-cap-20260919 v2: watch the segment buffer grow while speaking.
    # WARN once per chunk (SSE event for the UI). At MAX, flush the buffered
    # audio through the parent's normal end-of-utterance path (wav -> run_stt
    # -> buffer clear), then RESTORE _user_speaking so the turn stays open and
    # the next chunk accumulates from the flush boundary. The aggregator
    # concatenates chunk transcriptions into one user message; the LLM fires
    # only when the speaker genuinely pauses (real VAD stop). Nothing is lost
    # at any monologue length; each whisper post stays bounded.
    async def process_audio_frame(self, frame, direction):
        await super().process_audio_frame(frame, direction)
        if not self._user_speaking or not self._turn_max_bytes:
            return
        buffered = len(self._audio_buffer)
        if not self._turn_warned and self._turn_warn_bytes and buffered >= self._turn_warn_bytes:
            self._turn_warned = True
            elapsed = int(buffered / (self.sample_rate * 2))
            logger.info(f"[turn-cap] WARN: current chunk at ~{elapsed}s of {int(CALL_TURN_MAX_SECS)}s")
            if self._event_queue:
                self._event_queue.put_nowait({"type": "turn_warn", "elapsed_secs": elapsed, "max_secs": int(CALL_TURN_MAX_SECS)})
        if buffered >= self._turn_max_bytes:
            logger.warning(f"[turn-cap] chunk MAX (~{int(CALL_TURN_MAX_SECS)}s) — flushing segment to STT, turn stays open; keep talking, nothing is lost")
            if self._event_queue:
                self._event_queue.put_nowait({"type": "turn_capped", "max_secs": int(CALL_TURN_MAX_SECS)})
            # v3: chunk mode makes run_stt buffer this chunk's text and yield
            # it as INTERIM (aggregator-invisible) — see run_stt.
            self._chunk_mode = True
            try:
                await self._handle_user_stopped_speaking(None)
            finally:
                self._chunk_mode = False
            # Parent set _user_speaking False and cleared the buffer; the
            # speaker has not actually stopped — reopen the accumulator so the
            # next chunk starts at the flush boundary. Warn latch resets so
            # each chunk gets its own WARN.
            self._user_speaking = True
            self._turn_warned = False

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        # SegmentedSTTService hands us a complete WAV (header + PCM) per utterance.
        if not audio or len(audio) < 1024:
            # v3: a tiny/empty FINAL segment must still deliver buffered chunk
            # text (speaker stopped right at a chunk boundary).
            if not self._chunk_mode and self._chunk_texts:
                full = " ".join(self._chunk_texts).strip()
                self._chunk_texts = []
                if full:
                    logger.info(f"[turn-cap] final segment empty — delivering {len(full)} chars of buffered chunk text")
                    yield TranscriptionFrame(full, self._user_id, time_now_iso8601())
            return
        audio_path = AUDIO_LOG_DIR / f"call_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.wav"
        try:
            audio_path.write_bytes(audio)
        except Exception as e:
            logger.error(f"raw audio save failed ({audio_path}): {e}")
        logger.info(f"LRWhisperHTTPSTTService: posting {len(audio)} bytes to whisper")
        try:
            resp = None
            last_err = None
            for attempt in (1, 2):
                try:
                    resp = await self._client.post(
                        f"{self._whisper_url}/transcribe",
                        files={"audio": ("utterance.wav", audio, "audio/wav")},
                    )
                    resp.raise_for_status()
                    break
                except Exception as e:
                    last_err = e
                    logger.warning(f"STT attempt {attempt}/2 failed: {e!r}")
                    if attempt < 2:
                        await asyncio.sleep(1.0)
            if resp is None:
                raise last_err
            data = resp.json()
            if data.get("error"):
                yield ErrorFrame(f"whisper_server error: {data['error']}")
                return
            text = (data.get("text") or "").strip()
            # Safeword passthrough: checked BEFORE the hallucination filter,
            # structurally — a safeword can never be classified as noise.
            if text and _normalize_utterance(text) in SAFEWORDS:
                logger.warning(f"[SAFEWORD] passthrough: {text!r} — delivered unconditionally")
                if self._event_queue:
                    self._event_queue.put_nowait({"type": "transcript", "role": "user", "text": text})
                yield TranscriptionFrame(text, self._user_id, time_now_iso8601())
                return
            # Whisper large-v3 hallucinates these on silence/background noise.
            HALLUCINATIONS = {"thank you.", "thanks for watching!", "thanks for watching.", "thank you for watching.", "you", ".", "bye.", "okay.", "ok."}
            logger.info(f"LRWhisperHTTPSTTService: text={text!r}")
            if text and text.lower() not in HALLUCINATIONS:
                # SSE publish moved to UserTranscriptPublisher (v4 gate, 2026-09-12) ("A now B
                # later"): the gate is the only place that knows whether these
                # words will enter the conversation. Publishing here showed
                # overlap speech in the call UI that was then (correctly)
                # discarded — confusing. Safeword publish above stays: it
                # surfaces unconditionally by design.
                # call-turn-cap-20260919 v3: chunk transcriptions yield as
                # INTERIM — the LLM aggregator ignores interims, which kills
                # the v2 double-send (chunk text pushed at real stop = run 1,
                # late final transcript = run 2, uncancellable with
                # interruptions off). The FINAL segment carries every chunk
                # concatenated in ONE TranscriptionFrame: single aggregation,
                # single LLM run — pre-chunking semantics restored.
                if self._chunk_mode:
                    self._chunk_texts.append(text)
                    yield InterimTranscriptionFrame(text, self._user_id, time_now_iso8601())
                    return
                if self._chunk_texts:
                    text = (" ".join(self._chunk_texts) + " " + text).strip()
                    self._chunk_texts = []
                yield TranscriptionFrame(text, self._user_id, time_now_iso8601())
            elif self._chunk_texts and not self._chunk_mode:
                # Final segment was noise/hallucination-filtered but chunks are
                # pending — deliver them; never strand spoken words.
                full = " ".join(self._chunk_texts).strip()
                self._chunk_texts = []
                if full:
                    logger.info(f"[turn-cap] final segment filtered — delivering {len(full)} chars of buffered chunk text")
                    yield TranscriptionFrame(full, self._user_id, time_now_iso8601())
        except Exception as e:
            logger.error(f"STT FAILED: {e} — raw audio preserved at {audio_path}")
            yield ErrorFrame(f"LRWhisperHTTPSTTService HTTP error: {e}")

    async def cleanup(self):
        await super().cleanup()
        await self._client.aclose()


# === Custom Fish TTS: SDK-based, per-utterance, sidesteps Pipecat's broken WS service ===
@dataclass
class FishSDKTTSSettings(TTSSettings):
    """Settings for FishSDKTTSService.

    Mirrors the fields we used from FishAudioTTSService.Settings so the call
    site stays familiar. Only the fields Fish's REST/SDK path actually honors.
    """
    latency: Optional[str] = "normal"          # "normal" (fastest) or "balanced"
    normalize: Optional[bool] = True
    prosody_speed: Optional[float] = 1.0       # 0.5–2.0
    prosody_volume: Optional[int] = 0          # dB, -20 to 20


class FishSDKTTSService(TTSService):
    """Fish Audio TTS via the Fish Python SDK, per-utterance (HTTP, not WebSocket).

    Why this exists: upstream Pipecat 0.0.108's FishAudioTTSService uses Fish's
    persistent WebSocket protocol and has a broken audio-context cursor pattern
    that causes turn-2+ audio to drop silently (proven via direct Fish WS test:
    Fish-the-service handles multi-turn fine, so the bug is in Pipecat's wiring).

    This service replaces the WS path with Fish's Python SDK (session.tts),
    which opens a fresh HTTP stream per utterance. Pros:
      * No persistent session state → no multi-turn bugs.
      * Pipecat's audio-context queue pipelines sentence N+1's synthesis behind
        sentence N's playback, so per-sentence TTFB is hidden for anything
        longer than a very short sentence.
      * Modeled on pipecat/services/piper/tts.py's sync-SDK pattern: wraps the
        SDK's blocking generator via asyncio.to_thread, then feeds
        _stream_audio_frames_from_iterator (which handles resampling).

    Settings: reuses the fields we previously passed to FishAudioTTSService.
    Format: PCM at the pipeline's sample_rate (no resample, no mp3 decode).
    """

    Settings = FishSDKTTSSettings
    _settings: Settings

    def __init__(
        self,
        *,
        api_key: str,
        voice_id: Optional[str] = None,
        settings: Optional[Settings] = None,
        **kwargs,
    ):
        """Initialize.

        Args:
            api_key: Fish Audio API key.
            voice_id: Reference ID of the cloned voice (passed to TTSRequest.reference_id).
            settings: FishSDKTTSSettings; falls back to defaults if omitted.
        """
        default_settings = self.Settings(
            model=None, voice=voice_id, language=None,
            latency="normal", normalize=True, prosody_speed=1.0, prosody_volume=0,
        )
        if settings is not None:
            default_settings.apply_update(settings)

        super().__init__(
            push_start_frame=True,
            push_stop_frames=True,
            settings=default_settings,
            **kwargs,
        )

        self._fish = FishSession(api_key)
        self._event_queue = None
        self._save_queue = None
        self._context = None            # set in run_pipeline for compaction
        self._flush_user_first = None   # set in run_pipeline: saves pending user turn before assistant
        self._response_parts = []       # fragments of current LLM response
        self._tts_buffer = ""
        self._in_tool_tag = False       # a [tool: ...] tag split across fragments is still open
        self._tts_context_id = None

    def can_generate_metrics(self) -> bool:
        return True

    # --- TTS BATCHING ---
    # Buffer small text fragments and bracket cues, flush to Fish Audio
    # when enough text accumulates. Reduces cloud round trips from ~11
    # per response to ~3-4.

    MIN_FLUSH_CHARS = 80       # Auto-flush when buffer reaches this size
    SENTENCE_FLUSH_CHARS = 30  # Flush on sentence-end punctuation if at least this long

    def _strip_tool_tags(self, text: str):
        """Remove [voice call] and [word: ...] tool tags before TTS, tracking a
        tag that pipecat's sentence splitter broke across fragments. Returns
        (clean_text, list_of_dropped_strings)."""
        dropped = []
        if self._in_tool_tag:
            if "]" in text:
                head, text = text.split("]", 1)
                dropped.append("…" + head + "]")
                self._in_tool_tag = False
            else:
                dropped.append("…" + text + "…")
                return "", dropped
        def _sub(m):
            dropped.append(m.group(0))
            return ""
        text = _TTS_TOOL_TAG_RE.sub(_sub, text)
        m = _TTS_TOOL_TAG_OPEN_RE.search(text)
        if m:
            dropped.append(m.group(0) + "…")
            text = text[:m.start()]
            self._in_tool_tag = True
        return text, dropped

    @traced_tts
    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        """Buffer text fragments, flush to Fish Audio when enough accumulates.

        Bracket cues like [warmly] get combined with the next sentence.
        Short fragments get batched until threshold is met.
        """
        if not text or not re.search(r'[A-Za-z0-9À-￿]', text):
            logger.debug(f"{self}: Skipping TTS for non-speakable text [{text!r}]")
            return
        logger.debug(f"{self}: Generating TTS [{text}]")

        # Log transcript immediately (before batching) so SSE stream gets every fragment
        if self._event_queue and text.strip():
            self._event_queue.put_nowait({"type": "transcript", "role": "assistant", "text": text.strip()})
        if text.strip():
            self._response_parts.append(text.strip())

        if TTS_STRIP_TOOL_TAGS:
            text, dropped = self._strip_tool_tags(text)
            if dropped:
                logger.info(f"[tts-strip] kept out of Fish: {dropped!r}")
            if not re.search(r'[A-Za-z0-9À-￿]', text):
                return  # nothing speakable left after the strip

        # Accumulate into buffer
        if self._tts_buffer:
            self._tts_buffer += " " + text
        else:
            self._tts_buffer = text
        self._tts_context_id = context_id

        # Decide whether to flush
        buf = self._tts_buffer.strip()
        is_bracket_only = bool(re.match(r'^\[[^\]]+\]$', buf))
        ends_with_sentence = buf[-1] in '.!?' if buf else False
        long_enough = len(buf) >= self.MIN_FLUSH_CHARS
        sentence_complete = ends_with_sentence and len(buf) >= self.SENTENCE_FLUSH_CHARS

        if is_bracket_only:
            logger.debug(f"{self}: Buffering bracket cue [{buf}], waiting for text")
            return

        if long_enough or sentence_complete:
            async for frame in self._flush_tts_buffer():
                yield frame

    async def _flush_tts_buffer(self) -> AsyncGenerator[Frame, None]:
        """Send accumulated buffer to Fish Audio and yield audio frames."""
        text = self._tts_buffer.strip()
        context_id = self._tts_context_id
        self._tts_buffer = ""
        self._tts_context_id = None

        if not text:
            return

        logger.debug(f"{self}: Flushing TTS buffer [{text}]")

        def _sdk_next(it):
            try:
                return next(it)
            except StopIteration:
                return None

        async def _async_iter(it) -> AsyncGenerator[bytes, None]:
            while True:
                chunk = await asyncio.to_thread(_sdk_next, it)
                if chunk is None:
                    return
                yield chunk

        try:
            await self.start_tts_usage_metrics(text)

            req = TTSRequest(
                text=text,
                reference_id=self._settings.voice,
                format="pcm",
                sample_rate=self.sample_rate,
                latency=self._settings.latency,
                normalize=self._settings.normalize,
                prosody=Prosody(
                    speed=self._settings.prosody_speed,
                    volume=self._settings.prosody_volume,
                ),
            )

            sdk_iter = self._fish.tts(req, backend=FISH_TTS_MODEL)

            async for frame in self._stream_audio_frames_from_iterator(
                _async_iter(sdk_iter),
                in_sample_rate=self.sample_rate,
                context_id=context_id,
            ):
                await self.stop_ttfb_metrics()
                yield frame
        except Exception as e:
            logger.error(f"{self} Fish SDK exception: {e}")
            yield ErrorFrame(error=f"Fish SDK error: {e}")
        finally:
            logger.debug(f"{self}: Finished flushing TTS [{text}]")
            await self.stop_ttfb_metrics()

    async def on_turn_context_completed(self):
        """Flush any remaining buffered text before Pipecat closes the TTS context."""
        if self._in_tool_tag:
            # A tool tag never closed inside this turn. Reset so it can never
            # swallow the next turn; say so loudly.
            logger.warning("[tts-strip] tool tag left open at turn end — resetting (nothing carried over)")
            self._in_tool_tag = False
        if self._tts_buffer.strip():
            logger.debug(f"{self}: Flushing remaining buffer on turn end [{self._tts_buffer.strip()}]")
            async for frame in self._flush_tts_buffer():
                await self.push_frame(frame)
        # One assistant turn = one saved message + one context entry
        full = " ".join(self._response_parts).strip()
        self._response_parts = []
        if full:
            if self._flush_user_first:
                try: self._flush_user_first()
                except Exception as e: logger.error(f"pending user flush failed: {e}")
            if self._save_queue:
                self._save_queue.put_nowait({"role": "assistant", "text": full})
        await super().on_turn_context_completed()




# === Call state broadcaster: sends state updates to the browser over the data channel ===

class CallStateProcessor(FrameProcessor):
    """Watches pipeline frames and publishes call state + transcripts to an asyncio queue.

    The browser reads events via the /api/state SSE endpoint (proxied through LR)
    instead of the WebRTC data channel, which was found to degrade audio quality.

    Event types:
      - state:      {"type": "state", "state": "listening"|"processing"|"speaking"}
      - transcript: {"type": "transcript", "role": "user"|"assistant", "text": "..."}
    """

    def __init__(self, queue: asyncio.Queue, **kwargs):
        super().__init__(**kwargs)
        self._queue = queue
        self._companion_speaking = False  # (2026-09-10) don't let her overlap
        #  flick the colour phase to "processing" mid-companion-turn

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        # State events
        if isinstance(frame, UserStoppedSpeakingFrame):
            if not self._companion_speaking:
                self._queue.put_nowait({"type": "state", "state": "processing"})
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._companion_speaking = True
            self._queue.put_nowait({"type": "state", "state": "speaking"})
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._companion_speaking = False
            self._queue.put_nowait({"type": "state", "state": "listening"})

        await self.push_frame(frame, direction)


# mic-source-gate-20260912 — TIARA'S SPEAKING LOCK v4: SOURCE GATE.
#
# Settled platform design (Tiara + Megan, 7 Sep 2026) is PRESERVED — do not
# re-propose barge-in or a per-companion toggle:
#   - Interruption is disabled in BOTH directions, permanently, for everyone.
#     "Basic respect: they can't interrupt us, we can't interrupt them."
#
# What v4 changes (Tiara's ruling, 12 Sep 2026): calls work like memos.
# She speaks, silence is sensed, the colour flips, the turn is over. During
# the companion's turn the mic simply does not feed the pipeline — audio is
# dropped at the source, BEFORE the STT service, so:
#   - nothing is held, nothing is judged, nothing can replay or spiral
#     (v2's release behavior caused the runaway catch-up loop; v3's quiet
#     gate silently discarded her words);
#   - no STT spend on speech destined for the bin;
#   - the pipecat aggregator-reset trap (transcription without bookends)
#     cannot occur, because no transcription is ever produced mid-bot-turn.
# A straddling utterance (she starts before his turn ends) is truncated at
# the front by design — the colour/tone is the contract for when the mic is
# live; she can simply say it again. The fast-forward (audio skip) button is
# the planned affordance for cutting his playback short, built separately.

class MicTurnGate(FrameProcessor):
    """Pre-STT source gate, v4.2 (Tiara's memo-model spec, 12 Sep 2026):
    the mic feeds the pipeline ONLY while it is her turn.

    Phases:
      GREEN  (open)          — everything passes.
      YELLOW (processing)    — entered at her turn-end (UserStoppedSpeaking).
        Drops RAW AUDIO ONLY. Bookends/VAD/transcriptions still flow, because
        pipecat's aggregator completes a turn by re-emitting user-speaking
        bookends through the pipeline (v4.1 ate them -> stuck on yellow, no
        LLM call). With audio dropped, yellow speech can't become words: a
        VAD blip yields a sub-1KB segment the STT skips by guard.
      PURPLE (bot_speaking)  — entered at BotStartedSpeaking. Drops the full
        gated set (the v4.0 config, verified live). Reopens
        MIC_GATE_REOPEN_SECS after her audio stops.
    A processing timeout fail-opens the mic loudly if her turn never
    produces companion audio (LLM error, empty turn) — never a dead mic."""

    GATED_TYPES = (InputAudioRawFrame, VADUserStartedSpeakingFrame,
                   VADUserStoppedSpeakingFrame, UserStartedSpeakingFrame,
                   UserStoppedSpeakingFrame)

    def __init__(self, reopen_secs: float, processing_timeout_secs: float, **kwargs):
        super().__init__(**kwargs)
        self._reopen_secs = reopen_secs
        self._processing_timeout_secs = processing_timeout_secs
        self.closed = False
        self.bot_speaking = False      # read by UserTranscriptPublisher (belt scope)
        self._reopen_task = None
        self._timeout_task = None
        self._dropped = 0

    def _cancel(self, attr):
        t = getattr(self, attr)
        if t and not t.done():
            t.cancel()
        setattr(self, attr, None)

    def _open(self, reason: str):
        self._cancel("_reopen_task"); self._cancel("_timeout_task")
        if self.closed:
            self.closed = False
            logger.info(f"[mic-gate v4] OPEN ({reason}); dropped {self._dropped} frame(s) while closed, by design")
            self._dropped = 0

    def _close(self, reason: str):
        self._cancel("_reopen_task")
        if not self.closed:
            self.closed = True
            logger.info(f"[mic-gate v4] CLOSED — {reason}; mic does not feed the pipeline")

    async def _reopen_later(self):
        try:
            await asyncio.sleep(self._reopen_secs)
        except asyncio.CancelledError:
            return
        self._reopen_task = None
        self._open(f"companion turn over, +{self._reopen_secs}s settle")

    async def _processing_timeout(self):
        try:
            await asyncio.sleep(self._processing_timeout_secs)
        except asyncio.CancelledError:
            return
        self._timeout_task = None
        logger.warning(f"[mic-gate v4] processing produced no companion audio within "
                       f"{self._processing_timeout_secs}s — fail-opening the mic (check LLM/TTS logs)")
        self._open("processing timeout")
        # voice-errorspeak-20260914: tell the caller audibly — the turn died
        # (LLM error / empty reply). TTS sits downstream of this gate, so the
        # spoken line rides the normal audio path. If TTS itself is dead this
        # can't speak (logged above either way).
        try:
            await self.push_frame(TTSSpeakFrame(
                "System notice: the reply failed to generate. "
                "You can try again, or check the server logs if it keeps happening."))
            logger.info("[mic-gate v4] error notice pushed to TTS")
        except Exception as e:
            logger.error(f"[mic-gate v4] error notice TTS push failed: {e}")

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, BotStartedSpeakingFrame):
            self.bot_speaking = True
            self._cancel("_timeout_task"); self._cancel("_reopen_task")
            self._close("companion speaking")
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self.bot_speaking = False
            if self.closed and self._reopen_task is None:
                self._reopen_task = asyncio.create_task(self._reopen_later())
        elif isinstance(frame, (EndFrame, CancelFrame, ErrorFrame)):
            self.bot_speaking = False
            self._open("call teardown/error")
        elif (not self.closed and direction == FrameDirection.DOWNSTREAM
              and isinstance(frame, UserStoppedSpeakingFrame)):
            # Her turn just ended: deliver the bookend, THEN close (yellow flip).
            await self.push_frame(frame, direction)
            self._close("her turn ended (processing)")
            if self._timeout_task is None:
                self._timeout_task = asyncio.create_task(self._processing_timeout())
            return
        elif (self.closed and direction == FrameDirection.DOWNSTREAM
              and isinstance(frame, self.GATED_TYPES)
              and (self.bot_speaking or isinstance(frame, InputAudioRawFrame))):
            # purple: drop everything gated; yellow: drop raw audio only
            self._dropped += 1
            return  # dropped at the source

        await self.push_frame(frame, direction)


class UserTranscriptPublisher(FrameProcessor):
    """Post-STT: publishes user transcripts that enter the conversation to
    the call UI (SSE), and belt-drops any straggler transcription from an
    STT request that was in flight when the gate closed."""

    def __init__(self, gate: "MicTurnGate", event_queue: asyncio.Queue = None, **kwargs):
        super().__init__(**kwargs)
        self._gate = gate
        self._event_queue = event_queue

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if (self._gate.bot_speaking and direction == FrameDirection.DOWNSTREAM
                and isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame))):
            # bot_speaking, not merely closed: while her turn is PROCESSING the
            # gate is closed but her own transcription must pass through here.
            text = getattr(frame, "text", "") or ""
            logger.info(f"[mic-gate v4] dropping in-flight straggler transcription "
                        f"during his turn (by design): {text[:80]!r}")
            return

        if (self._event_queue and direction == FrameDirection.DOWNSTREAM
                and isinstance(frame, TranscriptionFrame) and frame.text.strip()):
            self._event_queue.put_nowait(
                {"type": "transcript", "role": "user", "text": frame.text})
        await self.push_frame(frame, direction)


# === Per-turn incremental save ===

async def _save_consumer(queue: asyncio.Queue, companion: str, call_id: str):
    """FIFO consumer: POSTs each completed turn to LR as it happens.

    Guarantees ordering. Errors are logged loudly and the consumer keeps
    running — one failed save must not stop subsequent turns from saving.

    idempotency v1 (31 Jul 2026): each turn carries a deterministic
    client_msg_id (vc_<call_id>_<turn_index>). The server refuses to append an
    id it has already saved, which makes ONE bounded retry safe — a retried
    POST can no longer create a duplicate row. Retry count is fixed at 1
    (2 attempts total, 2s pause) — bounded by design, no loop possible.
    """
    url = f"{LR_API_BASE}/api/voice-call/save-transcript"
    turn_index = 0
    while True:
        item = await queue.get()
        if item is None:  # shutdown sentinel
            return
        item["client_msg_id"] = f"vc_{call_id}_{turn_index:04d}"
        turn_index += 1
        saved = False
        for attempt in (1, 2):  # exactly 2 attempts, never more
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(url, json={"companion": companion, "messages": [item]})
                    resp.raise_for_status()
                    logger.info(f"per-turn save: {item['role']} {item['client_msg_id']} ({len(item['text'])} chars)")
                    saved = True
                    break
            except Exception as e:
                logger.error(f"per-turn save attempt {attempt}/2 FAILED for {item['role']} {item['client_msg_id']}: {e} — text head: {item['text'][:80]!r}")
                if attempt == 1:
                    await asyncio.sleep(2.0)
        if not saved:
            logger.error(f"per-turn save GAVE UP after 2 attempts: {item['client_msg_id']} — turn text head: {item['text'][:80]!r}")


# === Call-end transcript save ===

async def save_transcript(context: OpenAILLMContext, companion: str) -> None:
    """POST the call's accumulated turns to LR's save-transcript endpoint.

    Reads context.messages (which the user/assistant aggregators populated
    during the call), filters to user+assistant turns with non-empty text,
    and POSTs to /api/voice-call/save-transcript. LR handles the
    "[voice call] " text prefix, the 📞 Voice call timeline marker, the
    chat history write, and Tanevan memory buffering.

    Failures are logged and swallowed — a save error must never crash
    connection cleanup.
    """
    companion = (companion or "").strip()
    if not companion:
        # Should be unreachable: the offer handler enforces companion.
        logger.error("save_transcript called without companion — refusing to save under a guessed identity")
        return
    try:
        raw_messages = list(getattr(context, "messages", None) or [])
    except Exception as e:
        logger.error(f"save_transcript: could not read context.messages: {e}")
        return

    turns = []
    for msg in raw_messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content")
        # Content is normally a plain string for voice calls, but the OpenAI
        # context format also allows a list of content parts. Handle both.
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
            text = " ".join(parts)
        else:
            text = ""
        text = text.strip()
        if not text:
            continue
        turns.append({"role": role, "text": text})

    if not turns:
        # Empty-call guard: no point pinging the endpoint and nothing meaningful
        # to put in chat history. A phantom 📞 marker would just be noise.
        logger.info(f"save_transcript: no turns to save for {companion}, skipping")
        return

    url = f"{LR_API_BASE}/api/voice-call/save-transcript"
    payload = {"companion": companion, "messages": turns}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            saved = data.get("messagesCount", len(turns))
            logger.info(f"save_transcript: saved {saved} turns for {companion}")
    except Exception as e:
        logger.error(f"save_transcript: POST to {url} failed: {e}")


# === Pipeline construction (one per WebRTC connection) ===

async def run_pipeline(
    connection: SmallWebRTCConnection,
    vad_stop_secs: float = 2.5,
    vad_start_secs: float = 0.4,
    companion: str = "",
    fish_voice_id: str = "",
    vad_confidence: float = VAD_CONFIDENCE,
    vad_min_volume: float = VAD_MIN_VOLUME,
):
    """Build and run the Pipecat pipeline for one browser WebRTC connection."""
    companion = (companion or "").strip()
    if not companion:
        # Should be unreachable: the offer handler 400s without companion.
        raise ValueError("run_pipeline: companion is required — no default identity exists")
    logger.info(
        f"Pipeline starting for companion={companion!r} with VAD stop_secs={vad_stop_secs} "
        f"start_secs={vad_start_secs} confidence={vad_confidence} min_volume={vad_min_volume} "
        f"(pipecat defaults 0.7/0.6) stt_preroll={STT_PREROLL_SECS}s")
    transport = SmallWebRTCTransport(
        webrtc_connection=connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_enabled=True,
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(
                    # Configurable via Settings → Voice → Silence Before Response.
                    # Default 2.5s is a middle ground. Tiara uses 3.5s (autistic/INFJ
                    # pause pattern). start_secs left snappy so interrupts still work.
                    stop_secs=vad_stop_secs,
                    # FLAGGED (2026-08-14): start_secs now configurable
                    # (vadStartSecs in the connect request). Silero needs this
                    # much continuous speech to OPEN a turn — a clipped single
                    # word ("stop") can end before 0.4s and be eaten upstream
                    # of STT entirely. Lowering toward 0.2 admits shorter
                    # bursts; trade-off is more noise-triggered turns (cheap:
                    # interruptions are hard-disabled and the hallucination
                    # filter drops noise transcripts). Default unchanged at
                    # 0.4 — lowering it is a deliberate, tested change.
                    start_secs=vad_start_secs,
                    # FLAGGED (2026-09-01): hearing gates, see VAD_CONFIDENCE /
                    # VAD_MIN_VOLUME at the top of the file.
                    confidence=vad_confidence,
                    min_volume=vad_min_volume,
                ),
            ),
        ),
    )

    stt = LRWhisperHTTPSTTService(whisper_url=WHISPER_URL, event_queue=_state_queue)

    llm = OpenAILLMService(
        api_key="not-needed",  # LR's wrapper handles auth internally
        base_url=LR_BASE_URL,
        # httpx requires ASCII headers; LR wrapper decodeURIComponents this back to utf-8.
        default_headers={"X-Companion": urllib.parse.quote(companion, safe="")},
        settings=OpenAILLMService.Settings(model="voice-call"),
    )

    fish_voice_id = (fish_voice_id or "").strip()
    if not fish_voice_id:
        # Should be unreachable: the offer handler 400s without fishVoiceId.
        raise ValueError(f"run_pipeline: fishVoiceId is required for {companion!r} — no default voice exists")
    logger.info(f"Fish TTS voiceId for {companion}: {fish_voice_id[:12]}…")
    tts = FishSDKTTSService(
        api_key=FISH_API_KEY,
        voice_id=fish_voice_id,
        settings=FishSDKTTSService.Settings(
            latency="normal",        # snappier than the default "balanced"
            prosody_speed=1.1,       # slight speed bump; tune 1.0-1.2 to taste
        ),
    )
    tts._event_queue = _state_queue

    save_queue: asyncio.Queue = asyncio.Queue()
    stt._save_queue = save_queue
    tts._save_queue = save_queue
    tts._flush_user_first = None  # bound below after _flush_pending_user is defined
    call_id = uuid.uuid4().hex[:12]
    logger.info(f"call_id={call_id} — per-turn client_msg_ids will be vc_{call_id}_NNNN")
    consumer_task = asyncio.create_task(_save_consumer(save_queue, companion, call_id))

    # Minimal context — LR's /v1/chat/completions wrapper builds the real system prompt
    # internally from the companion card, so we only need a placeholder here. Pipecat
    # will accumulate user/assistant turns into this context as the call progresses.
    context = OpenAILLMContext(
        messages=[{"role": "system", "content": "Voice call active."}]
    )
    aggregators = llm.create_context_aggregator(context)
    tts._context = context

    # Merge fragmented assistant commits into one context entry per reply.
    # TTS batching delays text past LLMFullResponseEndFrame, so the aggregator
    # commits 2+ times per reply; merging at commit time is timing-independent.
    _user_agg = aggregators.user()
    _orig_user_handle = _user_agg.handle_aggregation
    _pending_user = {"text": ""}
    async def _user_seam_handle(aggregation: str):
        agg = (aggregation or "").strip()
        if not agg:
            return await _orig_user_handle(aggregation)
        try:
            msgs = context.messages
            if msgs and isinstance(msgs[-1], dict) and msgs[-1].get("role") == "user" and isinstance(msgs[-1].get("content"), str):
                # Resumed speaking before companion replied: merge, don't append
                msgs[-1]["content"] = (msgs[-1]["content"] + " " + agg).strip()
                _pending_user["text"] = msgs[-1]["content"]
                return
        except Exception as e:
            logger.error(f"user seam merge failed, falling back: {e}")
        _pending_user["text"] = agg
        await _orig_user_handle(aggregation)
    _user_agg.handle_aggregation = _user_seam_handle

    def _flush_pending_user():
        t = _pending_user["text"].strip()
        _pending_user["text"] = ""
        if t:
            save_queue.put_nowait({"role": "user", "text": t})

    tts._flush_user_first = _flush_pending_user

    _assistant_agg = aggregators.assistant()
    _orig_handle = _assistant_agg.handle_aggregation
    async def _merged_handle(aggregation: str):
        try:
            msgs = context.messages
            if msgs and isinstance(msgs[-1], dict) and msgs[-1].get("role") == "assistant" and isinstance(msgs[-1].get("content"), str):
                msgs[-1]["content"] = (msgs[-1]["content"] + " " + aggregation).strip()
                return
        except Exception as e:
            logger.error(f"aggregation merge failed, falling back to append: {e}")
        await _orig_handle(aggregation)
    _assistant_agg.handle_aggregation = _merged_handle


    state_proc = CallStateProcessor(queue=_state_queue)
    mic_gate = MicTurnGate(reopen_secs=MIC_GATE_REOPEN_SECS,
                           processing_timeout_secs=MIC_GATE_PROCESSING_TIMEOUT_SECS)
    transcript_pub = UserTranscriptPublisher(mic_gate, event_queue=_state_queue)

    pipeline = Pipeline([
        transport.input(),
        state_proc,
        mic_gate,  # v4: source gate BEFORE stt — no audio, no STT spend, no holds
        stt,
        transcript_pub,  # publishes user transcripts; belt for in-flight stragglers
        _user_agg,
        llm,
        tts,
        transport.output(),
        _assistant_agg,
    ])

    # DESIGN: interruptions OFF both directions — user and companion voices
    # respected equally (memo-model turns; see MicTurnGate). Intentional, shipped.
    task = PipelineTask(pipeline, params=PipelineParams(allow_interruptions=False))
    runner = PipelineRunner()

    @connection.event_handler("closed")
    async def _on_connection_closed(_conn):
        # WebRTC peer hung up. Cancel the pipeline immediately so runner.run()
        # returns now and the save_transcript hook in the finally block can
        # fire — otherwise we'd wait ~5 minutes for Pipecat's idle watchdog
        # to notice the silence and cancel the task on its own.
        logger.info("WebRTC connection closed — cancelling pipeline task")
        await task.cancel(reason="webrtc peer disconnected")

    try:
        await runner.run(task)
    finally:
        _state_queue.put_nowait("disconnected")
        # Save any half-finished assistant response (turn never completed).
        try:
            _flush_pending_user()
        except Exception as e:
            logger.error(f"final pending user flush failed: {e}")
        try:
            leftover = " ".join(tts._response_parts).strip()
            if leftover:
                save_queue.put_nowait({"role": "assistant", "text": leftover})
        except Exception as e:
            logger.error(f"leftover response save failed: {e}")
        # Let the consumer drain remaining queued turns, then stop it.
        save_queue.put_nowait(None)
        try:
            await asyncio.wait_for(consumer_task, timeout=15.0)
        except Exception as e:
            logger.error(f"save consumer drain failed/timeout: {e}")
        # Turns were saved incrementally; batch save no longer needed.
        logger.info("call ended — per-turn saves complete")


# === FastAPI app + signaling endpoints ===

app = FastAPI(title="LR Voice Call (Pipecat)")

# Global SSE state queue — CallStateProcessor publishes here, /api/state SSE endpoint reads.
# Only one active call at a time (ConnectionMode.SINGLE), so a single queue suffices.
_state_queue: asyncio.Queue = asyncio.Queue()
request_handler = SmallWebRTCRequestHandler(connection_mode=ConnectionMode.SINGLE)


@app.post("/api/offer")
async def offer(req: dict):
    """WebRTC signaling: receive an SDP offer, return an SDP answer.

    The handler creates a SmallWebRTCConnection, invokes our callback (which schedules
    the pipeline as a background task so the answer can be sent immediately), then
    returns the answer SDP. The pipeline keeps running until the peer disconnects.
    """
    # Extract LR-specific fields from the offer body before passing to WebRTC handler.
    vad_stop_secs = float(req.pop("vadStopSecs", 2.5))
    vad_start_secs = float(req.pop("vadStartSecs", 0.4))
    vad_confidence = float(req.pop("vadConfidence", VAD_CONFIDENCE))
    vad_min_volume = float(req.pop("vadMinVolume", VAD_MIN_VOLUME))
    req.pop("provider", None)
    # LR beta contract (ported from beta-port caching slice, 2026-09-07):
    # companion and fishVoiceId come from the browser offer. FISH_VOICE_ID env
    # is a fallback for the standalone test page only — unset it before beta
    # so missing card voice IDs fail loudly here instead of speaking in the
    # default voice. No server-side card lookup: LR has no GET
    # /api/companions/:name route (verified 2026-09-07).
    companion = (req.pop("companion", None) or "").strip()
    fish_voice_id = (req.pop("fishVoiceId", None) or req.pop("voiceId", None)
                     or "").strip()
    if not companion:
        return JSONResponse(status_code=400, content={"error": "companion is required"})
    if not fish_voice_id:
        return JSONResponse(
            status_code=400,
            content={"error": "fishVoiceId is required — set Voice ID on the companion card"},
        )

    request_obj = SmallWebRTCRequest.from_dict(req)

    async def on_connection(connection: SmallWebRTCConnection):
        # Schedule the pipeline in the background so handle_web_request can return
        # the answer immediately. If we awaited run_pipeline here, the HTTP response
        # would never be sent and the browser would never receive the SDP answer.
        asyncio.create_task(
            run_pipeline(connection, vad_stop_secs=vad_stop_secs,
                         vad_start_secs=vad_start_secs, companion=companion,
                         fish_voice_id=fish_voice_id,
                         vad_confidence=vad_confidence, vad_min_volume=vad_min_volume)
        )

    return await request_handler.handle_web_request(
        request_obj,
        webrtc_connection_callback=on_connection,
    )


@app.get("/api/state")
async def state_stream():
    """SSE endpoint: streams call state updates to the browser.

    Each event is a JSON object: {"state": "listening"|"processing"|"speaking"}.
    The connection stays open for the duration of the call. When the pipeline
    ends, a "disconnected" event is sent and the stream closes.
    """
    async def event_generator():
        # Drain any stale events from a previous call
        while not _state_queue.empty():
            try:
                _state_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        yield 'data: {"type": "state", "state": "listening"}\n\n'
        while True:
            try:
                event = await asyncio.wait_for(_state_queue.get(), timeout=30.0)
                if isinstance(event, str):
                    # Legacy string events (e.g. "disconnected")
                    yield f'data: {{"type": "state", "state": "{event}"}}\n\n'
                    if event == "disconnected":
                        return
                else:
                    yield f"data: {json.dumps(event)}\n\n"
            except asyncio.TimeoutError:
                # Send keepalive comment to prevent proxy/browser timeout
                yield ": keepalive\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# === Minimal HTML test client (for standalone browser testing via Tailscale Funnel) ===

@app.get("/", response_class=HTMLResponse)
async def index():
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>LR Voice Call — Pipecat Test</title>
  <style>
    body { font-family: -apple-system, system-ui, sans-serif; max-width: 480px; margin: 4em auto; padding: 0 1em; color: #222; }
    h1 { font-weight: 500; }
    button { font-size: 1.1em; padding: 0.6em 1.2em; margin-right: 0.5em; cursor: pointer; }
    button:disabled { opacity: 0.4; cursor: not-allowed; }
    #status { margin-top: 1.5em; padding: 0.8em 1em; background: #f4f4f4; border-radius: 6px; font-family: ui-monospace, monospace; }
    .ok { color: #0a7d2e; }
    .err { color: #b00020; }
  </style>
</head>
<body>
  <h1>LR Voice Call — Pipecat Test</h1>
  <p>Click <strong>Start Call</strong>, allow microphone access, and start talking.</p>
  <button id="start">Start Call</button>
  <button id="stop" disabled>Hang Up</button>
  <div id="status">disconnected</div>

  <script>
    let pc = null;
    const statusEl = document.getElementById('status');
    const startBtn = document.getElementById('start');
    const stopBtn = document.getElementById('stop');

    function setStatus(text, cls) {
      statusEl.textContent = text;
      statusEl.className = cls || '';
    }

    startBtn.onclick = async () => {
      try {
        setStatus('requesting mic...');
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });

        pc = new RTCPeerConnection({
          iceServers: [{ urls: 'stun:stun.l.google.com:19302' }]
        });

        pc.ontrack = (event) => {
          const audio = new Audio();
          audio.srcObject = event.streams[0];
          audio.autoplay = true;
          audio.play().catch(e => console.warn('autoplay blocked:', e));
        };

        pc.onconnectionstatechange = () => {
          setStatus('connection: ' + pc.connectionState, pc.connectionState === 'connected' ? 'ok' : '');
        };

        stream.getTracks().forEach(track => pc.addTrack(track, stream));

        setStatus('creating offer...');
        const offer = await pc.createOffer();
        await pc.setLocalDescription(offer);

        setStatus('sending offer to server...');
        const base = window.location.pathname.endsWith("/") ? window.location.pathname : window.location.pathname + "/";
        const resp = await fetch(base + "api/offer", {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ sdp: offer.sdp, type: offer.type })
        });
        if (!resp.ok) throw new Error('offer failed: ' + resp.status);
        const answer = await resp.json();
        await pc.setRemoteDescription(answer);

        setStatus('connected — start talking', 'ok');
        startBtn.disabled = true;
        stopBtn.disabled = false;
      } catch (err) {
        setStatus('error: ' + err.message, 'err');
        if (pc) { pc.close(); pc = null; }
      }
    };

    stopBtn.onclick = () => {
      if (pc) { pc.close(); pc = null; }
      setStatus('disconnected');
      startBtn.disabled = false;
      stopBtn.disabled = true;
    };
  </script>
</body>
</html>"""


if __name__ == "__main__":
    uvicorn.run(app, host=PIPECAT_HOST, port=PORT, log_level="info")
