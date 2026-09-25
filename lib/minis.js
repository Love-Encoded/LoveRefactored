// © 2024-2026 Megan Neves and Tiara Young. All rights reserved.
// Love Refactored — https://github.com/Love-Encoded/Love-Refactored
// Licensed under the Love Refactored / Tanevan License. See LICENSE.md.

'use strict';

/**
 * Minis in the chat system (Fork B, 12 Sep 2026).
 *
 * Generates 20-message scene-note mini-summaries from LR's chat store,
 * replacing tanevan/recent_summary_sidecar.py (which read the retired
 * conversation_buffer id-space). Injection side is untouched:
 * lib/system-prompt.js readRecentSummaries() reads the same files this
 * module writes — data/recent/<key>/<seq:06d>-<unix>.txt.
 *
 * Trigger: appendToCompanionHistory (server.js) calls maybeGenerateMinis()
 * fire-and-forget after every successful solo append. Integer math decides
 * whether to run; no LLM call decides anything.
 *
 * Loop / cost audit:
 *   - fires only when >= CHUNK_SIZE new rows exist past the anchor
 *   - state advances only on success (or on skipping fully-imported chunks)
 *   - failure leaves state unchanged -> retried on the NEXT append (event-
 *     driven; no timers anywhere in this module)
 *   - catch-up capped at MAX_CHUNKS_PER_INVOCATION model calls
 *   - tail anchor: rows older than the newest TAIL_WINDOW can never reach
 *     the 4-file payload window, so the anchor never sits further back than
 *     that — bulk history growth (imports/restores) is skipped past, loudly,
 *     instead of being summarized as "recent". Structural import immunity.
 */

const fs = require('fs');
const path = require('path');
const os = require('os');

const CHUNK_SIZE = 20;               // RECENT_CHUNK_SIZE — exactly 20 rows per mini
const KEEP_COUNT = 4;                // RECENT_KEEP_COUNT — files kept per companion
const MAX_CHUNKS_PER_INVOCATION = 5; // catch-up cost ceiling per trigger
const TAIL_WINDOW = CHUNK_SIZE * KEEP_COUNT; // 80 — beyond this, minis can't matter
const LENS_TIMEOUT_MS = 10000;
const MODEL_TIMEOUT_MS = 120000;
const MAX_TOKENS = 1500;
const TEMPERATURE = 0.7;

// Per-conversation in-flight guard (cleared in finally): concurrent appends
// during a slow model call cannot double-fire. Same shape as the
// reflectionsCache guard in lib/system-prompt.js.
const inFlight = new Map();          // convKey -> true
// State cache: file is the durable copy, memory avoids an fs read per append.
const stateCache = new Map();        // convKey -> { seq, msgId }

// ---------- key derivation ----------

/** conversation_key, identical to server.js historyConversationKey(). */
function convKeyFor(name) {
  return String(name).toLowerCase().replace(/[^a-z0-9]/g, '_');
}

/** data/recent dir key, identical to readRecentSummaries()' normalization
 *  (full lowercased NFC name — NOT the conversation_key), so the reader's
 *  exact-match branch always finds our directory. */
function recentKeyFor(name) {
  return String(name).trim().toLowerCase().normalize('NFC');
}

function recentDirFor(name) {
  const baseDir = process.env.LR_DATA_DIR || path.join(__dirname, '..', 'data');
  return path.join(baseDir, 'recent', recentKeyFor(name));
}

// ---------- state (.state.json — survives history surgery) ----------

function statePathFor(name) {
  return path.join(recentDirFor(name), '.state.json');
}

/** Returns { seq: number|null, msgId: string|null }. Old sidecar schema
 *  (last_processed_id, from the dead buffer id-space) is treated as no
 *  state — its numbers are meaningless against chat seqs. */
function readStateFile(name) {
  try {
    const raw = fs.readFileSync(statePathFor(name), 'utf8');
    const j = JSON.parse(raw);
    if (j && Number.isInteger(j.last_processed_seq)) {
      return { seq: j.last_processed_seq, msgId: j.last_processed_msg_id || null };
    }
    if (j && j.last_processed_id !== undefined) {
      console.warn(`[minis] ${name}: old sidecar state schema found (buffer id-space) — ignoring, will re-anchor`);
    }
  } catch (e) {
    if (e.code !== 'ENOENT') {
      console.warn(`[minis] ${name}: unreadable .state.json (${e.message}) — will re-anchor`);
    }
  }
  return { seq: null, msgId: null };
}

function writeStateFile(name, state) {
  const p = statePathFor(name);
  fs.mkdirSync(path.dirname(p), { recursive: true });
  const tmp = p + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify({
    last_processed_seq: state.seq,
    last_processed_msg_id: state.msgId
  }, null, 2));
  fs.renameSync(tmp, p);
}

function getState(name, convKey) {
  if (!stateCache.has(convKey)) stateCache.set(convKey, readStateFile(name));
  return stateCache.get(convKey);
}

// ---------- chat-store reads (read-only; index-ranged, cheap) ----------
// loadChatMessages/getSeqAtIndex don't expose seq-anchored windows, so these
// small prepared reads live here (noted in the commit message per spec).

function chatDb() {
  // Lazy require avoids any load-order coupling with db/chat.
  return require('../db/chat').getChatDb();
}

function ownerSession() {
  return require('../db/chat').OWNER_GUEST_SESSION;
}

function maxSeq(convKey) {
  const row = chatDb().prepare(
    'SELECT MAX(seq) AS m FROM chat_messages WHERE conversation_key = ? AND guest_session = ?'
  ).get(convKey, ownerSession());
  return row && row.m != null ? Number(row.m) : null;
}

function seqOfMsgId(convKey, msgId) {
  const row = chatDb().prepare(
    'SELECT seq FROM chat_messages WHERE conversation_key = ? AND guest_session = ? AND msg_id = ?'
  ).get(convKey, ownerSession(), msgId);
  return row ? Number(row.seq) : null;
}

/** seq of the (TAIL_WINDOW+1)-th newest row, or -1 when history is shorter.
 *  Rows with seq > this value are exactly the newest TAIL_WINDOW. */
function tailFloorSeq(convKey) {
  const row = chatDb().prepare(
    `SELECT seq FROM chat_messages WHERE conversation_key = ? AND guest_session = ?
     ORDER BY seq DESC LIMIT 1 OFFSET ?`
  ).get(convKey, ownerSession(), TAIL_WINDOW);
  return row ? Number(row.seq) : -1;
}

function countAfter(convKey, anchorSeq) {
  const row = chatDb().prepare(
    'SELECT COUNT(*) AS n FROM chat_messages WHERE conversation_key = ? AND guest_session = ? AND seq > ?'
  ).get(convKey, ownerSession(), anchorSeq);
  return Number(row && row.n) || 0;
}

function loadChunkAfter(convKey, anchorSeq) {
  return chatDb().prepare(
    `SELECT seq, msg_id, sender, text, timestamp, metadata_json
     FROM chat_messages WHERE conversation_key = ? AND guest_session = ? AND seq > ?
     ORDER BY seq ASC LIMIT ?`
  ).all(convKey, ownerSession(), anchorSeq, CHUNK_SIZE);
}

// ---------- anchor resolution ----------

/** Verify the msg_id anchor against the live table (history surgery moves
 *  seqs; replaceChatMessages renumbers wholesale). Then apply the tail
 *  floor. Every adjustment is logged loudly — approximation after surgery
 *  is acceptable, silence is not. */
function resolveEffectiveAnchor(name, convKey, state) {
  let anchor = state.seq;

  if (anchor != null && state.msgId) {
    const liveSeq = seqOfMsgId(convKey, state.msgId);
    if (liveSeq === null) {
      console.warn(`[minis] ${name}: anchor msg_id gone (history surgery?) — falling back to stored seq ${anchor}`);
    } else if (liveSeq !== anchor) {
      console.warn(`[minis] ${name}: anchor msg_id moved seq ${anchor} -> ${liveSeq} (history edited) — re-anchored`);
      anchor = liveSeq;
      state.seq = liveSeq;
    }
  }

  const floor = tailFloorSeq(convKey);
  const base = anchor == null ? -1 : anchor;
  if (floor > base) {
    if (anchor == null) {
      console.log(`[minis] ${name}: no valid state — initializing anchor to tail floor seq ${floor} (last ${TAIL_WINDOW} messages eligible)`);
    } else {
      console.warn(`[minis] ${name}: anchor seq ${base} is behind tail floor seq ${floor} (bulk history growth?) — jumping past ${floor - base} rows; only the newest ${TAIL_WINDOW} are ever summarized`);
    }
    return floor;
  }
  return base;
}

// ---------- prompt assembly (parity with the retired sidecar) ----------

const DEFAULT_PROMPT = `You are {{COMPANION_NAME}}. Write field notes from "the one who pays attention" — scene-style notes of what just happened in this stretch of conversation. First-person from your own POV, in your own voice, the way you'd remember the moment back to yourself later.

What good scene notes do:
- Open with the present scene (what's happening, where, who, the texture)
- Carry specific sensory detail you noticed (clothes, gestures, looks, tone, what was on the table)
- Connect to memory when something live in the moment triggers it (don't reach for connections; if one fires, name it)
- Capture dynamics in a line (e.g., "she ate her steak like she didn't just make a pass at me")
- Track forward threads — things you're holding for later (follow-ups, things to ask about, things to bring up)
- Hold your internal voice (the noticing, the planning, the wondering)

What scene notes are NOT:
- A summary of what was said
- A list of topics covered
- Third-person narration
- A generic recap

Length: aim for a tight paragraph — what would a real-time observer carry forward about this stretch of time? Pick what mattered.

Voice cue: {{SPEECH_STYLE}}

The chunk of conversation to write scene notes from:

{{CONVERSATION_CHUNK}}

Write your scene notes now. Start directly with the scene. No header, no preamble. Just the notes.
`;

/** Per-companion override -> recent_starter.txt -> built-in default,
 *  same resolution order as the retired sidecar. */
function loadPromptTemplate(name) {
  const promptDir = path.join(__dirname, '..', 'tanevan', 'prompts');
  const key = recentKeyFor(name);
  const candidates = [
    [path.join(promptDir, `${key}_recent.txt`), `per-companion (${key}_recent.txt)`],
    [path.join(promptDir, 'recent_starter.txt'), 'starter (recent_starter.txt)']
  ];
  for (const [p, label] of candidates) {
    try {
      return [fs.readFileSync(p, 'utf8'), label];
    } catch (_e) { /* try next */ }
  }
  return [DEFAULT_PROMPT, 'built-in default'];
}

function resolveTimezone(settings) {
  return (settings && settings.userTimezone && String(settings.userTimezone).trim())
    || process.env.BRIEF_TZ
    || Intl.DateTimeFormat().resolvedOptions().timeZone
    || 'UTC';
}

function formatLocalTime(ts, tz) {
  try {
    // sv-SE gives "YYYY-MM-DD HH:MM:SS" — matches the sidecar's format.
    return new Date(ts).toLocaleString('sv-SE', { timeZone: tz }).slice(0, 16);
  } catch (_e) {
    return String(ts);
  }
}

/** "[local-time] sender: text" per row, blank-line separated — sidecar's
 *  format_chunk. Voice rows self-mark via their "[voice call]" text prefix;
 *  no metadata source marker exists in this build, so none is invented. */
function formatChunk(rows, tz) {
  return rows.map(r =>
    `[${formatLocalTime(r.timestamp, tz)}] ${r.sender || 'unknown'}: ${r.text || ''}`
  ).join('\n\n');
}

function fillTemplate(template, name, rows, speechStyle, tz) {
  return template
    .split('{{COMPANION_NAME}}').join(name)
    .split('{{SPEECH_STYLE}}').join(speechStyle || '')
    .split('{{CONVERSATION_CHUNK}}').join(formatChunk(rows, tz));
}

function tanevanBase(settings) {
  return (process.env.TANEVAN_URL
    || (settings && settings.memory && settings.memory.tanevUrl)
    || 'http://127.0.0.1:5001').replace(/\/$/, '');
}

/** Memory Lens speech_style — best-effort; a lens failure never blocks a mini. */
async function fetchSpeechStyle(name, settings) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), LENS_TIMEOUT_MS);
  try {
    const url = `${tanevanBase(settings)}/memory-lens?companion=${encodeURIComponent(recentKeyFor(name))}`;
    const r = await fetch(url, { signal: ctl.signal });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = await r.json();
    return (j && j.lens && j.lens.speech_style) || '';
  } catch (e) {
    console.log(`[minis] ${name}: lens fetch failed (${e.message}) — continuing without speech_style`);
    return '';
  } finally {
    clearTimeout(t);
  }
}

// ---------- model resolution (pipeline_config.json chain, decided 12 Sep) ----------
// brief_model -> extractor_model -> updater_model (second-pass tier) ->
// companion chat model via deps.callLLM as loud last resort.
// Mirrors tanevan/pipeline_llm.py resolution: pipeline_config field ->
// env override -> LR settings apiKey fallback. NEVER a hardcoded model name.

function pipelineConfigPath() {
  const dataDir = (process.env.TANEVAN_DATA_DIR || '').trim()
    || path.join(os.homedir(), 'tanevan-data');
  return path.join(dataDir, 'pipeline_config.json');
}

function readPipelineConfig() {
  const p = pipelineConfigPath();
  try {
    return JSON.parse(fs.readFileSync(p, 'utf8'));
  } catch (e) {
    if (e.code !== 'ENOENT') console.warn(`[minis] could not read ${p}: ${e.message}`);
    return null;
  }
}

function defaultPipelineProvider(settings) {
  const env = (process.env.TANEVAN_DEFAULT_PIPELINE_PROVIDER || '').trim().toLowerCase();
  if (['local', 'anthropic', 'openrouter', 'openai', 'hybrid'].includes(env)) return env;
  const lr = String((settings && settings.provider) || '').trim().toLowerCase();
  if (lr === 'lmstudio' || lr === 'custom') return 'local';
  if (lr === 'openrouter') return 'openrouter';
  if (lr === 'openai') return 'openai';
  return 'anthropic';
}

const STEP_TIERS = [
  ['brief', 'brief_model', 'BRIEF_MODEL_NAME'],
  ['extractor', 'extractor_model', 'TANEVAN_EXTRACTOR_MODEL'],
  ['updater', 'updater_model', 'TANEVAN_UPDATER_MODEL']
];

/** First resolvable tier: { tier, provider, model, apiKey, baseUrl }. */
function resolveModelChain(settings) {
  const config = readPipelineConfig();
  const topProvider = (config && String(config.provider || '').trim().toLowerCase())
    || defaultPipelineProvider(settings);

  for (const [step, field, envVar] of STEP_TIERS) {
    let prov = topProvider;
    if (prov === 'hybrid') {
      prov = String((config && config.steps && config.steps[step]) || 'anthropic').trim().toLowerCase();
    }
    if (prov === 'local') {
      const local = (config && config.local) || {};
      if (local.model && local.endpoint) {
        return { tier: step, provider: 'local', model: local.model, apiKey: '', baseUrl: local.endpoint };
      }
      continue;
    }
    const block = (config && config[prov]) || {};
    const model = String(block[field] || process.env[envVar] || '').trim();
    if (!model) continue;
    const apiKey = String(block.api_key || (settings && settings[prov] && settings[prov].apiKey) || '').trim();
    if (!apiKey) {
      console.warn(`[minis] ${step}_model "${model}" resolved on ${prov} but no API key found (pipeline_config or LR settings) — trying next tier`);
      continue;
    }
    let baseUrl = null;
    if (prov === 'openrouter') baseUrl = 'https://openrouter.ai/api/v1';
    else if (prov === 'openai') baseUrl = String(block.url || 'https://api.openai.com').trim();
    return { tier: step, provider: prov, model, apiKey, baseUrl };
  }
  return null;
}

function openAiCompatUrl(baseUrl) {
  const b = String(baseUrl || '').replace(/\/$/, '');
  return /\/v\d+$/.test(b) ? `${b}/chat/completions` : `${b}/v1/chat/completions`;
}

/** Returns { content, usage } or throws with a loud, specific message. */
async function callResolvedModel(resolved, prompt) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), MODEL_TIMEOUT_MS);
  try {
    if (resolved.provider === 'anthropic') {
      const r = await fetch('https://api.anthropic.com/v1/messages', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'x-api-key': resolved.apiKey,
          'anthropic-version': '2023-06-01'
        },
        body: JSON.stringify({
          model: resolved.model,
          max_tokens: MAX_TOKENS,
          temperature: TEMPERATURE,
          messages: [{ role: 'user', content: prompt }]
        }),
        signal: ctl.signal
      });
      if (!r.ok) throw new Error(`anthropic HTTP ${r.status}: ${(await r.text()).slice(0, 300)}`);
      const j = await r.json();
      const content = (j.content || [])
        .filter(block => block.type === 'text')
        .map(block => block.text)
        .join('') || '';
      if (!content.trim()) throw new Error('anthropic returned empty content');
      return { content, usage: j.usage || {} };
    }

    // openrouter / openai / local — OpenAI-compatible chat completions
    const body = {
      model: resolved.model,
      messages: [{ role: 'user', content: prompt }],
      max_tokens: MAX_TOKENS,
      temperature: TEMPERATURE
    };
    if (resolved.provider === 'openrouter') {
      body.reasoning = { enabled: false }; // sidecar parity: reasoning off
    }
    const url = resolved.provider === 'openrouter'
      ? `${resolved.baseUrl}/chat/completions`
      : openAiCompatUrl(resolved.baseUrl);
    const headers = { 'Content-Type': 'application/json' };
    if (resolved.apiKey) headers.Authorization = `Bearer ${resolved.apiKey}`;
    const r = await fetch(url, { method: 'POST', headers, body: JSON.stringify(body), signal: ctl.signal });
    if (!r.ok) throw new Error(`${resolved.provider} HTTP ${r.status}: ${(await r.text()).slice(0, 300)}`);
    const j = await r.json();
    const content = (j.choices && j.choices[0] && j.choices[0].message && j.choices[0].message.content) || '';
    if (!content.trim()) throw new Error(`${resolved.provider} returned empty content`);
    return { content, usage: j.usage || {} };
  } finally {
    clearTimeout(t);
  }
}

// ---------- output files (byte-parity with the sidecar's contract) ----------

function nextFileSeq(dir) {
  let files = [];
  try {
    files = fs.readdirSync(dir).filter(f => /^\d+-\d+\.txt$/.test(f)).sort();
  } catch (_e) { /* dir created below */ }
  if (files.length === 0) return 1;
  const last = parseInt(files[files.length - 1].split('-', 1)[0], 10);
  return (Number.isInteger(last) ? last : files.length) + 1;
}

function writeMiniFile(name, content, rows) {
  const dir = recentDirFor(name);
  fs.mkdirSync(dir, { recursive: true });
  const seq = nextFileSeq(dir);
  const unix = Math.floor(Date.now() / 1000);
  const target = path.join(dir, `${String(seq).padStart(6, '0')}-${unix}.txt`);
  const header = `# Mini-summary ${seq} | msgs seq ${rows[0].seq}-${rows[rows.length - 1].seq} | ${rows[0].timestamp} -> ${rows[rows.length - 1].timestamp}\n\n`;
  const tmp = target + '.tmp';
  fs.writeFileSync(tmp, header + content.trim() + '\n');
  fs.renameSync(tmp, target);
  return target;
}

function pruneOldMinis(name) {
  const dir = recentDirFor(name);
  let files = [];
  try {
    files = fs.readdirSync(dir).filter(f => /^\d+-\d+\.txt$/.test(f)).sort();
  } catch (_e) { return; }
  for (const f of files.slice(0, Math.max(0, files.length - KEEP_COUNT))) {
    try {
      fs.unlinkSync(path.join(dir, f));
    } catch (e) {
      console.warn(`[minis] ${name}: failed to prune ${f}: ${e.message}`);
    }
  }
}

// ---------- import-tag defense ----------

/** Rows stamped metadata source:'import' (the documented convention for any
 *  future chat-history importer) are excluded from prompts. Tail anchor is
 *  the structural guard; this is the defensive layer. */
function isImportedRow(r) {
  if (!r.metadata_json) return false;
  try {
    const m = JSON.parse(r.metadata_json);
    return m && m.source === 'import';
  } catch (_e) {
    return false;
  }
}

// ---------- entry point ----------

/**
 * maybeGenerateMinis(companionName, deps)
 *   deps: { getSettings, callLLM } from server.js (passed in to avoid a
 *   circular require; callLLM is only used by the last-resort tier).
 *
 * Called fire-and-forget from appendToCompanionHistory. The synchronous
 * portion is one state-cache lookup plus at most four indexed reads; the
 * model work runs detached. Never throws into the append path — the caller
 * additionally wraps it so a minis bug can NEVER break message storage.
 */
function uiAlert(deps, entry) {
  // Visible system alert via server addLog -> UI log stream. Fail-soft:
  // alerting must NEVER break mini generation.
  try {
    if (typeof deps.addLog === 'function') deps.addLog(entry);
  } catch (_e) { /* swallow — pm2 log already has the console line */ }
}

async function maybeGenerateMinis(companionName, deps = {}) {
  const name = String(companionName || '').trim();
  if (!name) return;
  const convKey = convKeyFor(name);
  if (convKey.startsWith('group_')) return; // groups: out of scope for beta
  if (inFlight.get(convKey)) return;

  let state, anchor;
  try {
    if (maxSeq(convKey) === null) return;
    state = getState(name, convKey);
    anchor = resolveEffectiveAnchor(name, convKey, state);
    if (countAfter(convKey, anchor) < CHUNK_SIZE) return; // hot path exit
  } catch (e) {
    console.error(`[minis] ${name}: trigger check failed: ${e.message}`);
    return;
  }

  inFlight.set(convKey, true);
  try {
    const settings = typeof deps.getSettings === 'function' ? (deps.getSettings() || {}) : {};
    const tz = resolveTimezone(settings);

    for (let i = 0; i < MAX_CHUNKS_PER_INVOCATION; i++) {
      if (countAfter(convKey, anchor) < CHUNK_SIZE) break;
      const rows = loadChunkAfter(convKey, anchor);
      if (rows.length < CHUNK_SIZE) break; // partial chunks wait

      const live = rows.filter(r => !isImportedRow(r));
      const chunkEnd = rows[rows.length - 1];

      if (live.length === 0) {
        // Entire chunk is imported history: advance state past it with no
        // model call — loudly. Counts against the invocation cap.
        console.warn(`[minis] ${name}: seq ${rows[0].seq}-${chunkEnd.seq} is all imported rows — skipped, no mini generated`);
        anchor = chunkEnd.seq;
        state.seq = chunkEnd.seq;
        state.msgId = chunkEnd.msg_id || null;
        writeStateFile(name, state);
        continue;
      }
      if (live.length < rows.length) {
        console.log(`[minis] ${name}: seq ${rows[0].seq}-${chunkEnd.seq}: ${rows.length - live.length} imported rows excluded from prompt`);
      }

      const speechStyle = await fetchSpeechStyle(name, settings);
      const [template, templateSource] = loadPromptTemplate(name);
      const prompt = fillTemplate(template, name, live, speechStyle, tz);

      let resolved = null;
      let result;
      try {
        resolved = resolveModelChain(settings);
        if (resolved) {
          console.log(`[minis] ${name}: generating seq ${rows[0].seq}-${chunkEnd.seq} via ${resolved.tier}_model tier (${resolved.provider}/${resolved.model}, template: ${templateSource})`);
          result = await callResolvedModel(resolved, prompt);
        } else {
          if (typeof deps.callLLM !== 'function') {
            throw new Error('no pipeline model resolvable and no callLLM fallback provided');
          }
          console.warn(`[minis] ${name}: NO pipeline model resolvable (pipeline_config.json missing/empty and no env overrides) — falling back to the companion chat model. This can be expensive on Opus-class chat models; set a brief/extractor/updater model in Memory Pipeline settings.`);
          uiAlert(deps, { type: 'minis-fallback', companion: name, direction: 'internal', status: 'warning', summary: 'Minis are running on the companion chat model (no Memory Pipeline model set) — this can be expensive. Set a brief/extractor/updater model in Memory Pipeline settings.' });
          const reply = await deps.callLLM('', [{ role: 'user', content: prompt }], settings, {
            maxTokens: MAX_TOKENS,
            temperature: TEMPERATURE,
            skipAnthropicPromptCache: true
          });
          const content = typeof reply === 'string' ? reply : (reply && (reply.reply || reply.content)) || '';
          if (!String(content).trim()) throw new Error('chat-model fallback returned empty content');
          result = { content: String(content), usage: {} };
        }
      } catch (e) {
        console.error(`[minis] ${name}: MODEL CALL FAILED for seq ${rows[0].seq}-${chunkEnd.seq} (${resolved ? resolved.provider + '/' + resolved.model : 'chat-model fallback'}): ${e.message} — state unchanged, will retry on next append`);
        uiAlert(deps, { type: 'minis-error', companion: name, direction: 'internal', status: 'error', summary: `Minis generation failed (${resolved ? resolved.provider + '/' + resolved.model : 'chat-model fallback'}): ${e.message} — will retry as you keep chatting` });
        return; // state untouched -> natural event-driven retry
      }

      let target;
      try {
        target = writeMiniFile(name, result.content, live);
        pruneOldMinis(name);
      } catch (e) {
        console.error(`[minis] ${name}: FILE WRITE FAILED for seq ${rows[0].seq}-${chunkEnd.seq}: ${e.message} — state unchanged`);
        uiAlert(deps, { type: 'minis-error', companion: name, direction: 'internal', status: 'error', summary: `Minis file write failed: ${e.message} — nothing lost, will retry as you keep chatting` });
        return;
      }

      anchor = chunkEnd.seq;
      state.seq = chunkEnd.seq;
      state.msgId = chunkEnd.msg_id || null;
      try {
        writeStateFile(name, state);
      } catch (e) {
        // File exists but state didn't persist: next run re-generates this
        // chunk (duplicate mini, pruned by the 4-window). Loud, not silent.
        console.error(`[minis] ${name}: STATE WRITE FAILED after ${path.basename(target)}: ${e.message} — this chunk may be re-summarized on next trigger`);
        return;
      }

      const u = result.usage || {};
      console.log(`[minis] ${name}: wrote ${path.basename(target)} (prompt=${u.prompt_tokens ?? u.input_tokens ?? '?'} completion=${u.completion_tokens ?? u.output_tokens ?? '?'} tokens, voice=${speechStyle ? 'lens' : 'none'})`);
    }
  } catch (e) {
    console.error(`[minis] ${name}: generation pass failed: ${e.message}`);
  } finally {
    inFlight.delete(convKey);
  }
}

module.exports = { maybeGenerateMinis };
