"""Project discovery and logical-source mapping for compiler tooling."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from zlang.module_resolver import ModuleResolutionError, StdlibModuleResolver
from zlang.project import ProjectModelError, discover_project_manifest
from zlang import workspace as workspace_api
from zlang.tooling_models import (
    ProjectLocation, ResolvedImport, ToolingError, WorkspaceIndex, WorkspaceModule,
)

class WorkspaceIndexOwner(Protocol):
    def _workspace_index_for(self, manifest: Path) -> object: ...

def _root_logical_source(
    source: Path,
    session: WorkspaceIndexOwner | None = None,
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
    session: WorkspaceIndexOwner | None = None,
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


def resolve_direct_imports(
    source: Path | str,
    imports: tuple[str, ...],
) -> tuple[ResolvedImport, ...]:
    requested = tuple(dict.fromkeys(imports))
    if not requested:
        return ()
    path = Path(source)
    try:
        workspace = workspace_api.load_project_workspace(path)
    except workspace_api.WorkspaceError as error:
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
        workspace = workspace_api.load_project_workspace(selected, project=selected)
    except (workspace_api.WorkspaceError, ProjectModelError, ModuleResolutionError) as error:
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

