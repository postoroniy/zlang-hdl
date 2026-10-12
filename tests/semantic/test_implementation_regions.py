from __future__ import annotations

from dataclasses import replace

import pytest

from zlang.implementation_regions import (
    IMPLEMENTATION_REGION_SCHEMA,
    ImplementationRegionError,
    canonical_type_data,
    discover_implementation_regions,
    match_implementation_regions,
    module_semantic_identity,
)
from zlang.ir.types import FixedOverflowPolicy, FixedType, StructField, StructType, UIntType
from zlang.parser import parse
from zlang.semantic import analyze


def _analyze(source: str):
    return analyze(parse(source))


def test_discovers_public_scalar_wire_assignments_deterministically() -> None:
    module = _analyze(
        "module Math { in a:u8 in b:u8 out sum:u9 out product:u16 "
        "sum=a+b product=a*b }"
    )

    first = discover_implementation_regions(module)
    second = discover_implementation_regions(module)

    assert first == second
    assert tuple(item.output_name for item in first) == ("product", "sum")
    assert len({item.identity for item in first}) == 2
    assert all(len(item.identity) == 64 for item in first)
    assert all(len(item.module_identity) == 64 for item in first)
    assert all(len(item.output_binding_identity) == 64 for item in first)
    assert all(len(item.expression_identity) == 64 for item in first)
    assert all(item.to_data()["identity"] == item.identity for item in first)
    assert IMPLEMENTATION_REGION_SCHEMA == "zlang-implementation-region-v1"


def test_equivalent_mux_and_concise_ternary_have_the_same_region_identity() -> None:
    verbose = _analyze(
        "module Pick { in select:bit in a:u8 in b:u8 out y:u8 "
        "y=mux(select,a,b) }"
    )
    concise = _analyze(
        """
        module Pick {
          in select : bit
          in a : u8
          in b : u8
          out y : u8
          y = select ? a : b
        }
        """
    )

    verbose_region = discover_implementation_regions(verbose)[0]
    concise_region = discover_implementation_regions(concise)[0]
    assert verbose_region.expression_identity == concise_region.expression_identity
    assert verbose_region.module_identity == concise_region.module_identity
    assert verbose_region.identity == concise_region.identity


def test_identical_expressions_bound_to_different_outputs_do_not_alias() -> None:
    module = _analyze(
        "module Fanout { in a:u8 in b:u8 out left:u9 out right:u9 "
        "left=a+b right=a+b }"
    )
    left, right = discover_implementation_regions(module)

    assert left.expression_identity == right.expression_identity
    assert left.output_binding_identity != right.output_binding_identity
    assert left.identity != right.identity


def test_module_name_and_concrete_specialization_are_semantic_identity_inputs() -> None:
    first = _analyze("module First { in x:u8 out y:u8 y=x }")
    second = _analyze("module Second { in x:u8 out y:u8 y=x }")
    hierarchy = _analyze(
        "module Cell<type T,N=1>{in x:T out y:T y=x} "
        "module Top{in a:u8 in b:u16 out y:u8 "
        "inst a1:Cell<T=u8,N=1>{x=a} "
        "inst a2:Cell<T=u8,N=2>{x=a} "
        "inst b1:Cell<T=u16,N=1>{x=b} y=a1.y}"
    )

    assert module_semantic_identity(first) != module_semantic_identity(second)
    child_identities = tuple(module_semantic_identity(child) for child in hierarchy.children)
    assert len(child_identities) == len(set(child_identities)) == 3


def test_discovery_ignores_aggregate_outputs_and_protocol_member_assignments() -> None:
    aggregate = _analyze(
        "struct Pair { low:u8 high:u8 } "
        "module Aggregate { in x:Pair out y:Pair y=x }"
    )
    protocol = _analyze(
        "module Stream { in rx:rv<u8> out tx:rv<u8> "
        "tx.payload=rx.payload tx.valid=rx.valid rx.ready=tx.ready }"
    )

    assert discover_implementation_regions(aggregate) == ()
    assert discover_implementation_regions(protocol) == ()


def test_canonical_result_type_retains_structure_and_fixed_overflow_policy() -> None:
    wrapped = FixedType(16, 8, FixedOverflowPolicy.WRAP)
    saturated = FixedType(16, 8, FixedOverflowPolicy.SATURATE)
    pair = StructType("Pair", (StructField("sample", saturated),))

    assert canonical_type_data(wrapped) != canonical_type_data(saturated)
    assert canonical_type_data(pair) == {
        "kind": "struct",
        "name": "Pair",
        "fields": [
            {
                "name": "sample",
                "type": {
                    "kind": "fixed",
                    "width": 16,
                    "fraction": 8,
                    "overflow": "saturate",
                },
            }
        ],
    }


def test_exact_match_preserves_selector_order_and_rejects_duplicate_or_stale_ids() -> None:
    module = _analyze(
        "module Math { in a:u8 in b:u8 out sum:u9 out product:u16 "
        "sum=a+b product=a*b }"
    )
    product, sum_ = discover_implementation_regions(module)

    assert match_implementation_regions(module, (sum_.identity, product.identity)) == (
        sum_,
        product,
    )
    with pytest.raises(
        ImplementationRegionError,
        match="duplicate implementation region selector",
    ):
        match_implementation_regions(module, (sum_.identity, sum_.identity))
    with pytest.raises(
        ImplementationRegionError,
        match="unknown or stale implementation region identity 'stale-region'",
    ):
        match_implementation_regions(module, ("stale-region",))
    with pytest.raises(
        ImplementationRegionError,
        match="duplicate discovered implementation region identity",
    ):
        match_implementation_regions((sum_, sum_), (sum_.identity,))


def test_duplicate_output_binding_and_malformed_typed_assignment_fail_closed() -> None:
    module = _analyze("module One { in x:u8 out y:u8 y=x }")
    assignment = module.assignments[0]
    duplicated = replace(module, assignments=(assignment, assignment))
    with pytest.raises(
        ImplementationRegionError,
        match="duplicate implementation region binding 'One.y'",
    ):
        discover_implementation_regions(duplicated)

    malformed_expression = replace(assignment.expression, type=UIntType(9))
    malformed = replace(
        module,
        assignments=(replace(assignment, expression=malformed_expression),),
    )
    with pytest.raises(
        ImplementationRegionError,
        match="mismatched output and expression types",
    ):
        discover_implementation_regions(malformed)


def test_recursive_discovery_is_explicitly_outside_the_first_profile_slice() -> None:
    module = _analyze("module One { in x:u8 out y:u8 y=x }")
    with pytest.raises(
        ImplementationRegionError,
        match="recursive implementation-region discovery is not supported",
    ):
        discover_implementation_regions(module, recursive=True)
