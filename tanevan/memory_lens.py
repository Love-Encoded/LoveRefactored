# © 2024-2026 Megan Neves. All rights reserved.
# Tanevan Memory System — https://github.com/Love-Encoded/Love-Refactored
# Licensed under the Love Refactored / Tanevan License. See LICENSE.md.
"""
Per-companion Memory Lens — relational context injected into Tanevan pipeline prompts.
Stored at ~/tanevan-data/{companion}/memory_lens.json (not live chat).
"""

import json
import os
from pathlib import Path

from companion_resolve import format_companion_voice_reference

MEMORY_LENS_FIELDS = (
    "relational_brief",
    "companion_user_backstory",
    "whos_who",
    "special_considerations",
    "speech_style",
)

MEMORY_LENS_LABELS = {
    "relational_brief": "Relational brief / history",
    "companion_user_backstory": "Companion & user backstories",
    "whos_who": "Who's who",
    "special_considerations": "Special considerations (weight heavily)",
    "speech_style": "Speech style (for summaries and memories)",
}

DEFAULT_MEMORY_LENS = {field: "" for field in MEMORY_LENS_FIELDS}

BASE_DATA_DIR = os.environ.get("TANEVAN_DATA_DIR", os.path.expanduser("~/tanevan-data"))


def memory_lens_path(companion_name):
    key = (companion_name or "").strip().lower()
    if not key:
        raise ValueError("companion name required")
    return os.path.join(BASE_DATA_DIR, key, "memory_lens.json")


def _normalize_lens_data(data):
    if not isinstance(data, dict):
        data = {}
    out = dict(DEFAULT_MEMORY_LENS)
    for field in MEMORY_LENS_FIELDS:
        raw = data.get(field, "")
        out[field] = str(raw).strip() if raw is not None else ""
    return out


def load_memory_lens(companion_name):
    """Return lens dict (all keys present, strings)."""
    try:
        path = memory_lens_path(companion_name)
    except ValueError:
        return dict(DEFAULT_MEMORY_LENS)
    if not os.path.isfile(path):
        return dict(DEFAULT_MEMORY_LENS)
    try:
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
        return _normalize_lens_data(saved)
    except (json.JSONDecodeError, OSError) as e:
        print(f"⚠️ memory_lens: could not load {path}: {e}")
        return dict(DEFAULT_MEMORY_LENS)


def save_memory_lens(companion_name, data):
    """Persist lens; returns normalized dict."""
    path = memory_lens_path(companion_name)
    normalized = _normalize_lens_data(data)
    Path(os.path.dirname(path)).mkdir(parents=True, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(normalized, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)
    return normalized


def format_memory_lens_block(companion_name):
    """
    Build prompt prefix for summarizer / extractor / updater.
    Returns empty string when no section has content.
    """
    lens = load_memory_lens(companion_name)
    sections = []
    for field in MEMORY_LENS_FIELDS:
        text = (lens.get(field) or "").strip()
        if text:
            sections.append(f"{MEMORY_LENS_LABELS[field]}:\n{text}")

    if not sections:
        return ""

    intro = (
        "=== MEMORY LENS — Read before summarizing, extracting, or updating memories. ===\n"
        "This is relational and interpretive context for this companion and their person. "
        "Use it as your lens for every judgment call in this task.\n"
        "- SPECIAL CONSIDERATIONS: weight heavily when interpreting identity, relationship dynamics, "
        "minority contexts, and anything that deviates from mainstream assumptions.\n"
        "- SPEECH STYLE: all summaries, narratives, and memory text must match this dialect, slang, "
        "spelling, and verbal register — not generic assistant English. This overrides generic phrasing.\n"
        "=== END MEMORY LENS INTRO ===\n\n"
    )
    body = "\n\n".join(sections)
    return intro + body + "\n\n=== END MEMORY LENS ===\n\n"


def prepend_pipeline_context(system_prompt, companion_name):
    """Prepend Memory Lens + character-card voice reference when available."""
    blocks = []
    lens = format_memory_lens_block(companion_name)
    voice = format_companion_voice_reference(companion_name)
    if lens:
        blocks.append(lens)
    if voice:
        blocks.append(voice)
    if not blocks:
        return system_prompt
    return "".join(blocks) + system_prompt


def prepend_memory_lens(system_prompt, companion_name):
    """Alias for prepend_pipeline_context."""
    return prepend_pipeline_context(system_prompt, companion_name)
