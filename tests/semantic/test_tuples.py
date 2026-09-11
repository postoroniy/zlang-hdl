from __future__ import annotations

from dataclasses import replace

import pytest

from zlang.compiler import compile_source
from zlang.ir import expressions as expr
from zlang.ir.types import BitType, SIntType, TupleType, UIntType, VecType
from zlang.opt import EGraphAdapterError, canonical_to_egraph
from zlang.opt import lower, restore
from zlang.semantic import SemanticError
from zlang.simulate import simulate, simulate_cycles


def _compile(source: str):
    return compile_source(source, include_clash=False).ir


def test_tuple_context_inference_projection_equality_and_msb_packing() -> None:
    module = _compile(
        "module TupleValues { in p:(u8,bit) out contextual:(u8,s8) "
        "out swapped:(bit,u8) out same:bit out raw:bits<9> "
        "contextual=(1,-1) swapped=(p[1],p[0]) same=p==p raw=pack(p) }"
    )

    assert module.outputs[0].type == TupleType((UIntType(8), SIntType(8)))
    assert module.outputs[1].type == TupleType((BitType(), UIntType(8)))
    result = simulate(module, p=(0xA5, 1))
    assert result == {
        "contextual": (1, -1),
        "swapped": (1, 0xA5),
        "same": 1,
        "raw": 0x14B,
    }


def test_uncontextual_tuple_literal_keeps_each_minimum_literal_type() -> None:
    module = _compile(
        "fn inferred(){(1,-1)} module Top{out y:(u1,s1) y=inferred()}"
    )
    definition = module.functions[0]
    assert definition.return_type == TupleType((UIntType(1), SIntType(1)))


@pytest.mark.parametrize("arity", range(2, 9))
def test_every_supported_tuple_arity_has_exact_order_and_packing(arity: int) -> None:
    type_text = f"({','.join('u8' for _ in range(arity))})"
    width = arity * 8
    module = _compile(
        f"module TupleArity {{ in value:{type_text} out raw:bits<{width}> "
        "raw=pack(value) }"
    )
    value = tuple(range(1, arity + 1))
    expected = 0
    for item in value:
        expected = (expected << 8) | item
    assert module.inputs[0].type == TupleType(tuple(UIntType(8) for _ in value))
    assert simulate(module, value=value) == {"raw": expected}


def test_unpack_restores_an_exact_structural_tuple() -> None:
    module = _compile(
        "module TupleUnpack { in raw:bits<9> out value:(u8,bit) "
        "value=unpack<(u8,bit)>(raw) }"
    )
    assert simulate(module, raw=0x14B) == {"value": (0xA5, 1)}
    assert restore(lower(module)) == module


def test_generic_tuple_function_and_nested_generic_type_specialize_exactly() -> None:
    module = _compile(
        "struct Box<type T>{value:T} "
        "fn swap<type A,type B>(p:(A,B)){(p[1],p[0])} "
        "module Top { in p:Box<(u8,bit)> out y:(bit,u8) y=swap(p.value) }"
    )

    assert module.outputs[0].type == TupleType((BitType(), UIntType(8)))
    definition = module.callable_definitions[0]
    assert definition.parameters[0].type == TupleType((UIntType(8), BitType()))
    assert definition.return_type == module.outputs[0].type


def test_tuple_is_an_exact_typed_compile_time_constant_parameter() -> None:
    module = _compile(
        "module Holder<value:(u8,bit)>{out y:(u8,bit) y=value} "
        "module Top{out y:(u8,bit) value:(u8,bit)=(7,1) "
        "holder:Holder<value=value> y=holder.y}"
    )

    assert simulate(module) == {"y": (7, 1)}
    assert restore(lower(module)) == module


def test_module_and_callable_destructuring_are_flat_immutable_aliases() -> None:
    module = _compile(
        "fn flip(p:(u8,bit)){\n(data,last)=p\n(last,data)\n} "
        "module Top { in p:(u8,bit) out data:u8 out last:bit out flipped:(bit,u8) "
        "(x,y)=p data=x last=y flipped=flip(p) }"
    )

    data, last, _ = module.assignments
    assert isinstance(data.expression, expr.TupleProject)
    assert isinstance(last.expression, expr.TupleProject)
    assert data.expression.expression is last.expression.expression
    body = module.functions[0].body
    assert isinstance(body, expr.TupleConstruct)
    assert all(isinstance(item, expr.TupleProject) for item in body.elements)


def test_tuple_storage_protocol_and_string_memory_use_existing_generic_models() -> None:
    register = _compile(
        "module RegTuple { clock clk reset rst in e:bit in p:(u8,bit) "
        "out q:(u8,bit) reg value:(u8,bit)=(0,0) "
        "when e { value <- p } q=value }"
    )
    fifo = _compile(
        "module FifoTuple { clock clk reset rst in i:rv<(u8,bit)> "
        "out o:rv<(u8,bit)> fifo q:fifo<(u8,bit),2> "
        "q.data=i.payload q.push=i.transfer q.pop=o.transfer "
        "i.ready=q.ready o.payload=q.front o.valid=q.valid }"
    )
    memory = _compile(
        "module MemoryText { clock clk reset rst in ra:u1 in we:bit in wa:u1 "
        "in wd:string<2> out rd:string<2> "
        "memory table:mem<string<2>,2>{read_latency 1 collision read_first} "
        "table.read_address=ra table.write_enable=we table.write_address=wa "
        "table.write_data=wd rd=table.read_data }"
    )
    tuple_memory = _compile(
        "module MemoryTuple { clock clk reset rst in ra:u1 in we:bit in wa:u1 "
        "in wd:(u8,bit) out rd:(u8,bit) "
        "memory table:mem<(u8,bit),2>{read_latency 1 collision read_first} "
        "table.read_address=ra table.write_enable=we table.write_address=wa "
        "table.write_data=wd rd=table.read_data }"
    )
    rom = _compile(
        "module RomTuple { clock clk reset rst in a:u1 out q:(u8,bit) "
        "rom table:rom<(u8,bit),2>{read_latency 1 init [(1,0),(2,1)]} "
        "table.read_address=a q=table.read_data }"
    )

    assert register.registers[0].type == TupleType((UIntType(8), BitType()))
    assert fifo.fifos[0].element_type == register.registers[0].type
    assert memory.memories[0].element_type == VecType(2, UIntType(8))
    assert tuple_memory.memories[0].element_type == register.registers[0].type
    assert rom.roms[0].element_type == register.registers[0].type
    trace = simulate_cycles(
        memory,
        [
            {"ra": 0, "we": 0, "wa": 0, "wd": [0, 0]},
            {"ra": 1, "we": 1, "wa": 1, "wd": [79, 75]},
            {"ra": 1, "we": 0, "wa": 0, "wd": [0, 0]},
            {"ra": 0, "we": 0, "wa": 0, "wd": [0, 0]},
        ],
        reset=[True, False, False, False],
    )
    assert [item["rd"] for item in trace] == [
        [0, 0], [0, 0], [0, 0], [79, 75]
    ]
    tuple_memory_trace = simulate_cycles(
        tuple_memory,
        [
            {"ra": 0, "we": 0, "wa": 0, "wd": (0, 0)},
            {"ra": 1, "we": 1, "wa": 1, "wd": (0xA5, 1)},
            {"ra": 1, "we": 0, "wa": 0, "wd": (0, 0)},
            {"ra": 0, "we": 0, "wa": 0, "wd": (0, 0)},
        ],
        reset=[True, False, False, False],
    )
    assert [item["rd"] for item in tuple_memory_trace] == [
        (0, 0), (0, 0), (0, 0), (0xA5, 1)
    ]
    rom_trace = simulate_cycles(
        rom,
        [{"a": 0}, {"a": 1}, {"a": 0}, {"a": 0}],
        reset=[True, False, False, False],
    )
    assert [item["q"] for item in rom_trace] == [
        (0, 0), (0, 0), (2, 1), (1, 0)
    ]
    register_trace = simulate_cycles(
        register,
        [
            {"e": 0, "p": (0, 0)},
            {"e": 1, "p": (0xA5, 1)},
            {"e": 0, "p": (0, 0)},
        ],
        reset=[True, False, False],
    )
    assert [item["q"] for item in register_trace] == [
        (0, 0), (0, 0), (0xA5, 1)
    ]
    fifo_trace = simulate_cycles(
        fifo,
        [
            {"i": {"payload": (0, 0), "valid": 0}, "o": {"ready": 0}},
            {"i": {"payload": (7, 1), "valid": 1}, "o": {"ready": 0}},
            {"i": {"payload": (0, 0), "valid": 0}, "o": {"ready": 0}},
            {"i": {"payload": (0, 0), "valid": 0}, "o": {"ready": 1}},
        ],
        reset=[True, False, False, False],
    )
    assert fifo_trace[2]["o"]["payload"] == (7, 1)
    assert fifo_trace[2]["o"]["valid"] == 1


def test_tuple_fixed_delay_and_pipeline_use_recursive_zero_reset_values() -> None:
    module = _compile(
        "module TupleStages { clock clk reset rst in x:(u8,bit) "
        "out delayed:(u8,bit) out piped:(u8,bit) "
        "delayed=delay<2>(x) piped=pipeline(2){x} }"
    )
    assert isinstance(module.assignments[0].expression, expr.Delay)
    assert isinstance(module.assignments[1].expression, expr.Pipeline)
    assert module.assignments[0].expression.type == TupleType(
        (UIntType(8), BitType())
    )
    trace = simulate_cycles(
        module,
        [
            {"x": (1, 0)},
            {"x": (2, 1)},
            {"x": (3, 0)},
            {"x": (4, 1)},
            {"x": (5, 0)},
        ],
        reset=[True, False, False, False, False],
    )
    assert [(item["delayed"], item["piped"]) for item in trace] == [
        ((0, 0), (0, 0)),
        ((0, 0), (0, 0)),
        ((0, 0), (0, 0)),
        ((2, 1), (2, 1)),
        ((3, 0), (3, 0)),
    ]


def test_in_order_request_response_accepts_exact_tuple_payloads() -> None:
    module = _compile(
        "module TupleRR { clock c reset r "
        "interface mem:request_response<(u8,bit),(bit,u8)>{"
        "max_outstanding 1 ordering in_order} "
        "in req:(u8,bit) in issue:bit in consume:bit out rsp:(bit,u8) "
        "mem.request.payload=req mem.request.valid=issue "
        "mem.response.ready=consume rsp=mem.response.payload }"
    )
    interface = module.request_responses[0]
    assert interface.request_type == TupleType((UIntType(8), BitType()))
    assert interface.response_type == TupleType((BitType(), UIntType(8)))
    assert restore(lower(module)) == module


@pytest.mark.parametrize(
    ("source", "message"),
    (
        (
            "module M{in p:(u8,bit) in i:u1 out y:u8 y=p[i]}",
            "tuple projection requires a zero-based integer literal index",
        ),
        (
            "module M<I=0>{in p:(u8,bit) out y:u8 y=p[I]}",
            "tuple projection requires a zero-based integer literal index",
        ),
        (
            "module M{in p:(u8,bit) out y:u8 y=p[2]}",
            "tuple index 2 is out of range",
        ),
        (
            "module M{in p:(u8,bit) out y:u8 (a,b,c)=p y=a}",
            "tuple destructuring has 3 bindings",
        ),
        (
            "module M{in p:(u8,bit) out y:u8 a:u8=0 (a,b)=p y=a}",
            "tuple binding 'a' shadows an existing symbol",
        ),
        (
            "module M{in p:(u8,bit) out y:u8 (a,a)=p y=a}",
            "tuple destructuring repeats binding 'a'",
        ),
        (
            "module M{in p:(u8,bit) out y:u2 y=length(p)}",
            r"length\(\.\.\.\) requires a concrete vec<N,T>",
        ),
        (
            "module M{in p:(u8,bit) out q:(u8,bit) q=p+p}",
            r"addition is not defined for \(u8,bit\)",
        ),
        (
            "module M{in p:(u8,bit) in q:(u8,bit) out y:bit y=p<q}",
            r"comparison is not defined for \(u8,bit\)",
        ),
        (
            "operator + (a:(u8,bit),b:(u8,bit))->(u8,bit){a} "
            "module M{out y:u1 y=0}",
            "must be owned by a nominal struct operand",
        ),
        (
            "enum E{A B} module M{out y:bits<9> "
            "p:(u8,E)=(1,E.A) y=pack(p)}",
            "pack requires a recursively bit-packable non-enum value",
        ),
    ),
)
def test_tuple_unsupported_or_ambiguous_operations_fail_closed(
    source: str, message: str
) -> None:
    with pytest.raises(SemanticError, match=message):
        _compile(source)


@pytest.mark.parametrize("operator", ("+", "-", "*"))
def test_tuple_operator_declarations_are_never_overloadable(operator: str) -> None:
    parameters = (
        "a:(u8,bit)"
        if operator == "-"
        else "a:(u8,bit),b:(u8,bit)"
    )
    with pytest.raises(
        SemanticError,
        match="must be owned by a nominal struct operand",
    ):
        _compile(
            f"operator {operator} ({parameters})->(u8,bit){{a}} "
            "module M{out y:u1 y=0}"
        )


@pytest.mark.parametrize(
    "source",
    (
        "module M{in p:(u8,bit) out y:(u8,bit) y=p}",
        "module M{in p:(u8,bit) out y:u8 y=p[0]}",
    ),
)
def test_m26_scalar_egraph_rejects_tuple_roots_and_scalar_projections(
    source: str,
) -> None:
    result = compile_source(source, include_clash=False)
    root = result.optimization_ir.assignments[0].expression
    with pytest.raises(
        EGraphAdapterError,
        match=(
            "scalar hardware types only|"
            "outside the exact scalar e-graph operation set"
        ),
    ):
        canonical_to_egraph(result.optimization_ir, root)


def test_tuple_contract_access_fails_before_formal_lowering() -> None:
    with pytest.raises(
        SemanticError,
        match="does not yet support aggregate access in generated SVA",
    ):
        _compile(
            "module M{clock c reset r in p:(u8,bit) "
            "guarantee g @ c disable iff r { p == p }}"
        )


def test_tuple_construct_and_projection_origins_survive_canonical_round_trip() -> None:
    result = compile_source(
        "module Origins {\n"
        "  in p : (u8,bit)\n"
        "  out pair : (bit,u8)\n"
        "  pair = (p[1],p[0])\n"
        "}",
        include_clash=False,
    )
    expression = result.ir.assignments[0].expression
    assert isinstance(expression, expr.TupleConstruct)
    assert expression.origin is not None
    assert expression.origin.span.start_line == 4
    assert all(
        isinstance(item, expr.TupleProject) and item.origin is not None
        for item in expression.elements
    )
    restored = restore(lower(result.ir))
    assert restored.assignments[0].expression.origin == expression.origin
    assert tuple(
        item.origin for item in restored.assignments[0].expression.elements
    ) == tuple(item.origin for item in expression.elements)


def test_canonical_tuple_callable_signature_corruption_is_rejected() -> None:
    canonical = lower(
        _compile(
            "fn flip(p:(u8,bit))->(bit,u8){(p[1],p[0])} "
            "module Top{in p:(u8,bit) out q:(bit,u8) q=flip(p)}"
        )
    )
    function = canonical.functions[0]
    with pytest.raises(ValueError, match="callee identity does not match"):
        replace(
            function,
            return_type=TupleType((UIntType(8), BitType())),
        )
