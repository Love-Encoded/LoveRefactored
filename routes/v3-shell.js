// © 2024-2026 Megan Neves and Tiara Young. All rights reserved.
// Love Refactored — https://github.com/Love-Encoded/Love-Refactored
// Licensed under the Love Refactored / Tanevan License. See LICENSE.md.

'use strict';

// Small read-mostly endpoints the v3 sidebar needs: last-message previews,
// household counts, and named collections. Additive — nothing here touches
// chat, memory, or the classic app.

module.exports = (app, deps) => {
  const {
    fs, path, DATA_DIR, COMPANION_DIR, JOURNAL_DIR,
    getChatHistory, getAllGroups, getGroupHistory, getChatDb,
    getAllParlors, getAllProjects, getAllLorebooks, getCalendarEvents
  } = deps;

  const COLLECTIONS_FILE = path.join(DATA_DIR, 'collections.json');

  function lastOf(history) {
    if (!Array.isArray(history) || !history.length) return null;
    const m = history[history.length - 1];
    if (!m) return null;
    let text = String(m.text || '');
    if (text.indexOf('__IMAGE__') === 0) text = '📷 photo';
    return {
      text: text.slice(0, 140),
      sender: m.sender || '',
      timestamp: m.timestamp || null
    };
  }

  // Last message for every companion and group, in one call.
  app.get('/api/v3/previews', (req, res) => {
    try {
      const companions = {};
      for (const f of fs.readdirSync(COMPANION_DIR).filter(x => x.endsWith('.json'))) {
        let name;
        try { name = JSON.parse(fs.readFileSync(path.join(COMPANION_DIR, f), 'utf-8')).name; } catch (e) { continue; }
        if (!name) continue;
        try {
          const last = lastOf(getChatHistory(name));
          if (last) companions[name] = last;
        } catch (e) { /* one bad history never kills the sidebar */ }
      }
      const groups = {};
      try {
        for (const g of getAllGroups()) {
          try {
            const last = lastOf(getGroupHistory(g.id));
            if (last) groups[g.id] = last;
          } catch (e) { /* skip */ }
        }
      } catch (e) { /* skip */ }
      res.json({ companions, groups });
    } catch (err) {
      res.status(500).json({ error: err.message });
    }
  });

  // Counts for the Together / world sidebar badges.
  app.get('/api/v3/counts', (req, res) => {
    let wall = 0, journal = 0, parlor = 0, studio = 0, lorebooks = 0, calendar = 0;
    try {
      const row = getChatDb().prepare('SELECT COUNT(*) AS n FROM wall_posts').get();
      wall = row ? row.n : 0;
    } catch (e) { /* wall table may not exist yet */ }
    try {
      for (const dir of fs.readdirSync(JOURNAL_DIR)) {
        const f = path.join(JOURNAL_DIR, dir, 'entries.json');
        if (!fs.existsSync(f)) continue;
        try {
          const entries = JSON.parse(fs.readFileSync(f, 'utf-8'));
          if (Array.isArray(entries)) journal += entries.length;
        } catch (e) { /* skip corrupt file */ }
      }
    } catch (e) { /* no journals yet */ }
    try { parlor = (getAllParlors() || []).length; } catch (e) { /* none yet */ }
    try { studio = (getAllProjects() || []).length; } catch (e) { /* none yet */ }
    try { lorebooks = (getAllLorebooks() || []).length; } catch (e) { /* none yet */ }
    try { calendar = (getCalendarEvents() || []).length; } catch (e) { /* none yet */ }
    res.json({ wall, journal, parlor, studio, lorebooks, calendar });
  });

  // Named collections — sidebar groupings. Empty until the household names them.
  // { collections: [{ id, name, members: [companionName] }] }
  app.get('/api/collections', (req, res) => {
    try {
      if (!fs.existsSync(COLLECTIONS_FILE)) return res.json({ collections: [] });
      const data = JSON.parse(fs.readFileSync(COLLECTIONS_FILE, 'utf-8'));
      res.json({ collections: Array.isArray(data.collections) ? data.collections : [] });
    } catch (err) {
      res.status(500).json({ error: err.message });
    }
  });

  app.put('/api/collections', (req, res) => {
    try {
      const incoming = req.body && Array.isArray(req.body.collections) ? req.body.collections : null;
      if (!incoming) return res.status(400).json({ error: 'collections array is required' });
      const clean = incoming
        .filter(c => c && typeof c === 'object' && String(c.name || '').trim())
        .slice(0, 40)
        .map(c => ({
          id: String(c.id || `col_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`),
          name: String(c.name).trim().slice(0, 60),
          members: (Array.isArray(c.members) ? c.members : [])
            .map(m => String(m)).filter(Boolean).slice(0, 100)
        }));
      const tmp = COLLECTIONS_FILE + '.tmp';
      fs.writeFileSync(tmp, JSON.stringify({ collections: clean }, null, 2));
      fs.renameSync(tmp, COLLECTIONS_FILE);
      res.json({ collections: clean });
    } catch (err) {
      res.status(500).json({ error: err.message });
    }
  });
};
