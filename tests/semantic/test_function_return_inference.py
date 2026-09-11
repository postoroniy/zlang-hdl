from __future__ import annotations

from pathlib import Path

import pytest

from zlang.backend.systemverilog import emit_experimental
from zlang.compiler import compile_source
from zlang.ir import expressions as expr
from zlang.module_resolver import IndexedModuleResolver, load_indexed_module
from zlang.opt import lower, restore
from zlang.semantic import SemanticError
from zlang.simulate import simulate


def _compile(source: str):
    return compile_source(source).ir


def test_ordinary_function_infers_exact_concat_return_without_caller_context() -> None:
    module = _compile(
        "fn inferred(){concat(pack(0),pack(1),pack(0),pack(1))} "
        "module Top{out y:bits<4> y=inferred()}"
    )
    function = module.functions[0]
    assert str(function.return_type) == "bits<4>"
    assert isinstance(function.body, expr.Concat)
    assert tuple(item.type.width for item in function.body.operands) == (1, 1, 1, 1)
    assert restore(lower(module)) == module

    with pytest.raises(SemanticError, match="cannot assign bits<4> expression to bits<5>"):
        _compile(
            "fn inferred(){concat(pack(0),pack(1),pack(0),pack(1))} "
            "module Top{out y:bits<5> y=inferred()}"
        )


def test_forward_inferred_calls_are_declaration_order_independent() -> None:
    first = _compile(
        "fn outer(x:u8){inner(x)} fn inner(x:u8){x+x} "
        "module Top{in x:u8 out y:u9 y=outer(x)}"
    )
    second = _compile(
        "fn inner(x:u8){x+x} fn outer(x:u8){inner(x)} "
        "module Top{in x:u8 out y:u9 y=outer(x)}"
    )
    first_signatures = {
        item.name: (item.return_type, item.callee_identity) for item in first.functions
    }
    second_signatures = {
        item.name: (item.return_type, item.callee_identity) for item in second.functions
    }
    assert first_signatures == second_signatures
    assert simulate(first, x=0xA5) == {"y": 0x14A}
    assert simulate(second, x=0xA5) == {"y": 0x14A}


def test_explicit_and_inferred_exact_signatures_share_callable_identity() -> None:
    inferred = _compile(
        "fn twice(x:u8){x+x} module Top{in x:u8 out y:u9 y=twice(x)}"
    )
    explicit = _compile(
        "fn twice(x:u8)->u9{x+x} module Top{in x:u8 out y:u9 y=twice(x)}"
    )
    assert inferred.functions[0].callee_identity == explicit.functions[0].callee_identity
    assert lower(inferred).functions[0] == lower(explicit).functions[0]


def test_inferred_return_cycles_are_rejected_deterministically() -> None:
    with pytest.raises(
        SemanticError,
        match=r"recursive inferred return cycle: f -> g -> f",
    ) as caught:
        _compile(
            "fn f(x:u8){g(x)} fn g(x:u8){f(x)} "
            "module Top{in x:u8 out y:u8 y=x}"
        )
    assert caught.value.code == "ZL-FUNCTION-RETURN-CYCLE"
    assert len(caught.value.notes) == 2


def test_generic_direct_literal_uses_type_fixed_by_another_argument_or_specialization() -> None:
    module = _compile(
        "fn choose<type T>(a:T,b:T){b} fn identity<type T>(x:T){x} "
        "module Top{in x:u8 in sx:s8 out a:u8 out b:u8 out c:s8 "
        "a=choose(x,0) b=identity<T=u8>(1) c=choose(sx,-1)}"
    )
    assert tuple(item.expression.type for item in module.assignments) == (
        module.inputs[0].type,
        module.inputs[0].type,
        module.inputs[1].type,
    )
    choose = tuple(
        item
        for item in module.callable_definitions
        if item.metadata.source_name == "choose"
    )
    identity = next(
        item
        for item in module.callable_definitions
        if item.metadata.source_name == "identity"
    )
    assert all(isinstance(item.body, expr.ParameterRef) for item in choose)
    assert {item.body.type for item in choose} == {
        module.inputs[0].type,
        module.inputs[1].type,
    }
    assert identity.return_type == module.inputs[0].type


def test_generic_direct_literal_is_independent_of_argument_order_and_fixed_target() -> None:
    module = _compile(
        "fn choose<type T>(a:T,b:T){a} fn identity<type T>(x:T){x} "
        "module Top{in x:u8 out a:u8 out b:fixed<8,4> "
        "a=choose(0,x) b=identity<T=fixed<8,4>>(-1)}"
    )
    assert tuple(str(item.expression.type) for item in module.assignments) == (
        "u8",
        "fixed<8,4>",
    )

    for arguments in ("0,3", "3,0"):
        with pytest.raises(SemanticError, match="conflicting inference for type 'T'"):
            _compile(
                "fn choose<type T>(a:T,b:T){a} "
                f"module Top{{out y:u2 y=choose({arguments})}}"
            )


def test_generic_literal_context_does_not_come_from_result_or_compound_argument() -> None:
    with pytest.raises(SemanticError, match="cannot assign u1 expression to u8"):
        _compile(
            "fn identity<type T>(x:T){x} "
            "module Top{out y:u8 y=identity(0)}"
        )
    with pytest.raises(SemanticError, match="conflicting inference for type 'T'"):
        _compile(
            "fn choose<type T>(a:T,b:T){b} "
            "module Top{in x:u8 out y:u8 y=choose(x,0+0)}"
        )


def test_provisional_generic_literal_binding_preserves_call_diagnostic() -> None:
    with pytest.raises(SemanticError) as caught:
        _compile(
            "fn choose<type T>(a:T,b:T,c:T){a} "
            "module Top{in a:u8 in b:u9 out y:u8 y=choose(a,b,0)}"
        )
    assert str(caught.value) == "conflicting inference for type 'T': u8 and u9"
    assert caught.value.primary is not None
    assert caught.value.primary.construct == "call choose"
    assert caught.value.notes and "callable declared at" in caught.value.notes[0]




def test_inferred_ordinary_function_is_valid_static_callable() -> None:
    module = _compile(
        "fn widen(x:u8){x+x} "
        "fn apply<type A,type B,operation:fn(A)->B>(x:A){operation(x)} "
        "module Top{in x:u8 out y:u9 "
        "y=apply<A=u8,B=u9,operation=fn widen>(x)}"
    )
    assert simulate(module, x=0x80) == {"y": 0x100}


def test_qualified_import_preserves_inferred_return_signature(tmp_path: Path) -> None:
    relative = Path("helpers.zhl")
    source_path = tmp_path / relative
    source_path.write_text("fn make_tag(x:bits<2>){concat(x,zeros<2>)}")
    record = load_indexed_module(
        "vendor.helpers",
        source_root=tmp_path,
        relative_path=relative,
        package_identity="vendor",
    )
    resolver = IndexedModuleResolver(
        (record,),
        package_namespaces=("vendor",),
        include_stdlib=False,
    )
    result = compile_source(
        "import vendor.helpers as h "
        "module Top{in x:bits<2> out y:bits<4> y=h.make_tag(x)}",
        module_resolver=resolver,
        source_unit="app.top",
    )
    assert str(result.ir.functions[0].return_type) == "bits<4>"
    assert simulate(result.ir, x=3) == {"y": 12}


def test_mixed_explicit_and_inferred_recursion_is_rejected() -> None:
    with pytest.raises(SemanticError, match="recursive function call"):
        _compile(
            "fn explicit(x:u8)->u8{inferred(x)} "
            "fn inferred(x:u8){explicit(x)} "
            "module Top{in x:u8 out y:u8 y=x}"
        )
