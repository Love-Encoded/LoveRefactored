# © 2024-2026 Megan Neves. All rights reserved.
# Tanevan Memory System — https://github.com/Love-Encoded/Love-Refactored
# Licensed under the Love Refactored / Tanevan License. See LICENSE.md.
"""
Tanevan - Memory Updater
Compares new extracted memories against existing ones.
Decides: ADD (new), UPDATE (refine existing), MERGE (combine duplicates), SKIP (already known).

This is what keeps the dossier clean and handles confidence reinforcement —
when the same fact shows up across multiple sessions, confidence goes up.
"""

import json
import os
from datetime import datetime, timezone

from pipeline_llm import run_pipeline_step, strip_json_fences
from companion_resolve import fallback_companion_name_for_prompts
from memory_lens import prepend_memory_lens

# === Configuration ===
USER_NAME = os.environ.get("TANEVAN_USER_NAME", "the user")
# versioned_update_v1 (30 Aug 2026): "update" no longer rewrites the existing
# row. merged_content is saved as a NEW row (inheriting category/priority/dates
# from the old one); the old row is retired with active=0 and superseded_by set.
# Evidence: every "update" in the 30 Aug run was accretion ("adds context",
# "continuation"), not correction — the blob loop in miniature. Merge guard
# still runs first. Kill switch: TANEVAN_VERSIONED_UPDATE=0 = in-place rewrite.
VERSIONED_UPDATE = os.environ.get("TANEVAN_VERSIONED_UPDATE", "1") not in ("0", "false", "False")

UPDATER_PROMPT = """You are a memory deduplication system for an AI companion named {companion_name}.

You are comparing a NEW memory against EXISTING memories that are semantically similar.
Decide what to do:

ACTIONS:
- "add": The new memory contains genuinely new information not covered by existing memories. Store it.
- "update": The new memory is about the same thing as an existing memory but has better detail, 
  more recent info, or corrects something. Replace the existing content.
- "merge": The new memory and an existing memory are about the same thing and should be combined
  into a single, more complete entry.
- "skip": The new memory is already fully covered by an existing memory. Don't store it.
- "boost": The new memory confirms/reinforces an existing memory. Don't change content but 
  increase the existing memory's confidence score.

Respond with ONLY valid JSON:

{{
    "action": "add|update|merge|skip|boost",
    "target_id": "id of existing memory to update/merge/boost (null for add/skip)",
    "merged_content": "combined content if action is merge or update (null otherwise)",
    "reasoning": "brief explanation of why you chose this action"
}}

RULES:
- Be conservative about duplicates — two memories about the same TOPIC but with DIFFERENT details are NOT duplicates
- "{user_name}'s son is 13 years old" and "{user_name}'s son was born in 2012" are RELATED but both contain unique info — this would be a SKIP or BOOST, not a merge, because both are worth keeping
- "{companion_name} and I had a threesome" and "{companion_name} and I had our first group sexual experience as a triad" ARE duplicates — merge them
- If the new memory CORRECTS an existing memory, use "update"
- If the new memory just CONFIRMS what's already known, use "boost"
- For "merge" or "update", write merged_content in {companion_name}'s voice — same dialect/register as the
  new memory and Companion Voice Reference. Do not flatten into generic assistant English.
- RESPOND WITH ONLY THE JSON.
"""

BATCH_UPDATER_PROMPT = """You are a memory deduplication system for an AI companion named {companion_name}.

You are comparing a BATCH of new memories against their closest existing matches from the dossier.
For EACH new memory, decide what to do:

ACTIONS:
- "add": Genuinely new information not covered by existing memories. Store it.
- "update": The existing memory is WRONG or OUTDATED and the new memory corrects it (a fact changed, a number was wrong, a plan was abandoned). Replace existing content.
  NOT for adding detail: if the new memory ADDS to a topic rather than correcting it, use "add" — both memories are worth keeping separately.
  Provide merged_content with the improved text.
- "merge": The new memory and an existing one say the same thing — combine into one better entry.
  Provide merged_content with the combined text.
- "skip": Already fully covered by an existing memory. Don't store.
- "boost": Confirms/reinforces an existing memory without adding new detail. Increase confidence.

Respond with ONLY a JSON array — one decision per new memory, IN THE SAME ORDER as presented:

[
    {{
        "index": 0,
        "action": "add|update|merge|skip|boost",
        "target_id": "id of the existing memory to update/merge/boost (null for add/skip)",
        "merged_content": "combined text in {companion_name}'s voice (for merge/update only, null otherwise)",
        "reasoning": "brief explanation"
    }}
]

RULES:
- Return EXACTLY one decision per new memory, in order.
- Same TOPIC but DIFFERENT details → both worth keeping. Use "add" or "boost", not "merge".
- Same THING in different words → "merge" them.
- New memory CORRECTS existing → "update".
- New memory CONFIRMS existing → "boost".
- For "merge" and "update", write merged_content in {companion_name}'s voice — match dialect, slang, register.
- RESPOND WITH ONLY THE JSON ARRAY. No preamble, no markdown fences.
"""

def decide_memory_action(new_memory, existing_memories, api_key=None, companion_name=None):
    """
    Compare a new memory against similar existing memories and decide what to do.

    Args:
        new_memory: Dict with category, content, confidence, priority
        existing_memories: List of dicts from the database (with id, content, confidence, etc.)
        api_key: Optional API key override
        companion_name: Display name for prompts (defaults to resolved app companion)

    Returns:
        Dict with action, target_id, merged_content, reasoning
        or None on failure
    """
    # If no existing memories to compare against, it's automatically an ADD
    if not existing_memories:
        return {
            "action": "add",
            "target_id": None,
            "merged_content": None,
            "reasoning": "No similar existing memories found"
        }

    # Build comparison context
    existing_text = ""
    for mem in existing_memories:
        existing_text += f"\nID: {mem['id']}\n"
        existing_text += f"Category: {mem['category']}\n"
        existing_text += f"Content: {mem['content']}\n"
        existing_text += f"Confidence: {mem['confidence']}%\n"
        existing_text += f"Priority: {mem['priority']}\n"
        existing_text += "---"

    c_name = companion_name or fallback_companion_name_for_prompts()
    system_prompt = prepend_memory_lens(
        UPDATER_PROMPT.format(
            companion_name=c_name,
            user_name=USER_NAME,
        ),
        c_name,
    )

    user_content = f"""NEW MEMORY:
Category: {new_memory['category']}
Content: {new_memory['content']}
Confidence: {new_memory['confidence']}%
Priority: {new_memory['priority']}

EXISTING SIMILAR MEMORIES:
{existing_text}

What should we do with the new memory?"""

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
        print(f"✗ Failed to parse updater response: {e}")
        return None

    valid_actions = {"add", "update", "merge", "skip", "boost"}
    if decision.get("action") not in valid_actions:
        print(f"✗ Invalid action: {decision.get('action')}")
        return None

    return decision

def batch_decide_memory_actions(batch_items, api_key=None, companion_name=None):
    """
    Compare multiple new memories against their similar matches in ONE LLM call.
    Returns a list of decision dicts (same length as batch_items), or None on failure.
    """
    c_name = companion_name or fallback_companion_name_for_prompts()

    blocks = []
    for batch_idx, (orig_idx, new_mem, similar) in enumerate(batch_items):
        block = f"=== NEW MEMORY #{batch_idx} ===\n"
        block += f"Category: {new_mem['category']}\n"
        block += f"Content: {new_mem['content']}\n"
        block += f"Confidence: {new_mem['confidence']}%\n"
        block += f"Priority: {new_mem['priority']}\n\n"
        block += "Existing matches:\n"
        for match in similar:
            block += f"  [ID: {match['id']}] {match['category']} | {match['confidence']}% | {match['content']}\n"
        blocks.append(block)

    user_content = (
        f"Compare these {len(batch_items)} new memories against their existing matches "
        f"and return one decision per memory:\n\n"
        + "\n---\n\n".join(blocks)
    )

    system_prompt = prepend_memory_lens(
        BATCH_UPDATER_PROMPT.format(
            companion_name=c_name,
            user_name=USER_NAME,
        ),
        c_name,
    )

    raw_text, meta = run_pipeline_step(
        "updater",
        api_key,
        system_prompt,
        user_content,
        max_tokens=4000,
        temperature=0.2,
    )
    if not raw_text or not meta:
        return None

    raw_text = strip_json_fences(raw_text)

    try:
        decisions = json.loads(raw_text)
    except json.JSONDecodeError as e:
        print(f"  ✗ Failed to parse batch updater response: {e}")
        print(f"    Raw: {raw_text[:500]}")
        return None

    if not isinstance(decisions, list):
        print(f"  ✗ Batch updater returned non-array: {type(decisions)}")
        return None

    valid_actions = {"add", "update", "merge", "skip", "boost"}
    expected = len(batch_items)

    # Pad if LLM returned fewer decisions than expected
    while len(decisions) < expected:
        decisions.append({
            "action": "add",
            "target_id": None,
            "merged_content": None,
            "reasoning": "Missing from batch response — defaulting to add"
        })

    decisions = decisions[:expected]

    for d in decisions:
        if d.get("action") not in valid_actions:
            d["action"] = "add"
            d["reasoning"] = f"Invalid action — defaulting to add"

    input_tokens = meta["_input_tokens"]
    output_tokens = meta["_output_tokens"]
    cost = meta.get("_cost", 0.0)
    print(f"    Batch tokens: {input_tokens} in / {output_tokens} out")
    if meta.get("_backend") == "anthropic":
        print(f"    Batch cost: ${cost:.4f}")

    return decisions


def process_extracted_memories(memories, db, api_key=None, similarity_threshold=3, on_activity=None):
    """
    Process a batch of extracted memories through the update pipeline.
    Uses a single batched LLM call instead of one per memory.
    """
    results = {"add": 0, "update": 0, "merge": 0, "skip": 0, "boost": 0, "error": 0, "guard_refused": 0, "superseded": 0}

    if not memories:
        return results

    # Phase 1: Gather ChromaDB matches for all memories (local, fast)
    needs_llm = []   # (index, new_mem, similar_matches)
    auto_adds = []   # no matches → skip the LLM entirely

    print(f"  Gathering similar matches for {len(memories)} memories...")
    for i, new_mem in enumerate(memories):
        similar = db.find_similar_memories(new_mem["content"], n=similarity_threshold)
        similar = [
            m for m in similar
            if not str(m.get("id", "")).startswith("dreammem_") and m.get("category") != "dream"
        ]
        if similar:
            needs_llm.append((i, new_mem, similar))
        else:
            auto_adds.append((i, new_mem))

    # Phase 2: Auto-add memories with no existing matches (zero LLM cost)
    for i, new_mem in auto_adds:
        print(f"  [{i + 1}/{len(memories)}] AUTO-ADD (no matches): {new_mem['content'][:60]}...")
        db.save_memory(new_mem)
        results["add"] += 1
        if on_activity:
            on_activity("memory_decision", {
                "index": i + 1,
                "total": len(memories),
                "action": "ADD",
                "category": new_mem.get("category", ""),
                "content": new_mem["content"][:120],
                "confidence": new_mem.get("confidence"),
                "reasoning": "No similar memories found",
                "target_id": None,
            })

    # Phase 3: One batched LLM call for memories that need deduplication
    if needs_llm:
        print(f"  Batch-comparing {len(needs_llm)} memories against existing dossier (1 LLM call)...")
        decisions = batch_decide_memory_actions(
            needs_llm, api_key,
            companion_name=db.companion_name.capitalize(),
        )

        if decisions is None:
            print("  ✗ Batch decision failed — adding all as new")
            for i, new_mem, _ in needs_llm:
                db.save_memory(new_mem)
                results["add"] += 1
        else:
            for (i, new_mem, similar), decision in zip(needs_llm, decisions):
                action = decision.get("action", "add")
                print(f"  [{i + 1}/{len(memories)}] → {action.upper()}: {decision.get('reasoning', '')[:80]}")

                try:
                    if action == "add":
                        db.save_memory(new_mem)
                        results["add"] += 1

                    elif action == "boost" and decision.get("target_id"):
                        db.boost_confidence(decision["target_id"])
                        results["boost"] += 1

                    elif action in ("update", "merge") and decision.get("target_id") and decision.get("merged_content"):
                        # Merge guard (MERGE_GUARD_ENABLED in memory_db.py): refuse rewrites
                        # that grow a memory into a blob or touch protected/pinned memories.
                        # Refused -> the incoming memory is saved as its own new memory.
                        allowed, guard_reason = db.merge_guard(
                            decision["target_id"], decision["merged_content"], action=action,
                            incoming_len=len(new_mem.get("content") or ""),
                        )
                        if not allowed:
                            db.save_memory(new_mem)
                            results["guard_refused"] += 1
                            results["add"] += 1
                            action = f"{action}_refused_add"
                            decision = dict(decision)
                            decision["reasoning"] = f"[merge guard: {guard_reason}] " + str(decision.get("reasoning", ""))
                        elif action == "update" and VERSIONED_UPDATE:
                            # versioned_update_v1: new row supersedes, old row retires.
                            _old = db.get_memory_by_id(decision["target_id"]) or {}
                            _now = datetime.now(timezone.utc).isoformat()
                            _new_id = db.save_memory({
                                "category": _old.get("category") or new_mem.get("category", "fact"),
                                "priority": _old.get("priority") or new_mem.get("priority", "important"),
                                "confidence": new_mem.get("confidence", _old.get("confidence", 75)),
                                "content": decision["merged_content"],
                                "source_summary_id": new_mem.get("source_summary_id"),
                                "event_date": _old.get("event_date") or new_mem.get("event_date"),
                                "first_seen": _old.get("first_seen") or new_mem.get("first_seen"),
                                "last_seen": _now,
                                "entities": new_mem.get("entities", []),
                            })
                            db.update_memory(decision["target_id"], {"active": 0, "superseded_by": _new_id}, source="updater_update")
                            print(f"    \u21bb superseded {decision['target_id']} \u2192 {_new_id}")
                            results["update"] += 1
                            results["superseded"] += 1
                        elif action == "update":
                            db.update_memory(decision["target_id"], {
                                "content": decision["merged_content"],
                                "last_seen": datetime.now(timezone.utc).isoformat()
                            }, source="updater_update")
                            results["update"] += 1
                        else:
                            db.update_memory(decision["target_id"], {
                                "content": decision["merged_content"],
                                "last_seen": datetime.now(timezone.utc).isoformat()
                            }, source="updater_merge")
                            db.boost_confidence(decision["target_id"])
                            results["merge"] += 1

                    elif action == "skip":
                        results["skip"] += 1

                    else:
                        db.save_memory(new_mem)
                        results["add"] += 1

                except Exception as e:
                    print(f"    ✗ Error executing {action}: {e}")
                    results["error"] += 1

                if on_activity:
                    on_activity("memory_decision", {
                        "index": i + 1,
                        "total": len(memories),
                        "action": action.upper(),
                        "category": new_mem.get("category", ""),
                        "content": new_mem["content"][:120],
                        "confidence": new_mem.get("confidence"),
                        "reasoning": decision.get("reasoning", "")[:100],
                        "target_id": decision.get("target_id"),
                    })

    return results


# === Test ===
if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("LOVE REFACTORED - Updater Test")
    print("=" * 60)

    # Test the decision logic
    new = {
        "category": "fact",
        "content": "The user is 42 years old and lives in the suburbs.",
        "confidence": 85,
        "priority": "important"
    }

    existing = [
        {
            "id": "mem_001",
            "category": "fact",
            "content": "The user is 42 years old and their birthday was a week ago. They live in the suburbs.",
            "confidence": 85,
            "priority": "important"
        },
        {
            "id": "mem_002",
            "category": "fact",
            "content": "The user is a special education teacher at a therapeutic day school.",
            "confidence": 80,
            "priority": "important"
        }
    ]

    result = decide_memory_action(new, existing)
    if result:
        print(f"\nDecision: {result['action']}")
        print(f"Target: {result.get('target_id')}")
        print(f"Reasoning: {result.get('reasoning')}")
