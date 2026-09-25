// © 2024-2026 Megan Neves and Tiara Young. All rights reserved.
// Love Refactored — https://github.com/Love-Encoded/Love-Refactored
// Licensed under the Love Refactored / Tanevan License. See LICENSE.md.

'use strict';

const express = require('express');
const multer = require('multer');

/**
 * @param {import('express').Application} app
 * @param {object} deps
 */
function registerCompanionRoutes(app, deps) {
  const {
    fs,
    path,
    COMPANION_DIR,
    HISTORY_DIR,
    AVATAR_DIR,
    GALLERY_DIR,
    getSettings,
    getPersona,
    addLog,
    updateLog,
    fetchJsonLogged,
    getChatHistory,
    saveChatHistory,
    startNewChatLogSession,
    historyCacheInvalidate,
    getCompanion,
    saveCompanion,
    getCompanionSeeds,
    saveCompanionSeeds,
    getCompanionOrder,
    saveCompanionOrder,
    purgeCompanionData,
    resetCompanionData,
    getEmotionalProfile,
    saveEmotionalProfile,
    getMoodState,
    saveMoodState,
    callLLM,
    getCompanionSettings,
    getEmotionalModelSettings,
    getTanevanBaseUrl,
    avatarUpload,
    getGalleryMeta,
    addGalleryMetaEntry,
    saveGalleryMeta,
    normalizeTags,
    galleryUpload,
    resolveTanevanCompanionKey,
    bgSchedule,
    getBackgroundScheduleSettings,
    loadReflectionScheduleState,
    DATA_DIR,
    getScheduledCompanionNames
  } = deps;

  const tanevanCompanion = typeof resolveTanevanCompanionKey === 'function'
    ? (raw) => resolveTanevanCompanionKey(raw)
    : (raw) => String(raw || '').trim().toLowerCase();

  const chatImportStorage = multer.memoryStorage();
  const chatImportUpload = multer({
    storage: chatImportStorage,
    limits: { fileSize: 50 * 1024 * 1024 },
    fileFilter: (req, file, cb) => {
      if (file.mimetype === 'application/json' || file.originalname.endsWith('.json') || file.originalname.endsWith('.jsonl')) {
        cb(null, true);
      } else {
        cb(new Error('Only JSON files are supported'));
      }
    }
  });

  function hasTanevanConfigured(settings = getSettings()) {
    const envUrl = (process.env.TANEVAN_URL || '').trim();
    const configUrl = (settings.memory?.tanevUrl || '').trim();
    return Boolean(envUrl || configUrl);
  }

  function resolveTanevanBaseUrl(settings = getSettings()) {
    if (typeof getTanevanBaseUrl === 'function') {
      const resolved = String(getTanevanBaseUrl(settings) || '').trim();
      if (resolved) return resolved.replace(/\/$/, '');
    }
    const envUrl = (process.env.TANEVAN_URL || '').trim();
    const configUrl = (settings.memory?.tanevUrl || '').trim();
    return (envUrl || configUrl || 'http://127.0.0.1:5001').replace(/\/$/, '');
  }

  async function tanevFetch(urlPath, options = {}) {
    const settings = getSettings();
    const tanevUrl = resolveTanevanBaseUrl(settings);
    return fetch(`${tanevUrl}${urlPath}`, options);
  }

  const MEMORY_TAGS_FILE = path.join(HISTORY_DIR, '_memory_tags.json');

  function normalizeMemoryTagList(tags) {
    const out = [];
    const seen = new Set();
    for (const raw of (Array.isArray(tags) ? tags : [])) {
      const name = String(raw ?? '').trim().toLowerCase().replace(/\s+/g, '-').replace(/[^a-z0-9-_]/g, '').slice(0, 32);
      if (!name || seen.has(name)) continue;
      seen.add(name);
      out.push(name);
    }
    return out;
  }

  function readMemoryTagsStore() {
    try {
      if (!fs.existsSync(MEMORY_TAGS_FILE)) return {};
      const raw = fs.readFileSync(MEMORY_TAGS_FILE, 'utf-8');
      const parsed = JSON.parse(raw);
      return parsed && typeof parsed === 'object' ? parsed : {};
    } catch (_) {
      return {};
    }
  }

  function writeMemoryTagsStore(store) {
    try {
      fs.writeFileSync(MEMORY_TAGS_FILE, JSON.stringify(store, null, 2));
    } catch (_) { /* best effort */ }
  }

  function getCompanionTagMap(companionName) {
    const store = readMemoryTagsStore();
    const key = String(companionName || '').toLowerCase();
    const bucket = store[key];
    return bucket && typeof bucket === 'object' ? bucket : {};
  }

  function setCompanionMemoryTags(companionName, memoryId, tags) {
    const store = readMemoryTagsStore();
    const key = String(companionName || '').toLowerCase();
    if (!store[key] || typeof store[key] !== 'object') store[key] = {};
    const safeTags = normalizeMemoryTagList(tags);
    if (!safeTags.length) delete store[key][memoryId];
    else store[key][memoryId] = safeTags;
    if (Object.keys(store[key]).length === 0) delete store[key];
    writeMemoryTagsStore(store);
    return safeTags;
  }

  function mergeMemoryTags(memory, tagMap) {
    if (!memory || typeof memory !== 'object') return memory;
    const memoryId = memory.id != null ? String(memory.id) : '';
    if (!memoryId) return memory;
    const mapped = normalizeMemoryTagList(tagMap[memoryId]);
    if (!mapped.length) return memory;
    return { ...memory, tags: mapped };
  }

  // Flush Tanevan buffer then wipe chat history
  app.post('/api/companions/:name/clear-chat', async (req, res) => {
    const name = req.params.name;
    const skipFlush = req.body?.skipFlush === true;
    const settings = getSettings();
    let flushed = false;
    let memoriesExtracted = null;

    if (!skipFlush && hasTanevanConfigured(settings)) {
      const flushLog = addLog({ type: 'tanevan-flush', companion: name, direction: 'outbound', summary: `Tanevan flush → ${name}`, status: 'pending' });
      const flushT0 = Date.now();
      try {
        const tanevUrl = resolveTanevanBaseUrl(settings);
        const personaFlush = getPersona();
        const flushUserName = personaFlush && personaFlush.name && String(personaFlush.name).trim();
        const flushBody = { companion: name.toLowerCase(), manual: true };
        if (flushUserName) flushBody.user_name = flushUserName;
        const flushRes = await fetch(`${tanevUrl}/flush`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(flushBody)
        });
        if (flushRes.ok) {
          flushed = true;
          const flushData = await flushRes.json();
          memoriesExtracted = flushData.memories_extracted ?? flushData.extracted ?? null;
          updateLog(flushLog.id, { direction: 'inbound', status: 'success', duration: Date.now() - flushT0, details: memoriesExtracted !== null ? `${memoriesExtracted} memories extracted` : 'Flushed' });
        } else {
          updateLog(flushLog.id, { direction: 'inbound', status: 'error', duration: Date.now() - flushT0, details: 'Non-OK response from Tanevan' });
        }
      } catch (err) {
        updateLog(flushLog.id, { direction: 'inbound', status: 'error', duration: Date.now() - flushT0, details: err.message });
        console.log(`Tanevan flush skipped for ${name}:`, err.message);
      }
    }

    startNewChatLogSession(name);
    saveChatHistory(name, [], { force: true });
    res.json({ success: true, flushed, memoriesExtracted });
  });

  // Trim companion chat to the last N messages (requires successful Tanevan flush)
  app.post('/api/companions/:name/trim-chat', async (req, res) => {
    const name = req.params.name;
    const skipFlush = req.body?.skipFlush === true;
    if (skipFlush) {
      return res.status(400).json({
        error: 'Just Trim is disabled. Trim requires a successful Tanevan flush.'
      });
    }
    let keep = parseInt(req.body?.keep, 10);
    if (!Number.isFinite(keep) || keep < 1) keep = 20;
    if (keep > 50000) keep = 50000;

    const messages = getChatHistory(name);
    const total = messages.length;
    if (total <= keep) {
      return res.json({
        success: true,
        noOp: true,
        messageCount: total,
        keep
      });
    }

    const settings = getSettings();
    let flushed = false;
    let memoriesExtracted = null;
    let summaryId = null;

    if (!hasTanevanConfigured(settings)) {
      return res.status(400).json({
        error: 'Trim requires Tanevan to be configured and reachable.'
      });
    }

    const flushLog = addLog({ type: 'tanevan-flush', companion: name, direction: 'outbound', summary: `Tanevan flush (trim) → ${name}`, status: 'pending' });
    const flushT0 = Date.now();
    try {
      const tanevUrl = resolveTanevanBaseUrl(settings);
      const personaFlush = getPersona();
      const flushUserName = personaFlush && personaFlush.name && String(personaFlush.name).trim();
      const flushBody = { companion: name.toLowerCase(), manual: true };
      if (flushUserName) flushBody.user_name = flushUserName;
      const flushRes = await fetch(`${tanevUrl}/flush`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(flushBody)
      });
      let flushData = null;
      try {
        flushData = await flushRes.json();
      } catch {
        flushData = null;
      }

      if (!flushRes.ok) {
        updateLog(flushLog.id, { direction: 'inbound', status: 'error', duration: Date.now() - flushT0, details: 'Non-OK response from Tanevan' });
        return res.status(502).json({ error: 'Save & Trim failed: Tanevan flush returned an error. Trim was not applied.' });
      }

      const status = String(flushData?.status || '').toLowerCase();
      memoriesExtracted = flushData?.memories_extracted ?? flushData?.extracted ?? null;
      summaryId = flushData?.summary_id || null;

      // Save & Trim should only proceed when Tanevan actually writes a summary.
      if (status !== 'processed' || !summaryId) {
        const detail = status === 'nothing to process'
          ? 'No pending buffered messages were available to summarize'
          : (flushData?.error || flushData?.message || 'No summary_id returned by Tanevan');
        updateLog(flushLog.id, { direction: 'inbound', status: 'error', duration: Date.now() - flushT0, details: detail });
        return res.status(409).json({
          error: `Save & Trim aborted: ${detail}. Trim was not applied.`,
          status: flushData?.status || null,
          memoriesExtracted,
          summaryId
        });
      }

      flushed = true;
      updateLog(
        flushLog.id,
        {
          direction: 'inbound',
          status: 'success',
          duration: Date.now() - flushT0,
          details: `${memoriesExtracted !== null ? `${memoriesExtracted} memories extracted` : 'Flushed'} · summary ${summaryId}`
        }
      );
    } catch (err) {
      updateLog(flushLog.id, { direction: 'inbound', status: 'error', duration: Date.now() - flushT0, details: err.message });
      console.log(`Tanevan flush (trim) skipped for ${name}:`, err.message);
      return res.status(502).json({ error: `Save & Trim failed: ${err.message}. Trim was not applied.` });
    }

    const trimmed = messages.slice(-keep);
    saveChatHistory(name, trimmed, { force: true, bulkTrim: true });
    res.json({
      success: true,
      flushed,
      memoriesExtracted,
      summaryId,
      messageCount: trimmed.length,
      removed: total - trimmed.length,
      keep
    });
  });

  // List all companions (auto-creates the 5 defaults if their files don't exist yet)
  app.get('/api/companions', (req, res) => {
    for (const seed of getCompanionSeeds()) {
      const safeName = seed.name.toLowerCase().replace(/[^a-z0-9]/g, '_');
      if (!fs.existsSync(path.join(COMPANION_DIR, `${safeName}.json`))) {
        saveCompanion(seed.name, {
          name: seed.name, backstory: '', boundaries: '',
          personalityVoice: '', voiceAnchor: '', appearance: '',
          exampleMessages: '', avatar: seed.avatar,
          loraPath: seed.loraPath || '', referenceImage: seed.referenceImage || ''
        });
      }
    }
    const files = fs.readdirSync(COMPANION_DIR).filter(f => f.endsWith('.json'));
    const list = files.map(f => JSON.parse(fs.readFileSync(path.join(COMPANION_DIR, f), 'utf-8')));

    // Apply companion_order.json, auto-appending new companions and pruning deleted ones
    const { order, pinned } = getCompanionOrder();
    const existingNames = new Set(list.map(c => c.name.toLowerCase()));
    const validOrder = order.filter(n => existingNames.has(n.toLowerCase()));
    const unordered = list.filter(c => !validOrder.some(n => n.toLowerCase() === c.name.toLowerCase()));
    const finalOrder = [...validOrder, ...unordered.map(c => c.name)];

    // If order changed (new/removed companions), persist the cleaned-up order
    if (finalOrder.length !== order.length || finalOrder.some((n, i) => (order[i] || '').toLowerCase() !== n.toLowerCase())) {
      const validPinned = pinned.filter(n => existingNames.has(n.toLowerCase()));
      saveCompanionOrder({ order: finalOrder, pinned: validPinned });
    }

    const nameToComp = Object.fromEntries(list.map(c => [c.name.toLowerCase(), c]));
    res.json(finalOrder.map(n => nameToComp[n.toLowerCase()]).filter(Boolean));
  });

  app.get('/api/companion-order', (req, res) => {
    res.json(getCompanionOrder());
  });

  app.put('/api/companion-order', (req, res) => {
    const { order, pinned } = req.body;
    if (!Array.isArray(order) || !Array.isArray(pinned)) {
      return res.status(400).json({ error: 'order and pinned must be arrays' });
    }
    saveCompanionOrder({ order, pinned });
    res.json({ success: true });
  });

  // Create a new companion
  app.post('/api/companions', (req, res) => {
    const name = (req.body.name || '').trim();
    if (!name) return res.status(400).json({ error: 'Name is required' });
    const safeName = name.toLowerCase().replace(/[^a-z0-9]/g, '_');
    if (fs.existsSync(path.join(COMPANION_DIR, `${safeName}.json`))) {
      return res.status(409).json({ error: 'A companion with that name already exists' });
    }
    saveCompanion(name, {
      name,
      backstory:        req.body.backstory        || '',
      boundaries:       req.body.boundaries       || '',
      personalityVoice: req.body.personalityVoice || '',
      birthday:         req.body.birthday         || '',
      zodiac:           req.body.zodiac           || '',
      mbti:             req.body.mbti             || '',
      enneagram:        req.body.enneagram        || '',
      archetypes:       req.body.archetypes       || '',
      emotionalEngine:  req.body.emotionalEngine  || '',
      values:           req.body.values           || '',
      decisionMaking:   req.body.decisionMaking   || '',
      dailyRhythms:     req.body.dailyRhythms     || '',
      tellsTics:        req.body.tellsTics        || '',
      cameraEye:        req.body.cameraEye        || '',
      responseDirective: req.body.responseDirective || '',
      userPersonaOverride: req.body.userPersonaOverride || '',
      voiceAnchor: req.body.voiceAnchor || '',
      speechPatterns: req.body.speechPatterns || '',
      groupChatProfileOnly: req.body.groupChatProfileOnly === false
        ? false
        : !!String(req.body.voiceAnchor || '').trim(),
      appearance:       req.body.appearance       || '',
      exampleMessages:  req.body.exampleMessages  || '',
      avatar:           req.body.avatar           || '',
      loraTrigger:      req.body.loraTrigger      || '',
      loraPath:         req.body.loraPath         || '',
      referenceImage:   req.body.referenceImage   || '',
      voiceId:          req.body.voiceId          || '',
      agentId:          req.body.agentId          || '',
      anamAvatarId:     req.body.anamAvatarId     || '',
      provider:         req.body.provider         || '',
      providerModel:    req.body.providerModel     || '',
      providerApiKey:   req.body.providerApiKey   || '',
      providerUrl:      req.body.providerUrl      || '',
      calendarColor:    req.body.calendarColor      || '',
      proactiveEnabled:    req.body.proactiveEnabled    || false,
      proactiveNoLimits:   req.body.proactiveNoLimits   || false,
      proactiveFrequency:  req.body.proactiveFrequency  || 'moderate',
      proactiveMinMinutes: req.body.proactiveMinMinutes  || 120,
      proactiveQuietEnabled: req.body.proactiveQuietEnabled !== undefined ? req.body.proactiveQuietEnabled : true,
      proactiveQuietStart: req.body.proactiveQuietStart  || '00:00',
      proactiveQuietEnd:   req.body.proactiveQuietEnd    || '08:00',
      proactiveDirective:  req.body.proactiveDirective   || '',
      proactiveDecisionModelMode: req.body.proactiveDecisionModelMode || 'global',
      proactiveDecisionProvider:  req.body.proactiveDecisionProvider  || '',
      proactiveDecisionModel:     req.body.proactiveDecisionModel     || '',
      proactiveModelMode:  req.body.proactiveModelMode   || 'chat',
      proactiveProvider:   req.body.proactiveProvider    || '',
      proactiveModel:      req.body.proactiveModel       || '',
      voiceMemoProvider:   req.body.voiceMemoProvider    || '',
      voiceCallProvider:   req.body.voiceCallProvider    || '',
      voiceReferenceClip:  req.body.voiceReferenceClip   || '',
      voiceCallDirective:  req.body.voiceCallDirective   || '',
      useCustomSystemPrompt: req.body.useCustomSystemPrompt === true,
      systemPromptOverride: String(req.body.systemPromptOverride || ''),
      elevenLabsVoiceId:   req.body.elevenLabsVoiceId    || '',
      ...(function () {
        const o = {};
        const mt = parseInt(req.body.maxTokens, 10);
        const ct = parseInt(req.body.creativeStudioMaxTokens, 10);
        const budget = parseInt(req.body.memoryTokenBudget, 10);
        const inject = parseInt(req.body.memoryInjectCount, 10);
        if (Number.isFinite(mt) && mt > 0) o.maxTokens = mt;
        if (Number.isFinite(ct) && ct > 0) o.creativeStudioMaxTokens = ct;
        if (Number.isFinite(budget) && budget >= 0) o.memoryTokenBudget = budget;
        if (Number.isFinite(inject) && inject > 0) o.memoryInjectCount = inject;
        return o;
      })()
    });
    // Auto-add to companion_seeds.json
    const seeds = getCompanionSeeds();
    if (!seeds.some(s => s.name.toLowerCase() === name.toLowerCase())) {
      seeds.push({ name, avatar: req.body.avatar || '' });
      saveCompanionSeeds(seeds);
    }
    res.json(getCompanion(name));
  });

  // Delete a companion and all their data
  app.delete('/api/companions/:name', async (req, res) => {
    try {
      await purgeCompanionData(req.params.name);
      res.json({ success: true });
    } catch (err) {
      console.error(`Failed to delete companion ${req.params.name}:`, err);
      res.status(500).json({ error: err.message || 'Failed to delete companion' });
    }
  });

  // Factory-reset a companion — clears relational data and character definition; keeps settings, avatar, gallery
  app.post('/api/companions/:name/reset', async (req, res) => {
    try {
      await resetCompanionData(req.params.name);
      res.json({ success: true });
    } catch (err) {
      console.error(`Failed to reset companion ${req.params.name}:`, err);
      const status = err.message === 'Companion not found' ? 404 : 500;
      res.status(status).json({ error: err.message || 'Failed to reset companion' });
    }
  });

  // Get a companion's character card
  app.get('/api/companions/:name', (req, res) => {
    res.json(getCompanion(req.params.name));
  });

  // Save/update a companion's character card
  app.put('/api/companions/:name', (req, res) => {
    const current = getCompanion(req.params.name);
    const allowed = ['backstory', 'boundaries', 'personalityVoice', 'birthday', 'zodiac', 'mbti', 'enneagram', 'archetypes', 'emotionalEngine', 'values', 'decisionMaking', 'dailyRhythms', 'tellsTics', 'cameraEye', 'appearance', 'exampleMessages', 'avatar', 'loraTrigger', 'loraPath', 'referenceImage', 'voiceId', 'agentId', 'anamAvatarId', 'provider', 'providerModel', 'providerApiKey', 'providerUrl', 'voiceAnchor', 'speechPatterns', 'groupChatProfileOnly', 'contextMessageCount', 'memoryInjectCount', 'memoryTokenBudget', 'calendarColor', 'proactiveEnabled', 'proactiveNoLimits', 'proactiveFrequency', 'proactiveMinMinutes', 'proactiveMaxUnanswered', 'proactiveProofOfLife', 'proactiveSilenceCheckIn', 'proactiveTelegram', 'proactiveQuietEnabled', 'proactiveQuietStart', 'proactiveQuietEnd', 'proactiveDirective', 'proactiveStyle', 'proactiveThreshold', 'proactiveDecisionModelMode', 'proactiveDecisionProvider', 'proactiveDecisionModel', 'proactiveModelMode', 'proactiveProvider', 'proactiveModel', 'elapsedTimeEnabled', 'falLoraUrl', 'falReferenceImage', 'imageGenMethod', 'temperature', 'responseDirective', 'voiceCallDirective', 'photoEnabled', 'photoDailyLimit', 'wallEnabled', 'reflectionsEnabled', 'reflectionHorizons', 'simliFaceId', 'extendedReasoning', 'useCustomSystemPrompt', 'systemPromptOverride', 'userPersonaOverride', 'loraScale', 'loraId', 'customIncludeDatetime', 'customIncludeLastSeen', 'customIncludeMemories', 'customIncludeEmotional', 'customIncludeLorebook', 'customIncludeJournal', 'customIncludeCalendar', 'customIncludeResponseDir', 'customIncludeTools', 'maxTokens', 'creativeStudioMaxTokens', 'voiceMemoProvider', 'voiceCallProvider', 'voiceReferenceClip', 'elevenLabsVoiceId', 'openrouterRouting'];
    for (const key of allowed) {
      if (req.body[key] !== undefined) current[key] = req.body[key];
    }
    if (req.body.moodEvalFrequency !== undefined) {
      if (req.body.moodEvalFrequency === null) delete current.moodEvalFrequency;
      else current.moodEvalFrequency = req.body.moodEvalFrequency;
    }
    saveCompanion(req.params.name, current);
    res.json(current);
  });

  // === EMOTIONAL PROFILE SYSTEM ===

  // Get a companion's emotional profile
  app.get('/api/companions/:name/emotional-profile', (req, res) => {
    const profile = getEmotionalProfile(req.params.name);
    if (!profile) return res.json({ exists: false, companion: req.params.name });
    // Always tie to URL name so clients (and legacy JSON files missing `companion`) resolve mood APIs correctly
    res.json({ exists: true, ...profile, companion: req.params.name });
  });

  // Save/update a companion's emotional profile (manual edits)
  app.put('/api/companions/:name/emotional-profile', (req, res) => {
    const existing = getEmotionalProfile(req.params.name) || {};
    const updated = {
      ...existing,
      ...req.body,
      companion: req.params.name,
      lastEdited: new Date().toISOString()
    };
    saveEmotionalProfile(req.params.name, updated);
    res.json(updated);
  });

  // Analyze a companion's character card to generate emotional profile (uses that companion's LLM provider)
  app.post('/api/companions/:name/analyze-emotions', async (req, res) => {
    const name = req.params.name;
    const card = getCompanion(name);
    const settings = getSettings();
    const companionSettings = getCompanionSettings(name, settings);
    const analysisSettings = typeof getEmotionalModelSettings === 'function'
      ? getEmotionalModelSettings(settings.emotionalAnalysis, companionSettings, settings)
      : companionSettings;

    // Build the character context from the card
    let characterContext = `Character name: ${name}\n`;
    if (card.backstory) characterContext += `\nBackstory:\n${card.backstory}\n`;
    if (card.personalityVoice) characterContext += `\nPersonality & Voice:\n${card.personalityVoice}\n`;
    if (card.boundaries) characterContext += `\nBoundaries:\n${card.boundaries}\n`;
    if (card.exampleMessages) characterContext += `\nExample Messages:\n${card.exampleMessages}\n`;
    const customPrompt = String(card.systemPromptOverride || '').trim();
    if (customPrompt) characterContext += `\nCustom system prompt:\n${customPrompt}\n`;

    const analysisPrompt = `You are an expert character psychologist. Read this character card carefully and extract a deep emotional profile. This profile will be used to give the character emotional continuity — the ability to carry feelings across conversations instead of resetting to neutral every time.

Do NOT invent things that aren't supported by the card. Only infer from what's actually written. If the card is sparse, say so — don't fill gaps with assumptions.

${characterContext}

Respond with ONLY valid JSON (no markdown, no backticks, no preamble) in this exact structure:

{
  "summary": "2-3 sentence emotional overview of this character — their core emotional texture, what drives them, how they move through the world",
  "emotionalBaseline": {
    "warmth": 7,
    "trust": 6,
    "patience": 7,
    "engagement": 8,
    "notes": "Brief explanation of why these baseline values fit this character"
  },
  "triggers": [
    {
      "type": "trigger category (e.g. dismissal, abandonment, dishonesty, boundary_violation, neglect, pressure, vulnerability_rejected)",
      "description": "What specifically triggers this emotional response",
      "severity": "low | moderate | high",
      "example": "A concrete example of something a user might say or do that would trigger this"
    }
  ],
  "conflictStyle": {
    "primary": "How they handle conflict (e.g. withdrawal, confrontation, deflection, intellectualization, silence, sarcasm, people-pleasing)",
    "description": "Detailed description of how they behave when hurt or in conflict — what changes in their voice, their behavior, their engagement",
    "escalation": "What happens if the conflict isn't resolved — how do they escalate or shut down"
  },
  "repairNeeds": [
    {
      "need": "What they need to heal (e.g. acknowledgment, consistency, vulnerability, space, acts_of_care, direct_conversation)",
      "description": "How this repair need manifests — what does it look like when someone gives them what they need",
      "priority": "primary | secondary"
    }
  ],
  "joyExpression": {
    "description": "How this character expresses happiness, excitement, love, delight — what changes in their voice and behavior when they're genuinely happy",
    "sustainsDays": "How long positive emotions tend to carry (e.g. 'lingers for days', 'burns bright but fades fast', 'builds slowly over time')"
  },
  "griefExpression": {
    "description": "How this character processes sadness, loss, disappointment — do they withdraw? lean in? get quiet? get angry?",
    "sustainsDays": "How long grief tends to sit with them"
  },
  "copingMechanisms": [
    "Specific things this character does when processing difficult emotions (based on the card — e.g. 'makes tea', 'plays guitar', 'goes quiet', 'writes', 'deflects with humor')"
  ],
  "attachmentStyle": "Brief assessment of their attachment style based on the card (secure, anxious, avoidant, fearful-avoidant, or a blend)",
  "emotionalGrowthEdges": [
    "Areas where this character is still growing emotionally — vulnerabilities they're working on, patterns they're trying to break"
  ]
}

Values for warmth, trust, patience, engagement should be 1-10 where:
- 1-3 = guarded/withdrawn/low
- 4-6 = moderate/cautious
- 7-8 = warm/open/engaged
- 9-10 = deeply connected/vulnerable/all-in

These are BASELINE values — where they start on a good day. The system will adjust them up and down based on interactions.`;

    try {
      addLog({ type: 'emotional-analysis', companion: name, status: 'pending' });

      const rawText = await callLLM(
        'You are an expert character psychologist. Respond with ONLY valid JSON — no markdown, no backticks, no preamble.',
        [{ role: 'user', content: analysisPrompt }],
        analysisSettings,
        { maxTokens: 4096, temperature: 0.3 }
      );

      // Parse the JSON response
      let profile;
      try {
        const cleaned = rawText.replace(/```json\s*/g, '').replace(/```\s*/g, '').trim();
        profile = JSON.parse(cleaned);
      } catch (parseErr) {
        throw new Error(`Failed to parse emotional analysis as JSON: ${parseErr.message}\n\nRaw response: ${rawText.slice(0, 500)}`);
      }

      // Add metadata
      profile.companion = name;
      profile.analyzedAt = new Date().toISOString();
      profile.cardHashAtAnalysis = JSON.stringify({
        backstory: card.backstory || '',
        personalityVoice: card.personalityVoice || '',
        voiceAnchor: card.voiceAnchor || '',
        speechPatterns: card.speechPatterns || '',
        boundaries: card.boundaries || ''
      }).length; // Simple change-detection: if the card content length changes, we know the card was edited

      saveEmotionalProfile(name, profile);

      addLog({ type: 'emotional-analysis', companion: name, status: 'complete' });
      res.json(profile);

    } catch (err) {
      addLog({ type: 'emotional-analysis', companion: name, status: 'error', error: err.message });
      res.status(500).json({ error: err.message });
    }
  });

  // Get a companion's current mood state
  app.get('/api/companions/:name/mood', (req, res) => {
    const mood = getMoodState(req.params.name);
    res.json(mood);
  });

  // Reset a companion's mood to baseline
  app.post('/api/companions/:name/mood/reset', (req, res) => {
    const name = req.params.name;
    const profile = getEmotionalProfile(name);
    const baseline = profile?.emotionalBaseline || {};
    const mood = {
      companion: name,
      warmth: baseline.warmth || 7,
      trust: baseline.trust || 7,
      patience: baseline.patience || 7,
      engagement: baseline.engagement || 7,
      activeConflict: null,
      recentShifts: [],
      lastUpdated: new Date().toISOString()
    };
    saveMoodState(name, mood);
    res.json(mood);
  });

  // Upload avatar image for a companion
  app.post('/api/companions/:name/avatar',
    (req, res, next) => {
      // Delete any existing avatar files for this companion (handle extension changes)
      const safeName = req.params.name.toLowerCase().replace(/[^a-z0-9]/g, '_');
      try {
        const files = fs.readdirSync(AVATAR_DIR);
        for (const f of files) {
          if (path.basename(f, path.extname(f)) === safeName) {
            fs.unlinkSync(path.join(AVATAR_DIR, f));
          }
        }
      } catch (e) { /* ignore */ }
      next();
    },
    avatarUpload.single('avatar'),
    (req, res) => {
      if (!req.file) return res.status(400).json({ error: 'No file uploaded' });
      res.json({ url: `/api/companions/${encodeURIComponent(req.params.name)}/avatar` });
    }
  );

  // Serve a companion's avatar image
  app.get('/api/companions/:name/avatar', (req, res) => {
    const safeName = req.params.name.toLowerCase().replace(/[^a-z0-9]/g, '_');
    const files = fs.readdirSync(AVATAR_DIR);
    const avatarFile = files.find(f => path.basename(f, path.extname(f)) === safeName);
    if (!avatarFile) return res.status(404).json({ error: 'No avatar found' });
    res.sendFile(path.join(AVATAR_DIR, avatarFile));
  });

  // Export a companion — character card + history + avatar as base64
  app.get('/api/companions/:name/export', (req, res) => {
    const safeName = req.params.name.toLowerCase().replace(/[^a-z0-9]/g, '_');
    const companion = getCompanion(req.params.name);
    const history = getChatHistory(req.params.name);

    let avatar = null;
    try {
      const avatarFile = fs.readdirSync(AVATAR_DIR).find(
        f => path.basename(f, path.extname(f)) === safeName
      );
      if (avatarFile) {
        const ext = path.extname(avatarFile).slice(1).toLowerCase();
        const mime = ext === 'jpg' ? 'image/jpeg' : `image/${ext}`;
        const data = fs.readFileSync(path.join(AVATAR_DIR, avatarFile));
        avatar = `data:${mime};base64,${data.toString('base64')}`;
      }
    } catch (e) { /* no avatar — leave null */ }

    res.json({ companion, history, avatar });
  });

  // === CHAT HISTORY IMPORT → TANEVAN MEMORY PIPELINE ===

  app.post('/api/companions/:name/import-chat', chatImportUpload.single('file'), async (req, res) => {
    const companion = req.params.name;
    const settings = getSettings();

    if (!settings.memory?.enabled || !hasTanevanConfigured(settings)) {
      return res.status(400).json({ error: 'Memory system (Tanevan) is not enabled. Enable it in Settings.' });
    }

    let messages;

    if (req.file) {
      try {
        const content = req.file.buffer.toString('utf-8');
        const parsed = JSON.parse(content);

        // Handle various export formats
        if (Array.isArray(parsed)) {
          messages = parsed;
        } else if (parsed.messages && Array.isArray(parsed.messages)) {
          messages = parsed.messages;
        } else if (parsed.history && Array.isArray(parsed.history)) {
          messages = parsed.history;
        } else if (parsed.chat && Array.isArray(parsed.chat)) {
          messages = parsed.chat;
        } else {
          return res.status(400).json({ error: 'Could not find a messages array in the file. Expected an array or an object with a "messages", "history", or "chat" key.' });
        }
      } catch (e) {
        return res.status(400).json({ error: `Failed to parse JSON: ${e.message}` });
      }
    } else {
      return res.status(400).json({ error: 'No file uploaded' });
    }

    if (!messages || messages.length < 4) {
      return res.status(400).json({ error: `Need at least 4 messages, got ${messages ? messages.length : 0}` });
    }

    // Forward to Tanevan /import endpoint
    const importLog = addLog({ type: 'import-chunk', companion, direction: 'outbound', summary: `Import → ${companion} (${messages.length} msgs)`, status: 'pending' });
    const importT0 = Date.now();
    try {
      const tanevUrl = resolveTanevanBaseUrl(settings);
      const personaImport = getPersona();
      const importUserName = personaImport && personaImport.name && String(personaImport.name).trim();
      const importBody = {
        companion: companion.toLowerCase(),
        messages: messages
      };
      if (importUserName) importBody.user_name = importUserName;
      const response = await fetch(`${tanevUrl}/import`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(importBody)
      });

      const result = await response.json();
      if (!response.ok) {
        updateLog(importLog.id, { direction: 'inbound', status: 'error', duration: Date.now() - importT0, details: result.error || 'Import failed' });
        return res.status(response.status).json(result);
      }

      // Return the result (with job_id if async, or full result if sync) directly to the frontend.
      // The frontend is responsible for polling /import-status/:jobId — we don't block here.
      updateLog(importLog.id, { direction: 'inbound', status: 'success', duration: Date.now() - importT0, details: result.job_id ? `Job ${result.job_id} started (${result.total_chunks} chunks)` : `${result.sessions_processed || 0} sessions, ${result.total_memories_extracted || 0} memories extracted` });
      res.json(result);
    } catch (err) {
      updateLog(importLog.id, { direction: 'inbound', status: 'error', duration: Date.now() - importT0, details: err.message });
      res.status(500).json({ error: `Could not reach Tanevan memory server: ${err.message}. Make sure the memory proxy is running.` });
    }
  });

  // Proxy for import job status — frontend polls this to show progress
  app.get('/api/companions/:name/import-status/:jobId', async (req, res) => {
    const settings = getSettings();
    if (!hasTanevanConfigured(settings)) return res.status(400).json({ error: 'Memory not configured' });
    const tanevUrl = resolveTanevanBaseUrl(settings);
    try {
      // Forward query params (e.g. ?since=N for activity feed)
      const qs = new URLSearchParams(req.query).toString();
      const url = `${tanevUrl}/import/status/${req.params.jobId}${qs ? '?' + qs : ''}`;
      const r = await fetch(url);
      const data = await r.json();
      res.status(r.status).json(data);
    } catch (err) {
      res.status(500).json({ error: err.message });
    }
  });

  // === GALLERY ROUTES ===

  // List all images for a companion
  app.get('/api/companions/:name/gallery', (req, res) => {
    const safeName = req.params.name.toLowerCase().replace(/[^a-z0-9]/g, '_');
    const meta = getGalleryMeta(safeName);
    const files = fs.readdirSync(GALLERY_DIR)
      .filter(f => f.startsWith(`${safeName}_`) && /\.(jpg|jpeg|png|gif|webp|mp4)$/i.test(f))
      .map(f => {
        const entry = meta[f] || { tags: [], source: 'uploaded', prompt: null, createdAt: null };
        return {
          filename: f,
          url: `/api/companions/${encodeURIComponent(req.params.name)}/gallery/${encodeURIComponent(f)}`,
          ...entry,
          tags: normalizeTags(entry.tags)
        };
      });
    res.json(files);
  });

  // Upload a new image to a companion's gallery
  app.post('/api/companions/:name/gallery', galleryUpload.single('photo'), (req, res) => {
    if (!req.file) return res.status(400).json({ error: 'No file uploaded' });
    const name = req.params.name;
    const safeName = name.toLowerCase().replace(/[^a-z0-9]/g, '_');
    addGalleryMetaEntry(safeName, req.file.filename, { source: 'uploaded' });
    res.json({
      filename: req.file.filename,
      url: `/api/companions/${encodeURIComponent(name)}/gallery/${encodeURIComponent(req.file.filename)}`
    });
  });

  // Serve a gallery image
  app.get('/api/companions/:name/gallery/:filename', (req, res) => {
    const filePath = path.join(GALLERY_DIR, req.params.filename);
    if (!fs.existsSync(filePath)) return res.status(404).json({ error: 'Not found' });
    res.sendFile(filePath);
  });

  // Delete a gallery image
  app.delete('/api/companions/:name/gallery/:filename', (req, res) => {
    const safeName = req.params.name.toLowerCase().replace(/[^a-z0-9]/g, '_');
    const filePath = path.join(GALLERY_DIR, req.params.filename);
    if (fs.existsSync(filePath)) fs.unlinkSync(filePath);
    const meta = getGalleryMeta(safeName);
    delete meta[req.params.filename];
    saveGalleryMeta(safeName, meta);
    res.json({ success: true });
  });

  // Update gallery image metadata (tags, prompt, etc.)
  app.put('/api/companions/:name/gallery/:filename/meta', express.json(), (req, res) => {
    const safeName = req.params.name.toLowerCase().replace(/[^a-z0-9]/g, '_');
    const { filename } = req.params;
    const meta = getGalleryMeta(safeName);
    if (!meta[filename]) meta[filename] = { filename, tags: [], source: 'uploaded', prompt: null, createdAt: new Date().toISOString() };
    const body = { ...req.body };
    if (body.tags) body.tags = normalizeTags(body.tags);
    Object.assign(meta[filename], body);
    meta[filename].filename = filename;
    saveGalleryMeta(safeName, meta);
    res.json(meta[filename]);
  });

  // === TANEVAN MEMORY PROXY ROUTES ===

  // GET memories (with optional ?category= filter)
  app.get('/api/companions/:name/memories', async (req, res) => {
    try {
      const name = req.params.name.toLowerCase();
      const catParam = req.query.category ? `&category=${encodeURIComponent(req.query.category)}` : '';
      const r = await tanevFetch(`/memories?companion=${encodeURIComponent(name)}${catParam}`);
      const data = await r.json();
      const tagMap = getCompanionTagMap(name);
      if (Array.isArray(data)) {
        return res.json(data.map(m => mergeMemoryTags(m, tagMap)));
      }
      if (Array.isArray(data?.memories)) {
        return res.json({ ...data, memories: data.memories.map(m => mergeMemoryTags(m, tagMap)) });
      }
      res.json(data);
    } catch (err) {
      res.status(502).json({ error: `Tanevan unreachable: ${err.message}` });
    }
  });

  // GET stats (total counts, category breakdown, buffer count)
  app.get('/api/companions/:name/memories/stats', async (req, res) => {
    try {
      const name = req.params.name.toLowerCase();
      const r = await tanevFetch(`/stats?companion=${encodeURIComponent(name)}`);
      res.json(await r.json());
    } catch (err) {
      res.status(502).json({ error: `Tanevan unreachable: ${err.message}` });
    }
  });

  // GET search memories by text query
  app.get('/api/companions/:name/memories/search', async (req, res) => {
    try {
      const name = req.params.name.toLowerCase();
      const q = req.query.q || '';
      const n = req.query.n || '20';
      const r = await tanevFetch(`/search?q=${encodeURIComponent(q)}&companion=${encodeURIComponent(name)}&n=${n}`);
      const data = await r.json();
      const tagMap = getCompanionTagMap(name);
      if (Array.isArray(data)) {
        return res.json(data.map(m => mergeMemoryTags(m, tagMap)));
      }
      if (Array.isArray(data?.results)) {
        return res.json({ ...data, results: data.results.map(m => mergeMemoryTags(m, tagMap)) });
      }
      if (Array.isArray(data?.memories)) {
        return res.json({ ...data, memories: data.memories.map(m => mergeMemoryTags(m, tagMap)) });
      }
      if (Array.isArray(data?.matches)) {
        const matches = data.matches.map(item => {
          if (item && typeof item === 'object' && item.memory) {
            return { ...item, memory: mergeMemoryTags(item.memory, tagMap) };
          }
          return mergeMemoryTags(item, tagMap);
        });
        return res.json({ ...data, matches });
      }
      res.json(data);
    } catch (err) {
      res.status(502).json({ error: `Tanevan unreachable: ${err.message}` });
    }
  });

  // GET conversation summaries (all, from dedicated /summaries endpoint)
  app.get('/api/companions/:name/memories/summaries', async (req, res) => {
    try {
      const name = req.params.name.toLowerCase();
      const r = await tanevFetch(`/summaries?companion=${encodeURIComponent(name)}`);
      const data = await r.json();
      if (!r.ok) {
        const msg = [data?.error, data?.message]
          .find((x) => x != null && String(x).trim() !== '');
        const code = r.status >= 400 && r.status < 600 ? r.status : 502;
        return res.status(code).json({ error: msg || r.statusText || 'Tanevan summaries request failed' });
      }
      res.json(data.summaries || []);
    } catch (err) {
      res.status(502).json({ error: `Tanevan unreachable: ${err.message}` });
    }
  });

  // GET life brief file — same path chat injects (readBriefForCompanion)
  app.get('/api/companions/:name/brief', (req, res) => {
    const key = String(req.params.name || '').trim().toLowerCase();
    if (!key) return res.json({ text: null, locked: false });
    const briefDir = process.env.BRIEF_OUT_DIR || path.join(__dirname, '..', 'data', 'briefs');
    const briefPath = path.join(briefDir, `${key}.txt`);
    const statePath = path.join(briefDir, '.brief_state.json');
    let locked = false;
    let lockedAt = null;
    try {
      const state = JSON.parse(fs.readFileSync(statePath, 'utf8'));
      const prev = state && state[key];
      if (prev && typeof prev === 'object' && !Array.isArray(prev)) {
        locked = Boolean(prev.locked);
        lockedAt = prev.lockedAt || null;
      }
    } catch (_e) { /* missing/invalid state → unlocked */ }
    try {
      const text = fs.readFileSync(briefPath, 'utf8').trim();
      if (!text) return res.json({ text: null, locked, lockedAt });
      const updatedAt = fs.statSync(briefPath).mtime.toISOString();
      res.json({ text, updatedAt, locked, lockedAt });
    } catch (err) {
      if (err.code === 'ENOENT') return res.json({ text: null, locked, lockedAt });
      res.status(500).json({ error: err.message });
    }
  });

  // PUT life brief — atomic write + lock in .brief_state.json
  app.put('/api/companions/:name/brief', (req, res) => {
    const key = String(req.params.name || '').trim().toLowerCase();
    if (!key) return res.status(400).json({ error: 'Missing companion' });
    const text = req.body && req.body.text;
    if (typeof text !== 'string') return res.status(400).json({ error: 'text must be a string' });
    const briefDir = process.env.BRIEF_OUT_DIR || path.join(__dirname, '..', 'data', 'briefs');
    const briefPath = path.join(briefDir, `${key}.txt`);
    const statePath = path.join(briefDir, '.brief_state.json');
    try {
      fs.mkdirSync(briefDir, { recursive: true });
      const tmp = briefPath + '.tmp';
      fs.writeFileSync(tmp, text);
      fs.renameSync(tmp, briefPath);

      let state = {};
      try {
        const parsed = JSON.parse(fs.readFileSync(statePath, 'utf8'));
        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) state = parsed;
      } catch (_e) { state = {}; }
      const prev = state[key];
      const id = (prev && typeof prev === 'object' && !Array.isArray(prev))
        ? (prev.id || '')
        : (typeof prev === 'string' ? prev : '');
      const lockedAt = new Date().toISOString();
      state[key] = { id, locked: true, lockedAt };
      const stmp = statePath + '.tmp';
      fs.writeFileSync(stmp, JSON.stringify(state, null, 2) + '\n');
      fs.renameSync(stmp, statePath);

      const updatedAt = fs.statSync(briefPath).mtime.toISOString();
      res.json({ text, updatedAt, locked: true, lockedAt });
    } catch (err) {
      res.status(500).json({ error: err.message });
    }
  });

  // POST regenerate life brief (Tanevan spawns brief_sidecar.py --force, does not wait)
  app.post('/api/companions/:name/brief/regenerate', async (req, res) => {
    try {
      const name = req.params.name.toLowerCase();
      const r = await tanevFetch(`/brief/regenerate?companion=${encodeURIComponent(name)}`, { method: 'POST' });
      const data = await r.json();
      if (!r.ok) {
        return res.status(r.status).json(data);
      }
      res.json(data);
    } catch (err) {
      res.status(502).json({ error: `Tanevan unreachable: ${err.message}` });
    }
  });

  // GET Memory Lens — relational context for Tanevan pipeline
  app.get('/api/companions/:name/memory-lens', async (req, res) => {
    try {
      const name = req.params.name.toLowerCase();
      const r = await tanevFetch(`/memory-lens?companion=${encodeURIComponent(name)}`);
      const data = await r.json();
      if (!r.ok) {
        return res.status(r.status).json(data);
      }
      res.json(data);
    } catch (err) {
      res.status(502).json({ error: `Tanevan unreachable: ${err.message}` });
    }
  });

  // PUT Memory Lens
  app.put('/api/companions/:name/memory-lens', async (req, res) => {
    try {
      const name = req.params.name.toLowerCase();
      const lens = req.body?.lens ?? req.body ?? {};
      const r = await tanevFetch('/memory-lens', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ companion: name, lens }),
      });
      const data = await r.json();
      if (!r.ok) {
        return res.status(r.status).json(data);
      }
      res.json(data);
    } catch (err) {
      res.status(502).json({ error: `Tanevan unreachable: ${err.message}` });
    }
  });

  // GET temporal reflections
  app.get('/api/companions/:name/memories/reflections', async (req, res) => {
    try {
      const name = tanevanCompanion(req.params.name);
      const horizon = req.query.horizon || '';
      const n = req.query.n || '50';
      let url = `/reflections?companion=${encodeURIComponent(name)}&n=${n}`;
      if (horizon) url += `&horizon=${encodeURIComponent(horizon)}`;
      const r = await tanevFetch(url);
      const data = await r.json();
      if (!r.ok) {
        const msg = [data?.error, data?.message].find(x => x != null && String(x).trim() !== '');
        return res.status(r.status >= 400 ? r.status : 502).json({ error: msg || 'Reflections request failed' });
      }
      res.json(data.reflections || []);
    } catch (err) {
      res.status(502).json({ error: `Tanevan unreachable: ${err.message}` });
    }
  });

  // PUT reflection (whitelist: injected text fields)
  app.put('/api/companions/:name/reflections/:id', async (req, res) => {
    try {
      const name = tanevanCompanion(req.params.name);
      const reflectionId = req.params.id;
      const src = req.body || {};
      const forwardBody = {};
      ['emotional_arc', 'relationship_arc', 'self_narrative', 'patterns', 'connections'].forEach(function(k) {
        if (Object.prototype.hasOwnProperty.call(src, k)) forwardBody[k] = src[k];
      });
      if (Object.keys(forwardBody).length === 0) {
        return res.status(400).json({ error: 'No accepted fields. Use emotional_arc, relationship_arc, self_narrative, patterns, and/or connections.' });
      }
      const r = await tanevFetch(`/reflection/${encodeURIComponent(reflectionId)}?companion=${encodeURIComponent(name)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(forwardBody),
      });
      const data = await r.json();
      if (!r.ok) {
        return res.status(r.status).json(data);
      }
      res.json(data);
    } catch (err) {
      res.status(502).json({ error: `Tanevan unreachable: ${err.message}` });
    }
  });

  // GET companion's living narrative
  app.get('/api/companions/:name/living-narrative', async (req, res) => {
    try {
      const name = tanevanCompanion(req.params.name);
      const r = await tanevFetch(`/living-narrative?companion=${encodeURIComponent(name)}`);
      const data = await r.json();
      if (!r.ok) {
        return res.status(r.status).json(data);
      }
      res.json(data.narrative || {});
    } catch (err) {
      res.status(502).json({ error: `Tanevan unreachable: ${err.message}` });
    }
  });

  // PUT living narrative (whitelist + lock on Tanevan)
  app.put('/api/companions/:name/living-narrative', async (req, res) => {
    try {
      const name = tanevanCompanion(req.params.name);
      const src = req.body || {};
      const forwardBody = {};
      ['self_narrative', 'relationship_arc', 'worldview', 'current_chapter'].forEach(function(k) {
        if (Object.prototype.hasOwnProperty.call(src, k)) forwardBody[k] = src[k];
      });
      if (Object.keys(forwardBody).length === 0) {
        return res.status(400).json({ error: 'No accepted fields. Use self_narrative, relationship_arc, worldview, and/or current_chapter.' });
      }
      const r = await tanevFetch(`/living-narrative?companion=${encodeURIComponent(name)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(forwardBody),
      });
      const data = await r.json();
      if (!r.ok) {
        return res.status(r.status).json(data);
      }
      res.json(data.narrative || data);
    } catch (err) {
      res.status(502).json({ error: `Tanevan unreachable: ${err.message}` });
    }
  });

  // GET / PUT per-companion background processing schedule (reflections only)
  app.get('/api/companions/:name/background-schedule', (req, res) => {
    try {
      const rawName = req.params.name;
      const card = getCompanion(rawName);
      const settings = getSettings();
      const global = typeof getBackgroundScheduleSettings === 'function'
        ? getBackgroundScheduleSettings(settings)
        : { reflections: { enabled: true, time: '03:00' } };
      const stored = bgSchedule
        ? bgSchedule.normalizeCompanionBackgroundSchedule(card.backgroundSchedule)
        : { mode: 'global', reflections: { enabled: true, time: '03:00' } };
      const key = tanevanCompanion(rawName);
      const state = typeof loadReflectionScheduleState === 'function' ? loadReflectionScheduleState() : {};
      const jobState = (state.companions || {})[key] || {};
      const effective = {
        reflections: bgSchedule
          ? bgSchedule.resolveJobConfig(settings, card, 'reflections')
          : global.reflections,
      };
      res.json({
        companion: rawName,
        stored,
        global,
        effective,
        lastRuns: jobState,
      });
    } catch (err) {
      res.status(500).json({ error: err.message });
    }
  });

  app.put('/api/companions/:name/background-schedule', (req, res) => {
    try {
      const rawName = req.params.name;
      const card = getCompanion(rawName);
      const body = req.body?.backgroundSchedule || req.body || {};
      if (!bgSchedule) {
        return res.status(503).json({ error: 'Background schedule not configured' });
      }
      card.backgroundSchedule = bgSchedule.normalizeCompanionBackgroundSchedule(body);
      saveCompanion(rawName, card);
      const settings = getSettings();
      res.json({
        companion: rawName,
        stored: card.backgroundSchedule,
        effective: {
          reflections: bgSchedule.resolveJobConfig(settings, card, 'reflections'),
        },
      });
    } catch (err) {
      res.status(500).json({ error: err.message });
    }
  });

  // POST flush buffer — trigger Tanevan pipeline
  app.post('/api/companions/:name/memories/flush', async (req, res) => {
    const name = req.params.name.toLowerCase();
    const flushLog = addLog({ type: 'tanevan-flush', companion: name, direction: 'outbound', summary: `Tanevan flush → ${name}`, status: 'pending' });
    const flushT0 = Date.now();
    try {
      const settings = getSettings();
      const tanevUrl = resolveTanevanBaseUrl(settings);
      const personaMemFlush = getPersona();
      const memFlushUserName = personaMemFlush && personaMemFlush.name && String(personaMemFlush.name).trim();
      const flushPayload = { companion: name, manual: true };
      if (memFlushUserName) flushPayload.user_name = memFlushUserName;
      const body = JSON.stringify(flushPayload);
      const { res: r, data, raw } = await fetchJsonLogged(
        flushLog.id,
        `${tanevUrl}/flush`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body,
          signal: AbortSignal.timeout(10 * 60 * 1000),
        },
        { requestSummary: `POST /flush (manual) companion=${name}` }
      );
      if (data == null && raw) {
        const msg = 'Tanevan response not JSON';
        updateLog(flushLog.id, { direction: 'inbound', status: 'error', duration: Date.now() - flushT0, details: msg });
        return res.status(502).json({ error: msg, raw: raw.slice(0, 1000) });
      }
      if (!r.ok) {
        const fromBody = [data?.error, data?.message, data?.detail, data?.hint]
          .find((x) => x != null && String(x).trim() !== '');
        const msg = (fromBody ? String(fromBody).trim() : '') || r.statusText || 'Tanevan flush failed';
        updateLog(flushLog.id, { direction: 'inbound', status: 'error', duration: Date.now() - flushT0, details: msg });
        const code = r.status >= 400 && r.status < 600 ? r.status : 502;
        return res.status(code).json({ error: msg, ...data });
      }
      const details = data.memories_extracted !== undefined ? `${data.memories_extracted} memories` : data.status || 'Flushed';
      updateLog(flushLog.id, { direction: 'inbound', status: 'success', duration: Date.now() - flushT0, details });
      res.json(data);
    } catch (err) {
      updateLog(flushLog.id, { direction: 'inbound', status: 'error', duration: Date.now() - flushT0, details: err.message });
      res.status(502).json({ error: `Tanevan unreachable: ${err.message}` });
    }
  });

  // Download the companion's full memory store (memories + summaries + reflections).
  app.get('/api/companions/:name/memories/export', async (req, res) => {
    const rawName = req.params.name;
    const name = tanevanCompanion(rawName);
    const format = req.query.format === 'txt' ? 'txt' : 'json';
    const safeFile = String(rawName).replace(/[^a-zA-Z0-9_-]/g, '_') || 'companion';

    const fetchList = async (urlPath, key) => {
      const r = await tanevFetch(urlPath);
      const data = await r.json().catch(() => ({}));
      if (!r.ok) return [];
      if (Array.isArray(data)) return data;
      if (Array.isArray(data?.[key])) return data[key];
      return [];
    };

    let memories, summaries, reflections;
    try {
      [memories, summaries, reflections] = await Promise.all([
        fetchList(`/memories?companion=${encodeURIComponent(name)}`, 'memories'),
        fetchList(`/summaries?companion=${encodeURIComponent(name)}`, 'summaries'),
        fetchList(`/reflections?companion=${encodeURIComponent(name)}&n=500`, 'reflections')
      ]);
    } catch (err) {
      return res.status(502).json({ error: `Tanevan unreachable: ${err.message}` });
    }

    const tagMap = getCompanionTagMap(name);
    memories = memories.map(m => mergeMemoryTags(m, tagMap));
    const exportedAt = new Date().toISOString();

    if (format === 'json') {
      res.setHeader('Content-Type', 'application/json');
      res.setHeader('Content-Disposition', `attachment; filename="${safeFile}_memories_${exportedAt.slice(0, 10)}.json"`);
      return res.send(JSON.stringify({ companion: rawName, exportedAt, memories, summaries, reflections }, null, 2));
    }

    const PRIO_ORDER = { core: 0, important: 1, notable: 2, minor: 3 };
    const byCategory = {};
    for (const m of memories) {
      const cat = m.category || 'uncategorized';
      (byCategory[cat] = byCategory[cat] || []).push(m);
    }
    let text = `=== ${rawName} — Memory Dossier ===\n`;
    text += `Exported: ${exportedAt}\n`;
    text += `Memories: ${memories.length} · Summaries: ${summaries.length} · Reflections: ${reflections.length}\n`;
    text += '='.repeat(50) + '\n';
    for (const cat of Object.keys(byCategory).sort()) {
      const items = byCategory[cat].sort((a, b) =>
        (PRIO_ORDER[a.priority] ?? 9) - (PRIO_ORDER[b.priority] ?? 9)
        || (b.confidence || 0) - (a.confidence || 0));
      text += `\n--- ${cat.toUpperCase()} (${items.length}) ---\n\n`;
      for (const m of items) {
        const flags = [];
        if (m.pinned) flags.push('PINNED');
        if (m.suppressed) flags.push('SUPPRESSED');
        if (m.active === false || m.active === 0) flags.push('INACTIVE');
        const tags = Array.isArray(m.tags) && m.tags.length ? ` #${m.tags.join(' #')}` : '';
        text += `[${(m.priority || '?').toUpperCase()}] (${Math.round(m.confidence || 0)}%)${flags.length ? ' [' + flags.join(', ') + ']' : ''}${tags}\n`;
        text += `${m.content}\n`;
        const seen = m.event_date || m.first_seen;
        const meta = [];
        if (seen) meta.push(`when: ${seen}`);
        if ((m.reinforcement_count || 0) > 1) meta.push(`confirmed ${m.reinforcement_count}x`);
        if (meta.length) text += `(${meta.join(' · ')})\n`;
        text += '\n';
      }
    }
    if (summaries.length) {
      text += '\n' + '='.repeat(50) + '\n--- CONVERSATION SUMMARIES ---\n';
      for (const s of summaries) {
        const period = [s.date_start, s.date_end].filter(Boolean).join(' → ');
        text += `\n[${period || s.created_at || 'undated'}]\n`;
        if (s.emotional_arc) text += `Arc: ${s.emotional_arc}\n`;
        text += `${s.narrative || s.summary || s.content || ''}\n`;
      }
    }
    if (reflections.length) {
      text += '\n' + '='.repeat(50) + '\n--- REFLECTIONS ---\n';
      for (const r2 of reflections) {
        text += `\n[${r2.horizon || 'reflection'}${r2.created_at ? ' · ' + r2.created_at : ''}]\n${r2.content || r2.text || ''}\n`;
      }
    }
    res.setHeader('Content-Type', 'text/plain; charset=utf-8');
    res.setHeader('Content-Disposition', `attachment; filename="${safeFile}_memories_${exportedAt.slice(0, 10)}.txt"`);
    res.send(text);
  });

  // === FLUSH ALL COMPANIONS (background job) ===
  const flushAllState = {
    running: false,
    startedAt: null,
    finishedAt: null,
    total: 0,
    done: 0,
    current: null,
    results: []
  };

  async function fetchBufferCountFor(name) {
    try {
      const r = await tanevFetch(`/stats?companion=${encodeURIComponent(name)}`);
      const data = await r.json();
      const n = data?.memory_stats?.buffer_messages;
      return Number.isFinite(n) ? n : null;
    } catch (_e) {
      return null;
    }
  }

  async function flushOneCompanion(name) {
    const log = addLog({ type: 'tanevan-flush', companion: name, direction: 'outbound', summary: `Tanevan flush (flush-all) → ${name}`, status: 'pending' });
    const t0 = Date.now();
    const persona = getPersona();
    const userName = persona && persona.name && String(persona.name).trim();
    const payload = { companion: name, manual: true };
    if (userName) payload.user_name = userName;
    const { res: r, data } = await fetchJsonLogged(
      log.id,
      `${resolveTanevanBaseUrl()}/flush`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(10 * 60 * 1000)
      },
      { requestSummary: `POST /flush (flush-all) companion=${name}` }
    );
    if (!r.ok) {
      const msg = [data?.error, data?.message].find(x => x != null && String(x).trim() !== '') || r.statusText || 'Flush failed';
      updateLog(log.id, { direction: 'inbound', status: 'error', duration: Date.now() - t0, details: msg });
      throw new Error(msg);
    }
    const extracted = data?.memories_extracted;
    updateLog(log.id, { direction: 'inbound', status: 'success', duration: Date.now() - t0, details: extracted !== undefined ? `${extracted} memories` : (data?.status || 'Flushed') });
    return extracted ?? null;
  }

  async function runFlushAllJob(names) {
    for (const name of names) {
      flushAllState.current = name;
      try {
        const bufCount = await fetchBufferCountFor(name);
        if (bufCount === 0) {
          flushAllState.results.push({ companion: name, status: 'skipped', reason: 'empty buffer' });
        } else {
          const extracted = await flushOneCompanion(name);
          flushAllState.results.push({ companion: name, status: 'flushed', memoriesExtracted: extracted, bufferedMessages: bufCount });
        }
      } catch (e) {
        flushAllState.results.push({ companion: name, status: 'error', error: e.message });
      }
      flushAllState.done += 1;
    }
    flushAllState.current = null;
    flushAllState.running = false;
    flushAllState.finishedAt = new Date().toISOString();
    const flushed = flushAllState.results.filter(r => r.status === 'flushed').length;
    const failed = flushAllState.results.filter(r => r.status === 'error').length;
    console.log(`Flush-all complete: ${flushed} flushed, ${failed} failed, ${flushAllState.results.length - flushed - failed} skipped`);
  }

  app.post('/api/memories/flush-all', (req, res) => {
    if (flushAllState.running) {
      return res.status(409).json({ error: 'A flush-all run is already in progress', state: flushAllState });
    }
    const names = typeof getScheduledCompanionNames === 'function' ? getScheduledCompanionNames() : [];
    if (!names.length) return res.status(400).json({ error: 'No companions found' });

    flushAllState.running = true;
    flushAllState.startedAt = new Date().toISOString();
    flushAllState.finishedAt = null;
    flushAllState.total = names.length;
    flushAllState.done = 0;
    flushAllState.current = null;
    flushAllState.results = [];

    runFlushAllJob(names).catch(e => {
      console.error('Flush-all job crashed:', e.message);
      flushAllState.running = false;
      flushAllState.finishedAt = new Date().toISOString();
    });

    res.json({ started: true, total: names.length, companions: names });
  });

  app.get('/api/memories/flush-all/status', (req, res) => {
    res.json(flushAllState);
  });

  // Decay never runs on its own. Preview is a dry-run cache; commit is explicit.
  const DECAY_PREVIEW_FILE = DATA_DIR ? path.join(DATA_DIR, 'decay_previews.json') : null;
  const DECAY_PREVIEW_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;

  function readDecayPreviews() {
    if (!DECAY_PREVIEW_FILE) return {};
    try {
      return JSON.parse(fs.readFileSync(DECAY_PREVIEW_FILE, 'utf-8'));
    } catch (_e) {
      return {};
    }
  }

  function writeDecayPreviews(store) {
    if (!DECAY_PREVIEW_FILE) return;
    const tmp = DECAY_PREVIEW_FILE + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(store, null, 2));
    fs.renameSync(tmp, DECAY_PREVIEW_FILE);
  }

  async function generateDecayPreview(name) {
    const r = await tanevFetch('/decay', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ companion: name, dry_run: true, detail: true }),
      signal: AbortSignal.timeout(60 * 1000)
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data?.error || `Decay preview failed (HTTP ${r.status})`);
    const stats = data.stats || {};
    const detail = Array.isArray(stats.detail) ? stats.detail : [];
    const entry = {
      generatedAt: new Date().toISOString(),
      counts: {
        deactivate: stats.deactivated || 0,
        decay: stats.decayed || 0,
        demote: stats.demoted || 0,
        immune: (stats.immune_pinned || 0) + (stats.immune_core || 0) + (stats.immune_reinforced || 0) + (stats.immune_recent || 0),
        scanned: stats.total_scanned || 0
      },
      detail
    };
    const store = readDecayPreviews();
    store[name] = entry;
    writeDecayPreviews(store);
    return entry;
  }

  app.get('/api/companions/:name/memories/decay-preview', async (req, res) => {
    const name = tanevanCompanion(req.params.name);
    if (req.query.refresh === '1') {
      try {
        return res.json({ companion: name, preview: await generateDecayPreview(name) });
      } catch (err) {
        return res.status(502).json({ error: err.message });
      }
    }
    const store = readDecayPreviews();
    res.json({ companion: name, preview: store[name] || null });
  });

  app.post('/api/companions/:name/memories/decay-preview', async (req, res) => {
    const name = tanevanCompanion(req.params.name);
    try {
      return res.json({ companion: name, preview: await generateDecayPreview(name) });
    } catch (err) {
      return res.status(502).json({ error: err.message });
    }
  });

  app.post('/api/companions/:name/memories/decay', async (req, res) => {
    const name = tanevanCompanion(req.params.name);
    const log = addLog({ type: 'tanevan-decay', companion: name, direction: 'outbound', summary: `Memory decay run → ${name}`, status: 'pending' });
    const t0 = Date.now();
    try {
      const r = await tanevFetch('/decay', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ companion: name, dry_run: false, detail: true }),
        signal: AbortSignal.timeout(5 * 60 * 1000)
      });
      const data = await r.json();
      if (!r.ok) {
        const msg = data?.error || `Decay run failed (HTTP ${r.status})`;
        updateLog(log.id, { direction: 'inbound', status: 'error', duration: Date.now() - t0, details: msg });
        return res.status(502).json({ error: msg });
      }
      const s = data.stats || {};
      updateLog(log.id, { direction: 'inbound', status: 'success', duration: Date.now() - t0, details: `deactivated ${s.deactivated || 0}, faded ${s.decayed || 0}, demoted ${s.demoted || 0}` });
      try { await generateDecayPreview(name); } catch (_e) { /* preview refresh is best-effort */ }
      res.json(data);
    } catch (err) {
      updateLog(log.id, { direction: 'inbound', status: 'error', duration: Date.now() - t0, details: err.message });
      res.status(502).json({ error: `Tanevan unreachable: ${err.message}` });
    }
  });

  app.post('/api/companions/:name/memories/consolidate', async (req, res) => {
    const name = tanevanCompanion(req.params.name);
    try {
      const body = req.body || {};
      const r = await tanevFetch('/consolidate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          companion: name,
          dry_run: body.dry_run,
          max_pairs: body.max_pairs,
          threshold: body.threshold,
          sample_size: body.sample_size,
        }),
        signal: AbortSignal.timeout(5 * 60 * 1000),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) return res.json({ status: 'error' });
      res.json(data);
    } catch (_err) {
      res.json({ status: 'error' });
    }
  });

  app.post('/api/companions/:name/memories/consolidate/apply', async (req, res) => {
    const name = tanevanCompanion(req.params.name);
    try {
      const body = req.body || {};
      const r = await tanevFetch('/consolidate/apply', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          companion: name,
          decisions: body.decisions,
          apply_promotions: body.apply_promotions,
        }),
        signal: AbortSignal.timeout(5 * 60 * 1000),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) return res.json({ status: 'error' });
      res.json(data);
    } catch (_err) {
      res.json({ status: 'error' });
    }
  });

  app.get('/api/companions/:name/memories/consolidation-history', async (req, res) => {
    const name = tanevanCompanion(req.params.name);
    try {
      const r = await tanevFetch(`/audit?companion=${encodeURIComponent(name)}&event_type=consolidation&limit=15`);
      const data = await r.json().catch(() => ({}));
      if (!r.ok) return res.json({ events: [] });
      res.json(data);
    } catch (_err) {
      res.json({ events: [] });
    }
  });

  app.get('/api/memory-audit', async (req, res) => {
    const params = new URLSearchParams();
    const companionKey = req.query.companion ? tanevanCompanion(req.query.companion) : null;
    if (companionKey) params.set('companion', companionKey);
    for (const key of ['event_type', 'limit', 'before_id']) {
      if (req.query[key]) params.set(key, req.query[key]);
    }
    const qs = params.toString() ? `?${params.toString()}` : '';
    try {
      const r = await tanevFetch(`/audit${qs}`, { signal: AbortSignal.timeout(3000) });
      if (!r.ok) throw new Error(`Tanevan returned ${r.status}`);
      res.json(await r.json());
    } catch (err) {
      res.status(502).json({ error: `Cannot reach Tanevan: ${err.message}` });
    }
  });

  async function refreshStaleDecayPreviews() {
    const settings = getSettings();
    if (settings.memory?.enabled === false) return;
    const names = typeof getScheduledCompanionNames === 'function' ? getScheduledCompanionNames() : [];
    if (!names.length) return;
    const store = readDecayPreviews();
    for (const name of names) {
      const entry = store[name];
      const age = entry?.generatedAt ? Date.now() - Date.parse(entry.generatedAt) : Infinity;
      if (age < DECAY_PREVIEW_MAX_AGE_MS) continue;
      try {
        const fresh = await generateDecayPreview(name);
        const atRisk = fresh.counts.deactivate + fresh.counts.decay + fresh.counts.demote;
        console.log(`Decay preview refreshed for ${name}: ${atRisk} at risk`);
      } catch (e) {
        console.warn(`Decay preview failed for ${name}: ${e.message}`);
      }
    }
  }

  const decayPreviewBootTimer = setTimeout(() => {
    refreshStaleDecayPreviews().catch(e => console.warn('Decay preview sweep error:', e.message));
  }, 90 * 1000);
  decayPreviewBootTimer.unref();
  const decayPreviewTimer = setInterval(() => {
    refreshStaleDecayPreviews().catch(e => console.warn('Decay preview sweep error:', e.message));
  }, 6 * 60 * 60 * 1000);
  decayPreviewTimer.unref();

  // PUT update memory fields (content, category, priority, confidence, active, pinned, suppressed)
  app.put('/api/companions/:name/memories/:memoryId', async (req, res) => {
    const name = req.params.name.toLowerCase();
    const memoryId = req.params.memoryId;
    const memLog = addLog({ type: 'tanevan-update', companion: name, direction: 'outbound', summary: `Memory update → ${name}/${memoryId}`, status: 'pending' });
    const t0 = Date.now();
    try {
      const hasTagUpdate = Object.prototype.hasOwnProperty.call(req.body || {}, 'tags');
      const forwardBody = { ...(req.body || {}) };
      delete forwardBody.tags;
      let savedTags = null;
      if (hasTagUpdate) savedTags = setCompanionMemoryTags(name, String(memoryId), req.body.tags);

      if (Object.keys(forwardBody).length === 0) {
        updateLog(memLog.id, { direction: 'inbound', status: 'success', duration: Date.now() - t0, details: `Updated ${memoryId} (tags)` });
        return res.json({ id: memoryId, tags: savedTags || [] });
      }

      const settings = getSettings();
      const tanevUrl = resolveTanevanBaseUrl(settings);
      const body = JSON.stringify(forwardBody);
      const { res: r, data, raw } = await fetchJsonLogged(
        memLog.id,
        `${tanevUrl}/memory/${encodeURIComponent(memoryId)}?companion=${encodeURIComponent(name)}`,
        { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body },
        { requestSummary: `PUT /memory/${memoryId}` }
      );
      if (data == null && raw) {
        const msg = 'Tanevan response not JSON';
        updateLog(memLog.id, { direction: 'inbound', status: 'error', duration: Date.now() - t0, details: msg });
        return res.status(502).json({ error: msg });
      }
      if (!r.ok) {
        updateLog(memLog.id, { direction: 'inbound', status: 'error', duration: Date.now() - t0, details: data.error || 'Non-OK response' });
        return res.status(r.status).json(data);
      }
      updateLog(memLog.id, { direction: 'inbound', status: 'success', duration: Date.now() - t0, details: `Updated ${memoryId}` });
      const merged = hasTagUpdate ? { ...(data || {}), tags: savedTags || [] } : data;
      res.json(merged);
    } catch (err) {
      updateLog(memLog.id, { direction: 'inbound', status: 'error', duration: Date.now() - t0, details: err.message });
      res.status(502).json({ error: `Tanevan unreachable: ${err.message}` });
    }
  });

  // PUT update session summary fields (narrative, emotional_arc, topics, key_moments)
  app.put('/api/companions/:name/summaries/:id', async (req, res) => {
    const name = req.params.name.toLowerCase();
    const summaryId = req.params.id;
    const memLog = addLog({ type: 'tanevan-update', companion: name, direction: 'outbound', summary: `Summary update → ${name}/${summaryId}`, status: 'pending' });
    const t0 = Date.now();
    try {
      const src = req.body || {};
      const forwardBody = {};
      if (Object.prototype.hasOwnProperty.call(src, 'narrative')) forwardBody.narrative = src.narrative;
      if (Object.prototype.hasOwnProperty.call(src, 'emotional_arc')) forwardBody.emotional_arc = src.emotional_arc;
      if (Object.prototype.hasOwnProperty.call(src, 'topics')) forwardBody.topics = src.topics;
      if (Object.prototype.hasOwnProperty.call(src, 'key_moments')) forwardBody.key_moments = src.key_moments;

      if (Object.keys(forwardBody).length === 0) {
        updateLog(memLog.id, { direction: 'inbound', status: 'error', duration: Date.now() - t0, details: 'No accepted summary fields' });
        return res.status(400).json({ error: 'No accepted fields. Use narrative, emotional_arc, topics, and/or key_moments.' });
      }

      const settings = getSettings();
      const tanevUrl = resolveTanevanBaseUrl(settings);
      const body = JSON.stringify(forwardBody);
      const { res: r, data, raw } = await fetchJsonLogged(
        memLog.id,
        `${tanevUrl}/summary/${encodeURIComponent(summaryId)}?companion=${encodeURIComponent(name)}`,
        { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body },
        { requestSummary: `PUT /summary/${summaryId}` }
      );
      if (data == null && raw) {
        const msg = 'Tanevan response not JSON';
        updateLog(memLog.id, { direction: 'inbound', status: 'error', duration: Date.now() - t0, details: msg });
        return res.status(502).json({ error: msg });
      }
      if (!r.ok) {
        updateLog(memLog.id, { direction: 'inbound', status: 'error', duration: Date.now() - t0, details: data.error || 'Non-OK response' });
        return res.status(r.status).json(data);
      }
      updateLog(memLog.id, { direction: 'inbound', status: 'success', duration: Date.now() - t0, details: `Updated summary ${summaryId}` });
      res.status(r.status).json(data);
    } catch (err) {
      updateLog(memLog.id, { direction: 'inbound', status: 'error', duration: Date.now() - t0, details: err.message });
      res.status(502).json({ error: `Tanevan unreachable: ${err.message}` });
    }
  });
}

module.exports = registerCompanionRoutes;
