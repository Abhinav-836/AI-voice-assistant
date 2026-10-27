/**
 * Global application state for the AI Voice Assistant
 */

class AppState {
  constructor() {
    this.connection = {
      status: 'connected', // 'disconnected', 'connecting', 'connected', 'error'
      room: null,
      identity: null,
      sessionId: null,
      sessionKey: null,
      roomName: null,
      token: null,
      livekitUrl: null
    };

    this.turn = {
      version: 0,
      status: 'Ready',
      interruptionCount: 0,
      lastUpdate: 'Ready'
    };

    this.telemetry = {
      ttft: 0,       // ms
      toolTime: 0,   // ms
      ttsStart: 0,   // ms
      totalTurn: 0,  // s
      fencingActive: true,
      allOperational: true
    };

    this.audio = {
      isMicActive: false,
      isMuted: false,
      inputLevel: 0,
      outputLevel: 0,
      ttsProvider: 'Rime (coda)',
      speaker: 'celeste',
      speed: 1.00,
      autoSpeak: true
    };

    this.tools = {
      total: 0,
      completed: 0,
      cancelled: 0,
      avgLatency: 0,
      active: [],
      cancelledList: []
    };

    this.listeners = new Set();
  }

  subscribe(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  notify(event, data) {
    this.listeners.forEach(cb => {
      try { cb(event, data); } catch (e) { console.error("Listener error:", e); }
    });
  }

  setConnectionStatus(status) {
    this.connection.status = status;
    this.notify('connection_changed', status);
  }

  bumpTurnVersion(reason = "User interruption") {
    this.turn.version += 1;
    this.turn.interruptionCount += 1;
    const now = new Date();
    this.turn.lastUpdate = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    this.notify('turn_updated', this.turn);
  }

  updateTelemetry(metrics) {
    Object.assign(this.telemetry, metrics);
    this.notify('telemetry_updated', this.telemetry);
  }
}

export const state = new AppState();