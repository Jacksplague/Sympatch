from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .context import resolve_symbol
from .models import ProjectIndex
from .patcher import PatchError, ReplacementOperation, apply_replacements_transaction, preview_replacements_transaction
from .reconcile import plan_reconcile_operations
from .storage import load_index
from .utils import read_text, write_text


SUPPORTED_INTENT_VERSION_PREFIXES = ("0.8", "0.9", "1.")


def load_intent_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise PatchError(f"Intent file not found: {path}")
    try:
        data = json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise PatchError(f"Intent file is not valid JSON: line {exc.lineno}: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise PatchError("Intent file must contain a JSON object.")
    return data


def run_intent_file(
    root: Path,
    intent_file: Path,
    *,
    apply: bool = False,
    validate: bool | None = None,
    quiet: bool = False,
    run_hooks: bool = False,
) -> dict[str, Any]:
    """Preview or apply a declarative patch intent.

    Supported operations:
    - replace: symbol-level replacement from source_file or inline source
    - reconcile: mine changed symbols from a rewritten file

    All resulting replacements are executed as a single transaction.
    """
    root = root.resolve()
    intent_path = intent_file if intent_file.is_absolute() else (Path.cwd() / intent_file).resolve()
    raw = load_intent_file(intent_path)
    index = load_index(root)
    normalized = normalize_intent(raw, intent_path)
    do_validate = normalized["validate"] if validate is None else validate
    plan = plan_intent(root, index, normalized, intent_path)
    operations = plan["operations"]
    metadata = {
        "intent_file": str(intent_path),
        "intent_name": normalized.get("name"),
        "intent_reason": normalized.get("reason"),
        "intent_version": normalized.get("version"),
        "planned_at": datetime.now(timezone.utc).isoformat(),
        "operation_reports": plan["operation_reports"],
    }

    if not operations:
        return {
            "ok": True,
            "applied": False,
            "intent_file": str(intent_path),
            "operation_count": 0,
            "message": "Intent produced no applicable replacement operations.",
            "operation_reports": plan["operation_reports"],
            "warnings": plan["warnings"],
        }

    if apply:
        record = apply_replacements_transaction(
            root,
            operations,
            operation="intent_apply",
            validate=do_validate,
            quiet=quiet,
            metadata=metadata,
            run_hooks=run_hooks,
        )
    else:
        record = preview_replacements_transaction(
            root,
            operations,
            operation="intent_preview",
            validate=do_validate,
            metadata=metadata,
        )

    record.update(
        {
            "ok": True,
            "applied": apply,
            "intent_file": str(intent_path),
            "operation_count": len(operations),
            "operation_reports": plan["operation_reports"],
            "warnings": plan["warnings"],
        }
    )
    return record


def plan_intent(root: Path, index: ProjectIndex, intent: dict[str, Any], intent_path: Path) -> dict[str, Any]:
    operations: list[ReplacementOperation] = []
    reports: list[dict[str, Any]] = []
    warnings: list[str] = []
    base_dir = intent_path.parent

    for i, op in enumerate(intent["operations"], start=1):
        op_type = str(op.get("operation") or op.get("type") or "").strip().lower().replace("-", "_")
        if op_type in {"replace", "replace_symbol"}:
            replacement = _operation_to_replacement(root, index, op, base_dir, index_in_intent=i)
            operations.append(replacement)
            reports.append({"index": i, "operation": "replace", "target": replacement.symbol_query, "planned": True})
        elif op_type == "reconcile":
            target_file = _required_path_value(op, "target_file", i)
            rewritten_file = _required_path_value(op, "rewritten_file", i)
            rewritten_path = _resolve_external_path(base_dir, rewritten_file)
            reconcile_plan = plan_reconcile_operations(
                root,
                index,
                Path(target_file),
                rewritten_path,
                force=bool(op.get("force", False)),
                allow_name_change=bool(op.get("allow_name_change", False)),
                include_classes=bool(op.get("include_classes", False)),
            )
            operations.extend(reconcile_plan["operations"])
            report = {
                "index": i,
                "operation": "reconcile",
                "target_file": reconcile_plan["target_file"],
                "rewritten_file": reconcile_plan["rewritten_file"],
                "planned": bool(reconcile_plan["operations"]),
                "changed_symbols": reconcile_plan["changed_symbols"],
                "added_symbols_not_applied": reconcile_plan["added_symbols_not_applied"],
                "deleted_symbols_not_applied": reconcile_plan["deleted_symbols_not_applied"],
                "skipped_symbols": reconcile_plan["skipped_symbols"],
            }
            reports.append(report)
            if reconcile_plan["added_symbols_not_applied"]:
                warnings.append(f"Operation {i} found added symbols that were not applied: {', '.join(reconcile_plan['added_symbols_not_applied'])}")
            if reconcile_plan["deleted_symbols_not_applied"]:
                warnings.append(f"Operation {i} found deleted symbols that were not applied: {', '.join(reconcile_plan['deleted_symbols_not_applied'])}")
        else:
            raise PatchError(f"Unsupported intent operation at operations[{i}]: {op_type!r}")

    return {"operations": operations, "operation_reports": reports, "warnings": warnings}


def normalize_intent(data: dict[str, Any], intent_path: Path) -> dict[str, Any]:
    version = str(data.get("version", "0.9.0"))
    # Do not reject future/older versions yet; just keep the version in metadata. The
    # operation schema is validated below so agents can evolve without hard breaks.
    operations_raw = data.get("operations")
    if operations_raw is None:
        # Single-operation shorthand.
        if "operation" in data or "type" in data:
            operations_raw = [data]
        elif "target" in data and ("source_file" in data or "replacement_file" in data or "source" in data):
            operations_raw = [{**data, "operation": "replace"}]
        else:
            raise PatchError("Intent must contain an operations list or a single replace/reconcile operation.")
    if not isinstance(operations_raw, list) or not operations_raw:
        raise PatchError("Intent operations must be a non-empty list.")
    operations: list[dict[str, Any]] = []
    for i, op in enumerate(operations_raw, start=1):
        if not isinstance(op, dict):
            raise PatchError(f"Intent operation {i} must be a JSON object.")
        operations.append(dict(op))
    return {
        "version": version,
        "name": data.get("name"),
        "reason": data.get("reason"),
        "validate": bool(data.get("validate", True)),
        "operations": operations,
    }


def write_intent_template(path: Path | None = None, *, kind: str = "replace") -> dict[str, Any]:
    if kind == "replace":
        template = {
            "version": "0.9.0",
            "name": "replace-one-symbol",
            "reason": "Explain the change in one sentence.",
            "validate": True,
            "operations": [
                {
                    "operation": "replace",
                    "target": "package.module.symbol",
                    "source_file": "replacement_symbol.py",
                    "expected_hash": "sha256:optional-current-symbol-hash",
                    "allow_name_change": False,
                    "force": False,
                }
            ],
        }
    elif kind == "reconcile":
        template = {
            "version": "0.9.0",
            "name": "reconcile-ai-rewrite",
            "reason": "Mine changed symbols from an AI/full-file rewrite.",
            "validate": True,
            "operations": [
                {
                    "operation": "reconcile",
                    "target_file": "package/module.py",
                    "rewritten_file": "rewritten_module.py",
                    "include_classes": False,
                    "allow_name_change": False,
                    "force": False,
                }
            ],
        }
    elif kind == "mixed":
        template = {
            "version": "0.9.0",
            "name": "multi-operation-transaction",
            "reason": "Apply multiple related symbol changes atomically.",
            "validate": True,
            "operations": [
                {
                    "operation": "replace",
                    "target": "package.module.Class.method",
                    "source_file": "patched_method.py",
                    "expected_hash": "sha256:optional-current-symbol-hash",
                    "allow_name_change": False,
                    "force": False,
                },
                {
                    "operation": "reconcile",
                    "target_file": "package/module.py",
                    "rewritten_file": "rewritten_module.py",
                    "include_classes": False,
                    "allow_name_change": False,
                    "force": False,
                },
            ],
        }
    else:
        raise PatchError("Template kind must be one of: replace, reconcile, mixed")
    if path is not None:
        write_text(path, json.dumps(template, indent=2, sort_keys=True) + "\n")
    return template


def _operation_to_replacement(
    root: Path,
    index: ProjectIndex,
    op: dict[str, Any],
    base_dir: Path,
    *,
    index_in_intent: int,
) -> ReplacementOperation:
    target = _required_str_value(op, ["target", "symbol", "symbol_query"], index_in_intent)
    expected_hash = op.get("expected_hash")
    if expected_hash:
        symbol = resolve_symbol(index, target)
        if symbol.source_hash != str(expected_hash) and not bool(op.get("force", False)):
            raise PatchError(
                f"Intent operation {index_in_intent} expected hash {expected_hash}, "
                f"but current index has {symbol.source_hash} for {symbol.id}. Run `sympatch index` or update the intent."
            )
    source = op.get("source")
    source_file_value = op.get("source_file") or op.get("replacement_file")
    if source is None and not source_file_value:
        raise PatchError(f"Intent operation {index_in_intent} replace requires source_file/replacement_file or inline source.")
    replacement_file = _resolve_external_path(base_dir, str(source_file_value)) if source_file_value else None
    return ReplacementOperation(
        symbol_query=target,
        replacement_file=replacement_file,
        replacement_source=str(source) if source is not None else None,
        force=bool(op.get("force", False)),
        allow_name_change=bool(op.get("allow_name_change", False)),
        label="intent",
    )


def _resolve_external_path(base_dir: Path, raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else (base_dir / path).resolve()


def _required_path_value(op: dict[str, Any], key: str, index_in_intent: int) -> str:
    value = op.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PatchError(f"Intent operation {index_in_intent} requires {key}.")
    return value


def _required_str_value(op: dict[str, Any], keys: list[str], index_in_intent: int) -> str:
    for key in keys:
        value = op.get(key)
        if isinstance(value, str) and value.strip():
            return value
    joined = "/".join(keys)
    raise PatchError(f"Intent operation {index_in_intent} requires {joined}.")
