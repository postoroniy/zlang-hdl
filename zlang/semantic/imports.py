# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Compiler-owned import resolution and declaration-surface preparation."""

from __future__ import annotations

from dataclasses import dataclass, replace

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


class ImportAnalyzer:
    """Resolve one locked import closure and merge its public declarations."""

    @staticmethod
    def _duplicate_import(
        module: ast.Module,
        path: str,
        source_unit: str | None,
        source_digest: str | None,
    ) -> SemanticError:
        declaration = next(item for item in module.imports if item.path == path)
        seen: set[tuple[str, str | None]] = set()
        duplicate = None
        for item in module.imports:
            if item.path != path:
                continue
            identity = (item.path, item.alias)
            if identity in seen:
                duplicate = item
                break
            seen.add(identity)
        machine_fixes = ()
        if (
            duplicate is not None
            and duplicate.origin is not None
            and source_unit is not None
            and source_digest is not None
        ):
            machine_fixes = (DiagnosticFix(
                "Remove duplicate import declaration",
                (DiagnosticEdit(SourceOrigin(
                    duplicate.origin,
                    "duplicate import declaration",
                    source_unit,
                    source_digest,
                ), ""),),
            ),)
        return SemanticError(
            f"duplicate import '{path}'",
            code="ZL-IMPORT-DUPLICATE",
            primary=(
                SourceOrigin(
                    declaration.origin, "import", source_unit, source_digest
                )
                if declaration.origin is not None else None
            ),
            fixes=("remove the duplicate import declaration",),
            machine_fixes=machine_fixes,
        )

    @staticmethod
    def _reject_conflicts(
        module: ast.Module,
        imported: tuple[ast.Module, ...],
        root_owner: str,
    ) -> None:
        def register(
            owners: dict[str, str], name: str, owner: str, label: str,
            *, root_duplicate: bool = True,
        ) -> None:
            previous = owners.get(name)
            if previous is not None:
                location = (
                    f"in '{root_owner}'"
                    if root_duplicate and previous == owner == root_owner
                    else f"from '{previous}' and '{owner}'"
                )
                raise SemanticError(
                    f"conflicting {label} declaration '{name}' {location}",
                    code="ZL-IMPORT-CONFLICT",
                )
            owners[name] = owner

        for label, attribute in (
            ("type alias", "type_aliases"),
            ("struct", "structs"),
            ("enum", "enums"),
            ("tagged union", "tagged_unions"),
            ("function", "functions"),
            ("protocol", "protocols"),
            ("module interface", "module_interfaces"),
        ):
            owners: dict[str, str] = {}
            for declaration in getattr(module, attribute):
                register(owners, declaration.name, root_owner, label)
            for source in imported:
                owner = source.source_identity or source.name
                for declaration in getattr(source, attribute):
                    register(owners, declaration.name, owner, label)

        owners: dict[str, str] = {}
        for source in (module, *module.submodules, *imported):
            if source.declaration_only:
                continue
            owner = source.source_identity or root_owner
            register(
                owners, source.name, owner, "module", root_duplicate=False
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
        direct_imports = tuple(item.path for item in module.imports)
        if len(direct_imports) != len(set(direct_imports)):
            duplicate = next(
                path for path in direct_imports if direct_imports.count(path) > 1
            )
            raise self._duplicate_import(module, duplicate, source_unit, source_digest)
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
            self._reject_conflicts(module, imported, owner)
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
