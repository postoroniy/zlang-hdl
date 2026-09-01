from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path

import pytest

from zlang.dependencies import DependencyClosure, DependencyModuleIdentity
from zlang.module_resolver import (
    IndexedModuleResolver,
    ModuleResolutionError,
    StdlibModuleResolver,
    load_indexed_module,
)
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


def _write_module(
    root: Path,
    logical: str,
    source: str,
    *,
    package: str = "vendor",
):
    relative = Path(*logical.split(".")[1:]).with_suffix(".zl")
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return load_indexed_module(
        logical,
        source_root=root,
        relative_path=relative,
        package_identity=package,
    )


def test_parser_accepts_dotted_logical_import_and_retains_origin() -> None:
    module = parse("import vendor.math.fixed\nmodule Top {}")
    assert module.imports[0].path == "vendor.math.fixed"
    assert module.imports[0].origin is not None
    assert module.imports[0].origin.start_line == 1


def test_default_semantics_remains_std_only_with_structured_import_origin() -> None:
    with pytest.raises(SemanticError) as captured:
        analyze(parse("import vendor.math.fixed\nmodule Top {}"))
    assert "external imports are unsupported without a locked project" in str(
        captured.value
    )
    assert captured.value.diagnostic.code == "ZL-IMPORT-RESOLVE"
    assert captured.value.diagnostic.primary is not None
    assert captured.value.diagnostic.primary.span.start_line == 1

    std = analyze(parse("import std.bus.reg module Top {}"))
    assert std.library_imports == ("std.bus.reg",)


def test_exact_index_resolves_transitive_root_and_std_modules_dependency_first(
    tmp_path: Path,
) -> None:
    common = _write_module(
        tmp_path,
        "vendor.common",
        "type Byte = u8 module Common { in x:Byte out y:Byte y=x }",
    )
    child = _write_module(
        tmp_path,
        "vendor.child",
        "import vendor.common import std.bus.reg "
        "module Child { in x:u8 out y:u8 y=x }",
    )
    resolver = IndexedModuleResolver(
        (child, common), package_namespaces=("vendor",)
    )
    closure = resolver.resolve(("vendor.child",))
    assert tuple(item.logical_path for item in closure) == (
        "vendor.common",
        "std.bus.reg",
        "vendor.child",
    )

    module = analyze(
        parse(
            "import vendor.child module Top { "
            "in x:Byte out y:Byte inst c:Child c.x=x y=c.y }"
        ),
        module_resolver=resolver,
    )
    assert module.library_imports == (
        "std.bus.reg",
        "vendor.child",
        "vendor.common",
    )
    assert tuple(path for path, _ in module.library_dependencies) == (
        "vendor.common",
        "std.bus.reg",
        "vendor.child",
    )
    # The imported child is analyzed recursively.  Its own external import
    # succeeds only when the same per-compilation context is propagated.
    assert module.children[0].name == "Child"


def test_index_rejects_logical_case_namespace_and_reserved_std_conflicts(
    tmp_path: Path,
) -> None:
    lower = _write_module(tmp_path, "vendor.math", "module Lower {}")
    upper = _write_module(tmp_path, "Vendor.Math", "module Upper {}", package="Vendor")
    with pytest.raises(ModuleResolutionError, match="case-folding"):
        IndexedModuleResolver((lower, upper), include_stdlib=False)
    with pytest.raises(ModuleResolutionError, match="package namespaces"):
        IndexedModuleResolver(
            (lower,),
            package_namespaces=("vendor", "vendor.math"),
            include_stdlib=False,
        )
    with pytest.raises(ModuleResolutionError, match="package namespaces"):
        IndexedModuleResolver(
            (),
            package_namespaces=("Vendor", "vendor.math"),
            include_stdlib=False,
        )

    std_root = tmp_path / "std-root"
    std_source = _write_module(
        std_root,
        "std.user",
        "module FakeStd {}",
        package="std",
    )
    with pytest.raises(ModuleResolutionError, match="reserved"):
        IndexedModuleResolver((std_source,), include_stdlib=False)


def test_duplicate_logical_index_entries_are_rejected_even_if_identical(
    tmp_path: Path,
) -> None:
    source = _write_module(tmp_path, "vendor.shared", "module Shared {}")
    with pytest.raises(ModuleResolutionError, match="conflicting logical module"):
        IndexedModuleResolver((source, source), include_stdlib=False)


def test_import_cycles_unknown_modules_and_undeclared_packages_are_distinct(
    tmp_path: Path,
) -> None:
    a = _write_module(
        tmp_path, "vendor.a", "import vendor.b module A {}"
    )
    b = _write_module(
        tmp_path, "vendor.b", "import vendor.a module B {}"
    )
    resolver = IndexedModuleResolver(
        (a, b), package_namespaces=("vendor",), include_stdlib=False
    )
    with pytest.raises(
        ModuleResolutionError,
        match=r"vendor\.a -> vendor\.b -> vendor\.a",
    ):
        resolver.resolve(("vendor.a",))
    with pytest.raises(ModuleResolutionError, match="unknown module.*vendor.missing"):
        resolver.resolve(("vendor.missing",))
    with pytest.raises(ModuleResolutionError, match="undeclared logical import"):
        resolver.resolve(("other.missing",))


def test_source_paths_reject_traversal_symlink_escape_and_dirty_locked_content(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.zl"
    outside.write_text("module Outside {}", encoding="utf-8")
    with pytest.raises(ModuleResolutionError, match="invalid source path"):
        load_indexed_module(
            "vendor.outside", source_root=root, relative_path="../outside.zl"
        )

    (root / "escaped.zl").symlink_to(outside)
    with pytest.raises(ModuleResolutionError, match="escapes package root"):
        load_indexed_module(
            "vendor.escaped", source_root=root, relative_path="escaped.zl"
        )

    clean = _write_module(root, "vendor.clean", "module Clean {}")
    clean.source_path.write_text("module Dirty {}", encoding="utf-8")
    resolver = IndexedModuleResolver(
        (clean,), package_namespaces=("vendor",), include_stdlib=False
    )
    with pytest.raises(ModuleResolutionError, match="locked module.*dirty"):
        resolver.resolve(("vendor.clean",))


def test_expected_digest_and_index_dependency_metadata_are_enforced(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "dep.zl"
    source.write_text("module Dep {}", encoding="utf-8")
    wrong = hashlib.sha256(b"other").hexdigest()
    with pytest.raises(ModuleResolutionError, match="locked module.*dirty"):
        load_indexed_module(
            "vendor.dep",
            source_root=root,
            relative_path="dep.zl",
            expected_digest=wrong,
        )

    record = load_indexed_module(
        "vendor.dep", source_root=root, relative_path="dep.zl"
    )
    inconsistent = replace(record, dependencies=("vendor.hidden",))
    resolver = IndexedModuleResolver(
        (inconsistent,), package_namespaces=("vendor",), include_stdlib=False
    )
    with pytest.raises(ModuleResolutionError, match="dependency mismatch"):
        resolver.resolve(("vendor.dep",))


@pytest.mark.parametrize(
    "kind", ("type alias", "struct", "function", "protocol", "module")
)
def test_cross_source_declaration_and_module_conflicts_fail_closed(
    tmp_path: Path,
    kind: str,
) -> None:
    imported_source = {
        "type alias": "type Clash = u8 module Library {}",
        "struct": "struct Clash { value:u8 } module Library {}",
        "function": "fn clash(x:u8)->u8 { x } module Library {}",
        "protocol": (
            "protocol Clash { role source role sink "
            "channel data:u8 source->sink } module Library {}"
        ),
        "module": "module Clash {}",
    }[kind]
    root_prefix = {
        "type alias": "type Clash = u16 ",
        "struct": "struct Clash { other:u8 } ",
        "function": "fn clash(x:u8)->u8 { x + 0 } ",
        "protocol": (
            "protocol Clash { role source role sink "
            "channel other:u8 source->sink } "
        ),
        "module": "module Clash {} ",
    }[kind]
    imported = _write_module(tmp_path, "vendor.library", imported_source)
    resolver = IndexedModuleResolver(
        (imported,), package_namespaces=("vendor",), include_stdlib=False
    )
    with pytest.raises(SemanticError, match=f"conflicting {kind}"):
        analyze(
            parse("import vendor.library " + root_prefix + "module Top {}"),
            module_resolver=resolver,
        )


def test_imported_source_identity_and_digest_are_logical_not_physical(
    tmp_path: Path,
) -> None:
    source = _write_module(
        tmp_path,
        "vendor.types",
        "struct Word { value:u8 } module Types {}",
    )
    assert source.ast.source_identity == "vendor.types"
    assert source.ast.source_hash == source.digest
    assert source.ast.structs[0].source_identity == "vendor.types"
    assert str(tmp_path) not in source.ast.source_identity


def test_resolution_context_and_explicit_resolver_cannot_disagree(
    tmp_path: Path,
) -> None:
    from zlang.module_resolver import ModuleResolutionContext

    first = IndexedModuleResolver((), include_stdlib=False)
    second = IndexedModuleResolver((), include_stdlib=False)
    with pytest.raises(SemanticError, match="conflicts with"):
        analyze(
            parse("module Top {}"),
            module_resolver=first,
            resolution_context=ModuleResolutionContext(second),
        )


def test_project_identity_context_reaches_root_and_imported_child_ir(
    tmp_path: Path,
) -> None:
    package_identity = "c" * 64
    child = _write_module(
        tmp_path,
        "vendor.child",
        "module Child { in x:u8 out y:u8 y=x }",
        package=package_identity,
    )
    resolver = IndexedModuleResolver(
        (child,), package_namespaces=("vendor",), include_stdlib=False
    )
    root_identity = DependencyModuleIdentity(
        "root.top", "a" * 64, "b" * 64
    )
    child_identity = DependencyModuleIdentity(
        child.logical_path,
        child.digest,
        package_identity,
    )
    closure = DependencyClosure(1, "d" * 64, (child_identity,))

    module = analyze(
        parse(
            "import vendor.child module Top { "
            "in x:u8 out y:u8 inst c:Child c.x=x y=c.y }"
        ),
        source_unit="root.top",
        source_digest=root_identity.digest,
        module_resolver=resolver,
        root_module_identity=root_identity,
        dependency_closure=closure,
    )

    assert module.root_module_identity == root_identity
    assert module.dependency_closure == closure
    assert module.children[0].root_module_identity == child_identity
    assert module.children[0].dependency_closure == closure


def test_stdlib_resolver_protocol_preserves_dependency_first_behavior() -> None:
    closure = StdlibModuleResolver().resolve(("std.bus.axi_lite",))
    assert tuple(item.logical_path for item in closure) == (
        "std.bus.reg",
        "std.bus.axi_lite",
    )
