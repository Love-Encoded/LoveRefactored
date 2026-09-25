// © 2024-2026 Megan Neves and Tiara Young. All rights reserved.
// Love Refactored — https://github.com/Love-Encoded/Love-Refactored
// Licensed under the Love Refactored / Tanevan License. See LICENSE.md.

'use strict';

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

/**
 * Telegram management — everything that used to be env vars + pm2 restart, from the UI.
 * The bridge itself (the /telegram/:companion route) lives in server.js before auth; these
 * routes sit behind auth like every other /api/* route.
 *
 * Delivery is by POLLING (telegram-poller.js, launched by start.sh): the poller fetches
 * updates from Telegram and posts them to the local route with the secret header. No
 * public URL, no HTTPS, no inbound port. Webhook registration was removed 2026-09-25 —
 * polling and webhooks are mutually exclusive per bot and the poller clears any webhook
 * it finds.
 *
 * @param {import('express').Application} app
 * @param {object} deps
 */
function registerTelegramRoutes(app, deps) {
  const {
    getSettings,
    saveSettings,
    getTelegramConfig,
    telegramTokenFor,
    telegramCompanionExists,
    telegramSend,
    getLastUnknown,
    clearLastUnknown
  } = deps;

  const TG = 'https://api.telegram.org/bot';
  const mask = (t) => (t ? `${String(t).slice(0, 4)}…${String(t).slice(-4)}` : '');

  async function tg(token, method, body) {
    const r = await fetch(`${TG}${token}/${method}`, {
      method: body ? 'POST' : 'GET',
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined
    });
    const data = await r.json().catch(() => ({}));
    if (!data.ok) throw new Error(data.description || `Telegram ${r.status}`);
    return data.result;
  }

  function saveTelegram(mutate) {
    const settings = getSettings();
    const cur = (settings.telegram && typeof settings.telegram === 'object') ? settings.telegram : {};
    const next = { ...cur, bots: { ...(cur.bots || {}) } };
    mutate(next);
    settings.telegram = next;
    saveSettings(settings);
    return next;
  }

  const LR_HOME = process.env.LR_HOME || path.resolve(__dirname, '..');
  const POLL_STATUS = path.join(LR_HOME, 'data', 'telegram-poll-status.json');

  // Heartbeat written by telegram-poller.js. Absent or stale => the poller is not running.
  function pollerStatus() {
    let raw = null;
    try { raw = JSON.parse(fs.readFileSync(POLL_STATUS, 'utf8')); } catch (e) { return { running: false, reason: 'no heartbeat file — poller never started', bots: {} }; }
    const age = raw.lastTickAt ? (Date.now() - Date.parse(raw.lastTickAt)) / 1000 : Infinity;
    let alive = false;
    if (raw.pid) { try { process.kill(raw.pid, 0); alive = true; } catch (e) { alive = false; } }
    const running = alive && age < 120;
    return {
      running,
      reason: running ? null : (!alive ? `poller process (pid ${raw.pid}) is not running — restart with ./start.sh` : `poller last heartbeat ${Math.round(age)}s ago`),
      pid: raw.pid || null,
      startedAt: raw.startedAt || null,
      lastTickAt: raw.lastTickAt || null,
      error: raw.error || null,
      bots: raw.bots || {}
    };
  }

  function ensureSecret() {
    const cfg = getTelegramConfig();
    if (cfg.webhookSecret) return { secret: cfg.webhookSecret, generated: false };
    const secret = crypto.randomBytes(24).toString('base64url');
    saveTelegram((t) => { t.webhookSecret = secret; });
    return { secret, generated: true };
  }

  // Which companions have a token from anywhere (settings or env).
  function knownBots(cfg) {
    const names = new Set(Object.keys(cfg.bots || {}));
    for (const k of Object.keys(process.env)) {
      if (k.startsWith('TELEGRAM_BOT_TOKEN_') && process.env[k]) names.add(k.slice('TELEGRAM_BOT_TOKEN_'.length));
    }
    if (process.env.TELEGRAM_BOT_TOKEN && cfg.defaultCompanion) names.add(cfg.defaultCompanion);
    return [...names];
  }

  // Resolve an env-derived name (NOVA) back to the real companion name (Nova) when possible.
  function prettyName(name) {
    if (telegramCompanionExists(name)) return name;
    const cap = name.charAt(0).toUpperCase() + name.slice(1).toLowerCase();
    if (telegramCompanionExists(cap)) return cap;
    return name;
  }

  app.get('/api/telegram/status', async (req, res) => {
    const cfg = getTelegramConfig();
    const live = req.query.live === '1';
    const poller = pollerStatus();
    const bots = [];
    for (const raw of knownBots(cfg)) {
      const name = prettyName(raw);
      const token = telegramTokenFor(name) || telegramTokenFor(raw);
      const fromSettings = !!(cfg.bots[name] && cfg.bots[name].token) || !!(cfg.bots[raw] && cfg.bots[raw].token);
      const ps = poller.bots[name] || poller.bots[raw] || null;
      const row = {
        companion: name,
        exists: telegramCompanionExists(name),
        tokenSet: !!token,
        tokenMasked: mask(token),
        source: fromSettings ? 'settings' : 'env',
        username: cfg.bots[name] && cfg.bots[name].username || null,
        // polling state from the poller heartbeat (null = poller has not picked this bot up)
        poll: ps ? { since: ps.since || null, lastUpdateAt: ps.lastUpdateAt || null, lastError: ps.lastError || null, lastErrorAt: ps.lastErrorAt || null, dropped: ps.dropped || 0 } : null
      };
      if (live && token) {
        try { row.username = (await tg(token, 'getMe')).username || null; }
        catch (e) { row.error = e.message; }
      }
      bots.push(row);
    }
    res.json({
      mode: 'polling',
      poller: { running: poller.running, reason: poller.reason, pid: poller.pid, startedAt: poller.startedAt, lastTickAt: poller.lastTickAt, error: poller.error },
      webhookSecretSet: !!cfg.webhookSecret,
      webhookSecretSource: (getSettings().telegram || {}).webhookSecret ? 'settings' : (process.env.TELEGRAM_WEBHOOK_SECRET ? 'env' : null),
      allowedChatId: cfg.allowedChatId,
      allowedChatIdSource: (getSettings().telegram || {}).allowedChatId ? 'settings' : (process.env.TELEGRAM_ALLOWED_CHAT_ID ? 'env' : null),
      defaultCompanion: cfg.defaultCompanion,
      lastUnknown: getLastUnknown(),
      bots
    });
  });

  // Global fields. '••••' (the redaction placeholder) means "leave as is".
  app.put('/api/telegram', (req, res) => {
    const b = req.body || {};
    const next = saveTelegram((t) => {
      if (typeof b.allowedChatId === 'string') t.allowedChatId = b.allowedChatId.trim();
      if (typeof b.defaultCompanion === 'string' && b.defaultCompanion.trim()) t.defaultCompanion = b.defaultCompanion.trim();
      if (typeof b.webhookSecret === 'string' && b.webhookSecret !== '••••') t.webhookSecret = b.webhookSecret.trim();
    });
    if (b.allowedChatId && getLastUnknown() && String(getLastUnknown().chatId) === String(next.allowedChatId)) clearLastUnknown();
    res.json({ ok: true, allowedChatId: next.allowedChatId || '', webhookSecretSet: !!(next.webhookSecret || process.env.TELEGRAM_WEBHOOK_SECRET) });
  });

  // A fresh webhook secret — Telegram allows 1–256 chars of [A-Za-z0-9_-].
  app.post('/api/telegram/secret', (req, res) => {
    const secret = crypto.randomBytes(24).toString('base64url');
    saveTelegram((t) => { t.webhookSecret = secret; });
    res.json({ ok: true, webhookSecret: secret, note: 'The poller picks the new secret up on its next message.' });
  });

  app.put('/api/telegram/bots/:companion', async (req, res) => {
    const name = String(req.params.companion || '').trim();
    const token = String((req.body && req.body.token) || '').trim();
    if (!name) return res.status(400).json({ error: 'companion required' });
    if (!telegramCompanionExists(name)) return res.status(404).json({ error: `No companion named "${name}"` });
    if (!token || token === '••••') return res.status(400).json({ error: 'A bot token is required' });
    if (!/^\d+:[A-Za-z0-9_-]{20,}$/.test(token)) return res.status(400).json({ error: 'That does not look like a Telegram bot token (expected 123456:ABC…)' });
    let username = null;
    try { username = (await tg(token, 'getMe')).username || null; }
    catch (e) { return res.status(400).json({ error: `Telegram rejected that token: ${e.message}` }); }
    const sec = ensureSecret(); // a bot with no secret would have every update rejected silently
    saveTelegram((t) => { t.bots[name] = { ...(t.bots[name] || {}), token, username, addedAt: new Date().toISOString() }; });
    res.json({ ok: true, companion: name, username, tokenMasked: mask(token), secretGenerated: sec.generated });
  });

  app.delete('/api/telegram/bots/:companion', async (req, res) => {
    const name = String(req.params.companion || '').trim();
    const cfg = getTelegramConfig();
    const token = telegramTokenFor(name);
    // Best effort: drop anything Telegram is still holding for this bot.
    if (token && req.query.unregister !== '0') { try { await tg(token, 'deleteWebhook', { drop_pending_updates: true }); } catch (e) { /* ignore */ } }
    saveTelegram((t) => { delete t.bots[name]; });
    const stillEnv = !!(process.env['TELEGRAM_BOT_TOKEN_' + name.toUpperCase().replace(/[^A-Z0-9]/g, '_')] || (name === cfg.defaultCompanion && process.env.TELEGRAM_BOT_TOKEN));
    res.json({ ok: true, companion: name, note: stillEnv ? 'Removed from settings; an env var still supplies a token for this companion.' : null });
  });

  // Send a line to the allowed chat from this bot — proves token + chat id in one go.
  app.post('/api/telegram/bots/:companion/test', async (req, res) => {
    const name = String(req.params.companion || '').trim();
    const cfg = getTelegramConfig();
    const token = telegramTokenFor(name);
    if (!token) return res.status(400).json({ error: `No bot token for ${name}` });
    if (!cfg.allowedChatId) return res.status(400).json({ error: 'No allowed chat id yet — message the bot once, then click "Use this chat".' });
    try {
      await tg(token, 'sendMessage', { chat_id: cfg.allowedChatId, text: `📱 ${name} is wired up. This came from Love Refactored.` });
      res.json({ ok: true });
    } catch (e) {
      res.status(502).json({ error: `Telegram: ${e.message}` });
    }
  });

  void telegramSend; // available for future use (broadcasts); the test route talks to Telegram directly
}

module.exports = registerTelegramRoutes;
