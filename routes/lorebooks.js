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
    LOREBOOK_DIR
  } = deps;

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
}

module.exports = registerLorebookRoutes;
