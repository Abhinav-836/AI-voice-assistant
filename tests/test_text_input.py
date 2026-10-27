"""
Typed chat messages must go through the same turn versioning and fencing as
speech. LiveKit only calls on_user_turn_completed for spoken turns, so typed
input used to bypass it: a typed correction never cancelled the running tool
call, and the stale result could still be spoken.
"""

import asyncio
from unittest.mock import patch

import pytest

from agent.orchestrator import Orchestrator
from agent.voice.livekit_handler import ContinuityAgent


class _FakeRepo:
    def __init__(self):
        self.turns = []

    def record_turn(self, **kwargs):
        self.turns.append(kwargs)
        return len(self.turns)


@pytest.mark.asyncio
async def test_typed_correction_fences_the_running_lookup():
    fake_repo = _FakeRepo()
    with patch("agent.voice.livekit_handler.repo", fake_repo):
        orch = Orchestrator()
        agent = ContinuityAgent(orch, session_id="sess-text")

        assert await agent.process_user_text("What's Apple's price?") is True
        v1 = orch.state.turn_version

        async def slow_apple_lookup():
            await asyncio.sleep(5)
            return {"symbol": "AAPL"}

        lookup = asyncio.create_task(orch.tool_mgr.run(v1, slow_apple_lookup()))
        await asyncio.sleep(0.05)

        # Typed, not spoken.
        assert await agent.process_user_text("No, check Tesla instead") is True

        assert await asyncio.wait_for(lookup, timeout=1) is None  # stale Apple result discarded
        assert orch.state.turn_version == v1 + 1
        assert orch.state.current_intent.get("symbols") == ["TSLA"]
        assert [t["text"] for t in fake_repo.turns] == ["What's Apple's price?", "No, check Tesla instead"]
        assert fake_repo.turns[1]["is_interrupted"] is True