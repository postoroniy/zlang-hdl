"""Read-only project loading and explicit dependency-lock updates.

Ordinary compilation calls :func:`load_project_workspace`; that path performs
no writes and never invokes Git.  :func:`update_project_lock` is the sole
fetching/mutating operation and publishes the lock only after the complete
package graph and every source module validate.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Iterable

from zlang.common.graph import DependencyCycle, dependency_postorder
from zlang.dependencies import (
    DependencyClosure,
    DependencyModuleIdentity,
    DependencySourceKind,
    DependencySpec,
    LOCK_SCHEMA,
    LockedModule,
    LockedPackage,
)
from zlang.module_resolver import (
    IndexedModuleResolver,
    ModuleResolutionError,
    ResolvedModuleSource,
    load_indexed_module,
)
from zlang.project import (
    LockedExternalMapping,
    LockedExternalSource,
    ProjectLock,
    ProjectManifest,
    ProjectModelError,
    discover_project_manifest,
)
from zlang.source_identity import SOURCE_GLOB, SOURCE_SUFFIX


class WorkspaceError(ValueError):
    """A project cannot be locked or compiled reproducibly."""


@dataclass(frozen=True)
class ProjectWorkspace:
    """Fully validated immutable input to one project compilation."""

    manifest: ProjectManifest
    lock: ProjectLock
    resolver: IndexedModuleResolver
    dependency_closure: DependencyClosure
    root_modules: tuple[ResolvedModuleSource, ...]
    dependency_modules: tuple[ResolvedModuleSource, ...] = ()
    dependency_manifests: tuple[Path, ...] = ()
    lock_path: Path | None = None

    @property
    def external_source_paths(self) -> tuple[Path, ...]:
        return tuple(
            _external_source_path(self.manifest.project_root, source.relative_path)
            for mapping in self.lock.external_mappings
            for source in mapping.sources
        )

    @property
    def identity(self) -> str:
        return self.dependency_closure.identity

    def root_identity_for(self, source: Path | str) -> DependencyModuleIdentity:
        requested = Path(source).resolve(strict=True)
        for module in self.root_modules:
            if module.source_path.resolve(strict=True) == requested:
                return DependencyModuleIdentity(
                    module.logical_path,
                    module.digest,
                    module.package_identity or self.manifest.resolution_digest,
                )
        raise WorkspaceError(
            f"source '{source}' is not a module below project source-root "
            f"'{self.manifest.source_root.as_posix()}'"
        )

    def compilation_closure_for(self, source: Path | str) -> DependencyClosure:
        """Add transitive root-package imports to the exact locked closure."""

        root = self.root_identity_for(source)
        source_record = next(
            item for item in self.root_modules if item.logical_path == root.logical_path
        )
        try:
            resolved = self.resolver.resolve(
                source_record.dependencies,
                importer=root.logical_path,
            )
        except ModuleResolutionError as error:
            raise WorkspaceError(str(error)) from error
        identities = {
            item.logical_path: item for item in self.dependency_closure.modules
        }
        for item in resolved:
            package_identity = getattr(item, "package_identity", None)
            if package_identity is None or item.logical_path == root.logical_path:
                continue
            identities[item.logical_path] = DependencyModuleIdentity(
                item.logical_path,
                item.digest,
                package_identity,
                getattr(item, "package_revision", None),
            )
        return DependencyClosure(
            LOCK_SCHEMA,
            self.lock.identity,
            tuple(identities.values()),
        )


@dataclass(frozen=True)
class _PackageLocation:
    manifest: ProjectManifest
    root: Path
    spec: DependencySpec


def _external_source_path(project_root: Path, relative_path: str) -> Path:
    lexical = project_root / relative_path
    if lexical.is_symlink():
        raise WorkspaceError(
            f"external source '{relative_path}' must not be a symlink"
        )
    try:
        root = project_root.resolve(strict=True)
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(root)
    except FileNotFoundError as error:
        raise WorkspaceError(f"external source '{relative_path}' is unavailable") from error
    except ValueError:
        raise WorkspaceError(
            f"external source '{relative_path}' escapes project root"
        ) from None
    if not resolved.is_file():
        raise WorkspaceError(f"external source '{relative_path}' is not a file")
    return resolved


def _lock_external_mappings(manifest: ProjectManifest) -> tuple[LockedExternalMapping, ...]:
    mappings: list[LockedExternalMapping] = []
    for mapping in manifest.external_mappings:
        sources = tuple(
            LockedExternalSource(
                relative,
                hashlib.sha256(
                    _external_source_path(manifest.project_root, relative).read_bytes()
                ).hexdigest(),
            )
            for relative in mapping.sources
        )
        mappings.append(LockedExternalMapping(
            mapping.name,
            mapping.logical_module,
            mapping.backend,
            mapping.physical_module,
            mapping.ports,
            sources,
        ))
    return tuple(mappings)


def _validate_external_mappings(
    manifest: ProjectManifest,
    lock: ProjectLock,
) -> tuple[Path, ...]:
    expected = _lock_external_mappings(manifest)
    if expected != lock.external_mappings:
        raise WorkspaceError(
            "locked external mappings are missing, dirty, or do not match zlang.toml; "
            "run 'zlang-lock update'"
        )
    return tuple(
        _external_source_path(manifest.project_root, source.relative_path)
        for mapping in lock.external_mappings
        for source in mapping.sources
    )


def _cache_key(spec: DependencySpec) -> str:
    assert spec.kind is DependencySourceKind.GIT and spec.revision is not None
    digest = hashlib.sha256(
        f"{spec.package}\0{spec.locator}\0{spec.revision}".encode("utf-8")
    ).hexdigest()[:16]
    package = spec.package.replace(".", "-")
    return f"{package}-{spec.revision[:12]}-{digest}"


def _cache_root(project_root: Path) -> Path:
    return project_root / ".zlang" / "dependencies"


def _validated_state_root(project_root: Path, *, create: bool) -> Path:
    root = project_root.resolve(strict=True)
    state = root / ".zlang"
    if state.exists() and state.is_symlink():
        raise WorkspaceError("project .zlang state directory must not be a symlink")
    if create:
        state.mkdir(parents=True, exist_ok=True)
    if not state.exists():
        return state
    resolved = state.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError:
        raise WorkspaceError("project .zlang state directory escapes project root") from None
    if not resolved.is_dir():
        raise WorkspaceError("project .zlang state path is not a directory")
    cache = resolved / "dependencies"
    if cache.exists() and cache.is_symlink():
        raise WorkspaceError("project dependency cache must not be a symlink")
    return resolved


def _git_cache_path(
    project_root: Path,
    spec: DependencySpec,
    package_identity: str,
) -> Path:
    return _cache_root(project_root) / (
        _cache_key(spec) + "-" + package_identity[:12]
    )


def _logical_module(package: str, relative: Path) -> str:
    without_suffix = relative.with_suffix("")
    components = without_suffix.parts
    if not components:
        raise WorkspaceError(f"package '{package}' contains an unnamed source module")
    return ".".join((package, *components))


def _annotate_source(record: ResolvedModuleSource) -> ResolvedModuleSource:
    """Attach logical identity to declarations without changing source syntax."""

    module = record.ast
    identity = record.logical_path
    digest = record.digest

    def annotate_unit(unit):
        return replace(
            unit,
            source_identity=identity,
            source_hash=digest,
            enums=tuple(
                replace(item, source_identity=identity) for item in unit.enums
            ),
            structs=tuple(
                replace(item, source_identity=identity) for item in unit.structs
            ),
            functions=tuple(
                replace(item, source_identity=identity) for item in unit.functions
            ),
            operators=tuple(
                replace(item, source_identity=identity) for item in unit.operators
            ),
            module_interfaces=tuple(
                replace(item, source_identity=identity)
                for item in unit.module_interfaces
            ),
        )

    annotated = annotate_unit(module)
    annotated = replace(
        annotated,
        submodules=tuple(annotate_unit(child) for child in module.submodules),
    )
    return replace(record, ast=annotated)


def _safe_source_directory(manifest: ProjectManifest) -> Path:
    try:
        package_root = manifest.project_root.resolve(strict=True)
        source_root = manifest.source_directory.resolve(strict=True)
        source_root.relative_to(package_root)
    except FileNotFoundError as error:
        raise WorkspaceError(
            f"package '{manifest.package}' source-root is unavailable: "
            f"{manifest.source_directory}"
        ) from error
    except ValueError:
        raise WorkspaceError(
            f"package '{manifest.package}' source-root escapes its project root"
        ) from None
    if not source_root.is_dir():
        raise WorkspaceError(
            f"package '{manifest.package}' source-root is not a directory: {source_root}"
        )
    return source_root


def _source_roots_do_not_overlap(
    root_manifest: ProjectManifest,
    locations: dict[str, _PackageLocation],
) -> None:
    roots = {
        root_manifest.package: _safe_source_directory(root_manifest),
        **{
            name: _safe_source_directory(location.manifest)
            for name, location in locations.items()
        },
    }
    names = tuple(sorted(roots))
    state_root = (root_manifest.project_root / ".zlang").resolve()
    for index, left_name in enumerate(names):
        left = roots[left_name]
        for right_name in names[index + 1 :]:
            right = roots[right_name]
            if left == right or left in right.parents or right in left.parents:
                dependency_name = (
                    right_name if left_name == root_manifest.package else left_name
                )
                dependency_root = right if dependency_name == right_name else left
                dependency_location = locations.get(dependency_name)
                if (
                    dependency_location is not None
                    and dependency_location.spec.kind is DependencySourceKind.GIT
                    and dependency_root.is_relative_to(state_root)
                ):
                    # The compiler-owned cache is explicitly excluded from a
                    # root source-root='.' index and cannot alias user source.
                    continue
                raise WorkspaceError(
                    f"package source roots overlap: '{left_name}' and '{right_name}'"
                )


def _index_package(
    manifest: ProjectManifest,
    *,
    package_identity: str,
    package_revision: str | None = None,
    expected: tuple[LockedModule, ...] | None = None,
) -> tuple[tuple[ResolvedModuleSource, ...], tuple[LockedModule, ...]]:
    source_root = _safe_source_directory(manifest)
    discovered: dict[str, tuple[Path, Path]] = {}
    folded: dict[str, str] = {}
    for candidate in sorted(source_root.rglob(SOURCE_GLOB)):
        state_root = manifest.project_root / ".zlang"
        try:
            candidate.relative_to(state_root)
        except ValueError:
            pass
        else:
            # Compiler cache/state is never user source, including when the
            # package deliberately uses source-root='.'.
            continue
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(source_root)
        except (FileNotFoundError, ValueError):
            raise WorkspaceError(
                f"package '{manifest.package}' source escapes source-root: {candidate}"
            ) from None
        relative = candidate.relative_to(source_root)
        logical = _logical_module(manifest.package, relative)
        previous = folded.get(logical.casefold())
        if previous is not None:
            raise WorkspaceError(
                f"conflicting logical modules '{previous}' and '{logical}'"
            )
        folded[logical.casefold()] = logical
        discovered[logical] = (relative, resolved)
    if not discovered and expected is None:
        raise WorkspaceError(
            f"package '{manifest.package}' contains no {SOURCE_SUFFIX} modules"
        )

    expected_by_name = (
        {module.logical_path: module for module in expected}
        if expected is not None
        else None
    )
    if expected_by_name is not None and set(expected_by_name) != set(discovered):
        missing = sorted(set(expected_by_name) - set(discovered))
        added = sorted(set(discovered) - set(expected_by_name))
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if added:
            details.append("added " + ", ".join(added))
        raise WorkspaceError(
            f"locked package '{manifest.package}' module index is dirty: "
            + "; ".join(details)
        )
    if not discovered:
        raise WorkspaceError(
            f"package '{manifest.package}' contains no {SOURCE_SUFFIX} modules"
        )

    records: list[ResolvedModuleSource] = []
    locked: list[LockedModule] = []
    for logical in sorted(discovered):
        relative, _ = discovered[logical]
        expected_module = expected_by_name.get(logical) if expected_by_name else None
        try:
            record = load_indexed_module(
                logical,
                source_root=source_root,
                relative_path=relative.as_posix(),
                expected_digest=(expected_module.digest if expected_module else None),
                package_identity=package_identity,
                package_revision=package_revision,
            )
        except ModuleResolutionError as error:
            raise WorkspaceError(str(error)) from error
        record = _annotate_source(record)
        module_lock = LockedModule(
            logical,
            relative.as_posix(),
            record.digest,
            tuple(record.dependencies),
        )
        if expected_module is not None and module_lock != expected_module:
            raise WorkspaceError(
                f"locked module metadata is dirty for '{logical}'"
            )
        records.append(record)
        locked.append(module_lock)
    return tuple(records), tuple(locked)


def _load_manifest_at(root: Path) -> ProjectManifest:
    try:
        resolved_root = root.resolve(strict=True)
        manifest_path = (root / "zlang.toml").resolve(strict=True)
        manifest_path.relative_to(resolved_root)
    except FileNotFoundError as error:
        raise WorkspaceError(f"project manifest is unavailable below: {root}") from error
    except ValueError:
        raise WorkspaceError(f"project manifest escapes package root: {root}") from None
    if not manifest_path.is_file():
        raise WorkspaceError(f"project manifest is not a file: {manifest_path}")
    try:
        return ProjectManifest.load(manifest_path)
    except ProjectModelError as error:
        raise WorkspaceError(str(error)) from error


def _relative_locator(root: Path, target: Path) -> str:
    return Path(os.path.relpath(target, root)).as_posix()


def _validate_graph(
    root_manifest: ProjectManifest,
    lock: ProjectLock,
    locations: dict[str, _PackageLocation],
) -> None:
    locked = {package.name: package for package in lock.packages}
    if set(locked) != set(locations):
        missing = sorted(set(locked) - set(locations))
        extra = sorted(set(locations) - set(locked))
        details = []
        if missing:
            details.append("unavailable " + ", ".join(missing))
        if extra:
            details.append("unlocked " + ", ".join(extra))
        raise WorkspaceError("dependency graph does not match lock: " + "; ".join(details))

    def validate_edges(owner: ProjectManifest, dependencies: Iterable[DependencySpec]) -> None:
        owner_location = locations.get(owner.package)
        for spec in dependencies:
            location = locations.get(spec.package)
            package = locked.get(spec.package)
            if location is None or package is None:
                raise WorkspaceError(
                    f"dependency '{spec.package}' required by '{owner.package}' is not locked"
                )
            if spec.kind is not package.source_kind:
                raise WorkspaceError(
                    f"dependency source kind mismatch for '{spec.package}'"
                )
            if (
                owner_location is not None
                and owner_location.spec.kind is DependencySourceKind.GIT
                and spec.kind is DependencySourceKind.PATH
            ):
                raise WorkspaceError(
                    f"Git package '{owner.package}' cannot use path dependency "
                    f"'{spec.package}' in this project slice"
                )
            if spec.kind is DependencySourceKind.GIT:
                if spec.locator != package.source_locator or spec.revision != package.revision:
                    raise WorkspaceError(
                        f"Git lock mismatch for dependency '{spec.package}'"
                    )
            else:
                try:
                    expected = (owner.project_root / spec.locator).resolve(strict=True)
                    actual = location.root.resolve(strict=True)
                except FileNotFoundError as error:
                    raise WorkspaceError(
                        f"path dependency '{spec.package}' is unavailable"
                    ) from error
                if expected != actual:
                    raise WorkspaceError(
                        f"path lock mismatch for dependency '{spec.package}'"
                    )

    validate_edges(root_manifest, root_manifest.dependencies)
    for name, location in locations.items():
        package = locked[name]
        if location.manifest.package != name:
            raise WorkspaceError(
                f"dependency key '{name}' does not match package "
                f"'{location.manifest.package}'"
            )
        if location.manifest.version != package.version:
            raise WorkspaceError(f"locked version mismatch for package '{name}'")
        if location.manifest.resolution_digest != package.manifest_digest:
            raise WorkspaceError(f"locked manifest for package '{name}' is dirty")
        if tuple(item.package for item in location.manifest.dependencies) != package.dependencies:
            raise WorkspaceError(f"locked dependency edges for package '{name}' are dirty")
        validate_edges(location.manifest, location.manifest.dependencies)

    try:
        ordered = dependency_postorder(
            (dependency.package for dependency in root_manifest.dependencies),
            lambda name: locked[name].dependencies,
        )
    except DependencyCycle as error:
        raise WorkspaceError(f"dependency cycle: {error}") from error
    visited = set(ordered)
    if visited != set(locked):
        unreachable = ", ".join(sorted(set(locked) - visited))
        raise WorkspaceError(f"lock contains unreachable packages: {unreachable}")
    _source_roots_do_not_overlap(root_manifest, locations)


def _validate_module_graph(
    root_manifest: ProjectManifest,
    lock: ProjectLock,
    root_records: tuple[ResolvedModuleSource, ...],
    dependency_records: tuple[ResolvedModuleSource, ...],
) -> IndexedModuleResolver:
    """Validate every indexed module, including unused dependency modules."""

    packages = {item.name: item for item in lock.packages}
    namespaces = (root_manifest.package, *packages)
    resolver = IndexedModuleResolver(
        (*root_records, *dependency_records),
        package_namespaces=namespaces,
    )
    ordered_namespaces = tuple(sorted(namespaces, key=len, reverse=True))

    def owner_of(logical: str) -> str:
        owner = next(
            (
                namespace
                for namespace in ordered_namespaces
                if logical.startswith(namespace + ".")
            ),
            None,
        )
        if owner is None:
            raise WorkspaceError(f"logical module '{logical}' has no package owner")
        return owner

    root_allowed = {item.package for item in root_manifest.dependencies}
    for record in (*root_records, *dependency_records):
        owner = owner_of(record.logical_path)
        allowed = (
            root_allowed
            if owner == root_manifest.package
            else set(packages[owner].dependencies)
        )
        for imported in record.dependencies:
            if imported.startswith("std.") or imported.startswith(owner + "."):
                continue
            imported_owner = next(
                (
                    namespace
                    for namespace in ordered_namespaces
                    if imported.startswith(namespace + ".")
                ),
                None,
            )
            if imported_owner is None:
                # Let the resolver render the stable unknown-import diagnostic.
                continue
            if imported_owner not in allowed:
                raise WorkspaceError(
                    f"module '{record.logical_path}' imports package "
                    f"'{imported_owner}' without declaring it as a dependency"
                )
        try:
            resolver.resolve(record.dependencies, importer=record.logical_path)
        except ModuleResolutionError as error:
            raise WorkspaceError(str(error)) from error
    return resolver


def load_project_workspace(
    source: Path | str,
    *,
    project: Path | str | None = None,
) -> ProjectWorkspace | None:
    """Load one project without writes/fetches, or return no-project mode."""

    try:
        manifest = discover_project_manifest(source, explicit=project)
    except ProjectModelError as error:
        raise WorkspaceError(str(error)) from error
    if manifest is None:
        return None
    lock_path = manifest.project_root / "zlang.lock"
    try:
        lock = ProjectLock.load(lock_path)
    except ProjectModelError as error:
        raise WorkspaceError(str(error)) from error
    if lock.manifest_resolution_digest != manifest.resolution_digest:
        raise WorkspaceError(
            "zlang.lock does not match zlang.toml; run 'zlang-lock update'"
        )
    _validate_external_mappings(manifest, lock)

    locations: dict[str, _PackageLocation] = {}
    for package in lock.packages:
        spec = DependencySpec(
            package.name,
            package.source_kind,
            package.source_locator,
            package.revision,
        )
        if package.source_kind is DependencySourceKind.PATH:
            root = manifest.project_root / package.source_locator
        else:
            state_root = _validated_state_root(manifest.project_root, create=False)
            cache_root = state_root / "dependencies"
            root = _git_cache_path(manifest.project_root, spec, package.identity)
            try:
                root.resolve(strict=True).relative_to(cache_root.resolve(strict=True))
            except (FileNotFoundError, ValueError):
                raise WorkspaceError(
                    f"Git cache for dependency '{package.name}' is unavailable or escapes cache root"
                ) from None
        dependency_manifest = _load_manifest_at(root)
        locations[package.name] = _PackageLocation(dependency_manifest, root, spec)
    _validate_graph(manifest, lock, locations)

    root_records, _ = _index_package(
        manifest,
        package_identity=manifest.resolution_digest,
    )
    dependency_records: list[ResolvedModuleSource] = []
    identities: list[DependencyModuleIdentity] = []
    for package in lock.packages:
        location = locations[package.name]
        records, _ = _index_package(
            location.manifest,
            package_identity=package.identity,
            package_revision=package.revision,
            expected=package.modules,
        )
        dependency_records.extend(records)
        identities.extend(
            DependencyModuleIdentity(
                record.logical_path,
                record.digest,
                package.identity,
                package.revision,
            )
            for record in records
        )
    resolver = _validate_module_graph(
        manifest,
        lock,
        root_records,
        tuple(dependency_records),
    )
    closure = DependencyClosure(LOCK_SCHEMA, lock.identity, tuple(identities))
    return ProjectWorkspace(
        manifest,
        lock,
        resolver,
        closure,
        root_records,
        tuple(dependency_records),
        tuple(
            locations[name].manifest.path
            for name in sorted(locations)
        ),
        lock_path,
    )


def _clone_git(spec: DependencySpec, destination: Path) -> None:
    assert spec.kind is DependencySourceKind.GIT and spec.revision is not None
    repository = destination.parent / (destination.name + ".repository")
    try:
        clone = subprocess.run(
            ("git", "clone", "--quiet", "--no-checkout", spec.locator, str(repository)),
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError as error:
        raise WorkspaceError("Git executable is unavailable for lock update") from error
    if clone.returncode != 0:
        raise WorkspaceError(
            f"cannot fetch Git dependency '{spec.package}': "
            f"{(clone.stderr or clone.stdout).strip()}"
        )
    checkout = subprocess.run(
        ("git", "-C", str(repository), "checkout", "--quiet", "--detach", spec.revision),
        check=False,
        text=True,
        capture_output=True,
    )
    if checkout.returncode != 0:
        raise WorkspaceError(
            f"Git revision for '{spec.package}' is unavailable: "
            f"{(checkout.stderr or checkout.stdout).strip()}"
        )
    actual = subprocess.run(
        ("git", "-C", str(repository), "rev-parse", "HEAD"),
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip().lower()
    if actual != spec.revision:
        raise WorkspaceError(
            f"Git revision mismatch for '{spec.package}': expected "
            f"{spec.revision}, found {actual}"
        )
    shutil.copytree(
        repository,
        destination,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git"),
    )


def update_project_lock(project: Path | str = Path("zlang.toml")) -> ProjectLock:
    """Resolve/fetch the complete graph and atomically publish ``zlang.lock``."""

    try:
        manifest = discover_project_manifest(project, explicit=project)
    except ProjectModelError as error:
        raise WorkspaceError(str(error)) from error
    assert manifest is not None
    state_root = _validated_state_root(manifest.project_root, create=True)
    locations: dict[str, _PackageLocation] = {}
    specs: dict[str, DependencySpec] = {}
    resolved_roots: dict[str, Path] = {}
    packages: dict[str, LockedPackage] = {}
    active: list[str] = []

    with tempfile.TemporaryDirectory(prefix="lock-update-", dir=state_root) as temporary:
        staging = Path(temporary)
        git_staging: dict[str, Path] = {}

        def resolve_spec(owner: ProjectManifest, spec: DependencySpec) -> Path:
            existing = specs.get(spec.package)
            if spec.kind is DependencySourceKind.PATH:
                try:
                    resolved = (owner.project_root / spec.locator).resolve(strict=True)
                except FileNotFoundError as error:
                    raise WorkspaceError(
                        f"path dependency '{spec.package}' is unavailable"
                    ) from error
                if existing is not None:
                    if existing.kind is not DependencySourceKind.PATH:
                        raise WorkspaceError(
                            f"conflicting dependency declarations for '{spec.package}'"
                        )
                    if resolved_roots[spec.package] != resolved:
                        raise WorkspaceError(
                            f"dependency '{spec.package}' resolves to conflicting locations"
                        )
                    return resolved
                specs[spec.package] = spec
                resolved_roots[spec.package] = resolved
                return resolved
            if existing is not None:
                if existing != spec:
                    raise WorkspaceError(
                        f"conflicting dependency declarations for '{spec.package}'"
                    )
                return resolved_roots[spec.package]
            specs[spec.package] = spec
            key = _cache_key(spec)
            if key not in git_staging:
                destination = staging / key
                _clone_git(spec, destination)
                git_staging[key] = destination
            resolved_roots[spec.package] = git_staging[key]
            return resolved_roots[spec.package]

        def visit(owner: ProjectManifest, spec: DependencySpec) -> None:
            if spec.package in packages:
                resolved = resolve_spec(owner, spec)
                if resolved.resolve() != locations[spec.package].root.resolve():
                    raise WorkspaceError(
                        f"dependency '{spec.package}' resolves to conflicting locations"
                    )
                return
            if spec.package in active:
                cycle = " -> ".join((*active[active.index(spec.package):], spec.package))
                raise WorkspaceError(f"dependency cycle: {cycle}")
            active.append(spec.package)
            root = resolve_spec(owner, spec)
            dependency_manifest = _load_manifest_at(root)
            if dependency_manifest.package != spec.package:
                raise WorkspaceError(
                    f"dependency key '{spec.package}' does not match package "
                    f"'{dependency_manifest.package}'"
                )
            if (
                spec.kind is DependencySourceKind.GIT
                and any(
                    item.kind is DependencySourceKind.PATH
                    for item in dependency_manifest.dependencies
                )
            ):
                child = next(
                    item.package
                    for item in dependency_manifest.dependencies
                    if item.kind is DependencySourceKind.PATH
                )
                raise WorkspaceError(
                    f"Git package '{spec.package}' cannot use path dependency "
                    f"'{child}' in this project slice"
                )
            for dependency in dependency_manifest.dependencies:
                visit(dependency_manifest, dependency)
            source_locator = (
                _relative_locator(manifest.project_root, root)
                if spec.kind is DependencySourceKind.PATH
                else spec.locator
            )
            provisional = LockedPackage(
                dependency_manifest.package,
                dependency_manifest.version,
                spec.kind,
                source_locator,
                spec.revision,
                dependency_manifest.resolution_digest,
                tuple(item.package for item in dependency_manifest.dependencies),
                (),
            )
            _, modules = _index_package(
                dependency_manifest,
                package_identity=provisional.identity,
                package_revision=spec.revision,
            )
            package = replace(provisional, modules=modules)
            packages[spec.package] = package
            locations[spec.package] = _PackageLocation(dependency_manifest, root, spec)
            active.pop()

        for dependency in manifest.dependencies:
            visit(manifest, dependency)

        lock = ProjectLock(
            LOCK_SCHEMA,
            manifest.resolution_digest,
            tuple(packages.values()),
            _lock_external_mappings(manifest),
        )
        _validate_graph(manifest, lock, locations)
        root_records, _ = _index_package(
            manifest,
            package_identity=manifest.resolution_digest,
        )
        dependency_records: list[ResolvedModuleSource] = []
        for package in lock.packages:
            package_records, _ = _index_package(
                locations[package.name].manifest,
                package_identity=package.identity,
                package_revision=package.revision,
                expected=package.modules,
            )
            dependency_records.extend(package_records)
        _validate_module_graph(
            manifest,
            lock,
            root_records,
            tuple(dependency_records),
        )

        cache_root = _cache_root(manifest.project_root)
        cache_root.mkdir(parents=True, exist_ok=True)
        for package in lock.packages:
            if package.source_kind is not DependencySourceKind.GIT:
                continue
            spec = specs[package.name]
            source = git_staging[_cache_key(spec)]
            destination = _git_cache_path(
                manifest.project_root,
                spec,
                package.identity,
            )
            if destination.exists():
                try:
                    existing_manifest = _load_manifest_at(destination)
                    if (
                        existing_manifest.package != package.name
                        or existing_manifest.version != package.version
                        or existing_manifest.resolution_digest != package.manifest_digest
                        or tuple(
                            item.package for item in existing_manifest.dependencies
                        ) != package.dependencies
                    ):
                        raise WorkspaceError(
                            f"cached Git manifest for '{package.name}' is dirty"
                        )
                    _, existing_modules = _index_package(
                        existing_manifest,
                        package_identity=package.identity,
                        package_revision=package.revision,
                        expected=package.modules,
                    )
                except WorkspaceError as error:
                    raise WorkspaceError(
                        f"cached Git package '{package.name}' is dirty; "
                        "remove that cache entry and rerun zlang-lock update"
                    ) from error
                else:
                    assert existing_modules == package.modules
            else:
                source.rename(destination)

        lock_path = manifest.project_root / "zlang.lock"
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=manifest.project_root,
            prefix=".zlang.lock.",
            delete=False,
        ) as output:
            output.write(lock.render())
            output.flush()
            os.fsync(output.fileno())
            staged_lock = Path(output.name)
        os.replace(staged_lock, lock_path)
        return lock


__all__ = [
    "ProjectWorkspace",
    "WorkspaceError",
    "load_project_workspace",
    "update_project_lock",
]
