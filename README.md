# sympatch

`sympatch` is a small, standalone, symbol-aware patching tool for Python projects.

It indexes Python modules with `ast`, gives every class/function/method a stable symbol ID, extracts exact symbol source, replaces individual symbols with hash checks, validates patched files, records history, and re-indexes after successful edits.

The intended use case is agentic coding, large-file maintenance, and memory-efficient code navigation:

```text
AST parse -> symbol index -> exact symbol extraction -> hash-checked replacement -> validation -> patch history -> re-index
```

## Install

From this folder:

```bash
python -m pip install -e .
```

Then run:

```bash
sympatch --help
```

You can also run without installing:

```bash
python -m sympatch.cli --help
```

## Quick start

Index a project:

```bash
sympatch scan /path/to/project
```

List indexed modules:

```bash
sympatch --root /path/to/project modules
```

Show a module's symbols:

```bash
sympatch --root /path/to/project symbols your_file.py
```

Print a compact code tree:

```bash
sympatch --root /path/to/project tree
```

Search symbols:

```bash
sympatch --root /path/to/project search run_tool
```

Show exact symbol source:

```bash
sympatch --root /path/to/project show your_file.py::AgentGUI.run_tool_loop
```

Show metadata for a symbol:

```bash
sympatch --root /path/to/project card your_file.py::AgentGUI.run_tool_loop
```

Replace a function or method:

```bash
sympatch --root /path/to/project replace your_file.py::AgentGUI.run_tool_loop patched_run_tool_loop.py
```

Validate a file or project:

```bash
sympatch --root /path/to/project validate your_file.py
sympatch --root /path/to/project validate .
```

Show latest patch diff:

```bash
sympatch --root /path/to/project diff
```

Rollback latest patch:

```bash
sympatch --root /path/to/project rollback last
```

## Replacement files

For method replacement, write the replacement method at normal zero indentation:

```python
def run_tool_loop(self, max_tool_rounds: int = 8) -> None:
    pass
```

`sympatch` will automatically indent it to match the target method's existing indentation.

## Safety features

- Refuses to patch if the indexed symbol hash does not match the current file.
- Writes patch history under `.sympatch/history/`.
- Restores the original file automatically if Python compilation fails after a patch.
- Re-indexes the project after successful patch or rollback.

## Current limitations

- Python-only.
- Uses `ast`, so it does not preserve comments inside rewritten symbols unless the replacement contains them.
- Does not yet build full call graphs or dependency slices.
- Symbol summaries are not generated yet.
- Does not perform semantic search.

## Recommended workflow

Use Git or copy your project before heavy automated patching. `sympatch` has rollback history, but Git remains the correct source of truth.

