import asyncio
import threading
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
        lambda _path, **_kwargs: (_ for _ in ()).throw(RuntimeError("sensitive OCR output")),
    )

    result = await server.split_receipt(str(private_path), cast(Any, None))
    assert result == {
        "error": "Receipt processing failed without retaining a derivative image.",
        "error_detail": {"category": "processing_failed", "retryable": True},
        "needs_new_photo": True,
    }
    assert str(private_path) not in str(result)


@pytest.mark.asyncio
async def test_cancelled_ocr_holds_its_slot_until_worker_exits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = threading.Event()
    release = threading.Event()

    def blocked_ocr(_path: Path, **_kwargs: object) -> list[str]:
        started.set()
        release.wait(timeout=2)
        return []

    slots = asyncio.Semaphore(1)
    monkeypatch.setattr(server, "_OCR_SLOTS", slots)
    monkeypatch.setattr(server, "extract_receipt_lines", blocked_ocr)
    task = asyncio.create_task(server._extract_with_limits(tmp_path / "receipt.png"))
    assert await asyncio.to_thread(started.wait, 1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert slots.locked()

    release.set()
    await asyncio.wait_for(slots.acquire(), timeout=1)
