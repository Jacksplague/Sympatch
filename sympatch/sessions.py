from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .patcher import PatchError, ReplacementOperation, apply_replacements_transaction, preview_replacements_transaction
from .storage import sympatch_dir
from .utils import ensure_dir, read_text, write_text


def sessions_dir(root: Path) -> Path:
    return sympatch_dir(root) / "sessions"


def active_session_path(root: Path) -> Path:
    return sessions_dir(root) / "active"


def start_session(root: Path, name: str | None = None, *, activate: bool = True) -> dict[str, Any]:
    root = root.resolve()
    ensure_dir(sessions_dir(root))
    session_id = _session_id(name)
    path = sessions_dir(root) / f"{session_id}.json"
    session = {
        "id": session_id,
        "name": name,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "operations": [],
    }
    write_text(path, json.dumps(session, indent=2, sort_keys=True))
    if activate:
        write_text(active_session_path(root), session_id + "\n")
    return session


def list_sessions(root: Path) -> list[dict[str, Any]]:
    root = root.resolve()
    if not sessions_dir(root).exists():
        return []
    sessions: list[dict[str, Any]] = []
    for path in sorted(sessions_dir(root).glob("*.json")):
        sessions.append(json.loads(read_text(path)))
    return sessions


def load_session(root: Path, session_id: str | None = None) -> dict[str, Any]:
    root = root.resolve()
    sid = session_id or get_active_session_id(root)
    if not sid:
        raise PatchError("No active session. Run `sympatch session start` or pass a session id.")
    path = sessions_dir(root) / f"{sid}.json"
    if not path.exists():
        raise PatchError(f"Session not found: {sid}")
    return json.loads(read_text(path))


def save_session(root: Path, session: dict[str, Any]) -> None:
    root = root.resolve()
    ensure_dir(sessions_dir(root))
    write_text(sessions_dir(root) / f"{session['id']}.json", json.dumps(session, indent=2, sort_keys=True))


def get_active_session_id(root: Path) -> str | None:
    path = active_session_path(root.resolve())
    if not path.exists():
        return None
    sid = read_text(path).strip()
    return sid or None


def add_replace_operation(
    root: Path,
    symbol: str,
    replacement_file: Path,
    *,
    session_id: str | None = None,
    force: bool = False,
    allow_name_change: bool = False,
) -> dict[str, Any]:
    root = root.resolve()
    session = load_session(root, session_id)
    _assert_pending(session)
    replacement_path = replacement_file if replacement_file.is_absolute() else (Path.cwd() / replacement_file).resolve()
    if not replacement_path.exists():
        raise PatchError(f"Replacement file not found: {replacement_path}")
    op = {
        "type": "replace",
        "symbol": symbol,
        "replacement_file": str(replacement_path),
        "force": force,
        "allow_name_change": allow_name_change,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    # Validate the operation can at least be prepared before queueing it.
    preview_replacements_transaction(root, [_operation_from_dict(op)], operation="session_queue_check", validate=False)
    session["operations"].append(op)
    save_session(root, session)
    return session


def preview_session(root: Path, session_id: str | None = None, *, validate: bool = True) -> dict[str, Any]:
    session = load_session(root, session_id)
    _assert_has_operations(session)
    return preview_replacements_transaction(
        root,
        [_operation_from_dict(op) for op in session["operations"]],
        operation="session_preview",
        validate=validate,
        metadata={"session_id": session["id"], "session_name": session.get("name")},
    )


def commit_session(root: Path, session_id: str | None = None, *, validate: bool = True, quiet: bool = False, run_hooks: bool = False) -> dict[str, Any]:
    root = root.resolve()
    session = load_session(root, session_id)
    _assert_pending(session)
    _assert_has_operations(session)
    record = apply_replacements_transaction(
        root,
        [_operation_from_dict(op) for op in session["operations"]],
        operation="transaction_commit",
        validate=validate,
        quiet=quiet,
        metadata={"session_id": session["id"], "session_name": session.get("name")},
        run_hooks=run_hooks,
    )
    session["status"] = "committed"
    session["committed_at"] = datetime.now(timezone.utc).isoformat()
    session["patch_id"] = record["id"]
    save_session(root, session)
    if get_active_session_id(root) == session["id"]:
        active_session_path(root).unlink(missing_ok=True)
    return record


def abort_session(root: Path, session_id: str | None = None) -> dict[str, Any]:
    root = root.resolve()
    session = load_session(root, session_id)
    _assert_pending(session)
    session["status"] = "aborted"
    session["aborted_at"] = datetime.now(timezone.utc).isoformat()
    save_session(root, session)
    if get_active_session_id(root) == session["id"]:
        active_session_path(root).unlink(missing_ok=True)
    return session


def _operation_from_dict(data: dict[str, Any]) -> ReplacementOperation:
    if data.get("type") != "replace":
        raise PatchError(f"Unsupported session operation type: {data.get('type')}")
    return ReplacementOperation(
        symbol_query=str(data["symbol"]),
        replacement_file=Path(str(data["replacement_file"])),
        force=bool(data.get("force", False)),
        allow_name_change=bool(data.get("allow_name_change", False)),
        label="session",
    )


def _assert_pending(session: dict[str, Any]) -> None:
    if session.get("status") != "pending":
        raise PatchError(f"Session {session.get('id')} is {session.get('status')}, not pending.")


def _assert_has_operations(session: dict[str, Any]) -> None:
    if not session.get("operations"):
        raise PatchError(f"Session {session.get('id')} has no queued operations.")


def _session_id(name: str | None) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = _safe_text(name or "session")
    return f"{stamp}_{suffix}"


def _safe_text(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in text)[:80]
