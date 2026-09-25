// © 2024-2026 Megan Neves and Tiara Young. All rights reserved.
// Love Refactored — https://github.com/Love-Encoded/Love-Refactored
// Licensed under the Love Refactored / Tanevan License. See LICENSE.md.

'use strict';

// Beta Participation Agreement gate.
//
// The app refuses to serve the UI until the CURRENT version of
// BETA_AGREEMENT.md has been accepted on this install. Acceptance is
// recorded locally in data/beta-acceptance.json — nothing leaves the machine.
//
// To force re-acceptance after a material change to BETA_AGREEMENT.md,
// bump BETA_AGREEMENT_VERSION below (and the "Version" line in the .md).

const BETA_AGREEMENT_VERSION = '1.0';

module.exports = (app, deps) => {
  const { fs, path, DATA_DIR, ROOT_DIR } = deps;

  const AGREEMENT_FILE = path.join(ROOT_DIR, 'BETA_AGREEMENT.md');
  const ACCEPT_FILE = path.join(DATA_DIR, 'beta-acceptance.json');

  function readAcceptance() {
    try {
      if (!fs.existsSync(ACCEPT_FILE)) return null;
      return JSON.parse(fs.readFileSync(ACCEPT_FILE, 'utf-8'));
    } catch (e) {
      return null;
    }
  }

  function isAccepted() {
    const a = readAcceptance();
    return !!(a && a.version === BETA_AGREEMENT_VERSION && a.acceptedAt);
  }

  // Full agreement text + version, for the acceptance page.
  app.get('/api/beta-agreement', (req, res) => {
    let text = '';
    try { text = fs.readFileSync(AGREEMENT_FILE, 'utf-8'); } catch (e) { /* fall through */ }
    if (!text) return res.status(500).json({ error: 'BETA_AGREEMENT.md not found' });
    res.set('Cache-Control', 'no-store');
    res.json({ version: BETA_AGREEMENT_VERSION, accepted: isAccepted(), text });
  });

  // Record acceptance. Requires the client to echo back the version it read,
  // so a stale page can never accept a newer agreement by accident.
  app.post('/api/beta-agreement/accept', require('express').json(), (req, res) => {
    const sent = String((req.body && req.body.version) || '');
    if (sent !== BETA_AGREEMENT_VERSION) {
      return res.status(409).json({ error: 'Agreement version mismatch — reload and try again.', current: BETA_AGREEMENT_VERSION });
    }
    let appVersion = '';
    try { appVersion = require(path.join(ROOT_DIR, 'package.json')).version || ''; } catch (e) { /* optional */ }
    const record = {
      version: BETA_AGREEMENT_VERSION,
      acceptedAt: new Date().toISOString(),
      appVersion
    };
    try {
      fs.writeFileSync(ACCEPT_FILE, JSON.stringify(record, null, 2));
    } catch (e) {
      return res.status(500).json({ error: 'Could not record acceptance: ' + e.message });
    }
    res.json({ ok: true, ...record });
  });

  return { isAccepted, BETA_AGREEMENT_VERSION };
};
