#!/usr/bin/env python3
# © 2024-2026 Megan Neves. All rights reserved.
# Tanevan Memory System — https://github.com/Love-Encoded/Love-Refactored
# Licensed under the Love Refactored / Tanevan License. See LICENSE.md.
"""
Backfill memory conversation timestamps from linked session summaries.

Before the timestamp fix, many memories got:
  - empty event_date
  - first_seen / last_seen set to pipeline processing time (SQLite datetime('now'))

This script joins memories.source_summary_id → summaries and sets:
  - event_date  ← summary.date_start
  - first_seen  ← summary.date_start (when missing or clearly wrong)
  - last_seen   ← summary.date_end (when missing or clearly wrong)

Usage:
  python3 backfill_event_dates.py --dry-run          # all companions, preview
  python3 backfill_event_dates.py --companion companion-name
  python3 backfill_event_dates.py --all --force      # overwrite existing event_date too

Env:
  TANEVAN_DATA_DIR  default ~/tanevan-data
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


BASE_DATA_DIR = os.environ.get("TANEVAN_DATA_DIR", os.path.expanduser("~/tanevan-data"))


def normalize_conversation_timestamp(raw):
    """Return ISO-8601 UTC string for a message/summary time, or None if unknown."""
    if raw is None:
        return None
    if isinstance(raw, str) and raw.strip() == "":
        return None
    if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.strip().lstrip("-").isdigit()):
        ts = float(raw)
        if ts > 1e12:
            ts = ts / 1000
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except (ValueError, OSError):
            return None
    return str(raw).strip() or None


def _ts_empty(value) -> bool:
    return not normalize_conversation_timestamp(value)


def _ts_key(value) -> str:
    """Sortable key for ISO-ish timestamps; empty sorts last."""
    n = normalize_conversation_timestamp(value)
    return n or "\xff"


def _looks_like_processing_time(first_seen, last_seen, summary) -> bool:
    """
    Heuristic: first_seen after the conversation ended, or first_seen date matches
    summary.created_at but not conversation date_start.
    """
    start = normalize_conversation_timestamp(summary["date_start"])
    end = normalize_conversation_timestamp(summary["date_end"]) or start
    fs = normalize_conversation_timestamp(first_seen)
    if not start or not fs:
        return False
    if end and _ts_key(fs) > _ts_key(end):
        return True
    created = normalize_conversation_timestamp(summary["created_at"])
    if created and fs[:10] == created[:10] and fs[:10] != start[:10]:
        return True
    # Pipeline often set first_seen == last_seen at insert time
    ls = normalize_conversation_timestamp(last_seen)
    if ls and fs == ls and _ts_key(fs) > _ts_key(end):
        return True
    return False


def discover_companions(data_dir: str) -> list[str]:
    root = Path(data_dir)
    if not root.is_dir():
        return []
    names = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "memories.db").is_file():
            names.append(child.name)
    return names


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Add event_date column if this DB predates the migration."""
    if _table_exists(conn, "memories") and not _column_exists(conn, "memories", "event_date"):
        conn.execute("ALTER TABLE memories ADD COLUMN event_date TEXT")
        conn.commit()


def fetch_memories_for_backfill(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    if _column_exists(conn, "memories", "event_date"):
        return conn.execute(
            """
            SELECT id, source_summary_id, event_date, first_seen, last_seen, content
            FROM memories
            WHERE active = 1
            """
        ).fetchall()
    return conn.execute(
        """
        SELECT id, source_summary_id, NULL AS event_date, first_seen, last_seen, content
        FROM memories
        WHERE active = 1
        """
    ).fetchall()


def load_summary_map(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    if not _table_exists(conn, "summaries"):
        return {}
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, date_start, date_end, created_at FROM summaries").fetchall()
    return {row["id"]: row for row in rows}


def plan_updates(conn: sqlite3.Connection, *, force: bool) -> list[dict]:
    if not _table_exists(conn, "memories"):
        return []
    summaries = load_summary_map(conn)
    memories = fetch_memories_for_backfill(conn)

    plans = []
    for mem in memories:
        sid = mem["source_summary_id"]
        if not sid:
            continue
        summary = summaries.get(sid)
        if not summary:
            continue

        conv_start = normalize_conversation_timestamp(summary["date_start"])
        conv_end = normalize_conversation_timestamp(summary["date_end"]) or conv_start
        if not conv_start:
            continue

        updates = {}
        reasons = []

        if force or _ts_empty(mem["event_date"]):
            new_event = conv_start
            if (mem["event_date"] or "") != (new_event or ""):
                updates["event_date"] = new_event
                reasons.append("event_date")

        fix_seen = (
            _ts_empty(mem["first_seen"])
            or _looks_like_processing_time(mem["first_seen"], mem["last_seen"], summary)
        )
        if fix_seen:
            if (mem["first_seen"] or "") != conv_start:
                updates["first_seen"] = conv_start
                reasons.append("first_seen")

        fix_last = _ts_empty(mem["last_seen"]) or (
            "first_seen" in updates
            and normalize_conversation_timestamp(mem["last_seen"])
            == normalize_conversation_timestamp(mem["first_seen"])
        )
        if fix_last and conv_end:
            if (mem["last_seen"] or "") != conv_end:
                updates["last_seen"] = conv_end
                reasons.append("last_seen")

        if updates:
            plans.append(
                {
                    "memory_id": mem["id"],
                    "summary_id": sid,
                    "reasons": reasons,
                    "updates": updates,
                    "preview": (mem["content"] or "")[:80],
                }
            )
    return plans


def apply_updates(conn: sqlite3.Connection, plans: list[dict]) -> int:
    applied = 0
    for plan in plans:
        fields = plan["updates"]
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [plan["memory_id"]]
        conn.execute(f"UPDATE memories SET {set_clause} WHERE id = ?", values)
        applied += 1
    conn.commit()
    return applied


def backfill_companion(companion: str, *, dry_run: bool, force: bool) -> dict:
    db_path = Path(BASE_DATA_DIR) / companion.lower() / "memories.db"
    if not db_path.is_file():
        return {
            "companion": companion,
            "error": f"No database at {db_path}",
            "planned": 0,
            "applied": 0,
        }

    conn = sqlite3.connect(db_path)
    try:
        if not _table_exists(conn, "memories"):
            return {
                "companion": companion,
                "error": f"No memories table in {db_path}",
                "planned": 0,
                "applied": 0,
            }
        if not _table_exists(conn, "summaries"):
            return {
                "companion": companion,
                "error": f"No summaries table in {db_path}",
                "planned": 0,
                "applied": 0,
            }
        needs_schema = not _column_exists(conn, "memories", "event_date")
        if needs_schema and dry_run:
            print(f"[{companion}] note: event_date column will be added on apply")
        plans = plan_updates(conn, force=force)
        applied = 0
        if plans and not dry_run:
            if needs_schema:
                ensure_schema(conn)
            applied = apply_updates(conn, plans)
        return {
            "companion": companion,
            "db_path": str(db_path),
            "planned": len(plans),
            "applied": applied,
            "plans": plans,
        }
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill memory event_date / first_seen / last_seen from linked summaries."
    )
    parser.add_argument(
        "--companion",
        action="append",
        dest="companions",
        metavar="NAME",
        help="Companion to process (repeatable). Default: every companion under TANEVAN_DATA_DIR.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Process all companions (default when --companion is omitted).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing event_date values from summary date_start.",
    )
    args = parser.parse_args(argv)

    if args.companions:
        companions = [c.strip().lower() for c in args.companions if c.strip()]
    else:
        companions = discover_companions(BASE_DATA_DIR)

    if not companions:
        print(f"No companions found under {BASE_DATA_DIR}", file=sys.stderr)
        return 1

    mode = "DRY RUN" if args.dry_run else "APPLY"
    print(f"=== Tanevan event_date backfill ({mode}) ===")
    print(f"Data dir: {BASE_DATA_DIR}")
    if args.force:
        print("Force: yes (overwrite existing event_date)")
    print()

    total_planned = 0
    total_applied = 0
    for name in companions:
        result = backfill_companion(name, dry_run=args.dry_run, force=args.force)
        if result.get("error"):
            print(f"[{name}] SKIP — {result['error']}")
            continue

        planned = result["planned"]
        applied = result["applied"]
        total_planned += planned
        total_applied += applied

        print(f"[{name}] {planned} memor{'y' if planned == 1 else 'ies'} to update"
              + (f" → {applied} written" if not args.dry_run else ""))

        if args.dry_run and result.get("plans"):
            for plan in result["plans"][:20]:
                fields = ", ".join(
                    f"{k}={v!r}" for k, v in plan["updates"].items()
                )
                print(f"  • {plan['memory_id']} ({', '.join(plan['reasons'])}): {fields}")
                print(f"    {plan['preview']!r}")
            if planned > 20:
                print(f"  … and {planned - 20} more")

    print()
    if args.dry_run:
        print(f"Total: {total_planned} memories would be updated. Re-run without --dry-run to apply.")
    else:
        print(f"Total: {total_applied} memories updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
