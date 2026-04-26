from __future__ import annotations

import json
import textwrap
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .diffutil import unified_diff
from .indexer import scan_project
from .models import ProjectIndex, SymbolRecord
from .storage import append_history, history_dir, load_index, read_history, save_index
from .utils import ensure_dir, read_text, sha256_text, write_text
from .validator import validate_path


class PatchError(RuntimeError):
    pass


def get_symbol(index: ProjectIndex, symbol_id: str) -> SymbolRecord:
    symbol = index.symbol_map().get(symbol_id)
    if symbol is None:
        raise PatchError(f"Unknown symbol: {symbol_id}")
    return symbol


def symbol_source(root: Path, symbol: SymbolRecord) -> str:
    path = root.resolve() / symbol.file
    lines = read_text(path).splitlines()
    return "\n".join(lines[symbol.start_line - 1 : symbol.end_line])


def replace_symbol(root: Path, symbol_id: str, replacement_file: Path) -> dict:
    root = root.resolve()
    index = load_index(root)
    symbol = get_symbol(index, symbol_id)
    target_path = root / symbol.file
    replacement_path = replacement_file if replacement_file.is_absolute() else Path.cwd() / replacement_file

    if not target_path.exists():
        raise PatchError(f"Target file does not exist: {target_path}")
    if not replacement_path.exists():
        raise PatchError(f"Replacement file does not exist: {replacement_path}")

    before_text = read_text(target_path)
    current_source = "\n".join(before_text.splitlines()[symbol.start_line - 1 : symbol.end_line])
    current_hash = sha256_text(current_source)
    if current_hash != symbol.source_hash:
        raise PatchError(
            "Refusing patch: source hash mismatch. "
            "The file changed since indexing. Run `sympatch scan` and retry."
        )

    replacement_raw = read_text(replacement_path)
    replacement = prepare_replacement(replacement_raw, symbol.indent)
    after_text = splice_lines(before_text, symbol.start_line, symbol.end_line, replacement)

    patch_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
    hdir = history_dir(root) / patch_id
    ensure_dir(hdir)
    before_snapshot = hdir / "before.py"
    after_snapshot = hdir / "after.py"
    diff_path = hdir / "diff.patch"
    record_path = hdir / "record.json"

    write_text(before_snapshot, before_text)
    write_text(after_snapshot, after_text)
    diff = unified_diff(before_text, after_text, fromfile=f"a/{symbol.file}", tofile=f"b/{symbol.file}")
    write_text(diff_path, diff)

    # Write patch, validate, restore on failure.
    write_text(target_path, after_text)
    ok, message = validate_path(root, target_path)
    if not ok:
        write_text(target_path, before_text)
        raise PatchError(f"Patch failed validation and was reverted:\n{message}")

    new_index = scan_project(root)
    save_index(root, new_index)

    record = {
        "id": patch_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "operation": "replace_symbol",
        "root": str(root),
        "file": symbol.file,
        "symbol_id": symbol.id,
        "old_hash": symbol.source_hash,
        "new_hash": sha256_text(replacement.rstrip("\n")),
        "before_snapshot": str(before_snapshot.relative_to(root)),
        "after_snapshot": str(after_snapshot.relative_to(root)),
        "diff": str(diff_path.relative_to(root)),
    }
    write_text(record_path, json.dumps(record, indent=2, sort_keys=True))
    append_history(root, record)
    return record


def prepare_replacement(raw: str, target_indent: int) -> str:
    stripped = raw.strip("\n")
    if not stripped.strip():
        raise PatchError("Replacement file is empty.")
    dedented = textwrap.dedent(stripped)
    indent = " " * target_indent
    return "\n".join((indent + line if line.strip() else "") for line in dedented.splitlines()) + "\n"


def splice_lines(text: str, start_line: int, end_line: int, replacement: str) -> str:
    keep_newline = text.endswith("\n")
    lines = text.splitlines()
    repl_lines = replacement.rstrip("\n").splitlines()
    new_lines = lines[: start_line - 1] + repl_lines + lines[end_line:]
    result = "\n".join(new_lines)
    if keep_newline:
        result += "\n"
    return result


def latest_history_record(root: Path) -> dict | None:
    records = read_history(root)
    return records[-1] if records else None


def find_history_record(root: Path, patch_id: str) -> dict | None:
    for record in reversed(read_history(root)):
        if record.get("id") == patch_id:
            return record
    return None


def rollback(root: Path, patch_id: str = "last") -> dict:
    root = root.resolve()
    record = latest_history_record(root) if patch_id == "last" else find_history_record(root, patch_id)
    if record is None:
        raise PatchError("No matching patch history record found.")

    target_path = root / record["file"]
    before_snapshot = root / record["before_snapshot"]
    if not before_snapshot.exists():
        raise PatchError(f"Before snapshot is missing: {before_snapshot}")

    current_text = read_text(target_path) if target_path.exists() else ""
    restored_text = read_text(before_snapshot)
    write_text(target_path, restored_text)
    ok, message = validate_path(root, target_path)
    if not ok:
        write_text(target_path, current_text)
        raise PatchError(f"Rollback failed validation and was reverted:\n{message}")

    save_index(root, scan_project(root))
    rollback_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
    rb_record = {
        "id": rollback_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "operation": "rollback",
        "rolled_back_patch": record["id"],
        "file": record["file"],
    }
    append_history(root, rb_record)
    return rb_record
