from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import ProjectIndex
from .patcher import PatchError, ReplacementOperation, apply_replacements_transaction, preview_replacements_transaction
from .utils import line_slice, read_text, relpath, sha256_text


@dataclass(slots=True)
class ParsedSymbol:
    name: str
    qualname: str
    kind: str
    start_line: int
    end_line: int
    source: str
    ast_hash: str
    parent: str | None

    @property
    def is_class(self) -> bool:
        return self.kind == "class"


def reconcile_file(
    root: Path,
    index: ProjectIndex,
    target_file: Path,
    rewritten_file: Path,
    *,
    apply: bool = False,
    validate: bool = True,
    force: bool = False,
    allow_name_change: bool = False,
    include_classes: bool = False,
    quiet: bool = False,
    run_hooks: bool = False,
) -> dict[str, Any]:
    """Compare a real project file to an AI-rewritten file and patch changed symbols only."""
    plan = plan_reconcile_operations(
        root,
        index,
        target_file,
        rewritten_file,
        force=force,
        allow_name_change=allow_name_change,
        include_classes=include_classes,
    )
    operations = plan["operations"]
    metadata = plan["metadata"]

    if not operations:
        return {
            "ok": True,
            "applied": False,
            "target_file": plan["target_file"],
            "rewritten_file": plan["rewritten_file"],
            "changed_symbols": [],
            "added_symbols_not_applied": plan["added_symbols_not_applied"],
            "deleted_symbols_not_applied": plan["deleted_symbols_not_applied"],
            "skipped_symbols": plan["skipped_symbols"],
            "message": "No applicable changed symbols found.",
        }

    if apply:
        record = apply_replacements_transaction(
            root,
            operations,
            operation="reconcile",
            validate=validate,
            quiet=quiet,
            metadata=metadata,
            run_hooks=run_hooks,
        )
    else:
        record = preview_replacements_transaction(
            root,
            operations,
            operation="reconcile_preview",
            validate=validate,
            metadata=metadata,
        )
    record.update(
        {
            "ok": True,
            "applied": apply,
            "target_file": plan["target_file"],
            "rewritten_file": plan["rewritten_file"],
            "changed_symbols": plan["changed_symbols"],
            "added_symbols_not_applied": plan["added_symbols_not_applied"],
            "deleted_symbols_not_applied": plan["deleted_symbols_not_applied"],
            "skipped_symbols": plan["skipped_symbols"],
        }
    )
    return record


def plan_reconcile_operations(
    root: Path,
    index: ProjectIndex,
    target_file: Path,
    rewritten_file: Path,
    *,
    force: bool = False,
    allow_name_change: bool = False,
    include_classes: bool = False,
) -> dict[str, Any]:
    """Return replacement operations mined from a full-file rewrite without applying them."""
    root = root.resolve()
    target_path = _resolve_target_path(root, target_file)
    rewritten_path = rewritten_file if rewritten_file.is_absolute() else (Path.cwd() / rewritten_file).resolve()
    if not target_path.exists():
        raise PatchError(f"Target file not found: {target_path}")
    if not rewritten_path.exists():
        raise PatchError(f"Rewritten file not found: {rewritten_path}")
    file_rel = relpath(target_path, root)

    indexed_by_qualname = {s.qualname: s for s in index.all_symbols() if s.file == file_rel}
    if not indexed_by_qualname:
        raise PatchError(f"No indexed symbols found for {file_rel}. Run `sympatch index` first.")

    original_symbols = _parse_symbols(read_text(target_path), file_rel)
    rewritten_symbols = _parse_symbols(read_text(rewritten_path), rewritten_path.as_posix())
    original_by_q = {s.qualname: s for s in original_symbols}
    rewritten_by_q = {s.qualname: s for s in rewritten_symbols}

    changed: list[ParsedSymbol] = []
    skipped: list[dict[str, Any]] = []

    for qualname, original in original_by_q.items():
        rewritten = rewritten_by_q.get(qualname)
        if rewritten is None:
            continue
        if not _compatible_kind(original.kind, rewritten.kind):
            skipped.append({"qualname": qualname, "reason": f"kind changed from {original.kind} to {rewritten.kind}"})
            continue
        if original.ast_hash == rewritten.ast_hash:
            continue
        if original.is_class and not include_classes:
            skipped.append({"qualname": qualname, "reason": "class changed; skipped by default to avoid broad parent replacement"})
            continue
        if qualname not in indexed_by_qualname:
            skipped.append({"qualname": qualname, "reason": "changed symbol is not present in the current index"})
            continue
        changed.append(rewritten)

    # Avoid replacing a class and one of its children in the same transaction.
    selected_qualnames = {s.qualname for s in changed}
    selected: list[ParsedSymbol] = []
    for sym in changed:
        parent_selected = False
        parent = sym.parent
        while parent:
            if parent in selected_qualnames:
                parent_selected = True
                break
            parent = original_by_q.get(parent).parent if original_by_q.get(parent) else None
        if parent_selected:
            skipped.append({"qualname": sym.qualname, "reason": "parent symbol is already selected for replacement"})
        else:
            selected.append(sym)

    added = sorted(q for q in rewritten_by_q if q not in original_by_q)
    deleted = sorted(q for q in original_by_q if q not in rewritten_by_q)

    operations = [
        ReplacementOperation(
            symbol_query=f"{indexed_by_qualname[s.qualname].module}.{s.qualname}",
            replacement_source=s.source,
            replacement_file=rewritten_path,
            force=force,
            allow_name_change=allow_name_change,
            label="reconcile",
        )
        for s in selected
    ]

    metadata = {
        "target_file": file_rel,
        "rewritten_file": str(rewritten_path),
        "added_symbols_not_applied": added,
        "deleted_symbols_not_applied": deleted,
        "skipped_symbols": skipped,
        "include_classes": include_classes,
    }
    return {
        "operations": operations,
        "metadata": metadata,
        "target_file": file_rel,
        "rewritten_file": str(rewritten_path),
        "changed_symbols": [s.qualname for s in selected],
        "added_symbols_not_applied": added,
        "deleted_symbols_not_applied": deleted,
        "skipped_symbols": skipped,
    }


def _parse_symbols(source: str, filename: str) -> list[ParsedSymbol]:
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        raise PatchError(f"Cannot parse {filename}: line {exc.lineno}: {exc.msg}") from exc
    collector = _ReconcileCollector(source)
    collector.visit(tree)
    return collector.symbols


class _ReconcileCollector(ast.NodeVisitor):
    def __init__(self, source: str) -> None:
        self.source = source
        self.stack: list[str] = []
        self.class_stack: list[str] = []
        self.symbols: list[ParsedSymbol] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:  # noqa: N802
        self._add(node, "class")
        self.stack.append(node.name)
        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:  # noqa: N802
        self._visit_function(node, "function")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:  # noqa: N802
        self._visit_function(node, "async_function")

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, base_kind: str) -> None:
        kind = "method" if self.class_stack else base_kind
        self._add(node, kind)
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def _add(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef, kind: str) -> None:
        qualname = ".".join([*self.stack, node.name]) if self.stack else node.name
        parent = ".".join(self.stack) if self.stack else None
        start_line = getattr(node, "lineno", 1)
        end_line = getattr(node, "end_lineno", start_line)
        self.symbols.append(
            ParsedSymbol(
                name=node.name,
                qualname=qualname,
                kind=kind,
                start_line=start_line,
                end_line=end_line,
                source=line_slice(self.source, start_line, end_line),
                ast_hash=sha256_text(ast.dump(node, include_attributes=False)),
                parent=parent,
            )
        )


def _resolve_target_path(root: Path, target_file: Path) -> Path:
    if target_file.is_absolute():
        path = target_file.resolve()
    else:
        path = (root / target_file).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise PatchError(f"Target file must be inside project root: {path}") from exc
    return path


def _compatible_kind(a: str, b: str) -> bool:
    if a == b:
        return True
    function_family = {"function", "method"}
    return a in function_family and b in function_family
