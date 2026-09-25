#!/usr/bin/env python3
"""
SenseVoice Server — Drop-in replacement for whisper_server.py
=============================================================
Uses FunAudioLLM's SenseVoice-Small for:
  - Speech-to-text (ASR)
  - Emotion recognition (happy, sad, angry, neutral, etc.)
  - Audio event detection (laughter, crying, coughing, etc.)

Serves on the same port (5555) with the same /transcribe endpoint
so Love Refactored's server.js doesn't need changes.

Response format:
{
  "text": "the transcribed words without emotion tags",
  "emotion": "happy",          # or "sad", "angry", "neutral", "unknown"
  "audio_events": ["laughter"], # list of detected events
  "raw_text": "😊😀the transcribed words"  # original with emoji tags
}
"""

import os
import re
import tempfile
from flask import Flask, request, jsonify

# ── Model loading ────────────────────────────────────────────
print("Loading SenseVoice model...")

from funasr import AutoModel
from funasr.utils.postprocess_utils import rich_transcription_postprocess

# Use GPU if available, fall back to CPU
import torch
device = "cuda:0" if torch.cuda.is_available() else "cpu"
print(f"  Device: {device}")

model = AutoModel(
    model="FunAudioLLM/SenseVoiceSmall",
    vad_model="fsmn-vad",
    vad_kwargs={"max_single_segment_time": 30000},
    device=device,
    hub="hf",
)

print("SenseVoice model ready.")

# ── Emotion/event parsing ────────────────────────────────────

EMOTION_MAP = {
    "😊": "happy",
    "😡": "angry",
    "😔": "sad",
}

EVENT_MAP = {
    "😀": "laughter",
    "🎼": "music",
    "👏": "applause",
}

def parse_sensevoice_output(raw_text):
    """
    SenseVoice prepends emoji tags for emotion and events.
    Extract them, then return clean text + metadata.
    """
    emotion = "neutral"
    events = []

    # Check for emotion emojis at the start
    for emoji, label in EMOTION_MAP.items():
        if emoji in raw_text:
            emotion = label
            raw_text_cleaned = raw_text  # keep raw for reference

    # Check for event emojis
    for emoji, label in EVENT_MAP.items():
        if emoji in raw_text:
            events.append(label)

    # Strip all known emoji tags from text to get clean transcription
    clean = raw_text
    for emoji in list(EMOTION_MAP.keys()) + list(EVENT_MAP.keys()):
        clean = clean.replace(emoji, "")
    clean = clean.strip()

    return clean, emotion, events


# ── Flask app ────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/transcribe", methods=["POST"])
def transcribe():
    if "audio" not in request.files:
        return jsonify({"error": "No audio file provided"}), 400

    audio_file = request.files["audio"]

    # Save to temp file (SenseVoice needs a file path)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        audio_file.save(tmp.name)
        tmp_path = tmp.name

    try:
        res = model.generate(
            input=tmp_path,
            cache={},
            language="auto",
            use_itn=True,
            batch_size_s=60,
            merge_vad=True,
        )

        raw_text = rich_transcription_postprocess(res[0]["text"])
        clean_text, emotion, events = parse_sensevoice_output(raw_text)

        return jsonify({
            "text": clean_text,
            "emotion": emotion,
            "audio_events": events,
            "raw_text": raw_text,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        os.unlink(tmp_path)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "model": "SenseVoiceSmall", "device": device})


if __name__ == "__main__":
    port = int(os.environ.get("SENSEVOICE_PORT", 5555))
    print(f"SenseVoice server listening on http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port)
