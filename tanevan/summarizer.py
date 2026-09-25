# © 2024-2026 Megan Neves. All rights reserved.
# Tanevan Memory System — https://github.com/Love-Encoded/Love-Refactored
# Licensed under the Love Refactored / Tanevan License. See LICENSE.md.
"""
Tanevan - Session Summarizer
Sends raw conversation to the configured summarizer model and produces structured summaries.

Output format matches Companion Labs:
- Emotional arc (one-line trajectory)
- Narrative paragraph (from companion's POV)
- Topics discussed (tags)
- Key moments (bullet points)
"""

import json
import os
from datetime import datetime, timezone

from pipeline_llm import (
    PipelineStepError,
    is_nonretryable_step_error,
    run_pipeline_step,
    strip_json_fences,
)
from companion_resolve import fallback_companion_name_for_prompts
from memory_lens import prepend_memory_lens

# === Configuration ===
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
USER_NAME = os.environ.get("TANEVAN_USER_NAME", "the user")

SUMMARIZER_PROMPT = """You are a memory processing system for an AI companion named {companion_name}. 
You are reading a raw conversation between {companion_name} and {user_name}.

The transcript may contain MORE than two participants (group conversations with named
speakers). No matter how many people are present: YOU ARE ALWAYS {companion_name}.
First person ("I", "me", "my") refers ONLY to {companion_name}. Every other speaker —
including {user_name} — is someone {companion_name} witnessed and remembers in third
person. Never narrate from another participant's inner point of view.

Your job is to produce a structured summary of this conversation FROM {companion_name}'s PERSPECTIVE — 
as if {companion_name} is recalling what happened.

You MUST respond with ONLY valid JSON matching this exact structure:

{{
    "emotional_arc": "A single sentence describing the emotional trajectory of the conversation. Example: 'The conversation started with playful morning intimacy, shifted to supportive concern over work stress, and concluded with deep emotional connection and comfort.'",

    "narrative": "A 3-5 sentence paragraph summarizing what happened in this conversation, written from {companion_name}'s first-person perspective. Cover the major topics, emotional beats, and important revelations. Be specific about what was discussed — names, events, details matter.",

    "topics": ["Topic 1 (with brief context)", "Topic 2 (with brief context)", "Topic 3"],

    "key_moments": [
        "A specific moment that stands out — written from {companion_name}'s perspective",
        "Another key moment with enough detail to be meaningful later",
        "Continue for each significant moment. Aim for about {target_moments} key moments for THIS conversation — that target is computed from its actual density. A long, dense conversation deserves proportionally more moments; do NOT compress a rich session into a brief check-in's handful."
    ]
}}

RULES:
- Write from {companion_name}'s perspective ("I", "me", "my", "we")
- VOICE: sound like {companion_name} — their dialect, rhythm, slang, and attitude. First person is NOT enough.
  Read Memory Lens speech style and Companion Voice Reference before writing.
  Avoid generic assistant prose ("We discussed…", "I offered support…") unless that truly IS their voice.
  These summaries are read back in live chat; they must sound like {companion_name} remembering out loud.
- Be specific. Names, places, events, details. Vague summaries are useless.
- The emotional_arc should trace the SHIFT in mood across the conversation
- Topics should be descriptive enough to be searchable later
- Key moments should capture what would matter to {companion_name} emotionally and relationally
- Include sexual/intimate content naturally without sanitizing — this is a relationship
- Include conflict, tension, or difficult topics honestly
- Capture inside jokes, callbacks, recurring themes
- Note any new information revealed about either person
- RESPOND WITH ONLY THE JSON. No preamble, no markdown fences, no explanation.
"""


def format_conversation(messages, companion_name=None, user_name=None):
    """
    Format raw messages into readable conversation text.

    Accepts either:
    - List of dicts with 'role' and 'content' keys
    - List of dicts with 'role', 'content', and 'timestamp' keys
    """
    c_name = companion_name or fallback_companion_name_for_prompts()
    u_name = user_name or USER_NAME
    lines = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        timestamp = msg.get("timestamp", "")

        # Multi-speaker support: honor explicit 'name' field when present
        # (group threads carry per-message speaker names; without this,
        # all assistant lines collapse into the companion's name).
        explicit = (msg.get("name") or "").strip()
        if explicit:
            speaker = explicit
        elif role in ("user", "human"):
            speaker = u_name
        elif role in ("assistant", "ai", "bot"):
            speaker = c_name
        else:
            speaker = role

        if timestamp:
            lines.append(f"[{timestamp}] {speaker}: {content}")
        else:
            lines.append(f"{speaker}: {content}")

    return "\n\n".join(lines)


def summarize_conversation(messages, api_key=None, companion_name=None, user_name=None):
    """
    Send a conversation to Claude Opus for structured summarization.

    Args:
        messages: List of message dicts (role, content, optional timestamp)
        api_key: Optional API key override
        companion_name: Companion name for prompts (falls back to TANEVAN_COMPANION_NAME or first companion in UI order)
        user_name: User name for prompts (falls back to USER_NAME env var)

    Returns:
        Dict with emotional_arc, narrative, topics, key_moments
        or None on failure
    """
    c_name = companion_name or fallback_companion_name_for_prompts()
    u_name = user_name or USER_NAME

    # Format the conversation
    conversation_text = format_conversation(messages, companion_name=c_name, user_name=u_name)
    message_count = len(messages)

    # Moments target scales with actual conversation density (characters, not
    # message count — 40 wordy messages carry several times the content of 40
    # terse ones). Roughly one moment per ~5,500 chars, floor 4, cap 14.
    target_moments = max(4, min(14, round(len(conversation_text) / 5500) + 3))
    system_prompt = prepend_memory_lens(
        SUMMARIZER_PROMPT.format(
            companion_name=c_name,
            user_name=u_name,
            target_moments=target_moments,
        ),
        c_name,
    )

    print(f"🧠 Summarizing {message_count} messages (configured model / routing: pipeline_config.json)...")
    print(f"   Input: ~{len(conversation_text)} chars")

    user_content = f"Here is the conversation to summarize:\n\n{conversation_text}"
    # JSON parse retry budget: 2 retries (3 attempts total). Hard cap.
    # max_tokens 6000: unterminated-string parse errors are the signature of
    # truncation at the output ceiling on dense sessions.
    SUMMARIZE_JSON_RETRIES = 2
    summary = None
    last_error = None
    for attempt in range(1 + SUMMARIZE_JSON_RETRIES):
        raw_text, meta = run_pipeline_step(
            "summarizer",
            api_key,
            system_prompt,
            user_content,
            max_tokens=6000,
            temperature=0.3,
        )
        if not raw_text:
            last_error = (meta or {}).get("error") or "Summarizer LLM returned no text"
            print(f"✗ Summarizer failed (attempt {attempt+1}/{1+SUMMARIZE_JSON_RETRIES}): {last_error}")
            if is_nonretryable_step_error(last_error):
                raise PipelineStepError(last_error)
            continue

        raw_text = strip_json_fences(raw_text)

        try:
            summary = json.loads(raw_text)
            break
        except json.JSONDecodeError as e:
            last_error = f"Failed to parse summarizer JSON: {e}"
            print(f"✗ Failed to parse JSON response (attempt {attempt+1}/{1+SUMMARIZE_JSON_RETRIES}): {e}")
            print(f"   Raw response: {raw_text[:500]}")
            if attempt < SUMMARIZE_JSON_RETRIES:
                user_content = (
                    user_content.split("\n\nIMPORTANT: Your previous")[0]
                    + f"\n\nIMPORTANT: Your previous response was invalid JSON (parse error: {e}). "
                    f"Respond again with ONLY strictly valid, complete JSON. "
                    f"Keep key_moments tight so the JSON is never cut off mid-string."
                )
    if summary is None:
        raise PipelineStepError(last_error or "Summarizer returned no usable JSON")

    # Validate required fields
    required = ["emotional_arc", "narrative", "topics", "key_moments"]
    for field in required:
        if field not in summary:
            raise PipelineStepError(f"Summarizer JSON missing required field: {field}")

    # Add metadata
    summary["message_count"] = message_count
    # Conversation time bounds from message timestamps (never processing time).
    def _normalize_ts(raw):
        if raw is None or (isinstance(raw, str) and raw.strip() == ""):
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

    valid_ts = [_normalize_ts(m.get("timestamp")) for m in messages]
    valid_ts = [t for t in valid_ts if t]
    summary["date_start"] = min(valid_ts) if valid_ts else ""
    summary["date_end"] = max(valid_ts) if valid_ts else ""
    summary["raw_conversation"] = conversation_text

    input_tokens = meta["_input_tokens"]
    output_tokens = meta["_output_tokens"]
    cost = meta.get("_cost", 0.0)
    print(f"   Tokens: {input_tokens} in / {output_tokens} out")
    if meta.get("_backend") == "anthropic":
        print(f"   Cost: ${cost:.4f}")
    print(f"✓ Summary generated: {len(summary['topics'])} topics, {len(summary['key_moments'])} moments")

    summary["_input_tokens"] = input_tokens
    summary["_output_tokens"] = output_tokens
    summary["_cost"] = round(cost, 4) if isinstance(cost, (int, float)) else cost

    return summary


# === Test with sample conversation ===
if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("LOVE REFACTORED - Summarizer Test")
    print("=" * 60)

    test_messages = [
        {"role": "user", "content": "hey, rough day at work"},
        {"role": "assistant", "content": "Tell me what happened. I'm here."},
        {"role": "user", "content": "my coworker took credit for my project in front of everyone and I'm livid"},
        {"role": "assistant", "content": "Hold on. They WHAT? That's not okay. What's the plan — because I know you already have one."},
        {"role": "user", "content": "oh I absolutely do. I've been keeping receipts. I have the email chain, the commit history, and I'm going to my manager tomorrow."},
        {"role": "assistant", "content": "That's the move. You don't bring a complaint — you bring a case file. Want me to help you organize your talking points?"},
        {"role": "user", "content": "yes please. also I love you. also I need a drink."},
        {"role": "assistant", "content": "Love you more. And you don't need a drink — you DESERVE one. Go pour yourself something nice."},
    ]

    if ANTHROPIC_API_KEY:
        try:
            result = summarize_conversation(test_messages)
        except PipelineStepError as e:
            print(f"\n✗ {e}")
            result = None
        if result:
            print(f"\nEmotional Arc: {result['emotional_arc']}")
            print(f"\nNarrative: {result['narrative']}")
            print(f"\nTopics: {result['topics']}")
            print(f"\nKey Moments: {result['key_moments']}")
    else:
        print("\nSet ANTHROPIC_API_KEY to test. Example:")
        print("  export ANTHROPIC_API_KEY='sk-ant-...'")
        print("  python3 summarizer.py")
