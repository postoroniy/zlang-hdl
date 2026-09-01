"""Per-compilation resolution of dotted logical ZLang module imports.

Source syntax names logical modules only.  Physical standard-library roots,
project source trees, locked path dependencies, and populated Git caches are
providers for one exact logical index; they are never reconstructed from a
name during semantic analysis.

The project/lock model intentionally depends on the small protocols in this
module rather than the resolver depending on a particular lock-file schema.
That keeps ordinary ``compile_source`` users on the existing std-only policy
while allowing ``compile_file`` to inject an immutable project index later.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from pathlib import Path, PurePosixPath
import re
from typing import Iterable, Protocol, runtime_checkable

from zlang.common.graph import DependencyCycle, dependency_postorder
from zlang.parser import parse


_COMPONENT = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")


class ModuleResolutionError(ValueError):
    """A logical import graph or one of its immutable sources is invalid."""


@runtime_checkable
class ModuleSourceRecord(Protocol):
    """Resolver-facing shape shared by stdlib and project lock records."""

    logical_path: str
    source_path: Path
    ast: object
    digest: str
    dependencies: tuple[str, ...]


@runtime_checkable
class ModuleResolver(Protocol):
    """Resolve direct logical imports to a dependency-first source closure."""

    def resolve(
        self,
        imports: Iterable[str],
        *,
        importer: str | None = None,
    ) -> tuple[ModuleSourceRecord, ...]: ...


@dataclass(frozen=True)
class ModuleResolutionContext:
    """Immutable resolver selection shared by one recursive semantic build."""

    resolver: ModuleResolver


@dataclass(frozen=True)
class ResolvedModuleSource:
    """One immutable indexed source used by resolver tests/project adapters.

    ``source_root`` is the security boundary for the physical file.  Project
    lock records may use their own concrete type as long as it satisfies
    :class:`ModuleSourceRecord`; this class is not the lock-file data model.
    """

    logical_path: str
    source_path: Path
    source_root: Path
    ast: object
    digest: str
    dependencies: tuple[str, ...]
    package_identity: str | None = None
    package_revision: str | None = None

    @property
    def path(self) -> str:
        """Compatibility spelling used by the original stdlib semantic path."""

        return self.logical_path


def attach_source_identity(module: object, logical_path: str, digest: str) -> object:
    """Attach one logical unit/digest to its declaration-bearing AST nodes."""

    return replace(
        module,
        source_identity=logical_path,
        source_hash=digest,
        enums=tuple(
            replace(item, source_identity=logical_path) for item in module.enums
        ),
        tagged_unions=tuple(
            replace(item, source_identity=logical_path)
            for item in module.tagged_unions
        ),
        structs=tuple(
            replace(item, source_identity=logical_path) for item in module.structs
        ),
        operators=tuple(
            replace(item, source_identity=logical_path) for item in module.operators
        ),
        functions=tuple(
            replace(item, source_identity=logical_path) for item in module.functions
        ),
        module_interfaces=tuple(
            replace(item, source_identity=logical_path)
            for item in module.module_interfaces
        ),
        submodules=tuple(
            attach_source_identity(child, logical_path, digest)
            for child in module.submodules
        ),
    )


def validate_logical_module_path(path: str) -> tuple[str, ...]:
    """Validate and return components of one public dotted module identity."""

    parts = tuple(path.split("."))
    if len(parts) < 2 or any(
        not part
        or part.startswith("_")
        or _COMPONENT.fullmatch(part) is None
        for part in parts
    ):
        raise ModuleResolutionError(f"invalid logical module path '{path}'")
    return parts


def load_indexed_module(
    logical_path: str,
    *,
    source_root: Path,
    relative_path: str | Path,
    expected_digest: str | None = None,
    package_identity: str | None = None,
    package_revision: str | None = None,
) -> ResolvedModuleSource:
    """Load one exact module-index entry with traversal/symlink containment.

    The relative path is lock/manifest data, not source syntax.  Resolving the
    candidate before reading rejects both lexical traversal and a symlink that
    leaves the declared package source root.
    """

    validate_logical_module_path(logical_path)
    relative = PurePosixPath(str(relative_path).replace("\\", "/"))
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
        or relative.suffix != ".zl"
    ):
        raise ModuleResolutionError(
            f"invalid source path for logical module '{logical_path}': {relative_path}"
        )
    try:
        root = source_root.resolve(strict=True)
        source = (root / Path(*relative.parts)).resolve(strict=True)
    except FileNotFoundError as error:
        raise ModuleResolutionError(
            f"source for logical module '{logical_path}' is unavailable: {relative_path}"
        ) from error
    try:
        source.relative_to(root)
    except ValueError:
        raise ModuleResolutionError(
            f"source for logical module '{logical_path}' escapes package root '{root}'"
        ) from None
    if not source.is_file():
        raise ModuleResolutionError(
            f"source for logical module '{logical_path}' is not a file: {source}"
        )
    try:
        payload = source.read_bytes()
        text = payload.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ModuleResolutionError(
            f"cannot read UTF-8 source for logical module '{logical_path}': {source}"
        ) from error
    digest = hashlib.sha256(payload).hexdigest()
    if expected_digest is not None and digest != expected_digest:
        raise ModuleResolutionError(
            f"locked module '{logical_path}' is dirty: expected sha256:"
            f"{expected_digest}, found sha256:{digest}"
        )
    parsed = attach_source_identity(parse(text), logical_path, digest)
    dependencies = tuple(item.path for item in parsed.imports)
    return ResolvedModuleSource(
        logical_path,
        source,
        root,
        parsed,
        digest,
        dependencies,
        package_identity,
        package_revision,
    )


def _record_path(record: ModuleSourceRecord) -> str:
    logical = getattr(record, "logical_path", None)
    if logical is None:
        # Compatibility with StdlibSource before all callers migrate to the
        # resolver protocol.  New project records must use ``logical_path``.
        logical = getattr(record, "path", None)
    if not isinstance(logical, str):
        raise ModuleResolutionError("module source record has no logical path")
    validate_logical_module_path(logical)
    return logical


def _validated_record(record: ModuleSourceRecord) -> ModuleSourceRecord:
    """Reject stale or physically escaped indexed records before use."""

    logical = _record_path(record)
    source_path = Path(record.source_path)
    root_value = getattr(record, "source_root", None)
    if root_value is not None:
        try:
            root = Path(root_value).resolve(strict=True)
            source = source_path.resolve(strict=True)
            source.relative_to(root)
        except FileNotFoundError as error:
            raise ModuleResolutionError(
                f"source for logical module '{logical}' is unavailable: {source_path}"
            ) from error
        except ValueError:
            raise ModuleResolutionError(
                f"source for logical module '{logical}' escapes package root '{root_value}'"
            ) from None
    try:
        digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    except OSError as error:
        raise ModuleResolutionError(
            f"source for logical module '{logical}' is unavailable: {source_path}"
        ) from error
    if digest != record.digest:
        raise ModuleResolutionError(
            f"locked module '{logical}' is dirty: expected sha256:{record.digest}, "
            f"found sha256:{digest}"
        )
    parsed_imports = tuple(item.path for item in record.ast.imports)
    if parsed_imports != tuple(record.dependencies):
        raise ModuleResolutionError(
            f"module index dependency mismatch for '{logical}'"
        )
    return record


class StdlibModuleResolver:
    """Compatibility resolver exposing only compiler-shipped ``std.*``."""

    def resolve(
        self,
        imports: Iterable[str],
        *,
        importer: str | None = None,
    ) -> tuple[ModuleSourceRecord, ...]:
        # Import lazily to keep stdlib parsing as the sole owner of its physical
        # checkout/install root policy and avoid an import cycle.
        from zlang.stdlib import resolve_stdlib

        requested = tuple(imports)
        external = next((path for path in requested if not path.startswith("std.")), None)
        if external is not None:
            raise ModuleResolutionError(
                f"external imports are unsupported without a locked project: '{external}'"
            )
        try:
            return tuple(resolve_stdlib(requested))
        except ValueError as error:
            raise ModuleResolutionError(str(error)) from error


class IndexedModuleResolver:
    """Exact immutable root/dependency index with optional std fallback."""

    def __init__(
        self,
        sources: Iterable[ModuleSourceRecord],
        *,
        package_namespaces: Iterable[str] = (),
        include_stdlib: bool = True,
    ) -> None:
        index: dict[str, ModuleSourceRecord] = {}
        casefolded: dict[str, str] = {}
        for source in sources:
            logical = _record_path(source)
            if logical == "std" or logical.startswith("std."):
                raise ModuleResolutionError(
                    "the 'std' namespace is reserved for the compiler-shipped library"
                )
            if logical in index:
                raise ModuleResolutionError(
                    f"conflicting logical module index entry '{logical}'"
                )
            previous = casefolded.get(logical.casefold())
            if previous is not None and previous != logical:
                raise ModuleResolutionError(
                    f"case-folding logical module conflict: '{previous}' and '{logical}'"
                )
            index[logical] = source
            casefolded[logical.casefold()] = logical

        namespaces = tuple(sorted(set(package_namespaces)))
        for namespace in namespaces:
            validate_logical_module_path(namespace + ".module")
            if namespace == "std" or namespace.startswith("std."):
                raise ModuleResolutionError("the 'std' package namespace is reserved")
        for index_, left in enumerate(namespaces):
            for right in namespaces[index_ + 1 :]:
                left_folded = left.casefold()
                right_folded = right.casefold()
                if (
                    left_folded == right_folded
                    or left_folded.startswith(right_folded + ".")
                    or right_folded.startswith(left_folded + ".")
                ):
                    raise ModuleResolutionError(
                        f"conflicting package namespaces '{left}' and '{right}'"
                    )

        self._index = index
        self._namespaces = namespaces
        self._stdlib = StdlibModuleResolver() if include_stdlib else None

    def _load(self, logical: str) -> ModuleSourceRecord:
        source = self._index.get(logical)
        if source is not None:
            return _validated_record(source)
        if logical.startswith("std."):
            if self._stdlib is None:
                raise ModuleResolutionError(
                    f"compiler-shipped import '{logical}' is unavailable in this resolver"
                )
            resolved = self._stdlib.resolve((logical,))
            if not resolved:
                raise ModuleResolutionError(f"unknown standard library module '{logical}'")
            return resolved[-1]
        owner = next(
            (
                namespace
                for namespace in self._namespaces
                if logical == namespace or logical.startswith(namespace + ".")
            ),
            None,
        )
        if owner is not None:
            raise ModuleResolutionError(
                f"unknown module '{logical}' in declared package '{owner}'"
            )
        raise ModuleResolutionError(f"undeclared logical import '{logical}'")

    def resolve(
        self,
        imports: Iterable[str],
        *,
        importer: str | None = None,
    ) -> tuple[ModuleSourceRecord, ...]:
        loaded: dict[str, ModuleSourceRecord] = {}

        def dependencies(logical: str) -> tuple[str, ...]:
            validate_logical_module_path(logical)
            source = loaded.get(logical)
            if source is None:
                source = self._load(logical)
                loaded[logical] = source
            return source.dependencies

        try:
            ordered = dependency_postorder(imports, dependencies)
        except DependencyCycle as error:
            raise ModuleResolutionError(f"logical import cycle: {error}") from error
        return tuple(loaded[logical] for logical in ordered)


__all__ = [
    "IndexedModuleResolver",
    "ModuleResolutionContext",
    "ModuleResolutionError",
    "ModuleResolver",
    "ModuleSourceRecord",
    "ResolvedModuleSource",
    "StdlibModuleResolver",
    "attach_source_identity",
    "load_indexed_module",
    "validate_logical_module_path",
]
