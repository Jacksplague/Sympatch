from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .context import analyze_symbol, build_context_slice, resolve_symbol
from .models import ImportRecord, ProjectIndex, SymbolRecord
from .utils import line_slice, read_text


DIRECTION_CHOICES = {"both", "in", "out", "callers", "dependencies", "callees"}


def build_context_bundle(
    index: ProjectIndex,
    root: Path,
    query: str,
    *,
    depth: int = 1,
    direction: str = "both",
    include_source: bool = True,
) -> dict[str, Any]:
    """Build an LLM-oriented patch brief around one target symbol.

    The raw context slice remains available through `sympatch context`. This bundle
    adds the information an agent usually needs before producing a replacement:
    target constraints, direct call graph facts, nearby siblings, relevant imports,
    validation guidance, and stable source hashes.
    """
    if direction not in DIRECTION_CHOICES:
        raise ValueError(f"Unsupported direction: {direction}")
    root = root.resolve()
    target = resolve_symbol(index, query)
    context = build_context_slice(
        index,
        root,
        target.id,
        depth=max(0, depth),
        direction=direction,
        include_source=include_source,
    )
    analysis = analyze_symbol(index, target.id)
    files = sorted(set(context.get("files", [])) | {target.file})
    file_records = {f.path: f for f in index.files}
    imports_by_file = {
        file_rel: [_import_to_compact(i) for i in file_records[file_rel].imports]
        for file_rel in files
        if file_rel in file_records
    }
    direct_callers = [e.to_dict() for e in index.call_edges if e.callee == target.id]
    direct_dependencies = [e.to_dict() for e in index.call_edges if e.caller == target.id]
    nearby = _nearby_symbols(index, target, include_source=include_source, root=root)

    bundle = {
        "ok": True,
        "bundle_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(root),
        "target": _symbol_payload(target, root=root, include_source=include_source),
        "replacement_constraints": _replacement_constraints(target),
        "direct_callers": direct_callers,
        "direct_dependencies": direct_dependencies,
        "nearby_symbols": nearby,
        "imports_by_file": imports_by_file,
        "context_slice": context,
        "analysis": analysis,
        "validation_plan": _validation_plan(target),
        "patch_intent_template": replacement_intent_template(target),
        "agent_notes": _agent_notes(target),
    }
    return bundle


def render_bundle_markdown(bundle: dict[str, Any]) -> str:
    target = bundle["target"]
    constraints = bundle["replacement_constraints"]
    validation = bundle["validation_plan"]
    lines: list[str] = []
    lines.append(f"# Sympatch LLM Context Bundle: `{target['id']}`")
    lines.append("")
    lines.append(f"Generated: `{bundle['generated_at']}`")
    lines.append(f"Project root: `{bundle['project_root']}`")
    lines.append("")
    lines.append("## Target")
    lines.append("")
    lines.append(f"- Symbol: `{target['id']}`")
    lines.append(f"- Kind: `{target['kind']}`")
    lines.append(f"- File: `{target['file']}:{target['start_line']}-{target['end_line']}`")
    lines.append(f"- Hash: `{target['hash']}`")
    if target.get("signature"):
        lines.append(f"- Signature: `{target['signature'].strip()}`")
    if target.get("docstring"):
        lines.append(f"- Docstring: {target['docstring']}")
    lines.append("")

    if target.get("source"):
        lines.append("### Target source")
        lines.append("")
        lines.append("```python")
        lines.append(target["source"].rstrip())
        lines.append("```")
        lines.append("")

    lines.append("## Replacement constraints")
    lines.append("")
    for item in constraints["rules"]:
        lines.append(f"- {item}")
    lines.append("")

    lines.append("## Direct callers")
    lines.append("")
    _append_edges(lines, bundle.get("direct_callers", []), empty="No direct static callers found.")
    lines.append("")

    lines.append("## Direct dependencies / callees")
    lines.append("")
    _append_edges(lines, bundle.get("direct_dependencies", []), empty="No direct static dependencies found.")
    lines.append("")

    lines.append("## Nearby symbols")
    lines.append("")
    nearby = bundle.get("nearby_symbols", [])
    if not nearby:
        lines.append("No nearby symbols selected.")
    else:
        for sym in nearby:
            lines.append(f"### `{sym['id']}`")
            lines.append(f"- Role: `{sym['role']}`")
            lines.append(f"- File: `{sym['file']}:{sym['start_line']}-{sym['end_line']}`")
            if sym.get("signature"):
                lines.append(f"- Signature: `{sym['signature'].strip()}`")
            if sym.get("source"):
                lines.append("")
                lines.append("```python")
                lines.append(sym["source"].rstrip())
                lines.append("```")
            lines.append("")

    lines.append("## Imports by file")
    lines.append("")
    imports_by_file = bundle.get("imports_by_file", {})
    if not imports_by_file:
        lines.append("No imports recorded for selected files.")
    else:
        for file_rel, imports in imports_by_file.items():
            lines.append(f"### `{file_rel}`")
            if not imports:
                lines.append("No imports.")
            else:
                for imp in imports:
                    rendered = _render_import(imp)
                    lines.append(f"- line {imp['line']}: `{rendered}`")
            lines.append("")

    lines.append("## Analysis")
    lines.append("")
    for reason in bundle.get("analysis", {}).get("reasons", []):
        lines.append(f"- {reason}")
    if not bundle.get("analysis", {}).get("reasons"):
        lines.append("No analysis notes generated.")
    lines.append("")

    lines.append("## Validation plan")
    lines.append("")
    for step in validation["steps"]:
        lines.append(f"- {step}")
    lines.append("")
    lines.append("Suggested commands:")
    lines.append("")
    lines.append("```bash")
    for command in validation["commands"]:
        lines.append(command)
    lines.append("```")
    lines.append("")

    lines.append("## Patch intent template")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(bundle.get("patch_intent_template", {}), indent=2, sort_keys=True))
    lines.append("```")
    lines.append("")

    lines.append("## Agent notes")
    lines.append("")
    for note in bundle.get("agent_notes", []):
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def replacement_intent_template(target: SymbolRecord) -> dict[str, Any]:
    return {
        "version": "0.9.0",
        "name": f"patch-{target.name}",
        "reason": "Describe why this symbol needs to change.",
        "validate": True,
        "operations": [
            {
                "operation": "replace",
                "target": target.id,
                "source_file": "path/to/replacement_symbol.py",
                "expected_hash": target.source_hash,
                "allow_name_change": False,
                "force": False,
            }
        ],
    }


def _symbol_payload(symbol: SymbolRecord, *, root: Path, include_source: bool, role: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": symbol.id,
        "name": symbol.name,
        "qualname": symbol.qualname,
        "kind": symbol.kind,
        "file": symbol.file,
        "module": symbol.module,
        "start_line": symbol.start_line,
        "end_line": symbol.end_line,
        "signature": symbol.signature,
        "docstring": symbol.docstring,
        "hash": symbol.source_hash,
        "parent": symbol.parent,
        "decorators": symbol.decorators,
        "calls": symbol.calls,
    }
    if role:
        payload["role"] = role
    if include_source:
        payload["source"] = line_slice(read_text(root / symbol.file), symbol.start_line, symbol.end_line)
    return payload


def _nearby_symbols(index: ProjectIndex, target: SymbolRecord, *, include_source: bool, root: Path) -> list[dict[str, Any]]:
    symbols = index.all_symbols()
    nearby: list[tuple[str, SymbolRecord]] = []
    if target.parent:
        for s in symbols:
            if s.id != target.id and s.file == target.file and s.parent == target.parent:
                nearby.append(("same_parent", s))
    # Include the parent class/function if applicable; useful when replacing methods.
    if target.parent:
        for s in symbols:
            if s.file == target.file and s.qualname == target.parent:
                nearby.append(("parent", s))
                break
    # Include immediate file neighbors to preserve local style and helper conventions.
    file_symbols = sorted([s for s in symbols if s.file == target.file and s.id != target.id], key=lambda s: s.start_line)
    before = [s for s in file_symbols if s.end_line < target.start_line][-1:]
    after = [s for s in file_symbols if s.start_line > target.end_line][:1]
    for s in before:
        nearby.append(("previous_in_file", s))
    for s in after:
        nearby.append(("next_in_file", s))

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for role, sym in nearby:
        if sym.id in seen:
            continue
        seen.add(sym.id)
        deduped.append(_symbol_payload(sym, root=root, include_source=include_source, role=role))
    return deduped


def _replacement_constraints(target: SymbolRecord) -> dict[str, Any]:
    family = "class" if target.kind == "class" else "async def" if target.kind == "async_function" else "def"
    return {
        "target_kind": target.kind,
        "target_name": target.name,
        "target_hash": target.source_hash,
        "rules": [
            f"Replacement must contain exactly one top-level {family} matching `{target.name}` unless allow_name_change is explicitly enabled.",
            "Do not include imports, module-level assignments, comments outside the symbol body, or multiple top-level definitions in the replacement file.",
            "Preserve the public call contract unless the context bundle shows all callers and the intent deliberately updates them too.",
            f"Use the expected hash `{target.source_hash}` in patch intents to guard against stale context.",
            "Run a dry-run intent preview or session validation before committing agent-generated changes.",
        ],
    }


def _validation_plan(target: SymbolRecord) -> dict[str, Any]:
    return {
        "steps": [
            "Compile-check the changed file or project.",
            "Preview the patch diff before commit when generated by an LLM.",
            "Re-index after commit and verify the target symbol still resolves.",
            "Run the caller/dependency smoke path when the symbol participates in tool, validation, parsing, state, or execution flow.",
        ],
        "commands": [
            f"sympatch validate {target.file}",
            f"sympatch find {target.id}",
            "sympatch intent preview patch_intent.json",
            "sympatch intent apply patch_intent.json",
        ],
    }


def _agent_notes(target: SymbolRecord) -> list[str]:
    notes = [
        "Prefer minimal symbol replacement over whole-file rewriting.",
        "If more than one symbol must change, use a patch intent with multiple operations or a transaction session.",
        "Treat missing callers as inconclusive, not proof that the symbol is unused; dynamic dispatch and callbacks may not be visible statically.",
    ]
    if target.name.startswith("_"):
        notes.append("Target appears internal; check class/module invariants before changing behavior.")
    if any(word in target.id.lower() for word in ("validate", "patch", "tool", "session", "state", "parse", "index")):
        notes.append("Target name suggests infrastructure code; require a smoke test beyond syntax validation.")
    return notes


def _import_to_compact(record: ImportRecord) -> dict[str, Any]:
    return {
        "module": record.module,
        "name": record.name,
        "alias": record.alias,
        "line": record.line,
        "import_type": record.import_type,
    }


def _render_import(imp: dict[str, Any]) -> str:
    alias = f" as {imp['alias']}" if imp.get("alias") else ""
    if imp.get("import_type") == "from":
        name = imp.get("name") or "*"
        return f"from {imp['module']} import {name}{alias}"
    return f"import {imp['module']}{alias}"


def _append_edges(lines: list[str], edges: list[dict[str, Any]], *, empty: str) -> None:
    if not edges:
        lines.append(empty)
        return
    for edge in edges:
        lines.append(
            f"- `{edge['caller']}` -> `{edge['callee']}` "
            f"at `{edge['file']}:{edge['line']}` "
            f"raw=`{edge['raw_callee']}` confidence={edge['confidence']}"
        )
