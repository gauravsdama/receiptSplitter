from pathlib import Path
from typing import Any, cast

import pytest

from receiptsplitter import server


def test_session_count_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "MAX_SESSIONS", 2)
    app = server.AppContext()
    first = app.state_for("first")
    app.state_for("second")
    app.state_for("third")

    assert len(app.sessions) == 2
    assert "first" not in app.sessions
    assert first is not app.state_for("first")


def test_expired_sessions_are_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "SESSION_TTL_SECONDS", 1)
    app = server.AppContext()
    app.state_for("old")
    app.sessions["old"].touched_at -= 2

    app.state_for("new")
    assert set(app.sessions) == {"new"}


@pytest.mark.asyncio
async def test_processing_failure_redacts_the_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    private_path = tmp_path / "private-name.png"
    monkeypatch.setattr(server, "validate_receipt_path", lambda _value: private_path)
    monkeypatch.setattr(
        server,
        "extract_receipt_lines",
        lambda _path: (_ for _ in ()).throw(RuntimeError("sensitive OCR output")),
    )

    result = await server.split_receipt(str(private_path), cast(Any, None))
    assert result == {
        "error": "Receipt processing failed without retaining a derivative image.",
        "needs_new_photo": True,
    }
    assert str(private_path) not in str(result)
