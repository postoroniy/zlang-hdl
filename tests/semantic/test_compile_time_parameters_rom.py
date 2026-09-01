from __future__ import annotations

from dataclasses import replace

import pytest

from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.common import stable_digest
from zlang.compiler import compile_source
from zlang.ir import expressions as expr
from zlang.ir.module import SpecializationBindingKind
from zlang.ir.types import FixedType, StructType, UIntType, VecType
from zlang.opt import lower, restore
from zlang.semantic import SemanticError
from zlang.simulate import simulate_cycles


CALLABLE = """
fn add_as<type A,type B>(x:A,y:A)->B {
    extend<9>(x) + extend<9>(y)
}
fn apply2<type A,type B,operation:fn(A,A)->B>(x:A,y:A) {
    operation(x,y)
}
module Top { in x:u8 in y:u8 out z:u10
    z=apply2<A=u8,B=u10,operation=fn add_as<A=u8,B=u10>>(x,y)
}
"""


ROM = """
import std.storage.core

fn make_image<type T,N>() {
    generate(i in 0..N) extend<8>(i)
}

module ForwardRom<type T,N,IW,image:vec<N,T>,producer:fn()->vec<N,T>>
where N >= 2 && is_power_of_two(N) && IW == floor_log2(N) {
    clock clk reset rst
    in address:uint<IW>
    out direct:T
    out generated:T
    inst a:StorageRom<T=T,N=N,IW=IW,image=image>
    inst b:StorageGeneratedRom<T=T,N=N,IW=IW,producer=fn producer>
    a.address=address
    b.address=address
    direct=a.data
    generated=b.data
}

module Top { clock clk reset rst
    in address:u3
    out direct:u8
    out generated:u8
    image:vec<8,u8>=generate(i in 0..8) extend<8>(i)
    inst bank:ForwardRom<
        T=u8,N=8,IW=3,image=image,
        producer=fn make_image<T=u8,N=8>
    >
    bank.address=address
    direct=bank.direct
    generated=bank.generated
}
"""


def _compile(source: str):
    return compile_source(source, include_clash=False).ir


def test_callable_parameter_lowers_to_exact_concrete_calls() -> None:
    module = _compile(CALLABLE)
    outer = module.assignments[0].expression
    assert isinstance(outer, expr.Call)
    definition = next(
        item for item in module.callable_definitions
        if item.callee_identity == outer.callee_identity
    )
    assert isinstance(definition.body, expr.Call)
    target = next(item for item in module.callable_definitions if item is not definition)
    assert definition.body.callee_identity == target.callee_identity
    assert definition.body.type == definition.return_type
    binding = next(
        item for item in module.generic_specializations
        if item.name == "apply2"
    ).bindings[0]
    assert binding.kind is SpecializationBindingKind.CALLABLE
    assert binding.parameter_types == (target.parameters[0].type, target.parameters[1].type)
    assert binding.return_type == target.return_type
    assert binding.callee_identity == target.callee_identity
    assert binding.callee_identity == definition.body.callee_identity
    assert binding.evaluator_schema == "zlang-ct-v1"
    assert restore(lower(module)) == module


def test_typed_constant_parameter_on_generic_function_is_hashed_and_exact() -> None:
    module = _compile("""
        fn pick<type T,N,image:vec<N,T>>(index:u1) { image[index] }
        module Top { in index:u1 out y:u8
          image:vec<2,u8>=generate(i in 0..2) extend<8>(i)
          y=pick<T=u8,N=2,image=image>(index) }
    """)
    record = module.generic_specializations[0]
    assert record.arguments[2][0] == "image"
    assert record.arguments[2][1].startswith("constant:")
    binding = record.bindings[0]
    assert binding.kind is SpecializationBindingKind.CONSTANT
    assert binding.canonical_type == VecType(2, UIntType(8))
    assert binding.canonical_value == (
        "vec", "vec<2,u8>", (("scalar", "u8", 0), ("scalar", "u8", 1))
    )
    assert binding.content_hash in record.arguments[2][1]
    assert restore(lower(module)) == module


def test_generic_rom_direct_and_forwarded_images_are_concrete() -> None:
    module = _compile(ROM)
    forward = module.children[0]
    assert tuple(item.name for item in forward.instances) == ("a", "b")
    direct, generated = forward.children
    assert tuple(word.value for word in direct.roms[0].contents) == tuple(range(8))
    assert tuple(word.value for word in generated.roms[0].contents) == tuple(range(8))
    assert direct.roms[0].content_hash == generated.roms[0].content_hash
    assert restore(lower(module)) == module

    outputs = simulate_cycles(
        module,
        ({"address": 3}, {"address": 5}, {"address": 0}),
        (True, False, False),
    )
    assert outputs == [
        {"direct": 0, "generated": 0},
        {"direct": 0, "generated": 0},
        {"direct": 5, "generated": 5},
    ]


def test_generic_rom_artifacts_are_deterministic_and_publish_companions() -> None:
    module = _compile(ROM)
    first = emit_sv_artifact(module)
    second = emit_sv_artifact(module)
    assert first.text == second.text
    assert first.artifact_hash == second.artifact_hash
    assert first.companions == second.companions
    assert first.companions
    assert all(item.object_kind == "rom_image" for item in first.companions)


@pytest.mark.parametrize(
    "source,match",
    (
        (
            """fn f<type T,c:vec<2,T>>(x:T){x}
               module Top{in x:u8 out y:u8
                 y=f<T=u8,c=x>(x)}""",
            "not an immutable compile-time value",
        ),
        (
            """fn f<type T,c:vec<2,T>>(x:T){x}
               module Top{in x:u8 out y:u8
                 c:vec<2,u9>=generate(i in 0..2) extend<9>(i)
                 y=f<T=u8,c=c>(x)}""",
            "expected exact vec<2,u8>",
        ),
        (
            """fn id(x:u8)->u8{x}
               fn f<type T,op:fn(T)->T>(x:T){op(x)}
               module Top{in x:u8 out y:u8 y=f<u8,fn id>(x)}""",
            "requires a named specialization argument",
        ),
        (
            """fn id(x:u8)->u8{x}
               fn f<type T,op:fn(T)->T>(x:T){op(x)}
               module Top{in x:u8 out y:u8 y=f<T=u8>(x)}""",
            "missing required compile-time callable argument 'op'",
        ),
        (
            """fn id(x:u8)->u8{x}
               fn f<type T,op:fn(T)->T>(x:T){op(x)}
               module Top{in x:u8 out y:u8
                 y=f<T=u8,op=fn id,op=fn id>(x)}""",
            "parameter 'op' is assigned more than once",
        ),
        (
            """fn f<type T,op:fn(T)->T>(x:T){op(x)}
               module Top{in x:u8 out y:u8 y=f<T=u8,op=x>(x)}""",
            "requires an explicit 'fn name' reference",
        ),
        (
            """module Top<c:u8>{in c:u8 out y:u8 y=c}""",
            "conflicts with compile-time module constant parameter 'c'",
        ),
        (
            """module Top<op:fn(u8)->u8>{in op:u8 out y:u8 y=op}""",
            "conflicts with compile-time module callable parameter 'op'",
        ),
    ),
)
def test_compile_time_parameter_fail_closed_diagnostics(source: str, match: str) -> None:
    with pytest.raises(SemanticError, match=match):
        _compile(source)


def test_two_rom_images_have_distinct_specialization_and_rom_identity() -> None:
    module = _compile("""
        module R<type T,N,IW,image:vec<N,T>> { clock clk reset rst
          in a:uint<IW> out y:T
          rom r:rom<T,N>{read_latency 1 init image}
          r.read_address=a y=r.read_data }
        module Top { clock clk reset rst in a:u1 out y0:u8 out y1:u8
          zero:vec<2,u8>=generate(i in 0..2) 0
          one:vec<2,u8>=generate(i in 0..2) 1
          inst r0:R<T=u8,N=2,IW=1,image=zero>
          inst r1:R<T=u8,N=2,IW=1,image=one>
          r0.a=a r1.a=a y0=r0.y y1=r1.y }
    """)
    assert module.elaborated_instances[0].specialization_identity != module.elaborated_instances[1].specialization_identity
    assert module.children[0].roms[0].semantic_id != module.children[1].roms[0].semantic_id
    assert module.children[0].roms[0].content_hash != module.children[1].roms[0].content_hash


def test_same_generic_rom_binding_deduplicates_specialization_and_companion() -> None:
    module = _compile("""
        import std.storage.core
        module Top { clock clk reset rst in a:u1 out y0:u8 out y1:u8
          image:vec<2,u8>=generate(i in 0..2) i
          inst r0:StorageRom<T=u8,N=2,IW=1,image=image>
          inst r1:StorageRom<T=u8,N=2,IW=1,image=image>
          r0.address=a r1.address=a y0=r0.data y1=r1.data }
    """)
    assert (
        module.elaborated_instances[0].specialization_identity
        == module.elaborated_instances[1].specialization_identity
    )
    assert module.children[0].specialization_bindings == module.children[1].specialization_bindings
    assert len(emit_sv_artifact(module).companions) == 1


@pytest.mark.parametrize(
    ("source", "expected_type"),
    (
        (
            """import std.storage.core
            module Top { clock clk reset rst in a:u1 out y:SF2.2
              image:vec<2,SF2.2>=generate(i in 0..2) 1.5
              inst r:StorageRom<T=SF2.2,N=2,IW=1,image=image>
              r.address=a y=r.data }""",
            FixedType(4, 2),
        ),
        (
            """import std.storage.core
            struct Pair { hi:u4 lo:u4 }
            module Top { clock clk reset rst in a:u1 out y:Pair
              image:vec<2,Pair>=generate(i in 0..2) Pair{hi=i lo=i}
              inst r:StorageRom<T=Pair,N=2,IW=1,image=image>
              r.address=a y=r.data }""",
            "Pair",
        ),
        (
            """import std.storage.core
            module Top { clock clk reset rst in a:u1 out y:vec<2,u4>
              image:vec<2,vec<2,u4>>=generate(i in 0..2)
                generate(j in 0..2) j
              inst r:StorageRom<T=vec<2,u4>,N=2,IW=1,image=image>
              r.address=a y=r.data }""",
            VecType(2, UIntType(4)),
        ),
    ),
)
def test_generic_rom_images_preserve_fixed_struct_and_nested_vector_types(
    source: str, expected_type: object
) -> None:
    module = _compile(source)
    actual = module.children[0].roms[0].element_type
    if expected_type == "Pair":
        assert isinstance(actual, StructType) and actual.name == "Pair"
    else:
        assert actual == expected_type
    assert module.children[0].specialization_bindings[0].canonical_type == VecType(2, actual)
    assert restore(lower(module)) == module


def test_canonical_rejects_specialization_binding_argument_corruption() -> None:
    canonical = lower(_compile("""
        fn pick<type T,N,image:vec<N,T>>(index:u1) { image[index] }
        module Top { in index:u1 out y:u8
          image:vec<2,u8>=generate(i in 0..2) i
          y=pick<T=u8,N=2,image=image>(index) }
    """))
    record = canonical.generic_specializations[0]
    binding = record.bindings[0]
    changed_value = (
        "vec", "vec<2,u8>", (("scalar", "u8", 7), ("scalar", "u8", 7))
    )
    changed_hash = stable_digest({
        "schema": binding.evaluator_schema,
        "kind": binding.kind.value,
        "type": str(binding.canonical_type),
        "value": changed_value,
        "dependencies": binding.dependency_identity,
    })
    changed = replace(
        binding,
        canonical_value=changed_value,
        content_hash=changed_hash,
    )
    with pytest.raises(ValueError, match="binding identity is inconsistent"):
        replace(
            canonical,
            generic_specializations=(replace(record, bindings=(changed,)),),
        )


def test_canonical_binding_identity_rejects_coordinated_value_hash_and_argument_mutation() -> None:
    canonical = lower(_compile("""
        fn pick<type T,N,image:vec<N,T>>(index:u1) { image[index] }
        module Top { in index:u1 out y:u8
          image:vec<2,u8>=generate(i in 0..2) i
          y=pick<T=u8,N=2,image=image>(index) }
    """))
    record = canonical.generic_specializations[0]
    binding = record.bindings[0]
    changed_value = (
        "vec", "vec<2,u8>", (("scalar", "u8", 6), ("scalar", "u8", 7))
    )
    changed_hash = stable_digest({
        "schema": binding.evaluator_schema,
        "kind": binding.kind.value,
        "type": str(binding.canonical_type),
        "value": changed_value,
        "dependencies": binding.dependency_identity,
    })
    changed = replace(
        binding,
        canonical_value=changed_value,
        content_hash=changed_hash,
    )
    arguments = tuple(
        (name, f"constant:{changed_hash}" if name == binding.name else value)
        for name, value in record.arguments
    )
    with pytest.raises(ValueError, match="binding identity is inconsistent"):
        replace(record, arguments=arguments, bindings=(changed,))


def test_unused_constant_binding_still_changes_semantic_and_artifact_identity() -> None:
    template = """
        fn identity_with_image<type T,N,image:vec<N,T>>(x:T) { x }
        module Top { in x:u8 out y:u8
          image:vec<2,u8>=generate(i in 0..2) VALUE
          y=identity_with_image<T=u8,N=2,image=image>(x) }
    """
    first = compile_source(template.replace("VALUE", "0"), include_clash=False)
    changed = compile_source(template.replace("VALUE", "1"), include_clash=False)
    assert first.high_level_ir_identity != changed.high_level_ir_identity
    assert first.selected_ir_identity != changed.selected_ir_identity
    first_artifact = emit_sv_artifact(
        first.ir, selected_ir_identity=first.selected_ir_identity
    )
    changed_artifact = emit_sv_artifact(
        changed.ir, selected_ir_identity=changed.selected_ir_identity
    )
    assert first_artifact.artifact_hash != changed_artifact.artifact_hash


def test_recursive_cycle_through_callable_parameter_is_rejected() -> None:
    with pytest.raises(
        SemanticError,
        match="recursive generic specialization cycle for 'recursive'",
    ):
        _compile("""
            fn invoke<type T,op:fn(T)->T>(x:T) { op(x) }
            fn recursive<type T>(x:T) {
                invoke<T=T,op=fn recursive<T=T>>(x)
            }
            module Top { in x:u8 out y:u8 y=recursive<T=u8>(x) }
        """)


@pytest.mark.parametrize(
    ("target", "reference", "message"),
    (
        (
            "fn id<type A>(x:A){x}",
            "fn id<B=u8>",
            "unknown or excess generic argument 'B'",
        ),
        (
            "fn missing<type T,type U>(x:T){x}",
            "fn missing<T=u8>",
            "cannot infer type parameter 'U'",
        ),
        (
            "fn bad<type A>(x:A)->A{extend<9>(x)}",
            "fn bad<A=u8>",
            "returns u9, expected u8",
        ),
    ),
)
def test_unused_generic_callable_actual_is_fully_validated_at_binding(
    target: str, reference: str, message: str
) -> None:
    with pytest.raises(SemanticError, match=message):
        _compile(f"""
            {target}
            fn hold<type T,op:fn(T)->T>(x:T) {{ x }}
            module Top {{ in x:u8 out y:u8
              y=hold<T=u8,op={reference}>(x) }}
        """)


def test_unused_valid_generic_callable_publishes_one_exact_definition() -> None:
    module = _compile("""
        fn id<type A>(x:A) { x }
        fn hold<type T,op:fn(T)->T>(x:T) { x }
        module Top { in x:u8 out y0:u8 out y1:u8
          y0=hold<T=u8,op=fn id<A=u8>>(x)
          y1=hold<T=u8,op=fn id<A=u8>>(x) }
    """)
    id_definitions = tuple(
        item for item in module.callable_definitions
        if item.metadata is not None and item.metadata.source_name == "id"
    )
    assert len(id_definitions) == 1
    hold_record = next(
        item for item in module.generic_specializations if item.name == "hold"
    )
    binding = hold_record.bindings[0]
    assert binding.callee_identity == id_definitions[0].callee_identity
    assert binding.parameter_types == tuple(
        item.type for item in id_definitions[0].parameters
    )
    assert binding.return_type == id_definitions[0].return_type
    assert restore(lower(module)) == module
