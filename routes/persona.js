// © 2024-2026 Megan Neves and Tiara Young. All rights reserved.
// Love Refactored — https://github.com/Love-Encoded/Love-Refactored
// Licensed under the Love Refactored / Tanevan License. See LICENSE.md.

'use strict';

/**
 * @param {import('express').Application} app
 * @param {object} deps
 */
function registerPersonaRoutes(app, deps) {
  const {
    fs,
    path,
    getPersona,
    savePersona,
    refImageUpload,
    personaUpload,
    AVATAR_DIR,
    referenceImagesDir,
    refImageList,
    setRefImages,
    MAX_FACE_REFS
  } = deps;

  app.get('/api/persona', (req, res) => {
    res.json(getPersona());
  });

  app.put('/api/persona', (req, res) => {
    const current = getPersona();
    const allowed = ['name', 'gender', 'backstory', 'avatar', 'appearance', 'falReferenceImage', 'falReferenceImages'];
    for (const key of allowed) {
      if (req.body[key] !== undefined) current[key] = req.body[key];
    }
    if (req.body.falReferenceImages !== undefined || req.body.falReferenceImage !== undefined) {
      const list = Array.isArray(req.body.falReferenceImages) ? req.body.falReferenceImages
        : (req.body.falReferenceImage ? [req.body.falReferenceImage, ...(current.falReferenceImages || []).filter(f => f !== req.body.falReferenceImage)] : []);
      setRefImages(current, list);
    }
    savePersona(current);
    res.json(current);
  });

  // Upload a reference face photo for the user persona (for "couple" and group selfies)
  app.post('/api/persona/reference-image', refImageUpload.single('image'), async (req, res) => {
    try {
      if (!req.file) return res.status(400).json({ error: 'No image file received' });
      const ext = req.file.originalname.match(/\.(jpe?g|png|webp|gif)$/i)?.[0] || '.png';
      const persona = getPersona();
      const existing = refImageList(persona);
      if (existing.length >= MAX_FACE_REFS) {
        return res.status(400).json({ error: `Maximum of ${MAX_FACE_REFS} reference photos reached. Remove one first.`, files: existing, max: MAX_FACE_REFS });
      }
      // Unique filename per upload so photos don't overwrite each other
      const filename = `_persona_reference_${Date.now()}${ext}`;
      const refDir = referenceImagesDir;
      if (!fs.existsSync(refDir)) fs.mkdirSync(refDir, { recursive: true });
      fs.writeFileSync(path.join(refDir, filename), req.file.buffer);

      // Add to the persona's list of reference photos (instead of replacing)
      const list = [...existing, filename];
      setRefImages(persona, list);
      savePersona(persona);

      console.log(`📸 Saved persona reference image: ${filename} (${(req.file.size / 1024).toFixed(0)}KB) — ${list.length}/${MAX_FACE_REFS}`);
      return res.json({ success: true, filename, files: list, max: MAX_FACE_REFS });
    } catch (e) {
      console.error('Persona reference upload error:', e.message);
      return res.status(500).json({ error: `Upload failed: ${e.message}` });
    }
  });

  // Remove one persona reference photo
  app.delete('/api/persona/reference-image/:filename', (req, res) => {
    try {
      const filename = path.basename(decodeURIComponent(req.params.filename || ''));
      if (!filename) return res.status(400).json({ error: 'filename required' });
      const persona = getPersona();
      const list = refImageList(persona).filter(f => f !== filename);
      setRefImages(persona, list);
      savePersona(persona);
      try { fs.unlinkSync(path.join(referenceImagesDir, filename)); } catch (e) { /* already gone */ }
      console.log(`🗑️ Removed persona reference image ${filename} (${list.length} left)`);
      return res.json({ success: true, files: list, max: MAX_FACE_REFS });
    } catch (e) {
      console.error('Persona reference delete error:', e.message);
      return res.status(500).json({ error: `Delete failed: ${e.message}` });
    }
  });

  // Upload persona avatar
  app.post('/api/persona/avatar',
    (req, res, next) => {
      // Remove any existing persona avatar before saving the new one
      try {
        const files = fs.readdirSync(AVATAR_DIR);
        for (const f of files) {
          if (path.basename(f, path.extname(f)) === '_persona') {
            fs.unlinkSync(path.join(AVATAR_DIR, f));
          }
        }
      } catch (e) { /* ignore */ }
      next();
    },
    (req, res, next) => {
      personaUpload.single('avatar')(req, res, (err) => {
        if (!err) return next();
        const status = err.code === 'LIMIT_FILE_SIZE' ? 413 : 400;
        return res.status(status).json({ error: err.message || 'Avatar upload failed' });
      });
    },
    (req, res) => {
      if (!req.file) return res.status(400).json({ error: 'No file uploaded' });
      res.json({ url: '/api/persona/avatar' });
    }
  );

  // Serve persona avatar
  app.get('/api/persona/avatar', (req, res) => {
    const files = fs.readdirSync(AVATAR_DIR);
    const avatarFile = files.find(f => path.basename(f, path.extname(f)) === '_persona');
    if (!avatarFile) return res.status(404).json({ error: 'No persona avatar found' });
    res.sendFile(path.join(AVATAR_DIR, avatarFile));
  });
}

module.exports = registerPersonaRoutes;
