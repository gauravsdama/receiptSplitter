# Receipt Splitter

Receipt Splitter is a local MCP server for turning a receipt photo into a checked, per-person
expense split. It runs over stdio and does not expose a web server.

Receipt parsing is heuristic. Review the recognized rows and totals before settling a bill. When
an image is unclear or an assignment is ambiguous, use a better photo or assign the stable row ID.

## Requirements

- macOS on Apple silicon or Intel
- Python 3.13 or 3.14
- [uv](https://docs.astral.sh/uv/)
- Tesseract 5.x (`brew install tesseract`); verified locally with Tesseract 5.5.1

PaddleOCR is available as an optional alternative. The default install uses Tesseract and does not
download Paddle models.

## Install and run

```bash
uv sync --locked
mkdir -p "$HOME/Documents/Receipt Splitter/Inputs"
uv run receipt-splitter
```

The MCP client should launch `uv run receipt-splitter` from this checkout. The compatibility command
`uv run python main.py` starts the same stdio server.

To use a different input folder, set `RECEIPT_ALLOWED_ROOT` to its absolute path before launch. Only
PNG and JPEG files inside that folder are accepted. Files are checked by extension, decoded format,
byte size, and pixel count. Preprocessed derivatives live in a private temporary directory and are
removed after OCR.

Optional Paddle setup:

```bash
uv sync --locked --extra paddle
RECEIPT_OCR_ENGINE=paddle uv run receipt-splitter
```

## MCP flow

1. Call `split_receipt` with an image inside the configured input folder.
2. Review every row and the validation report.
3. Call `provide_assignments` with row IDs mapped to names.
4. Call `finalize_split`; settle only when it reports `ready: true`.
5. Call `reset` after the split is complete.

Duplicate names are kept as separate rows. Fuzzy matches that are unclear remain unresolved. Tax is
allocated in whole cents, so the per-person totals reconcile exactly with the receipt.

## Privacy

Receipt photos stay on the machine. The server does not upload them, log their contents, or retain
preprocessed copies. Sessions expire after two hours and are limited to 32 active clients by
default. `RECEIPT_SESSION_TTL_SECONDS` and `RECEIPT_MAX_SESSIONS` can lower those bounds.

Keep real receipts outside the checkout. Root-level images and common input/output folders are
ignored as a second line of defense. The repository fixture is fictitious text.

## Verify

```bash
uv sync --locked --extra dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy receiptsplitter tests
uv run pip-audit
uv run python scripts/check_release_privacy.py
uv build
```

CI runs the same checks, scans the Git history for secrets, and builds the package. See
[`RELEASE_READINESS.md`](RELEASE_READINESS.md) for recorded release evidence.

## License

Copyright (c) 2026 Gaurav Dama. Released under the [MIT License](LICENSE). Third-party software keeps
its own license; distribution notes are in [`NOTICE.md`](NOTICE.md).
