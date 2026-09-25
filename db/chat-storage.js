// © 2024-2026 Megan Neves and Tiara Young. All rights reserved.
// Love Refactored — https://github.com/Love-Encoded/Love-Refactored
// Licensed under the Love Refactored / Tanevan License. See LICENSE.md.

'use strict';

const { getChatDb, normalizeGuestSession, OWNER_GUEST_SESSION } = require('./chat');

const MESSAGE_CORE_FIELDS = new Set(['text', 'sender', 'timestamp', 'msgId', 'msg_id']);

function extractMetadataJson(message) {
  if (!message || typeof message !== 'object') return null;
  const meta = {};
  for (const [key, value] of Object.entries(message)) {
    if (!MESSAGE_CORE_FIELDS.has(key)) meta[key] = value;
  }
  return Object.keys(meta).length > 0 ? JSON.stringify(meta) : null;
}

function rowToMessage(row) {
  let message = {};
  if (row.metadata_json) {
    try {
      const meta = JSON.parse(row.metadata_json);
      if (meta && typeof meta === 'object') message = { ...meta };
    } catch {
      /* ignore corrupt metadata */
    }
  }
  message.text = row.text ?? message.text ?? '';
  message.sender = row.sender ?? message.sender ?? 'unknown';
  if (row.timestamp != null) message.timestamp = row.timestamp;
  if (row.msg_id) message.msgId = row.msg_id;
  return message;
}

function messageToRow(conversationKey, guestSession, message, seq) {
  const gs = normalizeGuestSession(guestSession);
  const msgId = message.msgId || message.msg_id || null;
  const timestamp = message.timestamp || new Date().toISOString();
  return {
    conversation_key: conversationKey,
    guest_session: gs,
    msg_id: msgId,
    seq,
    sender: message.sender ?? 'unknown',
    text: message.text ?? '',
    timestamp,
    metadata_json: extractMetadataJson(message)
  };
}

function stampMissingTimestamps(messages) {
  if (!Array.isArray(messages)) return messages;
  const nowIso = new Date().toISOString();
  for (const message of messages) {
    if (message && typeof message === 'object' && !message.timestamp) {
      message.timestamp = nowIso;
    }
  }
  return messages;
}

function countChatMessages(conversationKey, guestSession = OWNER_GUEST_SESSION) {
  const gs = normalizeGuestSession(guestSession);
  const row = getChatDb()
    .prepare(
      'SELECT COUNT(*) AS n FROM chat_messages WHERE conversation_key = ? AND guest_session = ?'
    )
    .get(conversationKey, gs);
  return Number(row?.n) || 0;
}

function loadChatMessages(conversationKey, guestSession = OWNER_GUEST_SESSION) {
  const gs = normalizeGuestSession(guestSession);
  const rows = getChatDb()
    .prepare(
      `SELECT msg_id, seq, sender, text, timestamp, metadata_json
       FROM chat_messages
       WHERE conversation_key = ? AND guest_session = ?
       ORDER BY seq ASC`
    )
    .all(conversationKey, gs);
  return rows.map(rowToMessage);
}

function shouldBlockHistoryShrink(existingCount, incomingCount, { force = false } = {}) {
  if (force) return false;
  return existingCount > 10 && incomingCount < existingCount * 0.5;
}

function replaceChatMessages(
  conversationKey,
  messages,
  guestSession = OWNER_GUEST_SESSION
) {
  const gs = normalizeGuestSession(guestSession);
  const db = getChatDb();
  const stamped = stampMissingTimestamps(Array.isArray(messages) ? messages : []);

  const deleteStmt = db.prepare(
    'DELETE FROM chat_messages WHERE conversation_key = ? AND guest_session = ?'
  );
  const insertStmt = db.prepare(
    `INSERT INTO chat_messages
      (conversation_key, guest_session, msg_id, seq, sender, text, timestamp, metadata_json)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
  );

  const tx = db.transaction((rows) => {
    deleteStmt.run(conversationKey, gs);
    rows.forEach((message, seq) => {
      const row = messageToRow(conversationKey, gs, message, seq);
      insertStmt.run(
        row.conversation_key,
        row.guest_session,
        row.msg_id,
        row.seq,
        row.sender,
        row.text,
        row.timestamp,
        row.metadata_json
      );
    });
  });

  tx(stamped);
  return stamped;
}

function getChatState(conversationKey, guestSession = OWNER_GUEST_SESSION) {
  const gs = normalizeGuestSession(guestSession);
  const row = getChatDb()
    .prepare(
      `SELECT conversation_key, guest_session, current_session, high_water
       FROM chat_state
       WHERE conversation_key = ? AND guest_session = ?`
    )
    .get(conversationKey, gs);
  return {
    conversation_key: conversationKey,
    guest_session: gs,
    current_session: row?.current_session ?? null,
    high_water: Number(row?.high_water) || 0
  };
}

function upsertChatState(conversationKey, guestSession, patch = {}) {
  const gs = normalizeGuestSession(guestSession);
  const existing = getChatState(conversationKey, gs);
  const nextSession =
    patch.current_session !== undefined ? patch.current_session : existing.current_session;
  const nextHighWater =
    patch.high_water !== undefined ? patch.high_water : existing.high_water;

  getChatDb()
    .prepare(
      `INSERT INTO chat_state (conversation_key, guest_session, current_session, high_water)
       VALUES (?, ?, ?, ?)
       ON CONFLICT (conversation_key, guest_session) DO UPDATE SET
         current_session = excluded.current_session,
         high_water = excluded.high_water`
    )
    .run(conversationKey, gs, nextSession, nextHighWater);
}

function ensureChatSession(conversationKey, guestSession = OWNER_GUEST_SESSION) {
  const state = getChatState(conversationKey, guestSession);
  if (state.current_session) return state.current_session;
  const sessionId = new Date().toISOString();
  upsertChatState(conversationKey, guestSession, {
    current_session: sessionId,
    high_water: state.high_water
  });
  return sessionId;
}

function getSeqAtIndex(conversationKey, index, guestSession = OWNER_GUEST_SESSION) {
  const gs = normalizeGuestSession(guestSession);
  if (!Number.isInteger(index) || index < 0) return null;
  const row = getChatDb()
    .prepare(
      `SELECT seq FROM chat_messages
       WHERE conversation_key = ? AND guest_session = ?
       ORDER BY seq ASC
       LIMIT 1 OFFSET ?`
    )
    .get(conversationKey, gs, index);
  return row?.seq ?? null;
}

function appendChatMessages(
  conversationKey,
  messages,
  guestSession = OWNER_GUEST_SESSION
) {
  const gs = normalizeGuestSession(guestSession);
  const db = getChatDb();
  const incoming = stampMissingTimestamps(Array.isArray(messages) ? messages : []);
  if (incoming.length === 0) return [];

  const maxSeqRow = db
    .prepare(
      `SELECT COALESCE(MAX(seq), -1) AS m
       FROM chat_messages
       WHERE conversation_key = ? AND guest_session = ?`
    )
    .get(conversationKey, gs);
  let nextSeq = (Number(maxSeqRow?.m) ?? -1) + 1;

  const findByMsgId = db.prepare(
    `SELECT id FROM chat_messages
     WHERE conversation_key = ? AND guest_session = ? AND msg_id = ?`
  );
  const insertStmt = db.prepare(
    `INSERT INTO chat_messages
      (conversation_key, guest_session, msg_id, seq, sender, text, timestamp, metadata_json)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
  );

  const tailRowStmt = db.prepare(
    `SELECT sender, text FROM chat_messages
     WHERE conversation_key = ? AND guest_session = ?
     ORDER BY seq DESC LIMIT 1`
  );

  const appended = [];
  const tx = db.transaction((msgs) => {
    for (const message of msgs) {
      const msgId = message.msgId || message.msg_id || null;
      if (msgId) {
        const existing = findByMsgId.get(conversationKey, gs, msgId);
        if (existing) continue;
      }
      const tail = tailRowStmt.get(conversationKey, gs);
      if (
        tail &&
        tail.sender === (message.sender ?? 'unknown') &&
        tail.text === (message.text ?? '')
      ) {
        continue;
      }
      const row = messageToRow(conversationKey, gs, message, nextSeq);
      insertStmt.run(
        row.conversation_key,
        row.guest_session,
        row.msg_id,
        row.seq,
        row.sender,
        row.text,
        row.timestamp,
        row.metadata_json
      );
      appended.push(message);
      nextSeq++;
    }
  });
  tx(incoming);
  return appended;
}

function updateChatMessageAtIndex(
  conversationKey,
  index,
  patch,
  guestSession = OWNER_GUEST_SESSION
) {
  const gs = normalizeGuestSession(guestSession);
  const seq = getSeqAtIndex(conversationKey, index, gs);
  if (seq == null) return false;
  const rows = loadChatMessages(conversationKey, gs);
  const current = rows[index];
  if (!current) return false;
  const updated = { ...current, ...(patch && typeof patch === 'object' ? patch : {}) };
  const row = messageToRow(conversationKey, gs, updated, seq);
  getChatDb()
    .prepare(
      `UPDATE chat_messages
       SET sender = ?, text = ?, timestamp = ?, msg_id = ?, metadata_json = ?
       WHERE conversation_key = ? AND guest_session = ? AND seq = ?`
    )
    .run(
      row.sender,
      row.text,
      row.timestamp,
      row.msg_id,
      row.metadata_json,
      conversationKey,
      gs,
      seq
    );
  return true;
}

function countFavoritedByConversation(guestSession = OWNER_GUEST_SESSION) {
  const gs = normalizeGuestSession(guestSession);
  const rows = getChatDb()
    .prepare(
      `SELECT conversation_key, COUNT(*) AS n
       FROM chat_messages
       WHERE guest_session = ?
         AND json_extract(metadata_json, '$.favorited') = 1
       GROUP BY conversation_key`
    )
    .all(gs);
  const counts = {};
  for (const row of rows) counts[row.conversation_key] = Number(row.n) || 0;
  return counts;
}

function deleteChatMessageAtIndex(
  conversationKey,
  index,
  guestSession = OWNER_GUEST_SESSION
) {
  const gs = normalizeGuestSession(guestSession);
  const seq = getSeqAtIndex(conversationKey, index, gs);
  if (seq == null) return false;
  getChatDb()
    .prepare(
      'DELETE FROM chat_messages WHERE conversation_key = ? AND guest_session = ? AND seq = ?'
    )
    .run(conversationKey, gs, seq);
  return true;
}

function insertChatMessagesAtIndex(
  conversationKey,
  index,
  messages,
  guestSession = OWNER_GUEST_SESSION
) {
  const gs = normalizeGuestSession(guestSession);
  const all = loadChatMessages(conversationKey, gs);
  const stamped = stampMissingTimestamps(Array.isArray(messages) ? messages : []);
  if (stamped.length === 0) return [];
  const idx = Math.max(0, Math.min(Number(index) || 0, all.length));
  all.splice(idx, 0, ...stamped);
  replaceChatMessages(conversationKey, all, gs);
  return stamped;
}

function truncateChatMessagesFromIndex(
  conversationKey,
  fromIndex,
  guestSession = OWNER_GUEST_SESSION
) {
  const gs = normalizeGuestSession(guestSession);
  const seq = getSeqAtIndex(conversationKey, fromIndex, gs);
  if (seq == null) return false;
  getChatDb()
    .prepare(
      'DELETE FROM chat_messages WHERE conversation_key = ? AND guest_session = ? AND seq >= ?'
    )
    .run(conversationKey, gs, seq);
  return true;
}

function insertChatLogEntry(
  conversationKey,
  entry,
  guestSession = OWNER_GUEST_SESSION
) {
  const gs = normalizeGuestSession(guestSession);
  const timestamp = entry.timestamp || new Date().toISOString();
  const eventType = entry.type || entry.event_type || 'message';
  const detail = { ...entry };
  delete detail.sender;
  delete detail.text;
  delete detail.timestamp;
  delete detail.session;
  delete detail.type;
  delete detail.event_type;

  getChatDb()
    .prepare(
      `INSERT INTO chat_log
        (conversation_key, guest_session, session, event_type, sender, text, timestamp, detail_json)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
    )
    .run(
      conversationKey,
      gs,
      entry.session ?? null,
      eventType,
      entry.sender ?? null,
      entry.text ?? '',
      timestamp,
      Object.keys(detail).length > 0 ? JSON.stringify(detail) : null
    );
}

function countChatLogEntries(conversationKey, guestSession = OWNER_GUEST_SESSION) {
  const gs = normalizeGuestSession(guestSession);
  const row = getChatDb()
    .prepare('SELECT COUNT(*) AS c FROM chat_log WHERE conversation_key = ? AND guest_session = ?')
    .get(conversationKey, gs);
  return Number(row?.c) || 0;
}

function chatLogRowToRecord(row) {
  let record = {};
  if (row.detail_json) {
    try {
      const detail = JSON.parse(row.detail_json);
      if (detail && typeof detail === 'object') record = { ...detail };
    } catch { /* ignore corrupt detail */ }
  }
  record.sender = row.sender ?? 'unknown';
  record.text = row.text ?? '';
  record.timestamp = row.timestamp;
  if (row.session != null) record.session = row.session;
  if (row.event_type != null) record.type = row.event_type;
  return record;
}

function loadChatLogEntries(conversationKey, guestSession = OWNER_GUEST_SESSION, { tail = 0, startLine = -1, lineLimit = 0, sessionFilter = null, hideEvents = false } = {}) {
  const gs = normalizeGuestSession(guestSession);
  const db = getChatDb();
  const totalLines = countChatLogEntries(conversationKey, gs);
  const useTail = Number.isFinite(tail) && tail > 0;
  const usePage = Number.isFinite(startLine) && startLine >= 0 && Number.isFinite(lineLimit) && lineLimit > 0;

  let rows = [];
  let physicalRange;
  if (useTail) {
    rows = db.prepare(
      'SELECT session, event_type, sender, text, timestamp, detail_json FROM chat_log WHERE conversation_key = ? AND guest_session = ? ORDER BY id DESC LIMIT ?'
    ).all(conversationKey, gs, tail).reverse();
    physicalRange = { start: Math.max(0, totalLines - tail), end: totalLines - 1 };
  } else if (usePage) {
    rows = db.prepare(
      'SELECT session, event_type, sender, text, timestamp, detail_json FROM chat_log WHERE conversation_key = ? AND guest_session = ? ORDER BY id ASC LIMIT ? OFFSET ?'
    ).all(conversationKey, gs, lineLimit, startLine);
    physicalRange = { start: startLine, end: startLine + rows.length - 1 };
  }

  let messages = rows.map(chatLogRowToRecord);
  if (sessionFilter) messages = messages.filter(m => m.session === sessionFilter);
  if (hideEvents) messages = messages.filter(m => !m.type || m.type === 'message');

  return {
    messages,
    totalLines,
    totalComplete: true,
    linesReadPartial: null,
    startLine: usePage ? startLine : undefined,
    lineLimit: usePage ? lineLimit : undefined,
    tail: useTail ? tail : undefined,
    physicalRange
  };
}

function loadChatLogSession(conversationKey, session, guestSession = OWNER_GUEST_SESSION, { hideEvents = false } = {}) {
  const gs = normalizeGuestSession(guestSession);
  const rows = getChatDb().prepare(
    `SELECT session, event_type, sender, text, timestamp, detail_json
     FROM chat_log
     WHERE conversation_key = ? AND guest_session = ? AND session = ?
     ORDER BY id ASC`
  ).all(conversationKey, gs, session);

  let messages = rows.map(chatLogRowToRecord);
  if (hideEvents) messages = messages.filter(m => !m.type || m.type === 'message');

  return { messages, total: messages.length, session };
}

function listChatLogSessions(conversationKey, guestSession = OWNER_GUEST_SESSION) {
  const gs = normalizeGuestSession(guestSession);
  return getChatDb().prepare(
    `SELECT session AS id,
            MIN(timestamp) AS firstMessage,
            MAX(timestamp) AS lastMessage,
            SUM(CASE WHEN event_type != 'session_start' THEN 1 ELSE 0 END) AS count
     FROM chat_log
     WHERE conversation_key = ? AND guest_session = ? AND session IS NOT NULL
     GROUP BY session
     ORDER BY MIN(id) ASC`
  ).all(conversationKey, gs);
}

function searchChatLog(query, { conversationKey = null, guestSession = OWNER_GUEST_SESSION, limit = 200 } = {}) {
  const gs = normalizeGuestSession(guestSession);
  const raw = String(query || '').trim();
  if (!raw) return { results: [], total: 0, query: raw };

  const escaped = raw.replace(/[\\%_]/g, ch => '\\' + ch);
  const pattern = '%' + escaped + '%';
  const cappedLimit = Math.max(1, Math.min(Number(limit) || 200, 500));

  const params = [gs, pattern];
  let where = "guest_session = ? AND (event_type IS NULL OR event_type = 'message') AND text LIKE ? ESCAPE '\\'";
  if (conversationKey) {
    where += ' AND conversation_key = ?';
    params.push(conversationKey);
  }

  const rows = getChatDb().prepare(
    `SELECT conversation_key, session, sender, text, timestamp
     FROM chat_log
     WHERE ${where}
     ORDER BY id DESC
     LIMIT ?`
  ).all(...params, cappedLimit);

  return { results: rows, total: rows.length, query: raw };
}

function listChatLogConversations(guestSession = OWNER_GUEST_SESSION) {
  const gs = normalizeGuestSession(guestSession);
  return getChatDb().prepare(
    'SELECT conversation_key AS name, COUNT(*) AS messages FROM chat_log WHERE guest_session = ? GROUP BY conversation_key ORDER BY conversation_key ASC'
  ).all(gs).map(r => ({ name: r.name, isGroup: r.name.startsWith('group_'), messages: r.messages }));
}

function deleteConversationData(conversationKey, guestSession) {
  const db = getChatDb();
  const scoped = guestSession != null;
  const gs = scoped ? normalizeGuestSession(guestSession) : null;
  const deleteMessages = scoped
    ? db.prepare('DELETE FROM chat_messages WHERE conversation_key = ? AND guest_session = ?')
    : db.prepare('DELETE FROM chat_messages WHERE conversation_key = ?');
  const deleteLog = scoped
    ? db.prepare('DELETE FROM chat_log WHERE conversation_key = ? AND guest_session = ?')
    : db.prepare('DELETE FROM chat_log WHERE conversation_key = ?');
  const deleteState = scoped
    ? db.prepare('DELETE FROM chat_state WHERE conversation_key = ? AND guest_session = ?')
    : db.prepare('DELETE FROM chat_state WHERE conversation_key = ?');

  const tx = db.transaction(() => {
    if (scoped) {
      deleteMessages.run(conversationKey, gs);
      deleteLog.run(conversationKey, gs);
      deleteState.run(conversationKey, gs);
    } else {
      deleteMessages.run(conversationKey);
      deleteLog.run(conversationKey);
      deleteState.run(conversationKey);
    }
  });
  tx();
}

module.exports = {
  OWNER_GUEST_SESSION,
  extractMetadataJson,
  rowToMessage,
  messageToRow,
  stampMissingTimestamps,
  countChatMessages,
  loadChatMessages,
  shouldBlockHistoryShrink,
  replaceChatMessages,
  getSeqAtIndex,
  appendChatMessages,
  updateChatMessageAtIndex,
  deleteChatMessageAtIndex,
  truncateChatMessagesFromIndex,
  insertChatMessagesAtIndex,
  getChatState,
  upsertChatState,
  ensureChatSession,
  insertChatLogEntry,
  countChatLogEntries,
  loadChatLogEntries,
  loadChatLogSession,
  searchChatLog,
  listChatLogSessions,
  listChatLogConversations,
  deleteConversationData,
  countFavoritedByConversation
};
