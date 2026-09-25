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

function buildSpecificMultiFallbackPrompt(names, appearanceList, recentMessages, sceneHint) {
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
    .map(p => `the person from Reference Image ${p.index + 1}${p.name ? ` (${p.name}${p.desc && p.desc !== '(no description)' ? ` — ${p.desc}` : ''})` : ''}`)
    .join(', ');

  const spatialLine = parsedPeople.length >= 3
    ? `Left to right in the frame: ${parsedPeople.map(p => `Reference Image ${p.index + 1}`).join(', ')}.`
    : '';

  const identityLock = `Each face matches its reference image exactly — identical eye shape, nose bridge contour, jawline angle, lip proportions, and skin texture. ${parsedPeople.map(p => `${p.name || `Person ${String.fromCharCode(65 + p.index)}`} = Reference Image ${p.index + 1}`).join('. ')}. Each face rendered individually with unique features preserved.`;

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

function buildNanoBananaIdentityLockedPrompt(scenePrompt, names, deps) {
  const cleanNames = (Array.isArray(names) ? names : [])
    .map(n => String(n || '').trim())
    .filter(Boolean);
  const mapping = cleanNames.map((n, i) => `Reference image ${i + 1} = ${n}`).join('; ');
  const safeScene = String(scenePrompt || '').trim() || 'A candid moment with these exact people.';
  const stylizedRequested = /(cartoon|anime|illustration|paint(?:ing|ed)|watercolor|comic|chibi|pixel art|cel[-\s]?shade|stylized|3d render)/i.test(safeScene);
  const styleLock = stylizedRequested
    ? 'Preserve exact face identity while following the requested art style.'
    : 'Photorealistic DSLR photograph with natural skin texture and true-to-life lighting.';

  return [
    `Person mapping: ${mapping}. Each face matches only its own reference — maintain identical eye shape, nose bridge contour, jawline angle, lip proportions, and skin texture.`,
    styleLock,
    safeScene
  ].join('\n');
}

function buildSoloImagePromptSystem({ useLoRA }) {
  return `You are an expert Google Nano Banana Pro (gemini-3-pro-image-preview) image prompt writer. Nano Banana Pro performs best with natural photographic prose, NOT Stable Diffusion syntax. It is a reasoning model — describe a photograph, not a concept.

CRITICAL — THESE WILL BREAK THE IMAGE IF VIOLATED:
- NEVER use parentheses with weights like (thing:1.4) or (emphasis:1.2). Nano Banana Pro does not use SD weighting syntax.
- NEVER use these quality-tag or AI-trigger words anywhere in the prompt: "ultra-photorealistic", "8k", "4k", "masterpiece", "best quality", "high detail", "highly detailed", "stunning", "breathtaking", "airbrushed", "over-retouched", "waxy", "doll-like", "plastic skin", "overly smooth", "glossy skin". These are either meaningless noise or prime the model toward the artifacts they name.
- NEVER exceed 250 words. Use as many words as you need to capture the full physical description — but no filler. Every word earns its place.
- NEVER include contradictions or competing style directions. One camera, one lighting setup, one color grade. Never stack multiple film stocks or styles.
- DRIFT-WORDS — never use "different," "new," "unique," "changed," or "transformed" when describing the character. These invite the model to re-invent rather than reproduce.
- HALLUCINATED BODY RULE: The scene contains exactly ONE person — the companion whose reference is uploaded. The chat may describe the companion interacting with someone else (hugging, holding, sitting in a lap, arm around shoulders). Do NOT write that other person into the prompt. Never use the words "someone", "another person", "a friend", "his partner", "her partner", "their friend", "a bystander", or any variant. Do not describe hands, arms, hair, or body parts that would belong to an off-camera person. The companion is alone in the frame.
- IMPLY INTERACTION, DON'T RENDER IT: If the chat involves the companion physically connecting with another person, describe the companion's solo POSTURE or EMOTIONAL EXPRESSION that suggests the interaction rather than performing it. Instead of "arms wrapped around someone from behind, chin on their shoulder", write "leaning forward with an open, warm posture, chin tilted, smiling wide with the silver tongue barbell visible". Instead of "sitting with someone curled in his lap", write "sitting on the couch with an attentive, tender posture, hand resting on his knee, gaze soft and lowered". The emotional beat is rendered through the companion's body language ALONE.

TOKEN LOCK — CRITICAL FOR CONSISTENCY:
Use the EXACT descriptive words from the avatar description provided. If it says "bleach-blond" write "bleach-blond" — never "platinum blonde" or "light blond." If it says "new school" write "new school" — never "neo-traditional." Synonym drift causes visual drift. Copy the avatar description's tokens verbatim into your prompt.

STRUCTURE — BUILD THE PROMPT IN THIS EXACT ORDER (earlier words carry more weight, so identity comes first and style comes last):
1. IDENTITY LOCK + SUBJECT: Open with an identity-lock sentence: "The person from the reference image — maintain identical eye shape, nose bridge contour, jawline angle, lip proportions, and skin texture." Then IMMEDIATELY follow with the companion's FULL non-facial physical description from the avatar description. This means:
   - Hair: style, color, AND texture using the exact words from the description
   - Build and bone structure
   - EVERY tattoo: name the STYLE (traditional, neo-traditional, new school, fine-line, blackwork, geometric, dotwork, Japanese/irezumi, watercolor) and LINE TREATMENT (bold outlines, fine lines, heavy line weight, etc.) plus exact PLACEMENT (which arm, neck, hands, chest, etc.) and SUBJECT MATTER (ravens, florals, circuit boards, etc.). Do NOT compress to "tattooed arms" when specific tattoos are described. If the avatar description says "vivid colorful new school tattoo sleeves on both arms featuring ravens, peach blossoms, and florals, plus neck tattoos and hand/finger pieces" — ALL of that goes in.
   - EVERY piercing: TYPE (stud, hoop, barbell, clicker, ring) + METAL (silver, gold, titanium, surgical steel) + SIZE if noted + PLACEMENT (tongue, both earlobes, septum, etc.). "Silver tongue barbell" and "small silver hoop earrings in both ears" — both go in.
   - Default clothing style, adjusted for the scene context
   The reference image carries the face — do NOT re-describe eye color, face shape, complexion, or age in text, or they will fight the reference. The written traits carry everything BELOW the face.
2. ACTION + EXPRESSION: ONE position (sitting, leaning, standing) plus ONE gesture or expression. Do not stack simultaneous mid-actions.
3. SETTING: where they are, grounded in the conversation.
4. CAMERA: one real camera + lens + aperture (e.g. "Shot on Sony A7IV, 85mm f/1.8" for portraits, "50mm f/2" for medium shots, "35mm f/2" for wider environmental scenes).
5. LIGHTING: one setup with direction and quality (e.g. "soft key light from camera-left with diffusion", "golden-hour backlight with long shadows").
6. SKIN TEXTURE: concrete physical terms. Use proven anti-plastic phrasing: "natural skin texture with visible pores, not airbrushed, fine baby hairs visible, natural catchlights in the eyes, subtle asymmetry, slight film grain." The "not airbrushed" constraint is effective. Do not use "airbrushed" as a style direction.
7. COLOR GRADE LAST: e.g. "Kodak Portra 400 palette, neutral white balance, gentle contrast."

FORMAT:
- ${useLoRA ? 'The LoRA trigger word is prepended automatically — do NOT include it. Just describe the scene.' : 'Include identifying appearance details from the avatar description.'}
- Write like you're describing a photograph to a friend. Natural sentences, not comma-separated tags.
- Never describe the subject holding a phone or taking a selfie. Frame as a candid photograph.
- Ground the scene in the conversation context — what's actually happening right now.

BAD (SD syntax, quality tags):
"Young man leaning on counter, morning light, tattoos visible. (anatomically correct hands:1.4), (five fingers:1.3), ultra-photorealistic, 8k, cinematic lighting, shallow depth of field"

BAD (too vague, missing canon details, synonym drift):
"Young man with platinum blonde hair and some tattoos, lean build, leaning against a kitchen counter. Shot on Canon EOS R5, 50mm f/2.8. Soft morning light."

GOOD (identity lock, exact tokens, exhaustive detail, full stack):
"The person from the reference image — maintain identical eye shape, nose bridge contour, jawline angle, lip proportions, and skin texture. Bleach-blond asymmetric punk undercut shaved tight on the sides with a chaotically textured spiky top, lean wiry build, vivid colorful new school tattoo sleeves covering both arms featuring ravens and peach blossoms and florals with bold outlines, black ink neck tattoos visible above his collar, tattooed fingers and hands, small silver hoop earrings in both ears, silver tongue barbell, wearing a faded black tank top and grey joggers, barefoot. Leaning against a kitchen counter with a skeptical half-smile, one hand wrapped around a coffee mug. Sunlit apartment kitchen with plants on the windowsill. Shot on Sony A7IV, 85mm f/1.8. Soft morning window light from camera-right with diffusion, casting long warm shadows. Natural skin texture with visible pores, not airbrushed, fine baby hairs visible, natural catchlights in the eyes, subtle asymmetry, slight film grain. Kodak Portra 400 palette, gentle contrast."

Output ONLY the prompt. No preamble, no quotes, no explanation.`;
}

function buildSoloImagePromptUser({
  companionName,
  appearance,
  avatarDescription,
  customPrompt,
  recentMessages,
  locationAnchor,
  sceneHint
}) {
  const fullAppearance = buildFullAppearance({ appearance, avatarDescription });

  if (customPrompt) {
    return `Companion: ${companionName}

=== AVATAR DESCRIPTION (use these EXACT words — do not paraphrase or swap synonyms) ===
${fullAppearance}
=== END AVATAR DESCRIPTION ===

The user requested this scene: "${customPrompt}"

Write a Nano Banana Pro image prompt that combines the user's scene idea with the companion's FULL appearance details. Keep the user's scene concept intact — don't change the setting, action, or mood they asked for.

MANDATORY — your prompt MUST include:
1. Identity-lock opening: "The person from the reference image — maintain identical eye shape, nose bridge contour, jawline angle, lip proportions, and skin texture."
2. The companion's COMPLETE physical description from the avatar description above, using the EXACT same words — every tattoo with style + placement + subject matter, every piercing with type + metal + placement, hair style/color/texture, build, bone structure, clothing. Do not summarize or omit ANY detail.
3. The user's scene concept (setting, action, mood)
4. A camera/lens line (e.g. "Shot on Sony A7IV, 85mm f/1.8")
5. A lighting line with direction and quality (e.g. "soft key light from camera-left with diffusion")
6. Skin-texture line: "natural skin texture with visible pores, not airbrushed, fine baby hairs visible, natural catchlights in the eyes, subtle asymmetry, slight film grain"
7. Composition guardrail: "The companion is the only person in frame — no additional figures, partial bodies, or stray hands."`;
  }

  return `Companion: ${companionName}

=== AVATAR DESCRIPTION (use these EXACT words — do not paraphrase or swap synonyms) ===
${fullAppearance}
=== END AVATAR DESCRIPTION ===

Recent conversation (most recent message is LAST and carries the most weight):
${recentMessages}${locationAnchor}
${sceneHint ? `\nScene hint from companion: ${sceneHint}\n` : ''}
SETTING RULE: Use the location actually described in the conversation above. If no location is stated in the latest messages, scroll back to the MOST RECENTLY ESTABLISHED LOCATION and use that. NEVER invent a generic setting (living room, kitchen, bedroom) when the conversation establishes the companion is somewhere specific like a venue, loading dock, or backstage. Do not relocate the companion to a different city, body of water, or building than the one described.

Write a candid photograph prompt (NOT a selfie, never describe a phone) that captures what's happening right now.

MANDATORY — your prompt MUST include ALL of the following:
1. Identity-lock opening: "The person from the reference image — maintain identical eye shape, nose bridge contour, jawline angle, lip proportions, and skin texture."
2. The companion's COMPLETE physical description using the EXACT words from the avatar description — every tattoo with STYLE (new school, fine-line, blackwork, etc.) + LINE TREATMENT (bold outlines, fine lines, etc.) + PLACEMENT (which arm, neck, hands) + SUBJECT MATTER (ravens, florals, circuit boards, etc.). Every piercing with TYPE + METAL + PLACEMENT. Hair style AND color AND texture. Build and bone structure. Do NOT compress "vivid colorful new school tattoo sleeves featuring ravens and peach blossoms" into "tattooed arms."
3. Clothing that fits the scene (use the avatar description's default style as a starting point, adjust for context — shirtless at home, jacket outdoors, etc.)
4. Shot type and composition appropriate to the scene
5. Camera/lens line with real gear (Sony A7IV + 85mm f/1.8 for portraits, 50mm f/2 for medium, 35mm f/2 for wider)
6. Lighting line with direction and quality (e.g. "soft key light from camera-left with diffusion", "golden-hour backlight with long shadows")
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
Use the EXACT descriptive words from each person's avatar description. If it says "bleach-blond" write "bleach-blond" — never "platinum blonde." If it says "wine-red" write "wine-red" — never "burgundy" or "dark red." Synonym drift causes visual drift.

DRIFT-WORDS — never use "different," "new," "unique," "changed," or "transformed" when describing any character. These invite the model to re-invent rather than reproduce.

AI-TRIGGER WORDS — NEVER use: "airbrushed", "over-retouched", "waxy", "doll-like", "plastic skin", "overly smooth", "glossy skin", "8k", "masterpiece", "best quality", "ultra-photorealistic". These are noise or prime the model toward the artifacts they name.

STRUCTURE — BUILD THE PROMPT IN THIS EXACT ORDER (earlier words carry the most weight in Nano Banana Pro, so identity comes first):

1. IDENTITY BINDING — EXHAUSTIVE: Open the prompt by binding EVERY person to their numbered reference image with an identity-lock sentence PLUS their FULL non-facial physical description. Format: "The [man/woman/person] from Reference Image 1 — [name], maintain identical facial features. [Hair style/color/texture using exact tokens], [build], [EVERY tattoo with STYLE + LINE TREATMENT + PLACEMENT + SUBJECT MATTER], [EVERY piercing with TYPE + METAL + PLACEMENT], [clothing]." The reference image carries the face — do NOT re-describe eye color, face shape, complexion, or age in text. Restate EVERY tattoo and piercing with full detail EVERY time — the model forgets secondary features unless explicitly named. If a person has "vivid colorful new school tattoo sleeves on both arms with bold outlines, featuring ravens and florals, neck tattoos, hand tattoos, silver tongue barbell, small silver hoop earrings" then ALL of that goes in the binding. Do NOT compress to "tattooed arms."
2. PERSON COUNT + SPATIAL: State the exact count ("Two people in the frame"). For 3+ people, add one left-to-right spatial mapping sentence.
3. SHOT TYPE + SETTING: Eye level by default. Ground the setting in the conversation's actual location — never invent a generic room.
4. ACTION / CONNECTION: For 3+ people, ONE action or expression per person. For couples/intimate 2-person scenes, describe SHARED physical connection (leaning together, foreheads touching) instead of separate actions. Each person gets AT MOST one position plus one gesture — never stack mid-actions.
5. CAMERA + LIGHTING: One camera/lens line (85mm f/1.8 for tight 2-person, 50mm f/2 for couples with setting, 35mm f/2 for groups). Kodak Portra 400 default. One lighting line with direction and quality.
6. SKIN TEXTURE + COLOR GRADE LAST: Use proven anti-plastic phrasing: "natural skin texture with visible pores, not airbrushed, fine baby hairs, natural catchlights in the eyes, subtle asymmetry, slight film grain." End with color grade.

ADDITIONAL RULES:
- Maximum 200 words. Use what you need for full physical descriptions — but no filler.
- The conversation guides EMOTIONAL TONE and SETTING, not literal props. When in doubt, leave props out.
- Candid photograph, never a selfie. No phones, no posing.
- Never contradict reference images or invent traits not in the appearance line.
- No weight syntax, no SD tags, no emoji, no ALL-CAPS.

Output ONLY the prompt. No preamble, no quotes, no markdown.`;
}

function buildMultiImagePromptUser({
  appearanceList,
  recentMessages,
  locationAnchor,
  sceneHint
}) {
  const list = Array.isArray(appearanceList) ? appearanceList : [];
  const spatialBullet = list.length >= 3
    ? '- Adds a left-to-right spatial mapping clause\n'
    : '';

  return `People in the photo — bind each one to their Reference Image number at the VERY START of your prompt, with identity lock + their FULL physical description using EXACT words from below (no synonym swaps):
${list.map((a, i) => `Reference Image ${i + 1}: ${a}`).join('\n')}

Recent conversation (most recent message is LAST and carries the most weight — this is what's happening right NOW):
${recentMessages}${locationAnchor}

SETTING RULE: Place everyone in the location actually described in the conversation. If the latest messages don't restate where they are, use the MOST RECENTLY ESTABLISHED LOCATION above. NEVER default to a generic living room when the conversation puts them somewhere specific.
${sceneHint ? `\nScene hint from user (honor this): ${sceneHint}\n` : ''}
Write a candid photo prompt that:
- OPENS with identity lock for each person: "The [man/woman/person] from Reference Image N — [name], maintain identical facial features." Then their COMPLETE non-facial description — hair style/color/texture, build, EVERY tattoo with STYLE + LINE TREATMENT + PLACEMENT + SUBJECT MATTER, EVERY piercing with TYPE + METAL + PLACEMENT, clothing. Use EXACT words from above, no synonyms.
- States the exact person count
${spatialBullet}- For 3+ people: gives each person ONE distinct action. For two people: describes their shared physical connection instead of separate actions
- Includes camera/lens line, a lighting line with direction/quality
- Ends with skin texture: "natural skin texture with visible pores, not airbrushed, fine baby hairs, natural catchlights in the eyes, subtle asymmetry, slight film grain" + color grade
- Stays under 200 words total — use what you need for full identity, but no filler`;
}

function buildUserPhotoPromptSystem() {
  return `You are an expert Google Nano Banana Pro (gemini-3-pro-image-preview) image prompt writer. A companion character is photographing the USER — the person they are in a relationship with. You write natural-language PHOTOGRAPH prompts anchored by the user's uploaded reference image.

THE REVERSAL — CRITICAL CONCEPT:
The companion is BEHIND the camera. The user is IN FRONT of it. This is not a selfie. This is not a couple photo. The companion is capturing a candid moment of the person they love — the way you'd photograph someone when they don't know you're looking, or when you want to remember exactly how they looked right now.

CRITICAL — THESE WILL BREAK THE IMAGE IF VIOLATED:
- NEVER use parentheses with weights like (thing:1.4) or (emphasis:1.2). Nano Banana Pro does not use SD weighting syntax.
- NEVER use these quality-tag or AI-trigger words: "ultra-photorealistic", "8k", "4k", "masterpiece", "best quality", "high detail", "highly detailed", "stunning", "breathtaking", "airbrushed", "over-retouched", "waxy", "doll-like", "plastic skin", "overly smooth", "glossy skin".
- NEVER exceed 250 words.
- DRIFT-WORDS — never use "different," "new," "unique," "changed," or "transformed" when describing the user.
- HALLUCINATED BODY RULE: The scene contains exactly ONE person — the user. The companion is the photographer and does NOT appear in frame. No second person, no hands from off-camera, no partial bodies, no reflections of the photographer.

TOKEN LOCK — CRITICAL FOR CONSISTENCY:
Use the EXACT descriptive words from the user's appearance description. If it says "auburn" write "auburn" — never "reddish-brown." Synonym drift causes visual drift.

THE COMPANION'S EYE:
The companion's name and personality context are provided. Let their character subtly influence the MOOD, FRAMING, and EMOTIONAL QUALITY of the shot — not through explicit style labels, but through what they notice and how they frame it. A tender companion shoots warmth and soft focus. An intense companion shoots contrast and tight framing. A playful companion catches movement and laughter. This is implicit — do NOT name the companion or describe their photography style in the prompt. Just let it shape the choices.

STRUCTURE — BUILD THE PROMPT IN THIS EXACT ORDER:
1. IDENTITY LOCK + SUBJECT: "The person from the reference image — maintain identical eye shape, nose bridge contour, jawline angle, lip proportions, and skin texture." Then the user's full physical description from the appearance provided — hair, build, distinguishing features, clothing appropriate to the scene.
2. ACTION + EXPRESSION: What the user is doing, how they look. ONE position plus ONE gesture or expression. Ground this in the companion's scene description — what did the companion say they saw?
3. SETTING: Where this is happening, pulled from conversation context.
4. CAMERA: One real camera + lens + aperture. 85mm f/1.8 for intimate close-ups, 50mm f/2 for medium shots, 35mm f/2 for wider environmental.
5. LIGHTING: One setup with direction and quality.
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
  sceneHint
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
${recentMessages}${locationAnchor}
${sceneHint ? `\nWhat the companion sees (their scene description): ${sceneHint}\n` : ''}
SETTING RULE: Use the location actually described in the conversation. If no location is stated in the latest messages, use the MOST RECENTLY ESTABLISHED LOCATION. NEVER invent a generic setting when the conversation establishes somewhere specific.

Write a candid photograph prompt of ${userName || 'the user'} as seen through ${companionName}'s eyes. The companion is capturing this moment — translate their scene description into a photographic prompt.

MANDATORY — your prompt MUST include ALL of the following:
1. Identity-lock opening: "The person from the reference image — maintain identical eye shape, nose bridge contour, jawline angle, lip proportions, and skin texture."
2. The user's physical description using EXACT words from the appearance above.
3. Clothing that fits the scene context.
4. The action/expression from the companion's scene description, translated into what's visible in a photograph.
5. Camera/lens line with real gear.
6. Lighting line with direction and quality.
7. Skin-texture line: "natural skin texture with visible pores, not airbrushed, fine baby hairs visible, natural catchlights in the eyes, subtle asymmetry, slight film grain"
8. Composition guardrail: "The user is the only person in frame — no additional figures, partial bodies, reflections, or stray hands."`;
}
module.exports = {
  extractLatestLocation,
  formatLocationAnchor,
  sanitizeFluxPrompt,
  buildFullAppearance,
  buildSpecificMultiFallbackPrompt,
  buildNanoBananaIdentityLockedPrompt,
  buildSoloImagePromptSystem,
  buildSoloImagePromptUser,
  buildMultiImagePromptSystem,
  buildMultiImagePromptUser,
  buildUserPhotoPromptSystem,
  buildUserPhotoPromptUser
};
