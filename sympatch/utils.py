from __future__ import annotations

import fnmatch
import hashlib
import os
from pathlib import Path

DEFAULT_EXCLUDES = {
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
    ".sympatch",
}


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def relpath(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def module_name_for(rel_file: str) -> str:
    p = Path(rel_file)
    parts = list(p.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else "__init__"


def line_slice(source: str, start_line: int, end_line: int) -> str:
    lines = source.splitlines(keepends=True)
    return "".join(lines[start_line - 1 : end_line])


def prefix_lines(source: str, start_line: int) -> str:
    return "\n".join(f"{n:>5}: {line}" for n, line in enumerate(source.splitlines(), start=start_line))


def leading_whitespace(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def is_inside(parent: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def discover_python_files(root: Path, explicit_paths: list[Path] | None = None) -> list[Path]:
    root = root.resolve()
    if explicit_paths:
        out: list[Path] = []
        for raw in explicit_paths:
            path = raw if raw.is_absolute() else root / raw
            path = path.resolve()
            if path.is_dir():
                out.extend(discover_python_files(path))
            elif path.exists() and path.suffix == ".py" and is_inside(root, path):
                out.append(path)
        return sorted(set(out))

    results: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_EXCLUDES]
        for filename in filenames:
            if filename.endswith(".py"):
                results.append((Path(dirpath) / filename).resolve())
    return sorted(results)


def load_gitignore_patterns(root: Path) -> list[str]:
    path = root / ".gitignore"
    if not path.exists():
        return []
    patterns: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        patterns.append(line.lstrip("/"))
    return patterns


def matches_any_gitignore(rel: str, patterns: list[str]) -> bool:
    for pat in patterns:
        pat = pat.rstrip("/")
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(Path(rel).name, pat):
            return True
    return False


def dedent_to_zero(text: str) -> str:
    import textwrap

    stripped = text.strip("\n")
    return textwrap.dedent(stripped).rstrip() + "\n"


def indent_block(text: str, indent: str) -> str:
    lines = text.rstrip("\n").splitlines()
    return "\n".join((indent + line if line else line) for line in lines) + "\n"
