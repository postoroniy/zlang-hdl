"""Backend-independent project dependency and lock records.

The records in this module deliberately contain logical, portable identities.
Physical checkout/cache paths belong to the resolver and never participate in
dependency, artifact, or proof-cache identities.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
import re
from typing import Mapping

from zlang.common.serialization import stable_digest
from zlang.source_identity import SOURCE_SUFFIX


LOCK_SCHEMA = 2

_COMPONENT = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_GIT_REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class DependencyModelError(ValueError):
    """A project dependency or lock record is malformed."""


class DependencySourceKind(str, Enum):
    PATH = "path"
    GIT = "git"


def validate_logical_path(value: str, *, description: str = "logical path") -> str:
    if not isinstance(value, str) or not value:
        raise DependencyModelError(f"{description} must be a non-empty string")
    parts = value.split(".")
    if any(not _COMPONENT.fullmatch(part) or part.startswith("_") for part in parts):
        raise DependencyModelError(f"invalid {description} '{value}'")
    return value


def validate_package_name(value: str) -> str:
    validate_logical_path(value, description="package name")
    if value == "std" or value.startswith("std."):
        raise DependencyModelError("package namespace 'std' is reserved")
    return value


def validate_digest(value: str, *, description: str = "digest") -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise DependencyModelError(
            f"{description} must be a lowercase 64-character SHA-256 digest"
        )
    return value


def _portable_locator(value: str, *, allow_parent: bool) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise DependencyModelError("path dependency locator must be a portable relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or value in {".", ".."}:
        raise DependencyModelError("path dependency locator must be a portable relative path")
    if not allow_parent and ".." in path.parts:
        raise DependencyModelError("relative path must not escape its package source root")
    # Parent components are valid for manifest-relative path dependencies, but
    # normalization ambiguity (``a/../b``) is not.
    if any(part in {"", "."} for part in path.parts):
        raise DependencyModelError("path dependency locator must be normalized")
    saw_regular = False
    for part in path.parts:
        if part == "..":
            if saw_regular:
                raise DependencyModelError("path dependency locator must be normalized")
        else:
            saw_regular = True
    return path.as_posix()


def _unique_sorted(values: tuple[str, ...], *, description: str) -> tuple[str, ...]:
    seen: dict[str, str] = {}
    for value in values:
        folded = value.casefold()
        if folded in seen:
            raise DependencyModelError(
                f"duplicate {description} '{value}' conflicts with '{seen[folded]}'"
            )
        seen[folded] = value
    return tuple(sorted(values))


@dataclass(frozen=True)
class DependencySpec:
    """One direct manifest dependency with exactly one source locator."""

    package: str
    kind: DependencySourceKind
    locator: str
    revision: str | None = None

    def __post_init__(self) -> None:
        validate_package_name(self.package)
        try:
            kind = DependencySourceKind(self.kind)
        except ValueError as error:
            raise DependencyModelError(f"unknown dependency source kind '{self.kind}'") from error
        object.__setattr__(self, "kind", kind)
        if kind is DependencySourceKind.PATH:
            object.__setattr__(self, "locator", _portable_locator(self.locator, allow_parent=True))
            if self.revision is not None:
                raise DependencyModelError("path dependency must not declare a Git revision")
        else:
            if not isinstance(self.locator, str) or not self.locator or "\x00" in self.locator:
                raise DependencyModelError("Git dependency locator must be a non-empty string")
            if not isinstance(self.revision, str) or not _GIT_REVISION.fullmatch(self.revision):
                raise DependencyModelError(
                    "Git dependency revision must be a complete lowercase 40- or "
                    "64-character hexadecimal object ID"
                )

    @property
    def identity(self) -> str:
        return stable_digest(self.to_data())

    def to_data(self) -> dict[str, object]:
        data: dict[str, object] = {
            "kind": self.kind.value,
            "locator": self.locator,
            "package": self.package,
        }
        if self.revision is not None:
            data["revision"] = self.revision
        return data

    @classmethod
    def from_data(cls, data: Mapping[str, object]) -> "DependencySpec":
        _require_keys(data, {"package", "kind", "locator"}, {"revision"}, "dependency")
        try:
            return cls(
                _string(data["package"], "dependency package"),
                DependencySourceKind(_string(data["kind"], "dependency kind")),
                _string(data["locator"], "dependency locator"),
                _optional_string(data.get("revision"), "dependency revision"),
            )
        except ValueError as error:
            raise DependencyModelError(str(error)) from error


@dataclass(frozen=True)
class LockedModule:
    """Exact source index entry for one locked logical module."""

    logical_path: str
    relative_path: str
    digest: str
    imports: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_logical_path(self.logical_path, description="module path")
        relative = _portable_locator(self.relative_path, allow_parent=False)
        if not relative.endswith(SOURCE_SUFFIX):
            raise DependencyModelError(
                f"locked module relative path must end in '{SOURCE_SUFFIX}'"
            )
        object.__setattr__(self, "relative_path", relative)
        validate_digest(self.digest, description="module digest")
        for item in self.imports:
            validate_logical_path(item, description="import path")
        object.__setattr__(
            self,
            "imports",
            _unique_sorted(tuple(self.imports), description="module import"),
        )

    def to_data(self) -> dict[str, object]:
        return {
            "digest": self.digest,
            "imports": list(self.imports),
            "logical_path": self.logical_path,
            "relative_path": self.relative_path,
        }

    @property
    def dependencies(self) -> tuple[str, ...]:
        """Resolver-compatible spelling for this exact import list."""
        return self.imports

    @classmethod
    def from_data(cls, data: Mapping[str, object]) -> "LockedModule":
        _require_keys(
            data,
            {"logical_path", "relative_path", "digest", "imports"},
            set(),
            "locked module",
        )
        return cls(
            _string(data["logical_path"], "module logical path"),
            _string(data["relative_path"], "module relative path"),
            _string(data["digest"], "module digest"),
            _string_tuple(data["imports"], "module imports"),
        )


@dataclass(frozen=True)
class LockedPackage:
    """Portable, source-exact record for one resolved dependency package.

    ``manifest_digest`` is the manifest's dependency-resolution digest; profile
    tables are deliberately excluded. Exact `.zhl` bytes remain represented by
    the locked module digests.
    """

    name: str
    version: str
    source_kind: DependencySourceKind
    source_locator: str
    revision: str | None
    manifest_digest: str
    dependencies: tuple[str, ...] = ()
    modules: tuple[LockedModule, ...] = ()

    def __post_init__(self) -> None:
        validate_package_name(self.name)
        if not isinstance(self.version, str) or not self.version:
            raise DependencyModelError("locked package version must be a non-empty string")
        # Reuse source-policy validation without giving the package a second
        # independently evolving interpretation of locators/revisions.
        spec = DependencySpec(self.name, self.source_kind, self.source_locator, self.revision)
        object.__setattr__(self, "source_kind", spec.kind)
        object.__setattr__(self, "source_locator", spec.locator)
        validate_digest(self.manifest_digest, description="package manifest digest")
        for dependency in self.dependencies:
            validate_package_name(dependency)
        object.__setattr__(
            self,
            "dependencies",
            _unique_sorted(tuple(self.dependencies), description="package dependency"),
        )
        modules = tuple(self.modules)
        _reject_named_duplicates(
            tuple(module.logical_path for module in modules), "locked module"
        )
        relative_paths = tuple(module.relative_path for module in modules)
        _reject_named_duplicates(relative_paths, "locked module relative path")
        for module in modules:
            if module.logical_path != self.name and not module.logical_path.startswith(self.name + "."):
                raise DependencyModelError(
                    f"locked module '{module.logical_path}' is outside package '{self.name}'"
                )
        object.__setattr__(self, "modules", tuple(sorted(modules, key=lambda item: item.logical_path)))

    @property
    def identity(self) -> str:
        return stable_digest(self.to_data())

    @property
    def package_identity(self) -> str:
        return self.identity

    def to_data(self) -> dict[str, object]:
        return {
            "dependencies": list(self.dependencies),
            "manifest_digest": self.manifest_digest,
            "modules": [module.to_data() for module in self.modules],
            "name": self.name,
            "revision": self.revision,
            "source_kind": self.source_kind.value,
            "source_locator": self.source_locator,
            "version": self.version,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, object]) -> "LockedPackage":
        _require_keys(
            data,
            {
                "name", "version", "source_kind", "source_locator", "revision",
                "manifest_digest", "dependencies", "modules",
            },
            set(),
            "locked package",
        )
        raw_modules = data["modules"]
        if not isinstance(raw_modules, list):
            raise DependencyModelError("locked package modules must be an array")
        return cls(
            _string(data["name"], "package name"),
            _string(data["version"], "package version"),
            DependencySourceKind(_string(data["source_kind"], "package source kind")),
            _string(data["source_locator"], "package source locator"),
            _optional_string(data["revision"], "package revision"),
            _string(data["manifest_digest"], "package manifest digest"),
            _string_tuple(data["dependencies"], "package dependencies"),
            tuple(LockedModule.from_data(_mapping(item, "locked module")) for item in raw_modules),
        )


@dataclass(frozen=True)
class DependencyModuleIdentity:
    """Logical content identity threaded through IR and backend artifacts."""

    logical_path: str
    digest: str
    package_identity: str
    package_revision: str | None = None

    def __post_init__(self) -> None:
        validate_logical_path(self.logical_path, description="module path")
        validate_digest(self.digest, description="module digest")
        validate_digest(self.package_identity, description="package identity")
        if self.package_revision is not None and not _GIT_REVISION.fullmatch(self.package_revision):
            raise DependencyModelError("package revision must be a complete Git object ID")

    def to_data(self) -> dict[str, object]:
        return {
            "digest": self.digest,
            "logical_path": self.logical_path,
            "package_identity": self.package_identity,
            "package_revision": self.package_revision,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, object]) -> "DependencyModuleIdentity":
        _require_keys(
            data,
            {"logical_path", "digest", "package_identity", "package_revision"},
            set(),
            "dependency module identity",
        )
        return cls(
            _string(data["logical_path"], "module logical path"),
            _string(data["digest"], "module digest"),
            _string(data["package_identity"], "package identity"),
            _optional_string(data["package_revision"], "package revision"),
        )


@dataclass(frozen=True)
class DependencyClosure:
    """Exact dependency module closure for one compilation."""

    schema: int
    lock_identity: str
    modules: tuple[DependencyModuleIdentity, ...] = ()

    def __post_init__(self) -> None:
        if self.schema != LOCK_SCHEMA:
            raise DependencyModelError(f"unsupported dependency closure schema {self.schema}")
        validate_digest(self.lock_identity, description="lock identity")
        modules = tuple(self.modules)
        _reject_named_duplicates(tuple(item.logical_path for item in modules), "closure module")
        object.__setattr__(self, "modules", tuple(sorted(modules, key=lambda item: item.logical_path)))

    @property
    def identity(self) -> str:
        return stable_digest(self.to_data())

    def to_data(self) -> dict[str, object]:
        return {
            "lock_identity": self.lock_identity,
            "modules": [module.to_data() for module in self.modules],
            "schema": self.schema,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, object]) -> "DependencyClosure":
        _require_keys(data, {"schema", "lock_identity", "modules"}, set(), "dependency closure")
        raw_modules = data["modules"]
        if not isinstance(raw_modules, list):
            raise DependencyModelError("dependency closure modules must be an array")
        return cls(
            _integer(data["schema"], "dependency closure schema"),
            _string(data["lock_identity"], "lock identity"),
            tuple(
                DependencyModuleIdentity.from_data(_mapping(item, "dependency module identity"))
                for item in raw_modules
            ),
        )


@dataclass(frozen=True)
class ResolvedModule:
    """Small resolver-facing source shape, independent of lock implementation."""

    logical_path: str
    source_path: Path
    ast: object
    digest: str
    dependencies: tuple[str, ...]
    package_identity: str
    package_revision: str | None = None

    def __post_init__(self) -> None:
        validate_logical_path(self.logical_path, description="module path")
        validate_digest(self.digest, description="module digest")
        validate_digest(self.package_identity, description="package identity")
        object.__setattr__(self, "source_path", Path(self.source_path))
        for dependency in self.dependencies:
            validate_logical_path(dependency, description="import path")
        object.__setattr__(self, "dependencies", tuple(self.dependencies))
        # Validate the optional revision at construction time rather than only
        # when a consumer asks for ``identity``.
        DependencyModuleIdentity(
            self.logical_path,
            self.digest,
            self.package_identity,
            self.package_revision,
        )

    @property
    def imports(self) -> tuple[str, ...]:
        return self.dependencies

    @property
    def identity(self) -> DependencyModuleIdentity:
        return DependencyModuleIdentity(
            self.logical_path,
            self.digest,
            self.package_identity,
            self.package_revision,
        )


def _require_keys(
    data: Mapping[str, object], required: set[str], optional: set[str], description: str
) -> None:
    if not isinstance(data, Mapping):
        raise DependencyModelError(f"{description} must be an object")
    missing = required - set(data)
    if missing:
        raise DependencyModelError(f"{description} is missing '{sorted(missing)[0]}'")
    unknown = set(data) - required - optional
    if unknown:
        raise DependencyModelError(f"unknown {description} key '{sorted(unknown)[0]}'")


def _mapping(value: object, description: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise DependencyModelError(f"{description} must be an object")
    return value


def _string(value: object, description: str) -> str:
    if not isinstance(value, str):
        raise DependencyModelError(f"{description} must be a string")
    return value


def _optional_string(value: object, description: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise DependencyModelError(f"{description} must be a string")
    return value


def _integer(value: object, description: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DependencyModelError(f"{description} must be an integer")
    return value


def _string_tuple(value: object, description: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DependencyModelError(f"{description} must be an array of strings")
    return tuple(value)


def _reject_named_duplicates(values: tuple[str, ...], description: str) -> None:
    _unique_sorted(values, description=description)


__all__ = [
    "DependencyClosure", "DependencyModelError", "DependencyModuleIdentity",
    "DependencySourceKind", "DependencySpec", "LOCK_SCHEMA", "LockedModule",
    "LockedPackage", "ResolvedModule", "validate_digest", "validate_logical_path",
    "validate_package_name",
]
