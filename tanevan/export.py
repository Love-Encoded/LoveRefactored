# © 2024-2026 Megan Neves. All rights reserved.
# Tanevan Memory System — https://github.com/Love-Encoded/Love-Refactored
# Licensed under the Love Refactored / Tanevan License. See LICENSE.md.
"""
Tanevan - Export Dossier
Exports the memory dossier and summaries as clean, readable text files.
"""

from memory_db import MemoryDB
from companion_resolve import resolve_default_companion_name
from datetime import datetime
import os
import sys

OUTPUT_DIR = os.path.expanduser("~/Desktop")


def _require_default_companion():
    n = resolve_default_companion_name()
    if not n:
        raise ValueError(
            "No default companion: add one in Love Refactored or set TANEVAN_COMPANION_NAME."
        )
    return n


def export_dossier(db=None):
    """Export the full memory dossier as a readable text file."""
    own_db = db is None
    if own_db:
        db = MemoryDB(_require_default_companion())

    memories = db.get_all_memories()
    stats = db.get_memory_stats()

    # Group by category
    by_category = {}
    for mem in memories:
        cat = mem["category"]
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(mem)

    # Build the document
    lines = []
    lines.append("=" * 70)
    lines.append("TANEVAN — MEMORY DOSSIER")
    lines.append(f"Exported: {datetime.now().strftime('%B %d, %Y at %I:%M %p')}")
    lines.append(f"Total memories: {stats['total']}")
    lines.append("=" * 70)

    category_labels = {
        "fact": "FACTS — What I Know",
        "experience": "EXPERIENCES — What We've Shared",
        "milestone": "MILESTONES — Turning Points",
        "preference": "PREFERENCES — How She Wants Things",
        "relationship": "RELATIONSHIPS — Who We Are To Each Other"
    }

    category_order = ["fact", "experience", "milestone", "preference", "relationship"]

    for cat in category_order:
        mems = by_category.get(cat, [])
        if not mems:
            continue

        lines.append("")
        lines.append("")
        lines.append("-" * 70)
        lines.append(f"  {category_labels.get(cat, cat.upper())}  ({len(mems)})")
        lines.append("-" * 70)

        # Sort by priority then confidence
        priority_order = {"core": 0, "important": 1, "notable": 2, "minor": 3}
        mems.sort(key=lambda m: (priority_order.get(m["priority"], 99), -m["confidence"]))

        for mem in mems:
            lines.append("")
            priority_tag = f"[{mem['priority'].upper()}]"
            confidence = f"{mem['confidence']}%"
            lines.append(f"  {priority_tag} ({confidence})")
            lines.append(f"  {mem['content']}")
            if mem["reinforcement_count"] > 1:
                lines.append(f"  — confirmed {mem['reinforcement_count']} times")

    lines.append("")
    lines.append("")
    lines.append("=" * 70)
    lines.append("END OF DOSSIER")
    lines.append("=" * 70)

    # Write to file
    filepath = os.path.join(OUTPUT_DIR, "tanevan_dossier.txt")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"✓ Dossier exported to: {filepath}")

    if own_db:
        db.close()

    return filepath


def export_summaries(db=None, n=20):
    """Export recent summaries as a readable text file."""
    own_db = db is None
    if own_db:
        db = MemoryDB(_require_default_companion())

    summaries = db.get_recent_summaries(n=n)

    lines = []
    lines.append("=" * 70)
    lines.append("TANEVAN — CONVERSATION SUMMARIES")
    lines.append(f"Exported: {datetime.now().strftime('%B %d, %Y at %I:%M %p')}")
    lines.append(f"Total summaries shown: {len(summaries)}")
    lines.append("=" * 70)

    for s in summaries:
        lines.append("")
        lines.append("")
        lines.append("-" * 70)
        lines.append(f"  {s['date_start']}  —  {s['date_end']}")
        lines.append(f"  {s['message_count']} messages summarized")
        lines.append("-" * 70)
        lines.append("")
        lines.append(f"  {s['emotional_arc']}")
        lines.append("")
        lines.append(f"  {s['narrative']}")
        lines.append("")
        lines.append("  TOPICS:")
        for topic in s["topics"]:
            lines.append(f"    • {topic}")
        lines.append("")
        lines.append("  KEY MOMENTS:")
        for moment in s["key_moments"]:
            lines.append(f"    ◆ {moment}")

    lines.append("")
    lines.append("")
    lines.append("=" * 70)
    lines.append("END OF SUMMARIES")
    lines.append("=" * 70)

    filepath = os.path.join(OUTPUT_DIR, "tanevan_summaries.txt")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"✓ Summaries exported to: {filepath}")

    if own_db:
        db.close()

    return filepath


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("TANEVAN — Exporting Dossier & Summaries")
    print("=" * 60)

    try:
        db = MemoryDB(_require_default_companion())
    except ValueError as e:
        print(f"✗ {e}")
        sys.exit(1)
    export_dossier(db)
    export_summaries(db)
    db.close()

    print("\nFiles saved to your Desktop.")
