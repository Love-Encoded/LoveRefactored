#!/usr/bin/env python3
# © 2024-2026 Megan Neves. All rights reserved.
# Tanevan Memory System — https://github.com/Love-Encoded/Love-Refactored
# Licensed under the Love Refactored / Tanevan License. See LICENSE.md.
"""
One-time backfill: extract entity names from existing memory content
and populate the entities column in SQLite.

Uses regex-based name extraction (same approach as consolidation entity guard).
No LLM calls, no cost, runs in seconds.

Run BEFORE re-embedding so reembed.py can include entities in embed text.

Usage:
  python3 backfill_entities.py --dry-run              # preview, no changes
  python3 backfill_entities.py --companion evan       # one companion
  python3 backfill_entities.py --all                  # every companion
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

BASE_DATA_DIR = os.environ.get("TANEVAN_DATA_DIR", os.path.expanduser("~/tanevan-data"))

# Words that look like names (capitalized) but aren't
COMMON_WORDS = {
    "the", "and", "but", "for", "are", "was", "were", "has", "had", "have",
    "this", "that", "she", "her", "him", "his", "they", "them", "their", "its",
    "not", "with", "from", "about", "what", "when", "where", "which", "who",
    "how", "why", "been", "being", "does", "did", "will", "would", "could",
    "should", "also", "just", "very", "really", "actually", "still", "already",
    "told", "said", "know", "knew", "think", "thought", "feel", "felt",
    "want", "wanted", "like", "going", "went", "come", "came",
    "something", "anything", "everything", "nothing", "someone", "anyone",
    "because", "since", "after", "before", "during", "between", "through",
    "into", "onto", "upon", "under", "over", "above", "below",
    "never", "always", "often", "sometimes", "maybe", "probably",
    "today", "yesterday", "tonight", "morning", "evening", "week", "month",
    "remember", "recall", "happened", "first", "last", "time", "then", "now",
    "told", "asked", "mentioned", "expressed", "described", "explained",
    "started", "began", "ended", "continued", "stopped", "tried", "needed",
    "made", "took", "gave", "got", "saw", "found", "left", "came", "went",
    "says", "feels", "wants", "needs", "thinks", "knows", "loves",
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "topics", "key", "moments", "conversation", "experience", "milestone",
    "fact", "preference", "relationship", "emotional", "arc", "narrative",
    "core", "important", "notable", "minor", "confidence",
    "however", "although", "though", "while", "whereas",
}

# Category/metadata words that appear in embed text but aren't entities
CATEGORY_WORDS = {
    "fact", "experience", "milestone", "preference", "relationship",
    "core", "important", "notable", "minor", "about",
}


def extract_entities_from_content(content):
    """
    Extract likely person/pet/entity names from memory content using regex.
    Returns a sorted list of unique names (original casing preserved).
    """
    if not content:
        return []

    # Find capitalized words 2+ chars that aren't at obvious sentence starts
    # We look for Title Case words
    words = re.findall(r'\b([A-Z][a-zA-Zà-öø-ÿĀ-žḀ-ỹ]{1,})\b', content)

    seen_lower = set()
    entities = []
    for w in words:
        lower = w.lower()
        if lower in COMMON_WORDS or lower in CATEGORY_WORDS:
            continue
        if lower in seen_lower:
            continue
        # Skip ALL-CAPS abbreviations that snuck through (shouldn't with our regex, but safety)
        if w.isupper() and len(w) <= 3:
            continue
        seen_lower.add(lower)
        entities.append(w)

    return sorted(entities)


def _parse_entities_field(raw):
    """Normalize entities from a list or SQLite JSON string to a list of names."""
    if not raw:
        return []
    if isinstance(raw, list):
        parsed = raw
    elif isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(parsed, list):
            return []
    else:
        return []
    return [str(e).strip() for e in parsed if e and str(e).strip()]


def refresh_entities_for_content(content, existing_entities=None):
    """
    Rebuild entity tags after a content rewrite.

    Keeps any existing tag whose name still appears in the new text (so LLM-cased
    tags like "Amanda" survive a lowercase reword), drops names that left, and
    adds newly extracted Title Case names. Used by MemoryDB.update_memory so
    user edits, updater merge/update, and consolidation merge don't leave stale
    ABOUT: prefixes / entity-recall tags pointing at the old person.
    """
    text = content or ""
    text_lower = text.lower()
    existing = _parse_entities_field(existing_entities)
    extracted = extract_entities_from_content(text)

    kept = []
    seen_lower = set()
    for name in existing:
        key = name.lower()
        if key in seen_lower:
            continue
        if re.search(r"\b" + re.escape(key) + r"\b", text_lower):
            kept.append(name)
            seen_lower.add(key)
    for name in extracted:
        key = name.lower()
        if key not in seen_lower:
            kept.append(name)
            seen_lower.add(key)
    return kept


def discover_companions(data_dir):
    root = Path(data_dir)
    if not root.is_dir():
        return []
    names = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "memories.db").is_file():
            names.append(child.name)
    return names


def backfill_companion(companion, *, dry_run=False):
    """Backfill entities for one companion."""
    companion_dir = os.path.join(BASE_DATA_DIR, companion.lower())
    sqlite_path = os.path.join(companion_dir, "memories.db")

    if not os.path.isfile(sqlite_path):
        return {"companion": companion, "error": f"No database at {sqlite_path}"}

    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row

    # Check if entities column exists
    columns = [row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()]
    if "entities" not in columns:
        print(f"  Adding entities column...")
        conn.execute("ALTER TABLE memories ADD COLUMN entities TEXT NOT NULL DEFAULT '[]'")
        conn.commit()

    rows = conn.execute(
        "SELECT id, content, entities FROM memories WHERE active = 1"
    ).fetchall()

    stats = {
        "companion": companion,
        "total": len(rows),
        "updated": 0,
        "already_tagged": 0,
        "no_entities_found": 0,
    }

    for row in rows:
        existing = row["entities"] or "[]"
        try:
            existing_list = json.loads(existing)
        except (json.JSONDecodeError, TypeError):
            existing_list = []

        # Skip if already has entities (from new extraction pipeline)
        if existing_list:
            stats["already_tagged"] += 1
            continue

        entities = extract_entities_from_content(row["content"])

        if not entities:
            stats["no_entities_found"] += 1
            continue

        if dry_run:
            print(f"    {row['id']}: {entities}")
            stats["updated"] += 1
            continue

        conn.execute(
            "UPDATE memories SET entities = ? WHERE id = ?",
            (json.dumps(entities), row["id"])
        )
        stats["updated"] += 1

    if not dry_run:
        conn.commit()
    conn.close()

    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Backfill entity tags on existing memories using regex name extraction."
    )
    parser.add_argument(
        "--companion", action="append", dest="companions", metavar="NAME",
        help="Companion to process (repeatable). Default: all.",
    )
    parser.add_argument("--all", action="store_true", help="Process all companions.")
    parser.add_argument("--dry-run", action="store_true", help="Preview without changing.")
    args = parser.parse_args(argv)

    companions = (
        [c.strip().lower() for c in args.companions if c.strip()]
        if args.companions
        else discover_companions(BASE_DATA_DIR)
    )

    if not companions:
        print(f"No companions found under {BASE_DATA_DIR}", file=sys.stderr)
        return 1

    mode = "DRY RUN" if args.dry_run else "LIVE"
    print(f"=== Entity Backfill ({mode}) ===")
    print(f"Data dir: {BASE_DATA_DIR}")
    print(f"Companions: {', '.join(companions)}")
    print()

    total_updated = 0
    for name in companions:
        print(f"[{name}]")
        result = backfill_companion(name, dry_run=args.dry_run)
        if result.get("error"):
            print(f"  SKIP — {result['error']}")
            continue
        print(f"  Total: {result['total']}, Updated: {result['updated']}, "
              f"Already tagged: {result['already_tagged']}, "
              f"No entities found: {result['no_entities_found']}")
        total_updated += result["updated"]

    print(f"\n{'=' * 50}")
    action = "Would update" if args.dry_run else "Updated"
    print(f"{action}: {total_updated} memories with entity tags")
    if not args.dry_run:
        print("Now run: python3 reembed.py --all")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
