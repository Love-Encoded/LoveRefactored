// © 2024-2026 Megan Neves and Tiara Young. All rights reserved.
// Love Refactored — https://github.com/Love-Encoded/Love-Refactored
// Licensed under the Love Refactored / Tanevan License. See LICENSE.md.

// telegram-poller.js — outbound-only Telegram bridge (no webhook, no public URL,
// no inbound port). Polls getUpdates for every companion bot in
// settings.telegram.bots and forwards each update to the app's
// /telegram/:companion route exactly as Telegram would have delivered it.
//
// Design (agreed 2026-09-20, ported to LR 2026-09-25):
//   - offsets persisted to disk  → an update is never replayed
//   - forward fails              → one retry after 5s, then a loud DROPPED log
//   - every error path sleeps    → no tight loops, no runaway API calls
//   - heartbeat file             → the Settings UI can show "listening" / the real error
//   - pidfile lock               → a second copy exits loudly (two pollers = Telegram 409s)
//   - bots added/removed in Settings are picked up within RECONCILE_MS, no restart

'use strict';

const fs = require('fs');
const path = require('path');

// NZ latency > Node's 250ms happy-eyeballs default; older Nodes lack the setter.
try { require('net').setDefaultAutoSelectFamilyAttemptTimeout(2000); } catch (_) { /* pre-20.x Node */ }

const LR_HOME = process.env.LR_HOME || __dirname;
const PORT = process.env.PORT || 3000;
const APP = `http://127.0.0.1:${PORT}`;
const DATA = path.join(LR_HOME, 'data');
const SETTINGS = path.join(DATA, 'settings.json');
const OFFSETS = path.join(DATA, 'telegram-poll-offsets.json');
const STATUS = path.join(DATA, 'telegram-poll-status.json');
const PIDFILE = path.join(DATA, 'telegram-poller.pid');

const RECONCILE_MS = 30000;   // how often we re-read settings for added/removed bots
const POLL_TIMEOUT_S = 25;    // Telegram long-poll hold time
const ERR_SLEEP_MS = 15000;   // after a getUpdates failure
const RETRY_MS = 5000;        // before the single forward retry

const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const now = () => new Date().toISOString();
const log = (m) => console.log(`[tg-poll] ${now()} ${m}`);
const warn = (m) => console.warn(`[tg-poll] ${now()} ${m}`);
const error = (m) => console.error(`[tg-poll] ${now()} ${m}`);

function readJson(file, fallback) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return fallback; }
}
function writeJson(file, obj) {
  const tmp = file + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(obj, null, 2));
  fs.renameSync(tmp, file);
}

function tgConfig() {
  const s = readJson(SETTINGS, {});
  const t = (s && s.telegram) || {};
  const bots = (t.bots && typeof t.bots === 'object') ? t.bots : {};
  return { secret: String(t.webhookSecret || '').trim(), bots };
}

// ---- offsets -----------------------------------------------------------
function readOffsets() { return readJson(OFFSETS, {}); }
function writeOffset(name, offset) {
  const all = readOffsets(); all[name] = offset;
  writeJson(OFFSETS, all);
}

// ---- heartbeat / status -----------------------------------------------
// Shape: { pid, startedAt, lastTickAt, error, bots: { [name]: {...} } }
const status = { pid: process.pid, startedAt: now(), lastTickAt: now(), error: null, bots: {} };
function botStatus(name) {
  if (!status.bots[name]) status.bots[name] = { since: now(), lastUpdateAt: null, lastError: null, lastErrorAt: null, dropped: 0, mode: 'polling' };
  return status.bots[name];
}
function flushStatus() {
  status.lastTickAt = now();
  try { writeJson(STATUS, status); } catch (e) { error(`could not write status file: ${e.message}`); }
}
function setBotError(name, msg) {
  const b = botStatus(name); b.lastError = msg; b.lastErrorAt = now(); flushStatus();
}
function clearBotError(name) {
  const b = botStatus(name); if (b.lastError) { b.lastError = null; b.lastErrorAt = null; flushStatus(); }
}

// ---- pidfile lock -------------------------------------------------------
function acquireLock() {
  fs.mkdirSync(DATA, { recursive: true });
  const existing = parseInt(fs.readFileSync(PIDFILE, 'utf8'), 10);
  if (existing && existing !== process.pid) {
    let alive = false;
    try { process.kill(existing, 0); alive = true; } catch { alive = false; }
    if (alive) {
      error(`another poller is already running (pid ${existing}). Two pollers make Telegram return 409 and drop updates. Exiting.`);
      process.exit(2);
    }
  }
  fs.writeFileSync(PIDFILE, String(process.pid));
}
// readFileSync throws if the pidfile is absent — wrap so a missing file means "no lock".
function acquireLockSafe() {
  try { acquireLock(); }
  catch (e) {
    if (e && e.code === 'ENOENT') { fs.mkdirSync(DATA, { recursive: true }); fs.writeFileSync(PIDFILE, String(process.pid)); }
    else throw e;
  }
}
function releaseLock() {
  try { if (parseInt(fs.readFileSync(PIDFILE, 'utf8'), 10) === process.pid) fs.unlinkSync(PIDFILE); } catch { /* fine */ }
}

// ---- forwarding ---------------------------------------------------------
async function forward(name, update, secret) {
  const url = `${APP}/telegram/${encodeURIComponent(name)}`;
  const attempt = async () => {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'x-telegram-bot-api-secret-token': secret },
      body: JSON.stringify(update)
    });
    if (!res.ok) throw new Error(`app responded ${res.status}`);
  };
  try { await attempt(); return true; }
  catch (e1) {
    warn(`${name}: forward failed (${e1.message}), retrying in ${RETRY_MS / 1000}s`);
    await sleep(RETRY_MS);
    try { await attempt(); return true; }
    catch (e2) {
      error(`${name}: DROPPED update ${update.update_id} after retry (${e2.message})`);
      const b = botStatus(name); b.dropped += 1;
      setBotError(name, `dropped an update: ${e2.message}`);
      return false; // dropped by design — offset still advances, no replay
    }
  }
}

// ---- per-bot loop -------------------------------------------------------
// `running` is a shared registry; a bot loop exits when its entry is removed.
const running = new Map(); // name -> { token, stop: false }

async function pollBot(name, ctl) {
  botStatus(name); flushStatus();

  // Polling and webhooks are mutually exclusive — clear any half-set webhook once.
  try {
    const r = await fetch(`https://api.telegram.org/bot${ctl.token}/deleteWebhook`, { method: 'POST' });
    const body = await r.json().catch(() => ({}));
    if (!body.ok) throw new Error(body.description || `status ${r.status}`);
    log(`${name}: webhook cleared, polling mode`);
  } catch (e) {
    warn(`${name}: deleteWebhook failed (${e.message}) — continuing`);
    setBotError(name, `could not clear webhook: ${e.message}`);
  }

  while (!ctl.stop) {
    const offset = readOffsets()[name] || 0;
    let updates;
    try {
      const res = await fetch(`https://api.telegram.org/bot${ctl.token}/getUpdates?timeout=${POLL_TIMEOUT_S}&offset=${offset}&allowed_updates=%5B%22message%22%5D`);
      const body = await res.json().catch(() => null);
      if (!body) throw new Error(`HTTP ${res.status} (not a Telegram answer — network/proxy problem?)`);
      if (!body.ok) throw new Error(body.description || `status ${res.status}`);
      updates = body.result || [];
      clearBotError(name);
    } catch (e) {
      warn(`${name}: getUpdates failed (${e.message}) — sleeping ${ERR_SLEEP_MS / 1000}s`);
      setBotError(name, `Telegram: ${e.message}`);
      await sleep(ERR_SLEEP_MS);
      continue;
    }

    if (updates.length) {
      const { secret } = tgConfig();
      if (!secret) {
        // The app would silently drop these. Say so loudly and keep the offset so we don't spin.
        error(`${name}: no webhook secret in settings — the app will reject every update. Open Settings → Integrations → Telegram and click Generate.`);
        setBotError(name, 'no secret set — open Settings → Telegram and click Generate');
      }
      for (const u of updates) {
        if (secret) await forward(name, u, secret);
        writeOffset(name, u.update_id + 1); // advance regardless — no replays
      }
      botStatus(name).lastUpdateAt = now();
    }
    flushStatus();
    if (!updates.length) await sleep(500); // breathe between empty long polls
  }
  log(`${name}: stopped (bot removed from settings)`);
  delete status.bots[name]; flushStatus();
}

// ---- reconcile loop -----------------------------------------------------
async function main() {
  acquireLockSafe();
  process.on('SIGINT', () => { releaseLock(); process.exit(0); });
  process.on('SIGTERM', () => { releaseLock(); process.exit(0); });
  process.on('exit', releaseLock);

  log(`starting — app ${APP}, settings ${SETTINGS}`);
  let announcedEmpty = false;

  while (true) {
    const { bots } = tgConfig();
    const wanted = Object.keys(bots).filter(n => bots[n] && bots[n].token);

    // start new / re-tokened bots
    for (const n of wanted) {
      const token = String(bots[n].token);
      const cur = running.get(n);
      if (cur && cur.token === token) continue;
      if (cur) { cur.stop = true; running.delete(n); }
      const ctl = { token, stop: false };
      running.set(n, ctl);
      log(`polling for: ${n}`);
      pollBot(n, ctl).catch(e => {
        error(`${n}: poll loop crashed (${e.message}) — will restart on next reconcile`);
        setBotError(n, `poll loop crashed: ${e.message}`);
        running.delete(n);
      });
    }
    // stop removed bots
    for (const [n, ctl] of running) {
      if (!wanted.includes(n)) { ctl.stop = true; running.delete(n); }
    }

    if (!wanted.length && !announcedEmpty) { log('no bots in settings.telegram.bots — waiting (add one in Settings → Integrations → Telegram)'); announcedEmpty = true; }
    if (wanted.length) announcedEmpty = false;

    status.error = null;
    flushStatus();
    await sleep(RECONCILE_MS);
  }
}

main().catch(e => { error(`fatal: ${e.message}`); status.error = e.message; flushStatus(); releaseLock(); process.exit(1); });
