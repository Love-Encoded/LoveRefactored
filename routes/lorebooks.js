// © 2024-2026 Megan Neves and Tiara Young. All rights reserved.
// Love Refactored — https://github.com/Love-Encoded/Love-Refactored
// Licensed under the Love Refactored / Tanevan License. See LICENSE.md.

'use strict';

/**
 * @param {import('express').Application} app
 * @param {object} deps
 */
function registerLorebookRoutes(app, deps) {
  const {
    fs,
    path,
    isSafeId,
    makeId,
    getAllLorebooks,
    getLorebook,
    saveLorebook,
    LOREBOOK_DIR,
    refImageUpload,
    multer,
    sharp,
    DATA_DIR
  } = deps;

  // world_books_v1 — place / pet / object entries for world books (see README → World books)
  const WORLD_ENTRY_KINDS = new Set(['place', 'pet', 'object', 'npc', 'clothing']);
  const MAX_SETTING_REFS_PER_ENTRY = 2;
  const LOREBOOK_REF_DIR = path.join(DATA_DIR, 'lorebook_refs');

  /** Copy the world-book entry fields from a request body when present. isDefault is one-per-book. */
  function applyWorldFields(book, entry, body) {
    if (body.kind !== undefined) entry.kind = WORLD_ENTRY_KINDS.has(String(body.kind)) ? String(body.kind) : 'place';
    if (body.name !== undefined) entry.name = String(body.name || '').trim();
    if (body.owner !== undefined) {
      const list = (Array.isArray(body.owner) ? body.owner : [body.owner]).map(x => String(x == null ? '' : x).trim()).filter(Boolean);
      entry.owner = list.length === 0 ? null : (list.length === 1 ? list[0] : list);
    }
    if (Array.isArray(body.refs)) entry.refs = body.refs.map(f => path.basename(String(f || ''))).filter(Boolean).slice(0, MAX_SETTING_REFS_PER_ENTRY);
    if (body.isDefault !== undefined) {
      entry.isDefault = body.isDefault === true;
      if (entry.isDefault) for (const other of book.entries) if (other !== entry) other.isDefault = false;
    }
  }

  function refStillUsed(filename) {
    for (const b of getAllLorebooks()) for (const e of (b.entries || [])) if (Array.isArray(e.refs) && e.refs.includes(filename)) return true;
    return false;
  }

  app.get('/api/lorebooks', (req, res) => {
    res.json(getAllLorebooks());
  });

  app.get('/api/lorebooks/:id', (req, res) => {
    const book = getLorebook(req.params.id);
    if (!book) return res.status(404).json({ error: 'Lorebook not found' });
    res.json(book);
  });

  app.post('/api/lorebooks', (req, res) => {
    const { name, type, description, companions } = req.body;
    const book = {
      id: makeId(),
      name: name || 'Untitled Book',
      type: type || 'storybook',
      description: description || '',
      companions: companions || [],
      enabled: true,
      prompt: '',
      entries: [],
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString()
    };
    saveLorebook(book);
    res.json(book);
  });

  app.put('/api/lorebooks/:id', (req, res) => {
    const book = getLorebook(req.params.id);
    if (!book) return res.status(404).json({ error: 'Lorebook not found' });

    const allowed = ['name', 'type', 'description', 'companions', 'enabled', 'prompt'];
    for (const key of allowed) {
      if (req.body[key] !== undefined) book[key] = req.body[key];
    }
    book.updatedAt = new Date().toISOString();
    saveLorebook(book);
    res.json(book);
  });

  app.delete('/api/lorebooks/:id', (req, res) => {
    if (!isSafeId(req.params.id)) return res.status(400).json({ error: 'Invalid ID' });
    const filePath = path.join(LOREBOOK_DIR, `${req.params.id}.json`);
    if (!fs.existsSync(filePath)) return res.status(404).json({ error: 'Lorebook not found' });
    fs.unlinkSync(filePath);
    res.json({ success: true });
  });

  app.post('/api/lorebooks/:id/entries', (req, res) => {
    const book = getLorebook(req.params.id);
    if (!book) return res.status(404).json({ error: 'Lorebook not found' });

    const entry = {
      id: makeId(),
      keywords: req.body.keywords || [],
      prompt: req.body.prompt || '',
      enabled: true,
      caseSensitive: false,
      createdAt: new Date().toISOString()
    };
    applyWorldFields(book, entry, req.body || {});
    if (String(book.type || '').toLowerCase() === 'world' && !entry.kind) entry.kind = 'place';
    book.entries.push(entry);
    book.updatedAt = new Date().toISOString();
    saveLorebook(book);
    res.json(entry);
  });

  app.put('/api/lorebooks/:bookId/entries/:entryId', (req, res) => {
    const book = getLorebook(req.params.bookId);
    if (!book) return res.status(404).json({ error: 'Lorebook not found' });

    const entry = book.entries.find(e => e.id === req.params.entryId);
    if (!entry) return res.status(404).json({ error: 'Entry not found' });

    if (req.body.keywords !== undefined) entry.keywords = req.body.keywords;
    if (req.body.prompt !== undefined) entry.prompt = req.body.prompt;
    if (req.body.enabled !== undefined) entry.enabled = req.body.enabled;
    if (req.body.caseSensitive !== undefined) entry.caseSensitive = req.body.caseSensitive;
    applyWorldFields(book, entry, req.body || {});

    book.updatedAt = new Date().toISOString();
    saveLorebook(book);
    res.json(entry);
  });

  app.delete('/api/lorebooks/:bookId/entries/:entryId', (req, res) => {
    const book = getLorebook(req.params.bookId);
    if (!book) return res.status(404).json({ error: 'Lorebook not found' });

    book.entries = book.entries.filter(e => e.id !== req.params.entryId);
    book.updatedAt = new Date().toISOString();
    saveLorebook(book);
    res.json({ success: true });
  });

  // world_books_v1: reference photo for a place / pet / object entry → data/lorebook_refs/
  app.post('/api/lorebooks/:bookId/entries/:entryId/ref', (req, res) => {
    if (!refImageUpload || !multer) return res.status(500).json({ error: 'Upload middleware not available' });
    refImageUpload.single('image')(req, res, async (uploadErr) => {
      if (uploadErr) {
        if (uploadErr instanceof multer.MulterError && uploadErr.code === 'LIMIT_FILE_SIZE') return res.status(413).json({ error: 'That image is too large' });
        return res.status(400).json({ error: uploadErr.message || 'Upload failed' });
      }
      try {
        if (!req.file) return res.status(400).json({ error: 'No image file received' });
        const book = getLorebook(req.params.bookId);
        if (!book) return res.status(404).json({ error: 'Lorebook not found' });
        const entry = (book.entries || []).find(e => e.id === req.params.entryId);
        if (!entry) return res.status(404).json({ error: 'Entry not found' });
        if (!Array.isArray(entry.refs)) entry.refs = [];
        if (entry.refs.length >= MAX_SETTING_REFS_PER_ENTRY) {
          return res.status(409).json({ error: `Up to ${MAX_SETTING_REFS_PER_ENTRY} photos per entry. Remove one first.`, files: entry.refs });
        }
        let outBuffer = req.file.buffer;
        let ext = (String(req.file.originalname || '').match(/\.(jpe?g|png|webp)$/i) || [])[0] || '.png';
        if (sharp) {
          try {
            const meta = await sharp(req.file.buffer).metadata();
            if ((meta.width || 0) > 2048 || (meta.height || 0) > 2048 || req.file.buffer.length > 4 * 1024 * 1024) {
              outBuffer = await sharp(req.file.buffer, { failOnError: false })
                .resize(2048, 2048, { fit: 'inside', withoutEnlargement: true })
                .jpeg({ quality: 90, mozjpeg: true })
                .toBuffer();
              ext = '.jpg';
            }
          } catch (_e) { outBuffer = req.file.buffer; }
        }
        if (!fs.existsSync(LOREBOOK_REF_DIR)) fs.mkdirSync(LOREBOOK_REF_DIR, { recursive: true });
        const safe = (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40) || 'ref';
        const filename = `${safe(entry.name || (entry.keywords || [])[0] || entry.id)}_${Date.now().toString(36)}${ext.toLowerCase()}`;
        fs.writeFileSync(path.join(LOREBOOK_REF_DIR, filename), outBuffer);
        entry.refs.push(filename);
        book.updatedAt = new Date().toISOString();
        saveLorebook(book);
        res.json({ success: true, files: entry.refs, max: MAX_SETTING_REFS_PER_ENTRY });
      } catch (e) {
        res.status(500).json({ error: e.message });
      }
    });
  });

  app.delete('/api/lorebooks/:bookId/entries/:entryId/ref/:filename', (req, res) => {
    const book = getLorebook(req.params.bookId);
    if (!book) return res.status(404).json({ error: 'Lorebook not found' });
    const entry = (book.entries || []).find(e => e.id === req.params.entryId);
    if (!entry) return res.status(404).json({ error: 'Entry not found' });
    const filename = path.basename(decodeURIComponent(req.params.filename));
    entry.refs = (Array.isArray(entry.refs) ? entry.refs : []).filter(f => f !== filename);
    book.updatedAt = new Date().toISOString();
    saveLorebook(book);
    if (!refStillUsed(filename)) { try { fs.unlinkSync(path.join(LOREBOOK_REF_DIR, filename)); } catch (_e) { /* already gone */ } }
    res.json({ success: true, files: entry.refs });
  });

  app.get('/api/lorebook-ref/:filename', (req, res) => {
    const safeName = path.basename(decodeURIComponent(req.params.filename));
    const filePath = path.join(LOREBOOK_REF_DIR, safeName);
    if (!fs.existsSync(filePath)) return res.status(404).send('Not found');
    res.sendFile(filePath);
  });
}

module.exports = registerLorebookRoutes;
