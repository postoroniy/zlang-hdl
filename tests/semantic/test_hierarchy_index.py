"""Stable physical hierarchy lookup independent of Python object identity."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from zlang.compilation_session import CompilationSession
from zlang.compiler import compile_source
from zlang.ir import hierarchy as ir_hierarchy
from zlang.ir.hierarchy import (
    HierarchyError,
    HierarchySpecializationKey,
    HierarchyTraversalCache,
    build_hierarchy_index,
)


SOURCE = """
module Leaf<W=8> {
    in x : uint<W>
    out y : uint<W>
    y = x
}

module Branch<W=8> {
    in x : uint<W>
    out y : uint<W>
    inst leaf : Leaf<W=W>
    leaf.x = x
    y = leaf.y
}

module Top {
    in a : u8
    in b : u16
    out x : u8
    out y : u8
    out z : u16
    inst left : Branch<W=8>
    inst right : Branch<W=8>
    inst wide : Branch<W=16>
    left.x = a
    right.x = a
    wide.x = b
    x = left.y
    y = right.y
    z = wide.y
}
"""


def _module():
    return compile_source(SOURCE, top="Top", include_clash=False).ir


def test_hierarchy_index_exposes_root_and_depth_first_physical_paths() -> None:
    hierarchy = build_hierarchy_index(_module())

    assert hierarchy.root_path == ("Top",)
    assert hierarchy.root.module.name == "Top"
    assert hierarchy.root.elaborated is None
    assert tuple(entry.physical_path for entry in hierarchy.entries) == (
        ("Top",),
        ("Top", "left"),
        ("Top", "left", "leaf"),
        ("Top", "right"),
        ("Top", "right", "leaf"),
        ("Top", "wide"),
        ("Top", "wide", "leaf"),
    )


def test_children_of_preserves_authoritative_parent_elaboration_order() -> None:
    hierarchy = build_hierarchy_index(_module())

    children = hierarchy.children_of(hierarchy.root_path)
    assert tuple(entry.physical_name for entry in children) == (
        "left",
        "right",
        "wide",
    )
    assert tuple(entry.parent_path for entry in children) == (
        ("Top",),
        ("Top",),
        ("Top",),
    )
    assert hierarchy.children_of(("Top", "left", "leaf")) == ()


def test_child_lookup_pairs_exact_parent_path_and_physical_instance() -> None:
    hierarchy = build_hierarchy_index(_module())

    left = hierarchy.child(("Top",), "left")
    right = hierarchy.child(("Top",), "right")
    assert left.physical_path == ("Top", "left")
    assert right.physical_path == ("Top", "right")
    assert left.specialization_identity == right.specialization_identity
    assert left.instance_identity != right.instance_identity

    left_leaf = hierarchy.child(left.physical_path, "leaf")
    right_leaf = hierarchy.child(right.physical_path, "leaf")
    assert left_leaf.physical_path == ("Top", "left", "leaf")
    assert right_leaf.physical_path == ("Top", "right", "leaf")
    # Reused typed specializations may reuse a child-local semantic identity.
    # Its physical parent path is therefore an essential part of lookup.
    assert left_leaf.instance_identity == right_leaf.instance_identity


def test_semantic_instance_lookup_is_scoped_by_physical_parent() -> None:
    hierarchy = build_hierarchy_index(_module())
    left_leaf = hierarchy.child(("Top", "left"), "leaf")
    identity = left_leaf.instance_identity
    assert identity is not None

    assert hierarchy.child_by_instance_identity(
        ("Top", "left"), identity
    ).physical_path == ("Top", "left", "leaf")
    assert hierarchy.child_by_instance_identity(
        ("Top", "right"), identity
    ).physical_path == ("Top", "right", "leaf")


def test_specialization_catalog_deduplicates_reuse_in_first_dfs_order() -> None:
    hierarchy = build_hierarchy_index(_module())
    catalog = hierarchy.specializations

    assert tuple(item.key.module_name for item in catalog) == (
        "Branch",
        "Leaf",
        "Branch",
        "Leaf",
    )
    assert catalog[0].occurrence_paths == (
        ("Top", "left"),
        ("Top", "right"),
    )
    assert catalog[1].occurrence_paths == (
        ("Top", "left", "leaf"),
        ("Top", "right", "leaf"),
    )
    assert catalog[2].occurrence_paths == (("Top", "wide"),)
    assert catalog[3].occurrence_paths == (("Top", "wide", "leaf"),)
    assert (
        catalog[0].key.specialization_identity
        != catalog[2].key.specialization_identity
    )
    assert (
        catalog[1].key.specialization_identity
        != catalog[3].key.specialization_identity
    )
    for item in catalog:
        assert (
            hierarchy.at(item.representative_path).module.name
            == item.key.module_name
        )
        assert hierarchy.specialization(item.key) == item

    with pytest.raises(HierarchyError, match="typed specialization is absent"):
        hierarchy.specialization(HierarchySpecializationKey("Leaf", "missing"))


def test_paths_and_catalog_are_stable_across_sessions_and_deepcopy() -> None:
    first_module = _module()
    second_module = _module()
    copied_module = deepcopy(first_module)
    assert id(first_module) != id(second_module)
    assert id(first_module) != id(copied_module)

    first = build_hierarchy_index(first_module)
    second = build_hierarchy_index(second_module)
    copied = build_hierarchy_index(copied_module)

    paths = tuple(entry.physical_path for entry in first.entries)
    assert tuple(entry.physical_path for entry in second.entries) == paths
    assert tuple(entry.physical_path for entry in copied.entries) == paths
    assert second.specializations == first.specializations
    assert copied.specializations == first.specializations


def test_stage_local_cache_reuses_exact_fingerprint_and_immutable_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = compile_source(
        "module Child { in x:u8 out y:u8 y=x } "
        "module Top { in x:u8 out y:u8 inst child:Child "
        "child.x=x y=child.y }",
        top="Top",
        include_clash=False,
    ).ir
    original = ir_hierarchy.specialization_fingerprint
    calls: list[object] = []

    def counted(child):
        calls.append(child)
        return original(child)

    monkeypatch.setattr(ir_hierarchy, "specialization_fingerprint", counted)
    cache = HierarchyTraversalCache()
    first = build_hierarchy_index(module, cache=cache)
    second = build_hierarchy_index(module, cache=cache)

    assert first is second
    assert calls == [module.children[0]]


def test_hierarchy_caches_are_independent_and_never_process_global(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (
        "module Child { in x:u8 out y:u8 y=x } "
        "module Top { in x:u8 out y:u8 inst child:Child "
        "child.x=x y=child.y }"
    )
    original = ir_hierarchy.specialization_fingerprint
    calls: list[object] = []

    def counted(child):
        calls.append(child)
        return original(child)

    monkeypatch.setattr(ir_hierarchy, "specialization_fingerprint", counted)
    first_module = CompilationSession(
        source, top="Top", include_clash=False
    ).check()
    second_module = CompilationSession(
        source, top="Top", include_clash=False
    ).check()
    first = build_hierarchy_index(first_module)
    second = build_hierarchy_index(second_module)

    assert first is not second
    # Each session owns one cache during recursive semantic analysis.  The two
    # uncached inspection calls above add one more fingerprint per session.
    assert len(calls) == 4
    assert calls[0] is first_module.children[0]
    assert calls[1] is second_module.children[0]
    assert calls[2] is first_module.children[0]
    assert calls[3] is second_module.children[0]
    assert tuple(item.physical_path for item in first.entries) == tuple(
        item.physical_path for item in second.entries
    )


def test_stable_lookup_reports_missing_parent_child_and_identity() -> None:
    hierarchy = build_hierarchy_index(_module())

    with pytest.raises(HierarchyError, match="physical instance path is absent"):
        hierarchy.children_of(("Top", "missing"))
    with pytest.raises(HierarchyError, match="physical child instance is absent"):
        hierarchy.child(("Top",), "missing")
    with pytest.raises(HierarchyError, match="semantic instance identity.*is absent"):
        hierarchy.child_by_instance_identity(("Top",), "missing-identity")


def test_builder_rejects_duplicate_sibling_semantic_instance_identity() -> None:
    module = _module()
    first, second, third = module.elaborated_instances
    malformed = replace(
        module,
        elaborated_instances=(
            first,
            replace(second, instance_identity=first.instance_identity),
            third,
        ),
    )

    with pytest.raises(
        HierarchyError,
        match="duplicate semantic instance identity",
    ):
        build_hierarchy_index(malformed)


def test_builder_rejects_duplicate_sibling_physical_name() -> None:
    module = _module()
    first, second, third = module.elaborated_instances
    duplicate = replace(
        second,
        instance=replace(second.instance, name=first.instance.name),
        semantic_path=(module.name, first.instance.name),
    )
    malformed = replace(
        module,
        elaborated_instances=(first, duplicate, third),
    )

    with pytest.raises(HierarchyError, match="duplicate physical child 'left'"):
        build_hierarchy_index(malformed)


def test_builder_rejects_incompatible_reused_specialization_identity() -> None:
    module = _module()
    cache = HierarchyTraversalCache()
    build_hierarchy_index(module, cache=cache)
    first, second, wide = module.elaborated_instances
    malformed = replace(
        module,
        elaborated_instances=(
            first,
            second,
            replace(
                wide,
                specialization_identity=first.specialization_identity,
            ),
        ),
    )

    with pytest.raises(
        HierarchyError,
        match="specialization identity .* is reused for incompatible 'Branch'",
    ):
        build_hierarchy_index(malformed, cache=cache)


def test_builder_rejects_recursive_module_object_cycle() -> None:
    module = _module()
    template = module.elaborated_instances[0]
    cycle_instance = replace(
        template,
        instance=replace(template.instance, name="cycle", module=module.name),
        child_module=module.name,
        instance_identity="cycle-instance",
        semantic_path=(module.name, "cycle"),
        specialization_identity="cycle-specialization",
    )
    object.__setattr__(module, "children", (module,))
    object.__setattr__(module, "elaborated_instances", (cycle_instance,))

    with pytest.raises(HierarchyError, match="cyclic typed module hierarchy"):
        build_hierarchy_index(module)


def test_specialization_key_keeps_module_names_distinct() -> None:
    module = compile_source(
        """
module First { in x : u8 out y : u8 y = x }
module Second { in x : u8 out y : u8 y = x }
module Top {
    in x : u8
    out a : u8
    out b : u8
    inst first : First
    inst second : Second
    first.x = x
    second.x = x
    a = first.y
    b = second.y
}
""",
        top="Top",
        include_clash=False,
    ).ir
    first, second = module.elaborated_instances
    shared_identity = first.specialization_identity
    assert shared_identity is not None
    module = replace(
        module,
        elaborated_instances=(
            first,
            replace(second, specialization_identity=shared_identity),
        ),
    )

    catalog = build_hierarchy_index(module).specializations
    assert tuple(item.key for item in catalog) == (
        HierarchySpecializationKey("First", shared_identity),
        HierarchySpecializationKey("Second", shared_identity),
    )
