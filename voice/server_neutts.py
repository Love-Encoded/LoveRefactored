"""
NeuTTS + RVC Voice Server
Drop-in replacement for Chatterbox TTS server.
Port 5050. Same API interface.
"""

import re
import os
import sys
import time
import tempfile
import traceback

import numpy as np
import soundfile as sf
import soxr
from flask import Flask, request, jsonify
from pydub import AudioSegment

_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)
from lib.lr_settings import ensure_voice_messages_dir, love_refactored_root

LR_HOME = love_refactored_root()
VOICE_MESSAGES_DIR = ensure_voice_messages_dir()
PORT = int(os.environ.get("NEUTTS_TTS_PORT", os.environ.get("PORT", "5050")))
HOST = os.environ.get("NEUTTS_TTS_HOST", "0.0.0.0")

APPLIO_HOME = os.path.abspath(os.path.expanduser(os.environ.get("APPLIO_HOME", "~/Applio")))
# Add Applio to path for RVC
sys.path.insert(0, APPLIO_HOME)

from neutts import NeuTTS
from rvc.infer.infer import VoiceConverter

REF_DIR = os.path.join(os.path.dirname(__file__), "reference")
REF_AUDIO = os.path.join(REF_DIR, "ref_clip.wav")
REF_TEXT_FILE = os.path.join(REF_DIR, "ref_clip.txt")

_default_rvc_model = os.path.join(APPLIO_HOME, "logs/voice-model/voice-model.pth")
_default_rvc_index = os.path.join(APPLIO_HOME, "logs/voice-model/voice-model.index")
RVC_MODEL = os.path.abspath(os.path.expanduser(os.environ.get("RVC_MODEL_PATH", _default_rvc_model)))
RVC_INDEX = os.path.abspath(os.path.expanduser(os.environ.get("RVC_INDEX_PATH", _default_rvc_index)))

SILENCE_GAP_MS = 250  # milliseconds between sentence chunks — tune to taste

# === Load models on startup ===
print("Loading NeuTTS Air Q8...")
tts = NeuTTS(
    backbone_repo="neuphonic/neutts-air-q8-gguf",
    backbone_device="cpu",
    codec_repo="neuphonic/neucodec",
    codec_device="cpu"
)
print("NeuTTS loaded.")

# Encode reference once
print("Encoding reference clip...")
ref_data, ref_sr = sf.read(REF_AUDIO)
if ref_sr != 24000:
    ref_data = soxr.resample(ref_data, ref_sr, 24000)
    resampled_path = os.path.join(REF_DIR, "ref_clip_24k.wav")
    sf.write(resampled_path, ref_data, 24000)
    ref_audio_path = resampled_path
    print(f"  Resampled {ref_sr} -> 24000")
else:
    ref_audio_path = REF_AUDIO

ref_codes = tts.encode_reference(ref_audio_path)
ref_text = open(REF_TEXT_FILE, "r").read().strip()
print(f'Reference encoded. Text: "{ref_text[:80]}..."')

# Init RVC
print("Initializing RVC...")
os.chdir(APPLIO_HOME)
rvc = VoiceConverter()
print("RVC ready.")

app = Flask(__name__)


# === Fix 3: Preprocess text before chunking ===
def preprocess_text(text):
    # Collapse ellipses into a comma to prevent orphan chunks
    text = re.sub(r'\.{2,}', ',', text)
    # Clean up resulting double commas or comma-period
    text = re.sub(r',\s*,', ',', text)
    text = re.sub(r',\s*\.', '.', text)
    # Collapse multiple spaces
    text = re.sub(r'\s{2,}', ' ', text)
    return text.strip()


# === Fix 5: Improved chunking with em-dash support ===
def chunk_text(text):
    text = preprocess_text(text)

    # Split on sentence boundaries
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if s.strip()]

    # If any chunk is still very long, split on em-dashes then commas
    result = []
    for s in sentences:
        if len(s) > 200:
            parts = [p.strip() for p in re.split(r'\s*\u2014\s*', s) if p.strip()]
            for p in parts:
                if len(p) > 200:
                    subparts = [sp.strip() for sp in p.split(',') if sp.strip()]
                    result.extend(subparts)
                else:
                    result.append(p)
        else:
            result.append(s)

    # Filter out empty or tiny fragments
    result = [s for s in result if len(s) >= 3]
    return result


def generate_chunk(sentence):
    """Generate speech for a single sentence with NeuTTS, return numpy array at 24kHz."""
    wav = tts.infer(sentence, ref_codes, ref_text)
    return wav


def run_rvc(wav_array, sr=24000):
    """Run RVC voice conversion on a numpy array."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_in:
        sf.write(tmp_in.name, wav_array, sr)
        tmp_in_path = tmp_in.name

    tmp_out_path = tmp_in_path.replace(".wav", "_rvc.wav")

    try:
        rvc.convert_audio(
            audio_input_path=tmp_in_path,
            audio_output_path=tmp_out_path,
            model_path=RVC_MODEL,
            index_path=RVC_INDEX,
            pitch=-1,
            f0_method="rmvpe",
            index_rate=0.75,
            protect=0.33,
            export_format="WAV",
        )
        data, out_sr = sf.read(tmp_out_path)
        return data, out_sr
    finally:
        for p in [tmp_in_path, tmp_out_path]:
            try:
                os.unlink(p)
            except OSError:
                pass


# === Fix 4: Concatenate with silence gaps ===
def concatenate_with_gaps(audio_chunks, sample_rate, gap_ms=SILENCE_GAP_MS):
    """Concatenate audio chunks with consistent silence gaps between them."""
    gap_samples = int(sample_rate * gap_ms / 1000)
    silence = np.zeros(gap_samples, dtype=audio_chunks[0].dtype)

    result = []
    for i, chunk in enumerate(audio_chunks):
        result.append(chunk)
        if i < len(audio_chunks) - 1:
            result.append(silence)

    return np.concatenate(result)


@app.route('/tts', methods=['POST'])
def tts_endpoint():
    data = request.json
    if not data or not data.get('text'):
        return jsonify({'error': 'text is required'}), 400

    text = data['text'].strip()
    if not text:
        return jsonify({'error': 'text is empty'}), 400

    t_start = time.time()
    sentences = chunk_text(text)
    print(f"  -> {len(sentences)} chunk(s): {sentences}")

    converted_chunks = []
    target_sr = None

    for i, sentence in enumerate(sentences):
        label = f"{sentence[:60]}..." if len(sentence) > 60 else sentence
        print(f'  [{i+1}/{len(sentences)}] "{label}"')

        try:
            t0 = time.time()
            raw_wav = generate_chunk(sentence)
            t1 = time.time()
            print(f"    NeuTTS: {t1-t0:.1f}s, {len(raw_wav)/24000:.2f}s audio")

            t0 = time.time()
            rvc_wav, rvc_sr = run_rvc(raw_wav, 24000)
            t1 = time.time()
            print(f"    RVC: {t1-t0:.1f}s")

            if target_sr is None:
                target_sr = rvc_sr
            elif rvc_sr != target_sr:
                rvc_wav = soxr.resample(rvc_wav, rvc_sr, target_sr)

            converted_chunks.append(rvc_wav)

        except Exception as e:
            print(f"    ERROR in chunk {i+1}: {e}")
            traceback.print_exc()
            try:
                raw_wav = generate_chunk(sentence)
                if target_sr and target_sr != 24000:
                    raw_wav = soxr.resample(raw_wav, 24000, target_sr)
                converted_chunks.append(raw_wav)
                print("    Fallback: using raw NeuTTS audio")
            except Exception as e2:
                print(f"    SKIP chunk {i+1}: {e2}")

    if not converted_chunks:
        return jsonify({'error': 'TTS generation failed'}), 500

    # Concatenate with silence gaps
    if len(converted_chunks) == 1:
        combined = converted_chunks[0]
    else:
        combined = concatenate_with_gaps(converted_chunks, target_sr or 24000)

    # Save as temp WAV then convert to MP3
    timestamp = int(time.time() * 1000)
    mp3_filename = f"tts_{timestamp}.mp3"
    mp3_path = os.path.join(VOICE_MESSAGES_DIR, mp3_filename)

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_wav = tmp.name
        sf.write(tmp_wav, combined, target_sr or 24000)

    try:
        audio = AudioSegment.from_wav(tmp_wav)
        audio.export(mp3_path, format="mp3", bitrate="128k")
    finally:
        try:
            os.unlink(tmp_wav)
        except OSError:
            pass

    elapsed = time.time() - t_start
    fsize = os.path.getsize(mp3_path)
    print(f"  Done: {mp3_filename} ({fsize} bytes, {elapsed:.1f}s total)")
    return jsonify({'audioUrl': f'/api/voice-message/{mp3_filename}'})


@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'running',
        'engine': 'neutts+rvc',
        'reference': os.path.basename(REF_AUDIO),
        'silence_gap_ms': SILENCE_GAP_MS,
    })


if __name__ == '__main__':
    print(f"Starting NeuTTS+RVC voice server on {HOST}:{PORT}...")
    print(f"Voice messages dir: {VOICE_MESSAGES_DIR}")
    app.run(host=HOST, port=PORT, debug=False)
