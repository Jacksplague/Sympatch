from __future__ import annotations

from pathlib import Path

from sympatch.indexer import scan_project
from sympatch.patcher import replace_symbol, symbol_source
from sympatch.storage import load_index, save_index


def test_scan_and_replace(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    app = project / "app.py"
    app.write_text(
        "class A:\n"
        "    def hello(self):\n"
        "        return 'old'\n"
        "\n"
        "def top():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    repl = tmp_path / "replacement.py"
    repl.write_text("def hello(self):\n    return 'new'\n", encoding="utf-8")

    index = scan_project(project)
    save_index(project, index)
    loaded = load_index(project)
    assert "app.py::A.hello" in loaded.symbol_map()

    src = symbol_source(project, loaded.symbol_map()["app.py::A.hello"])
    assert "return 'old'" in src

    record = replace_symbol(project, "app.py::A.hello", repl)
    assert record["operation"] == "replace_symbol"
    assert "return 'new'" in app.read_text(encoding="utf-8")
