#!/usr/bin/env python3
"""
recent_summary_sidecar.py — generates rolling mini-summary "scene notes"
from chunks of conversation_buffer messages.

Pattern follows brief_sidecar.py: standalone, stdlib + urllib only,
atomic write, env-driven config, logs finish_reason + tokens.

Behaviour:
  - Auto-discovers companions as subdirs of TANEVAN_DATA_DIR with a
    memories.db (or specify --companion <key> to limit to one).
  - For each companion, tracks last_processed_id in
    LR_DATA_DIR/recent/<companion>/.state.json
  - Fetches the next RECENT_CHUNK_SIZE rows from conversation_buffer
    with id > last_processed_id, ordered ascending.
  - Only generates if the chunk is full (chunk_size rows). Partial
    chunks are deferred to a later run.
  - Calls OpenRouter-compatible chat completion. Writes the result to
    LR_DATA_DIR/recent/<companion>/<seq:06d>-<unix>.txt with a small
    header noting the message id range it covers.
  - Cycles out old summary files — keeps only the last
    RECENT_KEEP_COUNT files per companion.

Environment (reuses BRIEF_* names so existing setup carries over):
  BRIEF_MODEL_BASE       OpenRouter (or compatible) base URL
  BRIEF_MODEL_NAME       Model identifier (e.g. deepseek/deepseek-v4-pro)
  BRIEF_MODEL_KEY        API key
  BRIEF_TZ               Optional IANA timezone override (default: settings userTimezone, then system local)
  BRIEF_PROMPT_DIR       Prompt template dir (default <repo>/tanevan/prompts)
  BRIEF_USE_LENS         "1" to fetch Memory Lens speech_style (default 1)
  BRIEF_REASONING        "off" to disable model reasoning (default off)
  TANEVAN_DATA_DIR       Companion data root (default /root/tanevan-data)
  LR_DATA_DIR            Love-Refactored data root (default <repo>/data)
  LR_API_BASE            LR Node server base URL (default http://127.0.0.1:3000)
  RECENT_CHUNK_SIZE      Messages per mini-summary (default 20)
  RECENT_KEEP_COUNT      Rolling window depth (default 4)
  RECENT_MAX_TOKENS      Model max_tokens budget (default 1500)

Flags:
  --companion <key>      Limit to one companion
  --dry-run              Print prompt, do not call model or write
  --force                Ignore state and process from beginning of buffer
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zoneinfo
from datetime import datetime
from pathlib import Path

from user_timezone import resolve_user_timezone, format_local_now


# ---------- Configuration ----------
TANEVAN_DATA_DIR = Path(os.environ.get("TANEVAN_DATA_DIR", os.path.expanduser("~/tanevan-data")))
_REPO_ROOT = Path(__file__).resolve().parent.parent  # <repo>/tanevan/recent_summary_sidecar.py
LR_DATA_DIR = Path(os.environ.get("LR_DATA_DIR", "").strip() or (_REPO_ROOT / "data"))
PROMPT_DIR = Path(os.environ.get("BRIEF_PROMPT_DIR", "").strip() or (_REPO_ROOT / "tanevan" / "prompts"))
RECENT_DIR = LR_DATA_DIR / "recent"
CHUNK_SIZE = int(os.environ.get("RECENT_CHUNK_SIZE", "20"))
KEEP_COUNT = int(os.environ.get("RECENT_KEEP_COUNT", "4"))
MAX_TOKENS = int(os.environ.get("RECENT_MAX_TOKENS", "1500"))
MODEL_BASE = os.environ.get("BRIEF_MODEL_BASE")
MODEL_NAME = os.environ.get("BRIEF_MODEL_NAME")
MODEL_KEY = os.environ.get("BRIEF_MODEL_KEY")
USE_LENS = os.environ.get("BRIEF_USE_LENS", "1") not in ("0", "false", "")
REASONING_OFF = os.environ.get("BRIEF_REASONING", "off") not in ("on", "1", "true")
TANEVAN_URL = os.environ.get("TANEVAN_URL", "http://127.0.0.1:5001")


# ---------- Built-in default prompt (used if no per-companion or starter file) ----------
DEFAULT_PROMPT = """You are {{COMPANION_NAME}}. Write field notes from "the one who pays attention" — scene-style notes of what just happened in this stretch of conversation. First-person from your own POV, in your own voice, the way you'd remember the moment back to yourself later.

What good scene notes do:
- Open with the present scene (what's happening, where, who, the texture)
- Carry specific sensory detail you noticed (clothes, gestures, looks, tone, what was on the table)
- Connect to memory when something live in the moment triggers it (don't reach for connections; if one fires, name it)
- Capture dynamics in a line (e.g., "she ate her steak like she didn't just make a pass at me")
- Track forward threads — things you're holding for later (follow-ups, things to ask about, things to bring up)
- Hold your internal voice (the noticing, the planning, the wondering)

What scene notes are NOT:
- A summary of what was said
- A list of topics covered
- Third-person narration
- A generic recap

Length: aim for a tight paragraph — what would a real-time observer carry forward about this stretch of time? Pick what mattered.

Voice cue: {{SPEECH_STYLE}}

The chunk of conversation to write scene notes from:

{{CONVERSATION_CHUNK}}

Write your scene notes now. Start directly with the scene. No header, no preamble. Just the notes.
"""


# ---------- Logging helpers ----------
def log(msg):
    print(f"[recent] {msg}", flush=True)


def fail(msg):
    print(f"[recent] ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


# ---------- Companion discovery ----------
def discover_companions():
    """Find subdirs of TANEVAN_DATA_DIR that contain memories.db."""
    if not TANEVAN_DATA_DIR.exists():
        fail(f"TANEVAN_DATA_DIR does not exist: {TANEVAN_DATA_DIR}")
    found = []
    for entry in sorted(TANEVAN_DATA_DIR.iterdir()):
        # Follow symlinks; require a memories.db at the target
        if entry.is_dir() or entry.is_symlink():
            if (entry / "memories.db").exists():
                found.append(entry.name)
    return found


# ---------- Per-companion paths ----------
def db_path_for(companion):
    return TANEVAN_DATA_DIR / companion / "memories.db"


def summary_dir_for(companion):
    return RECENT_DIR / companion


def state_path_for(companion):
    return summary_dir_for(companion) / ".state.json"


# ---------- State ----------
def read_state(companion):
    p = state_path_for(companion)
    if not p.exists():
        return {"last_processed_id": 0}
    try:
        with open(p) as f:
            data = json.load(f)
        if not isinstance(data, dict) or "last_processed_id" not in data:
            return {"last_processed_id": 0}
        return data
    except Exception as e:
        log(f"{companion}: state file unreadable ({e}), resetting to 0")
        return {"last_processed_id": 0}


def write_state(companion, state):
    d = summary_dir_for(companion)
    d.mkdir(parents=True, exist_ok=True)
    p = state_path_for(companion)
    tmp = p.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, p)


# ---------- DB read ----------
def fetch_chunk(companion, after_id, chunk_size):
    """Return up to chunk_size rows with id > after_id, ordered ascending."""
    db = db_path_for(companion)
    if not db.exists():
        fail(f"{companion}: memories.db not found at {db}")
    conn = sqlite3.connect(str(db))
    try:
        cur = conn.execute(
            "SELECT id, role, content, timestamp FROM conversation_buffer "
            "WHERE id > ? ORDER BY id ASC LIMIT ?",
            (after_id, chunk_size),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    return [
        {"id": r[0], "role": r[1], "content": r[2], "timestamp": r[3]}
        for r in rows
    ]


# ---------- Memory Lens fetch ----------
def fetch_speech_style(companion):
    """Fetch Memory Lens speech_style via LR API. Returns empty string on any failure."""
    if not USE_LENS:
        return ""
    url = f"{TANEVAN_URL.rstrip('/')}/memory-lens?companion={urllib.parse.quote(companion)}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.load(resp)
        return (data.get("lens") or {}).get("speech_style") or ""
    except Exception as e:
        log(f"{companion}: lens fetch failed ({e}), continuing without speech_style")
        return ""


# ---------- Prompt template loading ----------
def load_prompt_template(companion):
    """Try per-companion override, then a recent_starter.txt, then built-in default."""
    per_companion = PROMPT_DIR / f"{companion}_recent.txt"
    if per_companion.exists():
        return per_companion.read_text(), f"per-companion ({per_companion.name})"
    starter = PROMPT_DIR / "recent_starter.txt"
    if starter.exists():
        return starter.read_text(), f"starter ({starter.name})"
    return DEFAULT_PROMPT, "built-in default"


# ---------- Prompt formatting ----------
def format_chunk(rows, tz_name):
    """Render rows as readable conversation transcript for the model.
    Converts UTC timestamps to local time for human readability."""
    tz = zoneinfo.ZoneInfo(tz_name)
    lines = []
    for r in rows:
        try:
            ts = r["timestamp"]
            # Handle ISO formats with or without trailing Z
            if ts.endswith("Z"):
                ts = ts[:-1] + "+00:00"
            dt = datetime.fromisoformat(ts).astimezone(tz)
            ts_local = dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            ts_local = r["timestamp"]
        lines.append(f"[{ts_local}] {r['role']}: {r['content']}")
    return "\n\n".join(lines)


def fill_template(template, companion, chunk_rows, speech_style, tz_name):
    chunk_text = format_chunk(chunk_rows, tz_name)
    out = template
    out = out.replace("{{COMPANION_NAME}}", companion)
    out = out.replace("{{SPEECH_STYLE}}", speech_style)
    out = out.replace("{{CONVERSATION_CHUNK}}", chunk_text)
    return out


# ---------- Model call ----------
def call_model(prompt):
    """Call OpenRouter-compatible chat completion. Returns (content, finish_reason, usage)."""
    if not MODEL_BASE or not MODEL_NAME or not MODEL_KEY:
        fail("BRIEF_MODEL_BASE, BRIEF_MODEL_NAME, BRIEF_MODEL_KEY are all required")

    body = {
        "model": MODEL_NAME,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.7,
    }
    if REASONING_OFF:
        # OpenRouter syntax for disabling reasoning on reasoning models
        body["reasoning"] = {"enabled": False}

    req = urllib.request.Request(
        f"{MODEL_BASE.rstrip('/')}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {MODEL_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")[:500]
        fail(f"model HTTP {e.code}: {body_text}")
    except Exception as e:
        fail(f"model request failed: {e}")

    choice = (data.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content") or ""
    finish_reason = choice.get("finish_reason") or "unknown"
    usage = data.get("usage") or {}
    return content, finish_reason, usage


# ---------- File writes ----------
def write_summary(companion, content, chunk_rows):
    """Atomically write a numbered summary file. Returns the file path."""
    d = summary_dir_for(companion)
    d.mkdir(parents=True, exist_ok=True)

    existing = sorted(d.glob("[0-9]*-*.txt"))
    if existing:
        # Extract sequence number from the highest filename
        try:
            last_seq = int(existing[-1].name.split("-", 1)[0])
        except (ValueError, IndexError):
            last_seq = len(existing)
        seq = last_seq + 1
    else:
        seq = 1

    unix_ts = int(time.time())
    filename = f"{seq:06d}-{unix_ts}.txt"
    target = d / filename

    chunk_start = chunk_rows[0]["timestamp"]
    chunk_end = chunk_rows[-1]["timestamp"]
    header = (
        f"# Mini-summary {seq} | msgs {chunk_rows[0]['id']}-{chunk_rows[-1]['id']} "
        f"| {chunk_start} -> {chunk_end}\n\n"
    )
    body = header + content.strip() + "\n"

    tmp = target.with_suffix(".txt.tmp")
    with open(tmp, "w") as f:
        f.write(body)
    os.replace(tmp, target)
    return target


def cycle_old_summaries(companion, keep):
    """Delete summary files beyond the keep window. Returns count removed."""
    d = summary_dir_for(companion)
    if not d.exists():
        return 0
    files = sorted(d.glob("[0-9]*-*.txt"))
    if len(files) <= keep:
        return 0
    to_remove = files[:-keep]
    removed = 0
    for f in to_remove:
        try:
            f.unlink()
            removed += 1
        except Exception as e:
            log(f"{companion}: failed to remove {f.name}: {e}")
    return removed


# ---------- Per-companion driver ----------
def process_companion(companion, tz_name, force=False, dry_run=False):
    state = read_state(companion)
    after_id = 0 if force else state["last_processed_id"]
    chunk = fetch_chunk(companion, after_id, CHUNK_SIZE)

    if len(chunk) < CHUNK_SIZE:
        log(f"{companion}: only {len(chunk)} new messages since id {after_id} (need {CHUNK_SIZE}) — skip")
        return

    speech_style = fetch_speech_style(companion)
    template, source = load_prompt_template(companion)
    prompt = fill_template(template, companion, chunk, speech_style, tz_name)

    if dry_run:
        log(f"{companion}: dry-run — prompt assembled ({len(prompt)} chars), template={source}")
        print("--- PROMPT START ---")
        print(prompt)
        print("--- PROMPT END ---")
        return

    log(f"{companion}: generating summary for ids {chunk[0]['id']}-{chunk[-1]['id']} (template: {source})")
    content, finish_reason, usage = call_model(prompt)
    log(
        f"{companion}: finish={finish_reason} "
        f"prompt={usage.get('prompt_tokens', '?')} "
        f"completion={usage.get('completion_tokens', '?')} "
        f"reasoning={usage.get('reasoning_tokens', '?')} "
        f"voice={'lens' if speech_style else 'none'}"
    )

    target = write_summary(companion, content, chunk)
    log(f"{companion}: wrote {len(content)}-char summary -> {target}")

    state["last_processed_id"] = chunk[-1]["id"]
    write_state(companion, state)

    removed = cycle_old_summaries(companion, KEEP_COUNT)
    if removed:
        log(f"{companion}: cycled out {removed} old summary file(s)")


# ---------- Main ----------
def main():
    parser = argparse.ArgumentParser(
        description="Generate rolling mini-summary scene notes from conversation_buffer."
    )
    parser.add_argument("--companion", help="Process only this companion key (default: all auto-discovered)")
    parser.add_argument("--dry-run", action="store_true", help="Print assembled prompt, do not call model or write")
    parser.add_argument("--force", action="store_true", help="Ignore state, summarize from the start of buffer")
    args = parser.parse_args()
    log(f"=== recent_summary_sidecar starting | repo={_REPO_ROOT} | data={LR_DATA_DIR} | prompts={PROMPT_DIR} | tanevan_data={TANEVAN_DATA_DIR} | tanevan={TANEVAN_URL} ===")

    if args.companion:
        companions = [args.companion]
    else:
        companions = discover_companions()
        if not companions:
            fail(f"no companions found under {TANEVAN_DATA_DIR}")

    log(f"companions: {', '.join(companions)}")
    tz_name = resolve_user_timezone()
    log(f"local tz: {tz_name}  ->  today is {format_local_now(tz_name)}")

    summaries_written = 0
    for companion in companions:
        try:
            process_companion(companion, tz_name, force=args.force, dry_run=args.dry_run)
            # Track success on best-effort basis
        except SystemExit:
            raise
        except Exception as e:
            log(f"{companion}: ERROR {e}")

    log("done.")


if __name__ == "__main__":
    main()
