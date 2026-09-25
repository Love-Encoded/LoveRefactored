// © 2024-2026 Megan Neves and Tiara Young. All rights reserved.
// Love Refactored — https://github.com/Love-Encoded/Love-Refactored
// Licensed under the Love Refactored / Tanevan License. See LICENSE.md.
//
// auth.example.js — login wall template for Love Refactored.
//
// SETUP (required before exposing the app to the internet):
//
//   cp auth.example.js auth.js
//   node scripts/auth-users.js add <username> --role admin
//   ./start.sh
//
// auth.js is gitignored (your local copy). Without it, the server runs with
// auth disabled — fine for localhost dev only.
//
// Optional guest account (one companion, redacted settings):
//
//   node scripts/auth-users.js add friend --role guest --companion Aria
//
// Behind nginx / Caddy / Tailscale Serve? Set AUTH_TRUST_PROXY=1 so secure
// cookies and client IPs work correctly.
//
// Accounts: data/auth/users.json (scrypt hashes — managed by scripts/auth-users.js)
// Sessions: data/auth/sessions.db + data/auth/session-secret
//
// Design notes:
// - Passwords are never stored in plaintext (scrypt + per-user salt,
//   constant-time comparison, dummy verify for unknown users so timing
//   doesn't reveal which usernames exist).
// - Sessions persist across restarts (better-sqlite3 store + a session
//   secret generated once and kept in data/auth/session-secret).
// - Login is rate-limited per IP and per username.
// - Guests are fenced to an exact method+path allowlist, pinned to their
//   assigned companion, and any settings/companion JSON they can read is
//   redacted (API keys, tokens, phone numbers masked).
// - app.set('authorizeUpgrade', fn) lets server.js gate WebSocket upgrades
//   with the same session.

const path = require('path');
const fs = require('fs');
const crypto = require('crypto');
const express = require('express');
const session = require('express-session');
const { Store } = session;
const Database = require('better-sqlite3');

const AUTH_DIR = path.join(__dirname, 'data', 'auth');
const USERS_FILE = path.join(AUTH_DIR, 'users.json');
const SECRET_FILE = path.join(AUTH_DIR, 'session-secret');
const SESSIONS_DB = path.join(AUTH_DIR, 'sessions.db');

const ADMIN_SESSION_MS = 30 * 24 * 60 * 60 * 1000; // 30 days
const GUEST_SESSION_MS = 7 * 24 * 60 * 60 * 1000;  // 7 days

// Login rate limiting: lock after N failures within the window.
const MAX_LOGIN_FAILURES = 5;
const LOCKOUT_MS = 15 * 60 * 1000;

// ───────────────────────────────────────────────────────────────────────────
// Password hashing (scrypt, no external deps)
// Format: scrypt:N:r:p:salt_b64:hash_b64
// ───────────────────────────────────────────────────────────────────────────

const SCRYPT = { N: 16384, r: 8, p: 1, keylen: 64 };

function hashPassword(password) {
  const salt = crypto.randomBytes(16);
  const hash = crypto.scryptSync(password, salt, SCRYPT.keylen, {
    N: SCRYPT.N, r: SCRYPT.r, p: SCRYPT.p
  });
  return `scrypt:${SCRYPT.N}:${SCRYPT.r}:${SCRYPT.p}:${salt.toString('base64')}:${hash.toString('base64')}`;
}

function verifyPassword(password, stored) {
  try {
    const [scheme, N, r, p, saltB64, hashB64] = String(stored || '').split(':');
    if (scheme !== 'scrypt') return false;
    const salt = Buffer.from(saltB64, 'base64');
    const expected = Buffer.from(hashB64, 'base64');
    const actual = crypto.scryptSync(password, salt, expected.length, {
      N: Number(N), r: Number(r), p: Number(p)
    });
    return crypto.timingSafeEqual(actual, expected);
  } catch (_e) {
    return false;
  }
}

// Used when the username doesn't exist, so the request takes the same time
// as a real password check (prevents username discovery via timing).
const DUMMY_HASH = hashPassword(crypto.randomBytes(18).toString('base64'));

// ───────────────────────────────────────────────────────────────────────────
// User accounts (data/auth/users.json — managed by scripts/auth-users.js)
// ───────────────────────────────────────────────────────────────────────────

function loadUsers() {
  try {
    const raw = JSON.parse(fs.readFileSync(USERS_FILE, 'utf-8'));
    return raw && typeof raw.users === 'object' ? raw.users : {};
  } catch (_e) {
    return null; // missing or unreadable
  }
}

function isValidUsername(name) {
  return typeof name === 'string' && /^[a-z0-9_-]{1,32}$/.test(name);
}

// ───────────────────────────────────────────────────────────────────────────
// Session store (better-sqlite3 — sessions survive restarts)
// ───────────────────────────────────────────────────────────────────────────

class SqliteSessionStore extends Store {
  constructor(dbPath) {
    super();
    this.db = new Database(dbPath);
    this.db.pragma('journal_mode = WAL');
    this.db.exec(`CREATE TABLE IF NOT EXISTS sessions (
      sid TEXT PRIMARY KEY,
      expires INTEGER NOT NULL,
      data TEXT NOT NULL
    )`);
    this._get = this.db.prepare('SELECT data, expires FROM sessions WHERE sid = ?');
    this._set = this.db.prepare('INSERT INTO sessions (sid, expires, data) VALUES (?, ?, ?) ON CONFLICT(sid) DO UPDATE SET expires = excluded.expires, data = excluded.data');
    this._touch = this.db.prepare('UPDATE sessions SET expires = ? WHERE sid = ?');
    this._destroy = this.db.prepare('DELETE FROM sessions WHERE sid = ?');
    this._prune = this.db.prepare('DELETE FROM sessions WHERE expires < ?');
    this._prune.run(Date.now());
    this._pruneTimer = setInterval(() => {
      try { this._prune.run(Date.now()); } catch (_e) {}
    }, 60 * 60 * 1000);
    this._pruneTimer.unref();
  }

  _expiry(sess) {
    const ms = sess?.cookie?.maxAge;
    return Date.now() + (typeof ms === 'number' ? ms : ADMIN_SESSION_MS);
  }

  get(sid, cb) {
    try {
      const row = this._get.get(sid);
      if (!row || row.expires < Date.now()) return cb(null, null);
      cb(null, JSON.parse(row.data));
    } catch (err) { cb(err); }
  }

  set(sid, sess, cb) {
    try {
      this._set.run(sid, this._expiry(sess), JSON.stringify(sess));
      cb && cb(null);
    } catch (err) { cb && cb(err); }
  }

  touch(sid, sess, cb) {
    try {
      this._touch.run(this._expiry(sess), sid);
      cb && cb(null);
    } catch (err) { cb && cb(err); }
  }

  destroy(sid, cb) {
    try {
      this._destroy.run(sid);
      cb && cb(null);
    } catch (err) { cb && cb(err); }
  }
}

// ───────────────────────────────────────────────────────────────────────────
// Login rate limiting (in memory)
// ───────────────────────────────────────────────────────────────────────────

const loginFailures = new Map(); // key → { count, lockedUntil, firstAt }

function rateKeyList(ip, username) {
  return [`ip:${ip}`, `user:${username}`];
}

function isLockedOut(ip, username) {
  const now = Date.now();
  for (const key of rateKeyList(ip, username)) {
    const entry = loginFailures.get(key);
    if (entry && entry.lockedUntil && entry.lockedUntil > now) return true;
  }
  return false;
}

function recordFailure(ip, username) {
  const now = Date.now();
  for (const key of rateKeyList(ip, username)) {
    let entry = loginFailures.get(key);
    if (!entry || now - entry.firstAt > LOCKOUT_MS) {
      entry = { count: 0, firstAt: now, lockedUntil: 0 };
    }
    entry.count++;
    if (entry.count >= MAX_LOGIN_FAILURES) entry.lockedUntil = now + LOCKOUT_MS;
    loginFailures.set(key, entry);
  }
}

function clearFailures(ip, username) {
  for (const key of rateKeyList(ip, username)) loginFailures.delete(key);
}

setInterval(() => {
  const now = Date.now();
  for (const [key, entry] of loginFailures) {
    if (now - entry.firstAt > LOCKOUT_MS && (!entry.lockedUntil || entry.lockedUntil < now)) {
      loginFailures.delete(key);
    }
  }
}, 10 * 60 * 1000).unref();

// ───────────────────────────────────────────────────────────────────────────
// Secret redaction for anything guests are allowed to read
// ───────────────────────────────────────────────────────────────────────────

const SECRET_KEY_RE = /key|secret|token|password|phone|sid|credential/i;

function redactSecrets(value) {
  if (Array.isArray(value)) return value.map(redactSecrets);
  if (value && typeof value === 'object') {
    const out = {};
    for (const [k, v] of Object.entries(value)) {
      if (SECRET_KEY_RE.test(k)) {
        out[k] = typeof v === 'string' && v ? '••••' : v && typeof v === 'object' ? redactSecrets(v) : v;
      } else {
        out[k] = redactSecrets(v);
      }
    }
    return out;
  }
  return value;
}

function interceptJson(res, transform) {
  const orig = res.json.bind(res);
  res.json = (body) => {
    try { return orig(transform(body)); }
    catch (_e) { return orig(body); }
  };
}

// ───────────────────────────────────────────────────────────────────────────
// Main setup
// ───────────────────────────────────────────────────────────────────────────

const activeGuests = new Map();

function setupAuth(app) {
  fs.mkdirSync(AUTH_DIR, { recursive: true });

  if (loadUsers() === null) {
    console.warn('⚠️  auth.js: no data/auth/users.json found.');
    console.warn('    All logins will fail until you create an account:');
    console.warn('    node scripts/auth-users.js add <username> --role admin');
  }

  // Session secret: generated once, reused forever (sessions survive restarts).
  let secret;
  try {
    secret = fs.readFileSync(SECRET_FILE, 'utf-8').trim();
    if (!secret) throw new Error('empty');
  } catch (_e) {
    secret = crypto.randomBytes(48).toString('hex');
    fs.writeFileSync(SECRET_FILE, secret, { mode: 0o600 });
  }

  // Behind a TLS-terminating proxy (tailscale serve, nginx, caddy)?
  // Set AUTH_TRUST_PROXY=1 so secure cookies and client IPs work correctly.
  if (process.env.AUTH_TRUST_PROXY === '1') app.set('trust proxy', 1);

  app.use(express.urlencoded({ extended: false }));
  // Parse JSON here too (server.js mounts its own parser later; whichever
  // runs first wins) so the guest wall can inspect/pin chat request bodies.
  app.use(express.json({ limit: '50mb' }));

  const sessionMiddleware = session({
    secret,
    store: new SqliteSessionStore(SESSIONS_DB),
    resave: false,
    saveUninitialized: false,
    name: 'lr.sid',
    cookie: {
      maxAge: ADMIN_SESSION_MS,
      httpOnly: true,
      sameSite: 'lax',
      secure: 'auto'
    }
  });
  app.use(sessionMiddleware);

  // For WebSocket upgrade auth in server.js.
  app.sessionMiddleware = sessionMiddleware;
  app.set('authorizeUpgrade', (req, done) => {
    sessionMiddleware(req, {}, () => {
      done(Boolean(req.session && req.session.authenticated));
    });
  });

  // === LOGIN PAGE ===
  const PAGE_CSS = `
  :root{--void:#08060E;--room:#100B1C;--lamp:#18112C;--lamp-hi:#211738;--line:#251B40;--line-lit:#3B2B63;
    --ink:#F0EAF8;--ink-2:#B3A2D4;--ink-3:#8B77A8;--violet:#B44AFF;--danger:#F0566B;
    --r-sm:6px;--r-md:12px;--sans:"DM Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;--mono:"JetBrains Mono",ui-monospace,Menlo,monospace}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--void);color:var(--ink-2);font-family:var(--sans);font-size:15px;line-height:1.55;
    min-height:100dvh;display:grid;place-items:center;padding:24px;-webkit-font-smoothing:antialiased}
  .door{width:100%;max-width:360px;background:var(--room);border:1px solid var(--line);border-radius:var(--r-md);padding:32px 28px 28px}
  .mark{display:flex;align-items:center;gap:10px;margin-bottom:24px}
  .mark img{width:28px;height:28px;border-radius:var(--r-sm)}
  .lbl{font-family:var(--mono);font-size:10px;font-weight:500;letter-spacing:.16em;text-transform:uppercase;color:var(--ink-3)}
  h1{font-size:22px;font-weight:600;color:var(--ink);margin-bottom:4px}
  .intro{font-size:13px;color:var(--ink-3);margin-bottom:20px}
  label{display:block;font-family:var(--mono);font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--ink-3);margin:14px 0 6px}
  .inp{width:100%;padding:11px 12px;background:var(--lamp);border:1px solid var(--line);border-radius:var(--r-sm);
    color:var(--ink);font:inherit;font-size:15px;transition:border-color .14s}
  .inp:hover{border-color:var(--line-lit)}
  .inp:focus{outline:none;border-color:var(--violet)}
  .hint{font-size:12px;color:var(--ink-3);margin-top:6px}
  .btn{width:100%;margin-top:22px;padding:12px;border:none;border-radius:var(--r-sm);background:var(--violet);color:#fff;
    font:inherit;font-size:15px;font-weight:600;cursor:pointer;transition:filter .14s}
  .btn:hover{filter:brightness(1.1)}
  .btn:focus-visible{outline:2px solid var(--violet);outline-offset:2px}
  .error{margin-top:14px;font-size:13px;color:var(--danger)}
  .foot{margin-top:20px;padding-top:16px;border-top:1px solid var(--line);font-size:12px;color:var(--ink-3);line-height:1.6}
  .foot code{font-family:var(--mono);font-size:11px;color:var(--ink-2)}`;

  const pageHTML = ({ title, intro, form, error = '', foot = '' }) => `<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Love Refactored</title>
<link rel="icon" href="/LoveRefactoredFavicon.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=DM+Sans:opsz,wght@9..40,400;9..40,500;9..40,600;9..40,700&family=JetBrains+Mono:wght@400;500&display=swap">
<style>${PAGE_CSS}</style></head>
<body><main class="door">
  <div class="mark"><img src="/LoveRefactoredLogo.png" alt=""><span class="lbl">Love Refactored</span></div>
  <h1>${title}</h1>
  <p class="intro">${intro}</p>
  ${form}
  ${error}
  ${foot}
</main></body></html>`;

  const loginHTML = (error = '') => pageHTML({
    title: "Who's there?",
    intro: 'The house is locked. Sign in to come inside.',
    form: `<form method="POST" action="/login">
    <label for="u">Username</label>
    <input class="inp" id="u" name="username" autocomplete="username" autocapitalize="none" spellcheck="false" required autofocus>
    <label for="p">Password</label>
    <input class="inp" id="p" name="password" type="password" autocomplete="current-password" required>
    <button class="btn" type="submit">Come in</button>
  </form>`,
    error
  });

  const setupHTML = (error = '') => pageHTML({
    title: 'Set up the house',
    intro: 'The login wall is on and nobody has a key yet. Make the first account — it owns everything.',
    form: `<form method="POST" action="/setup">
    <label for="u">Username</label>
    <input class="inp" id="u" name="username" autocomplete="username" autocapitalize="none" spellcheck="false" pattern="[a-z0-9_-]{1,32}" required autofocus>
    <p class="hint">Lowercase letters, numbers, _ or -</p>
    <label for="p">Password</label>
    <input class="inp" id="p" name="password" type="password" autocomplete="new-password" minlength="12" required>
    <p class="hint">At least 12 characters. A few random words beats symbol soup.</p>
    <label for="c">Confirm password</label>
    <input class="inp" id="c" name="confirm" type="password" autocomplete="new-password" minlength="12" required>
    <button class="btn" type="submit">Create account</button>
  </form>`,
    error,
    foot: `<p class="foot">Running this on your own machine and don't want a login at all? Delete <code>auth.js</code> and restart — the wall comes down. Only do that for localhost.</p>`
  });

  const hasUsers = () => { const u = loadUsers(); return !!(u && Object.keys(u).length); };

  const saveUsers = (users) => {
    fs.mkdirSync(AUTH_DIR, { recursive: true });
    const tmp = USERS_FILE + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify({ users }, null, 2) + '\n', { mode: 0o600 });
    fs.renameSync(tmp, USERS_FILE);
  };

  app.get('/login', (req, res) => {
    if (req.session.authenticated) return res.redirect('/');
    res.type('html').send(hasUsers() ? loginHTML() : setupHTML());
  });

  // First-run only: create the owner account from the browser. Refuses once any account exists.
  app.post('/setup', (req, res) => {
    if (hasUsers()) return res.status(403).type('html').send(loginHTML('<div class="error">The house already has an owner. Sign in instead.</div>'));
    const username = String(req.body?.username || '').toLowerCase().trim();
    const password = String(req.body?.password || '');
    const confirm = String(req.body?.confirm || '');
    if (!isValidUsername(username)) return res.type('html').send(setupHTML('<div class="error">Username must be 1–32 chars: lowercase letters, numbers, _ or -</div>'));
    if (password.length < 12) return res.type('html').send(setupHTML('<div class="error">Password needs at least 12 characters.</div>'));
    if (password !== confirm) return res.type('html').send(setupHTML('<div class="error">Passwords don\'t match.</div>'));
    saveUsers({ [username]: { passwordHash: hashPassword(password), role: 'admin', createdAt: new Date().toISOString() } });
    req.session.regenerate((err) => {
      if (err) return res.status(500).type('html').send(loginHTML('<div class="error">Account created, but sign-in failed. Try signing in.</div>'));
      req.session.authenticated = true;
      req.session.username = username;
      req.session.role = 'admin';
      req.session.companion = null;
      req.session.cookie.maxAge = ADMIN_SESSION_MS;
      req.session.save(() => res.redirect('/'));
    });
  });

  app.post('/login', (req, res) => {
    const username = String(req.body?.username || '').toLowerCase().trim();
    const password = String(req.body?.password || '');
    const ip = req.ip || req.socket?.remoteAddress || 'unknown';

    if (!isValidUsername(username)) {
      return res.type('html').send(loginHTML('<div class="error">Wrong username or password</div>'));
    }
    if (isLockedOut(ip, username)) {
      return res.status(429).type('html').send(loginHTML('<div class="error">Too many attempts. Try again in 15 minutes.</div>'));
    }

    const users = loadUsers() || {};
    const user = users[username];
    // Always run a verify so unknown usernames take the same time as real ones.
    const ok = user
      ? verifyPassword(password, user.passwordHash)
      : (verifyPassword(password, DUMMY_HASH), false);

    if (!ok) {
      recordFailure(ip, username);
      return res.type('html').send(loginHTML('<div class="error">Wrong username or password</div>'));
    }

    clearFailures(ip, username);

    // Fresh session ID on login (prevents session fixation).
    req.session.regenerate((err) => {
      if (err) return res.status(500).type('html').send(loginHTML('<div class="error">Something went wrong. Try again.</div>'));
      req.session.authenticated = true;
      req.session.username = username;
      req.session.role = user.role === 'admin' ? 'admin' : 'guest';
      req.session.companion = user.companion || null;
      req.session.cookie.maxAge = req.session.role === 'admin' ? ADMIN_SESSION_MS : GUEST_SESSION_MS;

      if (req.session.role === 'guest') {
        activeGuests.set(req.sessionID, {
          username,
          companion: user.companion || null,
          loginTime: new Date().toISOString(),
          lastSeen: new Date().toISOString(),
          sessionID: req.sessionID
        });
      }
      req.session.save(() => res.redirect('/'));
    });
  });

  // === LOGOUT ===
  const doLogout = (req, res) => {
    const sid = req.sessionID;
    activeGuests.delete(sid);
    req.session.destroy(() => {
      res.clearCookie('lr.sid');
      res.redirect('/login');
    });
  };
  app.get('/logout', doLogout);
  app.post('/logout', doLogout);

  // === WHO AM I ===
  app.get('/api/me', (req, res) => {
    if (!req.session.authenticated) return res.status(401).json({ error: 'Not logged in' });
    res.json({
      username: req.session.username,
      role: req.session.role,
      companion: req.session.companion || null
    });
  });

  // === ADMIN: GUEST STATUS ===
  app.get('/api/guest/status', (req, res) => {
    if (req.session.role !== 'admin') return res.status(403).json({ error: 'Admin only' });
    const guests = Array.from(activeGuests.values());
    res.json({ active: guests.length > 0, guests });
  });

  // === ADMIN: KILL GUEST SESSIONS ===
  app.post('/api/guest/kill', (req, res) => {
    if (req.session.role !== 'admin') return res.status(403).json({ error: 'Admin only' });
    const store = req.sessionStore;
    let killed = 0;
    for (const [sid] of activeGuests) {
      store.destroy(sid, () => {});
      activeGuests.delete(sid);
      killed++;
    }
    res.json({ killed, message: killed > 0 ? 'Guest sessions terminated' : 'No active guest sessions' });
  });

  // === ADMIN: PAUSE/UNPAUSE GUESTS ===
  let guestPaused = false;
  app.post('/api/guest/pause', (req, res) => {
    if (req.session.role !== 'admin') return res.status(403).json({ error: 'Admin only' });
    guestPaused = !guestPaused;
    res.json({ paused: guestPaused });
  });
  app.get('/api/guest/paused', (req, res) => {
    if (!req.session.authenticated) return res.status(401).json({ error: 'Not logged in' });
    res.json({ paused: guestPaused });
  });

  // === THE WALL — everything below requires a session ===
  app.use((req, res, next) => {
    // Internal self-calls: server.js calling its own API (companion selfies,
    // voice photo jobs, video gen → image gen). The secret is generated by
    // server.js at boot and shared in-process — allowed only when it matches
    // AND the request arrives over a loopback socket.
    const internalSecret = req.app.get('internalApiSecret') || process.env.INTERNAL_API_SECRET || '';
    const givenSecret = req.headers['x-internal-auth'];
    if (internalSecret && typeof givenSecret === 'string') {
      const given = Buffer.from(givenSecret);
      const expected = Buffer.from(internalSecret);
      const peer = req.socket?.remoteAddress || '';
      const isLoopback = peer === '127.0.0.1' || peer === '::1' || peer === '::ffff:127.0.0.1';
      if (isLoopback && given.length === expected.length && crypto.timingSafeEqual(given, expected)) {
        req.userRole = 'admin';
        return next();
      }
    }

    // Public paths needed before login (PWA manifest + icons).
    if (req.method === 'GET' && ['/manifest.json', '/LoveRefactoredFavicon.png', '/LoveRefactoredLogo.png'].includes(req.path)) {
      return next();
    }

    if (!req.session.authenticated) {
      if (req.path === '/login') return next();
      const isAPI = req.headers['content-type']?.includes('application/json') ||
                    req.path.startsWith('/api/') ||
                    req.path === '/chat' ||
                    req.method !== 'GET';
      if (isAPI) return res.status(401).json({ error: 'Unauthorized' });
      return res.redirect('/login');
    }

    req.userRole = req.session.role;
    req.userCompanion = req.session.companion;

    if (req.session.role !== 'guest') return next(); // admin: full access

    // ── GUEST FENCE ──
    const guestEntry = activeGuests.get(req.sessionID);
    if (guestEntry) guestEntry.lastSeen = new Date().toISOString();

    if (guestPaused && (req.path === '/chat' || req.path === '/api/chat')) {
      return res.status(503).json({ error: '⏸️ Session is paused. Please wait.' });
    }

    const companionName = req.session.companion || '';
    const safeName = companionName.toLowerCase().replace(/[^a-z0-9]/g, '_');

    // Chat: allowed, but pinned to their assigned companion no matter what
    // the request body says.
    if (req.method === 'POST' && req.path === '/chat') {
      if (!companionName) return res.status(403).json({ error: 'No companion assigned to this account' });
      if (req.body && typeof req.body === 'object') req.body.companion = companionName;
      return next();
    }

    // Their guest history only — exact method+path (no truncate, no delete,
    // no message edits: those endpoints would touch the owner's history).
    if (req.method === 'GET' && req.path === `/api/history/${safeName}`) return next();
    if (req.method === 'PUT' && req.path === `/api/history/${safeName}`) return next();
    if (req.method === 'POST' && [
      `/api/history/${safeName}/messages`,
      `/api/history/${safeName}/messages/beacon`,
      `/api/history/${safeName}/beacon`
    ].includes(req.path)) return next();

    // Read-only app data, redacted and (for companions) filtered to theirs.
    if (req.method === 'GET' && req.path === '/api/settings') {
      interceptJson(res, redactSecrets);
      return next();
    }
    if (req.method === 'GET' && req.path === '/api/companions') {
      interceptJson(res, (body) => {
        const list = Array.isArray(body) ? body : [];
        const mine = list.filter(c => String(c?.name || '').toLowerCase() === companionName.toLowerCase());
        return redactSecrets(mine);
      });
      return next();
    }

    const allowedGets = [
      '/api/me',
      '/api/guest/paused',
      '/api/memory/health',
      '/api/persona'
    ];
    if (req.method === 'GET' && allowedGets.includes(req.path)) {
      if (req.path === '/api/persona') interceptJson(res, redactSecrets);
      return next();
    }

    // Their companion's avatar + general static assets (never under /api/).
    if (req.method === 'GET') {
      if (req.path === `/api/companions/${encodeURIComponent(companionName)}/avatar` ||
          req.path === `/api/companions/${companionName}/avatar` ||
          req.path.startsWith(`/api/companions/${safeName}/avatar`)) return next();
      if (req.path.startsWith('/avatars/')) return next();
      if (req.path.startsWith('/icons/')) return next();
      if (req.path === '/' || req.path === '/index.html' ||
          req.path === '/v3' || req.path === '/v3/' || req.path === '/v3/index.html') return next();
      if (!req.path.startsWith('/api/') && /\.(css|js|woff2?|ttf|svg|png|jpg|jpeg|webp|ico)$/.test(req.path)) return next();
    }

    return res.status(403).json({ error: 'Access restricted' });
  });
}

module.exports = setupAuth;
module.exports.hashPassword = hashPassword;
module.exports.verifyPassword = verifyPassword;
