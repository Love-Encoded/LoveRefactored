'use strict';

/**
 * Small-model image prompt templates — used when the image prompt writer runs on LM Studio.
 *
 * The big prompts in image-prompts.js work great on frontier models, but small local models
 * (Gemma, Llama 8B, etc.) copy the prose example verbatim, ignore clothing changes, and
 * misgender the companion. These variants are:
 *   - slot-based with NO prose example (nothing to copy)
 *   - explicit about pronouns for every person
 *   - explicit about clothing priority (scene beats default outfit)
 *   - ordered so the avatar description is the LAST thing the model reads (recency bias)
 */

const { buildFullAppearance } = require('./image-prompts');

// ---------------------------------------------------------------------------
// Pronouns
// ---------------------------------------------------------------------------

/**
 * Infer a companion's pronouns from their card text (companion cards have no gender field).
 * Counts he/him vs she/her vs they/them across the card; majority wins; nothing found → they/them.
 * If the card ever gets a real `pronouns` field, it wins.
 */
function inferCompanionPronouns(card) {
  const explicit = String(card?.pronouns || '').trim();
  if (explicit) return explicit;

  const text = [
    card?.avatarDescription, card?.appearance, card?.personalityVoice,
    card?.backstory, card?.systemPromptOverride
  ].filter(Boolean).join(' ').toLowerCase();

  const count = re => (text.match(re) || []).length;
  const he = count(/\b(he|him|his|himself)\b/g);
  const she = count(/\b(she|her|hers|herself)\b/g);
  const they = count(/\b(they|them|their|theirs|themself|themselves)\b/g);

  if (he === 0 && she === 0 && they === 0) return 'they/them';
  if (he >= she && he >= they) return 'he/him';
  if (she >= he && she >= they) return 'she/her';
  return 'they/them';
}

/** Turn the user persona's gender field into a pronoun string. */
function personaPronouns(persona) {
  const g = String(persona?.gender || '').trim().toLowerCase();
  if (/\b(she|her|woman|female|girl|femme|wife|girlfriend|f)\b/.test(g)) return 'she/her';
  if (/\b(he|him|man|male|boy|masc|husband|boyfriend|m)\b/.test(g)) return 'he/him';
  return 'they/them';
}

// ---------------------------------------------------------------------------
// Shared blocks
// ---------------------------------------------------------------------------

const HARD_RULES = `HARD RULES:
- Copy hair, build, tattoos, and piercings from each AVATAR DESCRIPTION word for word. Do not shorten them. Do not swap synonyms.
- Do NOT describe faces, eye color, skin color, or age. The reference images handle faces.
- Use each person's PRONOUNS exactly as given, every time. Never switch pronouns mid-paragraph. Never give one person another person's pronouns.
- No phones, no selfies.
- No parentheses, no weights like (thing:1.4), no words like "8k", "masterpiece", "ultra-realistic", "airbrushed".`;

const CLOTHING_PRIORITY = `CLOTHING PRIORITY (follow in this order, stop at the first one that exists):
1. If a CURRENT OUTFIT is given for that person, use it exactly.
2. Otherwise, if the conversation says what they are wearing, use that.
3. Otherwise, use the default clothing from their avatar description.
Never use #3 when #1 or #2 exists.`;

const SKIN_AND_GRADE = `"Natural skin texture with visible pores, not airbrushed, fine baby hairs visible, natural catchlights in the eyes, subtle asymmetry, slight film grain. Kodak Portra 400 palette, gentle contrast."`;

// ---------------------------------------------------------------------------
// SOLO
// ---------------------------------------------------------------------------

function buildSoloImagePromptSystemSmall({ useLoRA }) {
  return `You write image prompts for a photo generator. You will be given a character's PRONOUNS, an AVATAR DESCRIPTION, and a conversation. Write ONE paragraph describing a photograph of that character.

${HARD_RULES}
- Only ONE person in the photo. Never mention anyone else, even if the conversation does.
- Under 200 words.

${CLOTHING_PRIORITY}

WRITE THE PARAGRAPH BY FILLING THESE SLOTS IN ORDER:
[1] "The person from the reference image — maintain identical eye shape, nose bridge contour, jawline angle, lip proportions, and skin texture."
[2] Hair, build, every tattoo, every piercing — copied from the avatar description.
[3] "wearing" + clothing chosen by the CLOTHING PRIORITY.
[4] One position (sitting / standing / leaning) + one expression or gesture, based on the latest message.
[5] The setting from the conversation. Use the MOST RECENTLY ESTABLISHED LOCATION if given. Never invent a generic room.
[6] "Shot on Sony A7IV, 85mm f/1.8."
[7] One lighting sentence with a direction (from the left, from a window, overhead, etc.).
[8] ${SKIN_AND_GRADE}
[9] "The companion is the only person in frame — no additional figures, partial bodies, or stray hands."

${useLoRA ? 'Do NOT include a LoRA trigger word — it is added automatically.\n' : ''}Output ONLY the paragraph. No labels, no slot numbers, no quotes, no explanation.`;
}

function buildSoloImagePromptUserSmall({
  companionName,
  card,
  appearance,
  avatarDescription,
  customPrompt,
  recentMessages,
  locationAnchor,
  sceneHint,
  currentOutfit
}) {
  const fullAppearance = buildFullAppearance({
    appearance: appearance ?? card?.appearance,
    avatarDescription: avatarDescription ?? card?.avatarDescription
  });
  const pronouns = inferCompanionPronouns(card);
  const outfitLine = currentOutfit
    ? `\nCURRENT OUTFIT (use this, it overrides the default clothing): "${String(currentOutfit).trim()}"\n`
    : '';

  const identityBlock = `PRONOUNS for ${companionName}: ${pronouns} — use these and only these.

=== AVATAR DESCRIPTION for ${companionName} — copy hair, build, tattoos, piercings word for word ===
${fullAppearance}
=== END AVATAR DESCRIPTION ===

Now write the paragraph.`;

  if (customPrompt) {
    return `Companion: ${companionName}

The user wants this scene: "${customPrompt}"
Keep their setting, action, and mood exactly as asked.${outfitLine}
${identityBlock}`;
  }

  return `Companion: ${companionName}

Recent conversation (the LAST message is what is happening right now):
${recentMessages}${locationAnchor}
${sceneHint ? `\nScene hint from companion: ${sceneHint}\n` : ''}${outfitLine}
${identityBlock}`;
}

// ---------------------------------------------------------------------------
// MULTI (couples + groups)
// ---------------------------------------------------------------------------

function buildMultiImagePromptSystemSmall() {
  return `You write image prompts for a photo generator. You will be given a list of people, each with a Reference Image number, PRONOUNS, and an AVATAR DESCRIPTION, plus a conversation. Write ONE paragraph describing a single photograph of ALL of those people together.

${HARD_RULES}
- Only describe the people in the list. Never add anyone else, even if the conversation mentions them.
- State the exact number of people in the photo.
- Under 220 words.

${CLOTHING_PRIORITY}

WRITE THE PARAGRAPH BY FILLING THESE SLOTS IN ORDER:
[1] For EACH person, in list order: "The person from Reference Image N — [name], maintain identical facial features." Then that person's hair, build, every tattoo, every piercing copied from their avatar description, then "wearing" + their clothing chosen by the CLOTHING PRIORITY. Use that person's pronouns.
[2] "[Number] people in the frame." If three or more, add one sentence listing them left to right by Reference Image number.
[3] For two people: one sentence describing how they are physically connected (leaning together, foreheads touching, arm around shoulders). For three or more: one action or expression per person.
[4] The setting from the conversation. Use the MOST RECENTLY ESTABLISHED LOCATION if given. Never invent a generic room.
[5] Two people: "Shot on a 50mm f/2 lens." Three or more: "Shot on a 35mm f/2.8 lens."
[6] One lighting sentence with a direction.
[7] ${SKIN_AND_GRADE}

Output ONLY the paragraph. No labels, no slot numbers, no quotes, no explanation.`;
}

/**
 * @param {string[]} appearanceList  - "Name: description" strings, same as the big prompt gets
 * @param {string[]} pronounsList    - parallel array of pronoun strings ("he/him", "she/her", "they/them")
 * @param {string[]} [outfitList]    - optional parallel array of current outfits (empty string = none)
 */
function buildMultiImagePromptUserSmall({
  appearanceList,
  pronounsList,
  outfitList,
  recentMessages,
  locationAnchor,
  sceneHint
}) {
  const list = Array.isArray(appearanceList) ? appearanceList : [];
  const pronouns = Array.isArray(pronounsList) ? pronounsList : [];
  const outfits = Array.isArray(outfitList) ? outfitList : [];

  const people = list.map((entry, i) => {
    const colonIdx = entry.indexOf(':');
    const name = colonIdx > 0 ? entry.slice(0, colonIdx).trim() : `Person ${i + 1}`;
    const desc = colonIdx > 0 ? entry.slice(colonIdx + 1).trim() : entry.trim();
    const p = pronouns[i] || 'they/them';
    const outfit = String(outfits[i] || '').trim();
    return `--- Reference Image ${i + 1}: ${name} ---
PRONOUNS: ${p} — use these and only these for ${name}.${outfit ? `\nCURRENT OUTFIT for ${name} (overrides default clothing): "${outfit}"` : ''}
AVATAR DESCRIPTION for ${name} — copy hair, build, tattoos, piercings word for word:
${desc}`;
  }).join('\n\n');

  return `Recent conversation (the LAST message is what is happening right now):
${recentMessages}${locationAnchor}
${sceneHint ? `\nScene hint from user (honor this): ${sceneHint}\n` : ''}
${list.length} people in this photo. Here they are:

${people}

Now write the paragraph.`;
}

module.exports = {
  inferCompanionPronouns,
  personaPronouns,
  buildSoloImagePromptSystemSmall,
  buildSoloImagePromptUserSmall,
  buildMultiImagePromptSystemSmall,
  buildMultiImagePromptUserSmall
};