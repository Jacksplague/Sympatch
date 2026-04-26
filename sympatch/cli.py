from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .indexer import scan_project
from .patcher import PatchError, get_symbol, replace_symbol, rollback, symbol_source
from .storage import load_index, read_history, save_index
from .validator import validate_path, validate_project_files


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    try:
        return int(args.func(args, root) or 0)
    except (FileNotFoundError, PatchError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sympatch", description="Symbol-aware patching for Python projects.")
    parser.add_argument("--root", default=".", help="Project root. Default: current directory.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan", help="Index Python files under a project root.")
    p.add_argument("path", nargs="?", default=None, help="Optional root/file/directory to scan. If omitted, --root is used.")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("modules", help="List indexed modules.")
    p.set_defaults(func=cmd_modules)

    p = sub.add_parser("symbols", help="List symbols in one module or all modules.")
    p.add_argument("file", nargs="?", default=None, help="Relative module path, e.g. gui.py")
    p.set_defaults(func=cmd_symbols)

    p = sub.add_parser("tree", help="Print a compact symbol tree.")
    p.set_defaults(func=cmd_tree)

    p = sub.add_parser("search", help="Search indexed symbols by text.")
    p.add_argument("query")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("show", help="Show exact source for a symbol.")
    p.add_argument("symbol_id")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("card", help="Show JSON metadata for a symbol.")
    p.add_argument("symbol_id")
    p.set_defaults(func=cmd_card)

    p = sub.add_parser("replace", help="Replace one symbol with source from a replacement file.")
    p.add_argument("symbol_id")
    p.add_argument("replacement_file")
    p.set_defaults(func=cmd_replace)

    p = sub.add_parser("validate", help="Validate a file or project with Python compilation.")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("diff", help="Show patch diff from history. Defaults to latest replace patch.")
    p.add_argument("patch_id", nargs="?", default="last")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("history", help="Show patch history.")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("rollback", help="Rollback a patch. Use 'last' or a patch id.")
    p.add_argument("patch_id", nargs="?", default="last")
    p.set_defaults(func=cmd_rollback)

    return parser


def cmd_scan(args: argparse.Namespace, root: Path) -> int:
    if args.path is None:
        scan_root = root
        specific = None
    else:
        given = Path(args.path)
        if not given.is_absolute():
            given = Path.cwd() / given
        if given.is_dir():
            scan_root = given.resolve()
            specific = None
        else:
            scan_root = root
            specific = given.resolve()
    index = scan_project(scan_root, specific=specific)
    save_index(scan_root, index)
    symbol_count = sum(len(m.symbols) for m in index.modules)
    error_count = sum(1 for m in index.modules if m.parse_error)
    print(f"Indexed {len(index.modules)} Python file(s), {symbol_count} symbol(s).")
    print(f"Index: {scan_root / '.sympatch' / 'index.json'}")
    if error_count:
        print(f"WARNING: {error_count} module(s) had parse errors.", file=sys.stderr)
    return 0


def cmd_modules(args: argparse.Namespace, root: Path) -> int:
    index = load_index(root)
    for module in index.modules:
        suffix = "  [PARSE ERROR]" if module.parse_error else ""
        print(f"{module.file}  ({len(module.symbols)} symbols){suffix}")
    return 0


def cmd_symbols(args: argparse.Namespace, root: Path) -> int:
    index = load_index(root)
    modules = index.modules
    if args.file:
        normalized = Path(args.file).as_posix()
        modules = [m for m in modules if m.file == normalized]
        if not modules:
            raise PatchError(f"No indexed module found for: {normalized}")
    for module in modules:
        print(module.file)
        for symbol in module.symbols:
            print(f"  {symbol.id}  [{symbol.kind}] L{symbol.start_line}-L{symbol.end_line}")
    return 0


def cmd_tree(args: argparse.Namespace, root: Path) -> int:
    index = load_index(root)
    for module in index.modules:
        print(module.file)
        for symbol in module.symbols:
            indent = "  " * symbol.qualname.count(".")
            print(f"  {indent}{symbol.qualname}  [{symbol.kind}] L{symbol.start_line}-L{symbol.end_line}")
    return 0


def cmd_search(args: argparse.Namespace, root: Path) -> int:
    index = load_index(root)
    query = args.query.lower()
    count = 0
    for module in index.modules:
        for symbol in module.symbols:
            blob = "\n".join([symbol.id, symbol.kind, symbol.name, symbol.qualname, symbol.signature]).lower()
            if query in blob:
                print(f"{symbol.id}  [{symbol.kind}] L{symbol.start_line}-L{symbol.end_line}")
                count += 1
    if count == 0:
        print("No matching symbols.")
    return 0


def cmd_show(args: argparse.Namespace, root: Path) -> int:
    index = load_index(root)
    symbol = get_symbol(index, args.symbol_id)
    print(symbol_source(root, symbol))
    return 0


def cmd_card(args: argparse.Namespace, root: Path) -> int:
    index = load_index(root)
    symbol = get_symbol(index, args.symbol_id)
    print(json.dumps(symbol.to_dict(), indent=2, sort_keys=True))
    return 0


def cmd_replace(args: argparse.Namespace, root: Path) -> int:
    record = replace_symbol(root, args.symbol_id, Path(args.replacement_file))
    print(f"Patched {record['symbol_id']}")
    print(f"Patch id: {record['id']}")
    print(f"Diff: {record['diff']}")
    return 0


def cmd_validate(args: argparse.Namespace, root: Path) -> int:
    if args.path == ".":
        ok, msg = validate_project_files(root)
    else:
        ok, msg = validate_path(root, args.path)
    print(msg)
    return 0 if ok else 1


def cmd_diff(args: argparse.Namespace, root: Path) -> int:
    records = read_history(root)
    if not records:
        print("No history.")
        return 0
    record = None
    if args.patch_id == "last":
        for candidate in reversed(records):
            if candidate.get("operation") == "replace_symbol" and candidate.get("diff"):
                record = candidate
                break
    else:
        for candidate in records:
            if candidate.get("id") == args.patch_id:
                record = candidate
                break
    if record is None or not record.get("diff"):
        raise PatchError("No matching diff record found.")
    diff_path = root / record["diff"]
    if not diff_path.exists():
        raise PatchError(f"Diff file missing: {diff_path}")
    print(diff_path.read_text(encoding="utf-8"))
    return 0


def cmd_history(args: argparse.Namespace, root: Path) -> int:
    records = read_history(root)
    if not records:
        print("No history.")
        return 0
    for record in records:
        op = record.get("operation", "?")
        patch_id = record.get("id", "?")
        ts = record.get("timestamp", "?")
        file = record.get("file", "")
        sym = record.get("symbol_id", record.get("rolled_back_patch", ""))
        print(f"{patch_id}  {ts}  {op}  {file}  {sym}")
    return 0


def cmd_rollback(args: argparse.Namespace, root: Path) -> int:
    record = rollback(root, args.patch_id)
    print(f"Rollback complete. Rollback id: {record['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
