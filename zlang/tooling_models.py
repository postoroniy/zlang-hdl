"""Immutable models for compiler-owned tooling sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from zlang._version import __version__
from zlang.analysis_needs import AnalysisNeeds
from zlang.definition_resolution import DefinitionResolution, DefinitionTarget
from zlang.project import ProjectModelError, discover_project_manifest
from zlang.public_capabilities import CAPABILITY_REGISTRY
from zlang.source_identity import SOURCE_SUFFIX
from zlang import workspace as workspace_api

TOOLING_API_SCHEMA = 1
TOOLING_DOCUMENT_SYMBOL_SCHEMA = 1
TOOLING_HOVER_SCHEMA = 1
TOOLING_DEFINITION_SCHEMA = 1
TOOLING_REFERENCE_SCHEMA = 1
TOOLING_RENAME_SCHEMA = 1
TOOLING_COMPLETION_SCHEMA = 1
TOOLING_SIGNATURE_HELP_SCHEMA = 1
TOOLING_SEMANTIC_TOKEN_SCHEMA = 1
TOOLING_DIAGNOSTIC_EDIT_SCHEMA = 1

class ToolingError(ValueError):
    """The tooling request could not produce a trustworthy immutable record."""


class ToolingRenameError(ToolingError):
    """A rename request was rejected by compiler-owned safety rules."""


@dataclass(frozen=True)
class EditorDocumentSnapshot:
    """One exact open editor buffer used by compiler-backed tooling."""

    path: Path
    text: str
    version: int | None = None
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        path = Path(self.path).expanduser().resolve(strict=True)
        if not isinstance(self.text, str):
            raise TypeError("editor document text must be a string")
        object.__setattr__(self, "path", path)
        object.__setattr__(
            self,
            "digest",
            hashlib.sha256(self.text.encode("utf-8")).hexdigest(),
        )


@dataclass(frozen=True)
class EditorWorkspaceSnapshot:
    """Atomic immutable view of every open ZLang document."""

    documents: tuple[EditorDocumentSnapshot, ...]

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.documents, key=lambda item: item.path.as_posix()))
        if len({item.path for item in ordered}) != len(ordered):
            raise ValueError("editor workspace document paths must be unique")
        object.__setattr__(self, "documents", ordered)

    def overlays_for(self, source: Path) -> dict[Path, str]:
        """Return root-package overlays for the source's nearest project."""

        resolved = Path(source).expanduser().resolve(strict=True)
        try:
            manifest = discover_project_manifest(resolved)
        except ProjectModelError as error:
            raise ToolingError(str(error)) from error
        if manifest is None:
            return {
                item.path: item.text
                for item in self.documents
                if item.path == resolved
            }
        manifest_path = manifest.path.resolve(strict=True)
        source_root = manifest.source_directory.resolve(strict=True)
        result: dict[Path, str] = {}
        for item in self.documents:
            try:
                item.path.relative_to(source_root)
            except ValueError:
                continue
            try:
                owner = discover_project_manifest(item.path)
            except ProjectModelError as error:
                raise ToolingError(str(error)) from error
            if owner is not None and owner.path.resolve(strict=True) == manifest_path:
                result[item.path] = item.text
        return result

    def identity_for(self, source: Path) -> tuple[tuple[str, str], ...]:
        identities: list[tuple[str, str]] = []
        for path, text in sorted(
            self.overlays_for(source).items(),
            key=lambda item: item[0].as_posix(),
        ):
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            try:
                saved = _digest_file(path) == digest
            except OSError:
                saved = False
            if not saved:
                identities.append((path.as_posix(), digest))
        return tuple(identities)


@dataclass(frozen=True)
class _ToolingSnapshotEntry:
    """One bounded session-owned semantic snapshot and its environment key."""

    context_key: tuple[object, ...]
    needs: AnalysisNeeds
    result: object
    environment_signature: tuple[object, ...]
    environment_fingerprint: tuple[object, ...]
    root_inventory: tuple[str, ...] = ()


@dataclass(frozen=True)
class _SymbolSnapshot:
    """Normalized definition records without AST or typed IR ownership."""

    physical_inputs: object
    definition_resolutions: tuple[DefinitionResolution, ...]
    definition_declarations: tuple[DefinitionTarget, ...]
    serialized_size: int
    occurrences_by_line: Mapping[
        tuple[str | None, int], tuple[DefinitionResolution, ...]
    ] = field(init=False, compare=False, repr=False)
    declarations_by_line: Mapping[
        tuple[str | None, int], tuple[DefinitionTarget, ...]
    ] = field(init=False, compare=False, repr=False)
    occurrences_by_declaration: Mapping[
        tuple[object, ...], tuple[DefinitionResolution, ...]
    ] = field(init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        occurrence_lines: dict[
            tuple[str | None, int], list[DefinitionResolution]
        ] = {}
        declaration_lines: dict[
            tuple[str | None, int], list[DefinitionTarget]
        ] = {}
        occurrences_by_declaration: dict[
            tuple[object, ...], list[DefinitionResolution]
        ] = {}
        for resolution in self.definition_resolutions:
            occurrence_lines.setdefault(
                (
                    resolution.occurrence.source_unit,
                    resolution.occurrence.span.start_line,
                ),
                [],
            ).append(resolution)
            occurrences_by_declaration.setdefault(
                _declaration_coordinate_key(resolution.target), []
            ).append(resolution)
        for declaration in self.definition_declarations:
            declaration_lines.setdefault(
                (
                    declaration.target.source_unit,
                    declaration.target.span.start_line,
                ),
                [],
            ).append(declaration)
        object.__setattr__(
            self,
            "occurrences_by_line",
            MappingProxyType({
                key: tuple(value) for key, value in occurrence_lines.items()
            }),
        )
        object.__setattr__(
            self,
            "declarations_by_line",
            MappingProxyType({
                key: tuple(value) for key, value in declaration_lines.items()
            }),
        )
        object.__setattr__(
            self,
            "occurrences_by_declaration",
            MappingProxyType({
                key: tuple(value)
                for key, value in occurrences_by_declaration.items()
            }),
        )


@dataclass(frozen=True)
class _SymbolPhysicalInputs:
    """Minimal path projection needed to map cached logical source origins."""

    root_source: Path
    project_manifest: Path | None
    project_lock: Path | None
    source_unit_paths: tuple[tuple[str, Path], ...]

    @property
    def all_paths(self) -> tuple[Path, ...]:
        return tuple(sorted({
            self.root_source,
            *(path for _, path in self.source_unit_paths),
            *(
                (self.project_manifest,)
                if self.project_manifest is not None
                else ()
            ),
            *((self.project_lock,) if self.project_lock is not None else ()),
        }, key=lambda path: path.as_posix()))


@dataclass(frozen=True)
class _ToolingSymbolEntry:
    """One bounded symbol-only snapshot and its validated environment."""

    context_key: tuple[object, ...]
    result: _SymbolSnapshot
    environment_signature: tuple[object, ...]
    environment_fingerprint: tuple[object, ...]
    root_inventory: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolingIdentity:
    api_schema: int
    compiler_version: str
    source_suffix: str
    capability_schema: int
    capability_identity: str
    capability_count: int


@dataclass(frozen=True)
class ResolvedImport:
    logical_path: str
    source_path: Path | None
    error: str | None = None

    def __post_init__(self) -> None:
        if bool(self.source_path) == bool(self.error):
            raise ValueError("resolved import must contain exactly one result")


@dataclass(frozen=True)
class ProjectLocation:
    manifest_path: Path
    project_root: Path
    source_directory: Path


@dataclass(frozen=True)
class WorkspaceModule:
    logical_path: str
    source_path: Path
    dependencies: tuple[str, ...]
    root: bool


@dataclass(frozen=True)
class WorkspaceIndex:
    manifest_path: Path
    source_directory: Path
    root_modules: tuple[WorkspaceModule, ...]
    dependency_modules: tuple[WorkspaceModule, ...]

    def source_path_for_unit(self, logical_path: str) -> Path | None:
        return workspace_api.source_path_for_logical_unit(
            self.root_modules,
            self.dependency_modules,
            logical_path,
        )

    def dependency_closure(self, logical_path: str) -> tuple[str, ...]:
        records = {
            item.logical_path: item
            for item in (*self.root_modules, *self.dependency_modules)
        }
        if logical_path not in records:
            raise ToolingError(f"workspace module is not indexed: {logical_path}")
        result: set[str] = set()
        visiting: set[str] = set()

        def visit(name: str) -> None:
            if name in visiting:
                return
            visiting.add(name)
            record = records.get(name)
            if record is None:
                return
            for dependency in record.dependencies:
                if dependency in records:
                    result.add(dependency)
                    visit(dependency)

        visit(logical_path)
        result.discard(logical_path)
        return tuple(sorted(result))


def _digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _declaration_coordinate_key(origin: object) -> tuple[object, ...]:
    """Identify one declaration across independent locked root analyses.

    Imported declarations can be reached through several compilation roots.
    Their digest remains snapshot evidence, while source unit plus exact
    parser-owned span/construct is the stable declaration coordinate within one
    project workspace.
    """

    span = getattr(origin, "span", origin)
    return (
        getattr(origin, "source_unit", None),
        int(span.start_line),
        int(span.start_column),
        int(span.end_line),
        int(span.end_column),
        getattr(origin, "construct", None),
    )


def _origin_key(origin: object) -> tuple[object, ...]:
    """Return the stable compiler-origin identity used for reference grouping."""

    span = getattr(origin, "span", origin)
    return (
        getattr(origin, "source_unit", None),
        getattr(origin, "digest", None),
        int(span.start_line),
        int(span.start_column),
        int(span.end_line),
        int(span.end_column),
        getattr(origin, "construct", None),
    )


def tooling_identity() -> ToolingIdentity:
    payload = {
        "schema_version": CAPABILITY_REGISTRY.schema_version,
        "editor_surface": CAPABILITY_REGISTRY.editor_surface(),
        "capabilities": CAPABILITY_REGISTRY.capability_matrix(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return ToolingIdentity(
        TOOLING_API_SCHEMA,
        __version__,
        SOURCE_SUFFIX,
        CAPABILITY_REGISTRY.schema_version,
        hashlib.sha256(encoded).hexdigest(),
        len(CAPABILITY_REGISTRY.capabilities),
    )
