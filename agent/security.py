"""
Session access keys and admin checks for the HTTP API.

Every session gets a secret "session key" when it is created. The key is an
HMAC of the session id, signed with a server-side secret, so nothing extra
has to be stored: the server can always recompute and compare it.

Only the browser that created a session receives its key, and every
endpoint that touches a session (new LiveKit token, transcript, settings)
requires it. Knowing a session id or room name alone is not enough to join
someone else's room or read their conversation.
"""

import hashlib
import hmac
from typing import Optional

from agent.config import config


def _signing_key() -> bytes:
    secret = config.session_signing_key or config.livekit_api_secret
    if not secret:
        raise RuntimeError(
            "Set SESSION_SIGNING_KEY (or LIVEKIT_API_SECRET) so session keys can be signed."
        )
    return secret.encode("utf-8")


def make_session_key(session_id: str) -> str:
    """The secret key handed to the browser that created this session."""
    return hmac.new(_signing_key(), session_id.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_session_key(session_id: Optional[str], key: Optional[str]) -> bool:
    """Constant-time check that `key` belongs to `session_id`."""
    if not session_id or not key or not isinstance(key, str):
        return False
    try:
        expected = make_session_key(session_id)
    except RuntimeError:
        return False
    return hmac.compare_digest(expected, key)


def is_admin(authorization_header: Optional[str]) -> bool:
    """True when the request carries `Authorization: Bearer <ADMIN_TOKEN>`.

    Admin endpoints are disabled entirely while ADMIN_TOKEN is unset.
    """
    token = config.admin_token
    if not token or not authorization_header:
        return False
    scheme, _, value = authorization_header.partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(value.strip(), token)