#!/usr/bin/env python3
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff", ".db", ".sqlite", ".sqlite3"}
PRIVATE_NAMES = {".env"}
ABSOLUTE_USER_PATH = re.compile(r"/(?:Users|home)/[^/\s]+/")


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [ROOT / item.decode() for item in result.stdout.split(b"\0") if item]


def main() -> int:
    problems: list[str] = []
    for path in tracked_files():
        relative = path.relative_to(ROOT)
        if path.suffix.lower() in PRIVATE_SUFFIXES or path.name in PRIVATE_NAMES:
            problems.append(f"private artifact is release-visible: {relative}")
            continue
        if path.suffix.lower() not in {".py", ".md", ".toml", ".txt", ".json", ".yaml", ".yml"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if ABSOLUTE_USER_PATH.search(text):
            problems.append(f"personal absolute path found: {relative}")

    if problems:
        print("Release privacy check failed:", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 1
    print("Release privacy check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
