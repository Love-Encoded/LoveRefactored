#!/usr/bin/env python3
"""
prune_chroma_ghosts.py — remove search-index entries for retired memories.

A "ghost" is a Chroma vector whose SQLite row has active=0 (or no row at all).
Nothing in memories.db is touched. Retired memories stay readable in the Vault
and can be reactivated later (Step 3 adds re-embed on active=1).

Run with tanevan STOPPED (pm2 stop tanevan). Uses the same paths as MemoryDB.

Usage:
    TANEVAN_DATA_DIR=/opt/love-refactored/tanevan-data \\
      /opt/love-refactored/tanevan-venv/bin/python prune_chroma_ghosts.py --companion evan
    add --yes to skip the confirmation prompt
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

import chromadb

BASE_DATA_DIR = os.environ.get("TANEVAN_DATA_DIR", os.path.expanduser("~/tanevan-data"))
BATCH = 500


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--companion", required=True)
    ap.add_argument("--yes", action="store_true", help="skip confirmation")
    args = ap.parse_args()

    companion_dir = os.path.join(BASE_DATA_DIR, args.companion)
    sqlite_path = os.path.join(companion_dir, "memories.db")
    chroma_path = os.path.join(companion_dir, "chroma_db")
    if not os.path.isfile(sqlite_path):
        sys.exit(f"no memories.db at {sqlite_path} — check TANEVAN_DATA_DIR / --companion")
    if not os.path.isdir(chroma_path):
        sys.exit(f"no chroma_db at {chroma_path}")

    db = sqlite3.connect(sqlite_path)
    active_ids = {r[0] for r in db.execute("SELECT id FROM memories WHERE active = 1")}
    inactive_ids = {r[0] for r in db.execute("SELECT id FROM memories WHERE active = 0")}

    chroma = chromadb.PersistentClient(path=chroma_path)
    col = chroma.get_or_create_collection(name="memories")
    index_ids = set(col.get(include=[])["ids"])

    ghosts_inactive = sorted(index_ids & inactive_ids)   # retired rows still indexed
    ghosts_orphan = sorted(index_ids - active_ids - inactive_ids)  # no SQLite row at all
    missing = sorted(active_ids - index_ids)             # active but NOT indexed (info only)
    ghosts = ghosts_inactive + ghosts_orphan

    print(f"companion:            {args.companion}")
    print(f"SQLite active:        {len(active_ids)}")
    print(f"SQLite inactive:      {len(inactive_ids)}")
    print(f"Chroma index size:    {len(index_ids)}")
    print(f"ghosts (inactive):    {len(ghosts_inactive)}")
    print(f"ghosts (no row):      {len(ghosts_orphan)}")
    print(f"active but unindexed: {len(missing)}  (not touched — informational)")
    print(f"index after cleanup:  {len(index_ids) - len(ghosts)}")

    if not ghosts:
        print("nothing to do.")
        return
    if not args.yes:
        ans = input(f"\nDelete {len(ghosts)} ghost vectors from Chroma? memories.db is NOT modified. [y/N] ").strip().lower()
        if ans != "y":
            print("aborted.")
            return

    removed = 0
    for i in range(0, len(ghosts), BATCH):
        chunk = ghosts[i:i + BATCH]
        col.delete(ids=chunk)
        removed += len(chunk)
        print(f"  removed {removed}/{len(ghosts)}")

    final = col.count()
    stamp = datetime.now(timezone.utc).isoformat()
    print(f"\n✓ done {stamp} — removed {removed}, Chroma memories index now {final}")
    if final != len(active_ids):
        print(f"  note: index ({final}) != active rows ({len(active_ids)}). "
              f"Difference = {len(missing)} active memories never embedded — separate issue, nothing lost.")


if __name__ == "__main__":
    main()