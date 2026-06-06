from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class ImportRecord:
    module: str
    name: str | None
    alias: str | None
    file: str
    line: int
    import_type: str  # import | from

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ImportRecord":
        return cls(**data)


@dataclass(slots=True)
class CallEdge:
    caller: str
    callee: str
    raw_callee: str
    file: str
    line: int
    call_type: str
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CallEdge":
        return cls(**data)


@dataclass(slots=True)
class SymbolRecord:
    id: str
    name: str
    qualname: str
    kind: str
    file: str
    module: str
    start_line: int
    end_line: int
    signature: str
    source_hash: str
    docstring: str | None = None
    parent: str | None = None
    decorators: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SymbolRecord":
        return cls(**data)


@dataclass(slots=True)
class FileRecord:
    path: str
    module: str
    sha256: str
    line_count: int
    parse_error: str | None = None
    symbols: list[SymbolRecord] = field(default_factory=list)
    imports: list[ImportRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["symbols"] = [s.to_dict() for s in self.symbols]
        data["imports"] = [i.to_dict() for i in self.imports]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FileRecord":
        copied = dict(data)
        copied["symbols"] = [SymbolRecord.from_dict(x) for x in data.get("symbols", [])]
        copied["imports"] = [ImportRecord.from_dict(x) for x in data.get("imports", [])]
        return cls(**copied)


@dataclass(slots=True)
class ProjectIndex:
    root: str
    generated_at: str
    files: list[FileRecord] = field(default_factory=list)
    call_edges: list[CallEdge] = field(default_factory=list)
    version: str = "0.9.0"

    def all_symbols(self) -> list[SymbolRecord]:
        out: list[SymbolRecord] = []
        for f in self.files:
            out.extend(f.symbols)
        return out

    def all_imports(self) -> list[ImportRecord]:
        out: list[ImportRecord] = []
        for f in self.files:
            out.extend(f.imports)
        return out

    def find_symbol(self, needle: str) -> SymbolRecord | None:
        needle = needle.strip()
        if not needle:
            return None
        symbols = self.all_symbols()
        for s in symbols:
            if needle == s.id or needle in s.aliases:
                return s
        exact_qual = [s for s in symbols if needle == s.qualname]
        if len(exact_qual) == 1:
            return exact_qual[0]
        exact_name = [s for s in symbols if needle == s.name]
        if len(exact_name) == 1:
            return exact_name[0]
        normalized = needle.lstrip("_")
        normalized_name = [s for s in symbols if s.name.lstrip("_") == normalized]
        if len(normalized_name) == 1:
            return normalized_name[0]
        suffix = [s for s in symbols if s.id.endswith("." + needle) or s.qualname.endswith("." + needle)]
        if len(suffix) == 1:
            return suffix[0]
        normalized_suffix = [
            s for s in symbols
            if s.id.lstrip("_").endswith("." + normalized) or s.qualname.lstrip("_").endswith("." + normalized)
        ]
        if len(normalized_suffix) == 1:
            return normalized_suffix[0]
        return None

    def search_symbols(self, query: str) -> list[SymbolRecord]:
        q = query.strip().lower()
        if not q:
            return []
        matches: list[SymbolRecord] = []
        for s in self.all_symbols():
            haystack = "\n".join([
                s.id,
                s.name,
                s.qualname,
                s.kind,
                s.file,
                s.module,
                s.signature,
                s.docstring or "",
                " ".join(s.calls),
                " ".join(s.aliases),
            ]).lower()
            if q in haystack:
                matches.append(s)
        return matches

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "root": self.root,
            "generated_at": self.generated_at,
            "files": [f.to_dict() for f in self.files],
            "call_edges": [e.to_dict() for e in self.call_edges],
            "counts": {
                "files": len(self.files),
                "symbols": sum(len(f.symbols) for f in self.files),
                "imports": sum(len(f.imports) for f in self.files),
                "calls": len(self.call_edges),
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectIndex":
        return cls(
            version=data.get("version", "0.9.0"),
            root=data.get("root", "."),
            generated_at=data.get("generated_at", ""),
            files=[FileRecord.from_dict(x) for x in data.get("files", [])],
            call_edges=[CallEdge.from_dict(x) for x in data.get("call_edges", [])],
        )
