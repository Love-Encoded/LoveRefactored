#!/usr/bin/env python3
# © 2024-2026 Megan Neves. All rights reserved.
# Tanevan Memory System — https://github.com/Love-Encoded/Love-Refactored
# Licensed under the Love Refactored / Tanevan License. See LICENSE.md.
"""
One-time migration script: re-embed all memories and summaries with the new
embedding model and enriched text format.

Run AFTER updating memory_db.py with the new model constant.
Stops Tanevan first to avoid conflicts — restart it after.

Usage:
  python3 reembed.py --dry-run              # preview, no changes
  python3 reembed.py --companion evan       # one companion
  python3 reembed.py --all                  # every companion

Env:
  TANEVAN_DATA_DIR  default ~/tanevan-data
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import chromadb
from sentence_transformers import SentenceTransformer

# Import the canonical model name and helpers from memory_db
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memory_db import EMBEDDING_MODEL, build_memory_embed_text, build_summary_embed_text

BASE_DATA_DIR = os.environ.get("TANEVAN_DATA_DIR", os.path.expanduser("~/tanevan-data"))
BATCH_SIZE = 200  # ChromaDB add batch size


def discover_companions(data_dir: str) -> list[str]:
    root = Path(data_dir)
    if not root.is_dir():
        return []
    names = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "memories.db").is_file():
            names.append(child.name)
    return names


def reembed_companion(companion: str, embedder: SentenceTransformer, *, dry_run: bool) -> dict:
    """Re-embed all memories and summaries for one companion."""
    companion_dir = os.path.join(BASE_DATA_DIR, companion.lower())
    sqlite_path = os.path.join(companion_dir, "memories.db")
    chroma_path = os.path.join(companion_dir, "chroma_db")

    if not os.path.isfile(sqlite_path):
        return {"companion": companion, "error": f"No database at {sqlite_path}"}

    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row

    # --- Load memories from SQLite ---
    mem_rows = conn.execute(
        "SELECT id, category, priority, confidence, content, entities, suppressed FROM memories WHERE active = 1"
    ).fetchall()

    # --- Load summaries from SQLite ---
    sum_rows = conn.execute(
        "SELECT id, narrative, topics, key_moments, date_start, date_end, message_count FROM summaries"
    ).fetchall()

    conn.close()

    stats = {
        "companion": companion,
        "memories": len(mem_rows),
        "summaries": len(sum_rows),
    }

    if dry_run:
        print(f"  [DRY RUN] Would re-embed {len(mem_rows)} memories + {len(sum_rows)} summaries")
        return stats

    # --- Build enriched texts for memories ---
    mem_ids = []
    mem_texts = []
    mem_documents = []
    mem_metadatas = []
    for row in mem_rows:
        entities_raw = row["entities"] if "entities" in row.keys() else "[]"
        embed_text = build_memory_embed_text(row["category"], row["priority"], row["content"], entities=entities_raw)
        mem_ids.append(row["id"])
        mem_texts.append(embed_text)
        mem_documents.append(row["content"])
        mem_metadatas.append({
            "category": row["category"],
            "priority": row["priority"],
            "confidence": row["confidence"],
            "entities": entities_raw,
            "suppressed": int(row["suppressed"] or 0),
        })

    # --- Build enriched texts for summaries ---
    sum_ids = []
    sum_texts = []
    sum_documents = []
    sum_metadatas = []
    for row in sum_rows:
        topics = json.loads(row["topics"]) if row["topics"] else []
        key_moments = json.loads(row["key_moments"]) if row["key_moments"] else []
        narrative = row["narrative"] or ""
        embed_text = build_summary_embed_text(narrative, topics, key_moments)
        sum_ids.append(row["id"])
        sum_texts.append(embed_text)
        sum_documents.append(narrative)
        sum_metadatas.append({
            "date_start": row["date_start"] or "",
            "date_end": row["date_end"] or "",
            "message_count": row["message_count"] or 0,
        })

    # --- Encode all texts in batches ---
    print(f"  Encoding {len(mem_texts)} memories...")
    t0 = time.time()
    mem_embeddings = embedder.encode(mem_texts, batch_size=64, show_progress_bar=True)
    print(f"  Encoding {len(sum_texts)} summaries...")
    sum_embeddings = embedder.encode(sum_texts, batch_size=64, show_progress_bar=True)
    encode_time = time.time() - t0
    print(f"  Encoded in {encode_time:.1f}s")

    # --- Rebuild ChromaDB collections ---
    print(f"  Rebuilding ChromaDB collections...")
    chroma = chromadb.PersistentClient(path=chroma_path)

    # Delete old collections
    try:
        chroma.delete_collection("memories")
    except Exception:
        pass
    try:
        chroma.delete_collection("summaries")
    except Exception:
        pass

    # Recreate with fresh schema
    memories_col = chroma.get_or_create_collection(
        name="memories",
        metadata={
            "hnsw:space": "cosine",
            "hnsw:construction_ef": 200,
            "hnsw:search_ef": 150,
            "hnsw:M": 32,
        }
    )
    summaries_col = chroma.get_or_create_collection(
        name="summaries",
        metadata={
            "hnsw:space": "cosine",
            "hnsw:construction_ef": 200,
            "hnsw:search_ef": 150,
            "hnsw:M": 32,
        }
    )

    # --- Add memories in batches ---
    for i in range(0, len(mem_ids), BATCH_SIZE):
        end = min(i + BATCH_SIZE, len(mem_ids))
        memories_col.add(
            ids=mem_ids[i:end],
            embeddings=[e.tolist() for e in mem_embeddings[i:end]],
            documents=mem_documents[i:end],
            metadatas=mem_metadatas[i:end],
        )

    # --- Add summaries in batches ---
    for i in range(0, len(sum_ids), BATCH_SIZE):
        end = min(i + BATCH_SIZE, len(sum_ids))
        summaries_col.add(
            ids=sum_ids[i:end],
            embeddings=[e.tolist() for e in sum_embeddings[i:end]],
            documents=sum_documents[i:end],
            metadatas=sum_metadatas[i:end],
        )

    stats["encode_seconds"] = round(encode_time, 1)
    print(f"  ✓ Done — {len(mem_ids)} memories + {len(sum_ids)} summaries re-embedded")
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-embed all memories and summaries with the new embedding model."
    )
    parser.add_argument(
        "--companion", action="append", dest="companions", metavar="NAME",
        help="Companion to process (repeatable). Default: all.",
    )
    parser.add_argument("--all", action="store_true", help="Process all companions.")
    parser.add_argument("--dry-run", action="store_true", help="Preview without changing.")
    args = parser.parse_args(argv)

    companions = [c.strip().lower() for c in args.companions if c.strip()] if args.companions else discover_companions(BASE_DATA_DIR)

    if not companions:
        print(f"No companions found under {BASE_DATA_DIR}", file=sys.stderr)
        return 1

    mode = "DRY RUN" if args.dry_run else "LIVE"
    print(f"=== Tanevan Re-Embed Migration ({mode}) ===")
    print(f"Model: {EMBEDDING_MODEL}")
    print(f"Data dir: {BASE_DATA_DIR}")
    print(f"Companions: {', '.join(companions)}")
    print()

    if not args.dry_run:
        print("⚠️  STOP TANEVAN BEFORE RUNNING THIS (pm2 stop tanevan)")
        print("   ChromaDB collections will be deleted and rebuilt.")
        print("   Press Enter to continue or Ctrl+C to abort...")
        try:
            input()
        except KeyboardInterrupt:
            print("\nAborted.")
            return 1

    # Load model once
    print(f"Loading embedding model: {EMBEDDING_MODEL}...")
    t0 = time.time()
    embedder = SentenceTransformer(EMBEDDING_MODEL)
    print(f"✓ Model loaded in {time.time() - t0:.1f}s")
    print()

    total_memories = 0
    total_summaries = 0

    for name in companions:
        print(f"\n[{name}]")
        result = reembed_companion(name, embedder, dry_run=args.dry_run)
        if result.get("error"):
            print(f"  SKIP — {result['error']}")
            continue
        total_memories += result.get("memories", 0)
        total_summaries += result.get("summaries", 0)

    print(f"\n{'=' * 50}")
    if args.dry_run:
        print(f"Would re-embed: {total_memories} memories + {total_summaries} summaries")
        print("Re-run without --dry-run to apply.")
    else:
        print(f"✓ Re-embedded: {total_memories} memories + {total_summaries} summaries")
        print("Now restart Tanevan: pm2 restart tanevan")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
