# © 2024-2026 Megan Neves. All rights reserved.
# Tanevan Memory System — https://github.com/Love-Encoded/Love-Refactored
# Licensed under the Love Refactored / Tanevan License. See LICENSE.md.
"""
Tanevan - Memory Consolidation
Periodic maintenance that finds near-duplicates, resolves contradictions,
and auto-promotes reinforced memories. Like sleep consolidation for the dossier.

Run manually via POST /consolidate or on a weekly cron.
"""

import json
import os
import random
from datetime import datetime, timezone

from pipeline_llm import run_pipeline_step, strip_json_fences
from companion_resolve import fallback_companion_name_for_prompts
from memory_lens import prepend_memory_lens

USER_NAME = os.environ.get("TANEVAN_USER_NAME", "the user")
# auto_promote_ceiling_v1 (30 Aug 2026): reinforcement no longer mints "core".
# r>=10 mostly meant "got merged a lot", which mostly meant "was big" — 36% of
# Evan's corpus was core and the tier stopped meaning anything in rerank.
# Reinforcement now tops out at "important"; core is assigned by the extractor
# or a human on purpose. Set TANEVAN_AUTO_PROMOTE_CORE=1 to restore r>=10 -> core.
AUTO_PROMOTE_CORE = os.environ.get("TANEVAN_AUTO_PROMOTE_CORE", "0") in ("1", "true", "True")

CONSOLIDATION_PROMPT = """You are a memory maintenance system for an AI companion named {companion_name}.

You are comparing two EXISTING memories from {companion_name}'s dossier that appear very similar.
Decide what to do with them:

ACTIONS:
- "keep_both": Both memories contain unique, non-redundant information worth preserving separately.
  Example: "She's 42" and "Her birthday is March 15" — same topic, different details. Keep both.
- "merge": They describe the same thing and should be combined into one better entry.
  Provide merged_content in {companion_name}'s voice.
  Example: "We had a threesome" and "Our first group experience as a triad" — merge them.
- "supersede": One is more current, accurate, or complete. Keep that one, archive the other.
  Set keep_id to the ID of the better memory.
  Example: "She's 42" and "She's 43" — the more recent one supersedes.

Respond with ONLY valid JSON:

{{
    "action": "keep_both|merge|supersede",
    "keep_id": "id of the memory to KEEP (for supersede only, null otherwise)",
    "merged_content": "combined text in {companion_name}'s voice (for merge only, null otherwise)",
    "reasoning": "brief explanation"
}}

RULES:
- Be CONSERVATIVE. When in doubt, keep_both. Losing information is worse than having duplicates.
- Two memories about the same TOPIC with DIFFERENT details → keep_both.
- Two memories saying the SAME THING in different words → merge.
- One memory is clearly outdated by the other → supersede (keep the newer/better one).
- For "merge", write merged_content in {companion_name}'s voice — match dialect, slang, register.
- RESPOND WITH ONLY THE JSON.
"""


def _extract_entity_set(mem):
    """Get the entity set for a memory, from the entities field or by scanning content."""
    raw = mem.get("entities", "[]")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raw = []
    if isinstance(raw, list) and raw:
        return {str(e).strip().lower() for e in raw if e and str(e).strip()}
    # Fallback: scan content for capitalized names (same heuristic as retrieval)
    content = mem.get("content", "")
    import re
    names = re.findall(r'\b[A-Z][a-zA-Z]{2,}\b', content)
    common = {
        "the", "and", "but", "for", "are", "was", "were", "has", "had", "have",
        "this", "that", "she", "her", "his", "they", "them", "their", "not",
        "with", "from", "about", "what", "when", "where", "which", "who", "how",
        "been", "being", "does", "did", "will", "would", "could", "should",
        "also", "just", "very", "really", "actually", "still", "already",
        "told", "said", "know", "think", "feel", "want", "like", "going",
        "something", "anything", "everything", "nothing", "someone", "anyone",
    }
    return {n.lower() for n in names if n.lower() not in common}


def _entities_overlap(mem_a, mem_b):
    """Check whether two memories share at least one entity. Empty sets = unknown = allow."""
    set_a = _extract_entity_set(mem_a)
    set_b = _extract_entity_set(mem_b)
    if not set_a or not set_b:
        return True  # can't tell — allow the pair through
    return bool(set_a & set_b)


def find_consolidation_candidates(db, threshold=0.3, sample_size=None, max_pairs=50):
    """
    Find pairs of existing memories that are suspiciously similar.
    Scales sample size to corpus size so coverage stays meaningful at high volumes.
    Applies entity guard: pairs about different entities are skipped.

    Args:
        db: MemoryDB instance
        threshold: ChromaDB cosine distance below which a pair is "suspicious" (0.0 = identical)
        sample_size: How many memories to spot-check this run (None = auto-scale)
        max_pairs: Maximum pairs to return

    Returns:
        List of dicts: {mem_a, mem_b, distance}
    """
    all_memories = db.get_all_memories(active_only=True)
    def _is_dream_mem(m):
        return str(m.get("id", "")).startswith("dreammem_") or m.get("category") == "dream"
    all_memories = [m for m in all_memories if not _is_dream_mem(m)]
    if len(all_memories) < 2:
        return []

    # Auto-scale: sample 5% of corpus, floor 100, ceiling 500
    if sample_size is None:
        sample_size = max(100, min(500, int(len(all_memories) * 0.05)))

    sample = random.sample(all_memories, min(sample_size, len(all_memories)))

    seen_pairs = set()
    candidates = []

    for mem in sample:
        similar = db.find_similar_memories(mem["content"], n=5, min_confidence=0)
        for s in similar:
            if s["id"] == mem["id"]:
                continue
            if _is_dream_mem(s):
                continue
            distance = s.get("_distance", 2.0)
            if distance > threshold:
                continue
            pair = tuple(sorted([mem["id"], s["id"]]))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)

            # Entity guard: never consolidate memories about different entities.
            # This prevents cross-entity merges (Chris/Jody collision).
            if not _entities_overlap(mem, s):
                continue

            candidates.append({
                "mem_a": mem,
                "mem_b": s,
                "distance": distance,
            })

    candidates.sort(key=lambda c: c["distance"])
    return candidates[:max_pairs]


def resolve_pair(mem_a, mem_b, api_key=None, companion_name=None):
    """
    Ask the LLM to decide what to do with a pair of similar memories.

    Returns:
        Dict with action, keep_id, merged_content, reasoning — or None on failure.
    """
    c_name = companion_name or fallback_companion_name_for_prompts()

    system_prompt = prepend_memory_lens(
        CONSOLIDATION_PROMPT.format(
            companion_name=c_name,
            user_name=USER_NAME,
        ),
        c_name,
    )

    user_content = f"""MEMORY A:
ID: {mem_a['id']}
Category: {mem_a['category']}
Content: {mem_a['content']}
Confidence: {mem_a['confidence']}%
Priority: {mem_a['priority']}
Event date: {mem_a.get('event_date', 'unknown')}
Reinforced: {mem_a.get('reinforcement_count', 1)} times

MEMORY B:
ID: {mem_b['id']}
Category: {mem_b['category']}
Content: {mem_b['content']}
Confidence: {mem_b['confidence']}%
Priority: {mem_b['priority']}
Event date: {mem_b.get('event_date', 'unknown')}
Reinforced: {mem_b.get('reinforcement_count', 1)} times

What should we do with these two memories?"""

    raw_text, meta = run_pipeline_step(
        "updater",
        api_key,
        system_prompt,
        user_content,
        max_tokens=500,
        temperature=0.2,
    )
    if not raw_text or not meta:
        return None

    raw_text = strip_json_fences(raw_text)

    try:
        decision = json.loads(raw_text)
    except json.JSONDecodeError as e:
        print(f"  ✗ Failed to parse consolidation response: {e}")
        return None

    valid_actions = {"keep_both", "merge", "supersede"}
    if decision.get("action") not in valid_actions:
        print(f"  ✗ Invalid action: {decision.get('action')}")
        return None

    return decision


BATCH_RESOLVE_PROMPT_SUFFIX = """

You will receive MULTIPLE numbered pairs in one message. Respond with ONLY a
valid JSON array — one decision object per pair, in the same order, each with
a "pair" field matching the pair number:

[
  {"pair": 1, "action": "merge", "merged_content": "...", "reasoning": "..."},
  {"pair": 2, "action": "keep_both", "reasoning": "..."},
  {"pair": 3, "action": "supersede", "keep_id": "mem_...", "reasoning": "..."}
]

Every pair MUST get exactly one decision. RESPOND WITH ONLY THE JSON ARRAY."""


def resolve_pairs_batch(pairs, api_key=None, companion_name=None):
    """
    Resolve many suspicious pairs in ONE LLM call (mirrors the batching pattern
    in updater.process_extracted_memories). Returns a dict {index: decision}
    for pairs that got a valid decision; missing indices count as errors.
    Falls back to an empty dict on total failure.
    """
    if not pairs:
        return {}
    c_name = companion_name or fallback_companion_name_for_prompts()
    system_prompt = prepend_memory_lens(
        CONSOLIDATION_PROMPT.format(companion_name=c_name, user_name=USER_NAME)
        + BATCH_RESOLVE_PROMPT_SUFFIX,
        c_name,
    )
    blocks = []
    for i, pair in enumerate(pairs):
        a, b = pair["mem_a"], pair["mem_b"]
        blocks.append(
            f"=== PAIR {i + 1} ===\n"
            f"MEMORY A:\nID: {a['id']}\nCategory: {a['category']}\nContent: {a['content']}\n"
            f"Confidence: {a['confidence']}%\nPriority: {a['priority']}\n"
            f"Reinforced: {a.get('reinforcement_count', 1)} times\n\n"
            f"MEMORY B:\nID: {b['id']}\nCategory: {b['category']}\nContent: {b['content']}\n"
            f"Confidence: {b['confidence']}%\nPriority: {b['priority']}\n"
            f"Reinforced: {b.get('reinforcement_count', 1)} times"
        )
    user_content = (
        f"Resolve these {len(pairs)} memory pairs:\n\n" + "\n\n".join(blocks)
    )
    BATCH_JSON_RETRIES = 1
    for attempt in range(1 + BATCH_JSON_RETRIES):
        raw_text, meta = run_pipeline_step(
            "updater", api_key, system_prompt, user_content,
            max_tokens=max(2000, 350 * len(pairs)), temperature=0.2,
        )
        if not raw_text or not meta:
            return {}
        raw_text = strip_json_fences(raw_text)
        try:
            arr = json.loads(raw_text)
            break
        except json.JSONDecodeError as e:
            print(f"  ✗ Batch resolve JSON parse failed (attempt {attempt + 1}): {e}")
            if attempt < BATCH_JSON_RETRIES:
                user_content += (
                    f"\n\nIMPORTANT: Your previous response was invalid JSON ({e}). "
                    f"Respond again with ONLY the valid JSON array."
                )
            else:
                return {}
    valid_actions = {"keep_both", "merge", "supersede"}
    out = {}
    if isinstance(arr, list):
        for item in arr:
            if not isinstance(item, dict):
                continue
            idx = item.get("pair")
            if isinstance(idx, int) and 1 <= idx <= len(pairs) and item.get("action") in valid_actions:
                out[idx - 1] = item
    return out


def apply_consolidation_decision(db, mem_a, mem_b, decision):
    """
    Apply a consolidation decision to the database.
    Returns a string describing what happened.
    """
    action = decision["action"]

    def _audit_source(mem):
        return {"memory_id": mem["id"], "category": mem.get("category"), "content": mem.get("content")}

    if action == "keep_both":
        return "kept both"

    if action == "merge":
        merged = decision.get("merged_content")
        if not merged:
            return "merge skipped (no merged_content)"
        keeper = mem_a if mem_a["confidence"] >= mem_b["confidence"] else mem_b
        loser = mem_b if keeper is mem_a else mem_a
        # Merge guard (MERGE_GUARD_ENABLED in memory_db.py): a refused merge keeps
        # both memories untouched. Loud + audited inside merge_guard.
        allowed, guard_reason = db.merge_guard(keeper["id"], merged, action="merge",
                                               incoming_len=len(loser.get("content") or ""))
        if not allowed:
            return f"merge refused by guard ({guard_reason}) \u2014 kept both"
        # Audit row written immediately before the merge commits; flipped to
        # 'failed' if the merge raises. Audit failure never blocks the merge.
        audit_id = db.write_audit_event(
            "consolidation",
            [_audit_source(mem_a), _audit_source(mem_b)],
            result_memory={"memory_id": keeper["id"], "category": keeper.get("category"), "content": merged},
            reason=f"merge: {decision.get('reasoning', '')}",
        )
        try:
            db.update_memory(keeper["id"], source="consolidation_merge", updates={
                "content": merged,
                "last_seen": datetime.now(timezone.utc).isoformat(),
            })
            db.boost_confidence(keeper["id"])
            db.update_memory(loser["id"], {"active": 0})
        except Exception:
            db.set_audit_status(audit_id, "failed")
            raise
        return f"merged into {keeper['id']}, archived {loser['id']}"

    if action == "supersede":
        keep_id = decision.get("keep_id")
        if keep_id not in (mem_a["id"], mem_b["id"]):
            return "supersede skipped (invalid keep_id)"
        archive_id = mem_b["id"] if keep_id == mem_a["id"] else mem_a["id"]
        keeper = mem_a if keep_id == mem_a["id"] else mem_b
        audit_id = db.write_audit_event(
            "consolidation",
            [_audit_source(mem_a), _audit_source(mem_b)],
            result_memory={"memory_id": keeper["id"], "category": keeper.get("category"), "content": keeper.get("content")},
            reason=f"supersede: {decision.get('reasoning', '')}",
        )
        try:
            db.update_memory(archive_id, {"active": 0})
            db.boost_confidence(keep_id)
        except Exception:
            db.set_audit_status(audit_id, "failed")
            raise
        return f"kept {keep_id}, archived {archive_id}"

    return "no action"


def _fetch_active_memory(db, mem_id):
    if not mem_id:
        return None
    row = db.db.execute(
        "SELECT * FROM memories WHERE id = ? AND active = 1", (mem_id,)
    ).fetchone()
    return dict(row) if row else None


def apply_selected_decisions(db, decisions):
    """
    Apply decisions that were previewed in a dry run (per-pair or batch from
    the UI). Re-fetches both memories fresh and skips any pair where either
    side is missing or already archived — previews can go stale.
    Reuses apply_consolidation_decision, so audit rows are written normally.
    """
    results = []
    for d in decisions or []:
        a = _fetch_active_memory(db, d.get("mem_a_id"))
        b = _fetch_active_memory(db, d.get("mem_b_id"))
        if not a or not b:
            results.append({"pair": [d.get("mem_a_id"), d.get("mem_b_id")],
                            "result": "skipped (memory missing or already archived)"})
            continue
        decision = {
            "action": d.get("action"),
            "merged_content": d.get("merged_content"),
            "keep_id": d.get("keep_id"),
            "reasoning": d.get("reasoning") or "applied from consolidation preview",
        }
        if decision["action"] not in ("keep_both", "merge", "supersede"):
            results.append({"pair": [a["id"], b["id"]], "result": "skipped (invalid action)"})
            continue
        try:
            res = apply_consolidation_decision(db, a, b, decision)
        except Exception as e:
            res = f"error: {e}"
        results.append({"pair": [a["id"], b["id"]], "action": decision["action"], "result": res})
    return results


def auto_promote(db, dry_run=False):
    """
    Promote highly reinforced memories to higher priority tiers.
    Pure rule-based — no LLM calls.

    - reinforcement_count >= 10 → promote to core
    - reinforcement_count >= 5  → promote to at least important
    - reinforcement_count >= 3  → promote to at least notable

    Returns dict with counts.
    """
    priority_ladder = ["minor", "notable", "important", "core"]
    memories = db.get_all_memories(active_only=True)

    stats = {"promoted": 0, "already_max": 0, "below_threshold": 0}

    for mem in memories:
        # Dream memories never climb the priority ladder — their priority is
        # set once at creation, by charge, and decays like everything else.
        if str(mem.get("id", "")).startswith("dreammem_") or mem.get("category") == "dream":
            continue
        rc = mem.get("reinforcement_count", 1) or 1
        current = mem.get("priority", "important")
        current_idx = priority_ladder.index(current) if current in priority_ladder else 2

        if AUTO_PROMOTE_CORE and rc >= 10 and current != "core":
            new_pri = "core"
        elif rc >= 5 and current_idx < 2:
            new_pri = "important"
        elif rc >= 3 and current_idx < 1:
            new_pri = "notable"
        else:
            if current == "core" or rc < 3:
                stats["below_threshold" if rc < 3 else "already_max"] += 1
            continue

        if not dry_run:
            db.update_memory(mem["id"], {"priority": new_pri})
        stats["promoted"] += 1

    return stats


def run_consolidation(db, api_key=None, max_pairs=50, threshold=0.3,
                      sample_size=None, dry_run=False, companion_name=None):
    """
    Full consolidation pass: find near-duplicates, resolve them, auto-promote.

    Args:
        db: MemoryDB instance
        api_key: Optional API key override
        max_pairs: Max LLM comparison calls this run (cost control)
        threshold: ChromaDB distance below which pairs are suspicious
        sample_size: How many memories to spot-check for duplicates
        dry_run: Preview without changing anything
        companion_name: Display name for LLM prompts

    Returns:
        Dict with stats
    """
    c_name = companion_name or db.companion_name.capitalize()
    mode = "DRY RUN" if dry_run else "LIVE"
    print(f"\n{'=' * 50}")
    print(f"🧹 Memory Consolidation ({mode}) — {c_name}")
    print(f"{'=' * 50}")

    stats = {
        "pairs_found": 0,
        "pairs_processed": 0,
        "keep_both": 0,
        "merged": 0,
        "superseded": 0,
        "errors": 0,
        "promotions": 0,
        "decisions": [],
    }

    # Phase 1: Find suspicious pairs
    print(f"\n🔍 Scanning {sample_size} memories for near-duplicates (threshold: {threshold})...")
    candidates = find_consolidation_candidates(db, threshold=threshold,
                                                sample_size=sample_size,
                                                max_pairs=max_pairs)
    stats["pairs_found"] = len(candidates)
    print(f"   Found {len(candidates)} suspicious pairs")

    if not candidates:
        print("   No near-duplicates found this run")
    else:
        # Phase 2: Resolve ALL pairs in one batched LLM call (mirrors the
        # updater's batching pattern — one call instead of one per pair).
        print(f"\n  ⚖️ Judging {len(candidates)} pairs in one batched call...")
        batch_decisions = resolve_pairs_batch(candidates, api_key=api_key, companion_name=c_name)
        for i, pair in enumerate(candidates):
            a = pair["mem_a"]
            b = pair["mem_b"]
            dist = pair["distance"]
            print(f"\n  [{i + 1}/{len(candidates)}] d={dist:.3f}")
            print(f"    A: {a['content'][:80]}...")
            print(f"    B: {b['content'][:80]}...")

            decision = batch_decisions.get(i)
            if not decision:
                stats["errors"] += 1
                continue

            if dry_run:
                # Judge but don't touch: the preview gets real verdicts so the
                # UI can show exactly what a live run would do to each pair.
                stats["pairs_processed"] += 1
                action = decision["action"]
                stats_key = {"merge": "merged", "supersede": "superseded"}.get(action, action)
                stats[stats_key if stats_key in stats else "errors"] += 1
                stats["decisions"].append({
                    "mem_a": {"id": a["id"], "content": a["content"], "category": a.get("category"), "priority": a.get("priority"), "confidence": a.get("confidence")},
                    "mem_b": {"id": b["id"], "content": b["content"], "category": b.get("category"), "priority": b.get("priority"), "confidence": b.get("confidence")},
                    "distance": dist,
                    "action": decision["action"],
                    "merged_content": decision.get("merged_content"),
                    "keep_id": decision.get("keep_id"),
                    "reasoning": decision.get("reasoning", ""),
                })
                print(f"    → would {decision['action']}: {decision.get('reasoning', '')[:60]}")
                continue

            result = apply_consolidation_decision(db, a, b, decision)
            action = decision["action"]
            if result.startswith("merge refused by guard"):
                action = "keep_both"  # MERGE_GUARD_ENABLED: count a refused merge as kept-both
            stats_key = {"merge": "merged", "supersede": "superseded"}.get(action, action)
            stats[stats_key if stats_key in stats else "errors"] += 1
            stats["pairs_processed"] += 1
            print(f"    → {action}: {decision.get('reasoning', '')[:60]}")
            print(f"    Applied: {result}")

    # Phase 3: Auto-promote reinforced memories
    print(f"\n📈 Auto-promoting reinforced memories...")
    promo_stats = auto_promote(db, dry_run=dry_run)
    stats["promotions"] = promo_stats["promoted"]
    print(f"   {'Would promote' if dry_run else 'Promoted'}: {promo_stats['promoted']}")

    print(f"\n{'=' * 50}")
    print(f"✓ Consolidation {'preview' if dry_run else 'complete'}")
    print(f"  Pairs: {stats['pairs_found']} found, {stats['pairs_processed']} processed")
    print(f"  Kept both: {stats['keep_both']}, Merged: {stats['merged']}, Superseded: {stats['superseded']}")
    print(f"  Promotions: {stats['promotions']}, Errors: {stats['errors']}")
    print(f"{'=' * 50}")

    db.log("consolidation_run", json.dumps(stats))
    return stats
