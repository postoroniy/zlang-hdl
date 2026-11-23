# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Compiler-owned import resolution and declaration-surface preparation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from zlang.ast import nodes as ast
from zlang.common import stable_digest
from zlang.dependencies import DependencyClosure, DependencyModuleIdentity
from zlang.diagnostics import DiagnosticEdit, DiagnosticFix
from zlang import module_resolver as module_resolution
from zlang.source import SourceOrigin

from .errors import SemanticError
from .context import AnalysisContext
from . import module_preparation


@dataclass(frozen=True)
class ImportAnalysisProduct:
    module: ast.Module
    protocol_sources: tuple[tuple[str, str | None], ...]
    resolution_context: module_resolution.ModuleResolutionContext
    resolved_imports: tuple[module_resolution.ModuleSourceRecord, ...]
    source_digests: tuple[tuple[str, str], ...]
    generic_dependency_identity: tuple[tuple[str, str], ...]
    source_unit: str | None
    source_digest: str | None
    enum_identity_namespace: str
    active_module_identity: DependencyModuleIdentity | None


def dependency_identity_for_source(
    logical_path: str | None,
    root: DependencyModuleIdentity | None,
    closure: DependencyClosure | None,
) -> DependencyModuleIdentity | None:
    if logical_path is None or root is not None and root.logical_path == logical_path:
        return root
    if closure is None:
        return None
    return next(
        (item for item in closure.modules if item.logical_path == logical_path), None
    )


def direct_import_paths(
    module: ast.Module,
    source_unit: str | None,
    source_digest: str | None,
    *,
    physical_source: Path | None = None,
) -> tuple[str, ...]:
    """Read imports in source order, diagnosing the first repeated logical path."""
    seen: dict[str, ast.ImportDecl] = {}
    for declaration in module.imports:
        key = declaration.path.casefold()
        previous = seen.get(key)
        if previous is None:
            seen[key] = declaration
            continue
        identical = (declaration.path, declaration.alias) == (previous.path, previous.alias)
        origin = (
            SourceOrigin(declaration.origin, "import", source_unit, source_digest)
            if declaration.origin is not None else None
        )
        machine_fixes = ()
        if identical and origin is not None and source_unit and source_digest:
            machine_fixes = (DiagnosticFix(
                "Remove duplicate import declaration",
                (DiagnosticEdit(replace(origin, construct="duplicate import declaration"), ""),),
            ),)
        suggestion = (
            "remove the duplicate import declaration" if identical
            else "use one spelling and alias for this import"
        )
        raise SemanticError(
            f"duplicate import '{declaration.path}'",
            code="ZL-IMPORT-DUPLICATE",
            primary=origin,
            physical_source=physical_source,
            fixes=(suggestion,),
            machine_fixes=machine_fixes,
        )
    return tuple(item.path for item in module.imports)


class ImportAnalyzer:
    """Resolve one locked import closure and merge its public declarations."""

    @staticmethod
    def _reject_conflicts(
        module: ast.Module,
        imported: tuple[ast.Module, ...],
        root_owner: str,
        *,
        source_unit: str | None = None,
        source_digest: str | None = None,
        records: tuple[module_resolution.ModuleSourceRecord, ...] = (),
    ) -> None:
        indexed = {
            id(unit): record
            for record in records
            for unit in (record.ast, *record.ast.submodules)
        }

        def register(
            owners: dict[str, tuple[str, SourceOrigin | None]],
            declaration: object, source: ast.Module, label: str,
            *, root: bool = False, root_duplicate: bool = True,
        ) -> None:
            name = declaration.name
            owner = source.source_identity or (root_owner if root else source.name)
            record = indexed.get(id(source))
            unit = source.source_identity or (
                record.logical_path if record is not None else source_unit if root else None
            )
            digest = source.source_hash or (
                record.digest if record is not None else source_digest if root else None
            )
            span = getattr(declaration, "name_origin", None) or getattr(declaration, "origin", None)
            origin = SourceOrigin(span, f"{label} {name}", unit, digest) if span is not None else None
            previous = owners.get(name)
            if previous is not None:
                previous_owner, previous_origin = previous
                location = (
                    f"in '{root_owner}'"
                    if root_duplicate and previous_owner == owner == root_owner
                    else f"from '{previous_owner}' and '{owner}'"
                )
                previous_location = previous_owner
                if previous_origin is not None:
                    previous_location += (
                        f":{previous_origin.span.start_line}:"
                        f"{previous_origin.span.start_column}"
                    )
                raise SemanticError(
                    f"conflicting {label} declaration '{name}' {location}",
                    code="ZL-IMPORT-CONFLICT",
                    primary=origin,
                    physical_source=record.source_path if record is not None else None,
                    notes=(
                        f"previous declaration: {previous_location}",
                        "import aliases change local spelling, not declaration identity",
                    ),
                    fixes=("use distinct declaration names across the import closure",),
                )
            owners[name] = owner, origin

        for label, attribute in (
            ("type alias", "type_aliases"),
            ("struct", "structs"),
            ("enum", "enums"),
            ("tagged union", "tagged_unions"),
            ("function", "functions"),
            ("protocol", "protocols"),
            ("module interface", "module_interfaces"),
        ):
            owners: dict[str, tuple[str, SourceOrigin | None]] = {}
            for declaration in getattr(module, attribute):
                register(owners, declaration, module, label, root=True)
            for source in imported:
                for declaration in getattr(source, attribute):
                    register(owners, declaration, source, label)

        owners: dict[str, tuple[str, SourceOrigin | None]] = {}
        root_sources = (module, *module.submodules)
        root_ids = {id(source) for source in root_sources}
        for source in (*root_sources, *imported):
            if source.declaration_only:
                continue
            register(
                owners, source, source, "module", root=id(source) in root_ids,
                root_duplicate=False,
            )

    @staticmethod
    def _merge(module: ast.Module, imported: tuple[ast.Module, ...]) -> ast.Module:
        attributes = (
            "type_aliases", "protocols", "module_interfaces",
            "structs", "functions", "operators",
        )
        enums = tuple(
            replace(item, source_identity=item.source_identity or source.source_identity)
            for source in imported for item in source.enums
        )
        unions = tuple(
            replace(item, source_identity=item.source_identity or source.source_identity)
            for source in imported for item in source.tagged_unions
        )
        return replace(
            module,
            submodules=(*module.submodules, *(
                source for source in imported if not source.declaration_only
            )),
            enums=(*module.enums, *enums),
            tagged_unions=(*module.tagged_unions, *unions),
            **{
                attribute: (
                    *getattr(module, attribute),
                    *(item for source in imported for item in getattr(source, attribute)),
                )
                for attribute in attributes
            },
        )

    def analyze(self, context: AnalysisContext) -> ImportAnalysisProduct:
        module = context.source.module
        resolution = context.resolution.resolution_context
        resolver = context.resolution.module_resolver
        if resolution is not None and resolver is not None and resolution.resolver is not resolver:
            raise SemanticError(
                "module_resolver conflicts with the active resolution_context",
                code="ZL-IMPORT-RESOLVER",
            )
        if resolution is None:
            resolution = module_resolution.ModuleResolutionContext(
                resolver or module_resolution.StdlibModuleResolver()
            )
        source_unit = module.source_identity or context.source.source_unit
        source_digest = module.source_hash or context.source.source_digest
        namespace = context.source.enum_identity_namespace or source_unit
        if namespace is None:
            namespace = "compilation:" + ",".join(sorted({
                module.name, *(child.name for child in module.submodules)
            }))
        module = replace(
            module,
            enums=tuple(replace(
                item, source_identity=item.source_identity or namespace
            ) for item in module.enums),
            tagged_unions=tuple(replace(
                item, source_identity=item.source_identity or namespace
            ) for item in module.tagged_unions),
        )
        direct_imports = direct_import_paths(module, source_unit, source_digest)
        try:
            resolved = resolution.resolver.resolve(direct_imports, importer=source_unit)
        except (module_resolution.ModuleResolutionError, ValueError) as error:
            declaration = module.imports[0] if module.imports else None
            raise SemanticError(
                str(error),
                code="ZL-IMPORT-RESOLVE",
                primary=(
                    SourceOrigin(
                        declaration.origin, "import", source_unit, source_digest
                    )
                    if declaration is not None and declaration.origin is not None
                    else None
                ),
                fixes=("use an indexed logical module or update the project lock",),
            ) from error
        by_path = {item.logical_path: item for item in resolved}
        module = module_preparation.normalize_qualified_imports(
            module, by_path, source_unit=source_unit, source_digest=source_digest
        )
        ordered = (
            *(by_path[path] for path in direct_imports),
            *(item for item in resolved if item.logical_path not in direct_imports),
        )
        imported = () if context.resolution.imports_premerged else tuple(
            child
            for item in ordered
            for child in (item.ast, *item.ast.submodules)
        )
        if imported:
            owner = source_unit or f"compilation:{module.name}"
            self._reject_conflicts(
                module, imported, owner, source_unit=source_unit,
                source_digest=source_digest, records=ordered,
            )
            module = self._merge(module, imported)
        source_digests = {item.logical_path: item.digest for item in resolved}
        dependencies = dict(source_digests)
        if context.resolution.dependency_closure is not None:
            dependencies.update(
                (item.logical_path, stable_digest(item.to_data()))
                for item in context.resolution.dependency_closure.modules
            )
        if source_unit is not None and source_digest is not None:
            source_digests[source_unit] = source_digest
        return ImportAnalysisProduct(
            module,
            tuple(
                (protocol.name, source.source_identity)
                for source in imported for protocol in source.protocols
            ),
            resolution,
            tuple(resolved),
            tuple(source_digests.items()),
            tuple(sorted(dependencies.items())),
            source_unit,
            source_digest,
            namespace,
            dependency_identity_for_source(
                source_unit,
                context.resolution.root_module_identity,
                context.resolution.dependency_closure,
            ),
        )
