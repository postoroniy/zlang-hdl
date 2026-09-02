from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from tests.toolchain import CLASH_EXECUTABLE
from zlang.backend.clash import emit as emit_clash
from zlang.backend.clash.emitter import ClashEmissionError
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
from zlang.toolchain import generate_verilog, lint_with_verilator


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


def test_backends_emit_only_the_hierarchy_wide_callable_dependency_closure() -> None:
    module = compile_source(
        HIERARCHICAL_CALLABLE_SOURCE,
        top="CallableTop",
        include_clash=False,
    ).ir

    first_sv = emit_experimental(module)
    first_clash = emit_clash(module)
    assert emit_experimental(module) == first_sv
    assert emit_clash(module) == first_clash

    # Direct SV owns helpers in the physical child component; Clash owns the
    # same reachable closure once in its hierarchy-wide declaration prelude.
    for source_name in ("leaf", "used"):
        assert first_sv.count(
            f"function automatic logic [7:0] {source_name}("
        ) == 1
        assert first_clash.count(f"{source_name} ::") == 1
    assert "unused_imported_helper" not in first_sv
    assert "unused_imported_helper" not in first_clash
    assert "used(x)" in first_sv
    assert "used x" in first_clash


def test_hierarchy_wide_functional_callable_binders_are_alpha_equivalent() -> None:
    module = compile_source(
        FUNCTIONAL_CALLABLE_HIERARCHY_SOURCE,
        top="FunctionalCallableTop",
        include_clash=False,
    ).ir

    # The same source callable is typed in the parent and physical child
    # contexts.  Context-local compact-region binder IDs must not turn that one
    # monomorphic definition into a hierarchy-wide Clash conflict.
    generated = emit_clash(module)
    assert generated.count("reorder ::") == 1
    assert generated.count("reorder values") == 1


def test_hierarchy_deduplicates_resolved_nested_generic_call_graphs() -> None:
    module = compile_source(
        NESTED_GENERIC_CALLABLE_HIERARCHY_SOURCE,
        top="CallableGenericTop",
        include_clash=False,
    ).ir

    first = reachable_module_callables(module, include_hierarchy=True)
    second = reachable_module_callables(module, include_hierarchy=True)
    assert tuple(item.callee_identity for item in first) == tuple(
        item.callee_identity for item in second
    )
    assert len(first) == 2
    assert sum(item.name == "reverse6" for item in first) == 1
    assert sum(
        item.metadata is not None
        and item.metadata.source_name == "reverse_bits"
        for item in first
    ) == 1

    modules = (module, *module.children)
    definitions = tuple(
        definition
        for current in modules
        for definition in (*current.functions, *current.callable_definitions)
    )
    roots = tuple(
        assignment
        for current in modules
        for assignment in current.assignments
    )
    reordered = reachable_callable_definitions(
        tuple(reversed(definitions)),
        roots,
    )
    assert tuple(item.callee_identity for item in reordered) == tuple(
        item.callee_identity for item in first
    )

    generated = emit_clash(module)
    assert generated == emit_clash(module)
    assert generated.count("reverse6 ::") == 1


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


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_reachable_child_helpers_generate_and_lint_in_both_backends(
    tmp_path: Path,
) -> None:
    module = compile_source(
        HIERARCHICAL_CALLABLE_SOURCE,
        top="CallableTop",
        include_clash=False,
    ).ir
    direct = tmp_path / "CallableTop.sv"
    direct.write_text(emit_experimental(module))
    lint_with_verilator((direct,), "CallableTop")

    rtl = generate_verilog(
        emit_clash(module),
        "CallableTop",
        tmp_path / "clash",
        CLASH_EXECUTABLE,
    )
    lint_with_verilator(rtl, "CallableTop")


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
        include_clash=False,
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


@pytest.mark.parametrize(
    ("backend", "error_type"),
    (
        (emit_experimental, SystemVerilogEmissionError),
        (emit_clash, ClashEmissionError),
    ),
)
def test_reachable_unknown_callable_remains_a_backend_error(
    backend,
    error_type,
) -> None:
    u8 = UIntType(8)
    output = Port(PortDirection.OUTPUT, "y", u8)
    module = Module(
        "UnknownCallable",
        (output,),
        (Assignment(output, Call("missing", (), u8)),),
    )
    with pytest.raises(error_type, match="unknown legacy typed function"):
        backend(module)


@pytest.mark.parametrize(
    ("backend", "error_type"),
    (
        (emit_experimental, SystemVerilogEmissionError),
        (emit_clash, ClashEmissionError),
    ),
)
def test_reachable_recursive_callable_remains_a_backend_error(
    backend,
    error_type,
) -> None:
    u8 = UIntType(8)
    parameter = FunctionParameter("x", u8)
    prototype = Function("loop", (parameter,), u8, ParameterRef("x", u8))
    recursive = Function(
        "loop",
        (parameter,),
        u8,
        Call(
            "loop",
            (ParameterRef("x", u8),),
            u8,
            prototype.callee_identity,
        ),
        prototype.callee_identity,
    )
    input_ = Port(PortDirection.INPUT, "x", u8)
    output = Port(PortDirection.OUTPUT, "y", u8)
    module = Module(
        "RecursiveCallable",
        (input_, output),
        (
            Assignment(
                output,
                Call(
                    "loop",
                    (InputRef("x", u8),),
                    u8,
                    recursive.callee_identity,
                ),
            ),
        ),
        functions=(recursive,),
    )
    with pytest.raises(error_type, match="recursive typed callable cycle"):
        backend(module)
