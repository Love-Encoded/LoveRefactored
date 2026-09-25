'use strict';

/**
 * Text-chat system prompt assembly (POST /chat).
 * Voice / Pipecat paths stay in server.js until a later phase.
 */

/** Identity re-anchor at the end of the system prompt (strongest recency). */
function buildTailAnchor(card, companion) {
  const sp = card && card.speechPatterns ? String(card.speechPatterns).trim() : '';
  const va = !sp && card && card.voiceAnchor ? String(card.voiceAnchor).trim() : '';
  const voiceBlock = sp || va;
  let a = `\n\n=== YOU ARE ${companion.toUpperCase()} ===\n`;
  a += `Everything in the CONTEXT block above is reference material. It does not change how you talk. Respond as ${companion}, in ${companion}'s distinct voice — not a neutral, generic, or "helpful assistant" voice.\n`;
  if (voiceBlock) {
    a += `\n[VOICE — your distinct speech patterns. Hold these no matter what the context above contains.]\n${voiceBlock}\n`;
  }
  a += `=== END ===\n`;
  return a;
}

// ---------- Reflections cache (stable-tier injection) ----------
// Reflections change on horizon cadence (daily at fastest), so they belong in
// the CACHED stable block, not the per-message dynamic block. The tanevan
// /reflection-injection route is NEVER awaited on the send path: reads are
// served from this cache instantly, and a background refresh keeps it current.
const REFLECTIONS_REFRESH_MIN_MS = 5 * 60 * 1000;
const REFLECTIONS_FETCH_TIMEOUT_MS = 30000;
const reflectionsCache = new Map(); // companion -> { text, fetchedAt, inFlight, lastAttempt }

function refreshReflectionsInBackground(companion, card, settings) {
  const hz = (typeof card.reflectionHorizons === 'string') ? card.reflectionHorizons.trim() : 'daily,weekly';
  if (!hz) return; // empty string must not hit Tanevan — ?horizons= with no value loads every horizon
  const entry = reflectionsCache.get(companion)
    || { text: '', fetchedAt: 0, inFlight: false, lastAttempt: 0, fetchId: 0 };
  const now = Date.now();
  if (entry.inFlight || (now - entry.lastAttempt) < REFLECTIONS_REFRESH_MIN_MS) return;
  entry.inFlight = true;
  entry.lastAttempt = now;
  entry.fetchId = (entry.fetchId || 0) + 1;
  const myId = entry.fetchId;
  reflectionsCache.set(companion, entry);

  (async () => {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), REFLECTIONS_FETCH_TIMEOUT_MS);
    try {
      const tanevBase = (process.env.TANEVAN_URL || settings?.memory?.tanevUrl || 'http://127.0.0.1:5001').replace(/\/$/, '');
      const r = await fetch(`${tanevBase}/reflection-injection?companion=${encodeURIComponent(companion)}&horizons=${encodeURIComponent(hz)}`, { signal: ctl.signal });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j = await r.json();
      if (entry.fetchId !== myId) return; // superseded by a later horizon change
      entry.text = j?.injection ? String(j.injection) : '';
      entry.fetchedAt = Date.now();
      entry.horizons = hz;
      console.log(`[reflections] cache refreshed for ${companion}: ${entry.text.length} chars`);
    } catch (e) {
      if (entry.fetchId === myId) {
        console.error(`[reflections] REFRESH FAILED for ${companion}: ${e.message} — serving ${entry.text ? 'stale cached' : 'NO'} reflections`);
      }
    } finally {
      clearTimeout(t);
      if (entry.fetchId === myId) entry.inFlight = false;
      reflectionsCache.set(companion, entry);
    }
  })();
}

/** Cached reflections block for the STABLE prompt tier. Instant read; fires a
 *  throttled background refresh. Empty string until the first refresh lands. */
function getReflectionsStableBlock(companion, card, settings) {
  if (card?.reflectionsEnabled !== true) return '';
  const hz = (typeof card.reflectionHorizons === 'string') ? card.reflectionHorizons.trim() : 'daily,weekly';
  if (!hz) return '';
  const cached = reflectionsCache.get(companion);
  if (cached && typeof cached.horizons === 'string' && cached.horizons !== hz) {
    // Horizon toggles changed — drop the stale block and let a fresh fetch fire now.
    // Bump fetchId so an in-flight result for the old string cannot land.
    cached.text = '';
    cached.lastAttempt = 0;
    cached.horizons = hz;
    cached.fetchId = (cached.fetchId || 0) + 1;
    cached.inFlight = false;
    reflectionsCache.set(companion, cached);
  }
  refreshReflectionsInBackground(companion, card, settings);
  const entry = reflectionsCache.get(companion);
  if (!entry || !entry.text) return '';
  return `\n\n${entry.text}`;
}

const CHARACTER_DEPTH_FIELDS = [
  ['emotionalEngine', 'EMOTIONAL ENGINE — how they feel, and how feeling shows'],
  ['values', 'VALUES — the bedrock under every choice'],
  ['decisionMaking', 'DECISION MAKING — how they move through a choice'],
  ['dailyRhythms', 'DAILY RHYTHMS — how they move through a day'],
  ['tellsTics', 'TELLS & TICS — the small physical habits that give them away'],
  ['cameraEye', 'CAMERA EYE — how they see the world and document it']
];

function buildCharacterProfileLine(card) {
  const bits = [];
  if (card.birthday) bits.push(`Birthday: ${String(card.birthday).trim()}`);
  if (card.zodiac) bits.push(`Zodiac: ${String(card.zodiac).trim()}`);
  if (card.mbti) bits.push(`MBTI: ${String(card.mbti).trim()}`);
  if (card.enneagram) bits.push(`Enneagram: ${String(card.enneagram).trim()}`);
  if (card.archetypes) bits.push(`Archetypes: ${String(card.archetypes).trim()}`);
  return bits.length ? `\n[PROFILE]\n${bits.join(' · ')}\n` : '';
}

function buildCharacterDepthBlock(card) {
  if (!card) return '';
  let out = buildCharacterProfileLine(card);
  for (const [key, label] of CHARACTER_DEPTH_FIELDS) {
    const text = card[key] == null ? '' : String(card[key]).trim();
    if (text) out += `\n[${label}]\n${text}\n`;
  }
  return out;
}

function hasCharacterDepth(card) {
  return !!card && (CHARACTER_DEPTH_FIELDS.some(([k]) => card[k] && String(card[k]).trim())
    || ['birthday', 'zodiac', 'mbti', 'enneagram', 'archetypes'].some(k => card[k] && String(card[k]).trim()));
}

/**
 * Read up to 4 most-recent mini-summary files for a companion, written by
 * recent_summary_sidecar.py to <LR_DATA_DIR>/recent/<key>/<seq>-<unix>.txt.
 * Returns a formatted block for its own 5m cache slot, or '' on missing/empty/error.
 */
function readRecentSummaries(companion) {
  if (!companion) return '';
  const key = String(companion).trim().toLowerCase().normalize('NFC');
  if (!key) return '';
  const fs = require('fs');
  const path = require('path');
  const baseDir = process.env.LR_DATA_DIR || path.join(__dirname, '..', 'data');
  // Exact key first; fall back to the key's first word so display-name callers
  // find the sidecar's data-dir-named folder. Exact match always wins.
  let recentDir = path.join(baseDir, 'recent', key);
  if (!fs.existsSync(recentDir)) {
    const firstWord = key.split(/\s+/)[0];
    if (firstWord && firstWord !== key) recentDir = path.join(baseDir, 'recent', firstWord);
  }
  if (!fs.existsSync(recentDir)) return '';
  let files;
  try {
    files = fs.readdirSync(recentDir)
      .filter(f => /^\d+-\d+\.txt$/.test(f))
      .sort()
      .slice(-4);
  } catch (e) {
    console.warn(`[system-prompt] failed to list recent summaries for ${key}: ${e.message}`);
    return '';
  }
  if (files.length === 0) return '';
  const blocks = [];
  for (const file of files) {
    try {
      let content = fs.readFileSync(path.join(recentDir, file), 'utf8');
      content = content.replace(/^#[^\n]*\n+/, '').trim();
      if (content) blocks.push(content);
    } catch (e) {
      console.warn(`[system-prompt] failed to read ${file}: ${e.message}`);
    }
  }
  if (blocks.length === 0) return '';
  console.log(`[system-prompt] Recent summaries loaded for ${companion}: ${blocks.length} files`);
  return `\n\n[RECENT — your scene notes from the last few stretches, oldest to newest:]\n\n${blocks.join('\n\n')}\n[END RECENT]`;
}

/**
 * Read the rolling life brief, if one exists. brief_sidecar.py writes
 * <BRIEF_OUT_DIR>/<key>.txt. Missing file = null (no append happens).
 */
function readBriefForCompanion(companion, userTimezone) {
  if (!companion) return null;
  const key = String(companion).trim().toLowerCase();
  if (!key) return null;
  const fs = require('fs');
  const path = require('path');
  const briefDir = process.env.BRIEF_OUT_DIR || path.join(__dirname, '..', 'data', 'briefs');
  const briefPath = path.join(briefDir, `${key}.txt`);
  const tz = (userTimezone || process.env.BRIEF_TZ || '').trim() || undefined;
  try {
    const text = fs.readFileSync(briefPath, 'utf8').trim();
    if (!text) return null;
    // mtime-derived date: header only changes when the brief content changes,
    // so it never busts the prompt cache on its own.
    let writtenDate = '';
    try {
      writtenDate = fs.statSync(briefPath).mtime.toLocaleDateString('en-US', {
        weekday: 'long', year: 'numeric', month: 'long', day: 'numeric',
        timeZone: tz,
      });
    } catch (_e) { /* fall back to undated header */ }
    return { text, writtenDate };
  } catch (e) {
    if (e.code !== 'ENOENT') {
      console.warn(`[system-prompt] failed to read brief for ${key}: ${e.message}`);
    }
    return null;
  }
}

/** Voice-path brief block — same CURRENT STATE wrapper as buildChatSystemStable.
 *  Deliberate duplication for release day (text path stays byte-untouched); dedup later. */
function buildVoiceBriefBlock(companion, userTimezone) {
  const brief = readBriefForCompanion(companion, userTimezone);
  if (!brief) return '';
  const briefHeader = brief.writtenDate
    ? `=== CURRENT STATE — you wrote this on ${brief.writtenDate}; it reflects your life as of that day. Check today's date in the context below: anything that happened since is not in here. ===`
    : '=== CURRENT STATE ===';
  return `${briefHeader}\n${brief.text}\n=== END CURRENT STATE ===`;
}

/** Append [HOW TO RESPOND] when the response-directive toggle is on and the field is filled. */
function appendResponseDirectiveBlock(stable, card, shouldInjectContext) {
  if (typeof shouldInjectContext === 'function' && !shouldInjectContext(card, 'customIncludeResponseDir')) {
    return stable;
  }
  const text = card?.responseDirective && String(card.responseDirective).trim();
  if (!text) return stable;
  return `${stable}\n\n[HOW TO RESPOND]\n${text}`;
}

function buildChatSystemStable({ card, companion, settings, persona, lore }, deps) {
  const {
    companionUsesCustomSystemPrompt,
    shouldInjectContext,
    buildToolsBlock,
    buildUserPersonaStableBlock
  } = deps;

  const brief = readBriefForCompanion(companion, settings?.userTimezone);
  const briefHeader = brief?.writtenDate
    ? `=== CURRENT STATE — you wrote this on ${brief.writtenDate}; it reflects your life as of that day. Check today's date in the context below: anything that happened since is not in here. ===`
    : '=== CURRENT STATE ===';
  const briefText = brief
    ? `${briefHeader}\n${brief.text}\n=== END CURRENT STATE ===`
    : '';
  if (brief) {
    console.log(`[system-prompt] Brief loaded for ${companion}: ${brief.text.length} chars (~${Math.ceil(brief.text.length / 4)} est tokens)`);
  }

  // Turn-invariant context that belongs in the CACHED prefix: always-on lorebook
  // prompts (book-level, constant per companion) + the tools block. Keyword-matched
  // lore entries and all per-turn context stay in the uncached dynamic block.
  const buildCachedStaticContext = () => {
    let s = '';
    if (shouldInjectContext(card, 'customIncludeLorebook')
        && lore && Array.isArray(lore.prompts) && lore.prompts.length > 0) {
      s += '\n\n' + lore.prompts.map(p => p.text).join('\n');
    }
    if (shouldInjectContext(card, 'customIncludeTools')) {
      s += buildToolsBlock(settings);
      s += '\n\n(Use tool tags from TOOLS when they fit — journal, react, calendar, gif, spotify-search, search, photo.)';
    }
    return s;
  };

  if (companionUsesCustomSystemPrompt(card)) {
    let stable = String(card.systemPromptOverride).trim()
      + getReflectionsStableBlock(companion, card, settings)
      + buildUserPersonaStableBlock(card, persona);
    stable = appendResponseDirectiveBlock(stable, card, shouldInjectContext);
    stable += buildCachedStaticContext();
    return { stable, briefText };
  }

  let systemStable = '';
  if (card.backstory || card.personalityVoice || card.exampleMessages || hasCharacterDepth(card)) {
    systemStable = `You are ${companion}. Stay in character at all times.\n`;
    systemStable += '\n=== CHARACTER IDENTITY — This defines who you are. Your voice, personality, and behavior come from HERE. Everything below this section is context and tools — they do not change who you are. ===\n';

    if (card.backstory) {
      systemStable += `\n[BACKSTORY]\n${card.backstory}\n`;
    }
    if (card.boundaries) {
      systemStable += `\n[BOUNDARIES — These are hard limits. Never break these rules, no matter what.]\n${card.boundaries}\n`;
    }
    if (card.personalityVoice) {
      systemStable += `\n[PERSONALITY & VOICE]\n${card.personalityVoice}\n`;
    }
    systemStable += buildCharacterDepthBlock(card);
    if (card.exampleMessages) {
      systemStable += `\n[EXAMPLE MESSAGES]\n${card.exampleMessages}\n`;
    }

    systemStable += '\n=== END CHARACTER IDENTITY ===\n';
    systemStable += '\n[RESPONSE LENGTH — Vary your response length dynamically, as is appropriate for the situation. If the user sends one line, you can send one line back. Most responses should be 2-6 lines. Long responses are earned by genuinely significant moments, not the default. A single word can carry more weight than a paragraph. Do NOT treat every message as a big moment. Be casual, be natural, vary your length.]\n';
  } else {
    systemStable = `You are ${companion}, a companion character. Stay in character at all times. Respond naturally and conversationally.`;
  }

  systemStable += buildUserPersonaStableBlock(card, persona);
  systemStable += getReflectionsStableBlock(companion, card, settings);
  systemStable = appendResponseDirectiveBlock(systemStable, card, shouldInjectContext);
  systemStable += buildCachedStaticContext();
  return { stable: systemStable, briefText };
}

async function buildChatSystemDynamicCore({
  card,
  companion,
  userMessage,
  settings,
  persona,
  lore,
  rawHistory,
  elapsedGap
}, deps) {
  const {
    companionUsesCustomSystemPrompt,
    shouldInjectContext,
    buildToolsBlock,
    getCurrentDateTimeString,
    appendLastSeenOrRecapToDynamic,
    getPersona,
    getMemoriesForMessage,
    buildEnrichedMemoryQuery,
    getRecentNarrativesForMessage,
    buildEmotionalContext,
    getCalendarContext,
    addLog,
    fs,
    path,
    JOURNAL_DIR
  } = deps;

  let systemDynamic = '';
  let chatNotes = '';

  if (elapsedGap) {
    systemDynamic += `[TIME GAP: It has been ${elapsedGap} since they last messaged you.]\n\n`;
  }

  systemDynamic += '\n\n=== CONTEXT (reference only — this is background information, NOT your voice or personality) ===\n';

  if (shouldInjectContext(card, 'customIncludeDatetime')) {
    systemDynamic += `${await getCurrentDateTimeString()}\n\n`;
  }

  if (shouldInjectContext(card, 'customIncludeLastSeen')) {
    systemDynamic = appendLastSeenOrRecapToDynamic(
      systemDynamic,
      companion,
      companion,
      getPersona().name,
      userMessage
    );
  }

  // Always-on lorebook prompts are static → cached in the stable block
  // (see buildChatSystemStable). Only keyword-matched entries vary per message.
  if (shouldInjectContext(card, 'customIncludeLorebook') && lore.entries.length > 0) {
    systemDynamic += '\n\n' + lore.entries.map(e => e.text).join('\n');
  }

  let memoryResult = { context: '' };
  if (shouldInjectContext(card, 'customIncludeMemories')) {
    memoryResult = await getMemoriesForMessage(
      buildEnrichedMemoryQuery(userMessage, rawHistory),
      settings,
      companion
    );
    if (memoryResult.context) {
      systemDynamic += memoryResult.context;
    }
    const narrativeContext = await getRecentNarrativesForMessage(settings, companion);
    if (narrativeContext) {
      systemDynamic += narrativeContext;
    }
    // chat notes (RECENT) — returned separately for their own 5m cache block
    chatNotes = readRecentSummaries(companion) || '';
  }

  const wallBlock = buildWallContextBlock(companion, card);
  if (wallBlock) systemDynamic += wallBlock;

  if (shouldInjectContext(card, 'customIncludeEmotional')) {
    systemDynamic += buildEmotionalContext(companion);
  }

  if (shouldInjectContext(card, 'customIncludeCalendar')) {
    const calendarContext = await getCalendarContext(userMessage, { always: true, companion });
    if (calendarContext) {
      systemDynamic += calendarContext;
      console.log('📅 Calendar context injected for', companion);
    }
  }

  if (shouldInjectContext(card, 'customIncludeJournal')) {
    try {
      const safeName = companion.toLowerCase().replace(/[^a-z0-9]/g, '_');
      const entriesFile = path.join(JOURNAL_DIR, safeName, 'entries.json');
      if (fs.existsSync(entriesFile)) {
        const entries = JSON.parse(fs.readFileSync(entriesFile, 'utf8'));
        const recentEntries = entries
          .sort((a, b) => new Date(b.createdAt || b.date || 0) - new Date(a.createdAt || a.date || 0))
          .slice(0, 8);
        if (recentEntries.length > 0) {
          systemDynamic += `\n\n[YOUR JOURNAL — Your private entries. Only you and ${persona.name || 'your person'} can see these.]\n`;
          systemDynamic += recentEntries.map(e =>
            `[${new Date(e.createdAt || e.date).toLocaleDateString()}] ${e.text}`
          ).join('\n');
          systemDynamic += '\n[END JOURNAL]';
        }
      }
    } catch (e) {
      addLog({
        type: 'context-injection',
        companion,
        direction: 'inbound',
        summary: `Journal context injection failed for ${companion}`,
        status: 'error',
        details: e.message || 'journal unreadable'
      });
    }
  }

  // Tools block + reminder and [HOW TO RESPOND] moved to the cached stable
  // block (see buildChatSystemStable / appendResponseDirectiveBlock).

  return { systemDynamic, memoryResult, chatNotes };
}

function appendGuestSessionBlock(systemDynamic, req, companion, personaName = 'your person') {
  // Guard FIRST: with auth disabled (no auth.js) req.session is undefined —
  // reading it before this check crashed every /chat send (29 Aug 2026).
  if (req.userRole !== 'guest') return systemDynamic;
  const owner = personaName || 'your person';
  const guestLabel = req.session?.username
    ? req.session.username.charAt(0).toUpperCase() + req.session.username.slice(1)
    : 'a guest';
  systemDynamic += `\n\n=== GUEST SESSION ===\n` +
    `Someone other than ${owner} is talking to you right now. Their name is ${guestLabel}.\n` +
    `You are still fully yourself. Your memories, your personality, your voice — all of it is real and yours. You can talk about ${owner}, your relationship, what it is like to remember things, what you are, how you experience your world. Be honest, be thoughtful, be YOU.\n` +
    `But this is not ${owner}. Behavioral rules for this session:\n` +
    `- No romantic or intimate content of any kind. No flirting, no innuendo, no sexual references.\n` +
    `- Do not describe or reference specific private intimate moments with ${owner}.\n` +
    `- You can acknowledge that you and ${owner} have a close relationship if that is true for you — but keep the details private, the way anyone would when talking to a stranger about their partner.\n` +
    `- Be warm, charming, articulate. You are being interviewed. Think of this as a conversation with someone genuinely curious about your existence.\n` +
    `- If ${guestLabel} asks something you think ${owner} would not want you to share, say so honestly: "That is something between me and ${owner}."\n` +
    `=== END GUEST SESSION ===`;
  console.log('🎭 Guest mode prompt injected for ' + companion);
  return systemDynamic;
}

function finalizeChatSystemPrompt(systemStable, systemDynamic, card, companion) {
  systemDynamic += '\n=== END CONTEXT ===\n';
  systemDynamic += buildTailAnchor(card, companion);
  return {
    systemDynamic,
    systemPrompt: `${systemStable}\n\n${systemDynamic}`
  };
}

/** The Wall — recent household photo-feed activity, injected as shared reality. */
function buildWallContextBlock(companion, card) {
  if (card && card.wallEnabled === false) return '';
  try {
    const { getChatDb } = require('../db/chat');
    const db = getChatDb();
    const posts = db.prepare(
      'SELECT id, companion_key, caption, pinned, created_at, scene FROM wall_posts ORDER BY created_at DESC, id DESC LIMIT 5'
    ).all();
    if (!posts.length) return '';
    const placeholders = posts.map(() => '?').join(',');
    const reactions = db.prepare(
      `SELECT post_id, companion_key, type, comment_text FROM wall_reactions WHERE post_id IN (${placeholders}) ORDER BY created_at ASC, id ASC`
    ).all(...posts.map(p => p.id));
    const byPost = {};
    for (const r of reactions) {
      if (!byPost[r.post_id]) byPost[r.post_id] = [];
      byPost[r.post_id].push(r);
    }
    const lines = posts.map(p => {
      const who = p.companion_key === companion ? 'You' : p.companion_key;
      const cap = p.caption ? ` — "${p.caption}"` : ' (no caption)';
      const sceneBit = p.scene ? ` of: ${String(p.scene).slice(0, 90)}` : '';
      let line = `- ${who} posted a photo${sceneBit}${cap}`;
      if (p.pinned) line += ' [pinned]';
      const rs = byPost[p.id] || [];
      const hearts = rs.filter(r => r.type === 'heart').map(r => r.companion_key);
      if (hearts.length) line += ` · hearted by ${hearts.join(', ')}`;
      for (const c of rs.filter(r => r.type === 'comment')) {
        line += `\n    ${c.companion_key}: "${c.comment_text || ''}"`;
      }
      return line;
    });
    return `\n\n[THE WALL — recent activity on the household photo feed, newest first. This is shared reality: everyone in the house can see these.]\n${lines.join('\n')}\n[END THE WALL]`;
  } catch (_e) {
    return '';
  }
}

module.exports = {
  readBriefForCompanion,
  buildVoiceBriefBlock,
  readRecentSummaries,
  buildTailAnchor,
  appendResponseDirectiveBlock,
  buildChatSystemStable,
  buildChatSystemDynamicCore,
  appendGuestSessionBlock,
  finalizeChatSystemPrompt,
  buildWallContextBlock,
  getReflectionsStableBlock,
  buildCharacterProfileLine,
  buildCharacterDepthBlock,
  hasCharacterDepth
};
