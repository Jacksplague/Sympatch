from __future__ import annotations

import json
import os
import py_compile
import shlex
import subprocess
from pathlib import Path
from typing import Any

from .utils import discover_python_files, read_text, write_text


DEFAULT_VALIDATION_CONFIG: dict[str, Any] = {
    "validation": {
        "syntax": True,
        "commands": [],
        "timeout_seconds": 120,
        "fail_fast": False,
    }
}

EXAMPLE_CONFIG = """# Sympatch validation hooks.
# Commands run from the project root after syntax validation.
# Keep this deterministic and cheap for agent workflows.

[validation]
syntax = true
timeout_seconds = 120
fail_fast = false
commands = [
  # "python -m compileall .",
  # "python -m pytest tests",
]
"""


def validate_source_text(source: str, filename: str = "<string>") -> tuple[bool, str | None]:
    try:
        compile(source, filename, "exec")
    except SyntaxError as exc:
        return False, f"{filename}: line {exc.lineno}: {exc.msg}"
    return True, None


def validate_file(path: Path) -> tuple[bool, str | None]:
    try:
        py_compile.compile(str(path), doraise=True)
    except py_compile.PyCompileError as exc:
        return False, str(exc)
    return True, None


def validate_project(root: Path, specific: Path | None = None) -> tuple[bool, str]:
    """Backward-compatible syntax-only validation API."""
    report = run_validation(root, specific=specific, run_hooks=False, syntax=True)
    return bool(report["ok"]), str(report["summary"])


def run_validation(
    root: Path,
    *,
    specific: Path | None = None,
    run_hooks: bool = True,
    syntax: bool | None = None,
    extra_commands: list[str] | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Run syntax validation plus optional configured shell hooks.

    Configuration is read from `.sympatch/config.toml` or `.sympatch/config.json`.
    TOML support uses `tomllib` when available and falls back to a tiny parser for
    the simple `[validation]` shape Sympatch writes.
    """
    root = root.resolve()
    config = load_validation_config(root)
    validation_cfg = config.get("validation", {}) if isinstance(config, dict) else {}
    do_syntax = bool(validation_cfg.get("syntax", True)) if syntax is None else bool(syntax)
    commands = list(validation_cfg.get("commands", []) or [])
    if extra_commands:
        commands.extend(extra_commands)
    if not run_hooks:
        commands = []
    timeout = int(timeout_seconds or validation_cfg.get("timeout_seconds", 120) or 120)
    fail_fast = bool(validation_cfg.get("fail_fast", False))

    report: dict[str, Any] = {
        "ok": True,
        "root": str(root),
        "config_path": str(_config_path(root)) if _config_path(root).exists() else None,
        "syntax_enabled": do_syntax,
        "hooks_enabled": bool(commands),
        "syntax": {"ok": True, "files_checked": 0, "errors": []},
        "hooks": [],
        "summary": "",
    }

    if do_syntax:
        syntax_report = _run_syntax_validation(root, specific)
        report["syntax"] = syntax_report
        if not syntax_report["ok"]:
            report["ok"] = False
            if fail_fast:
                report["summary"] = _summarize_validation(report)
                return report

    for command in commands:
        hook = _run_hook(root, str(command), timeout)
        report["hooks"].append(hook)
        if not hook["ok"]:
            report["ok"] = False
            if fail_fast:
                break

    report["summary"] = _summarize_validation(report)
    return report


def write_example_validation_config(root: Path, *, overwrite: bool = False) -> Path:
    path = root.resolve() / ".sympatch" / "config.toml"
    if path.exists() and not overwrite:
        raise FileExistsError(f"Validation config already exists: {path}")
    write_text(path, EXAMPLE_CONFIG)
    return path


def load_validation_config(root: Path) -> dict[str, Any]:
    root = root.resolve()
    json_path = root / ".sympatch" / "config.json"
    toml_path = root / ".sympatch" / "config.toml"
    if json_path.exists():
        try:
            data = json.loads(read_text(json_path))
            return _merge_default_config(data)
        except Exception as exc:
            raise ValueError(f"Could not read validation config {json_path}: {exc}") from exc
    if toml_path.exists():
        try:
            return _merge_default_config(_read_toml(toml_path))
        except Exception as exc:
            raise ValueError(f"Could not read validation config {toml_path}: {exc}") from exc
    return json.loads(json.dumps(DEFAULT_VALIDATION_CONFIG))


def _config_path(root: Path) -> Path:
    json_path = root / ".sympatch" / "config.json"
    return json_path if json_path.exists() else root / ".sympatch" / "config.toml"


def _run_syntax_validation(root: Path, specific: Path | None) -> dict[str, Any]:
    if specific is not None:
        target = specific if specific.is_absolute() else (root / specific).resolve()
    else:
        target = root
    if not target.exists():
        return {"ok": False, "files_checked": 0, "errors": [f"Validation target does not exist: {target}"]}
    files = [target] if target.is_file() else discover_python_files(target)
    errors: list[str] = []
    for path in files:
        ok, msg = validate_file(path)
        if not ok:
            errors.append(msg or str(path))
    return {"ok": not errors, "files_checked": len(files), "errors": errors}


def _run_hook(root: Path, command: str, timeout_seconds: int) -> dict[str, Any]:
    started_env = os.environ.copy()
    started_env.setdefault("PYTHONUTF8", "1")
    try:
        completed = subprocess.run(
            command,
            cwd=str(root),
            shell=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            env=started_env,
        )
        return {
            "command": command,
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "timeout_seconds": timeout_seconds,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "ok": False,
            "returncode": None,
            "timeout_seconds": timeout_seconds,
            "timed_out": True,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or f"Timed out after {timeout_seconds} seconds.",
        }


def _summarize_validation(report: dict[str, Any]) -> str:
    syntax = report.get("syntax", {})
    hooks = report.get("hooks", [])
    parts: list[str] = []
    if report.get("syntax_enabled"):
        parts.append(f"syntax: {'ok' if syntax.get('ok') else 'failed'} ({syntax.get('files_checked', 0)} file(s))")
    if hooks:
        ok_hooks = sum(1 for h in hooks if h.get("ok"))
        parts.append(f"hooks: {ok_hooks}/{len(hooks)} passed")
    elif report.get("hooks_enabled"):
        parts.append("hooks: none")
    if not parts:
        parts.append("nothing to validate")
    prefix = "Validation passed" if report.get("ok") else "Validation failed"
    return prefix + ": " + "; ".join(parts) + "."


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        import tomllib  # type: ignore[attr-defined]

        return tomllib.loads(read_text(path))
    except ModuleNotFoundError:
        return _parse_simple_toml(read_text(path))


def _parse_simple_toml(text: str) -> dict[str, Any]:
    """Tiny TOML subset parser for Sympatch's simple config shape.

    It supports sections, booleans, integers, strings, and one-line/multiline
    arrays of strings. It is deliberately small; use Python 3.11+ tomllib for
    general TOML.
    """
    data: dict[str, Any] = {}
    section: dict[str, Any] = data
    lines = iter(text.splitlines())
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            section = data.setdefault(name, {})
            continue
        if "=" not in line:
            continue
        key, value = [part.strip() for part in line.split("=", 1)]
        if value == "[":
            items: list[str] = []
            for raw_item in lines:
                item = raw_item.split("#", 1)[0].strip().rstrip(",")
                if item == "]":
                    break
                if item:
                    items.append(_strip_quotes(item))
            section[key] = items
        elif value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            if not inner:
                section[key] = []
            else:
                section[key] = [_strip_quotes(part.strip().rstrip(",")) for part in inner.split(",") if part.strip()]
        else:
            section[key] = _parse_scalar(value)
    return data


def _parse_scalar(value: str) -> Any:
    value = value.strip().rstrip(",")
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    try:
        return int(value)
    except ValueError:
        return _strip_quotes(value)


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    return value


def _merge_default_config(data: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(DEFAULT_VALIDATION_CONFIG))
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key].update(value)
            else:
                merged[key] = value
    validation = merged.setdefault("validation", {})
    commands = validation.get("commands", [])
    if isinstance(commands, str):
        validation["commands"] = [commands]
    elif not isinstance(commands, list):
        validation["commands"] = []
    validation["commands"] = [str(cmd) for cmd in validation["commands"]]
    return merged
