'use strict';

/**
 * Scene resolver — turns "where are we" into a world-book entry the image writer can draw.
 *
 * Reads lorebooks of type 'world' (see README → World books). Pure functions:
 * nothing here touches the network, the LLM, or server state. Callers pass getAllLorebooks in.
 *
 * resolveScene() outcomes:
 *   known   — a world-book place matched (sceneHint first, then history newest → oldest)
 *   wild    — a LOCATION_CUES word matched before any world-book keyword did (they're somewhere
 *             the book doesn't know) → caller keeps today's free-text behavior
 *   default — nothing said anywhere → the book's isDefault place
 *   none    — no world book applies and no cue word either → caller changes nothing
 */

const fs = require('fs');
const path = require('path');
// LOCATION_CUES lives in image-prompts.js and is not exported. extractLatestLocation([row])
// returns row.text when that regex hits and '' otherwise, so we use it as a per-row test.
const { extractLatestLocation, LOCATION_CUES } = require('./image-prompts');

const MAX_HISTORY_ROWS = 40;
const FACE_REF_CUTOFF = 8;     // at/above this many face refs, send no setting refs (NBP fidelity degrades past ~10)
const MAX_SETTING_REFS = 2;

function normalizeText(text) {
  return String(text || '')
    .normalize('NFKC')
    .toLowerCase()
    .replace(/[_-]+/g, ' ')
    .replace(/[^\p{L}\p{N}\s']/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function entryKeywords(entry) {
  const raw = entry && entry.keywords;
  const list = Array.isArray(raw) ? raw : (typeof raw === 'string' ? raw.split(',') : []);
  return list
    .map(k => (typeof k === 'string' ? k : ((k && (k.value || k.name)) || '')))
    .map(k => normalizeText(k))
    .filter(Boolean);
}

// Cue words (LOCATION_CUES). Scene lines — a [photo:] tag or the studio box — are terse and deliberate, so a
// bare cue word counts there ("kitchen counter, coffee in hand"). Chat prose is noisy ("at this stage", "raise
// the bar", "car keys"), so in history a cue only counts when it is used AS a place: after a locative
// preposition, or one of the few words that are places on their own.
// LOCATION_CUES here also feeds the legacy location anchor and is wider than the list this resolver was tuned on:
// it carries words that mean "this house" or a feature of a room. Those are not evidence of being somewhere the
// world book doesn't know, so the resolver leaves them out ("at home" must not block the house default).
const NOT_ELSEWHERE = new Set(['home', 'my place', 'your place', 'window']);
const CUE_ALT = String((LOCATION_CUES && LOCATION_CUES.source) || '').replace(/^\\b\(/, '').replace(/\)\\b$/, '')
  .split('|').filter(w => !NOT_ELSEWHERE.has(w)).join('|');
const CUE_ANY = CUE_ALT ? new RegExp(`\\b(${CUE_ALT})\\b`, 'i') : null;
const CUE_BARE = /\b(backstage|onstage|on stage|outside|outdoors|indoors)\b/i;
const LOCATIVE_PREPS = 'in|at|on|to|into|onto|inside|by|near|from|across|around|behind|under|beneath|toward|towards|off|through|out of|back to|over to|down to|up to';
const CUE_LOCATIVE = CUE_ALT
  ? new RegExp(`\\b(?:${LOCATIVE_PREPS})\\s+(?:the|my|our|your|his|her|their|a|an)?\\s*(${CUE_ALT})\\b`, 'i')
  : null;
const escapeRe = (s) => String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const locativeCache = new Map();
/** Place keywords in chat prose must be used AS a place — "into the kitchen", "to bed", "in nova's room" — the same
 *  rule cue words follow. "kitchen sink records", "garage band", "sam's room is a mess" are mentions, not locations. */
function keywordLocative(keyword, normText) {
  let re = locativeCache.get(keyword);
  if (!re) { re = new RegExp(`\\b(?:${LOCATIVE_PREPS})\\s+(?:the|my|our|your|his|her|their|a|an)?\\s*${escapeRe(keyword)}\\b`); locativeCache.set(keyword, re); }
  return re.test(normText);
}

/** The cue word that fired, or ''. `strict` = chat-prose rules (see above). */
function cueIn(text, { strict = false } = {}) {
  const t = String(text || '');
  if (!t) return '';
  if (!CUE_ANY) return extractLatestLocation([{ text: t }]) ? 'cue' : '';   // safety net if the export is ever missing
  if (!strict) { const m = CUE_ANY.exec(t); return m ? m[1].toLowerCase() : ''; }
  const bare = CUE_BARE.exec(t);
  if (bare) return bare[1].toLowerCase();
  const loc = CUE_LOCATIVE.exec(t);
  return loc ? loc[1].toLowerCase() : '';
}

/** Whole-word match of an already-normalized keyword inside normalized text. */
function keywordInText(keyword, normText) {
  if (!keyword || !normText) return false;
  return (' ' + normText + ' ').includes(' ' + keyword + ' ');
}

function speakerOf(row) {
  const s = String((row && row.sender) || '').trim();
  return (!s || s.toLowerCase() === 'user') ? 'user' : s;
}

function isPlace(e) { return !e.kind || e.kind === 'place'; }

/** How well a matched keyword fits the speaker. 0 = excluded. Longer keywords win ties ("our bathroom" over "bathroom").
 *  owner 'shared' = anyone in the house: "my …" and "our …" both reach it. Otherwise "our …" needs the speaker on the
 *  owner list — a housemate saying "our bed" must not land in someone else's bed. */
/** Owners of an entry, normalized: `owner` may be a companion name, 'user', 'shared', or an array of those. */
function ownersOf(entry) {
  const raw = entry && entry.owner;
  const list = Array.isArray(raw) ? raw : ((raw == null || raw === '') ? [] : [raw]);
  return list.map(x => String(x || '').trim()).filter(Boolean);
}

function keywordScore(keyword, entry, speaker, frame) {
  const owners = ownersOf(entry);
  const named = owners.filter(o => o !== 'user' && o !== 'shared');
  const words = keyword.split(' ').length;
  const inFrame = (frame && named.some(o => frame.has(o))) ? 3 : 0;
  if (/^my\b/.test(keyword)) return owners.includes(speaker) ? 25 + words : (owners.includes('shared') ? 20 + words : 0);
  if (/^(his|her|their)\b/.test(keyword)) return named.some(o => o !== speaker) ? 20 + words + inFrame : 0;
  if (/^our\b/.test(keyword)) return (owners.length === 0 || owners.includes('shared') || owners.includes(speaker)) ? 15 + words : 0;
  return 10 + words + inFrame;
}

/** World books that apply to a companion: enabled, type 'world', and (no companions OR includes them).
 *  Household books sort before shared no-companion books so a house default beats a shared one. */
function worldBooksFor(companionName, getAllLorebooks) {
  let books = [];
  try { books = (typeof getAllLorebooks === 'function' ? getAllLorebooks() : []) || []; } catch (_e) { books = []; }
  const name = String(companionName || '').trim();
  const members = (b) => (Array.isArray(b.companions) ? b.companions : []).map(x => String(x || '').trim()).filter(Boolean);
  return books
    .filter(b => b && b.enabled !== false && b.type === 'world' && Array.isArray(b.entries))
    .filter(b => { const m = members(b); return m.length === 0 || m.includes(name); })
    .sort((a, b) => (members(a).length ? 0 : 1) - (members(b).length ? 0 : 1));
}

function liveEntries(books) {
  const out = [];
  for (const b of books) for (const e of b.entries) if (e && e.enabled !== false) out.push({ entry: e, book: b });
  return out;
}

/** Every entry with a keyword in `text`. `strict` = chat-prose rules: a place keyword only counts in locative
 *  position (see keywordLocative); pets and objects are names and count anywhere. */
function matchEntries(text, pairs, speaker, frame, { strict = false } = {}) {
  const norm = normalizeText(text);
  if (!norm) return [];
  const hits = [];
  for (const { entry, book } of pairs) {
    let score = -1, keyword = '';
    const needLocative = strict && isPlace(entry);
    for (const kw of entryKeywords(entry)) {
      if (!keywordInText(kw, norm)) continue;
      if (needLocative && !keywordLocative(kw, norm)) continue;
      const s = keywordScore(kw, entry, speaker, frame);
      if (s > score) { score = s; keyword = kw; }
    }
    if (score >= 0) hits.push({ entry, book, score, keyword });
  }
  return hits;
}

function pickPlace(hits) {
  let best = null;
  for (const h of hits) {
    if (!isPlace(h.entry) || h.score <= 0) continue;
    if (!best || h.score > best.score) best = h;
  }
  return best;
}

/** Clothing is worn only by its owner (clothing_v2), and an owned object travels only with its owner (objects_v2).
 *  Both are dropped from the frame unless an owner is in it — a named companion in `frame`, or the user when
 *  `userInFrame`. Clothing with no owner (or 'shared') is worn by nobody; an object with no owner belongs to the house. */
function wearableHere(entry, frame, userInFrame) {
  if (!entry || (entry.kind !== 'clothing' && entry.kind !== 'object')) return true;
  const owners = ownersOf(entry);
  if (entry.kind === 'object' && (!owners.length || owners.includes('shared'))) return true;
  for (const o of owners) {
    if (o === 'user') { if (userInFrame) return true; continue; }
    if (o === 'shared') continue;
    if (frame && frame.has(o)) return true;
  }
  return false;
}

function propsFrom(hits, seen, frame, userInFrame) {
  const out = [];
  for (const h of hits) {
    if (isPlace(h.entry) || h.score <= 0) continue;
    if (!wearableHere(h.entry, frame, userInFrame)) { console.log(`👗 ${h.entry.kind} skipped: "${labelOf(h.entry)}" belongs to ${ownerLabel(h.entry)}, who is not in this photo`); continue; }
    const id = h.entry.id || h.entry.name || JSON.stringify(h.entry.keywords);
    if (seen.has(id)) continue;
    seen.add(id);
    out.push(h.entry);
  }
  return out;
}

/** Where a photo lands when nobody said where. A person's own room beats the house default; the house
 *  default (isDefault) is for people who have no room of their own here. 'shared' does not count as
 *  ownership for this purpose — list the actual people. */
function defaultPlace(books, anchor) {
  const places = [];
  for (const b of books) for (const e of (b.entries || [])) if (e && e.enabled !== false && isPlace(e)) places.push({ entry: e, book: b });
  const house = places.find(p => p.entry.isDefault === true) || null;
  const who = String(anchor || '').trim();
  if (house && who && ownersOf(house.entry).includes(who)) return house;
  const own = who ? places.filter(p => ownersOf(p.entry).includes(who)) : [];
  if (own.length) return own.find(p => p.entry.isDefault === true) || own[0];
  if (house) return house;
  return places.find(p => !ownersOf(p.entry).length) || null;
}

function labelOf(e) { return (e && (e.name || (Array.isArray(e.keywords) && e.keywords[0]) || e.id)) || 'the setting'; }

function resolveScene({ sceneHint = '', history = [], companionsInFrame = [], speaker = 'user', userInFrame = false, getAllLorebooks } = {}) {
  const anchor = String((Array.isArray(companionsInFrame) ? companionsInFrame[0] : companionsInFrame) || '').trim();
  const frameSet = new Set((Array.isArray(companionsInFrame) ? companionsInFrame : [companionsInFrame]).map(x => String(x || '').trim()).filter(Boolean));
  const books = worldBooksFor(anchor, getAllLorebooks);
  const pairs = liveEntries(books);
  const seenProps = new Set();
  const result = { kind: 'none', world: null, place: null, props: [], refs: [], anchorText: '', matchedBy: null, cue: '', keyword: '', said: '', log: '' };
  const setPlace = (hit, via, said) => {
    result.place = hit.entry;
    result.world = { id: hit.book.id || null, name: hit.book.name || '' };
    result.matchedBy = via;
    result.keyword = hit.keyword || '';
    result.said = String(said || '');
  };

  // 1. The scene line itself — the companion's tag text, or what the user typed in the studio box.
  const hint = String(sceneHint || '');
  if (hint.trim()) {
    const hits = pairs.length ? matchEntries(hint, pairs, speaker, frameSet) : [];
    result.props.push(...propsFrom(hits, seenProps, frameSet, userInFrame));
    const p = pickPlace(hits);
    if (p) { result.kind = 'known'; setPlace(p, 'sceneHint', hint); }
    else { const c = cueIn(hint); if (c) { result.kind = 'wild'; result.anchorText = hint; result.matchedBy = 'sceneHint'; result.cue = c; } }
  }

  // 2. History, newest first. Props are collected on the way; the first row with a place keyword
  //    OR a cue word is where they are now (if nothing decided it yet) and the walk stops there.
  const rows = (Array.isArray(history) ? history : []).slice(-MAX_HISTORY_ROWS);
  for (let i = rows.length - 1; i >= 0; i--) {
    const row = rows[i];
    const text = String((row && row.text) || '');
    if (!text.trim()) continue;
    const hits = pairs.length ? matchEntries(text, pairs, speakerOf(row), frameSet, { strict: true }) : [];
    result.props.push(...propsFrom(hits, seenProps, frameSet, userInFrame));
    const p = pickPlace(hits);
    const cue = p ? '' : cueIn(text, { strict: true });
    if (cue && result.kind === 'none') result.cue = cue;
    if (!p && !cue) continue;
    if (result.kind === 'none') {
      if (p) { result.kind = 'known'; setPlace(p, 'history', speakerOf(row) + ': ' + text); }
      else { result.kind = 'wild'; result.anchorText = text; result.matchedBy = 'history'; }
    }
    break;
  }

  // 3. Silence → the book's default place.
  if (result.kind === 'none') {
    const d = defaultPlace(books, anchor);
    if (d) { result.kind = 'default'; setPlace(d, 'default'); }
  }

  // The menu for the image writer (writer_place_v1): every place in this house, so the model that reads the whole
  // conversation decides where they are. Not offered when the scene line itself named the place — a tag, or what
  // the user typed, is the person saying where they are, and it already wins.
  result.menu = pairs.filter(p => isPlace(p.entry));
  result.menuDefault = result.menu.length ? defaultPlace(books, anchor) : null;
  // menu_on_wild_hint_v1: withhold the menu only when the scene line itself named a KNOWN place. A cue word in a tag that
  // isn't one of the entry's keywords ("sprawled on the couch" when the living room is keyed "living room") used to go
  // wild with no menu — freelance living room. Now the writer gets the menu and can place it.
  result.menuOffered = result.menu.length > 0 && !(result.matchedBy === 'sceneHint' && result.kind === 'known');

  const refs = [];
  const pushRefs = (e) => { for (const r of (Array.isArray(e && e.refs) ? e.refs : [])) { const f = path.basename(String(r || '').trim()); if (f && !refs.includes(f)) refs.push(f); } };
  if (result.place) pushRefs(result.place);
  for (const e of result.props) pushRefs(e);
  result.refs = refs;

  const placeLabel = result.place ? ` "${labelOf(result.place)}"` : '';
  const propLabel = result.props.length ? ` +${result.props.map(labelOf).join(', ')}` : '';
  const wildLabel = result.kind === 'wild' ? ` (cue: "${result.cue}") — "${result.anchorText.slice(0, 60)}"` : '';
  const knownLabel = result.kind === 'known' ? ` (keyword: "${result.keyword}") — "${result.said.replace(/\s+/g, ' ').slice(0, 80)}"` : '';
  result.log = `📍 scene: ${result.kind}${placeLabel}${result.matchedBy ? ' via ' + result.matchedBy : ''}${result.world ? ` (world: ${result.world.name})` : ''}${propLabel}${wildLabel}${knownLabel}`;
  return result;
}

function ownerLabel(entry) {
  const o = ownersOf(entry);
  if (!o.length || o.includes('shared')) return 'everyone';
  return o.map(x => x === 'user' ? 'the user' : x).join(', ');
}

/** The tag after a prop's name in the writer's text. Clothing says whose it is and that it is worn, not placed. */
function propTag(e) {
  if (!e || !e.kind || e.kind === 'place') return '';
  if (e.kind === 'clothing') return ` (clothing — ${ownerLabel(e)}'s, worn by ${ownerLabel(e)} and by no one else in this photo, fitted to their body)`;
  return ` (${e.kind})`;
}

/** The KNOWN PLACES menu (writer_place_v1): the writer reads the conversation and answers PLACE: <name> on its first line. */
function buildPlaceMenu(scene) {
  const menu = Array.isArray(scene.menu) ? scene.menu : [];
  if (!menu.length) return '';
  const maxProse = 700;
  const items = menu.map((p, i) => {
    const e = p.entry;
    const kws = entryKeywords(e).filter(k => k !== normalizeText(labelOf(e))).slice(0, 5);
    let prose = String(e.prompt || '').replace(/\s+/g, ' ').trim();
    if (prose.length > maxProse) prose = prose.slice(0, maxProse).replace(/\s+\S*$/, '') + '…';
    return `${i + 1}. "${labelOf(e)}"${kws.length ? ` — also called: ${kws.join(', ')}` : ''}. Whose: ${ownerLabel(e)}.${prose ? `\n   ${prose}` : ''}`;
  });
  const dflt = scene.menuDefault && scene.menuDefault.entry ? labelOf(scene.menuDefault.entry) : '';
  let guess = 'Keyword guess: nothing matched.';
  if (scene.kind === 'known' && scene.place) guess = `Keyword guess: "${labelOf(scene.place)}" — it matched the word "${scene.keyword}" in "${String(scene.said || '').replace(/\s+/g, ' ').slice(0, 120)}". It cannot tell a mention from a location; you can.`;
  else if (scene.kind === 'wild') guess = `Keyword guess: somewhere not in this list — it saw "${scene.cue}" in "${String(scene.anchorText || '').replace(/\s+/g, ' ').slice(0, 120)}".`;
  else if (scene.kind === 'default') guess = 'Keyword guess: nothing in the conversation placed them anywhere.';
  const props = (scene.props || []).map(e => { const prose = String(e.prompt || '').trim(); return prose ? `${labelOf(e)}${propTag(e)}: ${prose}` : ''; }).filter(Boolean);
  return `\n\n=== KNOWN PLACES (this house) ===\nDecide where the people in this photo physically are at this moment, from the conversation above. A place that is merely mentioned does not count — a band or label name, a memory, a plan, somewhere another person is. If the last messages put them somewhere, that is where they are.${dflt ? ` If nothing says, they are in: "${dflt}".` : ''} If they are somewhere not listed below (a venue, a car, outside), answer NONE and describe it yourself.\n${guess}\n\n${items.join('\n')}\n=== END KNOWN PLACES ===\nTHE FIRST LINE OF YOUR REPLY is exactly: PLACE: <one name from the list, in quotes> or PLACE: NONE — this line is required even though the prompt otherwise has no preamble. Then the prompt. Build the setting only from the chosen place's words (layout, furniture, surfaces) and add only the light and the moment; do not add features it doesn't name. Light the person with light that room actually has — its windows and lamps — so person and room share one light setup, and say the room falls softly out of focus behind the subject at the chosen aperture.${props.length ? `\nIn the frame, exact words: ${props.join(' | ')}` : ''}`;
}

/** The writer's verdict (writer_place_v1). Finds the "PLACE: …" line in the writer's reply, removes it, and moves the
 *  scene there — a listed place, or NONE (somewhere the book doesn't know). No line, or a name that isn't on the menu,
 *  leaves the keyword result standing. Returns the prompt without the line. */
function applyWriterPlace(scene, text) {
  let out = String(text || '');
  const m = out.match(/^[ \t]*PLACE:[ \t]*([^\n]*)$/im);
  if (!m) return out;
  let end = m.index + m[0].length; if (out[end] === '\n') end++;
  out = (out.slice(0, m.index) + out.slice(end)).replace(/^\s+/, '').replace(/\n{3,}/g, '\n\n');
  if (!scene) return out;
  const raw = String(m[1] || '').trim();
  const dash = raw.search(/\s+[—–-]\s+/);
  let head = dash >= 0 ? raw.slice(0, dash) : raw;
  let reason = dash >= 0 ? raw.slice(dash).replace(/^\s+[—–-]\s+/, '').trim() : '';
  const par = head.indexOf('(');
  if (par > 0) { if (!reason) reason = head.slice(par + 1).replace(/\)\s*$/, '').trim(); head = head.slice(0, par); }
  const choice = head.replace(/^["'“”‘’]+|["'“”‘’.]+$/g, '').trim();
  const was = `${scene.kind}${scene.place ? ` "${labelOf(scene.place)}"` : ''}${scene.matchedBy ? ` via ${scene.matchedBy}` : ''}`;
  const menu = Array.isArray(scene.menu) ? scene.menu : [];
  if (!scene.menuOffered || !menu.length) { console.log(`📍 scene (writer): said ${raw} but no menu was offered — kept ${was}`); return out; }
  const tail = reason ? ` — ${reason.slice(0, 100)}` : '';
  if (/^none$/i.test(choice)) {
    scene.kind = 'wild'; scene.place = null; scene.world = null; scene.matchedBy = 'writer'; scene.keyword = ''; scene.said = '';
    console.log(`📍 scene (writer): NONE — somewhere not in the book (was ${was})${tail}`);
    return out;
  }
  const nc = normalizeText(choice);
  let hit = /^default$/i.test(choice) ? scene.menuDefault : null;
  if (!hit && nc) hit = menu.find(p => normalizeText(labelOf(p.entry)) === nc) || menu.find(p => entryKeywords(p.entry).includes(nc)) || menu.find(p => normalizeText(labelOf(p.entry)).includes(nc));
  if (!hit) { console.log(`📍 scene (writer): said ${raw} — not a known place, kept ${was}`); return out; }
  const same = scene.place === hit.entry;
  scene.kind = 'known'; scene.place = hit.entry; scene.world = { id: hit.book.id || null, name: hit.book.name || '' }; scene.matchedBy = 'writer'; scene.keyword = ''; scene.said = '';
  console.log(`📍 scene (writer): known "${labelOf(hit.entry)}"${same ? ' — agrees with the keywords' : ` — overrides ${was}`}${tail}`);
  return out;
}

/** The text block for the image writer. '' when there is nothing canonical to say. */
function buildSettingCanon(scene) {
  if (!scene) return '';
  if (scene.menuOffered) return buildPlaceMenu(scene);
  const lines = [];
  if (scene.place && (scene.kind === 'known' || scene.kind === 'default')) {
    const prose = String(scene.place.prompt || '').trim();
    if (prose) lines.push(`${labelOf(scene.place)}: ${prose}`);
  }
  for (const e of scene.props || []) {
    const prose = String(e.prompt || '').trim();
    if (!prose) continue;
    lines.push(`${labelOf(e)}${propTag(e)}: ${prose}`);
  }
  if (!lines.length) return '';
  return `\n\n=== SETTING CANON (use these EXACT words for the place and the things in it — do not reinvent them; this describes the real room, the scene supplies the moment) ===\n${lines.join('\n')}\n=== END SETTING CANON ===\nLight the person with light this room actually has — its windows and lamps — so the person and the room share one light setup. Describe the room as it looks through the chosen lens: at a portrait aperture it softens out of focus behind the subject.`;
}

/** Reference files to send after the face refs: place first, then props. Files live in data/lorebook_refs/. */
function settingRefs(scene, { dataDir, faceRefCount = 0, max = MAX_SETTING_REFS } = {}) {
  if (!scene || !dataDir) return [];
  if (Number(faceRefCount) >= FACE_REF_CUTOFF) return [];
  const dir = path.join(dataDir, 'lorebook_refs');
  const items = [];
  if (scene.place && (scene.kind === 'known' || scene.kind === 'default')) items.push(scene.place);
  for (const e of scene.props || []) items.push(e);
  const out = [];
  const seen = new Set();
  for (const e of items) {
    for (const r of (Array.isArray(e.refs) ? e.refs : [])) {
      const f = path.basename(String(r || '').trim());
      if (!f || seen.has(f)) continue;
      seen.add(f);
      const p = path.join(dir, f);
      if (!fs.existsSync(p)) continue;
      out.push({ path: p, filename: f, entry: e });
      if (out.length >= max) return out;
    }
  }
  return out;
}

/** One sentence per item telling NBP what the extra reference images are. `startIndex` = first setting ref's number. */
function settingRefSentence(refs, startIndex = 1) {
  if (!Array.isArray(refs) || !refs.length) return '';
  const parts = [];
  let i = 0;
  let idx = Number(startIndex) || 1;
  while (i < refs.length) {
    const e = refs[i].entry;
    let j = i;
    while (j + 1 < refs.length && refs[j + 1].entry === e) j++;
    const count = j - i + 1;
    const range = count === 1 ? `Reference image ${idx}` : `Reference images ${idx}–${idx + count - 1}`;
    if (isPlace(e)) parts.push(`${range} = the room this photograph is taken in: ${labelOf(e)}. The person is inside this room at true scale, seen from whatever camera angle suits the shot — not posed in front of it. Keep the room's layout, furniture, materials, and colors; do not copy the reference's framing, its lighting, or its width. The reference shows the whole room; the photograph does not have to. Framing is set by the shot description below, never by the reference — crop as tight as the shot says and let the room fall out of frame and out of focus around the person. This is one continuous photograph: the same light falls on the person and the room, with one lens, one focus falloff, one white balance, and one grain throughout. The reference contains no people.`);
    else if (e.kind === 'clothing') parts.push(`${range} = a garment: ${labelOf(e)}. In this photograph it is worn by ${ownerLabel(e)} and by no one else — fitted naturally to their body and pose, with its exact cut, length, fabric, pattern and colors. Do not reproduce the mannequin, hanger, backdrop, or any jewelry or accessories from the reference. The reference contains no people.`);
    else parts.push(`${range} = ${labelOf(e)}${e.kind ? ` (${/^[aeiou]/i.test(e.kind) ? 'an' : 'a'} ${e.kind})` : ''} — the same animal or object with its exact markings, placed naturally in the scene at true scale, lit and focused like everything else in the frame. It is not a person.`);
    idx += count;
    i = j + 1;
  }
  return parts.join(' ');
}

/** Place names a companion can use in a tag — the first keyword of each place entry in their world books. */
function worldPlaceNames(companionName, getAllLorebooks) {
  const names = [];
  for (const b of worldBooksFor(companionName, getAllLorebooks)) {
    for (const e of b.entries) {
      if (!e || e.enabled === false || !isPlace(e)) continue;
      const kws = entryKeywords(e);
      const label = kws[0] || (e.name ? String(e.name).trim() : '');
      if (label && !names.includes(label)) names.push(label);
    }
  }
  return names;
}

module.exports = {
  resolveScene,
  buildSettingCanon,
  applyWriterPlace,
  settingRefs,
  settingRefSentence,
  worldPlaceNames,
  normalizeText,
  FACE_REF_CUTOFF
};
