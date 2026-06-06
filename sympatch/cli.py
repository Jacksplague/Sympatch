from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .bundle import build_context_bundle, render_bundle_markdown
from .context import analyze_symbol, build_context_slice, resolve_symbol
from .diffutil import read_diff
from .indexer import scan_project
from .impact import analyze_impact
from .intents import run_intent_file, write_intent_template
from .patcher import PatchError, replace_symbol, rollback_record, symbol_source
from .reconcile import reconcile_file
from .sessions import (
    abort_session,
    add_replace_operation,
    commit_session,
    get_active_session_id,
    list_sessions,
    load_session,
    preview_session,
    start_session,
)
from .storage import load_index, read_history, save_index
from .utils import prefix_lines
from .validator import run_validation, validate_project, write_example_validation_config

ROLLBACKABLE_OPERATIONS = {"replace_symbol", "transaction_commit", "reconcile", "intent_apply"}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--help-all" in argv:
        argv.remove("--help-all")
        parser = build_parser(show_aliases=True)
        parser.print_help()
        return 0
    argv = _rewrite_hidden_aliases(argv)
    parser = build_parser(show_aliases=False)
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except (PatchError, FileNotFoundError, ValueError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


def build_parser(*, show_aliases: bool = False) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sympatch",
        description="Symbol-aware patching, reconciliation, and transaction-safe context slicing for Python projects.",
    )
    parser.add_argument("--root", default=".", help="Project root. Defaults to current directory.")
    parser.add_argument("--help-all", action="store_true", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("index", help="Index Python files and write .sympatch/index.json.")
    p.add_argument("path", nargs="?", default=None, help="Project root, subdirectory, or .py file. Defaults to --root.")
    p.add_argument("--json", action="store_true", help="Print machine-readable output.")
    p.set_defaults(func=cmd_index)
    p = sub.add_parser("modules", help="List indexed Python modules/files.")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_modules)

    p = sub.add_parser("symbols", help="List symbols, optionally for one file/module.")
    p.add_argument("file", nargs="?", help="Optional file path such as gui.py, or module name.")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_symbols)

    p = sub.add_parser("tree", help="Print a compact module to symbol tree.")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_tree)

    p = sub.add_parser("find", help="Search indexed symbols by ID, name, signature, docstring, or calls.")
    p.add_argument("query")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_find)
    p = sub.add_parser("show", help="Show exact source for one symbol.")
    p.add_argument("symbol")
    p.add_argument("--lines", action="store_true", help="Prefix source lines with line numbers.")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("card", help="Show compact metadata for one symbol.")
    p.add_argument("symbol")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_card)

    p = sub.add_parser("context", help="Return target symbol plus nearby dependencies/callers.")
    _add_context_args(p, default_depth=1)
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("bundle", help="Export an LLM-ready context bundle for one symbol.")
    p.add_argument("symbol_or_query")
    p.add_argument("--depth", type=int, default=1)
    p.add_argument("--direction", choices=["both", "in", "out", "callers", "dependencies", "callees"], default="both")
    p.add_argument("--format", choices=["markdown", "json"], default="markdown")
    p.add_argument("--out", help="Write bundle to this file instead of stdout.")
    p.add_argument("--no-source", action="store_true", help="Omit source bodies from the bundle.")
    p.add_argument("--json", action="store_true", help="Alias for --format json.")
    p.set_defaults(func=cmd_bundle)

    p = sub.add_parser("analyze", help="Explain call/dependency risk for one symbol.")
    p.add_argument("symbol_or_query")
    p.add_argument("--json", action="store_true", default=True)
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("impact", help="Analyze signature, return-value, caller, import, and dependency impact for one symbol.")
    p.add_argument("symbol_or_query")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_impact)

    p = sub.add_parser("replace", help="Replace one indexed symbol with source from a replacement file.")
    p.add_argument("symbol")
    p.add_argument("replacement_file")
    p.add_argument("--force", action="store_true", help="Allow patch even if source hash differs from index.")
    p.add_argument("--allow-name-change", action="store_true", help="Allow replacement def/class to have a different name.")
    p.add_argument("--no-validate", action="store_true", help="Skip whole-file syntax validation before write.")
    p.add_argument("--run-hooks", action="store_true", help="After applying, run configured validation hooks from .sympatch/config.toml; restore files if hooks fail.")
    p.add_argument("--quiet", action="store_true", help="Do not print the unified diff.")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_replace)

    p = sub.add_parser("reconcile", help="Compare a project file with a rewritten file and patch changed symbols only.")
    p.add_argument("target_file", help="Current project file, relative to --root or absolute inside --root.")
    p.add_argument("rewritten_file", help="AI/full-file rewrite to mine for changed symbols.")
    p.add_argument("--apply", action="store_true", help="Apply the reconciled symbol patch. Default is dry-run preview.")
    p.add_argument("--include-classes", action="store_true", help="Allow whole-class replacements when class AST changed.")
    p.add_argument("--force", action="store_true", help="Allow patch even if source hash differs from index.")
    p.add_argument("--allow-name-change", action="store_true", help="Allow replacement def/class to have a different name.")
    p.add_argument("--no-validate", action="store_true", help="Skip whole-file syntax validation before apply/preview validation.")
    p.add_argument("--run-hooks", action="store_true", help="When --apply is used, run configured validation hooks from .sympatch/config.toml; restore files if hooks fail.")
    p.add_argument("--quiet", action="store_true", help="Do not print unified diff after apply.")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_reconcile)

    session = sub.add_parser("session", help="Stage, preview, validate, commit, or abort transaction-safe patch sessions.")
    session_sub = session.add_subparsers(dest="session_command", required=True)

    p = session_sub.add_parser("start", help="Start a new pending patch session and make it active.")
    p.add_argument("name", nargs="?", default=None)
    p.add_argument("--no-activate", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_session_start)

    p = session_sub.add_parser("list", help="List patch sessions.")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_session_list)

    p = session_sub.add_parser("show", help="Show one session. Defaults to the active session.")
    p.add_argument("session_id", nargs="?", default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_session_show)

    p = session_sub.add_parser("replace", help="Queue a symbol replacement in a pending session.")
    p.add_argument("symbol")
    p.add_argument("replacement_file")
    p.add_argument("--session", dest="session_id", default=None)
    p.add_argument("--force", action="store_true")
    p.add_argument("--allow-name-change", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_session_replace)

    p = session_sub.add_parser("validate", help="Validate the pending transaction without writing files.")
    p.add_argument("session_id", nargs="?", default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_session_validate)

    p = session_sub.add_parser("diff", help="Show the combined pending transaction diff without writing files.")
    p.add_argument("session_id", nargs="?", default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_session_diff)

    p = session_sub.add_parser("commit", help="Validate and atomically apply all queued replacements.")
    p.add_argument("session_id", nargs="?", default=None)
    p.add_argument("--no-validate", action="store_true")
    p.add_argument("--run-hooks", action="store_true", help="Run configured validation hooks from .sympatch/config.toml before finalizing the transaction.")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_session_commit)

    p = session_sub.add_parser("abort", help="Abort a pending session without writing files.")
    p.add_argument("session_id", nargs="?", default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_session_abort)

    intent = sub.add_parser("intent", help="Preview, apply, or create declarative patch intent files.")
    intent_sub = intent.add_subparsers(dest="intent_command", required=True)

    p = intent_sub.add_parser("preview", help="Preview a patch intent transaction without writing files.")
    p.add_argument("intent_file")
    p.add_argument("--no-validate", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_intent_preview)

    p = intent_sub.add_parser("apply", help="Validate and apply a patch intent transaction atomically.")
    p.add_argument("intent_file")
    p.add_argument("--no-validate", action="store_true")
    p.add_argument("--run-hooks", action="store_true", help="Run configured validation hooks from .sympatch/config.toml before finalizing the transaction.")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_intent_apply)

    p = intent_sub.add_parser("template", help="Print or write a patch intent template.")
    p.add_argument("--kind", choices=["replace", "reconcile", "mixed"], default="replace")
    p.add_argument("--out", help="Write template JSON to this file instead of stdout.")
    p.add_argument("--json", action="store_true", default=True)
    p.set_defaults(func=cmd_intent_template)

    p = sub.add_parser("validate", help="Run syntax validation plus optional configured validation hooks.")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--no-hooks", action="store_true", help="Do not run commands from .sympatch/config.toml.")
    p.add_argument("--syntax-only", action="store_true", help="Alias for --no-hooks.")
    p.add_argument("--command", action="append", default=[], help="Extra validation command to run from the project root. May be repeated.")
    p.add_argument("--timeout", type=int, default=None, help="Per-command timeout in seconds.")
    p.add_argument("--init-config", action="store_true", help="Write an example .sympatch/config.toml and exit.")
    p.add_argument("--force", action="store_true", help="Overwrite existing config when used with --init-config.")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("diff", help="Show a patch diff. Defaults to latest rollbackable patch.")
    p.add_argument("patch_id", nargs="?", default="last")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("history", help="List patch history.")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("rollback", help="Rollback a patch. Use 'last' or a patch id.")
    p.add_argument("patch_id", nargs="?", default="last")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_rollback)

    if show_aliases:
        p = sub.add_parser("scan", help="Alias for index.")
        _add_index_args(p)
        p.set_defaults(func=cmd_index)
        p = sub.add_parser("search", help="Alias for find.")
        _add_find_args(p)
        p.set_defaults(func=cmd_find)
        p = sub.add_parser("slice", help="Alias for context with default --depth 2.")
        _add_context_args(p, default_depth=2)
        p.set_defaults(func=cmd_context)
        p = sub.add_parser("apply-intent", help="Alias for intent apply.")
        p.add_argument("intent_file")
        p.add_argument("--no-validate", action="store_true")
        p.add_argument("--run-hooks", action="store_true")
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--json", action="store_true")
        p.set_defaults(func=cmd_intent_apply)

    return parser


def _rewrite_hidden_aliases(argv: list[str]) -> list[str]:
    aliases = {"scan": "index", "search": "find", "slice": "context"}
    rewritten = list(argv)
    skip_next = False
    for i, token in enumerate(rewritten):
        if skip_next:
            skip_next = False
            continue
        if token == "--root":
            skip_next = True
            continue
        if token.startswith("--"):
            continue
        if token == "apply-intent":
            rewritten[i:i + 1] = ["intent", "apply"]
            return rewritten
        if token in aliases:
            old = token
            rewritten[i] = aliases[token]
            if old == "slice" and "--depth" not in rewritten[i + 1 :] and not any(t.startswith("--depth=") for t in rewritten[i + 1 :]):
                rewritten[i + 1:i + 1] = ["--depth", "2"]
            break
        # First non-option is a real command or an invalid command; stop looking.
        break
    return rewritten

def _hidden_alias(sub: argparse._SubParsersAction, name: str, func: Any, show_aliases: bool, help_text: str, add_args: Any) -> None:
    p = sub.add_parser(name, help=help_text if show_aliases else argparse.SUPPRESS)
    add_args(p)
    p.set_defaults(func=func)


def _add_index_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("path", nargs="?", default=None, help="Project root, subdirectory, or .py file. Defaults to --root.")
    p.add_argument("--json", action="store_true", help="Print machine-readable output.")


def _add_find_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("query")
    p.add_argument("--json", action="store_true")


def _add_context_args(p: argparse.ArgumentParser, *, default_depth: int) -> None:
    p.add_argument("symbol_or_query")
    p.add_argument("--depth", type=int, default=default_depth)
    p.add_argument("--direction", choices=["both", "in", "out", "callers", "dependencies", "callees"], default="both")
    p.add_argument("--no-source", action="store_true", help="Omit source bodies from output.")
    p.add_argument("--json", action="store_true", default=True)


def root_from_args(args: argparse.Namespace) -> Path:
    return Path(args.root).resolve()


def print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def cmd_index(args: argparse.Namespace) -> int:
    root = root_from_args(args)
    if args.path is None:
        scan_root = root
        explicit_paths = None
    else:
        given = Path(args.path)
        if not given.is_absolute():
            given = (Path.cwd() / given).resolve()
        if given.is_file():
            scan_root = root
            explicit_paths = [given]
        else:
            scan_root = given.resolve()
            explicit_paths = None
    index = scan_project(scan_root, explicit_paths)
    save_index(scan_root, index)
    counts = index.to_dict()["counts"]
    payload = {"ok": True, "index_path": str(scan_root / ".sympatch" / "index.json"), **counts}
    if args.json:
        print_json(payload)
    else:
        print(f"Indexed {counts['files']} Python file(s), {counts['symbols']} symbol(s).")
        print(f"Calls: {counts['calls']}  Imports: {counts['imports']}")
        print(f"Index: {payload['index_path']}")
    return 0


def cmd_modules(args: argparse.Namespace) -> int:
    index = load_index(root_from_args(args))
    modules = [
        {"file": f.path, "module": f.module, "symbols": len(f.symbols), "parse_error": f.parse_error}
        for f in index.files
    ]
    if args.json:
        print_json({"ok": True, "modules": modules})
    else:
        for m in modules:
            marker = "  [PARSE ERROR]" if m["parse_error"] else ""
            print(f"{m['file']}  ({m['symbols']} symbols){marker}")
    return 0


def cmd_symbols(args: argparse.Namespace) -> int:
    index = load_index(root_from_args(args))
    symbols = index.all_symbols()
    if args.file:
        needle = args.file.replace("\\", "/")
        symbols = [s for s in symbols if s.file == needle or s.module == needle or s.file.endswith("/" + needle)]
    if args.json:
        print_json({"ok": True, "symbols": [s.to_dict() for s in symbols]})
    else:
        for s in symbols:
            print(f"{s.id} [{s.kind}] {s.file}:{s.start_line}-{s.end_line}")
            if s.signature:
                print(f"  {s.signature.splitlines()[0].strip()}")
    return 0


def cmd_tree(args: argparse.Namespace) -> int:
    index = load_index(root_from_args(args))
    if args.json:
        print_json({"ok": True, "tree": [f.to_dict() for f in index.files]})
        return 0
    for f in index.files:
        print(f.path)
        if f.parse_error:
            print(f"  ! parse error: {f.parse_error}")
            continue
        for s in f.symbols:
            indent = "  " * s.qualname.count(".")
            print(f"  {indent}- {s.qualname} [{s.kind}] {s.start_line}-{s.end_line}")
    return 0


def cmd_find(args: argparse.Namespace) -> int:
    index = load_index(root_from_args(args))
    matches = index.search_symbols(args.query)
    payload = {"ok": True, "query": args.query, "matches": [s.to_dict() for s in matches]}
    if args.json:
        print_json(payload)
    else:
        for s in matches:
            print(f"{s.id} [{s.kind}] {s.file}:{s.start_line}-{s.end_line}")
        if not matches:
            return 1
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    root = root_from_args(args)
    index = load_index(root)
    symbol = resolve_symbol(index, args.symbol)
    source = symbol_source(root, symbol)
    if args.json:
        print_json({"ok": True, "symbol": symbol.to_dict(), "source": source})
    elif args.lines:
        print(prefix_lines(source, symbol.start_line))
    else:
        print(source, end="")
    return 0


def cmd_card(args: argparse.Namespace) -> int:
    index = load_index(root_from_args(args))
    symbol = resolve_symbol(index, args.symbol)
    if args.json:
        print_json({"ok": True, "symbol": symbol.to_dict()})
    else:
        print(symbol.id)
        print(f"kind: {symbol.kind}")
        print(f"file: {symbol.file}")
        print(f"lines: {symbol.start_line}-{symbol.end_line}")
        print(f"hash: {symbol.source_hash}")
        print(f"signature: {symbol.signature}")
        if symbol.calls:
            print("calls: " + ", ".join(symbol.calls))
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    root = root_from_args(args)
    index = load_index(root)
    payload = build_context_slice(
        index,
        root,
        args.symbol_or_query,
        depth=max(0, args.depth),
        direction=args.direction,
        include_source=not args.no_source,
    )
    print_json(payload)
    return 0


def cmd_bundle(args: argparse.Namespace) -> int:
    root = root_from_args(args)
    index = load_index(root)
    payload = build_context_bundle(
        index,
        root,
        args.symbol_or_query,
        depth=max(0, args.depth),
        direction=args.direction,
        include_source=not args.no_source,
    )
    fmt = "json" if args.json else args.format
    output = json.dumps(payload, indent=2, sort_keys=True) + "\n" if fmt == "json" else render_bundle_markdown(payload)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output, encoding="utf-8")
        print(str(out_path))
    else:
        print(output, end="")
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    index = load_index(root_from_args(args))
    print_json(analyze_symbol(index, args.symbol_or_query))
    return 0


def cmd_impact(args: argparse.Namespace) -> int:
    root = root_from_args(args)
    index = load_index(root)
    payload = analyze_impact(index, root, args.symbol_or_query)
    if args.json:
        print_json(payload)
    else:
        _print_impact(payload)
    return 0


def cmd_intent_preview(args: argparse.Namespace) -> int:
    record = run_intent_file(
        root_from_args(args),
        Path(args.intent_file),
        apply=False,
        validate=not args.no_validate,
        quiet=True,
    )
    if args.json:
        print_json(record)
    else:
        _print_intent_record(record, applied=False, quiet=False)
    return 0


def cmd_intent_apply(args: argparse.Namespace) -> int:
    record = run_intent_file(
        root_from_args(args),
        Path(args.intent_file),
        apply=True,
        validate=not args.no_validate,
        quiet=args.quiet or args.json,
        run_hooks=getattr(args, "run_hooks", False),
    )
    if args.json:
        print_json(record)
    else:
        _print_intent_record(record, applied=True, quiet=args.quiet)
    return 0


def cmd_intent_template(args: argparse.Namespace) -> int:
    out = Path(args.out).resolve() if args.out else None
    template = write_intent_template(out, kind=args.kind)
    if args.out:
        print(str(out))
    else:
        print_json(template)
    return 0


def cmd_replace(args: argparse.Namespace) -> int:
    root = root_from_args(args)
    record = replace_symbol(
        root,
        args.symbol,
        Path(args.replacement_file),
        force=args.force,
        allow_name_change=args.allow_name_change,
        validate=not args.no_validate,
        quiet=args.quiet or args.json,
        run_hooks=args.run_hooks,
    )
    if args.json:
        print_json({"ok": True, "patch": record})
    else:
        print(f"Patched {record['symbol_id']}")
        print(f"Patch id: {record['id']}")
        print(f"Diff: {record['diff']}")
        if not args.quiet and record.get("diff_text"):
            print(record["diff_text"])
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    root = root_from_args(args)
    index = load_index(root)
    record = reconcile_file(
        root,
        index,
        Path(args.target_file),
        Path(args.rewritten_file),
        apply=args.apply,
        validate=not args.no_validate,
        force=args.force,
        allow_name_change=args.allow_name_change,
        include_classes=args.include_classes,
        quiet=args.quiet or args.json,
        run_hooks=args.run_hooks if args.apply else False,
    )
    if args.json:
        print_json(record)
        return 0
    if not record.get("changed_symbols"):
        print(record.get("message", "No applicable changed symbols found."))
    else:
        action = "Applied" if args.apply else "Previewed"
        print(f"{action} reconcile for {record['target_file']}")
        print("Changed symbols:")
        for name in record["changed_symbols"]:
            print(f"  - {name}")
        if record.get("id"):
            print(f"Patch id: {record['id']}")
        if record.get("diff"):
            print(f"Diff: {record['diff']}")
        if not args.quiet and record.get("diff_text"):
            print(record["diff_text"])
    _print_reconcile_warnings(record)
    return 0


def cmd_session_start(args: argparse.Namespace) -> int:
    session = start_session(root_from_args(args), args.name, activate=not args.no_activate)
    if args.json:
        print_json({"ok": True, "session": session})
    else:
        print(f"Started session: {session['id']}")
        if not args.no_activate:
            print("Active session set.")
    return 0


def cmd_session_list(args: argparse.Namespace) -> int:
    root = root_from_args(args)
    sessions = list_sessions(root)
    active = get_active_session_id(root)
    if args.json:
        print_json({"ok": True, "active": active, "sessions": sessions})
    else:
        if not sessions:
            print("No sessions.")
            return 0
        for s in sessions:
            marker = "*" if s.get("id") == active else " "
            print(f"{marker} {s.get('id')}  {s.get('status')}  ops={len(s.get('operations', []))}  {s.get('name') or ''}")
    return 0


def cmd_session_show(args: argparse.Namespace) -> int:
    session = load_session(root_from_args(args), args.session_id)
    if args.json:
        print_json({"ok": True, "session": session})
    else:
        print(f"Session: {session['id']}")
        print(f"status: {session.get('status')}")
        print(f"name: {session.get('name')}")
        print(f"operations: {len(session.get('operations', []))}")
        for i, op in enumerate(session.get("operations", []), start=1):
            print(f"  {i}. replace {op.get('symbol')} <- {op.get('replacement_file')}")
    return 0


def cmd_session_replace(args: argparse.Namespace) -> int:
    session = add_replace_operation(
        root_from_args(args),
        args.symbol,
        Path(args.replacement_file),
        session_id=args.session_id,
        force=args.force,
        allow_name_change=args.allow_name_change,
    )
    if args.json:
        print_json({"ok": True, "session": session})
    else:
        print(f"Queued replacement in session {session['id']}.")
        print(f"Operations: {len(session.get('operations', []))}")
    return 0


def cmd_session_validate(args: argparse.Namespace) -> int:
    record = preview_session(root_from_args(args), args.session_id, validate=True)
    if args.json:
        print_json({"ok": True, "preview": record})
    else:
        print(f"Session validates: {len(record.get('changes', []))} change(s), {len(record.get('files', []))} file(s).")
    return 0


def cmd_session_diff(args: argparse.Namespace) -> int:
    record = preview_session(root_from_args(args), args.session_id, validate=True)
    if args.json:
        print_json({"ok": True, "preview": record})
    else:
        print(record.get("diff_text", ""), end="")
    return 0


def cmd_session_commit(args: argparse.Namespace) -> int:
    record = commit_session(
        root_from_args(args),
        args.session_id,
        validate=not args.no_validate,
        quiet=args.quiet or args.json,
        run_hooks=args.run_hooks,
    )
    if args.json:
        print_json({"ok": True, "patch": record})
    else:
        print(f"Committed session transaction: {record['id']}")
        print(f"Files: {', '.join(record.get('files', []))}")
        print(f"Diff: {record['diff']}")
        if not args.quiet and record.get("diff_text"):
            print(record["diff_text"])
    return 0


def cmd_session_abort(args: argparse.Namespace) -> int:
    session = abort_session(root_from_args(args), args.session_id)
    if args.json:
        print_json({"ok": True, "session": session})
    else:
        print(f"Aborted session: {session['id']}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    root = root_from_args(args)
    if args.init_config:
        path = write_example_validation_config(root, overwrite=args.force)
        if args.json:
            print_json({"ok": True, "config_path": str(path)})
        else:
            print(f"Wrote validation config: {path}")
        return 0
    target = Path(args.path)
    if not target.is_absolute():
        target = (root / target).resolve()
    report = run_validation(
        root,
        specific=target,
        run_hooks=not (args.no_hooks or args.syntax_only),
        extra_commands=args.command,
        timeout_seconds=args.timeout,
    )
    if args.json:
        print_json(report)
    else:
        print(report["summary"])
        syntax = report.get("syntax", {})
        for err in syntax.get("errors", []):
            print(err)
        for hook in report.get("hooks", []):
            status = "ok" if hook.get("ok") else "failed"
            print(f"hook [{status}] rc={hook.get('returncode')}: {hook.get('command')}")
            if not hook.get("ok"):
                if hook.get("stdout"):
                    print(hook["stdout"].rstrip())
                if hook.get("stderr"):
                    print(hook["stderr"].rstrip())
    return 0 if report.get("ok") else 1


def cmd_diff(args: argparse.Namespace) -> int:
    root = root_from_args(args)
    records = read_history(root)
    if not records:
        raise PatchError("No history.")
    if args.patch_id == "last":
        record = next((r for r in reversed(records) if r.get("operation") in ROLLBACKABLE_OPERATIONS and r.get("diff")), None)
    else:
        record = next((r for r in records if r.get("id") == args.patch_id), None)
    if not record or not record.get("diff"):
        raise PatchError(f"No diff record found for {args.patch_id}")
    diff_text = read_diff(root / record["diff"])
    if args.json:
        print_json({"ok": True, "patch": record, "diff": diff_text})
    else:
        print(diff_text)
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    records = read_history(root_from_args(args))
    if args.json:
        print_json({"ok": True, "history": records})
    else:
        if not records:
            print("No history.")
            return 0
        for r in records:
            files = r.get("files") or ([r.get("file")] if r.get("file") else [])
            subject = r.get("symbol_id") or r.get("rolled_back_patch") or ",".join(files)
            print(f"{r.get('id')}  {r.get('timestamp')}  {r.get('operation')}  {subject}")
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    record = rollback_record(root_from_args(args), args.patch_id)
    if args.json:
        print_json({"ok": True, "rollback": record})
    else:
        print(f"Rollback complete. Rollback id: {record['id']}")
        files = record.get("files") or []
        if files:
            print("Restored: " + ", ".join(files))
    return 0


def _print_intent_record(record: dict[str, Any], *, applied: bool, quiet: bool) -> None:
    if not record.get("operation_count"):
        print(record.get("message", "Intent produced no applicable operations."))
    else:
        action = "Applied" if applied else "Previewed"
        print(f"{action} patch intent: {record.get('operation_count')} operation(s), {len(record.get('files', []))} file(s).")
        if record.get("id"):
            print(f"Patch id: {record['id']}")
        if record.get("diff"):
            print(f"Diff: {record['diff']}")
        if not quiet and record.get("diff_text"):
            print(record["diff_text"])
    warnings = record.get("warnings") or []
    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"  ! {warning}")
    reports = record.get("operation_reports") or []
    if reports:
        print("Operation reports:")
        for report in reports:
            op = report.get("operation")
            if op == "replace":
                print(f"  - replace {report.get('target')} planned={report.get('planned')}")
            elif op == "reconcile":
                changed = ", ".join(report.get("changed_symbols") or []) or "none"
                print(f"  - reconcile {report.get('target_file')} changed=[{changed}]")


def _print_reconcile_warnings(record: dict[str, Any]) -> None:
    added = record.get("added_symbols_not_applied") or []
    deleted = record.get("deleted_symbols_not_applied") or []
    skipped = record.get("skipped_symbols") or []
    if added:
        print("Added symbols not applied:")
        for name in added:
            print(f"  + {name}")
    if deleted:
        print("Deleted symbols not applied:")
        for name in deleted:
            print(f"  - {name}")
    if skipped:
        print("Skipped symbols:")
        for item in skipped:
            print(f"  ! {item.get('qualname')}: {item.get('reason')}")


def _print_impact(payload: dict[str, Any]) -> None:
    target = payload["target"]
    risk = payload["risk"]
    print(target["id"])
    print(f"overall risk: {risk['overall']}")
    print(f"signature risk: {risk['signature']}  return risk: {risk['return_value']}  dependency risk: {risk['dependencies']}")
    print(f"file: {target['file']}:{target['start_line']}-{target['end_line']}")
    callers = payload.get("callers", {})
    print(f"callers: {callers.get('count', 0)} edge(s), {len(callers.get('call_sites', []))} inspected call site(s)")
    for site in callers.get("call_sites", [])[:20]:
        kws = site.get("keyword_args") or []
        kw_text = ", ".join(kws) if kws else "-"
        print(f"  {site.get('file')}:{site.get('line')} raw={site.get('raw_callee')} args={site.get('positional_args')} kwargs={kw_text} return={site.get('return_usage')} caller={site.get('caller')}")
    deps = payload.get("dependencies", {})
    print(f"dependencies: {deps.get('outgoing_count', 0)} outgoing, {len(deps.get('unresolved_or_low_confidence', []))} unresolved/low-confidence")
    print("reasons:")
    for group, reasons in payload.get("reasons", {}).items():
        for reason in reasons:
            print(f"  - {group}: {reason}")
    tests = payload.get("recommended_tests") or []
    if tests:
        print("recommended tests:")
        for test in tests:
            print(f"  - {test}")


if __name__ == "__main__":
    raise SystemExit(main())
