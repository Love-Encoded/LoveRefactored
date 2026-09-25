# © 2024-2026 Megan Neves. All rights reserved.
# Tanevan Memory System — https://github.com/Love-Encoded/Love-Refactored
# Licensed under the Tanevan / Love Refactored License v2.0. See LICENSE.md.
"""
Resolve the user's local IANA timezone for life-brief and scene-note sidecars.

Precedence:
  1. BRIEF_TZ environment variable (explicit operator override)
  2. settings.json userTimezone (auto-detected from the browser)
  3. System local IANA timezone of the host running Tanevan
  4. UTC
"""

import datetime
import json
import os

try:
    from zoneinfo import ZoneInfo
    _HAVE_ZONEINFO = True
except ImportError:
    _HAVE_ZONEINFO = False


def _love_refactored_root():
    env_home = (os.environ.get("LR_HOME") or "").strip()
    if env_home:
        return os.path.abspath(os.path.expanduser(env_home))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _settings_candidates():
    root = _love_refactored_root()
    return [
        os.path.join(root, "data", "settings.json"),
        os.path.join(root, "settings.json"),
    ]


def _load_lr_settings():
    try:
        for settings_path in _settings_candidates():
            if os.path.isfile(settings_path):
                with open(settings_path, encoding="utf-8") as f:
                    return json.load(f)
    except Exception:
        pass
    return {}


def _is_valid_iana(name):
    if not name or not _HAVE_ZONEINFO:
        return bool(name)
    try:
        ZoneInfo(name)
        return True
    except Exception:
        return False


def _path_lexists(path):
    try:
        os.lstat(path)
        return True
    except OSError:
        return False


def _iana_from_localtime_symlink():
    """Read IANA zone name from /etc/localtime (macOS + most Linux)."""
    for link in ("/etc/localtime", "/private/etc/localtime"):
        try:
            if not _path_lexists(link):
                continue
            target = os.readlink(link)
            marker = "zoneinfo/"
            idx = target.find(marker)
            if idx >= 0:
                name = target[idx + len(marker):].strip("/")
                if _is_valid_iana(name):
                    return name
        except OSError:
            continue
    return ""


def _system_timezone_name():
    """Best-effort IANA name for the host's local timezone."""
    name = _iana_from_localtime_symlink()
    if name:
        return name

    try:
        local = datetime.datetime.now().astimezone().tzinfo
        key = getattr(local, "key", None)
        if key and _is_valid_iana(str(key)):
            return str(key)
    except Exception:
        pass

    tz_env = (os.environ.get("TZ") or "").strip()
    if tz_env and _is_valid_iana(tz_env):
        return tz_env

    return "UTC"


def read_user_timezone():
    """User timezone from settings, else system local, else UTC. Ignores BRIEF_TZ env."""
    settings = _load_lr_settings()
    tz = (settings.get("userTimezone") or settings.get("timezone") or "").strip()
    if tz and _is_valid_iana(tz):
        return tz
    return _system_timezone_name()


def resolve_user_timezone():
    """Effective timezone for sidecars — honors BRIEF_TZ env override when set."""
    env = (os.environ.get("BRIEF_TZ") or "").strip()
    if env and _is_valid_iana(env):
        return env
    return read_user_timezone()


def format_local_now(tz_name):
    """Portable '17 July 2026, 2:41 PM (Friday)' string without strftime %- flags."""
    if _HAVE_ZONEINFO:
        now = datetime.datetime.now(ZoneInfo(tz_name))
    else:
        now = datetime.datetime.now()
    hour = now.hour % 12 or 12
    return f"{now.day} {now.strftime('%B %Y')}, {hour}:{now.strftime('%M %p')} ({now.strftime('%A')})"


if __name__ == "__main__":
    print(resolve_user_timezone())
