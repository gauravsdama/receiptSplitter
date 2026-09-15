# Release readiness

Last reviewed: 2026-09-15

## Product boundary

- Owner: Gaurav Dama
- License: MIT
- Distribution: open-source Python package and local stdio MCP server
- Network transport: none
- OCR: Tesseract by default; PaddleOCR is optional
- Canonical remote: `https://github.com/gauravsdama/receiptSplitter` (create after the publishable
  file review)

## Implemented safeguards

- Inputs must resolve inside one configured folder. PNG and JPEG files are checked by extension,
  signature, decoded format, byte size, and pixel count.
- OCR preprocessing uses a private temporary directory with automatic cleanup. Failed writes are
  detected.
- Errors omit receipt contents and local paths.
- OCR work runs outside the async event loop. The optional Paddle model is cached once per process.
- Sessions expire and have a fixed upper bound.
- Duplicate items have stable row IDs. Ambiguous fuzzy assignments remain unresolved.
- Tax allocation uses whole cents and reconciles the final total exactly.
- Git ignores receipt images and runtime data. The release privacy check rejects images, databases,
  environment files, and personal absolute paths from the publishable set.

## Release checks

The following must pass on the release commit:

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

Also retain:

- an MCP initialize and tool-discovery transcript with no receipt data;
- an EXIF result showing that no release-visible image is present;
- `git status`, `git ls-files`, and archive listings for the release commit;
- the Tesseract and uv versions used for verification;
- the GitHub Actions result for the pinned CI workflow.

## Deferred validation

OCR accuracy across real-world lighting, crumpled paper, and unusual layouts remains an operational
quality check. Real receipt photos must stay local and outside release archives.
