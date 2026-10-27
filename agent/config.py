"""
Centralized configuration management and environment validation.
Ensures zero hardcoded secrets and validates required credentials at startup.
"""

import os
from pathlib import Path
from typing import List, Optional
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def mask_secret(secret: Optional[str], visible_prefix: int = 4, visible_suffix: int = 4) -> str:
    """Safely mask a secret for logging purposes."""
    if not secret:
        return "<not-set>"
    if len(secret) <= (visible_prefix + visible_suffix):
        return "****"
    return f"{secret[:visible_prefix]}****{secret[-visible_suffix:]}"


class AppConfig:
    """Application configuration and environment validator."""

    def __init__(self):
        # Rime TTS
        self.rime_api_key: str = os.getenv("RIME_API_KEY", "").strip()
        self.rime_model_id: str = os.getenv("RIME_MODEL_ID", "coda").strip()
        self.rime_speaker: str = os.getenv("RIME_SPEAKER", "celeste").strip()
        self.rime_endpoint: str = os.getenv("RIME_ENDPOINT", "api.rime.ai").strip()

        # LiveKit Cloud
        self.livekit_url: str = os.getenv("LIVEKIT_URL", "").strip()
        self.livekit_api_key: str = os.getenv("LIVEKIT_API_KEY", "").strip()
        self.livekit_api_secret: str = os.getenv("LIVEKIT_API_SECRET", "").strip()

        # Deepgram STT
        self.deepgram_api_key: str = os.getenv("DEEPGRAM_API_KEY", "").strip()

        # LLM Providers (OpenAI primary, Groq fallback)
        self.openai_api_key: str = os.getenv("OPENAI_API_KEY", "").strip()
        self.openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
        self.groq_api_key: str = os.getenv("GROQ_API_KEY", "").strip()
        self.groq_model: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip()
        # Order in which LLM providers are tried (see livekit_handler.llm_provider_order).
        self.llm_providers: str = os.getenv("LLM_PROVIDERS", "openai,groq").strip()

        # Financial Data
        self.finnhub_api_key: str = os.getenv("FINNHUB_API_KEY", "").strip()

        # Security & CORS
        raw_origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:5500,http://127.0.0.1:5500")
        self.allowed_origins: List[str] = [o.strip() for o in raw_origins.split(",") if o.strip()]

        # Session keys (see agent/security.py). Falls back to the LiveKit
        # API secret when unset, so existing deployments keep working.
        self.session_signing_key: str = os.getenv("SESSION_SIGNING_KEY", "").strip()

        # Bearer token for admin-only endpoints (GET /api/sessions). The
        # admin endpoints are disabled while this is empty.
        self.admin_token: str = os.getenv("ADMIN_TOKEN", "").strip()

        # Set TRUST_PROXY=1 only when running behind a reverse proxy that
        # sets X-Forwarded-For (Render, Railway, Nginx...). Without a proxy
        # the header is client-controlled and must be ignored, otherwise
        # anyone can dodge the rate limit by sending a fake IP.
        self.trust_proxy: bool = os.getenv("TRUST_PROXY", "").strip().lower() in ("1", "true", "yes")

        # Rate Limiting
        try:
            self.rate_limit_per_minute: int = int(os.getenv("RATE_LIMIT_PER_MINUTE", "60"))
        except ValueError:
            self.rate_limit_per_minute = 60

        # Database Path
        raw_db_path = os.getenv("DATABASE_PATH", "data/agent.db")
        self.database_path: Path = (PROJECT_ROOT / raw_db_path).resolve()

        # Artificial Delay (0.0 in production)
        try:
            self.artificial_delay_seconds: float = float(os.getenv("ARTIFICIAL_DELAY_SECONDS", "0.0"))
        except ValueError:
            self.artificial_delay_seconds = 0.0

        # Logging Level
        self.log_level: str = os.getenv("LOG_LEVEL", "INFO").upper().strip()

    def validate_agent_config(self) -> List[str]:
        """Validate required configuration for running the voice agent."""
        errors = []
        if not self.rime_api_key:
            errors.append("RIME_API_KEY is required for voice synthesis.")
        if not self.livekit_url:
            errors.append("LIVEKIT_URL is required for WebRTC transport.")
        if not self.livekit_api_key:
            errors.append("LIVEKIT_API_KEY is required for WebRTC transport.")
        if not self.livekit_api_secret:
            errors.append("LIVEKIT_API_SECRET is required for WebRTC transport.")
        if not self.deepgram_api_key:
            errors.append("DEEPGRAM_API_KEY is required for speech transcription.")
        if not (self.openai_api_key or self.groq_api_key):
            errors.append("Either OPENAI_API_KEY (primary) or GROQ_API_KEY (fallback) must be provided.")
        return errors

    def validate_server_config(self) -> List[str]:
        """Validate configuration required for running the HTTP server."""
        errors = []
        if not self.livekit_api_key or not self.livekit_api_secret or not self.livekit_url:
            errors.append(
                "LiveKit credentials (LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET) "
                "are required to mint session tokens."
            )
        return errors

    def get_masked_summary(self) -> dict:
        """Return non-sensitive configuration overview for startup logs."""
        return {
            "livekit_url": self.livekit_url,
            "livekit_api_key": mask_secret(self.livekit_api_key),
            "rime_configured": bool(self.rime_api_key),
            "deepgram_configured": bool(self.deepgram_api_key),
            "openai_configured": bool(self.openai_api_key),
            "openai_model": self.openai_model,
            "groq_configured": bool(self.groq_api_key),
            "groq_model": self.groq_model,
            "finnhub_configured": bool(self.finnhub_api_key),
            "allowed_origins": self.allowed_origins,
            "session_signing_key_set": bool(self.session_signing_key),
            "admin_endpoints_enabled": bool(self.admin_token),
            "trust_proxy": self.trust_proxy,
            "database_path": str(self.database_path),
            "artificial_delay_seconds": self.artificial_delay_seconds,
            "log_level": self.log_level,
            "llm_providers": self.llm_providers,
        }


# Global singleton instance
config = AppConfig()