from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import CallEdge, FileRecord, ImportRecord, ProjectIndex, SymbolRecord
from .utils import discover_python_files, line_slice, module_name_for, read_text, relpath, sha256_file, sha256_text


class _CallCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str]] = []

    def visit_Call(self, node: ast.Call) -> Any:  # noqa: N802
        raw, call_type = _callee_name(node.func)
        if raw:
            self.calls.append((raw, getattr(node, "lineno", 0), call_type))
        self.generic_visit(node)


class _ImportCollector(ast.NodeVisitor):
    def __init__(self, rel_file: str) -> None:
        self.rel_file = rel_file
        self.imports: list[ImportRecord] = []

    def visit_Import(self, node: ast.Import) -> Any:  # noqa: N802
        for alias in node.names:
            self.imports.append(
                ImportRecord(
                    module=alias.name,
                    name=None,
                    alias=alias.asname,
                    file=self.rel_file,
                    line=node.lineno,
                    import_type="import",
                )
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:  # noqa: N802
        module = "." * node.level + (node.module or "")
        for alias in node.names:
            self.imports.append(
                ImportRecord(
                    module=module,
                    name=alias.name,
                    alias=alias.asname,
                    file=self.rel_file,
                    line=node.lineno,
                    import_type="from",
                )
            )


class _SymbolCollector(ast.NodeVisitor):
    def __init__(self, module: str, rel_file: str, source: str) -> None:
        self.module = module
        self.rel_file = rel_file
        self.source = source
        self.lines = source.splitlines()
        self.class_stack: list[str] = []
        self.function_stack: list[str] = []
        self.symbols: list[SymbolRecord] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:  # noqa: N802
        self._add_symbol(node, "class")
        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:  # noqa: N802
        self._visit_function(node, "function")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:  # noqa: N802
        self._visit_function(node, "async_function")

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, base_kind: str) -> None:
        kind = "method" if self.class_stack else base_kind
        self._add_symbol(node, kind)
        self.function_stack.append(node.name)
        self.generic_visit(node)
        self.function_stack.pop()

    def _add_symbol(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef, kind: str) -> None:
        start = int(node.lineno)
        end = int(getattr(node, "end_lineno", node.lineno))
        qual_parts = [*self.class_stack, *self.function_stack, node.name]
        qualname = ".".join(qual_parts)
        symbol_id = f"{self.module}.{qualname}" if self.module else qualname
        source = line_slice(self.source, start, end)
        calls = _calls_in_node(node)
        decorators = [_safe_unparse(d) for d in getattr(node, "decorator_list", [])]
        parent = f"{self.module}." + ".".join(qual_parts[:-1]) if qual_parts[:-1] else self.module
        aliases = [f"{self.rel_file}::{qualname}"]
        self.symbols.append(
            SymbolRecord(
                id=symbol_id,
                name=node.name,
                qualname=qualname,
                kind=kind,
                file=self.rel_file,
                module=self.module,
                start_line=start,
                end_line=end,
                signature=_signature_from_source(self.lines, start),
                source_hash=sha256_text(source),
                docstring=ast.get_docstring(node),
                parent=parent if parent else None,
                decorators=decorators,
                calls=[raw for raw, _, _ in calls],
                aliases=aliases,
            )
        )


def scan_project(root: Path, explicit_paths: list[Path] | None = None) -> ProjectIndex:
    root = root.resolve()
    files: list[FileRecord] = []
    for path in discover_python_files(root, explicit_paths):
        files.append(index_file(root, path))
    project = ProjectIndex(
        root=str(root),
        generated_at=datetime.now(timezone.utc).isoformat(),
        files=files,
    )
    project.call_edges = build_call_edges(project, root)
    return project


def index_file(root: Path, path: Path) -> FileRecord:
    rel_file = relpath(path, root)
    module = module_name_for(rel_file)
    try:
        source = read_text(path)
    except UnicodeDecodeError as exc:
        return FileRecord(
            path=rel_file,
            module=module,
            sha256=sha256_file(path),
            line_count=0,
            parse_error=f"UnicodeDecodeError: {exc}",
        )
    lines = source.splitlines()
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return FileRecord(
            path=rel_file,
            module=module,
            sha256=sha256_file(path),
            line_count=len(lines),
            parse_error=f"line {exc.lineno}: {exc.msg}",
        )
    imports = _ImportCollector(rel_file)
    imports.visit(tree)
    symbols = _SymbolCollector(module, rel_file, source)
    symbols.visit(tree)
    return FileRecord(
        path=rel_file,
        module=module,
        sha256=sha256_file(path),
        line_count=len(lines),
        symbols=symbols.symbols,
        imports=imports.imports,
    )


def build_call_edges(index: ProjectIndex, root: Path) -> list[CallEdge]:
    by_id = {s.id: s for s in index.all_symbols()}
    by_module_qual = {(s.module, s.qualname): s for s in index.all_symbols()}
    by_module_name = {(s.module, s.name): s for s in index.all_symbols()}
    by_short_qual: dict[str, list[SymbolRecord]] = {}
    for s in index.all_symbols():
        by_short_qual.setdefault(s.qualname, []).append(s)
        by_short_qual.setdefault(s.name, []).append(s)

    import_aliases: dict[tuple[str, str], str] = {}
    for f in index.files:
        for imp in f.imports:
            if imp.import_type == "import":
                visible = imp.alias or imp.module.split(".")[0]
                import_aliases[(f.module, visible)] = imp.module
            elif imp.name:
                visible = imp.alias or imp.name
                import_aliases[(f.module, visible)] = f"{imp.module}.{imp.name}".strip(".")

    edges: list[CallEdge] = []
    for f in index.files:
        if f.parse_error:
            continue
        source = read_text(root / f.path)
        tree = ast.parse(source, filename=str(root / f.path))
        spans = sorted(f.symbols, key=lambda s: (s.start_line, -(s.end_line - s.start_line)))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            raw, call_type = _callee_name(node.func)
            if not raw:
                continue
            caller = _containing_symbol(spans, getattr(node, "lineno", 0))
            if caller is None:
                continue
            callee, confidence = _resolve_call(raw, caller, by_id, by_module_qual, by_module_name, by_short_qual, import_aliases)
            edges.append(
                CallEdge(
                    caller=caller.id,
                    callee=callee,
                    raw_callee=raw,
                    file=f.path,
                    line=getattr(node, "lineno", 0),
                    call_type=call_type,
                    confidence=confidence,
                )
            )
    return edges


def _resolve_call(
    raw: str,
    caller: SymbolRecord,
    by_id: dict[str, SymbolRecord],
    by_module_qual: dict[tuple[str, str], SymbolRecord],
    by_module_name: dict[tuple[str, str], SymbolRecord],
    by_short_qual: dict[str, list[SymbolRecord]],
    import_aliases: dict[tuple[str, str], str],
) -> tuple[str, float]:
    if raw.startswith("self.") and "." in caller.qualname:
        class_name = caller.qualname.split(".")[0]
        member = raw.split(".", 1)[1]
        candidate = f"{caller.module}.{class_name}.{member}"
        if candidate in by_id:
            return candidate, 0.90
    if raw.startswith("cls.") and "." in caller.qualname:
        class_name = caller.qualname.split(".")[0]
        member = raw.split(".", 1)[1]
        candidate = f"{caller.module}.{class_name}.{member}"
        if candidate in by_id:
            return candidate, 0.88
    same_module = by_module_qual.get((caller.module, raw)) or by_module_name.get((caller.module, raw))
    if same_module:
        return same_module.id, 0.96
    parts = raw.split(".")
    if parts:
        imported = import_aliases.get((caller.module, parts[0]))
        if imported:
            candidate = ".".join([imported, *parts[1:]])
            if candidate in by_id:
                return candidate, 0.82
            return candidate, 0.55
    matches = by_short_qual.get(raw, [])
    if len(matches) == 1:
        return matches[0].id, 0.70
    if len(matches) > 1:
        return raw, 0.25
    if raw in {"str", "int", "len", "print", "dict", "list", "set", "tuple", "isinstance", "enumerate", "range", "open", "compile"}:
        return raw, 0.10
    return raw, 0.10


def _containing_symbol(symbols: list[SymbolRecord], line: int) -> SymbolRecord | None:
    candidates = [s for s in symbols if s.start_line <= line <= s.end_line]
    if not candidates:
        return None
    return min(candidates, key=lambda s: s.end_line - s.start_line)


def _calls_in_node(node: ast.AST) -> list[tuple[str, int, str]]:
    collector = _CallCollector()
    for child in ast.iter_child_nodes(node):
        collector.visit(child)
    return collector.calls


def _callee_name(node: ast.AST) -> tuple[str, str]:
    if isinstance(node, ast.Name):
        return node.id, "direct"
    if isinstance(node, ast.Attribute):
        parts: list[str] = []
        cur: ast.AST = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
            return ".".join(reversed(parts)), "method" if parts[-1] in {"self", "cls"} else "attribute"
        if isinstance(cur, ast.Call):
            inner, _ = _callee_name(cur.func)
            parts.append(inner or "<call>")
            return ".".join(reversed(parts)), "attribute"
    return "", "unknown"


def _safe_unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return node.__class__.__name__


def _signature_from_source(lines: list[str], start_line: int) -> str:
    idx = start_line - 1
    collected: list[str] = []
    paren_balance = 0
    while idx < len(lines):
        line = lines[idx]
        collected.append(line.rstrip())
        paren_balance += line.count("(") - line.count(")")
        if line.rstrip().endswith(":") and paren_balance <= 0:
            break
        if len(collected) >= 20:
            break
        idx += 1
    return "\n".join(collected)
