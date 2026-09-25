# © 2024-2026 Megan Neves. All rights reserved.
# Tanevan Memory System — https://github.com/Love-Encoded/Love-Refactored
# Licensed under the Love Refactored / Tanevan License. See LICENSE.md.
"""
Tanevan - Memory Pipeline
Orchestrates the full flow: Raw Conversation → Summary → Extraction → Update

This is what you call to process a conversation into memories.
Can be triggered manually, on a schedule, or by the proxy.
"""

from memory_db import MemoryDB
from summarizer import summarize_conversation
from extractor import extract_memories
from updater import process_extracted_memories
from companion_resolve import resolve_default_companion_name
from db_time import ensure_utc, parse_db_datetime
from pipeline_llm import PipelineStepError
import os
import json
from datetime import datetime


_NO_DEFAULT_COMPANION = (
    "No default companion: add one in Love Refactored (companions/) or set TANEVAN_COMPANION_NAME."
)


def _result_failure(error, *, status="failed", **extra):
    out = {
        "success": False,
        "summary_id": None,
        "memories_extracted": 0,
        "update_results": {},
        "cost_estimate": 0.0,
        "status": status,
        "error": error,
    }
    out.update(extra)
    return out


def process_session(messages, db=None, api_key=None, save_raw=True, companion_name=None, user_name=None, on_activity=None):
    """
    Full pipeline: take raw messages and turn them into structured memories.

    Args:
        messages: List of message dicts (role, content, optional timestamp)
        db: MemoryDB instance (creates one if not provided)
        api_key: Optional API key override
        save_raw: Whether to store the raw conversation text
        companion_name: Companion name for prompts (properly capitalized)
        user_name: User name for prompts

    Returns:
        Dict with summary, memories extracted, and update results
    """
    own_db = db is None
    if own_db:
        default_name = resolve_default_companion_name()
        if not default_name:
            print(f"✗ {_NO_DEFAULT_COMPANION}")
            return _result_failure(_NO_DEFAULT_COMPANION)
        db = MemoryDB(default_name)

    if companion_name is None:
        companion_name = db.companion_name.capitalize()

    if on_activity is None:
        on_activity = lambda t, d=None: None  # no-op when not in import context

    print("\n" + "=" * 60)
    print("🧠 LOVE REFACTORED - Processing Session")
    print(f"   {len(messages)} messages")
    print("=" * 60)

    result = {
        "success": False,
        "summary_id": None,
        "memories_extracted": 0,
        "update_results": {},
        "cost_estimate": 0.0
    }

    # =========================================
    # STEP 1: SUMMARIZE
    # =========================================
    print("\n📝 Step 1: Summarizing conversation...")
    try:
        summary = summarize_conversation(messages, api_key=api_key, companion_name=companion_name, user_name=user_name)
    except PipelineStepError as e:
        print("✗ Summarization failed. Aborting pipeline.")
        result["error"] = str(e)
        if own_db:
            db.close()
        return result

    if not summary:
        print("✗ Summarization failed. Aborting pipeline.")
        result["error"] = "Summarization failed — check Tanevan logs for LLM connection details"
        if own_db:
            db.close()
        return result

    if not save_raw:
        summary["raw_conversation"] = ""

    # Save summary to database
    summary_id = db.save_summary(summary)
    result["summary_id"] = summary_id
    print(f"   Saved as: {summary_id}")

    on_activity("summary_complete", {
        "summary_id": summary_id,
        "emotional_arc": summary.get("emotional_arc", ""),
        "topics": summary.get("topics", []),
        "key_moments_count": len(summary.get("key_moments", [])),
        "input_tokens": summary.get("_input_tokens"),
        "output_tokens": summary.get("_output_tokens"),
        "cost": summary.get("_cost"),
    })

    # =========================================
    # STEP 2: EXTRACT MEMORIES
    # =========================================
    print("\n🔍 Step 2: Extracting memories from summary...")
    try:
        extraction = extract_memories(summary, api_key=api_key, companion_name=companion_name, user_name=user_name)
    except PipelineStepError as e:
        print("✗ Extraction failed. Summary saved but no memories extracted.")
        result["success"] = False
        result["error"] = str(e)
        if own_db:
            db.close()
        return result

    if not extraction:
        print("✗ Extraction failed. Summary saved but no memories extracted.")
        # NOT success: callers and batch loops must see this as a failure.
        # (Previously reported success=True, which silently hid extraction
        # failures from failure logs.)
        result["success"] = False
        result["error"] = "Extraction step failed — summary was saved but no memories extracted"
        if own_db:
            db.close()
        return result

    memories = extraction["memories"]
    if not memories:
        print("✗ Extraction returned 0 memories.")
        result["success"] = True
        if own_db:
            db.close()
        return result

    result["memories_extracted"] = len(memories)

    on_activity("extraction_complete", {
        "count": len(memories),
        "categories": extraction.get("_categories", {}),
        "input_tokens": extraction.get("_input_tokens"),
        "output_tokens": extraction.get("_output_tokens"),
        "cost": extraction.get("_cost"),
    })

    # Tag each memory with its source summary and conversation time.
    # Use message timestamps when available; these are the actual conversation dates.
    # For batch imports without timestamps, date_start/date_end will be empty
    # and memories won't get a misleading date — better no date than a wrong one.
    conv_start = (summary.get("date_start") or "").strip()
    conv_end = (summary.get("date_end") or conv_start).strip()
    for mem in memories:
        mem["source_summary_id"] = summary_id
        if conv_start:
            mem["event_date"] = conv_start
            mem["first_seen"] = conv_start
            mem["last_seen"] = conv_end or conv_start
        # If no timestamps on messages, don't stamp event_date at all.
        # A missing date is better than a wrong one (import processing date).
        elif not mem.get("event_date"):
            mem["event_date"] = ""
            mem["first_seen"] = ""
            mem["last_seen"] = ""

    # =========================================
    # STEP 3: UPDATE/DEDUPLICATE
    # =========================================
    print(f"\n🔄 Step 3: Processing {len(memories)} memories through updater...")
    update_results = process_extracted_memories(memories, db, api_key=api_key, on_activity=on_activity)
    result["update_results"] = update_results

    # =========================================
    # DONE
    # =========================================
    stats = db.get_memory_stats()
    result["success"] = True

    print("\n" + "=" * 60)
    print("✓ PIPELINE COMPLETE")
    print(f"   Summary: {summary_id}")
    print(f"   Memories extracted: {result['memories_extracted']}")
    print(f"   Added: {update_results.get('add', 0)}")
    print(f"   Boosted: {update_results.get('boost', 0)}")
    print(f"   Updated: {update_results.get('update', 0)}")
    print(f"   Merged: {update_results.get('merge', 0)}")
    print(f"   Skipped: {update_results.get('skip', 0)}")
    print(f"   Total memories in dossier: {stats['total']}")
    print(f"   Total summaries: {stats['total_summaries']}")
    print("=" * 60)

    if own_db:
        db.close()

    return result


# Minimum gap (in seconds) between messages to consider a session boundary.
# 2 hours = a distinct conversation. Tunable via env var.
SESSION_GAP_SECONDS = int(os.environ.get("TANEVAN_SESSION_GAP_SECONDS", "7200"))

# Max approx tokens (chars/4) per session before it is split at a user-turn
# boundary. Long voice calls arrive as one continuous session; without this
# cap a 4-hour call gets ONE summary pass and detail is lost. Tunable.
SESSION_TOKEN_CAP = int(os.environ.get("TANEVAN_SESSION_TOKEN_CAP", "12000"))


def _split_session_by_tokens(session, token_cap=None, min_messages=4):
    """Split one session into token-capped chunks at user-turn boundaries.
    Guarantees no message loss: output message count must equal input."""
    cap = token_cap if token_cap is not None else SESSION_TOKEN_CAP
    if cap <= 0:
        return [session]
    chunks, cur, cur_tok = [], [], 0
    for m in session:
        t = len(m.get("content", "")) / 4
        if cur and cur_tok + t > cap and m.get("role") == "user" and len(cur) >= min_messages:
            chunks.append(cur)
            cur, cur_tok = [], 0
        cur.append(m)
        cur_tok += t
    if cur:
        if chunks and len(cur) < min_messages:
            chunks[-1].extend(cur)
        else:
            chunks.append(cur)
    total_in, total_out = len(session), sum(len(c) for c in chunks)
    if total_in != total_out:
        raise RuntimeError(
            f"_split_session_by_tokens message loss: {total_in} in vs {total_out} out")
    return chunks


def _split_buffer_into_sessions(messages, gap_seconds=SESSION_GAP_SECONDS, min_messages=4):
    """
    Split a list of messages into distinct sessions based on time gaps.
    If two consecutive messages are more than gap_seconds apart, that's a boundary.
    Segments smaller than min_messages get merged into the adjacent segment.
    Returns a list of message lists.
    """
    if not messages:
        return []

    segments = []
    current_segment = [messages[0]]

    for i in range(1, len(messages)):
        prev_ts = messages[i - 1].get("timestamp", "")
        curr_ts = messages[i].get("timestamp", "")

        gap = None
        if prev_ts and curr_ts:
            try:
                prev_dt = ensure_utc(parse_db_datetime(prev_ts))
                curr_dt = ensure_utc(parse_db_datetime(curr_ts))
                if prev_dt is None or curr_dt is None:
                    gap = None
                else:
                    gap = (curr_dt - prev_dt).total_seconds()
            except (ValueError, TypeError):
                pass

        if gap is not None and gap > gap_seconds:
            segments.append(current_segment)
            current_segment = [messages[i]]
        else:
            current_segment.append(messages[i])

    if current_segment:
        segments.append(current_segment)

    # Merge tiny segments into their neighbors
    merged = []
    for seg in segments:
        if merged and len(seg) < min_messages:
            merged[-1].extend(seg)
        else:
            merged.append(seg)

    # If the first segment ended up too small after merging, merge it forward
    if len(merged) > 1 and len(merged[0]) < min_messages:
        merged[1] = merged[0] + merged[1]
        merged.pop(0)

    return merged


def process_buffer(db=None, api_key=None, on_activity=None, min_messages=4, user_name=None):
    """
    Process whatever is currently in the conversation buffer.
    Called by the proxy on trigger events.
    Detects session boundaries (time gaps > 2 hours) and processes each
    distinct conversation separately for coherent emotional arcs.
    min_messages: auto-pipeline uses 4; manual flush from the app can use 1.
    user_name: Human display name for prompts (from Love Refactored persona); optional.
    """
    own_db = db is None
    if own_db:
        default_name = resolve_default_companion_name()
        if not default_name:
            print(_NO_DEFAULT_COMPANION)
            return _result_failure(_NO_DEFAULT_COMPANION)
        db = MemoryDB(default_name)

    buffer = db.get_buffer()
    # Snapshot the highest buffered id NOW: anything arriving after this point
    # belongs to the next cycle and must survive the post-run clear.
    snapshot_max_id = max((m.get("id") or 0 for m in buffer), default=None)

    if len(buffer) < min_messages:
        print(f"Buffer too small to process (need at least {min_messages} messages)")
        if own_db:
            db.close()
        return _result_failure(
            f"Buffer too small to process (need at least {min_messages} messages)",
            status="nothing_to_process",
            buffer_messages=len(buffer),
            min_required=min_messages,
        )

    # Convert buffer format to standard message format
    messages = [{"role": m["role"], "content": m["content"], "timestamp": m["timestamp"]}
                for m in buffer]

    # Split into distinct sessions based on time gaps
    sessions = _split_buffer_into_sessions(messages, min_messages=min_messages)

    # Then cap each session by token size (long voice calls -> multiple
    # summary passes, each with a full memory budget).
    capped = []
    for s in sessions:
        capped.extend(_split_session_by_tokens(s, min_messages=min_messages))
    if capped and len(capped) != len(sessions):
        print(f"   Token cap: {len(sessions)} session(s) -> {len(capped)} after splitting oversized sessions")
    sessions = capped
    companion_name = db.companion_name.capitalize()

    print(f"📋 Processing buffer: {len(buffer)} messages → {len(sessions)} session(s)")

    if not sessions:
        if own_db:
            db.close()
        return _result_failure(
            f"Buffer had {len(buffer)} messages but produced 0 sessions to process"
        )

    all_succeeded = True
    last_result = None
    last_failure = None
    errors = []

    for i, session_messages in enumerate(sessions):
        label = f"[{i + 1}/{len(sessions)}]" if len(sessions) > 1 else ""
        print(f"\n{'─' * 40} Session {label} ({len(session_messages)} messages)")

        try:
            result = process_session(
                session_messages,
                db=db,
                api_key=api_key,
                companion_name=companion_name,
                user_name=user_name,
                on_activity=on_activity,
            )
        except Exception as e:
            print(f"   ✗ Session {label} crashed: {e}")
            result = _result_failure(str(e))

        if result and result.get("success"):
            last_result = result
        else:
            all_succeeded = False
            last_failure = result if result else _result_failure("process_session returned None")
            err = (last_failure or {}).get("error")
            if err:
                errors.append(err)
            print(f"   ✗ Session {label} failed — continuing with remaining sessions")

    # Clear the buffer ONLY if every session succeeded. On partial failure the
    # buffer is kept so failed sessions can be reprocessed later — the updater
    # dedupes anything already processed, so retries can't duplicate memories.
    # (Previously: any one success cleared the buffer, permanently deleting the
    # messages of failed sibling sessions.)
    if last_result and last_result.get("success") and not all_succeeded:
        print("   ⚠️ Partial failure — buffer KEPT for reprocessing (updater dedupes on retry).")
    if last_result and last_result.get("success") and all_succeeded:
        db.clear_buffer(up_to_id=snapshot_max_id)
        session_word = f"{len(sessions)} session(s)" if len(sessions) > 1 else "1 session"
        db.log("buffer_processed",
               f"Processed {len(buffer)} messages as {session_word} → last summary: {last_result.get('summary_id')}")

    if own_db:
        db.close()

    if last_result and last_result.get("success"):
        return last_result

    failure = last_failure or _result_failure("Pipeline returned no result")
    if len(errors) > 1:
        failure = dict(failure)
        failure["error"] = f"{len(errors)} session(s) failed. Last error: {errors[-1]}"
        failure["errors"] = errors
    return failure


def process_file(filepath, db=None, api_key=None, companion_name=None, user_name=None):
    """
    Process a conversation from a JSON file.
    Useful for backfilling from exports.

    Expected format: list of {"role": "user/assistant", "content": "...", "timestamp": "..."}
    """
    print(f"\n📂 Loading conversation from: {filepath}")

    with open(filepath, 'r', encoding='utf-8') as f:
        messages = json.load(f)

    if not isinstance(messages, list):
        print("✗ File should contain a JSON array of messages")
        return None

    print(f"   Loaded {len(messages)} messages")
    return process_session(messages, db=db, api_key=api_key, companion_name=companion_name, user_name=user_name)


# === CLI Interface ===
if __name__ == "__main__":
    import sys

    print("\n" + "=" * 60)
    print("LOVE REFACTORED - Memory Pipeline")
    print("=" * 60)

    if len(sys.argv) > 1:
        # Process a file
        filepath = sys.argv[1]
        result = process_file(filepath)
    else:
        # Process the buffer
        result = process_buffer()

    if result and result.get("status") == "nothing_to_process":
        print("\nNothing to process.")
        print("\nUsage:")
        print("  python3 pipeline.py                    # Process conversation buffer")
        print("  python3 pipeline.py conversation.json  # Process a JSON file")
    elif result:
        print(f"\nResult: {'SUCCESS' if result.get('success') else 'FAILED'}")
        if result.get("error"):
            print(f"Error: {result['error']}")
    else:
        print("\nNothing to process.")
        print("\nUsage:")
        print("  python3 pipeline.py                    # Process conversation buffer")
        print("  python3 pipeline.py conversation.json  # Process a JSON file")
