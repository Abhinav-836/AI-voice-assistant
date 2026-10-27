"""
Repository data access layer for Sessions, Turns, Tool Executions, and Settings.
Encapsulates all SQL queries to keep business logic clean and maintainable.
"""

import json
import re
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from pathlib import Path
from agent.db.database import get_db_connection, init_db
from agent.utils.logger import get_logger

logger = get_logger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


DEFAULT_SETTINGS = {
    "voice_model": "coda",
    "voice_speaker": "celeste",
    "voice_speed": 1.0,
    "auto_speak": 1,
}
VOICE_SPEED_RANGE = (0.5, 2.0)
_VOICE_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


def sanitize_settings(raw: Optional[Dict[str, Any]], base: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Merge user-supplied voice settings onto `base`, keeping only valid values.

    These values go straight into the Rime TTS plugin, so anything that is
    not a short lowercase id (model, speaker) or a number in range (speed)
    is ignored instead of stored.
    """
    result = dict(DEFAULT_SETTINGS)
    if base:
        result.update({k: base[k] for k in DEFAULT_SETTINGS if k in base and base[k] is not None})
    if not isinstance(raw, dict):
        return result

    for key in ("voice_model", "voice_speaker"):
        value = raw.get(key)
        if isinstance(value, str) and _VOICE_ID_RE.match(value.strip().lower()):
            result[key] = value.strip().lower()

    speed = raw.get("voice_speed")
    try:
        if speed is not None and not isinstance(speed, bool):
            low, high = VOICE_SPEED_RANGE
            result["voice_speed"] = round(min(high, max(low, float(speed))), 2)
    except (TypeError, ValueError):
        pass

    if "auto_speak" in raw:
        result["auto_speak"] = 1 if raw.get("auto_speak") else 0

    return result


class Repository:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path
        init_db(self.db_path)

    # =========================================================================
    # SESSIONS
    # =========================================================================

    def create_session(
        self,
        session_id: str,
        room_name: str,
        client_ip: str = "",
        settings: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create and persist a new isolated session with its voice settings."""
        now = _utc_now_iso()
        voice = sanitize_settings(settings)
        with get_db_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO sessions (id, room_name, client_ip, status, created_at, updated_at)
                VALUES (?, ?, ?, 'active', ?, ?)
                """,
                (session_id, room_name, client_ip, now, now),
            )
            # Initialize default settings for session
            conn.execute(
                """
                INSERT INTO settings (session_id, voice_model, voice_speaker, voice_speed, auto_speak, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, voice["voice_model"], voice["voice_speaker"],
                 voice["voice_speed"], voice["auto_speak"], now),
            )

        return {
            "id": session_id,
            "room_name": room_name,
            "client_ip": client_ip,
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a session by ID."""
        with get_db_connection(self.db_path) as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if not row:
                return None
            return dict(row)

    def get_session_by_room(self, room_name: str) -> Optional[Dict[str, Any]]:
        """Retrieve a session by LiveKit room name."""
        with get_db_connection(self.db_path) as conn:
            row = conn.execute("SELECT * FROM sessions WHERE room_name = ?", (room_name,)).fetchone()
            if not row:
                return None
            return dict(row)

    def list_sessions(
        self,
        limit: int = 50,
        offset: int = 0,
        status: Optional[str] = None,
        search: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List sessions with optional filtering and pagination."""
        query = """
            SELECT s.*, COUNT(DISTINCT t.id) as turns_count, COUNT(DISTINCT te.id) as tools_count 
            FROM sessions s 
            LEFT JOIN turns t ON s.id = t.session_id 
            LEFT JOIN tool_executions te ON s.id = te.session_id 
            WHERE 1=1
        """
        params: List[Any] = []

        if status:
            query += " AND s.status = ?"
            params.append(status)

        if search:
            query += " AND (s.id LIKE ? OR s.room_name LIKE ?)"
            term = f"%{search}%"
            params.extend([term, term])

        query += " GROUP BY s.id ORDER BY s.created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with get_db_connection(self.db_path) as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def update_session_status(self, session_id: str, status: str) -> bool:
        """Update session lifecycle status (e.g. 'active', 'completed', 'disconnected')."""
        now = _utc_now_iso()
        with get_db_connection(self.db_path) as conn:
            cur = conn.execute(
                "UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, session_id),
            )
            return cur.rowcount > 0

    # =========================================================================
    # TURNS
    # =========================================================================

    def record_turn(
        self,
        session_id: str,
        turn_number: int,
        turn_version: int,
        sender: str,
        text: str,
        is_interrupted: bool = False,
        latency_ms: Optional[float] = None,
    ) -> int:
        """Record a user or agent speech turn."""
        now = _utc_now_iso()
        with get_db_connection(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO turns (session_id, turn_number, turn_version, sender, text, is_interrupted, latency_ms, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, turn_number, turn_version, sender, text, 1 if is_interrupted else 0, latency_ms, now),
            )
            return cur.lastrowid

    def get_session_turns(self, session_id: str) -> List[Dict[str, Any]]:
        """Get all turns for a session in chronological order."""
        with get_db_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM turns WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    # =========================================================================
    # TOOL EXECUTIONS
    # =========================================================================

    def record_tool_execution(
        self,
        session_id: str,
        turn_version: int,
        tool_name: str,
        parameters: Optional[Dict[str, Any]] = None,
        status: str = "running",
        latency_ms: Optional[float] = None,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> int:
        """Record the start or full execution of a tool call."""
        now = _utc_now_iso()
        param_str = json.dumps(parameters) if parameters else None
        res_str = json.dumps(result) if result else None
        with get_db_connection(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO tool_executions (session_id, turn_version, tool_name, parameters, status, latency_ms, result, error, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, turn_version, tool_name, param_str, status, latency_ms, res_str, error, now),
            )
            return cur.lastrowid

    def update_tool_execution(
        self,
        tool_id: int,
        status: str,
        latency_ms: Optional[float] = None,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> bool:
        """Update tool status upon completion, cancellation, or error."""
        res_str = json.dumps(result) if result else None
        with get_db_connection(self.db_path) as conn:
            cur = conn.execute(
                """
                UPDATE tool_executions
                SET status = ?, latency_ms = COALESCE(?, latency_ms), result = COALESCE(?, result), error = COALESCE(?, error)
                WHERE id = ?
                """,
                (status, latency_ms, res_str, error, tool_id),
            )
            return cur.rowcount > 0

    def get_session_tools(self, session_id: str) -> List[Dict[str, Any]]:
        """Get all tool executions for a session."""
        with get_db_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM tool_executions WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
            tools = []
            for r in rows:
                item = dict(r)
                if item.get("parameters"):
                    try:
                        item["parameters"] = json.loads(item["parameters"])
                    except Exception:
                        pass
                if item.get("result"):
                    try:
                        item["result"] = json.loads(item["result"])
                    except Exception:
                        pass
                tools.append(item)
            return tools

    # =========================================================================
    # SETTINGS
    # =========================================================================

    def get_settings(self, session_id: str) -> Dict[str, Any]:
        """Get current settings for a session, or global defaults if not found."""
        with get_db_connection(self.db_path) as conn:
            row = conn.execute("SELECT * FROM settings WHERE session_id = ?", (session_id,)).fetchone()
            if row:
                return dict(row)
        return {"session_id": session_id, **DEFAULT_SETTINGS, "updated_at": _utc_now_iso()}

    def update_settings(self, session_id: str, new_settings: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Update an existing session's settings. Returns None if the session doesn't exist.

        Settings belong to a session (foreign key), so there is no "global"
        row to write to. The old API tried to save to session "global",
        which always failed with a foreign-key error.
        """
        if not self.get_session(session_id):
            return None
        now = _utc_now_iso()
        voice = sanitize_settings(new_settings, base=self.get_settings(session_id))

        with get_db_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO settings (session_id, voice_model, voice_speaker, voice_speed, auto_speak, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    voice_model = excluded.voice_model,
                    voice_speaker = excluded.voice_speaker,
                    voice_speed = excluded.voice_speed,
                    auto_speak = excluded.auto_speak,
                    updated_at = excluded.updated_at
                """,
                (session_id, voice["voice_model"], voice["voice_speaker"],
                 voice["voice_speed"], voice["auto_speak"], now),
            )

        return {"session_id": session_id, **voice, "updated_at": now}


# Global repository instance - lazy initialization for test isolation
_repo_instance: Optional[Repository] = None


def get_repository(db_path: Optional[Path] = None) -> Repository:
    """Get the global repository instance, optionally with a custom db path."""
    global _repo_instance
    if _repo_instance is None or db_path is not None:
        _repo_instance = Repository(db_path)
    return _repo_instance


# Backward compatibility - but now it's a property that can be overridden for tests
class _RepoProxy:
    """Proxy that delegates to get_repository() for lazy loading."""
    
    def __getattr__(self, name):
        return getattr(get_repository(), name)
    
    def __setattr__(self, name, value):
        if name.startswith('_'):
            super().__setattr__(name, value)
        else:
            setattr(get_repository(), name, value)


# Use a proxy that always delegates to the current repository instance
repo = _RepoProxy()