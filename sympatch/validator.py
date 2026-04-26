from __future__ import annotations

import compileall
import py_compile
from pathlib import Path

from .utils import iter_python_files


class ValidationError(RuntimeError):
    pass


def validate_path(root: Path, target: Path | str = ".") -> tuple[bool, str]:
    root = root.resolve()
    path = Path(target)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()

    try:
        if path.is_file():
            py_compile.compile(str(path), doraise=True)
            return True, f"OK: {path}"
        if path.is_dir():
            ok = compileall.compile_dir(str(path), quiet=1, maxlevels=20)
            if ok:
                return True, f"OK: compiled Python files under {path}"
            return False, f"compileall failed under {path}"
        return False, f"Path does not exist: {path}"
    except py_compile.PyCompileError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"Validation failed: {exc}"


def validate_project_files(root: Path) -> tuple[bool, str]:
    errors: list[str] = []
    for path in iter_python_files(root):
        ok, msg = validate_path(root, path)
        if not ok:
            errors.append(msg)
    if errors:
        return False, "\n".join(errors)
    return True, f"OK: validated Python files under {root.resolve()}"
