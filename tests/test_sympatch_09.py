#!/usr/bin/env python3
"""
End-to-end verification suite for Sympatch 0.9.x.

This script creates a disposable, controlled Python project, runs the Sympatch CLI
against it, and prints PASS/FAIL for each major public command and 0.9 feature.
It does not touch your real project tree.

Usage:
    python test_sympatch_09.py
    python test_sympatch_09.py --keep
    python test_sympatch_09.py --workdir C:/tmp/sympatch-test

Requirements:
    Sympatch 0.9.x must be importable in the Python environment running this script.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

EXPECTED_MAJOR_MINOR = "0.9"
SYMPATCH_CLI_SNIPPET = "import sys; from sympatch.cli import main; sys.exit(main())"

SUBJECT_SOURCE = '''\
CONSTANT = 10


def helper(value: int) -> int:
    """Return a deterministic adjusted integer."""
    return value + CONSTANT


def consume(value: int) -> str:
    return f"consume:{helper(value)}"


class Worker:
    def __init__(self, prefix: str = "W") -> None:
        self.prefix = prefix

    def format_output(self, value: int) -> str:
        return f"{self.prefix}:{value}"

    def run(self, value: int) -> str:
        adjusted = helper(value)
        return self.format_output(adjusted)


def top_entry(value: int) -> str:
    worker = Worker(prefix="T")
    result = helper(value)
    print(consume(result))
    return worker.run(result)
'''

CALLER_SOURCE = '''\
from subject import helper, top_entry
import subject


def use_helper_positional() -> int:
    return helper(3)


def use_helper_keyword() -> int:
    return helper(value=4)


def ignore_helper() -> None:
    helper(5)


def use_top_entry() -> str:
    return top_entry(2)


def use_module_call() -> str:
    return subject.consume(6)
'''

SMOKE_CHECK_SOURCE = '''\
import caller

assert caller.use_helper_positional() == 13
assert caller.use_helper_keyword() == 14
assert caller.ignore_helper() is None
assert caller.use_module_call() == "consume:16"
print("sympatch smoke ok")
'''

FAIL_CHECK_SOURCE = '''\
raise SystemExit("intentional failing validation hook")
'''

MUTATION_MARKERS = [
    "CONSUME:",
    "HOOK:",
    "BROKEN_HOOK:",
    "INTENT:",
    "ALIAS:",
    "SESSION:",
    "SESSION_TOP:",
    "+ CONSTANT + 1",
]


@dataclass
class CmdResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


@dataclass
class TestResult:
    name: str
    passed: bool
    details: str = ""


class SympatchHarness:
    def __init__(self, root: Path, replacement_dir: Path, verbose: bool = False) -> None:
        self.root = root.resolve()
        self.replacement_dir = replacement_dir.resolve()
        self.verbose = verbose

    @property
    def subject_path(self) -> Path:
        return self.root / "subject.py"

    @property
    def config_json(self) -> Path:
        return self.root / ".sympatch" / "config.json"

    def run(self, args: list[str], *, expect: int | tuple[int, ...] = 0, parse_json: bool = False) -> CmdResult | dict[str, Any]:
        expected = (expect,) if isinstance(expect, int) else tuple(expect)
        cmd = [sys.executable, "-c", SYMPATCH_CLI_SNIPPET, "--root", str(self.root), *args]
        env = os.environ.copy()
        env.setdefault("PYTHONUTF8", "1")
        completed = subprocess.run(
            cmd,
            cwd=str(self.root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        result = CmdResult(args=args, returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)
        if self.verbose:
            print("\n$", " ".join(cmd))
            print("rc=", completed.returncode)
            if completed.stdout:
                print("stdout:\n" + completed.stdout)
            if completed.stderr:
                print("stderr:\n" + completed.stderr)
        if completed.returncode not in expected:
            raise AssertionError(
                f"command returned {completed.returncode}, expected {expected}: sympatch {' '.join(args)}\n"
                f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            )
        if parse_json:
            text = completed.stdout.strip()
            if not text:
                raise AssertionError(f"command produced no JSON output: sympatch {' '.join(args)}")
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise AssertionError(
                    f"command did not produce valid JSON: sympatch {' '.join(args)}\n"
                    f"JSON error: {exc}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
                ) from exc
        return result

    def write_file(self, relative_or_path: str | Path, text: str) -> Path:
        path = Path(relative_or_path)
        if not path.is_absolute():
            path = self.replacement_dir / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).strip("\n") + "\n", encoding="utf-8")
        return path

    def write_passing_hook_config(self) -> None:
        self.config_json.parent.mkdir(parents=True, exist_ok=True)
        command = (
            f'"{sys.executable}" -c '
            '"import caller; '
            'assert caller.use_helper_positional() == 13; '
            'assert caller.use_helper_keyword() == 14; '
            'assert caller.ignore_helper() is None; '
            'print(\'sympatch smoke ok\')"'
        )
        self.config_json.write_text(
            json.dumps(
                {
                    "validation": {
                        "syntax": True,
                        "commands": [command],
                        "timeout_seconds": 30,
                        "fail_fast": False,
                    }
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def write_failing_hook_config(self) -> None:
        self.config_json.parent.mkdir(parents=True, exist_ok=True)
        command = f'"{sys.executable}" "tests/fail_check.py"'
        self.config_json.write_text(
            json.dumps(
                {
                    "validation": {
                        "syntax": True,
                        "commands": [command],
                        "timeout_seconds": 30,
                        "fail_fast": False,
                    }
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def index(self) -> dict[str, Any]:
        return self.run(["index", "--json"], parse_json=True)  # type: ignore[return-value]

    def rollback_if_mutated(self) -> None:
        if not self.subject_path.exists():
            return
        text = self.subject_path.read_text(encoding="utf-8")
        if any(marker in text for marker in MUTATION_MARKERS):
            self.run(["rollback", "last", "--json"], expect=(0, 2), parse_json=False)
            self.index()

    def assert_baseline_subject(self) -> None:
        text = self.subject_path.read_text(encoding="utf-8")
        assert "return value + CONSTANT\n" in text, "baseline helper body was not restored"
        for marker in MUTATION_MARKERS:
            assert marker not in text, f"unexpected mutation marker still present: {marker}"


def create_controlled_project(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "subject.py").write_text(SUBJECT_SOURCE, encoding="utf-8")
    (root / "caller.py").write_text(CALLER_SOURCE, encoding="utf-8")
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "smoke_check.py").write_text(SMOKE_CHECK_SOURCE, encoding="utf-8")
    (root / "tests" / "fail_check.py").write_text(FAIL_CHECK_SOURCE, encoding="utf-8")


def require_ok(payload: dict[str, Any], label: str) -> None:
    assert payload.get("ok") is True, f"{label} returned ok={payload.get('ok')}: {payload}"


def symbol_ids(payload: dict[str, Any], key: str = "symbols") -> set[str]:
    return {item["id"] for item in payload.get(key, [])}


def make_tests(h: SympatchHarness) -> list[tuple[str, Callable[[], None]]]:
    def test_version() -> None:
        import sympatch  # type: ignore

        version = getattr(sympatch, "__version__", "unknown")
        assert version.startswith(EXPECTED_MAJOR_MINOR + "."), f"expected Sympatch {EXPECTED_MAJOR_MINOR}.x, got {version!r}"

    def test_index() -> None:
        payload = h.index()
        require_ok(payload, "index")
        assert payload["files"] >= 3, payload
        assert payload["symbols"] >= 12, payload
        assert payload["calls"] >= 10, payload
        assert Path(payload["index_path"]).exists(), payload

    def test_modules() -> None:
        payload = h.run(["modules", "--json"], parse_json=True)  # type: ignore[assignment]
        require_ok(payload, "modules")
        files = {m["file"] for m in payload["modules"]}
        assert {"subject.py", "caller.py", "tests/smoke_check.py"}.issubset(files), files

    def test_symbols() -> None:
        payload = h.run(["symbols", "--json"], parse_json=True)  # type: ignore[assignment]
        require_ok(payload, "symbols")
        ids = symbol_ids(payload)
        expected = {"subject.helper", "subject.consume", "subject.Worker.run", "subject.top_entry", "caller.use_helper_keyword"}
        assert expected.issubset(ids), ids
        filtered = h.run(["symbols", "subject.py", "--json"], parse_json=True)  # type: ignore[assignment]
        filtered_ids = symbol_ids(filtered)
        assert "subject.helper" in filtered_ids and "caller.use_helper_keyword" not in filtered_ids, filtered_ids

    def test_tree() -> None:
        result = h.run(["tree"])
        assert isinstance(result, CmdResult)
        assert "subject.py" in result.stdout, result.stdout
        assert "Worker.run" in result.stdout, result.stdout

    def test_find() -> None:
        payload = h.run(["find", "helper", "--json"], parse_json=True)  # type: ignore[assignment]
        require_ok(payload, "find")
        ids = {m["id"] for m in payload["matches"]}
        assert "subject.helper" in ids, ids

    def test_show() -> None:
        result = h.run(["show", "subject.helper", "--lines"])
        assert isinstance(result, CmdResult)
        assert "def helper" in result.stdout, result.stdout
        assert ":" in result.stdout.splitlines()[0], result.stdout
        payload = h.run(["show", "subject.helper", "--json"], parse_json=True)  # type: ignore[assignment]
        require_ok(payload, "show json")
        assert "return value + CONSTANT" in payload["source"], payload

    def test_card() -> None:
        payload = h.run(["card", "subject.helper", "--json"], parse_json=True)  # type: ignore[assignment]
        require_ok(payload, "card")
        symbol = payload["symbol"]
        assert symbol["id"] == "subject.helper", symbol
        assert symbol["source_hash"].startswith("sha256:"), symbol

    def test_context() -> None:
        payload = h.run(["context", "subject.helper", "--depth", "1", "--direction", "both", "--json"], parse_json=True)  # type: ignore[assignment]
        require_ok(payload, "context")
        ids = symbol_ids(payload)
        assert "subject.helper" in ids, ids
        assert "caller.use_helper_positional" in ids or "subject.consume" in ids, ids
        assert payload["slice_hashes"]["subject.helper"].startswith("sha256:"), payload

    def test_bundle() -> None:
        md_out = h.replacement_dir / "helper.bundle.md"
        json_out = h.replacement_dir / "helper.bundle.json"
        h.run(["bundle", "subject.helper", "--format", "markdown", "--out", str(md_out)])
        assert md_out.exists(), md_out
        md = md_out.read_text(encoding="utf-8")
        assert "Sympatch LLM Context Bundle" in md and "Patch intent template" in md, md[:500]
        h.run(["bundle", "subject.helper", "--json", "--out", str(json_out)])
        data = json.loads(json_out.read_text(encoding="utf-8"))
        require_ok(data, "bundle json file")
        assert data["target"]["id"] == "subject.helper", data["target"]
        assert data["patch_intent_template"]["operations"][0]["target"] == "subject.helper", data

    def test_analyze() -> None:
        payload = h.run(["analyze", "subject.helper"], parse_json=True)  # type: ignore[assignment]
        require_ok(payload, "analyze")
        assert payload["target"]["id"] == "subject.helper", payload
        assert payload["incoming_calls"], payload
        assert payload["recommended_tests"], payload

    def test_impact() -> None:
        payload = h.run(["impact", "subject.helper", "--json"], parse_json=True)  # type: ignore[assignment]
        require_ok(payload, "impact")
        assert payload["target"]["id"] == "subject.helper", payload
        assert payload["callers"]["count"] >= 4, payload["callers"]
        usages = payload["callers"]["call_sites"]
        assert any(u["keyword_args"] == ["value"] for u in usages), usages
        assert any(u["positional_args"] >= 1 for u in usages), usages
        assert any(u["return_usage"] == "ignored" for u in usages), usages
        assert payload["risk"]["overall"] in {"low", "medium", "high", "unknown"}, payload["risk"]
        assert payload["recommended_tests"], payload

    def test_validate_syntax_and_hooks() -> None:
        payload = h.run(["validate", "--syntax-only", "--json"], parse_json=True)  # type: ignore[assignment]
        require_ok(payload, "validate syntax-only")
        assert payload["syntax"]["files_checked"] >= 3, payload
        init = h.run(["validate", "--init-config", "--force", "--json"], parse_json=True)  # type: ignore[assignment]
        require_ok(init, "validate init-config")
        assert Path(init["config_path"]).exists(), init
        h.write_passing_hook_config()
        hooks = h.run(["validate", "--json"], parse_json=True)  # type: ignore[assignment]
        require_ok(hooks, "validate hooks")
        assert hooks["hooks"] and hooks["hooks"][0]["ok"] is True, hooks

    def test_replace_diff_history_rollback() -> None:
        patch = h.write_file(
            "consume_replacement.py",
            '''
            def consume(value: int) -> str:
                return f"CONSUME:{helper(value)}"
            ''',
        )
        try:
            payload = h.run(["replace", "subject.consume", str(patch), "--quiet", "--json"], parse_json=True)  # type: ignore[assignment]
            require_ok(payload, "replace")
            patch_record = payload["patch"]
            assert patch_record["operation"] == "replace_symbol", patch_record
            assert "CONSUME:" in h.subject_path.read_text(encoding="utf-8")
            diff = h.run(["diff", "--json"], parse_json=True)  # type: ignore[assignment]
            require_ok(diff, "diff")
            assert "CONSUME:" in diff["diff"], diff
            history = h.run(["history", "--json"], parse_json=True)  # type: ignore[assignment]
            require_ok(history, "history")
            assert any(r.get("operation") == "replace_symbol" for r in history["history"]), history
            rollback = h.run(["rollback", "last", "--json"], parse_json=True)  # type: ignore[assignment]
            require_ok(rollback, "rollback")
            h.assert_baseline_subject()
        finally:
            h.rollback_if_mutated()

    def test_replace_run_hooks_success() -> None:
        h.write_passing_hook_config()
        patch = h.write_file(
            "consume_hook_replacement.py",
            '''
            def consume(value: int) -> str:
                return f"HOOK:{helper(value)}"
            ''',
        )
        try:
            payload = h.run(["replace", "subject.consume", str(patch), "--run-hooks", "--quiet", "--json"], parse_json=True)  # type: ignore[assignment]
            require_ok(payload, "replace --run-hooks")
            patch_record = payload["patch"]
            assert patch_record.get("validation_report", {}).get("ok") is True, patch_record
            assert "HOOK:" in h.subject_path.read_text(encoding="utf-8")
            h.run(["rollback", "last", "--json"], parse_json=True)
            h.assert_baseline_subject()
        finally:
            h.rollback_if_mutated()
            h.write_passing_hook_config()

    def test_run_hooks_failure_restores_files() -> None:
        h.write_failing_hook_config()
        patch = h.write_file(
            "consume_broken_hook_replacement.py",
            '''
            def consume(value: int) -> str:
                return f"BROKEN_HOOK:{helper(value)}"
            ''',
        )
        try:
            payload = h.run(["replace", "subject.consume", str(patch), "--run-hooks", "--quiet", "--json"], expect=2, parse_json=True)  # type: ignore[assignment]
            assert payload.get("ok") is False, payload
            assert "Validation hooks failed" in payload.get("error", ""), payload
            assert "BROKEN_HOOK:" not in h.subject_path.read_text(encoding="utf-8"), "failed hook left patched text in place"
            h.assert_baseline_subject()
        finally:
            h.rollback_if_mutated()
            h.write_passing_hook_config()

    def test_reconcile_preview_apply_rollback() -> None:
        rewritten = h.replacement_dir / "subject_rewritten.py"
        text = h.subject_path.read_text(encoding="utf-8")
        text = text.replace("return value + CONSTANT\n", "return value + CONSTANT + 1\n")
        text += '\n\ndef brand_new_feature() -> str:\n    return "new"\n'
        rewritten.write_text(text, encoding="utf-8")
        try:
            preview = h.run(["reconcile", "subject.py", str(rewritten), "--json"], parse_json=True)  # type: ignore[assignment]
            require_ok(preview, "reconcile preview")
            assert preview["applied"] is False, preview
            assert "helper" in preview["changed_symbols"], preview
            assert "brand_new_feature" in preview["added_symbols_not_applied"], preview
            applied = h.run(["reconcile", "subject.py", str(rewritten), "--apply", "--quiet", "--json"], parse_json=True)  # type: ignore[assignment]
            require_ok(applied, "reconcile apply")
            assert applied["applied"] is True, applied
            subject = h.subject_path.read_text(encoding="utf-8")
            assert "+ CONSTANT + 1" in subject, subject
            assert "brand_new_feature" not in subject, subject
            h.run(["rollback", "last", "--json"], parse_json=True)
            h.assert_baseline_subject()
        finally:
            h.rollback_if_mutated()

    def test_session_transaction_commit_and_abort() -> None:
        consume_patch = h.write_file(
            "consume_session_replacement.py",
            '''
            def consume(value: int) -> str:
                return f"SESSION:{helper(value)}"
            ''',
        )
        top_entry_patch = h.write_file(
            "top_entry_session_replacement.py",
            '''
            def top_entry(value: int) -> str:
                worker = Worker(prefix="SESSION_TOP")
                result = helper(value)
                return worker.run(result)
            ''',
        )
        try:
            started = h.run(["session", "start", "transaction-test", "--json"], parse_json=True)  # type: ignore[assignment]
            require_ok(started, "session start")
            sid = started["session"]["id"] if "session" in started else started["id"]
            sessions = h.run(["session", "list", "--json"], parse_json=True)  # type: ignore[assignment]
            assert any(s["id"] == sid for s in sessions["sessions"]), sessions
            shown = h.run(["session", "show", sid, "--json"], parse_json=True)  # type: ignore[assignment]
            assert shown["session"]["id"] == sid if "session" in shown else shown["id"] == sid
            queued1 = h.run(["session", "replace", "subject.consume", str(consume_patch), "--json"], parse_json=True)  # type: ignore[assignment]
            require_ok(queued1, "session replace 1")
            queued2 = h.run(["session", "replace", "subject.top_entry", str(top_entry_patch), "--json"], parse_json=True)  # type: ignore[assignment]
            require_ok(queued2, "session replace 2")
            valid = h.run(["session", "validate", sid, "--json"], parse_json=True)  # type: ignore[assignment]
            preview = valid["preview"]
            assert preview["dry_run"] is True and len(preview["changes"]) == 2, preview
            diff = h.run(["session", "diff", sid, "--json"], parse_json=True)  # type: ignore[assignment]
            diff_preview = diff["preview"]
            assert "SESSION:" in diff_preview["diff_text"] and "SESSION_TOP" in diff_preview["diff_text"], diff_preview
            committed = h.run(["session", "commit", sid, "--quiet", "--json"], parse_json=True)  # type: ignore[assignment]
            require_ok(committed, "session commit")
            patch_record = committed["patch"]
            assert patch_record["operation"] == "transaction_commit", patch_record
            subject = h.subject_path.read_text(encoding="utf-8")
            assert "SESSION:" in subject and "SESSION_TOP" in subject, subject
            h.run(["rollback", "last", "--json"], parse_json=True)
            h.assert_baseline_subject()

            abort_started = h.run(["session", "start", "abort-test", "--json"], parse_json=True)  # type: ignore[assignment]
            abort_sid = abort_started["session"]["id"] if "session" in abort_started else abort_started["id"]
            aborted = h.run(["session", "abort", abort_sid, "--json"], parse_json=True)  # type: ignore[assignment]
            status = aborted.get("session", aborted).get("status")
            assert status == "aborted", aborted
        finally:
            h.rollback_if_mutated()

    def test_intent_template_preview_apply_and_alias() -> None:
        template_path = h.replacement_dir / "template_replace.json"
        h.run(["intent", "template", "--kind", "replace", "--out", str(template_path)])
        template = json.loads(template_path.read_text(encoding="utf-8"))
        assert template["operations"][0]["operation"] == "replace", template
        for kind in ("reconcile", "mixed"):
            extra_template_path = h.replacement_dir / f"template_{kind}.json"
            h.run(["intent", "template", "--kind", kind, "--out", str(extra_template_path)])
            extra_template = json.loads(extra_template_path.read_text(encoding="utf-8"))
            assert extra_template["operations"], extra_template

        intent_path = h.replacement_dir / "intent_replace.json"
        intent_path.write_text(
            json.dumps(
                {
                    "version": "0.9.0",
                    "name": "intent-replace-consume",
                    "reason": "Verify patch intent apply.",
                    "validate": True,
                    "operations": [
                        {
                            "operation": "replace",
                            "target": "subject.consume",
                            "source": 'def consume(value: int) -> str:\n    return f"INTENT:{helper(value)}"\n',
                        }
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        try:
            preview = h.run(["intent", "preview", str(intent_path), "--json"], parse_json=True)  # type: ignore[assignment]
            require_ok(preview, "intent preview")
            assert preview["operation_count"] == 1, preview
            applied = h.run(["intent", "apply", str(intent_path), "--run-hooks", "--quiet", "--json"], parse_json=True)  # type: ignore[assignment]
            require_ok(applied, "intent apply")
            assert applied["operation"] == "intent_apply", applied
            assert applied.get("validation_report", {}).get("ok") is True, applied
            assert "INTENT:" in h.subject_path.read_text(encoding="utf-8")
            h.run(["rollback", "last", "--json"], parse_json=True)
            h.assert_baseline_subject()

            reconcile_rewrite = h.replacement_dir / "intent_subject_rewrite.py"
            rewrite_text = h.subject_path.read_text(encoding="utf-8").replace(
                "return value + CONSTANT\n", "return value + CONSTANT + 1\n"
            )
            reconcile_rewrite.write_text(rewrite_text, encoding="utf-8")
            reconcile_intent = h.replacement_dir / "intent_reconcile.json"
            reconcile_intent.write_text(
                json.dumps(
                    {
                        "version": "0.9.0",
                        "name": "intent-reconcile-helper",
                        "validate": True,
                        "operations": [
                            {
                                "operation": "reconcile",
                                "target_file": "subject.py",
                                "rewritten_file": str(reconcile_rewrite),
                                "include_classes": False,
                            }
                        ],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            reconcile_preview = h.run(["intent", "preview", str(reconcile_intent), "--json"], parse_json=True)  # type: ignore[assignment]
            require_ok(reconcile_preview, "intent reconcile preview")
            assert reconcile_preview["operation_count"] == 1, reconcile_preview
            assert reconcile_preview["operation_reports"][0]["operation"] == "reconcile", reconcile_preview
            reconcile_applied = h.run(["intent", "apply", str(reconcile_intent), "--quiet", "--json"], parse_json=True)  # type: ignore[assignment]
            require_ok(reconcile_applied, "intent reconcile apply")
            assert "+ CONSTANT + 1" in h.subject_path.read_text(encoding="utf-8")
            h.run(["rollback", "last", "--json"], parse_json=True)
            h.assert_baseline_subject()

            alias_intent = h.replacement_dir / "intent_alias.json"
            alias_intent.write_text(
                json.dumps(
                    {
                        "version": "0.9.0",
                        "name": "alias-intent",
                        "validate": True,
                        "operation": "replace",
                        "target": "subject.consume",
                        "source": 'def consume(value: int) -> str:\n    return f"ALIAS:{helper(value)}"\n',
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            alias_applied = h.run(["apply-intent", str(alias_intent), "--quiet", "--json"], parse_json=True)  # type: ignore[assignment]
            require_ok(alias_applied, "apply-intent alias")
            assert "ALIAS:" in h.subject_path.read_text(encoding="utf-8")
            h.run(["rollback", "last", "--json"], parse_json=True)
            h.assert_baseline_subject()
        finally:
            h.rollback_if_mutated()

    def test_public_cli_cleanup_and_hidden_aliases() -> None:
        normal_help = h.run(["--help"])
        assert isinstance(normal_help, CmdResult)
        assert "scan" not in normal_help.stdout, normal_help.stdout
        assert "slice" not in normal_help.stdout, normal_help.stdout
        all_help = h.run(["--help-all"])
        assert isinstance(all_help, CmdResult)
        assert "scan" in all_help.stdout and "apply-intent" in all_help.stdout, all_help.stdout
        scan = h.run(["scan", "--json"], parse_json=True)  # type: ignore[assignment]
        require_ok(scan, "scan alias")
        search = h.run(["search", "helper", "--json"], parse_json=True)  # type: ignore[assignment]
        require_ok(search, "search alias")
        assert any(m["id"] == "subject.helper" for m in search["matches"]), search
        sliced = h.run(["slice", "subject.helper", "--json"], parse_json=True)  # type: ignore[assignment]
        require_ok(sliced, "slice alias")
        assert sliced["target"] == "subject.helper", sliced

    return [
        ("import/version", test_version),
        ("index", test_index),
        ("modules", test_modules),
        ("symbols", test_symbols),
        ("tree", test_tree),
        ("find", test_find),
        ("show", test_show),
        ("card", test_card),
        ("context", test_context),
        ("bundle", test_bundle),
        ("analyze", test_analyze),
        ("impact", test_impact),
        ("validate syntax/config/hooks", test_validate_syntax_and_hooks),
        ("replace/diff/history/rollback", test_replace_diff_history_rollback),
        ("replace --run-hooks success", test_replace_run_hooks_success),
        ("replace --run-hooks failure restore", test_run_hooks_failure_restores_files),
        ("reconcile preview/apply/rollback", test_reconcile_preview_apply_rollback),
        ("session transaction/abort", test_session_transaction_commit_and_abort),
        ("intent template/preview/apply/alias", test_intent_template_preview_apply_and_alias),
        ("public CLI cleanup + hidden aliases", test_public_cli_cleanup_and_hidden_aliases),
    ]


def run_suite(h: SympatchHarness, stop_on_fail: bool = False) -> list[TestResult]:
    results: list[TestResult] = []
    for name, fn in make_tests(h):
        try:
            fn()
            results.append(TestResult(name=name, passed=True))
            print(f"[PASS] {name}")
        except Exception as exc:  # noqa: BLE001 - this is a test harness
            detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            results.append(TestResult(name=name, passed=False, details=detail))
            print(f"[FAIL] {name}: {detail}")
            if h.verbose:
                traceback.print_exc()
            try:
                h.rollback_if_mutated()
            except Exception as cleanup_exc:  # noqa: BLE001
                print(f"[WARN] cleanup rollback failed after {name}: {cleanup_exc}")
            if stop_on_fail:
                break
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="End-to-end Sympatch 0.9 verification suite.")
    parser.add_argument("--workdir", help="Use this directory for the disposable test workspace.")
    parser.add_argument("--keep", action="store_true", help="Keep the test workspace after the run.")
    parser.add_argument("--verbose", action="store_true", help="Print every Sympatch command with stdout/stderr.")
    parser.add_argument("--stop-on-fail", action="store_true", help="Stop after the first failed check.")
    args = parser.parse_args()

    if args.workdir:
        base = Path(args.workdir).resolve()
        if base.exists():
            shutil.rmtree(base)
        base.mkdir(parents=True)
        cleanup = False
    else:
        base = Path(tempfile.mkdtemp(prefix="sympatch_09_e2e_"))
        cleanup = not args.keep

    root = base / "project"
    replacement_dir = base / "replacements"
    replacement_dir.mkdir(parents=True, exist_ok=True)
    create_controlled_project(root)
    h = SympatchHarness(root=root, replacement_dir=replacement_dir, verbose=args.verbose)

    print(f"Sympatch 0.9 test workspace: {base}")
    print(f"Controlled project root:     {root}")
    print()

    results = run_suite(h, stop_on_fail=args.stop_on_fail)
    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed

    print("\nSummary")
    print("-------")
    print(f"Passed: {passed}")
    print(f"Failed: {failed}")
    if failed:
        print("\nFailures:")
        for r in results:
            if not r.passed:
                print(f"- {r.name}: {r.details}")

    if cleanup:
        shutil.rmtree(base, ignore_errors=True)
    else:
        print(f"\nWorkspace retained: {base}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
