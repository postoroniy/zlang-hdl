"""Safe resolver for compiler-shipped ZLang standard-library sources.

``std`` is the stable logical namespace.  Its ordinary ZLang sources live
below the physical ``stdlib`` directory; protocol or math semantics never live
in this resolver.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import sys
from typing import Iterable, Iterator

from zlang.common.graph import DependencyCycle, dependency_postorder
from zlang.parser import parse
from zlang.module_resolver import attach_source_identity


_COMPONENT = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")
_ROOTS = (
    Path(__file__).resolve().parent.parent / "stdlib",
    Path(sys.prefix) / "stdlib",
)
_RESOLVED_SOURCE_PATHS: ContextVar[set[Path] | None] = ContextVar(
    "zlang_resolved_stdlib_source_paths", default=None,
)


@contextmanager
def track_resolved_stdlib_source_paths() -> Iterator[set[Path]]:
    """Collect physical stdlib files consulted by one compiler invocation.

    The set is process-local, nonsemantic bookkeeping.  Logical module names
    and content hashes remain the only stdlib data carried by typed/canonical
    IR and backend artifacts.
    """

    active = _RESOLVED_SOURCE_PATHS.get()
    if active is not None:
        yield active
        return
    paths: set[Path] = set()
    token = _RESOLVED_SOURCE_PATHS.set(paths)
    try:
        yield paths
    finally:
        _RESOLVED_SOURCE_PATHS.reset(token)


@dataclass(frozen=True)
class StdlibSource:
    path: str
    source_path: Path
    ast: object
    digest: str
    dependencies: tuple[str, ...]

    @property
    def logical_path(self) -> str:
        """Resolver-protocol spelling for the stable ``std.*`` identity."""

        return self.path


def _relative_source(path: str) -> Path:
    parts = path.split(".")
    if len(parts) < 2 or parts[0] != "std":
        raise ValueError(f"external imports are unsupported: '{path}'")
    if any(not part or part.startswith("_") or not _COMPONENT.fullmatch(part)
           for part in parts[1:]):
        raise ValueError(f"invalid standard library module path '{path}'")
    return Path(*parts[1:]).with_suffix(".zl")


def _locate(path: str) -> Path:
    relative = _relative_source(path)
    matches: list[tuple[Path, str]] = []
    for root in _ROOTS:
        if not root.is_dir():
            continue
        resolved_root = root.resolve()
        candidate = root / relative
        try:
            candidate = candidate.resolve(strict=True)
        except FileNotFoundError:
            continue
        try:
            candidate.relative_to(resolved_root)
        except ValueError:
            raise ValueError(
                f"standard library module '{path}' escapes root '{resolved_root}'"
            ) from None
        if not candidate.is_file():
            continue
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        matches.append((candidate, digest))
    if matches:
        tracked = _RESOLVED_SOURCE_PATHS.get()
        if tracked is not None:
            tracked.update(source for source, _ in matches)
        digests = {digest for _, digest in matches}
        if len(digests) != 1:
            locations = ", ".join(
                f"{source} (sha256:{digest[:12]})" for source, digest in matches
            )
            raise ValueError(
                f"conflicting duplicate standard library module '{path}': {locations}"
            )
        # Root order is part of the resolver policy.  Byte-identical checkout and
        # installed-wheel copies therefore resolve predictably without silently
        # accepting a stale or locally modified duplicate.
        selected = matches[0][0]
        return selected
    raise ValueError(f"unknown standard library module '{path}'")


def available_stdlib_modules() -> tuple[str, ...]:
    modules: set[str] = set()
    for root in _ROOTS:
        if not root.is_dir():
            continue
        for source in root.rglob("*.zl"):
            relative = source.relative_to(root).with_suffix("")
            if all(_COMPONENT.fullmatch(part) and not part.startswith("_")
                   for part in relative.parts):
                modules.add("std." + ".".join(relative.parts))
    ordered = tuple(sorted(modules))
    # Discovery must not conceal a conflict that a later import would reject.
    for path in ordered:
        _locate(path)
    return ordered


def load_stdlib_source(path: str) -> StdlibSource:
    source_path = _locate(path)
    try:
        source = source_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValueError(f"standard library source is missing: {source_path}") from exc
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    module = attach_source_identity(parse(source), path, digest)
    dependencies = tuple(declaration.path for declaration in module.imports)
    return StdlibSource(path, source_path, module, digest, dependencies)


def resolve_stdlib(imports: Iterable[str]) -> tuple[StdlibSource, ...]:
    """Resolve imports and their dependency closure in dependency-first order."""
    loaded: dict[str, StdlibSource] = {}

    def dependencies(path: str) -> tuple[str, ...]:
        item = loaded.get(path)
        if item is None:
            item = load_stdlib_source(path)
            loaded[path] = item
        return item.dependencies

    try:
        ordered = dependency_postorder(imports, dependencies)
    except DependencyCycle as error:
        raise ValueError(f"standard library import cycle: {error}") from error
    return tuple(loaded[path] for path in ordered)


def stdlib_source(path: str) -> tuple[object, str, str]:
    """Compatibility API returning one parsed source, identity, and hash."""
    item = load_stdlib_source(path)
    return item.ast, item.path, item.digest


def stdlib_source_hash(path: str) -> str:
    return load_stdlib_source(path).digest


__all__ = [
    "StdlibSource", "available_stdlib_modules", "load_stdlib_source",
    "resolve_stdlib", "stdlib_source", "stdlib_source_hash",
    "track_resolved_stdlib_source_paths",
]
