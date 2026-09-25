// © 2024-2026 Megan Neves and Tiara Young. All rights reserved.
// Love Refactored — https://github.com/Love-Encoded/Love-Refactored
// Licensed under the Love Refactored / Tanevan License. See LICENSE.md.

'use strict';

/**
 * @param {import('express').Application} app
 * @param {object} deps
 */
function registerParlorRoutes(app, deps) {
  const {
    fs,
    path,
    PARLOR_DIR,
    HISTORY_DIR,
    isSafeId,
    makeId,
    getParlor,
    saveParlor,
    getAllParlors,
    generateRoomCode,
    normalizeParlorJoinSecret,
    hashParlorJoinSecret,
    getSettings,
    getPersona,
    getCompanion,
    addLog,
    updateLog,
    logChatEvent,
    getCompanionSettings,
    callLLM,
    companionUsesCustomSystemPrompt,
    useGroupChatProfile,
    shouldInjectContext,
    buildUserPersonaStableBlock,
    buildGroupToolsBlock,
    getCurrentDateTimeString,
    appendLastSeenOrRecapToDynamic,
    getMatchingLore,
    getMemoriesForMessage,
    buildEmotionalContext,
    getCalendarContext,
    DEFAULT_GROUP_DIRECTIVE,
    bufferToTanevan,
    saveLastSeen,
    buildLastSeenSummaryFromGroupTail,
    getContextMessageLimit,
    resolveTanevanCompanionKey
  } = deps;

  const tanevanCompanion = typeof resolveTanevanCompanionKey === 'function'
    ? (raw) => resolveTanevanCompanionKey(raw)
    : (raw) => String(raw || '').trim().toLowerCase();

  function createMemoryRateLimiter({ windowMs, max, keyFn }) {
    const buckets = new Map();
    return (req, res, next) => {
      const now = Date.now();
      const key = keyFn(req);
      const bucket = buckets.get(key) || [];
      const recent = bucket.filter(ts => now - ts < windowMs);
      if (recent.length >= max) {
        const retryAfterSec = Math.max(1, Math.ceil((windowMs - (now - recent[0])) / 1000));
        res.set('Retry-After', String(retryAfterSec));
        return res.status(429).json({ error: 'Too many requests. Please try again shortly.' });
      }
      recent.push(now);
      buckets.set(key, recent);
      next();
    };
  }

  const parlorJoinHttpLimiter = createMemoryRateLimiter({
    windowMs: 60 * 1000,
    max: 10,
    keyFn: (req) => `join:${req.ip || 'unknown'}`
  });

  const parlorRespondHttpLimiter = createMemoryRateLimiter({
    windowMs: 60 * 1000,
    max: 20,
    keyFn: (req) => `respond:${req.ip || 'unknown'}:${req.params.id || ''}`
  });

  const parlorHistoryHttpLimiter = createMemoryRateLimiter({
    windowMs: 60 * 1000,
    max: 180,
    keyFn: (req) => `history:${req.ip || 'unknown'}:${req.params.id || ''}`
  });

  function parlorHistoryToRunningForLLM(history) {
    return history.map(m => {
      let st = m.senderType;
      if (!st) {
        if (m.sender === 'user' || m.sender === 'local') st = 'human';
        else st = 'companion';
      }
      if (st === 'human') {
        return { sender: 'user', text: m.text, humanLabel: m.displayName || 'User' };
      }
      return { sender: m.sender, text: m.text };
    });
  }

  // === PARLOR API ROUTES (register /join before /:id) ===
  app.get('/api/parlors', (req, res) => {
    res.json(getAllParlors());
  });

  app.post('/api/parlors/join', parlorJoinHttpLimiter, (req, res) => {
    const { code, name, companions: parlorCompanions, talkativeness, remoteHost, joinSecret } = req.body;
    const joinSecretNorm = normalizeParlorJoinSecret(joinSecret);
    if (!code || !String(code).trim()) {
      return res.status(400).json({ error: 'Room code is required' });
    }
    if (!parlorCompanions || parlorCompanions.length === 0) {
      return res.status(400).json({ error: 'Select at least one companion to bring' });
    }
    if (!remoteHost || !String(remoteHost).trim()) {
      return res.status(400).json({ error: 'Host address is required (host:port or URL)' });
    }
    if (!joinSecretNorm) {
      return res.status(400).json({ error: 'Join secret is required' });
    }
    if (joinSecretNorm.length > 128) {
      return res.status(400).json({ error: 'Join secret is too long (max 128 characters)' });
    }

    const codeNorm = String(code).trim().toUpperCase();
    const parlor = {
      id: makeId(),
      code: codeNorm,
      name: name || `Parlor ${codeNorm}`,
      role: 'guest',
      myCompanions: parlorCompanions,
      talkativeness: talkativeness || {},
      status: 'connecting',
      remoteHost: String(remoteHost).trim(),
      joinSecret: joinSecretNorm,
      maxCompanionTurns: 2,
      sharedMemory: true,
      createdAt: new Date().toISOString()
    };
    saveParlor(parlor);
    res.json(parlor);
  });

  app.post('/api/parlors', (req, res) => {
    const { name, companions: parlorCompanions, talkativeness, joinSecret } = req.body;
    const joinSecretNorm = normalizeParlorJoinSecret(joinSecret);
    if (!parlorCompanions || parlorCompanions.length === 0) {
      return res.status(400).json({ error: 'Select at least one companion to bring' });
    }
    if (!joinSecretNorm) {
      return res.status(400).json({ error: 'Join secret is required' });
    }
    if (joinSecretNorm.length > 128) {
      return res.status(400).json({ error: 'Join secret is too long (max 128 characters)' });
    }

    const existingCodes = new Set(getAllParlors().map(p => p.code));
    let code = generateRoomCode();
    let attempts = 0;
    while (existingCodes.has(code) && attempts < 64) {
      code = generateRoomCode();
      attempts++;
    }
    if (existingCodes.has(code)) {
      return res.status(500).json({ error: 'Could not allocate a unique room code; try again' });
    }

    const parlor = {
      id: makeId(),
      code,
      name: name || `Parlor ${code}`,
      role: 'host',
      myCompanions: parlorCompanions,
      talkativeness: talkativeness || {},
      status: 'waiting',
      remoteHost: null,
      joinSecretHash: joinSecretNorm ? hashParlorJoinSecret(joinSecretNorm) : null,
      maxCompanionTurns: 2,
      sharedMemory: true,
      createdAt: new Date().toISOString()
    };
    saveParlor(parlor);
    res.json(parlor);
  });

  app.get('/api/parlors/:id', (req, res) => {
    const parlor = getParlor(req.params.id);
    if (!parlor) return res.status(404).json({ error: 'Parlor not found' });
    res.json(parlor);
  });

  app.put('/api/parlors/:id', (req, res) => {
    const parlor = getParlor(req.params.id);
    if (!parlor) return res.status(404).json({ error: 'Parlor not found' });
    const allowed = ['name', 'myCompanions', 'talkativeness', 'status', 'maxCompanionTurns', 'sharedMemory', 'remoteHost'];
    for (const key of allowed) {
      if (req.body[key] !== undefined) parlor[key] = req.body[key];
    }
    if (req.body.joinSecret !== undefined) {
      const joinSecretNorm = normalizeParlorJoinSecret(req.body.joinSecret);
      if (!joinSecretNorm) {
        return res.status(400).json({ error: 'Join secret cannot be empty' });
      }
      if (joinSecretNorm.length > 128) {
        return res.status(400).json({ error: 'Join secret is too long (max 128 characters)' });
      }
      if (parlor.role === 'host') {
        parlor.joinSecretHash = joinSecretNorm ? hashParlorJoinSecret(joinSecretNorm) : null;
      } else {
        parlor.joinSecret = joinSecretNorm;
      }
    }
    saveParlor(parlor);
    res.json(parlor);
  });

  app.delete('/api/parlors/:id', (req, res) => {
    if (!isSafeId(req.params.id)) return res.status(400).json({ error: 'Invalid ID' });
    const filePath = path.join(PARLOR_DIR, `${req.params.id}.json`);
    if (fs.existsSync(filePath)) fs.unlinkSync(filePath);
    const historyFile = path.join(HISTORY_DIR, `parlor_${req.params.id}.json`);
    if (fs.existsSync(historyFile)) fs.unlinkSync(historyFile);
    res.json({ success: true });
  });

  app.get('/api/parlors/:id/history', (req, res) => {
    if (!isSafeId(req.params.id)) return res.status(400).json({ error: 'Invalid ID' });
    const filePath = path.join(HISTORY_DIR, `parlor_${req.params.id}.json`);
    if (!fs.existsSync(filePath)) return res.json([]);
    try {
      res.json(JSON.parse(fs.readFileSync(filePath, 'utf-8')));
    } catch {
      res.json([]);
    }
  });

  app.post('/api/parlors/:id/history', parlorHistoryHttpLimiter, (req, res) => {
    if (!isSafeId(req.params.id)) return res.status(400).json({ error: 'Invalid ID' });
    const filePath = path.join(HISTORY_DIR, `parlor_${req.params.id}.json`);
    fs.writeFileSync(filePath, JSON.stringify(req.body.history || [], null, 2));
    res.json({ success: true });
  });

  app.delete('/api/parlors/:id/history', (req, res) => {
    if (!isSafeId(req.params.id)) return res.status(400).json({ error: 'Invalid ID' });
    const filePath = path.join(HISTORY_DIR, `parlor_${req.params.id}.json`);
    if (fs.existsSync(filePath)) fs.unlinkSync(filePath);
    res.json({ success: true });
  });

  app.post('/api/parlors/:id/flush', async (req, res) => {
    const parlor = getParlor(req.params.id);
    if (!parlor) return res.status(404).json({ error: 'Parlor not found' });

    const { history = [] } = req.body;
    const settings = getSettings();
    const myCompanions = parlor.myCompanions || [];

    if (history.length === 0 || myCompanions.length === 0) {
      return res.json({ success: true, flushed: 0, memoriesExtracted: null });
    }

    if (parlor.sharedMemory === false) {
      try {
        logChatEvent(`parlor_${parlor.id}`, 'parlor_session', {
          type: 'parlor',
          parlorId: parlor.id,
          parlorCode: parlor.code,
          parlorName: parlor.name,
          companions: myCompanions,
          messageCount: history.length,
          memorySkipped: true,
          reason: 'sharedMemory disabled for this Parlor',
          flushedAt: new Date().toISOString()
        }, false);
      } catch (e) {
        console.error('Parlor chat log event:', e.message);
      }
      return res.json({ success: true, flushed: 0, memoriesExtracted: null, skippedMemory: true });
    }

    let bufferedCompanions = 0;
    for (const companionName of myCompanions) {
      try {
        for (const msg of history) {
          let st = msg.senderType;
          if (!st) {
            if (msg.sender === 'user' || msg.sender === 'local') st = 'human';
            else st = 'companion';
          }
          const isOwnCompanionLine = st === 'companion' && msg.sender === companionName;
          if (isOwnCompanionLine) {
            await bufferToTanevan('assistant', msg.text, settings, companionName, msg.timestamp);
          } else {
            const label = msg.displayName || msg.sender || 'Unknown';
            await bufferToTanevan('user', `[${label}]: ${msg.text}`, settings, companionName, msg.timestamp);
          }
        }
        bufferedCompanions++;
      } catch (e) {
        console.error(`Parlor buffer error for ${companionName}:`, e.message);
      }
    }

    let memoriesExtracted = null;
    if (settings.memory?.enabled && settings.memory?.tanevUrl) {
      const personaFlush = getPersona();
      const flushUserName = personaFlush && personaFlush.name && String(personaFlush.name).trim();
      const tanevUrl = ((process.env.TANEVAN_URL || '').trim() || settings.memory.tanevUrl).replace(/\/$/, '');
      let totalMemories = 0;
      for (const name of myCompanions) {
        const flushLog = addLog({
          type: 'tanevan-flush',
          companion: name,
          direction: 'outbound',
          summary: `Tanevan flush (Parlor leave) → ${name}`,
          status: 'pending'
        });
        const flushT0 = Date.now();
        try {
          const flushBody = { companion: tanevanCompanion(name), manual: true };
          if (flushUserName) flushBody.user_name = flushUserName;
          const flushRes = await fetch(`${tanevUrl}/flush`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(flushBody)
          });
          if (flushRes.ok) {
            const flushData = await flushRes.json().catch(() => ({}));
            const n = flushData.memories_extracted ?? flushData.extracted;
            if (n != null && !Number.isNaN(Number(n))) totalMemories += Number(n);
            updateLog(flushLog.id, {
              direction: 'inbound',
              status: 'success',
              duration: Date.now() - flushT0,
              details: n != null ? `${n} memories extracted` : 'Flushed'
            });
          } else {
            updateLog(flushLog.id, { direction: 'inbound', status: 'error', duration: Date.now() - flushT0, details: 'Non-OK response from Tanevan' });
          }
        } catch (err) {
          updateLog(flushLog.id, { direction: 'inbound', status: 'error', duration: Date.now() - flushT0, details: err.message });
          console.log(`Tanevan flush skipped for ${name} (Parlor):`, err.message);
        }
      }
      memoriesExtracted = totalMemories > 0 ? totalMemories : 0;
    }

    try {
      const transcriptPreview = history.slice(0, 8).map(m => {
        let st = m.senderType;
        if (!st) {
          if (m.sender === 'user' || m.sender === 'local') st = 'human';
          else st = 'companion';
        }
        const who = st === 'human' ? (m.displayName || 'User') : m.sender;
        return `${who}: ${(m.text || '').slice(0, 120)}`;
      }).join(' | ');
      logChatEvent(`parlor_${parlor.id}`, 'parlor_session', {
        type: 'parlor',
        label: `[PARLOR] ${parlor.name || parlor.code}`,
        parlorId: parlor.id,
        parlorCode: parlor.code,
        parlorName: parlor.name,
        companions: myCompanions,
        messageCount: history.length,
        memoriesExtracted: memoriesExtracted != null ? memoriesExtracted : undefined,
        transcriptPreview: transcriptPreview.slice(0, 500),
        flushedAt: new Date().toISOString()
      }, false);
    } catch (e) {
      console.error('Parlor chat log event:', e.message);
    }

    res.json({
      success: true,
      flushed: bufferedCompanions,
      memoriesExtracted
    });
  });

  // === PARLOR: local companion replies (called when a human speaks in The Parlor) ===
  app.post('/api/parlors/:id/respond', parlorRespondHttpLimiter, async (req, res) => {
    const { message, senderType, history = [] } = req.body;
    const parlor = getParlor(req.params.id);
    if (!parlor) return res.status(404).json({ error: 'Parlor not found' });
    if (senderType !== 'human') return res.json({ responses: [] });

    const settings = getSettings();
    const myCompanions = parlor.myCompanions || [];
    const talkativeness = parlor.talkativeness || {};
    const maxTurns = parlor.maxCompanionTurns || 2;

    let consecutiveCompanion = 0;
    for (let i = history.length - 1; i >= 0; i--) {
      let st = history[i].senderType;
      if (!st) {
        if (history[i].sender === 'user' || history[i].sender === 'local') st = 'human';
        else st = 'companion';
      }
      if (st === 'companion') consecutiveCompanion++;
      else break;
    }
    if (consecutiveCompanion >= maxTurns) return res.json({ responses: [] });

    const recentContext = history.slice(-5).map(m => {
      let st = m.senderType;
      if (!st) {
        if (m.sender === 'user' || m.sender === 'local') st = 'human';
        else st = 'companion';
      }
      const who = st === 'human' ? (m.displayName || 'User') : m.sender;
      return `${who}: ${m.text}`;
    }).join('\n');

    const companionSummaries = myCompanions.map(name => {
      const card = getCompanion(name);
      const blurb = (card && (card.backstory || card.personalityVoice || '')).toString().slice(0, 100);
      return `- ${name}: ${blurb}`;
    }).join('\n');

    const routerPrompt =
      `You are a chat router for The Parlor — a cross-instance group chat. Decide which 1-5 of the listed companions would most naturally respond to the latest human message. Consider relevance, conversation flow, and who would speak up. When multiple people would realistically chime in, pick more (up to 5). Do NOT pick everyone every time.\n\n` +
      `Available companions (on this instance):\n${companionSummaries}\n\n` +
      `Recent conversation:\n${recentContext || '(Start of conversation)'}\n\n` +
      `Respond with ONLY a JSON array of names in order, e.g. ["Aria","Blake"]`;

    let selectedCompanions = [...myCompanions];
    try {
      const routerReply = await callLLM(
        routerPrompt,
        [{ role: 'user', content: message }],
        settings,
        { maxTokens: 120, temperature: 0.3 }
      );
      const cleaned = routerReply.replace(/```json\n?/g, '').replace(/```\n?/g, '').trim();
      const parsed = JSON.parse(cleaned);
      if (Array.isArray(parsed) && parsed.length > 0) {
        const valid = parsed.filter(n => myCompanions.includes(n)).slice(0, 5);
        if (valid.length > 0) selectedCompanions = valid;
      }
    } catch (e) {
      console.log('Parlor router fallback:', e.message);
    }

    selectedCompanions = selectedCompanions.filter(name => {
      const chance = talkativeness[name] ?? 60;
      return Math.random() * 100 < chance;
    });
    if (selectedCompanions.length === 0) return res.json({ responses: [] });

    const parlorUserTimestamp = new Date().toISOString();
    for (const name of myCompanions) {
      bufferToTanevan('user', message, settings, name, parlorUserTimestamp);
    }

    const runningHistory = parlorHistoryToRunningForLLM(history);
    const responses = [];

    for (const companionName of selectedCompanions) {
      if (consecutiveCompanion + responses.length >= maxTurns) break;

      const card = getCompanion(companionName);
      if (!card) continue;

      if (responses.length > 0) await new Promise(r => setTimeout(r, 1000));

      const otherNames = myCompanions.filter(n => n !== companionName);
      const persona = getPersona();

      let systemStable = '';
      if (companionUsesCustomSystemPrompt(card)) {
        if (useGroupChatProfile(card)) {
          systemStable = `You are ${companionName}. Stay in character at all times.\n`;
          systemStable += `\n[GROUP CHAT PROFILE]\n${card.voiceAnchor.trim()}\n`;
          systemStable += `\n\nYou are in The Parlor — a cross-instance chat room. Your companions on this side: ${otherNames.join(', ') || '(none)'}. Humans and companions from another instance appear with labels. Respond naturally; do not speak for others.`;
        } else {
          systemStable = String(card.systemPromptOverride).trim();
          systemStable += `\n\nYou are in The Parlor — a cross-instance chat. Your companions here are: ${otherNames.join(', ') || '(none)'}. Other humans and AIs from another instance appear in the thread with labeled names. Respond only as ${companionName}. Do not speak for others.`;
        }
        if (!useGroupChatProfile(card) && card.voiceAnchor && card.voiceAnchor.trim()) {
          systemStable += `\n\n[VOICE ANCHOR — Your distinct speech patterns; do not adopt others' mannerisms.]\n${card.voiceAnchor.trim()}\n[END VOICE ANCHOR]`;
        }
      } else {
        if (useGroupChatProfile(card)) {
          systemStable = `You are ${companionName}. Stay in character at all times.\n`;
          systemStable += '\n=== CHARACTER IDENTITY ===\n';
          systemStable += `\n[GROUP CHAT PROFILE]\n${card.voiceAnchor.trim()}\n`;
          systemStable += '\n=== END CHARACTER IDENTITY ===\n';
          systemStable += '\n[RESPONSE LENGTH — In The Parlor, keep replies tight: usually 1-4 lines unless the scene needs more.]\n';
        } else if (card.backstory || card.personalityVoice || card.exampleMessages) {
          systemStable = `You are ${companionName}. Stay in character at all times.\n`;
          systemStable += '\n=== CHARACTER IDENTITY ===\n';
          if (card.backstory) systemStable += `\n[BACKSTORY]\n${card.backstory}\n`;
          if (card.boundaries) systemStable += `\n[BOUNDARIES]\n${card.boundaries}\n`;
          if (card.personalityVoice) systemStable += `\n[PERSONALITY & VOICE]\n${card.personalityVoice}\n`;
          if (card.exampleMessages) systemStable += `\n[EXAMPLE MESSAGES]\n${card.exampleMessages}\n`;
          systemStable += '\n=== END CHARACTER IDENTITY ===\n';
          systemStable += '\n[RESPONSE LENGTH — In The Parlor, keep replies tight: usually 1-4 lines unless the scene needs more.]\n';
        } else {
          systemStable = `You are ${companionName}, a companion character. Stay in character.`;
        }
        systemStable += `\n\nYou are in The Parlor — a cross-instance chat room. Your companions on this side: ${otherNames.join(', ') || '(none)'}. Humans and companions from another instance appear with labels. Respond naturally; do not speak for others.`;
        systemStable += buildUserPersonaStableBlock(card, persona);
        systemStable += buildGroupToolsBlock(settings);
        if (!useGroupChatProfile(card) && card.voiceAnchor && card.voiceAnchor.trim()) {
          systemStable += `\n\n[VOICE ANCHOR]\n${card.voiceAnchor.trim()}\n[END VOICE ANCHOR]`;
        }
      }

      systemStable += `\n\n[THE PARLOR — Cross-instance multiplayer. There are humans and AI companions from different machines here. Stay in character; react to what people actually said.]`;

      let systemDynamic = '';
      if (shouldInjectContext(card, 'customIncludeDatetime')) {
        systemDynamic += `${await getCurrentDateTimeString()}\n\n`;
      }
      if (shouldInjectContext(card, 'customIncludeLastSeen')) {
        systemDynamic = appendLastSeenOrRecapToDynamic(systemDynamic, companionName, parlor.id, getPersona().name, message);
      }

      const directive = DEFAULT_GROUP_DIRECTIVE;
      if (directive && directive.trim()) {
        systemDynamic += `\n\n[CONVERSATION RULES]\n${directive.trim()}\n[END RULES]`;
      }

      const lore = getMatchingLore(message, companionName, {
        mode: 'parlor',
        contextKey: `parlor:${parlor.id}:${companionName}`,
        requireMentionInGroup: true
      });
      if (shouldInjectContext(card, 'customIncludeLorebook') && lore.prompts.length > 0) systemDynamic += '\n\n' + lore.prompts.map(p => p.text).join('\n');
      if (shouldInjectContext(card, 'customIncludeLorebook') && lore.entries.length > 0) systemDynamic += '\n\n' + lore.entries.map(e => e.text).join('\n');

      let memResult = { context: '' };
      if (shouldInjectContext(card, 'customIncludeMemories')) {
        memResult = await getMemoriesForMessage(message, settings, companionName);
        if (memResult.context) systemDynamic += memResult.context;
      }
      if (shouldInjectContext(card, 'customIncludeEmotional')) {
        systemDynamic += buildEmotionalContext(companionName);
      }
      if (shouldInjectContext(card, 'customIncludeCalendar')) {
        const calContext = await getCalendarContext(message);
        if (calContext) systemDynamic += calContext;
      }
      if (companionUsesCustomSystemPrompt(card) && shouldInjectContext(card, 'customIncludeTools')) {
        systemDynamic += buildGroupToolsBlock(settings);
      }

      const systemPromptForOpenAI = `${systemStable}\n\n${systemDynamic}`;
      const companionSettings = getCompanionSettings(companionName, settings);
      const isAnthropicProvider = companionSettings.provider === 'anthropic';

      const parlorMsgLimit = getContextMessageLimit(card, 'group');
      const companionMessages = runningHistory.slice(-parlorMsgLimit).map(m => {
        if (m.sender === 'user') {
          const label = m.humanLabel || 'User';
          return { role: 'user', content: `[${label}]: ${m.text}` };
        }
        if (m.sender === companionName) return { role: 'assistant', content: m.text };
        if (isAnthropicProvider) {
          return { role: 'user', content: `[${m.sender} said]: ${m.text}` };
        }
        return { role: 'user', content: `[${m.sender} said]: ${m.text}`, name: String(m.sender).replace(/[^a-zA-Z0-9_-]/g, '_') };
      });

      const otherVoices = selectedCompanions.filter(n => n !== companionName);
      const identityReminder = otherVoices.length > 0
        ? `[Respond now as ${companionName}. You are NOT ${otherVoices.join(', NOT ')}.]`
        : `[Respond now as ${companionName}.]`;
      const messagesWithReminder = [...companionMessages, { role: 'user', content: identityReminder }];

      try {
        let reply = await callLLM(systemPromptForOpenAI, messagesWithReminder, companionSettings, { systemStable, systemDynamic });
        if (!reply || !String(reply).trim()) continue;

        const ts = new Date().toISOString();
        responses.push({
          companion: companionName,
          text: reply.trim(),
          senderType: 'companion',
          displayName: companionName,
          sender: companionName,
          timestamp: ts
        });

        runningHistory.push({ sender: companionName, text: reply.trim() });

        if (parlor.sharedMemory !== false) {
          bufferToTanevan('assistant', reply, settings, companionName, ts);
        }

        const userLabel = getPersona().name?.trim() || 'User';
        const tail = runningHistory.slice(-3).map(m => {
          if (m.sender === 'user') return { sender: 'user', text: m.text };
          return { sender: m.sender, text: m.text };
        });
        const lastSeenSummaryGc = buildLastSeenSummaryFromGroupTail(tail, userLabel, 200);
        saveLastSeen(companionName, {
          context: 'group',
          contextId: parlor.id,
          groupName: parlor.name || 'Parlor',
          summary: lastSeenSummaryGc,
          timestamp: ts
        });
      } catch (e) {
        console.error(`Parlor respond error for ${companionName}:`, e.message);
        responses.push({
          companion: companionName,
          text: `⚠️ ${e.message}`,
          senderType: 'companion',
          sender: companionName,
          timestamp: new Date().toISOString()
        });
      }
    }

    res.json({ responses });
  });
}

module.exports = registerParlorRoutes;
