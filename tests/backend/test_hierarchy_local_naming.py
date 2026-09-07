from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib

import pytest

from zlang.backend import naming
from zlang.backend.identifiers import rtl_identifier, rtl_instance_identifier
from zlang.backend.naming import (
    RTL_NAMING_SCHEMA,
    RtlNamingError,
    build_component_name_plan,
    module_rtl_names,
    rtl_hierarchy_instance_path,
    validate_component_name_plans,
)
from zlang.compiler import compile_source
from zlang.ir import CompileTimeBinderRef, Constant, FunctionalRegion, FunctionalRegionKind, UIntType, VecType
from zlang.ir.hierarchy import build_hierarchy_index
from zlang.ir.module import LocalValue


SPECIALIZED = """
module Child<W=8> { in x:uint<W> out y:uint<W> y=x }
module Top {
    in a:u8 in b:u16 out x:u8 out y:u16
    first:Child<8>{x=a}
    second:Child<16>{x=b}
    x=first.y y=second.y
}
"""

COLLISIONS = """
module Leaf {
    clock clk reset rst
    in step:bit out ready:bit out value:u8
    reg count:u8=0
    when step { count <- truncate<8>(count+1) }
    ready=1 value=count
}
module Top {
    clock clk reset rst
    in step:bit out y:u8
    reg cfg_ready:u8=0
    reg rule_tick_fire:u8=0
    cfg:Leaf{step}
    lane[2]:Leaf
    generate(i in 0..2){ lane[i].step=step }
    lane_0:Leaf{step}
    tick:when step { cfg_ready <- truncate<8>(cfg_ready+1) }
    y=cfg.value
}
"""


@pytest.fixture(scope="module")
def specialized():
    return compile_source(SPECIALIZED, include_clash=False).ir


@pytest.fixture(scope="module")
def collision_module():
    return compile_source(COLLISIONS, include_clash=False).ir


def test_component_names_are_compact_exact_keyed_and_order_independent(specialized) -> None:
    hierarchy = build_hierarchy_index(specialized)
    plan = build_component_name_plan(hierarchy)
    reversed_plan = build_component_name_plan(replace(
        hierarchy, entries=(hierarchy.entries[0], *reversed(hierarchy.entries[1:])),
    ))
    copied_plan = build_component_name_plan(build_hierarchy_index(deepcopy(specialized)))
    assert plan == reversed_plan == copied_plan
    assert plan.root_name == "Top"
    assert plan.schema == RTL_NAMING_SCHEMA
    assert len(plan.entries) == 2
    for item in plan.entries:
        assert item.physical_name == "Child_s" + item.key.specialization_identity[:8]
        assert item.suffix == "s" + item.key.specialization_identity[:8]
        assert len(item.key.specialization_identity) == 24
        assert plan.component(item.key.module_name, item.key.specialization_identity) == item.physical_name
    with pytest.raises(RtlNamingError, match="key is absent"):
        plan.component("Child", "not-a-specialization")


def test_component_prefix_collisions_extend_all_members_and_preserve_leading_zeroes(specialized) -> None:
    identities = ("00000000aaaa111111111111", "00000000aaaa222222222222")
    module = replace(specialized, elaborated_instances=tuple(
        replace(item, specialization_identity=identity)
        for item, identity in zip(specialized.elaborated_instances, identities, strict=True)
    ))
    plan = build_component_name_plan(build_hierarchy_index(module))
    assert tuple(item.physical_name for item in plan.entries) == (
        "Child_s00000000aaaa1111", "Child_s00000000aaaa2222",
    )
    assert tuple(item.key.specialization_identity for item in plan.entries) == identities

    # A frozen top token also wins; its child suffix extends instead.
    blocked = build_component_name_plan(
        build_hierarchy_index(module), reserved=("Child_s00000000aaaa1111",),
    )
    assert blocked.component("Child", identities[0]) == "Child_s00000000aaaa11111111"


def test_component_normalization_collision_never_equates_full_keys(specialized) -> None:
    hierarchy = build_hierarchy_index(specialized)
    children = tuple(replace(entry, module=replace(entry.module, name=name),
                             elaborated=replace(entry.elaborated, specialization_identity=identity))
                     for entry, name, identity in zip(
        hierarchy.entries[1:], ("Child", "CHILD"),
        ("aaaaaaaaaaaa111111111111", "aaaaaaaaaaaa222222222222"), strict=True,
    ))
    plan = build_component_name_plan(replace(hierarchy, entries=(hierarchy.root, *children)), identifier=str.lower)
    assert len({item.physical_name for item in plan.entries}) == 2
    assert all(item.physical_name.startswith("child_s") for item in plan.entries)
    assert {item.suffix for item in plan.entries} == {"saaaaaaaaaaaa1111", "saaaaaaaaaaaa2222"}
    assert {item.key.module_name for item in plan.entries} == {"Child", "CHILD"}
    assert plan.root_name == "top"


def test_independent_compilation_registry_rejects_truncated_prefix_collisions(specialized) -> None:
    hierarchy = build_hierarchy_index(specialized)
    first = replace(hierarchy, entries=(hierarchy.root, hierarchy.entries[1]))
    second_root = replace(hierarchy.root, module=replace(hierarchy.root.module, name="OtherTop"))
    second = replace(hierarchy, entries=(second_root, hierarchy.entries[2]))
    first_plan = build_component_name_plan(first)
    second_plan = build_component_name_plan(second)
    validate_component_name_plans(first_plan, second_plan)

    collision_entry = replace(
        second_plan.entries[0], physical_name=first_plan.entries[0].physical_name,
    )
    with pytest.raises(RtlNamingError, match="conflicting full identities"):
        validate_component_name_plans(first_plan, replace(second_plan, entries=(collision_entry,)))
    same_definition = replace(second_plan, entries=first_plan.entries)
    validate_component_name_plans(first_plan, same_definition)
    with pytest.raises(RtlNamingError, match="repeat a frozen public top"):
        validate_component_name_plans(first_plan, first_plan)


def test_source_names_win_over_generated_arrays_signals_and_rule_helpers(collision_module) -> None:
    from zlang.backend.systemverilog.emitter import (
        SystemVerilogEmissionError, _expression, emit_artifact,
    )

    plan = module_rtl_names(collision_module)
    assert plan.instance("cfg") == "cfg"
    assert plan.instance("lane_0") == "lane_0"
    assert plan.instance("lane[0]").startswith("lane_0_")
    assert plan.instance("lane[1]") == "lane_1"
    assert plan.child_signal("cfg", "value") == "cfg_value"
    assert plan.child_signal("cfg", "ready").startswith("cfg_ready_")
    assert plan.rule("tick").startswith("rule_tick_fire_")
    assert plan.instance_helper("cfg", "result") == "cfg_result"
    assert {"cfg_ready", "rule_tick_fire"} <= plan.reserved
    assert len({item.physical_name for item in plan.entries}) == len(plan.entries)
    assert not any("__" in item.physical_name for item in plan.entries)
    assert plan.allocated_names == plan.reserved | {item.physical_name for item in plan.entries}
    assert plan == module_rtl_names(deepcopy(collision_module))
    with pytest.raises(RtlNamingError, match="key is absent"):
        plan.instance("missing")
    # A contextless expression must never invent a stale or colliding locator.
    with pytest.raises(SystemVerilogEmissionError, match="containing module naming plan") as error:
        _expression(collision_module.assignments[0].expression)
    assert error.value.code == "ZL-BACKEND-SYSTEMVERILOG-NAMING-CONTEXT"
    assert error.value.semantic_path == ("cfg", "value")
    assert "assign y = cfg_value;" in emit_artifact(collision_module).text


def test_reused_parent_component_names_ignore_physical_ancestor_identities(collision_module) -> None:
    first = module_rtl_names(collision_module)
    second_occurrence = replace(collision_module, elaborated_instances=tuple(
        replace(item, instance_identity=hashlib.sha256(f"other-parent/{item.instance_identity}".encode()).hexdigest(),
                semantic_path=("GrandTop", "second", item.instance.name))
        for item in collision_module.elaborated_instances
    ))
    assert first == module_rtl_names(second_occurrence)
    reordered = replace(collision_module,
                        children=tuple(reversed(collision_module.children)),
                        elaborated_instances=tuple(reversed(collision_module.elaborated_instances)))
    assert first == module_rtl_names(reordered)


def test_anonymous_rules_and_source_hint_stages_are_local_and_deterministic() -> None:
    module = compile_source("""
module StageNames {
    clock clk reset rst
    in x:u8 in go:bit out y:u8 out z:u8
    reg count:u8=0
    when go { count <- truncate<8>(count+1) }
    acc:u8 = pipeline(2) { x }
    y=acc z=delay<1>(x)
}
""", include_clash=False).ir
    plan = module_rtl_names(module)
    anonymous = module.rules[0].name
    assert anonymous.startswith("__anonymous_rule_")
    assert plan.rule(anonymous, "guard") == "rule_when_00_guard"
    assert plan.rule(anonymous) == "rule_when_00_fire"
    stages = {item.physical_name for item in plan.entries if item.kind == "stage"}
    # The selected compiler product may have already eliminated `acc`; do not
    # guess its spelling from source text.  Its direct output remains a hint.
    assert stages == {"y_pipe_s1", "y_pipe_s2", "z_delay_s1"}
    pipeline = module.assignments[0].expression
    retained = replace(module, locals=(LocalValue("acc", pipeline.type, pipeline),))
    retained_stages = {item.physical_name for item in module_rtl_names(retained).entries if item.kind == "stage"}
    assert retained_stages == {"acc_pipe_s1", "acc_pipe_s2", "z_delay_s1"}
    assert all(anonymous not in name for name in stages)


def test_reserved_identifier_policy_and_protocol_leaf_keys_are_supported() -> None:
    module = compile_source("""
module Child {
    in rx:rv<u8> out tx:rv<u8>
    tx.payload=rx.payload tx.valid=rx.valid rx.ready=tx.ready
}
module Top {
    in rx:rv<u8> out tx:rv<u8>
    table:Child
    rx -> table.rx
    table.tx -> tx
}
""", include_clash=False).ir
    sv = module_rtl_names(module)
    assert sv.instance("table") == "zlang_table"
    assert sv.child_signal("table", "tx", "payload") == "zlang_table_tx_payload"
    assert sv.child_signal("table", "rx_ready") == "zlang_table_rx_ready"
    clash = module_rtl_names(module, identifier=lambda value: f"v_{value}" if value == "table" else value,
                             reserved=("v_table_tx_payload",))
    assert clash.instance("table") == "v_table"
    assert clash.child_signal("table", "tx_payload").startswith("v_table_tx_payload_")
    assert rtl_instance_identifier("lane[0]") == "lane_0"
    assert rtl_instance_identifier("table") == rtl_identifier("table")


def test_child_packed_ports_do_not_reserve_duplicate_public_leaf_aliases() -> None:
    from tests.test_parameterized_aggregate_protocol import SOURCE
    from zlang.backend.clash.emitter import _clash_module_names

    aggregate = compile_source(SOURCE, include_clash=False).ir
    for plan in (module_rtl_names(aggregate), _clash_module_names(aggregate)):
        assert plan.child_signal("c", "bus__irq") == "c_bus_irq"
        assert plan.child_signal("c", "bus__req", "ready") == "c_bus_req_ready"
        assert not any(item.kind == "child_signal" and item.key == ("c", "bus_irq")
                       for item in plan.entries)

    structured = compile_source("""
    struct Payload { irq:bit data:u8 }
    module Child { in x:u8 out packet:Payload packet=Payload{irq=1 data=x} }
    module Top { in x:u8 out y:u8 c:Child{x} y=c.packet.data }
    """, include_clash=False).ir
    packed = module_rtl_names(structured)
    assert packed.child_signal("c", "packet") == "c_packet"
    assert not any(item.kind == "child_signal" and item.key in {
        ("c", "packet_irq"), ("c", "packet_data"),
    } for item in packed.entries)

    # Two source ports with colliding public spellings remain rejected by the
    # typed ABI; removing an alias reservation must not merge their identities.
    with pytest.raises(ValueError, match="public leaf name collision"):
        compile_source("""
        module Child { in x:bit out bus_irq:bit out bus__irq:bit bus_irq=x bus__irq=x }
        module Top { in x:bit out y:bit out z:bit c:Child{x} y=c.bus_irq z=c.bus__irq }
        """, include_clash=False)

    # Distinct legal packed ports below different physical owners can also
    # share one preferred local spelling. Both must retain unique allocation.
    distinct = compile_source("""
    module First { in x:bit out irq:bit irq=x }
    module Second { in x:bit out b_irq:bit b_irq=x }
    module Top { in x:bit out y:bit out z:bit a_b:First{x} a:Second{x} y=a_b.irq z=a.b_irq }
    """, include_clash=False).ir
    for plan in (module_rtl_names(distinct), _clash_module_names(distinct)):
        first = plan.child_signal("a_b", "irq")
        second = plan.child_signal("a", "b_irq")
        assert first.startswith("a_b_irq_") and second.startswith("a_b_irq_")
        assert first != second


def test_semantic_hierarchy_paths_resolve_through_each_parent_namespace(collision_module) -> None:
    hierarchy = build_hierarchy_index(collision_module)
    plans = {hierarchy.root_path: module_rtl_names(collision_module)}
    for path in (("Top", "lane[0]"), ("Top", "lane_0"), ("Top", "cfg")):
        mapped = rtl_hierarchy_instance_path(hierarchy, path, plans=plans)
        assert mapped == (plans[("Top",)].instance(path[-1]),)
    assert rtl_hierarchy_instance_path(hierarchy, ("Top",)) == ()
    assert rtl_hierarchy_instance_path(hierarchy, ("Top", "lane[0]")) != rtl_hierarchy_instance_path(hierarchy, ("Top", "lane_0"))


def test_naming_traversal_does_not_expand_or_fingerprint_functional_regions(monkeypatch, collision_module) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("naming must not expand functional IR or fingerprint hierarchy")

    import zlang.ir.functional as functional
    import zlang.ir.hierarchy as hierarchy
    monkeypatch.setattr(functional, "materialize_functional_region", forbidden)
    monkeypatch.setattr(functional, "materialize_exact_reduction", forbidden)
    monkeypatch.setattr(hierarchy, "specialization_fingerprint", forbidden)
    byte = UIntType(8)
    region = FunctionalRegion(
        FunctionalRegionKind.GENERATE,
        CompileTimeBinderRef("naming:i", "i", 0, 64),
        Constant(1, byte), (), (), VecType(64, byte),
    )
    with_region = replace(collision_module, locals=(LocalValue("generated", region.type, region),))
    plan = module_rtl_names(with_region)
    assert plan.child_signal("cfg", "value") == "cfg_value"
    # Force digest exhaustion rather than accepting two equal short names.
    monkeypatch.setattr(naming, "_digest", lambda identity: "a" * 64)
    with pytest.raises(RtlNamingError, match="exhaust"):
        naming._allocate_requests((
            naming._Request("probe", ("a",), "identity-a", "same"),
            naming._Request("probe", ("b",), "identity-b", "same"),
        ), set(), rtl_identifier)
