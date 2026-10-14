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

PaddleOCR is available as an optional alternative on Python 3.13. The default install uses
Tesseract and does not download Paddle models.

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
uv python pin 3.13
uv sync --locked --extra paddle
RECEIPT_OCR_ENGINE=paddle uv run receipt-splitter
```

Python 3.14 installs omit the Paddle dependencies because the locked Paddle runtime has no 3.14
wheel. Selecting Paddle on 3.14 returns a configuration error instead of silently falling back.

## MCP flow

1. Call `split_receipt` with an image inside the configured input folder.
2. Review every row and the validation report.
3. Use `correct_row` for a wrong item name, quantity, line total, or tax applicability.
4. Call `provide_assignments` with row IDs mapped to names.
5. Call `finalize_split`; settle only when it reports `ready: true`.
6. Call `reset` after the split is complete.

Duplicate names are kept as separate rows. Fuzzy matches that are unclear remain unresolved. Tax is
allocated in whole cents. Finalization separately proves that the sum of every person's total equals
the recognized receipt total.

The amount at the end of an item row is always treated as that row's line total. A printed quantity
is retained as metadata; Receipt Splitter does not guess whether a store printed a unit price or
multiply the amount by quantity. Use `correct_row` when OCR captured either field incorrectly.

OCR is limited to one concurrent job by default. Tesseract receives a 30-second timeout, callers
wait up to five seconds for OCR capacity, and timed-out background work keeps its slot until it
actually exits. Set `RECEIPT_OCR_TIMEOUT_SECONDS` from 1 to 300,
`RECEIPT_OCR_QUEUE_TIMEOUT_SECONDS` from 1 to 60, or `RECEIPT_MAX_CONCURRENT_OCR` from 1 to 4 before
launch. Invalid values fail at startup.

## Privacy

Receipt photos stay on the machine. The server does not upload them, log their contents, or retain
preprocessed copies. Sessions expire after two hours and are limited to 32 active clients by
default. `RECEIPT_SESSION_TTL_SECONDS` accepts 60 to 86,400 seconds and
`RECEIPT_MAX_SESSIONS` accepts 1 to 256.

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
