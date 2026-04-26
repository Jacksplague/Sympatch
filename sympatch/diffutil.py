from __future__ import annotations

import difflib


def unified_diff(before: str, after: str, fromfile: str = "before", tofile: str = "after") -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=fromfile,
            tofile=tofile,
        )
    )
