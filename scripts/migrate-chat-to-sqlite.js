#!/usr/bin/env node
// © 2024-2026 Megan Neves and Tiara Young. All rights reserved.
// Love Refactored — https://github.com/Love-Encoded/Love-Refactored
// One-time import: data/chat_history/*.json + data/chat_logs/*.jsonl → data/love.db

'use strict';

const fs = require('fs');
const path = require('path');

const {
  initChatDb,
  getChatDb,
  getChatDbPath,
  OWNER_GUEST_SESSION
} = require('../db/chat');
const {
  messageToRow,
  upsertChatState,
  insertChatLogEntry
} = require('../db/chat-storage');

function parseArgs(argv) {
  const dataDir = path.resolve(__dirname, '..', 'data');
  let dryRun = false;
  for (let i = 2; i < argv.length; i++) {
    if (argv[i] === '--data-dir' && argv[i + 1]) {
      i++;
      continue;
    }
    if (argv[i] === '--dry-run') dryRun = true;
  }
  const explicit = argv.indexOf('--data-dir');
  const resolvedDataDir =
    explicit >= 0 && argv[explicit + 1]
      ? path.resolve(argv[explicit + 1])
      : dataDir;
  return { dataDir: resolvedDataDir, dryRun };
}

function readJsonArray(filePath) {
  try {
    const raw = JSON.parse(fs.readFileSync(filePath, 'utf-8'));
    return Array.isArray(raw) ? raw : [];
  } catch (err) {
    console.warn(`  skip unreadable history ${filePath}: ${err.message}`);
    return null;
  }
}

function importHistoryFile(db, historyDir, fileName, stats) {
  if (!fileName.endsWith('.json')) return;
  if (fileName.endsWith('.bak') || fileName.endsWith('.tmp')) return;
  if (fileName === '_memory_tags.json') return;

  const conversationKey = fileName.replace(/\.json$/, '');
  const filePath = path.join(historyDir, fileName);
  const messages = readJsonArray(filePath);
  if (messages == null) return;

  stats.historyFiles++;

  const insert = db.prepare(
    `INSERT INTO chat_messages
      (conversation_key, guest_session, msg_id, seq, sender, text, timestamp, metadata_json)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
  );

  const tx = db.transaction(() => {
    db.prepare(
      'DELETE FROM chat_messages WHERE conversation_key = ? AND guest_session = ?'
    ).run(conversationKey, OWNER_GUEST_SESSION);

    messages.forEach((message, seq) => {
      const row = messageToRow(conversationKey, OWNER_GUEST_SESSION, message, seq);
      insert.run(
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

    upsertChatState(conversationKey, OWNER_GUEST_SESSION, {
      current_session: null,
      high_water: messages.length
    });
  });

  tx();
  stats.messageRows += messages.length;
  console.log(`  history ${conversationKey}: ${messages.length} messages`);
}

function importJsonlFile(db, chatLogDir, fileName, stats) {
  if (!fileName.endsWith('.jsonl')) return;
  const conversationKey = fileName.replace(/\.jsonl$/, '');
  const filePath = path.join(chatLogDir, fileName);
  const text = fs.readFileSync(filePath, 'utf-8');
  const lines = text.split('\n').filter(Boolean);

  stats.logFiles++;

  const tx = db.transaction(() => {
    db.prepare(
      'DELETE FROM chat_log WHERE conversation_key = ? AND guest_session = ?'
    ).run(conversationKey, OWNER_GUEST_SESSION);

    let lastSession = null;
    for (const line of lines) {
      let entry;
      try {
        entry = JSON.parse(line);
      } catch {
        stats.logParseErrors++;
        continue;
      }
      if (entry.session) lastSession = entry.session;
      insertChatLogEntry(conversationKey, entry, OWNER_GUEST_SESSION);
      stats.logRows++;
    }

    const existingState = db
      .prepare(
        'SELECT high_water FROM chat_state WHERE conversation_key = ? AND guest_session = ?'
      )
      .get(conversationKey, OWNER_GUEST_SESSION);
    const highWater = Number(existingState?.high_water) || 0;

    upsertChatState(conversationKey, OWNER_GUEST_SESSION, {
      current_session: lastSession,
      high_water: highWater
    });
  });

  tx();
  console.log(`  chat_log ${conversationKey}: ${lines.length} lines`);
}

function main() {
  const { dataDir, dryRun } = parseArgs(process.argv);
  const historyDir = path.join(dataDir, 'chat_history');
  const chatLogDir = path.join(dataDir, 'chat_logs');

  if (!fs.existsSync(historyDir)) {
    console.error(`Missing history dir: ${historyDir}`);
    process.exit(1);
  }

  console.log(`Migrating chat storage → SQLite`);
  console.log(`  data dir: ${dataDir}`);
  if (dryRun) {
    console.log('  (dry-run: counting only, no DB writes)');
  }

  const stats = {
    historyFiles: 0,
    messageRows: 0,
    logFiles: 0,
    logRows: 0,
    logParseErrors: 0
  };

  if (dryRun) {
    for (const f of fs.readdirSync(historyDir)) {
      if (!f.endsWith('.json') || f.endsWith('.bak') || f.endsWith('.tmp')) continue;
      if (f === '_memory_tags.json') continue;
      const messages = readJsonArray(path.join(historyDir, f));
      if (messages) {
        stats.historyFiles++;
        stats.messageRows += messages.length;
        console.log(`  would import history ${f.replace(/\.json$/, '')}: ${messages.length}`);
      }
    }
    if (fs.existsSync(chatLogDir)) {
      for (const f of fs.readdirSync(chatLogDir)) {
        if (!f.endsWith('.jsonl')) continue;
        const n = fs
          .readFileSync(path.join(chatLogDir, f), 'utf-8')
          .split('\n')
          .filter(Boolean).length;
        stats.logFiles++;
        stats.logRows += n;
        console.log(`  would import chat_log ${f.replace(/\.jsonl$/, '')}: ${n}`);
      }
    }
    console.log(JSON.stringify(stats, null, 2));
    return;
  }

  initChatDb(dataDir);
  const db = getChatDb();
  console.log(`  db: ${getChatDbPath()}`);

  const historyFiles = fs.readdirSync(historyDir).sort();
  for (const fileName of historyFiles) {
    importHistoryFile(db, historyDir, fileName, stats);
  }

  if (fs.existsSync(chatLogDir)) {
    const logFiles = fs.readdirSync(chatLogDir).sort();
    for (const fileName of logFiles) {
      importJsonlFile(db, chatLogDir, fileName, stats);
    }
  }

  const verify = {
    chat_messages: db.prepare('SELECT COUNT(*) AS n FROM chat_messages').get().n,
    chat_log: db.prepare('SELECT COUNT(*) AS n FROM chat_log').get().n,
    chat_state: db.prepare('SELECT COUNT(*) AS n FROM chat_state').get().n
  };

  console.log('Done.');
  console.log(JSON.stringify({ stats, verify }, null, 2));
}

main();
