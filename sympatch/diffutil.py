from __future__ import annotations

import difflib
from pathlib import Path


def unified_diff(before: str, after: str, fromfile: str, tofile: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=fromfile,
            tofile=tofile,
        )
    )


def read_diff(path: Path) -> str:
    return path.read_text(encoding="utf-8")
