from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class SymbolRecord:
    id: str
    file: str
    kind: str
    name: str
    qualname: str
    signature: str
    start_line: int
    end_line: int
    indent: int
    source_hash: str
    parent: str | None = None
    decorators: list[str] = field(default_factory=list)
    docstring: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SymbolRecord":
        return SymbolRecord(**data)


@dataclass(slots=True)
class ModuleRecord:
    file: str
    sha256: str
    symbols: list[SymbolRecord]
    parse_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "sha256": self.sha256,
            "parse_error": self.parse_error,
            "symbols": [s.to_dict() for s in self.symbols],
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "ModuleRecord":
        return ModuleRecord(
            file=data["file"],
            sha256=data["sha256"],
            parse_error=data.get("parse_error"),
            symbols=[SymbolRecord.from_dict(s) for s in data.get("symbols", [])],
        )


@dataclass(slots=True)
class ProjectIndex:
    root: str
    version: str
    generated_at: str
    modules: list[ModuleRecord]

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "version": self.version,
            "generated_at": self.generated_at,
            "modules": [m.to_dict() for m in self.modules],
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "ProjectIndex":
        return ProjectIndex(
            root=data["root"],
            version=data.get("version", "0.1.0"),
            generated_at=data.get("generated_at", ""),
            modules=[ModuleRecord.from_dict(m) for m in data.get("modules", [])],
        )

    def symbol_map(self) -> dict[str, SymbolRecord]:
        out: dict[str, SymbolRecord] = {}
        for module in self.modules:
            for symbol in module.symbols:
                out[symbol.id] = symbol
        return out

    def module_map(self) -> dict[str, ModuleRecord]:
        return {module.file: module for module in self.modules}
