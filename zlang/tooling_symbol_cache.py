"""Persistent compiler-owned symbol snapshot storage."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time

from zlang._version import __version__
from zlang.common.serialization import stable_digest
from zlang.dependencies import DependencyModelError
from zlang.definition_resolution import DefinitionResolution, DefinitionTarget
from zlang.module_resolver import ModuleResolutionError
from zlang.opt.identity import CANONICAL_IR_IDENTITY_SCHEMA
from zlang.parser import is_valid_identifier
from zlang.project import ProjectModelError, discover_project_manifest
from zlang.source import SourceOrigin
from zlang.source_identity import SOURCE_SUFFIX
from zlang import tooling_models as models
from zlang import tooling_workspace as tooling_workspace
from zlang import workspace as workspace_api

SYMBOL_CACHE_SCHEMA = 1
_SYMBOL_CACHE_ANALYSIS_SCHEMA = 6
_SYMBOL_MEMORY_MAX_ENTRIES = 64
_SYMBOL_MEMORY_MAX_BYTES = 64 * 1024 * 1024
_SYMBOL_DISK_MAX_ENTRIES = 512
_SYMBOL_DISK_MAX_BYTES = 256 * 1024 * 1024
_SYMBOL_DISK_MAX_SHARD_BYTES = 16 * 1024 * 1024
_SYMBOL_DISK_MAX_RECORDS = 200_000
_SYMBOL_DISK_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
_SYMBOL_DISK_TOUCH_INTERVAL_SECONDS = 24 * 60 * 60

def _symbol_root_inventory(inputs: models._SymbolPhysicalInputs) -> tuple[str, ...]:
    if inputs.project_manifest is None:
        return ()
    manifest = discover_project_manifest(
        inputs.project_manifest, explicit=inputs.project_manifest
    )
    if manifest is None:
        raise models.ToolingError("symbol cache project manifest disappeared")
    root = manifest.source_directory.resolve(strict=True)
    return tuple(sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*.zhl")
    ))


def _inventory_matches(entry: models._ToolingSnapshotEntry | models._ToolingSymbolEntry) -> bool:
    try:
        return entry.root_inventory == _symbol_root_inventory(entry.result.physical_inputs)
    except (OSError, ProjectModelError, models.ToolingError, ValueError):
        return False


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
        location = tooling_workspace.discover_project(source)
        if location is None:
            return None
        workspace = workspace_api.load_project_workspace(
            location.manifest_path,
            project=project or location.manifest_path,
        )
        if workspace is None:
            return None
        root_identity = workspace.root_identity_for(source)
        text_digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        if root_identity.digest != text_digest or models._digest_file(source) != text_digest:
            return None
        closure = workspace.compilation_closure_for(source)
        project_namespace = stable_digest({
            "project_root": location.project_root.as_posix(),
            "schema": SYMBOL_CACHE_SCHEMA,
        })
        lookup_identity = stable_digest({
            "analysis_schema": _SYMBOL_CACHE_ANALYSIS_SCHEMA,
            "capability_identity": models.tooling_identity().capability_identity,
            "compiler_schema": CANONICAL_IR_IDENTITY_SCHEMA,
            "compiler_version": __version__,
            "dependency_closure": closure.identity,
            "profile": profile,
            "root_digest": root_identity.digest,
            "root_source_unit": root_identity.logical_path,
            "schema": SYMBOL_CACHE_SCHEMA,
            "tooling_api_schema": models.TOOLING_API_SCHEMA,
            "top": top,
        })
        path = _symbol_cache_root() / project_namespace / f"{lookup_identity}.json"
        return path, lookup_identity, workspace, root_identity
    except (
        ModuleResolutionError,
        workspace_api.WorkspaceError,
        ProjectModelError,
        DependencyModelError,
        OSError,
        ValueError,
    ):
        return None


def _normalized_symbol_payload(
    source: Path,
    result: object,
    session: object,
) -> tuple[models._SymbolSnapshot, dict[str, object]]:
    """Deduplicate compiler observations into one deterministic symbol shard."""

    source_paths: dict[str, Path] = {}
    path_by_unit: dict[str | None, Path | None] = {}
    digest_by_unit: dict[str | None, str | None] = {}
    normalized_origins: dict[tuple[object, ...], SourceOrigin] = {}
    root_unit = tooling_workspace._root_logical_source(source, session)
    overlay_digests = {
        Path(path).expanduser().resolve(): digest
        for path, digest in getattr(
            result.physical_inputs, "editor_source_overlays", ()
        )
    }

    def normalize_origin(origin: SourceOrigin) -> SourceOrigin:
        coordinate = models._declaration_coordinate_key(origin)
        cached = normalized_origins.get(coordinate)
        if cached is not None:
            return cached
        unit = origin.source_unit
        if unit not in path_by_unit:
            path_by_unit[unit] = tooling_workspace._source_path_for_origin(
                source, origin, result.physical_inputs, session
            )
            resolved_path = path_by_unit[unit]
            digest_by_unit[unit] = (
                overlay_digests.get(resolved_path.resolve())
                if resolved_path is not None
                and resolved_path.resolve() in overlay_digests
                else (
                    models._digest_file(resolved_path)
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
            *models._declaration_coordinate_key(target),
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
            *models._declaration_coordinate_key(target),
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
        (name, kind, *models._declaration_coordinate_key(target)): stable_digest({
            "kind": kind,
            "name": name,
            "target": target.to_data(),
        })
        for target, name, kind in ordered_declarations
    }
    by_id = {
        declaration_ids[
            (name, kind, *models._declaration_coordinate_key(target))
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
                *models._declaration_coordinate_key(target),
            )
        ]
        key = (declaration_id, *models._origin_key(occurrence))
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
            *models._declaration_coordinate_key(target),
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
    physical_inputs = models._SymbolPhysicalInputs(
        source.resolve(),
        Path(project_manifest).resolve() if project_manifest is not None else None,
        Path(project_lock).resolve() if project_lock is not None else None,
        tuple(sorted(source_paths.items())),
    )
    dependencies = [
        {"digest": models._digest_file(path), "source_unit": unit}
        for unit, path in sorted(source_paths.items())
    ]
    payload: dict[str, object] = {
        "declarations": [
            {
                "declared": (
                    name, kind, *models._declaration_coordinate_key(target)
                ) in declared_keys,
                "id": declaration_ids[
                    (name, kind, *models._declaration_coordinate_key(target))
                ],
                "kind": kind,
                "name": name,
                "target": target.to_data(),
            }
            for target, name, kind in ordered_declarations
        ],
        "dependencies": dependencies,
        "manifest_digest": (
            models._digest_file(physical_inputs.project_manifest)
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
            models._digest_file(physical_inputs.project_lock)
            if physical_inputs.project_lock is not None
            else None
        ),
        "schema": SYMBOL_CACHE_SCHEMA,
    }
    serialized_size = len(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return (
        models._SymbolSnapshot(
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
) -> models._SymbolSnapshot | None:
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
            or raw["tooling_api_schema"] != models.TOOLING_API_SCHEMA
            or raw["compiler_schema"] != CANONICAL_IR_IDENTITY_SCHEMA
            or raw["compiler_version"] != __version__
            or raw["capability_identity"]
            != models.tooling_identity().capability_identity
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
            models._digest_file(source_paths[unit]) != digest
            for unit, digest in dependencies.items()
        ):
            raise ValueError("symbol cache dependency changed")

        manifest = Path(workspace.manifest.path).resolve()
        lock = (
            Path(workspace.lock_path).resolve()
            if workspace.lock_path is not None
            else None
        )
        if raw["manifest_digest"] != models._digest_file(manifest):
            raise ValueError("symbol cache manifest changed")
        if (
            raw["lock_digest"]
            != (models._digest_file(lock) if lock is not None else None)
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
            occurrence_key = (declaration_id, *models._origin_key(origin))
            if occurrence_key in seen_occurrences:
                raise ValueError("duplicate symbol cache occurrence")
            seen_occurrences.add(occurrence_key)
            resolutions.append(
                DefinitionResolution(origin, target, name, kind)
            )

        physical_inputs = models._SymbolPhysicalInputs(
            source.resolve(),
            manifest,
            lock,
            tuple(sorted(source_paths.items())),
        )
        snapshot = models._SymbolSnapshot(
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
        "capability_identity": models.tooling_identity().capability_identity,
        "compiler_schema": CANONICAL_IR_IDENTITY_SCHEMA,
        "compiler_version": __version__,
        "lookup_identity": lookup_identity,
        "tooling_api_schema": models.TOOLING_API_SCHEMA,
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
