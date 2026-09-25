// © 2024-2026 Megan Neves and Tiara Young. All rights reserved.
// Love Refactored — https://github.com/Love-Encoded/Love-Refactored
// Licensed under the Love Refactored / Tanevan License. See LICENSE.md.

'use strict';

const readline = require('readline');

/**
 * @param {import('express').Application} app
 * @param {object} deps
 */
function registerHistoryChatRoutes(app, deps) {
  const {
    fs,
    path,
    CHAT_LOG_DIR,
    historyConversationKey,
    getChatHistory,
    saveChatHistory,
    appendToCompanionHistory,
    appendToChatLog,
    countChatMessages,
    updateChatMessageAtIndex,
    deleteChatMessageAtIndex,
    truncateChatMessagesFromIndex,
    startNewChatLogSession,
    logChatEvent,
    getChatLogKey,
    setChatLogHighWater,
    broadcastHistoryUpdated,
    loadChatLogEntries,
    loadChatLogSession,
    searchChatLog,
    listChatLogSessions,
    listChatLogConversations,
    countFavoritedByConversation
  } = deps;

  // === PERMANENT CHAT LOG ENDPOINTS ===
  const CHATLOG_MAX_TAIL = 50_000;
  const CHATLOG_MAX_PAGE = 10_000;
  const CHATLOG_DEFAULT_PAGE = 500;

  function countLinesInFileSync(filePath) {
    const fd = fs.openSync(filePath, 'r');
    const buf = Buffer.alloc(256 * 1024);
    let n = 0;
    try {
      let off = 0;
      for (;;) {
        const br = fs.readSync(fd, buf, 0, buf.length, off);
        if (br === 0) break;
        for (let i = 0; i < br; i++) if (buf[i] === 0x0a) n++;
        off += br;
      }
    } finally {
      fs.closeSync(fd);
    }
    return n;
  }

  function parseJsonlObject(line) {
    try {
      const o = JSON.parse(line);
      return o && typeof o === 'object' ? o : null;
    } catch {
      return null;
    }
  }

  function chatlogMessageMatchesFilters(msg, sessionFilter, hideEvents) {
    if (!msg) return false;
    if (sessionFilter && msg.session !== sessionFilter) return false;
    if (hideEvents && msg.type) return false;
    return true;
  }

  async function readChatLogStreaming(filePath, { tail, startLine, lineLimit, sessionFilter, hideEvents, countTotal = true }) {
    const useTail = Number.isFinite(tail) && tail > 0;
    const usePage = Number.isFinite(startLine) && startLine >= 0 && Number.isFinite(lineLimit) && lineLimit > 0;

    const rl = readline.createInterface({
      input: fs.createReadStream(filePath, { encoding: 'utf8' }),
      crlfDelay: Infinity
    });

    let linesRead = 0;
    let lineIndex = 0;
    let readToEof = true;
    const tailBuf = useTail ? [] : null;
    const pageOut = usePage ? [] : null;

    try {
      for await (const line of rl) {
        linesRead++;
        if (useTail) {
          tailBuf.push(line);
          if (tailBuf.length > tail) tailBuf.shift();
        } else if (usePage) {
          if (lineIndex >= startLine && pageOut.length < lineLimit) {
            pageOut.push(line);
          }
          lineIndex++;
          if (!countTotal && pageOut.length >= lineLimit) {
            readToEof = false;
            break;
          }
        }
      }
    } finally {
      rl.close();
    }

    const rawLines = useTail ? tailBuf : pageOut;
    const messages = [];
    for (const str of rawLines) {
      const msg = parseJsonlObject(str);
      if (!msg) continue;
      if (!chatlogMessageMatchesFilters(msg, sessionFilter, hideEvents)) continue;
      messages.push(msg);
    }

    const fullTotal = readToEof ? linesRead : null;
    let physicalRange = undefined;
    if (useTail && fullTotal != null && tail) {
      physicalRange = { start: Math.max(0, fullTotal - tail), end: fullTotal - 1 };
    } else if (usePage) {
      physicalRange = { start: startLine, end: startLine + (pageOut?.length || 0) - 1 };
    }

    return {
      messages,
      totalLines: fullTotal,
      linesReadPartial: readToEof ? null : linesRead,
      totalComplete: readToEof,
      startLine: usePage ? startLine : undefined,
      lineLimit: usePage ? lineLimit : undefined,
      tail: useTail ? tail : undefined,
      physicalRange
    };
  }

  async function scanChatLogSessionsStreaming(filePath) {
    const rl = readline.createInterface({
      input: fs.createReadStream(filePath, { encoding: 'utf8' }),
      crlfDelay: Infinity
    });
    const sessionMap = {};
    try {
      for await (const line of rl) {
        const m = parseJsonlObject(line);
        if (!m || !m.session) continue;
        if (!sessionMap[m.session]) {
          sessionMap[m.session] = { id: m.session, firstMessage: m.timestamp, lastMessage: m.timestamp, count: 0 };
        }
        sessionMap[m.session].lastMessage = m.timestamp;
        if (m.type !== 'session_start') sessionMap[m.session].count++;
      }
    } finally {
      rl.close();
    }
    return Object.values(sessionMap);
  }

  function isValidHistoryName(name) {
    const normalized = String(name ?? '').trim();
    if (!normalized) return false;
    const lowered = normalized.toLowerCase();
    return lowered !== 'null' && lowered !== 'undefined';
  }

  app.use('/api/history/:name', (req, res, next) => {
    if (!isValidHistoryName(req.params?.name)) {
      return res.status(400).json({ error: 'Invalid history target name' });
    }
    next();
  });

  app.get('/api/history/:name', (req, res) => {
    // Guest mode: return guest-specific history
    if (req.userRole === 'guest') {
      const guestDir = path.resolve(CHAT_LOG_DIR, '..', 'guest_history');
      if (!fs.existsSync(guestDir)) fs.mkdirSync(guestDir, { recursive: true });
      const safeName = req.params.name.toLowerCase().replace(/[^a-z0-9]/g, '_');
      const dateStr = new Date().toISOString().slice(0, 10);
      const guestFile = path.join(guestDir, `${safeName}_${req.session.username || 'guest'}_${dateStr}.json`);
      let msgs = [];
      try { msgs = JSON.parse(fs.readFileSync(guestFile, 'utf-8')); } catch(e) {}
      return res.json({ messages: msgs, total: msgs.length, startIndex: 0 });
    }
    const tailQ = req.query.tail;
    const startQ = req.query.start;
    const endQ = req.query.end;
    const tail = tailQ !== undefined && tailQ !== '' ? parseInt(tailQ, 10) : NaN;
    const start = startQ !== undefined && startQ !== '' ? parseInt(startQ, 10) : NaN;
    const end = endQ !== undefined && endQ !== '' ? parseInt(endQ, 10) : NaN;

    const all = getChatHistory(req.params.name);
    const total = all.length;

    const hasTail = Number.isFinite(tail) && tail > 0;
    const hasRange = Number.isFinite(start) && Number.isFinite(end) && start >= 0 && end > start;
    const MAX_TAIL = 20000;
    const MAX_RANGE = 20000;

    if (!hasTail && !hasRange) {
      return res.json(all);
    }
    if (hasTail && hasRange) {
      return res.status(400).json({ error: 'Use only one of tail or start/end' });
    }

    if (hasTail) {
      const n = Math.min(tail, MAX_TAIL, total);
      const startIndex = total - n;
      return res.json({ messages: all.slice(startIndex), total, startIndex });
    }

    if (end - start > MAX_RANGE) {
      return res.status(400).json({ error: `Maximum ${MAX_RANGE} messages per request` });
    }
    const startClamped = Math.max(0, start);
    const endClamped = Math.min(end, total);
    if (startClamped >= endClamped) {
      return res.json({ messages: [], total, startIndex: startClamped });
    }
    return res.json({
      messages: all.slice(startClamped, endClamped),
      total,
      startIndex: startClamped
    });
  });

  app.get('/api/chatlog/:name', (req, res) => {
    const isGroup = req.query.group === 'true';
    const sessionFilter = req.query.session || '';
    const hideEvents = req.query.hideEvents === 'true';
    let tail = parseInt(req.query.tail, 10);
    let startLine = parseInt(req.query.startLine, 10);
    let lineLimit = parseInt(req.query.lineLimit, 10);

    const hasTail = Number.isFinite(tail) && tail > 0;
    const hasPage = Number.isFinite(startLine) && startLine >= 0 && Number.isFinite(lineLimit) && lineLimit > 0;

    if (hasTail && hasPage) {
      return res.status(400).json({ error: 'Use either tail or startLine/lineLimit, not both.' });
    }

    if (hasTail) {
      tail = Math.min(tail, CHATLOG_MAX_TAIL);
    } else if (hasPage) {
      lineLimit = Math.min(lineLimit, CHATLOG_MAX_PAGE);
    } else if (Number.isFinite(startLine) && startLine >= 0) {
      lineLimit = Math.min(CHATLOG_DEFAULT_PAGE, CHATLOG_MAX_PAGE);
    } else {
      startLine = 0;
      lineLimit = Math.min(CHATLOG_DEFAULT_PAGE, CHATLOG_MAX_PAGE);
    }

    const usePage = !hasTail && Number.isFinite(startLine) && startLine >= 0 && Number.isFinite(lineLimit) && lineLimit > 0;

    try {
      const key = getChatLogKey(req.params.name, isGroup);
      const result = loadChatLogEntries(key, undefined, {
        tail: hasTail ? tail : 0,
        startLine: usePage ? startLine : -1,
        lineLimit: usePage ? lineLimit : 0,
        sessionFilter: sessionFilter || null,
        hideEvents
      });

      res.json({
        messages: result.messages,
        totalLines: result.totalLines,
        totalComplete: result.totalComplete,
        linesReadPartial: result.linesReadPartial,
        physicalRange: result.physicalRange,
        total: result.totalLines != null ? result.totalLines : undefined,
        startLine: result.startLine,
        lineLimit: result.lineLimit,
        tail: result.tail,
        hint:
          result.totalLines != null && result.totalLines > (result.messages?.length || 0)
            ? 'Large log: use ?tail=… for recent lines, or ?startLine=&lineLimit= to page by physical lines. append countTotal=false when paging after the first request. hideEvents only filters the returned rows; ranges are still physical lines.'
            : undefined
      });
    } catch (err) {
      res.status(500).json({ error: 'Failed to read chat log', detail: err.message });
    }
  });

  app.get('/api/chatlog/:name/sessions', (req, res) => {
    const isGroup = req.query.group === 'true';

    try {
      const key = getChatLogKey(req.params.name, isGroup);
      const sessions = listChatLogSessions(key);
      res.json({ sessions });
    } catch (err) {
      res.status(500).json({ error: 'Failed to read sessions', detail: err.message });
    }
  });

  app.get('/api/chatlog/:name/session/:sessionId', (req, res) => {
    const isGroup = req.query.group === 'true';
    const hideEvents = req.query.hideEvents === 'true';

    try {
      const key = getChatLogKey(req.params.name, isGroup);
      const result = loadChatLogSession(key, req.params.sessionId, undefined, { hideEvents });
      res.json(result);
    } catch (err) {
      res.status(500).json({ error: 'Failed to read session', detail: err.message });
    }
  });

  app.get('/api/chatlog-search', (req, res) => {
    const q = String(req.query.q || '').trim();
    if (!q) return res.status(400).json({ error: 'q is required' });
    const name = req.query.name ? String(req.query.name) : null;
    const isGroup = req.query.group === 'true';
    const limit = parseInt(req.query.limit, 10);

    try {
      const conversationKey = name ? getChatLogKey(name, isGroup) : null;
      const result = searchChatLog(q, {
        conversationKey,
        limit: Number.isFinite(limit) ? limit : 200
      });
      res.json(result);
    } catch (err) {
      res.status(500).json({ error: 'Search failed', detail: err.message });
    }
  });

  app.get('/api/chatlog', (req, res) => {
    try {
      res.json({ logs: listChatLogConversations() });
    } catch (err) {
      res.status(500).json({ error: 'Failed to read chat logs', detail: err.message });
    }
  });

  function handleAppendMessages(req, res) {
    if (req.userRole === 'guest') {
      const guestDir = path.resolve(CHAT_LOG_DIR, '..', 'guest_history');
      if (!fs.existsSync(guestDir)) fs.mkdirSync(guestDir, { recursive: true });
      const safeName = req.params.name.toLowerCase().replace(/[^a-z0-9]/g, '_');
      const dateStr = new Date().toISOString().slice(0, 10);
      const guestFile = path.join(guestDir, `${safeName}_${req.session.username || 'guest'}_${dateStr}.json`);
      const body = req.body || {};
      const incoming = Array.isArray(body.messages) ? body.messages : [];
      let existing = [];
      try {
        existing = JSON.parse(fs.readFileSync(guestFile, 'utf-8'));
      } catch (_e) { /* new guest file */ }
      if (!Array.isArray(existing)) existing = [];
      try {
        fs.writeFileSync(guestFile, JSON.stringify(existing.concat(incoming), null, 2));
      } catch (_e) { /* best effort */ }
      return res.json({ success: true, appended: incoming.length });
    }

    const body = req.body || {};
    const incoming = Array.isArray(body.messages)
      ? body.messages
      : body.message
        ? [body.message]
        : [];
    if (incoming.length === 0) {
      return res.status(400).json({ error: 'messages array is required' });
    }

    const name = req.params.name;
    const key = historyConversationKey(name);
    const before = countChatMessages(key);
    appendToCompanionHistory(name, incoming);
    const after = countChatMessages(key);
    res.json({
      success: true,
      appended: Math.max(0, after - before),
      total: after
    });
  }

  app.post('/api/history/:name/messages', handleAppendMessages);
  app.post('/api/history/:name/messages/beacon', handleAppendMessages);

  app.post('/api/history/:name/truncate', (req, res) => {
    if (req.userRole === 'guest') {
      return res.status(403).json({ error: 'Not available in guest mode' });
    }
    const name = req.params.name;
    const fromIndex = Number.parseInt(req.body?.fromIndex, 10);
    if (!Number.isFinite(fromIndex) || fromIndex < 0) {
      return res.status(400).json({ error: 'fromIndex is required' });
    }
    const key = historyConversationKey(name);
    const total = countChatMessages(key);
    if (fromIndex >= total) {
      return res.status(400).json({ error: 'fromIndex out of range' });
    }
    if (!truncateChatMessagesFromIndex(key, fromIndex)) {
      return res.status(400).json({ error: 'Truncate failed' });
    }
    appendToChatLog(name, getChatHistory(name), false);
    broadcastHistoryUpdated(name, countChatMessages(key));
    res.json({ success: true, total: countChatMessages(key) });
  });

  app.put('/api/history/:name', (req, res) => {
    // Guest mode: save to guest history file (only writer — no server-side race)
    if (req.userRole === 'guest') {
      const guestDir = path.resolve(CHAT_LOG_DIR, '..', 'guest_history');
      if (!fs.existsSync(guestDir)) fs.mkdirSync(guestDir, { recursive: true });
      const safeName = req.params.name.toLowerCase().replace(/[^a-z0-9]/g, '_');
      const dateStr = new Date().toISOString().slice(0, 10);
      const guestFile = path.join(guestDir, `${safeName}_${req.session.username || 'guest'}_${dateStr}.json`);
      const body = req.body || {};
      const incoming = Array.isArray(body.messages) ? body.messages : [];
      try { fs.writeFileSync(guestFile, JSON.stringify(incoming, null, 2)); } catch(e) {}
      return res.json({ success: true });
    }
    const body = req.body || {};
    const incoming = Array.isArray(body.messages) ? body.messages : [];
    const parsedStartIndex = Number.parseInt(body.startIndex, 10);
    const startIndex = Number.isFinite(parsedStartIndex) ? Math.max(0, parsedStartIndex) : 0;
    const existing = getChatHistory(req.params.name);
    let nextMessages;
    if (startIndex > 0) {
      const prefix = existing.slice(0, Math.min(startIndex, existing.length));
      nextMessages = prefix.concat(incoming);
    } else {
      nextMessages = incoming;
    }
    const saved = saveChatHistory(req.params.name, nextMessages);
    if (!saved) {
      console.warn(`⚠️ history save rejected for ${req.params.name}: attempted ${nextMessages.length}, existing ${existing.length}`);
      return res.status(409).json({
        error: 'history_shrink_blocked',
        message: 'Save blocked to prevent data loss. Please reload history and retry.',
        companion: req.params.name,
        existingCount: existing.length,
        attemptedCount: nextMessages.length,
        startIndex
      });
    }
    res.json({ success: true });
  });

  app.post('/api/history/:name/beacon', (req, res) => {
    // Guest mode: save to guest history file
    if (req.userRole === 'guest') {
      const guestDir = path.resolve(CHAT_LOG_DIR, '..', 'guest_history');
      if (!fs.existsSync(guestDir)) fs.mkdirSync(guestDir, { recursive: true });
      const safeName = req.params.name.toLowerCase().replace(/[^a-z0-9]/g, '_');
      const dateStr = new Date().toISOString().slice(0, 10);
      const guestFile = path.join(guestDir, `${safeName}_${req.session.username || 'guest'}_${dateStr}.json`);
      const body = req.body || {};
      const incoming = Array.isArray(body.messages) ? body.messages : [];
      try { fs.writeFileSync(guestFile, JSON.stringify(incoming, null, 2)); } catch(e) {}
      return res.json({ success: true });
    }
    const body = req.body || {};
    const incoming = Array.isArray(body.messages) ? body.messages : [];
    const parsedStartIndex = Number.parseInt(body.startIndex, 10);
    const startIndex = Number.isFinite(parsedStartIndex) ? Math.max(0, parsedStartIndex) : 0;
    const existing = getChatHistory(req.params.name);
    let nextMessages;
    if (startIndex > 0) {
      const prefix = existing.slice(0, Math.min(startIndex, existing.length));
      nextMessages = prefix.concat(incoming);
    } else {
      nextMessages = incoming;
    }
    const saved = saveChatHistory(req.params.name, nextMessages);
    if (!saved) {
      console.warn(`⚠️ beacon history save rejected for ${req.params.name}: attempted ${nextMessages.length}, existing ${existing.length}`);
      return res.status(409).json({
        error: 'history_shrink_blocked',
        message: 'Save blocked to prevent data loss. Please reload history and retry.',
        companion: req.params.name,
        existingCount: existing.length,
        attemptedCount: nextMessages.length,
        startIndex
      });
    }
    res.json({ success: true });
  });

  app.delete('/api/history/:name', (req, res) => {
    startNewChatLogSession(req.params.name);
    saveChatHistory(req.params.name, [], { force: true });
    res.json({ success: true });
  });

  app.delete('/api/history/:name/message', (req, res) => {
    const name = req.params.name;
    const index = Number.parseInt(req.body?.index, 10);
    const messages = getChatHistory(name);
    if (!Number.isFinite(index) || index < 0 || index >= messages.length) {
      return res.status(400).json({ error: 'Invalid index' });
    }
    const deleted = messages[index];
    logChatEvent(name, 'message_deleted', {
      originalSender: deleted.sender,
      originalText: deleted.text,
      originalTimestamp: deleted.timestamp,
      deletedAtIndex: index
    });
    const key = historyConversationKey(name);
    if (!deleteChatMessageAtIndex(key, index)) {
      return res.status(500).json({ error: 'Delete failed' });
    }
    appendToChatLog(name, getChatHistory(name), false);
    broadcastHistoryUpdated(name, countChatMessages(key));
    res.json({ success: true, total: countChatMessages(key) });
  });

  // All favorited messages across the full conversation, each with its absolute
  // index so the client can unfavorite without loading the whole history.
  app.get('/api/history/:name/favorites', (req, res) => {
    const messages = getChatHistory(req.params.name);
    const favorites = [];
    messages.forEach((m, i) => {
      if (m && m.favorited) favorites.push({ ...m, index: i });
    });
    res.json({ favorites, total: messages.length });
  });

  // Favorite counts per conversation key — feeds the heart badges without
  // needing every companion's history loaded client-side.
  app.get('/api/favorites/counts', (req, res) => {
    res.json({ counts: countFavoritedByConversation() });
  });

  app.put('/api/history/:name/message', (req, res) => {
    const name = req.params.name;
    const index = Number.parseInt(req.body?.index, 10);
    const text = typeof req.body?.text === 'string' ? req.body.text : null;
    const favorited = typeof req.body?.favorited === 'boolean' ? req.body.favorited : undefined;
    const messages = getChatHistory(name);
    if (!Number.isFinite(index) || index < 0 || index >= messages.length) {
      return res.status(400).json({ error: 'Invalid index' });
    }
    if (text == null && favorited === undefined) {
      return res.status(400).json({ error: 'text or favorited is required' });
    }
    const original = messages[index];
    const patch = {};
    if (text != null) {
      logChatEvent(name, 'message_edited', {
        originalSender: original.sender,
        originalText: original.text,
        newText: text,
        originalTimestamp: original.timestamp,
        editedAtIndex: index
      });
      patch.text = text;
      patch.gifs = {};
    }
    if (favorited !== undefined) {
      patch.favorited = favorited;
    }
    const key = historyConversationKey(name);
    if (!updateChatMessageAtIndex(key, index, patch)) {
      return res.status(500).json({ error: 'Edit failed' });
    }
    broadcastHistoryUpdated(name, countChatMessages(key));
    res.json({ success: true });
  });
}

module.exports = registerHistoryChatRoutes;
