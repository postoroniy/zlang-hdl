from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from zlang.backend.systemverilog import emit_experimental
from zlang.backend.systemverilog.emitter import SystemVerilogEmissionError
from zlang.compiler import compile_file, compile_source
from zlang.ir import (
    Assignment,
    Call,
    Constant,
    Function,
    FunctionParameter,
    InputRef,
    Module,
    ParameterRef,
    Port,
    PortDirection,
)
from zlang.ir.callables import (
    CallableKind,
    CallableMetadata,
    CallableReachabilityError,
    reachable_callable_definitions,
    reachable_module_callables,
)
from zlang.ir.types import UIntType
from zlang.toolchain import lint_with_verilator


ROOT = Path(__file__).resolve().parents[2]


HIERARCHICAL_CALLABLE_SOURCE = """
fn leaf(x : u8) -> u8 {
    truncate<8>(x + 1)
}

fn used(x : u8) -> u8 {
    leaf(leaf(x))
}

// Models an imported source helper that is legal but irrelevant to this top.
fn unused_imported_helper(x : u8) -> u8 {
    truncate<8>(x + 7)
}

module CallableChild {
    in x : u8
    out y : u8
    y = used(x)
}

module CallableTop {
    in x : u8
    out y : u8
    inst child : CallableChild { x }
    y = child.y
}
"""


FUNCTIONAL_CALLABLE_HIERARCHY_SOURCE = """
fn reorder(values : vec<64,u8>) -> vec<64,u8> {
    generate(i in 0..64) values[63 - i]
}

module FunctionalCallableChild {
    in values : vec<64,u8>
    out result : vec<64,u8>
    result = reorder(values)
}

module FunctionalCallableTop {
    in values : vec<64,u8>
    out result : vec<64,u8>
    inst child : FunctionalCallableChild { values }
    result = child.result
}
"""


NESTED_GENERIC_CALLABLE_HIERARCHY_SOURCE = """
fn reverse_bits<N>(value : bits<N>) {
    bitcast<bits<N>>(
        generate(i in 0..N) bitcast<vec<N,bit>>(value)[N - 1 - i]
    )
}

fn reverse6(index : u6) -> u6 {
    bitcast<u6>(reverse_bits<N=6>(bitcast<bits<6>>(index)))
}

module CallableGenericChild {
    in index : u6
    out result : u6
    result = reverse6(index)
}

module CallableGenericTop {
    in index : u6
    out result : u6
    inst child : CallableGenericChild { index }
    result = child.result
}
"""








def test_same_outer_identity_accepts_equivalent_context_specific_nested_ids() -> None:
    u8 = UIntType(8)
    parameter = FunctionParameter("x", u8)
    nested_metadata = dict(
        kind=CallableKind.FUNCTION,
        source_name="identity",
        declaration_identity="test:identity",
        arguments=(("T", "u8"),),
    )
    nested_a = Function(
        "zlang_spec_a",
        (parameter,),
        u8,
        ParameterRef("x", u8),
        metadata=CallableMetadata(
            **nested_metadata,
            specialization_identity="nested-a",
        ),
    )
    nested_b = Function(
        "zlang_spec_b",
        (parameter,),
        u8,
        ParameterRef("x", u8),
        metadata=CallableMetadata(
            **nested_metadata,
            specialization_identity="nested-b",
        ),
    )
    outer_metadata = CallableMetadata(
        CallableKind.FUNCTION,
        "outer",
        "test:outer",
        specialization_identity="outer-id",
    )
    outer_a = Function(
        "outer",
        (parameter,),
        u8,
        Call(
            nested_a.name,
            (ParameterRef("x", u8),),
            u8,
            nested_a.callee_identity,
        ),
        metadata=outer_metadata,
    )
    outer_b = Function(
        "outer",
        (parameter,),
        u8,
        Call(
            nested_b.name,
            (ParameterRef("x", u8),),
            u8,
            nested_b.callee_identity,
        ),
        metadata=outer_metadata,
    )
    root = Call("outer", (Constant(1, u8),), u8, outer_a.callee_identity)

    first = reachable_callable_definitions(
        (nested_a, nested_b, outer_a, outer_b),
        (root,),
    )
    second = reachable_callable_definitions(
        (outer_b, outer_a, nested_b, nested_a),
        (root,),
    )
    assert tuple(item.callee_identity for item in first) == tuple(
        item.callee_identity for item in second
    )
    assert len(first) == 2


def test_same_callable_identity_with_different_body_remains_a_conflict() -> None:
    u8 = UIntType(8)
    parameter = FunctionParameter("x", u8)
    first = Function("same", (parameter,), u8, ParameterRef("x", u8))
    conflicting = Function(
        "same",
        (parameter,),
        u8,
        Constant(0, u8),
        first.callee_identity,
    )
    root = Call("same", (Constant(1, u8),), u8, first.callee_identity)

    with pytest.raises(
        CallableReachabilityError,
        match="has conflicting definitions",
    ):
        reachable_callable_definitions((first, conflicting), (root,))


def test_same_identity_rejects_a_call_name_that_disagrees_with_its_callee() -> None:
    u8 = UIntType(8)
    parameter = FunctionParameter("x", u8)
    inner = Function("inner", (parameter,), u8, ParameterRef("x", u8))
    outer_metadata = CallableMetadata(
        CallableKind.FUNCTION,
        "outer",
        "test:outer",
        specialization_identity="outer-id",
    )
    valid = Function(
        "outer",
        (parameter,),
        u8,
        Call("inner", (ParameterRef("x", u8),), u8, inner.callee_identity),
        metadata=outer_metadata,
    )
    malformed = Function(
        "outer",
        (parameter,),
        u8,
        Call("wrong", (ParameterRef("x", u8),), u8, inner.callee_identity),
        metadata=outer_metadata,
    )
    root = Call("outer", (Constant(1, u8),), u8, valid.callee_identity)

    with pytest.raises(
        CallableReachabilityError,
        match="has conflicting definitions",
    ):
        reachable_callable_definitions((inner, valid, malformed), (root,))


def test_same_identity_rejects_different_definition_helper_names() -> None:
    u8 = UIntType(8)
    parameter = FunctionParameter("x", u8)
    metadata = CallableMetadata(
        CallableKind.FUNCTION,
        "source_name",
        "test:source-name",
        specialization_identity="same-id",
    )
    first = Function(
        "aaa",
        (parameter,),
        u8,
        ParameterRef("x", u8),
        metadata=metadata,
    )
    conflicting = Function(
        "zzz",
        (parameter,),
        u8,
        ParameterRef("x", u8),
        metadata=metadata,
    )
    root = Call("aaa", (Constant(1, u8),), u8, first.callee_identity)

    with pytest.raises(
        CallableReachabilityError,
        match="has conflicting definitions",
    ):
        reachable_callable_definitions((first, conflicting), (root,))


def test_distinct_reachable_identities_cannot_share_an_emitted_name() -> None:
    u8 = UIntType(8)
    parameter = FunctionParameter("x", u8)
    first = Function(
        "same",
        (parameter,),
        u8,
        ParameterRef("x", u8),
        metadata=CallableMetadata(
            CallableKind.FUNCTION,
            "same",
            "test:same",
            specialization_identity="same-a",
        ),
    )
    second = Function(
        "same",
        (parameter,),
        u8,
        ParameterRef("x", u8),
        metadata=CallableMetadata(
            CallableKind.FUNCTION,
            "same",
            "test:same",
            specialization_identity="same-b",
        ),
    )
    roots = (
        Call("same", (Constant(1, u8),), u8, first.callee_identity),
        Call("same", (Constant(2, u8),), u8, second.callee_identity),
    )

    with pytest.raises(
        CallableReachabilityError,
        match="typed function name 'same' has conflicting reachable definitions",
    ):
        reachable_callable_definitions((first, second), roots)




def test_ieee_framed_ifft_does_not_emit_unused_mapper_helpers() -> None:
    source = (
        ROOT
        / "examples/projects/80211a_transmitter/src/ifft.zhl"
    )
    if not source.exists():
        pytest.skip("production framed IFFT64 source is not present")
    module = compile_file(
        source,
        top="IeeeFramedIFFT64Raw",
    ).ir
    generated = emit_experimental(module)

    # Before reachable-only publication this artifact was about 1.61 MiB and
    # contained 441 helper declarations merely because the mapper dependency was
    # imported.  Keep a generous structural bound rather than snapshotting
    # unrelated RTL formatting.
    assert len(generated.encode()) < 256 * 1024
    assert generated.count("function automatic") < 64
    assert "ieee_mapper_frame" not in generated
    assert "raw_mapper_sample" not in generated
