// © 2024-2026 Megan Neves and Tiara Young. All rights reserved.
// Love Refactored — https://github.com/Love-Encoded/Love-Refactored
// Licensed under the Love Refactored / Tanevan License. See LICENSE.md.

'use strict';

/**
 * @param {import('express').Application} app
 * @param {object} deps
 */
function registerLogsSettingsRoutes(app, deps) {
  const {
    apiLogs,
    apiPayloads,
    sseClients,
    clearDebugLogBufferPersisted,
    getDebugLogArchiveText,
    getSettings,
    saveSettings,
    mergeSettingsPayload,
    DATA_DIR,
    runChatDbBackup,
    listChatDbBackups,
    getChatBackupSettings,
    rescheduleChatDbBackup,
    invalidateWeatherCache,
    runTanevanBackup,
    listTanevanBackups,
    getTanevanBaseUrl
  } = deps;

  // Redact secret-looking values before sending settings to the browser
const SECRET_KEY_RE = /key|secret|token|password|sid|credential/i;
function redactSettings(value) {
  if (Array.isArray(value)) return value.map(redactSettings);
  if (value && typeof value === 'object') {
    const out = {};
    for (const [k, v] of Object.entries(value)) {
      out[k] = SECRET_KEY_RE.test(k) && typeof v === 'string' && v
        ? '••••'
        : redactSettings(v);
    }
    return out;
  }
  return value;
}
// When the browser sends back '••••' placeholders, swap in the real saved
// value so masked keys never overwrite the actual secrets on disk.
function restoreMaskedValues(incoming, current) {
  if (Array.isArray(incoming)) {
    return incoming.map((v, i) => restoreMaskedValues(v, current?.[i]));
  }
  if (incoming && typeof incoming === 'object') {
    const out = {};
    for (const [k, v] of Object.entries(incoming)) {
      out[k] = v === '••••' ? (current?.[k] ?? '') : restoreMaskedValues(v, current?.[k]);
    }
    return out;
  }
  return incoming;
}

  app.get('/api/logs', (req, res) => res.json(apiLogs));

  app.get('/api/logs/:id/payload', (req, res) => {
    const id = parseInt(req.params.id);
    const payload = apiPayloads[id];
    if (!payload) return res.status(404).json({ error: 'No payload found for this log entry' });
    res.json(payload);
  });

  app.get('/api/logs/archive', (req, res) => {
    const archive = typeof getDebugLogArchiveText === 'function' ? getDebugLogArchiveText() : '';
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    res.setHeader('Content-Type', 'application/x-ndjson; charset=utf-8');
    res.setHeader('Content-Disposition', `attachment; filename="debug-log-archive-${stamp}.jsonl"`);
    res.send(archive || '');
  });

  app.delete('/api/logs', (req, res) => {
    if (typeof clearDebugLogBufferPersisted === 'function') {
      clearDebugLogBufferPersisted();
    } else {
      apiLogs.length = 0;
      Object.keys(apiPayloads).forEach(k => delete apiPayloads[k]);
    }
    res.json({ ok: true });
  });

  app.get('/api/logs/stream', (req, res) => {
    res.setHeader('Content-Type', 'text/event-stream');
    res.setHeader('Cache-Control', 'no-cache');
    res.setHeader('Connection', 'keep-alive');
    res.flushHeaders();
    sseClients.add(res);
    res.write(`data: ${JSON.stringify({ _init: true, logs: apiLogs })}\n\n`);
    req.on('close', () => sseClients.delete(res));
  });

  app.get('/api/settings', (req, res) => {
    res.json(redactSettings(getSettings()));
  });

  /* serialize writes so two overlapping PUTs (provider click + key paste)
     cannot each read the old file and the slower one drop the new key */
  let settingsPut = Promise.resolve();
  app.put('/api/settings', (req, res) => {
    const job = settingsPut.then(() => {
    const current = getSettings();
    const incoming = restoreMaskedValues(req.body, current);
    const updated = mergeSettingsPayload(current, incoming);
    const prevW = current.weather || {};
    const nextW = updated.weather || {};
    if (
      String(prevW.latitude ?? '') !== String(nextW.latitude ?? '') ||
      String(prevW.longitude ?? '') !== String(nextW.longitude ?? '') ||
      !!prevW.enabled !== !!nextW.enabled
    ) {
      if (typeof invalidateWeatherCache === 'function') invalidateWeatherCache();
    }
    saveSettings(updated);
    if (typeof rescheduleChatDbBackup === 'function') {
      rescheduleChatDbBackup();
    }
    res.json(redactSettings(updated));
    }).catch((err) => {
      if (!res.headersSent) res.status(500).json({ error: 'Settings not saved', detail: String(err && err.message || err) });
    });
    settingsPut = job.catch(() => {});
  });

  app.get('/api/backups', (req, res) => {
    if (typeof listChatDbBackups !== 'function' || !DATA_DIR) {
      return res.status(503).json({ error: 'Backups not configured' });
    }
    const cfg = typeof getChatBackupSettings === 'function' ? getChatBackupSettings() : {};
    const backups = listChatDbBackups(DATA_DIR).map((b) => ({
      name: b.name,
      size: b.size,
      mtime: b.mtime
    }));
    res.json({ backups, settings: cfg });
  });

  app.post('/api/backups/run', async (req, res) => {
    if (typeof runChatDbBackup !== 'function' || !DATA_DIR) {
      return res.status(503).json({ error: 'Backups not configured' });
    }
    try {
      const cfg = typeof getChatBackupSettings === 'function' ? getChatBackupSettings() : {};
      const result = await runChatDbBackup(DATA_DIR, { retentionCount: cfg.retentionCount });
      res.json({ success: true, backup: result });
    } catch (err) {
      res.status(500).json({ error: 'Backup failed', detail: err.message });
    }
  });

  // --- Tanevan memory backups ---

  app.get('/api/backups/memory', async (req, res) => {
    if (typeof listTanevanBackups !== 'function' || typeof getTanevanBaseUrl !== 'function') {
      return res.status(503).json({ error: 'Memory backups not configured' });
    }
    try {
      const backups = await listTanevanBackups(getTanevanBaseUrl());
      res.json({ backups });
    } catch (err) {
      res.json({ backups: [], error: err.message });
    }
  });

  app.post('/api/backups/memory/run', async (req, res) => {
    if (typeof runTanevanBackup !== 'function' || typeof getTanevanBaseUrl !== 'function') {
      return res.status(503).json({ error: 'Memory backups not configured' });
    }
    try {
      const cfg = typeof getChatBackupSettings === 'function' ? getChatBackupSettings() : {};
      const result = await runTanevanBackup(getTanevanBaseUrl(), { retentionCount: cfg.retentionCount });
      res.json({ success: true, backup: result });
    } catch (err) {
      res.status(500).json({ error: 'Memory backup failed', detail: err.message });
    }
  });
}

module.exports = registerLogsSettingsRoutes;
