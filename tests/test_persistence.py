"""
Unit tests for SQLite persistence and Repository data access layer.
"""

import tempfile
from pathlib import Path
from agent.db.database import init_db, get_db_connection
from agent.db.repository import Repository


def test_schema_initialization():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = Path(tmpdir) / "test.db"
        init_db(db_file)

        with get_db_connection(db_file) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = {row["name"] for row in cursor.fetchall()}
            assert {"sessions", "turns", "tool_executions", "settings"}.issubset(tables)


def test_session_repository_crud():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = Path(tmpdir) / "test.db"
        repo = Repository(db_file)

        # Create
        sess = repo.create_session("sess-1", "room_sess-1", "127.0.0.1")
        assert sess["id"] == "sess-1"
        assert sess["status"] == "active"

        # Get
        fetched = repo.get_session("sess-1")
        assert fetched is not None
        assert fetched["room_name"] == "room_sess-1"

        # Update status
        repo.update_session_status("sess-1", "completed")
        updated = repo.get_session("sess-1")
        assert updated["status"] == "completed"


def test_turns_recording_and_ordering():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = Path(tmpdir) / "test.db"
        repo = Repository(db_file)
        repo.create_session("sess-turns", "room_sess-turns")

        repo.record_turn("sess-turns", turn_number=1, turn_version=1, sender="user", text="What's Apple price?")
        repo.record_turn("sess-turns", turn_number=2, turn_version=1, sender="agent", text="Checking Apple...")
        repo.record_turn("sess-turns", turn_number=3, turn_version=2, sender="user", text="Actually check Nvidia", is_interrupted=True)

        turns = repo.get_session_turns("sess-turns")
        assert len(turns) == 3
        assert turns[0]["turn_number"] == 1
        assert turns[0]["sender"] == "user"
        assert turns[2]["is_interrupted"] == 1
        assert turns[2]["turn_version"] == 2


def test_tool_executions_tracking():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = Path(tmpdir) / "test.db"
        repo = Repository(db_file)
        repo.create_session("sess-tools", "room_sess-tools")

        tool_id = repo.record_tool_execution(
            session_id="sess-tools",
            turn_version=1,
            tool_name="stock_quote",
            parameters={"symbol": "AAPL", "field": "price"},
            status="running"
        )
        assert tool_id > 0

        # Update to success
        repo.update_tool_execution(
            tool_id=tool_id,
            status="success",
            latency_ms=350.5,
            result={"symbol": "AAPL", "price": 182.50}
        )

        tools = repo.get_session_tools("sess-tools")
        assert len(tools) == 1
        assert tools[0]["status"] == "success"
        assert tools[0]["latency_ms"] == 350.5
        assert tools[0]["parameters"]["symbol"] == "AAPL"
        assert tools[0]["result"]["price"] == 182.50


def test_settings_persistence():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = Path(tmpdir) / "test.db"
        repo = Repository(db_file)
        repo.create_session("sess-settings", "room_sess-settings")

        # Initial default settings
        s = repo.get_settings("sess-settings")
        assert s["voice_model"] == "coda"
        assert s["voice_speaker"] == "celeste"

        # Update settings
        repo.update_settings("sess-settings", {
            "voice_model": "v1",
            "voice_speaker": "marcus",
            "voice_speed": 1.25,
            "auto_speak": False
        })

        updated = repo.get_settings("sess-settings")
        assert updated["voice_model"] == "v1"
        assert updated["voice_speaker"] == "marcus"
        assert updated["voice_speed"] == 1.25
        assert updated["auto_speak"] == 0



def test_settings_validation_and_global_row():
    from agent.db.repository import sanitize_settings
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Repository(Path(tmpdir) / "test.db")

        # There is no "global" session: updating it returns None instead of
        # crashing with a foreign-key error (the old behaviour).
        assert repo.update_settings("global", {"voice_speed": 1.2}) is None

        repo.create_session("s1", "room_s1", settings={"voice_speaker": "Marcus", "voice_speed": "0.1"})
        s = repo.get_settings("s1")
        assert s["voice_speaker"] == "marcus"
        assert s["voice_speed"] == 0.5  # clamped to the allowed range

        # Partial updates keep the other values.
        repo.update_settings("s1", {"voice_model": "v1"})
        s = repo.get_settings("s1")
        assert (s["voice_model"], s["voice_speaker"]) == ("v1", "marcus")

    assert sanitize_settings({"voice_model": "x" * 50, "voice_speed": True})["voice_model"] == "coda"