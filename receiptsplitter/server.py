from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

from .core import ReceiptState, parse_receipt_lines
from .images import ReceiptInputError, extract_receipt_lines, validate_receipt_path

SESSION_TTL_SECONDS = int(os.environ.get("RECEIPT_SESSION_TTL_SECONDS", "7200"))
MAX_SESSIONS = int(os.environ.get("RECEIPT_MAX_SESSIONS", "32"))


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
        lines = await asyncio.to_thread(extract_receipt_lines, path)
    except ReceiptInputError as cause:
        return {"error": str(cause), "needs_new_photo": "unreadable" in str(cause).lower()}
    except Exception:
        return {
            "error": "Receipt processing failed without retaining a derivative image.",
            "needs_new_photo": True,
        }

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
    if state.df.empty:
        return {"error": "No receipt is active. Call split_receipt first."}
    unresolved = state.assign(assignments)
    return {
        "updated_table": state.to_markdown(),
        "unresolved": unresolved,
        "next": "Use row IDs for unresolved or duplicate item names."
        if unresolved
        else "Call finalize_split.",
    }


@mcp.tool()
async def finalize_split(ctx: Context[ServerSession, AppContext]) -> dict[str, Any]:
    state = _state(ctx)
    if state.df.empty:
        return {"error": "No receipt is active. Call split_receipt first."}
    unassigned = list(
        state.df.loc[state.df["who"].astype(str).str.strip() == "", "item_id"].astype(int)
    )
    return {
        "final_table": state.to_markdown(),
        "per_person": state.per_person_totals(),
        "validation": state.validation_report(),
        "unassigned_row_ids": unassigned,
        "ready": not unassigned and state.validation_report()["ok"],
    }


@mcp.tool()
async def help_receipt() -> str:
    return (
        "Put a PNG or JPEG in the configured receipt input folder. Call split_receipt, "
        "review validation, assign stable row IDs with provide_assignments, then call "
        "finalize_split. Call reset when finished."
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
