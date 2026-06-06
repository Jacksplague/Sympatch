from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

from .models import CallEdge, ProjectIndex, SymbolRecord
from .utils import line_slice, read_text


def resolve_symbol(index: ProjectIndex, query: str) -> SymbolRecord:
    symbol = index.find_symbol(query)
    if symbol is not None:
        return symbol
    matches = index.search_symbols(query)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"No symbol matched: {query}")
    raise ValueError("Ambiguous symbol query. Matches: " + ", ".join(s.id for s in matches[:10]))


def build_context_slice(
    index: ProjectIndex,
    root: Path,
    query: str,
    *,
    depth: int = 1,
    direction: str = "both",
    include_source: bool = True,
) -> dict[str, Any]:
    target = resolve_symbol(index, query)
    by_id = {s.id: s for s in index.all_symbols()}
    internal_ids = set(by_id)
    outgoing: dict[str, list[CallEdge]] = {}
    incoming: dict[str, list[CallEdge]] = {}
    for edge in index.call_edges:
        if edge.caller in internal_ids:
            outgoing.setdefault(edge.caller, []).append(edge)
        if edge.callee in internal_ids:
            incoming.setdefault(edge.callee, []).append(edge)

    visited: dict[str, tuple[int, str]] = {target.id: (0, "target")}
    q: deque[tuple[str, int]] = deque([(target.id, 0)])
    selected_edges: list[CallEdge] = []

    while q:
        sid, dist = q.popleft()
        if dist >= depth:
            continue
        next_edges: list[tuple[CallEdge, str]] = []
        if direction in {"both", "out", "dependencies", "callees"}:
            next_edges.extend((e, "dependency") for e in outgoing.get(sid, []) if e.callee in internal_ids)
        if direction in {"both", "in", "callers"}:
            next_edges.extend((e, "caller") for e in incoming.get(sid, []) if e.caller in internal_ids)
        for edge, role in next_edges:
            selected_edges.append(edge)
            nid = edge.callee if role == "dependency" else edge.caller
            if nid not in visited:
                visited[nid] = (dist + 1, role)
                q.append((nid, dist + 1))

    ordered_symbols = sorted((by_id[sid] for sid in visited), key=lambda s: (s.file, s.start_line, s.id))
    symbol_payloads = []
    for s in ordered_symbols:
        _, role = visited[s.id]
        payload: dict[str, Any] = {
            "id": s.id,
            "role": role,
            "file": s.file,
            "module": s.module,
            "start_line": s.start_line,
            "end_line": s.end_line,
            "signature": s.signature,
            "docstring": s.docstring,
            "hash": s.source_hash,
        }
        if include_source:
            payload["source"] = line_slice(read_text(root / s.file), s.start_line, s.end_line)
        symbol_payloads.append(payload)

    external_or_low_confidence = [e for e in selected_edges if e.callee not in internal_ids or e.confidence < 0.5]
    warnings = []
    if external_or_low_confidence:
        warnings.append(f"Slice includes {len(external_or_low_confidence)} unresolved or low-confidence call edge(s).")

    files = sorted({s.file for s in ordered_symbols})
    return {
        "ok": True,
        "target": target.id,
        "depth": depth,
        "direction": direction,
        "symbols": symbol_payloads,
        "call_edges": [e.to_dict() for e in selected_edges],
        "files": files,
        "warnings": warnings,
        "slice_hashes": {s.id: s.source_hash for s in ordered_symbols},
    }


def analyze_symbol(index: ProjectIndex, query: str) -> dict[str, Any]:
    target = resolve_symbol(index, query)
    internal = {s.id for s in index.all_symbols()}
    outgoing = [e for e in index.call_edges if e.caller == target.id]
    incoming = [e for e in index.call_edges if e.callee == target.id]
    internal_out = [e for e in outgoing if e.callee in internal]
    reasons: list[str] = []
    if outgoing:
        reasons.append(f"Symbol has {len(outgoing)} direct callee/dependency call(s).")
    if incoming:
        reasons.append(f"Symbol has {len(incoming)} internal static caller(s).")
    else:
        reasons.append("No internal static callers found; target may be an entry point, callback, dynamic call, or unused.")
    if target.name.startswith("_"):
        reasons.append("Target appears internal by naming convention.")
    important_words = ("validate", "patch", "parse", "tool", "loop", "state", "index", "write", "read")
    if any(w in target.name.lower() for w in important_words):
        reasons.append("Target name suggests participation in validation, patching, parsing, tool, loop, or state flow.")

    tests = ["Run Python syntax validation for changed files."]
    if internal_out:
        tests.append("Run import check for modules in the dependency slice.")
    if "tool" in target.id.lower() or "execute" in target.name.lower():
        tests.append("Run an agent-requested tool call and an automated validation path separately.")

    return {
        "ok": True,
        "target": target.to_dict(),
        "outgoing_calls": [e.to_dict() for e in outgoing],
        "incoming_calls": [e.to_dict() for e in incoming],
        "reasons": reasons,
        "recommended_tests": tests,
    }
