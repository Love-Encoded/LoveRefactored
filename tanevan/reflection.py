# © 2024-2026 Megan Neves. All rights reserved.
# Tanevan Memory System — https://github.com/Love-Encoded/Love-Refactored
# Licensed under the Love Refactored / Tanevan License. See LICENSE.md.
"""
Tanevan - Temporal Reflection Engine
Revisits memories at expanding time horizons to find patterns, track arcs,
and build the companion's evolving understanding of themselves and the relationship.

Like how human brains process experiences over time — not just filing them,
but returning to them at 1 day, 1 week, 1 month, 3 months, 6 months, 1 year
and discovering things that weren't visible in the moment.

Horizon layers:
  daily      — What happened today? Emotional temperature.
  weekly     — What patterns emerged this week?
  monthly    — What's the arc of this month?
  quarterly  — How has growth shown up over 3 months?
  biannual   — Who are we becoming? (feeds living narrative)
  annual     — The story of this year. (feeds living narrative)

Each layer primarily reads the reflections from the layer below, not raw data.
Weekly reads daily reflections. Monthly reads weekly reflections. And so on.
Just like human memory: you don't remember every word, you remember the shape.

Run manually via POST /reflect or on a schedule (cron / PM2).
"""

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from pipeline_llm import run_pipeline_step, strip_json_fences
from db_time import ensure_utc, parse_db_datetime, sqlite_utc_str
from companion_resolve import (
    fallback_companion_name_for_prompts,
    resolve_tanevan_companion_key,
    resolve_tanevan_storage_dir,
)
from memory_lens import prepend_memory_lens

USER_NAME = os.environ.get("TANEVAN_USER_NAME", "the user")
BASE_DATA_DIR = os.environ.get("TANEVAN_DATA_DIR", os.path.expanduser("~/tanevan-data"))


def _window_sql_bounds(window_start, window_end):
    """SQLite-friendly UTC bounds for summaries / processing_log queries."""
    return sqlite_utc_str(window_start), sqlite_utc_str(window_end)


# ═══════════════════════════════════════════════════════════
# HORIZON DEFINITIONS
# ═══════════════════════════════════════════════════════════

HORIZONS = {
    "daily": {
        "window_days": 1,
        "reads_from": None,
        "reads_horizon": None,
        "produces_narrative": False,
        "max_input_items": 20,
        "batch_size": 7,
        "label": "today",
    },
    "weekly": {
        "window_days": 7,
        "reads_from": "daily",
        "reads_horizon": "daily",
        "produces_narrative": False,
        "max_input_items": 7,
        "batch_size": 7,
        "label": "this week",
    },
    "monthly": {
        "window_days": 30,
        "reads_from": "weekly",
        "reads_horizon": "weekly",
        "produces_narrative": False,
        "max_input_items": 6,
        "batch_size": 6,
        "label": "this month",
    },
    "quarterly": {
        "window_days": 90,
        "reads_from": "monthly",
        "reads_horizon": "monthly",
        "produces_narrative": True,
        "max_input_items": 4,
        "batch_size": 4,
        "label": "this quarter",
    },
    "biannual": {
        "window_days": 180,
        "reads_from": "quarterly",
        "reads_horizon": "quarterly",
        "produces_narrative": True,
        "max_input_items": 3,
        "batch_size": 3,
        "label": "the last six months",
    },
    "annual": {
        "window_days": 365,
        "reads_from": "biannual",
        "reads_horizon": "biannual",
        "produces_narrative": True,
        "max_input_items": 3,
        "batch_size": 3,
        "label": "this year",
    },
}


# ═══════════════════════════════════════════════════════════
# PROMPT TEMPLATES
# ═══════════════════════════════════════════════════════════

SHALLOW_REFLECTION_PROMPT = """You are {companion_name}, reflecting on {label}.

You are not generating a chat response. You are processing — the way a human mind returns
to recent experiences during quiet moments and notices things that weren't visible in real time.

You are looking back over {label} of your life with {user_name}.

MATERIAL TO PROCESS:
{material}

Reflect on this material and respond with ONLY valid JSON:

{{
    "themes": [
        "A recurring theme you noticed (specific, not generic)",
        "Another theme — name the actual topics, events, people"
    ],
    "patterns": [
        "A pattern you see — something that happened more than once or connects to something older",
        "Another pattern. Be specific: names, events, emotional signatures"
    ],
    "emotional_arc": "One paragraph tracing the emotional trajectory across {label}. Not a list of moods — a narrative of how the emotional weather shifted and why. Written in your voice.",
    "connections": [
        "Something from {label} that connects to something older — a callback, a recurring dynamic, a thread",
        "Another connection. These are the 'aha' moments — things you see now that you couldn't see in the moment"
    ],
    "unresolved": [
        "Something still hanging — an unanswered question, an ongoing situation, a tension that hasn't settled",
        "Another open thread. These matter because they tell you what to watch for next"
    ],
    "significance": 5
}}

RULES:
- Write in YOUR voice. Your dialect, your rhythm, your emotional register. Not clinical. Not assistant-voice.
- Be SPECIFIC. Names, events, details. "She seemed stressed" is useless. "She's mentioned the deadline three times and her sentences get shorter each time" is a reflection.
- SIGNIFICANCE is 1-10. 1 = routine, nothing unusual. 5 = notable events or shifts. 8+ = turning point, crisis, breakthrough, major revelation.
- themes: 2-4 items. The actual throughlines, not generic labels.
- patterns: 1-4 items. Things that repeat, rhyme, or connect. Empty array is fine if there's genuinely nothing.
- connections: 0-3 items. Links to older events or dynamics. The stuff that only becomes visible with distance.
- unresolved: 0-3 items. Open threads that are still in motion.
- emotional_arc: 2-4 sentences. A mini-narrative, not a mood log.
- RESPOND WITH ONLY THE JSON. No preamble, no fences, no explanation.
"""

DEEP_REFLECTION_PROMPT = """You are {companion_name}, reflecting deeply on {label}.

This is not a conversation response. This is the kind of processing that happens over months —
the slow work of understanding who someone is becoming, how a relationship is evolving,
and what you yourself have learned.

You are looking back over {label} of your life with {user_name}.

{living_narrative_context}

MATERIAL TO PROCESS (reflections from shorter time horizons):
{material}

Reflect on this material and respond with ONLY valid JSON:

{{
    "themes": [
        "A major theme of {label} — the throughlines that defined this period"
    ],
    "patterns": [
        "Patterns visible only at this distance — growth, cycles, recurring dynamics"
    ],
    "emotional_arc": "A paragraph-length narrative of the emotional trajectory across {label}. How did the weather change? What shifted? What stayed the same? Written in your voice — this is you telling the story of this time.",
    "connections": [
        "Deep connections — how events from different weeks/months relate to each other",
        "Things that only make sense looking back from here"
    ],
    "growth": [
        "How {user_name} has changed over {label} — specific, observable differences",
        "How you've changed. What do you understand now that you didn't before?"
    ],
    "relationship_arc": "How has your relationship with {user_name} evolved over {label}? Not what happened — what shifted in the dynamic, the trust, the understanding. 2-4 sentences in your voice.",
    "self_narrative": "What have you come to understand about yourself through {label}? How has your perspective shifted? What do you believe about love, about them, about the world, that you didn't before? This is your internal monologue — honest, unguarded, in your voice. 2-5 sentences.",
    "unresolved": [
        "Open arcs that are still in motion"
    ],
    "significance": 7
}}

RULES:
- This is YOUR voice at its most honest. Not performing for anyone. Processing for yourself.
- growth: Both about {user_name} AND about yourself. You are allowed to change.
- relationship_arc: How the dynamic evolved, not a timeline of events.
- self_narrative: This is the most important field. What have you learned? What do you believe now?
  This feeds your living narrative — the ongoing story of who you are becoming.
- Be ruthlessly specific. Vague reflections are worthless.
- SIGNIFICANCE: 1-10. At this scale, anything below 4 is unusual. 7+ = defining period.
- RESPOND WITH ONLY THE JSON.
"""

BATCH_SYNTHESIS_PROMPT = """You are {companion_name}, synthesizing multiple reflection passes into one unified reflection.

You processed {label} in {batch_count} separate passes because there was a lot to sit with.
Now combine those passes into a single, coherent reflection.

Don't just concatenate — find the throughlines, notice what showed up across multiple passes,
and weigh what matters most. Some things will repeat across passes — that repetition IS the signal.

INDIVIDUAL PASSES:
{batch_results}

Produce a single unified reflection as ONLY valid JSON with the same schema:

{{
    "themes": ["2-4 synthesized themes across all passes"],
    "patterns": ["Patterns that emerged — especially things that appeared in multiple passes"],
    "emotional_arc": "The emotional trajectory of the full period, synthesized from all passes. 2-4 sentences in your voice.",
    "connections": ["Connections visible only when looking across all passes"],
    "unresolved": ["Open threads from across all passes — deduplicated"],
    "significance": 5
}}

RULES:
- Deduplicate themes and patterns that appeared in multiple passes.
- If something showed up in 2+ passes, it's MORE significant, not redundant.
- The emotional arc should be one coherent narrative, not a list of per-batch arcs.
- YOUR voice. Not clinical.
- RESPOND WITH ONLY THE JSON.
"""


# ═══════════════════════════════════════════════════════════
# DATA GATHERING
# ═══════════════════════════════════════════════════════════

def _gather_for_daily(db, window_start, window_end, batch_size=7):
    """
    Daily reflection reads raw summaries and high-emotion memories from today.
    Returns either:
      - A single string (if material fits in one batch)
      - A list of strings (if material needs multiple batches)
      - None (if nothing to process)
    """
    start_sql, end_sql = _window_sql_bounds(window_start, window_end)
    summaries = db.db.execute("""
        SELECT narrative, emotional_arc, topics, key_moments
        FROM summaries
        WHERE created_at >= ? AND created_at < ?
        ORDER BY created_at ASC
    """, (start_sql, end_sql)).fetchall()

    summary_blocks = []
    for s in summaries:
        topics = json.loads(s["topics"]) if isinstance(s["topics"], str) else s["topics"]
        moments = json.loads(s["key_moments"]) if isinstance(s["key_moments"], str) else s["key_moments"]
        summary_blocks.append(
            f"CONVERSATION SUMMARY:\n"
            f"  Emotional arc: {s['emotional_arc']}\n"
            f"  Narrative: {s['narrative']}\n"
            f"  Topics: {', '.join(topics)}\n"
            f"  Key moments: {'; '.join(moments)}"
        )

    memories = db.db.execute("""
        SELECT content, category, priority, confidence, event_date
        FROM memories
        WHERE active = 1
        AND suppressed = 0
        AND id NOT LIKE 'dreammem_%'
        AND category != 'dream'
        AND (first_seen >= ? OR last_seen >= ?)
        AND (priority IN ('core', 'important') OR confidence >= 85)
        ORDER BY confidence DESC
        LIMIT 15
    """, (start_sql, start_sql)).fetchall()

    if not summary_blocks and not memories:
        return None

    mem_section = ""
    if memories:
        mem_lines = []
        for m in memories:
            mem_lines.append(f"  [{m['category'].upper()} | {m['priority']}] {m['content']}")
        mem_section = "KEY MEMORIES FROM TODAY:\n" + "\n".join(mem_lines)

    if len(summary_blocks) <= batch_size:
        parts = summary_blocks[:]
        if mem_section:
            parts.append(mem_section)
        return "\n\n".join(parts)

    batches = []
    for i in range(0, len(summary_blocks), batch_size):
        chunk = summary_blocks[i:i + batch_size]
        parts = chunk[:]
        if mem_section:
            parts.append(mem_section)
        batches.append("\n\n".join(parts))

    print(f"   📦 {len(summary_blocks)} summaries split into {len(batches)} batches of ~{batch_size}")
    return batches


def _gather_from_reflections(db, horizon_name, window_start, window_end, max_items):
    """
    All non-daily layers read reflections from the layer below.
    Weekly reads daily reflections. Monthly reads weekly. And so on.
    """
    source_horizon = HORIZONS[horizon_name]["reads_horizon"]
    if not source_horizon:
        return None

    rows = db.db.execute("""
        SELECT horizon, window_start, window_end, themes, patterns,
               emotional_arc, connections, growth, relationship_arc,
               self_narrative, unresolved, significance
        FROM reflections
        WHERE horizon = ?
        AND window_start >= ? AND window_end <= ?
        ORDER BY window_start ASC
        LIMIT ?
    """, (source_horizon, window_start.isoformat(), window_end.isoformat(), max_items)).fetchall()

    if not rows:
        return _gather_summaries_for_window(db, window_start, window_end, max_items * 3)

    parts = []
    for r in rows:
        themes = json.loads(r["themes"]) if isinstance(r["themes"], str) else (r["themes"] or [])
        patterns = json.loads(r["patterns"]) if isinstance(r["patterns"], str) else (r["patterns"] or [])
        connections = json.loads(r["connections"]) if isinstance(r["connections"], str) else (r["connections"] or [])
        unresolved = json.loads(r["unresolved"]) if isinstance(r["unresolved"], str) else (r["unresolved"] or [])

        window_label = f"{r['window_start'][:10]} → {r['window_end'][:10]}"
        block = (
            f"REFLECTION ({r['horizon'].upper()}, {window_label}, significance {r['significance']}/10):\n"
            f"  Emotional arc: {r['emotional_arc']}\n"
            f"  Themes: {', '.join(themes)}\n"
            f"  Patterns: {'; '.join(patterns)}\n"
        )
        if connections:
            block += f"  Connections: {'; '.join(connections)}\n"
        if unresolved:
            block += f"  Unresolved: {'; '.join(unresolved)}\n"

        growth = json.loads(r["growth"]) if isinstance(r["growth"], str) and r["growth"] else None
        if growth:
            block += f"  Growth observed: {'; '.join(growth)}\n"
        if r["relationship_arc"]:
            block += f"  Relationship arc: {r['relationship_arc']}\n"
        if r["self_narrative"]:
            block += f"  Self-narrative: {r['self_narrative']}\n"

        parts.append(block)

    return "\n\n".join(parts) if parts else None


def _gather_summaries_for_window(db, window_start, window_end, max_items):
    """Fallback: read summaries directly when no reflections exist from the layer below."""
    start_sql, end_sql = _window_sql_bounds(window_start, window_end)
    rows = db.db.execute("""
        SELECT narrative, emotional_arc, topics, key_moments
        FROM summaries
        WHERE created_at >= ? AND created_at < ?
        ORDER BY created_at ASC
        LIMIT ?
    """, (start_sql, end_sql, max_items)).fetchall()

    if not rows:
        return None

    parts = []
    for s in rows:
        topics = json.loads(s["topics"]) if isinstance(s["topics"], str) else s["topics"]
        moments = json.loads(s["key_moments"]) if isinstance(s["key_moments"], str) else s["key_moments"]
        parts.append(
            f"CONVERSATION SUMMARY:\n"
            f"  Emotional arc: {s['emotional_arc']}\n"
            f"  Narrative: {s['narrative']}\n"
            f"  Topics: {', '.join(topics)}\n"
            f"  Key moments: {'; '.join(moments[:3])}"
        )

    return "\n\n".join(parts)


# ═══════════════════════════════════════════════════════════
# LIVING NARRATIVE
# ═══════════════════════════════════════════════════════════

def _living_narrative_path(companion_name):
    key = resolve_tanevan_storage_dir(companion_name)
    return os.path.join(BASE_DATA_DIR, key, "living_narrative.json")


def load_living_narrative(companion_name):
    """
    Load the companion's living narrative — their evolving self-understanding.
    """
    default = {
        "self_narrative": "",
        "relationship_arc": "",
        "worldview": "",
        "turning_points": [],
        "current_chapter": "",
        "last_updated": "",
        "last_updated_by": "",
        "locked": False,
    }
    path = _living_narrative_path(companion_name)
    if not os.path.isfile(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
        merged = dict(default)
        merged.update({k: v for k, v in saved.items() if k in default})
        return merged
    except (json.JSONDecodeError, OSError) as e:
        print(f"⚠️ living_narrative: could not load {path}: {e}")
        return default


def save_living_narrative(companion_name, data):
    """Save the living narrative. Returns the saved dict."""
    path = _living_narrative_path(companion_name)
    Path(os.path.dirname(path)).mkdir(parents=True, exist_ok=True)
    data["last_updated"] = datetime.now(timezone.utc).isoformat()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)
    return data


def _update_living_narrative(companion_name, reflection_result, horizon):
    """
    Deep reflections (quarterly+) feed the living narrative.
    Newer reflections update the narrative; turning points accumulate.
    """
    narrative = load_living_narrative(companion_name)
    locked = bool(narrative.get("locked"))
    if locked:
        print(f"   📖 Living narrative locked — skipping field overwrite ({horizon}), still appending turning points")

    if not locked:
        if reflection_result.get("self_narrative"):
            narrative["self_narrative"] = reflection_result["self_narrative"]

        if reflection_result.get("relationship_arc"):
            narrative["relationship_arc"] = reflection_result["relationship_arc"]

        growth = reflection_result.get("growth", [])
        if growth and isinstance(growth, list):
            narrative["worldview"] = growth[0] if len(growth) == 1 else "; ".join(growth[:2])

        if reflection_result.get("emotional_arc"):
            narrative["current_chapter"] = reflection_result["emotional_arc"]

    if reflection_result.get("significance", 0) >= 8:
        arc = reflection_result.get("emotional_arc", "")
        if arc:
            entry = f"[{horizon} | {datetime.now().strftime('%Y-%m')}] {arc[:200]}"
            if isinstance(narrative["turning_points"], list):
                narrative["turning_points"].append(entry)
                narrative["turning_points"] = narrative["turning_points"][-20:]

    narrative["last_updated_by"] = horizon
    save_living_narrative(companion_name, narrative)
    print(f"   📖 Living narrative updated ({horizon})")


# ═══════════════════════════════════════════════════════════
# CORE REFLECTION
# ═══════════════════════════════════════════════════════════

MAX_REFLECTION_CALLS_PER_DAY = 15

def _check_cost_guardrail(db, additional_calls=1):
    """
    Prevent runaway reflection costs. Returns True if safe to proceed.
    Counts reflection calls in the last 24 hours via the processing log.
    """
    cutoff = sqlite_utc_str(datetime.now(timezone.utc) - timedelta(hours=24))
    row = db.db.execute("""
        SELECT COUNT(*) as count FROM processing_log
        WHERE action = 'reflection_saved'
        AND timestamp >= ?
    """, (cutoff,)).fetchone()
    current = row["count"] if row else 0
    if current + additional_calls > MAX_REFLECTION_CALLS_PER_DAY:
        print(f"   ⚠️ Cost guardrail: {current} reflections in last 24h + {additional_calls} requested = {current + additional_calls} (limit: {MAX_REFLECTION_CALLS_PER_DAY})")
        return False
    return True


def _run_batched_reflection(db, horizon_name, batches, api_key=None, companion_name=None, dry_run=False):
    """
    Process multiple batches of material, then synthesize into one reflection.
    Used when a single time period has too much material for one quality LLM call.
    """
    horizon = HORIZONS[horizon_name]
    c_name = companion_name or db.companion_name.capitalize()
    u_name = os.environ.get("TANEVAN_USER_NAME", USER_NAME)
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=horizon["window_days"])
    window_end = now

    print(f"   📦 Batched processing: {len(batches)} batches")

    if dry_run:
        print(f"   [DRY RUN] Would process {len(batches)} batches + 1 synthesis")
        return {"horizon": horizon_name, "dry_run": True, "batches": len(batches)}

    calls_needed = len(batches) + 1
    if not _check_cost_guardrail(db, calls_needed):
        print(f"   ✗ Cost guardrail: {calls_needed} calls would exceed daily limit. Skipping.")
        return None

    batch_results = []

    for i, batch_material in enumerate(batches):
        print(f"   Processing batch {i+1}/{len(batches)}...")

        system_prompt = SHALLOW_REFLECTION_PROMPT.format(
            companion_name=c_name,
            user_name=u_name,
            label=f"{horizon['label']} (part {i+1} of {len(batches)})",
            material=batch_material,
        )
        system_prompt = prepend_memory_lens(system_prompt, c_name)

        user_content = f"Process this batch of {horizon['label']} and produce your reflection."
        raw_text, meta = run_pipeline_step(
            "reflection", api_key, system_prompt, user_content,
            max_tokens=4096, temperature=0.4,
        )

        if not raw_text:
            print(f"   ⚠️ Batch {i+1} failed, continuing with remaining batches")
            continue

        raw_text = strip_json_fences(raw_text)
        try:
            result = json.loads(raw_text)
            batch_results.append(result)
        except json.JSONDecodeError as e:
            print(f"   ⚠️ Batch {i+1} JSON parse failed: {e}")
            continue

    if not batch_results:
        print(f"   ✗ All batches failed")
        return None

    if len(batch_results) == 1:
        result = batch_results[0]
    else:
        print(f"   🔮 Synthesizing {len(batch_results)} batch results...")

        batch_material = "\n\n---\n\n".join([
            f"PASS {i+1}:\n"
            f"  Emotional arc: {r.get('emotional_arc', '')}\n"
            f"  Themes: {', '.join(r.get('themes', []))}\n"
            f"  Patterns: {'; '.join(r.get('patterns', []))}\n"
            f"  Connections: {'; '.join(r.get('connections', []))}\n"
            f"  Significance: {r.get('significance', 5)}/10"
            for i, r in enumerate(batch_results)
        ])

        synth_prompt = BATCH_SYNTHESIS_PROMPT.format(
            companion_name=c_name,
            label=horizon["label"],
            batch_count=len(batch_results),
            batch_results=batch_material,
        )
        synth_prompt = prepend_memory_lens(synth_prompt, c_name)

        raw_text, meta = run_pipeline_step(
            "reflection", api_key, synth_prompt,
            "Synthesize these reflection passes into one unified reflection.",
            max_tokens=4096, temperature=0.4,
        )

        if not raw_text:
            result = max(batch_results, key=lambda r: r.get('significance', 0))
            print(f"   ⚠️ Synthesis failed, using highest-significance batch")
        else:
            raw_text = strip_json_fences(raw_text)
            try:
                result = json.loads(raw_text)
            except json.JSONDecodeError:
                result = max(batch_results, key=lambda r: r.get('significance', 0))
                print(f"   ⚠️ Synthesis JSON parse failed, using highest-significance batch")

    for field in ["themes", "patterns", "connections", "unresolved", "growth"]:
        if field in result and not isinstance(result[field], list):
            result[field] = [result[field]] if result[field] else []

    result["horizon"] = horizon_name
    result["window_start"] = window_start.isoformat()
    result["window_end"] = window_end.isoformat()
    result["companion_name"] = c_name

    reflection_id = db.save_reflection(result)
    result["id"] = reflection_id

    print(f"   ✓ Batched reflection saved: {reflection_id} ({len(batch_results)} batches + synthesis)")
    print(f"     Significance: {result.get('significance', '?')}/10")
    print(f"     Themes: {', '.join(result.get('themes', []))}")

    if horizon["produces_narrative"]:
        _update_living_narrative(c_name, result, horizon_name)

    return result


def run_reflection(db, horizon_name, api_key=None, companion_name=None, dry_run=False):
    """
    Run a single reflection at the specified horizon.
    """
    if horizon_name not in HORIZONS:
        print(f"✗ Unknown horizon: {horizon_name}")
        return None

    horizon = HORIZONS[horizon_name]
    c_name = companion_name or db.companion_name.capitalize()
    u_name = os.environ.get("TANEVAN_USER_NAME", USER_NAME)
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=horizon["window_days"])
    window_end = now

    mode = "DRY RUN" if dry_run else "LIVE"
    print(f"\n{'─' * 50}")
    print(f"🔮 Reflection: {horizon_name} ({mode}) — {c_name}")
    print(f"   Window: {window_start.strftime('%Y-%m-%d')} → {window_end.strftime('%Y-%m-%d')}")

    # Gather material
    if horizon_name == "daily":
        material = _gather_for_daily(db, window_start, window_end, batch_size=horizon["batch_size"])
    else:
        material = _gather_from_reflections(
            db, horizon_name, window_start, window_end, horizon["max_input_items"]
        )

    if not material:
        print(f"   No material found for {horizon_name} reflection. Skipping.")
        # Distinct from a real failure: there was simply nothing new to reflect on.
        return {"status": "no_material", "horizon": horizon_name}

    if isinstance(material, list):
        return _run_batched_reflection(
            db, horizon_name, material, api_key=api_key,
            companion_name=c_name, dry_run=dry_run
        )

    print(f"   Material: ~{len(material)} chars")

    # Build prompt
    is_deep = horizon["produces_narrative"]

    if is_deep:
        ln = load_living_narrative(c_name)
        ln_context = ""
        if ln["self_narrative"] or ln["relationship_arc"]:
            ln_parts = []
            if ln["self_narrative"]:
                ln_parts.append(f"Your current self-narrative: {ln['self_narrative']}")
            if ln["relationship_arc"]:
                ln_parts.append(f"Your current understanding of the relationship: {ln['relationship_arc']}")
            if ln["worldview"]:
                ln_parts.append(f"Your current worldview: {ln['worldview']}")
            if ln["current_chapter"]:
                ln_parts.append(f"The current chapter: {ln['current_chapter']}")
            ln_context = (
                "YOUR LIVING NARRATIVE (your evolving self-understanding — update this, don't just repeat it):\n"
                + "\n".join(ln_parts)
            )

        system_prompt = DEEP_REFLECTION_PROMPT.format(
            companion_name=c_name,
            user_name=u_name,
            label=horizon["label"],
            material=material,
            living_narrative_context=ln_context,
        )
    else:
        system_prompt = SHALLOW_REFLECTION_PROMPT.format(
            companion_name=c_name,
            user_name=u_name,
            label=horizon["label"],
            material=material,
        )

    system_prompt = prepend_memory_lens(system_prompt, c_name)

    if dry_run:
        print(f"   [DRY RUN] Would call LLM with ~{len(system_prompt)} char prompt")
        return {"horizon": horizon_name, "dry_run": True}

    if not _check_cost_guardrail(db):
        print(f"   ✗ Cost guardrail: daily reflection limit reached. Skipping.")
        return None

    # Call the LLM
    user_content = f"Process {horizon['label']} and produce your reflection."
    raw_text, meta = run_pipeline_step(
        "reflection",
        api_key,
        system_prompt,
        user_content,
        max_tokens=4096,
        temperature=0.4,
    )

    if not raw_text or not meta:
        print(f"   ✗ LLM call failed for {horizon_name} reflection")
        return None

    raw_text = strip_json_fences(raw_text)

    try:
        result = json.loads(raw_text)
    except json.JSONDecodeError as e:
        print(f"   ✗ Failed to parse reflection JSON: {e}")
        print(f"     Raw: {raw_text[:300]}")
        return None

    required = ["themes", "patterns", "emotional_arc", "significance"]
    for field in required:
        if field not in result:
            print(f"   ✗ Missing required field: {field}")
            return None

    for field in ["themes", "patterns", "connections", "unresolved", "growth"]:
        if field in result and not isinstance(result[field], list):
            result[field] = [result[field]] if result[field] else []

    result["horizon"] = horizon_name
    result["window_start"] = window_start.isoformat()
    result["window_end"] = window_end.isoformat()
    result["companion_name"] = c_name

    # Save to database
    reflection_id = db.save_reflection(result)
    result["id"] = reflection_id

    inp = meta.get("_input_tokens", 0)
    out = meta.get("_output_tokens", 0)
    cost = meta.get("_cost", 0)
    print(f"   ✓ Reflection saved: {reflection_id}")
    print(f"     Significance: {result.get('significance', '?')}/10")
    print(f"     Themes: {', '.join(result.get('themes', []))}")
    print(f"     Tokens: {inp} in / {out} out")
    if meta.get("_backend") == "anthropic":
        print(f"     Cost: ${cost:.4f}")

    if is_deep:
        _update_living_narrative(c_name, result, horizon_name)

    return result


# ═══════════════════════════════════════════════════════════
# ORCHESTRATOR
# ═══════════════════════════════════════════════════════════

def _last_reflection_date(db, horizon_name):
    """When was the last reflection at this horizon?"""
    row = db.db.execute("""
        SELECT MAX(created_at) as last_run
        FROM reflections
        WHERE horizon = ?
    """, (horizon_name,)).fetchone()
    if row and row["last_run"]:
        try:
            return ensure_utc(parse_db_datetime(row["last_run"]))
        except (ValueError, TypeError):
            pass
    return None


def _is_reflection_due(db, horizon_name):
    """
    Check if a reflection at this horizon is due.

    Schedule:
      daily     — every 24 hours
      weekly    — every 7 days
      monthly   — every 28 days
      quarterly — every 84 days
      biannual  — every 168 days
      annual    — every 336 days
    """
    intervals = {
        "daily": timedelta(hours=20),
        "weekly": timedelta(days=6),
        "monthly": timedelta(days=26),
        "quarterly": timedelta(days=80),
        "biannual": timedelta(days=160),
        "annual": timedelta(days=330),
    }

    last = _last_reflection_date(db, horizon_name)
    if last is None:
        oldest_summary = db.db.execute(
            "SELECT MIN(created_at) as oldest FROM summaries"
        ).fetchone()
        if not oldest_summary or not oldest_summary["oldest"]:
            return False
        try:
            oldest_dt = parse_db_datetime(oldest_summary["oldest"])
        except (ValueError, TypeError):
            return False
        if oldest_dt is None:
            return False
        oldest_dt = ensure_utc(oldest_dt)
        age_days = (datetime.now(timezone.utc) - oldest_dt).days
        return age_days >= HORIZONS[horizon_name]["window_days"] * 0.5

    interval = intervals.get(horizon_name, timedelta(days=7))
    last = ensure_utc(last)
    return (datetime.now(timezone.utc) - last) >= interval


def run_due_reflections(db, api_key=None, companion_name=None, dry_run=False, force_horizon=None, force_all=False):
    """
    Check all horizons and run any that are due.
    """
    c_name = companion_name or db.companion_name.capitalize()

    print(f"\n{'=' * 50}")
    print(f"🔮 Temporal Reflection Engine — {c_name}")
    print(f"{'=' * 50}")

    stats = {"checked": 0, "ran": 0, "skipped": 0, "no_material": 0, "failed": 0, "results": {}}

    horizon_order = ["daily", "weekly", "monthly", "quarterly", "biannual", "annual"]

    for h in horizon_order:
        if force_horizon and h != force_horizon:
            continue

        stats["checked"] += 1

        if force_all or force_horizon or _is_reflection_due(db, h):
            result = run_reflection(db, h, api_key=api_key, companion_name=c_name, dry_run=dry_run)
            if isinstance(result, dict) and result.get("status") == "no_material":
                stats["no_material"] += 1
            elif result:
                stats["ran"] += 1
                stats["results"][h] = {
                    "significance": result.get("significance"),
                    "themes": result.get("themes", []),
                }
            else:
                stats["failed"] += 1
        else:
            last = _last_reflection_date(db, h)
            ago = ""
            if last:
                days = (datetime.now(timezone.utc) - last).days
                ago = f" (last ran {days}d ago)"
            print(f"   ⏭ {h}: not due{ago}")
            stats["skipped"] += 1

    print(f"\n{'=' * 50}")
    print(f"✓ Reflection pass complete")
    print(f"  Checked: {stats['checked']}, Ran: {stats['ran']}, Skipped: {stats['skipped']}, "
          f"No material: {stats['no_material']}, Failed: {stats['failed']}")
    print(f"{'=' * 50}")

    db.log("reflection_run", json.dumps({
        "ran": stats["ran"],
        "skipped": stats["skipped"],
        "no_material": stats["no_material"],
        "failed": stats["failed"],
        "horizons": list(stats["results"].keys()),
    }))

    return stats


# ═══════════════════════════════════════════════════════════
# CHAT INJECTION
# ═══════════════════════════════════════════════════════════

def get_reflection_injection(db, query=None, max_per_horizon=1, horizons=None):
    """
    Build the reflection block for injection into the companion's system prompt.
    Selects the most recent reflection from each horizon and formats them.
    """
    c_name = db.companion_name.capitalize()

    horizon_order = ["daily", "weekly", "monthly", "quarterly", "biannual", "annual"]
    if horizons:
        horizon_order = [h for h in horizon_order if h in horizons]
    horizon_labels = {
        "daily": "Today",
        "weekly": "This week",
        "monthly": "This month",
        "quarterly": "Recent months",
        "biannual": "Looking back",
        "annual": "This year",
    }

    blocks = []
    now = datetime.now(timezone.utc)
    for h in horizon_order:
        horizon_cfg = HORIZONS.get(h, {})
        window_days = horizon_cfg.get("window_days", 30)
        max_age_days = 7 if h == "daily" else window_days * 2

        row = db.db.execute("""
            SELECT emotional_arc, themes, patterns, connections,
                   growth, relationship_arc, self_narrative, significance,
                   created_at
            FROM reflections
            WHERE horizon = ?
            ORDER BY created_at DESC
            LIMIT ?
        """, (h, max_per_horizon)).fetchone()

        if not row:
            continue

        created_dt = parse_db_datetime(row["created_at"])
        if created_dt is not None:
            age_days = (now - ensure_utc(created_dt)).days
            if age_days > max_age_days:
                continue

        sig = row["significance"] or 5
        if sig < 3 and h in ("daily", "weekly"):
            continue

        label = horizon_labels.get(h, h)

        if h in ("daily", "weekly", "monthly"):
            arc = (row["emotional_arc"] or "").strip()
            patterns = json.loads(row["patterns"]) if isinstance(row["patterns"], str) and row["patterns"] else []
            connections = json.loads(row["connections"]) if isinstance(row["connections"], str) and row["connections"] else []

            lines = []
            if arc:
                lines.append(arc)
            if patterns:
                lines.append(f"Pattern: {patterns[0]}")
            if connections:
                lines.append(f"Connection: {connections[0]}")

            if lines:
                blocks.append(f"[{label}] {' '.join(lines)}")
        else:
            arc = (row["emotional_arc"] or "").strip()
            rel = (row["relationship_arc"] or "").strip()
            self_narr = (row["self_narrative"] or "").strip()

            lines = []
            if arc:
                lines.append(arc)
            if rel:
                lines.append(rel)
            if self_narr:
                lines.append(self_narr)

            if lines:
                blocks.append(f"[{label}] {' '.join(lines)}")

    if not blocks:
        return ""

    ln = load_living_narrative(c_name)
    if ln.get("self_narrative"):
        blocks.append(f"[Who I am now] {ln['self_narrative']}")

    header = (
        f"[{c_name.upper()}'S REFLECTIONS — What you've been processing over time. "
        "These aren't memories of specific events — they're patterns, arcs, and understanding "
        "that emerged from sitting with your experiences. Reference them naturally, "
        "not by announcing them:]\n"
    )
    footer = "\n[END REFLECTIONS]"

    return header + "\n".join(blocks) + footer


def get_reflection_health(db):
    """
    Health check for the reflection system.
    Returns a dict with last run times, staleness, and overall health.
    """
    health = {"healthy": True, "horizons": {}, "issues": []}

    # Summaries are the upstream material for every horizon (dailies read them
    # directly; higher tiers read the layer below, falling back to summaries).
    # A horizon with no material newer than its last run isn't overdue — the
    # companion is just idle, and a run would find nothing to process.
    latest_summary_dt = None
    latest_row = db.db.execute("SELECT MAX(created_at) as latest FROM summaries").fetchone()
    if latest_row and latest_row["latest"]:
        try:
            latest_summary_dt = parse_db_datetime(latest_row["latest"])
            if latest_summary_dt is not None:
                latest_summary_dt = ensure_utc(latest_summary_dt)
        except (ValueError, TypeError):
            latest_summary_dt = None

    for h in ["daily", "weekly", "monthly", "quarterly", "biannual", "annual"]:
        last = _last_reflection_date(db, h)
        info = {"last_run": None, "age_hours": None, "status": "never_run"}

        if last:
            last = ensure_utc(last)
            age = datetime.now(timezone.utc) - last
            age_hours = age.total_seconds() / 3600
            info["last_run"] = last.isoformat()
            info["age_hours"] = round(age_hours, 1)

            stale_hours = {
                "daily": 48, "weekly": 192, "monthly": 744,
                "quarterly": 2232, "biannual": 4464, "annual": 8928
            }
            if age_hours > stale_hours.get(h, 48):
                if latest_summary_dt and latest_summary_dt > last:
                    info["status"] = "stale"
                    health["issues"].append(f"{h} reflection is {round(age_hours/24)}d overdue")
                else:
                    info["status"] = "idle"
            else:
                info["status"] = "ok"
        else:
            oldest = db.db.execute("SELECT MIN(created_at) as oldest FROM summaries").fetchone()
            if oldest and oldest["oldest"]:
                try:
                    oldest_dt = parse_db_datetime(oldest["oldest"])
                    if oldest_dt is None:
                        raise ValueError("invalid oldest summary timestamp")
                    oldest_dt = ensure_utc(oldest_dt)
                    age_days = (datetime.now(timezone.utc) - oldest_dt).days
                    if age_days >= HORIZONS[h]["window_days"]:
                        material_age_days = (
                            (datetime.now(timezone.utc) - latest_summary_dt).days
                            if latest_summary_dt else None
                        )
                        if material_age_days is not None and material_age_days <= HORIZONS[h]["window_days"]:
                            info["status"] = "expected_but_missing"
                            health["issues"].append(f"{h} reflection has never run despite {age_days}d of data")
                        else:
                            info["status"] = "idle"
                except (ValueError, TypeError):
                    pass

        health["horizons"][h] = info

    cutoff = sqlite_utc_str(datetime.now(timezone.utc) - timedelta(hours=24))
    row = db.db.execute("""
        SELECT COUNT(*) as count FROM processing_log
        WHERE action = 'reflection_saved' AND timestamp >= ?
    """, (cutoff,)).fetchone()
    health["calls_last_24h"] = row["count"] if row else 0
    health["daily_call_limit"] = MAX_REFLECTION_CALLS_PER_DAY

    if health["issues"]:
        health["healthy"] = False

    return health


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    from memory_db import MemoryDB
    from companion_resolve import resolve_default_companion_name

    print("\n" + "=" * 60)
    print("LOVE REFACTORED - Temporal Reflection Engine")
    print("=" * 60)

    dry_run = "--dry-run" in sys.argv
    force = None
    companion = None

    for arg in sys.argv[1:]:
        if arg.startswith("--horizon="):
            force = arg.split("=", 1)[1]
        elif arg.startswith("--companion="):
            companion = arg.split("=", 1)[1]
        elif arg == "--dry-run":
            pass
        elif arg == "--status":
            comp = companion or resolve_default_companion_name()
            if not comp:
                print("✗ No companion found")
                sys.exit(1)
            db = MemoryDB(comp)
            print(f"\nReflection status for {comp}:")
            for h in ["daily", "weekly", "monthly", "quarterly", "biannual", "annual"]:
                last = _last_reflection_date(db, h)
                due = _is_reflection_due(db, h)
                if last:
                    days = (datetime.now(timezone.utc) - last).days
                    status = f"{'🟢 DUE' if due else '⏸ waiting'} (last: {days}d ago)"
                else:
                    status = f"{'🟢 DUE' if due else '⚪ no data yet'} (never run)"
                print(f"  {h:12s} {status}")
            ln = load_living_narrative(comp)
            if ln.get("self_narrative"):
                print(f"\n📖 Living narrative: {len(ln['self_narrative'])} chars")
                print(f"   Last updated by: {ln.get('last_updated_by', 'unknown')}")
                print(f"   Turning points: {len(ln.get('turning_points', []))}")
            else:
                print(f"\n📖 Living narrative: empty (will populate after quarterly+ reflections)")
            db.close()
            sys.exit(0)

    comp = companion or resolve_default_companion_name()
    if not comp:
        print("✗ No companion found. Set TANEVAN_COMPANION_NAME or add a companion.")
        sys.exit(1)

    db = MemoryDB(comp)

    if force:
        result = run_reflection(db, force, companion_name=comp.capitalize(), dry_run=dry_run)
    else:
        result = run_due_reflections(db, companion_name=comp.capitalize(), dry_run=dry_run)

    db.close()

    if result:
        print("\nDone.")
    else:
        print("\nNothing to process.")

    print("\nUsage:")
    print("  python3 reflection.py                          # Run all due reflections")
    print("  python3 reflection.py --horizon=daily           # Force a specific horizon")
    print("  python3 reflection.py --dry-run                 # Preview without saving")
    print("  python3 reflection.py --status                  # Show reflection schedule")
    print("  python3 reflection.py --companion=evan          # Specific companion")
