# © 2024-2026 Megan Neves. All rights reserved.
# Tanevan Memory System — https://github.com/Love-Encoded/Love-Refactored
# Licensed under the Love Refactored / Tanevan License. See LICENSE.md.
"""
Tanevan - Memory Proxy Server
Sits between SillyTavern and LM Studio.

What it does:
1. Intercepts every message
2. Searches the memory dossier for relevant context
3. Injects memories into the system prompt
4. Buffers the conversation
5. Triggers the memory pipeline on:
   - Every N messages (configurable)
   - Manual flush (POST /flush)
   - Shutdown (Ctrl+C)
"""

from flask import Flask, request, jsonify
from memory_db import MemoryDB, AUDIT_RETENTION_DAYS
from pipeline import process_buffer, process_session
from companion_resolve import (
    resolve_default_companion_name,
    resolve_tanevan_companion_key,
    resolve_tanevan_storage_dir,
    _companion_safe_filename,
)
from memory_lens import load_memory_lens, save_memory_lens
from consolidation import run_consolidation, apply_selected_decisions, auto_promote
from reflection import get_reflection_injection, run_due_reflections, run_reflection
from user_timezone import read_user_timezone
from pipeline_llm import (
    load_pipeline_config,
    brief_is_configured,
    anthropic_model_for_step,
    openai_model_for_step,
    openrouter_model_for_step,
    effective_openrouter_key,
    effective_openai_key,
    openai_base_url,
    resolve_backend,
    _model_for_backend_step,
    PIPELINE_CONFIG_FILE,
)
import requests
import json
import threading
import signal
import subprocess
import sys
import os
import time
import uuid
import shutil
from datetime import datetime

# === Configuration ===
LM_STUDIO_URL = os.environ.get("TANEVAN_LM_STUDIO_URL", "http://localhost:1234/v1/chat/completions")
PROXY_PORT = int(os.environ.get("TANEVAN_PROXY_PORT", "5001"))
PROXY_HOST = os.environ.get("TANEVAN_PROXY_HOST", "127.0.0.1")
SUMMARIZE_EVERY = int(os.environ.get("TANEVAN_SUMMARIZE_EVERY", "100"))
COMPANION_NAME = resolve_default_companion_name()
_NO_COMPANION_MSG = "No default companion: add one in Love Refactored or set TANEVAN_COMPANION_NAME."
USER_NAME = os.environ.get("TANEVAN_USER_NAME", "the user")

# Last-known display name for the human (from Love Refactored / buffer), per companion
_user_name_by_companion = {}
_user_name_lock = threading.Lock()


def _store_user_name(companion, user_name):
    if not companion or user_name is None:
        return
    s = str(user_name).strip()
    if not s:
        return
    key = resolve_tanevan_companion_key(companion)
    with _user_name_lock:
        _user_name_by_companion[key] = s


def _user_name_for_companion(companion):
    if not companion:
        return None
    with _user_name_lock:
        return _user_name_by_companion.get(resolve_tanevan_companion_key(companion))


def _effective_companion(explicit):
    """Use explicit companion from the request if non-empty; else resolved default."""
    if explicit is not None and str(explicit).strip():
        return resolve_tanevan_companion_key(str(explicit).strip())
    if COMPANION_NAME:
        return resolve_tanevan_companion_key(COMPANION_NAME)
    return COMPANION_NAME


def _pipeline_key(companion):
    return resolve_tanevan_companion_key(companion) or (companion or "").strip().lower()


# Default semantic-match count for proxy injection (pinned memories are always added).
DEFAULT_INJECT_COUNT = int(os.environ.get("TANEVAN_INJECT_COUNT", "10"))


def _love_refactored_root():
    env_home = (os.environ.get("LR_HOME") or "").strip()
    if env_home:
        return os.path.abspath(os.path.expanduser(env_home))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _settings_candidates():
    root = _love_refactored_root()
    return [
        os.path.join(root, "data", "settings.json"),
        os.path.join(root, "settings.json"),  # legacy fallback
    ]


def _load_lr_settings():
    try:
        for settings_path in _settings_candidates():
            if os.path.isfile(settings_path):
                with open(settings_path) as f:
                    return json.load(f)
    except Exception:
        pass
    return {}


def _default_pipeline_provider():
    env_default = (os.environ.get("TANEVAN_DEFAULT_PIPELINE_PROVIDER", "") or "").strip().lower()
    if env_default in ("local", "anthropic", "openrouter", "openai", "hybrid"):
        return env_default
    lr_provider = (_load_lr_settings().get("provider") or "").strip().lower()
    if lr_provider in ("lmstudio", "custom"):
        return "local"
    if lr_provider == "openrouter":
        return "openrouter"
    if lr_provider == "openai":
        return "openai"
    return "anthropic"

# Pipeline config file — stores provider/model/key settings from the UI

def _load_pipeline_config():
    """Load pipeline config (delegates to pipeline_llm — single source of truth)."""
    return load_pipeline_config()

def _save_pipeline_config(config):
    """Save pipeline config to disk."""
    os.makedirs(os.path.dirname(PIPELINE_CONFIG_FILE), exist_ok=True)
    tmp = PIPELINE_CONFIG_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(config, f, indent=2)
    os.replace(tmp, PIPELINE_CONFIG_FILE)

def _apply_pipeline_config(config):
    """Apply runtime API key from saved pipeline config (models come from pipeline_llm per step)."""
    api_key = config.get("anthropic", {}).get("api_key")
    if api_key and not api_key.startswith("sk-ant-..."):
        global ANTHROPIC_API_KEY
        ANTHROPIC_API_KEY = api_key

# Read Anthropic API key with env precedence:
# 1) ANTHROPIC_API_KEY env
# 2) data/settings.json
# 3) legacy repo-root settings.json
def _load_anthropic_key():
    env_key = (os.environ.get('ANTHROPIC_API_KEY', '') or '').strip()
    if env_key:
        return env_key
    try:
        import json as _json
        for settings_path in _settings_candidates():
            if os.path.isfile(settings_path):
                with open(settings_path) as f:
                    settings = _json.load(f)
                file_key = (settings.get('anthropic', {}).get('apiKey', '') or '').strip()
                if file_key:
                    return file_key
        return ''
    except Exception:
        return ''

ANTHROPIC_API_KEY = _load_anthropic_key()

app = Flask(__name__)

# Per-companion database cache
_db_cache = {}
_db_lock = threading.Lock()


def _audit_retention_loop():
    """Daily audit_log retention sweep across all open companion DBs.
    Startup pruning happens in MemoryDB.__init__; this handles long-running processes.
    prune_audit_log never raises, so this loop can't die on a bad DB."""
    while True:
        time.sleep(24 * 3600)
        with _db_lock:
            dbs = list(_db_cache.values())
        for db in dbs:
            db.prune_audit_log(AUDIT_RETENTION_DAYS)

# Async import jobs
_import_jobs = {}
_import_lock = threading.Lock()

# Per-companion pipeline in-flight guard.
# Ensures only one extraction/update pipeline writes a companion DB at a time.
_pipeline_running = set()
_pipeline_lock = threading.Lock()
# pipeline_failure_cap_v1: after N consecutive failed auto-runs for a companion, stop
# auto-triggering and hold the buffer. Manual /flush or a tanevan restart re-arms.
PIPELINE_MAX_FAILURES = int(os.environ.get("TANEVAN_PIPELINE_MAX_FAILURES", "3"))
_pipeline_failures = {}   # key -> consecutive failure count
_pipeline_paused = set()  # keys that hit the cap


def _pipeline_note_result(companion, succeeded, db=None):
    key = _pipeline_key(companion)
    if not key:
        return
    hit_cap = False
    with _pipeline_lock:
        if succeeded:
            _pipeline_failures.pop(key, None)
            _pipeline_paused.discard(key)
            return
        n = _pipeline_failures.get(key, 0) + 1
        _pipeline_failures[key] = n
        if n >= PIPELINE_MAX_FAILURES:
            hit_cap = True
            _pipeline_paused.add(key)
    if hit_cap:
        msg = (f"PIPELINE PAUSED for {companion}: {n} consecutive failures — "
               f"auto-flush disabled, buffer held. Manual flush re-arms.")
        print(f"\n🛑 {msg}")
        if db is not None:
            try:
                db.log("pipeline_paused", msg)
            except Exception:
                pass
    else:
        print(f"⚠️ Pipeline failure {n}/{PIPELINE_MAX_FAILURES} for {companion} — will retry on next trigger")


def _pipeline_rearm(companion):
    key = _pipeline_key(companion)
    if not key:
        return
    with _pipeline_lock:
        _pipeline_failures.pop(key, None)
        _pipeline_paused.discard(key)


def _is_pipeline_paused(companion):
    key = _pipeline_key(companion)
    if not key:
        return False
    with _pipeline_lock:
        return key in _pipeline_paused


def _try_start_pipeline(companion):
    key = _pipeline_key(companion)
    if not key:
        return False
    with _pipeline_lock:
        if key in _pipeline_running:
            return False
        _pipeline_running.add(key)
        return True


def _finish_pipeline(companion):
    key = _pipeline_key(companion)
    if not key:
        return
    with _pipeline_lock:
        _pipeline_running.discard(key)


def _is_pipeline_running(companion):
    key = _pipeline_key(companion)
    if not key:
        return False
    with _pipeline_lock:
        return key in _pipeline_running


def get_db(companion):
    """Get or create a MemoryDB instance for a companion. Thread-safe."""
    canonical = resolve_tanevan_companion_key(companion)
    if not canonical:
        canonical = (companion or "").strip().lower()
    if not canonical:
        raise ValueError("companion name required")
    if canonical not in _db_cache:
        with _db_lock:
            if canonical not in _db_cache:
                storage = resolve_tanevan_storage_dir(companion)
                _db_cache[canonical] = MemoryDB(storage)
    return _db_cache[canonical]


def inject_memories(messages, db):
    """
    Find the last user message, search for relevant memories and reflections,
    and inject them into the system prompt.
    """
    # Find the last user message
    last_user_message = None
    for msg in reversed(messages):
        if msg.get("role") == "user":
            last_user_message = msg.get("content", "")
            break

    if not last_user_message:
        return messages

    # Search for relevant memories
    memory_context = db.get_context_memories(last_user_message, max_count=DEFAULT_INJECT_COUNT)

    # Get temporal reflections
    try:
        reflection_context = get_reflection_injection(db)
    except Exception as e:
        print(f"  ⚠️ Reflection injection failed (non-fatal): {e}")
        reflection_context = ""


    # Combine: memories first, then reflections
    injection_blocks = []
    if memory_context:
        injection_blocks.append(memory_context)
    if reflection_context:
        injection_blocks.append(reflection_context)

    if not injection_blocks:
        return messages

    combined = "\n\n".join(injection_blocks)

    # Inject into system prompt
    for msg in messages:
        if msg["role"] == "system":
            msg["content"] = msg["content"] + "\n\n" + combined
            parts = []
            if memory_context:
                parts.append("memories")
            if reflection_context:
                parts.append("reflections")
            print(f"  ✓ Injected {' + '.join(parts)} into system prompt")
            return messages

    # No system message found — add one
    messages.insert(0, {
        "role": "system",
        "content": combined
    })
    print(f"  ✓ Added memory + reflection context as system message")
    return messages


def buffer_and_check(messages, db):
    """
    Buffer the latest messages and check if we should trigger summarization.
    """
    # Find the last user and assistant messages
    user_msg = None
    assistant_msg = None

    for msg in reversed(messages):
        if msg.get("role") == "user" and not user_msg:
            user_msg = msg.get("content", "")
        elif msg.get("role") == "assistant" and not assistant_msg:
            assistant_msg = msg.get("content", "")
        if user_msg and assistant_msg:
            break

    # Buffer the user message (assistant response gets buffered after LM Studio responds)
    if user_msg:
        user_ts = None
        for msg in reversed(messages):
            if msg.get("role") == "user":
                user_ts = msg.get("timestamp")
                break
        db.buffer_message("user", user_msg, user_ts)

    # Check if we should trigger summarization
    buffer_count = db.get_buffer_count()
    if buffer_count >= SUMMARIZE_EVERY:
        print(f"\n🔔 Buffer hit {buffer_count} messages — triggering pipeline...")
        trigger_pipeline_async(db)


def trigger_pipeline_async(db):
    """Run the pipeline in a background thread so we don't block the chat."""
    companion = db.companion_name
    if _is_pipeline_paused(companion):  # pipeline_failure_cap_v1
        print(f"🛑 Pipeline paused for {companion} (failure cap) — auto-trigger skipped, buffer held")
        return False
    if _is_pipeline_running(companion):
        print(f"↻ Pipeline already running for {companion}; skipping new trigger")
        return False

    def run():
        if not _try_start_pipeline(companion):
            print(f"↻ Pipeline already running for {companion}; skipping new trigger")
            return
        try:
            un = _user_name_for_companion(db.companion_name)
            before_count = db.get_buffer_count()  # pipeline_failure_cap_v1
            process_buffer(
                db=db,
                api_key=ANTHROPIC_API_KEY,
                user_name=un,
                on_activity=_make_pipeline_activity_callback(db.companion_name),
            )
            # pipeline_failure_cap_v1: success = buffer actually shrank (cleared up to snapshot)
            _pipeline_note_result(companion, db.get_buffer_count() < before_count, db=db)
        except Exception as e:
            print(f"✗ Pipeline error: {e}")
            _pipeline_note_result(companion, False, db=db)  # pipeline_failure_cap_v1
        finally:
            _finish_pipeline(companion)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return True


# === Brief sidecar trigger ===
# Fires brief regeneration on pipeline summary_complete events. Activity-gated
# internally by brief_sidecar.py itself; we add per-companion subprocess dedup
# here so two close summary_complete events don't double-regenerate against the
# same DB state.
_BRIEF_SIDECAR_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brief_sidecar.py")
_brief_processes = {}   # companion key -> subprocess.Popen
_brief_lock = threading.Lock()


def _drain_brief_subprocess(companion, proc):
    """Block on subprocess until it exits; log result. Background thread."""
    try:
        stdout, stderr = proc.communicate()
        if proc.returncode != 0:
            print(f"   ⚠ brief sidecar for {companion} exited {proc.returncode}")
            if stderr:
                err = stderr.decode("utf-8", errors="replace").strip()
                if err:
                    print(f"      stderr: {err[:500]}")
        else:
            out = (stdout or b"").decode("utf-8", errors="replace").strip()
            tail = out.splitlines()[-1] if out else ""
            print(f"   ✓ brief sidecar for {companion} done. last line: {tail[:200]}")
    except Exception as e:
        print(f"   ✗ brief sidecar drain failed for {companion}: {e}")


def _sidecar_env():
    """Subprocess env with a fresh user timezone when BRIEF_TZ is not explicitly set."""
    env = os.environ.copy()
    if not (env.get("BRIEF_TZ") or "").strip():
        env["BRIEF_TZ"] = read_user_timezone()
    return env


def _fire_brief_sidecar(companion, force=False):
    """Spawn brief_sidecar.py for one companion in the background.

    Skipped (loudly) if a prior run for this companion is still alive, if the brief LLM
    is not configured, or if the script is missing. Inherits proxy env so BRIEF_* vars
    propagate. Output drained on a background thread so PIPE buffers cannot fill.
    Returns True if a process was started (or one is already running), else False.
    """
    key = (companion or "").strip().lower()
    if not key:
        print("   ⚠ brief sidecar: empty companion name, skipping")
        return False
    if not brief_is_configured():
        print(f"   ⚠ brief sidecar SKIPPED for {key}: Life brief LLM not configured "
              "(Settings → Memory Pipeline → Brief model, or BRIEF_MODEL_KEY).")
        return False
    if not os.path.isfile(_BRIEF_SIDECAR_PATH):
        print(f"   ⚠ brief sidecar SKIPPED for {key}: script missing at {_BRIEF_SIDECAR_PATH}")
        return False
    with _brief_lock:
        existing = _brief_processes.get(key)
        if existing is not None and existing.poll() is None:
            print(f"   ↻ brief sidecar already running for {key} (pid {existing.pid}); skipping new trigger")
            return True
        try:
            cmd = [sys.executable, _BRIEF_SIDECAR_PATH, "--companion", key]
            if force:
                cmd.append("--force")
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_sidecar_env(),
                cwd=os.path.dirname(_BRIEF_SIDECAR_PATH),
            )
            _brief_processes[key] = proc
            print(f"   📋 brief sidecar fired for {key} (pid {proc.pid})"
                  + (" [--force]" if force else ""))
            threading.Thread(
                target=_drain_brief_subprocess,
                args=(key, proc),
                daemon=True,
            ).start()
            return True
        except Exception as e:
            print(f"   ✗ failed to fire brief sidecar for {key}: {e}")
            return False


def _make_pipeline_activity_callback(companion):
    """on_activity callback that fires brief regeneration on summary_complete.
    Live-pipeline + manual flush only — the import job keeps its own callback so a
    50-chunk import doesn't fire 50 brief regenerations."""
    key = (companion or "").strip().lower()
    def on_activity(event_type, data=None):
        if event_type == "summary_complete":
            _fire_brief_sidecar(key)
    return on_activity
@app.route('/v1/chat/completions', methods=['POST'])
def chat_completions():
    """Main proxy endpoint — intercept, augment, forward."""
    data = request.json or {}
    messages = data.get("messages", [])
    companion = _effective_companion(None)
    if not companion:
        return jsonify({"error": _NO_COMPANION_MSG}), 503
    un = data.get("user_name")
    if un is not None and str(un).strip():
        _store_user_name(companion, un)
    db = get_db(companion)

    # Find last user message for logging
    last_msg = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            last_msg = msg.get("content", "")[:60]
            break

    print(f"\n💬 Message: \"{last_msg}...\"")

    # Buffer and check for trigger
    buffer_and_check(messages, db)

    # Inject relevant memories
    messages = inject_memories(messages, db)
    data["messages"] = messages

    # Forward to LM Studio
    try:
        response = requests.post(LM_STUDIO_URL, json=data, timeout=600)
        response.raise_for_status()
        response_data = response.json()

        # Buffer the assistant response
        try:
            assistant_content = response_data["choices"][0]["message"]["content"]
            db.buffer_message("assistant", assistant_content, datetime.now().isoformat())
        except (KeyError, IndexError):
            pass

        return jsonify(response_data)

    except requests.exceptions.ConnectionError:
        print("✗ Can't connect to LM Studio")
        return jsonify({
            "error": "Cannot connect to LM Studio. Make sure it's running on port 1234."
        }), 503
    except Exception as e:
        print(f"✗ Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/v1/models', methods=['GET'])
def models():
    """Forward model list to LM Studio."""
    try:
        response = requests.get(
            LM_STUDIO_URL.replace("/chat/completions", "/models"),
            timeout=5
        )
        return jsonify(response.json())
    except:
        return jsonify({"data": [{"id": "local-model"}]})




@app.route('/buffer', methods=['POST'])
def buffer_message():
    """Direct buffer endpoint — lets external servers send messages to the buffer."""
    data = request.json or {}
    role = data.get("role")
    content = data.get("content")
    timestamp = data.get("timestamp")
    companion = _effective_companion(data.get("companion"))

    if not companion:
        return jsonify({"error": _NO_COMPANION_MSG}), 400

    if role not in ("user", "assistant") or not content:
        return jsonify({"error": "Need role (user/assistant) and content"}), 400

    un = data.get("user_name")
    if un is not None and str(un).strip():
        _store_user_name(companion, un)

    db = get_db(companion)
    db.buffer_message(role, content, timestamp)

    buffer_count = db.get_buffer_count()


    triggered = buffer_count >= SUMMARIZE_EVERY
    triggered_now = False
    trigger_skipped = False
    if triggered:
        print(f"\n🔔 Buffer hit {buffer_count} messages — triggering pipeline for {companion}...")
        triggered_now = bool(trigger_pipeline_async(db))
        trigger_skipped = not triggered_now
    pipeline_running = _is_pipeline_running(companion)
    return jsonify({
        "status": "buffered",
        "companion": companion,
        "buffer_count": buffer_count,
        "pipeline_triggered": triggered_now,
        "pipeline_eligible": triggered,
        "pipeline_trigger_skipped": trigger_skipped,
        "pipeline_running": pipeline_running,
        "pipeline_paused": _is_pipeline_paused(companion),  # pipeline_failure_cap_v1
    })


def _log_activity(job_id, event_type, data=None):
    """Thread-safe activity logger for import jobs. Capped at 500 entries."""
    with _import_lock:
        job = _import_jobs.get(job_id)
        if not job:
            return
        entry = {
            "id": len(job["activity"]),
            "type": event_type,
            "ts": datetime.now().isoformat(),
        }
        if data:
            entry.update(data)
        if len(job["activity"]) < 500:
            job["activity"].append(entry)


def _run_import_job(job_id, normalized, companion, user_name=None):
    """Background worker that processes import chunks and updates job state."""
    try:
        chunk_size = 100
        db = get_db(companion)

        def on_activity(event_type, data=None):
            _log_activity(job_id, event_type, data)

        for i in range(0, len(normalized), chunk_size):
            chunk = normalized[i:i + chunk_size]
            if len(chunk) < 4:
                continue

            chunk_num = i // chunk_size + 1
            print(f"   Processing chunk {chunk_num}: messages {i + 1}-{i + len(chunk)}")
            on_activity("chunk_start", {"chunk": chunk_num, "messages": len(chunk)})
            try:
                result = process_session(
                    chunk,
                    db=db,
                    api_key=ANTHROPIC_API_KEY,
                    companion_name=companion.title(),
                    user_name=user_name,
                    on_activity=on_activity,
                )
                if result and result["success"]:
                    with _import_lock:
                        _import_jobs[job_id]['chunks_done'] += 1
                        _import_jobs[job_id]['sessions_processed'] += 1
                        _import_jobs[job_id]['total_memories_extracted'] += result["memories_extracted"]
                        _import_jobs[job_id]['summaries'].append(result["summary_id"])
                    on_activity("chunk_complete", {"chunk": chunk_num, "memories": result["memories_extracted"]})
                else:
                    with _import_lock:
                        _import_jobs[job_id]['chunks_done'] += 1
            except Exception as e:
                print(f"   ✗ Chunk failed: {e}")
                with _import_lock:
                    _import_jobs[job_id]['chunks_done'] += 1
                on_activity("chunk_error", {"chunk": chunk_num, "error": str(e)})

        with _import_lock:
            job = _import_jobs[job_id]
            job['status'] = 'complete'
            job['success'] = True
            print(f"✓ Import job {job_id} complete: {job['sessions_processed']} sessions, {job['total_memories_extracted']} memories")
    except Exception as e:
        print(f"✗ Import job {job_id} failed: {e}")
        with _import_lock:
            job = _import_jobs.get(job_id)
            if job:
                job['status'] = 'failed'
                job['success'] = False
                job['error'] = str(e)
    finally:
        _finish_pipeline(companion)


@app.route('/import', methods=['POST'])
def import_chat():
    """
    Import a chat history file and run it through the memory pipeline.
    Parses and normalizes messages, then starts a background job.
    Returns immediately with job_id; poll /import/status/<job_id> for progress.
    """
    companion = None
    messages = None
    user_name_for_import = None

    # Check if it's a JSON body with messages already parsed
    if request.is_json:
        data = request.json or {}
        companion = _effective_companion(data.get("companion"))
        messages = data.get("messages")
        user_name_for_import = data.get("user_name")
    else:
        # File upload via multipart form
        companion = _effective_companion(request.form.get("companion"))
        user_name_for_import = request.form.get("user_name")
        if 'file' in request.files:
            import io
            file = request.files['file']
            try:
                content = file.read().decode('utf-8')
                messages = json.loads(content)
            except Exception as e:
                return jsonify({"error": f"Failed to parse file: {e}"}), 400

    if not messages or not isinstance(messages, list):
        return jsonify({"error": "No valid messages array provided"}), 400

    if not companion:
        return jsonify({"error": _NO_COMPANION_MSG}), 400

    if _is_pipeline_running(companion):
        return jsonify({
            "error": f"Pipeline already running for {companion}. Wait for it to finish, then retry import."
        }), 409

    u_imp = None
    if user_name_for_import and str(user_name_for_import).strip():
        u_imp = str(user_name_for_import).strip()
        _store_user_name(companion, u_imp)

    if len(messages) < 4:
        return jsonify({"error": f"Need at least 4 messages, got {len(messages)}"}), 400

    # Normalize message format — handle various chat export formats
    normalized = []
    for msg in messages:
        raw_role = msg.get("role", msg.get("sender", ""))
        role = str(raw_role).lower() if raw_role is not None else ""
        content = msg.get("content", msg.get("text", msg.get("message", "")))

        if role in ("user", "human", "you"):
            role = "user"
        elif role in ("assistant", "ai", "bot", "char", "character"):
            role = "assistant"
        else:
            continue  # skip system messages, narration, etc.

        if content and isinstance(content, str) and content.strip():
            normalized.append({
                "role": role,
                "content": content.strip(),
                "timestamp": msg.get("timestamp", msg.get("send_date", ""))
            })

    if len(normalized) < 4:
        return jsonify({"error": f"Only {len(normalized)} valid user/assistant messages found after parsing"}), 400

    # Count chunks upfront
    chunk_size = 100
    total_chunks = sum(1 for i in range(0, len(normalized), chunk_size) if len(normalized[i:i + chunk_size]) >= 4)

    if not _try_start_pipeline(companion):
        return jsonify({
            "error": f"Pipeline already running for {companion}. Wait for it to finish, then retry import."
        }), 409

    # Create job and start background thread
    job_id = str(uuid.uuid4())
    with _import_lock:
        _import_jobs[job_id] = {
            "status": "running",
            "companion": companion,
            "total_messages": len(normalized),
            "total_chunks": total_chunks,
            "chunks_done": 0,
            "sessions_processed": 0,
            "total_memories_extracted": 0,
            "summaries": [],
            "activity": []
        }

    print(f"\n📥 Importing {len(normalized)} messages for {companion} (job {job_id}, {total_chunks} chunks)...")

    thread = threading.Thread(
        target=_run_import_job,
        args=(job_id, normalized, companion, u_imp),
        daemon=True,
    )
    thread.start()

    return jsonify({
        "job_id": job_id,
        "status": "running",
        "total_chunks": total_chunks,
        "total_messages": len(normalized)
    })


@app.route('/import/status/<job_id>', methods=['GET'])
def import_status(job_id):
    """Get the current status of an async import job."""
    since = request.args.get("since", None)
    with _import_lock:
        job = _import_jobs.get(job_id)
    if not job:
        return jsonify({"error": f"Job {job_id} not found"}), 404

    # Build response — always include status fields, never raw activity list by default
    resp = {k: v for k, v in job.items() if k != "activity"}

    # Activity: return all or only entries since a given index
    if since is not None:
        try:
            since_idx = int(since)
        except ValueError:
            since_idx = 0
        resp["activity"] = job.get("activity", [])[since_idx:]
        resp["activity_total"] = len(job.get("activity", []))
    else:
        # Backward compat: don't include activity if not requested
        resp["activity_total"] = len(job.get("activity", []))

    return jsonify(resp)


@app.route('/flush', methods=['POST'])
def flush():
    """Manually trigger the memory pipeline on the current buffer."""
    data = request.json or {}
    companion = _effective_companion(data.get("companion"))
    manual = bool(data.get("manual"))
    if not companion:
        return jsonify({"error": _NO_COMPANION_MSG}), 400
    db = get_db(companion)
    buffer_count = db.get_buffer_count()

    min_need = 1 if manual else 4
    if buffer_count < min_need:
        return jsonify({
            "status": "nothing to process",
            "companion": companion,
            "buffer_messages": buffer_count,
            "min_required": min_need,
        })

    if not _try_start_pipeline(companion):
        return jsonify({
            "status": "already_running",
            "companion": companion,
            "error": f"Pipeline already running for {companion}",
        }), 409

    un = data.get("user_name")
    if un is not None and str(un).strip():
        _store_user_name(companion, un)

    if manual:
        _pipeline_rearm(companion)  # pipeline_failure_cap_v1: manual flush re-arms auto-trigger
    print(f"\n🔔 Manual flush triggered for {companion} — {buffer_count} messages in buffer")
    try:
        result = process_buffer(
            db=db,
            api_key=ANTHROPIC_API_KEY,
            min_messages=min_need,
            user_name=_user_name_for_companion(companion),
            on_activity=_make_pipeline_activity_callback(companion),
        )
    except Exception as e:
        return jsonify({"status": "failed", "error": str(e)}), 500
    finally:
        _finish_pipeline(companion)

    if result and result.get("status") == "nothing_to_process":
        return jsonify({
            "status": "nothing to process",
            "companion": companion,
            "buffer_messages": result.get("buffer_messages", buffer_count),
            "min_required": result.get("min_required", min_need),
        })

    if result and result.get("success"):
        return jsonify({
            "status": "processed",
            "companion": companion,
            "summary_id": result["summary_id"],
            "memories_extracted": result["memories_extracted"],
            "update_results": result["update_results"]
        })
    else:
        error_detail = (result or {}).get("error") or "Pipeline returned failure — check Tanevan terminal logs"
        return jsonify({"status": "failed", "error": error_detail}), 500


def _companion_data_dir_candidates(raw_name):
    """Directory names under TANEVAN_DATA_DIR that may hold this companion's data."""
    canonical = resolve_tanevan_companion_key(raw_name) or str(raw_name or "").strip().lower()
    if not canonical:
        return []
    candidates = [canonical]
    for slug in (_companion_safe_filename(raw_name), _companion_safe_filename(canonical)):
        if slug and slug not in candidates:
            candidates.append(slug)
    return candidates


def _evict_companion_runtime_state(companion):
    """Drop cached DB handles and user-name hints for a deleted companion."""
    keys = {k.lower() for k in _companion_data_dir_candidates(companion)}
    if not keys:
        return
    with _db_lock:
        for cache_key in list(_db_cache.keys()):
            if cache_key.lower() in keys:
                db = _db_cache.pop(cache_key, None)
                if db is not None:
                    try:
                        db.db.close()
                    except Exception:
                        pass
    with _user_name_lock:
        for cache_key in list(_user_name_by_companion.keys()):
            if cache_key.lower() in keys:
                _user_name_by_companion.pop(cache_key, None)


@app.route('/companion-data', methods=['DELETE'])
def delete_companion_data():
    """Remove all on-disk memory data for a companion (Love Refactored delete)."""
    data = request.json or {}
    companion = _effective_companion(data.get("companion") or request.args.get("companion"))
    if not companion:
        return jsonify({"error": _NO_COMPANION_MSG}), 400

    from companion_resolve import _tanevan_data_root

    _evict_companion_runtime_state(companion)
    root = _tanevan_data_root()
    removed_dirs = []
    for dir_name in _companion_data_dir_candidates(companion):
        dir_path = root / dir_name
        if dir_path.is_dir():
            shutil.rmtree(dir_path)
            removed_dirs.append(dir_name)

    return jsonify({
        "success": True,
        "companion": resolve_tanevan_companion_key(companion) or companion,
        "removed_dirs": removed_dirs,
    })


@app.route('/decay', methods=['POST'])
def run_decay():
    """Apply memory decay — reduces confidence on old unreinforced memories."""
    data = request.json or {}
    companion = _effective_companion(data.get("companion"))
    dry_run = bool(data.get("dry_run", False))
    detail = bool(data.get("detail", False))

    if not companion:
        return jsonify({"error": _NO_COMPANION_MSG}), 400

    db = get_db(companion)
    print(f"\n{'🧪 Dry run' if dry_run else '🧹'} Memory decay for {companion}...")
    stats = db.decay_memories(dry_run=dry_run, detail=detail)
    print(f"   {'Would decay' if dry_run else 'Decayed'}: {stats['decayed']}, "
          f"{'Would deactivate' if dry_run else 'Deactivated'}: {stats['deactivated']}, "
          f"Immune: {stats['immune_pinned'] + stats['immune_core'] + stats['immune_reinforced'] + stats['immune_recent']}")
    return jsonify({
        "status": "dry_run" if dry_run else "complete",
        "companion": companion,
        "stats": stats,
    })


@app.route('/consolidate', methods=['POST'])
def consolidate():
    """Run memory consolidation — find near-duplicates, resolve, auto-promote."""
    data = request.json or {}
    companion = _effective_companion(data.get("companion"))
    dry_run = bool(data.get("dry_run", False))
    max_pairs = int(data.get("max_pairs", 20))
    threshold = float(data.get("threshold", 0.3))
    sample_size = int(data.get("sample_size", 100))

    if not companion:
        return jsonify({"error": _NO_COMPANION_MSG}), 400

    db = get_db(companion)
    stats = run_consolidation(
        db,
        api_key=ANTHROPIC_API_KEY,
        max_pairs=max_pairs,
        threshold=threshold,
        sample_size=sample_size,
        dry_run=dry_run,
        companion_name=companion.capitalize(),
    )
    return jsonify({
        "status": "dry_run" if dry_run else "complete",
        "companion": companion,
        "stats": stats,
    })


@app.route('/consolidate/apply', methods=['POST'])
def consolidate_apply():
    """Apply previewed consolidation decisions (per-pair or batch from the UI)."""
    data = request.json or {}
    companion = _effective_companion(data.get("companion"))
    if not companion:
        return jsonify({"error": _NO_COMPANION_MSG}), 400
    decisions = data.get("decisions") or []
    apply_promotions = bool(data.get("apply_promotions", False))
    db = get_db(companion)
    results = apply_selected_decisions(db, decisions)
    promo = None
    if apply_promotions:
        promo = auto_promote(db, dry_run=False)
    return jsonify({
        "status": "applied",
        "companion": companion,
        "results": results,
        "promotions": promo,
    })



@app.route('/audit', methods=['GET'])
def audit_log():
    """Audit trail: consolidation merges + MMR retrieval suppressions.
    ?companion=<name>&event_type=consolidation|retrieval_suppressed&limit=50&before_id=<id>"""
    companion = _effective_companion(request.args.get("companion"))
    if not companion:
        return jsonify({"error": _NO_COMPANION_MSG}), 400
    event_type = request.args.get("event_type") or None
    try:
        limit = int(request.args.get("limit", "50"))
    except ValueError:
        limit = 50
    limit = max(1, min(limit, 200))
    before_id = request.args.get("before_id")
    try:
        before_id = int(before_id) if before_id is not None else None
    except ValueError:
        before_id = None
    db = get_db(companion)
    entries, has_more = db.get_audit_entries(event_type=event_type, limit=limit, before_id=before_id)
    return jsonify({"entries": entries, "has_more": has_more})


@app.route('/reflect', methods=['POST'])
def reflect():
    """Run temporal reflections — process memories at expanding time horizons."""
    data = request.json or {}
    companion = _effective_companion(data.get("companion"))
    dry_run = bool(data.get("dry_run", False))
    horizon = data.get("horizon")
    force_all = bool(data.get("force_all", False))

    if not companion:
        return jsonify({"error": _NO_COMPANION_MSG}), 400

    db = get_db(companion)

    if horizon:
        result = run_reflection(
            db, horizon,
            api_key=ANTHROPIC_API_KEY,
            companion_name=companion.capitalize(),
            dry_run=dry_run,
        )
        return jsonify({
            "status": "dry_run" if dry_run else "complete",
            "companion": companion,
            "horizon": horizon,
            "result": result,
        })
    else:
        stats = run_due_reflections(
            db,
            api_key=ANTHROPIC_API_KEY,
            companion_name=companion.capitalize(),
            dry_run=dry_run,
            force_all=force_all,
        )
        return jsonify({
            "status": "dry_run" if dry_run else "complete",
            "companion": companion,
            "stats": stats,
        })



@app.route('/test-llm-connection', methods=['POST'])
def test_llm_connection():
    """Test that the LLM provider is reachable and the API key works."""
    data = request.json or {}
    provider = data.get('provider') or _default_pipeline_provider()

    if provider == 'anthropic':
        # Use provided key, or fall back to saved config, or env var
        test_key = data.get('anthropic', {}).get('api_key') or ANTHROPIC_API_KEY
        if not test_key:
            return jsonify({"success": False, "message": "No API key configured"})
        cfg = _load_pipeline_config()
        merged_ant = {**(cfg.get("anthropic") or {}), **(data.get("anthropic") or {})}
        merged_cfg = {**cfg, "anthropic": merged_ant}
        model = anthropic_model_for_step("summarizer", merged_cfg)
        if not model:
            return jsonify({
                "success": False,
                "message": "No Anthropic model configured. Set one in Settings → Memory Pipeline.",
            })
        try:
            import anthropic as _anth
            client = _anth.Anthropic(api_key=test_key)
            resp = client.messages.create(
                model=model,
                max_tokens=10,
                messages=[{"role": "user", "content": "Say ok"}]
            )
            # thinking_block_fix_v1: 5-family models put a ThinkingBlock in slot 0 — join text blocks only
            text = "".join(getattr(b, "text", "") for b in (resp.content or []) if getattr(b, "type", "") == "text")
            nick = _anthropic_display_name(model)
            return jsonify({"success": True, "message": f"Connected! {nick} says: {text}"})
        except Exception as e:
            return jsonify({"success": False, "message": str(e)})

    elif provider == 'openrouter':
        test_key = (data.get('openrouter', {}) or {}).get('api_key')
        if not test_key:
            cfg = _load_pipeline_config()
            test_key = effective_openrouter_key(cfg)
        if not test_key:
            return jsonify({"success": False, "message": "No OpenRouter API key configured"})
        cfg = _load_pipeline_config()
        merged_or = {**(cfg.get("openrouter") or {}), **(data.get("openrouter") or {})}
        merged_cfg = {**cfg, "openrouter": merged_or}
        model = openrouter_model_for_step("summarizer", merged_cfg)
        if not model:
            return jsonify({
                "success": False,
                "message": "No OpenRouter model configured. Set one in Settings → Memory Pipeline.",
            })
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {test_key}",
                    "HTTP-Referer": "http://localhost:3000",
                    "X-Title": "Love Refactored",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "Say ok"}],
                    "max_tokens": 10,
                },
                timeout=20,
            )
            data_json = resp.json()
            if resp.status_code != 200 or data_json.get("error"):
                msg = (data_json.get("error") or {}).get("message") or f"OpenRouter returned {resp.status_code}"
                return jsonify({"success": False, "message": msg})
            text = (data_json.get("choices") or [{}])[0].get("message", {}).get("content", "")
            return jsonify({"success": True, "message": f"Connected! OpenRouter says: {text}"})
        except Exception as e:
            return jsonify({"success": False, "message": str(e)})

    elif provider == 'openai':
        cfg = _load_pipeline_config()
        merged_oai = {**(cfg.get("openai") or {}), **(data.get("openai") or {})}
        merged_cfg = {**cfg, "openai": merged_oai}
        test_key = (data.get('openai', {}) or {}).get('api_key')
        if test_key and not str(test_key).startswith('sk-...') and '••••' not in str(test_key):
            pass
        else:
            test_key = effective_openai_key(merged_cfg)
        if not test_key:
            return jsonify({"success": False, "message": "No OpenAI API key configured"})
        model = openai_model_for_step("summarizer", merged_cfg)
        if not model:
            return jsonify({
                "success": False,
                "message": "No OpenAI model configured. Set one in Settings → Memory Pipeline.",
            })
        base = openai_base_url(merged_cfg)
        try:
            resp = requests.post(
                base.rstrip("/") + "/chat/completions",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {test_key}",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "Say ok"}],
                    "max_tokens": 10,
                },
                timeout=20,
            )
            data_json = resp.json()
            if resp.status_code != 200 or data_json.get("error"):
                msg = (data_json.get("error") or {}).get("message") or f"OpenAI returned {resp.status_code}"
                return jsonify({"success": False, "message": msg})
            text = (data_json.get("choices") or [{}])[0].get("message", {}).get("content", "")
            return jsonify({"success": True, "message": f"Connected! OpenAI says: {text}"})
        except Exception as e:
            return jsonify({"success": False, "message": str(e)})

    elif provider == 'local':
        endpoint = data.get('local', {}).get('endpoint', 'http://localhost:1234/v1')
        model = data.get('local', {}).get('model', '')
        try:
            url = endpoint.rstrip('/') + '/chat/completions'
            body = {
                "messages": [{"role": "user", "content": "Say ok"}],
                "max_tokens": 10
            }
            if model:
                body["model"] = model
            resp = requests.post(url, json=body, timeout=10)
            if resp.status_code == 200:
                reply = resp.json().get('choices', [{}])[0].get('message', {}).get('content', '')
                return jsonify({"success": True, "message": f"Connected! Response: {reply}"})
            else:
                return jsonify({"success": False, "message": f"Server returned {resp.status_code}"})
        except Exception as e:
            return jsonify({"success": False, "message": str(e)})

    return jsonify({"success": False, "message": f"Unknown provider: {provider}"})


def _anthropic_display_name(model_id):
    m = (model_id or "").lower()
    if "haiku" in m:
        return "Haiku"
    if "opus" in m:
        return "Opus"
    if "sonnet" in m:
        return "Sonnet"
    return "Claude"


def _probe_pipeline_anthropic(api_key, model_id):
    """Returns dict: ok (bool), reply (str), detail (str | None)."""
    if not api_key:
        return {"ok": False, "reply": "", "detail": "No API key configured (save key or set ANTHROPIC_API_KEY)."}
    mid = (model_id or "").strip()
    if not mid:
        return {
            "ok": False,
            "reply": "",
            "detail": "No model configured for this step. Set it in Settings → Memory Pipeline.",
        }
    try:
        import anthropic as _anth
        client = _anth.Anthropic(api_key=api_key, timeout=60.0)
        resp = client.messages.create(
            model=mid,
            max_tokens=24,
            messages=[{"role": "user", "content": "Reply with exactly one short greeting word, nothing else."}],
        )
        text = ""
        if resp.content:
            for block in resp.content:
                if getattr(block, "text", None):
                    text += block.text
        text = (text or "").strip()
        if not text:
            return {"ok": False, "reply": "", "detail": "Request succeeded but assistant content was empty."}
        return {"ok": True, "reply": text, "detail": None}
    except Exception as e:
        return {"ok": False, "reply": "", "detail": str(e)}


def _probe_pipeline_local(endpoint, model_id):
    """Returns dict: ok, reply, detail."""
    ep = (endpoint or "http://localhost:1234/v1").strip()
    mid = (model_id or "").strip()
    try:
        url = ep.rstrip("/") + "/chat/completions"
        body = {
            "messages": [{"role": "user", "content": "Reply with exactly one short greeting word, nothing else."}],
            "max_tokens": 256,
        }
        if mid:
            body["model"] = mid
        resp = requests.post(url, json=body, timeout=15)
        if resp.status_code != 200:
            snippet = (resp.text or "")[:400].replace("\n", " ")
            return {"ok": False, "reply": "", "detail": f"HTTP {resp.status_code}: {snippet}"}
        data = resp.json()
        reply = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        reply = (reply or "").strip()
        if not reply:
            fr = (data.get("choices") or [{}])[0].get("finish_reason")
            hint = " Model hit max_tokens while reasoning (reasoning model)." if fr == "length" else ""
            return {"ok": False, "reply": "", "detail": f"HTTP 200 but empty message content.{hint}"}
        return {"ok": True, "reply": reply, "detail": None}
    except Exception as e:
        return {"ok": False, "reply": "", "detail": str(e)}


def _probe_pipeline_openai(api_key, model_id, base_url):
    if not api_key:
        return {"ok": False, "reply": "", "detail": "No OpenAI API key configured."}
    mid = (model_id or "").strip()
    if not mid:
        return {
            "ok": False,
            "reply": "",
            "detail": "No model configured for this step. Set it in Settings → Memory Pipeline.",
        }
    ep = (base_url or "https://api.openai.com/v1").strip().rstrip("/")
    if not ep.endswith("/v1"):
        ep = ep + "/v1"
    try:
        resp = requests.post(
            ep + "/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            json={
                "model": mid,
                "messages": [{"role": "user", "content": "Reply with exactly one short greeting word, nothing else."}],
                "max_tokens": 256,
            },
            timeout=20,
        )
        data = resp.json()
        if resp.status_code != 200:
            snippet = (resp.text or "")[:400].replace("\n", " ")
            return {"ok": False, "reply": "", "detail": f"HTTP {resp.status_code}: {snippet}"}
        if data.get("error"):
            return {"ok": False, "reply": "", "detail": data["error"].get("message", "OpenAI error")}
        reply = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        reply = (reply or "").strip()
        if not reply:
            fr = (data.get("choices") or [{}])[0].get("finish_reason")
            hint = " Model hit max_tokens while reasoning (reasoning model)." if fr == "length" else ""
            return {"ok": False, "reply": "", "detail": f"HTTP 200 but empty message content.{hint}"}
        return {"ok": True, "reply": reply, "detail": None}
    except Exception as e:
        return {"ok": False, "reply": "", "detail": str(e)}


def _probe_pipeline_openrouter(api_key, model_id):
    if not api_key:
        return {"ok": False, "reply": "", "detail": "No OpenRouter API key configured."}
    mid = (model_id or "").strip()
    if not mid:
        return {
            "ok": False,
            "reply": "",
            "detail": "No model configured for this step. Set it in Settings → Memory Pipeline.",
        }
    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "http://localhost:3000",
                "X-Title": "Love Refactored",
            },
            json={
                "model": mid,
                "messages": [{"role": "user", "content": "Reply with exactly one short greeting word, nothing else."}],
                "max_tokens": 256,
            },
            timeout=20,
        )
        data = resp.json()
        if resp.status_code != 200:
            snippet = (resp.text or "")[:400].replace("\n", " ")
            return {"ok": False, "reply": "", "detail": f"HTTP {resp.status_code}: {snippet}"}
        if data.get("error"):
            return {"ok": False, "reply": "", "detail": data["error"].get("message", "OpenRouter error")}
        reply = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        reply = (reply or "").strip()
        if not reply:
            fr = (data.get("choices") or [{}])[0].get("finish_reason")
            hint = " Model hit max_tokens while reasoning (reasoning model)." if fr == "length" else ""
            return {"ok": False, "reply": "", "detail": f"HTTP 200 but empty message content.{hint}"}
        return {"ok": True, "reply": reply, "detail": None}
    except Exception as e:
        return {"ok": False, "reply": "", "detail": str(e)}


@app.route('/test-llm-pipeline', methods=['POST'])
def test_llm_pipeline():
    """
    Run deduped connectivity checks for the memory pipeline (local + anthropic + openrouter).
    Request body matches PUT /config shape from the UI: provider, local, anthropic, openrouter, steps (hybrid).
    """
    data = request.json or {}
    saved = _load_pipeline_config()
    provider = data.get("provider") or saved.get("provider") or _default_pipeline_provider()
    merged_local = {**(saved.get("local") or {}), **(data.get("local") or {})}
    merged_ant = {**(saved.get("anthropic") or {}), **(data.get("anthropic") or {})}
    merged_or = {**(saved.get("openrouter") or {}), **(data.get("openrouter") or {})}
    merged_oai = {**(saved.get("openai") or {}), **(data.get("openai") or {})}
    req_key = (data.get("anthropic") or {}).get("api_key")
    if req_key and not str(req_key).startswith("sk-ant-...") and "••••" not in str(req_key):
        effective_key = req_key
    else:
        effective_key = (merged_ant.get("api_key") or ANTHROPIC_API_KEY or "").strip()
    req_or_key = (data.get("openrouter") or {}).get("api_key")
    if req_or_key and not str(req_or_key).startswith("sk-or-...") and "••••" not in str(req_or_key):
        effective_or_key = req_or_key
    else:
        effective_or_key = effective_openrouter_key({**saved, "openrouter": merged_or})
    req_oai_key = (data.get("openai") or {}).get("api_key")
    if req_oai_key and not str(req_oai_key).startswith("sk-...") and "••••" not in str(req_oai_key):
        effective_oai_key = req_oai_key
    else:
        effective_oai_key = effective_openai_key({**saved, "openai": merged_oai})
    oai_base = openai_base_url({**saved, "openai": merged_oai})
    steps_cfg = {**(saved.get("steps") or {}), **(data.get("steps") or {})}
    merged_cfg = {
        **saved,
        "provider": provider,
        "local": merged_local,
        "anthropic": merged_ant,
        "openai": merged_oai,
        "openrouter": merged_or,
        "steps": steps_cfg,
    }
    step_order = ["summarizer", "extractor", "updater", "reflection"]

    raw = []
    if provider == "local":
        ep = merged_local.get("endpoint") or "http://localhost:1234/v1"
        mo = (merged_local.get("model") or "").strip()
        raw.append({"kind": "local", "endpoint": ep, "model": mo, "steps": list(step_order)})
    elif provider == "anthropic":
        for step in step_order:
            mid = _model_for_backend_step(step, "anthropic", merged_cfg)
            raw.append({"kind": "anthropic", "model": mid, "steps": [step]})
    elif provider == "openrouter":
        for step in step_order:
            mid = _model_for_backend_step(step, "openrouter", merged_cfg)
            raw.append({"kind": "openrouter", "model": mid, "steps": [step]})
    elif provider == "openai":
        for step in step_order:
            mid = _model_for_backend_step(step, "openai", merged_cfg)
            raw.append({"kind": "openai", "model": mid, "endpoint": oai_base, "steps": [step]})
    elif provider == "hybrid":
        for step in step_order:
            st_prov = (steps_cfg.get(step) or "anthropic").lower()
            if st_prov == "local":
                ep = merged_local.get("endpoint") or "http://localhost:1234/v1"
                mo = (merged_local.get("model") or "").strip()
                raw.append({"kind": "local", "endpoint": ep, "model": mo, "steps": [step]})
            elif st_prov == "openrouter":
                mid = _model_for_backend_step(step, "openrouter", merged_cfg)
                raw.append({"kind": "openrouter", "model": mid, "steps": [step]})
            elif st_prov == "openai":
                mid = _model_for_backend_step(step, "openai", merged_cfg)
                raw.append({"kind": "openai", "model": mid, "endpoint": oai_base, "steps": [step]})
            else:
                mid = _model_for_backend_step(step, "anthropic", merged_cfg)
                raw.append({"kind": "anthropic", "model": mid, "steps": [step]})
    else:
        return jsonify({"success": False, "message": f"Unknown pipeline provider: {provider}", "results": []}), 400

    # Dedupe probes (same target + steps merged)
    dedup = {}
    for p in raw:
        if p["kind"] == "local":
            key = ("local", p["endpoint"], p["model"])
        elif p["kind"] == "openrouter":
            key = ("openrouter", p["model"])
        elif p["kind"] == "openai":
            key = ("openai", p.get("endpoint", oai_base), p["model"])
        else:
            key = ("anthropic", p["model"])
        if key not in dedup:
            dedup[key] = {**p, "steps": list(p["steps"])}
        else:
            for s in p["steps"]:
                if s not in dedup[key]["steps"]:
                    dedup[key]["steps"].append(s)

    results = []
    all_ok = True
    for _, spec in dedup.items():
        if spec["kind"] == "local":
            pr = _probe_pipeline_local(spec["endpoint"], spec["model"])
            mo_display = spec["model"] or "(server default / loaded model)"
            tech = f"Local · {spec['endpoint']} · model {mo_display}"
            if pr["ok"]:
                friendly = f"Your rig answered: “{pr['reply'][:120]}{'…' if len(pr['reply']) > 120 else ''}”"
                detail = tech
            else:
                all_ok = False
                if "empty" in (pr["detail"] or "").lower() or "empty message" in (pr["detail"] or "").lower():
                    friendly = "Your local model stared into space…"
                else:
                    friendly = "Local LLM didn’t pick up."
                detail = f"{tech}\n{pr['detail']}"
            results.append(
                {
                    "ok": pr["ok"],
                    "friendly": friendly,
                    "detail": detail,
                    "steps_for": spec["steps"],
                    "reply": pr["reply"] or None,
                }
            )
        elif spec["kind"] == "anthropic":
            pr = _probe_pipeline_anthropic(effective_key, spec["model"])
            nick = _anthropic_display_name(spec["model"])
            mid = spec["model"]
            tech = f"Anthropic · {mid}"
            if pr["ok"]:
                friendly = f"{nick} says: “{pr['reply'][:120]}{'…' if len(pr['reply']) > 120 else ''}”"
                detail = tech
            else:
                all_ok = False
                if "empty" in (pr["detail"] or "").lower() or "content was empty" in (pr["detail"] or "").lower():
                    friendly = f"Oh no, {nick} wasn’t home."
                else:
                    friendly = f"{nick} didn’t pick up."
                detail = f"{tech}\n{pr['detail']}"
            results.append(
                {
                    "ok": pr["ok"],
                    "friendly": friendly,
                    "detail": detail,
                    "steps_for": spec["steps"],
                    "reply": pr["reply"] or None,
                }
            )
        elif spec["kind"] == "openai":
            pr = _probe_pipeline_openai(effective_oai_key, spec["model"], spec.get("endpoint", oai_base))
            tech = f"OpenAI · {spec['model']}"
            if pr["ok"]:
                friendly = f"OpenAI says: “{pr['reply'][:120]}{'…' if len(pr['reply']) > 120 else ''}”"
                detail = tech
            else:
                all_ok = False
                friendly = "OpenAI didn’t pick up."
                detail = f"{tech}\n{pr['detail']}"
            results.append(
                {
                    "ok": pr["ok"],
                    "friendly": friendly,
                    "detail": detail,
                    "steps_for": spec["steps"],
                    "reply": pr["reply"] or None,
                }
            )
        else:
            pr = _probe_pipeline_openrouter(effective_or_key, spec["model"])
            tech = f"OpenRouter · {spec['model']}"
            if pr["ok"]:
                friendly = f"OpenRouter says: “{pr['reply'][:120]}{'…' if len(pr['reply']) > 120 else ''}”"
                detail = tech
            else:
                all_ok = False
                friendly = "OpenRouter didn’t pick up."
                detail = f"{tech}\n{pr['detail']}"
            results.append(
                {
                    "ok": pr["ok"],
                    "friendly": friendly,
                    "detail": detail,
                    "steps_for": spec["steps"],
                    "reply": pr["reply"] or None,
                }
            )

    return jsonify({"success": all_ok, "results": results})


@app.route('/config', methods=['GET'])
def get_config():
    """Return pipeline configuration."""
    config = _load_pipeline_config()
    # Mask the API key for display, indicate if env var is set
    env_key = os.environ.get('ANTHROPIC_API_KEY', '')
    stored_key = config.get('anthropic', {}).get('api_key', '')
    effective_key = stored_key or ANTHROPIC_API_KEY
    if effective_key:
        masked = effective_key[:7] + '...' + effective_key[-4:] if len(effective_key) > 12 else '••••'
        config.setdefault('anthropic', {})['api_key'] = masked
    or_stored_key = config.get('openrouter', {}).get('api_key', '')
    if or_stored_key:
        masked_or = or_stored_key[:6] + '...' + or_stored_key[-4:] if len(or_stored_key) > 12 else '••••'
        config.setdefault('openrouter', {})['api_key'] = masked_or
    oai_stored_key = config.get('openai', {}).get('api_key', '')
    if oai_stored_key:
        masked_oai = oai_stored_key[:6] + '...' + oai_stored_key[-4:] if len(oai_stored_key) > 12 else '••••'
        config.setdefault('openai', {})['api_key'] = masked_oai
    config['_using_env_key'] = bool(env_key) and not stored_key
    config['_env_key_set'] = bool(env_key)
    return jsonify(config)

@app.route('/config', methods=['PUT'])
def put_config():
    """Update pipeline configuration."""
    data = request.json or {}
    # Load existing config so we don't wipe fields not sent
    config = _load_pipeline_config()
    # Update provider
    if 'provider' in data:
        config['provider'] = data['provider']
    # Update local settings
    if 'local' in data:
        loc = data['local']
        config.setdefault('local', {})
        if 'endpoint' in loc and (loc.get('endpoint') or '').strip():
            config['local']['endpoint'] = loc['endpoint']
        if 'model' in loc:
            config['local']['model'] = loc.get('model') or ''
    # Update anthropic settings
    if 'anthropic' in data:
        ant = data['anthropic']
        config.setdefault('anthropic', {})
        # Only update key if a real new key was sent (not masked placeholder)
        if ant.get('api_key') and not ant['api_key'].startswith('sk-ant-...') and '••••' not in ant['api_key']:
            config['anthropic']['api_key'] = ant['api_key']
        for field in ['summarizer_model', 'extractor_model', 'updater_model', 'reflection_model', 'brief_model']:
            if field in ant:
                config['anthropic'][field] = ant[field] or ''
    if 'openrouter' in data:
        o = data['openrouter']
        config.setdefault('openrouter', {})
        if o.get('api_key') and not o['api_key'].startswith('sk-or-...') and '••••' not in o['api_key']:
            config['openrouter']['api_key'] = o['api_key']
        for field in ['summarizer_model', 'extractor_model', 'updater_model', 'reflection_model', 'brief_model']:
            if field in o:
                config['openrouter'][field] = o[field] or ''
    if 'openai' in data:
        o = data['openai']
        config.setdefault('openai', {})
        if o.get('api_key') and not o['api_key'].startswith('sk-...') and '••••' not in o['api_key']:
            config['openai']['api_key'] = o['api_key']
        if o.get('url'):
            config['openai']['url'] = o['url']
        for field in ['summarizer_model', 'extractor_model', 'updater_model', 'reflection_model', 'brief_model']:
            if field in o:
                config['openai'][field] = o[field] or ''
    # Update hybrid steps (merge so non-hybrid saves don't drop step keys)
    if 'steps' in data:
        config.setdefault('steps', {}).update(data['steps'])
    # Save and apply
    _save_pipeline_config(config)
    _apply_pipeline_config(config)
    return jsonify({"success": True, "config": config})


@app.route('/memory-lens', methods=['GET'])
def get_memory_lens():
    """Return per-companion Memory Lens (relational context for pipeline prompts)."""
    companion = _effective_companion(request.args.get("companion"))
    if not companion:
        return jsonify({"error": _NO_COMPANION_MSG}), 400
    lens = load_memory_lens(companion)
    return jsonify({"companion": companion, "lens": lens})


@app.route('/memory-lens', methods=['PUT'])
def put_memory_lens():
    """Save per-companion Memory Lens."""
    data = request.json or {}
    companion = _effective_companion(data.get("companion"))
    if not companion:
        return jsonify({"error": _NO_COMPANION_MSG}), 400
    payload = data.get("lens") if isinstance(data.get("lens"), dict) else data
    lens = save_memory_lens(companion, payload)
    return jsonify({"success": True, "companion": companion, "lens": lens})


@app.route('/health', methods=['GET'])
def health():
    """Health check with stats."""
    companion = _effective_companion(request.args.get("companion"))
    if not companion:
        return jsonify({"error": _NO_COMPANION_MSG}), 400
    db = get_db(companion)
    stats = db.get_memory_stats()

    try:
        from reflection import get_reflection_health
        reflection_health = get_reflection_health(db)
    except Exception:
        reflection_health = {"healthy": "unknown", "error": "could not check"}

    return jsonify({
        "status": "running",
        "companion": companion,
        "stats": stats,
        "reflection": reflection_health,
    })


@app.route('/pipeline-status', methods=['GET'])
def pipeline_status():
    """Current per-companion pipeline lock status."""
    with _pipeline_lock:
        active = sorted(_pipeline_running)
    companion = _effective_companion(request.args.get("companion"))
    is_running = bool(companion and _pipeline_key(companion) in active)
    return jsonify({
        "status": "running",
        "active_companions": active,
        "active_count": len(active),
        "companion": companion,
        "companion_running": is_running,
    })


@app.route('/stats', methods=['GET'])
def stats():
    """Detailed memory stats."""
    companion = _effective_companion(request.args.get("companion"))
    if not companion:
        return jsonify({"error": _NO_COMPANION_MSG}), 400
    db = get_db(companion)
    stats = db.get_memory_stats()
    recent = db.get_recent_summaries(3)
    try:
        recent_reflections = db.get_recent_reflections(n=3)
        reflection_list = [
            {
                "id": r["id"],
                "horizon": r["horizon"],
                "significance": r.get("significance"),
                "themes": r.get("themes", []),
                "emotional_arc": r.get("emotional_arc", ""),
                "created_at": r.get("created_at", ""),
            }
            for r in recent_reflections
        ]
    except Exception:
        reflection_list = []
    return jsonify({
        "companion": companion,
        "memory_stats": stats,
        "recent_reflections": reflection_list,
        "recent_summaries": [
            {
                "id": s["id"],
                "date_start": s["date_start"],
                "date_end": s["date_end"],
                "message_count": s["message_count"],
                "emotional_arc": s["emotional_arc"],
                "topics": s["topics"]
            }
            for s in recent
        ]
    })



@app.route('/summaries', methods=['GET'])
def list_summaries():
    """Full list of conversation session summaries (Memory Dashboard → Summaries tab)."""
    companion = _effective_companion(request.args.get("companion"))
    if not companion:
        return jsonify({"error": _NO_COMPANION_MSG}), 400
    db = get_db(companion)
    summaries = db.get_all_summaries()
    return jsonify({
        "companion": companion,
        "count": len(summaries),
        "summaries": summaries,
    })


@app.route('/summary/<summary_id>', methods=['PUT'])
def update_summary(summary_id):
    """Update a specific summary's fields."""
    companion = _effective_companion(request.args.get("companion"))
    if not companion:
        return jsonify({"error": _NO_COMPANION_MSG}), 400
    db = get_db(companion)

    summary = db.get_summary(summary_id)
    if not summary:
        return jsonify({"error": f"Summary {summary_id} not found"}), 404

    data = request.json or {}
    updates = {}

    if "narrative" in data:
        updates["narrative"] = str(data["narrative"])
    if "emotional_arc" in data:
        updates["emotional_arc"] = str(data["emotional_arc"])
    if "topics" in data:
        topics = data["topics"]
        if not isinstance(topics, list):
            return jsonify({"error": "topics must be an array of strings"}), 400
        updates["topics"] = [str(t) for t in topics]
    if "key_moments" in data:
        moments = data["key_moments"]
        if not isinstance(moments, list):
            return jsonify({"error": "key_moments must be an array of strings"}), 400
        updates["key_moments"] = [str(m) for m in moments]

    if updates:
        db.update_summary(summary_id, updates)

    updated = db.get_summary(summary_id)
    return jsonify(updated)


@app.route('/reflections', methods=['GET'])
def list_reflections():
    """List temporal reflections, optionally filtered by horizon."""
    companion = _effective_companion(request.args.get("companion"))
    if not companion:
        return jsonify({"error": _NO_COMPANION_MSG}), 400
    horizon = request.args.get("horizon")  # optional filter: daily, weekly, monthly, quarterly, biannual, annual
    n = int(request.args.get("n", "50"))
    db = get_db(companion)
    reflections = db.get_recent_reflections(horizon=horizon, n=n)
    return jsonify({
        "companion": companion,
        "count": len(reflections),
        "reflections": reflections,
    })


@app.route('/reflection/<reflection_id>', methods=['PUT'])
def update_reflection(reflection_id):
    """Update a reflection's injected text fields."""
    companion = _effective_companion(request.args.get("companion"))
    if not companion:
        return jsonify({"error": _NO_COMPANION_MSG}), 400
    db = get_db(companion)

    reflection = db.get_reflection(reflection_id)
    if not reflection:
        return jsonify({"error": f"Reflection {reflection_id} not found"}), 404

    data = request.json or {}
    updates = {}

    if "emotional_arc" in data:
        updates["emotional_arc"] = str(data["emotional_arc"])
    if "relationship_arc" in data:
        updates["relationship_arc"] = str(data["relationship_arc"])
    if "self_narrative" in data:
        updates["self_narrative"] = str(data["self_narrative"])
    if "patterns" in data:
        patterns = data["patterns"]
        if not isinstance(patterns, list):
            return jsonify({"error": "patterns must be an array of strings"}), 400
        updates["patterns"] = [str(t) for t in patterns]
    if "connections" in data:
        connections = data["connections"]
        if not isinstance(connections, list):
            return jsonify({"error": "connections must be an array of strings"}), 400
        updates["connections"] = [str(c) for c in connections]

    if updates:
        db.update_reflection(reflection_id, updates)

    updated = db.get_reflection(reflection_id)
    return jsonify(updated)


@app.route('/living-narrative', methods=['GET'])
def get_living_narrative():
    """Get the companion's living narrative — their evolving self-understanding."""
    companion = _effective_companion(request.args.get("companion"))
    if not companion:
        return jsonify({"error": _NO_COMPANION_MSG}), 400
    from reflection import load_living_narrative
    narrative = load_living_narrative(companion.capitalize())
    return jsonify({
        "companion": companion,
        "narrative": narrative,
    })


@app.route('/living-narrative', methods=['PUT'])
def put_living_narrative():
    """Merge hand edits into the living narrative and lock it against auto-overwrite."""
    companion = _effective_companion(request.args.get("companion"))
    if not companion:
        return jsonify({"error": _NO_COMPANION_MSG}), 400
    from reflection import load_living_narrative, save_living_narrative
    data = request.json or {}
    narrative = load_living_narrative(companion.capitalize())
    allowed = ("self_narrative", "relationship_arc", "worldview", "current_chapter")
    for k in allowed:
        if k in data:
            narrative[k] = str(data[k] if data[k] is not None else "")
    narrative["locked"] = True
    saved = save_living_narrative(companion.capitalize(), narrative)
    return jsonify({
        "companion": companion,
        "narrative": saved,
    })


@app.route('/brief/regenerate', methods=['POST'])
def regenerate_brief():
    """Spawn brief_sidecar.py --force for this companion; do not wait for it to finish."""
    companion = _effective_companion(request.args.get("companion"))
    if not companion:
        return jsonify({"error": _NO_COMPANION_MSG}), 400
    started = bool(_fire_brief_sidecar(companion, force=True))
    return jsonify({"ok": True, "started": started})


@app.route('/reflection-injection', methods=['GET'])
def reflection_injection_endpoint():
    """Formatted reflection block + living narrative for prompt injection (?companion=<name>)."""
    companion = _effective_companion(request.args.get("companion"))
    if not companion:
        return jsonify({"error": _NO_COMPANION_MSG}), 400
    try:
        # fix 15 Aug 2026: was constructing a fresh MemoryDB (new embedder on
        # GPU, unclosed sqlite/chroma handles) on EVERY request — the GPU leak.
        # Route through the shared per-companion cache like every other endpoint.
        cdb = get_db(companion)
        hz = request.args.get("horizons")
        hz_list = [h.strip() for h in hz.split(",")] if hz else None
        block = get_reflection_injection(cdb, horizons=hz_list) or ""
    except Exception as e:
        print(f"  ⚠️ reflection-injection endpoint failed (non-fatal): {e}")
        block = ""
    return jsonify({"companion": companion, "injection": block})


@app.route('/recent-summaries', methods=['GET'])
def recent_summaries():
    """Recent session summaries WITH narrative text, for lightweight context injection.
    Lean by design: only id/date/narrative for the N most recent (max 20); never the full
    table or raw_conversation."""
    companion = _effective_companion(request.args.get("companion"))
    if not companion:
        return jsonify({"error": _NO_COMPANION_MSG}), 400
    try:
        n = int(request.args.get("n", 3))
    except (TypeError, ValueError):
        n = 3
    n = max(1, min(n, 20))
    db = get_db(companion)
    rows = db.db.execute(
        "SELECT id, date_start, date_end, narrative FROM summaries "
        "ORDER BY date_end DESC LIMIT ?", (n,)
    ).fetchall()
    recent = [dict(r) for r in rows]
    return jsonify({
        "companion": companion,
        "count": len(recent),
        "summaries": [
            {"id": s["id"], "date_start": s["date_start"],
             "date_end": s["date_end"], "narrative": s["narrative"]}
            for s in recent
        ],
    })


@app.route('/memories', methods=['GET'])
def list_memories():
    """List memories, optionally filtered by category."""
    companion = _effective_companion(request.args.get("companion"))
    if not companion:
        return jsonify({"error": _NO_COMPANION_MSG}), 400
    category = request.args.get("category")
    db = get_db(companion)
    memories = db.get_all_memories(category=category)
    return jsonify({
        "companion": companion,
        "count": len(memories),
        "memories": memories
    })


@app.route('/inject', methods=['GET'])
def inject_for_chat():
    """Memories for live chat injection — count-based, full content per memory; recalled pins rank first."""
    query = request.args.get("q", "")
    companion = _effective_companion(request.args.get("companion"))
    try:
        n = int(request.args.get("n", "10"))
    except ValueError:
        n = 10
    n = max(1, min(n, 100))

    if not companion:
        return jsonify({"error": _NO_COMPANION_MSG}), 400

    if not query:
        return jsonify({"error": "Provide a query with ?q=your+search (optional &companion=name&n=10)"}), 400

    db = get_db(companion)
    try:
        budget = int(request.args.get("budget", "0"))
    except ValueError:
        budget = 0
    results = db.get_chat_injection_memories(query, n=n, token_budget=budget if budget > 0 else None)


    return jsonify({
        "companion": companion,
        "query": query,
        "requested_count": n,
        "count": len(results),
        "results": results,
    })


@app.route('/search', methods=['GET'])
def search():
    """Search memories by query."""
    query = request.args.get("q", "")
    companion = _effective_companion(request.args.get("companion"))
    category = request.args.get("category")
    n = int(request.args.get("n", "10"))
    include_suppressed = request.args.get("include_suppressed", "false").lower() == "true"

    if not companion:
        return jsonify({"error": _NO_COMPANION_MSG}), 400

    if not query:
        return jsonify({"error": "Provide a query with ?q=your+search (optional &companion=name)"}), 400

    db = get_db(companion)
    results = db.search_memories(query, n=n, category=category, include_suppressed=include_suppressed)
    return jsonify({
        "companion": companion,
        "query": query,
        "count": len(results),
        "results": results
    })


@app.route('/memory/<memory_id>', methods=['PUT'])
def update_memory(memory_id):
    """Update a specific memory's fields."""
    companion = _effective_companion(request.args.get("companion"))
    if not companion:
        return jsonify({"error": _NO_COMPANION_MSG}), 400
    db = get_db(companion)

    # Check memory exists
    mem = db.get_memory_by_id(memory_id)
    if not mem:
        return jsonify({"error": f"Memory {memory_id} not found"}), 404

    data = request.json or {}
    updates = {}

    VALID_CATEGORIES = {"fact", "experience", "milestone", "preference", "relationship"}
    VALID_PRIORITIES = {"core", "important", "notable", "minor"}

    if "content" in data:
        updates["content"] = str(data["content"])
    if "category" in data:
        if data["category"] not in VALID_CATEGORIES:
            return jsonify({"error": f"Invalid category. Must be one of: {', '.join(VALID_CATEGORIES)}"}), 400
        updates["category"] = data["category"]
    if "priority" in data:
        if data["priority"] not in VALID_PRIORITIES:
            return jsonify({"error": f"Invalid priority. Must be one of: {', '.join(VALID_PRIORITIES)}"}), 400
        updates["priority"] = data["priority"]
    if "confidence" in data:
        updates["confidence"] = max(0, min(100, int(data["confidence"])))
    if "active" in data:
        updates["active"] = 1 if data["active"] else 0
        # Reactivating a dormant memory counts as reinforcement: bump last_seen
        # so the next decay run's staleness rules don't immediately re-cull it.
        if updates["active"] and not mem.get("active"):
            updates["last_seen"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if "pinned" in data:
        updates["pinned"] = 1 if data["pinned"] else 0
        if updates["pinned"]:
            updates["suppressed"] = 0
    if "suppressed" in data:
        updates["suppressed"] = 1 if data["suppressed"] else 0
        if updates["suppressed"]:
            updates["pinned"] = 0
    if "protected" in data:
        updates["protected"] = 1 if data["protected"] else 0
        # Sensitive implies protected, so dropping protection also drops sensitive
        # (unless sensitive is being set in the same request — handled below).
        if not updates["protected"]:
            updates["sensitive"] = 0
    if "sensitive" in data:
        updates["sensitive"] = 1 if data["sensitive"] else 0
        if updates["sensitive"]:
            updates["protected"] = 1

    if updates:
        db.update_memory(memory_id, updates, source="ui_edit")

    updated = db.get_memory_by_id(memory_id)
    return jsonify(updated)


def _backup_dir():
    from companion_resolve import _tanevan_data_root
    return os.path.join(str(_tanevan_data_root()), "backups")


def _discover_companions():
    from companion_resolve import _tanevan_data_root
    root = str(_tanevan_data_root())
    if not os.path.isdir(root):
        return []
    companions = []
    for entry in sorted(os.listdir(root)):
        if entry.startswith(".") or entry == "backups":
            continue
        entry_path = os.path.join(root, entry)
        if os.path.isdir(entry_path) and os.path.isfile(os.path.join(entry_path, "memories.db")):
            companions.append(entry)
    return companions


@app.route('/backup', methods=['POST'])
def backup_memories():
    """Hot-snapshot every companion's memories.db into a timestamped backup directory."""
    import sqlite3 as _sqlite3
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    name = f"tanevan-{stamp}"
    dest_dir = os.path.join(_backup_dir(), name)
    os.makedirs(dest_dir, exist_ok=True)

    companions = _discover_companions()
    from companion_resolve import _tanevan_data_root
    root = str(_tanevan_data_root())
    total_size = 0
    for comp in companions:
        src_path = os.path.join(root, comp, "memories.db")
        dst_path = os.path.join(dest_dir, f"{comp}.db")
        src_conn = _sqlite3.connect(src_path)
        dst_conn = _sqlite3.connect(dst_path)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()
        total_size += os.path.getsize(dst_path)

    print(f"💾 Memory backup: {name} ({len(companions)} companions, {total_size} bytes)")
    return jsonify({
        "success": True,
        "name": name,
        "path": dest_dir,
        "companions": len(companions),
        "size": total_size,
    })


@app.route('/backup/list', methods=['GET'])
def list_backups():
    """List all Tanevan memory backup directories, newest first."""
    backups_root = _backup_dir()
    if not os.path.isdir(backups_root):
        return jsonify({"backups": []})
    results = []
    for entry in os.listdir(backups_root):
        if not entry.startswith("tanevan-"):
            continue
        entry_path = os.path.join(backups_root, entry)
        if not os.path.isdir(entry_path):
            continue
        db_files = [f for f in os.listdir(entry_path) if f.endswith(".db")]
        size = sum(os.path.getsize(os.path.join(entry_path, f)) for f in db_files)
        mtime = os.path.getmtime(entry_path)
        results.append({
            "name": entry,
            "size": size,
            "companions": len(db_files),
            "mtime": datetime.fromtimestamp(mtime).isoformat(),
        })
    results.sort(key=lambda b: b["mtime"], reverse=True)
    return jsonify({"backups": results})


@app.route('/backup/prune', methods=['POST'])
def prune_backups():
    """Remove old backup directories beyond the retention count."""
    data = request.json or {}
    retention = max(1, int(data.get("retentionCount", 7)))
    backups_root = _backup_dir()
    if not os.path.isdir(backups_root):
        return jsonify({"pruned": 0})
    dirs = sorted(
        [e for e in os.listdir(backups_root)
         if e.startswith("tanevan-") and os.path.isdir(os.path.join(backups_root, e))],
        key=lambda e: os.path.getmtime(os.path.join(backups_root, e)),
        reverse=True,
    )
    pruned = 0
    for old in dirs[retention:]:
        shutil.rmtree(os.path.join(backups_root, old))
        pruned += 1
    return jsonify({"pruned": pruned})


def shutdown_handler(signum, frame):
    """Process all companion buffers on shutdown."""
    print("\n\n🛑 Shutting down...")
    for companion, db in list(_db_cache.items()):
        buffer_count = db.get_buffer_count()
        if buffer_count >= 4:
            print(f"   Processing {buffer_count} buffered messages for {companion}...")
            try:
                process_buffer(
                    db=db,
                    api_key=ANTHROPIC_API_KEY,
                    user_name=_user_name_for_companion(companion),
                )
            except Exception as e:
                print(f"   ✗ Failed to process buffer for {companion}: {e}")
        db.close()
    print("   ✓ Clean shutdown complete")
    sys.exit(0)


# Apply saved pipeline config on startup
try:
    _saved_config = _load_pipeline_config()
    _apply_pipeline_config(_saved_config)
    print(f"  ✓ Pipeline config loaded from {PIPELINE_CONFIG_FILE}")
except Exception as e:
    print(f"  ⚠️ Could not apply pipeline config: {e}")

if __name__ == '__main__':
    # Register shutdown handler
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    if not COMPANION_NAME:
        print(f"✗ {_NO_COMPANION_MSG}")
        sys.exit(1)

    default_db = get_db(COMPANION_NAME)
    threading.Thread(target=_audit_retention_loop, daemon=True).start()
    stats = default_db.get_memory_stats()

    print("\n" + "=" * 70)
    print("🧠 LOVE REFACTORED - Memory Proxy Server")
    print("=" * 70)
    print(f"Proxy:        http://localhost:{PROXY_PORT}")
    print(f"LM Studio:    {LM_STUDIO_URL}")
    print(f"Default companion: {COMPANION_NAME}")
    print(f"User:         {USER_NAME}")
    print(f"")
    print(f"Memory Dossier ({COMPANION_NAME}):")
    print(f"  Total memories:  {stats['total']}")
    print(f"  Categories:      {stats['by_category']}")
    print(f"  Summaries:       {stats['total_summaries']}")
    print(f"  Buffer:          {stats['buffer_messages']} messages pending")
    print(f"")
    print(f"Pipeline triggers:")
    print(f"  Auto:     every {SUMMARIZE_EVERY} messages")
    print(f"  Manual:   POST http://localhost:{PROXY_PORT}/flush  (body: {{\"companion\": \"<name>\"}})")
    print(f"  Shutdown: Ctrl+C (processes all companion buffers before exit)")
    print(f"")
    print(f"Endpoints (add ?companion=name or body companion field):")
    print(f"  GET  /health     - Status check")
    print(f"  GET  /stats      - Detailed memory stats")
    print(f"  GET  /summaries  - All session summaries (?companion=<name>)")
    print(f"  GET  /reflections - Temporal reflections (?horizon=weekly&companion=<name>)")
    print(f"  GET  /living-narrative - Companion's living narrative (?companion=<name>)")
    print(f"  GET  /memory-lens - Memory Lens config (?companion=<name>)")
    print(f"  PUT  /memory-lens - Save Memory Lens (body: companion, lens)")
    print(f"  GET  /memories   - List all memories (?category=fact&companion=<name>)")
    print(f"  GET  /inject      - Chat injection memories (?q=query&n=10&companion=<name>)")
    print(f"  GET  /search      - Search memories (?q=query&companion=<name>)")
    print(f"  GET  /audit       - Audit log (?companion=<name>&event_type=&limit=50&before_id=)")
    print(f"  POST /buffer     - Buffer a message (body: {{role, content, companion}})")
    print(f"  POST /flush      - Trigger pipeline (body: {{companion}})")
    print(f"  DELETE /companion-data - Wipe companion memory data (body: {{companion}})")
    print(f"  POST /reflect    - Run temporal reflections (body: {{companion, horizon?, dry_run?}})")
    print(f"  POST /backup     - Snapshot all companion memory DBs")
    print(f"  GET  /backup/list - List memory backups")
    print(f"  POST /backup/prune - Prune old memory backups (body: {{retentionCount?}})")
    print(f"  GET  /recent-summaries - Last N summary narratives (query: companion, n)")
    _brief_path_ok = os.path.isfile(_BRIEF_SIDECAR_PATH)
    if brief_is_configured() and _brief_path_ok:
        print(f"")
        print(f"📋 Brief trigger ARMED — summary_complete regenerates the life brief")
        print(f"     Sidecar script: {_BRIEF_SIDECAR_PATH}")
    else:
        print(f"")
        print(f"⚠⚠⚠ BRIEF TRIGGER DISABLED ⚠⚠⚠")
        if not brief_is_configured():
            print(f"     Life brief LLM is not configured.")
            print(f"     Set Brief model in Settings → Memory Pipeline, or set BRIEF_MODEL_KEY.")
        if not _brief_path_ok:
            print(f"     Sidecar script missing at: {_BRIEF_SIDECAR_PATH}")
        print(f"     Pipeline summary_complete events will NOT regenerate briefs.")
    print("=" * 70 + "\n")

    app.run(host=PROXY_HOST, port=PROXY_PORT, debug=False)
