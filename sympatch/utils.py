from __future__ import annotations

import hashlib
import os
from pathlib import Path

DEFAULT_IGNORE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    ".sympatch",
}


def normalize_relpath(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogateescape")).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_text(path.read_text(encoding="utf-8", errors="surrogateescape"))


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def iter_python_files(root: Path, specific: Path | None = None) -> list[Path]:
    root = root.resolve()
    if specific is not None:
        specific = specific.resolve()
        if specific.is_file():
            return [specific] if specific.suffix == ".py" else []
        if specific.is_dir():
            root = specific

    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_IGNORE_DIRS]
        base = Path(dirpath)
        for filename in filenames:
            if filename.endswith(".py"):
                files.append(base / filename)
    return sorted(files)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="surrogateescape")


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", errors="surrogateescape")
