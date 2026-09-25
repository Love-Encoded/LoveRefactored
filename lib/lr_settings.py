# © 2024-2026 Megan Neves. All rights reserved.
# Love Refactored — canonical settings path resolution for Python services.
# Licensed under the Love Refactored / Tanevan License. See LICENSE.md.
"""
Single source of truth for Love Refactored settings on disk.

Resolution order for reads:
  1. LR_SETTINGS_PATH env (explicit override)
  2. ${LR_HOME}/data/settings.json

Legacy repo-root settings.json is never read directly. If it exists and the
canonical file does not, it is copied once into data/settings.json.
"""

from __future__ import annotations

import json
import os
import shutil
import sys


def love_refactored_root() -> str:
    env_home = (os.environ.get("LR_HOME") or "").strip()
    if env_home:
        return os.path.abspath(os.path.expanduser(env_home))
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def data_dir() -> str:
    explicit = (os.environ.get("LR_DATA_DIR") or "").strip()
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    return os.path.join(love_refactored_root(), "data")


def settings_path() -> str:
    explicit = (os.environ.get("LR_SETTINGS_PATH") or "").strip()
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    return os.path.join(data_dir(), "settings.json")


def legacy_settings_path() -> str:
    return os.path.join(love_refactored_root(), "settings.json")


def ensure_settings_file(*, migrate_legacy: bool = True) -> str:
    """Return canonical settings path; optionally migrate legacy file once."""
    canonical = settings_path()
    if os.path.isfile(canonical):
        return canonical
    legacy = legacy_settings_path()
    if migrate_legacy and os.path.isfile(legacy):
        os.makedirs(os.path.dirname(canonical), exist_ok=True)
        shutil.copy2(legacy, canonical)
        print(
            f"⚠ Migrated legacy settings: {legacy} → {canonical}",
            file=sys.stderr,
        )
    return canonical


def load_settings(*, migrate_legacy: bool = True) -> dict:
    path = ensure_settings_file(migrate_legacy=migrate_legacy)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def settings_api_key(section: str) -> str:
    block = load_settings().get(section) or {}
    if not isinstance(block, dict):
        return ""
    return (block.get("apiKey") or "").strip()


def voice_messages_dir() -> str:
    """Canonical directory for generated voice memo MP3s served at /api/voice-message/."""
    explicit = (os.environ.get("VOICE_MESSAGES_DIR") or "").strip()
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    return os.path.join(data_dir(), "voice_messages")


def ensure_voice_messages_dir() -> str:
    path = voice_messages_dir()
    os.makedirs(path, exist_ok=True)
    return path
