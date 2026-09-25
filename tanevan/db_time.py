# © 2024-2026 Megan Neves. All rights reserved.
# Tanevan Memory System — https://github.com/Love-Encoded/Love-Refactored
# Licensed under the Love Refactored / Tanevan License. See LICENSE.md.
"""Shared UTC / SQLite datetime helpers for Tanevan."""

from __future__ import annotations

from datetime import datetime, timezone


def ensure_utc(dt):
    """Make a datetime timezone-aware (UTC) if it isn't already."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def parse_db_datetime(raw):
    """Parse SQLite or ISO timestamps from the database into UTC-aware datetimes."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return ensure_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except (ValueError, TypeError):
        return None


def sqlite_utc_str(dt):
    """Format a datetime for SQLite TEXT comparisons (UTC, space-separated)."""
    dt = ensure_utc(dt)
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%d %H:%M:%S")
