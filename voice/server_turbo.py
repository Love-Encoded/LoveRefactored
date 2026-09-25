"""
Chatterbox Turbo + RVC Voice Server
Stock pretrained Turbo model with emotion tag mapping + optional RVC.
Port 5050. Same API interface as NeuTTS version.
Reads voice config from settings.json (sovereignVoice key).
"""

import sys
import re
import os
import time
import tempfile
import traceback

_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)
from lib.lr_settings import ensure_voice_messages_dir, load_settings, love_refactored_root, settings_path

APPLIO_HOME = os.path.abspath(os.path.expanduser(os.environ.get("APPLIO_HOME", "~/Applio")))
sys.path.insert(0, APPLIO_HOME)

import torch
import numpy as np
import soundfile as sf
import soxr
from flask import Flask, request, jsonify

from chatterbox.tts_turbo import ChatterboxTurboTTS, S3GEN_SR

LR_HOME = love_refactored_root()
HOST = os.environ.get("TURBO_TTS_HOST", "0.0.0.0")
PORT = int(os.environ.get("TURBO_TTS_PORT", os.environ.get("PORT", "5050")))

# === Load config from data/settings.json ===
voice_cfg = {}
try:
    settings = load_settings()
    voice_cfg = settings.get('sovereignVoice', {})
    print(f"Loaded voice config from {settings_path()}")
except Exception as e:
    print(f"Warning: Could not load settings.json ({e}), using defaults")

_ref_audio_raw = voice_cfg.get('referenceAudioPath') or os.environ.get('REFERENCE_AUDIO_PATH', '')
REFERENCE_AUDIO = os.path.expanduser(_ref_audio_raw) if _ref_audio_raw else ''
VOICE_MESSAGES_DIR = ensure_voice_messages_dir()

_default_rvc_model = os.path.join(APPLIO_HOME, 'logs/voice-model/voice-model.pth')
_default_rvc_index = os.path.join(APPLIO_HOME, 'logs/voice-model/voice-model.index')
RVC_ENABLED = voice_cfg.get('rvcEnabled', True)
RVC_MODEL = os.path.abspath(os.path.expanduser(os.environ.get("RVC_MODEL_PATH", voice_cfg.get('rvcModelPath', _default_rvc_model))))
RVC_INDEX = os.path.abspath(os.path.expanduser(os.environ.get("RVC_INDEX_PATH", voice_cfg.get('rvcIndexPath', _default_rvc_index))))
if RVC_ENABLED and (not os.path.isfile(RVC_MODEL) or not os.path.isfile(RVC_INDEX)):
    print(f"Warning: RVC model/index not found ({RVC_MODEL}); running without RVC.")
    RVC_ENABLED = False
RVC_PITCH = voice_cfg.get('rvcPitchShift', -1)
RVC_INDEX_RATE = voice_cfg.get('rvcIndexRate', 0.75)
RVC_PROTECT = voice_cfg.get('rvcProtect', 0.33)
RVC_F0_METHOD = voice_cfg.get('rvcF0Method', 'rmvpe')

SILENCE_GAP_MS = 250

# === Turbo emotion tag support ===
# The 19 tokens in base Turbo's added_tokens.json (IDs 50257-50275)
TURBO_RECOGNIZED_TAGS = {
    '[angry]', '[fear]', '[surprised]', '[whispering]',
    '[advertisement]', '[dramatic]', '[narration]',
    '[crying]', '[happy]', '[sarcastic]',
    '[clear throat]', '[sigh]', '[shush]', '[cough]',
    '[groan]', '[sniff]', '[gasp]', '[chuckle]', '[laugh]',
}

# Style tokens that work best at sentence start (mood/delivery style)
TURBO_STYLE_TAGS = {
    '[angry]', '[fear]', '[surprised]', '[whispering]',
    '[dramatic]', '[crying]', '[happy]', '[sarcastic]',
}

# Map companion-style tags to Turbo's recognized token names
TURBO_TAG_MAP = {
    '[laughs]': '[laugh]',
    '[laughing]': '[laugh]',
    '[laughter]': '[laugh]',
    '[chuckles]': '[chuckle]',
    '[chuckling]': '[chuckle]',
    '[sighs]': '[sigh]',
    '[sighing]': '[sigh]',
    '[coughs]': '[cough]',
    '[coughing]': '[cough]',
    '[gasps]': '[gasp]',
    '[gasping]': '[gasp]',
    '[groans]': '[groan]',
    '[groaning]': '[groan]',
    '[sniffs]': '[sniff]',
    '[sniffing]': '[sniff]',
    '[clears throat]': '[clear throat]',
    '[clearing throat]': '[clear throat]',
    '[shushes]': '[shush]',
    '[shushing]': '[shush]',
    '[cries]': '[crying]',
    '[sobbing]': '[crying]',
    '[sobs]': '[crying]',
    '[whispers]': '[whispering]',
    '[whispering]': '[whispering]',
    # Style/mood mappings
    '[angrily]': '[angry]',
    '[happily]': '[happy]',
    '[sarcastically]': '[sarcastic]',
    '[dramatically]': '[dramatic]',
    '[fearfully]': '[fear]',
    '[scared]': '[fear]',
    '[surprised]': '[surprised]',
    '[softly]': '[whispering]',
    '[quietly]': '[whispering]',
    '[excited]': '[happy]',
    '[excitedly]': '[happy]',
    '[gently]': '[whispering]',
    '[firmly]': '[dramatic]',
    '[pause]': ',',
}


def map_emotion_tags(text):
    """Map companion-style tags to Turbo's recognized token names."""
    for companion_tag, turbo_tag in TURBO_TAG_MAP.items():
        text = text.replace(companion_tag, turbo_tag)
    return text


def strip_unrecognized_brackets(text):
    """Strip bracket content that ISN'T a recognized Turbo tag."""
    def replace_bracket(match):
        tag = match.group(0)
        if tag in TURBO_RECOGNIZED_TAGS:
            return tag
        return ''
    return re.sub(r'\[[^\]]+\]', replace_bracket, text)


# === Text preprocessing ===
def preprocess_text(text):
    """Full text preprocessing pipeline for Turbo."""
    # 0. Rescue emotion sounds from asterisk format BEFORE stripping
    asterisk_emotions = {
        '*laughs*': '[laugh]',
        '*laughing*': '[laugh]',
        '*chuckles*': '[chuckle]',
        '*chuckling*': '[chuckle]',
        '*sighs*': '[sigh]',
        '*sighing*': '[sigh]',
        '*coughs*': '[cough]',
        '*gasps*': '[gasp]',
        '*groans*': '[groan]',
        '*sniffs*': '[sniff]',
        '*cries*': '[crying]',
        '*sobs*': '[crying]',
        '*clears throat*': '[clear throat]',
        '*whispers*': '[whispering]',
    }
    for ast_tag, bracket_tag in asterisk_emotions.items():
        text = text.replace(ast_tag, bracket_tag)
    # 1. Strip double asterisks (emphasis — keep the word)
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    # 2. Single word in asterisks = emphasis, keep the word
    text = re.sub(r'\*(\S+)\*', r'\1', text)
    # Multi-word in asterisks = action, strip entirely
    text = re.sub(r'\*[^*]+\s[^*]+\*', '', text)
    # 3. Map companion emotion tags to Turbo token names
    text = map_emotion_tags(text)
    # 4. Strip unrecognized bracket content
    text = strip_unrecognized_brackets(text)
    # 5. Collapse ellipses into comma
    text = re.sub(r'\.{2,}', ',', text)
    text = re.sub(r',\s*,', ',', text)
    text = re.sub(r',\s*\.', '.', text)
    # 6. Whitespace cleanup
    text = re.sub(r'\s{2,}', ' ', text)
    return text.strip()


def chunk_text(text):
    """Split text into sentence chunks, keeping style tags attached to their sentence."""
    text = preprocess_text(text)

    # Split on sentence boundaries
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if s.strip()]

    # Break long sentences on em-dashes and commas
    expanded = []
    for s in sentences:
        if len(s) > 200:
            parts = [p.strip() for p in re.split(r'\s*\u2014\s*', s) if p.strip()]
            for p in parts:
                if len(p) > 200:
                    subparts = [sp.strip() for sp in p.split(',') if sp.strip()]
                    expanded.extend(subparts)
                else:
                    expanded.append(p)
        else:
            expanded.append(s)

    # Reattach orphaned style tags to the next chunk
    result = []
    pending_tag = None
    for chunk in expanded:
        stripped = chunk.strip()
        if stripped in TURBO_STYLE_TAGS:
            pending_tag = stripped
            continue
        if pending_tag:
            chunk = pending_tag + ' ' + chunk
            pending_tag = None
        if len(chunk) >= 3:
            result.append(chunk)
    if pending_tag:
        result.append(pending_tag)

    return result


# === Load model on startup ===
print("Loading Chatterbox Turbo (stock pretrained)...")
model = ChatterboxTurboTTS.from_pretrained(device="cuda")
print(f"  Turbo loaded. Sample rate: {model.sr}")

# Init RVC (conditional)
if RVC_ENABLED:
    from rvc.infer.infer import VoiceConverter
    print("Initializing RVC...")
    os.chdir(APPLIO_HOME)
    rvc = VoiceConverter()
    print(f"RVC ready. Model: {os.path.basename(RVC_MODEL)}")
else:
    rvc = None
    print("RVC disabled in settings.")


app = Flask(__name__)


# === Generation ===
@torch.inference_mode()
def generate_chunk(text):
    wav = model.generate(
        text,
        temperature=0.8,
        top_k=1000,
        top_p=0.95,
        repetition_penalty=1.2,
    )
    return wav.squeeze(0).numpy()


def run_rvc(wav_array, sr):
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
            pitch=RVC_PITCH,
            f0_method=RVC_F0_METHOD,
            index_rate=RVC_INDEX_RATE,
            protect=RVC_PROTECT,
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

    skip_rvc = data.get('skip_rvc', False) or not RVC_ENABLED or rvc is None
    ref_audio = data.get('reference_audio')
    if ref_audio:
        ref_path = os.path.expanduser(ref_audio)
        if os.path.exists(ref_path):
            print(f"  Using custom reference: {ref_path}")
            model.prepare_conditionals(ref_path)
        else:
            return jsonify({'error': f'reference_audio not found: {ref_path}'}), 400

    t_start = time.time()
    sentences = chunk_text(text)
    print(f"  -> {len(sentences)} chunk(s) (rvc={'off' if skip_rvc else 'on'}): {sentences}")

    converted_chunks = []
    target_sr = None

    for i, sentence in enumerate(sentences):
        label = f"{sentence[:60]}..." if len(sentence) > 60 else sentence
        print(f'  [{i+1}/{len(sentences)}] "{label}"')

        try:
            t0 = time.time()
            raw_wav = generate_chunk(sentence)
            t1 = time.time()
            print(f"    Turbo: {t1-t0:.1f}s, {len(raw_wav)/S3GEN_SR:.2f}s audio")

            if skip_rvc:
                if target_sr is None:
                    target_sr = S3GEN_SR
                converted_chunks.append(raw_wav)
            else:
                t0 = time.time()
                rvc_wav, rvc_sr = run_rvc(raw_wav, S3GEN_SR)
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
                if target_sr and target_sr != S3GEN_SR:
                    raw_wav = soxr.resample(raw_wav, S3GEN_SR, target_sr)
                converted_chunks.append(raw_wav)
                print("    Fallback: using raw Turbo audio")
            except Exception as e2:
                print(f"    SKIP chunk {i+1}: {e2}")

    if not converted_chunks:
        return jsonify({'error': 'TTS generation failed'}), 500

    if len(converted_chunks) == 1:
        combined = converted_chunks[0]
    else:
        combined = concatenate_with_gaps(converted_chunks, target_sr or S3GEN_SR)

    # Save as temp WAV then convert to MP3
    timestamp = int(time.time() * 1000)
    mp3_filename = f"tts_{timestamp}.mp3"
    mp3_path = os.path.join(VOICE_MESSAGES_DIR, mp3_filename)

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_wav = tmp.name
        sf.write(tmp_wav, combined, target_sr or S3GEN_SR)

    try:
        from pydub import AudioSegment
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
        'engine': 'chatterbox-turbo+rvc',
        'reference': os.path.basename(REFERENCE_AUDIO),
        'rvc_enabled': RVC_ENABLED,
        'silence_gap_ms': SILENCE_GAP_MS,
    })


if __name__ == '__main__':
    if not REFERENCE_AUDIO:
        print("ERROR: Set sovereignVoice.referenceAudioPath in data/settings.json or REFERENCE_AUDIO_PATH in the environment.")
        sys.exit(1)
    if not os.path.isfile(REFERENCE_AUDIO):
        print(f"ERROR: Reference audio not found: {REFERENCE_AUDIO}")
        sys.exit(1)
    print("Pre-caching reference audio conditionals...")
    model.prepare_conditionals(REFERENCE_AUDIO)
    print("  Conditionals cached")
    print(f"Starting Chatterbox Turbo+RVC voice server on {HOST}:{PORT}...")
    print(f"Voice messages dir: {VOICE_MESSAGES_DIR}")
    app.run(host=HOST, port=PORT, debug=False)
