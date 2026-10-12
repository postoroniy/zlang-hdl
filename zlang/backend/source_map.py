"""Deterministic mappings from generated backend lines to ZLang origins.

The map deliberately contains only entries for which an emitter can identify
one exact generated statement.  Absence of an entry means "not mapped"; tools
must never infer a source location from an RTL/Haskell identifier spelling.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from zlang.backend.manifest import BackendArtifact
from zlang.ir.equivalence import SignalRole
from zlang.ir.module import Module
from zlang.source import SourceOrigin


GENERATED_SOURCE_MAP_VERSION = 1


@dataclass(frozen=True, order=True)
class GeneratedLineRange:
    """One inclusive line range in a generated artifact."""

    start_line: int
    end_line: int

    def __post_init__(self) -> None:
        if self.start_line < 1 or self.end_line < self.start_line:
            raise ValueError("invalid generated line range")


@dataclass(frozen=True)
class GeneratedSourceMapEntry:
    """One exact generated range attributed to a typed semantic object."""

    generated: GeneratedLineRange
    semantic_identity: str
    source_origin: SourceOrigin

    def __post_init__(self) -> None:
        if not self.semantic_identity:
            raise ValueError("generated source-map semantic identity is empty")


@dataclass(frozen=True)
class GeneratedSourceMap:
    """Versioned sidecar for one immutable :class:`BackendArtifact`."""

    backend: str
    module: str
    selected_ir_identity: str
    artifact_hash: str
    entries: tuple[GeneratedSourceMapEntry, ...] = ()
    version: int = GENERATED_SOURCE_MAP_VERSION

    def __post_init__(self) -> None:
        if self.version != GENERATED_SOURCE_MAP_VERSION:
            raise ValueError(
                f"unsupported generated source-map version: {self.version}"
            )
        if not self.backend or not self.module or not self.selected_ir_identity:
            raise ValueError("generated source-map artifact identity is incomplete")
        if not re.fullmatch(r"[0-9a-f]{64}", self.artifact_hash):
            raise ValueError("generated source-map artifact hash must be SHA-256")
        ordered = tuple(sorted(
            self.entries,
            key=lambda item: (
                item.generated.start_line,
                item.generated.end_line,
                item.semantic_identity,
                item.source_origin.render(),
            ),
        ))
        if ordered != self.entries:
            raise ValueError("generated source-map entries are not canonical")
        keys = tuple(
            (item.generated.start_line, item.generated.end_line, item.semantic_identity)
            for item in self.entries
        )
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate generated source-map entry")

    def to_json(self) -> str:
        payload = {
            "version": self.version,
            "backend": self.backend,
            "module": self.module,
            "selected_ir_identity": self.selected_ir_identity,
            "artifact_hash": self.artifact_hash,
            "entries": [
                {
                    "generated": {
                        "start_line": item.generated.start_line,
                        "end_line": item.generated.end_line,
                    },
                    "semantic_identity": item.semantic_identity,
                    "source_origin": item.source_origin.to_data(),
                }
                for item in self.entries
            ],
        }
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(
        cls, payload: str | bytes | dict[str, object]
    ) -> "GeneratedSourceMap":
        data = json.loads(payload) if isinstance(payload, (str, bytes)) else payload
        raw_entries = data.get("entries", ())
        if not isinstance(raw_entries, list | tuple):
            raise ValueError("generated source-map entries must be a list")
        entries = tuple(sorted(
            (
                GeneratedSourceMapEntry(
                    GeneratedLineRange(
                        int(item["generated"]["start_line"]),
                        int(item["generated"]["end_line"]),
                    ),
                    str(item["semantic_identity"]),
                    SourceOrigin.from_data(item["source_origin"]),
                )
                for item in raw_entries
            ),
            key=lambda item: (
                item.generated.start_line,
                item.generated.end_line,
                item.semantic_identity,
                item.source_origin.render(),
            ),
        ))
        return cls(
            str(data["backend"]),
            str(data["module"]),
            str(data["selected_ir_identity"]),
            str(data["artifact_hash"]),
            entries,
            int(data.get("version", 0)),
        )

    def write_sidecar(self, path: str | Path) -> Path:
        """Write the canonical JSON sidecar and return its path."""

        destination = Path(path)
        destination.write_text(self.to_json(), encoding="utf-8")
        return destination

    def entries_for_line(self, line: int) -> tuple[GeneratedSourceMapEntry, ...]:
        """Return exact semantic mappings that cover one generated line."""

        if line < 1:
            raise ValueError("generated line must be positive")
        return tuple(
            item for item in self.entries
            if item.generated.start_line <= line <= item.generated.end_line
        )


def build_generated_source_map(
    module: Module, artifact: BackendArtifact
) -> GeneratedSourceMap:
    """Build the exact mappings currently supported by the selected backend.

    This first bounded implementation maps a simple top-level output assignment
    only.  The mapping uses the typed assignment origin and the artifact's
    explicit semantic/physical binding; it is omitted when either side is
    missing or the generated statement is not unique.
    """

    digest = hashlib.sha256(artifact.text.encode()).hexdigest()
    if digest != artifact.artifact_hash:
        raise ValueError("backend artifact text does not match its artifact hash")
    if (
        artifact.module != module.name
        and artifact.formal_artifact_hash is None
    ):
        raise ValueError(
            f"source-map module mismatch: {module.name} != {artifact.module}"
        )
    entries: list[GeneratedSourceMapEntry] = []
    if len(module.assignments) == 1 and len(module.outputs) == 1:
        assignment = module.assignments[0]
        output = module.outputs[0]
        origin = getattr(assignment.expression, "origin", None)
        binding = next((
            item
            for item in artifact.bindings
            if item.semantic_signal_id == f"port:{output.name}"
            and item.role is SignalRole.OUTPUT
            and item.source_origin is not None
        ), None)
        if (
            origin is not None
            and binding is not None
            and binding.source_origin == origin
        ):
            line = _exact_assignment_line(artifact, binding.rtl_path)
            if line is not None:
                entries.append(GeneratedSourceMapEntry(
                    GeneratedLineRange(line, line),
                    binding.semantic_signal_id,
                    origin,
                ))
    entries.sort(key=lambda item: (
        item.generated.start_line,
        item.generated.end_line,
        item.semantic_identity,
        item.source_origin.render(),
    ))
    return GeneratedSourceMap(
        artifact.backend,
        artifact.module,
        artifact.selected_ir_identity,
        artifact.artifact_hash,
        tuple(entries),
    )


def _exact_assignment_line(
    artifact: BackendArtifact, rtl_path: str
) -> int | None:
    lines = artifact.text.splitlines()
    if artifact.backend == "direct_systemverilog":
        if not rtl_path or "." in rtl_path:
            return None
        pattern = re.compile(rf"^\s*assign\s+{re.escape(rtl_path)}\s*=")
        matches = [index for index, line in enumerate(lines, 1) if pattern.search(line)]
        return matches[0] if len(matches) == 1 else None
    return None
__all__ = [
    "GENERATED_SOURCE_MAP_VERSION",
    "GeneratedLineRange",
    "GeneratedSourceMap",
    "GeneratedSourceMapEntry",
    "build_generated_source_map",
]
