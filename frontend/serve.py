"""
HTTP server for the AI Voice Assistant: serves the frontend and a small JSON API.

Access model:
  * POST /api/session creates an isolated session and returns, once, a secret
    session key (agent/security.py). Only that browser can use the session.
  * Everything that touches a session afterwards (new LiveKit token,
    transcript, settings) requires the session key.
  * Listing all sessions is an admin endpoint (Authorization: Bearer
    ADMIN_TOKEN) and is disabled while ADMIN_TOKEN is unset.
"""

import os
import re
import sys
import json
import uuid
import time
import functools
import http.server
import urllib.parse
from collections import defaultdict
import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
from dotenv import load_dotenv

FRONTEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = FRONTEND_DIR.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

from livekit import api
from agent.config import config
from agent.security import make_session_key, verify_session_key, is_admin
from agent.tools.stocks import _fetch_quote_sync
from agent.db.repository import repo, DEFAULT_SETTINGS
from agent.utils.logger import get_logger

logger = get_logger(__name__)

LIVEKIT_URL = config.livekit_url
LIVEKIT_API_KEY = config.livekit_api_key
LIVEKIT_API_SECRET = config.livekit_api_secret
ALLOWED_ORIGINS = config.allowed_origins

RATE_LIMIT_WINDOW_SECONDS = 60.0
RATE_LIMIT_MAX_REQUESTS = config.rate_limit_per_minute
_ip_request_timestamps: Dict[str, List[float]] = defaultdict(list)
_last_rate_limit_sweep = 0.0
MAX_BODY_BYTES = 65536
MAX_COMPARE_SYMBOLS = 5

TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-^=]{0,14}$")
IDENTITY_RE = re.compile(r"[^A-Za-z0-9_-]")

# Static files that must never be served even though they live in this folder.
BLOCKED_STATIC = {"/serve.py", "/package.json"}


def _is_rate_limited(client_ip: str) -> bool:
    global _last_rate_limit_sweep
    now = time.monotonic()
    window_start = now - RATE_LIMIT_WINDOW_SECONDS

    # Drop idle IPs every minute so the table doesn't grow forever.
    if now - _last_rate_limit_sweep > RATE_LIMIT_WINDOW_SECONDS:
        for ip in [ip for ip, ts in _ip_request_timestamps.items() if not ts or ts[-1] <= window_start]:
            del _ip_request_timestamps[ip]
        _last_rate_limit_sweep = now

    timestamps = [t for t in _ip_request_timestamps[client_ip] if t > window_start]
    timestamps.append(now)
    _ip_request_timestamps[client_ip] = timestamps
    return len(timestamps) > RATE_LIMIT_MAX_REQUESTS


def _livekit_configured() -> bool:
    return bool(LIVEKIT_API_KEY and LIVEKIT_API_SECRET and LIVEKIT_URL)


def _make_livekit_token(identity: str, room_name: str) -> str:
    if not (LIVEKIT_API_KEY and LIVEKIT_API_SECRET):
        raise ValueError("LIVEKIT_API_KEY and LIVEKIT_API_SECRET must be configured")
    token = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(api.VideoGrants(room_join=True, room=room_name))
        .with_ttl(datetime.timedelta(minutes=30))
    )
    return token.to_jwt()


def _clean_identity(raw: Any) -> str:
    """Participant names go into the LiveKit token; keep them short and plain."""
    if isinstance(raw, str):
        cleaned = IDENTITY_RE.sub("", raw)[:48]
        if cleaned:
            return cleaned
    return f"user-{uuid.uuid4().hex[:8]}"


def _int_param(query_params: Dict[str, List[str]], name: str, default: int) -> Optional[int]:
    try:
        return int(query_params.get(name, [str(default)])[0])
    except (TypeError, ValueError):
        return None


class ProductionServerHandler(http.server.SimpleHTTPRequestHandler):
    """Request handler for the JSON API and the static frontend."""

    def __init__(self, *args, **kwargs):
        self.directory = str(FRONTEND_DIR)
        super().__init__(*args, **kwargs)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_client_ip(self) -> str:
        # X-Forwarded-For is only trustworthy when a reverse proxy we control
        # sets it. The proxy appends the address it saw, so the LAST entry is
        # the real client; earlier entries can be forged by the client.
        if config.trust_proxy:
            forwarded = self.headers.get("X-Forwarded-For")
            if forwarded:
                last = forwarded.split(",")[-1].strip()
                if last:
                    return last
        return self.client_address[0] if self.client_address else "127.0.0.1"

    def _send_cors_headers(self):
        req_origin = self.headers.get("Origin", "")
        if "*" in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", "*")
        elif req_origin and req_origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", req_origin)
            self.send_header("Vary", "Origin")
        # Unknown origins get no CORS header at all. (Sending
        # "Access-Control-Allow-Origin: null" would allow sandboxed iframes,
        # whose origin is literally "null".)

        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization, X-Requested-With, X-Session-Key",
        )

    def _send_security_headers(self):
        self._send_cors_headers()
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "microphone=(self)")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com data:; "
            "connect-src 'self' wss: https:; "
            "img-src 'self' data: https:; "
            "media-src 'self' blob: mediastream:;"
        )

    def _send_json_response(self, status_code: int, data: Any):
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json_body(self) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            return None, "Invalid Content-Length"

        if content_length > MAX_BODY_BYTES:
            return None, f"Payload exceeds maximum allowed size of {MAX_BODY_BYTES} bytes"

        if content_length == 0:
            return {}, None

        try:
            raw_data = self.rfile.read(content_length).decode("utf-8")
            data = json.loads(raw_data)
        except Exception:
            return None, "Malformed JSON body"
        if not isinstance(data, dict):
            return None, "Body must be a JSON object"
        return data, None

    def _rate_limited_response(self) -> bool:
        """Send a 429 and return True if this client is over the limit."""
        if _is_rate_limited(self._get_client_ip()):
            self.send_response(429)
            self.send_header("Retry-After", "60")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "Rate limit exceeded. Please wait a moment."}')
            return True
        return False

    def _authorized_session(self, session_id: Optional[str], key: Optional[str]):
        """Return the session if `key` unlocks it, else send an error and return None.

        A wrong key and an unknown session both get 404, so the API never
        confirms whether a guessed session id exists.
        """
        if not session_id:
            self._send_json_response(400, {"error": "session_id is required"})
            return None
        session = repo.get_session(session_id) if verify_session_key(session_id, key) else None
        if not session:
            self._send_json_response(404, {"error": "Session not found"})
            return None
        return session

    def _run(self, handler):
        """Run a request handler; unexpected errors become a JSON 500."""
        try:
            handler()
        except Exception:
            logger.exception(f"Unhandled error for {self.command} {self.path}")
            try:
                self._send_json_response(500, {"error": "Internal server error"})
            except Exception:
                pass

    # ------------------------------------------------------------------
    # HTTP verbs
    # ------------------------------------------------------------------

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        self._run(self._handle_get)

    def do_POST(self):
        self._run(self._handle_post)

    def do_PUT(self):
        self._run(self._handle_put)

    # ------------------------------------------------------------------
    # GET
    # ------------------------------------------------------------------

    def _handle_get(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query_params = urllib.parse.parse_qs(parsed_url.query)

        if path == "/health":
            return self._send_json_response(200, {
                "status": "healthy",
                "timestamp": time.time(),
                "service": "ai-voice-assistant",
            })

        if path == "/ready":
            return self._send_json_response(200, {
                "status": "ready" if _livekit_configured() else "degraded",
                "livekit_configured": _livekit_configured(),
                "timestamp": time.time(),
            })

        if path.startswith("/api/"):
            if self._rate_limited_response():
                return None
            return self._handle_api_get(path, query_params)

        if path in BLOCKED_STATIC or path.startswith("/__pycache__") or "/." in path:
            return self._send_json_response(404, {"error": "Not found"})

        return super().do_GET()

    def _handle_api_get(self, path: str, query_params: Dict[str, List[str]]):
        if path.startswith("/api/sessions/"):
            session_id = path[len("/api/sessions/"):].strip("/")
            session = self._authorized_session(session_id, self.headers.get("X-Session-Key"))
            if not session:
                return None
            return self._send_json_response(200, {
                "session": session,
                "turns": repo.get_session_turns(session_id),
                "tools": repo.get_session_tools(session_id),
                "settings": repo.get_settings(session_id),
            })

        if path == "/api/sessions":
            if not is_admin(self.headers.get("Authorization")):
                return self._send_json_response(403, {"error": "Admin access required"})
            limit = _int_param(query_params, "limit", 50)
            offset = _int_param(query_params, "offset", 0)
            if limit is None or offset is None:
                return self._send_json_response(400, {"error": "limit and offset must be integers"})
            limit = min(max(limit, 1), 100)
            offset = max(offset, 0)
            status = query_params.get("status", [None])[0]
            search = query_params.get("search", [None])[0]
            sessions = repo.list_sessions(limit=limit, offset=offset, status=status, search=search)
            return self._send_json_response(200, {
                "sessions": sessions,
                "count": len(sessions),
                "limit": limit,
                "offset": offset,
            })

        if path == "/api/settings":
            # Without a session: the defaults new sessions start with.
            session_id = query_params.get("session_id", [None])[0]
            if not session_id:
                return self._send_json_response(200, dict(DEFAULT_SETTINGS))
            if not self._authorized_session(session_id, self.headers.get("X-Session-Key")):
                return None
            return self._send_json_response(200, repo.get_settings(session_id))

        if path == "/api/quote":
            symbol = query_params.get("symbol", ["AAPL"])[0].strip().upper()
            if not TICKER_RE.match(symbol):
                return self._send_json_response(400, {"error": "Invalid ticker symbol"})
            quote = _fetch_quote_sync(symbol)
            if not quote or "error" in quote:
                return self._send_json_response(404, quote or {"error": f"Quote failed for {symbol}"})
            return self._send_json_response(200, quote)

        if path == "/api/compare":
            symbols_str = query_params.get("symbols", ["AAPL,TSLA"])[0]
            symbols = [s.strip().upper() for s in symbols_str.split(",") if s.strip()]
            if not 2 <= len(symbols) <= MAX_COMPARE_SYMBOLS:
                return self._send_json_response(400, {"error": f"Provide 2 to {MAX_COMPARE_SYMBOLS} symbols"})
            if not all(TICKER_RE.match(s) for s in symbols):
                return self._send_json_response(400, {"error": "Invalid ticker symbol"})
            quotes = [_fetch_quote_sync(s) for s in symbols]
            valid_quotes = [q for q in quotes if q and "error" not in q]
            return self._send_json_response(200, {"results": valid_quotes})

        return self._send_json_response(404, {"error": f"GET {path} not found"})

    # ------------------------------------------------------------------
    # POST / PUT
    # ------------------------------------------------------------------

    def _handle_post(self):
        path = urllib.parse.urlparse(self.path).path
        if self._rate_limited_response():
            return None

        if path == "/api/session":
            return self._create_session()
        if path == "/api/token":
            return self._refresh_token()
        if path == "/api/settings":
            return self._update_settings()
        return self._send_json_response(404, {"error": f"POST {path} not found"})

    def _handle_put(self):
        path = urllib.parse.urlparse(self.path).path
        if self._rate_limited_response():
            return None
        if path == "/api/settings":
            return self._update_settings()
        return self._send_json_response(404, {"error": f"PUT {path} not found"})

    def _create_session(self):
        body, err = self._read_json_body()
        if err:
            return self._send_json_response(400, {"error": err})

        if not _livekit_configured():
            return self._send_json_response(503, {
                "error": "Voice service is not configured on this server (LiveKit credentials missing)."
            })

        session_id = uuid.uuid4().hex[:16]
        room_name = f"room_{session_id}"
        identity = _clean_identity(body.get("identity"))

        session_record = repo.create_session(
            session_id=session_id,
            room_name=room_name,
            client_ip=self._get_client_ip(),
            settings=body.get("settings"),
        )
        token = _make_livekit_token(identity, room_name)
        return self._send_json_response(201, {
            "session_id": session_id,
            "session_key": make_session_key(session_id),
            "room_name": room_name,
            "token": token,
            "url": LIVEKIT_URL,
            "identity": identity,
            "status": "active",
            "created_at": session_record["created_at"],
            "settings": repo.get_settings(session_id),
        })

    def _refresh_token(self):
        """A fresh 30-minute token for a session this browser owns."""
        body, err = self._read_json_body()
        if err:
            return self._send_json_response(400, {"error": err})

        session = self._authorized_session(body.get("session_id"), body.get("session_key"))
        if not session:
            return None
        if not _livekit_configured():
            return self._send_json_response(503, {"error": "Voice service is not configured on this server."})

        identity = _clean_identity(body.get("identity"))
        token = _make_livekit_token(identity, session["room_name"])
        return self._send_json_response(200, {
            "session_id": session["id"],
            "room_name": session["room_name"],
            "token": token,
            "url": LIVEKIT_URL,
            "identity": identity,
        })

    def _update_settings(self):
        body, err = self._read_json_body()
        if err:
            return self._send_json_response(400, {"error": err})

        session = self._authorized_session(body.get("session_id"), body.get("session_key"))
        if not session:
            return None
        updated = repo.update_settings(session["id"], body)
        return self._send_json_response(200, updated)

    def list_directory(self, path):
        # No directory listings: only real files (index.html, js/, css/) are served.
        self.send_error(404, "Not found")
        return None

    # ------------------------------------------------------------------
    # Static file caching
    # ------------------------------------------------------------------

    def end_headers(self):
        # No caching for the app shell, so a new deploy shows up on refresh.
        if self.path.endswith(('.css', '.js', '.html')) or self.path == '/':
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        self._send_security_headers()
        super().end_headers()


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5500))
    handler = functools.partial(ProductionServerHandler, directory=str(FRONTEND_DIR))
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    with http.server.ThreadingHTTPServer(("0.0.0.0", port), handler) as httpd:
        print(f"AI Voice Assistant server running on http://localhost:{port}")
        print(f"Serving files from: {FRONTEND_DIR}")
        if not _livekit_configured():
            print("WARNING: LiveKit credentials are missing - voice sessions will return 503.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down server gracefully...")