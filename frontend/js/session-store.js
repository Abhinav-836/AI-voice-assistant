/**
 * Per-browser storage for voice settings and this browser's own sessions.
 *
 * The server only hands a session's secret key to the browser that created
 * it, so "Session History" lists the sessions saved here, not everyone's.
 * Every access is wrapped in try/catch: storage can be unavailable (private
 * mode, blocked cookies) and the app must keep working without it.
 */

const SETTINGS_KEY = 'voiceAssistant.settings';
const SESSIONS_KEY = 'voiceAssistant.sessions';
const MAX_SAVED_SESSIONS = 20;

export const DEFAULT_VOICE_SETTINGS = {
  voice_model: 'coda',
  voice_speaker: 'celeste',
  voice_speed: 1.0,
};

function readJson(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch {
    return fallback;
  }
}

function writeJson(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Storage unavailable: settings simply won't persist across reloads.
  }
}

export function getVoiceSettings() {
  return { ...DEFAULT_VOICE_SETTINGS, ...readJson(SETTINGS_KEY, {}) };
}

export function saveVoiceSettings(settings) {
  writeJson(SETTINGS_KEY, { ...getVoiceSettings(), ...settings });
}

export function rememberSession({ id, key, createdAt }) {
  if (!id || !key) return;
  const sessions = listSavedSessions().filter((s) => s.id !== id);
  sessions.unshift({ id, key, createdAt: createdAt || new Date().toISOString() });
  writeJson(SESSIONS_KEY, sessions.slice(0, MAX_SAVED_SESSIONS));
}

export function listSavedSessions() {
  const sessions = readJson(SESSIONS_KEY, []);
  return Array.isArray(sessions) ? sessions.filter((s) => s && s.id && s.key) : [];
}

export function forgetSession(id) {
  writeJson(SESSIONS_KEY, listSavedSessions().filter((s) => s.id !== id));
}