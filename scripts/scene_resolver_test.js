#!/usr/bin/env node
// Love Refactored — cases for lib/scene-resolver.js (world books: places, pets, things)
//   node scripts/scene_resolver_test.js
// No server, no disk: the books are inline. Exits 1 if any case fails.
'use strict';
const path = require('path');
const R = require(path.join(__dirname, '..', 'lib', 'scene-resolver.js'));

const place = (id, keywords, name, owner, extra = {}) => ({ id, keywords, prompt: 'x', kind: 'place', name, owner, refs: [], enabled: true, ...extra });
const u = (text) => ({ sender: 'user', text });
const c = (who, text) => ({ sender: who, text });

// House A: one bedroom owned by the user, Alex has his own room, a garage, a shared pet book, and a chat book that must be ignored.
const houseA = [
  { id: 'a', name: 'The house', type: 'world', enabled: true, companions: ['Alex', 'Rio', 'Kit', 'Jo'], entries: [
    place('bed', ['bedroom', 'bed', 'my room', 'our room'], "Sam's bedroom", 'user', { isDefault: true }),
    place('evroom', ['my room', 'his room', "alex's room"], "Alex's room", 'Alex'),
    place('garage', ['garage'], 'The garage', null) ] },
  { id: 'j', name: "Max's", type: 'world', enabled: true, companions: ['Max'], entries: [
    place('jbed', ['bedroom', 'bed', 'my room'], "Sam's bedroom (Max's)", 'user', { isDefault: true }) ] },
  { id: 'shared', name: 'Everywhere', type: 'world', enabled: true, companions: [], entries: [
    { id: 'mochi', keywords: ['mochi', 'the cat', 'my cat'], prompt: 'x', kind: 'pet', name: 'Mochi', owner: 'user', refs: [], enabled: true } ] },
  { id: 'rel', name: 'Alex and Sam', type: 'relationship', enabled: true, companions: ['Alex'], entries: [
    { id: 'x', keywords: ['bedroom'], prompt: 'SHOULD NEVER APPEAR', enabled: true } ] }
];

// House B: a shared bedroom, two companions with their own rooms, two bathrooms.
const houseB = [
  { id: 'b', name: 'The house', type: 'world', enabled: true, companions: ['Alex', 'Rio', 'Jo'], entries: [
    place('bed', ['bedroom', 'bed', 'our room', 'my room'], "Sam and Alex's bedroom", 'shared', { isDefault: true }),
    place('rio', ["rio's room", 'my room', 'his room'], "Rio's room", 'Rio'),
    place('jo', ["jo's room", 'my room', 'his room'], "Jo's room", 'Jo'),
    place('hall', ['bathroom', 'hall bathroom'], 'The hall bathroom', null),
    place('master', ['our bathroom', 'my bathroom', 'master bathroom'], "Sam and Alex's bathroom", 'shared'),
    place('kitchen', ['kitchen', 'counter'], 'The kitchen', null) ] }
];

// House C: multi-owner bedroom, kids with their own rooms, a common room as the house default, one member with no room.
const houseC = [
  { id: 'c', name: 'The house', type: 'world', enabled: true, companions: ['Alex', 'Rio', 'Kit', 'Lee', 'Dee'], entries: [
    place('living', ['living room', 'couch'], 'The living room', null, { isDefault: true }),
    place('bed', ['bedroom', 'bed', 'our room', 'our bed', 'my room'], "Sam and Alex's bedroom", ['user', 'Alex']),
    place('rio', ["rio's room", 'my room', 'his room'], "Rio's room", 'Rio'),
    place('kit', ["kit's room", 'my room', 'his room', 'bedroom', 'bed'], "Kit's room", 'Kit'),
    place('lee', ["lee's room", 'my room', 'his room'], "Lee's room", 'Lee') ] },
  { id: 'j2', name: "Max's", type: 'world', enabled: true, companions: ['Max'], entries: [
    place('jbed', ['bedroom', 'bed', 'our room', 'my room'], "Sam and Max's bedroom", ['user', 'Max'], { isDefault: true }),
    place('jgarage', ['garage', 'my garage'], 'The garage', 'Max') ] }
];

// [label, books, args, expected kind, expected place name (or null), expected cue (or null), expected prop names]
const cases = [
  ['Alex tag, "my room"',               houseA, { sceneHint: 'sprawled on the bed in my room, guitar across my lap', speaker: 'Alex', companionsInFrame: ['Alex'], history: [u('hey')] }, 'known', "Alex's room"],
  ['you typed garage, chat says bedroom', houseA, { sceneHint: 'you in the garage', speaker: 'user', companionsInFrame: ['Alex'], history: [u('come to my bedroom')] }, 'known', 'The garage'],
  ['left the house',                     houseA, { sceneHint: '', speaker: 'user', companionsInFrame: ['Alex'], history: [u('in my bedroom, so cozy'), c('Alex', 'mm'), u('okay we are in the van now')] }, 'wild', null, 'van'],
  ['silence',                            houseA, { sceneHint: '', speaker: 'user', companionsInFrame: ['Alex'], history: [u('how was your day'), c('Alex', 'long. you?')] }, 'default', "Alex's room"],
  ["Max's house, your bedroom",       houseA, { sceneHint: '', speaker: 'user', companionsInFrame: ['Max'], history: [u('come to my bedroom')] }, 'known', "Sam's bedroom (Max's)"],
  ['Alex tag, cat on the bed',           houseA, { sceneHint: 'Mochi is asleep on the bed next to me', speaker: 'Alex', companionsInFrame: ['Alex'], history: [u('hi')] }, 'known', "Sam's bedroom", null, ['Mochi']],
  ['companion with no world book',       houseA, { sceneHint: '', speaker: 'user', companionsInFrame: ['Zed'], history: [u('meet me in the bedroom')] }, 'wild', null, 'bedroom'],
  ['you say "my room" to Alex',          houseA, { sceneHint: '', speaker: 'user', companionsInFrame: ['Alex'], history: [u('come to my room')] }, 'known', "Sam's bedroom"],
  ['Alex says "my room" in chat',        houseA, { sceneHint: '', speaker: 'user', companionsInFrame: ['Alex'], history: [c('Alex', 'come to my room')] }, 'known', "Alex's room"],
  ['"stage" with no preposition',        houseA, { sceneHint: '', speaker: 'user', companionsInFrame: ['Alex'], history: [u('i walk into the bedroom'), c('Alex', 'Like a spotlight. Go on, take center stage.')] }, 'known', "Sam's bedroom"],
  ['backstage, bare word',               houseA, { sceneHint: '', speaker: 'user', companionsInFrame: ['Alex'], history: [u('in my bedroom'), c('Alex', 'we are backstage, finally')] }, 'wild', null, 'backstage'],
  ['raise the bar / car keys',           houseA, { sceneHint: '', speaker: 'user', companionsInFrame: ['Alex'], history: [c('Alex', 'raise the bar. where are my car keys')] }, 'default', "Alex's room"],
  ['terse tag: kitchen counter',         houseA, { sceneHint: 'kitchen counter, coffee in hand', speaker: 'Alex', companionsInFrame: ['Alex'], history: [u('in my bedroom')] }, 'wild', null, 'kitchen'],
  ['Rio: my room',                      houseB, { sceneHint: 'sprawled in my room', speaker: 'Rio', companionsInFrame: ['Rio'], history: [] }, 'known', "Rio's room"],
  ['Alex: my room (shared bedroom)',     houseB, { sceneHint: 'sprawled in my room', speaker: 'Alex', companionsInFrame: ['Alex'], history: [] }, 'known', "Sam and Alex's bedroom"],
  ['you to Rio: his room',              houseB, { sceneHint: 'come to his room', speaker: 'user', companionsInFrame: ['Rio'], history: [] }, 'known', "Rio's room"],
  ['you to Jo: his room',             houseB, { sceneHint: 'come to his room', speaker: 'user', companionsInFrame: ['Jo'], history: [] }, 'known', "Jo's room"],
  ['Alex: my bathroom',                  houseB, { sceneHint: 'in my bathroom', speaker: 'Alex', companionsInFrame: ['Alex'], history: [] }, 'known', "Sam and Alex's bathroom"],
  ['Alex: the bathroom',                 houseB, { sceneHint: 'in the bathroom', speaker: 'Alex', companionsInFrame: ['Alex'], history: [] }, 'known', 'The hall bathroom'],
  ['Rio: our room',                     houseB, { sceneHint: 'in our room', speaker: 'Rio', companionsInFrame: ['Rio'], history: [] }, 'known', "Sam and Alex's bedroom"],
  ['Lee, silence → his own room',      houseC, { sceneHint: '', speaker: 'user', companionsInFrame: ['Lee'], history: [u('hey')] }, 'default', "Lee's room"],
  ['Rio, silence → his own room',       houseC, { sceneHint: '', speaker: 'user', companionsInFrame: ['Rio'], history: [u('hey')] }, 'default', "Rio's room"],
  ['Alex, silence → the bedroom',        houseC, { sceneHint: '', speaker: 'user', companionsInFrame: ['Alex'], history: [u('hey')] }, 'default', "Sam and Alex's bedroom"],
  ['Dee (no room), silence → living room', houseC, { sceneHint: '', speaker: 'user', companionsInFrame: ['Dee'], history: [u('hey')] }, 'default', 'The living room'],
  ['Alex: my room (two owners)',         houseC, { sceneHint: 'in my room', speaker: 'Alex', companionsInFrame: ['Alex'], history: [] }, 'known', "Sam and Alex's bedroom"],
  ['Lee: my room (two-owner bedroom exists)', houseC, { sceneHint: 'in my room', speaker: 'Lee', companionsInFrame: ['Lee'], history: [] }, 'known', "Lee's room"],
  ['Max, silence → bedroom not garage', houseC, { sceneHint: '', speaker: 'user', companionsInFrame: ['Max'], history: [u('hey')] }, 'default', "Sam and Max's bedroom"],
  ['Kit: "bed" → his own bed',         houseC, { sceneHint: '', speaker: 'Kit', companionsInFrame: ['Kit'], history: [c('Kit', 'come to bed')] }, 'known', "Kit's room"],
  ['Kit: "our bed" → still his bed',   houseC, { sceneHint: 'curled up in our bed', speaker: 'Kit', companionsInFrame: ['Kit'], history: [] }, 'known', "Kit's room"],
  ['Alex: "our bed" → the bedroom',      houseC, { sceneHint: 'in our bed', speaker: 'Alex', companionsInFrame: ['Alex'], history: [] }, 'known', "Sam and Alex's bedroom"],
  ['"kitchen sink records" is a label',  houseB, { sceneHint: '', speaker: 'Rio', companionsInFrame: ['Rio'], history: [c('Rio', 'kitchen sink records called about the masters')] }, 'default', "Rio's room"],
  ['"garage band" is not the garage',     houseA, { sceneHint: '', speaker: 'user', companionsInFrame: ['Alex'], history: [u('come to my room'), c('Alex', 'pure garage band energy tonight')] }, 'known', "Sam's bedroom"],
  ["mention: rio's room is a mess",      houseB, { sceneHint: '', speaker: 'user', companionsInFrame: ['Alex'], history: [u('come to bed'), c('Alex', "rio's room is a disaster")] }, 'known', "Sam and Alex's bedroom"],
  ['prose: pulls you into the kitchen',   houseB, { sceneHint: '', speaker: 'user', companionsInFrame: ['Alex'], history: [c('Alex', '*pulls you into the kitchen*')] }, 'known', 'The kitchen'],
  ['tag stays bare: kitchen counter',     houseB, { sceneHint: 'kitchen counter, coffee in hand', speaker: 'Alex', companionsInFrame: ['Alex'], history: [] }, 'known', 'The kitchen'],
  ['"at home" is still the house',         houseB, { sceneHint: '', speaker: 'user', companionsInFrame: ['Alex'], history: [u('finally at home, working from home tomorrow too')] }, 'default', "Sam and Alex's bedroom"],
  ['"by the window" is not elsewhere',     houseB, { sceneHint: '', speaker: 'user', companionsInFrame: ['Alex'], history: [c('Alex', '*pulls you into the kitchen*'), u('standing by the window with my coffee')] }, 'known', 'The kitchen'],
  ['prose: a bare "bed" is not enough',   houseB, { sceneHint: '', speaker: 'user', companionsInFrame: ['Alex'], history: [c('Alex', 'bed. now. i mean it')] }, 'default', "Sam and Alex's bedroom"]
];

let failed = 0;
for (const [label, books, args, kind, placeName, cue, props] of cases) {
  const r = R.resolveScene({ ...args, getAllLorebooks: () => books });
  const got = { kind: r.kind, place: r.place ? r.place.name : null, cue: r.kind === 'wild' ? r.cue : null, props: r.props.map(p => p.name) };
  const want = { kind, place: placeName || null, cue: cue || null, props: props || [] };
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) failed++;
  console.log((ok ? 'PASS ' : 'FAIL ') + label.padEnd(38) + (ok ? r.log.replace('📍 scene: ', '') : '\n       want ' + JSON.stringify(want) + '\n       got  ' + JSON.stringify(got)));
}
// writer_place_v1 — the KNOWN PLACES menu and the writer's PLACE: verdict (applyWriterPlace).
const wcases = [];
const w = (label, fn) => wcases.push([label, fn]);
const houseW = [
  { id: 'w', name: 'The house', type: 'world', enabled: true, companions: ['Alex', 'Kit'], entries: [
    place('living', ['living room', 'couch'], 'The living room', ['shared', 'Alex', 'Kit'], { isDefault: true }),
    place('kitchen', ['kitchen', 'counter'], 'The kitchen', null),
    place('kit', ["kit's room", 'my room', 'bed', 'bedroom'], "Kit's room", 'Kit') ] }
];
const fresh = () => R.resolveScene({ sceneHint: '', speaker: 'Kit', companionsInFrame: ['Kit'], history: [c('Kit', 'come to bed'), c('Kit', 'we should go to the kitchen later')], getAllLorebooks: () => houseW });
w('menu offered for a history hit',       () => { const s = fresh(); return { ok: s.menuOffered === true && /KNOWN PLACES/.test(R.buildSettingCanon(s)) && /Keyword guess: "The kitchen"/.test(R.buildSettingCanon(s)), got: s.log }; });
w('no menu when the tag named the place', () => { const s = R.resolveScene({ sceneHint: 'kitchen counter, coffee in hand', speaker: 'Alex', companionsInFrame: ['Alex'], history: [], getAllLorebooks: () => houseW }); return { ok: s.menuOffered === false && /SETTING CANON/.test(R.buildSettingCanon(s)), got: s.log }; });
w('no menu without a world book',          () => { const s = R.resolveScene({ sceneHint: '', speaker: 'Dee', companionsInFrame: ['Dee'], history: [u('meet me in the bedroom')], getAllLorebooks: () => houseA }); return { ok: s.menuOffered === false && R.buildSettingCanon(s) === '', got: s.log }; });
w('verdict: a listed place, with reason',  () => { const s = fresh(); const t = R.applyWriterPlace(s, 'PLACE: "Kit\'s room" — they are in his bed\nThe person from the reference image...'); return { ok: s.kind === 'known' && s.place.name === "Kit's room" && s.matchedBy === 'writer' && t === 'The person from the reference image...', got: `${s.kind} "${s.place && s.place.name}" | ${JSON.stringify(t)}` }; });
w('verdict: NONE → wild, line removed',     () => { const s = fresh(); const t = R.applyWriterPlace(s, 'PLACE: NONE\nprompt text'); return { ok: s.kind === 'wild' && s.place === null && t === 'prompt text', got: `${s.kind} | ${JSON.stringify(t)}` }; });
w('verdict: unknown name keeps keywords',   () => { const s = fresh(); const t = R.applyWriterPlace(s, 'PLACE: the moon\nprompt text'); return { ok: s.kind === 'known' && s.place.name === 'The kitchen' && t === 'prompt text', got: `${s.kind} "${s.place && s.place.name}" | ${JSON.stringify(t)}` }; });
w('verdict: no line, nothing changes',      () => { const s = fresh(); const t = R.applyWriterPlace(s, 'prompt text only'); return { ok: s.kind === 'known' && s.place.name === 'The kitchen' && t === 'prompt text only', got: `${s.kind} "${s.place && s.place.name}"` }; });
w('verdict: line not first still found',    () => { const s = fresh(); const t = R.applyWriterPlace(s, 'first line\nPLACE: "The kitchen" (by the island)\nlast line'); return { ok: s.place.name === 'The kitchen' && t === 'first line\nlast line', got: JSON.stringify(t) }; });
w('verdict ignored when the tag decided',  () => { const s = R.resolveScene({ sceneHint: 'kitchen counter, coffee in hand', speaker: 'Alex', companionsInFrame: ['Alex'], history: [], getAllLorebooks: () => houseW }); const t = R.applyWriterPlace(s, 'PLACE: "Kit\'s room"\nprompt'); return { ok: s.place.name === 'The kitchen' && t === 'prompt', got: `${s.kind} "${s.place && s.place.name}"` }; });
// clothing_v1 — a garment is a prop that is worn, not placed.
const robe = { id: 'robe', keywords: ['the robe', 'robe', 'kimono', 'my robe'], prompt: 'a long green robe', kind: 'clothing', name: 'the robe', owner: 'user', refs: ['robe.jpg'], enabled: true };
const houseR = [{ id: 'r', name: 'The house', type: 'world', enabled: true, companions: ['Bo'], entries: [
  place('living', ['living room', 'couch'], 'The living room', 'user', { isDefault: true }), robe ] }];
w('clothing: worn, counted as a prop',     () => { const s = R.resolveScene({ sceneHint: 'on the couch in my robe', speaker: 'user', companionsInFrame: ['Bo'], userInFrame: true, history: [], getAllLorebooks: () => houseR }); const canon = R.buildSettingCanon(s); const sent = R.settingRefSentence([{ path: 'x', filename: 'robe.jpg', entry: robe }], 5); return { ok: s.props.length === 1 && s.props[0].kind === 'clothing' && /the robe \(clothing — the user's, worn by the user and by no one else/.test(canon) && /Reference image 5 = a garment: the robe\./.test(sent) && /worn by the user and by no one else/.test(sent) && !/placed naturally/.test(sent), got: `props=${s.props.map(p => p.name).join(',')} | ${sent.slice(0, 60)}` }; });
w('clothing: owner not in frame → dropped', () => { const s = R.resolveScene({ sceneHint: 'me in your robe', speaker: 'Bo', companionsInFrame: ['Bo'], history: [], getAllLorebooks: () => houseR }); return { ok: s.props.length === 0, got: s.log }; });
w('clothing: user in frame (camera) → worn', () => { const s = R.resolveScene({ sceneHint: 'you on the couch in the robe', speaker: 'Bo', companionsInFrame: ['Bo'], userInFrame: true, history: [], getAllLorebooks: () => houseR }); return { ok: s.props.length === 1, got: s.log }; });
w("clothing: Bo's jacket, Bo in frame", () => { const jacket = { id: 'jk', keywords: ['the jacket', 'leather jacket'], prompt: 'a black leather jacket', kind: 'clothing', name: 'the jacket', owner: 'Bo', refs: [], enabled: true }; const books = [{ ...houseR[0], entries: houseR[0].entries.concat([jacket]) }]; const s = R.resolveScene({ sceneHint: 'in the jacket on the porch', speaker: 'Bo', companionsInFrame: ['Bo'], history: [], getAllLorebooks: () => books }); return { ok: s.props.length === 1 && s.props[0].name === 'the jacket', got: s.log }; });
const camper = { id: 'camp', keywords: ['the camper', 'camper'], prompt: 'a green camper van', kind: 'object', name: 'the camper', owner: 'Max', refs: [], enabled: true };
const lamp = { id: 'lamp', keywords: ['the lamp'], prompt: 'a brass floor lamp', kind: 'object', name: 'the lamp', owner: null, refs: [], enabled: true };
const houseO = [{ id: 'o', name: 'The house', type: 'world', enabled: true, companions: ['Alex', 'Max'], entries: [
  place('living', ['living room', 'couch'], 'The living room', null, { isDefault: true }), camper, lamp ] }];
w("object: Max's car, Alex in frame → dropped", () => { const s = R.resolveScene({ sceneHint: 'leaning on the camper', speaker: 'Alex', companionsInFrame: ['Alex'], history: [], getAllLorebooks: () => houseO }); return { ok: s.props.length === 0, got: s.log }; });
w("object: Max's car, Max in frame → kept", () => { const s = R.resolveScene({ sceneHint: 'leaning on the camper', speaker: 'Max', companionsInFrame: ['Max'], history: [], getAllLorebooks: () => houseO }); return { ok: s.props.length === 1 && s.props[0].name === 'the camper', got: s.log }; });
w('object: unowned lamp, anyone → kept',       () => { const s = R.resolveScene({ sceneHint: 'on the couch next to the lamp', speaker: 'Alex', companionsInFrame: ['Alex'], history: [], getAllLorebooks: () => houseO }); return { ok: s.props.length === 1 && s.props[0].name === 'the lamp', got: s.log }; });
for (const [label, fn] of wcases) {
  let r; try { r = fn(); } catch (e) { r = { ok: false, got: 'threw: ' + e.message }; }
  if (!r.ok) failed++;
  console.log((r.ok ? 'PASS ' : 'FAIL ') + label.padEnd(38) + r.got);
}

const total = cases.length + wcases.length;
console.log(`\n${total - failed} of ${total} passed`);
process.exit(failed ? 1 : 0);
