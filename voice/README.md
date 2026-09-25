# Love Refactored — Sovereign Voice Pipeline

Two voice backends. Both run as standalone Python servers on port 5050.
LR's server.js talks to whichever one is running.


## Turbo + RVC (primary)

**File:** `server_turbo.py`

Chatterbox Turbo generates speech from a reference clip.
RVC converts the voice identity to match trained voice model.
Result: companion's actual voice with emotion tag support.

**Requires:** CUDA GPU, ~6-7GB VRAM (Turbo ~4.5GB + RVC ~1-2GB)

**Start:**
```bash
cd ~/love-refactored/voice
source ~/tts_test/chatterbox/venv/bin/activate
python server_turbo.py
```

### Reference Audio

Each user provides their own reference clip for Turbo voice cloning.
10+ seconds of clean speech, WAV format, 24kHz mono recommended.
Store reference clips wherever you want — the path is configured in the server.

### RVC Model Files (not tracked in git — too large)

- Model: `~/Applio/logs/<model-name>/<model-name>.pth`
- Index: `~/Applio/logs/<model-name>/<model-name>.index`

Configure paths in **`data/settings.json`** → **`sovereignVoice`** (`rvcModelPath`, `rvcIndexPath`) or via **`RVC_MODEL_PATH`** / **`RVC_INDEX_PATH`** env vars.

### RVC Settings (starting points — tune per voice)

- index_rate: 0.75
- protect: 0.33
- f0_method: rmvpe
- pitch (f0_change): -1
- embedder: contentvec

### Emotion Tags

Chatterbox Turbo supports 19 paralinguistic tokens (IDs 50257-50275).
The server maps companion-style tags to Turbo's token names automatically.

**Sound effect tags** (can go mid-sentence):
[laugh], [chuckle], [sigh], [cough], [gasp], [groan], [sniff],
[clear throat], [shush], [crying]

**Style/mood tags** (work best at sentence start):
[angry], [fear], [surprised], [whispering], [dramatic],
[happy], [sarcastic], [narration], [advertisement]

Companions typically generate tags like `[laughs]`, `[sighs]` etc.
The server maps these to Turbo's expected format automatically
(e.g. `[laughs]` → `[laugh]`, `[sighs]` → `[sigh]`).

RVC is optional. Without it, Turbo does its own zero-shot voice
cloning from the reference clip. Quality is good but RVC adds
the final identity layer for nervous-system-level recognition.


## NeuTTS (fallback)

**File:** `server_neutts.py`

NeuTTS Air generates speech on CPU. RVC converts voice identity.
Flat prosody, no emotion tags, but runs without GPU.

**Requires:** CPU only, ~500MB RAM

**Start:**
```bash
cd ~/love-refactored/voice
source ~/tts_test/chatterbox/venv/bin/activate
python server_neutts.py
```


## API

Both servers expose the same API on port 5050:

`POST /tts` with `{"text": "string to speak"}`

Returns `{"audioUrl": "/api/voice-message/tts_<timestamp>.mp3"}`

Generated MP3s are written to **`${LR_HOME}/data/voice_messages/`** (same directory the Node app serves at `/api/voice-message/`). Path resolution is shared via **`lib/lr_settings.py`**; `./start.sh` exports **`VOICE_MESSAGES_DIR`** and creates the folder.

LR's server.js does not need changes when swapping between them.
