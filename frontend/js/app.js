/**
 * Minimal Voice Assistant
 * Clean, fast, focused UX
 */

import { state } from './state.js';
import { livekit } from './livekit.js';
import { voiceVisualizer } from './voice.js';
import { notifications } from './notifications.js';
import { metricsTracker } from './metrics.js';
import { getVoiceSettings, saveVoiceSettings, listSavedSessions, forgetSession } from './session-store.js';

// State
let conversationActive = false;
let isFirstMessage = true;
let hasGreeted = false;

document.addEventListener('DOMContentLoaded', async () => {
  console.log("🚀 Starting Voice Assistant");

  // Init notifications
  notifications.init();

  // Start timer
  metricsTracker.startTimer();

  // Connect to LiveKit — silently (no toasts on success)
  try {
    await livekit.connect();
    if (livekit.isConnected) {
      updateStatus('connected', 'Ready');
    } else {
      updateStatus('error', 'Connection failed');
    }
  } catch (e) {
    console.warn("LiveKit connection:", e);
    updateStatus('error', 'Connection failed');
    notifications.error('Failed to connect to voice agent. Please refresh.');
  }

  // State subscriptions
  state.subscribe((event, data) => {
    if (event === 'connection_changed') {
      if (data === 'connected') {
        updateStatus('connected', 'Ready');
      }
      if (data === 'disconnected') {
        updateStatus('error', 'Disconnected');
        resetUI();
      }
    }

    if (event === 'data_channel_message' && data) {
      // User transcript
      if (data.type === 'transcript' && data.sender === 'You' && data.is_final) {
        addMessage('user', data.text);
        updateStats();
      }

      // Agent response
      if (data.type === 'agent_response') {
        addMessage('agent', data.text, data.version);
        updateStatus('connected', 'Ready');
        document.getElementById('status-dot')?.classList.remove('speaking');
        updateStats();
        if (data.latency_ms) {
          updateLatency(data.latency_ms);
        }
        if (!hasGreeted) {
          hasGreeted = true;
        }
      }

      // Interruption
      if (data.type === 'interruption') {
        addInterruption(data.text);
        updateStatus('listening', 'Interrupted...');
        updateStats();
      }

      // Tool events
      if (data.type === 'tool_start') {
        updateToolActivity(data.name, 'running', '');
        updateToolBadge();
      }
      if (data.type === 'tool_complete') {
        updateToolActivity(data.name, 'completed', `${data.latency_ms || 0}ms`);
        if (data.latency_ms) {
          updateLatency(data.latency_ms);
        }
        updateToolBadge();
      }
      if (data.type === 'tool_cancel') {
        updateToolActivity(data.name, 'cancelled', '');
        updateToolBadge();
      }
    }

    if (event === 'turn_updated') {
      updateStats();
    }
  });

  // ORB: Click to toggle conversation
  const orb = document.getElementById('voice-orb');
  if (orb) {
    orb.addEventListener('click', toggleConversation);
  }

  // Keyboard shortcut: Space to toggle
  document.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT') return;
    if (e.key === ' ' || e.key === 'Space') {
      e.preventDefault();
      toggleConversation();
    }
  });

  // Chat form
  const chatForm = document.getElementById('form-chat-input');
  if (chatForm) {
    chatForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const input = document.getElementById('input-chat-text');
      const text = input?.value?.trim();
      if (!text) return;

      input.value = '';
      input.disabled = true;

      addMessage('user', text);
      updateStats();

      const sent = await livekit.sendTextMessage(text);
      if (!sent) {
        notifications.error('Failed to send message');
        input.disabled = false;
      } else {
        setTimeout(() => { input.disabled = false; }, 500);
      }
    });
  }

  // Sessions button
  document.getElementById('btn-sessions')?.addEventListener('click', showSessionsModal);

  // Settings button
  document.getElementById('btn-settings')?.addEventListener('click', showSettingsModal);

  // Hangup button
  document.getElementById('btn-hangup')?.addEventListener('click', endSession);

  // Modal close buttons
  document.getElementById('modal-sessions-close')?.addEventListener('click', () => {
    document.getElementById('modal-sessions').style.display = 'none';
  });
  document.getElementById('modal-settings-close')?.addEventListener('click', () => {
    document.getElementById('modal-settings').style.display = 'none';
  });

  // Close modals on backdrop click
  document.querySelectorAll('.modal-overlay').forEach(modal => {
    modal.addEventListener('click', (e) => {
      if (e.target === modal) {
        modal.style.display = 'none';
      }
    });
  });

  // Settings: Save
  document.getElementById('btn-save-settings')?.addEventListener('click', saveSettings);

  // Settings: Speed slider
  const speedSlider = document.getElementById('setting-voice-speed');
  const speedVal = document.getElementById('setting-voice-speed-val');
  if (speedSlider && speedVal) {
    speedSlider.addEventListener('input', () => {
      speedVal.textContent = parseFloat(speedSlider.value).toFixed(2) + 'x';
    });
  }

  // Load settings
  loadSettings();

  // Initial stats update
  setTimeout(updateStats, 500);
});

// ============================================================
// Core Functions
// ============================================================

async function toggleConversation() {
  const orb = document.getElementById('voice-orb');
  const orbLabel = document.getElementById('orb-label');
  const orbSubLabel = document.getElementById('orb-sub-label');
  const orbGlow = document.getElementById('orb-glow');
  const hangupBtn = document.getElementById('btn-hangup');
  const statusText = document.getElementById('status-text');

  if (!conversationActive) {
    // Guard before flipping the UI state.
    // Without this, clicking the orb while LiveKit is still connecting
    // would leave the UI stuck in "Listening" even though the mic never started.
    if (!livekit.isConnected) {
      if (state.connection.status === 'error') {
        notifications.error('Voice service is unavailable right now. Refresh the page to try again.');
      } else {
        notifications.warning('Still connecting to voice agent — please wait a moment and try again.');
      }
      return;
    }

    // START
    conversationActive = true;

    try {
      // await + error handling.
      await voiceVisualizer.startConversation();
    } catch (e) {
      conversationActive = false;
      notifications.error('Could not start microphone: ' + (e?.message || e));
      return;
    }

    if (orb) orb.classList.add('listening');
    if (orbLabel) {
      orbLabel.textContent = 'Listening...';
      orbLabel.className = 'orb-label active';
    }
    if (orbSubLabel) orbSubLabel.textContent = 'Tap to pause';
    if (orbGlow) orbGlow.classList.add('active');
    if (hangupBtn) hangupBtn.style.display = 'flex';
    if (statusText) statusText.textContent = 'Listening...';

    updateStatus('listening', 'Listening...');
    notifications.success('🎤 Listening... Speak now!');
  } else {
    // PAUSE
    conversationActive = false;
    try {
      await voiceVisualizer.stopConversation();
    } catch (e) {
      console.warn("stopConversation error:", e);
    }

    if (orb) orb.classList.remove('listening', 'speaking');
    if (orbLabel) {
      orbLabel.textContent = 'Paused';
      orbLabel.className = 'orb-label';
    }
    if (orbSubLabel) orbSubLabel.textContent = 'Tap to resume';
    if (orbGlow) orbGlow.classList.remove('active');
    if (hangupBtn) hangupBtn.style.display = 'none';
    if (statusText) statusText.textContent = 'Paused';

    updateStatus('connected', 'Paused');
    notifications.info('⏸️ Paused');
  }
}

function endSession() {
  livekit.disconnect();
  notifications.warning('Session ended');
  resetUI();
}

function resetUI() {
  conversationActive = false;
  isFirstMessage = true;
  hasGreeted = false;
  const orb = document.getElementById('voice-orb');
  const orbLabel = document.getElementById('orb-label');
  const orbSubLabel = document.getElementById('orb-sub-label');
  const orbGlow = document.getElementById('orb-glow');
  const hangupBtn = document.getElementById('btn-hangup');

  if (orb) orb.classList.remove('listening', 'speaking');
  if (orbLabel) {
    orbLabel.textContent = 'Tap to start';
    orbLabel.className = 'orb-label';
  }
  if (orbSubLabel) orbSubLabel.textContent = 'Press Space to toggle';
  if (orbGlow) orbGlow.classList.remove('active');
  if (hangupBtn) hangupBtn.style.display = 'none';
  updateStatus('error', 'Disconnected');
}

function updateStatus(dotState, text) {
  const dot = document.getElementById('status-dot');
  const label = document.getElementById('status-text');
  if (dot) {
    dot.className = 'status-dot';
    if (dotState !== 'error') {
      dot.classList.add(dotState);
    }
  }
  if (label) label.textContent = text;
}

// ============================================================
// Transcript Functions
// ============================================================

function addMessage(type, text, version) {
  const container = document.getElementById('transcript-messages');
  if (!container) return;

  const empty = container.querySelector('.empty-state');
  if (empty) empty.remove();

  const msg = document.createElement('div');
  msg.className = `message ${type}`;

  let versionHtml = '';
  if (version) {
    versionHtml = `<span style="font-size:10px;color:var(--accent-purple);margin-left:6px;">v${version}</span>`;
  }

  const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  msg.innerHTML = `
    <div class="message-content">${escapeHtml(text)}</div>
    <div class="message-meta">${type === 'user' ? 'You' : 'Agent'} • ${time} ${versionHtml}</div>
  `;
  container.appendChild(msg);
  container.scrollTop = container.scrollHeight;
}

function addInterruption(text) {
  const container = document.getElementById('transcript-messages');
  if (!container) return;

  const banner = document.createElement('div');
  banner.className = 'interruption-banner';
  banner.innerHTML = `⚡ Interrupted: ${escapeHtml(text)}`;
  container.appendChild(banner);
  container.scrollTop = container.scrollHeight;
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

// ============================================================
// Stats Functions
// ============================================================

function updateStats() {
  try {
    const versionEl = document.getElementById('stat-version');
    if (versionEl) versionEl.textContent = `v${state.turn.version}`;

    const turnCount = document.querySelectorAll('.message.user, .message.agent').length;
    const turnsEl = document.getElementById('stat-turns');
    if (turnsEl) turnsEl.textContent = turnCount || 0;

    const interEl = document.getElementById('stat-interruptions');
    if (interEl) interEl.textContent = state.turn.interruptionCount || 0;

    const voiceStatus = document.getElementById('voice-status-text');
    if (voiceStatus) {
      if (conversationActive) {
        voiceStatus.textContent = '🎤 Listening';
        voiceStatus.style.color = 'var(--accent-cyan)';
      } else {
        voiceStatus.textContent = '⏸️ Paused';
        voiceStatus.style.color = 'var(--text-muted)';
      }
    }
  } catch (e) {
    console.debug('Stats update error:', e);
  }
}

// ============================================================
// Latency Update Function
// ============================================================

function updateLatency(ms) {
  const latencyEl = document.getElementById('stat-latency');
  if (latencyEl) {
    if (ms) {
      latencyEl.textContent = `${Math.round(ms)} ms`;
      latencyEl.style.color = ms < 1000 ? 'var(--accent-emerald)' : ms < 2000 ? 'var(--accent-amber)' : 'var(--accent-rose)';
    }
  }
}

// ============================================================
// Tool Activity Functions
// ============================================================

function updateToolActivity(toolName, status, detail) {
  const container = document.getElementById('side-tool-activity-list');
  if (!container) return;

  const empty = document.getElementById('side-tool-empty');
  if (empty) empty.remove();

  const existingItems = container.querySelectorAll('.tool-activity-item');
  let found = false;

  existingItems.forEach(item => {
    const nameEl = item.querySelector('.tool-name');
    if (nameEl && nameEl.textContent === toolName) {
      const statusEl = item.querySelector('.tool-status');
      if (statusEl) {
        let statusClass = 'running';
        let statusText = '⏳';
        let statusColor = 'var(--accent-cyan)';

        if (status === 'completed') {
          statusClass = 'completed';
          statusText = '✅';
          statusColor = 'var(--accent-emerald)';
        } else if (status === 'cancelled') {
          statusClass = 'cancelled';
          statusText = '❌';
          statusColor = 'var(--accent-rose)';
        }

        statusEl.className = `tool-status ${statusClass}`;
        statusEl.style.color = statusColor;
        statusEl.textContent = `${statusText} ${detail || ''}`;
        found = true;
      }
    }
  });

  if (!found && status === 'running') {
    const item = document.createElement('div');
    item.className = 'tool-activity-item';
    item.innerHTML = `
      <span class="tool-name">${escapeHtml(toolName)}</span>
      <span class="tool-status running" style="color:var(--accent-cyan);">⏳</span>
    `;
    container.prepend(item);
  }

  // Keep only last 8
  while (container.children.length > 8) {
    container.removeChild(container.lastChild);
  }
}

function updateToolBadge() {
  const container = document.getElementById('side-tool-activity-list');
  const badge = document.getElementById('badge-active-tools');
  if (!badge || !container) return;

  const running = container.querySelectorAll('.tool-status.running').length;
  const total = container.querySelectorAll('.tool-activity-item').length;

  if (total === 0) {
    badge.textContent = '0 running';
    let empty = document.getElementById('side-tool-empty');
    if (!empty) {
      empty = document.createElement('div');
      empty.id = 'side-tool-empty';
      empty.style.cssText = 'font-size:12px;color:var(--text-muted);text-align:center;padding:8px 0;';
      empty.textContent = 'No active tools';
      container.appendChild(empty);
    }
  } else {
    badge.textContent = running > 0 ? `${running} running` : `${total} completed`;
  }
}

// ============================================================
// Modal Functions
// ============================================================

// Session History shows this browser's own sessions. The server only gives
// a session's key to the browser that created it, so there is no way (and
// no endpoint) to browse other people's conversations.
async function showSessionsModal() {
  const modal = document.getElementById('modal-sessions');
  const list = document.getElementById('sessions-list');
  if (!modal || !list) return;

  modal.style.display = 'flex';
  const saved = listSavedSessions();
  if (saved.length === 0) {
    list.innerHTML = '<div class="loading-text">No sessions yet on this device</div>';
    return;
  }
  list.innerHTML = '<div class="loading-text">Loading sessions...</div>';

  const rows = await Promise.all(saved.map(async (s) => {
    try {
      const resp = await fetch(`/api/sessions/${encodeURIComponent(s.id)}`, {
        headers: { 'X-Session-Key': s.key },
      });
      if (resp.status === 404) {
        forgetSession(s.id); // deleted on the server, or the server was reset
        return null;
      }
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      return {
        id: s.id,
        createdAt: data.session?.created_at || s.createdAt,
        status: data.session?.status || 'active',
        turns: (data.turns || []).length,
        tools: (data.tools || []).length,
      };
    } catch {
      return { id: s.id, createdAt: s.createdAt, status: 'unavailable', turns: 0, tools: 0 };
    }
  }));

  const sessions = rows.filter(Boolean);
  if (sessions.length === 0) {
    list.innerHTML = '<div class="loading-text">No sessions yet on this device</div>';
    return;
  }
  list.innerHTML = sessions.map((s) => `
    <div class="session-item">
      <div class="session-id">${escapeHtml(s.id)}${s.id === state.connection.sessionId ? ' (current)' : ''}</div>
      <div class="session-meta">
        <span>${s.createdAt ? escapeHtml(new Date(s.createdAt).toLocaleString()) : 'Unknown'}</span>
        <span>${s.turns} turns</span>
        <span>${s.tools} tool calls</span>
        <span>${escapeHtml(s.status)}</span>
      </div>
    </div>
  `).join('');
}

function showSettingsModal() {
  const modal = document.getElementById('modal-settings');
  if (modal) modal.style.display = 'flex';
}

// Voice settings are saved in this browser and sent when a session starts,
// since the agent picks its voice when it joins the room. (The old version
// saved them to a "global" session that didn't exist, so every save failed
// with a database foreign-key error.)
function loadSettings() {
  const settings = getVoiceSettings();
  const modelEl = document.getElementById('setting-tts-model');
  const speakerEl = document.getElementById('setting-tts-speaker');
  const speedEl = document.getElementById('setting-voice-speed');
  const speedValEl = document.getElementById('setting-voice-speed-val');

  if (modelEl) modelEl.value = settings.voice_model;
  if (speakerEl) speakerEl.value = settings.voice_speaker;
  if (speedEl) {
    const speed = Number(settings.voice_speed) || 1.0;
    speedEl.value = speed;
    if (speedValEl) speedValEl.textContent = speed.toFixed(2) + 'x';
  }
}

async function saveSettings() {
  const settings = {
    voice_model: document.getElementById('setting-tts-model')?.value || 'coda',
    voice_speaker: document.getElementById('setting-tts-speaker')?.value || 'celeste',
    voice_speed: parseFloat(document.getElementById('setting-voice-speed')?.value || 1.0),
  };
  saveVoiceSettings(settings);

  const modal = document.getElementById('modal-settings');
  if (modal) modal.style.display = 'none';

  // Apply the new voice right away by starting a fresh session, unless a
  // conversation is in progress (then it applies to the next one).
  if (conversationActive) {
    notifications.success('✅ Saved — the new voice applies to your next conversation');
    return;
  }
  notifications.success('✅ Settings saved');
  try {
    await livekit.disconnect();
    await livekit.connect();
  } catch (e) {
    notifications.error('Could not restart the session with the new voice');
  }
}