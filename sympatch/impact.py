from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from .context import resolve_symbol
from .models import CallEdge, ProjectIndex, SymbolRecord
from .utils import read_text


class _CallUsageCollector(ast.NodeVisitor):
    def __init__(self, target: SymbolRecord, candidate_raw_names: set[str], containing: list[SymbolRecord]) -> None:
        self.target = target
        self.candidate_raw_names = candidate_raw_names
        self.containing = sorted(containing, key=lambda s: (s.start_line, -(s.end_line - s.start_line)))
        self.usages: list[dict[str, Any]] = []
        self.parents: dict[ast.AST, ast.AST] = {}

    def visit(self, node: ast.AST) -> Any:  # noqa: ANN401
        for child in ast.iter_child_nodes(node):
            self.parents[child] = node
        return super().visit(node)

    def visit_Call(self, node: ast.Call) -> Any:  # noqa: N802
        raw = _callee_name(node.func)
        if raw in self.candidate_raw_names:
            caller = _containing_symbol(self.containing, getattr(node, "lineno", 0))
            self.usages.append(
                {
                    "file": self.target.file,
                    "line": getattr(node, "lineno", 0),
                    "raw_callee": raw,
                    "caller": caller.id if caller else None,
                    "positional_args": len(node.args),
                    "keyword_args": [kw.arg if kw.arg is not None else "**" for kw in node.keywords],
                    "has_starargs": any(isinstance(arg, ast.Starred) for arg in node.args),
                    "has_kwargs": any(kw.arg is None for kw in node.keywords),
                    "return_usage": _return_usage(node, self.parents),
                }
            )
        self.generic_visit(node)


def analyze_impact(index: ProjectIndex, root: Path, query: str) -> dict[str, Any]:
    """Build a stronger static caller/dependency impact report for one symbol."""
    target = resolve_symbol(index, query)
    symbols = index.all_symbols()
    internal_ids = {s.id for s in symbols}
    incoming = [e for e in index.call_edges if e.callee == target.id]
    outgoing = [e for e in index.call_edges if e.caller == target.id]
    unresolved_outgoing = [e for e in outgoing if e.callee not in internal_ids or e.confidence < 0.5]
    internal_outgoing = [e for e in outgoing if e.callee in internal_ids]

    call_usages = _collect_call_usages(index, root, target, incoming)
    direct_imports = _direct_imports(index, target)
    same_name_methods = _same_name_methods(symbols, target)
    signature = _signature_facts(target)

    signature_risk, signature_reasons = _signature_risk(incoming, call_usages, direct_imports, same_name_methods, signature)
    return_risk, return_reasons = _return_risk(call_usages)
    dependency_risk, dependency_reasons = _dependency_risk(outgoing, unresolved_outgoing)
    overall = _max_risk(signature_risk, return_risk, dependency_risk)

    recommended_tests = _recommended_tests(target, call_usages, internal_outgoing, unresolved_outgoing)

    return {
        "ok": True,
        "target": target.to_dict(),
        "risk": {
            "overall": overall,
            "signature": signature_risk,
            "return_value": return_risk,
            "dependencies": dependency_risk,
        },
        "signature_facts": signature,
        "callers": {
            "count": len(incoming),
            "edges": [e.to_dict() for e in incoming],
            "call_sites": call_usages,
            "direct_imports": direct_imports,
        },
        "return_value": {
            "used_call_sites": [u for u in call_usages if u.get("return_usage") != "ignored"],
            "ignored_call_sites": [u for u in call_usages if u.get("return_usage") == "ignored"],
        },
        "dependencies": {
            "outgoing_count": len(outgoing),
            "internal_outgoing": [e.to_dict() for e in internal_outgoing],
            "unresolved_or_low_confidence": [e.to_dict() for e in unresolved_outgoing],
        },
        "override_or_same_name_candidates": [s.to_dict() for s in same_name_methods],
        "reasons": {
            "signature": signature_reasons,
            "return_value": return_reasons,
            "dependencies": dependency_reasons,
        },
        "recommended_tests": recommended_tests,
        "notes": [
            "Impact analysis is static and conservative; dynamic dispatch, callbacks, reflection, monkey-patching, and string-based imports may be missed.",
            "Treat low-confidence or unresolved callees as review targets before changing the target's contract.",
        ],
    }


def _collect_call_usages(index: ProjectIndex, root: Path, target: SymbolRecord, incoming: list[CallEdge]) -> list[dict[str, Any]]:
    raw_by_file: dict[str, set[str]] = {}
    for edge in incoming:
        raw_by_file.setdefault(edge.file, set()).add(edge.raw_callee)
    # If the index did not resolve any incoming edge, still try common in-file forms.
    if not raw_by_file:
        raw_by_file[target.file] = {target.name}
        if target.kind == "method" and "." in target.qualname:
            raw_by_file[target.file].update({f"self.{target.name}", f"cls.{target.name}"})
    out: list[dict[str, Any]] = []
    symbols_by_file: dict[str, list[SymbolRecord]] = {}
    for s in index.all_symbols():
        symbols_by_file.setdefault(s.file, []).append(s)
    for file_rel, raw_names in sorted(raw_by_file.items()):
        path = root / file_rel
        if not path.exists():
            continue
        try:
            tree = ast.parse(read_text(path), filename=str(path))
        except SyntaxError:
            continue
        # Reuse one collector per file, but target.file is rewritten into individual usage rows below.
        collector = _CallUsageCollector(target, raw_names, symbols_by_file.get(file_rel, []))
        collector.visit(tree)
        for usage in collector.usages:
            usage["file"] = file_rel
        out.extend(collector.usages)
    return sorted(out, key=lambda u: (u["file"], u["line"], u["raw_callee"]))


def _direct_imports(index: ProjectIndex, target: SymbolRecord) -> list[dict[str, Any]]:
    imports: list[dict[str, Any]] = []
    for imp in index.all_imports():
        imported = None
        if imp.import_type == "from" and imp.name:
            imported = f"{imp.module}.{imp.name}".strip(".")
        elif imp.import_type == "import":
            imported = imp.module
        if imported in {target.id, f"{target.module}.{target.name}", target.module}:
            imports.append(imp.to_dict())
    return imports


def _same_name_methods(symbols: list[SymbolRecord], target: SymbolRecord) -> list[SymbolRecord]:
    if target.kind != "method":
        return []
    return [s for s in symbols if s.id != target.id and s.kind == "method" and s.name == target.name]


def _signature_facts(target: SymbolRecord) -> dict[str, Any]:
    facts: dict[str, Any] = {"signature": target.signature, "parameters": [], "has_varargs": False, "has_kwargs": False, "returns_annotation": False}
    try:
        tree = ast.parse(target.signature + "\n    pass\n")
        fn = next((n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
        if fn is None:
            return facts
        args = fn.args
        facts["parameters"] = [a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]]
        facts["has_varargs"] = args.vararg is not None
        facts["has_kwargs"] = args.kwarg is not None
        facts["returns_annotation"] = fn.returns is not None
        facts["positional_count"] = len(args.posonlyargs) + len(args.args)
        facts["keyword_only_count"] = len(args.kwonlyargs)
    except SyntaxError:
        pass
    return facts


def _signature_risk(
    incoming: list[CallEdge],
    call_usages: list[dict[str, Any]],
    direct_imports: list[dict[str, Any]],
    same_name_methods: list[SymbolRecord],
    signature: dict[str, Any],
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    risk = "low"
    if incoming:
        reasons.append(f"{len(incoming)} resolved internal caller edge(s) depend on this symbol.")
        risk = "medium"
    positional = sum(1 for u in call_usages if u.get("positional_args", 0) > 0)
    keyword = sum(1 for u in call_usages if u.get("keyword_args"))
    if positional:
        reasons.append(f"{positional} call site(s) pass positional arguments; parameter reordering is risky.")
        risk = _max_risk(risk, "medium")
    if keyword:
        reasons.append(f"{keyword} call site(s) pass keyword arguments; parameter renames are risky.")
        risk = _max_risk(risk, "high")
    if direct_imports:
        reasons.append(f"{len(direct_imports)} direct import(s) may break if the symbol is renamed or moved.")
        risk = _max_risk(risk, "high")
    if same_name_methods:
        reasons.append(f"{len(same_name_methods)} same-name method(s) exist; inheritance/protocol coupling is possible.")
        risk = _max_risk(risk, "medium")
    if signature.get("has_varargs") or signature.get("has_kwargs"):
        reasons.append("Target accepts *args or **kwargs; static call-site arity is less certain.")
        risk = _max_risk(risk, "medium")
    if not reasons:
        reasons.append("No resolved internal call sites were found; this may be unused, dynamically called, or an entry point.")
    return risk, reasons


def _return_risk(call_usages: list[dict[str, Any]]) -> tuple[str, list[str]]:
    used = [u for u in call_usages if u.get("return_usage") != "ignored"]
    if not call_usages:
        return "unknown", ["No concrete call sites were inspected; return-value impact is unknown."]
    if used:
        return "high", [f"{len(used)} call site(s) appear to use the return value."]
    return "low", ["All inspected call sites appear to ignore the return value."]


def _dependency_risk(outgoing: list[CallEdge], unresolved: list[CallEdge]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    risk = "low"
    if outgoing:
        reasons.append(f"Target calls {len(outgoing)} direct dependency/dependencies.")
        risk = "medium"
    if unresolved:
        reasons.append(f"{len(unresolved)} outgoing call(s) are unresolved or low-confidence.")
        risk = _max_risk(risk, "high")
    if not reasons:
        reasons.append("No direct outgoing calls were found.")
    return risk, reasons


def _recommended_tests(
    target: SymbolRecord,
    call_usages: list[dict[str, Any]],
    internal_outgoing: list[CallEdge],
    unresolved_outgoing: list[CallEdge],
) -> list[str]:
    tests = [
        f"sympatch validate {target.file}",
        "Run the narrowest unit/integration test that exercises each listed caller.",
    ]
    if call_usages:
        callers = sorted({u.get("caller") for u in call_usages if u.get("caller")})
        if callers:
            tests.append("Exercise callers: " + ", ".join(callers[:8]) + (" ..." if len(callers) > 8 else ""))
    if internal_outgoing:
        tests.append("Exercise dependency path(s): " + ", ".join(sorted({e.callee for e in internal_outgoing})[:8]))
    if unresolved_outgoing:
        tests.append("Manually review unresolved/low-confidence calls before changing control flow.")
    if any(word in target.id.lower() for word in ("validate", "patch", "tool", "session", "state", "parse", "index")):
        tests.append("Run the agent/tool-loop smoke path, not just syntax validation.")
    return tests


def _callee_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _callee_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _callee_name(node.func)
    return None


def _containing_symbol(symbols: list[SymbolRecord], line: int) -> SymbolRecord | None:
    matches = [s for s in symbols if s.start_line <= line <= s.end_line]
    if not matches:
        return None
    return max(matches, key=lambda s: (s.start_line, s.end_line - s.start_line))


def _return_usage(call: ast.Call, parents: dict[ast.AST, ast.AST]) -> str:
    parent = parents.get(call)
    child: ast.AST = call
    while parent is not None:
        if isinstance(parent, ast.Expr):
            return "ignored"
        if isinstance(parent, ast.Assign):
            return "assigned"
        if isinstance(parent, ast.AnnAssign):
            return "assigned"
        if isinstance(parent, ast.AugAssign):
            return "augmented"
        if isinstance(parent, ast.Return):
            return "returned"
        if isinstance(parent, (ast.If, ast.While)) and getattr(parent, "test", None) is child:
            return "branch_condition"
        if isinstance(parent, ast.BoolOp):
            child = parent
            parent = parents.get(parent)
            continue
        if isinstance(parent, ast.Compare):
            return "comparison"
        if isinstance(parent, ast.Call):
            return "argument"
        if isinstance(parent, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
            return "container"
        child = parent
        parent = parents.get(parent)
    return "unknown"


def _max_risk(*levels: str) -> str:
    order = {"low": 0, "medium": 1, "high": 2, "unknown": 1}
    return max(levels, key=lambda level: order.get(level, 0))
