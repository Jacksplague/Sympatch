from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import ProjectIndex
from .utils import ensure_dir


def sympatch_dir(root: Path) -> Path:
    return root.resolve() / ".sympatch"


def index_path(root: Path) -> Path:
    return sympatch_dir(root) / "index.json"


def history_dir(root: Path) -> Path:
    return sympatch_dir(root) / "history"


def history_log_path(root: Path) -> Path:
    return history_dir(root) / "history.jsonl"


def save_index(root: Path, index: ProjectIndex) -> None:
    ensure_dir(sympatch_dir(root))
    index_path(root).write_text(
        json.dumps(index.to_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def load_index(root: Path) -> ProjectIndex:
    path = index_path(root)
    if not path.exists():
        raise FileNotFoundError(
            f"No sympatch index found at {path}. Run `sympatch scan {root}` first."
        )
    return ProjectIndex.from_dict(json.loads(path.read_text(encoding="utf-8")))


def append_history(root: Path, record: dict[str, Any]) -> None:
    ensure_dir(history_dir(root))
    with history_log_path(root).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def read_history(root: Path) -> list[dict[str, Any]]:
    path = history_log_path(root)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out
