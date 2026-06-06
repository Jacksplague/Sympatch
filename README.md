# Sympatch

Symbol-aware patching and validation tools for Python projects.

Sympatch is designed for agent-assisted and LLM-assisted Python development where full-file rewrites are risky. It indexes Python code with the standard `ast` module, assigns stable symbol identities, extracts exact source ranges, applies targeted patches, validates changes, records history, and supports rollback.

## What Sympatch does

- Index Python projects with AST-based symbol discovery
- Search, inspect, and extract exact functions, classes, methods, and async functions
- Replace individual symbols safely instead of rewriting whole files
- Reconcile full-file AI rewrites into targeted symbol patches
- Run transactional multi-symbol patch sessions
- Export LLM-ready context bundles
- Apply declarative patch intent files
- Analyze caller and dependency impact before patching
- Run syntax checks and configured validation hooks
- Track diffs, patch history, and rollback points

## Install

From PyPI or an installed package source:

```bash
pip install sympatch
```

From a local wheel:

```bash
python -m pip install sympatch-0.9.0-py3-none-any.whl
```

For local development:

```bash
python -m pip install -e .
```

## Quick start

```bash
sympatch --root . index
sympatch --root . find check_python_syntax
sympatch --root . context tool_registry.ToolRegistry._check_python_syntax
sympatch --root . impact tool_registry.ToolRegistry._check_python_syntax
```

Sympatch stores its local project metadata in:

```text
.sympatch/
```

That directory should normally be ignored by Git.

## Core commands

| Command | Purpose |
|---|---|
| `index` | Build `.sympatch/index.json` |
| `modules` | List indexed Python files |
| `symbols` | List indexed symbols |
| `tree` | Show module-to-symbol hierarchy |
| `find` | Search for symbols |
| `show` | Print exact source for a symbol |
| `card` | Print compact symbol metadata |
| `context` | Show target, callers, callees, imports, and nearby context |
| `analyze` | Explain patch risk and suggested checks |
| `impact` | Inspect caller, dependency, signature, import, and return-value risk |
| `bundle` | Export LLM-ready Markdown or JSON context |
| `replace` | Replace one symbol from a patch file |
| `reconcile` | Convert a full-file rewrite into symbol-level patches |
| `session` | Run transactional multi-symbol patch sessions |
| `intent` | Preview or apply declarative patch intent files |
| `validate` | Run syntax checks and validation hooks |
| `diff` | Show patch diffs |
| `history` | Show patch history |
| `rollback` | Restore a previous patch state |

## Hidden compatibility aliases

These still work, but are not shown in normal help:

| Alias | Canonical command |
|---|---|
| `scan` | `index` |
| `search` | `find` |
| `slice` | `context` |

Use canonical commands for scripts and documentation.

## Main workflows

### Index a project

```bash
sympatch --root . index
```

After indexing, inspect the project:

```bash
sympatch --root . modules
sympatch --root . symbols
sympatch --root . tree
```

### Find and inspect a symbol

```bash
sympatch --root . find check_python_syntax
sympatch --root . card tool_registry.ToolRegistry._check_python_syntax
sympatch --root . show tool_registry.ToolRegistry._check_python_syntax --lines
```

### Generate context around a symbol

```bash
sympatch --root . context tool_registry.ToolRegistry._check_python_syntax
sympatch --root . context tool_registry.ToolRegistry._check_python_syntax --depth 2
```

Context output is intended for reviewing a symbol before patching. It can include the target source, imports, callers, callees, and nearby symbols.

### Analyze patch risk

```bash
sympatch --root . analyze tool_registry.ToolRegistry._check_python_syntax
sympatch --root . impact tool_registry.ToolRegistry._check_python_syntax
sympatch --root . impact tool_registry.ToolRegistry._check_python_syntax --json
```

The `impact` command reports information such as:

- internal caller edges
- inspected call sites
- positional vs keyword call usage
- return-value usage
- direct import risk
- same-name method candidates
- outgoing dependencies
- unresolved or low-confidence calls
- signature, return-value, dependency, and overall risk levels
- recommended validation checks

### Replace one symbol

Create a patch file containing the replacement function or method body, then run:

```bash
sympatch --root . replace package.module.Class.method patched_method.py
sympatch --root . validate
sympatch --root . diff
```

Rollback the last patch if needed:

```bash
sympatch --root . rollback last
```

### Reconcile an AI rewrite

Use `reconcile` when an LLM or agent produced a full rewritten file, but you only want to apply changed symbols.

Preview the differences:

```bash
sympatch --root . reconcile package/module.py ai_rewrite/module.py
```

Apply only the changed symbols:

```bash
sympatch --root . reconcile package/module.py ai_rewrite/module.py --apply
```

Apply and run validation hooks:

```bash
sympatch --root . reconcile package/module.py ai_rewrite/module.py --apply --run-hooks
```

`reconcile` is useful because it can ignore unrelated formatting drift and preserve untouched code from the real project file.

### Transactional patch session

Use sessions when multiple related symbols need to change together.

```bash
sympatch --root . session start syntax-validation-fix
sympatch --root . session replace package.module.func patched_func.py
sympatch --root . session replace package.module.Class.method patched_method.py
sympatch --root . session validate
sympatch --root . session diff
sympatch --root . session commit
```

Commit with validation hooks:

```bash
sympatch --root . session commit --run-hooks
```

Abort a session:

```bash
sympatch --root . session abort
```

A session lets multiple symbol edits commit as one atomic patch record.

### LLM context bundle

Export a compact context bundle for an LLM or agent.

Markdown:

```bash
sympatch --root . bundle package.module.func --out func.bundle.md
```

JSON:

```bash
sympatch --root . bundle package.module.func --format json --out func.bundle.json
```

A bundle can include:

- target symbol source
- signature
- symbol hash
- imports
- direct callers
- direct callees
- nearby symbols
- replacement constraints
- validation plan
- patch-intent template

This is intended to avoid dumping entire project files into LLM context.

### Patch intent files

Patch intent files allow an agent to describe what it wants changed while Sympatch remains the deterministic executor.

Generate a template:

```bash
sympatch --root . intent template --kind replace --out patch_intent.json
```

Preview an intent:

```bash
sympatch --root . intent preview patch_intent.json
```

Apply an intent:

```bash
sympatch --root . intent apply patch_intent.json
```

Apply an intent and run hooks:

```bash
sympatch --root . intent apply patch_intent.json --run-hooks
```

Intent files can represent operations such as targeted replacement or reconcile-based patching. Multi-operation intents are applied as a single transaction.

Example replace intent:

```json
{
  "version": 1,
  "description": "Replace one target symbol.",
  "operations": [
    {
      "operation": "replace",
      "target": "package.module.Class.method",
      "source_file": "patched_method.py",
      "allow_name_change": false
    }
  ],
  "validation": {
    "syntax": true,
    "run_hooks": true
  }
}
```

Example reconcile intent:

```json
{
  "version": 1,
  "description": "Apply changed symbols from an AI rewrite.",
  "operations": [
    {
      "operation": "reconcile",
      "original_file": "package/module.py",
      "rewritten_file": "ai_rewrite/module.py",
      "include_classes": false
    }
  ],
  "validation": {
    "syntax": true,
    "run_hooks": true
  }
}
```

## Validation

Run syntax validation:

```bash
sympatch --root . validate --syntax-only
```

Initialize validation config:

```bash
sympatch --root . validate --init-config
```

Run configured validation:

```bash
sympatch --root . validate
```

Run an ad-hoc validation command:

```bash
sympatch --root . validate --command "python -m pytest"
```

## Validation config

Sympatch can read validation config from:

```text
.sympatch/config.toml
```

or:

```text
.sympatch/config.json
```

Example `.sympatch/config.toml`:

```toml
[validation]
syntax = true
commands = [
  "python -m compileall .",
  "python -m pytest"
]
```

Example `.sympatch/config.json`:

```json
{
  "validation": {
    "syntax": true,
    "commands": [
      "python -m compileall .",
      "python -m pytest"
    ]
  }
}
```

## Hook-aware mutating operations

The following mutating operations can run validation hooks:

```bash
sympatch --root . replace package.module.func patched_func.py --run-hooks
sympatch --root . reconcile package/module.py ai_rewrite/module.py --apply --run-hooks
sympatch --root . session commit --run-hooks
sympatch --root . intent apply patch_intent.json --run-hooks
```

If validation hooks fail during a hook-aware mutation, Sympatch restores touched files and re-indexes the project.

## Diff, history, and rollback

Show diffs:

```bash
sympatch --root . diff
```

Show patch history:

```bash
sympatch --root . history
```

Rollback the most recent patch:

```bash
sympatch --root . rollback last
```

Rollback a specific patch if supported by the recorded history identifier:

```bash
sympatch --root . rollback PATCH_ID
```

## Safety model

Sympatch is designed to make AI-assisted patching less destructive.

Core safety properties:

- Patches target symbols instead of whole files
- Hash checks detect stale edits
- Syntax validation runs before accepting changes
- Validation hooks can block bad mutations
- Failed hook-aware transactions restore touched files
- Patch sessions commit atomically
- Intent files separate model intent from deterministic execution
- Reconcile converts full-file AI rewrites into symbol-level patches
- Patch history enables rollback

## Testing

Run the full standalone verification script:

```bash
python tests/test_sympatch_09.py --verbose
```

Optional test flags:

```bash
python tests/test_sympatch_09.py --keep
python tests/test_sympatch_09.py --stop-on-fail
```

The test script creates a disposable controlled project and verifies expected behavior for indexing, inspection, replacement, reconciliation, sessions, intents, validation hooks, rollback, aliases, and CLI cleanup.

## Git ignore

Recommended `.gitignore` entries:

```gitignore
.sympatch/
__pycache__/
*.pyc
build/
dist/
*.egg-info/
.venv/
```

## Release notes

### 0.9.0

Added stronger dependency/caller impact analysis and configurable validation hooks.

Highlights:

- Added `impact`
- Added validation config support
- Added validation hook execution
- Added `--run-hooks` support for mutating operations
- Added hook failure restoration behavior
- Added full standalone verification script

### 0.8.0

Added LLM context bundles and patch intent files.

Highlights:

- Added `bundle`
- Added `intent template`
- Added `intent preview`
- Added `intent apply`
- Added atomic multi-operation intent application

### 0.7.0

Added reconcile, transactional patch sessions, and public CLI cleanup.

Highlights:

- Added `reconcile`
- Added `session start`
- Added `session replace`
- Added `session validate`
- Added `session diff`
- Added `session commit`
- Added `session abort`
- Hid compatibility aliases from normal help

### 0.6.0

Packaged installable version with symbol indexing, replacement, validation, history, and rollback.

## Project status

Sympatch is currently focused on Python projects and uses Python AST parsing. It is intended as a deterministic safety layer for local agents, LLM coding workflows, and manual symbol-level patching.

