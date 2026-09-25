// © 2024-2026 Megan Neves and Tiara Young. All rights reserved.
// Love Refactored — https://github.com/Love-Encoded/Love-Refactored
// Licensed under the Love Refactored / Tanevan License. See LICENSE.md.

'use strict';

/**
 * @param {import('express').Application} app
 * @param {object} deps
 */
function registerGroupRoutes(app, deps) {
  const {
    fs,
    path,
    isSafeId,
    makeId,
    getAllGroups,
    getGroup,
    saveGroup,
    DEFAULT_GROUP_DIRECTIVE,
    GROUP_DIR,
    HISTORY_DIR,
    groupConversationKey,
    getGroupHistory,
    saveGroupHistory,
    appendToGroupHistory,
    appendToChatLog,
    countChatMessages,
    updateChatMessageAtIndex,
    deleteChatMessageAtIndex,
    truncateChatMessagesFromIndex,
    insertChatMessagesAtIndex,
    logChatEvent,
    startNewChatLogSession,
    getSettings,
    getPersona,
    addLog,
    updateLog
  } = deps;

  app.get('/api/groups', (req, res) => {
    res.json(getAllGroups());
  });

  app.post('/api/groups', (req, res) => {
    const { name, companions: groupCompanions, context, directive, sharedMemory } = req.body;
    if (!name || !groupCompanions || groupCompanions.length < 2) {
      return res.status(400).json({ error: 'Group name and at least 2 companions are required' });
    }
    const group = {
      id: makeId(),
      name,
      companions: groupCompanions,
      context: context || '',
      directive: directive !== undefined ? directive : DEFAULT_GROUP_DIRECTIVE,
      sharedMemory: sharedMemory !== false,
      createdAt: new Date().toISOString()
    };
    saveGroup(group);
    res.json(group);
  });

  app.put('/api/groups/:id', (req, res) => {
    const group = getGroup(req.params.id);
    if (!group) return res.status(404).json({ error: 'Group not found' });
    const allowed = ['name', 'companions', 'context', 'directive', 'sharedMemory'];
    for (const key of allowed) {
      if (req.body[key] !== undefined) group[key] = req.body[key];
    }
    saveGroup(group);
    res.json(group);
  });

  app.delete('/api/groups/:id', (req, res) => {
    if (!isSafeId(req.params.id)) return res.status(400).json({ error: 'Invalid ID' });
    logChatEvent(req.params.id, 'group_deleted', {
      text: '--- group deleted ---'
    }, true);
    const filePath = path.join(GROUP_DIR, `${req.params.id}.json`);
    if (fs.existsSync(filePath)) fs.unlinkSync(filePath);
    saveGroupHistory(req.params.id, [], { force: true });
    startNewChatLogSession(req.params.id, true);
    const historyFile = path.join(HISTORY_DIR, `group_${req.params.id}.json`);
    if (fs.existsSync(historyFile)) fs.unlinkSync(historyFile);
    res.json({ success: true });
  });

  app.get('/api/groups/:id/history', (req, res) => {
    if (!isSafeId(req.params.id)) return res.status(400).json({ error: 'Invalid ID' });
    res.json(getGroupHistory(req.params.id));
  });

  function handleGroupAppendMessages(req, res) {
    if (!isSafeId(req.params.id)) return res.status(400).json({ error: 'Invalid ID' });
    const groupId = req.params.id;
    const body = req.body || {};
    const incoming = Array.isArray(body.messages)
      ? body.messages
      : body.message
        ? [body.message]
        : [];
    if (incoming.length === 0) {
      return res.status(400).json({ error: 'messages array is required' });
    }
    const key = groupConversationKey(groupId);
    const before = countChatMessages(key);
    appendToGroupHistory(groupId, incoming);
    const after = countChatMessages(key);
    res.json({
      success: true,
      appended: Math.max(0, after - before),
      total: after
    });
  }

  app.post('/api/groups/:id/history/messages', handleGroupAppendMessages);
  app.post('/api/groups/:id/history/messages/beacon', handleGroupAppendMessages);

  // Remove companion replies that were saved twice (an older client re-posted
  // what /group-chat had already persisted). A row is a duplicate when the same
  // sender said the exact same thing within the previous 12 rows and 15 minutes.
  // User rows are never touched. dryRun:true only counts.
  app.post('/api/groups/:id/history/dedupe', (req, res) => {
    if (!isSafeId(req.params.id)) return res.status(400).json({ error: 'Invalid ID' });
    const groupId = req.params.id;
    if (!getGroup(groupId)) return res.status(404).json({ error: 'Group not found' });
    const dryRun = req.body?.dryRun === true;
    const history = getGroupHistory(groupId);
    const keep = [];
    const removed = [];
    const WINDOW = 12, MAX_GAP_MS = 15 * 60 * 1000;
    for (const m of history) {
      const text = typeof m?.text === 'string' ? m.text.trim() : '';
      const isCompanion = m && m.sender && m.sender !== 'user';
      let dup = false;
      if (isCompanion && text) {
        const t = Date.parse(m.timestamp || '') || 0;
        for (let i = keep.length - 1; i >= 0 && i >= keep.length - WINDOW; i--) {
          const k = keep[i];
          if (k.sender !== m.sender) continue;
          if ((typeof k.text === 'string' ? k.text.trim() : '') !== text) continue;
          const kt = Date.parse(k.timestamp || '') || 0;
          if (t && kt && Math.abs(t - kt) > MAX_GAP_MS) continue;
          dup = true; break;
        }
      }
      if (dup) removed.push(m); else keep.push(m);
    }
    if (!dryRun && removed.length) {
      saveGroupHistory(groupId, keep, { force: true });
    }
    res.json({ success: true, dryRun, removed: removed.length, total: keep.length, before: history.length });
  });

  app.post('/api/groups/:id/history/truncate', (req, res) => {
    if (!isSafeId(req.params.id)) return res.status(400).json({ error: 'Invalid ID' });
    const groupId = req.params.id;
    const fromIndex = Number.parseInt(req.body?.fromIndex, 10);
    if (!Number.isFinite(fromIndex) || fromIndex < 0) {
      return res.status(400).json({ error: 'fromIndex is required' });
    }
    const key = groupConversationKey(groupId);
    const total = countChatMessages(key);
    if (fromIndex >= total) {
      return res.status(400).json({ error: 'fromIndex out of range' });
    }
    if (!truncateChatMessagesFromIndex(key, fromIndex)) {
      return res.status(400).json({ error: 'Truncate failed' });
    }
    appendToChatLog(groupId, getGroupHistory(groupId), true);
    res.json({ success: true, total: countChatMessages(key) });
  });

  app.post('/api/groups/:id/history/insert', (req, res) => {
    if (!isSafeId(req.params.id)) return res.status(400).json({ error: 'Invalid ID' });
    const groupId = req.params.id;
    const index = Number.parseInt(req.body?.index, 10);
    const incoming = Array.isArray(req.body.messages)
      ? req.body.messages
      : req.body.message
        ? [req.body.message]
        : [];
    if (!Number.isFinite(index) || index < 0) {
      return res.status(400).json({ error: 'index is required' });
    }
    if (incoming.length === 0) {
      return res.status(400).json({ error: 'messages array is required' });
    }
    const key = groupConversationKey(groupId);
    insertChatMessagesAtIndex(key, index, incoming);
    appendToChatLog(groupId, getGroupHistory(groupId), true);
    res.json({ success: true, total: countChatMessages(key) });
  });

  app.delete('/api/groups/:id/history/message', (req, res) => {
    if (!isSafeId(req.params.id)) return res.status(400).json({ error: 'Invalid ID' });
    const groupId = req.params.id;
    const index = Number.parseInt(req.body?.index, 10);
    const messages = getGroupHistory(groupId);
    if (!Number.isFinite(index) || index < 0 || index >= messages.length) {
      return res.status(400).json({ error: 'Invalid index' });
    }
    const deleted = messages[index];
    logChatEvent(groupId, 'message_deleted', {
      originalSender: deleted.sender,
      originalText: deleted.text,
      originalTimestamp: deleted.timestamp,
      deletedAtIndex: index
    }, true);
    const key = groupConversationKey(groupId);
    if (!deleteChatMessageAtIndex(key, index)) {
      return res.status(500).json({ error: 'Delete failed' });
    }
    appendToChatLog(groupId, getGroupHistory(groupId), true);
    res.json({ success: true, total: countChatMessages(key) });
  });

  app.put('/api/groups/:id/history/message', (req, res) => {
    if (!isSafeId(req.params.id)) return res.status(400).json({ error: 'Invalid ID' });
    const groupId = req.params.id;
    const index = Number.parseInt(req.body?.index, 10);
    const messages = getGroupHistory(groupId);
    if (!Number.isFinite(index) || index < 0 || index >= messages.length) {
      return res.status(400).json({ error: 'Invalid index' });
    }
    const original = messages[index];
    const patch = {};
    if (typeof req.body?.text === 'string') patch.text = req.body.text;
    if (Array.isArray(req.body?.reactions)) patch.reactions = req.body.reactions;
    if (req.body?.gifs !== undefined) patch.gifs = req.body.gifs;
    if (Object.keys(patch).length === 0) {
      return res.status(400).json({ error: 'No fields to update' });
    }
    if (patch.text != null) {
      logChatEvent(groupId, 'message_edited', {
        originalSender: original.sender,
        originalText: original.text,
        newText: patch.text,
        originalTimestamp: original.timestamp,
        editedAtIndex: index
      }, true);
    }
    const key = groupConversationKey(groupId);
    if (!updateChatMessageAtIndex(key, index, patch)) {
      return res.status(500).json({ error: 'Update failed' });
    }
    res.json({ success: true });
  });

  app.put('/api/groups/:id/history', (req, res) => {
    if (!isSafeId(req.params.id)) return res.status(400).json({ error: 'Invalid ID' });
    const messages = req.body.messages || [];
    if (!saveGroupHistory(req.params.id, messages)) {
      return res.status(500).json({ error: 'Failed to save group history' });
    }
    res.json({ success: true });
  });

  app.delete('/api/groups/:id/history', (req, res) => {
    if (!isSafeId(req.params.id)) return res.status(400).json({ error: 'Invalid ID' });
    startNewChatLogSession(req.params.id, true);
    saveGroupHistory(req.params.id, [], { force: true });
    res.json({ success: true });
  });

  app.post('/api/groups/:id/clear-chat', async (req, res) => {
    if (!isSafeId(req.params.id)) return res.status(400).json({ error: 'Invalid ID' });
    const group = getGroup(req.params.id);
    if (!group) return res.status(404).json({ error: 'Group not found' });
    const skipFlush = req.body?.skipFlush === true;
    const settings = getSettings();
    let flushed = false;
    let memoriesExtracted = null;

    if (!skipFlush && settings.memory?.tanevUrl) {
      const personaFlush = getPersona();
      const flushUserName = personaFlush && personaFlush.name && String(personaFlush.name).trim();
      const tanevUrl = ((process.env.TANEVAN_URL || '').trim() || settings.memory.tanevUrl).replace(/\/$/, '');
      let totalMemories = 0;
      for (const name of group.companions) {
        const flushLog = addLog({ type: 'tanevan-flush', companion: name, direction: 'outbound', summary: `Tanevan flush (group clear) → ${name}`, status: 'pending' });
        const flushT0 = Date.now();
        try {
          const flushBody = { companion: name.toLowerCase(), manual: true };
          if (flushUserName) flushBody.user_name = flushUserName;
          const flushRes = await fetch(`${tanevUrl}/flush`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(flushBody)
          });
          if (flushRes.ok) {
            flushed = true;
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
          console.log(`Tanevan flush skipped for ${name} (group clear):`, err.message);
        }
      }
      memoriesExtracted = totalMemories > 0 ? totalMemories : null;
    }

    startNewChatLogSession(req.params.id, true);
    saveGroupHistory(req.params.id, [], { force: true });
    res.json({ success: true, flushed, memoriesExtracted });
  });

  app.get('/api/groups/:id/export', (req, res) => {
    if (!isSafeId(req.params.id)) return res.status(400).json({ error: 'Invalid ID' });
    const persona = getPersona();
    const group = getGroup(req.params.id);
    if (!group) return res.status(404).json({ error: 'Group not found' });

    const history = getGroupHistory(req.params.id);

    const format = req.query.format || 'json';

    if (format === 'txt') {
      let text = `=== ${group.name} ===\n`;
      text += `Members: ${group.companions.join(', ')}\n`;
      text += `Exported: ${new Date().toISOString()}\n`;
      text += `Messages: ${history.length}\n`;
      text += '='.repeat(40) + '\n\n';
      for (const msg of history) {
        const time = msg.timestamp ? new Date(msg.timestamp).toLocaleString() : '';
        const sender = msg.sender === 'user' ? (persona.name || 'User') : msg.sender;
        text += `[${sender}]${time ? ' (' + time + ')' : ''}\n${msg.text}\n\n`;
      }
      res.setHeader('Content-Type', 'text/plain');
      res.setHeader('Content-Disposition', `attachment; filename="${group.name.replace(/[^a-zA-Z0-9]/g, '_')}_export.txt"`);
      return res.send(text);
    }

    if (format === 'markdown') {
      let md = `# ${group.name}\n\n`;
      md += `**Members:** ${group.companions.join(', ')}  \n`;
      md += `**Exported:** ${new Date().toISOString()}  \n`;
      md += `**Messages:** ${history.length}\n\n---\n\n`;
      for (const msg of history) {
        const time = msg.timestamp ? new Date(msg.timestamp).toLocaleString() : '';
        const sender = msg.sender === 'user' ? (persona.name || 'User') : msg.sender;
        md += `**${sender}**${time ? ' _(' + time + ')_' : ''}  \n${msg.text}\n\n`;
      }
      res.setHeader('Content-Type', 'text/markdown');
      res.setHeader('Content-Disposition', `attachment; filename="${group.name.replace(/[^a-zA-Z0-9]/g, '_')}_export.md"`);
      return res.send(md);
    }

    res.json({ group, history, avatar: null });
  });
}

module.exports = registerGroupRoutes;
