// © 2024-2026 Megan Neves and Tiara Young. All rights reserved.
// Love Refactored — https://github.com/Love-Encoded/Love-Refactored
// Licensed under the Love Refactored / Tanevan License. See LICENSE.md.

'use strict';

/**
 * @param {import('express').Application} app
 * @param {object} deps
 */
function registerSpotifyRoutes(app, deps) {
  const {
    fs,
    SPOTIFY_TOKEN_FILE,
    getSettings,
    getSpotifyTokens,
    saveSpotifyTokens,
    escapeHtmlServer,
    getOrCreateCompanionPlaylist,
    getSpotifyPlaylists
  } = deps;

  app.get('/api/spotify/status', (req, res) => {
    const tokens = getSpotifyTokens();
    const settings = getSettings();
    const hasCredentials = !!(settings.spotify?.clientId && settings.spotify?.clientSecret);
    if (!tokens || !tokens.access_token) {
      return res.json({ connected: false, hasCredentials });
    }
    const elapsed = Date.now() - (tokens.savedAt || 0);
    const expired = elapsed > (tokens.expires_in || 3600) * 1000;
    res.json({ connected: !expired, hasCredentials, needsRefresh: expired });
  });

  app.get('/api/spotify/login', (req, res) => {
    const settings = getSettings();
    if (!settings.spotify?.clientId) {
      return res.status(400).json({ error: 'Spotify Client ID not configured. Add it in Settings.' });
    }
    const scopes = [
      'streaming',
      'user-read-email',
      'user-read-private',
      'user-read-playback-state',
      'user-modify-playback-state',
      'user-read-currently-playing',
      'playlist-modify-public',
      'playlist-modify-private',
      'playlist-read-private'
    ].join(' ');

    const params = new URLSearchParams({
      response_type: 'code',
      client_id: settings.spotify.clientId,
      scope: scopes,
      redirect_uri: settings.spotify?.redirectUri || 'http://127.0.0.1:3000/spotify/callback',
      show_dialog: 'true'
    });

    res.redirect(`https://accounts.spotify.com/authorize?${params.toString()}`);
  });

  app.get('/spotify/callback', async (req, res) => {
    const { code, error } = req.query;
    if (error) {
      return res.send(`<html><body style="background:#13111a;color:#e8e0f0;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;"><div><h2>❌ Spotify auth failed</h2><p>${escapeHtmlServer(error)}</p><p><a href="/" style="color:#2dd4a8;">Return to Love Refactored</a></p></div></body></html>`);
    }

    const settings = getSettings();
    try {
      const tokenRes = await fetch('https://accounts.spotify.com/api/token', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
          'Authorization': 'Basic ' + Buffer.from(`${settings.spotify.clientId}:${settings.spotify.clientSecret}`).toString('base64')
        },
        body: new URLSearchParams({
          grant_type: 'authorization_code',
          code,
          redirect_uri: settings.spotify?.redirectUri || 'http://127.0.0.1:3000/spotify/callback'
        })
      });
      const tokens = await tokenRes.json();
      if (tokens.error) throw new Error(tokens.error_description || tokens.error);
      saveSpotifyTokens(tokens);
      res.send(`<html><body style="background:#13111a;color:#e8e0f0;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;"><div style="text-align:center;"><h2 style="color:#2dd4a8;">✅ Spotify Connected!</h2><p>You can close this tab and go back to Love Refactored.</p><script>setTimeout(()=>window.close(),2000)</script></div></body></html>`);
    } catch (err) {
      res.status(500).send(`<html><body style="background:#13111a;color:#e8e0f0;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;"><div><h2>❌ Token exchange failed</h2><p>${escapeHtmlServer(err.message)}</p><p><a href="/" style="color:#2dd4a8;">Return to Love Refactored</a></p></div></body></html>`);
    }
  });

  app.get('/api/spotify/token', async (req, res) => {
    const tokens = getSpotifyTokens();
    if (!tokens) return res.status(401).json({ error: 'Not connected to Spotify' });

    const elapsed = Date.now() - (tokens.savedAt || 0);
    const expired = elapsed > (tokens.expires_in || 3600) * 1000 - 60000;

    if (expired && tokens.refresh_token) {
      const settings = getSettings();
      try {
        const refreshRes = await fetch('https://accounts.spotify.com/api/token', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/x-www-form-urlencoded',
            'Authorization': 'Basic ' + Buffer.from(`${settings.spotify.clientId}:${settings.spotify.clientSecret}`).toString('base64')
          },
          body: new URLSearchParams({
            grant_type: 'refresh_token',
            refresh_token: tokens.refresh_token
          })
        });
        const newTokens = await refreshRes.json();
        if (newTokens.error) throw new Error(newTokens.error);
        const merged = { ...tokens, ...newTokens, refresh_token: newTokens.refresh_token || tokens.refresh_token };
        saveSpotifyTokens(merged);
        return res.json({ access_token: merged.access_token });
      } catch (err) {
        return res.status(401).json({ error: 'Token refresh failed', details: err.message });
      }
    }

    res.json({ access_token: tokens.access_token });
  });

  app.post('/api/spotify/disconnect', (req, res) => {
    if (fs.existsSync(SPOTIFY_TOKEN_FILE)) fs.unlinkSync(SPOTIFY_TOKEN_FILE);
    res.json({ success: true });
  });

  app.get('/api/spotify/search', async (req, res) => {
    const { q, type } = req.query;
    if (!q) return res.status(400).json({ error: 'Query required' });

    const tokens = getSpotifyTokens();
    if (!tokens?.access_token) return res.status(401).json({ error: 'Not connected to Spotify' });

    try {
      const searchRes = await fetch(`https://api.spotify.com/v1/search?q=${encodeURIComponent(q)}&type=${type || 'track'}&limit=5`, {
        headers: { 'Authorization': `Bearer ${tokens.access_token}` }
      });
      const data = await searchRes.json();
      res.json(data);
    } catch (err) {
      res.status(500).json({ error: err.message });
    }
  });

  app.post('/api/spotify/playlist/add', async (req, res) => {
    const { companion, trackId } = req.body;
    if (!companion || !trackId) return res.status(400).json({ error: 'companion and trackId required' });

    const tokens = getSpotifyTokens();
    if (!tokens?.access_token) return res.status(401).json({ error: 'Not connected to Spotify' });

    try {
      const playlistId = await getOrCreateCompanionPlaylist(companion, tokens);

      const itemsRes = await fetch(`https://api.spotify.com/v1/playlists/${playlistId}/items?limit=50`, {
        headers: { 'Authorization': `Bearer ${tokens.access_token}` }
      });
      if (itemsRes.ok) {
        const itemsData = await itemsRes.json();
        const trackUri = `spotify:track:${trackId}`;
        const alreadyExists = itemsData.items?.some(item => item.track?.uri === trackUri);
        if (alreadyExists) {
          return res.json({ success: true, alreadyExists: true, playlistId });
        }
      }

      const addRes = await fetch(`https://api.spotify.com/v1/playlists/${playlistId}/items`, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${tokens.access_token}`,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ uris: [`spotify:track:${trackId}`] })
      });

      if (!addRes.ok) {
        const err = await addRes.json();
        throw new Error(err.error?.message || 'Failed to add track');
      }

      res.json({ success: true, playlistId });
    } catch (err) {
      res.status(500).json({ error: err.message });
    }
  });

  app.get('/api/spotify/playlists', (req, res) => {
    res.json(getSpotifyPlaylists());
  });
}

module.exports = registerSpotifyRoutes;
