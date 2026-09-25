// © 2024-2026 Megan Neves and Tiara Young. All rights reserved.
// Love Refactored — https://github.com/Love-Encoded/Love-Refactored
// Licensed under the Love Refactored / Tanevan License. See LICENSE.md.

'use strict';

const fs = require('fs');
const path = require('path');
const { backupChatDb } = require('./chat');

function getBackupsDir(dataDir) {
  return path.join(path.resolve(dataDir), 'backups');
}

function formatBackupFilename() {
  const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  return `love-${stamp}.db`;
}

function listChatDbBackups(dataDir) {
  const dir = getBackupsDir(dataDir);
  if (!fs.existsSync(dir)) return [];
  return fs
    .readdirSync(dir)
    .filter((f) => f.startsWith('love-') && f.endsWith('.db'))
    .map((f) => {
      const fp = path.join(dir, f);
      const st = fs.statSync(fp);
      return {
        name: f,
        path: fp,
        size: st.size,
        mtime: st.mtime.toISOString()
      };
    })
    .sort((a, b) => b.mtime.localeCompare(a.mtime));
}

function pruneOldBackups(dataDir, retentionCount = 7) {
  const keep = Math.max(1, Number(retentionCount) || 7);
  const backups = listChatDbBackups(dataDir);
  if (backups.length <= keep) return 0;
  let removed = 0;
  for (const backup of backups.slice(keep)) {
    try {
      fs.unlinkSync(backup.path);
      removed++;
    } catch (err) {
      console.warn(`Backup prune failed for ${backup.name}:`, err.message);
    }
  }
  return removed;
}

async function runChatDbBackup(dataDir, { retentionCount = 7 } = {}) {
  const backupsDir = getBackupsDir(dataDir);
  const destPath = path.join(backupsDir, formatBackupFilename());
  await backupChatDb(destPath);
  const pruned = pruneOldBackups(dataDir, retentionCount);
  const stat = fs.statSync(destPath);
  return {
    path: destPath,
    name: path.basename(destPath),
    size: stat.size,
    mtime: stat.mtime.toISOString(),
    pruned
  };
}

// ---------------------------------------------------------------------------
// Tanevan memory backup helpers (calls Tanevan proxy endpoints)
// ---------------------------------------------------------------------------

async function runTanevanBackup(tanevanBaseUrl, { retentionCount = 7 } = {}) {
  const runRes = await fetch(`${tanevanBaseUrl}/backup`, { method: 'POST' });
  if (!runRes.ok) throw new Error(`Tanevan backup failed: ${runRes.status}`);
  const result = await runRes.json();

  if (retentionCount > 0) {
    try {
      await fetch(`${tanevanBaseUrl}/backup/prune`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ retentionCount })
      });
    } catch (_e) { /* prune is best-effort */ }
  }

  return result;
}

async function listTanevanBackups(tanevanBaseUrl) {
  try {
    const res = await fetch(`${tanevanBaseUrl}/backup/list`);
    if (!res.ok) return [];
    const data = await res.json();
    return data.backups || [];
  } catch (_e) {
    return [];
  }
}

module.exports = {
  getBackupsDir,
  listChatDbBackups,
  pruneOldBackups,
  runChatDbBackup,
  runTanevanBackup,
  listTanevanBackups
};
