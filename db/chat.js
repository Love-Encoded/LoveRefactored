// © 2024-2026 Megan Neves and Tiara Young. All rights reserved.
// Love Refactored — https://github.com/Love-Encoded/Love-Refactored
// Licensed under the Love Refactored / Tanevan License. See LICENSE.md.

'use strict';

const fs = require('fs');
const path = require('path');
const Database = require('better-sqlite3');

/** Bump when schema migrations are added. */
const SCHEMA_VERSION = 5;

const OWNER_GUEST_SESSION = '';

let _db = null;
let _dbPath = null;

function normalizeGuestSession(guestSession) {
  if (guestSession == null || guestSession === '') return OWNER_GUEST_SESSION;
  return String(guestSession);
}

function runMigrations(db) {
  let version = db.pragma('user_version', { simple: true });
  if (version === 0) {
    db.transaction(() => {
      db.exec(`
        CREATE TABLE IF NOT EXISTS chat_messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          conversation_key TEXT NOT NULL,
          guest_session TEXT NOT NULL DEFAULT '',
          msg_id TEXT,
          seq INTEGER NOT NULL,
          sender TEXT,
          text TEXT,
          timestamp TEXT NOT NULL,
          metadata_json TEXT
        );

        CREATE TABLE IF NOT EXISTS chat_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          conversation_key TEXT NOT NULL,
          guest_session TEXT NOT NULL DEFAULT '',
          session TEXT,
          event_type TEXT,
          sender TEXT,
          text TEXT,
          timestamp TEXT NOT NULL,
          detail_json TEXT
        );

        CREATE TABLE IF NOT EXISTS chat_state (
          conversation_key TEXT NOT NULL,
          guest_session TEXT NOT NULL DEFAULT '',
          current_session TEXT,
          high_water INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (conversation_key, guest_session)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_messages_conv_guest_seq
          ON chat_messages (conversation_key, guest_session, seq);

        CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_messages_conv_guest_msg_id
          ON chat_messages (conversation_key, guest_session, msg_id)
          WHERE msg_id IS NOT NULL;

        CREATE INDEX IF NOT EXISTS idx_chat_messages_conv_guest_timestamp
          ON chat_messages (conversation_key, guest_session, timestamp);

        CREATE INDEX IF NOT EXISTS idx_chat_log_conv_guest_timestamp
          ON chat_log (conversation_key, guest_session, timestamp);
      `);
      db.pragma('user_version = 1');
    })();
    version = 1;
  }

  if (version === 1) {
    db.transaction(() => {
      db.exec(`
        CREATE TABLE IF NOT EXISTS wall_posts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          companion_key TEXT NOT NULL,
          image_path TEXT NOT NULL,
          caption TEXT,
          origin TEXT NOT NULL DEFAULT 'chat',
          pinned INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS wall_reactions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          post_id INTEGER NOT NULL,
          companion_key TEXT NOT NULL,
          type TEXT NOT NULL,
          comment_text TEXT,
          created_at TEXT NOT NULL DEFAULT (datetime('now')),
          FOREIGN KEY (post_id) REFERENCES wall_posts(id)
        );

        CREATE INDEX IF NOT EXISTS idx_wall_posts_created
          ON wall_posts (created_at);

        CREATE INDEX IF NOT EXISTS idx_wall_reactions_post
          ON wall_reactions (post_id);
      `);
      db.pragma('user_version = 2');
    })();
    version = 2;
  }

  if (version === 2) {
    db.transaction(() => {
      db.exec(`ALTER TABLE wall_posts ADD COLUMN scene TEXT;`);
      db.pragma('user_version = 3');
    })();
    version = 3;
  }

  if (version === 3) {
    // Wall pins grew from one slot to four: three the companions hold (pinned = 1,
    // oldest rolls off), one the human holds (pinned = 2). pinned_at orders them.
    db.transaction(() => {
      db.exec(`ALTER TABLE wall_posts ADD COLUMN pinned_at TEXT;`);
      db.exec(`UPDATE wall_posts SET pinned_at = created_at WHERE pinned = 1 AND pinned_at IS NULL;`);
      db.pragma('user_version = 4');
    })();
    version = 4;
  }

  if (version === 4) {
    // Who pinned it — a companion's name, or the human's.
    db.transaction(() => {
      db.exec(`ALTER TABLE wall_posts ADD COLUMN pinned_by TEXT;`);
      db.pragma('user_version = 5');
    })();
    version = 5;
  }

  if (version !== SCHEMA_VERSION) {
    throw new Error(
      `Unsupported chat DB schema version ${version} (expected ${SCHEMA_VERSION}).`
    );
  }
}

/**
 * Open (or create) data/love.db, apply pragmas, run migrations.
 * @param {string} dataDir — LR data directory (e.g. …/data)
 * @returns {import('better-sqlite3').Database}
 */
function initChatDb(dataDir) {
  if (_db) return _db;

  const dir = path.resolve(dataDir);
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });

  _dbPath = path.join(dir, 'love.db');
  const db = new Database(_dbPath);

  db.pragma('journal_mode = WAL');
  db.pragma('synchronous = NORMAL');
  db.pragma('foreign_keys = ON');
  db.pragma('busy_timeout = 5000');

  runMigrations(db);

  _db = db;
  return db;
}

function getChatDb() {
  if (!_db) {
    throw new Error('Chat database not initialized — call initChatDb() first.');
  }
  return _db;
}

function getChatDbPath() {
  return _dbPath;
}

/**
 * Hot backup via SQLite backup API (safe while server is running).
 * @param {string} destPath — full path for snapshot file
 */
async function backupChatDb(destPath) {
  const db = getChatDb();
  const destDir = path.dirname(destPath);
  if (!fs.existsSync(destDir)) fs.mkdirSync(destDir, { recursive: true });
  await db.backup(destPath);
}

/**
 * Cold compact snapshot (VACUUM INTO).
 * @param {string} destPath — must not exist yet
 */
function vacuumChatDbInto(destPath) {
  const db = getChatDb();
  const destDir = path.dirname(destPath);
  if (!fs.existsSync(destDir)) fs.mkdirSync(destDir, { recursive: true });
  if (fs.existsSync(destPath)) fs.unlinkSync(destPath);
  db.prepare('VACUUM INTO ?').run(destPath);
}

function closeChatDb() {
  if (_db) {
    try {
      _db.close();
    } catch (e) {
      console.warn('Chat DB close failed:', e.message);
    }
    _db = null;
    _dbPath = null;
  }
}

module.exports = {
  SCHEMA_VERSION,
  OWNER_GUEST_SESSION,
  normalizeGuestSession,
  initChatDb,
  getChatDb,
  getChatDbPath,
  backupChatDb,
  vacuumChatDbInto,
  closeChatDb
};
