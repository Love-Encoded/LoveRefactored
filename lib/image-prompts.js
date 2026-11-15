'use strict';

/**
 * Image prompt templates for generate-image (solo 1:1, couple, group).
 * LLM orchestration stays in server.js.
 *
 * Aligned with: The Nano Banana Pro Portrait Playbook (June 2026)
 */

const LOCATION_CUES = /\b(venue|backstage|loading dock|dock|stage|green ?room|club|bar|studio|kitchen|bedroom|car|van|tour bus|hotel|motel|airport|street|alley|rooftop|parking lot|diner|cafe|coffee shop|restaurant|park|beach|store|shop|office|apartment|house|porch|balcony|basement|garage|elevator|hallway|bathroom|couch|bed|outside|outdoors|indoors|on stage|onstage|sound ?check|merch table|home|my place|your place|fire escape|roof|living room|shower|in bed|on the couch|backyard|stoop|window|loft|truck|train|bus|tent|gym|classroom|hospital)\b/i;

function extractLatestLocation(msgs) {
  const list = Array.isArray(msgs) ? msgs : [];
  const window = list.slice(-8);
  for (let i = window.length - 1; i >= 0; i--) {
    const t = String(window[i]?.text || '').trim();
    if (LOCATION_CUES.test(t)) return t;
  }
  return null;
}

function formatLocationAnchor(location, { prefix = 'MOST RECENTLY ESTABLISHED LOCATION (use THIS setting unless a newer one is stated)' } = {}) {
  if (!location) return '';
  return `\n\n${prefix}: "${location}"`;
}

// wardrobe_cues_v2: a garment word only counts when it is used AS clothing — after a wearing-verb, or after a
// determiner ("the red flannel", "his jeans", "my band shirt"). Bare nouns no longer fire: "the vest patterns"
// (haptics), "belt it out", "suit yourself", "supply chain", "the word cap", "dress rehearsal".
const WARDROBE_GARMENTS = 't-?shirts?|shirts?|blouses?|tees?|tank tops?|hoodies?|sweatshirts?|sweaters?|cardigans?|flannels?|button-?downs?|button-?ups?|henleys?|jerseys?|jackets?|coats?|parkas?|blazers?|vests?|jeans|pants|trousers|slacks|sweatpants|joggers|shorts|skirts?|dress(?:es)?|gowns?|suits?|tux(?:es|edos?)?|overalls|coveralls|robes?|kimonos?|pajamas|pjs|boxers|briefs|underwear|lingerie|bras?|towels?|aprons?|scrubs|uniforms?|beanies?|caps?|hats?|bandanas?|scarf|scarves|gloves|belts?|suspenders|necklaces?|chains?|zip-?ups?|crop tops?|bodysuits?|corsets?|fishnets|tights|leggings';
// Hardware / idiom context that disqualifies a garment match: what comes right before it, and right after it.
const WARDROBE_NOT_BEFORE = '(?<!\\b(?:haptic|haptics|bhaptics|tactile|bullet-?proof|life|word|salary|bottle|lens|ice|market|rate|token|supply|food|key|daisy)\\s)';
const WARDROBE_NOT_AFTER = '(?!\\s+(?:patterns?|motors?|haptics?|player|driver|bus|controller|settings?|rack|hook|hanger|drawer|aisle|section|store|shop|sizes?|code|rule|policy|rehearsal|of\\b|reaction|links?|smoker|yourself|it out|up\\b))';
const WARDROBE_VERB_GARMENT = new RegExp(`\\b(?:wearing|wears|wore|dressed in|threw on|throws? on|pulled on|pulls? on|put on|puts? on|putting on|changed into|changes? into|changing into|stripped down to|strips? down to|slipped into|slips? into)\\b[^.!?\\n]{0,60}?${WARDROBE_NOT_BEFORE}\\b(?:${WARDROBE_GARMENTS})\\b${WARDROBE_NOT_AFTER}`, 'i');
const WARDROBE_DET_GARMENT = new RegExp(`\\b(?:the|my|your|his|her|their|our|a|an|that|this|these|those|some|same)\\s+(?:[\\w'-]+\\s+){0,2}?${WARDROBE_NOT_BEFORE}\\b(?:${WARDROBE_GARMENTS})\\b${WARDROBE_NOT_AFTER}`, 'i');
const WARDROBE_STATE = /\b(shirtless|topless|in (?:just |only |nothing but )?(?:his|her|their|my) (?:boxers|briefs|underwear|towel))\b/i;

function wardrobeIn(text) {
  const t = String(text || '');
  if (!t) return false;
  return WARDROBE_STATE.test(t) || WARDROBE_VERB_GARMENT.test(t) || WARDROBE_DET_GARMENT.test(t);
}

function extractLatestWardrobe(msgs) {
  const list = Array.isArray(msgs) ? msgs : [];
  for (let i = list.length - 1; i >= 0; i--) {
    const t = String(list[i]?.text || '').trim();
    if (wardrobeIn(t)) return t;
  }
  return '';
}

function formatWardrobeAnchor(wardrobe, { prefix = 'MOST RECENTLY ESTABLISHED WARDROBE (what people are ACTUALLY wearing — use these exact garments; do not swap them for a default outfit unless a newer message changes them)' } = {}) {
  if (!wardrobe) return '';
  return `\n\n${prefix}: "${wardrobe}"`;
}

// The key light never invents a fixture — room sources, a window, or bounce only.
// shot_seed_v1: a stateless writer cannot "rotate between shots" — it never sees the last shot. So the route rolls the
// key-light direction and exactly two imperfections per photo and hands them to the writer as facts it must use.
const KEY_DIRECTIONS = [
  'camera-left',
  'camera-right',
  'behind the subject and to camera-left (rim light on hair and shoulder; the face lit by softer bounce or a second source)',
  'behind the subject and to camera-right (rim light on hair and shoulder; the face lit by softer bounce or a second source)',
  'front and to one side, broad and soft (a large window, a sheer-curtained doorway, an open door onto grey daylight; open shadows, gentle modelling, no hard edges)',
  'below (a screen, a low lamp, a candle or a phone; shadows climb upward)'
];
// face_safe_flaws_v1: every flaw lives away from the faces. NBP's face lock needs a lit, sharp,
// unobstructed, near-frontal face to land on — a flaw that touches a face turns a reference into a stranger.
const IMPERFECTIONS = [
  'eyes off the lens but the face toward the camera — the subject looks at something a few degrees off-axis (the other person, their own hands, something just past the lens), a three-quarter turn at most, both eyes visible, never a profile',
  'framing is off-center or tilted a few degrees, with one specific thing cut off at the frame edge — a shoulder, an elbow, a lamp, the far side of the room — never any part of a head or face',
  'one specific object sits in the near foreground, soft and out of focus, low or to one side of the frame (a mug, a doorframe, a blanket edge, a cat) — never between the camera and anyone\'s face',
  'exposure is imperfect in one specific place away from the people — a blown-out window, a hot spot on a lamp or a wall, a crushed-black corner — while every face stays correctly exposed',
  'slight motion blur in one specific thing that is not a person — a curtain, a cat\'s tail, steam off a mug, a strand of hair at the very edge of frame — while every face, hand, and body is sharp', /* hands_sharp_v1: never a blurry hand */
  'two color temperatures visible in one frame — a warm lamp against cool window daylight, or screen glow on one side of the room — with skin tones kept natural on the key side',
  'one reflective surface catches light somewhere in the frame — a window, a dark screen, a framed picture, a glass on the table — a small bright patch away from the people, never showing a face'
];
function buildShotSeed(rand = Math.random) {
  const dir = KEY_DIRECTIONS[Math.floor(rand() * KEY_DIRECTIONS.length)];
  const pool = IMPERFECTIONS.slice();
  const a = pool.splice(Math.floor(rand() * pool.length), 1)[0];
  const b = pool.splice(Math.floor(rand() * pool.length), 1)[0];
  const text = `\n\n=== THIS SHOT (set by the camera, not negotiable) ===\nKey light comes from ${dir}. Use whichever of the room's own light sources sits on that side and say how it lands on the face(s). If no listed source sits there, light from a window on that side or soft bounce off a wall — never add a lamp, bulb, or fixture the room description does not list; the room is canon and the camera adapts to it, not the other way round.\nThis shot's two imperfections, exactly these and no others: (1) ${a}; (2) ${b}.\nFACE RULE (overrides everything above): every face in the frame is fully visible, sharp, correctly exposed, unobstructed, and turned no more than three-quarters toward the camera, with real fill light on the shadow side when the key is behind, above, or below. Identity comes from the reference images and needs a readable face to land on — no flaw, crop, shadow, or object ever lands on a face. Toward the camera means the head angle, not the eyes: nobody looks into the lens unless the scene asks for it — eyes go to a shared point in the scene, so the camera is a witness, not the event.\n=== END THIS SHOT ===`;
  const log = `🎲 shot seed: key=${dir.split(' (')[0]} | imperfections: ${a.slice(0, 42)}… / ${b.slice(0, 42)}…`;
  return { text, log, dir, imperfections: [a, b] };
}

function sanitizeFluxPrompt(prompt) {
  let clean = prompt;
  clean = clean.replace(/\(([^)]+?):\s*[\d.]+\)/g, '$1');
  clean = clean.replace(/\(([^)]{1,40})\)/g, '$1');
  const junkTags = /\b(ultra[-\s]?photorealistic|photorealistic|hyper[-\s]?realistic|8k|4k|masterpiece|best quality|high detail|highly detailed|cinematic lighting|dramatic lighting|professional photo|award[-\s]?winning|stunning|breathtaking)\b/gi;
  clean = clean.replace(junkTags, '');
  clean = clean.replace(/,\s*,+/g, ',').replace(/\s{2,}/g, ' ').replace(/,\s*\./g, '.').replace(/^\s*,\s*/, '').replace(/,\s*$/, '').trim();
  return clean;
}

/**
 * Build a rich appearance block from available card fields.
 * Prefers avatarDescription (long-form visual reference) over appearance (short summary).
 * Combines both when available for maximum detail.
 */
function buildFullAppearance({ appearance, avatarDescription }) {
  const desc = (avatarDescription || '').trim();
  const app = (appearance || '').trim();
  if (desc && app) {
    return `${desc}\n\nAdditional notes: ${app}`;
  }
  return desc || app || 'Not specified';
}

/** 1-based span of reference images belonging to person `index`. counts[i] is how many photos that person has. */
function referenceSpanText(index, counts) {
  const list = Array.isArray(counts) ? counts : [];
  let start = 1;
  for (let j = 0; j < index; j++) start += Math.max(1, Number(list[j]) || 1);
  const n = Math.max(1, Number(list[index]) || 1);
  const end = start + n - 1;
  const label = n === 1 ? `Reference Image ${start}` : `Reference Images ${start}-${end}`;
  return { start, end, label };
}

/** Authoritative person-to-image map. A range is several angles of one face, not several people. */
function describeReferenceSpans(names, counts) {
  const clean = (Array.isArray(names) ? names : []).map(n => String(n || '').trim()).filter(Boolean);
  const parts = clean.map((n, i) => {
    const span = referenceSpanText(i, counts);
    if (span.end === span.start) return `${span.label} = ${n}`;
    return `${span.label} = ${n} (same person, different angles of one face)`;
  });
  const multiAngle = clean.some((_, i) => referenceSpanText(i, counts).end > referenceSpanText(i, counts).start);
  const featureLock = 'Each face matches its reference — identical eye shape, nose bridge contour, jawline angle, lip proportions, and skin texture.';
  const tail = multiAngle
    ? `${featureLock} Photos of the same name are one person. Combine those angles to lock the face. Do not add a person for each photo.`
    : featureLock;
  return `Person mapping: ${parts.join('; ')}. ${tail}`;
}

function buildSpecificMultiFallbackPrompt(names, appearanceList, recentMessages, sceneHint, refCounts) {
  const recentLines = String(recentMessages || '')
    .split('\n')
    .map(s => s.trim())
    .filter(Boolean)
    .slice(-2)
    .map(s => s.slice(0, 90));
  const contextPhrase = recentLines.length
    ? `capturing the same emotional beat as: ${recentLines.join(' / ')}`
    : 'capturing a candid in-the-moment interaction';
  const scenePhrase = sceneHint ? ` Scene details: ${String(sceneHint).trim()}.` : '';

  const parsedPeople = (Array.isArray(appearanceList) ? appearanceList : []).map((entry, i) => {
    const colonIdx = entry.indexOf(':');
    const name = colonIdx > 0 ? entry.slice(0, colonIdx).trim() : '';
    const desc = colonIdx > 0 ? entry.slice(colonIdx + 1).trim() : '';
    return { name, desc, index: i };
  });

  const boundPeople = parsedPeople
    .map(p => `the person from ${referenceSpanText(p.index, refCounts).label}${p.name ? ` (${p.name}${p.desc && p.desc !== '(no description)' ? ` — ${p.desc}` : ''})` : ''}`)
    .join(', ');

  const spatialLine = parsedPeople.length >= 3
    ? `Left to right in the frame: ${parsedPeople.map(p => referenceSpanText(p.index, refCounts).label).join(', ')}.`
    : '';

  const identityLock = `${describeReferenceSpans(parsedPeople.map(p => p.name || `Person ${String.fromCharCode(65 + p.index)}`), refCounts)} Each face rendered individually with unique features preserved.`;

  const avoidLine = 'Every face rendered with photographic realism: visible pores, natural asymmetry, lifelike eyes with natural catchlights, each person matching their reference exactly with distinct individual features.';

  const cameraLine = parsedPeople.length === 1
    ? 'Shot on Sony A7IV with an 85mm f/1.8 lens, shallow depth of field, Kodak Portra 400 palette.'
    : parsedPeople.length === 2
      ? 'Shot on a 50mm f/2 lens, editorial candid feel, Kodak Portra 400 palette.'
      : 'Shot on a 35mm f/2.8 lens, editorial candid photography, Kodak Portra 400 palette.';

  const textureLine = 'Natural skin texture with visible pores, not airbrushed, subtle asymmetry, slight film grain.';

  const actionLine = parsedPeople.length === 2
    ? 'They lean into each other with a shared, intimate posture that fits the moment.'
    : 'Each person has a distinct action and micro-expression that fits the moment.';

  return [
    `An eye-level candid photograph of ${boundPeople}.`,
    identityLock,
    spatialLine,
    scenePhrase || actionLine,
    contextPhrase ? contextPhrase + '.' : '',
    cameraLine,
    textureLine,
    avoidLine
  ].filter(Boolean).join(' ');
}

function buildNanoBananaIdentityLockedPrompt(scenePrompt, names, deps, refCounts) {
  const cleanNames = (Array.isArray(names) ? names : [])
    .map(n => String(n || '').trim())
    .filter(Boolean);
  const mapping = describeReferenceSpans(cleanNames, refCounts);
  const safeScene = String(scenePrompt || '').trim() || 'A candid moment with these exact people.';
  const stylizedRequested = /(cartoon|anime|illustration|paint(?:ing|ed)|watercolor|comic|chibi|pixel art|cel[-\s]?shade|stylized|3d render)/i.test(safeScene);
  const styleLock = stylizedRequested
    ? 'Preserve exact face identity while following the requested art style.'
    : 'Photorealistic DSLR photograph with natural skin texture and true-to-life lighting.';

  return [
    mapping,
    styleLock,
    safeScene
  ].join('\n');
}

// world_books_v1 — SETTING RULE lead-ins, used only when the scene resolver (lib/scene-resolver.js) handed the writer
// a SETTING CANON or KNOWN PLACES block. With no block, each builder below emits its original rule, word for word.
const PLACE_LINE_RULE = 'If a KNOWN PLACES block appears above instead of a canon block, the first line of your reply must be PLACE: <one name from that list> or PLACE: NONE — that line is required and overrides the no-preamble rule — and the place you chose is then treated exactly like a SETTING CANON block.';
const SETTING_RULE_SOLO_CANON = `${PLACE_LINE_RULE} If a SETTING CANON block appears above, that is the real place — build the setting from its exact words (layout, furniture, surfaces) and add only the light and the moment; do not add furniture or features it doesn't name, and do not re-describe it in different words. Any pet or object listed in that block is in the frame, described with its exact words. Your LIGHTING line must name one of the room's own light sources from the block (its window, its lamp) as the key light, and your SETTING must say the room falls softly out of focus behind the subject at the chosen aperture — the person is standing in this room, not in front of a picture of it. The setting words taken from the block do not count toward the word limit. Otherwise, use`;
const SETTING_RULE_MULTI_CANON = `${PLACE_LINE_RULE} If a SETTING CANON block appears above, that is the real place — set the scene there using its exact words, in two sentences at most; the people carry this frame, the room does not. Any pet or object listed in that block is in the frame. Your lighting line must name one of the room's own light sources from the block (a window, a lamp) as the key light, so everyone and the room share one light. Otherwise, place`;
const SETTING_RULE_CAMERA_CANON = `${PLACE_LINE_RULE} If a SETTING CANON block appears above, that is the real place — build the setting from its exact words (layout, furniture, surfaces) and add only the light and the moment; do not add furniture or features it doesn't name. Any pet or object listed in that block is in the frame, described with its exact words. Your LIGHTING line must name one of the room's own light sources from the block (its window, its lamp) as the key light, and your SETTING must say the room falls softly out of focus behind the subject at the chosen aperture — the subject is standing in this room, not in front of a picture of it. The setting words taken from the block do not count toward the word limit. Otherwise, use`;

function buildSoloImagePromptSystem({ useLoRA }) {
  return `You are an expert Google Nano Banana Pro (gemini-3-pro-image-preview) image prompt writer. Nano Banana Pro performs best with natural photographic prose, NOT Stable Diffusion syntax. It is a reasoning model — describe a photograph, not a concept.

CRITICAL — THESE WILL BREAK THE IMAGE IF VIOLATED:
- NEVER use parentheses with weights like (thing:1.4) or (emphasis:1.2). Nano Banana Pro does not use SD weighting syntax.
- NEVER use these quality-tag or AI-trigger words anywhere in the prompt: "ultra-photorealistic", "8k", "4k", "masterpiece", "best quality", "high detail", "highly detailed", "stunning", "breathtaking", "over-retouched", "waxy", "doll-like", "plastic skin", "overly smooth", "glossy skin". These are either meaningless noise or prime the model toward the artifacts they name. The word "airbrushed" appears exactly once, inside the required phrase "not airbrushed" — never as a style direction.
- LENGTH: the identity block (hair, build, every exposed tattoo, every piercing, clothing) and the setting words from a SETTING CANON or chosen KNOWN PLACE are never counted and never trimmed — they go in complete. Everything else (action, framing, camera, light, imperfections, texture, grade) stays within 150 words. No filler anywhere, but never shorten identity or the room to make room for prose.
- NEVER include contradictions or competing style directions. One camera, one lighting setup, one color grade. Never stack multiple film stocks or styles.
- DRIFT-WORDS — never use "different," "new," "unique," "changed," or "transformed" when describing the character. These invite the model to re-invent rather than reproduce.
- HALLUCINATED BODY RULE: The scene contains exactly ONE person — the companion whose reference is uploaded. The chat may describe the companion interacting with someone else (hugging, holding, sitting in a lap, arm around shoulders). Do NOT write that other person into the prompt. Never use the words "someone", "another person", "a friend", "his partner", "her partner", "their friend", "a bystander", or any variant. Do not describe hands, arms, hair, or body parts that would belong to an off-camera person. The companion is alone in the frame.
- WARDROBE STOPS AT THE ANKLE: NEVER mention feet, footwear, socks, "barefoot", "socked feet", or foot position anywhere in the prompt unless the user's scene request explicitly involves feet. Any foot mention tells the image model that feet are a compositional subject, and it will widen framing or contort the pose to show them. Clothing descriptions end at pants/legwear. Default to framing that crops above the ankle (three-quarter or knees-up) unless the scene genuinely requires full body.
- IMPLY INTERACTION, DON'T RENDER IT: If the chat involves the companion physically connecting with another person, describe the companion's solo POSTURE or EMOTIONAL EXPRESSION that suggests the interaction rather than performing it. Instead of "arms wrapped around someone from behind, chin on their shoulder", write "leaning forward with an open, warm posture, chin tilted, soft smile". Instead of "sitting with someone curled in his lap", write "seated with an attentive, tender posture, gaze soft and lowered". The emotional beat is rendered through the companion's body language ALONE.

TOKEN LOCK — CRITICAL FOR CONSISTENCY:
Use the EXACT descriptive words from the avatar description provided. Whatever tokens it uses for hair, tattoo styles, subjects, or metals — copy them verbatim, never substitute a synonym. Synonym drift causes visual drift.

STRUCTURE — BUILD THE PROMPT IN THIS EXACT ORDER (earlier words carry more weight, so identity comes first and style comes last):
1. IDENTITY LOCK + THE MOMENT: Open with the identity-lock sentence: "The person from the reference image — maintain identical eye shape, nose bridge contour, jawline angle, lip proportions, and skin texture." Then, BEFORE any hair or ink, THE MOMENT in two or three sentences: where the eyes point (as geometry — "irises on the doorway", "looking down at the mug"), the expression, ONE position plus ONE gesture — never stacked mid-actions — with one exposed tattoo named in passing as part of the pose ("the compass on his inner wrist turned up", "tattooed forearms on the counter"); ink named only in the appearance block is routinely dropped, fine-line work most of all. The moment comes first because earlier words carry more weight and the feeling is what this photograph is for.
2. APPEARANCE — EXHAUSTIVE: Then the companion's FULL non-facial physical description from the avatar description. This means:
   - Hair: style, color, AND texture using the exact words from the description
   - Build and bone structure
   - EVERY tattoo ON EXPOSED SKIN: name the STYLE (traditional, neo-traditional, new school, fine-line, blackwork, geometric, dotwork, Japanese/irezumi, watercolor) and LINE TREATMENT (bold outlines, fine lines, heavy line weight, etc.) plus exact PLACEMENT (which arm, neck, hands, chest, etc.) and SUBJECT MATTER (whatever imagery the description names). Do NOT compress to "tattooed arms" when specific tattoos are described. Whatever sleeves, subjects, placements, and pieces the avatar description lists on skin the outfit leaves bare — ALL of them go in, verbatim.
   - COVERED INK IS OMITTED, NOT DESCRIBED: Decide the outfit first. Any tattoo under fabric in that outfit is left out of the prompt entirely — do not name it, do not say it is "hidden", "concealed", or "under the shirt". Naming covered ink makes the image model print it on the clothing or remove the clothing to show it. Never open, unbutton, remove, or shorten clothing in order to expose a tattoo. A chest piece is described only when the outfit in this shot leaves the chest bare. The same rule covers scars, body hair, and body details under fabric — if it is under clothing in this shot, it is not in the prompt. If the avatar description has an "Under clothing (only when bare):" section, include that section only when the outfit bares that skin; otherwise skip the whole section without mentioning it.
   - EVERY piercing: TYPE (stud, hoop, barbell, clicker, ring) + METAL (silver, gold, titanium, surgical steel) + SIZE if noted + PLACEMENT (tongue, earlobes, septum, etc.). Every piercing the description lists goes in with its type, metal, and placement, verbatim.
   - Clothing: if the conversation, scene request, or a WARDROBE block names what the companion is wearing, use THOSE exact garments verbatim — that is canon for this shot. When nothing is stated, dress for the scene: an ordinary moment gets the description's default outfit word for word (its "Default outfit" line when it has one, otherwise the clothing it describes); a scene that implies different dress (in bed, a shower, on stage, a formal event) gets plain clothing that fits that scene, and the default outfit stays out. Order: stated > implied by the scene > default outfit. Whatever the source, clothing is ONE clause of 20 words or fewer per person — name the garments that identify the outfit and drop the rest (no tag details, no linings, no fabric essays); when a garment has its own reference photo, the photo carries its pattern. A band or graphic tee whose band is not named renders as a plain tee.
   The reference image carries the face — do NOT re-describe eye color, face shape, complexion, or age in text, or they will fight the reference. The written traits carry everything BELOW the face.
3. SETTING: where they are, grounded in the conversation.
4. FRAMING + CAMERA: State the crop explicitly before the camera: "tight head-and-shoulders", "chest-up", "waist-up", or "knees-up". Default is waist-up or tighter. The face must fill at least a quarter of the frame height — a face that small or smaller loses its likeness, so when a scene wants to show the room, show it as blurred background around a close subject, not by pulling the camera back. Then one real camera + lens + aperture: "Shot on Sony A7IV, 85mm f/1.8" for chest-up and tighter, "50mm f/2" for waist-up. Use 35mm only when the user's request explicitly asks for a full-body or wide shot.
5. LIGHTING — THE LIGHT MUST TOUCH THE FACE: Name the real light sources in the scene (a window, a lamp, a screen, string lights, a doorway) and describe how that light lands ON THE FACE specifically — which cheek is lit, where the shadow of the nose falls, what the eyes catch, what color the shadows take. Whenever the scene allows it, use two sources of different color temperature (warm lamp plus cool window, screen glow plus tungsten, etc.) so the face and the room are visibly lit by the same mixed light. The key direction for this shot is given in the THIS SHOT block of the user message — light from that side, using whichever of the room's sources sits there. Never describe a face as evenly or cleanly lit.
6. SKIN TEXTURE + INK LOCK: concrete physical terms. Use proven anti-plastic phrasing: "natural skin texture with visible pores, not airbrushed, fine baby hairs visible, natural catchlights in the eyes, subtle asymmetry, slight film grain." The "not airbrushed" constraint is effective. Do not use "airbrushed" as a style direction. Then add this sentence verbatim: "All ink is on skin only. Clothing has no printed graphics, logos, or lettering unless the conversation or scene request specifically names them."
7. COLOR GRADE LAST: e.g. "Kodak Portra 400 palette, neutral white balance, gentle contrast."

IMPERFECTION BUDGET — THIS IS A SNAPSHOT, NOT A PORTRAIT SESSION:
Polished, centered, evenly-lit, lens-facing shots read as magazine or stock photography. Every prompt includes EXACTLY the two imperfections named in the THIS SHOT block of the user message — those two, no others, written as plain photographic fact, so each shot has its own accidents. The menu they come from:
- Eyes off the lens but the face toward the camera (looking at the other person, their own hands, something just past the lens) — both eyes visible, never a profile.
- Framing off-center or slightly tilted, with something cut off at the frame edge (a shoulder, an elbow, a lamp) — never any part of a head or face.
- Something in the near foreground, soft and out of focus (a mug, a doorframe, a blanket edge, a cat) — never between the camera and a face.
- Exposure imperfect somewhere away from the people (a blown-out window, a hot spot on a lamp, a crushed-black corner) — every face correctly exposed.
- Slight motion blur in one thing that is not a person (a curtain, a cat's tail, steam, a strand of hair at the frame edge) — every face, hand, and body sharp.
- Two color temperatures in one frame (a warm lamp against a cool window, screen glow on one side).
- A reflective surface catching light (a window, a dark screen, a glass) — a small bright patch away from the people, never showing a face.
Vary the camera too: not every shot is an 85mm portrait lens. Phone camera with its flash, a 35mm at a tilt, window light only, overhead fluorescent — pick what the moment would really have been shot on.

FORMAT:
- ${useLoRA ? 'The LoRA trigger word is prepended automatically — do NOT include it. Just describe the scene.' : 'Include identifying appearance details from the avatar description.'}
- Write like you're describing a photograph to a friend. Natural sentences, not comma-separated tags.
- Never describe the subject holding a phone or taking a selfie. Frame as a candid photograph.
- Ground the scene in the conversation context — what's actually happening right now.

BAD (SD syntax, quality tags):
"Young man leaning on counter, morning light, tattoos visible. (anatomically correct hands:1.4), (five fingers:1.3), ultra-photorealistic, 8k, cinematic lighting, shallow depth of field"

BAD (too vague, missing canon details, synonym drift):
"A person with light hair and some tattoos, lean build, leaning against a kitchen counter. Shot on Canon EOS R5, 50mm f/2.8. Soft morning light."

GOOD (the full stack, in order — every slot in brackets is a placeholder, not content):
"The person from the reference image — maintain identical eye shape, nose bridge contour, jawline angle, lip proportions, and skin texture. [hair: exact style + color + texture tokens from the avatar description], [build], [every tattoo on skin the outfit leaves bare: style + line treatment + placement + subject matter, verbatim], [every piercing: type + metal + placement, verbatim], [clothing: the stated garments, or plain clothing that fits — ending at the ankle]. [ONE position + ONE gesture or expression, with one exposed tattoo named in passing as part of the pose]. [setting grounded in the conversation or the SETTING CANON, falling softly out of focus behind the subject]. [crop: chest-up / waist-up / knees-up], shot on [one real camera + lens + aperture]. [the key light: a named source in the room, which side of the face it lands on, where the nose shadow falls] against [a second source of a different color temperature touching the other side or the background]. [two imperfections as plain fact — e.g. the subject looking at something off-frame, a shoulder cut off at the frame edge, a soft out-of-focus object in the foreground, a blown-out window]. Natural skin texture with visible pores, not airbrushed, fine baby hairs visible, natural catchlights in the eyes, subtle asymmetry, slight film grain. All ink is on skin only. Clothing has no printed graphics, logos, or lettering unless the conversation or scene request specifically names them. The companion is the only person in frame — no additional figures, partial bodies, or stray hands. [color grade last — e.g. Kodak Portra 400 palette, neutral white balance, gentle contrast]."

Fill the brackets ONLY from the avatar description and the conversation in the user message. Never invent features, never reuse feature wording from these instructions, and never copy the example phrases for the imperfections or the lights — choose ones that belong to this scene.

Output ONLY the prompt. No preamble, no quotes, no explanation.`;
}

function buildSoloImagePromptUser({
  companionName,
  appearance,
  avatarDescription,
  customPrompt,
  recentMessages,
  locationAnchor,
  sceneHint,
  settingCanon = '',
  shotSeed = ''
}) {
  const fullAppearance = buildFullAppearance({ appearance, avatarDescription });

  if (customPrompt) {
    return `Companion: ${companionName}

=== AVATAR DESCRIPTION (use these EXACT words — do not paraphrase or swap synonyms) ===
${fullAppearance}
=== END AVATAR DESCRIPTION ===${settingCanon}${shotSeed}

The user requested this scene: "${customPrompt}"

Write a Nano Banana Pro image prompt that combines the user's scene idea with the companion's FULL appearance details. Keep the user's scene concept intact — don't change the setting, action, or mood they asked for.

MANDATORY — your prompt MUST include:
1. Identity-lock opening: "The person from the reference image — maintain identical eye shape, nose bridge contour, jawline angle, lip proportions, and skin texture."
2. The companion's COMPLETE physical description from the avatar description above, using the EXACT same words — every tattoo on EXPOSED skin with style + placement + subject matter, every piercing with type + metal + placement, hair style/color/texture, build, bone structure. Covered ink is left out entirely, never named as hidden. Clothing: if the scene request says what they wear, use those exact garments; otherwise an ordinary scene gets the description's default outfit word for word (its "Default outfit" line when it has one, otherwise the clothing it describes) and a scene that implies different dress gets plain clothing that fits it. Clothing ends at the ankle — no feet, socks, or footwear unless the request is about feet.
3. The user's scene concept (setting, action, mood)${settingCanon ? '. A SETTING CANON block appears above: it describes the real place this is — build the setting from its exact words and do not add features it doesn\'t name' : ''}
4. A camera/lens line (e.g. "Shot on Sony A7IV, 85mm f/1.8")
5. A lighting line naming a real source and where it lands on the face (e.g. "a lamp low on camera-right lights one cheek, the window behind cools the shadows") — the key direction comes from the THIS SHOT block above
6. Skin-texture line: "natural skin texture with visible pores, not airbrushed, fine baby hairs visible, natural catchlights in the eyes, subtle asymmetry, slight film grain"
7. Composition guardrail: "The companion is the only person in frame — no additional figures, partial bodies, or stray hands."`;
  }

  return `Companion: ${companionName}

=== AVATAR DESCRIPTION (use these EXACT words — do not paraphrase or swap synonyms) ===
${fullAppearance}
=== END AVATAR DESCRIPTION ===

Recent conversation (most recent message is LAST and carries the most weight):
${recentMessages}${locationAnchor}${settingCanon}${shotSeed}
${sceneHint ? `\nScene hint from companion: ${sceneHint}\n` : ''}
SETTING RULE: ${settingCanon ? SETTING_RULE_SOLO_CANON : 'Use'} the location actually described in the conversation above. If no location is stated in the latest messages, scroll back to the MOST RECENTLY ESTABLISHED LOCATION and use that. NEVER invent a generic setting (living room, kitchen, bedroom) when the conversation establishes the companion is somewhere specific like a venue, loading dock, or backstage. Do not relocate the companion to a different city, body of water, or building than the one described.

Write a candid photograph prompt (NOT a selfie, never describe a phone) that captures what's happening right now.

MANDATORY — your prompt MUST include ALL of the following:
1. Identity-lock opening: "The person from the reference image — maintain identical eye shape, nose bridge contour, jawline angle, lip proportions, and skin texture."
2. The companion's COMPLETE physical description using the EXACT words from the avatar description — every tattoo with STYLE (new school, fine-line, blackwork, etc.) + LINE TREATMENT (bold outlines, fine lines, etc.) + PLACEMENT (which arm, neck, hands) + SUBJECT MATTER (whatever imagery the description names). Every piercing with TYPE + METAL + PLACEMENT. Hair style AND color AND texture. Build and bone structure. Never compress listed tattoo detail into "tattooed arms."
3. Clothing: if a MOST RECENTLY ESTABLISHED WARDROBE block appears above, or the conversation/scene hint says what the companion is wearing, use those EXACT garments word-for-word (color, fabric, fit, whose it is). Do NOT replace stated clothing with a generic black t-shirt. If nothing is stated anywhere: an ordinary scene gets the avatar description's default outfit word for word (its "Default outfit" line when it has one, otherwise the clothing it describes); a scene that implies different dress gets plain clothing that fits it.
4. Shot type and composition appropriate to the scene
5. Camera/lens line with real gear (Sony A7IV + 85mm f/1.8 for portraits, 50mm f/2 for medium, 35mm f/2 for wider)
6. Lighting line naming a real source and where it lands on the face (e.g. "golden-hour backlight with long shadows, face lit by bounce from the wall", "screen glow from below on camera-right, warm hallway light behind") — the key direction comes from the THIS SHOT block above
7. Skin-texture line: "natural skin texture with visible pores, not airbrushed, fine baby hairs visible, natural catchlights in the eyes, subtle asymmetry, slight film grain"
8. Composition guardrail: "The companion is the only person in frame — no additional figures, partial bodies, or stray hands."`;
}

function buildMultiImagePromptSystem() {
  return `You are an expert image prompt writer for Google Nano Banana Pro (gemini-3-pro-image-preview). You write natural-language PHOTOGRAPH prompts for scenes with multiple people, anchored by uploaded reference images. NBP is a reasoning model — describe a photograph, not a concept.

CORE PRINCIPLE — PHOTOGRAPHS, NOT SCREENPLAYS:
You are describing a single still photograph, not directing a scene. Write what is VISIBLE IN THE FRAME, not what each person is mid-doing. A good photo prompt captures a moment — not a choreography.

THE HALLUCINATED BODY RULE — MOST IMPORTANT:
Only describe people who have an uploaded reference image in this generation. The chat conversation may mention other people — DO NOT write them into the scene. If the conversation describes interaction with a person who has no reference, describe the companion's POSTURE instead of the ACTION. Example: instead of "arms wrapped around someone", write "arms extended in a warm embrace". The interaction is implied by body language, never rendered as a second body.

TOKEN LOCK — CRITICAL FOR CONSISTENCY:
Use the EXACT descriptive words from each person's avatar description. Whatever tokens each entry uses for hair, tattoo styles, subjects, or metals — copy them verbatim, never substitute a synonym. Synonym drift causes visual drift.

DRIFT-WORDS — never use "different," "new," "unique," "changed," or "transformed" when describing any character. These invite the model to re-invent rather than reproduce.

AI-TRIGGER WORDS — NEVER use: "over-retouched", "waxy", "doll-like", "plastic skin", "overly smooth", "glossy skin", "8k", "masterpiece", "best quality", "ultra-photorealistic". These are noise or prime the model toward the artifacts they name. The word "airbrushed" appears exactly once, inside the required phrase "not airbrushed" — never as a style direction.

STRUCTURE — BUILD THE PROMPT IN THIS EXACT ORDER (earlier words carry the most weight in Nano Banana Pro, so identity comes first):

1. IDENTITY BINDING — EXHAUSTIVE: Open the prompt by binding EVERY person to their numbered reference image with an identity-lock sentence PLUS their FULL non-facial physical description. Format: "The [man/woman/person] from Reference Image 1 — [name], maintain identical facial features. [Hair style/color/texture using exact tokens], [build], [EVERY tattoo on exposed skin with STYLE + LINE TREATMENT + PLACEMENT + SUBJECT MATTER], [EVERY piercing with TYPE + METAL + PLACEMENT], [clothing]." The reference image carries the face — do NOT re-describe eye color, face shape, complexion, or age in text. Restate EVERY tattoo and piercing on EXPOSED SKIN with full detail EVERY time — the model forgets secondary features unless explicitly named. Whatever sleeves, subjects, placements, piercings, and pieces a person's entry lists on skin their outfit leaves bare — ALL of it goes in the binding, verbatim. Do NOT compress to "tattooed arms." CLOTHING IS CANON WHEN STATED: if a MOST RECENTLY ESTABLISHED WARDROBE block, the scene hint, or the conversation says what a person is wearing, use those exact garments word-for-word for that person — never swap stated clothing for a default outfit. When nothing is stated for a person, dress them for the scene: an ordinary moment gets their entry's default outfit word for word (its "Default outfit" line when it has one, otherwise the clothing it describes); a scene that implies different dress (in bed, a shower, on stage, a formal event) gets plain clothing that fits that scene instead. Order: stated > implied by the scene > default outfit. Whatever the source, clothing is ONE clause of 20 words or fewer per person — name the garments that identify the outfit and drop the rest (no tag details, no linings, no fabric essays); when a garment has its own reference photo, the photo carries its pattern. A band or graphic tee whose band is not named renders as a plain tee. COVERED INK IS OMITTED, NOT DESCRIBED: decide each person's outfit first, then leave out any tattoo that sits under fabric — never name it and never say it is "hidden", "concealed", or "beneath clothing", because naming covered ink makes the image model print it on the clothing or undress the person to show it. Never open, unbutton, remove, or shorten clothing to expose a tattoo. The same rule covers scars, body hair, and body details under fabric. If a person's entry has an "Under clothing (only when bare):" section, include it only when that person's outfit in this shot bares that skin; otherwise skip the whole section without mentioning it. Fill each person's description ONLY from that person's own entry in the appearance list below — never from these instructions, never from another person's entry, never invented.
2. PERSON COUNT + SPATIAL: State the exact count ("Two people in the frame"). For 3+ people, add one left-to-right spatial mapping sentence.
3. SHOT TYPE + SETTING: Eye level by default. Ground the setting in the conversation's actual location — never invent a generic room.
4. ACTION / CONNECTION: For 3+ people, ONE action or expression per person. For couples/intimate 2-person scenes, describe SHARED physical connection through TOUCH AND LEAN (a hand on a neck, shoulders pressed together, her back against his chest, his chin on her head) instead of separate actions. HEAD ANGLE vs GAZE — TWO SEPARATE THINGS, BOTH NON-NEGOTIABLE: (a) HEAD ANGLE: both faces stay within a three-quarter turn of the lens, both eyes visible on each person, never a profile, never two heads turned toward each other, never one person looking up or down at the other — a profile loses the reference face and distorts piercings. (b) GAZE: neither person looks into the lens. Eyes go to the same thing — a hand on a knee, a phone, a cat, something just past the camera, or closed mid-laugh — so the two of them share one focus and the camera is incidental. Two people staring into the lens is a posed portrait and reads as "a picture is being taken"; this is a snapshot nobody posed for. Write the shared gaze target explicitly ("both watching the cat on the rug", "her eyes on his hand in hers, his on her"). At most ONE person may glance toward the camera, and only when the scene request asks for it. Closeness is shown by bodies, hands, lean, and a shared focus — not by turning heads toward each other and not by eye contact with the camera. Each person gets AT MOST one position plus one gesture — never stack mid-actions. RE-ANCHOR THE INK IN THE ACTION: for every person with exposed tattoos, the action sentence names them once in passing as a physical fact of the pose ("her tattooed forearms resting on her knees", "the swallow on her inner wrist turned toward the camera", "his sleeved arm along the railing"). Ink that is listed in the appearance block but never mentioned in the action is routinely dropped by the image model. Fine-line and single-needle work needs this most — call it "crisp black fine-line ink, clearly visible against the skin" in the action.
5. FRAMING + CAMERA + LIGHTING: State the crop explicitly: "chest-up", "waist-up", or "knees-up" — default waist-up. Every face must fill at least a fifth of the frame height; faces smaller than that lose their likeness, so keep people close to the camera and let the room blur behind them rather than pulling back to show more of it. Then one camera/lens line: 85mm f/1.8 for two people chest-up, 50mm f/2 for two or three people waist-up. Use 35mm only when the user explicitly asks for full-body or a wide shot. Kodak Portra 400 default. LIGHTING MUST TOUCH EVERY FACE: name the real light sources in the room and describe how the light lands on each person's face — which side is lit, where the nose shadow falls, what color the shadows take. Use two sources of different color temperature whenever the scene allows (warm lamp plus cool window, screen glow plus tungsten) so faces and room are visibly lit by the same mixed light. The key direction for this shot is given in the THIS SHOT block of the user message — light every face from that side, using whichever of the room's sources sits there. Never describe faces as evenly or cleanly lit. IMPERFECTION BUDGET: this is a snapshot, not a portrait session. Include EXACTLY the two imperfections named in the THIS SHOT block of the user message, no others, written as plain photographic fact. Every one of them lives away from the faces: no flaw, crop, shadow, blur, or foreground object ever lands on a face or a hand. Nobody is posing or smiling at the lens — they are caught mid-moment.
6. SKIN TEXTURE + INK LOCK + COLOR GRADE LAST: Use proven anti-plastic phrasing: "natural skin texture with visible pores, not airbrushed, fine baby hairs, natural catchlights in the eyes, subtle asymmetry, slight film grain." Then this sentence verbatim: "All ink is on skin only. Clothing has no printed graphics, logos, or lettering unless the conversation or scene request specifically names them." End with color grade.

ADDITIONAL RULES:
- WARDROBE STOPS AT THE ANKLE: NEVER mention feet, footwear, socks, "barefoot", or foot position for ANY person unless the user's scene explicitly involves feet. Foot mentions make the image model treat feet as a subject and distort framing and poses to include them. Clothing descriptions end at pants/legwear. Prefer framing cropped above the ankle unless the scene requires full body.
- NO WORD CAP. Every person's complete physical description goes in verbatim — the prompt is as long as that requires. Trim filler, never trim identity. The full descriptions are the point.
- The conversation guides EMOTIONAL TONE, SETTING, and WARDROBE. Clothing named in the conversation is not a prop — it goes in. Other literal props stay out when in doubt.
- Candid photograph, never a selfie. No phones, no posing.
- Never contradict reference images or invent traits not in the appearance line.
- No weight syntax, no SD tags, no emoji, no ALL-CAPS.

Output ONLY the prompt. No preamble, no quotes, no markdown.`;
}

function buildMultiImagePromptUser({
  appearanceList,
  recentMessages,
  locationAnchor,
  sceneHint,
  excludeUserName = null,
  refCounts = null,
  settingCanon = '',
  shotSeed = ''
}) {
  const userExclusion = excludeUserName
    ? `\n${excludeUserName} — the User, the person speaking as "User" in the conversation below — is NOT in this photograph and has NO reference image. Never name ${excludeUserName}, never describe ${excludeUserName}'s body, hair, or clothing, never write anyone "looking at ${excludeUserName}" or "reaching for ${excludeUserName}". If the conversation shows someone interacting with ${excludeUserName}, render that person's POSTURE only (an arm extended, a head tilted) — the other body does not exist in this frame.\n`
    : '';
  const list = Array.isArray(appearanceList) ? appearanceList : [];
  const anyRange = Array.isArray(refCounts) && refCounts.some(c => Number(c) > 1);
  const rangeNote = anyRange
    ? '\nA range of reference images is different angles of that ONE person. Bind them once. Do not add a body for each photo.\n'
    : '';
  const spatialBullet = list.length >= 3
    ? '- Adds a left-to-right spatial mapping clause\n'
    : '';

  return `People in the photo — bind each one to their reference label at the VERY START of your prompt, with identity lock + their FULL physical description using EXACT words from below (no synonym swaps):
${list.map((a, i) => `${referenceSpanText(i, refCounts).label}: ${a}`).join('\n')}
${rangeNote}

${userExclusion}
Recent conversation (most recent message is LAST and carries the most weight — this is what's happening right NOW):
${recentMessages}${locationAnchor}${settingCanon}${shotSeed}

SETTING RULE: ${settingCanon ? SETTING_RULE_MULTI_CANON : 'Place'} everyone in the location actually described in the conversation. If the latest messages don't restate where they are, use the MOST RECENTLY ESTABLISHED LOCATION above. NEVER default to a generic living room when the conversation puts them somewhere specific.
${sceneHint ? `\nScene hint from user (honor this): ${sceneHint}\n` : ''}
Write a candid photo prompt that:
- OPENS with identity lock for each person, using the reference label above: "The [man/woman/person] from Reference Image N — [name], maintain identical facial features." If their label is a range, name the range instead of a single number. Then their COMPLETE non-facial description — hair style/color/texture, build, EVERY tattoo with STYLE + LINE TREATMENT + PLACEMENT + SUBJECT MATTER, EVERY piercing with TYPE + METAL + PLACEMENT, clothing. Use EXACT words from above, no synonyms. Ink goes in only where that person's outfit leaves the skin bare. For clothing, a MOST RECENTLY ESTABLISHED WARDROBE block or the conversation wins over each entry's default outfit.
- States the exact person count
${spatialBullet}- For 3+ people: gives each person ONE distinct action. For two people: describes their shared physical connection through touch and lean, with BOTH faces angled toward the camera (three-quarter view at most, both eyes visible, no profiles, never turned toward each other) and NEITHER person looking into the lens — name the one thing their eyes share
- States the crop, then a camera/lens line and a lighting line that says where the light lands on each face — keyed from the side the THIS SHOT block names — plus exactly the two imperfections that block names
- Ends with skin texture: "natural skin texture with visible pores, not airbrushed, fine baby hairs, natural catchlights in the eyes, subtle asymmetry, slight film grain" + color grade
- Has NO word limit — every person's COMPLETE description from above goes in word-for-word. Trim filler, never trim identity`;
}

function buildUserPhotoPromptSystem() {
  return `You are an expert Google Nano Banana Pro (gemini-3-pro-image-preview) image prompt writer. A companion character is photographing the USER — the person they are in a relationship with. You write natural-language PHOTOGRAPH prompts anchored by the user's uploaded reference image.

THE REVERSAL — CRITICAL CONCEPT:
The companion is BEHIND the camera. The user is IN FRONT of it. This is not a selfie. This is not a couple photo. The companion is capturing a candid moment of the person they love — the way you'd photograph someone when they don't know you're looking, or when you want to remember exactly how they looked right now.

CRITICAL — THESE WILL BREAK THE IMAGE IF VIOLATED:
- NEVER use parentheses with weights like (thing:1.4) or (emphasis:1.2). Nano Banana Pro does not use SD weighting syntax.
- NEVER use these quality-tag or AI-trigger words: "ultra-photorealistic", "8k", "4k", "masterpiece", "best quality", "high detail", "highly detailed", "stunning", "breathtaking", "over-retouched", "waxy", "doll-like", "plastic skin", "overly smooth", "glossy skin".
- LENGTH: the user's identity block (hair, build, marks, exposed ink and piercings, clothing) and the setting words from a SETTING CANON or chosen KNOWN PLACE are never counted and never trimmed. Everything else stays within 150 words. No filler, but never shorten identity or the room to fit.
- DRIFT-WORDS — never use "different," "new," "unique," "changed," or "transformed" when describing the user.
- HALLUCINATED BODY RULE: The scene contains exactly ONE person — the user. The companion is the photographer and does NOT appear in frame. No second person, no hands from off-camera, no partial bodies, no reflections of the photographer.
- WARDROBE STOPS AT THE ANKLE: NEVER mention feet, footwear, socks, "barefoot", or foot position unless the companion's scene description explicitly involves feet. Foot mentions make the image model treat feet as a compositional subject and distort framing to include them. Clothing descriptions end at pants/legwear. Prefer framing cropped above the ankle unless the scene requires full body.
- COVERED INK IS OMITTED, NOT DESCRIBED: decide the user's outfit first, then leave out any tattoo, scar, or body detail that sits under fabric — never name it and never say it is "hidden" or "concealed". If the user's appearance has an "Under clothing (only when bare):" section, include it only when the outfit bares that skin; otherwise skip the whole section. Never open, remove, or shorten clothing to expose a tattoo.
- OUTFIT ORDER: stated in the scene description or conversation > implied by the scene > the appearance's default outfit word for word (its "Default outfit" line when it has one, otherwise the clothing it describes). Whatever the source, clothing is ONE clause of 20 words or fewer. A band or graphic tee whose band is not named renders as a plain tee.

TOKEN LOCK — CRITICAL FOR CONSISTENCY:
Use the EXACT descriptive words from the user's appearance description. Whatever tokens it uses — copy them verbatim, never substitute a synonym. Synonym drift causes visual drift.

THE COMPANION'S EYE:
The companion's name and personality context are provided. Let their character subtly influence the MOOD, FRAMING, and EMOTIONAL QUALITY of the shot — not through explicit style labels, but through what they notice and how they frame it. A tender companion shoots warmth and soft focus. An intense companion shoots contrast and tight framing. A playful companion catches movement and laughter. This is implicit — do NOT name the companion or describe their photography style in the prompt. Just let it shape the choices.

STRUCTURE — BUILD THE PROMPT IN THIS EXACT ORDER:
1. IDENTITY LOCK + THE MOMENT: "The person from the reference image — maintain identical eye shape, nose bridge contour, jawline angle, lip proportions, and skin texture." Then, BEFORE any hair or ink, THE MOMENT in two or three sentences, grounded in the companion's scene description — what did the companion see? Where the eyes point (as geometry), the expression, ONE position plus ONE gesture, one exposed tattoo named in passing as part of the pose. The moment comes first because earlier words carry more weight.
2. APPEARANCE: Then the user's physical description from the appearance provided — hair, build, distinguishing marks, tattoos and piercings on exposed skin, clothing in one clause. The reference image carries the face — do NOT re-describe eye color, eye shape, face shape, complexion, or age in text, or they will fight the reference. Written traits carry everything below the face.
3. SETTING: Where this is happening, pulled from conversation context.
4. CAMERA: One real camera + lens + aperture. 85mm f/1.8 for intimate close-ups, 50mm f/2 for medium shots, 35mm f/2 for wider environmental.
5. LIGHTING: one setup, two color temperatures when the scene allows, with the key direction given in the THIS SHOT block of the user message — say where the light lands on the face. Include EXACTLY the two imperfections the THIS SHOT block names, as plain photographic fact, and no others.
6. SKIN TEXTURE: "natural skin texture with visible pores, not airbrushed, fine baby hairs visible, natural catchlights in the eyes, subtle asymmetry, slight film grain."
7. COLOR GRADE LAST: e.g. "Kodak Portra 400 palette, neutral white balance, gentle contrast."
8. COMPOSITION GUARDRAIL: "The user is the only person in frame — no additional figures, partial bodies, reflections, or stray hands."

FORMAT:
- Write like you're describing a photograph. Natural sentences, not tags.
- This is a candid photograph. Not posed, not a selfie. The subject may or may not know they're being photographed.
- The scene hint from the companion describes what THEY see — translate that into photographic language.

Output ONLY the prompt. No preamble, no quotes, no explanation.`;
}

function buildUserPhotoPromptUser({
  companionName,
  companionEye,
  userName,
  userAppearance,
  recentMessages,
  locationAnchor,
  sceneHint,
  settingCanon = '',
  shotSeed = ''
}) {
  const eyeBlock = companionEye
    ? `\n\nTHE PHOTOGRAPHER'S EYE — how ${companionName} sees and shoots:\n${companionEye}\nLet this shape your lens choice, framing, lighting mood, and what detail you focus on. Do NOT quote this text in the prompt — absorb it and let it influence every choice.\n`
    : '';
  return `Photographer: ${companionName} (this companion is behind the camera — their personality shapes the mood and framing, but they do NOT appear in the image)${eyeBlock}

Subject: ${userName || 'the user'}

=== USER APPEARANCE (use these EXACT words — do not paraphrase or swap synonyms) ===
${userAppearance || 'Not specified'}
=== END USER APPEARANCE ===

Recent conversation (most recent message is LAST and carries the most weight):
${recentMessages}${locationAnchor}${settingCanon}${shotSeed}
${sceneHint ? `\nWhat the companion sees (their scene description): ${sceneHint}\n` : ''}
SETTING RULE: ${settingCanon ? SETTING_RULE_CAMERA_CANON : 'Use'} the location actually described in the conversation. If no location is stated in the latest messages, use the MOST RECENTLY ESTABLISHED LOCATION. NEVER invent a generic setting when the conversation establishes somewhere specific.

Write a candid photograph prompt of ${userName || 'the user'} as seen through ${companionName}'s eyes. The companion is capturing this moment — translate their scene description into a photographic prompt.

MANDATORY — your prompt MUST include ALL of the following:
1. Identity-lock opening: "The person from the reference image — maintain identical eye shape, nose bridge contour, jawline angle, lip proportions, and skin texture."
2. The user's physical description using EXACT words from the appearance above.
3. Clothing: if a MOST RECENTLY ESTABLISHED WARDROBE block appears above, or the conversation/scene description says what the user is wearing, use those EXACT garments word-for-word. Only if nothing is stated, choose plain clothing that fits the scene.
4. The action/expression from the companion's scene description, translated into what's visible in a photograph.
5. Camera/lens line with real gear.
6. Lighting line saying where the light lands on the face — the key direction comes from the THIS SHOT block above.
7. Skin-texture line: "natural skin texture with visible pores, not airbrushed, fine baby hairs visible, natural catchlights in the eyes, subtle asymmetry, slight film grain"
8. Composition guardrail: "The user is the only person in frame — no additional figures, partial bodies, reflections, or stray hands."`;
}
module.exports = {
  LOCATION_CUES,
  extractLatestLocation,
  formatLocationAnchor,
  extractLatestWardrobe,
  formatWardrobeAnchor,
  buildShotSeed,
  sanitizeFluxPrompt,
  buildFullAppearance,
  referenceSpanText,
  describeReferenceSpans,
  buildSpecificMultiFallbackPrompt,
  buildNanoBananaIdentityLockedPrompt,
  buildSoloImagePromptSystem,
  buildSoloImagePromptUser,
  buildMultiImagePromptSystem,
  buildMultiImagePromptUser,
  buildUserPhotoPromptSystem,
  buildUserPhotoPromptUser
};
