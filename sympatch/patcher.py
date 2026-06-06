from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .diffutil import unified_diff
from .indexer import scan_project
from .models import ProjectIndex, SymbolRecord
from .storage import append_history, history_dir, load_index, read_history, save_index
from .utils import dedent_to_zero, indent_block, leading_whitespace, line_slice, read_text, sha256_text, write_text
from .validator import run_validation, validate_source_text


class PatchError(Exception):
    """Raised when a sympatch operation cannot be applied safely."""


@dataclass(slots=True)
class ReplacementOperation:
    symbol_query: str
    replacement_file: Path | None = None
    replacement_source: str | None = None
    force: bool = False
    allow_name_change: bool = False
    label: str | None = None


@dataclass(slots=True)
class PreparedReplacement:
    symbol: SymbolRecord
    symbol_query: str
    replacement_source: str
    replacement_file: str | None
    force: bool
    allow_name_change: bool
    label: str | None


def symbol_source(root: Path, symbol: SymbolRecord) -> str:
    return line_slice(read_text(root / symbol.file), symbol.start_line, symbol.end_line)


def replace_symbol(
    root: Path,
    symbol_query: str,
    replacement_file: Path,
    *,
    force: bool = False,
    allow_name_change: bool = False,
    validate: bool = True,
    quiet: bool = False,
    run_hooks: bool = False,
) -> dict[str, Any]:
    """Replace one indexed symbol and record it as a rollbackable patch."""
    record = apply_replacements_transaction(
        root,
        [
            ReplacementOperation(
                symbol_query=symbol_query,
                replacement_file=replacement_file,
                force=force,
                allow_name_change=allow_name_change,
            )
        ],
        operation="replace_symbol",
        validate=validate,
        quiet=quiet,
        run_hooks=run_hooks,
    )
    change = record["changes"][0]
    # Preserve the old single-symbol record shape for scripts that already consume it.
    record.update(
        {
            "file": change["file"],
            "symbol_id": change["symbol_id"],
            "symbol_query": symbol_query,
            "old_hash": change["old_hash"],
            "new_hash": change["new_hash"],
            "replacement_file": change.get("replacement_file"),
        }
    )
    return record


def preview_replacements_transaction(
    root: Path,
    operations: list[ReplacementOperation],
    *,
    operation: str = "transaction_preview",
    validate: bool = True,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the combined diff/validation result without writing files or history."""
    return _run_replacements_transaction(
        root,
        operations,
        operation=operation,
        validate=validate,
        quiet=False,
        dry_run=True,
        metadata=metadata or {},
        run_hooks=False,
    )


def apply_replacements_transaction(
    root: Path,
    operations: list[ReplacementOperation],
    *,
    operation: str = "transaction_commit",
    validate: bool = True,
    quiet: bool = False,
    metadata: dict[str, Any] | None = None,
    run_hooks: bool = False,
) -> dict[str, Any]:
    """Apply multiple symbol replacements atomically.

    All replacements are planned against the same index/current source snapshot, all
    changed files are syntax-validated, then every file is written. If writing or
    re-indexing fails, all touched files are restored from memory before the error is
    re-raised.
    """
    return _run_replacements_transaction(
        root,
        operations,
        operation=operation,
        validate=validate,
        quiet=quiet,
        dry_run=False,
        metadata=metadata or {},
        run_hooks=run_hooks,
    )


def rollback_record(root: Path, target: str = "last") -> dict[str, Any]:
    root = root.resolve()
    records = read_history(root)
    if not records:
        raise PatchError("No sympatch history found.")

    rollbackable = {"replace_symbol", "transaction_commit", "reconcile", "intent_apply"}
    if target == "last":
        record = next((r for r in reversed(records) if r.get("operation") in rollbackable), None)
    else:
        record = next((r for r in records if r.get("id") == target), None)
    if record is None:
        raise PatchError(f"No matching rollbackable patch record found: {target}")

    before_map = _record_before_map(record)
    if not before_map:
        raise PatchError("History record is missing rollback data.")

    rollback_id = _rollback_id(record)
    current_snapshots: dict[str, str] = {}
    restored_files: list[str] = []
    combined_diff_parts: list[str] = []

    for file_rel, before_rel in before_map.items():
        before_path = root / before_rel
        target_path = root / file_rel
        if not before_path.exists():
            raise PatchError(f"Rollback source is missing: {before_path}")
        current = read_text(target_path) if target_path.exists() else ""
        before = read_text(before_path)
        current_snapshots[file_rel] = current
        write_text(target_path, before)
        restored_files.append(file_rel)
        combined_diff_parts.append(
            unified_diff(current, before, f"before-rollback/{file_rel}", f"after-rollback/{file_rel}")
        )

    try:
        save_index(root, scan_project(root))
    except Exception:
        for file_rel, current in current_snapshots.items():
            write_text(root / file_rel, current)
        raise

    safe_name = _safe_text("_".join(restored_files) or record.get("id", "rollback"))
    rollback_before_rel = f".sympatch/history/{rollback_id}_{safe_name}.before_rollback.json"
    rollback_diff_rel = f".sympatch/history/{rollback_id}_{safe_name}.rollback.diff"
    write_text(root / rollback_before_rel, _json_dumps(current_snapshots))
    write_text(root / rollback_diff_rel, "\n".join(part for part in combined_diff_parts if part))

    rollback = {
        "id": rollback_id,
        "operation": "rollback",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "rolled_back_patch": record.get("id"),
        "files": restored_files,
        "file": restored_files[0] if len(restored_files) == 1 else None,
        "before": rollback_before_rel,
        "diff": rollback_diff_rel,
    }
    append_history(root, rollback)
    return rollback


def _run_replacements_transaction(
    root: Path,
    operations: list[ReplacementOperation],
    *,
    operation: str,
    validate: bool,
    quiet: bool,
    dry_run: bool,
    metadata: dict[str, Any],
    run_hooks: bool,
) -> dict[str, Any]:
    root = root.resolve()
    if not operations:
        raise PatchError("No replacement operations were provided.")
    index = load_index(root)
    prepared = [_prepare_replacement(root, index, op) for op in operations]
    _assert_no_duplicate_symbols(prepared)

    originals: dict[str, str] = {}
    replacements_by_file: dict[str, list[PreparedReplacement]] = {}
    for prep in prepared:
        originals.setdefault(prep.symbol.file, read_text(root / prep.symbol.file))
        replacements_by_file.setdefault(prep.symbol.file, []).append(prep)

    new_texts: dict[str, str] = {}
    changes: list[dict[str, Any]] = []

    for file_rel, file_preps in replacements_by_file.items():
        original = originals[file_rel]
        _assert_no_overlapping_ranges(file_preps)
        new_text = original
        # Apply bottom-up so original line spans remain valid.
        for prep in sorted(file_preps, key=lambda p: p.symbol.start_line, reverse=True):
            symbol = prep.symbol
            current_symbol_text = line_slice(original, symbol.start_line, symbol.end_line)
            current_hash = sha256_text(current_symbol_text)
            if current_hash != symbol.source_hash and not prep.force:
                raise PatchError(
                    f"Refusing stale patch for {symbol.id}: indexed hash does not match current source. "
                    "Run `sympatch index` or pass --force if intentional."
                )
            lines = new_text.splitlines(keepends=True)
            # Indentation must be read from the original snapshot, not from already-mutated text.
            original_lines = original.splitlines(keepends=True)
            target_indent = leading_whitespace(original_lines[symbol.start_line - 1])
            indented_replacement = indent_block(prep.replacement_source, target_indent)
            new_text = "".join(lines[: symbol.start_line - 1]) + indented_replacement + "".join(lines[symbol.end_line :])
            changes.append(
                {
                    "symbol_id": symbol.id,
                    "symbol_query": prep.symbol_query,
                    "file": symbol.file,
                    "kind": symbol.kind,
                    "start_line": symbol.start_line,
                    "end_line": symbol.end_line,
                    "old_hash": symbol.source_hash,
                    "new_hash": sha256_text(indented_replacement),
                    "replacement_file": prep.replacement_file,
                    "label": prep.label,
                }
            )
        if validate:
            ok, msg = validate_source_text(new_text, str(root / file_rel))
            if not ok:
                raise PatchError(f"Replacement transaction produced invalid Python in {file_rel}: {msg}")
        new_texts[file_rel] = new_text

    patch_id = _generic_patch_id(operation, changes)
    diff_parts = [
        unified_diff(originals[file_rel], new_texts[file_rel], f"a/{file_rel}", f"b/{file_rel}")
        for file_rel in sorted(new_texts)
    ]
    diff_text = "\n".join(part for part in diff_parts if part)

    record: dict[str, Any] = {
        "id": patch_id,
        "operation": operation,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "files": sorted(new_texts),
        "changes": changes,
        "validated": validate,
        "dry_run": dry_run,
        "metadata": metadata,
        "validation_hooks_requested": run_hooks,
    }

    if dry_run:
        record["diff_text"] = diff_text
        return record

    before_paths: dict[str, str] = {}
    after_paths: dict[str, str] = {}
    hist_dir = history_dir(root)
    hist_dir.mkdir(parents=True, exist_ok=True)
    for file_rel in sorted(new_texts):
        safe_file = _safe_text(file_rel)
        before_rel = f".sympatch/history/{patch_id}_{safe_file}.before.py"
        after_rel = f".sympatch/history/{patch_id}_{safe_file}.after.py"
        write_text(root / before_rel, originals[file_rel])
        write_text(root / after_rel, new_texts[file_rel])
        before_paths[file_rel] = before_rel
        after_paths[file_rel] = after_rel
    diff_rel = f".sympatch/history/{patch_id}.diff"
    write_text(root / diff_rel, diff_text)

    written: list[str] = []
    try:
        for file_rel, new_text in new_texts.items():
            write_text(root / file_rel, new_text)
            written.append(file_rel)
        save_index(root, scan_project(root))
        if run_hooks:
            validation_report = run_validation(root, run_hooks=True)
            record["validation_report"] = validation_report
            if not validation_report.get("ok"):
                raise PatchError("Validation hooks failed after transaction apply; restored original files. " + str(validation_report.get("summary", "")))
    except Exception:
        for file_rel in written:
            write_text(root / file_rel, originals[file_rel])
        try:
            save_index(root, scan_project(root))
        except Exception:
            pass
        raise

    record.update(
        {
            "before": before_paths[record["files"][0]] if len(record["files"]) == 1 else None,
            "after": after_paths[record["files"][0]] if len(record["files"]) == 1 else None,
            "before_files": before_paths,
            "after_files": after_paths,
            "diff": diff_rel,
        }
    )
    append_history(root, record)
    if not quiet:
        record["diff_text"] = diff_text
    return record


def _prepare_replacement(root: Path, index: ProjectIndex, op: ReplacementOperation) -> PreparedReplacement:
    symbol = index.find_symbol(op.symbol_query)
    if symbol is None:
        matches = index.search_symbols(op.symbol_query)
        available = ", ".join(s.id for s in matches[:8])
        extra = f" Similar matches: {available}" if available else ""
        raise PatchError(f"Unknown symbol: {op.symbol_query}.{extra}")

    if op.replacement_source is None:
        if op.replacement_file is None:
            raise PatchError(f"No replacement source/file provided for {op.symbol_query}.")
        replacement_path = op.replacement_file if op.replacement_file.is_absolute() else Path.cwd() / op.replacement_file
        replacement_source = dedent_to_zero(read_text(replacement_path))
        replacement_file = str(replacement_path)
    else:
        replacement_source = dedent_to_zero(op.replacement_source)
        replacement_file = str(op.replacement_file) if op.replacement_file else None

    _validate_replacement_definition(replacement_source, target=symbol, allow_name_change=op.allow_name_change)
    return PreparedReplacement(
        symbol=symbol,
        symbol_query=op.symbol_query,
        replacement_source=replacement_source,
        replacement_file=replacement_file,
        force=op.force,
        allow_name_change=op.allow_name_change,
        label=op.label,
    )


def _validate_replacement_definition(source: str, *, target: SymbolRecord, allow_name_change: bool) -> None:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise PatchError(f"Replacement file/source is not valid Python: line {exc.lineno}: {exc.msg}") from exc

    defs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    non_defs = [n for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    if non_defs:
        raise PatchError("Replacement must contain only one top-level def, async def, or class.")
    if len(defs) != 1:
        raise PatchError("Replacement must contain exactly one top-level def, async def, or class.")
    replacement = defs[0]
    replacement_kind = "class" if isinstance(replacement, ast.ClassDef) else "async_function" if isinstance(replacement, ast.AsyncFunctionDef) else "function"
    target_family = "class" if target.kind == "class" else "async_function" if target.kind == "async_function" else "function"
    if replacement_kind != target_family:
        raise PatchError(f"Replacement kind {replacement_kind!r} does not match target kind {target.kind!r}.")
    if not allow_name_change and replacement.name != target.name:
        raise PatchError(
            f"Replacement name {replacement.name!r} does not match target name {target.name!r}. "
            "Pass --allow-name-change if intentional."
        )


def _assert_no_duplicate_symbols(prepared: list[PreparedReplacement]) -> None:
    seen: set[str] = set()
    for prep in prepared:
        if prep.symbol.id in seen:
            raise PatchError(f"Duplicate replacement for symbol {prep.symbol.id}.")
        seen.add(prep.symbol.id)


def _assert_no_overlapping_ranges(prepared: list[PreparedReplacement]) -> None:
    ordered = sorted(prepared, key=lambda p: p.symbol.start_line)
    for prev, curr in zip(ordered, ordered[1:]):
        if curr.symbol.start_line <= prev.symbol.end_line:
            raise PatchError(
                f"Overlapping replacements in {curr.symbol.file}: {prev.symbol.id} overlaps {curr.symbol.id}. "
                "Patch nested symbols separately or replace the parent only."
            )


def _record_before_map(record: dict[str, Any]) -> dict[str, str]:
    before_files = record.get("before_files")
    if isinstance(before_files, dict):
        return {str(k): str(v) for k, v in before_files.items()}
    file_rel = record.get("file")
    before_rel = record.get("before")
    if file_rel and before_rel and str(before_rel).endswith(".py"):
        return {str(file_rel): str(before_rel)}
    return {}


def _generic_patch_id(operation: str, changes: list[dict[str, Any]]) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if len(changes) == 1:
        suffix = _safe_text(changes[0]["symbol_id"])
    else:
        suffix = f"{len(changes)}_changes"
    return f"{stamp}_{_safe_text(operation)}_{suffix}"


def _rollback_id(record: dict[str, Any]) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}_rollback_{_safe_text(record.get('id', 'unknown'))}"


def _safe_text(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in text)[:160]


def _json_dumps(data: Any) -> str:
    import json

    return json.dumps(data, indent=2, sort_keys=True)
