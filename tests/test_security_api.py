"""
Integration and security tests for frontend/serve.py.

Runs a real server on a random port against an isolated test database, with
dummy LiveKit credentials, so the suite needs no API keys or network access
(it used to need real LiveKit keys and failed in CI without them).
"""

import tempfile
import threading
import functools
import http.server
from pathlib import Path

import pytest
import requests

import frontend.serve as serve
from agent.config import config
from agent.db.repository import get_repository
from agent.security import make_session_key, verify_session_key

ADMIN_TOKEN = "test-admin-token"


@pytest.fixture(scope="module")
def repo():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield get_repository(Path(tmpdir) / "test.db")


@pytest.fixture(scope="module")
def live_server(repo):
    saved = {
        "repo": serve.repo,
        "url": serve.LIVEKIT_URL,
        "key": serve.LIVEKIT_API_KEY,
        "secret": serve.LIVEKIT_API_SECRET,
        "signing": config.session_signing_key,
        "admin": config.admin_token,
        "trust_proxy": config.trust_proxy,
        "rate": serve.RATE_LIMIT_MAX_REQUESTS,
    }
    serve.repo = repo
    serve.LIVEKIT_URL = "wss://test.livekit.invalid"
    serve.LIVEKIT_API_KEY = "test-livekit-key"
    serve.LIVEKIT_API_SECRET = "test-livekit-secret-that-is-long-enough"
    config.session_signing_key = "test-signing-key"
    config.admin_token = ADMIN_TOKEN
    config.trust_proxy = False
    serve.RATE_LIMIT_MAX_REQUESTS = 10_000

    handler = functools.partial(serve.ProductionServerHandler, directory=str(serve.FRONTEND_DIR))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"

    server.shutdown()
    serve.repo = saved["repo"]
    serve.LIVEKIT_URL = saved["url"]
    serve.LIVEKIT_API_KEY = saved["key"]
    serve.LIVEKIT_API_SECRET = saved["secret"]
    config.session_signing_key = saved["signing"]
    config.admin_token = saved["admin"]
    config.trust_proxy = saved["trust_proxy"]
    serve.RATE_LIMIT_MAX_REQUESTS = saved["rate"]


def new_session(live_server, **body):
    resp = requests.post(f"{live_server}/api/session", json={"identity": "tester", **body})
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- Basics ------------------------------------------------------------------

def test_health_endpoint(live_server):
    resp = requests.get(f"{live_server}/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["service"] == "ai-voice-assistant"


def test_security_headers_present(live_server):
    resp = requests.get(f"{live_server}/health")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert "microphone=(self)" in resp.headers.get("Permissions-Policy", "")
    assert "Content-Security-Policy" in resp.headers


def test_cors_allowed_origin(live_server):
    resp = requests.get(f"{live_server}/health", headers={"Origin": "http://localhost:5500"})
    assert resp.headers.get("Access-Control-Allow-Origin") in ("http://localhost:5500", "*")


def test_cors_unknown_origin_gets_no_header(live_server):
    resp = requests.get(f"{live_server}/health", headers={"Origin": "http://evil.example"})
    # Not reflected, and never "null" (which sandboxed iframes would match).
    assert resp.headers.get("Access-Control-Allow-Origin") is None


def test_server_source_and_listings_not_served(live_server):
    assert requests.get(f"{live_server}/serve.py").status_code == 404
    assert requests.get(f"{live_server}/__pycache__/").status_code == 404
    assert requests.get(f"{live_server}/js/").status_code == 404


# --- Sessions and tokens -------------------------------------------------------

def test_create_session_returns_key_and_token(live_server, repo):
    data = new_session(live_server)
    assert data["room_name"] == f"room_{data['session_id']}"
    assert data["status"] == "active"
    assert data["token"]
    assert verify_session_key(data["session_id"], data["session_key"])
    assert repo.get_session(data["session_id"])["room_name"] == data["room_name"]


def test_no_open_token_endpoint(live_server):
    # The old GET /api/token?room=... minted a token for ANY room.
    resp = requests.get(f"{live_server}/api/token", params={"room": "room_victim"})
    assert resp.status_code == 404


def test_token_requires_session_key(live_server):
    data = new_session(live_server)
    sid = data["session_id"]

    assert requests.post(f"{live_server}/api/token", json={"session_id": sid}).status_code == 404
    assert requests.post(
        f"{live_server}/api/token", json={"session_id": sid, "session_key": "0" * 64}
    ).status_code == 404

    ok = requests.post(
        f"{live_server}/api/token", json={"session_id": sid, "session_key": data["session_key"]}
    )
    assert ok.status_code == 200
    assert ok.json()["room_name"] == f"room_{sid}"
    assert ok.json()["token"]


def test_unknown_session_and_wrong_key_look_the_same(live_server):
    fake = requests.post(f"{live_server}/api/token", json={
        "session_id": "does-not-exist", "session_key": make_session_key("does-not-exist"),
    })
    data = new_session(live_server)
    wrong = requests.post(f"{live_server}/api/token", json={
        "session_id": data["session_id"], "session_key": "bad",
    })
    assert fake.status_code == wrong.status_code == 404
    assert fake.json() == wrong.json()


def test_session_requires_livekit_config(live_server):
    saved = serve.LIVEKIT_API_KEY
    serve.LIVEKIT_API_KEY = ""
    try:
        resp = requests.post(f"{live_server}/api/session", json={})
        assert resp.status_code == 503
    finally:
        serve.LIVEKIT_API_KEY = saved


def test_identity_is_sanitized(live_server):
    data = new_session(live_server, identity="<script>alert(1)</script>" + "x" * 100)
    assert data["identity"].isascii()
    assert "<" not in data["identity"] and len(data["identity"]) <= 48


# --- Transcripts ------------------------------------------------------------------

def test_transcript_requires_session_key(live_server, repo):
    data = new_session(live_server)
    sid = data["session_id"]
    repo.record_turn(sid, 1, 1, "user", "what's apple's price")

    assert requests.get(f"{live_server}/api/sessions/{sid}").status_code == 404
    assert requests.get(
        f"{live_server}/api/sessions/{sid}", headers={"X-Session-Key": "nope"}
    ).status_code == 404

    ok = requests.get(f"{live_server}/api/sessions/{sid}", headers={"X-Session-Key": data["session_key"]})
    assert ok.status_code == 200
    detail = ok.json()
    assert detail["session"]["id"] == sid
    assert detail["turns"][0]["text"] == "what's apple's price"


def test_session_list_is_admin_only(live_server):
    data = new_session(live_server)

    assert requests.get(f"{live_server}/api/sessions").status_code == 403
    assert requests.get(
        f"{live_server}/api/sessions", headers={"Authorization": "Bearer wrong"}
    ).status_code == 403

    ok = requests.get(f"{live_server}/api/sessions", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
    assert ok.status_code == 200
    assert any(s["id"] == data["session_id"] for s in ok.json()["sessions"])


def test_session_list_disabled_without_admin_token(live_server):
    saved = config.admin_token
    config.admin_token = ""
    try:
        resp = requests.get(f"{live_server}/api/sessions", headers={"Authorization": "Bearer "})
        assert resp.status_code == 403
    finally:
        config.admin_token = saved


def test_bad_pagination_is_400_not_500(live_server):
    resp = requests.get(
        f"{live_server}/api/sessions",
        params={"limit": "lots"},
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    assert resp.status_code == 400


# --- Settings -------------------------------------------------------------------

def test_settings_from_session_creation(live_server, repo):
    data = new_session(live_server, settings={"voice_speaker": "marcus", "voice_speed": 1.25})
    stored = repo.get_settings(data["session_id"])
    assert stored["voice_speaker"] == "marcus"
    assert stored["voice_speed"] == 1.25


def test_invalid_settings_are_ignored(live_server, repo):
    data = new_session(live_server, settings={"voice_speaker": "../../etc", "voice_speed": 99})
    stored = repo.get_settings(data["session_id"])
    assert stored["voice_speaker"] == "celeste"
    assert stored["voice_speed"] == 2.0


def test_settings_update_requires_session_key(live_server, repo):
    data = new_session(live_server)
    sid = data["session_id"]

    denied = requests.put(f"{live_server}/api/settings", json={"session_id": sid, "voice_speed": 1.5})
    assert denied.status_code == 404
    assert requests.put(
        f"{live_server}/api/settings", json={"session_id": "global", "voice_speed": 1.5}
    ).status_code == 404

    ok = requests.put(f"{live_server}/api/settings", json={
        "session_id": sid, "session_key": data["session_key"], "voice_speed": 1.5,
    })
    assert ok.status_code == 200
    assert repo.get_settings(sid)["voice_speed"] == 1.5


def test_default_settings_are_public(live_server):
    resp = requests.get(f"{live_server}/api/settings")
    assert resp.status_code == 200
    assert resp.json()["voice_model"] == "coda"


# --- Rate limiting and input limits ----------------------------------------------

def test_forwarded_for_ignored_without_trust_proxy(live_server):
    serve._ip_request_timestamps.clear()
    requests.get(f"{live_server}/api/settings", headers={"X-Forwarded-For": "1.2.3.4"})
    assert "1.2.3.4" not in serve._ip_request_timestamps
    assert "127.0.0.1" in serve._ip_request_timestamps


def test_forwarded_for_uses_last_hop_with_trust_proxy(live_server):
    serve._ip_request_timestamps.clear()
    config.trust_proxy = True
    try:
        requests.get(f"{live_server}/api/settings", headers={"X-Forwarded-For": "6.6.6.6, 9.9.9.9"})
        assert "9.9.9.9" in serve._ip_request_timestamps
        assert "6.6.6.6" not in serve._ip_request_timestamps
    finally:
        config.trust_proxy = False


def test_rate_limit_returns_429(live_server):
    serve._ip_request_timestamps.clear()
    saved = serve.RATE_LIMIT_MAX_REQUESTS
    serve.RATE_LIMIT_MAX_REQUESTS = 3
    try:
        codes = [requests.get(f"{live_server}/api/settings").status_code for _ in range(5)]
        assert codes[:3] == [200, 200, 200]
        assert codes[3:] == [429, 429]
    finally:
        serve.RATE_LIMIT_MAX_REQUESTS = saved
        serve._ip_request_timestamps.clear()


def test_compare_symbol_limits(live_server):
    too_many = ",".join(["AAPL", "MSFT", "TSLA", "NVDA", "AMZN", "META"])
    assert requests.get(f"{live_server}/api/compare", params={"symbols": too_many}).status_code == 400
    assert requests.get(f"{live_server}/api/compare", params={"symbols": "AAPL,<x>"}).status_code == 400
    assert requests.get(f"{live_server}/api/quote", params={"symbol": "../etc"}).status_code == 400


def test_malformed_json_is_400(live_server):
    resp = requests.post(
        f"{live_server}/api/session", data="{not json", headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 400