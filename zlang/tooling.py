"""Stable, read-only integration surface for compiler-aware tooling.

This module deliberately exposes immutable records rather than parser,
workspace, or semantic implementation objects.  External tooling may depend on
``TOOLING_API_SCHEMA`` and these functions without importing compiler internals.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass, replace
import hashlib
import json
import os
import time
from collections import OrderedDict
from pathlib import Path
from types import MappingProxyType
from typing import Iterator, Mapping

from zlang._version import __version__
from zlang.analysis_needs import AnalysisNeeds
from zlang.ast import nodes as ast_nodes
from zlang.common.serialization import stable_digest
from zlang.completion_resolution import CompletionScope
from zlang.compiler import TopSelectionError, check_file_snapshot
from zlang.definition_resolution import DefinitionResolution, DefinitionTarget
from zlang.dependencies import DependencyModelError
from zlang.diagnostics import Diagnostic, DiagnosticError, DiagnosticFix
from zlang.ir.hierarchy import specialization_fingerprint
from zlang.ir import expressions as ir_expressions
from zlang.ir.types import (
    BitType,
    BitsType,
    FixedType,
    SIntType,
    UFixedType,
    UIntType,
)
from zlang.module_resolver import ModuleResolutionError, StdlibModuleResolver
from zlang.opt.identity import CANONICAL_IR_IDENTITY_SCHEMA
from zlang.parser import ParseError, is_valid_identifier, parse
from zlang.project import ProjectModelError, discover_project_manifest
from zlang.public_capabilities import CAPABILITY_REGISTRY
from zlang.semantic import SemanticError
from zlang.signature_help_resolution import SignatureHelpCall
from zlang.source import SourceOrigin
from zlang.source_identity import SOURCE_SUFFIX
from zlang.source_rebinding import TriviaRebinding
from zlang.workspace import (
    WorkspaceError,
    load_project_workspace,
    source_path_for_logical_unit,
)


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
SYMBOL_CACHE_SCHEMA = 1
# Compiler navigation coverage participates in the content recipe separately
# from the stable on-disk table schema.  Incrementing this value makes shards
# produced before newly supported semantic occurrences unreachable without
# renaming the public ``symbol-v1`` cache namespace.
_SYMBOL_CACHE_ANALYSIS_SCHEMA = 6

_SYMBOL_MEMORY_MAX_ENTRIES = 64
_SYMBOL_MEMORY_MAX_BYTES = 64 * 1024 * 1024
_SYMBOL_DISK_MAX_ENTRIES = 512
_SYMBOL_DISK_MAX_BYTES = 256 * 1024 * 1024
_SYMBOL_DISK_MAX_SHARD_BYTES = 16 * 1024 * 1024
_SYMBOL_DISK_MAX_RECORDS = 200_000
_SYMBOL_DISK_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
_SYMBOL_DISK_TOUCH_INTERVAL_SECONDS = 24 * 60 * 60
_REFERENCE_MAX_PROJECT_ROOTS = 128
_REFERENCE_MAX_PROJECT_CANDIDATES = 128


def _is_snapshot_race(error: BaseException) -> bool:
    message = str(error).lower()
    return "changed after" in message or " is dirty" in message


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


def _symbol_root_inventory(inputs: _SymbolPhysicalInputs) -> tuple[str, ...]:
    if inputs.project_manifest is None:
        return ()
    manifest = discover_project_manifest(
        inputs.project_manifest, explicit=inputs.project_manifest
    )
    if manifest is None:
        raise ToolingError("symbol cache project manifest disappeared")
    root = manifest.source_directory.resolve(strict=True)
    return tuple(sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*.zhl")
    ))


def _inventory_matches(entry: _ToolingSnapshotEntry | _ToolingSymbolEntry) -> bool:
    try:
        return entry.root_inventory == _symbol_root_inventory(entry.result.physical_inputs)
    except (OSError, ProjectModelError, ToolingError, ValueError):
        return False


def _digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _symbol_cache_root() -> Path:
    selected = os.environ.get("XDG_CACHE_HOME")
    if selected:
        candidate = Path(selected).expanduser()
        if candidate.is_absolute():
            return candidate / "zlang-hdl" / "lsp" / "symbol-v1"
    return Path.home() / ".cache" / "zlang-hdl" / "lsp" / "symbol-v1"


def _cache_origin_sort_key(origin: SourceOrigin) -> tuple[object, ...]:
    span = origin.span
    return (
        origin.source_unit or "",
        origin.digest or "",
        span.start_line,
        span.start_column,
        span.end_line,
        span.end_column,
        origin.construct,
    )


def _stdlib_source_unit(path: Path) -> str | None:
    """Recover a logical stdlib unit from a compiler-owned stdlib path."""

    resolved = path.expanduser().resolve()
    for parent in resolved.parents:
        if parent.name != "stdlib":
            continue
        relative = resolved.relative_to(parent)
        if relative.suffix != SOURCE_SUFFIX:
            return None
        components = relative.with_suffix("").parts
        if not components or any(not is_valid_identifier(item) for item in components):
            return None
        return "std." + ".".join(components)
    return None


def _symbol_lookup_context(
    source: Path,
    source_text: str,
    *,
    project: Path | str | None,
    profile: str | None,
    top: str | None,
) -> tuple[Path, str, object, object] | None:
    """Resolve one saved project snapshot to a content-addressed shard path."""

    try:
        location = discover_project(source)
        if location is None:
            return None
        workspace = load_project_workspace(
            location.manifest_path,
            project=project or location.manifest_path,
        )
        if workspace is None:
            return None
        root_identity = workspace.root_identity_for(source)
        text_digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        if root_identity.digest != text_digest or _digest_file(source) != text_digest:
            return None
        closure = workspace.compilation_closure_for(source)
        project_namespace = stable_digest({
            "project_root": location.project_root.as_posix(),
            "schema": SYMBOL_CACHE_SCHEMA,
        })
        lookup_identity = stable_digest({
            "analysis_schema": _SYMBOL_CACHE_ANALYSIS_SCHEMA,
            "capability_identity": tooling_identity().capability_identity,
            "compiler_schema": CANONICAL_IR_IDENTITY_SCHEMA,
            "compiler_version": __version__,
            "dependency_closure": closure.identity,
            "profile": profile,
            "root_digest": root_identity.digest,
            "root_source_unit": root_identity.logical_path,
            "schema": SYMBOL_CACHE_SCHEMA,
            "tooling_api_schema": TOOLING_API_SCHEMA,
            "top": top,
        })
        path = _symbol_cache_root() / project_namespace / f"{lookup_identity}.json"
        return path, lookup_identity, workspace, root_identity
    except (
        ModuleResolutionError,
        WorkspaceError,
        ProjectModelError,
        DependencyModelError,
        OSError,
        ValueError,
    ):
        return None


def _normalized_symbol_payload(
    source: Path,
    result: object,
    session: ToolingSession,
) -> tuple[_SymbolSnapshot, dict[str, object]]:
    """Deduplicate compiler observations into one deterministic symbol shard."""

    source_paths: dict[str, Path] = {}
    path_by_unit: dict[str | None, Path | None] = {}
    digest_by_unit: dict[str | None, str | None] = {}
    normalized_origins: dict[tuple[object, ...], SourceOrigin] = {}
    root_unit = _root_logical_source(source, session)
    overlay_digests = {
        Path(path).expanduser().resolve(): digest
        for path, digest in getattr(
            result.physical_inputs, "editor_source_overlays", ()
        )
    }

    def normalize_origin(origin: SourceOrigin) -> SourceOrigin:
        coordinate = _declaration_coordinate_key(origin)
        cached = normalized_origins.get(coordinate)
        if cached is not None:
            return cached
        unit = origin.source_unit
        if unit not in path_by_unit:
            path_by_unit[unit] = _source_path_for_origin(
                source, origin, result.physical_inputs, session
            )
            resolved_path = path_by_unit[unit]
            digest_by_unit[unit] = (
                overlay_digests.get(resolved_path.resolve())
                if resolved_path is not None
                and resolved_path.resolve() in overlay_digests
                else (
                    _digest_file(resolved_path)
                    if resolved_path is not None
                    else None
                )
            )
        path = path_by_unit[unit]
        is_root = (
            path is not None
            and path.resolve() == source.resolve()
            and unit in {None, root_unit}
        )
        digest = (
            origin.digest
            if is_root or path is None
            else digest_by_unit[unit]
        )
        if unit is not None and path is not None:
            source_paths[unit] = path
        normalized = SourceOrigin(origin.span, origin.construct, unit, digest)
        normalized_origins[coordinate] = normalized
        return normalized

    declaration_records: dict[
        tuple[object, ...], tuple[SourceOrigin, str, str]
    ] = {}
    declared_keys: set[tuple[object, ...]] = set()
    for declaration in result.definition_declarations:
        target = normalize_origin(declaration.target)
        key = (
            declaration.name,
            declaration.kind,
            *_declaration_coordinate_key(target),
        )
        declaration_records[key] = (
            target,
            declaration.name,
            declaration.kind,
        )
        declared_keys.add(key)
    for resolution in result.definition_resolutions:
        target = normalize_origin(resolution.target)
        key = (
            resolution.name,
            resolution.kind,
            *_declaration_coordinate_key(target),
        )
        declaration_records.setdefault(
            key,
            (target, resolution.name, resolution.kind),
        )

    ordered_declarations = tuple(
        sorted(declaration_records.values(), key=lambda item: (
            item[1], item[2], _cache_origin_sort_key(item[0])
        ))
    )
    declaration_ids = {
        (name, kind, *_declaration_coordinate_key(target)): stable_digest({
            "kind": kind,
            "name": name,
            "target": target.to_data(),
        })
        for target, name, kind in ordered_declarations
    }
    by_id = {
        declaration_ids[
            (name, kind, *_declaration_coordinate_key(target))
        ]: (target, name, kind)
        for target, name, kind in ordered_declarations
    }
    occurrence_records: dict[
        tuple[object, ...], tuple[str, SourceOrigin]
    ] = {}
    for resolution in result.definition_resolutions:
        target = normalize_origin(resolution.target)
        occurrence = normalize_origin(resolution.occurrence)
        declaration_id = declaration_ids[
            (
                resolution.name,
                resolution.kind,
                *_declaration_coordinate_key(target),
            )
        ]
        key = (declaration_id, *_origin_key(occurrence))
        occurrence_records[key] = (declaration_id, occurrence)
    ordered_occurrences = tuple(
        sorted(
            occurrence_records.values(),
            key=lambda item: (item[0], _cache_origin_sort_key(item[1])),
        )
    )

    definitions = tuple(
        DefinitionTarget(target, name, kind)
        for target, name, kind in ordered_declarations
        if (
            name,
            kind,
            *_declaration_coordinate_key(target),
        ) in declared_keys
    )
    resolutions = tuple(
        DefinitionResolution(
            occurrence,
            by_id[declaration_id][0],
            by_id[declaration_id][1],
            by_id[declaration_id][2],
        )
        for declaration_id, occurrence in ordered_occurrences
    )

    project_manifest = getattr(result.physical_inputs, "project_manifest", None)
    project_lock = getattr(result.physical_inputs, "project_lock", None)
    for stdlib_source in getattr(result.physical_inputs, "stdlib_sources", ()):
        stdlib_path = Path(stdlib_source).expanduser().resolve()
        stdlib_unit = _stdlib_source_unit(stdlib_path)
        if stdlib_unit is not None:
            source_paths[stdlib_unit] = stdlib_path
    physical_inputs = _SymbolPhysicalInputs(
        source.resolve(),
        Path(project_manifest).resolve() if project_manifest is not None else None,
        Path(project_lock).resolve() if project_lock is not None else None,
        tuple(sorted(source_paths.items())),
    )
    dependencies = [
        {"digest": _digest_file(path), "source_unit": unit}
        for unit, path in sorted(source_paths.items())
    ]
    payload: dict[str, object] = {
        "declarations": [
            {
                "declared": (
                    name, kind, *_declaration_coordinate_key(target)
                ) in declared_keys,
                "id": declaration_ids[
                    (name, kind, *_declaration_coordinate_key(target))
                ],
                "kind": kind,
                "name": name,
                "target": target.to_data(),
            }
            for target, name, kind in ordered_declarations
        ],
        "dependencies": dependencies,
        "manifest_digest": (
            _digest_file(physical_inputs.project_manifest)
            if physical_inputs.project_manifest is not None
            else None
        ),
        "occurrences": [
            {
                "declaration_id": declaration_id,
                "origin": origin.to_data(),
            }
            for declaration_id, origin in ordered_occurrences
        ],
        "lock_digest": (
            _digest_file(physical_inputs.project_lock)
            if physical_inputs.project_lock is not None
            else None
        ),
        "schema": SYMBOL_CACHE_SCHEMA,
    }
    serialized_size = len(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return (
        _SymbolSnapshot(
            physical_inputs,
            resolutions,
            definitions,
            serialized_size,
        ),
        payload,
    )


def _resolve_symbol_unit_paths(
    units: tuple[str, ...],
    workspace: object,
) -> dict[str, Path] | None:
    records = {
        item.logical_path: Path(item.source_path).resolve()
        for item in (
            *getattr(workspace, "root_modules", ()),
            *getattr(workspace, "dependency_modules", ()),
        )
    }
    unresolved = tuple(unit for unit in units if unit not in records)
    if unresolved:
        try:
            resolver = getattr(workspace, "resolver", None)
            if resolver is None:
                return None
            for item in resolver.resolve(unresolved):
                source_path = getattr(item, "source_path", None)
                if source_path is None:
                    return None
                records[item.logical_path] = Path(source_path).resolve()
        except (ModuleResolutionError, OSError, ValueError):
            return None
    if any(unit not in records for unit in units):
        return None
    return {unit: records[unit] for unit in units}


def _discard_cache_file(path: Path) -> None:
    try:
        if path.is_file() or path.is_symlink():
            path.unlink()
    except OSError:
        pass


def _restore_symbol_origin(data: object) -> SourceOrigin:
    if not isinstance(data, dict) or set(data) != {
        "construct", "digest", "source_unit", "span"
    }:
        raise ValueError("invalid symbol cache source origin")
    span = data["span"]
    if not isinstance(span, dict) or set(span) != {
        "end_column", "end_line", "start_column", "start_line"
    }:
        raise ValueError("invalid symbol cache source span")
    return SourceOrigin.from_data(data)


def _load_persistent_symbol_snapshot(
    source: Path,
    source_text: str,
    *,
    project: Path | str | None,
    profile: str | None,
    top: str | None,
) -> _SymbolSnapshot | None:
    context = _symbol_lookup_context(
        source,
        source_text,
        project=project,
        profile=profile,
        top=top,
    )
    if context is None:
        return None
    path, lookup_identity, workspace, _root_identity = context
    try:
        if path.is_symlink() or not path.is_file():
            if path.is_symlink():
                _discard_cache_file(path)
            return None
        stat = path.stat()
        if stat.st_size < 2 or stat.st_size > _SYMBOL_DISK_MAX_SHARD_BYTES:
            _discard_cache_file(path)
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or set(raw) != {
            "capability_identity",
            "compiler_schema",
            "compiler_version",
            "declarations",
            "dependencies",
            "lock_digest",
            "lookup_identity",
            "manifest_digest",
            "occurrences",
            "schema",
            "tooling_api_schema",
        }:
            raise ValueError("invalid symbol cache fields")
        if (
            raw["schema"] != SYMBOL_CACHE_SCHEMA
            or raw["tooling_api_schema"] != TOOLING_API_SCHEMA
            or raw["compiler_schema"] != CANONICAL_IR_IDENTITY_SCHEMA
            or raw["compiler_version"] != __version__
            or raw["capability_identity"]
            != tooling_identity().capability_identity
            or raw["lookup_identity"] != lookup_identity
        ):
            raise ValueError("symbol cache identity mismatch")
        declarations_data = raw["declarations"]
        occurrences_data = raw["occurrences"]
        dependencies_data = raw["dependencies"]
        if not all(
            isinstance(item, list)
            for item in (declarations_data, occurrences_data, dependencies_data)
        ):
            raise ValueError("symbol cache record tables must be arrays")
        if (
            len(declarations_data) + len(occurrences_data)
            > _SYMBOL_DISK_MAX_RECORDS
        ):
            raise ValueError("symbol cache has too many records")

        dependencies: dict[str, str] = {}
        for item in dependencies_data:
            if not isinstance(item, dict) or set(item) != {"digest", "source_unit"}:
                raise ValueError("invalid symbol cache dependency")
            unit = item["source_unit"]
            digest = item["digest"]
            if (
                not isinstance(unit, str)
                or not unit
                or not isinstance(digest, str)
                or len(digest) != 64
            ):
                raise ValueError("invalid symbol cache dependency identity")
            if unit in dependencies:
                raise ValueError("duplicate symbol cache dependency")
            dependencies[unit] = digest
        source_paths = _resolve_symbol_unit_paths(
            tuple(sorted(dependencies)), workspace
        )
        if source_paths is None or any(
            _digest_file(source_paths[unit]) != digest
            for unit, digest in dependencies.items()
        ):
            raise ValueError("symbol cache dependency changed")

        manifest = Path(workspace.manifest.path).resolve()
        lock = (
            Path(workspace.lock_path).resolve()
            if workspace.lock_path is not None
            else None
        )
        if raw["manifest_digest"] != _digest_file(manifest):
            raise ValueError("symbol cache manifest changed")
        if (
            raw["lock_digest"]
            != (_digest_file(lock) if lock is not None else None)
        ):
            raise ValueError("symbol cache lock changed")

        declaration_by_id: dict[str, tuple[SourceOrigin, str, str, bool]] = {}
        declared: list[DefinitionTarget] = []
        for item in declarations_data:
            if not isinstance(item, dict) or set(item) != {
                "declared", "id", "kind", "name", "target"
            }:
                raise ValueError("invalid symbol cache declaration")
            declaration_id = item["id"]
            name = item["name"]
            kind = item["kind"]
            is_declared = item["declared"]
            if (
                not isinstance(declaration_id, str)
                or len(declaration_id) != 64
                or not isinstance(name, str)
                or not name
                or not isinstance(kind, str)
                or not kind
                or not isinstance(is_declared, bool)
                or declaration_id in declaration_by_id
            ):
                raise ValueError("invalid symbol cache declaration identity")
            target = _restore_symbol_origin(item["target"])
            expected_id = stable_digest({
                "kind": kind,
                "name": name,
                "target": target.to_data(),
            })
            if declaration_id != expected_id:
                raise ValueError("symbol cache declaration hash mismatch")
            declaration_by_id[declaration_id] = (
                target, name, kind, is_declared
            )
            if is_declared:
                declared.append(DefinitionTarget(target, name, kind))

        resolutions: list[DefinitionResolution] = []
        seen_occurrences: set[tuple[object, ...]] = set()
        for item in occurrences_data:
            if not isinstance(item, dict) or set(item) != {
                "declaration_id", "origin"
            }:
                raise ValueError("invalid symbol cache occurrence")
            declaration_id = item["declaration_id"]
            if not isinstance(declaration_id, str):
                raise ValueError("invalid symbol cache occurrence identity")
            declaration = declaration_by_id.get(declaration_id)
            if declaration is None:
                raise ValueError("symbol cache occurrence target is unavailable")
            origin = _restore_symbol_origin(item["origin"])
            target, name, kind, _ = declaration
            occurrence_key = (declaration_id, *_origin_key(origin))
            if occurrence_key in seen_occurrences:
                raise ValueError("duplicate symbol cache occurrence")
            seen_occurrences.add(occurrence_key)
            resolutions.append(
                DefinitionResolution(origin, target, name, kind)
            )

        physical_inputs = _SymbolPhysicalInputs(
            source.resolve(),
            manifest,
            lock,
            tuple(sorted(source_paths.items())),
        )
        snapshot = _SymbolSnapshot(
            physical_inputs,
            tuple(resolutions),
            tuple(declared),
            stat.st_size,
        )
        now = time.time()
        if now - stat.st_mtime >= _SYMBOL_DISK_TOUCH_INTERVAL_SECONDS:
            try:
                os.utime(path, (now, now), follow_symlinks=False)
            except OSError:
                pass
        return snapshot
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        _discard_cache_file(path)
        return None


def _garbage_collect_symbol_cache(root: Path) -> None:
    try:
        files = [
            item for item in root.rglob("*.json")
            if item.is_file() and not item.is_symlink()
        ]
    except OSError:
        return
    now = time.time()
    retained: list[tuple[float, int, Path]] = []
    for item in files:
        try:
            stat = item.stat()
        except OSError:
            continue
        if now - stat.st_mtime > _SYMBOL_DISK_MAX_AGE_SECONDS:
            _discard_cache_file(item)
        else:
            retained.append((stat.st_mtime, stat.st_size, item))
    retained.sort()
    total = sum(size for _, size, _ in retained)
    while retained and (
        len(retained) > _SYMBOL_DISK_MAX_ENTRIES
        or total > _SYMBOL_DISK_MAX_BYTES
    ):
        _, size, item = retained.pop(0)
        _discard_cache_file(item)
        total -= size


def _publish_persistent_symbol_snapshot(
    source: Path,
    source_text: str,
    payload: dict[str, object],
    *,
    project: Path | str | None,
    profile: str | None,
    top: str | None,
) -> None:
    context = _symbol_lookup_context(
        source,
        source_text,
        project=project,
        profile=profile,
        top=top,
    )
    if context is None:
        return
    path, lookup_identity, _workspace, _root_identity = context
    persisted = {
        **payload,
        "capability_identity": tooling_identity().capability_identity,
        "compiler_schema": CANONICAL_IR_IDENTITY_SCHEMA,
        "compiler_version": __version__,
        "lookup_identity": lookup_identity,
        "tooling_api_schema": TOOLING_API_SCHEMA,
    }
    encoded = (
        json.dumps(persisted, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if (
        len(encoded) > _SYMBOL_DISK_MAX_SHARD_BYTES
        or len(payload["declarations"]) + len(payload["occurrences"])
        > _SYMBOL_DISK_MAX_RECORDS
    ):
        return
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.parent.is_symlink() or path.is_symlink():
            return
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
        )
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            _discard_cache_file(temporary)
            raise
        os.replace(temporary, path)
        temporary = None
        _garbage_collect_symbol_cache(_symbol_cache_root())
    except OSError:
        pass
    finally:
        if temporary is not None:
            _discard_cache_file(temporary)


class ToolingSession:
    """Bounded request-driven cache for compiler semantic tooling snapshots.

    The cache is deliberately owned by a tooling/LSP session rather than by
    the compiler process.  Entries are immutable semantic products, keyed by
    the exact root text plus the locked physical workspace snapshot.  No
    background refresh or global index is kept; the compiler-owned process-local
    session retains exact products for compatible snapshots.
    """

    _MAX_ENTRIES = 8

    def __init__(self) -> None:
        from zlang.incremental_workspace import IncrementalWorkspaceSession

        self._incremental_workspace = IncrementalWorkspaceSession()
        self._entries: OrderedDict[tuple[object, ...], _ToolingSnapshotEntry] = (
            OrderedDict()
        )
        self._workspace_indexes: OrderedDict[Path, object] = OrderedDict()
        self._navigation_regions: OrderedDict[
            tuple[Path, str], tuple[tuple[tuple[int, int], str], ...]
        ] = OrderedDict()
        self._symbol_entries: OrderedDict[
            tuple[object, ...], _ToolingSymbolEntry
        ] = OrderedDict()
        self._trivia_symbols: OrderedDict[
            Path, tuple[str, _ToolingSymbolEntry]
        ] = OrderedDict()
        self._trivia_checks: OrderedDict[
            Path, tuple[str, _ToolingSnapshotEntry]
        ] = OrderedDict()
        self._symbol_bytes = 0
        self._editor_workspace = EditorWorkspaceSnapshot(())
        requested_mode = os.environ.get(
            "ZLANG_LSP_SYMBOL_CACHE", "persistent"
        ).strip().lower()
        self._symbol_mode = (
            requested_mode
            if requested_mode in {"persistent", "memory", "off"}
            else "memory"
        )

    def _workspace_index_for(
        self,
        manifest: Path,
    ) -> object:
        """Return one session-local workspace projection for a manifest."""

        path = Path(manifest).expanduser().resolve()
        index = self._workspace_indexes.get(path)
        if index is None:
            # Resolve lazily so an unprojected standalone source never pays for
            # workspace indexing.  ``workspace_index`` is defined below in
            # this module and is available when the method is invoked.
            index = workspace_index(path)
            self._workspace_indexes[path] = index
            self._workspace_indexes.move_to_end(path)
            while len(self._workspace_indexes) > self._MAX_ENTRIES:
                self._workspace_indexes.popitem(last=False)
        else:
            self._workspace_indexes.move_to_end(path)
        return index

    @staticmethod
    def _context_key(
        source: Path,
        source_text: str,
        *,
        source_digest: str | None,
        project: Path | str | None,
        profile: str | None,
        top: str | None,
        editor_identity: tuple[tuple[str, str], ...] = (),
    ) -> tuple[object, ...]:
        # Always derive the content identity from the exact in-memory text.
        # ``source_digest`` is a caller-provided snapshot assertion (and is
        # checked by the compiler), not a substitute for the text identity in
        # the cache key.  A correct assertion is canonicalized to the same key
        # as an omitted assertion so an LSP didOpen check can be reused by a
        # subsequent definition/hover query.  A stale/mistyped assertion stays
        # distinct and is still rejected by the compiler on a cache miss.
        text_digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        digest_assertion = (
            None
            if source_digest is None or source_digest == text_digest
            else source_digest
        )
        project_path = (
            None
            if project is None
            else Path(project).expanduser().resolve().as_posix()
        )
        return (
            source,
            text_digest,
            digest_assertion,
            len(source_text),
            project_path,
            profile,
            top,
            TOOLING_API_SCHEMA,
            __version__,
            editor_identity,
        )

    def set_editor_workspace(self, snapshot: EditorWorkspaceSnapshot) -> None:
        """Select one immutable open-document view for subsequent requests."""

        if not isinstance(snapshot, EditorWorkspaceSnapshot):
            raise TypeError("editor workspace must be an EditorWorkspaceSnapshot")
        self._editor_workspace = snapshot

    def editor_text_for(self, source: Path | str) -> str | None:
        """Return exact open-buffer text for ``source``, when available."""

        path = Path(source).expanduser().resolve()
        for document in self._editor_workspace.documents:
            if document.path == path:
                return document.text
        return None

    def source_path_for_unit(
        self,
        source: Path | str,
        source_unit: str,
    ) -> Path | None:
        """Resolve one compiler logical unit within the source's project."""

        try:
            # Prefer the atomic editor view.  This route deliberately does not
            # parse bytes from disk: an imported open buffer may currently be
            # malformed, or an autosave may have replaced its physical bytes,
            # while the compiler diagnostic still carries the exact logical
            # unit that must receive the marker.
            manifest = discover_project_manifest(Path(source))
            if manifest is not None:
                source_root = manifest.source_directory.resolve(strict=True)
                for document in self._editor_workspace.documents:
                    try:
                        relative = document.path.relative_to(source_root)
                    except ValueError:
                        continue
                    if relative.suffix != SOURCE_SUFFIX:
                        continue
                    logical = ".".join(
                        (manifest.package, *relative.with_suffix("").parts)
                    )
                    if logical == source_unit:
                        return document.path

            location = discover_project(source)
            if location is None:
                return None
            index = self._workspace_index_for(location.manifest_path)
            return index.source_path_for_unit(source_unit)
        except (ToolingError, OSError, ValueError):
            return None

    @staticmethod
    def _file_fingerprint(path: Path) -> tuple[object, ...]:
        try:
            payload = path.read_bytes()
        except OSError:
            return (path.as_posix(), None)
        return (path.as_posix(), hashlib.sha256(payload).hexdigest())

    @staticmethod
    def _file_signature(path: Path) -> tuple[object, ...]:
        try:
            stat = path.stat()
        except OSError:
            return (path.as_posix(), None, None, None, None, None)
        return (
            path.as_posix(),
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
            stat.st_dev,
            stat.st_ino,
        )

    @classmethod
    def _environment_signature(cls, physical_inputs: object) -> tuple[object, ...]:
        overlays = {
            Path(path).expanduser().resolve(): digest
            for path, digest in getattr(
                physical_inputs, "editor_source_overlays", ()
            )
        }
        paths = tuple(
            Path(path).expanduser().resolve()
            for path in getattr(physical_inputs, "all_paths", ())
            if Path(path).expanduser().resolve() not in overlays
        )
        return (
            *tuple(cls._file_signature(path) for path in paths),
            *(('editor', path.as_posix(), digest) for path, digest in sorted(
                overlays.items(), key=lambda item: item[0].as_posix()
            )),
        )

    @classmethod
    def _environment_fingerprint(
        cls,
        physical_inputs: object,
    ) -> tuple[object, ...]:
        overlays = {
            Path(path).expanduser().resolve(): digest
            for path, digest in getattr(
                physical_inputs, "editor_source_overlays", ()
            )
        }
        paths = tuple(
            Path(path).expanduser().resolve()
            for path in getattr(physical_inputs, "all_paths", ())
            if Path(path).expanduser().resolve() not in overlays
        )
        # ``PhysicalCompilationInputs`` is the compiler's authoritative locked
        # dependency/source closure.  Fingerprint those paths only: no
        # directory scans, polling, or speculative workspace discovery is
        # performed by the cache.
        return (
            *tuple(cls._file_fingerprint(path) for path in paths),
            *(('editor', path.as_posix(), digest) for path, digest in sorted(
                overlays.items(), key=lambda item: item[0].as_posix()
            )),
        )

    def semantic_snapshot(
        self,
        source: Path | str,
        source_text: str,
        *,
        analysis_needs: AnalysisNeeds = AnalysisNeeds.NONE,
        source_digest: str | None = None,
        project: Path | str | None = None,
        profile: str | None = None,
        top: str | None = None,
    ) -> object:
        """Return a compatible semantic product, upgrading needs monotonically."""

        requested = AnalysisNeeds(analysis_needs)
        path = Path(source).expanduser().resolve()
        editor_identity = self._editor_workspace.identity_for(path)
        context_key = self._context_key(
            path,
            source_text,
            source_digest=source_digest,
            project=project,
            profile=profile,
            top=top,
            editor_identity=editor_identity,
        )
        entry = self._entries.get(context_key)
        if entry is not None:
            current_signature = self._environment_signature(
                entry.result.physical_inputs
            )
            if (
                _inventory_matches(entry)
                and entry.environment_signature == current_signature
                and entry.needs & requested == requested
            ):
                if requested.wants(AnalysisNeeds.DEFINITIONS):
                    self._remember_symbol_snapshot(
                        path, source_text, entry.result, context_key
                    )
                self._entries.move_to_end(context_key)
                return entry.result
            # File metadata is a cheap request-time guard.  Only when a
            # dependency/source signature changed do we re-hash the locked
            # inputs to distinguish a touch/replace with identical bytes from
            # an actual semantic snapshot change.
            if entry.environment_fingerprint == self._environment_fingerprint(
                entry.result.physical_inputs
            ):
                refreshed = _ToolingSnapshotEntry(
                    entry.context_key,
                    entry.needs,
                    entry.result,
                    current_signature,
                    entry.environment_fingerprint,
                    entry.root_inventory,
                )
                self._entries[context_key] = refreshed
                if _inventory_matches(entry) and entry.needs & requested == requested:
                    if requested.wants(AnalysisNeeds.DEFINITIONS):
                        self._remember_symbol_snapshot(
                            path, source_text, entry.result, context_key
                        )
                    self._entries.move_to_end(context_key)
                    return entry.result
            self._workspace_indexes.clear()
            requested |= entry.needs

        overlays = self._editor_workspace.overlays_for(path)
        for attempt in range(2):
            try:
                result = check_file_snapshot(
                    path,
                    source_text,
                    source_digest=source_digest,
                    project=project,
                    profile=profile,
                    top=top,
                    analysis_needs=requested,
                    allow_external_enum_inputs=True,
                    allow_unsaved_root=True,
                    source_overlays=overlays,
                    incremental_workspace=self._incremental_workspace,
                )
                break
            except (ModuleResolutionError, WorkspaceError) as error:
                if attempt or not _is_snapshot_race(error):
                    raise
                # A physical input was replaced between discovery and locked
                # snapshot validation.  Rebuild the workspace once from the
                # same immutable editor overlay; a second race propagates as a
                # neutral LSP environment failure instead of stale results.
                self._workspace_indexes.clear()
        stored = _ToolingSnapshotEntry(
            context_key,
            requested,
            result,
            self._environment_signature(result.physical_inputs),
            self._environment_fingerprint(result.physical_inputs),
            _symbol_root_inventory(result.physical_inputs),
        )
        self._entries[context_key] = stored
        self._entries.move_to_end(context_key)
        while len(self._entries) > self._MAX_ENTRIES:
            self._entries.popitem(last=False)
        if requested.wants(AnalysisNeeds.DEFINITIONS):
            self._remember_symbol_snapshot(path, source_text, result, context_key)
        return result

    def trivia_diagnostic_proof(self, source: Path, source_text: str) -> bool:
        """Reuse an earlier *clean* check without returning stale typed IR."""

        path = Path(source).expanduser().resolve()
        previous = self._trivia_checks.get(path)
        if previous is None:
            return False
        old_text, entry = previous
        if entry.context_key[6] is not None:
            # didChange clears a navigation-only selected top.  A proof for
            # that child is not a proof that the default public top is valid.
            return False
        inputs = entry.result.physical_inputs
        editor_identity = self._editor_workspace.identity_for(path)
        old_editor = tuple(item for item in entry.context_key[-1]
                           if item[0] != path.as_posix())
        new_editor = tuple(item for item in editor_identity
                           if item[0] != path.as_posix())
        if old_editor != new_editor:
            return False
        try:
            old_other = tuple(item for item in entry.environment_fingerprint
                              if item[0] != path.as_posix())
            new_other = tuple(item for item in self._environment_fingerprint(inputs)
                              if item[0] != path.as_posix())
            return (
                old_other == new_other
                and entry.root_inventory == _symbol_root_inventory(inputs)
                and TriviaRebinding.between(old_text, source_text) is not None
            )
        except (OSError, ProjectModelError, ToolingError, ValueError):
            return False

    def symbol_snapshot(
        self,
        source: Path | str,
        source_text: str,
        *,
        project: Path | str | None = None,
        profile: str | None = None,
        top: str | None = None,
    ) -> _SymbolSnapshot | None:
        """Return a validated symbol-only memory or persistent cache hit."""

        if self._symbol_mode == "off":
            return None
        path = Path(source).expanduser().resolve()
        editor_identity = self._editor_workspace.identity_for(path)
        context_key = self._context_key(
            path,
            source_text,
            source_digest=None,
            project=project,
            profile=profile,
            top=top,
            editor_identity=editor_identity,
        )
        entry = self._symbol_entries.get(context_key)
        if entry is not None and not _inventory_matches(entry):
            self._drop_symbol_entry(context_key)
            entry = None
        if entry is not None:
            signature = self._environment_signature(entry.result.physical_inputs)
            if signature == entry.environment_signature:
                self._symbol_entries.move_to_end(context_key)
                return entry.result
            fingerprint = self._environment_fingerprint(
                entry.result.physical_inputs
            )
            if fingerprint == entry.environment_fingerprint:
                refreshed = _ToolingSymbolEntry(
                    context_key,
                    entry.result,
                    signature,
                    fingerprint,
                    entry.root_inventory,
                )
                self._symbol_entries[context_key] = refreshed
                self._symbol_entries.move_to_end(context_key)
                return entry.result
            self._drop_symbol_entry(context_key)
        rebased = self._rebind_trivia_symbols(
            path, source_text, context_key, editor_identity
        )
        if rebased is not None:
            self._insert_symbol_entry(context_key, rebased)
            return rebased
        if self._symbol_mode != "persistent" or editor_identity:
            return None
        snapshot = _load_persistent_symbol_snapshot(
            path,
            source_text,
            project=project,
            profile=profile,
            top=top,
        )
        if snapshot is None:
            return None
        try:
            self._insert_symbol_entry(context_key, snapshot)
        except (OSError, ProjectModelError, ToolingError):
            return None
        return snapshot

    def _rebind_trivia_symbols(
        self,
        source: Path,
        source_text: str,
        context_key: tuple[object, ...],
        editor_identity: tuple[tuple[str, str], ...],
    ) -> _SymbolSnapshot | None:
        previous = self._trivia_symbols.get(source)
        if previous is None:
            return None
        old_text, entry = previous
        old_key = entry.context_key
        if any(old_key[index] != context_key[index] for index in (0, 4, 5, 6, 7, 8)):
            return None
        remaining_old = tuple(item for item in old_key[-1] if item[0] != source.as_posix())
        remaining_new = tuple(item for item in editor_identity if item[0] != source.as_posix())
        if remaining_old != remaining_new:
            return None
        inputs = entry.result.physical_inputs
        try:
            current = self._environment_fingerprint(inputs)
            old_other = tuple(item for item in entry.environment_fingerprint if item[0] != source.as_posix())
            new_other = tuple(item for item in current if item[0] != source.as_posix())
            if old_other != new_other or entry.root_inventory != _symbol_root_inventory(inputs):
                return None
            mapping = TriviaRebinding.between(old_text, source_text)
            if mapping is None:
                return None
            root_units = {
                unit for unit, path in inputs.source_unit_paths if path == source
            }
            old_digest = hashlib.sha256(old_text.encode("utf-8")).hexdigest()

            def origin(value: SourceOrigin) -> SourceOrigin:
                if value.source_unit is None and inputs.project_manifest is not None:
                    raise ValueError("project symbol origin has no source unit")
                if value.source_unit not in root_units and value.source_unit is not None:
                    return value
                if value.digest != old_digest:
                    raise ValueError("symbol origin is not bound to the old root snapshot")
                updated = mapping.origin(value)
                if updated is None:
                    raise ValueError("symbol span is not anchored to a parser token")
                return updated

            resolutions = tuple(replace(item,
                occurrence=origin(item.occurrence), target=origin(item.target))
                for item in entry.result.definition_resolutions)
            declarations = tuple(replace(item, target=origin(item.target))
                                 for item in entry.result.definition_declarations)
            return _SymbolSnapshot(inputs, resolutions, declarations,
                                   entry.result.serialized_size)
        except (OSError, ProjectModelError, ToolingError, ValueError):
            return None

    def symbol_snapshot_covering(
        self,
        source: Path | str,
        source_text: str,
        *,
        required_module: str,
    ) -> _SymbolSnapshot | None:
        """Return a valid shard that semantically observed this exact source.

        A root hierarchy analysis already records declarations and occurrences
        in imported/instantiated child units.  Reuse that compiler evidence for
        an opened definition target instead of compiling the child again for
        semantic tokens.  Cross-root reuse is saved-file-only and requires the
        child digest plus the complete originating shard environment to match.
        """

        if self._symbol_mode == "off" or not required_module:
            return None
        path = Path(source).expanduser().resolve()
        try:
            digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
            if _digest_file(path) != digest:
                return None
        except OSError:
            return None

        for context_key in reversed(tuple(self._symbol_entries)):
            entry = self._symbol_entries.get(context_key)
            if entry is None:
                continue
            if not _inventory_matches(entry):
                self._drop_symbol_entry(context_key)
                continue
            signature = self._environment_signature(entry.result.physical_inputs)
            if signature != entry.environment_signature:
                fingerprint = self._environment_fingerprint(
                    entry.result.physical_inputs
                )
                if fingerprint != entry.environment_fingerprint:
                    self._drop_symbol_entry(context_key)
                    continue
                entry = _ToolingSymbolEntry(
                    context_key,
                    entry.result,
                    signature,
                    fingerprint,
                    entry.root_inventory,
                )
                self._symbol_entries[context_key] = entry

            units = {
                unit
                for unit, candidate in entry.result.physical_inputs.source_unit_paths
                if candidate.resolve() == path
            }
            if entry.result.physical_inputs.root_source.resolve() == path:
                root_unit = _root_logical_source(path, self)
                if root_unit is not None:
                    units.add(root_unit)
            if not units:
                continue
            observed = tuple(
                origin
                for origin in (
                    *(
                        declaration.target
                        for declaration in entry.result.definition_declarations
                    ),
                    *(
                        resolution.occurrence
                        for resolution in entry.result.definition_resolutions
                    ),
                )
                if origin.source_unit in units
            )
            if not observed or any(origin.digest != digest for origin in observed):
                continue
            if not any(
                declaration.kind == "module"
                and declaration.name == required_module
                and declaration.target.source_unit in units
                and declaration.target.digest == digest
                for declaration in entry.result.definition_declarations
            ):
                continue
            self._symbol_entries.move_to_end(context_key)
            return entry.result
        return None

    def _drop_symbol_entry(self, key: tuple[object, ...]) -> None:
        entry = self._symbol_entries.pop(key, None)
        if entry is not None:
            self._symbol_bytes -= entry.result.serialized_size

    def _insert_symbol_entry(
        self,
        context_key: tuple[object, ...],
        snapshot: _SymbolSnapshot,
    ) -> None:
        self._drop_symbol_entry(context_key)
        entry = _ToolingSymbolEntry(
            context_key,
            snapshot,
            self._environment_signature(snapshot.physical_inputs),
            self._environment_fingerprint(snapshot.physical_inputs),
            _symbol_root_inventory(snapshot.physical_inputs),
        )
        self._symbol_entries[context_key] = entry
        self._symbol_entries.move_to_end(context_key)
        self._symbol_bytes += snapshot.serialized_size
        while self._symbol_entries and (
            len(self._symbol_entries) > _SYMBOL_MEMORY_MAX_ENTRIES
            or self._symbol_bytes > _SYMBOL_MEMORY_MAX_BYTES
        ):
            oldest_key = next(iter(self._symbol_entries))
            self._drop_symbol_entry(oldest_key)

    def _remember_symbol_snapshot(
        self,
        source: Path,
        source_text: str,
        result: object,
        context_key: tuple[object, ...],
    ) -> None:
        if self._symbol_mode == "off":
            return
        existing = self._symbol_entries.get(context_key)
        if existing is not None:
            self._symbol_entries.move_to_end(context_key)
            return
        try:
            snapshot, payload = _normalized_symbol_payload(source, result, self)
            self._insert_symbol_entry(context_key, snapshot)
            if self._symbol_mode == "persistent":
                overlays = getattr(
                    result.physical_inputs, "editor_source_overlays", ()
                )
                saved = all(
                    _digest_file(Path(path)) == digest
                    for path, digest in overlays
                )
                if saved:
                    _publish_persistent_symbol_snapshot(
                        source,
                        source_text,
                        payload,
                        project=context_key[4],
                        profile=context_key[5],
                        top=context_key[6],
                    )
        except (AttributeError, KeyError, OSError, TypeError, ValueError):
            # Navigation remains available from the just-produced semantic
            # result even when the optional cache cannot normalize or publish.
            return

    def invalidate(self, source: Path | str) -> None:
        """Drop all snapshots rooted at one document path."""

        path = Path(source).expanduser().resolve()
        old_text = self.editor_text_for(path)
        if old_text is not None:
            for key, entry in reversed(tuple(self._entries.items())):
                if key[0] == path and key[1] == hashlib.sha256(
                    old_text.encode("utf-8")
                ).hexdigest():
                    self._trivia_checks[path] = (old_text, entry)
                    self._trivia_checks.move_to_end(path)
                    while len(self._trivia_checks) > self._MAX_ENTRIES:
                        self._trivia_checks.popitem(last=False)
                    break
            for key, entry in reversed(tuple(self._symbol_entries.items())):
                if key[0] == path and key[1] == hashlib.sha256(
                    old_text.encode("utf-8")
                ).hexdigest():
                    self._trivia_symbols[path] = (old_text, entry)
                    self._trivia_symbols.move_to_end(path)
                    while len(self._trivia_symbols) > self._MAX_ENTRIES:
                        self._trivia_symbols.popitem(last=False)
                    break
        for key, entry in tuple(self._entries.items()):
            inputs = getattr(entry.result, "physical_inputs", None)
            paths = {
                Path(item).expanduser().resolve()
                for item in getattr(inputs, "all_paths", ())
            }
            if key[0] == path or path in paths:
                del self._entries[key]
        for key, entry in tuple(self._symbol_entries.items()):
            paths = {
                Path(item).expanduser().resolve()
                for item in getattr(entry.result.physical_inputs, "all_paths", ())
            }
            if key[0] == path or path in paths:
                self._drop_symbol_entry(key)
        for key in tuple(self._navigation_regions):
            if key[0] == path:
                del self._navigation_regions[key]
        self._workspace_indexes.clear()

    def clear(self) -> None:
        """Release all session-owned snapshots."""

        self._incremental_workspace.clear()
        self._entries.clear()
        self._symbol_entries.clear()
        self._trivia_symbols.clear()
        self._trivia_checks.clear()
        self._symbol_bytes = 0
        self._navigation_regions.clear()
        self._workspace_indexes.clear()


@dataclass(frozen=True)
class ToolingIdentity:
    api_schema: int
    compiler_version: str
    source_suffix: str
    capability_schema: int
    capability_identity: str
    capability_count: int


@dataclass(frozen=True)
class SourceFacts:
    imports: tuple[str, ...]
    declarations: tuple[str, ...]
    parsed: bool


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
        return source_path_for_logical_unit(
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


@dataclass(frozen=True)
class ToolingOrigin:
    source_unit: str | None
    construct: str | None
    start_line: int
    start_column: int
    end_line: int
    end_column: int


@dataclass(frozen=True)
class ToolingHover:
    """Narrow compiler-owned facts for one semantic hover result."""

    name: str | None
    kind: str
    type_text: str | None
    width: int | None
    signedness: str | None
    fixed_point: str | None
    port_direction: str | None
    signature: str | None
    origin: ToolingOrigin | None

    def __post_init__(self) -> None:
        if not self.kind:
            raise ValueError("tooling hover kind must not be empty")


@dataclass(frozen=True)
class ToolingDefinition:
    """Narrow compiler-owned target projection for go-to-definition."""

    name: str
    kind: str
    target_path: Path
    target_origin: ToolingOrigin

    def __post_init__(self) -> None:
        if not self.name or not self.kind:
            raise ValueError("tooling definition identity must not be empty")
        if not self.target_path.is_absolute():
            raise ValueError("tooling definition path must be absolute")


@dataclass(frozen=True)
class ToolingReference:
    """One compiler-owned reference occurrence projected for an editor."""

    source_path: Path
    origin: ToolingOrigin

    def __post_init__(self) -> None:
        if not self.source_path.is_absolute():
            raise ValueError("tooling reference path must be absolute")


@dataclass(frozen=True)
class ToolingRenameEdit:
    """One exact compiler-owned source edit for a safe rename."""

    source_path: Path
    origin: ToolingOrigin
    new_text: str

    def __post_init__(self) -> None:
        if not self.source_path.is_absolute():
            raise ValueError("tooling rename path must be absolute")
        if not self.new_text:
            raise ValueError("tooling rename text must not be empty")


@dataclass(frozen=True)
class ToolingCompletion:
    """One compiler-visible semantic completion candidate."""

    name: str
    kind: str
    detail: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.kind:
            raise ValueError("tooling completion identity must not be empty")


@dataclass(frozen=True)
class ToolingSignatureHelp:
    """One compiler-resolved callable signature for an editor position."""

    label: str
    parameters: tuple[str, ...]
    active_parameter: int
    origin: ToolingOrigin | None

    def __post_init__(self) -> None:
        if not self.label:
            raise ValueError("tooling signature label must not be empty")
        if (
            isinstance(self.active_parameter, bool)
            or not isinstance(self.active_parameter, int)
            or self.active_parameter < 0
            or (
                self.parameters
                and self.active_parameter >= len(self.parameters)
            )
        ):
            raise ValueError("tooling signature active parameter is invalid")


_SEMANTIC_TOKEN_KINDS = frozenset(
    {"function", "parameter", "property", "variable"}
)
_SEMANTIC_TOKEN_MODIFIERS = frozenset({"declaration"})


@dataclass(frozen=True)
class ToolingSemanticToken:
    """One exact compiler-owned semantic source occurrence.

    Token kinds and modifiers are protocol-independent names.  Numeric LSP
    legend indices and relative encoding remain owned by ``zlang.lsp``.
    """

    origin: ToolingOrigin
    kind: str
    modifiers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in _SEMANTIC_TOKEN_KINDS:
            raise ValueError(f"unsupported tooling semantic token kind: {self.kind}")
        if any(item not in _SEMANTIC_TOKEN_MODIFIERS for item in self.modifiers):
            raise ValueError("unsupported tooling semantic token modifier")
        if len(set(self.modifiers)) != len(self.modifiers):
            raise ValueError("tooling semantic token modifiers must be unique")
        if (
            self.origin.start_line != self.origin.end_line
            or self.origin.end_column <= self.origin.start_column
        ):
            raise ValueError("tooling semantic token requires an exact single-line span")


def _origin_from_source(
    origin: object | None,
    *,
    construct: str | None = None,
) -> ToolingOrigin | None:
    if origin is None:
        return None
    span = getattr(origin, "span", origin)
    if not all(
        hasattr(span, attribute)
        for attribute in ("start_line", "start_column", "end_line", "end_column")
    ):
        return None
    return ToolingOrigin(
        getattr(origin, "source_unit", None),
        construct if construct is not None else getattr(origin, "construct", None),
        int(span.start_line),
        int(span.start_column),
        int(span.end_line),
        int(span.end_column),
    )


def _type_metadata(
    type_value: object,
) -> tuple[str, int | None, str | None, str | None]:
    type_text = str(type_value)
    width = getattr(type_value, "width", None)
    width = int(width) if isinstance(width, int) else None
    if isinstance(type_value, (SIntType, FixedType)):
        signedness = "signed"
    elif isinstance(type_value, (UIntType, UFixedType)):
        signedness = "unsigned"
    elif isinstance(type_value, BitsType):
        signedness = "bit-vector"
    elif isinstance(type_value, BitType):
        signedness = "bit"
    else:
        signedness = None
    fixed_point = (
        type_text if isinstance(type_value, (FixedType, UFixedType)) else None
    )
    return type_text, width, signedness, fixed_point


def _hover_record(
    *,
    name: str | None,
    kind: str,
    type_value: object | None,
    origin: object | None,
    port_direction: str | None = None,
    signature: str | None = None,
) -> ToolingHover:
    if type_value is None:
        type_text = width = signedness = fixed_point = None
    else:
        type_text, width, signedness, fixed_point = _type_metadata(type_value)
    return ToolingHover(
        name,
        kind,
        type_text,
        width,
        signedness,
        fixed_point,
        port_direction,
        signature,
        _origin_from_source(origin),
    )


@dataclass(frozen=True)
class ToolingDiagnostic:
    code: str
    message: str
    primary: ToolingOrigin | None
    notes: tuple[str, ...]
    fixes: tuple[str, ...]
    machine_fixes: tuple[ToolingDiagnosticFix, ...] = ()
    severity: str = "error"

    def __post_init__(self) -> None:
        if self.severity not in {"error", "warning"}:
            raise ValueError("tooling diagnostic severity is unsupported")


@dataclass(frozen=True)
class ToolingDiagnosticEdit:
    """One exact current-source edit projected from compiler fix metadata."""

    source_path: Path
    origin: ToolingOrigin
    replacement: str

    def __post_init__(self) -> None:
        if not self.source_path.is_absolute():
            raise ValueError("tooling diagnostic edit path must be absolute")
        if not isinstance(self.replacement, str):
            raise TypeError("tooling diagnostic replacement must be a string")


@dataclass(frozen=True)
class ToolingDiagnosticFix:
    """One atomic machine-applicable compiler diagnostic fix."""

    title: str
    edits: tuple[ToolingDiagnosticEdit, ...]

    def __post_init__(self) -> None:
        if not self.title:
            raise ValueError("tooling diagnostic fix title must not be empty")
        if not self.edits:
            raise ValueError("tooling diagnostic fix requires at least one edit")
        if any(not isinstance(edit, ToolingDiagnosticEdit) for edit in self.edits):
            raise TypeError(
                "tooling diagnostic fix edits must be edit records"
            )


@dataclass(frozen=True)
class ToolingSymbol:
    """Compiler-parser-owned structure projected for document symbols only.

    This is intentionally not an AST or typed-IR export.  ``range`` and
    ``selection_range`` are compiler source origins; when a legacy AST node
    does not retain a narrower origin, the containing declaration origin is
    used rather than estimating character offsets from source text.
    """

    name: str
    kind: str
    range: ToolingOrigin | None
    selection_range: ToolingOrigin | None
    children: tuple["ToolingSymbol", ...] = ()

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("tooling symbol name must not be empty")
        if not self.kind:
            raise ValueError("tooling symbol kind must not be empty")


def _symbol_origin(node: object, kind: str, name: str) -> ToolingOrigin | None:
    return _origin_from_source(
        getattr(node, "origin", None),
        construct=f"{kind} {name}",
    )


def _enclosing_origin(
    name: str,
    kind: str,
    origins: tuple[ToolingOrigin, ...],
) -> ToolingOrigin | None:
    """Build a non-guessing declaration envelope from known child spans."""

    if not origins:
        return None
    start = min(
        origins,
        key=lambda value: (value.start_line, value.start_column),
    )
    end = max(
        origins,
        key=lambda value: (value.end_line, value.end_column),
    )
    return ToolingOrigin(
        None,
        f"{kind} {name}",
        start.start_line,
        start.start_column,
        end.end_line,
        end.end_column,
    )


def _make_symbol(
    name: str,
    kind: str,
    node: object | None = None,
    *,
    fallback: ToolingOrigin | None = None,
    children: tuple[ToolingSymbol, ...] = (),
) -> ToolingSymbol:
    origin = (
        None
        if node is None
        else _symbol_origin(node, kind, name)
    ) or fallback
    return ToolingSymbol(name, kind, origin, origin, children)


def _children_for_struct(
    fields: tuple[object, ...], fallback: ToolingOrigin | None
) -> tuple[ToolingSymbol, ...]:
    return tuple(
        _make_symbol(
            str(getattr(field, "name")),
            "field",
            field,
            fallback=fallback,
        )
        for field in fields
    )


def _children_for_enum(
    members: tuple[str, ...], fallback: ToolingOrigin | None
) -> tuple[ToolingSymbol, ...]:
    return tuple(
        _make_symbol(member, "enum_member", fallback=fallback)
        for member in members
    )


def _symbol_for_item(
    item: object,
    fallback: ToolingOrigin | None,
) -> tuple[ToolingSymbol, ...]:
    """Project one parser-owned declaration without source-text heuristics."""

    if isinstance(item, tuple):
        # Clock/reset declarations retain their physical declaration object in
        # the ordered parser item.  Other tagged tuples are not named symbols.
        if len(item) >= 3 and item[0] in {"clock", "reset"}:
            declaration = item[-1]
            return (
                _make_symbol(str(item[1]), "field", declaration, fallback=fallback),
            )
        return ()
    if isinstance(item, ast_nodes.PortDecl):
        names = item.names or (item.name,)
        return tuple(
            _make_symbol(name, "field", item, fallback=fallback)
            for name in names
        )
    if isinstance(item, ast_nodes.TypeAlias):
        return (_make_symbol(item.name, "type", item, fallback=fallback),)
    if isinstance(item, ast_nodes.StructDecl):
        symbol = _make_symbol(
            item.name,
            "struct",
            item,
            fallback=fallback,
            children=_children_for_struct(item.fields, fallback),
        )
        return (symbol,)
    if isinstance(item, ast_nodes.EnumDecl):
        return (_make_symbol(
            item.name,
            "enum",
            item,
            fallback=fallback,
            children=_children_for_enum(item.members, fallback),
        ),)
    if isinstance(item, ast_nodes.TaggedUnionDecl):
        children = tuple(
            _make_symbol(
                variant.name,
                "struct",
                variant,
                fallback=fallback,
                children=_children_for_struct(variant.fields, fallback),
            )
            for variant in item.variants
        )
        return (_make_symbol(
            item.name, "class", item, fallback=fallback, children=children
        ),)
    if isinstance(item, ast_nodes.FunctionDecl):
        return (_make_symbol(item.name, "function", item, fallback=fallback),)
    if isinstance(item, ast_nodes.OperatorDecl):
        return (_make_symbol(
            item.operator, "operator", item, fallback=fallback
        ),)
    if isinstance(item, ast_nodes.ModuleInterfaceDecl):
        children = tuple(
            _make_symbol(port.name, "field", port, fallback=fallback)
            for port in item.ports
            for _ in (0,)
        )
        return (_make_symbol(
            item.name, "interface", item, fallback=fallback, children=children
        ),)
    if isinstance(item, ast_nodes.ProtocolDecl):
        children = tuple(
            _make_symbol(channel.name, "field", fallback=fallback)
            for channel in item.channels
        )
        return (_make_symbol(
            item.name, "interface", item, fallback=fallback, children=children
        ),)
    if isinstance(item, ast_nodes.ResourceDefinitionDecl):
        children = tuple(
            _make_symbol(port.name, "field", fallback=fallback)
            for port in item.ports
        )
        return (_make_symbol(
            item.name, "class", item, fallback=fallback, children=children
        ),)
    if isinstance(item, ast_nodes.TargetFamilyDecl):
        return (_make_symbol(item.name, "class", item, fallback=fallback),)
    if isinstance(item, ast_nodes.TargetInstanceDecl):
        return (_make_symbol(item.name, "object", item, fallback=fallback),)
    if isinstance(item, ast_nodes.ArchitectureTemplateDecl):
        return (_make_symbol(item.name, "class", item, fallback=fallback),)
    if isinstance(item, ast_nodes.ModuleParameter):
        return (_make_symbol(
            item.name,
            "type_parameter" if item.kind == "type" else "variable",
            item,
            fallback=fallback,
        ),)
    if isinstance(item, ast_nodes.RegisterDecl):
        return (_make_symbol(item.name, "field", item, fallback=fallback),)
    if isinstance(item, ast_nodes.RequestResponseDecl):
        return (_make_symbol(item.name, "field", item, fallback=fallback),)
    if isinstance(item, ast_nodes.CsrBlockDecl):
        children = tuple(
            _make_symbol(
                register.name,
                "field",
                register,
                fallback=fallback,
                children=tuple(
                    _make_symbol(field.name, "field", field, fallback=fallback)
                    for field in register.fields
                ),
            )
            for register in item.registers
        )
        return (_make_symbol(
            item.name, "namespace", item, fallback=fallback, children=children
        ),)
    if isinstance(item, ast_nodes.RuleDecl):
        return (_make_symbol(item.name, "method", item, fallback=fallback),)
    if isinstance(item, ast_nodes.FsmDecl):
        return (_make_symbol(
            item.name,
            "class",
            item,
            fallback=fallback,
            children=tuple(
                _make_symbol(state.member, "enum_member", state, fallback=fallback)
                for state in item.states
            ),
        ),)
    if isinstance(item, ast_nodes.FifoDecl):
        return (_make_symbol(item.name, "field", item, fallback=fallback),)
    if isinstance(item, (ast_nodes.MemoryDecl, ast_nodes.RomDecl)):
        return (_make_symbol(item.name, "field", item, fallback=fallback),)
    # ArbiterDecl carries source semantics but intentionally has no declaration
    # name in the AST (its destination is a connection endpoint, not a symbol
    # identity).  Do not invent a name for document symbols.
    if isinstance(item, ast_nodes.InstanceDecl):
        return (_make_symbol(item.name, "object", item, fallback=fallback),)
    if isinstance(item, ast_nodes.AggregateInterfaceDecl):
        return (_make_symbol(item.name, "interface", item, fallback=fallback),)
    if isinstance(item, ast_nodes.GenericDeclaration):
        return (_make_symbol(item.name, "object", item, fallback=fallback),)
    if isinstance(item, ast_nodes.VerificationScopeDecl):
        children = tuple(
            _make_symbol(goal.name, "event", goal, fallback=fallback)
            for goal in item.goals
        )
        return (_make_symbol(
            item.name, "namespace", item, fallback=fallback, children=children
        ),)
    if isinstance(item, ast_nodes.VerificationGoalDecl):
        return (_make_symbol(item.name, "event", item, fallback=fallback),)
    if isinstance(item, ast_nodes.EquivDecl):
        return (_make_symbol(item.name, "event", item, fallback=fallback),)
    if isinstance(item, ast_nodes.Assignment):
        return (_make_symbol(item.target, "variable", item, fallback=fallback),)
    if isinstance(item, ast_nodes.ModuleTimingDecl):
        return ()
    return ()


def _module_symbols(module: ast_nodes.Module) -> tuple[ToolingSymbol, ...]:
    if module.ordered_items:
        items: tuple[object, ...] = (
            *(module.declared_parameters or module.parameters),
            *module.ordered_items,
        )
    else:
        # Declaration-only units use the parser's category carriers instead of
        # a module body.  Keep this fallback structural and deterministic; it
        # does not inspect source text or recreate grammar rules.
        items = (
            *(module.declared_parameters or module.parameters),
            *module.type_aliases,
            *module.enums,
            *module.tagged_unions,
            *module.structs,
            *module.functions,
            *module.operators,
            *module.protocols,
            *module.resource_definitions,
            *module.target_families,
            *module.target_instances,
            *module.architecture_templates,
            *module.module_interfaces,
        )
    known_origins = tuple(
        origin
        for item in items
        for origin in (
            _symbol_origin(item, type(item).__name__, str(getattr(item, "name", "")))
            if not isinstance(item, tuple)
            else None,
        )
        if origin is not None
    )
    module_origin = _enclosing_origin(module.name, "module", known_origins)
    if items:
        children: list[ToolingSymbol] = []
        for item in items:
            if isinstance(item, ast_nodes.Assignment):
                declared = {
                    name
                    for port in module.ports
                    for name in (port.names or (port.name,))
                }
                declared.update(
                    str(getattr(value, "name"))
                    for value in (
                        *module.registers,
                        *module.fifos,
                        *module.memories,
                        *module.roms,
                        *module.instances,
                    )
                )
                if item.target.split(".", 1)[0] in declared:
                    continue
            children.extend(_symbol_for_item(item, module_origin))
    else:
        children = []
    return (
        _make_symbol(
            module.name,
            "module",
            fallback=module_origin,
            children=tuple(children),
        ),
    )


def document_symbols(source_text: str) -> tuple[ToolingSymbol, ...]:
    """Return parser-owned document symbols for the current source text.

    Parse failures intentionally return an empty result.  Diagnostics remain
    the existing ``check_snapshot`` responsibility; no fallback scanner is
    used while a document is incomplete.
    """

    try:
        syntax = parse(source_text)
    except ParseError:
        return ()
    if syntax.declaration_only:
        carrier = _module_symbols(syntax)[0]
        symbols = list(carrier.children)
    else:
        symbols = list(_module_symbols(syntax))
    symbols.extend(
        symbol for item in syntax.submodules for symbol in _module_symbols(item)
    )
    return tuple(symbols)


def _position_in_origin(origin: ToolingOrigin | None, line: int, character: int) -> bool:
    if origin is None:
        return False
    position = (line + 1, character + 1)
    return (origin.start_line, origin.start_column) <= position < (
        origin.end_line,
        origin.end_column,
    )


def _indexed_symbol_line(
    index: Mapping[tuple[str | None, int], tuple[object, ...]],
    source_unit: str | None,
    line: int,
) -> tuple[object, ...]:
    if source_unit is not None:
        return index.get((source_unit, line), ())
    return tuple(
        item
        for (unit, indexed_line), records in index.items()
        if indexed_line == line
        for item in records
    )


def _semantic_snapshot(
    source: Path,
    source_text: str,
    *,
    analysis_needs: AnalysisNeeds,
    session: ToolingSession | None,
    source_digest: str | None = None,
    project: Path | str | None = None,
    profile: str | None = None,
    top: str | None = None,
) -> object:
    """Use a session cache when supplied, otherwise preserve direct behavior."""

    if session is not None:
        return session.semantic_snapshot(
            source,
            source_text,
            analysis_needs=analysis_needs,
            source_digest=source_digest,
            project=project,
            profile=profile,
            top=top,
        )
    return check_file_snapshot(
        source,
        source_text,
        source_digest=source_digest,
        project=project,
        profile=profile,
        top=top,
        analysis_needs=analysis_needs,
        allow_external_enum_inputs=True,
        allow_unsaved_root=True,
    )


def _definition_snapshot(
    source: Path,
    source_text: str,
    *,
    session: ToolingSession | None,
    project: Path | str | None = None,
    profile: str | None = None,
    top: str | None = None,
) -> object:
    """Prefer the normalized symbol cache before requesting typed analysis."""

    if session is not None:
        cached = session.symbol_snapshot(
            source,
            source_text,
            project=project,
            profile=profile,
            top=top,
        )
        if cached is not None:
            return cached
    return _semantic_snapshot(
        source,
        source_text,
        analysis_needs=AnalysisNeeds.DEFINITIONS,
        session=session,
        project=project,
        profile=profile,
        top=top,
    )


def _root_logical_source(
    source: Path,
    session: ToolingSession | None = None,
) -> str | None:
    """Return the locked logical identity for the current root when known."""

    try:
        location = discover_project(source)
        if location is None:
            return None
        index = (
            session._workspace_index_for(location.manifest_path)
            if session is not None
            else workspace_index(location.manifest_path)
        )
        resolved = source.expanduser().resolve()
        for module in (*index.root_modules, *index.dependency_modules):
            if module.source_path == resolved:
                return module.logical_path
    except (ToolingError, OSError, ValueError):
        return None
    return None


def _source_path_for_origin(
    source: Path,
    origin: object,
    physical_inputs: object,
    session: ToolingSession | None = None,
) -> Path | None:
    """Map a compiler logical source identity to its locked physical path."""

    unit = getattr(origin, "source_unit", None)
    if unit is None:
        root = getattr(physical_inputs, "root_source", None)
        return Path(root).resolve() if root is not None else source.resolve()
    cached_paths = dict(getattr(physical_inputs, "source_unit_paths", ()))
    if unit in cached_paths:
        return Path(cached_paths[unit]).resolve()
    root = getattr(physical_inputs, "root_source", None)
    if root is not None and Path(root).resolve() == source.resolve():
        root_unit = _root_logical_source(source, session)
        if root_unit == unit or (
            root_unit is None
            and unit in {source.name, source.as_posix(), str(source.resolve())}
        ):
            return source.resolve()
    try:
        location = discover_project(source)
        if location is not None:
            index = (
                session._workspace_index_for(location.manifest_path)
                if session is not None
                else workspace_index(location.manifest_path)
            )
            candidate = index.source_path_for_unit(unit)
            if candidate is not None:
                return candidate
            # ``WorkspaceIndex`` intentionally exposes project/dependency
            # modules only.  Declaration targets may also live in the exact
            # stdlib closure retained by ``PhysicalCompilationInputs``.  Ask
            # the same resolver that built the compilation, then accept the
            # answer only when it is one of those locked physical inputs.
            resolved = resolve_direct_imports(source, (unit,))
            candidate = resolved[0].source_path if resolved else None
            if candidate is not None:
                candidate = candidate.expanduser().resolve()
                locked = {
                    Path(path).expanduser().resolve()
                    for path in getattr(physical_inputs, "all_paths", ())
                }
                if candidate in locked:
                    return candidate
    except (ToolingError, OSError, ValueError):
        return None
    return None


def _definition_target_path(
    source: Path,
    resolution: DefinitionResolution,
    physical_inputs: object,
    session: ToolingSession | None = None,
) -> Path | None:
    return _source_path_for_origin(
        source, resolution.target, physical_inputs, session
    )


def _definition_from_result(
    source: Path,
    result: object,
    line: int,
    character: int,
    session: ToolingSession | None = None,
) -> ToolingDefinition | None:
    root_unit = _root_logical_source(source, session)
    candidates: list[tuple[tuple[int, int, int], DefinitionResolution]] = []
    indexed = getattr(result, "occurrences_by_line", None)
    resolutions = (
        _indexed_symbol_line(indexed, root_unit, line + 1)
        if indexed is not None
        else result.definition_resolutions
    )
    for index, resolution in enumerate(resolutions):
        occurrence = _origin_from_source(resolution.occurrence)
        if occurrence is None:
            continue
        if (
            root_unit is not None
            and resolution.occurrence.source_unit not in {None, root_unit}
        ):
            continue
        if not _position_in_origin(occurrence, line, character):
            continue
        line_span = occurrence.end_line - occurrence.start_line
        column_span = (
            occurrence.end_column - occurrence.start_column
            if line_span == 0 else 1_000_000
        )
        candidates.append(((line_span, column_span, index), resolution))
    if not candidates:
        return None
    resolution = min(candidates, key=lambda item: item[0])[1]
    target_path = _definition_target_path(
        source, resolution, result.physical_inputs, session
    )
    if target_path is None:
        raise ToolingError(
            "compiler resolved a definition whose locked source path is unavailable"
        )
    target_origin = _origin_from_source(resolution.target)
    if target_origin is None:
        raise ToolingError("compiler resolved a definition without a source origin")
    return ToolingDefinition(
        resolution.name,
        resolution.kind,
        target_path,
        target_origin,
    )


def _definition_lookup_columns(
    source_text: str,
    line: int,
    character: int,
) -> tuple[int, ...]:
    """Return the exact column and a safe identifier-edge fallback.

    LSP positions are insertion points.  VS Code can send the exclusive right
    edge of a selected identifier, while compiler source origins are correctly
    half-open.  Only that immediate identifier boundary is retried; arbitrary
    whitespace and punctuation positions still resolve to no definition.
    """

    columns = [character]
    lines = source_text.splitlines()
    if not (0 <= line < len(lines)):
        return tuple(columns)
    text = lines[line]
    if not (0 < character <= len(text)):
        return tuple(columns)
    previous = text[character - 1]
    current = text[character] if character < len(text) else None
    previous_is_identifier = previous == "_" or previous.isalnum()
    current_is_identifier = (
        current is not None and (current == "_" or current.isalnum())
    )
    if previous_is_identifier and not current_is_identifier:
        columns.append(character - 1)
    return tuple(columns)


def _hover_specificity(origin: ToolingOrigin) -> tuple[int, int]:
    line_span = origin.end_line - origin.start_line
    column_span = (
        origin.end_column - origin.start_column
        if line_span == 0
        else 1_000_000
    )
    return line_span, column_span


def _callable_signature(function: object) -> str:
    metadata = getattr(function, "metadata", None)
    kind = "operator" if getattr(getattr(metadata, "kind", None), "value", None) == "operator" else "fn"
    source_name = getattr(metadata, "source_name", None) or getattr(function, "name")
    parameters = ", ".join(
        f"{parameter.name} : {parameter.type}"
        for parameter in getattr(function, "parameters", ())
    )
    return f"{kind} {source_name}({parameters}) -> {function.return_type}"


def _expression_hover(
    value: ir_expressions.TracedExpression,
    callables: dict[str, object],
) -> ToolingHover:
    name: str | None = None
    kind = "expression"
    signature: str | None = None
    if isinstance(value, ir_expressions.InputRef):
        name, kind = value.name, "value"
    elif isinstance(value, ir_expressions.ParameterRef):
        name, kind = value.name, "parameter"
    elif isinstance(value, ir_expressions.RegisterRef):
        name, kind = value.name, "register"
    elif isinstance(value, ir_expressions.Constant):
        kind = "value"
    elif isinstance(value, ir_expressions.ReadyValidRef):
        name, kind = f"{value.interface}.{value.signal.value}", "protocol"
    elif isinstance(value, ir_expressions.RequestResponseRef):
        name, kind = f"{value.interface}.{value.signal.value}", "protocol"
    elif isinstance(value, ir_expressions.CreditRef):
        name, kind = f"{value.interface}.{value.signal.value}", "protocol"
    elif isinstance(value, ir_expressions.PacketRef):
        name, kind = f"{value.interface}.{value.signal.value}", "protocol"
    elif isinstance(value, ir_expressions.VirtualChannelCreditRef):
        name, kind = f"{value.interface}.{value.signal.value}", "protocol"
    elif isinstance(value, ir_expressions.FifoRef):
        name, kind = value.fifo, "fifo"
    elif isinstance(value, ir_expressions.MemoryRef):
        name, kind = value.memory, "memory"
    elif isinstance(value, ir_expressions.RomRef):
        name, kind = value.rom, "rom"
    elif isinstance(value, ir_expressions.InstanceOutputRef):
        name, kind = f"{value.instance}.{value.port}", "instance_output"
    elif isinstance(value, ir_expressions.FieldAccess):
        name, kind = value.field, "field"
    elif isinstance(value, ir_expressions.Call):
        callable_value = (
            callables.get(value.callee_identity)
            if value.callee_identity is not None
            else None
        )
        if callable_value is not None:
            name = getattr(getattr(callable_value, "metadata", None), "source_name", None)
            name = name or getattr(callable_value, "name")
            signature = _callable_signature(callable_value)
            kind = (
                "operator"
                if signature.startswith("operator ")
                else "function"
            )
        else:
            name, kind = value.function, "function"
    elif isinstance(value, ir_expressions.Add):
        name, kind = "+", "operator"
    elif isinstance(value, ir_expressions.Binary):
        name, kind = value.operator.value, "operator"
    elif isinstance(value, ir_expressions.Dot):
        name = "dot"
    elif isinstance(value, ir_expressions.Reduce):
        name = "reduce"
    elif isinstance(value, ir_expressions.FixedConvert):
        name = "fixed conversion"
    elif isinstance(value, ir_expressions.Mux):
        name = "mux"
    return _hover_record(
        name=name,
        kind=kind,
        type_value=getattr(value, "type", None),
        origin=getattr(value, "origin", None),
        signature=signature,
    )


def _walk_expressions(value: object) -> tuple[ir_expressions.TracedExpression, ...]:
    result: list[ir_expressions.TracedExpression] = []

    def visit(current: object) -> None:
        if not isinstance(current, ir_expressions.TracedExpression):
            return
        result.append(current)
        for item in fields(current):
            if item.name in {"origin", "type"}:
                continue
            child = getattr(current, item.name)
            if isinstance(child, ir_expressions.TracedExpression):
                visit(child)
            elif isinstance(child, tuple):
                for nested in child:
                    visit(nested)

    visit(value)
    return tuple(result)


def _module_expression_roots(module: object) -> tuple[object, ...]:
    roots: list[object] = []
    roots.extend(getattr(item, "expression") for item in module.assignments)
    roots.extend(
        getattr(item, "expression")
        for item in getattr(module, "next_assignments", ())
    )
    roots.extend(
        getattr(item, "initial") for item in getattr(module, "registers", ())
    )
    roots.extend(
        getattr(item, "expression") for item in getattr(module, "locals", ())
    )
    roots.extend(getattr(item, "body") for item in module.functions)
    roots.extend(
        getattr(item, "body")
        for item in getattr(module, "callable_definitions", ())
    )
    for rule in getattr(module, "rules", ()):
        roots.append(rule.guard)
        roots.extend(action.expression for action in rule.actions)
        roots.extend(
            action.activation
            for action in rule.actions
            if action.activation is not None
        )
    return tuple(roots)


def _module_hover_candidates(
    ast_module: ast_nodes.Module,
    ir_module: object,
    line: int,
    character: int,
) -> list[tuple[tuple[int, int, int], ToolingHover]]:
    candidates: list[tuple[tuple[int, int, int], ToolingHover]] = []

    def add(value: ToolingHover, priority: int) -> None:
        if value.origin is None or not _position_in_origin(
            value.origin, line, character
        ):
            return
        line_span, column_span = _hover_specificity(value.origin)
        candidates.append(((line_span, column_span, priority), value))

    semantic_ports = {
        port.name: port for port in getattr(ir_module, "ports", ())
    }
    for declaration in ast_module.ports:
        names = declaration.names or (declaration.name,)
        for name in names:
            port = semantic_ports.get(name)
            if port is None:
                continue
            add(
                _hover_record(
                    name=name,
                    kind="port",
                    type_value=port.type,
                    origin=declaration.origin,
                    port_direction=port.direction.value,
                ),
                4,
            )

    callables = {
        function.callee_identity: function
        for function in (
            *getattr(ir_module, "functions", ()),
            *getattr(ir_module, "callable_definitions", ()),
        )
    }
    for declaration in (*ast_module.functions, *ast_module.operators):
        function = next(
            (
                value
                for value in callables.values()
                if (
                    isinstance(declaration, ast_nodes.FunctionDecl)
                    and value.name == declaration.name
                )
                or (
                    isinstance(declaration, ast_nodes.OperatorDecl)
                    and getattr(getattr(value, "metadata", None), "source_name", None)
                    == f"operator{declaration.operator}"
                )
            ),
            None,
        )
        if function is None:
            continue
        name = (
            declaration.name
            if isinstance(declaration, ast_nodes.FunctionDecl)
            else declaration.operator
        )
        kind = "function" if isinstance(declaration, ast_nodes.FunctionDecl) else "operator"
        add(
            _hover_record(
                name=name,
                kind=kind,
                type_value=function.return_type,
                origin=declaration.origin,
                signature=_callable_signature(function),
            ),
            5,
        )

    for root in _module_expression_roots(ir_module):
        for expression in _walk_expressions(root):
            add(_expression_hover(expression, callables), 0)
    return candidates


def _hover_from_result(
    result: object,
    line: int,
    character: int,
) -> ToolingHover | None:
    candidates: list[tuple[tuple[int, int, int], ToolingHover]] = []
    ast_module = result.ast
    ir_module = result.ir
    candidates.extend(
        _module_hover_candidates(ast_module, ir_module, line, character)
    )
    for child_ast in getattr(ast_module, "submodules", ()):
        child_ir = next(
            (
                value
                for value in getattr(ir_module, "children", ())
                if getattr(value, "name", None) == child_ast.name
            ),
            None,
        )
        if child_ir is not None:
            candidates.extend(
                _module_hover_candidates(child_ast, child_ir, line, character)
            )
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]


def hover_at(
    source: Path | str,
    source_text: str,
    line: int,
    character: int,
    *,
    _session: ToolingSession | None = None,
) -> ToolingHover | None:
    """Return semantic hover facts for one zero-based source position.

    This function demands only the semantic check product.  Parse and semantic
    failures return no hover; resolver/environment failures remain explicit as
    ``ToolingError`` rather than being turned into a semantic result.
    """

    if (
        isinstance(line, bool)
        or isinstance(character, bool)
        or not isinstance(line, int)
        or not isinstance(character, int)
        or line < 0
        or character < 0
    ):
        return None
    try:
        result = _semantic_snapshot(
            Path(source).expanduser().resolve(),
            source_text,
            analysis_needs=AnalysisNeeds.NONE,
            session=_session,
        )
    except (
        ParseError,
        TopSelectionError,
        SemanticError,
        DiagnosticError,
    ):
        return None
    except (
        ModuleResolutionError,
        WorkspaceError,
        ProjectModelError,
        DependencyModelError,
        OSError,
        ValueError,
    ) as error:
        raise ToolingError(str(error)) from error
    return _hover_from_result(result, line, character)


def _completion_from_result(
    source: Path,
    result: object,
    line: int,
    character: int,
    session: ToolingSession | None = None,
) -> tuple[ToolingCompletion, ...]:
    """Select the most specific compiler-recorded scope at a position."""

    root_unit = _root_logical_source(source, session)
    candidates: list[tuple[tuple[int, int, int], CompletionScope]] = []
    for index, scope in enumerate(result.completion_scopes):
        origin = _origin_from_source(scope.origin)
        if origin is None:
            continue
        if root_unit is not None and scope.origin.source_unit not in {None, root_unit}:
            continue
        if not _position_in_origin(origin, line, character):
            continue
        line_span = origin.end_line - origin.start_line
        column_span = (
            origin.end_column - origin.start_column
            if line_span == 0
            else 1_000_000
        )
        candidates.append(((line_span, column_span, index), scope))
    if not candidates:
        return ()
    selected = min(candidates, key=lambda item: item[0])[1]
    projected: dict[tuple[str, str, str | None], ToolingCompletion] = {}
    for candidate in selected.candidates:
        key = (candidate.name, candidate.kind, candidate.detail)
        projected[key] = ToolingCompletion(
            candidate.name, candidate.kind, candidate.detail
        )
    return tuple(
        sorted(
            projected.values(),
            key=lambda item: (item.name, item.kind, item.detail or ""),
        )
    )


def completion_at(
    source: Path | str,
    source_text: str,
    line: int,
    character: int,
    *,
    _session: ToolingSession | None = None,
) -> tuple[ToolingCompletion, ...]:
    """Return compiler-visible semantic candidates at one source position."""

    if (
        isinstance(line, bool)
        or isinstance(character, bool)
        or not isinstance(line, int)
        or not isinstance(character, int)
        or line < 0
        or character < 0
    ):
        return ()
    path = Path(source).expanduser().resolve()
    try:
        result = _semantic_snapshot(
            path,
            source_text,
            analysis_needs=AnalysisNeeds.DEFINITIONS | AnalysisNeeds.COMPLETION,
            session=_session,
        )
    except (
        ParseError,
        TopSelectionError,
        SemanticError,
        DiagnosticError,
    ):
        return ()
    except (
        ModuleResolutionError,
        WorkspaceError,
        ProjectModelError,
        DependencyModelError,
        OSError,
        ValueError,
    ) as error:
        raise ToolingError(str(error)) from error
    return _completion_from_result(path, result, line, character, _session)


def _position_key(line: int, character: int) -> tuple[int, int]:
    """Convert an editor position to the compiler's one-based coordinates."""

    return line + 1, character + 1


def _signature_active_parameter(
    call: SignatureHelpCall,
    line: int,
    character: int,
) -> int:
    """Select an argument using compiler-owned argument spans only."""

    if not call.parameters:
        return 0
    position = _position_key(line, character)
    known_arguments = [
        _origin_from_source(origin)
        for origin in call.argument_origins
    ]
    for index, origin in enumerate(known_arguments):
        if origin is None:
            continue
        start = (origin.start_line, origin.start_column)
        if _position_in_origin(origin, line, character) or position < start:
            return index
    # A cursor after the final argument and before the closing delimiter is
    # conservatively associated with the final parameter.  This uses the
    # authoritative call/argument spans, never source punctuation parsing.
    return len(call.parameters) - 1


def _signature_help_from_result(
    source: Path,
    result: object,
    line: int,
    character: int,
    session: ToolingSession | None = None,
) -> ToolingSignatureHelp | None:
    """Project the most-specific compiler-resolved call at a position."""

    root_unit = _root_logical_source(source, session)
    candidates: list[tuple[tuple[int, int, int, int, int], SignatureHelpCall]] = []
    for index, call in enumerate(result.signature_help_calls):
        origin = _origin_from_source(call.call_origin)
        if origin is None:
            continue
        if root_unit is not None and call.call_origin.source_unit not in {
            None,
            root_unit,
        }:
            continue
        if not _position_in_origin(origin, line, character):
            continue
        line_span, column_span = _hover_specificity(origin)
        candidates.append(
            (
                (
                    line_span,
                    column_span,
                    origin.start_line,
                    origin.start_column,
                    index,
                ),
                call,
            )
        )
    if not candidates:
        return None
    call = min(candidates, key=lambda item: item[0])[1]
    parameters = tuple(parameter.label for parameter in call.parameters)
    return ToolingSignatureHelp(
        call.label,
        parameters,
        _signature_active_parameter(call, line, character),
        _origin_from_source(call.call_origin),
    )


def signature_help_at(
    source: Path | str,
    source_text: str,
    line: int,
    character: int,
    *,
    _session: ToolingSession | None = None,
) -> ToolingSignatureHelp | None:
    """Return one compiler-resolved callable signature at an editor position."""

    if (
        isinstance(line, bool)
        or isinstance(character, bool)
        or not isinstance(line, int)
        or not isinstance(character, int)
        or line < 0
        or character < 0
    ):
        return None
    path = Path(source).expanduser().resolve()
    try:
        result = _semantic_snapshot(
            path,
            source_text,
            analysis_needs=AnalysisNeeds.SIGNATURE_HELP,
            session=_session,
        )
    except (
        ParseError,
        TopSelectionError,
        SemanticError,
        DiagnosticError,
    ):
        return None
    except (
        ModuleResolutionError,
        WorkspaceError,
        ProjectModelError,
        DependencyModelError,
        OSError,
        ValueError,
    ) as error:
        raise ToolingError(str(error)) from error
    return _signature_help_from_result(path, result, line, character, _session)


_SEMANTIC_DECLARATION_KIND_MAP = {
    "function": "function",
    "parameter": "parameter",
    "port": "property",
    "value": "variable",
}
_SEMANTIC_REFERENCE_KIND_MAP = {
    "function": "function",
    "parameter": "parameter",
    "port": "property",
    "symbol": "variable",
    "value": "variable",
}


def _exact_semantic_token_origin(
    origin: object,
    name: str,
) -> ToolingOrigin | None:
    """Project only parser/compiler spans proven to cover one identifier."""

    projected = _origin_from_source(origin)
    if projected is None or not _origin_is_exact_name(projected, name):
        return None
    return projected


def _semantic_tokens_from_result(
    source: Path,
    result: object,
    session: ToolingSession | None = None,
) -> tuple[ToolingSemanticToken, ...]:
    """Project exact root-document declarations and resolved occurrences."""

    root_unit = _root_logical_source(source, session)
    root_names = {source.name, source.as_posix(), str(source.resolve())}

    def belongs_to_root(origin: object) -> bool:
        unit = getattr(origin, "source_unit", None)
        if unit is None:
            return True
        if root_unit is not None:
            return unit == root_unit
        return unit in root_names

    records: dict[
        tuple[object, ...], ToolingSemanticToken
    ] = {}

    def add(
        origin: object,
        name: str,
        kind: str | None,
        modifiers: tuple[str, ...],
    ) -> None:
        if kind is None or not belongs_to_root(origin):
            return
        projected = _exact_semantic_token_origin(origin, name)
        if projected is None:
            return
        token = ToolingSemanticToken(projected, kind, modifiers)
        key = (
            projected.source_unit,
            projected.start_line,
            projected.start_column,
            projected.end_line,
            projected.end_column,
            token.kind,
            token.modifiers,
        )
        records[key] = token

    for declaration in result.definition_declarations:
        add(
            declaration.target,
            declaration.name,
            _SEMANTIC_DECLARATION_KIND_MAP.get(declaration.kind),
            ("declaration",),
        )
    for resolution in result.definition_resolutions:
        add(
            resolution.occurrence,
            resolution.name,
            _SEMANTIC_REFERENCE_KIND_MAP.get(resolution.kind),
            (),
        )

    ordered = sorted(
        records.values(),
        key=lambda item: (
            item.origin.start_line,
            item.origin.start_column,
            item.origin.end_column - item.origin.start_column,
            0 if "declaration" in item.modifiers else 1,
            item.kind,
            item.modifiers,
        ),
    )
    # Exact identifier spans should never overlap.  If malformed observational
    # metadata does overlap, fail closed for the later record instead of
    # publishing two conflicting token classes for one source range.
    result_tokens: list[ToolingSemanticToken] = []
    for token in ordered:
        if result_tokens:
            previous = result_tokens[-1]
            if (
                previous.origin.start_line == token.origin.start_line
                and token.origin.start_column < previous.origin.end_column
            ):
                continue
        result_tokens.append(token)
    return tuple(result_tokens)


def semantic_tokens(
    source: Path | str,
    source_text: str,
    *,
    _session: ToolingSession | None = None,
    _top: str | None = None,
) -> tuple[ToolingSemanticToken, ...]:
    """Return exact compiler-classified occurrences for the root document."""

    path = Path(source).expanduser().resolve()
    try:
        result = (
            _session.symbol_snapshot_covering(
                path, source_text, required_module=_top
            )
            if _session is not None and _top is not None else None
        )
        if result is None:
            result = _definition_snapshot(
                path,
                source_text,
                session=_session,
            )
    except (
        ParseError,
        TopSelectionError,
        SemanticError,
        DiagnosticError,
    ):
        return ()
    except (
        ModuleResolutionError,
        WorkspaceError,
        ProjectModelError,
        DependencyModelError,
        OSError,
        ValueError,
    ) as error:
        raise ToolingError(str(error)) from error
    return _semantic_tokens_from_result(path, result, _session)


def definition_at(
    source: Path | str,
    source_text: str,
    line: int,
    character: int,
    *,
    _session: ToolingSession | None = None,
) -> ToolingDefinition | None:
    """Resolve one source position through the compiler's name resolver."""

    if (
        isinstance(line, bool)
        or isinstance(character, bool)
        or not isinstance(line, int)
        or not isinstance(character, int)
        or line < 0
        or character < 0
    ):
        return None
    path = Path(source).expanduser().resolve()
    columns = _definition_lookup_columns(source_text, line, character)
    for result in _navigation_results_at(path, source_text, line, character, _session):
        for column in columns:
            definition = _definition_from_result(path, result, line, column, _session)
            if definition is not None:
                return definition
    return None


def _navigation_results_at(
    path: Path,
    source_text: str,
    line: int,
    character: int,
    session: ToolingSession | None,
) -> Iterator[object]:
    """Yield exact compiler symbol contexts for both navigation methods.

    The parser selects a possible top only; no parser result can become a
    Location.  A warm selected top is tried first, then the default, then the
    enclosing earlier top if needed.  The iterator stops when its caller has
    a compiler-owned occurrence, so a warm hit avoids unrelated analysis.
    """

    seen: set[str | None] = set()

    def snapshot(top: str | None) -> object | None:
        try:
            return _definition_snapshot(path, source_text, session=session, top=top)
        except (ParseError, TopSelectionError, SemanticError, DiagnosticError):
            return None
        except (
            ModuleResolutionError, WorkspaceError, ProjectModelError,
            DependencyModelError, OSError, ValueError,
        ) as error:
            raise ToolingError(str(error)) from error

    cached_top = (
        _definition_top_at(
            source_text, line, character, source=path, session=session,
            cached_only=True,
        )
        if session is not None else None
    )
    for top in (cached_top, None):
        if top in seen:
            continue
        seen.add(top)
        result = snapshot(top)
        if result is not None:
            yield result
    try:
        selected_top = _definition_top_at(
            source_text, line, character, source=path, session=session
        )
    except ParseError:
        return
    if selected_top is not None and selected_top not in seen:
        result = snapshot(selected_top)
        if result is not None:
            yield result


def _definition_top_at(
    source_text: str,
    line: int,
    character: int,
    *,
    source: Path | None = None,
    session: ToolingSession | None = None,
    cached_only: bool = False,
) -> str | None:
    """Select the enclosing parsed module, not the last module in a file.

    A module name span begins each source module.  Its next name span ends
    the current module's navigation region.  Declaration-only units retain
    the ordinary compiler top selection.  This is only a compilation selector;
    compiler definition resolutions remain the sole navigation authority.
    """

    key = (
        (source, hashlib.sha256(source_text.encode("utf-8")).hexdigest())
        if source is not None and session is not None else None
    )
    regions = session._navigation_regions.get(key) if key is not None else None
    if regions is None:
        if cached_only:
            return None
        syntax = parse(source_text)
        modules = (*syntax.submodules, syntax)
        regions = tuple(
            ((module.name_origin.start_line, module.name_origin.start_column),
             module.name)
            for module in modules
            if module.name_origin is not None
        )
        if key is not None:
            session._navigation_regions[key] = regions
            session._navigation_regions.move_to_end(key)
            while len(session._navigation_regions) > 64:
                session._navigation_regions.popitem(last=False)
    elif key is not None:
        session._navigation_regions.move_to_end(key)
    if len(regions) < 2:
        return None
    position = (line + 1, character + 1)
    preceding = tuple(
        (start, name) for start, name in regions if start <= position
    )
    if not preceding:
        return None
    selected = max(preceding, key=lambda item: item[0])[1]
    return selected if selected != regions[-1][1] else None


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


def _reference_target_from_result(
    source: Path,
    result: object,
    line: int,
    character: int,
    session: ToolingSession | None = None,
) -> object | None:
    """Resolve the target identity under a cursor using compiler records."""

    root_unit = _root_logical_source(source, session)
    candidates: list[tuple[tuple[int, int, int], object]] = []
    occurrence_index = getattr(result, "occurrences_by_line", None)
    resolutions = (
        _indexed_symbol_line(occurrence_index, root_unit, line + 1)
        if occurrence_index is not None
        else result.definition_resolutions
    )
    for index, resolution in enumerate(resolutions):
        occurrence = _origin_from_source(resolution.occurrence)
        if occurrence is None:
            continue
        if (
            root_unit is not None
            and resolution.occurrence.source_unit not in {None, root_unit}
        ):
            continue
        if not _position_in_origin(occurrence, line, character):
            continue
        line_span = occurrence.end_line - occurrence.start_line
        column_span = (
            occurrence.end_column - occurrence.start_column
            if line_span == 0
            else 1_000_000
        )
        candidates.append(((line_span, column_span, index), resolution.target))
    if candidates:
        return min(candidates, key=lambda item: item[0])[1]

    # A declaration may have no usages.  Declaration targets are still
    # compiler-owned and allow includeDeclaration queries to resolve without
    # inventing a name match in the LSP.
    declaration_candidates: list[tuple[tuple[int, int, int], object]] = []
    declaration_index = getattr(result, "declarations_by_line", None)
    declarations = (
        _indexed_symbol_line(declaration_index, root_unit, line + 1)
        if declaration_index is not None
        else result.definition_declarations
    )
    for index, declaration in enumerate(declarations):
        origin = _origin_from_source(declaration.target)
        if origin is None:
            continue
        if (
            root_unit is not None
            and declaration.target.source_unit not in {None, root_unit}
        ):
            continue
        if not _position_in_origin(origin, line, character):
            continue
        declaration_candidates.append(
            ((origin.end_line - origin.start_line, index, 0), declaration.target)
        )
    if declaration_candidates:
        return min(declaration_candidates, key=lambda item: item[0])[1]
    return None


def _references_for_target(
    source: Path,
    result: object,
    target: object,
    include_declaration: bool,
    session: ToolingSession | None = None,
) -> tuple[ToolingReference, ...]:
    """Project occurrences matching one compiler-resolved declaration."""

    target_key = _declaration_coordinate_key(target)
    records: list[ToolingReference] = []
    seen: set[tuple[object, ...]] = set()

    def add(origin: object) -> None:
        path = _source_path_for_origin(
            source, origin, result.physical_inputs, session
        )
        projected = _origin_from_source(origin)
        if path is None or projected is None:
            raise ToolingError(
                "compiler resolved a reference whose locked source path is unavailable"
            )
        key = (
            path.resolve().as_posix(),
            projected.start_line,
            projected.start_column,
            projected.end_line,
            projected.end_column,
        )
        if key in seen:
            return
        seen.add(key)
        records.append(ToolingReference(path.resolve(), projected))

    if include_declaration:
        add(target)
    occurrence_index = getattr(result, "occurrences_by_declaration", None)
    resolutions = (
        occurrence_index.get(target_key, ())
        if occurrence_index is not None
        else result.definition_resolutions
    )
    for resolution in resolutions:
        if _declaration_coordinate_key(resolution.target) != target_key:
            continue
        add(resolution.occurrence)
    records.sort(
        key=lambda item: (
            item.source_path.as_posix(),
            item.origin.start_line,
            item.origin.start_column,
            item.origin.end_line,
            item.origin.end_column,
        )
    )
    return tuple(records)


def _project_reference_compilations(
    source: Path,
    source_text: str,
    target: object,
    reference_name: str,
) -> tuple[tuple[Path, str | None, str], ...]:
    """Return bounded root/top/text snapshots whose closure can use target."""

    target_unit = getattr(target, "source_unit", None)
    if not isinstance(target_unit, str):
        return ()
    location = discover_project(source)
    if location is None:
        return ()
    # A reference sweep must see the current manifest, lock and import graph,
    # not a long-lived index retained for ordinary F12 path projection.
    index = workspace_index(location.manifest_path)
    root_names = {item.logical_path for item in index.root_modules}
    if target_unit not in root_names and target_unit not in {
        item.logical_path for item in index.dependency_modules
    }:
        return ()
    closures = {
        item.logical_path: frozenset(index.dependency_closure(item.logical_path))
        for item in index.root_modules
    }
    eligible = tuple(
        item
        for item in index.root_modules
        if item.logical_path == target_unit
        or target_unit in closures[item.logical_path]
    )
    if len(eligible) > _REFERENCE_MAX_PROJECT_ROOTS:
        raise ToolingError("project reference root candidate limit exceeded")
    result: list[tuple[Path, str | None, str]] = []
    for item in eligible:
        text = (
            source_text if item.source_path.resolve() == source.resolve()
            else item.source_path.read_text(encoding="utf-8")
        )
        try:
            syntax = parse(text)
        except ParseError:
            # Keep malformed roots eligible so the semantic pass below remains
            # the single authority for accepting or rejecting their records.
            result.append((item.source_path, None, text))
            continue
        matching_submodules = tuple(
            module.name
            for module in syntax.submodules
            if _syntax_mentions_reference_name(module, reference_name)
        )
        if _syntax_mentions_reference_name(syntax, reference_name) or matching_submodules:
            result.append((item.source_path, None, text))
        result.extend(
            (item.source_path, name, text) for name in matching_submodules
        )
        if len(result) > _REFERENCE_MAX_PROJECT_CANDIDATES:
            raise ToolingError("project reference top candidate limit exceeded")
    return tuple(result)


def _syntax_mentions_reference_name(value: object, name: str) -> bool:
    """Parser prefilter only; this predicate never publishes a reference."""

    if isinstance(value, ast_nodes.TypeName):
        names = tuple(component for component, _ in value.named_origins)
        return (
            value.text == name
            or value.text.endswith("." + name)
            or any(
                component == name or component.endswith("." + name)
                for component in names
            )
        )
    if isinstance(value, str):
        return value == name or value.endswith("." + name)
    if isinstance(value, tuple):
        return any(_syntax_mentions_reference_name(item, name) for item in value)
    if is_dataclass(value) and not isinstance(value, type):
        return any(
            _syntax_mentions_reference_name(getattr(value, item.name), name)
            for item in fields(value)
            if item.name not in {
                "origin", "name_origin", "named_origins", "source_hash",
                "source_identity", "submodules",
            }
        )
    return False


def _same_file_reference_tops(source_text: str, name: str) -> tuple[str, ...]:
    """Select sibling modules that may contain an exact semantic use."""

    syntax = parse(source_text)
    matches = tuple(
        module.name for module in syntax.submodules
        if _syntax_mentions_reference_name(module, name)
    )
    if len(matches) > 64:
        raise ToolingError("same-file reference candidate limit exceeded")
    return matches


def _target_matches_disk_snapshot(
    source: Path,
    target: object,
    result: object,
    session: ToolingSession | None,
) -> bool:
    """Reject project-wide projection from an unsaved declaration snapshot."""

    digest = getattr(target, "digest", None)
    if digest is None:
        return False
    path = _source_path_for_origin(
        source, target, result.physical_inputs, session
    )
    if path is None:
        return False
    try:
        current = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return False
    return current == digest


def references_at(
    source: Path | str,
    source_text: str,
    line: int,
    character: int,
    include_declaration: bool = False,
    *,
    _session: ToolingSession | None = None,
) -> tuple[ToolingReference, ...]:
    """Return compiler-resolved references for one source position."""

    if (
        isinstance(line, bool)
        or isinstance(character, bool)
        or not isinstance(line, int)
        or not isinstance(character, int)
        or line < 0
        or character < 0
        or not isinstance(include_declaration, bool)
    ):
        return ()
    path = Path(source).expanduser().resolve()
    columns = _definition_lookup_columns(source_text, line, character)
    target = None
    result = None
    for candidate in _navigation_results_at(
        path, source_text, line, character, _session
    ):
        for column in columns:
            target = _reference_target_from_result(
                path, candidate, line, column, _session
            )
            if target is not None:
                result = candidate
                break
        if target is not None:
            break
    if target is None or result is None:
        return ()

    metadata = _rename_target_metadata(result, target)
    reference_name = metadata[0] if metadata is not None else None

    records = list(_references_for_target(
        path,
        result,
        target,
        include_declaration,
        _session,
    ))
    if reference_name is not None:
        try:
            same_file_tops = _same_file_reference_tops(
                source_text, reference_name
            )
        except ParseError:
            same_file_tops = ()
        for top in same_file_tops:
            try:
                sibling = _definition_snapshot(
                    path, source_text, session=_session, top=top
                )
            except (
                ParseError, TopSelectionError, SemanticError, DiagnosticError,
                ModuleResolutionError, WorkspaceError, ProjectModelError,
                DependencyModelError, OSError, ValueError,
            ):
                # An invalid sibling cannot supply trustworthy occurrences.
                continue
            records.extend(_references_for_target(
                path, sibling, target, False, _session
            ))
    if (
        reference_name is not None
        and _target_matches_disk_snapshot(path, target, result, _session)
    ):
        for root, root_top, root_text in _project_reference_compilations(
            path, source_text, target, reference_name
        ):
            try:
                project_result = _definition_snapshot(
                    root,
                    root_text,
                    session=_session,
                    top=root_top,
                )
            except (
                ParseError,
                TopSelectionError,
                SemanticError,
                DiagnosticError,
                ModuleResolutionError,
                WorkspaceError,
                ProjectModelError,
                DependencyModelError,
                OSError,
                ValueError,
            ):
                # A broken independent root cannot provide trustworthy
                # semantic references, but it must not invalidate records
                # already resolved from other roots.
                continue
            if root.resolve() != path:
                try:
                    current_digest = hashlib.sha256(root.read_bytes()).hexdigest()
                except OSError as error:
                    raise ToolingError(
                        "project reference source changed during lookup"
                    ) from error
                if current_digest != hashlib.sha256(root_text.encode("utf-8")).hexdigest():
                    raise ToolingError("project reference source changed during lookup")
            records.extend(_references_for_target(
                root,
                project_result,
                target,
                False,
                _session,
            ))

    source_cache: dict[Path, tuple[str, ...]] = {
        path: tuple(source_text.splitlines())
    }

    def exact_spelling(item: ToolingReference) -> bool:
        if reference_name is None:
            return True
        resolved = item.source_path.resolve()
        lines = source_cache.get(resolved)
        if lines is None:
            editor_text = (
                None
                if _session is None
                else _session.editor_text_for(resolved)
            )
            if editor_text is not None:
                lines = tuple(editor_text.splitlines())
            else:
                try:
                    lines = tuple(
                        resolved.read_text(encoding="utf-8").splitlines()
                    )
                except OSError:
                    return False
            source_cache[resolved] = lines
        origin = item.origin
        if (
            origin.start_line != origin.end_line
            or not 1 <= origin.start_line <= len(lines)
        ):
            return False
        line_text = lines[origin.start_line - 1]
        return (
            line_text[origin.start_column - 1 : origin.end_column - 1]
            == reference_name
        )

    unique = {
        (
            item.source_path.as_posix(),
            item.origin.start_line,
            item.origin.start_column,
            item.origin.end_line,
            item.origin.end_column,
        ): item
        for item in records
        if exact_spelling(item)
    }
    return tuple(unique[key] for key in sorted(unique))


_RENAMEABLE_KINDS = frozenset({"port", "value", "function", "parameter"})


def _origin_location_key(origin: object) -> tuple[object, ...]:
    span = getattr(origin, "span", origin)
    return (
        getattr(origin, "source_unit", None),
        int(span.start_line),
        int(span.start_column),
    )


def _origin_is_exact_name(origin: ToolingOrigin, name: str) -> bool:
    return (
        origin.start_line == origin.end_line
        and origin.end_column - origin.start_column == len(name)
    )


def _source_offset(source_text: str, line: int, column: int) -> int:
    lines = source_text.splitlines(keepends=True)
    if line < 1 or line > len(lines) + 1 or column < 1:
        raise ToolingRenameError("rename source origin is outside the document")
    return sum(len(item) for item in lines[: line - 1]) + column - 1


def _offset_to_position(source_text: str, offset: int) -> tuple[int, int]:
    if offset < 0 or offset > len(source_text):
        raise ToolingRenameError("rename source edit is outside the document")
    before = source_text[:offset]
    line = before.count("\n") + 1
    last_newline = before.rfind("\n")
    column = offset - last_newline
    return line, column


def _apply_rename_text(
    source_text: str,
    edits: tuple[ToolingRenameEdit, ...],
) -> str:
    positioned: list[tuple[int, int, str]] = []
    for edit in edits:
        start = _source_offset(
            source_text, edit.origin.start_line, edit.origin.start_column
        )
        end = _source_offset(
            source_text, edit.origin.end_line, edit.origin.end_column
        )
        if end < start:
            raise ToolingRenameError("rename source edit has an invalid range")
        positioned.append((start, end, edit.new_text))
    result = source_text
    for start, end, replacement in sorted(positioned, reverse=True):
        result = result[:start] + replacement + result[end:]
    return result


def _shifted_position(
    source_text: str,
    origin: object,
    edits: tuple[ToolingRenameEdit, ...],
    updated_text: str | None = None,
) -> tuple[int, int]:
    span = getattr(origin, "span", origin)
    offset = _source_offset(source_text, int(span.start_line), int(span.start_column))
    delta = 0
    for edit in edits:
        edit_start = _source_offset(
            source_text, edit.origin.start_line, edit.origin.start_column
        )
        edit_end = _source_offset(
            source_text, edit.origin.end_line, edit.origin.end_column
        )
        if edit_end <= offset:
            delta += len(edit.new_text) - (edit_end - edit_start)
    return _offset_to_position(
        source_text if updated_text is None else updated_text,
        offset + delta,
    )


def _rename_target_metadata(
    result: object,
    target: object,
) -> tuple[str, str] | None:
    target_key = _origin_key(target)
    for declaration in result.definition_declarations:
        if _origin_key(declaration.target) == target_key:
            return declaration.name, declaration.kind
    for resolution in result.definition_resolutions:
        if _origin_key(resolution.target) == target_key:
            return resolution.name, resolution.kind
    return None


def _rename_edits_from_result(
    source: Path,
    result: object,
    line: int,
    character: int,
    new_name: str,
    session: ToolingSession | None = None,
) -> tuple[ToolingRenameEdit, ...] | None:
    target = _reference_target_from_result(
        source, result, line, character, session
    )
    if target is None:
        return None
    metadata = _rename_target_metadata(result, target)
    if metadata is None:
        return None
    old_name, kind = metadata
    if kind not in _RENAMEABLE_KINDS:
        return None

    target_path = _source_path_for_origin(
        source, target, result.physical_inputs, session
    )
    if target_path is None or target_path.resolve() != source.resolve():
        raise ToolingRenameError(
            "cross-file rename is not supported without an authoritative editable snapshot"
        )
    target_origin = _origin_from_source(target)
    if target_origin is None or not _origin_is_exact_name(target_origin, old_name):
        return None

    origins: list[object] = [target]
    target_key = _origin_key(target)
    for resolution in result.definition_resolutions:
        if _origin_key(resolution.target) == target_key:
            origins.append(resolution.occurrence)

    edits: list[ToolingRenameEdit] = []
    seen: set[tuple[int, int, int, int]] = set()
    for origin in origins:
        projected = _origin_from_source(origin)
        path = _source_path_for_origin(
            source, origin, result.physical_inputs, session
        )
        if projected is None or path is None:
            raise ToolingRenameError(
                "symbol cannot be renamed safely because an exact source span is unavailable"
            )
        if path.resolve() != source.resolve() or not _origin_is_exact_name(projected, old_name):
            raise ToolingRenameError(
                "symbol cannot be renamed safely because an exact editable identifier span is unavailable"
            )
        key = (
            projected.start_line,
            projected.start_column,
            projected.end_line,
            projected.end_column,
        )
        if key in seen:
            continue
        seen.add(key)
        edits.append(ToolingRenameEdit(source.resolve(), projected, new_name))
    edits.sort(
        key=lambda item: (
            item.source_path.as_posix(),
            item.origin.start_line,
            item.origin.start_column,
        )
    )
    return tuple(edits)


def _validate_renamed_semantics(
    source: Path,
    source_text: str,
    original_target: object,
    edits: tuple[ToolingRenameEdit, ...],
    new_name: str,
    session: ToolingSession | None = None,
) -> None:
    """Recheck the edited root and preserve target resolution at each use."""

    candidate_text = _apply_rename_text(source_text, edits)
    try:
        candidate = _semantic_snapshot(
            source,
            candidate_text,
            analysis_needs=AnalysisNeeds.DEFINITIONS,
            session=session,
        )
    except (
        ParseError,
        TopSelectionError,
        SemanticError,
        DiagnosticError,
    ) as error:
        raise ToolingRenameError(
            "new name would change or invalidate ZLang name resolution"
        ) from error
    except (
        ModuleResolutionError,
        WorkspaceError,
        ProjectModelError,
        DependencyModelError,
        OSError,
        ValueError,
    ) as error:
        raise ToolingError(str(error)) from error

    shifted_target_line, shifted_target_column = _shifted_position(
        source_text, original_target, edits, candidate_text
    )
    target = _reference_target_from_result(
        source,
        candidate,
        shifted_target_line - 1,
        shifted_target_column - 1,
        session,
    )
    if target is None:
        raise ToolingRenameError("new name would change the resolved rename target")
    original_location = _origin_location_key(original_target)
    candidate_location = _origin_location_key(target)
    if original_location != candidate_location:
        raise ToolingRenameError("new name would change the resolved rename target")

    expected_starts = {
        _shifted_position(source_text, edit.origin, edits, candidate_text)
        for edit in edits
    }
    matched = {
        (
            resolution.occurrence.span.start_line,
            resolution.occurrence.span.start_column,
        )
        for resolution in candidate.definition_resolutions
        if resolution.name == new_name
        and _origin_location_key(resolution.target) == candidate_location
    }
    expected_starts.discard((shifted_target_line, shifted_target_column))
    if not expected_starts.issubset(matched):
        raise ToolingRenameError("new name would change the resolved rename target")


def rename_at(
    source: Path | str,
    source_text: str,
    line: int,
    character: int,
    new_name: str,
    *,
    _session: ToolingSession | None = None,
) -> tuple[ToolingRenameEdit, ...] | None:
    """Return exact semantic rename edits, or no result when unsupported."""

    if (
        isinstance(line, bool)
        or isinstance(character, bool)
        or not isinstance(line, int)
        or not isinstance(character, int)
        or line < 0
        or character < 0
    ):
        return None
    if not isinstance(new_name, str) or not is_valid_identifier(new_name):
        raise ToolingRenameError("new name is not a valid ZLang identifier")
    path = Path(source).expanduser().resolve()
    try:
        result = _semantic_snapshot(
            path,
            source_text,
            analysis_needs=AnalysisNeeds.DEFINITIONS,
            session=_session,
        )
    except (
        ParseError,
        TopSelectionError,
        SemanticError,
        DiagnosticError,
    ):
        return None
    except (
        ModuleResolutionError,
        WorkspaceError,
        ProjectModelError,
        DependencyModelError,
        OSError,
        ValueError,
    ) as error:
        raise ToolingError(str(error)) from error
    edits = _rename_edits_from_result(
        path, result, line, character, new_name, _session
    )
    if edits is None:
        return None
    target = _reference_target_from_result(
        source, result, line, character, _session
    )
    if target is None:
        return None
    _validate_renamed_semantics(
        path,
        source_text,
        target,
        edits,
        new_name,
        _session,
    )
    return edits


@dataclass(frozen=True)
class SemanticCheckRecord:
    status: str
    phase: str
    resolved_top: str | None
    module_identity: str | None
    diagnostics: tuple[ToolingDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.status not in {"passed", "failed"}:
            raise ValueError("tooling semantic status is unsupported")
        if self.phase not in {
            "parse",
            "resolution",
            "semantic",
            "top_selection",
            "complete",
        }:
            raise ValueError("tooling semantic phase is unsupported")
        if self.status == "passed" and (
            self.phase != "complete"
            or self.resolved_top is None
            or self.module_identity is None
            or self.diagnostics
        ):
            raise ValueError("passed tooling semantic record is inconsistent")
        if self.status == "failed" and not self.diagnostics:
            raise ValueError("failed tooling semantic record requires diagnostics")


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


def source_facts(source_text: str) -> SourceFacts:
    """Return deterministic declaration/import facts without making validity claims."""

    try:
        syntax = parse(source_text)
    except ParseError:
        return SourceFacts((), (), False)
    return SourceFacts(
        tuple(item.path for item in syntax.imports),
        tuple(sorted({syntax.name, *(item.name for item in syntax.submodules)})),
        True,
    )


def unwritten_register_warnings(source_text: str) -> tuple[ToolingDiagnostic, ...]:
    """Report only source-declared registers with no possible source update.

    A register without an update is valid ZLang and holds its reset value.
    This narrow editor observation uses the compiler parser's declaration and
    action records; it never changes semantic checking or searches identifier
    text for a matching assignment.  An update in any compile-time branch
    suppresses the warning, so unspecialized source cannot get a false alarm.
    """

    # A negative prefilter avoids reparsing most navigation targets.  Actual
    # classification below is exclusively parser-owned.
    if "reg" not in source_text:
        return ()
    try:
        syntax = parse(source_text)
    except ParseError:
        return ()

    def write_targets(module: ast_nodes.Module) -> set[str]:
        targets: set[str] = set()

        def visit(value: object) -> None:
            if isinstance(value, ast_nodes.NextAssignment):
                target = value.target
                targets.add(
                    target if isinstance(target, str) else target.register
                )
                return
            if isinstance(value, ast_nodes.Module) or isinstance(
                value, ast_nodes.Expression
            ):
                return
            if isinstance(value, (tuple, list)):
                for item in value:
                    visit(item)
            elif is_dataclass(value) and not isinstance(value, type):
                for item in fields(value):
                    if item.name not in {"origin", "name_origin", "name_origins"}:
                        visit(getattr(value, item.name))

        visit(
            module.ordered_items
            if module.ordered_items
            else (
                *module.next_assignments,
                *module.rules,
                *module.fsms,
                *module.compile_time_ifs,
                *module.generate_blocks,
            )
        )
        return targets

    warnings: list[ToolingDiagnostic] = []

    def collect(module: ast_nodes.Module) -> None:
        targets = write_targets(module)
        for declaration in module.registers:
            if declaration.name in targets:
                continue
            origin = _origin_from_source(
                declaration.name_origin, construct=f"register {declaration.name}"
            )
            if origin is None:
                continue
            warnings.append(ToolingDiagnostic(
                "ZL-REGISTER-NEVER-WRITTEN",
                f"register '{declaration.name}' has no next-state or rule "
                "assignment; it will hold its reset value",
                origin,
                (),
                (),
                severity="warning",
            ))
        for child in module.submodules:
            collect(child)

    collect(syntax)
    warnings.sort(key=lambda item: (
        item.primary.start_line if item.primary is not None else 0,
        item.primary.start_column if item.primary is not None else 0,
        item.message,
    ))
    return tuple(warnings)


def resolve_direct_imports(
    source: Path | str,
    imports: tuple[str, ...],
) -> tuple[ResolvedImport, ...]:
    requested = tuple(dict.fromkeys(imports))
    if not requested:
        return ()
    path = Path(source)
    try:
        workspace = load_project_workspace(path)
    except WorkspaceError as error:
        return tuple(
            ResolvedImport(item, None, f"resolver_error:{error}")
            for item in requested
        )
    result: list[ResolvedImport] = []
    for logical in requested:
        try:
            records = (
                workspace.resolver.resolve((logical,))
                if workspace is not None
                else StdlibModuleResolver().resolve((logical,))
            )
            record = next(
                (item for item in records if item.logical_path == logical), None
            )
            if record is None:
                result.append(
                    ResolvedImport(logical, None, "resolver_missing_direct_record")
                )
            else:
                result.append(ResolvedImport(logical, Path(record.source_path)))
        except ModuleResolutionError as error:
            result.append(ResolvedImport(logical, None, f"resolver_error:{error}"))
    return tuple(result)


def discover_project(source: Path | str) -> ProjectLocation | None:
    try:
        manifest = discover_project_manifest(Path(source))
    except ProjectModelError as error:
        raise ToolingError(str(error)) from error
    if manifest is None:
        return None
    return ProjectLocation(
        manifest.path.resolve(strict=True),
        manifest.project_root.resolve(strict=True),
        manifest.source_directory.resolve(strict=True),
    )


def workspace_index(manifest: Path | str) -> WorkspaceIndex:
    selected = Path(manifest)
    try:
        workspace = load_project_workspace(selected, project=selected)
    except (WorkspaceError, ProjectModelError, ModuleResolutionError) as error:
        raise ToolingError(str(error)) from error
    if workspace is None:
        raise ToolingError(f"project manifest is unavailable: {selected.name}")

    def convert(record: object, *, root: bool) -> WorkspaceModule:
        return WorkspaceModule(
            str(getattr(record, "logical_path")),
            Path(getattr(record, "source_path")).resolve(strict=True),
            tuple(getattr(record, "dependencies")),
            root,
        )

    return WorkspaceIndex(
        workspace.manifest.path.resolve(strict=True),
        workspace.manifest.source_directory.resolve(strict=True),
        tuple(convert(item, root=True) for item in workspace.root_modules),
        tuple(convert(item, root=False) for item in workspace.dependency_modules),
    )


def _phase(error: BaseException) -> str:
    if isinstance(error, ParseError):
        return "parse"
    if isinstance(error, TopSelectionError):
        return "top_selection"
    diagnostic = getattr(error, "diagnostic", None)
    code = getattr(diagnostic, "code", None)
    if isinstance(code, str) and code.startswith("ZL-IMPORT-"):
        return "resolution"
    if isinstance(
        error,
        (
            ModuleResolutionError,
            WorkspaceError,
            ProjectModelError,
            DependencyModelError,
        ),
    ):
        return "resolution"
    if isinstance(error, (SemanticError, DiagnosticError)):
        return "semantic"
    return "resolution"


def _diagnostic_position_offset(
    source_text: str,
    line: int,
    column: int,
) -> int | None:
    """Return one exact compiler position offset, rejecting stale ranges."""

    if line < 1 or column < 1:
        return None
    lines = source_text.splitlines(keepends=True)
    if not lines:
        return 0 if (line, column) == (1, 1) else None
    if line == len(lines) + 1 and source_text.endswith(("\n", "\r")):
        return len(source_text) if column == 1 else None
    if line > len(lines):
        return None
    selected = lines[line - 1]
    content = selected.rstrip("\r\n")
    if column > len(content) + 1:
        return None
    return sum(len(item) for item in lines[: line - 1]) + column - 1


def _tooling_diagnostic_fix(
    fix: DiagnosticFix,
    *,
    source_path: Path,
    source_text: str,
) -> ToolingDiagnosticFix | None:
    """Project one complete current-source fix, or reject it atomically."""

    digest = hashlib.sha256(source_text.encode()).hexdigest()
    projected: list[ToolingDiagnosticEdit] = []
    for edit in fix.edits:
        origin = edit.origin
        if origin.digest != digest:
            return None
        tooling_origin = _origin_from_source(origin)
        if tooling_origin is None:
            return None
        start = _diagnostic_position_offset(
            source_text,
            tooling_origin.start_line,
            tooling_origin.start_column,
        )
        end = _diagnostic_position_offset(
            source_text,
            tooling_origin.end_line,
            tooling_origin.end_column,
        )
        if start is None or end is None or end < start:
            return None
        projected.append(
            ToolingDiagnosticEdit(source_path, tooling_origin, edit.replacement)
        )
    return ToolingDiagnosticFix(fix.title, tuple(projected))


def _diagnostic(
    value: Diagnostic,
    *,
    machine_fixes: tuple[DiagnosticFix, ...] = (),
    source_path: Path | None = None,
    source_text: str | None = None,
) -> ToolingDiagnostic:
    origin = value.primary
    primary = _origin_from_source(origin)
    projected_fixes: tuple[ToolingDiagnosticFix, ...] = ()
    if source_path is not None and source_text is not None:
        projected_fixes = tuple(
            projected
            for fix in machine_fixes
            if (
                projected := _tooling_diagnostic_fix(
                    fix,
                    source_path=source_path,
                    source_text=source_text,
                )
            )
            is not None
        )
    return ToolingDiagnostic(
        value.code,
        value.message,
        primary,
        value.notes,
        value.fixes,
        projected_fixes,
    )


def _module_identity(module: object) -> str:
    if getattr(module, "parameters", None) is not None:
        return specialization_fingerprint(module)
    signature = getattr(module, "module_signature", None)
    identity = getattr(signature, "identity", None)
    if isinstance(identity, str) and identity:
        return identity
    name = getattr(module, "name", None)
    if isinstance(name, str) and name:
        return name
    return "module:" + stable_digest({"type": type(module).__name__})


def check_snapshot(
    source: Path | str,
    source_text: str,
    *,
    source_digest: str | None = None,
    project: Path | str | None = None,
    top: str | None = None,
    _session: ToolingSession | None = None,
) -> SemanticCheckRecord:
    """Run one semantic-only check and return a stable integration record."""

    source_path = Path(source).expanduser().resolve()
    try:
        result = _semantic_snapshot(
            source_path,
            source_text,
            source_digest=source_digest,
            project=project,
            top=top,
            analysis_needs=AnalysisNeeds.NONE,
            session=_session,
        )
    except (
        ParseError,
        TopSelectionError,
        ModuleResolutionError,
        WorkspaceError,
        ProjectModelError,
        DependencyModelError,
        SemanticError,
        DiagnosticError,
    ) as error:
        diagnostic = getattr(error, "diagnostic", None)
        if not isinstance(diagnostic, Diagnostic):
            raise ToolingError(str(error)) from error
        return SemanticCheckRecord(
            "failed",
            _phase(error),
            None,
            None,
            (
                _diagnostic(
                    diagnostic,
                    machine_fixes=tuple(getattr(error, "machine_fixes", ())),
                    source_path=source_path,
                    source_text=source_text,
                ),
            ),
        )
    module = result.ir
    return SemanticCheckRecord(
        "passed",
        "complete",
        getattr(module, "name", None),
        _module_identity(module),
        (),
    )


def is_unspecialized_generic_diagnostic(
    source_text: str, diagnostic: ToolingDiagnostic
) -> bool:
    """Classify editor-only template diagnostics without weakening a build.

    The ordinary compiler check still rejects an unbound template.  Only an
    exact unresolved *declared* value parameter in its parsed constraint is
    treated like the existing generic-type specialization requirement while
    viewing the declaration without a specialization.
    """

    if diagnostic.code == "ZL-GENERIC-SPECIALIZATION-REQUIRED":
        return True
    if diagnostic.code != "ZL-SEMANTIC-PARAMETER-CONSTRAINT":
        return False
    try:
        module = parse(source_text)
    except ParseError:
        return False
    if module.parameter_constraint is None or diagnostic.primary is None:
        return False
    origin = module.parameter_constraint.origin
    if origin is None or diagnostic.primary.start_line != origin.start_line:
        return False
    prefix = (
        f"module '{module.name}' parameter constraint cannot be discharged: "
        "compile-time condition references runtime value "
    )
    return diagnostic.message.startswith(prefix) and any(
        f"runtime value '{parameter.name}'" in diagnostic.message
        for parameter in module.parameters
        if parameter.kind == "value" and parameter.default is None
    )


__all__ = [
    "TOOLING_API_SCHEMA",
    "TOOLING_DOCUMENT_SYMBOL_SCHEMA",
    "TOOLING_HOVER_SCHEMA",
    "TOOLING_DEFINITION_SCHEMA",
    "TOOLING_REFERENCE_SCHEMA",
    "TOOLING_RENAME_SCHEMA",
    "TOOLING_COMPLETION_SCHEMA",
    "TOOLING_SIGNATURE_HELP_SCHEMA",
    "TOOLING_SEMANTIC_TOKEN_SCHEMA",
    "TOOLING_DIAGNOSTIC_EDIT_SCHEMA",
    "EditorDocumentSnapshot",
    "EditorWorkspaceSnapshot",
    "ProjectLocation",
    "ResolvedImport",
    "SemanticCheckRecord",
    "SourceFacts",
    "ToolingDiagnostic",
    "ToolingDiagnosticEdit",
    "ToolingDiagnosticFix",
    "ToolingError",
    "ToolingSession",
    "ToolingHover",
    "ToolingDefinition",
    "ToolingReference",
    "ToolingRenameEdit",
    "ToolingRenameError",
    "ToolingCompletion",
    "ToolingSignatureHelp",
    "ToolingSemanticToken",
    "ToolingIdentity",
    "ToolingOrigin",
    "ToolingSymbol",
    "WorkspaceIndex",
    "WorkspaceModule",
    "check_snapshot",
    "document_symbols",
    "discover_project",
    "resolve_direct_imports",
    "source_facts",
    "unwritten_register_warnings",
    "tooling_identity",
    "hover_at",
    "definition_at",
    "references_at",
    "rename_at",
    "completion_at",
    "signature_help_at",
    "semantic_tokens",
    "workspace_index",
]
