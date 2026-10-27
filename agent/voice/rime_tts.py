"""
Rime TTS integration. Rime is the agent's spoken output, streamed through the official LiveKit Rime plugin. (not called
as an incidental/side channel).

Install:
    pip install livekit-plugins-rime

Auth: set RIME_API_KEY in your .env.

Model/voice IDs:
    Model: "coda"
    Speaker: "celeste" (default)
"""

import os
from typing import Optional
from livekit.plugins import rime
from agent.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_MODEL_ID = os.getenv("RIME_MODEL_ID", "coda")
DEFAULT_SPEAKER = os.getenv("RIME_SPEAKER", "celeste")


def build_rime_tts(
    model: Optional[str] = None,
    speaker: Optional[str] = None,
    speed: Optional[float] = None,
) -> "rime.TTS":
    """
    Construct the Rime TTS plugin instance with WebSocket streaming for lowest latency.

    WebSocket keepalive kwargs are passed only if the installed plugin version
    accepts them. Older versions of livekit-plugins-rime do not expose these
    and would raise TypeError if passed unconditionally.
    """
    if not os.getenv("RIME_API_KEY"):
        logger.warning(
            "RIME_API_KEY not set — Rime TTS will fail to authenticate. "
            "Set it in .env before running."
        )

    selected_model = model or os.getenv("RIME_MODEL_ID", DEFAULT_MODEL_ID)
    selected_speaker = speaker or os.getenv("RIME_SPEAKER", DEFAULT_SPEAKER)
    selected_speed = speed if speed is not None else 1.0

    kwargs = {
        "model": selected_model,
        "speaker": selected_speaker,
        "speed_alpha": selected_speed,
        "use_websocket": True,
    }

    # Best-effort keepalive — many ws clients accept these, and if the plugin
    # forwards **kwargs to the underlying websocket, they'll help prevent the
    # "Rime ws closed unexpectedly" errors seen when the LLM stalls between
    # sentences. If the plugin doesn't accept them, we drop them silently.
    try:
        import inspect
        sig = inspect.signature(rime.TTS.__init__)
        if "ws_ping_interval" in sig.parameters:
            kwargs["ws_ping_interval"] = 20.0
        if "ws_ping_timeout" in sig.parameters:
            kwargs["ws_ping_timeout"] = 10.0
    except Exception:
        pass

    return rime.TTS(**kwargs)