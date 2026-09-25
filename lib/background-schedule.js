'use strict';

/**
 * Global + per-companion background memory job scheduling (reflections only).
 */

const DEFAULT_REFLECTION = { enabled: true, time: '03:00' };

function normalizeTime(timeStr, fallback = '03:00') {
  const m = String(timeStr || fallback).trim().match(/^(\d{1,2}):(\d{2})$/);
  if (!m) return fallback;
  const hours = Math.min(23, Math.max(0, parseInt(m[1], 10)));
  const minutes = Math.min(59, Math.max(0, parseInt(m[2], 10)));
  return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}`;
}

function parseTimeMinutes(timeStr) {
  const normalized = normalizeTime(timeStr);
  const [h, m] = normalized.split(':').map(Number);
  return h * 60 + m;
}

function formatLocalDate(d = new Date()) {
  const y = d.getFullYear();
  const mo = String(d.getMonth() + 1).padStart(2, '0');
  const da = String(d.getDate()).padStart(2, '0');
  return `${y}-${mo}-${da}`;
}

function isTimeReached(timeStr, now = new Date()) {
  const current = now.getHours() * 60 + now.getMinutes();
  return current >= parseTimeMinutes(timeStr);
}

function getGlobalReflectionSchedule(settings) {
  const raw = settings?.memory?.reflectionSchedule || {};
  return {
    enabled: raw.enabled !== false,
    time: normalizeTime(raw.time, DEFAULT_REFLECTION.time),
  };
}

function getGlobalBackgroundSchedule(settings) {
  return {
    reflections: getGlobalReflectionSchedule(settings),
  };
}

function normalizeCompanionBackgroundSchedule(raw) {
  const bg = raw && typeof raw === 'object' ? raw : {};
  const mode = bg.mode === 'off' || bg.mode === 'custom' ? bg.mode : 'global';
  const reflections = bg.reflections && typeof bg.reflections === 'object' ? bg.reflections : {};
  return {
    mode,
    reflections: {
      enabled: reflections.enabled !== false,
      time: normalizeTime(reflections.time, DEFAULT_REFLECTION.time),
    },
  };
}

function resolveJobConfig(settings, card, job) {
  if (job !== 'reflections') {
    return { enabled: false, source: 'unsupported', job };
  }
  const global = getGlobalBackgroundSchedule(settings).reflections;
  const bg = normalizeCompanionBackgroundSchedule(card?.backgroundSchedule);

  if (bg.mode === 'off') {
    return { enabled: false, source: 'off', job };
  }

  if (bg.mode === 'custom') {
    const custom = bg.reflections || {};
    if (custom.enabled === false) {
      return { enabled: false, source: 'custom_off', job };
    }
    return {
      enabled: true,
      time: custom.time || global.time,
      source: 'custom',
      job,
    };
  }

  if (!global.enabled) {
    return { enabled: false, source: 'global_off', job };
  }

  return { ...global, source: 'global', job };
}

function companionState(state, companionKey) {
  if (!state.companions) state.companions = {};
  if (!state.companions[companionKey]) state.companions[companionKey] = {};
  return state.companions[companionKey];
}

function getCompanionJobState(state, companionKey, job) {
  const c = (state.companions || {})[companionKey] || {};
  return c[job] || {};
}

function isReflectionDueForCompanion(settings, card, companionKey, state, now = new Date()) {
  if (settings?.memory?.enabled === false) return false;
  const cfg = resolveJobConfig(settings, card, 'reflections');
  if (!cfg.enabled) return false;
  const jobState = getCompanionJobState(state, companionKey, 'reflections');
  const today = formatLocalDate(now);
  if (jobState.lastRunDate === today) return false;
  if (!isTimeReached(cfg.time, now)) return false;
  if (jobState.lastFailedAttemptAt) {
    const elapsed = now.getTime() - new Date(jobState.lastFailedAttemptAt).getTime();
    if (Number.isFinite(elapsed) && elapsed >= 0 && elapsed < 30 * 60 * 1000) return false;
  }
  return true;
}

function stampReflectionRun(state, companionKey, success, now = new Date()) {
  const c = companionState(state, companionKey);
  if (!c.reflections) c.reflections = {};
  if (success) {
    c.reflections.lastRunDate = formatLocalDate(now);
    c.reflections.lastRunAt = now.toISOString();
    delete c.reflections.lastFailedAttemptAt;
  }
  return state;
}

function stampReflectionFailure(state, companionKey, now = new Date()) {
  const c = companionState(state, companionKey);
  if (!c.reflections) c.reflections = {};
  c.reflections.lastFailedAttemptAt = now.toISOString();
  return state;
}

function computeNextReflectionRunIso(timeStr, companionKey, state, now = new Date()) {
  const jobState = getCompanionJobState(state, companionKey, 'reflections');
  const { hours, minutes } = (() => {
    const t = normalizeTime(timeStr);
    const [h, m] = t.split(':').map(Number);
    return { hours: h, minutes: m };
  })();
  const next = new Date(now);
  next.setHours(hours, minutes, 0, 0);
  const today = formatLocalDate(now);
  if (jobState.lastRunDate === today || next <= now) {
    next.setDate(next.getDate() + 1);
    next.setHours(hours, minutes, 0, 0);
  }
  return next.toISOString();
}

module.exports = {
  DEFAULT_REFLECTION,
  normalizeTime,
  formatLocalDate,
  getGlobalReflectionSchedule,
  getGlobalBackgroundSchedule,
  normalizeCompanionBackgroundSchedule,
  resolveJobConfig,
  companionState,
  getCompanionJobState,
  isReflectionDueForCompanion,
  stampReflectionRun,
  stampReflectionFailure,
  computeNextReflectionRunIso,
};
