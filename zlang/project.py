"""Versioned ZLang project and dependency-lock document models.

Parsing and discovery are deliberately read-only.  Fetching, cache population,
and atomic lock publication belong to the explicit lock-update command rather
than ordinary compilation or these data models.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tomllib
from types import MappingProxyType
from typing import Mapping

from zlang.common.graph import DependencyCycle, dependency_postorder
from zlang.common.serialization import stable_digest
from zlang.dependencies import (
    DependencyModelError,
    DependencySourceKind,
    DependencySpec,
    LOCK_SCHEMA,
    LockedModule,
    LockedPackage,
    validate_digest,
    validate_package_name,
)


PROJECT_SCHEMA = 1

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+\Z")


class ProjectModelError(ValueError):
    """A ``zlang.toml`` or ``zlang.lock`` document is malformed."""


@dataclass(frozen=True)
class ExternalMappingSpec:
    """One portable, backend-physical external implementation declaration."""

    name: str
    logical_module: str
    backend: str
    physical_module: str
    ports: tuple[tuple[str, str], ...]
    sources: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.name:
            raise ProjectModelError("external mapping name must not be empty")
        if not self.logical_module:
            raise ProjectModelError("external mapping logical-module must not be empty")
        if self.backend != "systemverilog":
            raise ProjectModelError(
                "bounded external mappings support only backend 'systemverilog'"
            )
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", self.physical_module) is None:
            raise ProjectModelError(
                "external mapping physical-module is not an HDL identifier"
            )
        ports = tuple(self.ports)
        if not ports:
            raise ProjectModelError("external mapping ports must not be empty")
        _reject_casefold_duplicates(tuple(item[0] for item in ports), "external semantic port")
        _reject_casefold_duplicates(tuple(item[1] for item in ports), "external physical port")
        if any(
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", physical) is None
            for _, physical in ports
        ):
            raise ProjectModelError("external physical port is not an HDL identifier")
        sources = tuple(_validate_relative_file_path(item, "external source") for item in self.sources)
        if not sources:
            raise ProjectModelError("external mapping sources must not be empty")
        _reject_casefold_duplicates(sources, "external source")
        object.__setattr__(self, "ports", tuple(sorted(ports)))
        object.__setattr__(self, "sources", sources)

    def to_data(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "logical_module": self.logical_module,
            "name": self.name,
            "physical_module": self.physical_module,
            "ports": [[left, right] for left, right in self.ports],
            "sources": list(self.sources),
        }


@dataclass(frozen=True)
class LockedExternalSource:
    relative_path: str
    digest: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "relative_path",
            _validate_relative_file_path(self.relative_path, "locked external source"),
        )
        try:
            validate_digest(self.digest, description="locked external source digest")
        except DependencyModelError as error:
            raise ProjectModelError(str(error)) from error


@dataclass(frozen=True)
class LockedExternalMapping:
    name: str
    logical_module: str
    backend: str
    physical_module: str
    ports: tuple[tuple[str, str], ...]
    sources: tuple[LockedExternalSource, ...]

    def __post_init__(self) -> None:
        spec = ExternalMappingSpec(
            self.name,
            self.logical_module,
            self.backend,
            self.physical_module,
            self.ports,
            tuple(item.relative_path for item in self.sources),
        )
        object.__setattr__(self, "ports", spec.ports)
        object.__setattr__(self, "sources", tuple(self.sources))

    @property
    def identity(self) -> str:
        return stable_digest(self.to_data())

    def to_data(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "logical_module": self.logical_module,
            "name": self.name,
            "physical_module": self.physical_module,
            "ports": [[left, right] for left, right in self.ports],
            "sources": [
                {"digest": item.digest, "relative_path": item.relative_path}
                for item in self.sources
            ],
        }


@dataclass(frozen=True)
class ProjectManifest:
    """Portable, versioned contents of one ``zlang.toml`` document."""

    schema: int
    path: Path
    package: str
    version: str
    source_root: Path
    dependencies: tuple[DependencySpec, ...] = ()
    # Profiles are retained for forward-compatible project-file round trips,
    # but intentionally excluded from dependency resolution identity.
    profiles: Mapping[str, object] = field(default_factory=dict, repr=False)
    content_digest: str | None = field(default=None, compare=False, repr=False)
    external_mappings: tuple[ExternalMappingSpec, ...] = ()

    def __post_init__(self) -> None:
        if self.schema != PROJECT_SCHEMA:
            raise ProjectModelError(f"unsupported project schema {self.schema}")
        object.__setattr__(self, "path", Path(self.path))
        validate_package_name(self.package)
        if not isinstance(self.version, str) or not self.version:
            raise ProjectModelError("project version must be a non-empty string")
        object.__setattr__(self, "source_root", _validate_source_root(self.source_root))
        dependencies = tuple(self.dependencies)
        _reject_casefold_duplicates(
            tuple(item.package for item in dependencies), "project dependency"
        )
        object.__setattr__(
            self, "dependencies", tuple(sorted(dependencies, key=lambda item: item.package))
        )
        object.__setattr__(self, "profiles", _freeze_table(self.profiles, "profiles"))
        mappings = tuple(self.external_mappings)
        _reject_casefold_duplicates(
            tuple(item.name for item in mappings), "external mapping"
        )
        _reject_casefold_duplicates(
            tuple(item.logical_module for item in mappings),
            "external logical module",
        )
        object.__setattr__(
            self, "external_mappings", tuple(sorted(mappings, key=lambda item: item.name))
        )
        if self.content_digest is not None:
            try:
                validate_digest(self.content_digest, description="manifest content digest")
            except DependencyModelError as error:
                raise ProjectModelError(str(error)) from error

    @property
    def project_root(self) -> Path:
        return self.path.parent

    @property
    def source_directory(self) -> Path:
        """Physical source directory; never part of a portable identity."""
        return self.project_root / self.source_root

    @property
    def resolution_digest(self) -> str:
        return stable_digest(self.to_resolution_data())

    @property
    def manifest_digest(self) -> str:
        """Exact loaded bytes, or canonical rendered bytes for an in-memory model."""
        if self.content_digest is not None:
            return self.content_digest
        return hashlib.sha256(self.render().encode("utf-8")).hexdigest()

    def to_resolution_data(self) -> dict[str, object]:
        """Canonical dependency input, deliberately excluding profile tables."""
        data = {
            "dependencies": [item.to_data() for item in self.dependencies],
            "package": {
                "name": self.package,
                "source_root": self.source_root.as_posix(),
                "version": self.version,
            },
            "schema": self.schema,
        }
        return data

    def to_data(self, *, include_profiles: bool = True) -> dict[str, object]:
        data = self.to_resolution_data()
        if self.external_mappings:
            data["external_mappings"] = [
                item.to_data() for item in self.external_mappings
            ]
        if include_profiles:
            data["profiles"] = _thaw_table(self.profiles)
        return data

    def render(self) -> str:
        lines = [
            f"schema = {self.schema}",
            "",
            "[project]",
            f"name = {_toml_string(self.package)}",
            f"version = {_toml_string(self.version)}",
            f"source-root = {_toml_string(self.source_root.as_posix())}",
        ]
        if self.dependencies:
            lines.extend(("", "[dependencies]"))
            for dependency in self.dependencies:
                key = _toml_string(dependency.package)
                if dependency.kind is DependencySourceKind.PATH:
                    value = f"{{ path = {_toml_string(dependency.locator)} }}"
                else:
                    value = (
                        f"{{ git = {_toml_string(dependency.locator)}, "
                        f"rev = {_toml_string(dependency.revision or '')} }}"
                    )
                lines.append(f"{key} = {value}")
        if self.profiles:
            _render_mapping_tables(lines, ("profiles",), self.profiles)
        for mapping in self.external_mappings:
            lines.extend((
                "",
                "[external-mappings." + _toml_key(mapping.name) + "]",
                f"logical-module = {_toml_string(mapping.logical_module)}",
                f"backend = {_toml_string(mapping.backend)}",
                f"physical-module = {_toml_string(mapping.physical_module)}",
                f"sources = {_toml_array(mapping.sources)}",
                "",
                "[external-mappings." + _toml_key(mapping.name) + ".ports]",
            ))
            lines.extend(
                f"{_toml_key(semantic)} = {_toml_string(physical)}"
                for semantic, physical in mapping.ports
            )
        return "\n".join(lines) + "\n"

    @classmethod
    def from_data(
        cls,
        data: Mapping[str, object],
        *,
        path: Path | str = Path("zlang.toml"),
    ) -> "ProjectManifest":
        _require_keys(
            data,
            {"schema", "package", "dependencies"},
            {"profiles", "external_mappings"},
            "project manifest",
        )
        package = _mapping(data["package"], "project package")
        _require_keys(package, {"name", "version", "source_root"}, set(), "project package")
        raw_dependencies = data["dependencies"]
        if not isinstance(raw_dependencies, list):
            raise ProjectModelError("project dependencies must be an array")
        try:
            return cls(
                _integer(data["schema"], "project schema"),
                Path(path),
                _string(package["name"], "project name"),
                _string(package["version"], "project version"),
                Path(_string(package["source_root"], "project source root")),
                tuple(
                    DependencySpec.from_data(_mapping(item, "project dependency"))
                    for item in raw_dependencies
                ),
                _mapping(data.get("profiles", {}), "profiles table"),
                None,
                tuple(
                    _external_mapping_from_data(item)
                    for item in _list(data.get("external_mappings", []), "external mappings")
                ),
            )
        except DependencyModelError as error:
            raise ProjectModelError(str(error)) from error

    @classmethod
    def parse(cls, text: str, *, path: Path | str = Path("zlang.toml")) -> "ProjectManifest":
        try:
            raw = tomllib.loads(text)
        except (tomllib.TOMLDecodeError, UnicodeError) as error:
            raise ProjectModelError(f"invalid zlang.toml: {error}") from error
        _require_keys(
            raw,
            {"schema", "project"},
            {"dependencies", "profiles", "external-mappings"},
            "project manifest",
        )
        schema = _integer(raw["schema"], "project schema")
        project = _mapping(raw["project"], "project table")
        _require_keys(project, {"name", "version", "source-root"}, set(), "project table")
        raw_dependencies = _mapping(raw.get("dependencies", {}), "dependencies table")
        dependencies: list[DependencySpec] = []
        for package, value in raw_dependencies.items():
            validate_package_name(package)
            spec = _mapping(value, f"dependency '{package}'")
            keys = set(spec)
            if keys == {"path"}:
                dependencies.append(DependencySpec(
                    package, DependencySourceKind.PATH,
                    _string(spec["path"], f"dependency '{package}' path"),
                ))
            elif keys == {"git", "rev"}:
                dependencies.append(DependencySpec(
                    package, DependencySourceKind.GIT,
                    _string(spec["git"], f"dependency '{package}' Git locator"),
                    _string(spec["rev"], f"dependency '{package}' revision"),
                ))
            elif "path" in keys and ({"git", "rev"} & keys):
                raise ProjectModelError(
                    f"dependency '{package}' must declare exactly one of path or Git"
                )
            else:
                unknown = keys - {"path", "git", "rev"}
                if unknown:
                    raise ProjectModelError(
                        f"unknown dependency '{package}' key '{sorted(unknown)[0]}'"
                    )
                raise ProjectModelError(
                    f"dependency '{package}' must be {{path=...}} or {{git=..., rev=...}}"
                )
        profiles = _mapping(raw.get("profiles", {}), "profiles table")
        external_mappings = _parse_external_mapping_table(
            _mapping(raw.get("external-mappings", {}), "external-mappings table")
        )
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        try:
            return cls(
                schema,
                Path(path),
                _string(project["name"], "project name"),
                _string(project["version"], "project version"),
                Path(_string(project["source-root"], "project source-root")),
                tuple(dependencies),
                profiles,
                digest,
                external_mappings,
            )
        except DependencyModelError as error:
            raise ProjectModelError(str(error)) from error

    @classmethod
    def load(cls, path: Path | str) -> "ProjectManifest":
        source = Path(path)
        try:
            payload = source.read_bytes()
        except FileNotFoundError as error:
            raise ProjectModelError(f"project manifest is unavailable: {source}") from error
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ProjectModelError(f"project manifest is not UTF-8: {source}") from error
        manifest = cls.parse(text, path=source)
        object.__setattr__(manifest, "content_digest", hashlib.sha256(payload).hexdigest())
        return manifest


@dataclass(frozen=True)
class ProjectLock:
    """Exact deterministic ``zlang.lock`` dependency index."""

    schema: int
    manifest_resolution_digest: str
    packages: tuple[LockedPackage, ...] = ()
    external_mappings: tuple[LockedExternalMapping, ...] = ()

    def __post_init__(self) -> None:
        if self.schema != LOCK_SCHEMA:
            raise ProjectModelError(f"unsupported lock schema {self.schema}")
        try:
            validate_digest(
                self.manifest_resolution_digest,
                description="manifest resolution digest",
            )
        except DependencyModelError as error:
            raise ProjectModelError(str(error)) from error
        packages = tuple(self.packages)
        _reject_casefold_duplicates(tuple(item.name for item in packages), "locked package")
        package_names = {item.name for item in packages}
        for package in packages:
            for dependency in package.dependencies:
                if dependency == package.name:
                    raise ProjectModelError(
                        f"locked package '{package.name}' depends on itself"
                    )
                if dependency not in package_names:
                    raise ProjectModelError(
                        f"locked package '{package.name}' references unavailable package "
                        f"'{dependency}'"
                    )
        module_paths = tuple(
            module.logical_path for package in packages for module in package.modules
        )
        _reject_casefold_duplicates(module_paths, "locked module")
        _reject_package_cycles(packages)
        object.__setattr__(self, "packages", tuple(sorted(packages, key=lambda item: item.name)))
        mappings = tuple(self.external_mappings)
        _reject_casefold_duplicates(
            tuple(item.name for item in mappings), "locked external mapping"
        )
        object.__setattr__(
            self,
            "external_mappings",
            tuple(sorted(mappings, key=lambda item: item.name)),
        )

    @property
    def identity(self) -> str:
        # Physical external HDL is lock-pinned but never changes the semantic
        # dependency closure.  BackendArtifact text/hash carries that identity.
        return stable_digest({
            "manifest_resolution_digest": self.manifest_resolution_digest,
            "packages": [package.to_data() for package in self.packages],
            "schema": self.schema,
        })

    def to_data(self) -> dict[str, object]:
        data = {
            "manifest_resolution_digest": self.manifest_resolution_digest,
            "packages": [package.to_data() for package in self.packages],
            "schema": self.schema,
        }
        if self.external_mappings:
            data["external_mappings"] = [
                item.to_data() for item in self.external_mappings
            ]
        return data

    def package(self, name: str) -> LockedPackage:
        for package in self.packages:
            if package.name == name:
                return package
        raise KeyError(name)

    def module(self, logical_path: str) -> LockedModule:
        for package in self.packages:
            for module in package.modules:
                if module.logical_path == logical_path:
                    return module
        raise KeyError(logical_path)

    def render(self) -> str:
        lines = [
            f"schema = {self.schema}",
            f"manifest-resolution-digest = {_toml_string(self.manifest_resolution_digest)}",
        ]
        for package in self.packages:
            lines.extend((
                "",
                "[[packages]]",
                f"name = {_toml_string(package.name)}",
                f"version = {_toml_string(package.version)}",
                f"source-kind = {_toml_string(package.source_kind.value)}",
                f"source-locator = {_toml_string(package.source_locator)}",
            ))
            if package.revision is not None:
                lines.append(f"revision = {_toml_string(package.revision)}")
            lines.extend((
                f"manifest-digest = {_toml_string(package.manifest_digest)}",
                f"dependencies = {_toml_array(package.dependencies)}",
            ))
            for module in package.modules:
                lines.extend((
                    "",
                    "[[packages.modules]]",
                    f"logical-path = {_toml_string(module.logical_path)}",
                    f"relative-path = {_toml_string(module.relative_path)}",
                    f"digest = {_toml_string(module.digest)}",
                    f"imports = {_toml_array(module.imports)}",
                ))
        for mapping in self.external_mappings:
            lines.extend((
                "",
                "[[external-mappings]]",
                f"name = {_toml_string(mapping.name)}",
                f"logical-module = {_toml_string(mapping.logical_module)}",
                f"backend = {_toml_string(mapping.backend)}",
                f"physical-module = {_toml_string(mapping.physical_module)}",
            ))
            for semantic, physical in mapping.ports:
                lines.append(
                    "ports = "
                    + _toml_array(tuple(f"{semantic}={physical}" for semantic, physical in mapping.ports))
                )
                break
            for source in mapping.sources:
                lines.extend((
                    "",
                    "[[external-mappings.sources]]",
                    f"relative-path = {_toml_string(source.relative_path)}",
                    f"digest = {_toml_string(source.digest)}",
                ))
        return "\n".join(lines) + "\n"

    @classmethod
    def parse(cls, text: str) -> "ProjectLock":
        try:
            raw = tomllib.loads(text)
        except (tomllib.TOMLDecodeError, UnicodeError) as error:
            raise ProjectModelError(f"invalid zlang.lock: {error}") from error
        _require_keys(
            raw,
            {"schema", "manifest-resolution-digest"},
            {"packages", "external-mappings"},
            "project lock",
        )
        raw_packages = raw.get("packages", [])
        if not isinstance(raw_packages, list):
            raise ProjectModelError("lock packages must be an array of tables")
        packages: list[LockedPackage] = []
        for value in raw_packages:
            package = _mapping(value, "locked package")
            required = {
                "name", "version", "source-kind", "source-locator",
                "manifest-digest", "dependencies",
            }
            _require_keys(package, required, {"revision", "modules"}, "locked package")
            raw_modules = package.get("modules", [])
            if not isinstance(raw_modules, list):
                raise ProjectModelError("locked package modules must be an array of tables")
            modules: list[LockedModule] = []
            for module_value in raw_modules:
                module = _mapping(module_value, "locked module")
                _require_keys(
                    module,
                    {"logical-path", "relative-path", "digest", "imports"},
                    set(),
                    "locked module",
                )
                modules.append(LockedModule(
                    _string(module["logical-path"], "module logical-path"),
                    _string(module["relative-path"], "module relative-path"),
                    _string(module["digest"], "module digest"),
                    _string_tuple(module["imports"], "module imports"),
                ))
            try:
                packages.append(LockedPackage(
                    _string(package["name"], "package name"),
                    _string(package["version"], "package version"),
                    DependencySourceKind(_string(package["source-kind"], "package source-kind")),
                    _string(package["source-locator"], "package source-locator"),
                    _optional_string(package.get("revision"), "package revision"),
                    _string(package["manifest-digest"], "package manifest-digest"),
                    _string_tuple(package["dependencies"], "package dependencies"),
                    tuple(modules),
                ))
            except (DependencyModelError, ValueError) as error:
                raise ProjectModelError(str(error)) from error
        raw_external_mappings = raw.get("external-mappings", [])
        if not isinstance(raw_external_mappings, list):
            raise ProjectModelError("locked external mappings must be an array of tables")
        external_mappings = tuple(
            _locked_external_mapping_from_toml(value)
            for value in raw_external_mappings
        )
        try:
            return cls(
                _integer(raw["schema"], "lock schema"),
                _string(raw["manifest-resolution-digest"], "manifest resolution digest"),
                tuple(packages),
                external_mappings,
            )
        except DependencyModelError as error:
            raise ProjectModelError(str(error)) from error

    @classmethod
    def from_data(cls, data: Mapping[str, object]) -> "ProjectLock":
        _require_keys(
            data,
            {"schema", "manifest_resolution_digest", "packages"},
            {"external_mappings"},
            "project lock",
        )
        raw_packages = data["packages"]
        if not isinstance(raw_packages, list):
            raise ProjectModelError("project lock packages must be an array")
        try:
            return cls(
                _integer(data["schema"], "lock schema"),
                _string(data["manifest_resolution_digest"], "manifest resolution digest"),
                tuple(
                    LockedPackage.from_data(_mapping(item, "locked package"))
                    for item in raw_packages
                ),
                tuple(
                    _locked_external_mapping_from_data(item)
                    for item in _list(data.get("external_mappings", []), "locked external mappings")
                ),
            )
        except DependencyModelError as error:
            raise ProjectModelError(str(error)) from error

    @classmethod
    def load(cls, path: Path | str) -> "ProjectLock":
        source = Path(path)
        try:
            text = source.read_text(encoding="utf-8")
        except FileNotFoundError as error:
            raise ProjectModelError(f"project lock is unavailable: {source}") from error
        return cls.parse(text)


def discover_project_manifest(
    start: Path | str,
    *,
    explicit: Path | str | None = None,
) -> ProjectManifest | None:
    """Find and load ``zlang.toml`` without changing the filesystem.

    An explicit path may name either the manifest or its containing directory.
    Without one, discovery walks from a source file/directory to its parents.
    """
    if explicit is not None:
        candidate = Path(explicit)
        if candidate.is_dir():
            candidate = candidate / "zlang.toml"
        if candidate.name != "zlang.toml":
            raise ProjectModelError("explicit project path must name zlang.toml or its directory")
        return ProjectManifest.load(candidate)

    candidate = Path(start)
    if candidate.is_file() or (not candidate.exists() and candidate.suffix):
        candidate = candidate.parent
    for directory in (candidate, *candidate.parents):
        manifest = directory / "zlang.toml"
        if manifest.is_file():
            return ProjectManifest.load(manifest)
    return None


def _validate_source_root(value: Path | str) -> Path:
    raw = Path(value).as_posix()
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or "\\" in raw or "\x00" in raw:
        raise ProjectModelError("project source-root must be a normalized relative path")
    if raw != "." and raw != path.as_posix():
        raise ProjectModelError("project source-root must be a normalized relative path")
    return Path(raw)


def _validate_relative_file_path(value: str, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProjectModelError(f"{description} must be a non-empty string")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or "\\" in value
        or "\x00" in value
        or value != path.as_posix()
    ):
        raise ProjectModelError(f"{description} must be a normalized relative path")
    return value


def _parse_external_mapping_table(
    table: Mapping[str, object],
) -> tuple[ExternalMappingSpec, ...]:
    result: list[ExternalMappingSpec] = []
    for name in sorted(table):
        raw = _mapping(table[name], f"external mapping '{name}'")
        _require_keys(
            raw,
            {"logical-module", "backend", "physical-module", "ports", "sources"},
            set(),
            f"external mapping '{name}'",
        )
        ports = _mapping(raw["ports"], f"external mapping '{name}' ports")
        result.append(ExternalMappingSpec(
            name,
            _string(raw["logical-module"], f"external mapping '{name}' logical-module"),
            _string(raw["backend"], f"external mapping '{name}' backend"),
            _string(raw["physical-module"], f"external mapping '{name}' physical-module"),
            tuple(
                (
                    _string(semantic, f"external mapping '{name}' semantic port"),
                    _string(physical, f"external mapping '{name}' physical port"),
                )
                for semantic, physical in ports.items()
            ),
            _string_tuple(raw["sources"], f"external mapping '{name}' sources"),
        ))
    return tuple(result)


def _external_mapping_from_data(value: object) -> ExternalMappingSpec:
    raw = _mapping(value, "external mapping")
    _require_keys(
        raw,
        {"name", "logical_module", "backend", "physical_module", "ports", "sources"},
        set(),
        "external mapping",
    )
    return ExternalMappingSpec(
        _string(raw["name"], "external mapping name"),
        _string(raw["logical_module"], "external mapping logical module"),
        _string(raw["backend"], "external mapping backend"),
        _string(raw["physical_module"], "external mapping physical module"),
        _pair_tuple(raw["ports"], "external mapping ports"),
        _string_tuple(raw["sources"], "external mapping sources"),
    )


def _locked_external_mapping_from_toml(value: object) -> LockedExternalMapping:
    raw = _mapping(value, "locked external mapping")
    _require_keys(
        raw,
        {"name", "logical-module", "backend", "physical-module", "ports", "sources"},
        set(),
        "locked external mapping",
    )
    raw_sources = _list(raw["sources"], "locked external mapping sources")
    sources = []
    for value in raw_sources:
        source = _mapping(value, "locked external source")
        _require_keys(source, {"relative-path", "digest"}, set(), "locked external source")
        sources.append(LockedExternalSource(
            _string(source["relative-path"], "locked external source relative-path"),
            _string(source["digest"], "locked external source digest"),
        ))
    return LockedExternalMapping(
        _string(raw["name"], "locked external mapping name"),
        _string(raw["logical-module"], "locked external mapping logical-module"),
        _string(raw["backend"], "locked external mapping backend"),
        _string(raw["physical-module"], "locked external mapping physical-module"),
        _port_assignments(raw["ports"], "locked external mapping ports"),
        tuple(sources),
    )


def _locked_external_mapping_from_data(value: object) -> LockedExternalMapping:
    raw = _mapping(value, "locked external mapping")
    _require_keys(
        raw,
        {"name", "logical_module", "backend", "physical_module", "ports", "sources"},
        set(),
        "locked external mapping",
    )
    sources = tuple(
        LockedExternalSource(
            _string(_mapping(item, "locked external source")["relative_path"], "locked external source relative path"),
            _string(_mapping(item, "locked external source")["digest"], "locked external source digest"),
        )
        for item in _list(raw["sources"], "locked external mapping sources")
    )
    return LockedExternalMapping(
        _string(raw["name"], "locked external mapping name"),
        _string(raw["logical_module"], "locked external mapping logical module"),
        _string(raw["backend"], "locked external mapping backend"),
        _string(raw["physical_module"], "locked external mapping physical module"),
        _pair_tuple(raw["ports"], "locked external mapping ports"),
        sources,
    )


def _require_keys(
    data: Mapping[str, object], required: set[str], optional: set[str], description: str
) -> None:
    missing = required - set(data)
    if missing:
        raise ProjectModelError(f"{description} is missing '{sorted(missing)[0]}'")
    unknown = set(data) - required - optional
    if unknown:
        raise ProjectModelError(f"unknown {description} key '{sorted(unknown)[0]}'")


def _mapping(value: object, description: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ProjectModelError(f"{description} must be a table")
    return value


def _string(value: object, description: str) -> str:
    if not isinstance(value, str):
        raise ProjectModelError(f"{description} must be a string")
    return value


def _optional_string(value: object, description: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ProjectModelError(f"{description} must be a string")
    return value


def _integer(value: object, description: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProjectModelError(f"{description} must be an integer")
    return value


def _string_tuple(value: object, description: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ProjectModelError(f"{description} must be an array of strings")
    return tuple(value)


def _list(value: object, description: str) -> tuple[object, ...]:
    if not isinstance(value, (list, tuple)):
        raise ProjectModelError(f"{description} must be an array")
    return tuple(value)


def _pair_tuple(value: object, description: str) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    for item in _list(value, description):
        pair = _list(item, description)
        if len(pair) != 2:
            raise ProjectModelError(f"{description} entries must contain two strings")
        result.append((
            _string(pair[0], description),
            _string(pair[1], description),
        ))
    return tuple(result)


def _port_assignments(value: object, description: str) -> tuple[tuple[str, str], ...]:
    entries = _string_tuple(value, description)
    result: list[tuple[str, str]] = []
    for entry in entries:
        if entry.count("=") != 1:
            raise ProjectModelError(
                f"{description} entries must use 'semantic=physical'"
            )
        semantic, physical = entry.split("=", 1)
        if not semantic or not physical:
            raise ProjectModelError(
                f"{description} entries must use 'semantic=physical'"
            )
        result.append((semantic, physical))
    return tuple(result)


def _reject_casefold_duplicates(values: tuple[str, ...], description: str) -> None:
    seen: dict[str, str] = {}
    for value in values:
        folded = value.casefold()
        if folded in seen:
            raise ProjectModelError(
                f"duplicate {description} '{value}' conflicts with '{seen[folded]}'"
            )
        seen[folded] = value


def _reject_package_cycles(packages: tuple[LockedPackage, ...]) -> None:
    by_name = {package.name: package for package in packages}
    try:
        dependency_postorder(
            sorted(by_name), lambda name: by_name[name].dependencies
        )
    except DependencyCycle as error:
        raise ProjectModelError(
            f"locked package dependency cycle: {error}"
        ) from error


def _freeze_table(value: object, description: str) -> Mapping[str, object]:
    mapping = _mapping(value, description)
    frozen: dict[str, object] = {}
    for key, item in sorted(mapping.items()):
        if not isinstance(key, str) or not key:
            raise ProjectModelError(f"{description} keys must be non-empty strings")
        frozen[key] = _freeze_toml_value(item, f"{description}.{key}")
    return MappingProxyType(frozen)


def _freeze_toml_value(value: object, description: str) -> object:
    if isinstance(value, Mapping):
        return _freeze_table(value, description)
    if isinstance(value, list):
        return tuple(_freeze_toml_value(item, description) for item in value)
    if isinstance(value, (str, bool, int, float)) and not (
        isinstance(value, float) and (value != value or value in {float("inf"), float("-inf")})
    ):
        return value
    raise ProjectModelError(f"unsupported TOML value in {description}")


def _thaw_table(value: Mapping[str, object]) -> dict[str, object]:
    return {key: _thaw_toml_value(item) for key, item in value.items()}


def _thaw_toml_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _thaw_table(value)
    if isinstance(value, tuple):
        return [_thaw_toml_value(item) for item in value]
    return value


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_key(value: str) -> str:
    return value if _BARE_KEY.fullmatch(value) else _toml_string(value)


def _toml_value(value: object) -> str:
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return repr(value)
    if isinstance(value, tuple):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise ProjectModelError("profile value cannot be rendered as TOML")


def _toml_array(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _render_mapping_tables(
    lines: list[str], prefix: tuple[str, ...], table: Mapping[str, object]
) -> None:
    scalars = tuple((key, value) for key, value in table.items() if not isinstance(value, Mapping))
    children = tuple((key, value) for key, value in table.items() if isinstance(value, Mapping))
    if scalars or not children:
        lines.extend(("", "[" + ".".join(_toml_key(item) for item in prefix) + "]"))
        for key, value in scalars:
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    for key, child in children:
        _render_mapping_tables(lines, (*prefix, key), child)


__all__ = [
    "ExternalMappingSpec", "LockedExternalMapping", "LockedExternalSource",
    "PROJECT_SCHEMA", "ProjectLock", "ProjectManifest", "ProjectModelError",
    "discover_project_manifest",
]
