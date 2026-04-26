from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import ModuleRecord, ProjectIndex, SymbolRecord
from .utils import iter_python_files, normalize_relpath, read_text, sha256_file, sha256_text


FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
SYMBOL_NODES = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def scan_project(root: Path, specific: Path | None = None) -> ProjectIndex:
    root = root.resolve()
    modules = [index_module(path, root) for path in iter_python_files(root, specific)]
    return ProjectIndex(
        root=str(root),
        version="0.1.0",
        generated_at=datetime.now(timezone.utc).isoformat(),
        modules=modules,
    )


def index_module(path: Path, root: Path) -> ModuleRecord:
    rel = normalize_relpath(path, root)
    text = read_text(path)
    try:
        tree = ast.parse(text, filename=rel)
    except SyntaxError as exc:
        return ModuleRecord(file=rel, sha256=sha256_text(text), symbols=[], parse_error=str(exc))

    lines = text.splitlines()
    symbols: list[SymbolRecord] = []
    visit_body(
        body=tree.body,
        file_rel=rel,
        lines=lines,
        symbols=symbols,
        parents=[],
        in_class=False,
    )
    symbols.sort(key=lambda s: (s.start_line, s.end_line, s.id))
    return ModuleRecord(file=rel, sha256=sha256_file(path), symbols=symbols)


def visit_body(
    body: Iterable[ast.stmt],
    file_rel: str,
    lines: list[str],
    symbols: list[SymbolRecord],
    parents: list[str],
    in_class: bool,
) -> None:
    for node in body:
        if isinstance(node, ast.ClassDef):
            symbols.append(make_symbol(node, file_rel, lines, parents, kind="class"))
            visit_body(node.body, file_rel, lines, symbols, parents + [node.name], in_class=True)
        elif isinstance(node, ast.AsyncFunctionDef):
            kind = "async_method" if in_class else "async_function"
            symbols.append(make_symbol(node, file_rel, lines, parents, kind=kind))
            visit_body(node.body, file_rel, lines, symbols, parents + [node.name], in_class=False)
        elif isinstance(node, ast.FunctionDef):
            kind = "method" if in_class else "function"
            symbols.append(make_symbol(node, file_rel, lines, parents, kind=kind))
            visit_body(node.body, file_rel, lines, symbols, parents + [node.name], in_class=False)


def make_symbol(
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
    file_rel: str,
    lines: list[str],
    parents: list[str],
    kind: str,
) -> SymbolRecord:
    if getattr(node, "end_lineno", None) is None:
        raise ValueError(f"Python AST did not provide end_lineno for {node.name}")

    start = int(node.lineno)
    end = int(node.end_lineno)  # type: ignore[arg-type]
    qualname = ".".join(parents + [node.name])
    symbol_id = f"{file_rel}::{qualname}"
    source = "\n".join(lines[start - 1 : end])
    first_line = lines[start - 1] if 0 <= start - 1 < len(lines) else ""
    indent = len(first_line) - len(first_line.lstrip(" "))
    decorators = [safe_unparse(d) for d in getattr(node, "decorator_list", [])]
    signature = extract_signature(node, lines)
    parent = ".".join(parents) if parents else None
    docstring = ast.get_docstring(node, clean=True)

    return SymbolRecord(
        id=symbol_id,
        file=file_rel,
        kind=kind,
        name=node.name,
        qualname=qualname,
        signature=signature,
        start_line=start,
        end_line=end,
        indent=indent,
        source_hash=sha256_text(source),
        parent=parent,
        decorators=decorators,
        docstring=docstring,
    )


def extract_signature(
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
    lines: list[str],
) -> str:
    """Return a compact, human-readable header for a class/function node."""
    start = int(node.lineno)
    body_start = int(node.body[0].lineno) if getattr(node, "body", None) else start
    candidate_lines = lines[start - 1 : max(start, body_start - 1)]
    # Drop decorators if lineno points at the decorated def/class in this Python version.
    candidate = "\n".join(candidate_lines).strip()
    if not candidate:
        return ""
    # Most signatures are one line. For multiline signatures, preserve the compact header.
    if isinstance(node, ast.ClassDef):
        keyword = "class "
    elif isinstance(node, ast.AsyncFunctionDef):
        keyword = "async def "
    else:
        keyword = "def "
    idx = candidate.find(keyword)
    if idx >= 0:
        candidate = candidate[idx:]
    # Keep only header text before the body. This is heuristic but safe for display only.
    return " ".join(part.strip() for part in candidate.splitlines())


def safe_unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return "<unparseable>"
