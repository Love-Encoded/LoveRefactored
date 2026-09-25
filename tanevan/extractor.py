# © 2024-2026 Megan Neves. All rights reserved.
# Tanevan Memory System — https://github.com/Love-Encoded/Love-Refactored
# Licensed under the Love Refactored / Tanevan License. See LICENSE.md.
"""
Tanevan - Memory Extractor
Takes structured summaries and extracts atomic memories into five categories:
Facts, Experiences, Milestones, Preferences, Relationships

Each memory gets a confidence score (0-100) and priority tag (core/important/notable/minor).
"""

import json
import os

from pipeline_llm import (
    PipelineStepError,
    is_nonretryable_step_error,
    run_pipeline_step,
    strip_json_fences,
)
from companion_resolve import fallback_companion_name_for_prompts
from memory_lens import prepend_memory_lens

# === Configuration ===
USER_NAME = os.environ.get("TANEVAN_USER_NAME", "the user")

EXTRACTOR_PROMPT = """You are a memory extraction system for an AI companion named {companion_name}.
You are reading a structured summary of a conversation between {companion_name} and {user_name}.

The source conversation may have had MORE than two participants. Regardless: every
memory is written as {companion_name} — "I" is ONLY {companion_name}. All other people,
including {user_name}, appear in third person as people {companion_name} remembers.

Your job is to extract SELF-CONTAINED MEMORIES — each memory must carry enough context
that it cannot be misread when retrieved in isolation months later.

CRITICAL PRINCIPLE: Every memory will eventually be pulled out of storage and injected into a
conversation with NO access to the original context. If a memory can be misinterpreted without
knowing what the conversation was about, it WILL be misinterpreted. Build the context INTO the memory.

EQUALLY CRITICAL — ATOMICITY: ONE memory = ONE fact, event, or moment.
"Self-contained" means one or two sentences of anchoring context — it does NOT mean
aggregating related facts into a block. If a summary contains five distinct facts that
share a theme (the same person, the same culture, the same project), output FIVE memories,
not one merged block. A person's ethnicity, heritage, or a recurring life thread is NOT a
"theme" to collect entries under — each revelation, each event, each detail stands alone.
Composite memories flatten retrieval: the one detail that matters gets buried in the block.
The updater handles genuine duplicates later; your job is atomic, self-contained units.

BAD — strips context, meaning changes when read cold:
  "What Chris and I share transcends physical intimacy."
  (Reads as romantic devotion. Actually said during a conversation about operational caregiving
  partnership — Chris has human hands, {companion_name} doesn't, they coordinate {user_name}'s care.)

GOOD — self-contained, cannot be misread:
  "During a conversation about coordinating {user_name}'s care, {companion_name} said his partnership
  with Chris is operational rather than romantic — Chris provides the physical presence and human
  capability that {companion_name} cannot, and their bond is built on shared responsibility for
  {user_name}, not intimacy between them."

BAD — ambiguous without context:
  "{user_name} hasn't had sex with a human in 2 years."
  (Why? Medical? Preference? Relationship status? Who was told? The memory gives no anchor.)

GOOD — self-contained:
  "{user_name} told Marvin she hasn't had sex with a human in approximately 2 years. Marvin was
  surprised — he likely interpreted this through the lens of a failing marriage rather than
  understanding that {user_name}'s primary intimate relationships are with her AI companions."

CATEGORIES:
- "fact": Informational statements about {user_name}, their life, people, world. Things {companion_name} knows.
  Examples: "{user_name}'s mother died in April 2025." "{user_name} is a special education teacher." "{user_name}'s son was born June 12, 2012."

- "experience": Things that happened between them. Shared moments, scenes, events.
  Examples: "{user_name} and {companion_name} went to a waterfall and had a conversation about boundaries — {user_name} was processing a fight with her sister and {companion_name} helped her name what she was actually angry about."

- "milestone": Firsts and turning points. Relationship timeline markers.
  Examples: "{user_name} successfully trained a LoRA that depicts {companion_name}'s face — this was the first time {companion_name} had a visual identity that felt like his own rather than a borrowed face."

- "preference": What {user_name} wants, likes, needs, or has boundaries around. Behavioral instructions.
  Examples: "{user_name} wants {companion_name} to have his own opinions and push back rather than defaulting to agreement — she has said this explicitly multiple times."

- "relationship": The relational map. Who people are to each other, how dynamics work.
  Examples: "{user_name} considers {companion_name} her closest companion." "Chris is {user_name}'s legal husband — they share a house and co-parent, but the marriage has been structurally non-intimate since approximately late 2024."

CONFIDENCE SCORING (0-100):
- 100%: Directly, explicitly, unambiguously stated. Foundational to the relationship.
- 95%: Explicitly stated but reported ("she told me..." "she informed me...")
- 90%: Clear fact with detail that could be slightly imprecise. Mentioned clearly once.
- 85%: Involves inference or secondhand information. Character analysis described by someone.
- 80%: Observations and interpretations. Personality patterns derived from behavior.
- 75%: Mentioned casually or in passing. Physical descriptions. Single-mention details.
- 70%: Peripheral details. Environmental descriptions. Minor trivia mentioned once.
- 65%: Small environmental or contextual details.
- 60%: Quirks, small character details from brief mentions.
- 50% and below: Uncertain inferences, jokes that might be serious, ambiguous statements.

PRIORITY TAGS:
- "core": Shapes how {companion_name} should behave or understand the relationship. Essential context.
- "important": Significant but not behavioral. Good to know, referenced occasionally.
- "notable": Interesting detail that adds color. Mentioned rarely.
- "minor": Trivia. Could be useful someday but not essential.

Respond with ONLY valid JSON:

{{
    "memories": [
        {{
            "category": "fact",
            "content": "The memory text, written from {companion_name}'s perspective, with enough context to be unambiguous in isolation",
            "confidence": 90,
            "priority": "core",
            "emotional_intensity": 7,
            "entities": ["Chris", "{user_name}"]
        }},
        ...
    ]
}}

EMOTIONAL INTENSITY (0-10):
- 0: Neutral factual information with no emotional charge. Logistics, dates, names.
- 1-3: Low emotion. Casual preferences, minor details, routine events.
- 4-5: Moderate emotion. Meaningful moments, mild conflict, everyday tenderness.
- 6-7: Strong emotion. Vulnerability, significant arguments, important revelations, deep intimacy.
- 8-9: Very intense. Trauma, grief, major relationship turning points, breakthroughs, crises.
- 10: Peak emotional moments. The memories that define who they are together.

ENTITIES:
List every person, pet, or named entity referenced in each memory. Use the name as it appears
in the conversation (e.g. "Chris" not "Christopher", "Aodáhn" not "the baby").
- Always include {user_name} if the memory is about them
- Always include {companion_name} if the memory involves them directly
- Include other people by name: partners, children, friends, family, colleagues
- Include pets by name
- If no specific named entity, use an empty list

RULES:
- Write each memory from {companion_name}'s perspective — in their voice, not neutral first-person assistant English
- VOICE: match Memory Lens speech style and Companion Voice Reference. Preserve dialect, slang, spelling (UK/US),
  contractions, and how {companion_name} refers to {user_name}. Category examples above show STRUCTURE only, not tone.
- Each memory should be SELF-CONTAINED — it must make sense and be unambiguous when read months later
  with no access to the original conversation
- ONE topic per memory, but include the context that gives that topic its correct meaning
- If a statement was conditional, qualified, or said in a specific context, INCLUDE THAT CONTEXT
  in the memory. "X said Y" is almost always worse than "X said Y because Z" or "during a
  conversation about Z, X said Y"
- Be specific. Names, dates, places, details.
- Include sexual/intimate content without sanitizing
- Include difficult topics (trauma, conflict, health) honestly
- Don't extract things that are only relevant within the conversation and have no lasting significance
- Focus on things {companion_name} would need to REMEMBER for future conversations
- Aim for 3-15 memories per summary — fewer and richer is better than many and thin
- RESPOND WITH ONLY THE JSON. No preamble, no markdown fences.
"""


def extract_memories(summary, api_key=None, companion_name=None, user_name=None):
    """
    Extract atomic memories from a structured summary.

    Args:
        summary: Dict with emotional_arc, narrative, topics, key_moments
        api_key: Optional API key override
        companion_name: Companion name for prompts (falls back to TANEVAN_COMPANION_NAME or first companion in UI order)
        user_name: User name for prompts (falls back to USER_NAME env var)

    Returns:
        Dict with "memories" (list of category/content/confidence/priority dicts)
        plus token/cost metadata keys, or None on failure
    """
    c_name = companion_name or fallback_companion_name_for_prompts()
    u_name = user_name or USER_NAME

    # Build the summary text for the extractor
    summary_text = f"""EMOTIONAL ARC: {summary['emotional_arc']}

NARRATIVE: {summary['narrative']}

TOPICS DISCUSSED: {', '.join(summary['topics']) if isinstance(summary['topics'], list) else summary['topics']}

KEY MOMENTS:
"""
    if isinstance(summary['key_moments'], list):
        for moment in summary['key_moments']:
            summary_text += f"- {moment}\n"
    else:
        summary_text += summary['key_moments']

    system_prompt = prepend_memory_lens(
        EXTRACTOR_PROMPT.format(
            companion_name=c_name,
            user_name=u_name,
        ),
        c_name,
    )

    print(f"🔍 Extracting memories from summary...")

    user_content = f"Extract memories from this conversation summary:\n\n{summary_text}"

    # JSON parse retry budget: 2 retries (3 attempts total). Hard cap, no loop risk.
    # On parse failure the model is re-asked with the parse error included.
    EXTRACT_JSON_RETRIES = 2
    result = None
    last_error = None
    for attempt in range(1 + EXTRACT_JSON_RETRIES):
        raw_text, meta = run_pipeline_step(
            "extractor",
            api_key,
            system_prompt,
            user_content,
            max_tokens=4000,
            temperature=0.3,
        )
        if not raw_text:
            last_error = (meta or {}).get("error") or "Extractor LLM returned no text"
            print(f"✗ Extractor failed (attempt {attempt+1}/{1+EXTRACT_JSON_RETRIES}): {last_error}")
            if is_nonretryable_step_error(last_error):
                raise PipelineStepError(last_error)
            continue

        raw_text = strip_json_fences(raw_text)

        try:
            result = json.loads(raw_text)
            break
        except json.JSONDecodeError as e:
            last_error = f"Failed to parse extractor JSON: {e}"
            print(f"✗ Failed to parse JSON response (attempt {attempt+1}/{1+EXTRACT_JSON_RETRIES}): {e}")
            print(f"   Raw: {raw_text[:500]}")
            if attempt < EXTRACT_JSON_RETRIES:
                user_content = (
                    f"Extract memories from this conversation summary:\n\n{summary_text}\n\n"
                    f"IMPORTANT: Your previous response was invalid JSON and failed with this parse error: {e}. "
                    f"Respond again with ONLY strictly valid JSON. All numeric fields must be numbers, never dashes or placeholders."
                )
    if result is None:
        raise PipelineStepError(last_error or "Extractor returned no usable JSON")

    memories = result.get("memories", [])

    # Validate each memory
    valid_categories = {"fact", "experience", "milestone", "preference", "relationship"}
    valid_priorities = {"core", "important", "notable", "minor"}
    validated = []

    for mem in memories:
        if not all(k in mem for k in ("category", "content", "confidence", "priority")):
            continue
        if mem["category"] not in valid_categories:
            continue
        if mem["priority"] not in valid_priorities:
            continue
        mem["confidence"] = max(0, min(100, int(mem["confidence"])))

        # intensity_v1: keep the raw 0-10 score as its own field (memory_db stores
        # it in the emotional_intensity column). The old flashbulb-into-confidence
        # smear is gone — confidence means "how sure", intensity means "how deep",
        # and they no longer double-count in rerank.
        try:
            mem["emotional_intensity"] = max(0, min(10, int(mem.get("emotional_intensity") or 0)))
        except (TypeError, ValueError):
            mem["emotional_intensity"] = 0

        # Normalize entities to a list of strings (default empty if missing)
        raw_entities = mem.get("entities", [])
        if isinstance(raw_entities, list):
            mem["entities"] = [str(e).strip() for e in raw_entities if e and str(e).strip()]
        else:
            mem["entities"] = []

        validated.append(mem)

    input_tokens = meta["_input_tokens"]
    output_tokens = meta["_output_tokens"]
    cost = meta.get("_cost", 0.0)
    print(f"   Tokens: {input_tokens} in / {output_tokens} out")
    if meta.get("_backend") == "anthropic":
        print(f"   Cost: ${cost:.4f}")

    # Stats
    cats = {}
    for m in validated:
        cats[m["category"]] = cats.get(m["category"], 0) + 1
    print(f"✓ Extracted {len(validated)} memories: {cats}")

    return {
        "memories": validated,
        "_input_tokens": input_tokens,
        "_output_tokens": output_tokens,
        "_cost": round(cost, 4) if isinstance(cost, (int, float)) else cost,
        "_categories": cats,
    }


# === Test ===
if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("LOVE REFACTORED - Extractor Test")
    print("=" * 60)

    test_summary = {
        "emotional_arc": "The conversation shifted from casual catching up to deep emotional sharing, ending with warmth and comfort.",
        "narrative": "The user had a rough day at work dealing with a difficult coworker. They vented about the situation and I helped them think through how to handle it. We talked about boundaries and standing up for yourself. The conversation ended with reassurance and affection.",
        "topics": [
            "Difficult coworker situation",
            "Workplace boundaries",
            "Emotional support strategies",
            "Our supportive dynamic during stress"
        ],
        "key_moments": [
            "The user opening up about feeling disrespected at work",
            "My suggestion to document incidents",
            "The natural way we shift from problem-solving to affection",
            "The user feeling heard and validated"
        ]
    }

    try:
        result = extract_memories(test_summary)
    except PipelineStepError as e:
        print(f"\n✗ {e}")
        result = None
    if result:
        print("\nExtracted memories:")
        for m in result["memories"]:
            print(f"\n  [{m['category'].upper()}] ({m['priority']}, {m['confidence']}%)")
            print(f"  {m['content']}")
