// © 2024-2026 Megan Neves and Tiara Young. All rights reserved.
// Love Refactored — https://github.com/Love-Encoded/Love-Refactored
// Licensed under the Love Refactored / Tanevan License. See LICENSE.md.

'use strict';

/**
 * @param {import('express').Application} app
 * @param {object} deps
 */
function registerCalendarRoutes(app, deps) {
  const {
    makeId,
    getCalendarEvents,
    saveCalendarEvents,
    expandRecurringEvents
  } = deps;

  app.get('/api/calendar', (req, res) => {
    try {
      let events = getCalendarEvents();
      const { from, to, companion } = req.query;

      if (from || to) {
        const fromDate = from || '2000-01-01';
        const toDate = to || '2099-12-31';
        events = expandRecurringEvents(events, fromDate, toDate);
      }

      if (companion) {
        events = events.filter(e => e.companions && e.companions.includes(companion));
      }

      res.json(events);
    } catch (err) {
      res.status(500).json({ error: 'Failed to load calendar events' });
    }
  });

  app.get('/api/calendar/upcoming', (req, res) => {
    try {
      const events = getCalendarEvents();
      const today = new Date();
      today.setHours(0, 0, 0, 0);
      const nextWeek = new Date(today);
      nextWeek.setDate(nextWeek.getDate() + 7);

      const upcoming = expandRecurringEvents(events, today.toISOString().split('T')[0], nextWeek.toISOString().split('T')[0]);
      res.json(upcoming);
    } catch (err) {
      res.status(500).json({ error: 'Failed to load upcoming events' });
    }
  });

  app.post('/api/calendar', (req, res) => {
    try {
      const events = getCalendarEvents();
      const event = {
        id: makeId(),
        title: req.body.title || 'Untitled Event',
        date: req.body.date || new Date().toISOString().split('T')[0],
        time: req.body.time || null,
        endTime: req.body.endTime || null,
        allDay: req.body.allDay !== undefined ? req.body.allDay : true,
        notes: req.body.notes || '',
        category: req.body.category || 'personal',
        createdBy: req.body.createdBy || 'user',
        companions: req.body.companions || [],
        recurrence: req.body.recurrence || null,
        tags: req.body.tags || [],
        createdAt: new Date().toISOString(),
        updatedAt: new Date().toISOString()
      };
      events.push(event);
      saveCalendarEvents(events);
      res.json(event);
    } catch (err) {
      res.status(500).json({ error: 'Failed to create event' });
    }
  });

  app.put('/api/calendar/:id', (req, res) => {
    try {
      const events = getCalendarEvents();
      const event = events.find(e => e.id === req.params.id);
      if (!event) return res.status(404).json({ error: 'Event not found' });

      const allowed = ['title', 'date', 'time', 'endTime', 'allDay', 'notes', 'category', 'companions', 'recurrence', 'tags'];
      for (const key of allowed) {
        if (req.body[key] !== undefined) event[key] = req.body[key];
      }
      event.updatedAt = new Date().toISOString();
      saveCalendarEvents(events);
      res.json(event);
    } catch (err) {
      res.status(500).json({ error: 'Failed to update event' });
    }
  });

  app.delete('/api/calendar/:id', (req, res) => {
    try {
      let events = getCalendarEvents();
      const before = events.length;
      events = events.filter(e => e.id !== req.params.id);
      if (events.length === before) return res.status(404).json({ error: 'Event not found' });
      saveCalendarEvents(events);
      res.json({ success: true });
    } catch (err) {
      res.status(500).json({ error: 'Failed to delete event' });
    }
  });
}

module.exports = registerCalendarRoutes;
