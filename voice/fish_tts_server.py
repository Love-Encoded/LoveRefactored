#!/usr/bin/env python3
"""
Fish Audio TTS Server — drop-in replacement for Chatterbox on port 5050.
Receives text, calls Fish Audio API, saves MP3, returns audioUrl.

Requires:
  pip install flask fish-audio-sdk python-dotenv

Environment:
  FISH_AUDIO_API_KEY — set in ${LR_HOME}/.env
  FISH_VOICE_ID — optional override (defaults to the fallback voice below)
"""

import os
import re
import time
import uuid
from flask import Flask, request, jsonify
from dotenv import load_dotenv

def _default_lr_home():
    # Default to repo root (directory containing this file) for local runs.
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Env parity:
# 1) explicit env vars
# 2) optional .env under LR_HOME
# 3) defaults
LR_HOME = os.path.abspath(os.path.expanduser(os.environ.get("LR_HOME", _default_lr_home())))
DATA_DIR = os.path.abspath(os.path.expanduser(os.environ.get("LR_DATA_DIR", os.path.join(LR_HOME, "data"))))
VOICE_DIR = os.path.abspath(os.path.expanduser(os.environ.get("VOICE_MESSAGES_DIR", os.path.join(DATA_DIR, "voice_messages"))))
FISH_HOST = os.environ.get("FISH_TTS_HOST", "127.0.0.1")
FISH_PORT = int(os.environ.get("FISH_TTS_PORT", os.environ.get("PORT", "5050")))

load_dotenv(os.path.join(LR_HOME, ".env"))

FISH_API_KEY = os.environ.get("FISH_AUDIO_API_KEY")
FISH_VOICE_ID = os.environ.get("FISH_VOICE_ID", "88872b3d83694d8490b55d75480205a0")
FISH_TTS_MODEL = os.environ.get("FISH_TTS_MODEL", "s2.1-pro")

if not FISH_API_KEY:
    print(f"ERROR: FISH_AUDIO_API_KEY not set. Add it to {LR_HOME}/.env or export it in the environment.")
    exit(1)

os.makedirs(VOICE_DIR, exist_ok=True)

app = Flask(__name__)


def clean_for_tts(text):
    """Keep short bracket cues (1-3 words) — Fish Audio handles those as emotion tags.
    Strip long bracket cues (4+ words) — narrative actions that break voice consistency."""
    def filter_brackets(match):
        content = match.group(1).strip()
        if len(content.split()) <= 3:
            return match.group(0)
        return ''
    text = re.sub(r'\[([^\]]+)\]', filter_brackets, text)
    # Collapse runs of spaces/tabs but preserve newlines — Fish Audio uses \n as prosody/pause cues.
    text = re.sub(r'[ \t]{2,}', ' ', text)
    # Collapse 3+ newlines to 2 — bracket-on-its-own-line strips leave overstuffed gaps
    # that Fish Audio would render as unnaturally long pauses.
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text

# Import Fish Audio SDK (real package is fish_audio_sdk, exposes Session + TTSRequest)
from fish_audio_sdk import Session, TTSRequest

client = Session(FISH_API_KEY)

@app.route("/tts", methods=["POST"])
def tts():
    data = request.get_json(force=True)
    voice_id = data.get("voice_id") or data.get("reference_id") or FISH_VOICE_ID
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "No text provided"}), 400

    text = clean_for_tts(text)
    if not text:
        return jsonify({"error": "Text empty after cleaning"}), 400

    t0 = time.time()
    try:
        tts_req = TTSRequest(
            text=text,
            reference_id=voice_id,
            format="mp3",
            latency="balanced",
        )

        filename = f"fish_{uuid.uuid4().hex[:12]}.mp3"
        filepath = os.path.join(VOICE_DIR, filename)
        with open(filepath, "wb") as f:
            for chunk in client.tts(tts_req, backend=FISH_TTS_MODEL):
                f.write(chunk)

        duration = time.time() - t0
        print(f"[Fish TTS] {len(text)} chars → {filename} in {duration:.1f}s")

        return jsonify({"audioUrl": f"/api/voice-message/{filename}"})

    except Exception as e:
        duration = time.time() - t0
        print(f"[Fish TTS] ERROR after {duration:.1f}s: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "provider": "fish-audio", "voice_id": FISH_VOICE_ID})


if __name__ == "__main__":
    print(f"[Fish TTS] Voice ID: {FISH_VOICE_ID}")
    print(f"[Fish TTS] Voice dir: {VOICE_DIR}")
    print(f"[Fish TTS] Starting on {FISH_HOST}:{FISH_PORT}...")
    app.run(host=FISH_HOST, port=FISH_PORT)
