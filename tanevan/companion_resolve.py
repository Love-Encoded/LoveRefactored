# © 2024-2026 Megan Neves. All rights reserved.
# Tanevan Memory System — https://github.com/Love-Encoded/Love-Refactored
# Licensed under the Love Refactored / Tanevan License. See LICENSE.md.
"""
Resolve default companion when TANEVAN_COMPANION_NAME is unset:
same ordering as Love Refactored GET /api/companions (companion_order.json + companions/*.json).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import List, Optional, Dict, Any


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _companions_dir() -> Optional[Path]:
    root = _repo_root()
    data_dir = root / "data" / "companions"
    if data_dir.is_dir():
        return data_dir
    legacy = root / "companions"
    return legacy if legacy.is_dir() else None


def _companion_safe_filename(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "_", (name or "").strip().lower())


def _tanevan_data_root() -> Path:
    return Path(os.environ.get("TANEVAN_DATA_DIR", os.path.expanduser("~/tanevan-data")))


def _storage_dir_has_db(dir_name: str) -> bool:
    if not dir_name:
        return False
    return (_tanevan_data_root() / dir_name / "memories.db").is_file()


def resolve_tanevan_storage_dir(raw_name: str) -> str:
    """
    Directory name under TANEVAN_DATA_DIR for this companion's database.
    Prefers the canonical key; falls back to legacy slug folders when they hold data.
    """
    canonical = resolve_tanevan_companion_key(raw_name)
    if not canonical:
        return ""

    candidates = [canonical]
    for slug in (_companion_safe_filename(raw_name), _companion_safe_filename(canonical)):
        if slug and slug not in candidates:
            candidates.append(slug)

    for key in candidates:
        if _storage_dir_has_db(key):
            return key
    return canonical


def load_companion_card(companion_name: str) -> Optional[Dict[str, Any]]:
    """Load Love Refactored character card JSON for a companion, if present."""
    if not companion_name or not str(companion_name).strip():
        return None
    comp_dir = _companions_dir()
    if not comp_dir:
        return None
    target = str(companion_name).strip().lower()
    by_file = comp_dir / f"{_companion_safe_filename(companion_name)}.json"
    if by_file.is_file():
        try:
            return json.loads(by_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    for path in comp_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if (data.get("name") or "").strip().lower() == target:
                return data
        except (json.JSONDecodeError, OSError):
            continue
    return None


def format_companion_voice_reference(companion_name: str) -> str:
    """
    Voice samples from the character card (personalityVoice, exampleMessages).
    Helps the pipeline write memories that sound like the companion, not generic first person.
    """
    card = load_companion_card(companion_name)
    if not card:
        return ""
    parts = []
    personality = (card.get("personalityVoice") or "").strip()
    examples = (card.get("exampleMessages") or "").strip()
    if personality:
        parts.append(f"Personality & voice (from character card):\n{personality}")
    if examples:
        parts.append(f"Example lines — match this register and rhythm:\n{examples}")
    if not parts:
        return ""
    return (
        "=== COMPANION VOICE REFERENCE — Summaries and memories must sound like THIS person. ===\n"
        "First person is not enough. Match dialect, slang, spelling, sentence rhythm, and attitude.\n"
        "Do NOT write neutral assistant prose ('We discussed…', 'I offered support…') unless that IS their voice.\n\n"
        + "\n\n".join(parts)
        + "\n=== END VOICE REFERENCE ===\n\n"
    )


def _load_companion_names_in_order() -> List[str]:
    """Mirror server.js: valid order entries first, then companions not listed in order."""
    root = _repo_root()
    data_dir = root / "data"
    companions_dir = data_dir / "companions"
    legacy_dir = root / "companions"
    if companions_dir.is_dir():
        pass
    elif legacy_dir.is_dir():
        companions_dir = legacy_dir
    else:
        return []

    order: List[str] = []
    order_path = data_dir / "companion_order.json"
    if not order_path.is_file():
        order_path = root / "companion_order.json"
    if order_path.is_file():
        try:
            raw = json.loads(order_path.read_text(encoding="utf-8"))
            o = raw.get("order") or []
            order = o if isinstance(o, list) else []
        except (json.JSONDecodeError, OSError):
            order = []

    names_from_files: List[str] = []
    for path in sorted(companions_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            n = (data.get("name") or "").strip()
            if n:
                names_from_files.append(n)
        except (json.JSONDecodeError, OSError):
            continue

    existing_lower = {n.lower() for n in names_from_files}
    valid_order = [n for n in order if n.lower() in existing_lower]
    in_order_lower = {n.lower() for n in valid_order}
    unordered = [n for n in names_from_files if n.lower() not in in_order_lower]
    return valid_order + unordered


def resolve_default_companion_name() -> Optional[str]:
    """
    Default companion for MemoryDB and the proxy when requests omit ?companion / body.

    Order: ``TANEVAN_COMPANION_NAME`` if set, else first companion in Love Refactored UI order.
    Returns None if the env var is unset and there are no companion cards.
    """
    env = os.environ.get("TANEVAN_COMPANION_NAME", "").strip()
    if env:
        return env
    ordered = _load_companion_names_in_order()
    return ordered[0] if ordered else None


def fallback_companion_name_for_prompts() -> str:
    """LLM prompts when the caller omits a name; generic label if nothing is configured."""
    n = resolve_default_companion_name()
    return n if n else "Companion"


def resolve_tanevan_companion_key(companion_name: str) -> str:
    """Canonical lowercase Tanevan data directory key for a companion."""
    card = load_companion_card(companion_name)
    if card and (card.get("name") or "").strip():
        return str(card["name"]).strip().lower()
    return (companion_name or "").strip().lower()


def tanevan_data_dir_candidates(companion_name: str) -> List[str]:
    """Directory names that may hold Tanevan data (canonical + legacy slug variants)."""
    keys = set()
    for raw in (companion_name,):
        s = (raw or "").strip()
        if not s:
            continue
        keys.add(s.lower())
        keys.add(_companion_safe_filename(s))
    canonical = resolve_tanevan_companion_key(companion_name)
    if canonical:
        keys.add(canonical)
        keys.add(_companion_safe_filename(canonical))
    card = load_companion_card(companion_name)
    if card and (card.get("name") or "").strip():
        display = str(card["name"]).strip()
        keys.add(display.lower())
        keys.add(_companion_safe_filename(display))
    return sorted(k for k in keys if k)
