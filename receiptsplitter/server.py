from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

from .core import ReceiptCorrectionError, ReceiptState, parse_receipt_lines
from .images import (
    ReceiptBusyError,
    ReceiptConfigurationError,
    ReceiptInputError,
    ReceiptProcessingError,
    ReceiptTimeoutError,
    extract_receipt_lines,
    validate_receipt_path,
)


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as cause:
        raise RuntimeError(f"{name} must be an integer.") from cause
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}.")
    return value


SESSION_TTL_SECONDS = _bounded_env_int("RECEIPT_SESSION_TTL_SECONDS", 7200, 60, 86400)
MAX_SESSIONS = _bounded_env_int("RECEIPT_MAX_SESSIONS", 32, 1, 256)
OCR_TIMEOUT_SECONDS = _bounded_env_int("RECEIPT_OCR_TIMEOUT_SECONDS", 30, 1, 300)
OCR_QUEUE_TIMEOUT_SECONDS = _bounded_env_int("RECEIPT_OCR_QUEUE_TIMEOUT_SECONDS", 5, 1, 60)
MAX_CONCURRENT_OCR = _bounded_env_int("RECEIPT_MAX_CONCURRENT_OCR", 1, 1, 4)
_OCR_SLOTS = asyncio.Semaphore(MAX_CONCURRENT_OCR)


@dataclass
class SessionEntry:
    state: ReceiptState = field(default_factory=ReceiptState)
    touched_at: float = field(default_factory=time.monotonic)


@dataclass
class AppContext:
    sessions: dict[str, SessionEntry] = field(default_factory=dict)

    def state_for(self, client_id: str) -> ReceiptState:
        now = time.monotonic()
        self.sessions = {
            key: value
            for key, value in self.sessions.items()
            if now - value.touched_at <= SESSION_TTL_SECONDS
        }
        if client_id not in self.sessions and len(self.sessions) >= MAX_SESSIONS:
            oldest = min(self.sessions, key=lambda key: self.sessions[key].touched_at)
            del self.sessions[oldest]
        entry = self.sessions.setdefault(client_id, SessionEntry())
        entry.touched_at = now
        return entry.state


@asynccontextmanager
async def lifespan(_server: FastMCP) -> AsyncIterator[AppContext]:
    yield AppContext()


mcp = FastMCP(
    name="Receipt Splitter",
    instructions=(
        "Parse a private local receipt, review the extracted rows, assign each row, "
        "and verify totals."
    ),
    lifespan=lifespan,
)


def _state(ctx: Context[ServerSession, AppContext]) -> ReceiptState:
    client_id = ctx.client_id or "default"
    return ctx.request_context.lifespan_context.state_for(client_id)


def _error(
    category: str,
    message: str,
    *,
    retryable: bool,
    needs_new_photo: bool = False,
) -> dict[str, Any]:
    return {
        "error": message,
        "error_detail": {
            "category": category,
            "retryable": retryable,
        },
        "needs_new_photo": needs_new_photo,
    }


def _release_ocr_slot(task: asyncio.Task[list[str]]) -> None:
    if not task.cancelled():
        task.exception()
    _OCR_SLOTS.release()


async def _extract_with_limits(path: Path) -> list[str]:
    try:
        await asyncio.wait_for(_OCR_SLOTS.acquire(), timeout=OCR_QUEUE_TIMEOUT_SECONDS)
    except TimeoutError as cause:
        raise ReceiptBusyError("OCR capacity is busy; retry shortly.") from cause

    task = asyncio.create_task(
        asyncio.to_thread(extract_receipt_lines, path, timeout_seconds=OCR_TIMEOUT_SECONDS)
    )
    task.add_done_callback(_release_ocr_slot)
    try:
        return await asyncio.wait_for(
            asyncio.shield(task), timeout=OCR_TIMEOUT_SECONDS + OCR_QUEUE_TIMEOUT_SECONDS
        )
    except TimeoutError as cause:
        raise ReceiptTimeoutError("Receipt OCR exceeded the processing time limit.") from cause


@mcp.tool()
async def reset(ctx: Context[ServerSession, AppContext]) -> str:
    app = ctx.request_context.lifespan_context
    app.sessions.pop(ctx.client_id or "default", None)
    return "Receipt state cleared."


@mcp.tool()
async def split_receipt(
    image_path: str,
    ctx: Context[ServerSession, AppContext],
    tax_included: bool = False,
) -> dict[str, Any]:
    """Validate, privately preprocess, OCR, and parse a PNG or JPEG receipt."""
    try:
        path = validate_receipt_path(image_path)
        lines = await _extract_with_limits(path)
    except ReceiptInputError as cause:
        return _error(
            "invalid_input",
            str(cause),
            retryable=True,
            needs_new_photo="unreadable" in str(cause).lower(),
        )
    except ReceiptConfigurationError as cause:
        return _error("invalid_configuration", str(cause), retryable=False)
    except ReceiptBusyError as cause:
        return _error("ocr_busy", str(cause), retryable=True, needs_new_photo=False)
    except ReceiptTimeoutError as cause:
        return _error("ocr_timeout", str(cause), retryable=True, needs_new_photo=False)
    except ReceiptProcessingError as cause:
        return _error("ocr_unavailable", str(cause), retryable=True, needs_new_photo=False)
    except Exception:
        return _error(
            "processing_failed",
            "Receipt processing failed without retaining a derivative image.",
            retryable=True,
            needs_new_photo=True,
        )

    table, expected = parse_receipt_lines(lines)
    state = _state(ctx)
    state.df = table
    state.expected = expected
    state.tax_included = tax_included
    report = state.validation_report()
    return {
        "table_markdown": state.to_markdown(),
        "validation": report,
        "needs_new_photo": report["needs_review"],
        "next": (
            "Retake the photo if the rows or totals are unclear; otherwise review the rows, "
            "then call provide_assignments with stable row IDs."
        ),
    }


@mcp.tool()
async def provide_assignments(
    assignments: dict[str, str],
    ctx: Context[ServerSession, AppContext],
) -> dict[str, Any]:
    state = _state(ctx)
    if not state.df:
        return _error(
            "no_active_receipt",
            "No receipt is active. Call split_receipt first.",
            retryable=False,
        )
    unresolved = state.assign(assignments)
    return {
        "updated_table": state.to_markdown(),
        "unresolved": unresolved,
        "next": "Use row IDs for unresolved or duplicate item names."
        if unresolved
        else "Call finalize_split.",
    }


@mcp.tool()
async def correct_row(
    item_id: int,
    ctx: Context[ServerSession, AppContext],
    item: str | None = None,
    quantity: int | None = None,
    line_total: str | None = None,
    tax_applies: bool | None = None,
) -> dict[str, Any]:
    """Correct explicit row fields; line_total is never treated as a unit price."""
    state = _state(ctx)
    if not state.df:
        return _error(
            "no_active_receipt",
            "No receipt is active. Call split_receipt first.",
            retryable=False,
        )
    try:
        row = state.correct_row(
            item_id,
            item=item,
            quantity=quantity,
            line_total=line_total,
            tax_applies=tax_applies,
        )
    except ReceiptCorrectionError as cause:
        return _error("invalid_correction", str(cause), retryable=False)
    return {
        "corrected_row": row.as_dict(),
        "updated_table": state.to_markdown(),
        "validation": state.validation_report(),
        "next": "Review the corrected totals, then assign rows or call finalize_split.",
    }


@mcp.tool()
async def finalize_split(ctx: Context[ServerSession, AppContext]) -> dict[str, Any]:
    state = _state(ctx)
    if not state.df:
        return _error(
            "no_active_receipt",
            "No receipt is active. Call split_receipt first.",
            retryable=False,
        )
    unassigned = [row.item_id for row in state.df if not row.who.strip()]
    validation = state.validation_report()
    reconciliation = state.split_reconciliation()
    return {
        "final_table": state.to_markdown(),
        "per_person": state.per_person_totals(),
        "validation": validation,
        "reconciliation": reconciliation,
        "unassigned_row_ids": unassigned,
        "ready": not unassigned and validation["ok"] and reconciliation["ok"],
    }


@mcp.tool()
async def help_receipt() -> str:
    return (
        "Put a PNG or JPEG in the configured receipt input folder. Call split_receipt, "
        "review validation, correct explicit line totals if needed, assign stable row IDs "
        "with provide_assignments, then call finalize_split. Call reset when finished."
    )


@mcp.resource("state://receipt/current_table")
async def current_table(ctx: Context[ServerSession, AppContext]) -> str:
    return _state(ctx).to_markdown()


@mcp.resource("state://receipt/validation")
async def current_validation(ctx: Context[ServerSession, AppContext]) -> str:
    return json.dumps(_state(ctx).validation_report(), indent=2)


@mcp.prompt()
def process_receipt(image_path: str, tax_included: bool = False) -> str:
    return (
        f"Process the receipt at {image_path!r} with tax_included={str(tax_included).lower()}. "
        "Review every extracted row and the validation result. Ask for a clearer photo "
        "if no rows are recognized."
    )


def main() -> None:
    mcp.run()
